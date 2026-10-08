#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DropPrint - drag & drop print queue for PDFs, Word documents and images.

Works on Linux (KDE/KIO WebDAV, GVfs, kio-fuse, plain paths) and Windows 10
(WebClient redirector UNC paths / mapped drives). Everything is staged to a
local temp dir, normalised to PDF, then printed via CUPS (Linux) or
SumatraPDF (Windows).

    python dropprint.py            run the GUI
    python dropprint.py --check    print dependency table and exit
    python dropprint.py --probe    log raw drag&drop data (for debugging file managers)
    python dropprint.py a.pdf ...  start with files pre-queued

Optional config: <config dir>/dropprint/config.json  (keys: see DEFAULTS)
"""
from __future__ import annotations

import argparse
import atexit
import getpass
import glob
import hashlib
import itertools
import json
import logging
import os
import pathlib
import posixpath
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile
from logging.handlers import RotatingFileHandler

IS_WIN = os.name == "nt"
FROZEN = bool(getattr(sys, "frozen", False))  # running as a PyInstaller exe
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# where to look for bundled helper tools (e.g. SumatraPDF.exe next to the exe)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else SCRIPT_DIR

# ----------------------------------------------------------------------------
# Optional imports (each one is a capability, never a hard crash)
# ----------------------------------------------------------------------------
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, simpledialog, messagebox
    TK_ERR = None
except Exception as _e:  # pragma: no cover
    tk = ttk = filedialog = simpledialog = messagebox = None
    TK_ERR = _e

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES, DND_TEXT
    DND_ERR = None
except Exception as _e:
    TkinterDnD = None
    DND_FILES, DND_TEXT = "DND_Files", "DND_Text"
    DND_ERR = _e

try:
    from PIL import Image, ImageOps, ImageSequence
    PIL_ERR = None
except Exception as _e:
    Image = ImageOps = ImageSequence = None
    PIL_ERR = _e

try:
    import pypdf
    PYPDF_ERR = None
except Exception as _e:
    pypdf = None
    PYPDF_ERR = _e

win32print = None
if IS_WIN:
    try:
        import win32print  # type: ignore
    except Exception:
        win32print = None

log = logging.getLogger("dropprint")

DEFAULTS = {
    "printer": None,            # None = system default
    "paper": "A4",              # A4 | Letter (used for image -> PDF pages)
    "word_engine": "libreoffice",  # libreoffice | word (Windows, needs pywin32 + Word)
    "sumatra": None,            # explicit path to SumatraPDF.exe
    "soffice": None,            # explicit path to soffice
    "temp_dir": None,
    "copy_timeout": 600,        # seconds to fetch one file from a share
    "convert_timeout": 180,
    "print_timeout": 300,
    "clear_after": False,       # remove files from the list after a successful print
    "max_parallel": 4,          # files staged/converted at the same time (one thread each)
    "max_word": 1,              # Microsoft Word instances running at once (COM engine)
    "merge_batch": True,        # merge the whole queue into ONE pdf / ONE print job
}
URL_SCHEMES = {"webdav", "webdavs", "dav", "davs"}
MAX_FRAMES = 40


class UserError(Exception):
    """An error whose message is safe/useful to show to the user."""


# ----------------------------------------------------------------------------
# Config / logging / small helpers
# ----------------------------------------------------------------------------
def config_dir():
    if IS_WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    d = os.path.join(base, "dropprint")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = tempfile.gettempdir()
    return d


def setup_logging(verbose=False):
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        fh = RotatingFileHandler(os.path.join(config_dir(), "dropprint.log"),
                                 maxBytes=500_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    if verbose:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(config_dir(), "config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("bad config %s: %s", path, e)
    cfg["_path"] = path
    return cfg


def save_config(cfg):
    try:
        data = {k: v for k, v in cfg.items() if not k.startswith("_")}
        with open(cfg["_path"], "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning("could not save config: %s", e)


def run(cmd, timeout, cwd=None):
    """Run a command; never hangs the caller. Returns (rc, out, err); rc None = timeout, -1 = not runnable."""
    kw = {}
    if IS_WIN:
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    log.debug("run: %s", cmd)
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=cwd, **kw)
    except OSError as e:
        return -1, "", str(e)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except OSError:
            pass
        try:  # a process stuck in uninterruptible I/O may never die; don't wait forever
            p.communicate(timeout=3)
        except Exception:
            pass
        return None, "", ""
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def find_tool(names, extra=()):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    for p in extra:
        if p and os.path.isfile(p):
            return p
    return None


def longpath(p):
    if not IS_WIN or p.startswith("\\\\?\\") or len(p) < 240:
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def safe_stem(display):
    stem = os.path.splitext(display)[0]
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")[:80]
    return stem or "document"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------
# Dependency detection with install hints
# ----------------------------------------------------------------------------
def distro_family():
    if IS_WIN:
        return "win"
    ids = []
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k in ("ID", "ID_LIKE"):
                    ids += v.strip('"').lower().split()
    except OSError:
        pass
    for i in ids:
        if i in ("debian", "ubuntu", "linuxmint", "pop", "raspbian", "neon"):
            return "apt"
        if i in ("fedora", "rhel", "centos", "rocky", "almalinux"):
            return "dnf"
        if i in ("opensuse", "suse", "sles", "opensuse-leap", "opensuse-tumbleweed"):
            return "zypper"
        if i in ("arch", "manjaro", "endeavouros", "cachyos"):
            return "pacman"
    return None


def pip_cmd(pkg):
    if FROZEN:
        return "pip install %s   (then rebuild the exe so it gets bundled)" % pkg
    return '"%s" -m pip install %s%s' % (sys.executable, "" if IS_WIN else "--user ", pkg)


def pkg_hint(apt=None, dnf=None, zypper=None, pacman=None, win=None, pip=None):
    if IS_WIN:
        return win or (pip_cmd(pip) if pip else "")
    fam = distro_family()
    pk = {"apt": apt, "dnf": dnf, "zypper": zypper, "pacman": pacman}
    cmds = {"apt": "sudo apt install", "dnf": "sudo dnf install",
            "zypper": "sudo zypper install", "pacman": "sudo pacman -S"}
    if fam and pk.get(fam):
        return "%s %s" % (cmds[fam], pk[fam])
    if pip:
        return pip_cmd(pip)
    return "install '%s' with your package manager" % (apt or dnf or "the package")


class Cap:
    def __init__(self, key, label, ok, detail="", hint="", level="feature"):
        self.key, self.label, self.ok = key, label, ok
        self.detail, self.hint, self.level = detail, hint, level


def find_soffice(cfg):
    extra = [cfg.get("soffice")]
    if IS_WIN:
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            b = os.environ.get(env)
            if b:
                extra.append(os.path.join(b, "LibreOffice", "program", "soffice.exe"))
    else:
        extra += ["/usr/lib/libreoffice/program/soffice", "/usr/lib64/libreoffice/program/soffice"]
        extra += glob.glob("/opt/libreoffice*/program/soffice")
    return find_tool(["soffice", "libreoffice"], extra)


def find_sumatra(cfg):
    extra = [cfg.get("sumatra")]
    for d in dict.fromkeys([APP_DIR, SCRIPT_DIR]):
        extra.append(os.path.join(d, "SumatraPDF.exe"))
        extra += glob.glob(os.path.join(d, "SumatraPDF*.exe"))
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        b = os.environ.get(env)
        if b:
            extra.append(os.path.join(b, "SumatraPDF", "SumatraPDF.exe"))
    return find_tool(["SumatraPDF", "SumatraPDF.exe"], extra)


def find_kio():
    return find_tool(["kioclient6", "kioclient5", "kioclient"])


def find_gio():
    return find_tool(["gio"])


def have_word_com():
    if not IS_WIN:
        return False
    try:
        import winreg
        import win32com.client  # noqa: F401
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application"))
        return True
    except Exception:
        return False


def detect(cfg):
    caps = []
    add = lambda *a, **k: caps.append(Cap(*a, **k))  # noqa: E731
    add("tk", "Tk GUI", TK_ERR is None, str(TK_ERR or ""),
        pkg_hint(apt="python3-tk", dnf="python3-tkinter", zypper="python3-tk", pacman="tk",
                 win="re-run the python.org installer and enable 'tcl/tk and IDLE'"), "core")
    add("dnd", "Drag & drop (tkinterdnd2)", DND_ERR is None, str(DND_ERR or ""),
        pip_cmd("tkinterdnd2"), "feature")
    add("pil", "Images -> PDF (Pillow)", PIL_ERR is None, str(PIL_ERR or ""),
        pkg_hint(apt="python3-pil", dnf="python3-pillow", zypper="python3-Pillow",
                 pacman="python-pillow", pip="pillow"))
    add("pypdf", "PDF repair / page counts (pypdf)", PYPDF_ERR is None, str(PYPDF_ERR or ""),
        pkg_hint(apt="python3-pypdf", dnf="python3-pypdf", zypper="python3-pypdf",
                 pacman="python-pypdf", pip="pypdf"), "optional")
    if IS_WIN:
        sp = find_sumatra(cfg)
        add("print", "Printing (SumatraPDF)", bool(sp), sp or "", "winget install SumatraPDF.SumatraPDF", "core")
        add("pywin32", "Printer list / Word COM (pywin32)", win32print is not None, "",
            pip_cmd("pywin32"), "optional")
        add("word", "Word engine: Word (COM)", have_word_com(), "", "install Microsoft Word + pywin32", "optional")
    else:
        lp, ls = find_tool(["lp"]), find_tool(["lpstat"])
        add("print", "Printing (CUPS lp/lpstat)", bool(lp and ls), lp or "",
            pkg_hint(apt="cups-client", dnf="cups-client", zypper="cups-client", pacman="cups"), "core")
        add("gio", "dav:// and davs:// drops (gio)", bool(find_gio()), find_gio() or "",
            pkg_hint(apt="libglib2.0-bin", dnf="glib2", zypper="glib2-tools", pacman="glib2"), "optional")
        add("kio", "webdav:// and webdavs:// drops (kioclient)", bool(find_kio()), find_kio() or "",
            pkg_hint(apt="kde-cli-tools kio-extras", dnf="kde-cli-tools kio-extras",
                     zypper="kde-cli-tools kio-extras", pacman="kde-cli-tools kio-extras"), "optional")
    add("gs", "Robust PDF merging (Ghostscript)", bool(find_gs()), find_gs() or "",
        pkg_hint(apt="ghostscript", dnf="ghostscript", zypper="ghostscript", pacman="ghostscript",
                 win="winget install ArtifexSoftware.GhostScript"), "optional")
    add("preview", "Page previews (PyMuPDF / pdftoppm / Ghostscript)", bool(preview_engine()),
        preview_engine() or "",
        pkg_hint(apt="poppler-utils", dnf="poppler-utils", zypper="poppler-tools", pacman="poppler",
                 pip="pymupdf"), "optional")
    so = find_soffice(cfg)
    add("soffice", "Word -> PDF (LibreOffice)", bool(so), so or "",
        pkg_hint(apt="libreoffice-writer", dnf="libreoffice-writer", zypper="libreoffice-writer",
                 pacman="libreoffice-fresh", win="winget install TheDocumentFoundation.LibreOffice"))
    return caps


def format_caps(caps):
    lines = []
    for c in caps:
        mark = "[ OK ]" if c.ok else ("[ -- ]" if c.level == "optional" else "[MISS]")
        lines.append("%s %s%s" % (mark, c.label, ("  (%s)" % c.detail) if c.ok and c.detail else ""))
        if not c.ok and c.hint:
            lines.append("         install: %s" % c.hint)
    if not IS_WIN:
        lines.append("\nNote: if pip says 'externally-managed-environment', use a venv/pipx, "
                     "the distro package, or add --break-system-packages.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Resolver: dropped string -> Source
# ----------------------------------------------------------------------------
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
            return _path_source(path)
        if scheme in URL_SCHEMES:
            # Parse by hand: tkdnd may hand us already-decoded URLs, so '#', '?' and
            # spaces in file names must be treated as part of the path, not URL syntax.
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
            return _path_source(p)
        return None
    return _path_source(s) if s.startswith("/") else None


def _path_source(path):
    norm = os.path.normpath(path)
    if IS_WIN and path.startswith("\\\\") and not norm.startswith("\\\\"):
        norm = "\\" + norm
    key = os.path.normcase(norm)
    return Source("path", key, os.path.basename(norm.rstrip("\\/")) or norm, path=norm)


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


# ----------------------------------------------------------------------------
# Type sniffing
# ----------------------------------------------------------------------------
def sniff(path, display=""):
    """Return (kind, ext) where kind in pdf|image|word. Raises UserError."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if not head:
        raise UserError("file is empty")
    if b"%PDF-" in head[:1024]:
        return "pdf", ".pdf"
    if head.startswith(b"\x89PNG"):
        return "image", ".png"
    if head[:3] == b"\xff\xd8\xff":
        return "image", ".jpg"
    if head[:4] == b"GIF8":
        return "image", ".gif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image", ".tif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", ".webp"
    if head[:2] == b"BM" and display.lower().endswith(".bmp"):
        return "image", ".bmp"
    if head.startswith(b"{\\rtf"):
        return "word", ".rtf"
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if "word/document.xml" in names:
                    return "word", ".docx"
                if "mimetype" in names and z.read("mimetype").strip() == b"application/vnd.oasis.opendocument.text":
                    return "word", ".odt"
                if "xl/workbook.xml" in names:
                    raise UserError("Excel files are not supported")
                if any(n.startswith("ppt/") for n in names):
                    raise UserError("PowerPoint files are not supported")
        except zipfile.BadZipFile:
            raise UserError("corrupt or unsupported archive/document")
        raise UserError("unsupported file type")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        with open(path, "rb") as f:
            blob = f.read(4 << 20)
        if "WordDocument".encode("utf-16-le") in blob:
            return "word", ".doc"
        raise UserError("unsupported Office file (only Word documents are supported)")
    raise UserError("unsupported file type (accepted: PDF, Word, ODT, RTF, images)")


