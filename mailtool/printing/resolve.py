"""Turning dropped strings (paths, file:// and WebDAV URLs) into print sources."""
from __future__ import annotations

import os
import posixpath
import re
import urllib.parse

from mailtool.core.util import IS_WIN
from mailtool.library.folders import looks_like_message_folder, ordered_print_files

URL_SCHEMES = {"webdav", "webdavs", "dav", "davs"}


class Source:
    def __init__(self, kind, key, display, path=None, url=None):
        self.kind, self.key, self.display, self.path, self.url = kind, key, display, path, url


def classify(raw):
    s = (raw or "").strip().strip("\x00")
    if not s:
        return None
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", s)
    if m and len(m.group(1)) > 1:  # >1 char excludes Windows drive letters
        scheme = m.group(1).lower()
        u = urllib.parse.urlparse(s)
        if scheme == "file":
            path = urllib.parse.unquote(u.path)
            host = u.netloc
            if IS_WIN:
                if host and host.lower() != "localhost":
                    path = "//" + host + path
                elif re.match(r"^/[A-Za-z]:", path):
                    path = path[1:]
                path = path.replace("/", "\\")
            elif host and host.lower() != "localhost":
                return None
            return path_source(path)
        if scheme in URL_SCHEMES:
            # tkdnd may hand over already-decoded URLs: '#', '?' and spaces are part of the path
            mm = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://([^/]*)(/.*)?$", s, re.S)
            if not mm:
                return None
            netloc, rawpath = mm.group(1), mm.group(2) or ""
            upath = urllib.parse.unquote(rawpath)
            hostpart = netloc.lower().split("@")[-1]
            key = ("https" if scheme.endswith("s") else "http") + "://" + hostpart + upath.rstrip("/")
            disp = posixpath.basename(upath.rstrip("/")) or hostpart
            enc = urllib.parse.quote(upath, safe="/:@,=+~!$&'()*;")
            return Source("url", key, disp, url="%s://%s%s" % (scheme, netloc, enc))
        return None
    if IS_WIN:
        p = s.replace("/", "\\")
        if re.match(r"^[A-Za-z]:\\", p) or p.startswith("\\\\"):
            return path_source(p)
        return None
    return path_source(s) if s.startswith("/") else None


def path_source(path):
    norm = os.path.normpath(path)
    if IS_WIN and path.startswith("\\\\") and not norm.startswith("\\\\"):
        norm = "\\" + norm
    return Source("path", os.path.normcase(norm), os.path.basename(norm.rstrip("\\/")) or norm, path=norm)


def parse_drop(tkapp, data):
    """Split tkdnd drop data (Tcl list, or newline-separated uri-list) into strings."""
    if not data:
        return []
    if "\n" in data and "{" not in data:
        parts = data.splitlines()
    else:
        try:
            parts = list(tkapp.splitlist(data))
        except Exception:
            parts = data.split()
    out = []
    for p in parts:
        for line in p.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def expand_folder(path, depth=2):
    """Printable files inside a dropped local folder. A MailTool message folder
    gives its EMAILINFO page then attachments; a date folder (or the library)
    gives each message folder in turn. Plain folders give their files by name."""
    if looks_like_message_folder(path):
        return ordered_print_files(path)
    out = []
    try:
        names = sorted(os.listdir(path), key=str.lower)
    except OSError:
        return out
    for n in names:
        p = os.path.join(path, n)
        if n.startswith("."):
            continue
        if os.path.isdir(p):
            if depth > 0:
                out += expand_folder(p, depth - 1)
        elif os.path.isfile(p) and not n.upper().startswith("MERGED_"):
            out.append(p)
    return out
