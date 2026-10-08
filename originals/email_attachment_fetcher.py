#!/usr/bin/env python3
"""
Email Attachment Fetcher
-------------------------
A small Tkinter GUI that connects to an IMAP(S) server, searches a mailbox
for messages received on/after a given date, and saves all attachments to
a folder you choose.

Design notes on NOT marking messages as read:
  1. The mailbox is opened in read-only mode: imap.select(mailbox, readonly=True)
     This puts the IMAP session in "EXAMINE" mode, which the server is
     required to treat as read-only -- it will refuse to let the \\Seen
     flag (or any flag) change no matter what you fetch.
  2. As a second line of defense, we fetch bodies with BODY.PEEK[] instead
     of BODY[]. PEEK explicitly tells the server "don't set \\Seen even if
     you would normally do so" -- this matters if you ever remove the
     readonly select above.

Usage:
    python3 email_attachment_fetcher.py

Optional dependencies:
    pip install pypdf      # only needed for the "merge PDF attachments" option
    pip install reportlab  # only needed for the "Render Email Info PDFs" option
"""

import imaplib
import socket
import base64
import csv
import email
import json
import os
import quopri
import re
import ssl
import queue
import threading
import time
import types
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime, getaddresses

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from pypdf import PdfReader, PdfWriter
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
    )
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

try:
    from reportlab.platypus import Image as RLImage
    from PIL import Image as PILImage
    from playwright.sync_api import sync_playwright
    GRAPHICAL_AVAILABLE = REPORTLAB_AVAILABLE
except ImportError:
    GRAPHICAL_AVAILABLE = False

import io
from datetime import timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

DEFAULT_LOCAL_TZ = "Africa/Nairobi"


def resolve_timezone(tz_text):
    """Accept an IANA name ('Africa/Nairobi') or a fixed offset ('+03:00').
    Falls back to the machine's own local timezone if neither parses."""
    tz_text = (tz_text or "").strip()
    if tz_text and ZoneInfo is not None:
        try:
            return ZoneInfo(tz_text)
        except Exception:
            pass
    m = re.fullmatch(r"(?:UTC)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", tz_text, re.IGNORECASE)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return timezone(sign * delta)
    return datetime.now().astimezone().tzinfo


def get_received_datetime(msg):
    """Receipt time from the topmost Received: header (the final delivery
    hop into the mailbox -- what Zimbra shows). The timestamp is whatever
    follows the LAST ';' in that header. Falls back to the next Received
    header, then to Date:. Returns (aware_datetime_or_None, source)."""
    for value in msg.get_all("Received") or []:
        value = re.sub(r"\s+", " ", str(value))
        if ";" not in value:
            continue
        stamp = value.rsplit(";", 1)[1].strip()
        try:
            dt = parsedate_to_datetime(stamp)
            if dt is not None:
                return dt, "received"
        except (TypeError, ValueError, IndexError):
            continue
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt is not None:
            return dt, "date"
    except (TypeError, ValueError, IndexError):
        pass
    return None, "none"


def to_local(dt, tz):
    """Convert an aware datetime to the chosen local zone. A naive one
    (header had no offset) is assumed to already be local."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


_URL_RE = re.compile(r"(https?://[^\s<>\"]+)")


def linkify_escaped(escaped_text):
    """Make bare URLs clickable in the text layout (like Zimbra does).
    Runs on already-escaped text; trailing punctuation stays outside the link."""
    def sub(m):
        url = m.group(1).rstrip(".,;:!?)]'")
        return f'<a href="{url}" color="#1a4d8f">{url}</a>{m.group(1)[len(url):]}'
    return _URL_RE.sub(sub, escaped_text)


def apply_simple_markdown(escaped_text):
    """*text* -> bold. Runs on already-escaped text so it can't inject markup."""
    return re.sub(r"\*([^*\n]+?)\*", r"<b>\1</b>", escaped_text)

IMAP_TIMEOUT_S = 60

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".email_attachment_fetcher_config.json")


def decode_mime_words(s):
    """Decode a MIME-encoded header (e.g. subject, filename) into a str."""
    if not s:
        return ""
    parts = decode_header(s)
    decoded = []
    for text, enc in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


def resolve_sender(raw_from, raw_reply_to):
    """Pick the effective sender name/email for a message.

    Prefers Reply-To when present (common for noreply@... senders that set
    Reply-To to the address a human actually reads), otherwise falls back
    to From. Returns (name, email, used_reply_to).
    """
    from_name, from_email = parseaddr(decode_mime_words(raw_from) if raw_from else "")
    reply_name, reply_email = parseaddr(decode_mime_words(raw_reply_to) if raw_reply_to else "")

    if reply_email:
        # Prefer Reply-To's own display name; fall back to From's name
        # (e.g. Reply-To: bare-address-only, From: "Acme Support" <noreply@...>)
        name = reply_name or from_name
        return name, reply_email, True

    return from_name, from_email, False


def sanitize_for_path(text, max_len=80):
    """Make a string safe to use as a file/folder name across OSes."""
    text = text.strip()
    # Replace characters illegal on Windows/macOS/Linux filesystems
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    # Collapse whitespace and strip trailing dots/spaces (Windows quirk)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "unknown"
    return text[:max_len]


MESSAGE_MARKER = ".message-uid"   # inside each message folder: which email owns it


def claim_message_dir(outdir, date_folder, sender_email, subject, mailbox, uid):
    """Return (and create) the folder for ONE email. Normally
    <date>/<sender> - <subject>; if a different email already owns that name
    (same sender, subject and day), this one gets <sender>_UID<uid> - <subject>.
    Ownership is recorded in a small marker file, so the attachment fetcher
    and the EMAILINFO renderer always pick the same folder for the same email,
    whatever order or run they happen in. A pre-existing folder without a
    marker (made by an older version) is taken over by the first email to
    claim it."""
    me = f"{mailbox}\n{uid}"
    date_dir = os.path.join(outdir, date_folder)
    for name in (sanitize_for_path(f"{sender_email} - {subject}"),
                 sanitize_for_path(f"{sender_email}_UID{uid} - {subject}")):
        folder = os.path.join(date_dir, name)
        marker = os.path.join(folder, MESSAGE_MARKER)
        try:
            with open(marker, encoding="utf-8") as fh:
                owner = fh.read().strip()
        except OSError:
            owner = None
        if owner is not None and owner != me:
            continue                      # another email's folder -- try the UID name
        os.makedirs(folder, exist_ok=True)
        if owner is None:
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(me)
        return folder
    return folder                         # the UID-named folder is always ours


def touch_path(path, dt):
    """Set both mtime and atime of a file or folder to a given datetime."""
    try:
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
    except (OSError, OverflowError, ValueError):
        pass


def html_to_text(html):
    """Very small HTML->text fallback: strip tags/scripts/styles, unescape entities."""
    import html as html_module
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# BODYSTRUCTURE parsing: lets us ask the server for a message's MIME layout
# (a cheap, tiny response) and then fetch ONLY the specific text part number
# we want -- so attachment bytes are never requested from the server at all,
# not even into memory.
# ---------------------------------------------------------------------------

def _tokenize_imap_list(s):
    """Tokenize an IMAP parenthesized-list string into atoms/strings/() tokens."""
    tokens = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "(":
            tokens.append("(")
            i += 1
            continue
        if c == ")":
            tokens.append(")")
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n:
                    buf.append(s[j + 1])
                    j += 2
                else:
                    buf.append(s[j])
                    j += 1
            tokens.append("".join(buf))
            i = j + 1
            continue
        j = i
        while j < n and s[j] not in " \t\r\n()\"":
            j += 1
        atom = s[i:j]
        if atom.upper() == "NIL":
            tokens.append(None)
        else:
            try:
                tokens.append(int(atom))
            except ValueError:
                tokens.append(atom)
        i = j
    return tokens


def _parse_imap_tokens(tokens):
    """Recursively turn tokens into nested Python lists, per RFC 3501 syntax."""
    def parse_expr(pos):
        tok = tokens[pos]
        if tok == "(":
            pos += 1
            lst = []
            while tokens[pos] != ")":
                val, pos = parse_expr(pos)
                lst.append(val)
            return lst, pos + 1
        return tok, pos + 1

    value, _ = parse_expr(0)
    return value


def _extract_balanced_parens(s, start):
    """Return the substring of s starting at index `start` (a '(') through
    its matching ')', ignoring parens that appear inside quoted strings."""
    depth = 0
    in_quotes = False
    i = start
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"' and (i == 0 or s[i - 1] != "\\"):
            in_quotes = not in_quotes
        elif not in_quotes:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
        i += 1
    return s[start:]


def parse_bodystructure_response(msg_data):
    """Parse imaplib's raw FETCH (BODYSTRUCTURE) response into a nested list."""
    chunks = []
    for part in msg_data:
        if isinstance(part, tuple):
            chunks.append(part[0])
            chunks.append(part[1])
        elif part is not None:
            chunks.append(part)
    raw = b"".join(c for c in chunks if isinstance(c, bytes))
    text = raw.decode("latin-1", errors="replace")

    idx = text.upper().find("BODYSTRUCTURE")
    if idx == -1:
        return None
    paren_start = text.find("(", idx)
    if paren_start == -1:
        return None
    balanced = _extract_balanced_parens(text, paren_start)
    tokens = _tokenize_imap_list(balanced)
    if not tokens:
        return None
    try:
        return _parse_imap_tokens(tokens)
    except (IndexError, ValueError):
        return None


def _params_have_name(params):
    """True if a Content-Type/Content-Disposition parameter list has a NAME/FILENAME."""
    if not isinstance(params, list):
        return False
    for i in range(0, len(params) - 1, 2):
        key = params[i]
        if isinstance(key, str) and key.upper() in ("NAME", "FILENAME"):
            return True
    return False


def _param_value(params, wanted_key):
    if not isinstance(params, list):
        return None
    for i in range(0, len(params) - 1, 2):
        key = params[i]
        if isinstance(key, str) and key.upper() == wanted_key.upper():
            return params[i + 1]
    return None


def find_text_parts(struct, prefix=""):
    """Yield (part_number, subtype, charset, encoding, has_name) for every
    leaf TEXT/PLAIN or TEXT/HTML part in a parsed BODYSTRUCTURE tree."""
    if not isinstance(struct, list) or not struct:
        return
    if isinstance(struct[0], list):
        # Multipart: children are the leading list elements; the next
        # element after them is the multipart subtype (ALTERNATIVE/MIXED/...)
        i = 0
        children = []
        while i < len(struct) and isinstance(struct[i], list):
            children.append(struct[i])
            i += 1
        for idx, child in enumerate(children, start=1):
            child_prefix = f"{prefix}.{idx}" if prefix else str(idx)
            yield from find_text_parts(child, child_prefix)
    else:
        part_type = struct[0] if isinstance(struct[0], str) else ""
        part_subtype = struct[1] if len(struct) > 1 and isinstance(struct[1], str) else ""
        if part_type.upper() == "TEXT" and part_subtype.upper() in ("PLAIN", "HTML"):
            params = struct[2] if len(struct) > 2 else None
            encoding = struct[5] if len(struct) > 5 and isinstance(struct[5], str) else "7BIT"
            charset = _param_value(params, "CHARSET") or "utf-8"
            part_num = prefix if prefix else "1"
            yield (part_num, part_subtype.upper(), charset, encoding, _params_have_name(params))


def pick_body_part(struct):
    """Choose the best TEXT/PLAIN (preferred) or TEXT/HTML part to fetch,
    skipping ones that look like attached text files (have a NAME param)."""
    parts = list(find_text_parts(struct))
    plain = [p for p in parts if p[1] == "PLAIN" and not p[4]]
    html = [p for p in parts if p[1] == "HTML" and not p[4]]
    if plain:
        return plain[0]
    if html:
        return html[0]
    return None


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


