"""Test doubles: a fake read-only mailbox built from in-memory emails."""
from __future__ import annotations

import base64
import email
import io
from email.message import EmailMessage

from mailtool.core import timeutil


def make_pdf(text):
    """A one-page PDF containing `text` (needs reportlab)."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def make_docx(text):
    import docx
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_email(subject, sender, received, body="Hello", html=None, attachments=(), reply_to=None, date=None,
               to="me@example.com", inline_image=False):
    m = EmailMessage()
    m["Received"] = "from mx.example.com by mail.example.com; %s" % received
    m["From"] = sender
    m["To"] = to
    if reply_to:
        m["Reply-To"] = reply_to
    m["Subject"] = subject
    m["Date"] = date or received
    m["Message-ID"] = "<%s@test>" % abs(hash((subject, received)))
    m.set_content(body)
    if html is not None:
        m.add_alternative(html, subtype="html")
        if inline_image:
            png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
            m.get_payload()[1].add_related(png, "image", "png", cid="<logo1>")
    for name, data, mt in attachments:
        maintype, subtype = mt.split("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return email.message_from_bytes(m.as_bytes())


def _structure(part):
    """email.message.Message -> parsed BODYSTRUCTURE lists (as mailtool.mail.bodystructure produces)."""
    if part.is_multipart():
        return [_structure(p) for p in part.get_payload()] + [part.get_content_subtype().upper()]
    mt, st = part.get_content_type().split("/")
    params = []
    for k, v in part.get_params() or []:
        if k.lower() == part.get_content_type():
            continue
        params += [k.upper(), v]
    cid = part.get("Content-ID")
    enc = (part.get("Content-Transfer-Encoding") or "7BIT").upper()
    raw = part.get_payload(decode=False)
    size = len(raw.encode() if isinstance(raw, str) else raw)
    disp = part.get("Content-Disposition")
    ext = None
    if disp:
        fn = part.get_filename()
        ext = [disp.split(";")[0].upper(), ["FILENAME", fn] if fn else None]
    node = [mt.upper(), st.upper(), params or None, cid, None, enc, size]
    if mt.lower() == "text":
        node.append(raw.count("\n") if isinstance(raw, str) else 0)
    node += [None, ext, None, None]
    return node


def _part(msg, num):
    p = msg
    for n in num.split("."):
        if not p.is_multipart():       # IMAP: part 1 of a single-part message is its body
            break
        p = p.get_payload()[int(n) - 1]
    raw = p.get_payload(decode=False)
    return raw.encode() if isinstance(raw, str) else raw


class FakeSession:
    """Drop-in for mail.imap.MailSession over a dict {uid: Message}."""
    store = {}
    fetched_full = []

    def __init__(self, account, password, job=None, log=None):
        self.acc = account
        self.job = job
        self.mailbox = None
        self.uidvalidity = "777"

    def connect(self, mailbox=None):
        if mailbox is not False:
            self.mailbox = mailbox or "INBOX"
        return self

    def select(self, mailbox):
        self.mailbox = mailbox

    def close(self):
        pass

    def uids_in_range(self, start, end, tz):
        out = []
        for uid, m in self.store.items():
            dt, _ = timeutil.get_received_datetime(m)
            if timeutil.in_range(timeutil.to_local(dt, tz), start, end):
                out.append(uid)
        return out

    def fetch_headers(self, uid):
        m = self.store.get(uid)
        if m is None:
            return None
        h = email.message.Message()
        for k in ("From", "To", "Reply-To", "Received", "Date", "Subject", "Message-ID"):
            for v in m.get_all(k) or []:
                h[k] = v
        return h

    def fetch_structure(self, uid):
        return _structure(self.store[uid])

    def fetch_part(self, uid, num):
        return _part(self.store[uid], num)

    def fetch_message(self, uid):
        type(self).fetched_full.append(uid)
        return self.store.get(uid)

    def search_fuzzy(self, subject, sender_email, date_obj, window_days=1):
        out = []
        for uid, m in self.store.items():
            if subject and subject.lower() not in (m["Subject"] or "").lower():
                continue
            if sender_email and sender_email.lower() not in ((m["From"] or "") + (m["Reply-To"] or "")).lower():
                continue
            out.append(uid)
        return out

    def message_times(self, uid):
        m = self.store[uid]
        rec, _ = timeutil.get_received_datetime(m)
        return rec, timeutil.parse_header_date(m["Date"])