# ----------------------------------------------------------------------------
# Items + core pipeline (stage -> sniff -> dedupe -> convert -> inspect)
# ----------------------------------------------------------------------------
_SEQ = itertools.count()


class Item:
    def __init__(self, src):
        self.id = uuid.uuid4().hex[:10]
        self.seq = next(_SEQ)    # drop order; decides which duplicate is kept
        self.src = src
        self.display = src.display or "(unnamed)"
        self.status = "queued"   # queued staging converting ready printing done failed dup
        self.msg = ""
        self.pages = None
        self.copies = 1
        self.pdf = None
        self.dir = None
        self.hash = None
        self.removed = False
        self.noted = False
        self.excluded = set()    # 0-based page indexes left out of the print
        self.page_order = None   # custom print order of page indexes (None = document order)
        self.thumbs = {}         # {width: {page index: png path}}
        self.thumbs_done = set() # widths fully rendered

    def text(self):
        return "%s: %s" % (self.status, self.msg) if self.msg else self.status

    def pages_text(self):
        if self.pages is None:
            return ""
        if self.excluded or self.page_order:
            txt = "%d/%d" % (max(self.pages - len(self.excluded), 0), self.pages)
            return txt + (" \u2195" if self.page_order else "")
        return self.pages


PAPER_PX = {"A4": (1654, 2339), "LETTER": (1700, 2200)}  # 200 dpi


def _flatten(im):
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


def image_to_pdf(src, out, paper):
    W, H = PAPER_PX.get(str(paper).upper(), PAPER_PX["A4"])
    margin = 50
    pages = []
    try:
        with Image.open(src) as im:
            for i, frame in enumerate(ImageSequence.Iterator(im)):
                if i >= MAX_FRAMES:
                    break
                f = _flatten(ImageOps.exif_transpose(frame.copy()))
                pw, ph = (W, H) if f.height >= f.width else (H, W)
                scale = min((pw - 2 * margin) / f.width, (ph - 2 * margin) / f.height)
                nw, nh = max(1, int(f.width * scale)), max(1, int(f.height * scale))
                f = f.resize((nw, nh), Image.LANCZOS)
                page = Image.new("RGB", (pw, ph), "white")
                page.paste(f, ((pw - nw) // 2, (ph - nh) // 2))
                pages.append(page)
    except UserError:
        raise
    except Exception as e:
        raise UserError("image could not be read (%s)" % (str(e)[:80] or e.__class__.__name__))
    if not pages:
        raise UserError("image has no frames")
    pages[0].save(out, "PDF", resolution=200.0, save_all=True, append_images=pages[1:])
    return out


def word_com_convert(src, out, timeout):
    res = {}

    def job():
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            w = doc = None
            try:
                w = win32com.client.DispatchEx("Word.Application")
                w.Visible = False
                w.DisplayAlerts = 0
                doc = w.Documents.Open(os.path.abspath(src), ReadOnly=True,
                                       AddToRecentFiles=False, ConfirmConversions=False)
                doc.ExportAsFixedFormat(os.path.abspath(out), 17)  # wdExportFormatPDF
            finally:
                if doc is not None:
                    try:
                        doc.Close(False)
                    except Exception:
                        pass
                if w is not None:
                    try:
                        w.Quit()
                    except Exception:
                        pass
                pythoncom.CoUninitialize()
        except Exception as e:
            res["err"] = e

    t = threading.Thread(target=job, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise UserError("Word did not respond (a dialog may be open)")
    if "err" in res:
        raise UserError("Word failed: %s" % str(res["err"])[:100])
    if not os.path.isfile(out):
        raise UserError("Word produced no PDF")


def inspect_pdf(path):
    """Return page count (or None if pypdf is unavailable). Raises UserError for bad PDFs."""
    if pypdf is None:
        return None
    try:
        r = pypdf.PdfReader(path)
        if r.is_encrypted:
            try:
                if not r.decrypt(""):
                    raise UserError("PDF is password-protected")
            except UserError:
                raise
            except Exception:
                return None  # e.g. AES without 'cryptography'; CUPS/Sumatra can still handle it
        n = len(r.pages)
    except UserError:
        raise
    except Exception as e:
        raise UserError("PDF is unreadable (%s)" % (str(e)[:60] or e.__class__.__name__))
    if n == 0:
        raise UserError("PDF has no pages")
    return n


def _ap_ok(ap):
    """True if every appearance stream in an /AP dict has the required /BBox."""
    try:
        n = ap.get("/N")
        if n is None:
            return True
        n = n.get_object()
        streams = [n] if hasattr(n, "get_data") else [v.get_object() for v in n.values()]
        return all("/BBox" in s for s in streams)
    except Exception:
        return True


def _annot_bad(a):
    ap = a.get_object().get("/AP")
    return ap is not None and not _ap_ok(ap.get_object())


def sanitize_pdf(path):
    """Drop annotations whose appearance streams lack /BBox (CUPS pdftopdf aborts on these).
    Rewrites the file in place only if something was wrong. Returns number removed."""
    if pypdf is None:
        return 0
    try:
        r = pypdf.PdfReader(path)
        if r.is_encrypted and not r.decrypt(""):
            return 0
        bad = 0
        for page in r.pages:
            annots = page.get("/Annots")
            for a in (annots.get_object() if annots is not None else []):
                bad += _annot_bad(a)
        if not bad:
            return 0
        try:
            w = pypdf.PdfWriter(clone_from=r)
        except TypeError:  # older pypdf
            w = pypdf.PdfWriter()
            w.append_pages_from_reader(r)
        for page in w.pages:
            annots = page.get("/Annots")
            if annots is None:
                continue
            keep = pypdf.generic.ArrayObject([a for a in annots.get_object() if not _annot_bad(a)])
            page[pypdf.generic.NameObject("/Annots")] = keep
        tmp = path + ".fixed"
        with open(tmp, "wb") as f:
            w.write(f)
        os.replace(tmp, path)
        log.info("repaired %d broken annotation(s) in %s", bad, path)
        return bad
    except Exception as e:
        log.warning("PDF repair skipped for %s: %s", path, e)
        return 0


HELPER = r'''
import os, shutil, sys
src, dst = sys.argv[1], sys.argv[2]
try:
    if os.path.isdir(src): sys.exit(3)
    if not os.path.isfile(src): sys.exit(4)
    shutil.copyfile(src, dst)
except PermissionError: sys.exit(5)
except OSError as e:
    sys.stderr.write(str(e)); sys.exit(6)
'''
def copy_local(src, dst):
    """In-process equivalent of HELPER. Returns (exit code, message)."""
    try:
        if os.path.isdir(src):
            return 3, ""
        if not os.path.isfile(src):
            return 4, ""
        shutil.copyfile(src, dst)
        return 0, ""
    except PermissionError:
        return 5, ""
    except OSError as e:
        return 6, str(e)


AUTH_RE = re.compile(r"password|authenticat|unauthori[sz]ed|401|403|forbidden|credential|login", re.I)


def sweep(root):
    now = time.time()
    for d in glob.glob(os.path.join(root, "session-*")):
        try:
            pid = int(os.path.basename(d).split("-", 1)[1])
        except (ValueError, IndexError):
            continue
        dead = False
        if IS_WIN:
            dead = now - os.path.getmtime(d) > 2 * 86400
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                dead = True
            except PermissionError:
                pass
        if dead and pid != os.getpid():
            shutil.rmtree(d, ignore_errors=True)


def find_gs():
    extra = []
    if IS_WIN:
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            b = os.environ.get(env)
            if b:
                extra += sorted(glob.glob(os.path.join(b, "gs", "gs*", "bin", "gswin*c.exe")), reverse=True)
    return find_tool(["gswin64c", "gswin32c", "gs"] if IS_WIN else ["gs"], extra)


def merge_pdfs(paths, out, cfg):
    """Merge PDFs (paths may repeat for extra copies). Returns engine name. Raises UserError."""
    errs = []
    gs = find_gs()
    if gs:  # full re-render: also cleans up structural damage that breaks CUPS filters
        rc, o, e = run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite",
                        "-dPrinted=true", "-sOutputFile=" + out] + list(paths),
                       cfg["convert_timeout"] * 5)
        if rc == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            return "Ghostscript"
        errs.append("Ghostscript failed: %s" % ((e or o or "timeout").strip()[:120]))
        log.warning(errs[-1])
    if pypdf is None:
        raise UserError("; ".join(errs + ["cannot merge PDFs without Ghostscript or pypdf"]))
    try:
        w = pypdf.PdfWriter()
        for pth in paths:
            r = pypdf.PdfReader(pth)
            if r.is_encrypted:
                r.decrypt("")
            for page in r.pages:
                if "/Annots" in page:
                    del page["/Annots"]  # annotations are what breaks pdftopdf; drop them in fallback mode
                w.add_page(page)
        with open(out, "wb") as f:
            w.write(f)
    except Exception as e:
        raise UserError("merge failed: %s" % str(e)[:120])
    return "pypdf (annotations removed)"


THUMB_W = 170
_FITZ = False


def get_fitz():
    global _FITZ
    if _FITZ is False:
        _FITZ = None
        for name in ("pymupdf", "fitz"):
            try:
                m = __import__(name)
                if hasattr(m, "open"):
                    _FITZ = m
                    break
            except Exception:
                continue
    return _FITZ


def preview_engine():
    if get_fitz():
        return "PyMuPDF"
    if find_tool(["pdftoppm"]):
        return "pdftoppm"
    if find_gs():
        return "Ghostscript"
    return None


def _collect_pngs(outdir, pat):
    found = []
    for f in os.listdir(outdir):
        m = re.search(pat, f)
        if m:
            found.append((int(m.group(1)) - 1, os.path.join(outdir, f)))
    return sorted(found)


def render_pages(pdf, outdir, cfg, emit, limit=300, width=THUMB_W):
    """Render page thumbnails `width` px wide, calling emit(index, path); if emit returns True the
    render is aborted. Returns total page count (or None if unknown). Raises UserError."""
    os.makedirs(outdir, exist_ok=True)
    fz = get_fitz()
    if fz:
        try:
            doc = fz.open(pdf)
            if getattr(doc, "needs_pass", False):
                doc.authenticate("")
            total = len(doc)
            for i in range(min(total, limit)):
                pg = doc[i]
                z = width / max(pg.rect.width, 1.0)
                path = os.path.join(outdir, "p%04d.png" % i)
                pg.get_pixmap(matrix=fz.Matrix(z, z)).save(path)
                if emit(i, path):
                    break
            doc.close()
            return total
        except Exception as e:
            raise UserError("preview failed: %s" % str(e)[:100])
    pp = find_tool(["pdftoppm"])
    if pp:
        base = [pp, "-png", "-l", str(limit)]
        run(base + ["-scale-to-x", str(width), "-scale-to-y", "-1", pdf, os.path.join(outdir, "p")],
            cfg["convert_timeout"])
        pat = r"p-(\d+)\.png$"
        if not _collect_pngs(outdir, pat):  # older poppler without -scale-to-x
            run(base + ["-scale-to", str(int(width * 1.35)), pdf, os.path.join(outdir, "p")],
                cfg["convert_timeout"])
    else:
        gs = find_gs()
        if not gs:
            raise UserError("no page renderer found")
        run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dLastPage=%d" % limit, "-sDEVICE=png16m",
             "-r%d" % max(10, int(width / 8.27)), "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
             "-sOutputFile=" + os.path.join(outdir, "g%04d.png"), pdf], cfg["convert_timeout"])
        pat = r"g(\d+)\.png$"
    found = _collect_pngs(outdir, pat)
    if not found:
        raise UserError("preview rendering produced no images")
    for i, path in found:
        if emit(i, path):
            break
    return None


