"""The print queue engine: stage -> sniff -> dedupe -> convert -> inspect, then
print as one merged job (default) or file by file.

Everything is copied to a private temp folder first, so slow or flaky shares
(WebDAV, SMB, kio-fuse) never block printing and the originals are never touched."""
from __future__ import annotations

import getpass
import glob
import itertools
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid

from mailtool.core.util import FROZEN, IS_WIN, log, longpath, run, safe_stem, sha256
from mailtool.printing import pdfops
from mailtool.printing.pdfops import UserError
from mailtool.printing.tools import find_gio, find_kio, find_soffice, have_word_com

_SEQ = itertools.count()
ACTIVE = ("queued", "staging", "converting", "ready")
AUTH_RE = re.compile(r"password|authenticat|unauthori[sz]ed|401|403|forbidden|credential|login", re.I)

HELPER = r'''
import os, shutil, sys
src, dst = sys.argv[1], sys.argv[2]
try:
    if os.path.isdir(src): sys.exit(3)
    if not os.path.isfile(src): sys.exit(4)
    shutil.copyfile(src, dst)
except PermissionError: sys.exit(5)
except OSError as e:
    sys.stderr.write(str(e)); sys.exit(6)
'''


def copy_local(src, dst):
    """In-process equivalent of HELPER. Returns (exit code, message)."""
    try:
        if os.path.isdir(src):
            return 3, ""
        if not os.path.isfile(src):
            return 4, ""
        shutil.copyfile(src, dst)
        return 0, ""
    except PermissionError:
        return 5, ""
    except OSError as e:
        return 6, str(e)


class Item:
    def __init__(self, src):
        self.id = uuid.uuid4().hex[:10]
        self.seq = next(_SEQ)        # drop order; decides which duplicate is kept
        self.src = src
        self.display = src.display or "(unnamed)"
        self.status = "queued"       # queued staging converting ready printing done failed dup
        self.msg = ""
        self.pages = None
        self.copies = 1
        self.pdf = None
        self.dir = None
        self.hash = None
        self.removed = False
        self.noted = False
        self.excluded = set()        # 0-based pages left out
        self.page_order = None       # custom print order (None = document order)
        self.thumbs = {}             # {width: {page: png}}
        self.thumbs_done = set()

    def text(self):
        return "%s: %s" % (self.status, self.msg) if self.msg else self.status

    def pages_text(self):
        if self.pages is None:
            return ""
        if self.excluded or self.page_order:
            txt = "%d/%d" % (max(self.pages - len(self.excluded), 0), self.pages)
            return txt + (" ↕" if self.page_order else "")
        return str(self.pages)


def merge_sig(items):
    """Fingerprint of everything that affects the merged PDF."""
    return tuple((i.id, i.hash, tuple(sorted(i.excluded)), tuple(i.page_order or ()), i.copies) for i in items)


class Limiter:
    """Counting limiter whose limit can change while threads wait on it."""

    def __init__(self, n):
        self.n, self.active, self.cv = max(1, int(n)), 0, threading.Condition()

    def set(self, n):
        with self.cv:
            self.n = max(1, int(n))
            self.cv.notify_all()

    def __enter__(self):
        with self.cv:
            while self.active >= self.n:
                self.cv.wait()
            self.active += 1

    def __exit__(self, *exc):
        with self.cv:
            self.active -= 1
            self.cv.notify_all()


def sweep(root):
    """Remove temp folders left by MailTool instances that are no longer running."""
    now = time.time()
    for d in glob.glob(os.path.join(root, "session-*")):
        try:
            pid = int(os.path.basename(d).split("-", 1)[1])
        except (ValueError, IndexError):
            continue
        dead = False
        if IS_WIN:
            dead = now - os.path.getmtime(d) > 2 * 86400
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                dead = True
            except PermissionError:
                pass
        if dead and pid != os.getpid():
            shutil.rmtree(d, ignore_errors=True)