def get_body_text(msg):
    """Extract a plain-text body from an already-fully-fetched email.message.Message.

    Not used by the CSV export (which fetches only the specific text part
    via BODYSTRUCTURE to avoid ever touching attachment bytes) -- kept here
    as a general-purpose utility in case a full message is fetched elsewhere.
    Prefers text/plain; falls back to a tag-stripped text/html part.
    """
    plain_text = None
    html_text = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if content_type == "text/plain" and plain_text is None:
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    plain_text = payload.decode(charset, errors="replace")
            elif content_type == "text/html" and html_text is None:
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    html_text = payload.decode(charset, errors="replace")
    else:
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if content_type == "text/html":
                html_text = decoded
            else:
                plain_text = decoded

    if plain_text is not None:
        return plain_text.strip()
    if html_text is not None:
        return html_to_text(html_text)
    return ""


# ---------------------------------------------------------------------------
# "Render Email Info PDF" support: a Zimbra-style print layout (Subject/
# From/To/Date header block, body, attachments list) rendered to A4 PDF.
# Attachment metadata (name + size) comes entirely from BODYSTRUCTURE --
# the attachment bytes themselves are never fetched.
# ---------------------------------------------------------------------------

def _find_param_recursive(node, wanted_keys):
    """Search a parsed BODYSTRUCTURE leaf (and any nested lists inside it,
    e.g. the Content-Disposition extension field) for a NAME/FILENAME-style
    parameter, regardless of exactly where the server put it."""
    if not isinstance(node, list):
        return None
    for i in range(0, len(node) - 1, 2):
        key = node[i]
        if isinstance(key, str) and key.upper() in wanted_keys:
            return node[i + 1]
    for child in node:
        if isinstance(child, list):
            found = _find_param_recursive(child, wanted_keys)
            if found:
                return found
    return None


def find_attachment_parts(struct, body_part_num, prefix="", skip_parts=frozenset()):
    """Yield (filename, size_in_bytes) for every leaf part in a parsed
    BODYSTRUCTURE tree that is a real attachment/inline file -- i.e.
    everything except the chosen body part AND its unnamed text/plain or
    text/html alternative siblings (an ALTERNATIVE group's other rendering
    of the same content, not a separate attachment). Never touches part
    content, only the structure description the server already sent."""
    if not isinstance(struct, list) or not struct:
        return
    if isinstance(struct[0], list):
        i = 0
        children = []
        while i < len(struct) and isinstance(struct[i], list):
            children.append(struct[i])
            i += 1
        for idx, child in enumerate(children, start=1):
            child_prefix = f"{prefix}.{idx}" if prefix else str(idx)
            yield from find_attachment_parts(child, body_part_num, child_prefix, skip_parts)
    else:
        part_num = prefix if prefix else "1"
        part_type = struct[0] if isinstance(struct[0], str) else "APPLICATION"
        part_subtype = struct[1] if len(struct) > 1 and isinstance(struct[1], str) else "OCTET-STREAM"
        params = struct[2] if len(struct) > 2 else None
        has_name = _params_have_name(params) or bool(_find_param_recursive(struct, {"NAME", "FILENAME"}))
        is_unnamed_text = part_type.upper() == "TEXT" and part_subtype.upper() in ("PLAIN", "HTML") and not has_name
        if part_num == body_part_num or part_num in skip_parts or is_unnamed_text:
            return
        size = None
        for v in struct:
            if isinstance(v, int):
                size = v
                break
        name = _find_param_recursive(struct, {"NAME", "FILENAME"})
        if not name:
            name = f"part_{part_num} ({part_type}/{part_subtype})"
        yield (decode_mime_words(name) if isinstance(name, str) else str(name), size)


def format_file_size(num_bytes):
    """Match the odg template's own style: '106 kB', '1.4 MB', etc."""
    if num_bytes is None:
        return "unknown size"
    if num_bytes < 1024:
        return f"{num_bytes} bytes"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} kB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def format_zimbra_datetime(dt):
    """'Wednesday July 15, 2026 10:58:57 AM' -- the exact style shown in the
    odg template, built manually to avoid %-d's non-portability on Windows."""
    if dt is None:
        return "(unknown date)"
    return f"{dt:%A %B} {dt.day}, {dt:%Y %I:%M:%S %p}"


DEFAULT_HEADER_LEFT = ""          # blank -> your username
DEFAULT_HEADER_RIGHT = "EMAIL UID: <UID>"
TEMPLATE_SPECIFIERS = ("UID", "EMAIL", "SUBJECT", "MAILBOX", "DATE")


def expand_template(template, values):
    """Replace <UID>, <EMAIL>, <SUBJECT>, <MAILBOX>, <DATE> (any case) with
    this message's values. Unknown <...> text is left exactly as typed."""
    def sub(m):
        key = m.group(1).upper()
        return str(values[key]) if key in values else m.group(0)
    return re.sub(r"<([A-Za-z]+)>", sub, template or "")


