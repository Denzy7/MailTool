"""The library index: <library>/.mailtool/library.sqlite

One row per downloaded email (exact IMAP identity: account + mailbox +
UIDVALIDITY + UID) and one per attachment. Sorting reads from here, so it
never has to re-find a message by subject/sender/time."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime

from mailtool.core.util import data_dir

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    account         TEXT NOT NULL,
    mailbox         TEXT NOT NULL,
    uidvalidity     TEXT NOT NULL DEFAULT '',
    uid             TEXT NOT NULL,
    message_id      TEXT,
    received_local  TEXT,       -- 'YYYY-MM-DD HH:MM:SS' in the configured timezone
    received_utc    TEXT,       -- ISO 8601
    time_source     TEXT,       -- received | date | none
    from_name       TEXT, from_email  TEXT,
    reply_name      TEXT, reply_email TEXT,
    sender_name     TEXT, sender_email TEXT,   -- effective sender (Reply-To preferred)
    to_addrs        TEXT,
    subject         TEXT,
    body            TEXT,
    folder          TEXT,       -- relative to the library root, '/' separated
    emailinfo       TEXT,       -- relative path of the EMAILINFO pdf, if made
    attachments_saved INTEGER DEFAULT 0,
    fetched_at      TEXT,
    group_name      TEXT,
    group_term      TEXT,
    group_stage     TEXT,
    sort_reason     TEXT,
    sorted_at       TEXT,
    UNIQUE (account, mailbox, uidvalidity, uid)
);
CREATE INDEX IF NOT EXISTS ix_msg_received ON messages(received_local);
CREATE INDEX IF NOT EXISTS ix_msg_group ON messages(group_name);
CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY,
    message     INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    size        INTEGER,
    part        TEXT,
    path        TEXT,           -- relative to the library root; NULL if not downloaded
    text        TEXT,           -- extracted text (sorting cache)
    text_state  TEXT            -- NULL | ok | empty | error:<msg>
);
CREATE INDEX IF NOT EXISTS ix_att_msg ON attachments(message);
"""


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


