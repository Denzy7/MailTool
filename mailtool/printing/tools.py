"""Locating external helper programs (LibreOffice, SumatraPDF, Ghostscript, ...)."""
from __future__ import annotations

import glob
import os

from mailtool.core.util import APP_DIR, IS_WIN, PKG_DIR, find_tool

win32print = None
if IS_WIN:
    try:
        import win32print  # type: ignore
    except Exception:
        win32print = None


def find_soffice(pcfg):
    extra = [pcfg.get("soffice")]
    if IS_WIN:
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            b = os.environ.get(env)
            if b:
                extra.append(os.path.join(b, "LibreOffice", "program", "soffice.exe"))
    else:
        extra += ["/usr/lib/libreoffice/program/soffice", "/usr/lib64/libreoffice/program/soffice"]
        extra += glob.glob("/opt/libreoffice*/program/soffice")
    return find_tool(["soffice", "libreoffice"], extra)


def find_sumatra(pcfg):
    extra = [pcfg.get("sumatra")]
    for d in dict.fromkeys([APP_DIR, os.path.dirname(PKG_DIR)]):
        extra.append(os.path.join(d, "SumatraPDF.exe"))
        extra += glob.glob(os.path.join(d, "SumatraPDF*.exe"))
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        b = os.environ.get(env)
        if b:
            extra.append(os.path.join(b, "SumatraPDF", "SumatraPDF.exe"))
    return find_tool(["SumatraPDF", "SumatraPDF.exe"], extra)


def find_gs():
    extra = []
    if IS_WIN:
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            b = os.environ.get(env)
            if b:
                extra += sorted(glob.glob(os.path.join(b, "gs", "gs*", "bin", "gswin*c.exe")), reverse=True)
    return find_tool(["gswin64c", "gswin32c", "gs"] if IS_WIN else ["gs"], extra)


def find_kio():
    return find_tool(["kioclient6", "kioclient5", "kioclient"])


def find_gio():
    return find_tool(["gio"])


def find_antiword():
    return find_tool(["antiword"])


def find_catdoc():
    return find_tool(["catdoc"])


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


_FITZ = False


def get_fitz():
    """PyMuPDF if installed (fast page thumbnails)."""
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
