"""The print queue behind the web Print screen: the same PrintCore pipeline as the
desktop app (stage -> convert -> inspect), fed by browser uploads and by
Library › Print, with two ways out: a merged PDF to download, or a job sent to
a printer attached to this machine."""
from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from datetime import datetime

from mailtool.core.util import log, sanitize_for_path
from mailtool.printing import pdfops
from mailtool.printing.backends import get_backend
from mailtool.printing.pdfops import UserError
from mailtool.printing.queue import ACTIVE, Item, PrintCore
from mailtool.printing.resolve import expand_folder, path_source
from mailtool.web.state import ApiError

PENDING = ("queued", "staging", "converting")
CHUNK = 1024 * 1024


class PrintService:
    def __init__(self, state):
        self.state = state
        self.pcfg = state.cfg["print"]
        self.core = PrintCore(self.pcfg, self._core_event, hint=state.hint)
        self.backend = get_backend(self.pcfg)
        self.lock = threading.RLock()
        self.items = {}           # id -> Item
        self.order = []           # ids, print order
        self.uploads = {}         # id -> upload folder to delete with the item
        self.upload_root = os.path.join(state.work_dir, "uploads")
        os.makedirs(self.upload_root, exist_ok=True)
        self.busy = None          # "print" | "download" while a job uses the queue
        self.busy_job = None
        self.job_ids = []
        self._printers = None     # (names, default, time)

    # ------------------------------------------------------------------ state for the browser
    def item_json(self, it):
        return {"id": it.id, "name": it.display, "status": it.status, "msg": it.msg, "pages": it.pages,
                "pages_text": it.pages_text(), "copies": it.copies, "excluded": sorted(it.excluded),
                "page_order": list(it.page_order) if it.page_order else None}

    def snapshot(self):
        with self.lock:
            items = [self.item_json(self.items[i]) for i in self.order if i in self.items]
        return {"items": items, "busy": self.busy, "job": self.busy_job.id if self.busy_job else None,
                "can_print": self.backend is not None, "backend": self.backend.name if self.backend else None,
                "print_hint": "" if self.backend else self.state.hint("print"),
                "merge": bool(self.pcfg.get("merge_batch", True)), "clear_after": bool(self.pcfg.get("clear_after")),
                "printer": self.pcfg.get("printer")}

    def _changed(self):
        self.state.hub.publish({"type": "queue", "queue": self.snapshot()})

    def _core_event(self, ev):
        kind, it = ev
        if kind == "dup":
            self._drop(it)
            self.state.log("%s: already in the queue (same content as %s)" % (it.display, it.msg), prefix="Print")
        elif it.status == "failed" and not it.noted:
            it.noted = True
            self.state.log("%s: %s" % (it.display, it.msg), "error", prefix="Print")
        self._changed()

    # ------------------------------------------------------------------ adding
    def _check_idle(self):
        if self.busy:
            raise ApiError(409, "The queue is busy (%s) - wait for it to finish or cancel it." % self.busy)

    def add_upload(self, name, rfile, length, pos=None):
        """Store an uploaded file and queue it. Returns the item."""
        self._check_idle()
        if length is None or length < 0:
            raise ApiError(411, "Content-Length is required.")
        if length > self.state.max_upload:
            raise ApiError(413, "File too large (limit %d MB)." % (self.state.max_upload // (1024 * 1024)))
        name = clean_name(name)
        d = os.path.join(self.upload_root, uuid.uuid4().hex)
        os.makedirs(d)
        path = os.path.join(d, name)
        try:
            receive(rfile, length, path)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        it = self._add(path_source(path), pos)
        if it is None:
            shutil.rmtree(d, ignore_errors=True)
            return None
        with self.lock:
            self.uploads[it.id] = d
        return it

    def add_paths(self, paths, pos=None):
        """Files or folders on this machine (Library › Print). Returns how many were queued."""
        self._check_idle()
        added = 0
        for p in paths:
            if os.path.isdir(p):
                files = expand_folder(p)
            else:
                files = [p]
            for f in files:
                if self._add(path_source(f), None if pos is None else pos + added, notify=False):
                    added += 1
        self._changed()
        return added

    def _add(self, src, pos, notify=True):
        with self.lock:
            if any(self.items[i].src.key == src.key for i in self.order):
                self.state.log("%s: already in the queue" % src.display, prefix="Print")
                return None
            it = Item(src)
            self.items[it.id] = it
            if pos is None or pos >= len(self.order):
                self.order.append(it.id)
            else:
                self.order.insert(max(0, int(pos)), it.id)
        self.core.submit(it)
        if notify:
            self._changed()
        return it

    # ------------------------------------------------------------------ editing
    def get(self, iid):
        with self.lock:
            it = self.items.get(iid)
        if it is None:
            raise ApiError(404, "That file is no longer in the queue.")
        return it

    def _drop(self, it):
        with self.lock:
            self.items.pop(it.id, None)
            if it.id in self.order:
                self.order.remove(it.id)
            d = self.uploads.pop(it.id, None)
        self.core.forget(it)
        if d:
            shutil.rmtree(d, ignore_errors=True)

    def remove(self, iid):
        self._check_idle()
        self._drop(self.get(iid))
        self._changed()

    def clear(self, only_done=False):
        self._check_idle()
        with self.lock:
            its = [self.items[i] for i in self.order if not only_done or self.items[i].status == "done"]
        for it in its:
            self._drop(it)
        if not only_done:
            self.state.files.drop_group("print")
        self._changed()
        return len(its)

    def reorder(self, ids):
        self._check_idle()
        with self.lock:
            known = [i for i in ids if i in self.items]
            rest = [i for i in self.order if i not in known]
            self.order = known + rest
        self._changed()

    def sort_names(self):
        self._check_idle()
        with self.lock:
            self.order.sort(key=lambda i: self.items[i].display.lower())
        self._changed()

    def update(self, iid, data):
        self._check_idle()
        it = self.get(iid)
        if "copies" in data:
            try:
                it.copies = max(1, min(99, int(data["copies"])))
            except (TypeError, ValueError):
                raise ApiError(400, "Copies must be a number from 1 to 99.")
        n = it.pages or 0
        if "excluded" in data:
            try:
                ex = {int(x) for x in data["excluded"] or []}
            except (TypeError, ValueError):
                raise ApiError(400, "Bad page list.")
            if n and len([x for x in ex if 0 <= x < n]) >= n:
                raise ApiError(400, "At least one page has to stay in.")
            it.excluded = {x for x in ex if 0 <= x < n}
        if "page_order" in data:
            po = data["page_order"]
            if po:
                try:
                    po = [int(x) for x in po]
                except (TypeError, ValueError):
                    raise ApiError(400, "Bad page order.")
                if sorted(po) != list(range(n)):
                    raise ApiError(400, "The page order must list every page once.")
                it.page_order = None if po == sorted(po) else po
            else:
                it.page_order = None
        self._changed()
        return self.item_json(it)

    def thumb(self, iid, page, width):
        it = self.get(iid)
        if it.status not in ("ready", "done", "printing") or not it.pdf:
            raise ApiError(409, "Not ready yet.")
        if not (0 <= page < (it.pages or 0)):
            raise ApiError(404, "No such page.")
        width = max(60, min(1600, int(width)))
        out = os.path.join(it.dir, "thumbs", "w%d_p%04d.png" % (width, page))
        if not os.path.isfile(out):
            try:
                pdfops.render_page(it.pdf, page, out, width, int(self.pcfg.get("convert_timeout") or 180))
            except UserError as e:
                raise ApiError(500, str(e))
        return out

    def item_pdf(self, iid):
        """The PDF as it would print (excluded pages removed, custom order applied)."""
        it = self.get(iid)
        if it.status not in ("ready", "done") or not it.pdf:
            raise ApiError(409, "Not ready yet.")
        try:
            return self.core.effective_pdf(it), it
        except UserError as e:
            raise ApiError(400, str(e))

    # ------------------------------------------------------------------ settings
    def set_options(self, data):
        for k in ("merge_batch", "clear_after"):
            if k in data:
                self.pcfg[k] = bool(data[k])
        if "printer" in data:
            self.pcfg["printer"] = data["printer"] or None
        self._changed()

    def apply_settings(self):
        self.core.apply_settings()
        self.backend = get_backend(self.pcfg)
        self._printers = None
        self._changed()

    def printers(self, refresh=False):
        if not self.backend:
            return {"available": False, "names": [], "default": None, "hint": self.state.hint("print")}
        cached = self._printers
        if refresh or cached is None or time.time() - cached[2] > 60:
            try:
                names, default = self.backend.printers()
            except Exception as e:
                log.warning("printer list failed: %s", e)
                names, default = [], None
            cached = self._printers = (names, default, time.time())
        return {"available": True, "backend": self.backend.name, "names": cached[0], "default": cached[1],
                "selected": self.pcfg.get("printer")}

    # ------------------------------------------------------------------ output
    def _todo(self):
        with self.lock:
            return [self.items[i] for i in self.order if self.items[i].status in ACTIVE]

    def start_download(self):
        """Build one merged PDF of the queue for the browser to save. -> job"""
        self._check_idle()
        todo = self._todo()
        if not todo:
            raise ApiError(400, "Nothing in the queue to download.")
        return self._start("download", "Preparing a PDF of %d file(s)" % len(todo), self._download, todo=todo)

    def start_print(self, printer=None, merged=None):
        self._check_idle()
        if not self.backend:
            raise ApiError(409, "This server has no way to print. Install: %s" % self.state.hint("print"))
        todo = self._todo()
        if not todo:
            raise ApiError(400, "Nothing in the queue to print.")
        if merged is None:
            merged = bool(self.pcfg.get("merge_batch", True))
        self.pcfg["printer"] = printer or None
        self.job_ids = []
        fn = self._print_merged if merged else self._print_each
        return self._start("print", "Printing %d file(s)" % len(todo), fn, todo=todo, printer=printer or None)

    def _start(self, busy, title, fn, **kw):
        with self.lock:
            self._check_idle()
            self.busy = busy
        try:
            job = self.state.start_job("print", title, fn, on_done=self._done, **kw)
        except Exception:
            self.busy = None
            raise
        self.busy_job = job
        self._changed()
        return job

    @staticmethod
    def _wait_ready(job, todo, step=0.05):
        for it in todo:
            while it.status in PENDING and not job.cancelled:
                time.sleep(step)
            job.check()

    def _download(self, job, todo):
        self._wait_ready(job, todo)
        ready = [it for it in todo if it.status == "ready"]
        failed = [it.display for it in todo if it.status == "failed"]
        if not ready:
            raise RuntimeError("none of the files could be prepared")
        job.log("Merging %d file(s)…" % len(ready))
        mdir = os.path.join(self.core.root, "download-%d" % int(time.time() * 1000))
        try:
            out, engine = self.core.build_merged(ready, mdir)
        except Exception:
            shutil.rmtree(mdir, ignore_errors=True)
            raise
        job.check()
        name = "MailTool_%s.pdf" % datetime.now().strftime("%Y-%m-%d_%H%M%S")
        url = self.state.files.add(out, name, "application/pdf", group="print", cleanup=mdir)
        pages = pdfops.inspect_pdf(out)
        job.log("Merged %d file(s) with %s%s - ready to download." % (
            len(ready), engine, (", %d page(s)" % pages) if pages else ""), "ok")
        return {"files": len(ready), "failed": failed, "url": url, "name": name, "pages": pages}

    def _print_merged(self, job, todo, printer):
        self._wait_ready(job, todo)
        ready = [it for it in todo if it.status == "ready"]
        failed = [it.display for it in todo if it.status == "failed"]
        if not ready:
            return {"printed": 0, "failed": failed}
        for it in ready:
            self.core.set(it, "printing", "preparing")
        printed, mdir = 0, None
        try:
            job.log("Merging %d file(s)…" % len(ready))
            mdir = os.path.join(self.core.root, "merge-%d" % int(time.time() * 1000))
            out, engine = self.core.build_merged(ready, mdir)
            job.log("Merged %d file(s) with %s." % (len(ready), engine))
            if job.cancelled:
                for it in ready:
                    self.core.set(it, "ready")
                return {"printed": 0, "failed": failed}
            for it in ready:
                self.core.set(it, "printing", "submitting")
            jid = self.backend.submit(out, printer, 1, "MailTool batch (%d files)" % len(ready))
            if jid:
                self.job_ids.append(jid)
            for it in ready:
                self.core.set(it, "done", "sent in merged job%s" % ((" " + jid) if jid else ""))
                printed += 1
            job.log("Sent to %s%s." % (printer or "the default printer", (" as job " + jid) if jid else ""), "ok")
        except UserError as e:
            for it in ready:
                self.core.fail(it, str(e))
                failed.append(it.display)
        except Exception as e:
            log.exception("merged print failed")
            for it in ready:
                self.core.fail(it, "print error: %s" % e)
                failed.append(it.display)
        finally:
            if mdir:
                shutil.rmtree(mdir, ignore_errors=True)
        return {"printed": printed, "failed": failed}

    def _print_each(self, job, todo, printer):
        printed, failed = 0, []
        for n, it in enumerate(todo, 1):
            while it.status in PENDING and not job.cancelled:
                time.sleep(0.2)
            if job.cancelled:
                break
            job.progress(n - 1, len(todo), "printing ")
            if it.status != "ready":
                if it.status == "failed":
                    failed.append(it.display)
                continue
            self.core.set(it, "printing")
            try:
                jid = self.backend.submit(self.core.effective_pdf(it), printer, it.copies, it.display)
                if jid:
                    self.job_ids.append(jid)
                self.core.set(it, "done")
                printed += 1
            except UserError as e:
                self.core.fail(it, str(e))
                failed.append(it.display)
            except Exception as e:
                log.exception("print failed")
                self.core.fail(it, "print error: %s" % e)
                failed.append(it.display)
        job.progress(len(todo), len(todo), "printing ")
        return {"printed": printed, "failed": failed}

    def cancel(self):
        job = self.busy_job
        if job is not None:
            job.cancel()
        ids = list(self.job_ids)
        if self.backend and ids:
            threading.Thread(target=self.backend.cancel, args=(ids,), daemon=True).start()

    def _done(self, job):
        kind = self.busy
        self.busy, self.busy_job = None, None
        r = job.result or {}
        if kind == "download":
            job.web_result = dict(r, kind="download")
        else:
            r = {"printed": r.get("printed", 0), "failed": r.get("failed", [])}
            txt = "Printed %d file(s)." % r["printed"]
            if r["failed"]:
                txt += " Skipped/failed: %s." % ", ".join(r["failed"])
            if job.state == "cancelled":
                txt = "Cancelled. " + txt
                with self.lock:
                    its = list(self.items.values())
                for it in its:
                    if it.status == "printing":
                        self.core.set(it, "ready")
                if self.backend and self.job_ids:
                    threading.Thread(target=self.backend.cancel, args=(list(self.job_ids),), daemon=True).start()
            if self.pcfg.get("clear_after") and r["printed"]:
                n = self.clear(only_done=True)
                txt += " Cleared %d from the list." % n
            self.state.log(txt, "ok" if r["printed"] and not r["failed"] else ("warn" if r["printed"] else "error"),
                           prefix="Print")
            job.web_result = dict(r, kind="print")
        self._changed()

    def close(self):
        self.core.cleanup()
        shutil.rmtree(self.upload_root, ignore_errors=True)


def clean_name(name):
    """A safe file name from whatever the browser sent (no folders, no control characters)."""
    name = os.path.basename((name or "").replace("\\", "/")).strip().strip(".")
    stem, ext = os.path.splitext(name)
    stem = sanitize_for_path(stem, max_len=120) or "upload"
    ext = "".join(ch for ch in ext[:12] if ch.isalnum() or ch == ".")
    return stem + ext


def receive(rfile, length, path):
    """Copy exactly `length` bytes of a request body to `path`."""
    left = length
    with open(path, "wb") as f:
        while left > 0:
            chunk = rfile.read(min(CHUNK, left))
            if not chunk:
                raise ApiError(400, "The upload was interrupted.")
            f.write(chunk)
            left -= len(chunk)
