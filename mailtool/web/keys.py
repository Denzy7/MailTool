"""Login keys for the web server: a browser that signed in with the access code can
download a login file holding a long random key, and use that file to sign in later -
also after the server restarts with a new access code.

Only a SHA-256 hash of each key is kept, in web_login_keys.json next to the settings
file. Each key can be revoked on its own (Settings › General)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime

from mailtool.core.util import log

PREFIX = "mtk_"
FILE_NAME = "web_login_keys.json"


def _hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _now():
    return datetime.now().isoformat(timespec="seconds")


class LoginKeys:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.keys = []
        self._last_touch = {}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.keys = [k for k in data.get("keys", []) if isinstance(k, dict) and k.get("hash") and k.get("id")]
        except FileNotFoundError:
            self.keys = []
        except (OSError, ValueError) as e:
            log.warning("could not read %s: %s", self.path, e)
            self.keys = []

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"keys": self.keys}, f, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save %s: %s", self.path, e)

    def create(self, label):
        """-> (public info, the key itself). The key is never stored or shown again."""
        key = PREFIX + secrets.token_urlsafe(32)
        entry = {"id": secrets.token_hex(6), "label": (label or "").strip()[:80] or "Browser",
                 "hash": _hash(key), "created": _now(), "last_used": None}
        with self.lock:
            self.keys.append(entry)
            self._save()
        return self._public(entry), key

    def check(self, key):
        """The entry's id if `key` is a valid login key, else None."""
        if not isinstance(key, str) or not key.startswith(PREFIX):
            return None
        h = _hash(key)
        with self.lock:
            match = None
            for k in self.keys:
                if hmac.compare_digest(k["hash"], h):     # compare every entry; no early exit
                    match = k
            if match is None:
                return None
            # record use, but don't rewrite the file more than once a minute per key
            if time.time() - self._last_touch.get(match["id"], 0) > 60:
                self._last_touch[match["id"]] = time.time()
                match["last_used"] = _now()
                self._save()
            return match["id"]

    def exists(self, kid):
        with self.lock:
            return any(k["id"] == kid for k in self.keys)

    def list(self):
        with self.lock:
            return [self._public(k) for k in self.keys]

    def revoke(self, kid):
        with self.lock:
            before = len(self.keys)
            self.keys = [k for k in self.keys if k["id"] != kid]
            if len(self.keys) != before:
                self._save()
                return True
        return False

    @staticmethod
    def _public(k):
        return {"id": k["id"], "label": k["label"], "created": k["created"], "last_used": k.get("last_used")}
