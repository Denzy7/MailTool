"""Everything the web server shares between requests: settings, jobs, the print
queue, the library and the password. The Tk-free counterpart of ui/app.py."""
from __future__ import annotations

import getpass
import os
import queue
import secrets as pysecrets
import shutil
import tempfile
import threading
import time

from mailtool.core import deps, secrets, updates
from mailtool.core.config import Config
from mailtool.core.jobs import JobRunner
from mailtool.core.util import log
from mailtool.library.db import Library
from mailtool.web.events import EventHub

KIND_LABEL = {"fetch": "Fetch", "sort": "Sort", "print": "Print", "test": "Connection"}
WEB_HIDDEN_CAPS = ("tk", "dnd")      # desktop-only capabilities


class ApiError(Exception):
    """Turned into a JSON error response: {"error": message, ...extra}."""

    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def default_library_dir():
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    return os.path.join(docs if os.path.isdir(docs) else home, "MailTool Library")


def json_safe(v):
    if isinstance(v, dict):
        return {str(k): json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [json_safe(x) for x in v]
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


class FileRegistry:
    """Temporary download links: /files/<token>/<name> -> a file on this machine."""

    def __init__(self, ttl=3600):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._files = {}

    def add(self, path, name, ctype="application/octet-stream", group=None, cleanup=None):
        """cleanup: a file or folder deleted when the link expires."""
        token = pysecrets.token_urlsafe(18)
        with self._lock:
            self._files[token] = {"path": path, "name": name, "ctype": ctype, "group": group, "cleanup": cleanup,
                                  "expires": time.time() + self.ttl}
        self.sweep()
        return "/files/%s/%s" % (token, _url_name(name))

    def get(self, token):
        with self._lock:
            f = self._files.get(token)
        if f is None or f["expires"] < time.time() or not os.path.isfile(f["path"]):
            return None
        return f

    def drop_group(self, group):
        with self._lock:
            gone = [t for t, f in self._files.items() if f["group"] == group]
            entries = [self._files.pop(t) for t in gone]
        for f in entries:
            _remove(f["cleanup"])

    def sweep(self):
        now = time.time()
        with self._lock:
            gone = [t for t, f in self._files.items() if f["expires"] < now]
            entries = [self._files.pop(t) for t in gone]
        for f in entries:
            _remove(f["cleanup"])

    def clear(self):
        with self._lock:
            entries = list(self._files.values())
            self._files.clear()
        for f in entries:
            _remove(f["cleanup"])


def _url_name(name):
    import urllib.parse
    return urllib.parse.quote(name or "file", safe="")


def _remove(p):
    if not p:
        return
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            os.remove(p)
        except OSError:
            pass


class WebState:
    def __init__(self, config=None, max_upload_mb=512):
        self.cfg = config or Config()
        if not self.cfg.get("general", "library_dir"):
            self.cfg.set("general", "library_dir", default_library_dir())
        self.max_upload = int(max_upload_mb) * 1024 * 1024
        self.caps = deps.detect(self.cfg["print"])
        self.runner = JobRunner()
        self.hub = EventHub()
        self.files = FileRegistry()
        self._done = {}
        self._start_lock = threading.Lock()   # a job's on-done callback is registered before the pump can see it end
        self._lib, self._lib_root = None, None
        self._lib_lock = threading.Lock()
        base = os.path.join(tempfile.gettempdir(), "mailtool-web-" + getpass.getuser())
        os.makedirs(base, exist_ok=True)
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
        self.work_dir = tempfile.mkdtemp(prefix="session-", dir=base)
        self._stop = threading.Event()
        from mailtool.web.printsvc import PrintService
        self.prints = PrintService(self)
        self._pump_thread = threading.Thread(target=self._pump, name="web-events", daemon=True)
        self._pump_thread.start()
        self.update_info = None
        if self.cfg.get("general", "check_updates", True):
            updates.check_async(self._update_checked)

    def _update_checked(self, info):
        self.update_info = info
        self.notify("update", update=info)

    # ------------------------------------------------------------------ messages
    def log(self, text, level="info", prefix="MailTool"):
        getattr(log, {"warn": "warning", "ok": "info"}.get(level, level), log.info)(text)
        self.hub.publish({"type": "log", "level": level, "text": str(text), "prefix": prefix})

    def notify(self, what, **data):
        """Tell open tabs that something changed (library, settings, ...)."""
        self.hub.publish(dict(data, type=what))

    def _pump(self):
        while not self._stop.is_set():
            try:
                ev = self.runner.events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle(ev)
            except Exception:
                log.exception("event pump")

    def _handle(self, ev):
        kind, job = ev[0], ev[1]
        prefix = KIND_LABEL.get(job.kind, job.kind)
        if kind == "log":
            self.hub.publish({"type": "log", "level": ev[2], "text": ev[3], "prefix": prefix, "job": job.id})
        elif kind == "progress":
            self.hub.publish({"type": "job", "job": job_json(job)})
        elif kind == "state":
            if ev[2] != "running":
                try:
                    self._finished(job)
                finally:
                    job.web_final = True     # its result is ready: from now on report the real state
            self.hub.publish({"type": "job", "job": job_json(job)})

    def _finished(self, job):
        if job.error is not None and "Login failed" in str(job.error):
            a = self.cfg["account"]
            secrets.forget(a.get("server"), a.get("username"))
            self.cfg.set("account", "remember_password", False)
            self.log("The password was rejected; you'll be asked for it again next time.", "warn")
        with self._start_lock:
            cb = self._done.pop(job.id, None)
        if cb:
            try:
                cb(job)
            except Exception:
                log.exception("job callback failed")
        if job.kind in ("fetch", "sort"):
            self.notify("library")

    # ------------------------------------------------------------------ jobs
    def start_job(self, kind, title, fn, on_done=None, **kwargs):
        if self.runner.running(kind):
            raise ApiError(409, "A %s job is already running." % KIND_LABEL.get(kind, kind).lower())
        with self._start_lock:
            job = self.runner.start(kind, title, fn, **kwargs)
            if on_done:
                self._done[job.id] = on_done
        self.log("%s started" % title, "info", prefix=KIND_LABEL.get(kind, kind))
        return job

    def job(self, jid):
        for j in self.runner.jobs:
            if j.id == jid:
                return j
        return None

    # ------------------------------------------------------------------ account / password
    def account(self):
        return self.cfg.snapshot("account")

    def has_account(self):
        a = self.cfg["account"]
        return bool(a.get("server") and a.get("username"))

    def require_account(self):
        if not self.has_account():
            raise ApiError(409, "Set up your mail account first (Settings › Account).", need="account")

    def password(self, required=True):
        a = self.cfg["account"]
        pw = secrets.get_password(a.get("server", ""), a.get("username", ""))
        if not pw and required:
            raise ApiError(409, "Enter the mail password.", need="password")
        return pw

    def set_password(self, password, remember):
        a = self.cfg["account"]
        saved = secrets.set_password(a.get("server", ""), a.get("username", ""), password, bool(remember))
        self.cfg.set("account", "remember_password", bool(remember and saved))
        self.cfg.save()
        return saved

    def password_status(self):
        a = self.cfg["account"]
        have = bool(secrets.get_password(a.get("server"), a.get("username"))) if self.has_account() else False
        return {"keyring": secrets.keyring_available(), "have": have,
                "remembered": bool(have and a.get("remember_password"))}

    # ------------------------------------------------------------------ library / misc
    def library_root(self):
        return self.cfg.get("general", "library_dir") or default_library_dir()

    def tz_text(self):
        return self.cfg.get("general", "timezone") or "Africa/Nairobi"

    def library(self, create=False):
        """The shared Library for the configured folder (None if it doesn't exist yet)."""
        root = self.library_root()
        with self._lib_lock:
            if self._lib is not None and self._lib_root == root:
                return self._lib
            if self._lib is not None:
                self._lib.close()
                self._lib = None
            if not os.path.isdir(root):
                if not create:
                    return None
                os.makedirs(root, exist_ok=True)
            try:
                self._lib = Library(root)
                self._lib_root = root
            except Exception as e:
                raise ApiError(500, "Could not open the library at %s: %s" % (root, e))
            return self._lib

    def cap(self, key):
        return next((c for c in self.caps if c.key == key), None)

    def hint(self, key):
        c = self.cap(key)
        return c.hint if c else ""

    def recheck_caps(self):
        self.caps = deps.detect(self.cfg["print"])

    def web_caps(self):
        return [c for c in self.caps if c.key not in WEB_HIDDEN_CAPS]

    # ------------------------------------------------------------------ shutdown
    def close(self):
        self.runner.cancel_all()
        self.runner.wait_all(30)
        self._stop.set()
        try:
            self.cfg.save()
        except Exception:
            log.exception("saving settings")
        self.prints.close()
        self.files.clear()
        with self._lib_lock:
            if self._lib is not None:
                self._lib.close()
                self._lib = None
        shutil.rmtree(self.work_dir, ignore_errors=True)


def job_json(job):
    # A finished job counts as running until the event pump has run its on-done callback,
    # so nobody sees "done" without the result that callback adds.
    final = job.state == "running" or getattr(job, "web_final", False)
    return {"id": job.id, "kind": job.kind, "title": job.title, "state": job.state if final else "running",
            "current": job.current, "total": job.total, "note": job.note, "started": job.started,
            "finished": job.finished if final else None,
            "error": str(job.error) if job.error is not None else None,
            "result": json_safe(getattr(job, "web_result", None))}
