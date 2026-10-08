"""Small platform helpers shared by every part of MailTool."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

from mailtool import APP_NAME

IS_WIN = os.name == "nt"
FROZEN = bool(getattr(sys, "frozen", False))      # running as a PyInstaller build
PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# bundled data (assets) lives in sys._MEIPASS when frozen
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(PKG_DIR)) if FROZEN else os.path.dirname(PKG_DIR)
# where to look for helper tools shipped next to the exe (e.g. SumatraPDF.exe)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.path.dirname(PKG_DIR)

log = logging.getLogger("mailtool")


def asset(name):
    for base in (os.path.join(PKG_DIR, "assets"), os.path.join(RES_DIR, "mailtool", "assets")):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    return os.path.join(PKG_DIR, "assets", name)


def _ensure(d):
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        d = os.path.join(tempfile.gettempdir(), APP_NAME)
        os.makedirs(d, exist_ok=True)
        return d


def config_dir():
    """Per-user settings folder: %APPDATA%\\MailTool or ~/.config/MailTool."""
    if IS_WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return _ensure(os.path.join(base, APP_NAME))


def data_dir():
    """Per-user data folder (lookup cache, logs): %LOCALAPPDATA%\\MailTool or ~/.local/share/MailTool."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return _ensure(os.path.join(base, APP_NAME))


def logs_dir():
    return _ensure(os.path.join(data_dir(), "logs"))


def run(cmd, timeout, cwd=None):
    """Run a command without ever hanging the caller.
    Returns (rc, out, err); rc None = timeout, -1 = not runnable."""
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
    """Windows: allow paths over 260 chars (UNC/WebDAV shares get long fast)."""
    if not IS_WIN or p.startswith("\\\\?\\") or len(p) < 240:
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def sanitize_for_path(text, max_len=80):
    """Make a string safe to use as a file/folder name on every OS."""
    text = (text or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "unknown"
    return text[:max_len]


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


def touch_path(path, dt):
    """Set mtime/atime of a file or folder to a datetime (e.g. when the email arrived)."""
    try:
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
    except (OSError, OverflowError, ValueError, AttributeError):
        pass


def open_in_file_manager(path):
    try:
        if IS_WIN:
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log.warning("open %s failed: %s", path, e)
        return False


def human_size(num_bytes):
    if num_bytes is None:
        return "unknown size"
    if num_bytes < 1024:
        return f"{num_bytes} bytes"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} kB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"
