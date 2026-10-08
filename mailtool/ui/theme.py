"""MailTool's look: a flat ttk theme (built on 'clam', so nothing extra to bundle)
in deep teal, light and dark, following the OS setting by default."""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from mailtool.core.util import IS_WIN

LIGHT = {
    "bg": "#F3F6F6", "surface": "#FFFFFF", "surface2": "#EEF3F2", "border": "#D5DEDD",
    "text": "#17292A", "muted": "#5D6E6E", "faint": "#8A9A99",
    "accent": "#0F766E", "accent_hover": "#115E59", "accent_text": "#FFFFFF", "accent_soft": "#D3EEEA",
    "select": "#C9E9E4", "select_text": "#0B3B37",
    "danger": "#B42318", "ok": "#1B7F3B", "warn": "#A15C00",
    "field": "#FFFFFF", "field_border": "#C3CFCD", "disabled": "#A9B6B5",
    "sidebar": "#0D3B3C", "sidebar_hover": "#145052", "sidebar_active": "#0F766E",
    "sidebar_text": "#9FC5C1", "sidebar_text_on": "#FFFFFF",
    "row_alt": "#F7FAFA", "excluded": "#F4C7C3",
}
DARK = {
    "bg": "#111819", "surface": "#182223", "surface2": "#1E2A2B", "border": "#2B3A3B",
    "text": "#E2ECEB", "muted": "#97A9A8", "faint": "#6E8180",
    "accent": "#2DB3A6", "accent_hover": "#5CCFC3", "accent_text": "#062523", "accent_soft": "#163E3B",
    "select": "#1F5450", "select_text": "#E8FFFC",
    "danger": "#F2786D", "ok": "#5DD39E", "warn": "#F0B35A",
    "field": "#121B1C", "field_border": "#334546", "disabled": "#556868",
    "sidebar": "#0A2122", "sidebar_hover": "#113233", "sidebar_active": "#11615B",
    "sidebar_text": "#8DB5B1", "sidebar_text_on": "#FFFFFF",
    "row_alt": "#1B2627", "excluded": "#5B2B27",
}

P = dict(LIGHT)       # the active palette; views read colours from here
_FONTS = {}           # keep named fonts alive (Tk deletes them when the Python object dies)