def render_page(pdf, index, out, width, cfg):
    """Render one page (0-based) `width` px wide to PNG `out`. Raises UserError."""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fz = get_fitz()
    if fz:
        try:
            doc = fz.open(pdf)
            if getattr(doc, "needs_pass", False):
                doc.authenticate("")
            pg = doc[index]
            z = width / max(pg.rect.width, 1.0)
            pg.get_pixmap(matrix=fz.Matrix(z, z)).save(out)
            doc.close()
            return out
        except Exception as e:
            raise UserError("render failed: %s" % str(e)[:100])
    pp = find_tool(["pdftoppm"])
    if pp:
        run([pp, "-png", "-f", str(index + 1), "-l", str(index + 1), "-singlefile", "-scale-to-x", str(width),
             "-scale-to-y", "-1", pdf, out[:-4]], cfg["convert_timeout"])
        if os.path.isfile(out):
            return out
        raise UserError("pdftoppm could not render this page")
    gs = find_gs()
    if gs:
        run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dFirstPage=%d" % (index + 1),
             "-dLastPage=%d" % (index + 1), "-sDEVICE=png16m", "-r%d" % max(20, int(width / 8.27)),
             "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4", "-sOutputFile=" + out, pdf], cfg["convert_timeout"])
        if os.path.isfile(out):
            return out
        raise UserError("Ghostscript could not render this page")
    raise UserError("no page renderer found")


def parse_ranges(text, n):
    """'2-4, 7, 9-' -> {1,2,3,6,8,...} (0-based, clipped to n). Raises ValueError."""
    out = set()
    for tok in re.split(r"[,\s;]+", text.strip()):
        if not tok:
            continue
        m = re.match(r"^(\d*)\s*-\s*(\d*)$", tok)
        if m and (m.group(1) or m.group(2)):
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else n
        elif tok.isdigit():
            a = b = int(tok)
        else:
            raise ValueError(tok)
        if a > b:
            a, b = b, a
        out.update(range(max(a, 1) - 1, min(b, n)))
    return out


def compress_ranges(keep):
    """[0,2,3,4] -> '1,3-5' (1-based, for Ghostscript -sPageList)."""
    keep = sorted(keep)
    parts, i = [], 0
    while i < len(keep):
        j = i
        while j + 1 < len(keep) and keep[j + 1] == keep[j] + 1:
            j += 1
        parts.append(str(keep[i] + 1) if i == j else "%d-%d" % (keep[i] + 1, keep[j] + 1))
        i = j + 1
    return ",".join(parts)