def _escape_pdf_text(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _InfoPdfCanvas(rl_canvas.Canvas if REPORTLAB_AVAILABLE else object):
    """Draws the same header-left/header-right/footer-left/footer-right
    decorations on every page, and needs a 2-pass build to know the total
    page count for the 'Page X of Y' footer -- the standard reportlab
    NumberedCanvas recipe."""

    def __init__(self, *args, **kwargs):
        self._header_left = kwargs.pop("header_left", "")
        self._header_right = kwargs.pop("header_right", "")
        self._generated_stamp = kwargs.pop("generated_stamp", "")
        rl_canvas.Canvas.__init__(self, *args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            # Restoring a page's saved state would also rewind reportlab's
            # link counter, giving every page's header link the same internal
            # name (a crash on multi-page PDFs) -- so carry it forward.
            link_count = getattr(self, "_annotationCount", 0)
            self.__dict__.update(state)
            self._annotationCount = max(link_count, getattr(self, "_annotationCount", 0))
            self._draw_decorations(total_pages)
            rl_canvas.Canvas.showPage(self)
        rl_canvas.Canvas.save(self)

    def _draw_decorations(self, total_pages):
        draw_page_decorations(self, self._pageNumber, total_pages,
                              self._header_left, self._header_right, self._generated_stamp)


def draw_page_decorations(c, page_no, total_pages, header_left, header_right, stamp):
    """Page header/footer: left label, right label or link, Page X of Y, and
    the generated-at stamp. Shared by the text/image PDFs and the overlay
    laid on top of Chromium-printed pages."""
    width, height = A4
    margin = 1.5 * cm
    y_top = height - 1.0 * cm

    # Header: left and right share one line. If they'd collide (e.g. a
    # long link template), shrink the right side's font, then truncate.
    c.setFont("Helvetica", 10)
    left_w = c.stringWidth(header_left, "Helvetica", 10)
    c.drawString(margin, y_top, header_left)

    room = width - 2 * margin - left_w - (0.8 * cm if header_left else 0)
    size = 10
    while size > 6 and c.stringWidth(header_right, "Helvetica", size) > room:
        size -= 0.5
    shown = header_right
    while shown and c.stringWidth(shown, "Helvetica", size) > room:
        shown = shown[:-2] + "\u2026"
    c.setFont("Helvetica", size)
    c.drawRightString(width - margin, y_top, shown)
    if re.match(r"https?://", header_right):
        text_w = c.stringWidth(shown, "Helvetica", size)
        c.linkURL(header_right, (width - margin - text_w, y_top - 2, width - margin, y_top + size), relative=0)

    c.setFont("Helvetica", 10)
    c.drawString(margin, 1.0 * cm, f"Page {page_no} of {total_pages}")
    c.drawRightString(width - margin, 1.0 * cm, stamp)


# ---------------------------------------------------------------------------
# Graphical HTML rendering: draw the email's real HTML in headless Chromium
# and embed the result as images, so the PDF looks like the printed email.
# ---------------------------------------------------------------------------

def pick_html_part(struct):
    for p in find_text_parts(struct):
        if p[1] == "HTML" and not p[4]:
            return p
    return None


def _find_part_by_cid(struct, cid, prefix=""):
    """Locate the part whose Content-ID matches `cid` (an inline image the
    HTML refers to as src="cid:..."). Returns (part_num, mime, encoding)."""
    if not isinstance(struct, list) or not struct:
        return None
    if isinstance(struct[0], list):
        i = 0
        while i < len(struct) and isinstance(struct[i], list):
            child_prefix = f"{prefix}.{i + 1}" if prefix else str(i + 1)
            found = _find_part_by_cid(struct[i], cid, child_prefix)
            if found:
                return found
            i += 1
        return None
    part_id = struct[3] if len(struct) > 3 else None
    if isinstance(part_id, str) and part_id.strip("<> ").lower() == cid.strip("<> ").lower():
        mime = f"{struct[0]}/{struct[1]}".lower() if isinstance(struct[1], str) else "application/octet-stream"
        encoding = struct[5] if len(struct) > 5 and isinstance(struct[5], str) else "7BIT"
        return (prefix or "1", mime, encoding)
    return None


def inline_cid_images(html, struct, imap, num):
    """Replace src="cid:..." references with data: URIs. Fetches ONLY the
    specific inline image parts the HTML actually references -- regular
    file attachments are still never downloaded."""
    cids = set(re.findall(r"cid:([^\"'\s>)]+)", html, flags=re.IGNORECASE))
    inlined_parts = set()
    for cid in cids:
        found = _find_part_by_cid(struct, cid)
        if not found:
            continue
        part_num, mime, encoding = found
        status, part_data = imap.fetch(num, f"(BODY.PEEK[{part_num}])")
        if status != "OK" or not part_data or part_data[0] is None or not isinstance(part_data[0], tuple):
            continue
        raw = part_data[0][1]
        try:
            if encoding.upper() == "BASE64":
                raw = base64.b64decode(raw, validate=False)
            elif encoding.upper() == "QUOTED-PRINTABLE":
                raw = quopri.decodestring(raw)
        except (ValueError, base64.binascii.Error):
            continue
        data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        html = re.sub(r"cid:" + re.escape(cid), lambda _m: data_uri, html, flags=re.IGNORECASE)
        inlined_parts.add(part_num)
    return html, inlined_parts


MAX_REMOTE_IMAGE_BYTES = 15 * 1024 * 1024
MAX_REMOTE_REDIRECTS = 5
MAX_RENDER_WIDTH_CSS = 2400       # wider emails are still captured, then scaled to A4 width
MAX_RENDER_PAGES = 60             # longer bodies are cut, with a log note
RENDER_TILE_HEIGHT_CSS = 1000

# Quality slider: level -> (name, browser pixel scale, max output width in px
# across the A4 text width). Text width is 16.9 cm = 6.65 in, so 1600 px ~ 240 dpi.
QUALITY_LEVELS = {
    1: ("Draft", 1, 800),
    2: ("Normal", 2, 1600),
    3: ("High", 3, 2400),
    4: ("Max", 4, 3200),
}
DEFAULT_QUALITY = 2

# How an HTML email body goes into the PDF.
BODY_MODES = {
    "print": "Printed (selectable text & links)",
    "image": "Image snapshot",
    "text": "Plain text",
}
DEFAULT_BODY_MODE = "print"

# Worst-case memory per render worker (Python + its Chromium), measured on a
# very wide newsletter. Used to cap parallel renders to what the machine has.
WORKER_MEMORY_MB = {1: 450, 2: 700, 3: 800, 4: 900}   # image mode, by quality
PRINT_WORKER_MEMORY_MB = 450


def available_memory_mb():
    """Memory the OS can hand out right now, or None if it can't be read."""
    try:
        with open("/proc/meminfo") as fh:                      # Linux
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    try:                                                       # Windows
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullAvailPhys // (1024 * 1024)
    except Exception:
        pass
    return None


def quality_label(level):
    name, _, out_w = QUALITY_LEVELS[level]
    return f"{name} (~{round(out_w / (PDF_CONTENT_WIDTH_CM / 2.54))} dpi)"


def _looks_like_image(body):
    """Magic-byte check for servers that send images as octet-stream."""
    head = body[:16]
    return (
        head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") or head.startswith(b"GIF8")
        or (head.startswith(b"RIFF") and body[8:12] == b"WEBP") or head.startswith(b"BM")
        or head.startswith(b"\x00\x00\x01\x00") or b"<svg" in body[:512].lower()
    )


_EXPAND_SCROLL_BOXES_JS = """() => {
  for (const e of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(e);
    const scrolls = /(auto|scroll)/.test(cs.overflowY + ' ' + cs.overflowX);
    if (scrolls && (e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1)) {
      e.style.setProperty('overflow', 'visible', 'important');
      e.style.setProperty('height', 'auto', 'important');
      e.style.setProperty('max-height', 'none', 'important');
    }
  }
}"""

_MEASURE_CONTENT_JS = """() => {
  let bottom = 0;
  for (const e of document.body.querySelectorAll('*')) {
    const r = e.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) bottom = Math.max(bottom, r.bottom);
  }
  const pad = parseFloat(getComputedStyle(document.body).paddingBottom) || 0;
  const height = Math.ceil(bottom + window.scrollY + pad) || document.documentElement.scrollHeight;
  const width = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
  return [width, Math.max(height, 1)];
}"""


# Printed mode lays the email out at the A4 text width (21 cm - 2 x 1.8 cm).
PRINT_WIDTH_CSS = round((21.0 - 2 * 1.8) / 2.54 * 96)
PRINT_MARGINS = {"top": "2.2cm", "bottom": "2.0cm", "left": "1.8cm", "right": "1.8cm"}

# Printing doesn't scroll, so images marked loading="lazy" would never load.
_EAGER_IMAGES_JS = """() => {
  for (const i of document.querySelectorAll('img[loading]')) i.loading = 'eager';
}"""
_IMAGES_SETTLED_JS = "() => Array.from(document.images).every(i => i.complete)"

# Our Subject/From/To/Date block and Attachments list go INTO the printed
# page as real text, inside a shadow root: the email's CSS can't restyle
# them and theirs can't leak out. zoom undoes the print scale-down applied
# to wide emails, so these blocks always print at the same size.
_INJECT_BLOCKS_JS = """(a) => {
  const make = (html) => {
    const host = document.createElement('div');
    host.style.cssText = 'all:initial;display:block;zoom:' + a.zoom + ';width:' + (100 / a.zoom) + '%;';
    host.attachShadow({mode: 'open'}).innerHTML = html;
    return host;
  };
  if (a.top) document.body.insertBefore(make(a.top), document.body.firstChild);
  if (a.bottom) document.body.appendChild(make(a.bottom));
}"""


class HtmlRenderer:
    """One headless Chromium for a whole run. The sender's JavaScript is
    always disabled. Network access is blocked except, optionally, http(s)
    IMAGES: those are fetched with redirects followed (301/302/...) and only
    used if the final response really is an image."""

    def __init__(self, width_px, allow_remote_images=False):
        self.width_px = width_px
        self.allow_remote_images = allow_remote_images
        self.stats = {}
        self._pw = None
        self._browser = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _handle_request(self, route):
        req = route.request
        if (
            self.allow_remote_images
            and req.resource_type == "image"
            and req.url.lower().startswith(("http://", "https://"))
        ):
            try:
                # route.fetch follows 301/302/303/307/308 itself, so we only
                # ever judge the FINAL response.
                resp = route.fetch(max_redirects=MAX_REMOTE_REDIRECTS, timeout=10000)
                body = resp.body()
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if (
                    resp.ok
                    and 0 < len(body) <= MAX_REMOTE_IMAGE_BYTES
                    and (ctype.startswith("image/") or _looks_like_image(body))
                ):
                    route.fulfill(status=200, body=body,
                                  content_type=ctype if ctype.startswith("image/") else None)
                    self.stats["remote_images"] = self.stats.get("remote_images", 0) + 1
                    return
            except Exception:
                pass
        self.stats["blocked"] = self.stats.get("blocked", 0) + 1
        route.abort()

    @staticmethod
    def _document(html):
        base = (
            '<meta charset="utf-8"><style>'
            "html,body{background:#fff;height:auto!important;min-height:0!important;"
            "overflow:visible!important;}"
            "body{margin:0;padding:6px;font-family:Arial,Helvetica,sans-serif;}"
            "img{max-width:100%;height:auto;}</style>"
        )
        # Keep the email's own <!DOCTYPE> first: putting anything before
        # it silently switches Chromium into quirks mode, which changes
        # how heights (and scroll boxes) lay out.
        m = re.match(r"\s*(<!doctype[^>]*>)", html, flags=re.IGNORECASE)
        return (m.group(1) + base + html[m.end():]) if m else (base + html)

    def print_pdf(self, html, top_html="", bottom_html=""):
        """Print the email to PDF the way a browser's Print does (what Zimbra
        uses): real, selectable text and clickable links. Wide emails are
        scaled down to fit the A4 width. Returns the PDF bytes (body pages
        only -- page headers/footers are laid over them afterwards)."""
        self.stats = {"remote_images": 0, "blocked": 0, "truncated": False}
        context = self._browser.new_context(
            java_script_enabled=False,  # sender scripts never run; our evaluate() still works
            viewport={"width": PRINT_WIDTH_CSS, "height": RENDER_TILE_HEIGHT_CSS},
        )
        try:
            context.route("**/*", self._handle_request)
            page = context.new_page()
            page.set_content(self._document(html), wait_until="domcontentloaded", timeout=30000)
            page.evaluate(_EAGER_IMAGES_JS)
            try:
                page.wait_for_load_state("load", timeout=30000)
            except Exception:
                pass
            # Give remote images up to 15 s to settle. (wait_for_timeout keeps
            # Playwright processing requests; time.sleep would stall them.)
            for _ in range(75):
                if page.evaluate(_IMAGES_SETTLED_JS):
                    break
                page.wait_for_timeout(200)
            page.evaluate(_EXPAND_SCROLL_BOXES_JS)
            width, _ = page.evaluate(_MEASURE_CONTENT_JS)
            width = min(max(width, PRINT_WIDTH_CSS), MAX_RENDER_WIDTH_CSS)
            scale = max(0.1, min(1.0, PRINT_WIDTH_CSS / width))
            page.evaluate(_INJECT_BLOCKS_JS, {"top": top_html, "bottom": bottom_html, "zoom": 1 / scale})
            return page.pdf(format="A4", print_background=True, scale=scale,
                            margin=PRINT_MARGINS, prefer_css_page_size=False)
        finally:
            context.close()

    def _load(self, html, scale):
        """Open a fresh page at the given pixel scale and lay the email out.
        Returns (context, page, width_css, height_css)."""
        context = self._browser.new_context(
            java_script_enabled=False,  # sender scripts never run; our evaluate() still works
            viewport={"width": self.width_px, "height": RENDER_TILE_HEIGHT_CSS},
            device_scale_factor=scale,
        )
        try:
            context.route("**/*", self._handle_request)
            page = context.new_page()
            page.set_content(self._document(html), wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("load", timeout=30000)  # remote images, if allowed
            except Exception:
                pass  # render whatever arrived

            # Like printing: unroll any inner scroll box so all of its content
            # is laid out instead of being clipped to the box.
            page.evaluate(_EXPAND_SCROLL_BOXES_JS)
            width, height = page.evaluate(_MEASURE_CONTENT_JS)
            if width > self.width_px:
                # Lay the email out at its own width so nothing is clipped.
                width = min(width, MAX_RENDER_WIDTH_CSS)
                page.set_viewport_size({"width": width, "height": RENDER_TILE_HEIGHT_CSS})
                width, height = page.evaluate(_MEASURE_CONTENT_JS)
                width = min(width, MAX_RENDER_WIDTH_CSS)
            return context, page, max(width, self.width_px), height
        except Exception:
            context.close()
            raise

    def render_strips(self, html, first_page_room_cm, quality=DEFAULT_QUALITY):
        """Render the HTML and return it as page-sized PNG strips:
        [(png_bytes, height_cm), ...]. The width is the email's real layout
        width scaled to the A4 text width.

        Captured one viewport-sized tile at a time (some Chromium builds
        squash very tall full-page screenshots), and cut into pages AS it is
        captured, so memory stays at about two pages even at Max quality."""
        _, level_scale, max_out_w = QUALITY_LEVELS.get(quality, QUALITY_LEVELS[DEFAULT_QUALITY])
        self.stats = {"remote_images": 0, "blocked": 0, "truncated": False}

        context, page, width, height = self._load(html, level_scale)
        try:
            # A wide email gets shrunk to the page's pixel width anyway, so
            # rendering it at the full quality scale only makes pixels that
            # are thrown away -- and that is where the memory goes (a very
            # wide email at Max was ~1.9 GB per worker). Render it at just
            # enough scale for the target width (+25% for smooth downscaling).
            scale = round(min(level_scale, max(1.0, max_out_w * 1.25 / width)), 3)
            if scale < level_scale - 0.01:
                context.close()
                self.stats["remote_images"] = self.stats["blocked"] = 0  # counted again on reload
                context, page, width, height = self._load(html, scale)

            def px(css):
                return int(round(css * scale))

            out_w = min(px(width), max_out_w)
            factor = out_w / px(width)
            slicer = _PageSlicer(out_w, first_page_room_cm)
            tile = RENDER_TILE_HEIGHT_CSS
            y = 0
            while y < height and not slicer.full:
                # Can't scroll past the end, so the last tile is taken from
                # the bottom and only its unseen part is kept.
                scroll_to = min(y, max(0, height - tile))
                page.evaluate(f"window.scrollTo(0, {scroll_to})")
                shot = PILImage.open(io.BytesIO(page.screenshot(type="png"))).convert("RGB")
                keep_css = min(tile - (y - scroll_to), height - y)
                top_px = px(y - scroll_to)
                part = shot.crop((0, top_px, min(shot.width, px(width)), min(shot.height, top_px + px(keep_css))))
                del shot
                if factor < 1:
                    part = part.resize((out_w, max(1, round(part.height * factor))), PILImage.LANCZOS)
                slicer.add(part)
                y += keep_css
            strips = slicer.finish()
            # Cut short if we stopped capturing early, or if rows were left
            # over once the page cap was reached.
            if slicer.full and (y < height or slicer.buf is not None):
                self.stats["truncated"] = True
            return strips
        finally:
            context.close()


class _PageSlicer:
    """Collects rendered rows and cuts them into page-height PNG strips as
    soon as enough has arrived. Each cut is snapped up to a blank pixel row
    so a line of text is never split across two pages."""

    def __init__(self, width_px, first_page_room_cm):
        self.w = width_px
        self.px_per_cm = width_px / PDF_CONTENT_WIDTH_CM
        room = first_page_room_cm if first_page_room_cm >= 3 else PDF_CONTENT_HEIGHT_CM
        self.room_px = int(room * self.px_per_cm)
        self.buf = None
        self.strips = []

    @property
    def full(self):
        return len(self.strips) >= MAX_RENDER_PAGES

    def add(self, part):
        if self.buf is None:
            self.buf = part
        else:
            merged = PILImage.new("RGB", (self.w, self.buf.height + part.height), "white")
            merged.paste(self.buf, (0, 0))
            merged.paste(part, (0, self.buf.height))
            self.buf = merged
        while self.buf is not None and self.buf.height > self.room_px and not self.full:
            self._cut(self._snap(self.room_px))

    def finish(self):
        while self.buf is not None and self.buf.height > 0 and not self.full:
            cut = self._snap(self.room_px) if self.buf.height > self.room_px else self.buf.height
            self._cut(cut)
        return self.strips

    def _snap(self, cut):
        limit = int(cut * 0.2)
        region = self.buf.crop((0, cut - limit, self.w, cut + 1)).convert("L")
        for back in range(limit):
            row = region.height - 1 - back
            lo, hi = region.crop((0, row, self.w, row + 1)).getextrema()
            if hi - lo < 10:
                return cut - back
        return cut

    def _cut(self, cut):
        strip = self.buf.crop((0, 0, self.w, cut))
        out = io.BytesIO()
        strip.save(out, format="PNG", optimize=False)
        self.strips.append((out.getvalue(), cut / self.px_per_cm))
        rest = self.buf.height - cut
        self.buf = self.buf.crop((0, cut, self.w, self.buf.height)) if rest > 0 else None
        self.room_px = int((PDF_CONTENT_HEIGHT_CM - 0.1) * self.px_per_cm)


# Usable frame inside the PDF page margins below (A4 minus margins and
# reportlab's default 6pt frame padding on each side).
PDF_CONTENT_WIDTH_CM = 16.9
PDF_CONTENT_HEIGHT_CM = 24.8
RENDER_WIDTH_PX = round(PDF_CONTENT_WIDTH_CM / 2.54 * 96)


def _field_table(subject, from_display, to_display, date_str):
    label_style = ParagraphStyle("Label", fontName="Helvetica-Bold", fontSize=9, leading=13)
    value_style = ParagraphStyle("Value", fontName="Helvetica", fontSize=9, leading=13)
    fields = [
        ("Subject", subject or "(no subject)"),
        ("From", from_display or "(unknown)"),
        ("To", to_display or "(unknown)"),
        ("Date", date_str),
    ]
    rows = [[Paragraph(_escape_pdf_text(label), label_style), Paragraph(_escape_pdf_text(value), value_style)]
            for label, value in fields]
    table = Table(rows, colWidths=[2.2 * cm, None])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -1), 0.75, colors.HexColor("#d9d9d9")),
    ]))
    return table


