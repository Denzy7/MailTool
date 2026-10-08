"""Read-only IMAP access.

The mailbox is always opened with EXAMINE (select readonly=True) and bodies are
fetched with BODY.PEEK, so nothing is ever marked as read. Every network call has
a timeout, and the raw socket is handed to the job so Stop can cut a hung call."""
from __future__ import annotations

import email
import imaplib
import re
import ssl

from mailtool.core import timeutil
from mailtool.mail import bodystructure
from mailtool.mail.mime import decode_mime_words

HEADER_FIELDS = "FROM TO CC REPLY-TO RECEIVED DATE SUBJECT MESSAGE-ID"


class MailError(Exception):
    pass


def _make_classes(on_socket):
    class _SSL(imaplib.IMAP4_SSL):
        def _create_socket(self, timeout):
            sock = imaplib.IMAP4._create_socket(self, timeout)
            on_socket(sock)
            return self.ssl_context.wrap_socket(sock, server_hostname=self.host)

    class _Plain(imaplib.IMAP4):
        def _create_socket(self, timeout):
            sock = imaplib.IMAP4._create_socket(self, timeout)
            on_socket(sock)
            return sock

    return _SSL, _Plain


class MailSession:
    def __init__(self, account, password, job=None, log=None):
        self.acc = account
        self.password = password
        self.job = job
        self._log = log or (job.log if job else (lambda *a, **k: None))
        self.conn = None
        self.mailbox = None
        self.uidvalidity = None

    # ------------------------------------------------------------------ connect
    def _ssl_context(self):
        ctx = ssl.create_default_context()
        if self.acc.get("allow_self_signed"):
            # Still encrypted, but certificate/hostname are not checked: only for
            # servers you trust on networks you trust.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def connect(self, mailbox=None):
        server = (self.acc.get("server") or "").strip()
        port = int(self.acc.get("port") or (993 if self.acc.get("use_ssl", True) else 143))
        user = (self.acc.get("username") or "").strip()
        if not server or not user:
            raise MailError("Set the IMAP server and username in Settings > Account.")
        if not self.password:
            raise MailError("No password entered for %s." % user)
        timeout = int(self.acc.get("timeout") or 60)

        def on_socket(sock):
            if self.job is not None:
                try:
                    # wrap_socket takes over sock's handle; a duplicate cuts the same connection
                    self.job.track_socket(sock.dup())
                except OSError:
                    pass

        SSLCls, PlainCls = _make_classes(on_socket)
        self._log("Connecting to %s:%s ..." % (server, port))
        try:
            if self.acc.get("use_ssl", True):
                self.conn = SSLCls(server, port, ssl_context=self._ssl_context(), timeout=timeout)
            else:
                self.conn = PlainCls(server, port, timeout=timeout)
                try:
                    self.conn.starttls(ssl_context=self._ssl_context())
                except Exception as e:
                    self._log("STARTTLS not available (%s) - continuing UNENCRYPTED." % e, "warn")
        except ssl.SSLCertVerificationError as e:
            raise MailError("Certificate check failed (%s). If this is your own server with a self-signed "
                            "certificate, tick 'Allow self-signed certificate' in Settings." % e.verify_message)
        self._log("Logging in as %s ..." % user)
        try:
            self.conn.login(user, self.password)
        except imaplib.IMAP4.error as e:
            raise MailError("Login failed: %s" % _txt(e))
        if mailbox is not False:
            self.select(mailbox or self.acc.get("mailbox") or "INBOX")
        return self

    def select(self, mailbox):
        status, data = self.conn.select(_quote_mailbox(mailbox), readonly=True)
        if status != "OK":
            raise MailError("Could not open mailbox '%s': %s" % (mailbox, _txt(data)))
        self.mailbox = mailbox
        try:
            uv = self.conn.response("UIDVALIDITY")[1]
            self.uidvalidity = uv[0].decode() if uv and uv[0] else None
        except Exception:
            self.uidvalidity = None
        count = data[0].decode(errors="replace") if data and data[0] else "?"
        self._log("Opened '%s' read-only (%s messages)." % (mailbox, count))
        return count

    def close(self):
        if not self.conn:
            return
        for fn in (self.conn.close, self.conn.logout):
            try:
                fn()
            except Exception:
                pass
        self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def list_mailboxes(self):
        typ, data = self.conn.list()
        boxes = []
        if typ == "OK":
            for raw in data or []:
                if not raw:
                    continue
                line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
                m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
                if m:
                    boxes.append(m.group(1))
        return boxes

    # ------------------------------------------------------------------ search
    def uid_search(self, *criteria):
        typ, data = self.conn.uid("SEARCH", None, *criteria)
        if typ != "OK":
            raise MailError("Search failed: %s" % _txt(data))
        return [u.decode() for u in (data[0] or b"").split()]

    def uids_in_range(self, start, end, tz):
        """UIDs of messages whose arrival time (Received:, local tz) is within
        [start, end]. Server search is padded; exact filtering happens here
        with one batched header fetch per 250 messages."""
        since, before = timeutil.imap_search_window(start, end)
        uids = self.uid_search("SINCE", since, "BEFORE", before)
        self._log("Server returned %d candidate(s); checking their arrival times ..." % len(uids))
        keep = []
        for i in range(0, len(uids), 250):
            if self.job:
                self.job.check()
                self.job.progress(i, len(uids), "checking dates ")
            chunk = uids[i:i + 250]
            typ, data = self.conn.uid("FETCH", ",".join(chunk), "(UID BODY.PEEK[HEADER.FIELDS (RECEIVED DATE)])")
            if typ != "OK":
                keep += chunk            # can't tell - keep them
                continue
            for item in data or []:
                if not isinstance(item, tuple) or not item[0]:
                    continue
                m = re.search(rb"UID (\d+)", item[0])
                if not m:
                    continue
                dt, _ = timeutil.get_received_datetime(email.message_from_bytes(item[1] or b""))
                if timeutil.in_range(timeutil.to_local(dt, tz), start, end):
                    keep.append(m.group(1).decode())
        order = {u: n for n, u in enumerate(uids)}
        return sorted(set(keep), key=lambda u: order.get(u, 0))

    # ------------------------------------------------------------------ fetch
    def fetch_headers(self, uid):
        typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (%s)])" % HEADER_FIELDS)
        raw = _first_literal(data) if typ == "OK" else None
        return email.message_from_bytes(raw) if raw is not None else None

    def fetch_structure(self, uid):
        typ, data = self.conn.uid("FETCH", uid, "(BODYSTRUCTURE)")
        return bodystructure.parse_response(data) if typ == "OK" else None

    def fetch_part(self, uid, part_num):
        typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[%s])" % part_num)
        return _first_literal(data) if typ == "OK" else None

    def fetch_message(self, uid):
        typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")
        raw = _first_literal(data) if typ == "OK" else None
        return email.message_from_bytes(raw) if raw is not None else None

    # ------------------------------------------------------------------ fuzzy lookup (CSV fallback)
    def search_fuzzy(self, subject, sender_email, date_obj, window_days=1):
        """UIDs that plausibly match a CSV row. Tries FROM first and then
        Reply-To, since exported sender columns often hold the Reply-To address."""
        base = []
        if date_obj:
            since = (date_obj - timeutil.timedelta(days=window_days)).strftime(timeutil.DATE_FMT)
            before = (date_obj + timeutil.timedelta(days=window_days + 1)).strftime(timeutil.DATE_FMT)
            base += ["SINCE", since, "BEFORE", before]
        subj = []
        if subject:
            clean = subject.replace('"', " ").replace("\\", " ").strip()
            clean = re.sub(r"^((re|fwd|fw|aw)\s*:\s*)+", "", clean, flags=re.I).strip()
            clean = _imap_ascii(clean[:120])
            if clean:
                subj = ["HEADER", "SUBJECT", '"%s"' % clean]
        tries = []
        if sender_email:
            addr = '"%s"' % sender_email.replace('"', "")
            tries = [base + ["FROM", addr] + subj, base + ["HEADER", "REPLY-TO", addr] + subj]
        else:
            tries = [base + subj]
        for crit in tries:
            if not crit:
                continue
            try:
                uids = self.uid_search(*crit)
            except Exception:
                uids = []
            if uids:
                return uids
        return []

    def message_times(self, uid):
        """(received_dt, date_dt) for a UID, from headers only."""
        typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (RECEIVED DATE)])")
        raw = _first_literal(data) if typ == "OK" else None
        if raw is None:
            return None, None
        msg = email.message_from_bytes(raw)
        rec, src = timeutil.get_received_datetime(msg)
        return (rec if src == "received" else None), timeutil.parse_header_date(msg.get("Date"))


