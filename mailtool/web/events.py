"""Fan-out of live events to every open browser tab (Server-Sent Events).

JobRunner.events has a single reader; EventHub drains it on one thread and copies
each event into a small queue per connected tab."""
from __future__ import annotations

import collections
import itertools
import queue
import threading
import time

_seq = itertools.count(1)


class EventHub:
    def __init__(self, history=400):
        self._subs = set()
        self._lock = threading.Lock()
        self.log_ring = collections.deque(maxlen=history)   # recent log lines, for tabs opened later

    def subscribe(self, maxsize=2000):
        q = queue.Queue(maxsize)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def subscribers(self):
        with self._lock:
            return len(self._subs)

    def publish(self, event):
        """event: a JSON-able dict with a 'type' key."""
        event = dict(event)
        event.setdefault("seq", next(_seq))
        event.setdefault("ts", time.time())
        if event["type"] == "log":
            self.log_ring.append(event)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # a stalled tab: drop its backlog and tell it to reload everything
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait({"type": "resync", "seq": next(_seq), "ts": time.time()})
                except queue.Full:
                    pass
