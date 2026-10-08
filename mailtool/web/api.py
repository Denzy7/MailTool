"""JSON API for the browser, one section per screen.

Every handler takes a Request and returns a dict/list (sent as JSON), a
Response/FileResponse, or raises ApiError."""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from datetime import datetime

from mailtool import APP_NAME, __version__
from mailtool.core import deps, secrets, timeutil
from mailtool.core.util import IS_WIN
from mailtool.fetch import emailinfo as ei
from mailtool.fetch.fetcher import run_fetch
from mailtool.library.db import LookupCache
from mailtool.library.folders import ordered_print_files
from mailtool.mail.imap import MailSession
from mailtool.printing.tools import have_word_com
from mailtool.sort import sorter
from mailtool.sort.groups import split_keyword_lines
from mailtool.web.printsvc import clean_name, receive
from mailtool.web.state import ApiError, job_json
from mailtool.web.webio import FileResponse, Response

ISO = "%Y-%m-%d"
THEMES = ("system", "light", "dark")


# ============================================================================== helpers
def iso_to_dmy(s, field="date"):
    """'2026-10-06' -> '06-Oct-2026' (the format the engines and settings use)."""
    s = (s or "").strip()
    if not s:
        return ""
    for fmt in (ISO, timeutil.DATE_FMT):
        try:
            return datetime.strptime(s, fmt).strftime(timeutil.DATE_FMT)
        except ValueError:
            pass
    raise ApiError(400, "The %s %r is not a date." % (field, s))


def dmy_to_iso(s):
    s = (s or "").strip()
    if not s:
        return ""
    try:
        return datetime.strptime(s, timeutil.DATE_FMT).strftime(ISO)
    except ValueError:
        return ""


def _int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return default


def _str(v):
    return ("" if v is None else str(v)).strip()


def _ids(v):
    try:
        return [int(x) for x in (v or [])]
    except (TypeError, ValueError):
        raise ApiError(400, "Bad email id list.")


def inside(root, path):
    """True if path is root or below it (after resolving links and '..')."""
    try:
        root = os.path.realpath(root)
        path = os.path.realpath(path)
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