def subset_pdf(src, out, keep, cfg):
    """Write the pages `keep` (0-based, in the given order) of src to out."""
    if pypdf is not None:
        try:
            r = pypdf.PdfReader(src)
            if r.is_encrypted:
                r.decrypt("")
            w = pypdf.PdfWriter()
            for i in keep:
                w.add_page(r.pages[i])
            with open(out, "wb") as f:
                w.write(f)
            return
        except Exception as e:
            log.warning("pypdf subset failed (%s); trying Ghostscript", e)
    gs = find_gs()
    if not gs:
        raise UserError("excluding/reordering pages needs pypdf or Ghostscript")
    base = [gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite"]
    if list(keep) == sorted(set(keep)):  # document order: a single page-list pass
        rc, o, e = run(base + ["-sPageList=" + compress_ranges(keep), "-sOutputFile=" + out, src],
                       cfg["convert_timeout"] * 3)
        if rc != 0 or not os.path.isfile(out):
            raise UserError("could not drop pages: %s" % (e or o or "timeout").strip()[:100])
        return
    tmpd = out + ".pages"  # reordered: extract each page, then merge in the wanted order
    os.makedirs(tmpd, exist_ok=True)
    try:
        files = []
        for n, i in enumerate(keep):
            f = os.path.join(tmpd, "%04d.pdf" % n)
            rc, o, e = run(base + ["-dFirstPage=%d" % (i + 1), "-dLastPage=%d" % (i + 1),
                                   "-sOutputFile=" + f, src], cfg["convert_timeout"])
            if rc != 0 or not os.path.isfile(f):
                raise UserError("could not extract page %d: %s" % (i + 1, (e or o or "timeout").strip()[:80]))
            files.append(f)
        merge_pdfs(files, out, cfg)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def merge_sig(items):
    """Fingerprint of everything that affects the merged PDF (content, pages, order, copies)."""
    return tuple((i.id, i.hash, tuple(sorted(i.excluded)), tuple(i.page_order or ()), i.copies) for i in items)


class Limiter:
    """Counting limiter whose limit can be changed while threads are waiting on it."""

    def __init__(self, n):
        self.n, self.active, self.cv = max(1, int(n)), 0, threading.Condition()

    def set(self, n):
        with self.cv:
            self.n = max(1, int(n))
            self.cv.notify_all()

    def __enter__(self):
        with self.cv:
            while self.active >= self.n:
                self.cv.wait()
            self.active += 1

    def __exit__(self, *exc):
        with self.cv:
            self.active -= 1
            self.cv.notify_all()


class Core:
    def __init__(self, cfg, notify, caps):
        self.cfg, self.notify = cfg, notify
        self.caps = {c.key: c for c in caps}
        root = cfg.get("temp_dir") or os.path.join(tempfile.gettempdir(), "dropprint-" + getpass.getuser())
        os.makedirs(root, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        sweep(root)
        self.root = os.path.join(root, "session-%d" % os.getpid())
        os.makedirs(self.root, exist_ok=True)
        self.q = queue.Queue()
        self.hashes = {}
        self.lock = threading.Lock()
        self.slots = Limiter(cfg.get("max_parallel") or 4)
        self.word_slots = Limiter(cfg.get("max_word") or 1)
        threading.Thread(target=self._loop, daemon=True).start()
        atexit.register(self.cleanup)

    def apply_settings(self):
        self.slots.set(self.cfg.get("max_parallel") or 4)
        self.word_slots.set(self.cfg.get("max_word") or 1)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def hint(self, key):
        c = self.caps.get(key)
        return c.hint if c else ""

    # -- state helpers
    def set(self, it, status, msg=""):
        it.status, it.msg = status, msg
        self.notify(("upd", it))

    def fail(self, it, msg):
        it.status, it.msg = "failed", msg
        self.notify(("upd", it))

    def effective_pdf(self, it):
        """The PDF to print for this item: excluded pages removed, custom page order applied."""
        if not it.excluded and not it.page_order:
            return it.pdf
        n = it.pages or 0
        order = [i for i in (it.page_order or []) if 0 <= i < n]
        order += [i for i in range(n) if i not in order]
        keep = [i for i in order if i not in it.excluded]
        if not keep:
            raise UserError("all pages are excluded")
        out = os.path.join(it.dir, "subset.pdf")
        subset_pdf(it.pdf, out, keep, self.cfg)
        return out

    def build_merged(self, items, mdir):
        """Build the merged PDF for `items` (in order) inside mdir. Returns (path, engine)."""
        os.makedirs(mdir, exist_ok=True)
        inputs = []
        for n, it in enumerate(items):
            tmp = os.path.join(mdir, "%04d.pdf" % n)  # ASCII names: safe for Ghostscript on Windows
            shutil.copyfile(self.effective_pdf(it), tmp)
            inputs += [tmp] * it.copies              # per-item copies, collated
        out = os.path.join(mdir, "merged.pdf")
        return out, merge_pdfs(inputs, out, self.cfg)

    def forget(self, it):
        it.removed = True
        with self.lock:
            for h in [h for h, v in self.hashes.items() if v is it]:
                del self.hashes[h]
        if it.dir:
            shutil.rmtree(it.dir, ignore_errors=True)

    # -- worker
    def _loop(self):
        """Dispatcher: every queued item gets its own thread (concurrency capped by self.slots)."""
        while True:
            it = self.q.get()
            if it is None:
                return
            if it.removed:
                continue
            threading.Thread(target=self._run_item, args=(it,), daemon=True).start()

    def _run_item(self, it):
        with self.slots:
            if it.removed:
                return
            try:
                self.process(it)
            except UserError as e:
                self.fail(it, str(e))
            except Exception as e:
                log.exception("unexpected error for %s", it.display)
                self.fail(it, "unexpected error: %s" % e)

    def process(self, it):
        it.dir = os.path.join(self.root, it.id)
        os.makedirs(it.dir, exist_ok=True)
        self.set(it, "staging")
        raw = os.path.join(it.dir, "staged.bin")
        self.stage(it.src, raw)
        if it.removed:
            return
        kind, ext = sniff(raw, it.display)
        h = sha256(raw)
        evict = None
        with self.lock:
            other = self.hashes.get(h)
            if other is not None and other is not it and not other.removed:
                if other.seq > it.seq and other.status in ("queued", "staging", "converting", "ready"):
                    evict, dup = other, None   # threads finish in any order: keep the file dropped first
                    self.hashes[h] = it
                else:
                    dup = other
            else:
                self.hashes[h] = it
                dup = None
        if evict is not None:
            evict.status, evict.msg = "dup", it.display
            self.notify(("dup", evict))
        if dup:
            it.status, it.msg = "dup", dup.display
            shutil.rmtree(it.dir, ignore_errors=True)
            self.notify(("dup", it))
            return
        it.hash = h
        src_file = os.path.join(it.dir, safe_stem(it.display) + ext)
        os.replace(raw, src_file)
        if kind == "pdf":
            pdf = src_file
        else:
            self.set(it, "converting")
            pdf = self.to_pdf(kind, src_file, it)
        if it.removed:
            return
        fixed = sanitize_pdf(pdf)
        it.pages = inspect_pdf(pdf)
        it.pdf = pdf
        self.set(it, "ready", ("repaired %d broken annotation(s)" % fixed) if fixed else "")

    # -- staging
    def stage(self, src, dst):
        if src.kind == "path":
            self._stage_path(src.path, dst)
        else:
            self._stage_url(src.url, dst)

    def _stage_path(self, path, dst):
        if FROZEN:
            # A frozen exe cannot re-launch itself as "python -c ...", so copy in a thread. If the
            # share hangs, the thread is abandoned (daemon) and we just report a timeout.
            res = {}
            t = threading.Thread(target=lambda: res.update(r=copy_local(longpath(path), dst)), daemon=True)
            t.start()
            t.join(self.cfg["copy_timeout"])
            rc, err = (None, "") if t.is_alive() else res.get("r", (6, "copy failed"))
            out = ""
        else:
            rc, out, err = run([sys.executable, "-c", HELPER, longpath(path), dst], self.cfg["copy_timeout"])
        if rc is None:
            raise UserError("timed out reading the file (share unreachable or mount hung)")
        msgs = {3: "is a folder - only files are accepted", 4: "file not found or not readable",
                5: "permission denied"}
        if rc == 0 and os.path.isfile(dst):
            return
        if rc in msgs:
            raise UserError(msgs[rc])
        raise UserError((err or out or "could not read file").strip()[:150])

    def _stage_url(self, url, dst):
        scheme = url.split(":", 1)[0].lower()
        gio, kio = find_gio(), find_kio()
        if not (gio or kio):
            raise UserError("cannot fetch WebDAV URLs without gio or kioclient. Install: %s / %s"
                            % (self.hint("gio"), self.hint("kio")))
        rest = url.split(":", 1)[1]
        gio_url = ("davs:" if scheme.endswith("s") else "dav:") + rest
        kio_url = ("webdavs:" if scheme.endswith("s") else "webdav:") + rest
        tools = [("kio", kio, kio_url), ("gio", gio, gio_url)]
        if scheme in ("dav", "davs"):
            tools.reverse()
        last = ""
        for name, exe, u in tools:
            if not exe:
                continue
            if name == "gio":
                rc, out, err = run([exe, "info", "-a", "standard::type", u], 60)
                if rc == 0 and re.search(r"standard::type:\s*(directory|3)\b", out):
                    raise UserError("is a folder - only files are accepted")
            rc, out, err = run([exe, "copy", u, dst], self.cfg["copy_timeout"])
            if rc is None:
                last = "timed out (an authentication dialog may be waiting)"
                continue
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
                raise UserError("is a folder - only files are accepted")
            if rc == 0 and os.path.isfile(dst):
                return
            last = (err or out or "%s failed" % name).strip()[:150]
        if AUTH_RE.search(last):
            last += " -> open the share once in your file manager to save the credentials"
        raise UserError(last or "could not fetch URL")

    # -- conversion
    def to_pdf(self, kind, src_file, it):
        out = os.path.splitext(src_file)[0] + ".pdf"
        if kind == "image":
            if Image is None:
                raise UserError("images need Pillow. Install: %s" % self.hint("pil"))
            return image_to_pdf(src_file, out, self.cfg["paper"])
        return self.word_to_pdf(src_file, out)

    def word_to_pdf(self, src, out):
        first_err = None
        if IS_WIN and self.cfg["word_engine"] == "word" and have_word_com():
            try:
                with self.word_slots:  # limit simultaneous Word instances
                    word_com_convert(src, out, self.cfg["convert_timeout"])
                return out
            except UserError as e:
                first_err = str(e)
                log.warning("Word COM failed: %s", e)
        so = find_soffice(self.cfg)
        if not so:
            raise UserError(first_err or "Word documents need LibreOffice. Install: %s" % self.hint("soffice"))
        profile = os.path.join(os.path.dirname(src), "lo-profile")  # own profile per document
        cmd = [so, "-env:UserInstallation=" + pathlib.Path(os.path.abspath(profile)).as_uri(),
               "--headless", "--convert-to", "pdf", src, "--outdir", os.path.dirname(src)]
        try:
            rc, o, e = run(cmd, self.cfg["convert_timeout"])
        finally:
            shutil.rmtree(profile, ignore_errors=True)
        if rc is None:
            raise UserError("LibreOffice timed out converting the document")
        if not os.path.isfile(out):
            raise UserError("LibreOffice could not convert this file (password-protected or corrupt?)")
        return out


# ----------------------------------------------------------------------------
# Print backends
# ----------------------------------------------------------------------------
class CupsBackend:
    name = "CUPS"

    def __init__(self, cfg):
        self.cfg = cfg
        self.lp = find_tool(["lp"])
        self.lpstat = find_tool(["lpstat"])
        self.cancel_bin = find_tool(["cancel"])

    def ok(self):
        return bool(self.lp and self.lpstat)

    def printers(self):
        names, default = [], None
        rc, out, _ = run([self.lpstat, "-e"], 15)
        if rc == 0:
            names = [l.strip() for l in out.splitlines() if l.strip()]
        if not names:
            rc, out, _ = run([self.lpstat, "-p"], 15)
            names = re.findall(r"^printer (\S+)", out or "", re.M)
        rc, out, _ = run([self.lpstat, "-d"], 15)
        m = re.search(r"destination:\s*(\S+)", out or "")
        if m:
            default = m.group(1)
        return names, default

    def submit(self, pdf, printer, copies, title):
        cmd = [self.lp, "-t", title[:100], "-n", str(copies)]
        if printer:
            cmd += ["-d", printer]
        cmd.append(pdf)
        rc, out, err = run(cmd, self.cfg["print_timeout"])
        if rc is None:
            raise UserError("print command timed out")
        if rc != 0:
            raise UserError((err or out or "lp failed").strip()[:150])
        m = re.search(r"request id is (\S+)", out)
        return m.group(1) if m else None

    def cancel(self, job_ids):
        if self.cancel_bin and job_ids:
            run([self.cancel_bin] + list(job_ids), 30)


class SumatraBackend:
    name = "SumatraPDF"

    def __init__(self, cfg):
        self.cfg = cfg
        self.exe = find_sumatra(cfg)

    def ok(self):
        return bool(self.exe)

    def printers(self):
        names, default = [], None
        if win32print is not None:
            try:
                names = [p[2] for p in win32print.EnumPrinters(6, None, 1)]
                default = win32print.GetDefaultPrinter()
                return names, default
            except Exception:
                pass
        rc, out, _ = run(["powershell", "-NoProfile", "-Command",
                          "Get-Printer | Select-Object -ExpandProperty Name"], 30)
        if rc == 0:
            names = [l.strip() for l in out.splitlines() if l.strip()]
        rc, out, _ = run(["powershell", "-NoProfile", "-Command",
                          "(Get-CimInstance Win32_Printer | Where-Object Default).Name"], 30)
        if rc == 0 and out.strip():
            default = out.strip().splitlines()[0]
        return names, default

    def submit(self, pdf, printer, copies, title):
        cmd = [self.exe, "-print-to", printer] if printer else [self.exe, "-print-to-default"]
        cmd += ["-print-settings", "%dx" % copies, "-silent", pdf]
        rc, out, err = run(cmd, self.cfg["print_timeout"])
        if rc is None:
            raise UserError("SumatraPDF timed out")
        if rc != 0:
            raise UserError((err or out or "SumatraPDF exit code %s" % rc).strip()[:150])
        return None

    def cancel(self, job_ids):
        pass  # already handed to the Windows spooler; only stops further submissions


def get_backend(cfg):
    b = SumatraBackend(cfg) if IS_WIN else CupsBackend(cfg)
    return b if b.ok() else None


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
ACTIVE = ("queued", "staging", "converting", "ready")
SYSTEM_DEFAULT = "(system default)"


def make_root():
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    if TkinterDnD is not None:
        try:
            return TkinterDnD.Tk(), True
        except Exception as e:
            log.warning("tkdnd failed to load: %s", e)
    return tk.Tk(), False


def register_drop(widget, handler, types=None):
    types = types or (DND_FILES, "text/uri-list", DND_TEXT)
    try:
        widget.drop_target_register(*types)
    except Exception:
        widget.drop_target_register(DND_FILES)
    widget.dnd_bind("<<Drop>>", handler)


class PageDialog:
    def __init__(self, app, it):
        self.app, self.it = app, it
        self.total = it.pages
        self.vars, self.cells, self.imgs, self.frame_idx = {}, {}, {}, {}
        self.order = []          # display / print order of page indexes
        if it.page_order and self.total and sorted(it.page_order) == list(range(self.total)):
            self.order = list(it.page_order)
        self.q = queue.Queue()
        self.alive = True
        self.viewer = None
        self.width = max(100, min(420, int(app.cfg.get("thumb_width") or THUMB_W)))
        self.gen = 0
        self._zoom_job = self._layout_job = None
        self._cols = None
        self._drag = None
        self._hl = None
        self._skip_release = False
        t = self.top = tk.Toplevel(app.root)
        t.title("Pages - %s" % it.display)
        t.geometry("1000x720")
        t.transient(app.root)
        bar = ttk.Frame(t, padding=(6, 6, 6, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="Select all", command=lambda: self.setall(True)).pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: self.setall(False)).pack(side="left", padx=4)
        ttk.Label(bar, text="Exclude pages:").pack(side="left", padx=(12, 2))
        self.rng = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.rng, width=14)
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self.exclude_range())
        ttk.Button(bar, text="Exclude", command=self.exclude_range).pack(side="left", padx=4)
        ttk.Label(bar, text="Zoom:").pack(side="left", padx=(16, 2))
        self.zoom = tk.DoubleVar(value=self.width)
        ttk.Scale(bar, from_=100, to=420, orient="horizontal", length=150, variable=self.zoom,
                  command=self._zoom_moved).pack(side="left")
        self.zlabel = ttk.Label(bar, text="%d px" % self.width, width=7)
        self.zlabel.pack(side="left")
        bar2 = ttk.Frame(t, padding=(6, 4, 6, 0))
        bar2.pack(fill="x")
        ttk.Label(bar2, text="Order:").pack(side="left")
        ttk.Button(bar2, text="Reverse", command=self.reverse).pack(side="left", padx=4)
        ttk.Button(bar2, text="Reset order", command=self.reset_order).pack(side="left")
        self.count = ttk.Label(bar2)
        self.count.pack(side="right")
        self.info = ttk.Label(t, foreground="#a15c00", padding=(6, 0), wraplength=950)
        self.info.pack(fill="x")
        body = ttk.Frame(t)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, highlightthickness=0)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid = tk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.grid, anchor="nw")
        self.grid.bind("<Configure>", lambda ev: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda ev: self._sched_layout())
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            t.bind_all(seq, self._wheel)
        foot = ttk.Frame(t, padding=6)
        foot.pack(fill="x")
        ttk.Button(foot, text="OK", command=self.ok).pack(side="right")
        ttk.Button(foot, text="Cancel", command=self.close).pack(side="right", padx=6)
        ttk.Label(foot, text="Click = include/exclude. Drag a page (or use < >) to reorder. "
                             "Double-click or 'View' to inspect.", foreground="#666").pack(side="left")
        t.protocol("WM_DELETE_WINDOW", self.close)
        for i in range(self.total or 0):
            self.make_cell(i)
        self.refresh()
        self._start_render()
        t.after(100, self.pump)
        t.after(150, self.grab)

    def grab(self):
        try:
            self.top.grab_set()
            self.top.focus_set()
        except Exception:
            if self.alive:
                self.top.after(150, self.grab)

    def _wheel(self, e):
        d = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        self.canvas.yview_scroll(d * 2, "units")

    # -- cells and layout
    def make_cell(self, i):
        if i in self.cells:
            return
        f = tk.Frame(self.grid, bd=1, relief="solid", padx=3, pady=3, highlightthickness=0)
        lbl = tk.Label(f, text="...", width=20, height=9, cursor="hand2")
        lbl.pack()
        lbl.bind("<ButtonPress-1>", lambda ev: self._press(ev, i))
        lbl.bind("<B1-Motion>", self._motion)
        lbl.bind("<ButtonRelease-1>", lambda ev: self._release(ev, i))
        lbl.bind("<Double-ButtonPress-1>", lambda ev: self._dbl(i))
        row = tk.Frame(f)
        row.pack()
        var = tk.BooleanVar(value=(i not in self.it.excluded))
        tk.Button(row, text="<", width=1, padx=2, pady=0, command=lambda: self.shift(i, -1)).pack(side="left")
        cb = tk.Checkbutton(row, text="Page %d" % (i + 1), variable=var, command=self.refresh)
        cb.pack(side="left")
        tk.Button(row, text="View", padx=2, pady=0, command=lambda: self.inspect(i)).pack(side="left", padx=2)
        tk.Button(row, text=">", width=1, padx=2, pady=0, command=lambda: self.shift(i, 1)).pack(side="left")
        self.vars[i] = var
        self.cells[i] = (f, lbl, cb, row)
        self.frame_idx[str(f)] = i
        if i not in self.order:
            self.order.append(i)
        self.place(i)

    def cols(self):
        w = self.canvas.winfo_width()
        if w < 100:
            w = 900
        return max(1, w // (max(self.width, 190) + 34))

    def place(self, i):
        p = self.order.index(i)
        c = self._cols or self.cols()
        self.cells[i][0].grid(row=p // c, column=p % c, padx=4, pady=4)

    def _sched_layout(self):
        if self._layout_job:
            self.top.after_cancel(self._layout_job)
        self._layout_job = self.top.after(150, self.layout)

    def layout(self, force=False):
        self._layout_job = None
        if not self.alive:
            return
        c = self.cols()
        if c == self._cols and not force:
            return
        self._cols = c
        for p, i in enumerate(self.order):
            if i in self.cells:
                self.cells[i][0].grid(row=p // c, column=p % c, padx=4, pady=4)

    # -- include / exclude
    def toggle(self, i):
        self.vars[i].set(not self.vars[i].get())
        self.refresh()

    def _dbl(self, i):
        self._skip_release = True
        self._drag = None
        self.toggle(i)  # undo the toggle made by the first click of the double-click
        self.inspect(i)

    def setall(self, value):
        for v in self.vars.values():
            v.set(value)
        self.refresh()

    def exclude_range(self):
        n = self.total or len(self.cells)
        try:
            idx = parse_ranges(self.rng.get(), n)
        except ValueError as e:
            messagebox.showwarning("Pages", "Cannot understand '%s'. Use e.g. 2-4, 7, 10-" % e, parent=self.top)
            return
        for i in idx:
            if i in self.vars:
                self.vars[i].set(False)
        self.refresh()

    # -- reordering
    def shift(self, i, d):
        p = self.order.index(i)
        q = p + d
        if 0 <= q < len(self.order):
            self.order[p], self.order[q] = self.order[q], self.order[p]
            self.layout(force=True)
            self.refresh()

    def reverse(self):
        self.order.reverse()
        self.layout(force=True)
        self.refresh()

    def reset_order(self):
        self.order.sort()
        self.layout(force=True)
        self.refresh()

    def drop(self, src, tgt, x_root):
        f = self.cells[tgt][0]
        self.order.remove(src)
        pos = self.order.index(tgt)
        if x_root >= f.winfo_rootx() + f.winfo_width() / 2:
            pos += 1
        self.order.insert(pos, src)
        self.layout(force=True)
        self.refresh()

    def _cell_at(self, x, y):
        w = self.top.winfo_containing(x, y)
        while w is not None:
            i = self.frame_idx.get(str(w))
            if i is not None:
                return i
            w = getattr(w, "master", None)
        return None

    def _highlight(self, i):
        if self._hl is not None and self._hl in self.cells:
            self.cells[self._hl][0].config(highlightthickness=0)
        self._hl = i
        if i is not None:
            self.cells[i][0].config(highlightthickness=4, highlightbackground="#2a6fdb")

    def _press(self, ev, i):
        self._drag = {"i": i, "x": ev.x_root, "y": ev.y_root, "on": False}

    def _motion(self, ev):
        d = self._drag
        if not d:
            return
        if not d["on"]:
            if abs(ev.x_root - d["x"]) + abs(ev.y_root - d["y"]) < 8:
                return
            d["on"] = True
            self.top.config(cursor="fleur")
        t = self._cell_at(ev.x_root, ev.y_root)
        self._highlight(t if t != d["i"] else None)
        y = ev.y_root - self.canvas.winfo_rooty()  # auto-scroll near the edges
        if y < 30:
            self.canvas.yview_scroll(-1, "units")
        elif y > self.canvas.winfo_height() - 30:
            self.canvas.yview_scroll(1, "units")

    def _release(self, ev, i):
        if self._skip_release:
            self._skip_release = False
            return
        d, self._drag = self._drag, None
        if not d:
            return
        if d["on"]:
            self.top.config(cursor="")
            self._highlight(None)
            t = self._cell_at(ev.x_root, ev.y_root)
            if t is not None and t != d["i"]:
                self.drop(d["i"], t, ev.x_root)
        else:
            self.toggle(i)

    # -- display state
    def paint(self, i):
        f, lbl, cb, row = self.cells[i]
        bg = self.grid.cget("bg") if self.vars[i].get() else "#f4c7c3"
        if f.cget("bg") != bg:
            for w in (f, lbl, cb, row):
                w.config(bg=bg)

    def refresh(self):
        pos, n = {}, 0
        for i in self.order:
            if i in self.vars and self.vars[i].get():
                n += 1
                pos[i] = n
        reordered = self.order != sorted(self.order)
        self.count.config(text="%d of %d pages will print%s" % (n, len(self.vars), " (custom order)" if reordered else ""))
        for i, cell in self.cells.items():
            txt = "Page %d" % (i + 1)
            if reordered and i in pos:
                txt += " \u2192 #%d" % pos[i]
            if cell[2].cget("text") != txt:
                cell[2].config(text=txt)
            self.paint(i)

    def inspect(self, i):
        if not self.cells:
            return
        if self.viewer and self.viewer.alive:
            self.viewer.goto(i)
            self.viewer.top.lift()
        else:
            self.viewer = PageViewer(self, i)

    # -- zoom
    def _zoom_moved(self, _v=None):
        w = int(round(float(self.zoom.get()) / 10.0) * 10)
        self.zlabel.config(text="%d px" % w)
        if self._zoom_job:
            self.top.after_cancel(self._zoom_job)
        self._zoom_job = self.top.after(450, self._zoom_apply)

    def _zoom_apply(self):
        self._zoom_job = None
        w = int(round(float(self.zoom.get()) / 10.0) * 10)
        if not self.alive or w == self.width:
            return
        self.width = w
        self._start_render()
        self.layout(force=True)

    # -- rendering
    def _start_render(self):
        self.gen += 1
        threading.Thread(target=self._work, args=(self.width, self.gen), daemon=True).start()

    def _work(self, width, gen):
        it = self.it
        try:
            cache = it.thumbs.setdefault(width, {})
            if width in it.thumbs_done:
                for i, pth in sorted(cache.items()):
                    self.q.put(("img", gen, i, pth))
                return
            outdir = os.path.join(it.dir, "thumbs", "w%d" % width)
            aborted = []

            def emit(i, pth):
                cache[i] = pth
                if gen != self.gen or not self.alive:
                    aborted.append(1)
                    return True
                self.q.put(("img", gen, i, pth))
                return False
            n = render_pages(it.pdf, outdir, self.app.cfg, emit, width=width)
            if not aborted:
                it.thumbs_done.add(width)
            if n:
                self.q.put(("total", gen, n))
        except UserError as e:
            self.q.put(("err", gen, str(e)))
        except Exception as e:
            log.exception("preview")
            self.q.put(("err", gen, str(e)))

    def set_total(self, n):
        if self.total is None:
            self.total = n
            self.it.pages = n
        for i in range(n):
            self.make_cell(i)

    def pump(self):
        if not self.alive:
            return
        changed = False
        try:
            while True:
                m = self.q.get_nowait()
                changed = True
                if m[0] == "total":
                    self.set_total(m[2])
                elif m[1] != self.gen:
                    continue
                elif m[0] == "img":
                    self.make_cell(m[2])
                    try:
                        img = tk.PhotoImage(file=m[3])
                        self.imgs[m[2]] = img
                        self.cells[m[2]][1].config(image=img, text="", width=img.width(), height=img.height())
                    except Exception as e:
                        log.warning("thumb load failed: %s", e)
                elif m[0] == "err":
                    txt = "No thumbnails (%s). Install: %s" % (m[2], self.app.core.hint("preview"))
                    if self.total is None:
                        txt += "  Page count is also unknown (install pypdf or PyMuPDF)."
                    self.info.config(text=txt)
        except queue.Empty:
            pass
        if changed:
            self.refresh()
        self.top.after(100, self.pump)

    def ok(self):
        if not self.vars:
            self.close()
            return
        if not any(v.get() for v in self.vars.values()):
            messagebox.showwarning("Pages", "Select at least one page, or remove the file from the list.",
                                   parent=self.top)
            return
        self.it.excluded = {i for i, v in self.vars.items() if not v.get()}
        self.it.page_order = None if self.order == sorted(self.order) else list(self.order)
        self.app.update_row(self.it)
        self.close()

    def close(self):
        if self.viewer and self.viewer.alive:
            self.viewer.close()
        self.alive = False
        self.app.cfg["thumb_width"] = self.width
        save_config(self.app.cfg)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                self.top.unbind_all(seq)
            except Exception:
                pass
        try:
            self.top.grab_release()
        except Exception:
            pass
        self.top.destroy()


class PageViewer:
    """Full-size view of one page, with prev/next (in print order), zoom and an include checkbox."""

    def __init__(self, dlg, index):
        self.dlg, self.i = dlg, index
        self.alive = True
        self.gen = 0
        self.q = queue.Queue()
        self.img = None
        self._job = None
        t = self.top = tk.Toplevel(dlg.top)
        t.geometry("920x780")
        t.transient(dlg.top)
        bar = ttk.Frame(t, padding=6)
        bar.pack(fill="x")
        ttk.Button(bar, text="< Prev", command=lambda: self.step(-1)).pack(side="left")
        ttk.Button(bar, text="Next >", command=lambda: self.step(1)).pack(side="left", padx=4)
        self.lbl = ttk.Label(bar, width=16)
        self.lbl.pack(side="left", padx=6)
        self.chk = tk.Checkbutton(bar, text="Include this page", variable=dlg.vars[index], command=dlg.refresh)
        self.chk.pack(side="left", padx=6)
        ttk.Label(bar, text="Size:").pack(side="left", padx=(12, 2))
        self.size = tk.DoubleVar(value=800)
        ttk.Scale(bar, from_=300, to=1800, orient="horizontal", length=170, variable=self.size,
                  command=self._size_moved).pack(side="left")
        ttk.Button(bar, text="Fit width", command=self.fit).pack(side="left", padx=6)
        self.msg = ttk.Label(t, foreground="#a15c00", padding=(6, 0), wraplength=880)
        self.msg.pack(fill="x")
        body = ttk.Frame(t)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, highlightthickness=0, bg="#777")
        vs = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        hs = ttk.Scrollbar(body, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._wheel)
        for seq, fn in (("<Left>", lambda e: self.step(-1)), ("<Prior>", lambda e: self.step(-1)),
                        ("<Right>", lambda e: self.step(1)), ("<Next>", lambda e: self.step(1)),
                        ("<space>", lambda e: self.flip()), ("<Escape>", lambda e: self.close())):
            t.bind(seq, fn)
        t.protocol("WM_DELETE_WINDOW", self.close)
        self.goto(index)
        t.after(50, self.pump)
        t.after(150, self.grab)

    def grab(self):
        try:
            self.top.grab_set()
            self.top.focus_set()
        except Exception:
            if self.alive:
                self.top.after(150, self.grab)

    def _wheel(self, e):
        d = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        self.canvas.yview_scroll(d * 3, "units")
        return "break"

    def flip(self):
        v = self.dlg.vars[self.i]
        v.set(not v.get())
        self.dlg.refresh()

    def fit(self):
        self.size.set(max(300, self.canvas.winfo_width() - 24))
        self._render()

    def step(self, d):
        o = self.dlg.order
        if self.i in o:
            p = o.index(self.i) + d
            if 0 <= p < len(o):
                self.goto(o[p])

    def goto(self, n):
        if n not in self.dlg.vars:
            return
        self.i = n
        self.top.title("%s - page %d" % (self.dlg.it.display, n + 1))
        self.lbl.config(text="Page %d of %d" % (n + 1, len(self.dlg.vars)))
        self.chk.config(variable=self.dlg.vars[n])
        self._render()

    def _size_moved(self, _v=None):
        if self._job:
            self.top.after_cancel(self._job)
        self._job = self.top.after(400, self._render)

    def _render(self):
        self._job = None
        self.gen += 1
        gen, i, width = self.gen, self.i, int(round(float(self.size.get()) / 10.0) * 10)
        self.msg.config(text="Rendering...")
        it = self.dlg.it
        out = os.path.join(it.dir, "view", "p%d_w%d.png" % (i, width))

        def work():
            try:
                if not os.path.isfile(out):
                    render_page(it.pdf, i, out, width, self.dlg.app.cfg)
                self.q.put(("ok", gen, out))
            except UserError as e:
                self.q.put(("err", gen, str(e)))
            except Exception as e:
                log.exception("page render")
                self.q.put(("err", gen, str(e)))
        threading.Thread(target=work, daemon=True).start()

    def pump(self):
        if not self.alive:
            return
        try:
            while True:
                kind, gen, val = self.q.get_nowait()
                if gen != self.gen:
                    continue
                if kind == "ok":
                    try:
                        self.img = tk.PhotoImage(file=val)
                        self.canvas.delete("all")
                        self.canvas.create_image(0, 0, image=self.img, anchor="nw")
                        self.canvas.configure(scrollregion=(0, 0, self.img.width(), self.img.height()))
                        self.canvas.xview_moveto(0)
                        self.canvas.yview_moveto(0)
                        self.msg.config(text="")
                    except Exception as e:
                        self.msg.config(text="Could not display page: %s" % e)
                else:
                    self.msg.config(text="%s. Install: %s" % (val, self.dlg.app.core.hint("preview")))
        except queue.Empty:
            pass
        self.top.after(80, self.pump)

    def close(self):
        self.alive = False
        try:
            self.top.grab_release()
        except Exception:
            pass
        self.top.destroy()
        if self.dlg.alive:
            self.dlg.grab()


class SettingsDialog:
    """Edit config.json options from the GUI. Everything except the temp folder applies immediately."""

    def __init__(self, app):
        self.app = app
        cfg = app.cfg
        t = self.top = tk.Toplevel(app.root)
        t.title("Settings")
        t.transient(app.root)
        v = self.v = {
            "word_engine": tk.StringVar(value=cfg.get("word_engine") or "libreoffice"),
            "max_parallel": tk.IntVar(value=int(cfg.get("max_parallel") or 4)),
            "max_word": tk.IntVar(value=int(cfg.get("max_word") or 1)),
            "paper": tk.StringVar(value=cfg.get("paper") or "A4"),
            "copy_timeout": tk.IntVar(value=int(cfg.get("copy_timeout") or 600)),
            "convert_timeout": tk.IntVar(value=int(cfg.get("convert_timeout") or 180)),
            "print_timeout": tk.IntVar(value=int(cfg.get("print_timeout") or 300)),
            "merge_batch": tk.BooleanVar(value=bool(cfg.get("merge_batch", True))),
            "clear_after": tk.BooleanVar(value=bool(cfg.get("clear_after"))),
            "sumatra": tk.StringVar(value=cfg.get("sumatra") or ""),
            "soffice": tk.StringVar(value=cfg.get("soffice") or ""),
            "temp_dir": tk.StringVar(value=cfg.get("temp_dir") or ""),
        }
        body = ttk.Frame(t, padding=10)
        body.pack(fill="both", expand=True)

        # -- Word documents
        wf = ttk.LabelFrame(body, text="Word documents", padding=8)
        wf.pack(fill="x", pady=(0, 8))
        word_ok = have_word_com()
        so = find_soffice(cfg)
        ttk.Radiobutton(wf, text="LibreOffice" + ("  (found)" if so else "  (not found)"),
                        variable=v["word_engine"], value="libreoffice").grid(row=0, column=0, columnspan=3, sticky="w")
        rb = ttk.Radiobutton(wf, text="Microsoft Word" + ("  (found)" if word_ok else
                             "  (unavailable: needs Windows, Word and pywin32)"),
                             variable=v["word_engine"], value="word")
        rb.grid(row=1, column=0, columnspan=3, sticky="w")
        if not word_ok:
            rb.state(["disabled"])
            if v["word_engine"].get() == "word":
                v["word_engine"].set("libreoffice")
        ttk.Label(wf, text="Max Word instances at once:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(wf, from_=1, to=8, width=5, textvariable=v["max_word"]).grid(row=2, column=1, sticky="w",
                                                                                pady=(6, 0), padx=6)
        ttk.Label(wf, text="1 = documents convert in Word one after another (recommended)",
                  foreground="#666").grid(row=2, column=2, sticky="w", pady=(6, 0))
        ttk.Label(wf, text="Applies to Microsoft Word only; LibreOffice runs one process per document.",
                  foreground="#666").grid(row=3, column=0, columnspan=3, sticky="w")

        # -- performance
        pf = ttk.LabelFrame(body, text="Performance", padding=8)
        pf.pack(fill="x", pady=(0, 8))
        ttk.Label(pf, text="Files processed at once (threads):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(pf, from_=1, to=32, width=5, textvariable=v["max_parallel"]).grid(row=0, column=1, padx=6)
        for r, (label, key) in enumerate((("Fetch timeout (s):", "copy_timeout"),
                                          ("Convert timeout (s):", "convert_timeout"),
                                          ("Print timeout (s):", "print_timeout")), start=1):
            ttk.Label(pf, text=label).grid(row=r, column=0, sticky="w", pady=(4, 0))
            ttk.Spinbox(pf, from_=10, to=7200, increment=10, width=7, textvariable=v[key]).grid(
                row=r, column=1, padx=6, pady=(4, 0))

        # -- printing
        gf = ttk.LabelFrame(body, text="Printing", padding=8)
        gf.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(gf, text="Merge the whole queue into one print job",
                        variable=v["merge_batch"]).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(gf, text="Clear files from the list after a successful print",
                        variable=v["clear_after"]).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(gf, text="Paper size for images:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Combobox(gf, textvariable=v["paper"], values=["A4", "Letter"], state="readonly", width=8).grid(
            row=2, column=1, sticky="w", padx=6, pady=(4, 0))

        # -- paths
        hf = ttk.LabelFrame(body, text="Paths (leave empty to auto-detect)", padding=8)
        hf.pack(fill="x", pady=(0, 8))
        hf.columnconfigure(1, weight=1)
        rows = [("LibreOffice (soffice):", "soffice", "file"), ("Temp folder (restart needed):", "temp_dir", "dir")]
        if IS_WIN:
            rows.insert(0, ("SumatraPDF.exe:", "sumatra", "file"))
        for r, (label, key, kind) in enumerate(rows):
            ttk.Label(hf, text=label).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Entry(hf, textvariable=v[key], width=44).grid(row=r, column=1, sticky="ew", padx=6, pady=2)
            ttk.Button(hf, text="Browse...", command=lambda k=key, kd=kind: self.browse(k, kd)).grid(row=r, column=2)

        foot = ttk.Frame(body)
        foot.pack(fill="x")
        ttk.Button(foot, text="Reset to defaults", command=self.reset).pack(side="left")
        ttk.Button(foot, text="Save", command=self.save).pack(side="right")
        ttk.Button(foot, text="Cancel", command=t.destroy).pack(side="right", padx=6)
        ttk.Label(body, text="Engine, paper size and path changes apply to files added after saving.",
                  foreground="#666").pack(anchor="w", pady=(6, 0))
        t.bind("<Escape>", lambda e: t.destroy())
        t.after(150, self._grab)

    def _grab(self):
        try:
            self.top.grab_set()
            self.top.focus_set()
        except Exception:
            pass

    def browse(self, key, kind):
        cur = self.v[key].get()
        if kind == "dir":
            p = filedialog.askdirectory(parent=self.top, initialdir=cur or None)
        else:
            p = filedialog.askopenfilename(parent=self.top, initialdir=os.path.dirname(cur) if cur else None)
        if p:
            self.v[key].set(p)

    def reset(self):
        for k, var in self.v.items():
            d = DEFAULTS.get(k)
            var.set("" if d is None else d)

    def save(self):
        new = {}
        try:
            for k, lo, hi in (("max_parallel", 1, 32), ("max_word", 1, 8), ("copy_timeout", 10, 7200),
                              ("convert_timeout", 10, 7200), ("print_timeout", 10, 7200)):
                new[k] = max(lo, min(hi, int(self.v[k].get())))
        except (tk.TclError, ValueError):
            messagebox.showwarning("Settings", "Numeric fields must contain whole numbers.", parent=self.top)
            return
        new["word_engine"] = self.v["word_engine"].get()
        new["paper"] = self.v["paper"].get()
        new["merge_batch"] = bool(self.v["merge_batch"].get())
        new["clear_after"] = bool(self.v["clear_after"].get())
        for k in ("sumatra", "soffice", "temp_dir"):
            new[k] = self.v[k].get().strip() or None
        for k in ("sumatra", "soffice"):
            if new[k] and not os.path.isfile(new[k]):
                messagebox.showwarning("Settings", "File not found:\n%s" % new[k], parent=self.top)
                return
        self.top.destroy()
        self.app.apply_settings(new)


class App:
    def __init__(self, cfg, files):
        self.cfg = cfg
        self.caps = detect(cfg)
        self.ui_q = queue.Queue()
        self.core = Core(cfg, self.ui_q.put, self.caps)
        self.backend = get_backend(cfg)
        self.items, self.keys = {}, {}
        self.printing = False
        self.cancel = threading.Event()
        self.job_ids = []
        self._drag = None
        self.pre = self._no_pre()      # merged PDF prepared in the background
        self.pre_thread = None
        self._sig_seen, self._sig_time, self._pre_fail = None, 0.0, None

        self.root, self.dnd = make_root()
        self.root.title("DropPrint")
        self.root.geometry("820x580")
        self.root.minsize(560, 380)
        self._build()
        if self.dnd:
            register_drop(self.root, self.on_drop)
            register_drop(self.tree, self.on_drop)
        else:
            self.note("Drag & drop unavailable (%s). Use 'Add files...'. Install: %s"
                      % (DND_ERR or "tkdnd failed to load", pip_cmd("tkinterdnd2")))
        for c in self.caps:
            if not c.ok and c.level in ("core", "feature") and c.key not in ("dnd", "tk"):
                self.note("Missing: %s. Install: %s" % (c.label, c.hint))
        threading.Thread(target=self._load_printers, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)
        if files:
            self.root.after(200, lambda: self.add([os.path.abspath(f) for f in files]))

    # -- layout
    def _build(self):
        r = self.root
        top = ttk.Frame(r, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Printer:").pack(side="left")
        self.printer_var = tk.StringVar(value=SYSTEM_DEFAULT)
        self.printer_cb = ttk.Combobox(top, textvariable=self.printer_var, state="readonly",
                                       values=[SYSTEM_DEFAULT], width=40)
        self.printer_cb.pack(side="left", padx=6)
        self.printer_cb.bind("<<ComboboxSelected>>", self._printer_changed)
        ttk.Button(top, text="Refresh", command=lambda: threading.Thread(
            target=self._load_printers, daemon=True).start()).pack(side="left")
        ttk.Button(top, text="Dependencies...", command=self.show_deps).pack(side="right")
        ttk.Button(top, text="Settings...", command=self.open_settings).pack(side="right", padx=4)

        mid = ttk.Frame(r, padding=8)
        mid.pack(fill="both", expand=True)
        cols = ("name", "status", "pages", "copies")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        for c, t, w in (("name", "File (drop here)", 320), ("status", "Status", 300),
                        ("pages", "Pages", 60), ("copies", "Copies", 60)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=(c in ("name", "status")), anchor="w" if c in ("name", "status") else "center")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.tag_configure("failed", foreground="#b00020")
        self.tree.tag_configure("done", foreground="#1b7f3b")
        self.tree.bind("<ButtonPress-1>", self._press)
        self.tree.bind("<B1-Motion>", self._motion)
        self.tree.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.tree.bind("<Double-1>", self._dbl)
        r.bind("<Delete>", lambda e: self.remove_selected())
        r.bind("<Alt-Up>", lambda e: self.move(-1))
        r.bind("<Alt-Down>", lambda e: self.move(1))

        bar = ttk.Frame(r, padding=(8, 0, 8, 4))
        bar.pack(fill="x")
        self.btns = []
        for text, cmd in (("Add files...", self.add_dialog), ("Up", lambda: self.move(-1)),
                          ("Down", lambda: self.move(1)), ("Sort by name", self.sort_names),
                          ("Pages...", self.open_pages), ("Remove", self.remove_selected), ("Clear", self.clear)):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=2)
            self.btns.append(b)
        pbar = ttk.Frame(r, padding=(8, 0, 8, 4))  # own row, so they never get squeezed by the others
        pbar.pack(fill="x")
        self.b_cancel = ttk.Button(pbar, text="Cancel", command=self.cancel_print, state="disabled")
        self.b_cancel.pack(side="right", padx=2)
        self.b_print = ttk.Button(pbar, text="Print", command=self.start_print)
        self.b_print.pack(side="right", padx=2)

        opt = ttk.Frame(r, padding=(8, 0, 8, 4))
        opt.pack(fill="x")
        self.clear_var = tk.BooleanVar(value=bool(self.cfg.get("clear_after")))
        ttk.Checkbutton(opt, text="Clear files from the list after a successful print",
                        variable=self.clear_var, command=self._clear_pref).pack(side="left")
        self.prep_lbl = ttk.Label(opt, text="", foreground="#1b7f3b")
        self.prep_lbl.pack(side="right")

        lf = ttk.Frame(r, padding=(8, 0, 8, 8))
        lf.pack(fill="x")
        self.logbox = tk.Text(lf, height=7, wrap="word", state="disabled")
        self.logbox.pack(fill="x")
        ttk.Label(r, text="Tip: double-click a file to choose pages, or Copies to change them. Drag rows or Alt+Up/Down to reorder.",
                  foreground="#666").pack(anchor="w", padx=8, pady=(0, 6))

    # -- messages
    def note(self, text):
        log.info(text)
        self.logbox.config(state="normal")
        self.logbox.insert("end", "%s  %s\n" % (time.strftime("%H:%M:%S"), text))
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def show_deps(self):
        w = tk.Toplevel(self.root)
        w.title("Dependencies")
        t = tk.Text(w, width=100, height=22, font=("Courier", 10), wrap="word")
        t.insert("1.0", format_caps(self.caps))
        t.config(state="disabled")
        t.pack(fill="both", expand=True, padx=6, pady=6)

    def open_settings(self):
        if self.printing:
            self.note("Settings are locked while printing.")
            return
        SettingsDialog(self)

    def apply_settings(self, new):
        self.cfg.update(new)
        save_config(self.cfg)
        self.clear_var.set(bool(self.cfg.get("clear_after")))
        self.caps = detect(self.cfg)
        self.core.caps = {c.key: c for c in self.caps}
        self.core.apply_settings()
        self.backend = get_backend(self.cfg)
        threading.Thread(target=self._load_printers, daemon=True).start()
        self.note("Settings saved.")

    # -- printers
    def _load_printers(self):
        if not self.backend:
            return
        try:
            names, default = self.backend.printers()
        except Exception as e:
            log.warning("printer list failed: %s", e)
            names, default = [], None
        self.ui_q.put(("printers", names, default))

    def selected_printer(self):
        v = self.printer_var.get()
        return None if v == SYSTEM_DEFAULT else v.split("  [default]")[0]

    def _printer_changed(self, _e=None):
        self.cfg["printer"] = self.selected_printer()
        save_config(self.cfg)

    # -- adding / removing
    def add_dialog(self):
        if self.printing:
            return
        types = [("Printable files", "*.pdf *.doc *.docx *.odt *.rtf *.png *.jpg *.jpeg *.gif *.bmp *.tif *.tiff *.webp"),
                 ("All files", "*.*")]
        paths = filedialog.askopenfilenames(parent=self.root, filetypes=types)
        if paths:
            self.add(list(paths))

    def on_drop(self, event):
        raws = parse_drop(self.root.tk, event.data)
        if not raws:
            self.note("Drop contained no usable data. Run with --probe to see what your file manager sends.")
            return "copy"
        index = None
        try:
            y = event.y_root - self.tree.winfo_rooty()
            row = self.tree.identify_row(y)
            if row:
                index = self.tree.index(row)
                bb = self.tree.bbox(row)
                if bb and y > bb[1] + bb[3] // 2:
                    index += 1
        except Exception:
            index = None
        self.add(raws, index)
        return "copy"  # never let the source file manager treat this as a move

    def add(self, raws, index=None):
        if self.printing:
            self.note("Printing in progress - drop ignored.")
            return
        added = 0
        for raw in raws:
            src = classify(raw)
            if not src:
                self.note("Ignored (not a file path or WebDAV URL): %s" % raw[:100])
                continue
            if src.key in self.keys:
                self.note("%s: already in queue" % src.display)
                continue
            it = Item(src)
            self.items[it.id] = it
            self.keys[src.key] = it
            pos = "end" if index is None else index + added
            self.tree.insert("", pos, iid=it.id, values=self._vals(it))
            self.core.q.put(it)
            added += 1

    def _vals(self, it):
        return (it.display, it.text(), it.pages_text(), it.copies)

    def update_row(self, it):
        if it.id not in self.items:
            return
        tags = ("failed",) if it.status == "failed" else (("done",) if it.status == "done" else ())
        self.tree.item(it.id, values=self._vals(it), tags=tags)
        if it.status == "failed" and not it.noted:
            it.noted = True
            self.note("%s: %s" % (it.display, it.msg))

    def drop_row(self, it):
        if it.id in self.items:
            self.tree.delete(it.id)
            del self.items[it.id]
        if self.keys.get(it.src.key) is it:
            del self.keys[it.src.key]
        self.core.forget(it)

    def remove_selected(self):
        if self.printing:
            return
        for iid in self.tree.selection():
            self.drop_row(self.items[iid])

    def clear(self):
        if self.printing:
            return
        for it in list(self.items.values()):
            self.drop_row(it)

    # -- ordering
    def move(self, delta):
        if self.printing:
            return
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0]) + delta
        n = len(self.tree.get_children())
        if 0 <= idx < n:
            self.tree.move(sel[0], "", idx)

    def sort_names(self):
        if self.printing:
            return
        ids = sorted(self.tree.get_children(), key=lambda i: self.items[i].display.lower())
        for n, i in enumerate(ids):
            self.tree.move(i, "", n)

    def _press(self, e):
        self._drag = self.tree.identify_row(e.y) or None

    def _motion(self, e):
        if not self._drag or self.printing:
            return
        target = self.tree.identify_row(e.y)
        if target and target != self._drag:
            self.tree.move(self._drag, "", self.tree.index(target))
            return "break"

    def _dbl(self, e):
        if self.printing:
            return
        iid = self.tree.identify_row(e.y)
        if iid and self.tree.identify_column(e.x) != "#4":
            self.open_pages(iid)
            return
        if iid and self.tree.identify_column(e.x) == "#4":
            it = self.items[iid]
            n = simpledialog.askinteger("Copies", "Copies of %s:" % it.display, initialvalue=it.copies,
                                        minvalue=1, maxvalue=99, parent=self.root)
            if n:
                it.copies = n
                self.update_row(it)

    def _clear_pref(self):
        self.cfg["clear_after"] = bool(self.clear_var.get())
        save_config(self.cfg)

    def open_pages(self, iid=None):
        if self.printing:
            return
        iid = iid if isinstance(iid, str) else (self.tree.selection() or [None])[0]
        if not iid or iid not in self.items:
            self.note("Select a file first.")
            return
        it = self.items[iid]
        if it.status != "ready" or not it.pdf:
            self.note("%s is not ready yet (status: %s)." % (it.display, it.status))
            return
        PageDialog(self, it)

    # -- printing
    def start_print(self):
        if self.printing:
            return
        if not self.backend:
            c = next((c for c in self.caps if c.key == "print"), None)
            self.note("No print backend found. Install: %s" % (c.hint if c else "CUPS / SumatraPDF"))
            return
        todo = [self.items[i] for i in self.tree.get_children() if self.items[i].status in ACTIVE]
        if not todo:
            return
        self.printing = True
        self.cancel.clear()
        self.job_ids = []
        self.refresh_buttons()
        threading.Thread(target=self._print_worker, args=(todo, self.selected_printer()), daemon=True).start()

    def _print_worker(self, todo, printer):
        if self.cfg.get("merge_batch", True):
            self._print_merged(todo, printer)
        else:
            self._print_each(todo, printer)

    @staticmethod
    def _no_pre():
        return {"sig": None, "dir": None, "path": None, "engine": None}

    def ready_snapshot(self):
        return [self.items[i] for i in self.tree.get_children() if self.items[i].status == "ready"]

    def _maybe_prebuild(self):
        """Build the merged PDF in the background once the queue has been stable for a moment."""
        if self.printing or not self.backend or not self.cfg.get("merge_batch", True):
            return
        if self.pre_thread is not None and self.pre_thread.is_alive():
            return
        items = self.ready_snapshot()
        sig = merge_sig(items) if items else None
        now = time.time()
        if sig != self._sig_seen:
            self._sig_seen, self._sig_time = sig, now
            return
        pending = any(i.status in ("queued", "staging", "converting") for i in self.items.values())
        if sig is None or pending or sig == self.pre["sig"] or sig == self._pre_fail or now - self._sig_time < 1.0:
            return
        self.pre_thread = threading.Thread(target=self._prebuild, args=(sig, items), daemon=True)
        self.pre_thread.start()

    def _prebuild(self, sig, items):
        mdir = os.path.join(self.core.root, "pre-%d" % int(time.time() * 1000))
        try:
            path, engine = self.core.build_merged(items, mdir)
        except Exception as e:  # includes UserError; the print step will report it properly
            log.warning("background prepare failed: %s", e)
            self._pre_fail = sig
            shutil.rmtree(mdir, ignore_errors=True)
            return
        old = self.pre.get("dir")
        self.pre = {"sig": sig, "dir": mdir, "path": path, "engine": engine}
        if old:
            shutil.rmtree(old, ignore_errors=True)
        log.info("prepared merged PDF in background (%s)", engine)

    def _update_prep_label(self):
        if self.pre_thread is not None and self.pre_thread.is_alive():
            txt = "Preparing print job..."
        elif not self.printing and self._sig_seen is not None and self._sig_seen == self.pre["sig"]:
            txt = "Print job ready"
        else:
            txt = ""
        if self.prep_lbl.cget("text") != txt:
            self.prep_lbl.config(text=txt)

    def _print_merged(self, todo, printer):
        for it in todo:  # wait for everything to finish staging/converting
            while it.status in ("queued", "staging", "converting") and not self.cancel.is_set():
                time.sleep(0.05)
            if self.cancel.is_set():
                self.ui_q.put(("pdone", 0, [], True))
                return
        ready = [it for it in todo if it.status == "ready"]
        failed = [it.display for it in todo if it.status == "failed"]
        if not ready:
            self.ui_q.put(("pdone", 0, failed, False))
            return
        sig = merge_sig(ready)
        for it in ready:
            self.core.set(it, "printing", "preparing")
        t = self.pre_thread
        if t is not None and t.is_alive():
            t.join()  # a background build is nearly done; reuse it if it matches
        printed = 0
        mdir = None
        try:
            pre = self.pre
            if pre.get("sig") == sig and pre.get("path") and os.path.isfile(pre["path"]):
                out, mdir, engine = pre["path"], pre["dir"], pre["engine"] + ", prepared in advance"
                self.pre = self._no_pre()
            else:
                mdir = os.path.join(self.core.root, "merge-%d" % int(time.time() * 1000))
                out, engine = self.core.build_merged(ready, mdir)
            log.info("merged %d file(s) with %s", len(ready), engine)
            if self.cancel.is_set():
                for it in ready:
                    self.core.set(it, "ready")
                self.ui_q.put(("pdone", 0, failed, True))
                return
            for it in ready:
                self.core.set(it, "printing", "submitting")
            jid = self.backend.submit(out, printer, 1, "DropPrint batch (%d files)" % len(ready))
            if jid:
                self.job_ids.append(jid)
            for it in ready:
                self.core.set(it, "done", "sent in merged job%s" % ((" " + jid) if jid else ""))
                printed += 1
        except UserError as e:
            for it in ready:
                self.core.fail(it, str(e))
                failed.append(it.display)
        except Exception as e:
            log.exception("merged print failed")
            for it in ready:
                self.core.fail(it, "print error: %s" % e)
                failed.append(it.display)
        finally:
            if mdir:
                shutil.rmtree(mdir, ignore_errors=True)
        self.ui_q.put(("pdone", printed, failed, self.cancel.is_set()))

    def _print_each(self, todo, printer):
        printed, failed = 0, []
        for it in todo:
            while it.status in ("queued", "staging", "converting") and not self.cancel.is_set():
                time.sleep(0.2)
            if self.cancel.is_set():
                break
            if it.status != "ready":
                if it.status == "failed":
                    failed.append(it.display)
                continue
            self.core.set(it, "printing")
            try:
                jid = self.backend.submit(self.core.effective_pdf(it), printer, it.copies, it.display)
                if jid:
                    self.job_ids.append(jid)
                self.core.set(it, "done")
                printed += 1
            except UserError as e:
                self.core.fail(it, str(e))
                failed.append(it.display)
            except Exception as e:
                log.exception("print failed")
                self.core.fail(it, "print error: %s" % e)
                failed.append(it.display)
        self.ui_q.put(("pdone", printed, failed, self.cancel.is_set()))

    def cancel_print(self):
        self.cancel.set()
        ids = list(self.job_ids)
        if self.backend and ids:
            threading.Thread(target=self.backend.cancel, args=(ids,), daemon=True).start()
        self.note("Cancelling...")

    def refresh_buttons(self):
        busy = self.printing
        st = "disabled" if busy else "normal"
        for b in self.btns:
            b.config(state=st)
        self.b_print.config(state="disabled" if busy or not any(
            i.status in ACTIVE for i in self.items.values()) else "normal")
        self.b_cancel.config(state="normal" if busy else "disabled")

    # -- event pump
    def poll(self):
        try:
            while True:
                m = self.ui_q.get_nowait()
                k = m[0]
                if k == "upd":
                    self.update_row(m[1])
                elif k == "dup":
                    it = m[1]
                    self.drop_row(it)
                    self.note("%s: already in queue (same content as %s)" % (it.display, it.msg))
                elif k == "printers":
                    names, default = m[1], m[2]
                    vals = [SYSTEM_DEFAULT] + names
                    self.printer_cb.config(values=vals)
                    want = self.cfg.get("printer")
                    self.printer_var.set(want if want in names else SYSTEM_DEFAULT)
                    if not names:
                        self.note("No printers found.")
                    elif default and self.printer_var.get() == SYSTEM_DEFAULT:
                        self.note("System default printer: %s" % default)
                elif k == "pdone":
                    _, printed, failed, cancelled = m
                    self.printing = False
                    txt = "Printed %d file(s)." % printed
                    if failed:
                        txt += " Skipped/failed: %s." % ", ".join(failed)
                    if cancelled:
                        txt = "Cancelled. " + txt
                    if self.clear_var.get() and printed:
                        done = [i for i in self.items.values() if i.status == "done"]
                        for i in done:
                            self.drop_row(i)
                        txt += " Cleared %d from the list." % len(done)
                    self.note(txt)
        except queue.Empty:
            pass
        self._maybe_prebuild()
        self._update_prep_label()
        self.refresh_buttons()
        self.root.after(100, self.poll)

    def close(self):
        self.cancel.set()
        self.root.destroy()
        self.core.cleanup()

    def run(self):
        self.root.mainloop()


# ----------------------------------------------------------------------------
# Probe mode: shows exactly what your file manager sends
# ----------------------------------------------------------------------------
def run_probe():
    root, dnd = make_root()
    root.title("DropPrint probe")
    root.geometry("760x560")
    if not dnd:
        print("tkinterdnd2 unavailable: %s" % DND_ERR, file=sys.stderr)
    box = tk.Text(root, wrap="word", font=("Courier", 9))

    def emit(text):
        log.info("PROBE %s", text)
        print(text, flush=True)
        box.insert("end", text + "\n")
        box.see("end")

    frame = tk.Frame(root)
    frame.pack(fill="x")

    def handler(e, label):
        emit("---- DROP on zone [%s] at %s" % (label, time.strftime("%H:%M:%S")))
        for a in ("action", "actions", "type", "types", "modifiers"):
            emit("  %s = %r" % (a, getattr(e, a, None)))
        emit("  data(raw) = %r" % (e.data,))
        parts = parse_drop(root.tk, e.data)
        for p in parts:
            s = classify(p)
            emit("  -> %r  => %s" % (p, ("%s key=%s" % (s.kind, s.key)) if s else "NOT RECOGNISED"))
        return "copy"

    def enter(e, label):
        emit("enter [%s] offered types: %r" % (label, getattr(e, "types", None)))
        return getattr(e, "action", "copy")

    if dnd:
        for label, types in (("DND_Files", (DND_FILES,)), ("text/uri-list", ("text/uri-list",)),
                             ("DND_Text", (DND_TEXT,))):
            z = tk.Label(frame, text="Drop here:\n" + label, relief="groove", height=5)
            z.pack(side="left", fill="x", expand=True, padx=4, pady=4)
            z.drop_target_register(*types)
            z.dnd_bind("<<Drop>>", lambda e, l=label: handler(e, l))
            z.dnd_bind("<<DropEnter>>", lambda e, l=label: enter(e, l))
    box.pack(fill="both", expand=True)
    emit("Drag files from Dolphin/Nautilus/Explorer onto each zone in turn. Output also goes to the log file.")
    root.mainloop()


# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Drag & drop print queue")
    ap.add_argument("--check", action="store_true", help="show dependency table and exit")
    ap.add_argument("--probe", action="store_true", help="log raw drag&drop data")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("files", nargs="*")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config()

    if args.check:
        caps = detect(cfg)
        print(format_caps(caps))
        return 0 if all(c.ok for c in caps if c.level == "core") else 1
    if TK_ERR is not None:
        caps = detect(cfg)
        sys.stderr.write("Tk is not available: %s\nInstall: %s\n" % (TK_ERR, caps[0].hint))
        return 2
    if args.probe:
        run_probe()
        return 0
    App(cfg, args.files).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
