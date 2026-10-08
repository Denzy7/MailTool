"""A folder of .eml files (e.g. an extracted Zimbra .tgz export) used in place of
an IMAP mailbox. Same lookup interface as MailSession: search_fuzzy,
message_times, fetch_message. Subfolders (Inbox!1, Inbox!2, ...) are scanned;
only *.eml is read (.meta sidecars are ignored)."""
from __future__ import annotations

import email.parser
import email.policy
import os
import re
from datetime import timedelta

from mailtool.core import timeutil
from mailtool.mail.mime import decode_mime_words


class LocalEmlSource:
    def __init__(self, folder, job=None):
        self.folder = folder
        self.job = job
        self._index = {}

    def _log(self, msg, level="info"):
        if self.job:
            self.job.log(msg, level)

    def connect(self):
        if not self.folder or not os.path.isdir(self.folder):
            raise RuntimeError("Local export folder not found: %r" % self.folder)
        self._index = {}
        skipped = 0
        parser = email.parser.BytesParser(policy=email.policy.compat32)
        for root, _dirs, files in os.walk(self.folder):
            for fn in files:
                if os.path.splitext(fn)[1].lower() != ".eml":
                    skipped += 1
                    continue
                if self.job:
                    self.job.check()
                path = os.path.join(root, fn)
                try:
                    with open(path, "rb") as fh:
                        msg = parser.parse(fh, headersonly=True)
                except Exception as exc:
                    self._log("  could not read header of %s: %s" % (path, exc), "warn")
                    continue
                rec, src = timeutil.get_received_datetime(msg)
                self._index[path] = {
                    "from": decode_mime_words(msg.get("From", "")).lower(),
                    "reply_to": decode_mime_words(msg.get("Reply-To", "")).lower(),
                    "subject": decode_mime_words(msg.get("Subject", "")).lower(),
                    "date": timeutil.parse_header_date(msg.get("Date")),
                    "received": rec if src == "received" else None,
                }
                if len(self._index) % 1000 == 0:
                    self._log("  indexed %d .eml files ..." % len(self._index))
        if not self._index:
            raise RuntimeError("No .eml files found under %r" % self.folder)
        self._log("Indexed %d .eml files under %s (%d other files skipped)." % (
            len(self._index), self.folder, skipped))

    def close(self):
        self._index = {}

    def search_fuzzy(self, subject, sender_email, date_obj, window_days=1):
        since = before = None
        if date_obj:
            since = _naive_utc(date_obj - timedelta(days=window_days))
            before = _naive_utc(date_obj + timedelta(days=window_days + 1))
        clean = ""
        if subject:
            clean = re.sub(r"^((re|fwd|fw|aw)\s*:\s*)+", "", subject.replace('"', " ").strip(), flags=re.I)
            clean = clean.strip().lower()
        sender = (sender_email or "").lower()
        out = []
        for path, e in self._index.items():
            if since is not None:
                d = e["received"] or e["date"]
                if d is None or not (since <= _naive_utc(d) < before):
                    continue
            if sender and sender not in e["from"] and sender not in e["reply_to"]:
                continue
            if clean and clean not in e["subject"]:
                continue
            out.append(path)
        return out

    def message_times(self, uid):
        e = self._index.get(uid)
        return (e["received"], e["date"]) if e else (None, None)

    def fetch_message(self, uid):
        try:
            with open(uid, "rb") as fh:
                return email.parser.BytesParser(policy=email.policy.compat32).parse(fh)
        except Exception:
            return None


def _naive_utc(dt):
    from datetime import timezone
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