def first_page_room_cm(subject, from_display, to_display, date_str):
    """Body height left on page 1 under the Subject/From/To/Date block."""
    _, h = _field_table(subject, from_display, to_display, date_str).wrap(
        PDF_CONTENT_WIDTH_CM * cm, PDF_CONTENT_HEIGHT_CM * cm)
    return PDF_CONTENT_HEIGHT_CM - h / cm - 0.5 - 0.2


_BLOCK_FONT = "font-family:Helvetica,Arial,'Liberation Sans',sans-serif;color:#000;"


def info_block_html(subject, from_display, to_display, date_str):
    import html as _h
    rows = "".join(
        f'<tr><td class="l">{_h.escape(label)}</td><td>{_h.escape(value)}</td></tr>'
        for label, value in (("Subject", subject or "(no subject)"), ("From", from_display or "(unknown)"),
                             ("To", to_display or "(unknown)"), ("Date", date_str)))
    return (f"<style>table{{{_BLOCK_FONT}font-size:9pt;width:100%;border-collapse:collapse;margin:0 0 16px 0}}"
            "td{padding:4px 0;border-bottom:0.75pt solid #d9d9d9;vertical-align:top;line-height:1.4}"
            "td.l{font-weight:bold;width:2.2cm;padding-right:8px}</style>"
            f"<table>{rows}</table>")


def attachments_block_html(attachments):
    import html as _h
    if not attachments:
        return ""
    items = "".join(f"<div>{_h.escape(f'{name} ({format_file_size(size)})')}</div>" for name, size in attachments)
    return (f"<style>div.a{{{_BLOCK_FONT}font-size:9pt;line-height:1.45;margin-top:16px}}"
            "b{display:block;margin-bottom:2px}</style>"
            f'<div class="a"><b>Attachments</b>{items}</div>')


def compose_printed_pdf(path, body_pdf, header_left, header_right, stamp, title=""):
    """Lay the page header/footer (labels, UID link, Page X of Y, stamp) over
    Chromium's printed pages. The printed text and links are kept as-is."""
    body = PdfReader(io.BytesIO(body_pdf))
    total = len(body.pages)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    for i in range(total):
        draw_page_decorations(c, i + 1, total, header_left, header_right, stamp)
        c.showPage()
    c.save()
    overlay = PdfReader(io.BytesIO(buf.getvalue()))
    out = PdfWriter()
    for i, page in enumerate(body.pages):
        page.merge_page(overlay.pages[i])
        out.add_page(page)
    if title:
        out.add_metadata({"/Title": title})
    with open(path, "wb") as fh:
        out.write(fh)


def build_email_info_pdf(
    path, header_left, header_right, subject, from_display, to_display, date_str,
    body_text, attachments, generated_stamp, body_strips=None,
):
    """Render one email's info into an A4 PDF, styled after the Zimbra
    'print this email' layout in the odg template: a Subject/From/To/Date
    field block with grey divider lines, then the body (which can run to
    as many pages as it needs), then an Attachments list.

    body_strips: page-sized PNG strips from HtmlRenderer.render_strips(),
    used instead of body_text (graphical mode)."""
    doc = SimpleDocTemplate(
        path, pagesize=A4,
        topMargin=2.2 * cm, bottomMargin=2.0 * cm,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
        title=subject or "", author="",
    )
    body_style = ParagraphStyle("Body", fontName="Helvetica", fontSize=10, leading=14, spaceAfter=4)
    attach_heading_style = ParagraphStyle("AttachHeading", fontName="Helvetica-Bold", fontSize=9, leading=13, spaceBefore=10)
    attach_item_style = ParagraphStyle("AttachItem", fontName="Helvetica", fontSize=9, leading=13)

    story = [_field_table(subject, from_display, to_display, date_str), Spacer(1, 0.5 * cm)]

    if body_strips:
        for png, height_cm in body_strips:
            story.append(RLImage(io.BytesIO(png), width=PDF_CONTENT_WIDTH_CM * cm, height=height_cm * cm))
    else:
        body_text = body_text or "(no body text)"
        for line in body_text.splitlines() or [""]:
            story.append(Paragraph(apply_simple_markdown(linkify_escaped(_escape_pdf_text(line))) or "&nbsp;",
                                   body_style))

    if attachments:
        story.append(Paragraph("Attachments", attach_heading_style))
        for name, size in attachments:
            story.append(Paragraph(
                _escape_pdf_text(f"{name} ({format_file_size(size)})"), attach_item_style
            ))

    def make_canvas(*args, **kwargs):
        return _InfoPdfCanvas(
            *args,
            header_left=header_left,
            header_right=header_right,
            generated_stamp=generated_stamp,
            **kwargs,
        )

    doc.build(story, canvasmaker=make_canvas)