class PrintCore:
    def __init__(self, pcfg, notify, hint=lambda key: ""):
        """pcfg: the live 'print' settings dict. notify(event) receives
        ("upd", item) and ("dup", item) from worker threads."""
        self.cfg, self.notify, self.hint = pcfg, notify, hint
        root = pcfg.get("temp_dir") or os.path.join(tempfile.gettempdir(), "mailtool-" + getpass.getuser())
        os.makedirs(root, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        sweep(root)
        self.root = os.path.join(root, "session-%d" % os.getpid())
        os.makedirs(self.root, exist_ok=True)
        self.q = queue.Queue()
        self.hashes = {}
        self.lock = threading.Lock()
        self.slots = Limiter(pcfg.get("max_parallel") or 4)
        self.word_slots = Limiter(pcfg.get("max_word") or 1)
        threading.Thread(target=self._loop, daemon=True).start()

    def apply_settings(self):
        self.slots.set(self.cfg.get("max_parallel") or 4)
        self.word_slots.set(self.cfg.get("max_word") or 1)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _t(self, key, default):
        return int(self.cfg.get(key) or default)

    # -- state
    def set(self, it, status, msg=""):
        with self.lock:
            if it.status == "dup":      # evicted by an earlier-dropped copy while its worker was still running
                return
            it.status, it.msg = status, msg
        self.notify(("upd", it))

    def fail(self, it, msg):
        self.set(it, "failed", msg)

    def submit(self, it):
        self.q.put(it)

    def forget(self, it):
        it.removed = True
        with self.lock:
            for h in [h for h, v in self.hashes.items() if v is it]:
                del self.hashes[h]
        if it.dir:
            shutil.rmtree(it.dir, ignore_errors=True)

    # -- pipeline
    def _loop(self):
        while True:
            it = self.q.get()
            if it is None:
                return
            if it.removed:
                continue
            threading.Thread(target=self._run_item, args=(it,), daemon=True).start()

    def _run_item(self, it):
        with self.slots:
            if it.removed:
                return
            try:
                self.process(it)
            except UserError as e:
                self.fail(it, str(e))
            except Exception as e:
                log.exception("unexpected error for %s", it.display)
                self.fail(it, "unexpected error: %s" % e)

    def process(self, it):
        it.dir = os.path.join(self.root, it.id)
        os.makedirs(it.dir, exist_ok=True)
        self.set(it, "staging")
        raw = os.path.join(it.dir, "staged.bin")
        self.stage(it.src, raw)
        if it.removed:
            return
        kind, ext = pdfops.sniff(raw, it.display)
        h = sha256(raw)
        evict = dup = None
        with self.lock:
            other = self.hashes.get(h)
            if other is not None and other is not it and not other.removed:
                if other.seq > it.seq and other.status in ("queued", "staging", "converting", "ready"):
                    evict = other               # keep whichever was dropped first
                    evict.status, evict.msg = "dup", it.display
                    self.hashes[h] = it
                else:
                    dup = other
            else:
                self.hashes[h] = it
        if evict is not None:
            self.notify(("dup", evict))
        if dup is not None:
            it.status, it.msg = "dup", dup.display
            shutil.rmtree(it.dir, ignore_errors=True)
            self.notify(("dup", it))
            return
        it.hash = h
        src_file = os.path.join(it.dir, safe_stem(it.display) + ext)
        os.replace(raw, src_file)
        if kind == "pdf":
            pdf = src_file
        else:
            self.set(it, "converting")
            pdf = self.to_pdf(kind, src_file)
        if it.removed or it.status == "dup":
            return
        fixed = pdfops.sanitize_pdf(pdf)
        it.pages = pdfops.inspect_pdf(pdf)
        it.pdf = pdf
        self.set(it, "ready", ("repaired %d broken annotation(s)" % fixed) if fixed else "")

    # -- staging
    def stage(self, src, dst):
        if src.kind == "path":
            self._stage_path(src.path, dst)
        else:
            self._stage_url(src.url, dst)

    def _stage_path(self, path, dst):
        timeout = self._t("copy_timeout", 600)
        if FROZEN:
            # a frozen exe can't run "python -c"; copy in a thread and abandon it if the share hangs
            res = {}
            t = threading.Thread(target=lambda: res.update(r=copy_local(longpath(path), dst)), daemon=True)
            t.start()
            t.join(timeout)
            rc, err = (None, "") if t.is_alive() else res.get("r", (6, "copy failed"))
            out = ""
        else:
            # a separate process: a hung network mount can't freeze MailTool
            rc, out, err = run([sys.executable, "-c", HELPER, longpath(path), dst], timeout)
        if rc is None:
            raise UserError("timed out reading the file (share unreachable or mount hung)")
        msgs = {3: "is a folder - only files are accepted", 4: "file not found or not readable",
                5: "permission denied"}
        if rc == 0 and os.path.isfile(dst):
            return
        if rc in msgs:
            raise UserError(msgs[rc])
        raise UserError((err or out or "could not read file").strip()[:150])

    def _stage_url(self, url, dst):
        scheme = url.split(":", 1)[0].lower()
        gio, kio = find_gio(), find_kio()
        if not (gio or kio):
            raise UserError("WebDAV links need gio or kioclient. Install: %s / %s" % (self.hint("gio"),
                                                                                    self.hint("kio")))
        rest = url.split(":", 1)[1]
        gio_url = ("davs:" if scheme.endswith("s") else "dav:") + rest
        kio_url = ("webdavs:" if scheme.endswith("s") else "webdav:") + rest
        tools = [("kio", kio, kio_url), ("gio", gio, gio_url)]
        if scheme in ("dav", "davs"):
            tools.reverse()
        last = ""
        for name, exe, u in tools:
            if not exe:
                continue
            if name == "gio":
                rc, out, err = run([exe, "info", "-a", "standard::type", u], 60)
                if rc == 0 and re.search(r"standard::type:\s*(directory|3)\b", out):
                    raise UserError("is a folder - only files are accepted")
            rc, out, err = run([exe, "copy", u, dst], self._t("copy_timeout", 600))
            if rc is None:
                last = "timed out (an authentication dialog may be waiting)"
                continue
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
                raise UserError("is a folder - only files are accepted")
            if rc == 0 and os.path.isfile(dst):
                return
            last = (err or out or "%s failed" % name).strip()[:150]
        if AUTH_RE.search(last):
            last += " -> open the share once in your file manager to save the credentials"
        raise UserError(last or "could not fetch URL")

    # -- conversion
    def to_pdf(self, kind, src_file):
        out = os.path.splitext(src_file)[0] + ".pdf"
        if kind == "image":
            if pdfops.Image is None:
                raise UserError("images need Pillow. Install: %s" % self.hint("pil"))
            return pdfops.image_to_pdf(src_file, out, self.cfg.get("paper") or "A4")
        return self.word_to_pdf(src_file, out)

    def word_to_pdf(self, src, out):
        first_err = None
        timeout = self._t("convert_timeout", 180)
        if IS_WIN and self.cfg.get("word_engine") == "word" and have_word_com():
            try:
                with self.word_slots:
                    pdfops.word_com_convert(src, out, timeout)
                return out
            except UserError as e:
                first_err = str(e)
                log.warning("Word COM failed: %s", e)
        so = find_soffice(self.cfg)
        if not so:
            raise UserError(first_err or "Word documents need LibreOffice. Install: %s" % self.hint("soffice"))
        return pdfops.libreoffice_convert(so, src, out, timeout)

    # -- output
    def effective_pdf(self, it):
        """The PDF to print: excluded pages removed, custom order applied."""
        if not it.excluded and not it.page_order:
            return it.pdf
        n = it.pages or 0
        order = [i for i in (it.page_order or []) if 0 <= i < n]
        order += [i for i in range(n) if i not in order]
        keep = [i for i in order if i not in it.excluded]
        if not keep:
            raise UserError("all pages are excluded")
        out = os.path.join(it.dir, "subset.pdf")
        pdfops.subset_pdf(it.pdf, out, keep, self._t("convert_timeout", 180))
        return out

    def build_merged(self, items, mdir):
        """Merged PDF for items (in order, per-item copies collated). -> (path, engine)"""
        os.makedirs(mdir, exist_ok=True)
        inputs = []
        for n, it in enumerate(items):
            tmp = os.path.join(mdir, "%04d.pdf" % n)  # ASCII names: safe for Ghostscript on Windows
            shutil.copyfile(self.effective_pdf(it), tmp)
            inputs += [tmp] * it.copies
        out = os.path.join(mdir, "merged.pdf")
        return out, pdfops.merge_pdfs(inputs, out, self._t("convert_timeout", 180) * 5)
