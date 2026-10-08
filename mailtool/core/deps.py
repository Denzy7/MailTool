"""What is installed, what it enables, and how to install what's missing.

Every optional library or tool is a capability; nothing missing crashes the app,
it only greys out a feature and shows the install command."""
from __future__ import annotations

import glob
import importlib.util
import os
import sys

from mailtool.core.util import FROZEN, IS_WIN, find_tool
from mailtool.printing import tools

LEVELS = ("core", "feature", "optional")


class Cap:
    def __init__(self, key, area, label, ok, detail="", hint="", level="feature"):
        self.key, self.area, self.label, self.ok = key, area, label, ok
        self.detail, self.hint, self.level = detail, hint, level


def _has(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


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
        return "pip install %s   (then rebuild MailTool so it gets bundled)" % pkg
    return '"%s" -m pip install %s' % (sys.executable, pkg)


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


def chromium_installed():
    """Cheap check for a Playwright Chromium download (no browser is started)."""
    roots = []
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        roots.append(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    if IS_WIN:
        roots.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"))
    elif sys.platform == "darwin":
        roots.append(os.path.expanduser("~/Library/Caches/ms-playwright"))
    else:
        roots.append(os.path.expanduser("~/.cache/ms-playwright"))
    for r in roots:
        if r and glob.glob(os.path.join(r, "chromium*")):
            return True
    return False


def detect(pcfg):
    """pcfg = the print settings section (for user-set tool paths)."""
    caps = []

    def add(*a, **k):
        caps.append(Cap(*a, **k))

    # ---- core
    add("tk", "Core", "Tk GUI", _has("tkinter"), "",
        pkg_hint(apt="python3-tk", dnf="python3-tkinter", zypper="python3-tk", pacman="tk",
                 win="re-run the python.org installer and enable 'tcl/tk and IDLE'"), "core")
    add("tzdata", "Core", "Timezone database", _tz_ok(), "",
        pip_cmd("tzdata"), "core" if IS_WIN else "optional")
    add("keyring", "Core", "Remember password in OS keyring (keyring)", _has("keyring"), "",
        pip_cmd("keyring"), "optional")
    add("dnd", "Core", "Drag & drop (tkinterdnd2)", _has("tkinterdnd2"), "", pip_cmd("tkinterdnd2"), "feature")
    add("pypdf", "Core", "PDF read/merge (pypdf)", _has("pypdf"), "",
        pkg_hint(apt="python3-pypdf", dnf="python3-pypdf", zypper="python3-pypdf",
                 pacman="python-pypdf", pip="pypdf"), "feature")

    # ---- fetch
    add("reportlab", "Fetch", "Email info PDFs (reportlab)", _has("reportlab"), "", pip_cmd("reportlab"), "feature")
    pw = _has("playwright")
    add("playwright", "Fetch", "Printed/image email bodies (playwright)", pw, "",
        pip_cmd("playwright") + "  then: playwright install chromium", "optional")
    if pw:
        ch = chromium_installed()
        add("chromium", "Fetch", "Headless Chromium for email bodies", ch, "",
            '"%s" -m playwright install chromium' % sys.executable if not FROZEN else "playwright install chromium",
            "optional")

    # ---- sort
    add("docx", "Sort", "Read .docx attachments (python-docx / docx2txt)", _has("docx") or _has("docx2txt"), "",
        pip_cmd("python-docx"), "optional")
    doc_ok = bool(tools.find_antiword() or tools.find_catdoc())
    add("doc", "Sort", "Read legacy .doc well (antiword/catdoc)", doc_ok, "",
        pkg_hint(apt="antiword", dnf="antiword", zypper="antiword", pacman="antiword",
                 win="optional - olefile fallback is used: " + pip_cmd("olefile")), "optional")
    add("dateutil", "Sort", "Flexible CSV dates (python-dateutil)", _has("dateutil"), "",
        pip_cmd("python-dateutil"), "optional")

    # ---- print
    add("pil", "Print", "Images -> PDF (Pillow)", _has("PIL"), "",
        pkg_hint(apt="python3-pil", dnf="python3-pillow", zypper="python3-Pillow",
                 pacman="python-pillow", pip="pillow"), "feature")
    if IS_WIN:
        sp = tools.find_sumatra(pcfg)
        add("print", "Print", "Printing (SumatraPDF)", bool(sp), sp or "", "winget install SumatraPDF.SumatraPDF", "core")
        add("pywin32", "Print", "Printer list / Word (pywin32)", tools.win32print is not None, "",
            pip_cmd("pywin32"), "optional")
        add("word", "Print", "Word engine: Microsoft Word", tools.have_word_com(), "",
            "install Microsoft Word + pywin32", "optional")
    else:
        lp, ls = find_tool(["lp"]), find_tool(["lpstat"])
        add("print", "Print", "Printing (CUPS lp/lpstat)", bool(lp and ls), lp or "",
            pkg_hint(apt="cups-client", dnf="cups-client", zypper="cups-client", pacman="cups"), "core")
        g = tools.find_gio()
        add("gio", "Print", "dav:// and davs:// drops (gio)", bool(g), g or "",
            pkg_hint(apt="libglib2.0-bin", dnf="glib2", zypper="glib2-tools", pacman="glib2"), "optional")
        k = tools.find_kio()
        add("kio", "Print", "webdav:// and webdavs:// drops (kioclient)", bool(k), k or "",
            pkg_hint(apt="kde-cli-tools kio-extras", dnf="kde-cli-tools kio-extras",
                     zypper="kde-cli-tools kio-extras", pacman="kde-cli-tools kio-extras"), "optional")
    gs = tools.find_gs()
    add("gs", "Print", "Robust PDF merging (Ghostscript)", bool(gs), gs or "",
        pkg_hint(apt="ghostscript", dnf="ghostscript", zypper="ghostscript", pacman="ghostscript",
                 win="winget install ArtifexSoftware.GhostScript"), "optional")
    pe = tools.preview_engine()
    add("preview", "Print", "Page previews (PyMuPDF / pdftoppm / Ghostscript)", bool(pe), pe or "",
        pkg_hint(apt="poppler-utils", dnf="poppler-utils", zypper="poppler-tools", pacman="poppler",
                 pip="pymupdf"), "optional")
    so = tools.find_soffice(pcfg)
    add("soffice", "Print", "Word -> PDF (LibreOffice)", bool(so), so or "",
        pkg_hint(apt="libreoffice-writer", dnf="libreoffice-writer", zypper="libreoffice-writer",
                 pacman="libreoffice-fresh", win="winget install TheDocumentFoundation.LibreOffice"))
    return caps


def _tz_ok():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Africa/Nairobi")
        return True
    except Exception:
        return False


def format_caps(caps):
    lines, area = [], None
    for c in caps:
        if c.area != area:
            area = c.area
            lines.append("\n%s" % area.upper())
        mark = "[ OK ]" if c.ok else ("[ -- ]" if c.level == "optional" else "[MISS]")
        lines.append("  %s %s%s" % (mark, c.label, ("  (%s)" % c.detail) if c.ok and c.detail else ""))
        if not c.ok and c.hint:
            lines.append("           install: %s" % c.hint)
    if not IS_WIN:
        lines.append("\nNote: if pip says 'externally-managed-environment', use a venv, the distro "
                     "package, or add --break-system-packages.")
    return "\n".join(lines).lstrip("\n")


def by_key(caps):
    return {c.key: c for c in caps}


def install_chromium(job):
    """Download Playwright's headless Chromium (works from a frozen build too).
    Streams the installer's output into the job log."""
    import subprocess
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    exe = compute_driver_executable()
    cmd = list(exe) if isinstance(exe, (tuple, list)) else [str(exe)]
    cmd += ["install", "chromium"]
    kw = {"creationflags": 0x08000000} if IS_WIN else {}
    job.log("Downloading Chromium for email rendering (about 150 MB) ...")
    p = subprocess.Popen(cmd, env=get_driver_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, **kw)
    last = ""
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").strip()
        if line and not line.startswith("|") and line != last:   # skip progress-bar redraws
            job.log("  " + line[:200])
            last = line
        if job.cancelled:
            p.kill()
            break
    rc = p.wait()
    if rc != 0 and not job.cancelled:
        raise RuntimeError("the Chromium download failed (exit code %s)" % rc)
    job.log("Chromium is installed.", "ok")
    return True
