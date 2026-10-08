"""IMAP BODYSTRUCTURE parsing.

Asking the server for a message's MIME layout is cheap; with it we fetch only
the one text part (and inline images) we need, so attachment bytes are never
requested when they aren't wanted."""
from __future__ import annotations

import base64
import quopri
import re

from mailtool.mail.mime import decode_mime_words


def _tokenize(s):
    tokens = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c in "()":
            tokens.append(c)
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


def _parse_tokens(tokens):
    def expr(pos):
        tok = tokens[pos]
        if tok == "(":
            pos += 1
            lst = []
            while tokens[pos] != ")":
                val, pos = expr(pos)
                lst.append(val)
            return lst, pos + 1
        return tok, pos + 1
    value, _ = expr(0)
    return value


def _balanced(s, start):
    depth, in_quotes, i, n = 0, False, start, len(s)
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


def _literals_inline(msg_data):
    """imaplib splits a response containing {n} literals into tuples; rejoin them
    with the literal turned into a quoted string so the tokenizer sees one list."""
    out = []
    for part in msg_data:
        if isinstance(part, tuple):
            head, lit = part[0], part[1]
            head = re.sub(rb"\{\d+\}\s*$", b"", head or b"")
            lit = (lit or b"").replace(b"\\", b"\\\\").replace(b'"', b'\\"')
            out.append(head + b'"' + lit + b'"')
        elif isinstance(part, bytes):
            out.append(part)
    return b"".join(out)


def parse_response(msg_data):
    """Parse imaplib's raw FETCH (BODYSTRUCTURE) response into nested lists."""
    if not msg_data:
        return None
    text = _literals_inline(msg_data).decode("latin-1", errors="replace")
    idx = text.upper().find("BODYSTRUCTURE")
    if idx == -1:
        return None
    start = text.find("(", idx)
    if start == -1:
        return None
    tokens = _tokenize(_balanced(text, start))
    if not tokens:
        return None
    try:
        return _parse_tokens(tokens)
    except (IndexError, ValueError):
        return None


def _params_have_name(params):
    if not isinstance(params, list):
        return False
    for i in range(0, len(params) - 1, 2):
        k = params[i]
        if isinstance(k, str) and k.upper() in ("NAME", "FILENAME"):
            return True
    return False


def _param_value(params, wanted):
    if not isinstance(params, list):
        return None
    for i in range(0, len(params) - 1, 2):
        k = params[i]
        if isinstance(k, str) and k.upper() == wanted.upper():
            return params[i + 1]
    return None


def _find_param_recursive(node, wanted_keys):
    if not isinstance(node, list):
        return None
    for i in range(0, len(node) - 1, 2):
        k = node[i]
        if isinstance(k, str) and k.upper() in wanted_keys:
            return node[i + 1]
    for child in node:
        if isinstance(child, list):
            found = _find_param_recursive(child, wanted_keys)
            if found:
                return found
    return None


def _children(struct):
    i, kids = 0, []
    while i < len(struct) and isinstance(struct[i], list):
        kids.append(struct[i])
        i += 1
    return kids


def find_text_parts(struct, prefix=""):
    """Yield (part_number, subtype, charset, encoding, has_name) for every leaf
    TEXT/PLAIN or TEXT/HTML part."""
    if not isinstance(struct, list) or not struct:
        return
    if isinstance(struct[0], list):
        for idx, child in enumerate(_children(struct), start=1):
            yield from find_text_parts(child, f"{prefix}.{idx}" if prefix else str(idx))
        return
    ptype = struct[0] if isinstance(struct[0], str) else ""
    psub = struct[1] if len(struct) > 1 and isinstance(struct[1], str) else ""
    if ptype.upper() == "TEXT" and psub.upper() in ("PLAIN", "HTML"):
        params = struct[2] if len(struct) > 2 else None
        encoding = struct[5] if len(struct) > 5 and isinstance(struct[5], str) else "7BIT"
        charset = _param_value(params, "CHARSET") or "utf-8"
        yield (prefix or "1", psub.upper(), charset, encoding, _params_have_name(params))