class AttachmentFetcherApp:
    def __init__(self, root):
        self.root = root
        root.title("Email Attachment Fetcher")
        root.geometry("640x660")
        root.minsize(600, 600)

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)

        nb = ttk.Notebook(outer)
        nb.pack(fill="x")

        conn_tab = self._tab(nb, "Connection")
        msgs_tab = self._tab(nb, "Messages")
        att_tab = self._tab(nb, "Attachments")
        csv_tab = self._tab(nb, "CSV Export")
        pdf_tab = self._tab(nb, "Email Info PDF")

        # ---------------- Connection ----------------
        self.server_var = tk.StringVar(value="imap.example.com")
        self.port_var = tk.StringVar(value="993")
        self.user_var = tk.StringVar()
        self.pass_var = tk.StringVar()
        self.mailbox_var = tk.StringVar(value="INBOX")
        self._entry_row(conn_tab, 0, "IMAP server", self.server_var)
        self._entry_row(conn_tab, 1, "Port", self.port_var, width=8)
        self._entry_row(conn_tab, 2, "Email / username", self.user_var)
        self._entry_row(conn_tab, 3, "Password", self.pass_var, show="*")
        self._entry_row(conn_tab, 4, "Mailbox", self.mailbox_var)

        self.remember_login = tk.BooleanVar(value=True)
        self.remember_password = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            conn_tab, text="Remember these settings for next time", variable=self.remember_login
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 2))
        ttk.Checkbutton(
            conn_tab, text="Also remember password (stored in PLAIN TEXT on disk)",
            variable=self.remember_password
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=2)
        self._hint(conn_tab, 7, "The mailbox is always opened read-only, so nothing is ever marked as read.")

        # ---------------- Messages (range + output, shared by every action) ----------------
        today = datetime.now().strftime("%d-%b-%Y")
        self.from_date_var = tk.StringVar(value=today)
        self.from_time_var = tk.StringVar(value="00:00")
        self.to_date_var = tk.StringVar(value=today)
        self.to_time_var = tk.StringVar(value="23:59")
        self.local_tz_var = tk.StringVar(value=DEFAULT_LOCAL_TZ)
        self.outdir_var = tk.StringVar()

        ttk.Label(msgs_tab, text="From").grid(row=0, column=0, sticky="w", pady=4)
        self._date_time_pair(msgs_tab, 0, self.from_date_var, self.from_time_var)
        ttk.Label(msgs_tab, text="To").grid(row=1, column=0, sticky="w", pady=4)
        self._date_time_pair(msgs_tab, 1, self.to_date_var, self.to_time_var)
        self._hint(msgs_tab, 2, "Date as DD-Mon-YYYY (e.g. 01-Aug-2026), time as 24h HH:MM. Both ends inclusive.")
        self._entry_row(msgs_tab, 3, "Local timezone", self.local_tz_var, width=20)
        self._hint(msgs_tab, 4, "IANA name (Africa/Nairobi) or offset (+03:00). Times come from the\n"
                                "Received: header -- when the mail arrived in your mailbox.")

        ttk.Label(msgs_tab, text="Output folder").grid(row=5, column=0, sticky="w", pady=(12, 4))
        out_frame = ttk.Frame(msgs_tab)
        out_frame.grid(row=5, column=1, columnspan=2, sticky="we", pady=(12, 4))
        out_frame.columnconfigure(0, weight=1)
        ttk.Entry(out_frame, textvariable=self.outdir_var).grid(row=0, column=0, sticky="we")
        ttk.Button(out_frame, text="Browse...", command=self.browse_dir).grid(row=0, column=1, padx=(6, 0))
        self._hint(msgs_tab, 6, "Files go to <folder>/<YYYY-MM-DD>/<sender> - <subject>/")

        # ---------------- Attachments ----------------
        self.only_with_attachments = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            att_tab, text="Only log messages that have attachments", variable=self.only_with_attachments
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=2)

        merge_text = "Merge PDF attachments into MERGED_<email>.pdf"
        if not PYPDF_AVAILABLE:
            merge_text += "  (requires: pip install pypdf)"
        self.merge_pdfs = tk.BooleanVar(value=False)
        self.merge_check = ttk.Checkbutton(
            att_tab, text=merge_text, variable=self.merge_pdfs,
            state="normal" if PYPDF_AVAILABLE else "disabled"
        )
        self.merge_check.grid(row=1, column=0, columnspan=3, sticky="w", pady=2)
        self.attach_info_pdf_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            att_tab, text="Also create an EMAILINFO PDF in each message's folder\n"
                          "(uses the Email Info PDF tab's settings)",
            variable=self.attach_info_pdf_var,
            state="normal" if REPORTLAB_AVAILABLE else "disabled",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=2)
        self._hint(att_tab, 3, "Saves every attachment for messages in the range on the Messages tab.")
        self.run_btn = ttk.Button(att_tab, text="Fetch Attachments", command=self.start_fetch)
        self.run_btn.grid(row=4, column=0, columnspan=3, sticky="w", pady=(14, 0))

        # ---------------- CSV Export ----------------
        self._hint(csv_tab, 0, "One row per message: Sender, Email, Date Received, Subject, Body.\n"
                               "Only the body text is downloaded -- attachments never are.")
        self.csv_btn = ttk.Button(csv_tab, text="Export Message List to CSV...", command=self.start_csv_export)
        self.csv_btn.grid(row=1, column=0, columnspan=3, sticky="w", pady=(14, 0))

        # ---------------- Email Info PDF ----------------
        self.pdf_header_label_var = tk.StringVar(value=DEFAULT_HEADER_LEFT)
        self.pdf_header_right_var = tk.StringVar(value=DEFAULT_HEADER_RIGHT)
        self._entry_row(pdf_tab, 0, "Header, top-left", self.pdf_header_label_var)
        self._entry_row(pdf_tab, 1, "Header, top-right", self.pdf_header_right_var)
        self._hint(pdf_tab, 2,
                   "Specifiers: " + "  ".join(f"<{k}>" for k in TEMPLATE_SPECIFIERS) + "\n"
                   "e.g. https://mail.example.com/modern/email/conversation/-<UID>/\n"
                   "A top-right value starting with http(s):// becomes a clickable link.\n"
                   "Top-left blank = your username.")

        ttk.Label(pdf_tab, text="HTML body").grid(row=3, column=0, sticky="w", pady=(10, 2))
        mode_frame = ttk.Frame(pdf_tab)
        mode_frame.grid(row=3, column=1, columnspan=2, sticky="w", pady=(10, 2))
        self.body_mode_var = tk.StringVar(value=BODY_MODES[DEFAULT_BODY_MODE if GRAPHICAL_AVAILABLE else "text"])
        self.body_mode_box = ttk.Combobox(
            mode_frame, textvariable=self.body_mode_var, state="readonly", width=32,
            values=list(BODY_MODES.values()) if GRAPHICAL_AVAILABLE else [BODY_MODES["text"]],
        )
        self.body_mode_box.pack(side="left")
        self.body_mode_box.bind("<<ComboboxSelected>>", lambda _e: self._on_body_mode())
        if not GRAPHICAL_AVAILABLE:
            ttk.Label(mode_frame, text="  printed/image need: pip install playwright pillow",
                      foreground="gray").pack(side="left")
        self.remote_images_var = tk.BooleanVar(value=True)
        self.remote_images_check = ttk.Checkbutton(
            pdf_tab, text="Load remote images (http/https images only, redirects followed)",
            variable=self.remote_images_var,
            state="normal" if GRAPHICAL_AVAILABLE else "disabled"
        )
        self.remote_images_check.grid(row=4, column=0, columnspan=3, sticky="w", padx=(20, 0), pady=2)
        self._hint(pdf_tab, 5,
                   "Printed = like Zimbra's Print: selectable text, working links. Image = exact pixels.\n"
                   "Sender JavaScript never runs; nothing but images is ever fetched.\n"
                   "Loading remote images can tell the sender the email was opened.")

        gfx_state = "normal" if GRAPHICAL_AVAILABLE else "disabled"
        ttk.Label(pdf_tab, text="Image quality").grid(row=6, column=0, sticky="w", pady=(8, 2))
        q_frame = ttk.Frame(pdf_tab)
        q_frame.grid(row=6, column=1, columnspan=2, sticky="we", pady=(8, 2))
        self.quality_var = tk.IntVar(value=DEFAULT_QUALITY)
        self.quality_text = ttk.Label(q_frame, width=18)
        self.quality_scale = ttk.Scale(
            q_frame, from_=1, to=len(QUALITY_LEVELS), orient="horizontal", length=200,
            command=self._on_quality_slide, state=gfx_state,
        )
        self.quality_scale.set(DEFAULT_QUALITY)
        self.quality_scale.pack(side="left")
        self.quality_text.pack(side="left", padx=(10, 0))

        ttk.Label(pdf_tab, text="Parallel renders").grid(row=7, column=0, sticky="w", pady=2)
        w_frame = ttk.Frame(pdf_tab)
        w_frame.grid(row=7, column=1, columnspan=2, sticky="w", pady=2)
        cpus = os.cpu_count() or 2
        self.workers_var = tk.IntVar(value=max(1, min(4, cpus)))
        ttk.Spinbox(w_frame, from_=1, to=max(8, cpus), textvariable=self.workers_var, width=4,
                    state="readonly").pack(side="left")
        ttk.Label(w_frame, text="  each uses its own browser (~0.5 GB RAM)",
                  foreground="gray").pack(side="left")
        self._on_quality_slide(DEFAULT_QUALITY)
        self._on_body_mode()

        pdf_btn_text = "Render Email Info PDFs"
        if not REPORTLAB_AVAILABLE:
            pdf_btn_text += "  (requires: pip install reportlab)"
        self.info_pdf_btn = ttk.Button(
            pdf_tab, text=pdf_btn_text, command=self.start_render_info_pdfs,
            state="normal" if REPORTLAB_AVAILABLE else "disabled"
        )
        self.info_pdf_btn.grid(row=8, column=0, columnspan=3, sticky="w", pady=(14, 0))

        # ---------------- Always visible: progress + log ----------------
        progress_frame = ttk.Frame(outer)
        progress_frame.pack(fill="x", pady=(10, 4))
        self.progress = ttk.Progressbar(progress_frame, orient="horizontal", mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(progress_frame, text="", width=30, anchor="e")
        self.progress_label.pack(side="left", padx=(8, 0))
        self.stop_btn = ttk.Button(progress_frame, text="Stop", width=6, command=self.request_stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # Worker threads must never touch Tk widgets directly (Tk isn't
        # thread-safe); they post updates here and the main loop applies them.
        self._ui_queue = queue.Queue()
        self.root.after(80, self._drain_ui_queue)

        # Clean shutdown: background jobs own headless browsers (via
        # Playwright's Node helper). If Python exits while they're mid-render,
        # the helper crashes with "write EPIPE" and the browsers are orphaned.
        self._cancel = threading.Event()
        self._job_thread = None
        self._imap = None
        self._raw_sock = None
        self._closing = False
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.load_config()

    def body_mode(self):
        """'print' | 'image' | 'text' from the selector's display text."""
        shown = self.body_mode_var.get()
        return next((k for k, v in BODY_MODES.items() if v == shown), "text")

    def _on_body_mode(self):
        mode = self.body_mode()
        self.quality_scale.configure(state="normal" if mode == "image" else "disabled")
        self.remote_images_check.configure(state="normal" if mode in ("print", "image") else "disabled")

    def _on_quality_slide(self, value):
        level = int(round(float(value)))
        self.quality_var.set(level)
        if abs(float(self.quality_scale.get()) - level) > 1e-6:
            self.quality_scale.set(level)  # snap to whole steps
        self.quality_text.configure(text=quality_label(level))

    # --- small layout helpers ---
    @staticmethod
    def _tab(nb, title):
        tab = ttk.Frame(nb, padding=12)
        tab.columnconfigure(1, weight=1)
        nb.add(tab, text=title)
        return tab

    @staticmethod
    def _entry_row(parent, row, label, var, width=None, show=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        kwargs = {"textvariable": var}
        if show:
            kwargs["show"] = show
        if width:
            kwargs["width"] = width
        entry = ttk.Entry(parent, **kwargs)
        entry.grid(row=row, column=1, columnspan=2, sticky="w" if width else "we", pady=4)
        return entry

    @staticmethod
    def _hint(parent, row, text):
        ttk.Label(parent, text=text, foreground="gray", justify="left").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(2, 4)
        )

    @staticmethod
    def _date_time_pair(parent, row, date_var, time_var):
        f = ttk.Frame(parent)
        f.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        ttk.Entry(f, textvariable=date_var, width=14).pack(side="left")
        ttk.Label(f, text="at").pack(side="left", padx=6)
        ttk.Entry(f, textvariable=time_var, width=7).pack(side="left")

    def load_config(self):
        """Populate fields from a previously saved config, if any."""
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        self.server_var.set(cfg.get("server", self.server_var.get()))
        self.port_var.set(cfg.get("port", self.port_var.get()))
        self.user_var.set(cfg.get("username", self.user_var.get()))
        self.mailbox_var.set(cfg.get("mailbox", self.mailbox_var.get()))
        self.outdir_var.set(cfg.get("outdir", self.outdir_var.get()))
        self.from_date_var.set(cfg.get("from_date", self.from_date_var.get()))
        self.from_time_var.set(cfg.get("from_time", self.from_time_var.get()))
        self.to_date_var.set(cfg.get("to_date", self.to_date_var.get()))
        self.to_time_var.set(cfg.get("to_time", self.to_time_var.get()))
        self.only_with_attachments.set(cfg.get("only_with_attachments", True))
        self.merge_pdfs.set(cfg.get("merge_pdfs", False))
        self.attach_info_pdf_var.set(cfg.get("attach_info_pdf", False) and REPORTLAB_AVAILABLE)
        self.pdf_header_label_var.set(cfg.get("pdf_header_label", self.pdf_header_label_var.get()))
        self.pdf_header_right_var.set(cfg.get("pdf_header_right", self.pdf_header_right_var.get()))
        self.local_tz_var.set(cfg.get("local_tz", self.local_tz_var.get()))
        if GRAPHICAL_AVAILABLE:
            mode = cfg.get("body_mode") or ("print" if cfg.get("render_graphical") else "text")
            self.body_mode_var.set(BODY_MODES.get(mode, BODY_MODES["text"]))
            self._on_body_mode()
            self.remote_images_var.set(cfg.get("remote_images", True))
            level = cfg.get("render_quality", DEFAULT_QUALITY)
            if level in QUALITY_LEVELS:
                self._on_quality_slide(level)
            try:
                self.workers_var.set(max(1, int(cfg.get("render_workers", self.workers_var.get()))))
            except (TypeError, ValueError):
                pass
        self.remember_password.set(cfg.get("remember_password", False))
        if cfg.get("remember_password") and "password" in cfg:
            self.pass_var.set(cfg.get("password", ""))

    def save_config(self):
        """Persist login info to CONFIG_PATH. Password only if opted in."""
        cfg = {
            "server": self.server_var.get().strip(),
            "port": self.port_var.get().strip(),
            "username": self.user_var.get().strip(),
            "mailbox": self.mailbox_var.get().strip(),
            "outdir": self.outdir_var.get().strip(),
            "from_date": self.from_date_var.get().strip(),
            "from_time": self.from_time_var.get().strip(),
            "to_date": self.to_date_var.get().strip(),
            "to_time": self.to_time_var.get().strip(),
            "only_with_attachments": self.only_with_attachments.get(),
            "merge_pdfs": self.merge_pdfs.get(),
            "attach_info_pdf": self.attach_info_pdf_var.get(),
            "pdf_header_label": self.pdf_header_label_var.get().strip(),
            "pdf_header_right": self.pdf_header_right_var.get().strip(),
            "local_tz": self.local_tz_var.get().strip(),
            "body_mode": self.body_mode(),
            "remote_images": self.remote_images_var.get(),
            "render_quality": self.quality_var.get(),
            "render_workers": self.workers_var.get(),
            "remember_password": self.remember_password.get(),
        }
        if self.remember_password.get():
            cfg["password"] = self.pass_var.get()

        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
            # Best-effort: restrict permissions to the current user (POSIX only)
            try:
                os.chmod(CONFIG_PATH, 0o600)
            except (AttributeError, OSError):
                pass
        except OSError as e:
            self.log_msg(f"Could not save config: {e}")

    def browse_dir(self):
        path = filedialog.askdirectory()
        if path:
            self.outdir_var.set(path)

    # --- UI updates: safe to call from any thread ---
    def _ui(self, fn, *args):
        if threading.current_thread() is threading.main_thread():
            fn(*args)
        else:
            self._ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_ui_queue)

    def log_msg(self, text):
        self._ui(self._log_now, text)

    def _log_now(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_progress(self, current, total, note=""):
        self._ui(self._progress_now, current, total, note)

    def _progress_now(self, current, total, note):
        self.progress["maximum"] = max(total, 1)
        self.progress["value"] = current
        self.progress_label.configure(text=f"{note}{current} / {total}")

    def _validate_range(self, need_outdir=True):
        """Shared validation for both fetch modes. Returns (start, end) or None."""
        if need_outdir and not self.outdir_var.get():
            messagebox.showerror("Missing folder", "Please choose a folder to save attachments to.")
            return None
        if not self.user_var.get() or not self.pass_var.get():
            messagebox.showerror("Missing credentials", "Please enter your email and password.")
            return None
        try:
            range_start = datetime.strptime(
                f"{self.from_date_var.get().strip()} {self.from_time_var.get().strip()}", "%d-%b-%Y %H:%M"
            )
            range_end = datetime.strptime(
                f"{self.to_date_var.get().strip()} {self.to_time_var.get().strip()}", "%d-%b-%Y %H:%M"
            )
        except ValueError:
            messagebox.showerror(
                "Bad date/time",
                "Dates must be DD-Mon-YYYY (e.g. 01-Aug-2026) and times must be HH:MM 24h (e.g. 09:30)."
            )
            return None
        if range_start > range_end:
            messagebox.showerror("Bad range", "The 'From' date/time must be before the 'To' date/time.")
            return None
        return range_start, range_end

    def set_buttons_state(self, state):
        self._ui(self._buttons_now, state)

    def _buttons_now(self, state):
        self.stop_btn.configure(state="normal" if state == "disabled" else "disabled")
        self.run_btn.configure(state=state)
        self.csv_btn.configure(state=state)
        if REPORTLAB_AVAILABLE:
            self.info_pdf_btn.configure(state=state)

    def _launch(self, target, *args):
        """Start a background job (main thread only)."""
        self._cancel.clear()
        self._job = self._snapshot()

        def run():
            try:
                target(*args)
            finally:
                sock, self._raw_sock, self._imap = self._raw_sock, None, None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

        self._job_thread = threading.Thread(target=run, daemon=True)
        self._job_thread.start()

    def job_running(self):
        return self._job_thread is not None and self._job_thread.is_alive()

    def request_stop(self):
        if self.job_running() and not self._cancel.is_set():
            self._cancel.set()
            self.log_msg("Stopping -- finishing the current item(s) and closing browsers ...")
            self.stop_btn.configure(state="disabled")
            self.root.after(3000, self._drop_connection_if_stuck)

    def _drop_connection_if_stuck(self):
        """A job blocked inside a network call can't see the Stop flag;
        closing its socket makes that call fail at once."""
        if not self.job_running():
            return
        sock = getattr(self, "_raw_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _on_close(self):
        """Window close: if a job is running, stop it cleanly first, then exit."""
        if not self.job_running():
            self.root.destroy()
            return
        if not self._closing:
            self._closing = True
            self.request_stop()
            self._close_deadline = time.time() + 60
        self._wait_then_close()

    def _wait_then_close(self):
        if self.job_running() and time.time() < self._close_deadline:
            self.root.after(150, self._wait_then_close)
            return
        self.root.destroy()

    def shutdown(self, timeout=60):
        """For exits that bypass the window (e.g. Ctrl+C in the terminal)."""
        self._cancel.set()
        if self._job_thread is not None:
            self._job_thread.join(timeout)

    def _snapshot(self):
        """Read every setting the background job needs, on the main thread.
        Tk widgets/variables must not be touched from worker threads."""
        v = {name: getattr(self, attr).get() for attr, name in {
            "server_var": "server", "port_var": "port", "user_var": "user", "pass_var": "password",
            "mailbox_var": "mailbox", "outdir_var": "outdir", "pdf_header_label_var": "header_left",
            "pdf_header_right_var": "header_right",
            "remote_images_var": "remote_images", "quality_var": "quality", "workers_var": "workers",
            "local_tz_var": "local_tz", "from_date_var": "from_date", "from_time_var": "from_time",
            "to_date_var": "to_date", "to_time_var": "to_time",
            "only_with_attachments": "only_attachments", "merge_pdfs": "merge_pdfs",
        }.items()}
        v["body_mode"] = self.body_mode()
        v["attach_info_pdf"] = self.attach_info_pdf_var.get()
        return types.SimpleNamespace(**v)

    def start_fetch(self):
        if self._validate_range(need_outdir=True) is None:
            return

        self.set_buttons_state("disabled")
        self.set_progress(0, 0)
        if self.remember_login.get():
            self.save_config()
        if self.attach_info_pdf_var.get() and REPORTLAB_AVAILABLE:
            self._launch(self._fetch_then_info_pdfs)
        else:
            self._launch(self.fetch_attachments)

    def start_csv_export(self):
        rng = self._validate_range(need_outdir=False)
        if rng is None:
            return

        csv_path = filedialog.asksaveasfilename(
            title="Save message list CSV as...",
            defaultextension=".csv",
            initialfile="messages.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not csv_path:
            return

        self.set_buttons_state("disabled")
        self.set_progress(0, 0)
        if self.remember_login.get():
            self.save_config()
        self._launch(self.export_csv, csv_path)

    def export_csv(self, csv_path):
        """Text-only export: Sender, Email, Date Received, Subject, Body.

        This NEVER downloads attachment content, not even into memory:
        1. Fetches (DATE) header only, to apply the exact time filter.
        2. Fetches (BODYSTRUCTURE) -- a small text description of the
           message's MIME layout -- to find which part number holds the
           plain-text or HTML body.
        3. Fetches ONLY that one part with BODY.PEEK[<part number>].
           Attachment parts are never requested from the server.
        Uses a readonly IMAP session throughout, so nothing is ever marked
        as read either.
        """
        server = self._job.server.strip()
        port = int(self._job.port.strip())
        username = self._job.user.strip()
        password = self._job.password
        mailbox = self._job.mailbox.strip() or "INBOX"

        range_start, range_end = self._range_from_fields()
        imap_since, imap_before = self._imap_search_window(range_start, range_end)

        rows = []
        try:
            imap = self._connect_imap(server, port, username, password, mailbox)

            self.log_msg(
                f"Listing '{mailbox}' from {range_start:%d-%b-%Y %H:%M} "
                f"through {range_end:%d-%b-%Y %H:%M} (body text only, no attachments) ..."
            )
            status, data = imap.search(None, f'(SINCE "{imap_since}" BEFORE "{imap_before}")')
            if status != "OK":
                self.log_msg("Search failed.")
                return

            msg_nums, skipped_by_time = self._prefilter_in_range(
                imap, data[0].split(), self._local_tz(), range_start, range_end)
            total = len(msg_nums)
            self.log_msg(f"{total} message(s) in range. Reading ...")
            self.set_progress(0, total)

            no_text_part = 0
            reply_to_count = 0
            tz = self._local_tz()
            for i, num in enumerate(msg_nums, start=1):
                if self._cancel.is_set():
                    self.log_msg("Stopped by user.")
                    break
                # Pass 1: From/Reply-To/Received/Date/Subject headers only (tiny, no risk of pulling attachments)
                status, hdr_data = imap.fetch(
                    num, "(BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO RECEIVED DATE SUBJECT)])"
                )
                if status != "OK" or not hdr_data or hdr_data[0] is None:
                    self.set_progress(i, total)
                    continue

                header_msg = email.message_from_bytes(hdr_data[0][1])
                sent_dt, _ = self._message_time(header_msg, tz)

                if not self._in_range(sent_dt, range_start, range_end):
                    skipped_by_time += 1
                    self.set_progress(i, total)
                    continue

                sender_name, sender_email, used_reply_to = resolve_sender(
                    header_msg.get("From", ""), header_msg.get("Reply-To", "")
                )
                if used_reply_to:
                    reply_to_count += 1
                subject = decode_mime_words(header_msg.get("Subject", ""))

                # Pass 2: ask for the MIME layout only (tiny text description,
                # no part content included) so we know which part is the body.
                status, struct_data = imap.fetch(num, "(BODYSTRUCTURE)")
                struct = parse_bodystructure_response(struct_data) if status == "OK" else None
                target = pick_body_part(struct) if struct is not None else None

                body = ""
                if target is not None:
                    part_num, subtype, charset, encoding, _ = target
                    status, part_data = imap.fetch(num, f"(BODY.PEEK[{part_num}])")
                    if status == "OK" and part_data and part_data[0] is not None:
                        raw_payload = part_data[0][1]
                        decoded = decode_part_payload(raw_payload, encoding, charset)
                        body = html_to_text(decoded) if subtype == "HTML" else decoded.strip()
                    else:
                        no_text_part += 1
                else:
                    no_text_part += 1

                rows.append({
                    "Sender": sender_name or sender_email or "(unknown)",
                    "Email": sender_email or "(unknown)",
                    "Date Received": sent_dt.strftime("%Y-%m-%d %H:%M:%S") if sent_dt else "",
                    "Subject": subject,
                    "Body": body,
                })
                self.set_progress(i, total)

            imap.logout()

            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["Sender", "Email", "Date Received", "Subject", "Body"]
                )
                writer.writeheader()
                writer.writerows(rows)

            self.log_msg(
                f"Done. {len(rows)} row(s) written to {csv_path} "
                f"({skipped_by_time} outside the exact time range skipped, "
                f"{no_text_part} had no readable text part, "
                f"{reply_to_count} used Reply-To instead of From)."
            )

        except imaplib.IMAP4.error as e:
            self.log_msg(f"IMAP error: {e}")
        except (TimeoutError, OSError) as e:
            if self._cancel.is_set():
                self.log_msg("Stopped by user (connection closed).")
            else:
                self.log_msg(f"Network error: {e or type(e).__name__} -- the server didn't respond "
                             f"within {IMAP_TIMEOUT_S}s. Check the server/port, or try again shortly.")
        except OSError as e:
            self.log_msg(f"Could not write CSV: {e}")
        except Exception as e:
            self.log_msg(f"Error: {e}")
        finally:
            self.set_buttons_state("normal")

    def start_render_info_pdfs(self):
        if not REPORTLAB_AVAILABLE:
            messagebox.showerror("Missing dependency", "This feature needs reportlab: pip install reportlab")
            return
        if self._validate_range(need_outdir=True) is None:
            return

        self.set_buttons_state("disabled")
        self.set_progress(0, 0)
        if self.remember_login.get():
            self.save_config()
        self._launch(self.render_email_info_pdfs)

    def render_email_info_pdfs(self):
        """Save one EMAILINFO_<sender>.pdf per message, styled after the
        Zimbra 'print this email' layout (Subject/From/To/Date block, body,
        attachments list) on A4; the body can span multiple pages.

        Pipeline: this thread is the only one talking IMAP (one connection
        can't be shared across threads). It downloads each message's
        headers + body and queues it; N render workers -- each with its own
        browser in graphical mode -- turn queued messages into PDFs in
        parallel while downloading continues.

        Text mode: only the body's own text part is fetched.
        Graphical mode: the HTML part plus ONLY the inline images it
        references (cid:). File attachments are never downloaded in either
        mode -- they're listed from BODYSTRUCTURE.
        """
        server = self._job.server.strip()
        port = int(self._job.port.strip())
        username = self._job.user.strip()
        password = self._job.password
        mailbox = self._job.mailbox.strip() or "INBOX"
        outdir = self._job.outdir.strip()
        left_template = self._job.header_left.strip() or username
        right_template = self._job.header_right.strip()
        mode = self._job.body_mode if GRAPHICAL_AVAILABLE else "text"
        if mode == "print" and not PYPDF_AVAILABLE:
            self.log_msg("Printed mode needs pypdf (pip install pypdf) -- using Image snapshot instead.")
            mode = "image"
        graphical = mode in ("print", "image")
        allow_remote = graphical and self._job.remote_images
        quality = self._job.quality
        n_workers = max(1, int(self._job.workers))
        if graphical:
            # Running out of memory gets the whole app killed by the OS -- the
            # browsers' helper then dies with "write EPIPE". Don't start more
            # browsers than the machine can hold right now.
            avail = available_memory_mb()
            per_worker = PRINT_WORKER_MEMORY_MB if mode == "print" else WORKER_MEMORY_MB.get(quality, 700)
            if avail is not None:
                fits = max(1, int(avail * 0.7) // per_worker)
                if n_workers > fits:
                    self.log_msg(
                        f"Only ~{avail} MB of memory free: using {fits} parallel render(s) instead of "
                        f"{n_workers} (~{per_worker} MB each)."
                    )
                    n_workers = fits
        tz = self._local_tz()

        range_start, range_end = self._range_from_fields()
        imap_since, imap_before = self._imap_search_window(range_start, range_end)
        generated_stamp = datetime.now(tz).strftime("%m/%d/%Y, %H:%M")
        os.makedirs(outdir, exist_ok=True)

        jobs = queue.Queue(maxsize=n_workers * 2)   # backpressure keeps memory bounded
        state = {"total": 0, "downloaded": 0, "rendered": 0, "failed": 0, "date_fallbacks": 0}
        lock = threading.Lock()
        dir_times = {}          # folder -> latest message time inside it
        workers = []

        def progress():
            with lock:
                note = f"downloaded {state['downloaded']} · rendered "
                self.set_progress(state["rendered"], state["total"], note)

        def worker():
            renderer = None
            if graphical:
                try:
                    renderer = HtmlRenderer(RENDER_WIDTH_PX, allow_remote_images=allow_remote).__enter__()
                except Exception as e:
                    with lock:
                        first = not state.get("browser_error")
                        state["browser_error"] = True
                    if first:  # once, not once per worker; Playwright's errors are banners
                        reason = str(e).strip().splitlines()[0][:200]
                        self.log_msg(f"  Browser failed to start: {reason}")
                        self.log_msg("  Falling back to text layout. Fix: run 'playwright install chromium' "
                                     "with the same Python you run this app with.")
            try:
                while True:
                    job = jobs.get()
                    if job is None:
                        return
                    if self._cancel.is_set():
                        continue
                    try:
                        self._render_one(job, renderer, quality, mode)
                        with lock:
                            state["rendered"] += 1
                    except Exception as e:
                        with lock:
                            state["failed"] += 1
                        self.log_msg(f"  PDF render failed for UID {job['uid']}: {e}")
                    progress()
            finally:
                if renderer is not None:
                    try:
                        renderer.__exit__(None, None, None)  # must close on the thread that opened it
                    except Exception:
                        pass

        try:
            imap = self._connect_imap(server, port, username, password, mailbox)

            mode_desc = {"print": "printed", "image": f"image, {QUALITY_LEVELS[quality][0]} quality",
                         "text": "plain text"}[mode]
            self.log_msg(
                f"Rendering Email Info PDFs ({mode_desc}, {n_workers} in parallel) for '{mailbox}' "
                f"from {range_start:%d-%b-%Y %H:%M} through {range_end:%d-%b-%Y %H:%M} local time ..."
            )
            status, data = imap.search(None, f'(SINCE "{imap_since}" BEFORE "{imap_before}")')
            if status != "OK":
                self.log_msg("Search failed.")
                return

            msg_nums, _ = self._prefilter_in_range(imap, data[0].split(), tz, range_start, range_end)
            state["total"] = len(msg_nums)
            self.log_msg(f"{len(msg_nums)} message(s) in range.")
            progress()
            if not msg_nums:
                imap.logout()
                return

            if graphical:
                self.log_msg(f"Starting {n_workers} headless browser(s) ...")
            for _ in range(n_workers):
                t = threading.Thread(target=worker, daemon=True)
                t.start()
                workers.append(t)

            for num in msg_nums:
                if self._cancel.is_set():
                    self.log_msg("Stopped by user.")
                    break
                job = self._download_one(imap, num, tz, graphical, username, mailbox, outdir,
                                         left_template, right_template, generated_stamp)
                if job is None:
                    with lock:
                        state["total"] -= 1
                    progress()
                    continue
                with lock:
                    state["downloaded"] += 1
                    if job["time_source"] != "received":
                        state["date_fallbacks"] += 1
                    if job["sent_dt"] is not None:
                        for d in (job["msg_dir"], job["date_dir"]):
                            if d not in dir_times or job["sent_dt"] > dir_times[d]:
                                dir_times[d] = job["sent_dt"]
                progress()
                jobs.put(job)   # blocks while the workers are busy

            imap.logout()

        except imaplib.IMAP4.error as e:
            self.log_msg(f"IMAP error: {e}")
        except (TimeoutError, OSError) as e:
            if self._cancel.is_set():
                self.log_msg("Stopped by user (connection closed).")
            else:
                self.log_msg(f"Network error: {e or type(e).__name__} -- the server didn't respond "
                             f"within {IMAP_TIMEOUT_S}s. Check the server/port, or try again shortly.")
        except Exception as e:
            self.log_msg(f"Error: {e}")
        finally:
            for _ in workers:
                jobs.put(None)
            for t in workers:
                t.join()
            # Folders last: each gets the time of the newest message inside it
            # (the workers write in parallel, so per-file order isn't reliable).
            for d, dt in dir_times.items():
                if os.path.isdir(d):
                    touch_path(d, dt)
            if state["total"] or state["rendered"]:
                self.log_msg(
                    f"Done. {state['rendered']} Email Info PDF(s) rendered to {outdir}"
                    + (f", {state['failed']} failed" if state["failed"] else "")
                    + (f" ({state['date_fallbacks']} had no usable Received: header and used Date:)"
                       if state["date_fallbacks"] else "") + "."
                )
            self.set_buttons_state("normal")

    def _download_one(self, imap, num, tz, graphical, username, mailbox, outdir,
                      left_template, right_template, generated_stamp):
        """IMAP side of one message (runs on the single IMAP thread): headers,
        structure, and the body (HTML + its inline images, or text)."""
        status, hdr_data = imap.fetch(
            num, "(UID BODY.PEEK[HEADER.FIELDS (FROM TO REPLY-TO RECEIVED DATE SUBJECT)])"
        )
        if status != "OK" or not hdr_data or not isinstance(hdr_data[0], tuple):
            return None

        header_line = hdr_data[0][0]
        uid_match = re.search(rb"UID (\d+)", header_line if isinstance(header_line, bytes) else b"")
        uid = uid_match.group(1).decode() if uid_match else "unknown"

        header_msg = email.message_from_bytes(hdr_data[0][1])
        sent_dt, time_source = self._message_time(header_msg, tz)

        # The From header exactly as sent -- Reply-To is deliberately not
        # used in the PDF.
        from_name, from_email = parseaddr(decode_mime_words(header_msg.get("From", "")))
        from_email = from_email or "unknown_sender"
        from_display = f"{from_name} <{from_email}>" if from_name else from_email

        to_raw = decode_mime_words(header_msg.get("To", ""))
        to_addrs = [a for _, a in getaddresses([to_raw]) if a]
        to_display = ", ".join(to_addrs) if to_addrs else username
        subject = decode_mime_words(header_msg.get("Subject", ""))

        status, struct_data = imap.fetch(num, "(BODYSTRUCTURE)")
        struct = parse_bodystructure_response(struct_data) if status == "OK" else None

        html = None
        body = ""
        body_part_num = None
        inlined_parts = set()

        html_target = pick_html_part(struct) if (graphical and struct is not None) else None
        if html_target is not None:
            part_num, _, charset, encoding, _ = html_target
            status, part_data = imap.fetch(num, f"(BODY.PEEK[{part_num}])")
            if status == "OK" and part_data and isinstance(part_data[0], tuple):
                body_part_num = part_num
                html = decode_part_payload(part_data[0][1], encoding, charset)
                html, inlined_parts = inline_cid_images(html, struct, imap, num)

        if html is None:
            # Text mode, or a plain-text-only email in graphical mode
            target = pick_body_part(struct) if struct is not None else None
            if target is not None:
                part_num, subtype, charset, encoding, _ = target
                status, part_data = imap.fetch(num, f"(BODY.PEEK[{part_num}])")
                if status == "OK" and part_data and isinstance(part_data[0], tuple):
                    body_part_num = part_num
                    decoded = decode_part_payload(part_data[0][1], encoding, charset)
                    body = html_to_text(decoded) if subtype == "HTML" else decoded.strip()

        attachments = (
            list(find_attachment_parts(struct, body_part_num, skip_parts=inlined_parts))
            if struct is not None else []
        )

        # Folder only: same naming as the attachment fetcher (which does
        # prefer Reply-To) and the same per-email claim, so this email's PDF
        # and attachments always land in the same folder -- its own.
        _, folder_email, _ = resolve_sender(header_msg.get("From", ""), header_msg.get("Reply-To", ""))
        date_folder = sent_dt.strftime("%Y-%m-%d") if sent_dt is not None else "unknown_date"
        date_dir = os.path.join(outdir, date_folder)
        msg_dir = claim_message_dir(outdir, date_folder, folder_email or from_email, subject, mailbox, uid)

        values = {
            "UID": uid, "EMAIL": from_email, "SUBJECT": subject, "MAILBOX": mailbox,
            "DATE": sent_dt.strftime("%Y-%m-%d") if sent_dt is not None else "",
        }
        return {
            "uid": uid, "sent_dt": sent_dt, "time_source": time_source,
            "subject": subject, "from_display": from_display, "to_display": to_display,
            "date_str": format_zimbra_datetime(sent_dt), "html": html, "body": body,
            "attachments": attachments, "date_dir": date_dir, "msg_dir": msg_dir,
            "pdf_path": os.path.join(msg_dir, f"EMAILINFO_{sanitize_for_path(from_email)}.pdf"),
            "header_left": expand_template(left_template, values),
            "header_right": expand_template(right_template, values),
            "generated_stamp": generated_stamp,
            "label": f"{date_folder}/{os.path.basename(msg_dir)}",
        }

    def _render_one(self, job, renderer, quality, mode="image"):
        """Render side of one message (runs on a worker thread)."""
        strips = None
        body = job["body"]
        if job["html"] is not None and renderer is not None and mode == "print":
            try:
                pdf = renderer.print_pdf(
                    job["html"],
                    info_block_html(job["subject"], job["from_display"], job["to_display"], job["date_str"]),
                    attachments_block_html(job["attachments"]),
                )
                self._log_render_notes(job, renderer)
                os.makedirs(job["msg_dir"], exist_ok=True)
                compose_printed_pdf(job["pdf_path"], pdf, job["header_left"], job["header_right"],
                                    job["generated_stamp"], title=job["subject"])
                if job["sent_dt"] is not None:
                    touch_path(job["pdf_path"], job["sent_dt"])
                self.log_msg(f"  Rendered: {job['label']}/{os.path.basename(job['pdf_path'])}")
                return
            except Exception as e:
                self.log_msg(f"  Printing failed for UID {job['uid']} ({e}); using image snapshot instead.")
        if job["html"] is not None:
            if renderer is not None:
                try:
                    room = first_page_room_cm(job["subject"], job["from_display"],
                                              job["to_display"], job["date_str"])
                    strips = renderer.render_strips(job["html"], room, quality)
                    self._log_render_notes(job, renderer)
                except Exception as e:
                    self.log_msg(f"  HTML render failed for UID {job['uid']} ({e}); using text instead.")
            if strips is None:
                body = html_to_text(job["html"])

        os.makedirs(job["msg_dir"], exist_ok=True)
        build_email_info_pdf(
            job["pdf_path"], job["header_left"], job["header_right"], job["subject"],
            job["from_display"], job["to_display"], job["date_str"], body,
            job["attachments"], job["generated_stamp"], body_strips=strips,
        )
        if job["sent_dt"] is not None:
            touch_path(job["pdf_path"], job["sent_dt"])
        self.log_msg(f"  Rendered: {job['label']}/{os.path.basename(job['pdf_path'])}")

    def _connect_imap(self, server, port, username, password, mailbox):
        """Connect, log in and open the mailbox read-only, logging each step.
        Every network operation has a timeout, so a server that stops
        answering produces an error instead of an endless wait."""
        self.log_msg(f"Connecting to {server}:{port} ...")
        # Cert verification disabled (equivalent to curl -k): accepts
        # self-signed / mismatched-hostname certs. Traffic is still
        # encrypted, but this is vulnerable to MITM attacks -- only use
        # this against servers you trust on networks you trust.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        app = self

        class _TrackedIMAP4_SSL(imaplib.IMAP4_SSL):
            """Remembers the raw socket the moment it connects -- before the
            TLS handshake -- so Stop can cut a connection that hangs there."""
            def _create_socket(self, timeout):
                sock = imaplib.IMAP4._create_socket(self, timeout)
                # wrap_socket() takes over sock's file handle, so keep a
                # duplicate: shutting it down cuts the same connection.
                app._raw_sock = sock.dup()
                return self.ssl_context.wrap_socket(sock, server_hostname=self.host)

        imap = _TrackedIMAP4_SSL(server, port, ssl_context=context, timeout=IMAP_TIMEOUT_S)
        self._imap = imap
        self.log_msg(f"Connected. Logging in as {username} ...")
        imap.login(username, password)
        # readonly=True -> server treats session as EXAMINE, refuses to
        # change flags (including \Seen) on any message we touch.
        self.log_msg(f"Logged in. Opening '{mailbox}' read-only ...")
        status, data = imap.select(mailbox, readonly=True)
        if status != "OK":
            raise imaplib.IMAP4.error(f"could not open mailbox '{mailbox}': {data}")
        count = data[0].decode(errors="replace") if data and data[0] else "?"
        self.log_msg(f"Mailbox open ({count} messages). Searching ...")
        return imap

    def _log_render_notes(self, job, renderer):
        st = renderer.stats
        notes = []
        if st.get("remote_images"):
            notes.append(f"{st['remote_images']} remote image(s) loaded")
        if st.get("blocked"):
            notes.append(f"{st['blocked']} remote request(s) blocked")
        if st.get("truncated"):
            notes.append(f"body very long -- cut at {MAX_RENDER_PAGES} pages")
        if notes:
            self.log_msg(f"  UID {job['uid']}: " + ", ".join(notes))

    def _prefilter_in_range(self, imap, msg_nums, tz, range_start, range_end):
        """The server search is padded a day each side (it's date-only and in
        the server's own timezone), so it returns extra messages. Read just
        the Received/Date headers of all of them in a few batched requests and
        keep the ones really in the local range -- so progress totals match.
        Returns (in_range_nums, number_dropped)."""
        keep = set()
        self.log_msg(f"Server returned {len(msg_nums)} candidate(s); checking their dates ...")
        for i in range(0, len(msg_nums), 250):
            if self._cancel.is_set():
                break
            self.set_progress(i, len(msg_nums), "checking dates ")
            chunk = msg_nums[i:i + 250]
            status, data = imap.fetch(b",".join(chunk).decode(), "(BODY.PEEK[HEADER.FIELDS (RECEIVED DATE)])")
            if status != "OK":
                keep.update(chunk)  # can't tell -- the per-message check decides
                continue
            for item in data:
                if isinstance(item, tuple) and item[0]:
                    num = item[0].split(b" ", 1)[0]
                    dt, _ = self._message_time(email.message_from_bytes(item[1] or b""), tz)
                    if self._in_range(dt, range_start, range_end):
                        keep.add(num)
        kept = [n for n in msg_nums if n in keep]
        return kept, len(msg_nums) - len(kept)

    def _local_tz(self):
        tz = resolve_timezone(self._job.local_tz)
        return tz

    @staticmethod
    def _message_time(header_msg, tz):
        """(local aware datetime or None, source) -- from Received:, converted to tz."""
        aware, source = get_received_datetime(header_msg)
        return to_local(aware, tz), source

    @staticmethod
    def _in_range(local_dt, range_start, range_end):
        """Messages with no usable time at all are kept rather than silently dropped."""
        if local_dt is None:
            return True
        return range_start <= local_dt.replace(tzinfo=None) <= range_end

    @staticmethod
    def _imap_search_window(range_start, range_end):
        """IMAP SINCE/BEFORE are date-only and in the server's own zone, so
        pad by a day each side; the exact local-time filter runs client-side."""
        since = (range_start - timedelta(days=1)).strftime("%d-%b-%Y")
        before = (range_end + timedelta(days=2)).strftime("%d-%b-%Y")
        return since, before

    def _range_from_fields(self):
        return (
            datetime.strptime(
                f"{self._job.from_date.strip()} {self._job.from_time.strip()}", "%d-%b-%Y %H:%M"
            ),
            datetime.strptime(
                f"{self._job.to_date.strip()} {self._job.to_time.strip()}", "%d-%b-%Y %H:%M"
            ),
        )

    def _fetch_then_info_pdfs(self):
        """Attachments first, then EMAILINFO PDFs into the same folders, as one job."""
        self.fetch_attachments(final=False)
        if self._cancel.is_set():
            self.set_buttons_state("normal")
            return
        self.log_msg("--- Now creating EMAILINFO PDFs (Email Info PDF tab settings) ---")
        self.render_email_info_pdfs()

    def fetch_attachments(self, final=True):
        server = self._job.server.strip()
        port = int(self._job.port.strip())
        username = self._job.user.strip()
        password = self._job.password
        mailbox = self._job.mailbox.strip() or "INBOX"
        outdir = self._job.outdir.strip()
        only_attachments = self._job.only_attachments

        range_start = datetime.strptime(
            f"{self._job.from_date.strip()} {self._job.from_time.strip()}", "%d-%b-%Y %H:%M"
        )
        range_end = datetime.strptime(
            f"{self._job.to_date.strip()} {self._job.to_time.strip()}", "%d-%b-%Y %H:%M"
        )
        # IMAP SEARCH only supports day-level granularity, so we ask the
        # server for the whole day range first, then filter by exact
        # time-of-day ourselves using each message's Date header.
        imap_since, imap_before = self._imap_search_window(range_start, range_end)

        os.makedirs(outdir, exist_ok=True)

        try:
            imap = self._connect_imap(server, port, username, password, mailbox)

            self.log_msg(
                f"Searching '{mailbox}' from {range_start:%d-%b-%Y %H:%M} "
                f"through {range_end:%d-%b-%Y %H:%M} ..."
            )
            status, data = imap.search(None, f'(SINCE "{imap_since}" BEFORE "{imap_before}")')
            if status != "OK":
                self.log_msg("Search failed.")
                return

            msg_nums, skipped_by_time = self._prefilter_in_range(
                imap, data[0].split(), self._local_tz(), range_start, range_end)
            total = len(msg_nums)
            self.log_msg(f"{total} message(s) in range. Downloading ...")
            self.set_progress(0, total)

            saved_count = 0
            reply_to_count = 0
            tz = self._local_tz()
            for i, num in enumerate(msg_nums, start=1):
                if self._cancel.is_set():
                    self.log_msg("Stopped by user.")
                    break
                # Cheap first pass: just Received/Date headers, to filter by
                # local receive time before fetching the whole message.
                status, hdr_data = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (RECEIVED DATE)])")
                if status != "OK" or not hdr_data or hdr_data[0] is None:
                    self.set_progress(i, total)
                    continue

                sent_dt, _ = self._message_time(email.message_from_bytes(hdr_data[0][1]), tz)

                if not self._in_range(sent_dt, range_start, range_end):
                    skipped_by_time += 1
                    self.set_progress(i, total)
                    continue

                # BODY.PEEK[] as a second safeguard against marking \Seen
                status, msg_data = imap.fetch(num, "(UID BODY.PEEK[])")
                if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    self.set_progress(i, total)
                    continue

                uid_match = re.search(rb"UID (\d+)", msg_data[0][0] or b"")
                uid = uid_match.group(1).decode() if uid_match else num.decode()
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)
                subject = decode_mime_words(msg.get("Subject", "(no subject)"))
                sender_name, sender_email, used_reply_to = resolve_sender(
                    msg.get("From", ""), msg.get("Reply-To", "")
                )
                sender_email = sender_email or sender_name or "unknown_sender"
                if used_reply_to:
                    reply_to_count += 1

                date_folder = sent_dt.strftime("%Y-%m-%d") if sent_dt is not None else "unknown_date"

                # Layout: <outdir>/<YYYY-MM-DD>/<sender email> - <subject>/  -- one
                # folder per email (claimed on the first attachment, so emails
                # without attachments don't leave empty folders).
                msg_dir = None
                folder_name = ""

                attachments_saved_for_msg = 0
                saved_filepaths = []
                names_this_msg = set()
                for part in msg.walk():
                    if part.get_content_maintype() == "multipart":
                        continue
                    if part.get("Content-Disposition") is None:
                        continue

                    filename = part.get_filename()
                    if not filename:
                        continue
                    filename = decode_mime_words(filename)

                    # Only create the folder once we know there's something to save
                    if msg_dir is None:
                        msg_dir = claim_message_dir(outdir, date_folder, sender_email, subject, mailbox, uid)
                        folder_name = os.path.basename(msg_dir)
                    filepath = os.path.join(msg_dir, sanitize_for_path(filename, max_len=150))
                    # The folder belongs to this email alone, so a same-named
                    # file from an earlier run is this attachment: overwrite it.
                    # Only two attachments of THIS email sharing a name get _1, _2.
                    base, ext = os.path.splitext(filepath)
                    counter = 1
                    while filepath in names_this_msg:
                        filepath = f"{base}_{counter}{ext}"
                        counter += 1
                    names_this_msg.add(filepath)

                    payload = part.get_payload(decode=True)
                    if payload is None:
                        continue

                    with open(filepath, "wb") as f:
                        f.write(payload)

                    # Set the attachment's mtime/atime to when the email was sent
                    if sent_dt is not None:
                        touch_path(filepath, sent_dt)

                    attachments_saved_for_msg += 1
                    saved_count += 1
                    saved_filepaths.append(filepath)
                    self.log_msg(f"  Saved: {date_folder}/{folder_name}/{os.path.basename(filepath)}")

                if attachments_saved_for_msg == 0 and not only_attachments:
                    self.log_msg(f"  (no attachments) [{date_folder}] {subject} - {sender_email}")

                # Merge PDF attachments into MERGED_<email>.pdf
                if self._job.merge_pdfs and PYPDF_AVAILABLE and msg_dir is not None:
                    pdf_paths = [p for p in saved_filepaths if p.lower().endswith(".pdf")]
                    if len(pdf_paths) >= 2:
                        merged_name = f"MERGED_{sanitize_for_path(sender_email)}.pdf"
                        merged_path = os.path.join(msg_dir, merged_name)
                        try:
                            writer = PdfWriter()
                            for pdf_path in pdf_paths:
                                writer.append(pdf_path)
                            with open(merged_path, "wb") as f:
                                writer.write(f)
                            writer.close()
                            if sent_dt is not None:
                                touch_path(merged_path, sent_dt)
                            self.log_msg(
                                f"  Merged {len(pdf_paths)} PDF(s) -> {date_folder}/{folder_name}/{merged_name}"
                            )
                        except Exception as e:
                            self.log_msg(f"  PDF merge failed: {e}")

                # Touch the message folder and date folder with the sent time too
                if sent_dt is not None:
                    if msg_dir is not None and os.path.isdir(msg_dir):
                        touch_path(msg_dir, sent_dt)
                    date_dir = os.path.join(outdir, date_folder)
                    if os.path.isdir(date_dir):
                        touch_path(date_dir, sent_dt)

                self.set_progress(i, total)

            self.log_msg(
                f"Done. {saved_count} attachment(s) saved to {outdir} "
                f"({skipped_by_time} message(s) outside the exact time range were skipped, "
                f"{reply_to_count} used Reply-To instead of From)."
            )
            imap.logout()

        except imaplib.IMAP4.error as e:
            self.log_msg(f"IMAP error: {e}")
        except (TimeoutError, OSError) as e:
            if self._cancel.is_set():
                self.log_msg("Stopped by user (connection closed).")
            else:
                self.log_msg(f"Network error: {e or type(e).__name__} -- the server didn't respond "
                             f"within {IMAP_TIMEOUT_S}s. Check the server/port, or try again shortly.")
        except Exception as e:
            self.log_msg(f"Error: {e}")
        finally:
            if final:
                self.set_buttons_state("normal")


if __name__ == "__main__":
    root = tk.Tk()
    app = AttachmentFetcherApp(root)

    # Ctrl+C in the terminal: Tkinter would otherwise swallow it as an error
    # inside whatever callback was running. Route it to the same clean close
    # as the window's close button (finish current items, close browsers).
    import signal
    signal.signal(signal.SIGINT, lambda *_: root.after(0, app._on_close))

    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.shutdown()
