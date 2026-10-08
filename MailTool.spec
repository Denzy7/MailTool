# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for MailTool (Windows and Linux).

    pyinstaller --noconfirm MailTool.spec

Output: dist/MailTool/  (a folder - zip it to distribute). One-folder starts much
faster than one-file and trips fewer antivirus heuristics.

Options (environment variables):
    MAILTOOL_PLAYWRIGHT=0   leave Playwright out (~120 MB smaller; EMAILINFO bodies then use the
                            plain-text layout). Default: bundled if installed.
    MAILTOOL_CONSOLE=1      build with a console window (handy for debugging).

Chromium itself is never bundled: on each machine run once
    MailTool --install-browser        (or: python -m playwright install chromium)
SumatraPDF.exe (Windows): put it next to this spec file and it is copied in.
"""
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = os.path.abspath(SPECPATH)  # noqa: F821  (provided by PyInstaller)
IS_WIN = sys.platform.startswith("win")


def have(mod):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


datas = [(os.path.join(HERE, "mailtool", "assets"), os.path.join("mailtool", "assets")),
         (os.path.join(HERE, "mailtool", "web", "static"), os.path.join("mailtool", "web", "static"))]
binaries = []
hidden = []

# drag & drop: tkdnd's native library lives inside the tkinterdnd2 package
if have("tkinterdnd2"):
    datas += collect_data_files("tkinterdnd2")
    hidden += ["tkinterdnd2"]

# keyring picks its backend at runtime by entry point - include them all
if have("keyring"):
    hidden += collect_submodules("keyring.backends")
    if IS_WIN:
        hidden += ["win32timezone", "win32ctypes.core"]

# Windows has no system timezone database
if have("tzdata"):
    datas += collect_data_files("tzdata")
    hidden += collect_submodules("tzdata")

if have("reportlab"):
    datas += collect_data_files("reportlab")
    hidden += collect_submodules("reportlab.pdfbase") + ["reportlab.graphics.barcode"]

for mod in ("docx", "docx2txt", "olefile", "dateutil", "pypdf", "pymupdf", "fitz"):
    if have(mod):
        hidden.append(mod)
if have("docx"):
    datas += collect_data_files("docx")          # default document template

if os.environ.get("MAILTOOL_PLAYWRIGHT", "1") != "0" and have("playwright"):
    datas += collect_data_files("playwright")    # the Node driver
    hidden += collect_submodules("playwright")

if IS_WIN:
    for name in os.listdir(HERE):
        if name.lower().startswith("sumatrapdf") and name.lower().endswith(".exe"):
            binaries.append((os.path.join(HERE, name), "."))
    hidden += ["win32print", "win32com.client", "pythoncom", "pywintypes"]

a = Analysis(  # noqa: F821
    [os.path.join(HERE, "run_mailtool.py")],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["matplotlib", "numpy.tests", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
icon = os.path.join(HERE, "mailtool", "assets", "mailtool.ico" if IS_WIN else "icon_256.png")
exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="MailTool",
    console=os.environ.get("MAILTOOL_CONSOLE") == "1",
    icon=icon if IS_WIN else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="MailTool", upx=False)  # noqa: F821