class Library:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.meta_dir = os.path.join(self.root, ".mailtool")
        os.makedirs(self.meta_dir, exist_ok=True)
        self.path = os.path.join(self.meta_dir, "library.sqlite")
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            self.db.commit()

    def close(self):
        with self._lock:
            try:
                self.db.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- paths
    def rel(self, path):
        if not path:
            return None
        return os.path.relpath(os.path.abspath(path), self.root).replace(os.sep, "/")

    def abs(self, rel):
        if not rel:
            return None
        return os.path.join(self.root, *rel.split("/"))

    # ---------------------------------------------------------------- writes
    def upsert_message(self, m):
        """m: dict with account, mailbox, uidvalidity, uid + any other columns.
        Columns set to None keep their previous value. Returns the row id."""
        keys = ("account", "mailbox", "uidvalidity", "uid")
        ident = tuple(m.get(k) or "" for k in keys)
        cols = [k for k in m if k not in keys and k != "id"]
        with self._lock:
            row = self.db.execute("SELECT id FROM messages WHERE account=? AND mailbox=? AND uidvalidity=? AND uid=?",
                                  ident).fetchone()
            if row is None:
                allc = list(keys) + cols
                self.db.execute("INSERT INTO messages (%s) VALUES (%s)" % (",".join(allc), ",".join("?" * len(allc))),
                                ident + tuple(m[c] for c in cols))
                mid = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                mid = row["id"]
                upd = [c for c in cols if m[c] is not None]
                if upd:
                    self.db.execute("UPDATE messages SET %s WHERE id=?" % ",".join("%s=?" % c for c in upd),
                                    tuple(m[c] for c in upd) + (mid,))
            self.db.commit()
        return mid

    def set_attachments(self, mid, atts):
        """atts: [{filename, size, part, path}] for this message. Existing rows are
        matched by filename (+part); extracted text is kept when the size is unchanged."""
        with self._lock:
            old = {(r["filename"], r["part"] or ""): r for r in
                   self.db.execute("SELECT * FROM attachments WHERE message=?", (mid,))}
            seen = set()
            for a in atts:
                k = (a["filename"], a.get("part") or "")
                if k not in old:   # a part number may be unknown on one side
                    k = next((ok for ok in old if ok[0] == a["filename"] and ok not in seen), k)
                seen.add(k)
                r = old.get(k)
                path = self.rel(a.get("path")) if a.get("path") else None
                if r is None:
                    self.db.execute("INSERT INTO attachments (message, filename, size, part, path) VALUES (?,?,?,?,?)",
                                    (mid, a["filename"], a.get("size"), a.get("part"), path))
                else:
                    keep_text = r["size"] == a.get("size") or a.get("size") is None
                    self.db.execute(
                        "UPDATE attachments SET size=COALESCE(?, size), part=COALESCE(?, part), "
                        "path=COALESCE(?, path), text=?, text_state=? WHERE id=?",
                        (a.get("size"), a.get("part"), path,
                         r["text"] if keep_text else None, r["text_state"] if keep_text else None, r["id"]))
            for k, r in old.items():
                if k not in seen:
                    self.db.execute("DELETE FROM attachments WHERE id=?", (r["id"],))
            self.db.commit()

    def set_attachment_text(self, att_id, text, state="ok", path=None):
        with self._lock:
            if path:
                self.db.execute("UPDATE attachments SET text=?, text_state=?, path=? WHERE id=?",
                                (text, state, self.rel(path), att_id))
            else:
                self.db.execute("UPDATE attachments SET text=?, text_state=? WHERE id=?", (text, state, att_id))
            self.db.commit()

    def set_fields(self, mid, **fields):
        if not fields:
            return
        with self._lock:
            self.db.execute("UPDATE messages SET %s WHERE id=?" % ",".join("%s=?" % k for k in fields),
                            tuple(fields.values()) + (mid,))
            self.db.commit()

    def set_group(self, mid, group_name, term=None, stage=None, reason=None):
        with self._lock:
            self.db.execute("UPDATE messages SET group_name=?, group_term=?, group_stage=?, sort_reason=?, sorted_at=? "
                            "WHERE id=?", (group_name, term, stage, reason, now_iso(), mid))
            self.db.commit()

    def clear_groups(self, ids=None):
        with self._lock:
            if ids is None:
                self.db.execute("UPDATE messages SET group_name=NULL, group_term=NULL, group_stage=NULL, "
                                "sort_reason=NULL, sorted_at=NULL")
            else:
                self.db.executemany("UPDATE messages SET group_name=NULL, group_term=NULL, group_stage=NULL, "
                                    "sort_reason=NULL, sorted_at=NULL WHERE id=?", [(i,) for i in ids])
            self.db.commit()

    def forget_message(self, mid):
        with self._lock:
            self.db.execute("DELETE FROM messages WHERE id=?", (mid,))
            self.db.commit()

    # ---------------------------------------------------------------- reads
    def message(self, mid):
        with self._lock:
            return self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()

    def find(self, account, mailbox, uidvalidity, uid):
        with self._lock:
            return self.db.execute("SELECT * FROM messages WHERE account=? AND mailbox=? AND uid=? "
                                   "AND (uidvalidity=? OR ?='') ORDER BY id DESC LIMIT 1",
                                   (account, mailbox, str(uid), uidvalidity or "", uidvalidity or "")).fetchone()

    def attachments(self, mid):
        with self._lock:
            return list(self.db.execute("SELECT * FROM attachments WHERE message=? ORDER BY id", (mid,)))

    def query(self, start=None, end=None, text=None, group=None, limit=5000):
        """start/end: 'YYYY-MM-DD HH:MM:SS' local strings (inclusive).
        group: None = any, '' = unsorted only, '*unmatched*' = sorted but no group."""
        where, args = [], []
        if start:
            where.append("received_local >= ?")
            args.append(start)
        if end:
            where.append("received_local <= ?")
            args.append(end)
        if text:
            like = "%%%s%%" % text.replace("%", "").replace("_", "")
            where.append("(subject LIKE ? OR sender_email LIKE ? OR sender_name LIKE ? OR from_email LIKE ? "
                         "OR group_name LIKE ?)")
            args += [like] * 5
        if group == "":
            where.append("sorted_at IS NULL")
        elif group == "*unmatched*":
            where.append("sorted_at IS NOT NULL AND group_name IS NULL")
        elif group:
            where.append("group_name = ?")
            args.append(group)
        sql = "SELECT * FROM messages" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY received_local DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return list(self.db.execute(sql, args))

    def attachment_counts(self, ids):
        if not ids:
            return {}
        out = {}
        with self._lock:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in self.db.execute("SELECT message, COUNT(*) n FROM attachments WHERE message IN (%s) "
                                         "GROUP BY message" % ",".join("?" * len(chunk)), chunk):
                    out[r["message"]] = r["n"]
        return out

    def groups_in_use(self):
        with self._lock:
            return [r[0] for r in self.db.execute(
                "SELECT DISTINCT group_name FROM messages WHERE group_name IS NOT NULL ORDER BY group_name")]

    def stats(self):
        with self._lock:
            r = self.db.execute("SELECT COUNT(*) total, SUM(sorted_at IS NOT NULL) sorted, "
                                "SUM(group_name IS NOT NULL) grouped, MIN(received_local) first, "
                                "MAX(received_local) last FROM messages").fetchone()
        return dict(r) if r else {}


# --------------------------------------------------------------------------------------------
class LookupCache:
    """Results of the fuzzy CSV -> mailbox lookups (and their attachment text),
    so re-running a CSV doesn't hit the server again. Lives in the user data dir
    because CSV sorting doesn't need a library."""

    VERSION = "v3"

    def __init__(self, path=None):
        self.path = path or os.path.join(data_dir(), "lookup_cache.sqlite")
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        with self._lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, updated REAL)")
            self.db.commit()

    @classmethod
    def key(cls, source, email_addr, subject, date_s):
        import re
        subj = re.sub(r"^((re|fwd|fw|aw)\s*:\s*)+", "", (subject or "").strip(), flags=re.I).strip().lower()
        return "%s|%s|%s|%s|%s" % (cls.VERSION, source, (email_addr or "").strip().lower(), subj,
                                   (date_s or "").strip())

    def get(self, key):
        with self._lock:
            r = self.db.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        try:
            return json.loads(r[0])
        except ValueError:
            return None

    def put(self, key, value):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value), time.time()))
            self.db.commit()

    def count(self):
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]

    def clear(self):
        with self._lock:
            self.db.execute("DELETE FROM cache")
            self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()