def system_prefers_dark():
    try:
        if IS_WIN:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return v == 0
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True,
                                 timeout=2)
            return "dark" in out.stdout.lower()
        # GNOME / anything with the freedesktop colour-scheme setting
        try:
            out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                                 capture_output=True, text=True, timeout=2)
            if "dark" in out.stdout.lower():
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        # KDE Plasma: colour scheme name in kdeglobals
        kg = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "kdeglobals")
        if os.path.isfile(kg):
            with open(kg, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("ColorScheme=") and "dark" in line.lower():
                        return True
        return "dark" in (os.environ.get("GTK_THEME") or "").lower()
    except Exception:
        return False


def fonts(root):
    base = tkfont.nametofont("TkDefaultFont")
    fam = base.actual("family")
    size = abs(base.actual("size")) or 10
    if IS_WIN:
        fam, size = "Segoe UI", 10
    elif fam in ("fixed", "TkDefaultFont"):
        fam = "DejaVu Sans"
    f = {}
    for name, sz, wt in (("MT.Body", size, "normal"), ("MT.Bold", size, "bold"), ("MT.Small", size - 1, "normal"),
                         ("MT.H1", size + 9, "bold"), ("MT.H2", size + 2, "bold"), ("MT.Brand", size + 7, "bold"),
                         ("MT.Nav", size + 1, "normal")):
        try:
            f[name] = tkfont.Font(root=root, name=name, family=fam, size=sz, weight=wt, exists=False)
        except tk.TclError:
            f[name] = tkfont.Font(root=root, name=name, exists=True)
            f[name].configure(family=fam, size=sz, weight=wt)
    mono = tkfont.nametofont("TkFixedFont").actual("family")
    try:
        f["MT.Mono"] = tkfont.Font(root=root, name="MT.Mono", family="Consolas" if IS_WIN else mono, size=size - 1)
    except tk.TclError:
        f["MT.Mono"] = tkfont.Font(root=root, name="MT.Mono", exists=True)
    _FONTS.update(f)
    for n in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(n).configure(family=fam, size=size)
        except tk.TclError:
            pass
    return f


def apply(root, mode="system"):
    dark = system_prefers_dark() if mode == "system" else mode == "dark"
    P.clear()
    P.update(DARK if dark else LIGHT)
    P["is_dark"] = dark
    fonts(root)
    s = ttk.Style(root)
    s.theme_use("clam")
    c = P
    root.configure(bg=c["bg"])

    s.configure(".", background=c["bg"], foreground=c["text"], fieldbackground=c["field"],
                bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"], troughcolor=c["surface2"],
                focuscolor=c["accent"], selectbackground=c["select"], selectforeground=c["select_text"],
                insertcolor=c["text"], font="MT.Body", arrowcolor=c["muted"])
    s.map(".", foreground=[("disabled", c["disabled"])])

    for name, bg in (("TFrame", c["bg"]), ("Card.TFrame", c["surface"]), ("Sidebar.TFrame", c["sidebar"]),
                     ("Bar.TFrame", c["surface"])):
        s.configure(name, background=bg)
    s.configure("TLabel", background=c["bg"], foreground=c["text"])
    s.configure("Card.TLabel", background=c["surface"])
    s.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])
    s.configure("CardMuted.TLabel", background=c["surface"], foreground=c["muted"], font="MT.Small")
    s.configure("H1.TLabel", background=c["bg"], font="MT.H1")
    s.configure("Sub.TLabel", background=c["bg"], foreground=c["muted"])
    s.configure("H2.TLabel", background=c["surface"], font="MT.H2")
    s.configure("Ok.TLabel", background=c["surface"], foreground=c["ok"])
    s.configure("Warn.TLabel", background=c["surface"], foreground=c["warn"])
    s.configure("Err.TLabel", background=c["surface"], foreground=c["danger"])
    s.configure("Bar.TLabel", background=c["surface"], foreground=c["muted"])
    s.configure("Banner.TLabel", background=c["accent_soft"], foreground=c["text"], padding=(12, 8))

    # buttons
    s.configure("TButton", background=c["surface2"], foreground=c["text"], bordercolor=c["border"],
                lightcolor=c["surface2"], darkcolor=c["surface2"], padding=(12, 5), relief="flat", focusthickness=0)
    s.map("TButton", background=[("disabled", c["surface2"]), ("pressed", c["select"]), ("active", c["accent_soft"])],
          bordercolor=[("active", c["accent"]), ("focus", c["accent"])],
          lightcolor=[("active", c["accent_soft"])], darkcolor=[("active", c["accent_soft"])])
    s.configure("Accent.TButton", background=c["accent"], foreground=c["accent_text"], bordercolor=c["accent"],
                lightcolor=c["accent"], darkcolor=c["accent"], padding=(16, 7), font="MT.Bold")
    s.map("Accent.TButton", background=[("disabled", c["disabled"]), ("pressed", c["accent_hover"]),
                                        ("active", c["accent_hover"])],
          foreground=[("disabled", c["surface"])],
          bordercolor=[("active", c["accent_hover"]), ("disabled", c["disabled"])],
          lightcolor=[("active", c["accent_hover"])], darkcolor=[("active", c["accent_hover"])])
    s.configure("Danger.TButton", foreground=c["danger"])
    s.configure("Link.TButton", background=c["surface"], foreground=c["accent"], bordercolor=c["surface"],
                lightcolor=c["surface"], darkcolor=c["surface"], padding=(2, 0))
    s.map("Link.TButton", background=[("active", c["surface"])], foreground=[("active", c["accent_hover"])],
          bordercolor=[("active", c["surface"])])
    s.configure("Small.TButton", padding=(8, 2), font="MT.Small")

    # inputs
    for w in ("TEntry", "TCombobox", "TSpinbox"):
        s.configure(w, fieldbackground=c["field"], foreground=c["text"], bordercolor=c["field_border"],
                    lightcolor=c["field"], darkcolor=c["field"], padding=(6, 4), arrowsize=14)
        s.map(w, bordercolor=[("focus", c["accent"])], lightcolor=[("focus", c["accent"])],
              fieldbackground=[("readonly", c["field"]), ("disabled", c["surface2"])],
              foreground=[("disabled", c["disabled"])])
    s.configure("Bad.TEntry", bordercolor=c["danger"], lightcolor=c["danger"])
    for w in ("TCheckbutton", "TRadiobutton"):
        s.configure(w, background=c["surface"], foreground=c["text"], indicatorbackground=c["field"],
                    indicatorforeground=c["accent_text"], indicatormargin=(0, 0, 8, 0), padding=(0, 3),
                    indicatorsize=14,
                    focusthickness=0, upperbordercolor=c["field_border"], lowerbordercolor=c["field_border"])
        s.map(w, indicatorbackground=[("selected", c["accent"]), ("active", c["accent_soft"])],
              background=[("active", c["surface"])], foreground=[("disabled", c["disabled"])])
    s.configure("Bg.TCheckbutton", background=c["bg"])
    s.map("Bg.TCheckbutton", background=[("active", c["bg"])])
    s.configure("Bg.TRadiobutton", background=c["bg"])
    s.map("Bg.TRadiobutton", background=[("active", c["bg"])])

    # notebook
    s.configure("TNotebook", background=c["bg"], bordercolor=c["border"], tabmargins=(0, 0, 0, 0))
    s.configure("TNotebook.Tab", background=c["bg"], foreground=c["muted"], padding=(16, 7), bordercolor=c["bg"],
                lightcolor=c["bg"], darkcolor=c["bg"])
    s.map("TNotebook.Tab", background=[("selected", c["surface"])], foreground=[("selected", c["accent"])],
          lightcolor=[("selected", c["accent"])], bordercolor=[("selected", c["border"])])

    # tree
    s.configure("Treeview", background=c["surface"], fieldbackground=c["surface"], foreground=c["text"],
                bordercolor=c["border"], rowheight=int(tkfont.nametofont("MT.Body").metrics("linespace") * 1.9))
    s.map("Treeview", background=[("selected", c["select"])], foreground=[("selected", c["select_text"])])
    s.configure("Treeview.Heading", background=c["surface2"], foreground=c["muted"], bordercolor=c["border"],
                lightcolor=c["surface2"], darkcolor=c["surface2"], relief="flat", padding=(8, 5), font="MT.Bold")
    s.map("Treeview.Heading", background=[("active", c["accent_soft"])])

    # misc
    s.configure("TProgressbar", background=c["accent"], troughcolor=c["surface2"], bordercolor=c["surface2"],
                lightcolor=c["accent"], darkcolor=c["accent"], thickness=8)
    s.configure("TScrollbar", background=c["surface2"], troughcolor=c["bg"], bordercolor=c["bg"],
                lightcolor=c["surface2"], darkcolor=c["surface2"], arrowcolor=c["muted"], gripcount=0)
    s.map("TScrollbar", background=[("active", c["border"])])
    s.configure("TScale", background=c["accent"], troughcolor=c["surface2"], bordercolor=c["border"],
                lightcolor=c["accent"], darkcolor=c["accent"])
    s.configure("TLabelframe", background=c["surface"], bordercolor=c["border"], lightcolor=c["border"],
                darkcolor=c["border"])
    s.configure("TLabelframe.Label", background=c["surface"], foreground=c["muted"], font="MT.Bold")
    s.configure("TSeparator", background=c["border"])
    s.configure("TPanedwindow", background=c["bg"])
    s.configure("Sash", sashthickness=6, background=c["bg"])

    # classic Tk widgets
    root.option_add("*Text.background", c["field"])
    root.option_add("*Text.foreground", c["text"])
    root.option_add("*Text.insertBackground", c["text"])
    root.option_add("*Text.selectBackground", c["select"])
    root.option_add("*Text.relief", "flat")
    root.option_add("*Text.highlightThickness", 1)
    root.option_add("*Text.highlightColor", c["accent"])
    root.option_add("*Text.highlightBackground", c["border"])
    root.option_add("*Listbox.background", c["field"])
    root.option_add("*Listbox.foreground", c["text"])
    root.option_add("*Listbox.selectBackground", c["select"])
    root.option_add("*Listbox.selectForeground", c["select_text"])
    root.option_add("*Listbox.relief", "flat")
    root.option_add("*Listbox.highlightThickness", 1)
    root.option_add("*Listbox.highlightBackground", c["border"])
    root.option_add("*Listbox.highlightColor", c["accent"])
    root.option_add("*TCombobox*Listbox.background", c["field"])
    root.option_add("*TCombobox*Listbox.foreground", c["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", c["accent_text"])
    root.option_add("*Menu.background", c["surface"])
    root.option_add("*Menu.foreground", c["text"])
    root.option_add("*Menu.activeBackground", c["accent"])
    root.option_add("*Menu.activeForeground", c["accent_text"])
    root.option_add("*Canvas.background", c["bg"])
    root.option_add("*Canvas.highlightThickness", 0)
    root.option_add("*Toplevel.background", c["bg"])
    return P
