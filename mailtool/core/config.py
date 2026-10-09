"""One settings file for the whole app: <config dir>/MailTool/settings.json.

Passwords are never stored here (see core.secrets)."""
from __future__ import annotations

import copy
import json
import os
import threading

from mailtool.core.util import config_dir, log

DEFAULTS = {
    "general": {
        "library_dir": "",              # where emails are downloaded; holds .mailtool/library.sqlite
        "timezone": "Africa/Nairobi",   # IANA name or +HH:MM offset; used for all dates
        "theme": "system",              # system | light | dark
        "last_view": "fetch",
        "sidebar_open": True,           # False = icon-only sidebar
        "check_updates": True,          # ask GitHub for a newer release on start (shown next to the version)
    },
    "account": {
        "server": "",
        "port": 993,
        "username": "",
        "mailbox": "INBOX",
        "use_ssl": True,
        "allow_self_signed": False,     # skip certificate checks (only for servers you trust)
        "remember_password": False,     # in the OS keyring, never in this file
        "timeout": 60,
    },
    "fetch": {
        "from_date": "", "from_time": "00:00",
        "to_date": "", "to_time": "23:59",
        "save_attachments": True,
        "merge_pdfs": False,
        "emailinfo": True,
        "export_csv": False,
        "csv_path": "",
        "log_without_attachments": False,
        "sort_after": False,
    },
    "emailinfo": {
        "header_left": "",              # blank = username
        "header_right": "EMAIL UID: <UID>",
        "body_mode": "print",           # print | image | text
        "remote_images": True,
        "quality": 2,
        "workers": 2,
    },
    "sort": {
        "groups": [],
        "whole_word": True,
        "case_sensitive": False,
        "precedence": "keywords_first",   # keywords_first | groupname_first
        "excluded": [],
        "included": [],
        "dedupe": False,
        "input": "library",               # library | csv
        "library_from": "", "library_to": "",   # DD-Mon-YYYY, blank = everything
        "csv_path": "",
        "out_dir": "",
        "fallback_source": "imap",        # imap | local   (CSV input only)
        "local_folder": "",
        "search_attachments": True,
        "offline": False,
        "use_cache": True,
        "date_window_days": 1,
        "timestamp_window_seconds": 60,
    },
    "print": {
        "printer": None,
        "paper": "A4",
        "word_engine": "libreoffice",     # libreoffice | word
        "sumatra": None,
        "soffice": None,
        "temp_dir": None,
        "copy_timeout": 600,
        "convert_timeout": 180,
        "print_timeout": 300,
        "clear_after": False,
        "max_parallel": 4,
        "max_word": 1,
        "merge_batch": True,
        "thumb_width": 170,
    },
}


class Config:
    """Thread-safe nested settings. cfg["print"]["paper"] or cfg.get("print", "paper")."""

    def __init__(self, path=None):
        self.path = path or os.path.join(config_dir(), "settings.json")
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                stored = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("bad settings file %s: %s", self.path, e)
            return
        with self._lock:
            for section, values in stored.items():
                if section in self.data and isinstance(values, dict):
                    self.data[section].update(values)
            self.data["account"].pop("password", None)   # never keep one, even hand-added

    def save(self):
        with self._lock:
            data = copy.deepcopy(self.data)
        data["account"].pop("password", None)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as e:
            log.warning("could not save settings: %s", e)

    def __getitem__(self, section):
        return self.data[section]

    def get(self, section, key, default=None):
        with self._lock:
            v = self.data.get(section, {}).get(key, default)
        return default if v is None and default is not None else v

    def set(self, section, key, value):
        with self._lock:
            self.data.setdefault(section, {})[key] = value

    def update(self, section, values):
        with self._lock:
            self.data.setdefault(section, {}).update(values)

    def snapshot(self, section):
        with self._lock:
            return copy.deepcopy(self.data[section])