class Api:
    def __init__(self, state):
        self.s = state
        self.cfg = state.cfg

    def routes(self):
        """(method, pattern, handler). Patterns are anchored regexes on the path."""
        return [
            ("GET", r"/api/state", self.get_state),
            ("GET", r"/api/log", self.get_log),
            ("GET", r"/api/jobs", self.get_jobs),
            ("GET", r"/api/jobs/(\d+)", self.get_job),
            ("POST", r"/api/jobs/(\d+)/cancel", self.cancel_job),
            ("POST", r"/api/jobs/cancel", self.cancel_all),
            # print
            ("GET", r"/api/queue", self.get_queue),
            ("PUT", r"/api/upload", self.upload),
            ("DELETE", r"/api/queue", self.clear_queue),
            ("POST", r"/api/queue/order", self.order_queue),
            ("POST", r"/api/queue/sort", self.sort_queue),
            ("PATCH", r"/api/queue/(\w+)", self.patch_item),
            ("DELETE", r"/api/queue/(\w+)", self.delete_item),
            ("GET", r"/api/queue/(\w+)/thumb/(\d+)", self.thumb),
            ("GET", r"/api/queue/(\w+)/pdf", self.item_pdf),
            ("GET", r"/api/printers", self.printers),
            ("PUT", r"/api/print/options", self.print_options),
            ("POST", r"/api/print/download", self.print_download),
            ("POST", r"/api/print/server", self.print_server),
            ("POST", r"/api/print/cancel", self.print_cancel),
            # library
            ("GET", r"/api/library", self.library),
            ("GET", r"/api/library/(\d+)", self.library_item),
            ("GET", r"/api/library/(\d+)/file/(info|a\d+)", self.library_file),
            ("POST", r"/api/library/print", self.library_print),
            ("POST", r"/api/library/sort", self.library_sort),
            ("POST", r"/api/library/clear-group", self.library_clear_group),
            ("POST", r"/api/library/forget", self.library_forget),
            # fetch
            ("GET", r"/api/fetch", self.fetch_defaults),
            ("POST", r"/api/fetch", self.fetch),
            # sort
            ("GET", r"/api/sort", self.sort_get),
            ("PUT", r"/api/sort", self.sort_put),
            ("POST", r"/api/sort/run", self.sort_run),
            ("PUT", r"/api/sort/csv", self.sort_csv_upload),
            ("POST", r"/api/sort/cache/clear", self.sort_cache_clear),
            ("GET", r"/api/sort/groups/export", self.groups_export),
            ("POST", r"/api/sort/groups/import", self.groups_import),
            # settings
            ("GET", r"/api/settings", self.settings_get),
            ("PUT", r"/api/settings", self.settings_put),
            ("POST", r"/api/password", self.password_set),
            ("DELETE", r"/api/password", self.password_forget),
            ("POST", r"/api/account/test", self.account_test),
            ("GET", r"/api/tz", self.tz_check),
            ("GET", r"/api/deps", self.deps_get),
            ("POST", r"/api/deps/chromium", self.deps_chromium),
        ]

    # ========================================================================== app state
    def account_summary(self):
        a = self.cfg["account"]
        return {"configured": self.s.has_account(), "username": a.get("username") or "",
                "server": a.get("server") or "", "mailbox": a.get("mailbox") or "INBOX"}

    def get_state(self, req):
        return {"app": APP_NAME, "version": __version__, "account": self.account_summary(),
                "password": self.s.password_status(), "theme": self.cfg.get("general", "theme") or "system",
                "library_dir": self.s.library_root(), "timezone": self.s.tz_text(),
                "jobs": [job_json(j) for j in self.s.runner.jobs[-20:]], "queue": self.s.prints.snapshot(),
                "groups": len(self.cfg.get("sort", "groups") or []), "log": list(self.s.hub.log_ring),
                "max_upload": self.s.max_upload}

    def get_log(self, req):
        return {"log": list(self.s.hub.log_ring)}

    def get_jobs(self, req):
        return {"jobs": [job_json(j) for j in self.s.runner.jobs[-50:]]}

    def _job(self, req):
        j = self.s.job(int(req.params[0]))
        if j is None:
            raise ApiError(404, "No such job.")
        return j

    def get_job(self, req):
        return job_json(self._job(req))

    def cancel_job(self, req):
        j = self._job(req)
        if j.kind == "print":
            self.s.prints.cancel()
        else:
            j.cancel()
        return {"ok": True}

    def cancel_all(self, req):
        self.s.prints.cancel()
        self.s.runner.cancel_all()
        return {"ok": True}

    # ========================================================================== print
    def get_queue(self, req):
        return self.s.prints.snapshot()

    def upload(self, req):
        pos = req.query.get("pos")
        pos = int(pos) if pos not in (None, "") and str(pos).isdigit() else None
        it = self.s.prints.add_upload(req.q("name", "upload"), req.rfile, req.length, pos)
        return {"item": self.s.prints.item_json(it) if it else None}

    def clear_queue(self, req):
        return {"removed": self.s.prints.clear()}

    def order_queue(self, req):
        ids = req.json().get("ids")
        if not isinstance(ids, list):
            raise ApiError(400, "Expected {ids: [...]}.")
        self.s.prints.reorder([str(i) for i in ids])
        return {"ok": True}

    def sort_queue(self, req):
        self.s.prints.sort_names()
        return {"ok": True}

    def patch_item(self, req):
        return self.s.prints.update(req.params[0], req.json())

    def delete_item(self, req):
        self.s.prints.remove(req.params[0])
        return {"ok": True}

    def thumb(self, req):
        width = req.qint("w", int(self.cfg.get("print", "thumb_width") or 170))
        p = self.s.prints.thumb(req.params[0], int(req.params[1]), width)
        return FileResponse(p, ctype="image/png", inline=True, cache=True)

    def item_pdf(self, req):
        p, it = self.s.prints.item_pdf(req.params[0])
        name = os.path.splitext(it.display)[0] + ".pdf"
        return FileResponse(p, name=name, ctype="application/pdf", inline=req.q("download") != "1")

    def printers(self, req):
        return self.s.prints.printers(refresh=req.q("refresh") == "1")

    def print_options(self, req):
        self.s.prints.set_options(req.json())
        return {"ok": True}

    def print_download(self, req):
        return {"job": job_json(self.s.prints.start_download())}

    def print_server(self, req):
        d = req.json()
        merged = d.get("merged")
        job = self.s.prints.start_print(printer=_str(d.get("printer")) or None,
                                        merged=None if merged is None else bool(merged))
        return {"job": job_json(job)}

    def print_cancel(self, req):
        self.s.prints.cancel()
        return {"ok": True}

    # ========================================================================== library
    def _lib(self):
        lib = self.s.library()
        if lib is None:
            raise ApiError(404, "The library folder does not exist yet - fetch some mail first.")
        return lib

    def library(self, req):
        lib = self.s.library()
        if lib is None:
            return {"rows": [], "groups": [], "stats": {}, "root": self.s.library_root(), "exists": False}
        g = req.q("group", "")
        group = {"": None, "__unsorted__": "", "__unmatched__": "*unmatched*"}.get(g, g)
        start = end = None
        if req.q("from"):
            start = datetime.strptime(iso_to_dmy(req.q("from")), timeutil.DATE_FMT).strftime("%Y-%m-%d 00:00:00")
        if req.q("to"):
            end = datetime.strptime(iso_to_dmy(req.q("to")), timeutil.DATE_FMT).strftime("%Y-%m-%d 23:59:59")
        limit = max(1, min(5000, req.qint("limit", 2000)))
        rows = lib.query(start=start, end=end, text=(req.q("q") or "").strip() or None, group=group, limit=limit)
        only = req.q("ids")
        if only:
            keep = {int(x) for x in only.split(",") if x.strip().isdigit()}
            rows = [r for r in rows if r["id"] in keep]
        counts = lib.attachment_counts([r["id"] for r in rows])
        out = [{"id": r["id"], "received": (r["received_local"] or "")[:16],
                "sender": r["sender_name"] or r["sender_email"] or "", "sender_email": r["sender_email"] or "",
                "subject": r["subject"] or "", "files": counts.get(r["id"], 0), "group": r["group_name"] or "",
                "sorted": bool(r["sorted_at"]), "unmatched": bool(r["sorted_at"] and not r["group_name"])}
               for r in rows]
        return {"rows": out, "groups": lib.groups_in_use(), "stats": lib.stats(), "root": lib.root, "exists": True,
                "limited": len(rows) >= limit}

    def library_item(self, req):
        lib = self._lib()
        mid = int(req.params[0])
        r = lib.message(mid)
        if r is None:
            raise ApiError(404, "That email is not in the library.")
        files = []
        if r["emailinfo"]:
            p = lib.abs(r["emailinfo"])
            files.append({"key": "info", "name": os.path.basename(p), "label": "email print",
                          "size": _size(p), "available": bool(p and os.path.isfile(p))})
        for a in lib.attachments(mid):
            p = lib.abs(a["path"]) if a["path"] else None
            files.append({"key": "a%d" % a["id"], "name": a["filename"], "size": a["size"],
                          "available": bool(p and os.path.isfile(p))})
        for f in files:
            f["url"] = "/api/library/%d/file/%s" % (mid, f["key"]) if f["available"] else None
        folder = lib.abs(r["folder"]) if r["folder"] else None
        return {"id": mid, "subject": r["subject"] or "", "from_name": r["from_name"] or "",
                "from_email": r["from_email"] or "", "reply_email": r["reply_email"] or "",
                "to": r["to_addrs"] or "", "received": r["received_local"] or "", "time_source": r["time_source"],
                "mailbox": r["mailbox"], "uid": r["uid"], "group": r["group_name"] or "",
                "group_stage": r["group_stage"] or "", "group_term": r["group_term"] or "",
                "sorted": bool(r["sorted_at"]), "sort_reason": r["sort_reason"] or "",
                "body": (r["body"] or "")[:20000], "files": files,
                "folder": r["folder"] or "", "folder_exists": bool(folder and os.path.isdir(folder))}

    def library_file(self, req):
        lib = self._lib()
        mid, key = int(req.params[0]), req.params[1]
        r = lib.message(mid)
        if r is None:
            raise ApiError(404, "That email is not in the library.")
        path = name = None
        if key == "info":
            path = lib.abs(r["emailinfo"]) if r["emailinfo"] else None
        else:
            aid = int(key[1:])
            for a in lib.attachments(mid):
                if a["id"] == aid:
                    path = lib.abs(a["path"]) if a["path"] else None
                    name = a["filename"]
        if not path or not os.path.isfile(path) or not inside(lib.root, path):
            raise ApiError(404, "That file was not downloaded.")
        return FileResponse(path, name=name or os.path.basename(path), inline=req.q("download") != "1")

    def _print_files(self, lib, rows):
        out, skipped = [], 0
        for r in rows:
            p = lib.abs(r["folder"]) if r["folder"] else None
            files = ordered_print_files(p) if p and os.path.isdir(p) and inside(lib.root, p) else []
            if files:
                out += files
            else:
                skipped += 1
        return out, skipped

    def library_print(self, req):
        lib = self._lib()
        ids = _ids(req.json().get("ids"))
        rows = [r for r in (lib.message(i) for i in ids) if r is not None]
        rows.sort(key=lambda r: (r["received_local"] or "", r["id"]))
        files, skipped = self._print_files(lib, rows)
        if skipped:
            self.s.log("%d email(s) had nothing saved on disk to print." % skipped, "warn", prefix="Library")
        added = self.s.prints.add_paths(files) if files else 0
        return {"added": added, "skipped": skipped}

    def library_sort(self, req):
        ids = _ids(req.json().get("ids"))
        return {"job": job_json(self._start_sort_library(ids or None))}

    def library_clear_group(self, req):
        lib = self._lib()
        ids = _ids(req.json().get("ids"))
        if ids:
            lib.clear_groups(ids)
        self.s.notify("library")
        return {"ok": True}

    def library_forget(self, req):
        lib = self._lib()
        ids = _ids(req.json().get("ids"))
        for i in ids:
            lib.forget_message(i)
        self.s.notify("library")
        return {"removed": len(ids)}

    # ========================================================================== fetch
    def fetch_defaults(self, req):
        f = self.cfg.snapshot("fetch")
        today = datetime.now().strftime(ISO)
        mode = self.cfg.get("emailinfo", "body_mode") or "print"
        if not ei.GRAPHICAL:
            mode = "text"
        return {"from_date": dmy_to_iso(f.get("from_date")) or today, "from_time": f.get("from_time") or "00:00",
                "to_date": dmy_to_iso(f.get("to_date")) or today, "to_time": f.get("to_time") or "23:59",
                "save_attachments": bool(f.get("save_attachments")), "merge_pdfs": bool(f.get("merge_pdfs")),
                "emailinfo": bool(f.get("emailinfo") and ei.REPORTLAB), "export_csv": bool(f.get("export_csv")),
                "log_without_attachments": bool(f.get("log_without_attachments")),
                "sort_after": bool(f.get("sort_after")),
                "mailbox": self.cfg.get("account", "mailbox") or "INBOX", "library_dir": self.s.library_root(),
                "timezone": self.s.tz_text(), "account": self.account_summary(),
                "emailinfo_available": ei.REPORTLAB, "emailinfo_hint": "" if ei.REPORTLAB else self.s.hint("reportlab"),
                "layout": ei.BODY_MODES.get(mode, mode)}

    def fetch(self, req):
        d = req.json()
        self.s.require_account()
        opts = {k: bool(d.get(k)) for k in ("save_attachments", "merge_pdfs", "emailinfo", "export_csv",
                                            "log_without_attachments", "sort_after")}
        fd, td = iso_to_dmy(d.get("from_date"), "From date"), iso_to_dmy(d.get("to_date"), "To date")
        ft, tt = _str(d.get("from_time")) or "00:00", _str(d.get("to_time")) or "23:59"
        try:
            start, end = timeutil.parse_user_range(fd, ft, td, tt)
        except ValueError as e:
            raise ApiError(400, str(e))
        lib_dir = _str(d.get("library_dir")) or self.s.library_root()
        try:
            os.makedirs(lib_dir, exist_ok=True)
        except OSError as e:
            raise ApiError(400, "Cannot use the library folder: %s" % e)
        mailbox = _str(d.get("mailbox")) or "INBOX"
        self.cfg.update("fetch", dict(opts, from_date=fd, from_time=ft, to_date=td, to_time=tt))
        self.cfg.set("account", "mailbox", mailbox)
        self.cfg.set("general", "library_dir", lib_dir)
        self.cfg.save()
        pw = self.s.password()
        csv_path = ""
        if opts["export_csv"]:
            exports = os.path.join(lib_dir, "exports")
            os.makedirs(exports, exist_ok=True)
            csv_path = os.path.join(exports, "emails_%s.csv" % datetime.now().strftime("%Y-%m-%d_%H%M%S"))
        opts["csv_path"] = csv_path

        def done(job):
            r = job.result
            if not r:
                job.web_result = {"kind": "fetch"}
                return
            res = {"kind": "fetch", "messages": r["messages"], "attachments": r["attachments"], "pdfs": r["pdfs"],
                   "pdf_failed": r["pdf_failed"], "message_ids": r["message_ids"], "csv": None}
            if csv_path and os.path.isfile(csv_path):
                res["csv"] = {"name": os.path.basename(csv_path),
                              "url": self.s.files.add(csv_path, os.path.basename(csv_path), "text/csv")}
            job.web_result = res
            if opts["sort_after"] and r["messages"] and job.state == "done":
                try:
                    self._start_sort_library(r["message_ids"])
                except ApiError as e:
                    self.s.log("Sort after fetch: %s" % e.message, "warn", prefix="Sort")

        job = self.s.start_job("fetch", "Fetching mail", run_fetch, on_done=done, account=self.s.account(),
                               password=pw, library_root=lib_dir, tz_text=self.s.tz_text(), start=start, end=end,
                               opts=opts, info=self.cfg.snapshot("emailinfo"))
        return {"job": job_json(job)}

    # ========================================================================== sort
    def sort_get(self, req):
        s = self.cfg.snapshot("sort")
        groups = []
        for g in s.get("groups") or []:
            desc, kws, extra = split_keyword_lines(g.get("keywords", []))
            groups.append({"group_name": g.get("group_name", ""), "keywords": list(g.get("keywords") or []),
                           "desc": desc, "kws": kws, "extra": extra})
        try:
            c = LookupCache()
            cached = c.count()
            c.close()
        except Exception:
            cached = None
        csv_path = s.get("csv_path") or ""
        return {"groups": groups, "excluded": s.get("excluded") or [], "included": s.get("included") or [],
                "whole_word": bool(s.get("whole_word", True)), "case_sensitive": bool(s.get("case_sensitive")),
                "precedence": s.get("precedence") or "keywords_first", "dedupe": bool(s.get("dedupe")),
                "input": s.get("input") or "library", "library_from": dmy_to_iso(s.get("library_from")),
                "library_to": dmy_to_iso(s.get("library_to")), "csv_path": csv_path,
                "csv_name": os.path.basename(csv_path), "csv_exists": bool(csv_path and os.path.isfile(csv_path)),
                "out_dir": s.get("out_dir") or "", "fallback_source": s.get("fallback_source") or "imap",
                "local_folder": s.get("local_folder") or "",
                "search_attachments": bool(s.get("search_attachments", True)), "offline": bool(s.get("offline")),
                "use_cache": bool(s.get("use_cache", True)), "date_window_days": s.get("date_window_days", 1),
                "timestamp_window_seconds": s.get("timestamp_window_seconds", 60), "cache_count": cached,
                "library_dir": self.s.library_root()}

    def _clean_groups(self, groups):
        out, seen = [], set()
        for g in groups or []:
            if not isinstance(g, dict):
                continue
            name = _str(g.get("group_name"))
            kws = g.get("keywords")
            if isinstance(kws, str):
                kws = kws.splitlines()
            kws = [_str(k) for k in (kws or []) if _str(k)]
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"group_name": name, "keywords": kws})
        return out

    def sort_put(self, req):
        d = req.json()
        upd = {}
        if "groups" in d:
            upd["groups"] = self._clean_groups(d["groups"])
        for k in ("excluded", "included"):
            if k in d:
                pats, low = [], set()
                for p in d[k] or []:
                    p = _str(p)
                    if p and p.lower() not in low:
                        low.add(p.lower())
                        pats.append(p)
                upd[k] = pats
        for k in ("whole_word", "case_sensitive", "dedupe", "search_attachments", "offline", "use_cache"):
            if k in d:
                upd[k] = bool(d[k])
        if "precedence" in d:
            upd["precedence"] = "groupname_first" if d["precedence"] == "groupname_first" else "keywords_first"
        if "input" in d:
            upd["input"] = "csv" if d["input"] == "csv" else "library"
        if "fallback_source" in d:
            upd["fallback_source"] = "local" if d["fallback_source"] == "local" else "imap"
        for k in ("library_from", "library_to"):
            if k in d:
                upd[k] = iso_to_dmy(d[k], "date")
        for k in ("csv_path", "out_dir", "local_folder"):
            if k in d:
                upd[k] = _str(d[k])
        if "date_window_days" in d:
            upd["date_window_days"] = _int(d["date_window_days"], 0, 30, 1)
        if "timestamp_window_seconds" in d:
            upd["timestamp_window_seconds"] = _int(d["timestamp_window_seconds"], 0, 86400, 60)
        self.cfg.update("sort", upd)
        self.cfg.save()
        self.s.notify("sort")
        return self.sort_get(req)

    def _sort_done(self, job):
        r = job.result
        if not r:
            job.web_result = {"kind": "sort"}
            return
        reports = []
        for name, p in sorted((r.get("paths") or {}).items()):
            if os.path.isfile(p):
                reports.append({"name": name, "url": self.s.files.add(p, name, "text/csv")})
        job.web_result = {"kind": "sort", "matched": r["matched"], "unmatched": r["unmatched"],
                          "out_dir": r["out_dir"], "reports": reports}

    def _start_sort_library(self, ids=None):
        s = self.cfg.snapshot("sort")
        if not s.get("groups"):
            raise ApiError(409, "Add at least one group first (Sort › Groups).", need="groups")
        if ids is None:
            # the configured date range, if any
            lib = self._lib()
            start = end = None
            if s.get("library_from"):
                start = datetime.strptime(s["library_from"], timeutil.DATE_FMT).strftime("%Y-%m-%d 00:00:00")
            if s.get("library_to"):
                end = datetime.strptime(s["library_to"], timeutil.DATE_FMT).strftime("%Y-%m-%d 23:59:59")
            if start or end:
                ids = [r["id"] for r in lib.query(start=start, end=end, limit=1_000_000)]
                if not ids:
                    raise ApiError(400, "No emails in the library between those dates.")
        pw = self.s.password(required=False)   # only needed for attachments never downloaded
        title = "Sorting %s" % ("%d email(s)" % len(ids) if ids else "the library")
        return self.s.start_job("sort", title, sorter.run_sort_library, on_done=self._sort_done,
                                library_root=self.s.library_root(), sopts=s, account=self.s.account(), password=pw,
                                tz_text=self.s.tz_text(), ids=ids)

    def sort_run(self, req):
        d = req.json()
        if d.get("ids"):
            return {"job": job_json(self._start_sort_library(_ids(d["ids"])))}
        s = self.cfg.snapshot("sort")
        if not s.get("groups"):
            raise ApiError(409, "Add at least one group first (Sort › Groups).", need="groups")
        if s.get("input") != "csv":
            return {"job": job_json(self._start_sort_library())}
        csv_path = s.get("csv_path") or ""
        if not os.path.isfile(csv_path):
            raise ApiError(400, "Upload the CSV file to sort first.")
        pw = ""
        needs_mail = s.get("search_attachments") and not s.get("offline")
        if needs_mail and s.get("fallback_source") == "local":
            if not os.path.isdir(s.get("local_folder") or ""):
                raise ApiError(400, "The .eml folder %r does not exist on the server." % (s.get("local_folder") or ""))
        elif needs_mail:
            self.s.require_account()
            pw = self.s.password()
        out_dir = s.get("out_dir") or None
        if not out_dir and inside(self.s.work_dir, csv_path):
            out_dir = os.path.join(self.s.library_root(), "reports")   # not next to an uploaded temp copy
        job = self.s.start_job("sort", "Sorting %s" % os.path.basename(csv_path), sorter.run_sort_csv,
                               on_done=self._sort_done, csv_path=csv_path, out_dir=out_dir, sopts=s,
                               account=self.s.account(), password=pw, tz_text=self.s.tz_text(),
                               library_root=self.s.library_root())
        return {"job": job_json(job)}

    def sort_csv_upload(self, req):
        n = req.length
        if n is None:
            raise ApiError(411, "Content-Length is required.")
        if n > self.s.max_upload:
            raise ApiError(413, "File too large.")
        d = os.path.join(self.s.work_dir, "csv", uuid.uuid4().hex)
        os.makedirs(d)
        path = os.path.join(d, clean_name(req.q("name", "emails.csv")))
        try:
            receive(req.rfile, n, path)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        old = self.cfg.get("sort", "csv_path") or ""
        if old and inside(os.path.join(self.s.work_dir, "csv"), old):
            shutil.rmtree(os.path.dirname(old), ignore_errors=True)
        self.cfg.update("sort", {"csv_path": path, "input": "csv"})
        self.cfg.save()
        return {"csv_path": path, "csv_name": os.path.basename(path)}

    def sort_cache_clear(self, req):
        c = LookupCache()
        c.clear()
        c.close()
        self.s.log("Lookup cache cleared.", "ok", prefix="Sort")
        return {"ok": True}

    def groups_export(self, req):
        s = self.cfg.snapshot("sort")
        data = {"groups": s.get("groups") or [], "excluded_attachments": s.get("excluded") or [],
                "included_attachments": s.get("included") or []}
        return Response(json.dumps(data, indent=2, ensure_ascii=False), ctype="application/json; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="mailtool_groups.json"'})

    def groups_import(self, req):
        d = req.json()
        data = d.get("data")
        groups = data.get("groups") if isinstance(data, dict) else data
        if not isinstance(groups, list):
            raise ApiError(400, "No groups found in that file.")
        groups = self._clean_groups(groups)
        if not groups:
            raise ApiError(400, "No groups found in that file.")
        s = self.cfg.snapshot("sort")
        current = s.get("groups") or []
        if d.get("replace") or not current:
            new = groups
        else:
            have = {g["group_name"] for g in current}
            new = current + [g for g in groups if g["group_name"] not in have]
        upd = {"groups": new}
        if isinstance(data, dict):
            for key, field in (("excluded_attachments", "excluded"), ("included_attachments", "included")):
                pats = list(s.get(field) or [])
                for p in data.get(key) or []:
                    if isinstance(p, str) and p.strip() and p.strip() not in pats:
                        pats.append(p.strip())
                upd[field] = pats
        self.cfg.update("sort", upd)
        self.cfg.save()
        self.s.log("Imported %d group(s)." % len(groups), "ok", prefix="Sort")
        self.s.notify("sort")
        return self.sort_get(req)

    # ========================================================================== settings
    def settings_get(self, req):
        p = self.cfg.snapshot("print")
        return {"account": self.cfg.snapshot("account"), "general": self.cfg.snapshot("general"),
                "emailinfo": self.cfg.snapshot("emailinfo"),
                "print": {k: p.get(k) for k in ("word_engine", "max_word", "max_parallel", "paper", "copy_timeout",
                                                "convert_timeout", "print_timeout", "sumatra", "soffice",
                                                "temp_dir")},
                "password": self.s.password_status(), "keyring_hint": self.s.hint("keyring"),
                "platform": {"windows": IS_WIN, "word": have_word_com(), "graphical": ei.GRAPHICAL,
                             "playwright": ei.PLAYWRIGHT, "reportlab": ei.REPORTLAB,
                             "body_modes": ei.BODY_MODES if ei.GRAPHICAL else {"text": ei.BODY_MODES["text"]},
                             "quality": {str(k): ei.quality_label(k) for k in ei.QUALITY_LEVELS},
                             "placeholders": list(ei.TEMPLATE_SPECIFIERS)},
                "library_dir": self.s.library_root(), "config_dir": self.cfg.path, "version": __version__}

    def settings_put(self, req):
        d = req.json()
        warnings = []
        if isinstance(d.get("account"), dict):
            a = d["account"]
            use_ssl = bool(a.get("use_ssl", True))
            acc = {"server": _str(a.get("server")), "username": _str(a.get("username")),
                   "mailbox": _str(a.get("mailbox")) or "INBOX",
                   "port": _int(a.get("port"), 1, 65535, 993 if use_ssl else 143),
                   "timeout": _int(a.get("timeout"), 10, 600, 60), "use_ssl": use_ssl,
                   "allow_self_signed": bool(a.get("allow_self_signed"))}
            old = self.cfg["account"]
            if (old.get("server"), old.get("username")) != (acc["server"], acc["username"]):
                acc["remember_password"] = False
            self.cfg.update("account", acc)
        if isinstance(d.get("general"), dict):
            g = d["general"]
            upd = {}
            if "library_dir" in g:
                upd["library_dir"] = _str(g["library_dir"]) or self.s.library_root()
            if "timezone" in g:
                tz = _str(g["timezone"]) or "Africa/Nairobi"
                if not timeutil.timezone_ok(tz):
                    warnings.append("The timezone %r is not recognised - the server's own timezone is used until "
                                    "it is fixed." % tz)
                upd["timezone"] = tz
            if "theme" in g:
                upd["theme"] = g["theme"] if g["theme"] in THEMES else "system"
            self.cfg.update("general", upd)
        if isinstance(d.get("emailinfo"), dict):
            i = d["emailinfo"]
            mode = i.get("body_mode") if i.get("body_mode") in ei.BODY_MODES else "print"
            self.cfg.update("emailinfo", {"header_left": _str(i.get("header_left")),
                                          "header_right": _str(i.get("header_right")),
                                          "body_mode": mode, "remote_images": bool(i.get("remote_images", True)),
                                          "quality": _int(i.get("quality"), 1, len(ei.QUALITY_LEVELS), 2),
                                          "workers": _int(i.get("workers"), 1, 32, 2)})
        if isinstance(d.get("print"), dict):
            p = d["print"]
            pr = {"word_engine": "word" if p.get("word_engine") == "word" else "libreoffice",
                  "paper": "Letter" if p.get("paper") == "Letter" else "A4",
                  "max_word": _int(p.get("max_word"), 1, 8, 1), "max_parallel": _int(p.get("max_parallel"), 1, 32, 4),
                  "copy_timeout": _int(p.get("copy_timeout"), 10, 7200, 600),
                  "convert_timeout": _int(p.get("convert_timeout"), 10, 7200, 180),
                  "print_timeout": _int(p.get("print_timeout"), 10, 7200, 300)}
            for k in ("sumatra", "soffice", "temp_dir"):
                pr[k] = _str(p.get(k)) or None
            for k in ("sumatra", "soffice"):
                if pr[k] and not os.path.isfile(pr[k]):
                    raise ApiError(400, "File not found on the server: %s" % pr[k])
            self.cfg.update("print", pr)
        self.cfg.save()
        self.s.recheck_caps()
        self.s.prints.apply_settings()
        self.s.notify("settings", account=self.account_summary(), theme=self.cfg.get("general", "theme"))
        out = self.settings_get(req)
        out["warnings"] = warnings
        return out

    def password_set(self, req):
        d = req.json()
        self.s.require_account()
        pw = d.get("password")
        if not isinstance(pw, str) or not pw:
            raise ApiError(400, "Enter the password.")
        saved = self.s.set_password(pw, bool(d.get("remember")))
        return {"saved_to_keyring": saved, "password": self.s.password_status()}

    def password_forget(self, req):
        a = self.cfg["account"]
        secrets.forget(a.get("server"), a.get("username"))
        self.cfg.set("account", "remember_password", False)
        self.cfg.save()
        self.s.log("Password forgotten.", "ok", prefix="Settings")
        return {"password": self.s.password_status()}

    def account_test(self, req):
        self.s.require_account()
        pw = self.s.password()

        def work(job, account, password):
            s = MailSession(account, password, job=job)
            try:
                s.connect(False)
                boxes = s.list_mailboxes()
                count = s.select(account.get("mailbox") or "INBOX")
                return {"boxes": boxes, "count": count}
            finally:
                s.close()

        def done(job):
            r = job.result or {}
            job.web_result = {"kind": "test", "boxes": r.get("boxes") or [], "count": r.get("count"),
                              "mailbox": self.cfg.get("account", "mailbox") or "INBOX"}

        job = self.s.start_job("test", "Testing the connection", work, on_done=done, account=self.s.account(),
                               password=pw)
        return {"job": job_json(job)}

    def tz_check(self, req):
        name = _str(req.q("name"))
        if not timeutil.timezone_ok(name):
            return {"ok": False}
        now = datetime.now(timeutil.resolve_timezone(name))
        return {"ok": True, "now": now.strftime("%H:%M  (UTC%z)")}

    def deps_get(self, req):
        if req.q("refresh") == "1":
            self.s.recheck_caps()
        caps = self.s.web_caps()
        return {"caps": [{"key": c.key, "area": c.area, "label": c.label, "ok": c.ok, "detail": c.detail,
                          "hint": c.hint, "level": c.level} for c in caps],
                "text": deps.format_caps(caps), "chromium_installable": bool(ei.PLAYWRIGHT),
                "python": "%s %s" % (sys.executable, sys.version.split()[0]), "version": __version__}

    def deps_chromium(self, req):
        if not ei.PLAYWRIGHT:
            raise ApiError(409, "Playwright is not installed: %s" % self.s.hint("playwright"))

        def done(job):
            self.s.recheck_caps()
            job.web_result = {"kind": "chromium"}
        job = self.s.start_job("test", "Installing Chromium", deps.install_chromium, on_done=done)
        return {"job": job_json(job)}


def _size(p):
    try:
        return os.path.getsize(p)
    except (OSError, TypeError):
        return None

