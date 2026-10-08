"""Background jobs: one model for cancel / progress / log, used by fetch, sort and print.

Workers never touch Tk. They post events to JobRunner.events, which the UI drains
on its own thread:
    ("log", job, level, text)            level: info | ok | warn | error
    ("progress", job, current, total, note)
    ("state", job, state)                state: running | done | failed | cancelled
"""
from __future__ import annotations

import itertools
import logging
import os
import queue
import socket
import threading
import time
import traceback
from datetime import datetime

from mailtool.core.util import log, logs_dir

_ids = itertools.count(1)


class Cancelled(Exception):
    """Raised by Job.check() when the user pressed Stop."""


class Job:
    def __init__(self, runner, kind, title):
        self.id = next(_ids)
        self.runner = runner
        self.kind, self.title = kind, title
        self.state = "running"
        self.cancel_event = threading.Event()
        self.started = time.time()
        self.finished = None
        self.current, self.total, self.note = 0, 0, ""
        self.result = None
        self.error = None
        self._socks = []
        self._fh = None
        try:
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            self.log_path = os.path.join(logs_dir(), f"{stamp}_{kind}_{self.id}.log")
            self._fh = open(self.log_path, "w", encoding="utf-8")
        except OSError:
            self.log_path = None

    # -- worker side
    def log(self, text, level="info"):
        text = str(text)
        getattr(log, {"warn": "warning", "ok": "info"}.get(level, level), log.info)("[%s] %s", self.kind, text)
        if self._fh:
            ts = datetime.now().strftime("%H:%M:%S")
            try:
                for line in text.splitlines() or [""]:
                    self._fh.write(f"[{ts}] {level.upper():5} {line}\n")
                self._fh.flush()
            except (OSError, ValueError):
                pass
        self.runner.events.put(("log", self, level, text))

    def progress(self, current, total, note=""):
        self.current, self.total, self.note = current, total, note
        self.runner.events.put(("progress", self, current, total, note))

    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    def check(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def track_socket(self, sock):
        """Remember a raw socket so Stop can cut a network call that is hanging."""
        self._socks.append(sock)

    # -- UI side
    def cancel(self):
        if self.state != "running" or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.log("Stopping - finishing the current item(s) ...", "warn")
        threading.Timer(3.0, self._cut_sockets).start()

    def _cut_sockets(self):
        if self.state != "running":
            return
        for s in self._socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def _finish(self, state):
        self.state = state
        self.finished = time.time()
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass
        self._socks = []
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        self.runner.events.put(("state", self, state))


class JobRunner:
    def __init__(self):
        self.events = queue.Queue()
        self.jobs = []
        self._lock = threading.Lock()

    def start(self, kind, title, fn, *args, **kwargs):
        """Run fn(job, *args, **kwargs) on a daemon thread. Its return value becomes job.result."""
        job = Job(self, kind, title)
        with self._lock:
            self.jobs.append(job)
        self.events.put(("state", job, "running"))

        def body():
            state = "done"
            try:
                job.result = fn(job, *args, **kwargs)
                if job.cancelled:
                    state = "cancelled"
            except Cancelled:
                job.log("Stopped by user.", "warn")
                state = "cancelled"
            except Exception as e:
                if job.cancelled:
                    job.log("Stopped by user (%s)." % (e or type(e).__name__), "warn")
                    state = "cancelled"
                else:
                    job.error = e
                    job.log("Error: %s" % (e or type(e).__name__), "error")
                    logging.getLogger("mailtool").debug(traceback.format_exc())
                    if job._fh:
                        try:
                            job._fh.write(traceback.format_exc())
                        except (OSError, ValueError):
                            pass
                    state = "failed"
            finally:
                job._finish(state)

        threading.Thread(target=body, name=f"job-{kind}-{job.id}", daemon=True).start()
        return job

    def running(self, kind=None):
        with self._lock:
            return [j for j in self.jobs if j.state == "running" and (kind is None or j.kind == kind)]

    def cancel_all(self):
        for j in self.running():
            j.cancel()

    def wait_all(self, timeout):
        end = time.time() + timeout
        while self.running() and time.time() < end:
            time.sleep(0.1)