def closest_candidate(uids, target, window_seconds, tz, times_fn, max_check=25):
    """Pick the candidate whose Received: or Date: time is closest to target.
    Naive times count as local (tz) - this is what makes CSV dates written by
    MailTool (local Received time) line up with the server's headers.
    Returns (uid, matched_dt, delta) or (None, None, None)."""
    if target is None:
        return None, None, None
    best = (None, None, None)
    for uid in uids[:max_check]:
        for dt in times_fn(uid):
            d = timeutil.delta_seconds(target, dt, tz)
            if d is None or d > window_seconds:
                continue
            if best[2] is None or d < best[2]:
                best = (uid, dt, d)
    return best


def _first_literal(data):
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _txt(x):
    if isinstance(x, (list, tuple)):
        x = b" ".join(i if isinstance(i, bytes) else str(i).encode() for i in x if i is not None)
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return str(x)


def _quote_mailbox(name):
    if re.search(r'[\s"()\\]', name) and not (name.startswith('"') and name.endswith('"')):
        return '"%s"' % name.replace("\\", "\\\\").replace('"', '\\"')
    return name


def _imap_ascii(s):
    """SEARCH without CHARSET must be ASCII. Use the longest pure-ASCII stretch of
    the subject (a substring the server can still match), or nothing if too short."""
    runs = [r.strip() for r in re.split(r"[^\x20-\x7e]+", s)]
    best = max(runs, key=len) if runs else ""
    best = re.sub(r"\s+", " ", best)
    return best if len(best) >= 4 else ""


def header_summary(msg):
    """Decoded common headers of an email.message.Message."""
    return {
        "subject": decode_mime_words(msg.get("Subject", "")),
        "from": msg.get("From", ""),
        "reply_to": msg.get("Reply-To", ""),
        "to": msg.get("To", ""),
        "message_id": (msg.get("Message-ID") or "").strip(),
    }