def pick_body_part(struct):
    """Best TEXT/PLAIN (preferred) or TEXT/HTML part, skipping attached text files."""
    parts = list(find_text_parts(struct))
    for want in ("PLAIN", "HTML"):
        for p in parts:
            if p[1] == want and not p[4]:
                return p
    return None


def pick_html_part(struct):
    for p in find_text_parts(struct):
        if p[1] == "HTML" and not p[4]:
            return p
    return None


def find_attachment_parts(struct, body_part_nums=(), prefix="", skip_parts=frozenset()):
    """Yield (filename, size_in_bytes, part_number) for every real attachment or
    inline file - everything except the body part(s), their unnamed text
    alternatives, and parts listed in skip_parts. Reads structure only."""
    if not isinstance(struct, list) or not struct:
        return
    if isinstance(struct[0], list):
        for idx, child in enumerate(_children(struct), start=1):
            yield from find_attachment_parts(child, body_part_nums, f"{prefix}.{idx}" if prefix else str(idx),
                                             skip_parts)
        return
    part_num = prefix or "1"
    ptype = struct[0] if isinstance(struct[0], str) else "APPLICATION"
    psub = struct[1] if len(struct) > 1 and isinstance(struct[1], str) else "OCTET-STREAM"
    params = struct[2] if len(struct) > 2 else None
    has_name = _params_have_name(params) or bool(_find_param_recursive(struct, {"NAME", "FILENAME"}))
    unnamed_text = ptype.upper() == "TEXT" and psub.upper() in ("PLAIN", "HTML") and not has_name
    if part_num in body_part_nums or part_num in skip_parts or unnamed_text:
        return
    size = struct[6] if len(struct) > 6 and isinstance(struct[6], int) else None
    name = _find_param_recursive(struct, {"NAME", "FILENAME"})
    if not name:
        if len(struct) > 3 and isinstance(struct[3], str) and struct[3].strip():
            return      # unnamed part with a Content-ID: an image inside the HTML body, not a file
        name = f"part_{part_num} ({ptype}/{psub})".lower()
    yield (decode_mime_words(name) if isinstance(name, str) else str(name), size, part_num)


def find_part_by_cid(struct, cid, prefix=""):
    """(part_number, mime, encoding) of the part whose Content-ID is cid."""
    if not isinstance(struct, list) or not struct:
        return None
    if isinstance(struct[0], list):
        for idx, child in enumerate(_children(struct), start=1):
            found = find_part_by_cid(child, cid, f"{prefix}.{idx}" if prefix else str(idx))
            if found:
                return found
        return None
    part_id = struct[3] if len(struct) > 3 else None
    if isinstance(part_id, str) and part_id.strip("<> ").lower() == cid.strip("<> ").lower():
        mime = f"{struct[0]}/{struct[1]}".lower() if isinstance(struct[1], str) else "application/octet-stream"
        encoding = struct[5] if len(struct) > 5 and isinstance(struct[5], str) else "7BIT"
        return (prefix or "1", mime, encoding)
    return None


def inline_cid_images(html, struct, fetch_part):
    """Replace src="cid:..." with data: URIs. fetch_part(part_num) -> raw bytes or None.
    Only the parts the HTML references are fetched. Returns (html, inlined_part_numbers)."""
    cids = set(re.findall(r"cid:([^\"'\s>)]+)", html, flags=re.IGNORECASE))
    inlined = set()
    for cid in cids:
        found = find_part_by_cid(struct, cid)
        if not found:
            continue
        part_num, mime, encoding = found
        raw = fetch_part(part_num)
        if raw is None:
            continue
        try:
            if encoding.upper() == "BASE64":
                raw = base64.b64decode(raw, validate=False)
            elif encoding.upper() == "QUOTED-PRINTABLE":
                raw = quopri.decodestring(raw)
        except (ValueError, base64.binascii.Error):
            continue
        uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        html = re.sub(r"cid:" + re.escape(cid), lambda _m: uri, html, flags=re.IGNORECASE)
        inlined.add(part_num)
    return html, inlined
