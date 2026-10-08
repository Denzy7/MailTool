"""MIME helpers: header decoding, sender resolution, body/attachment extraction."""
from __future__ import annotations

import base64
import html as html_module
import quopri
import re
from email.header import decode_header
from email.utils import getaddresses, parseaddr


def decode_mime_words(s):
    """Decode an RFC 2047 header (subject, filename, ...) into a str. Never raises."""
    if not s:
        return ""
    s = str(s)
    try:
        parts = decode_header(s)
    except Exception:
        return s
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    # decode_header drops the whitespace between an encoded word and plain text
    joined = "".join(out)
    return re.sub(r"\s+", " ", joined).strip() if "\n" in joined or "\r" in joined else joined


def resolve_sender(raw_from, raw_reply_to):
    """Effective sender: Reply-To when present (noreply@ senders often point
    Reply-To at the address a person reads), otherwise From.
    Returns (name, email, used_reply_to)."""
    from_name, from_email = parseaddr(decode_mime_words(raw_from) if raw_from else "")
    reply_name, reply_email = parseaddr(decode_mime_words(raw_reply_to) if raw_reply_to else "")
    if reply_email and "@" in reply_email:
        return (reply_name or from_name), reply_email, True
    return from_name, from_email, False


def get_reply_to_pair(msg):
    """(name, email) from a message's Reply-To header, or None."""
    if msg is None:
        return None
    name, addr = parseaddr(decode_mime_words(msg.get("Reply-To", "")))
    addr = (addr or "").strip()
    if not addr or "@" not in addr:
        return None
    return (name or "").strip() or None, addr


def address_list(raw):
    raw = decode_mime_words(raw or "")
    return [a for _, a in getaddresses([raw]) if a]


def html_to_text(html):
    """Small HTML -> text fallback: drop scripts/styles/tags, unescape entities."""
    text = re.sub(r"(?is)<(script|style|head).*?>.*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def decode_part_payload(raw_bytes, encoding, charset):
    encoding = (encoding or "7BIT").upper()
    try:
        if encoding == "BASE64":
            raw_bytes = base64.b64decode(raw_bytes, validate=False)
        elif encoding == "QUOTED-PRINTABLE":
            raw_bytes = quopri.decodestring(raw_bytes)
    except (ValueError, base64.binascii.Error):
        pass
    try:
        return raw_bytes.decode(charset or "utf-8", errors="replace")
    except (LookupError, TypeError):
        return raw_bytes.decode("utf-8", errors="replace")


def _decode_text_part(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _is_attachment(part):
    disp = str(part.get("Content-Disposition") or "").lower()
    return "attachment" in disp or bool(part.get_filename())


def extract_full(msg):
    """From a fully downloaded email.message.Message, return
    (plain_text or None, html or None with cid: images inlined, files)
    where files = [(filename, payload_bytes)] for every named part that is not
    an image referenced inline by the HTML."""
    plain = html = None
    cid_map = {}
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        cid = part.get("Content-ID")
        if cid:
            cid_map[cid.strip("<> ").lower()] = part
        if _is_attachment(part):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _decode_text_part(part)
        elif ctype == "text/html" and html is None:
            html = _decode_text_part(part)

    used_cids = set()
    if html and cid_map:
        def sub(m):
            key = m.group(1).strip("<> ").lower()
            p = cid_map.get(key)
            if p is None:
                return m.group(0)
            data = p.get_payload(decode=True) or b""
            used_cids.add(key)
            return "data:%s;base64,%s" % (p.get_content_type(), base64.b64encode(data).decode("ascii"))
        html = re.sub(r"cid:([^\"'\s>)]+)", sub, html, flags=re.IGNORECASE)

    files = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        if not filename:
            continue
        cid = (part.get("Content-ID") or "").strip("<> ").lower()
        if cid and cid in used_cids and "attachment" not in str(part.get("Content-Disposition") or "").lower():
            continue          # an image shown inside the HTML body, not a file attachment
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        files.append((decode_mime_words(filename), payload))
    return plain, html, files


def body_text_of(plain, html):
    if plain and plain.strip():
        return plain.strip()
    if html:
        return html_to_text(html)
    return ""
