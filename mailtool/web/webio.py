"""Request and response objects shared by the server and the API handlers."""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.parse

from mailtool.web.state import ApiError

MAX_JSON = 4 * 1024 * 1024
# shown in the browser; anything else is downloaded (an HTML or SVG attachment must never run on our origin)
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
                "text/plain", "text/csv"}


class Request:
    def __init__(self, handler, method, path, query, params):
        self.handler = handler
        self.method = method
        self.path = path
        self.query = query            # {name: first value}
        self.params = params          # regex groups from the route
        self.headers = handler.headers
        self._body_read = False

    @property
    def length(self):
        v = self.headers.get("Content-Length")
        try:
            return int(v) if v is not None else None
        except ValueError:
            raise ApiError(400, "Bad Content-Length.")

    @property
    def rfile(self):
        self._body_read = True
        return self.handler.rfile

    def json(self):
        n = self.length or 0
        if n > MAX_JSON:
            raise ApiError(413, "Request too large.")
        self._body_read = True
        raw = self.handler.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Bad JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "Expected a JSON object.")
        return data

    def q(self, name, default=None):
        return self.query.get(name, default)

    def qint(self, name, default):
        try:
            return int(self.query.get(name, default))
        except (TypeError, ValueError):
            return default


class Response:
    def __init__(self, body=b"", status=200, ctype="application/json; charset=utf-8", headers=None):
        self.body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.status, self.ctype, self.headers = status, ctype, dict(headers or {})


class FileResponse:
    """Send a file. inline=None decides from the type (only safe types are shown in the browser)."""

    def __init__(self, path, name=None, ctype=None, inline=None, cache=False):
        self.path = path
        self.name = name or os.path.basename(path)
        self.ctype = ctype or mimetypes.guess_type(self.name)[0] or "application/octet-stream"
        if inline is None:
            inline = self.ctype in INLINE_TYPES
        elif inline and self.ctype not in INLINE_TYPES:
            inline = False
        self.inline = inline
        self.cache = cache

    def disposition(self):
        quoted = urllib.parse.quote(self.name, safe="")
        ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in self.name)
        return '%s; filename="%s"; filename*=UTF-8\'\'%s' % ("inline" if self.inline else "attachment",
                                                              ascii_name, quoted)


def json_response(data, status=200):
    return Response(json.dumps(data, ensure_ascii=False, default=str), status)
