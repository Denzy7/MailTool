"""Reusable widgets: cards, scrolling pages, form rows, path pickers, date ranges,
tooltips, the colour-tagged log view, and the password prompt."""
from __future__ import annotations

import os
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, ttk

from mailtool.core import timeutil
from mailtool.ui.theme import P

try:
    from tkinterdnd2 import DND_FILES, DND_TEXT, TkinterDnD
    DND_ERR = None
except Exception as _e:
    TkinterDnD = None
    DND_FILES, DND_TEXT = "DND_Files", "DND_Text"
    DND_ERR = _e


def make_root():
    """Tk root with drag & drop if tkdnd can load. -> (root, dnd_ok)"""
    from mailtool.core.util import IS_WIN, log
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


class Card(tk.Frame):
    """White (or dark) panel with a thin border, optional title and subtitle.
    Put children in .body."""

    def __init__(self, parent, title=None, subtitle=None, padding=16, **kw):
        super().__init__(parent, bg=P["surface"], highlightthickness=1, highlightbackground=P["border"],
                         highlightcolor=P["border"], bd=0, **kw)
        self.inner = ttk.Frame(self, style="Card.TFrame", padding=padding)
        self.inner.pack(fill="both", expand=True)
        if title:
            head = ttk.Frame(self.inner, style="Card.TFrame")
            head.pack(fill="x", pady=(0, 10))
            ttk.Label(head, text=title, style="H2.TLabel").pack(side="left")
            self.head = head
            if subtitle:
                ttk.Label(self.inner, text=subtitle, style="CardMuted.TLabel", wraplength=700,
                          justify="left").pack(fill="x", pady=(0, 10), before=None)
        self.body = ttk.Frame(self.inner, style="Card.TFrame")
        self.body.pack(fill="both", expand=True)


class ScrollPage(ttk.Frame):
    """Vertically scrolling page; put content in .content."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.canvas = tk.Canvas(self, bg=P["bg"], highlightthickness=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(self.canvas, padding=(24, 4, 24, 24))
        self._win = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        for w in (self.canvas, self.content):
            w.bind("<Enter>", lambda e: self._bind_wheel(True))
            w.bind("<Leave>", lambda e: self._bind_wheel(False))

    def _bind_wheel(self, on):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")
            self.canvas.bind_all("<Button-4>", self._wheel, add="+")
            self.canvas.bind_all("<Button-5>", self._wheel, add="+")
        else:
            for s in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.canvas.unbind_all(s)

    def _wheel(self, e):
        w = e.widget
        # let lists/text boxes scroll themselves
        while w is not None:
            if isinstance(w, (tk.Text, tk.Listbox, ttk.Treeview)):
                return
            w = getattr(w, "master", None)
        if self.content.winfo_height() <= self.canvas.winfo_height():
            return
        d = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        self.canvas.yview_scroll(d * 3, "units")


class Form:
    """Two-column label/field grid inside a card body."""

    def __init__(self, parent, label_width=None):
        self.f = parent
        self.row = 0
        self.f.columnconfigure(1, weight=1)
        self.label_width = label_width

    def add(self, label, widget, hint=None, sticky="ew"):
        lbl = ttk.Label(self.f, text=label, style="Card.TLabel", width=self.label_width)
        lbl.grid(row=self.row, column=0, sticky="nw", padx=(0, 14), pady=5)
        widget.grid(row=self.row, column=1, sticky=sticky, pady=5)
        self.row += 1
        if hint:
            self.hint(hint)
        return widget

    def hint(self, text):
        ttk.Label(self.f, text=text, style="CardMuted.TLabel", wraplength=620, justify="left").grid(
            row=self.row, column=1, sticky="w", pady=(0, 6))
        self.row += 1

    def full(self, widget, pady=4):
        widget.grid(row=self.row, column=0, columnspan=2, sticky="w", pady=pady)
        self.row += 1
        return widget


class PathEntry(ttk.Frame):
    def __init__(self, parent, var, kind="dir", title=None, save=False, filetypes=None, defaultext=None, **kw):
        super().__init__(parent, style="Card.TFrame", **kw)
        self.var, self.kind, self.title, self.save = var, kind, title, save
        self.filetypes, self.defaultext = filetypes, defaultext
        self.entry = ttk.Entry(self, textvariable=var)
        self.entry.pack(side="left", fill="x", expand=True)
        self.btn = ttk.Button(self, text="Browse…", command=self.browse)
        self.btn.pack(side="left", padx=(6, 0))

    def browse(self):
        cur = self.var.get().strip()
        init = cur if os.path.isdir(cur) else (os.path.dirname(cur) or None)
        if self.kind == "dir":
            p = filedialog.askdirectory(parent=self, title=self.title, initialdir=init)
        elif self.save:
            p = filedialog.asksaveasfilename(parent=self, title=self.title, initialdir=init,
                                             initialfile=os.path.basename(cur) or None,
                                             defaultextension=self.defaultext, filetypes=self.filetypes or [])
        else:
            p = filedialog.askopenfilename(parent=self, title=self.title, initialdir=init,
                                           filetypes=self.filetypes or [])
        if p:
            self.var.set(p)

    def state(self, s):
        self.entry.state(s)
        self.btn.state(s)


class DateRange(ttk.Frame):
    """From [date] at [time]   To [date] at [time]   plus quick picks."""

    def __init__(self, parent, from_d, from_t, to_d, to_t, times=True, allow_blank=False, **kw):
        super().__init__(parent, style="Card.TFrame", **kw)
        self.vars = (from_d, from_t, to_d, to_t)
        self.allow_blank = allow_blank
        row = ttk.Frame(self, style="Card.TFrame")
        row.pack(fill="x")
        self.entries = []
        for label, dv, tv in (("From", from_d, from_t), ("To", to_d, to_t)):
            ttk.Label(row, text=label, style="Card.TLabel").pack(side="left", padx=(0 if label == "From" else 18, 6))
            e = ttk.Entry(row, textvariable=dv, width=13)
            e.pack(side="left")
            self.entries.append(e)
            if times:
                ttk.Label(row, text="at", style="CardMuted.TLabel").pack(side="left", padx=5)
                te = ttk.Entry(row, textvariable=tv, width=6)
                te.pack(side="left")
                self.entries.append(te)
        quick = ttk.Frame(self, style="Card.TFrame")
        quick.pack(fill="x", pady=(8, 0))
        picks = [("Today", 0, 0), ("Yesterday", 1, 1), ("Last 7 days", 6, 0), ("Last 30 days", 29, 0)]
        if allow_blank:
            picks.append(("Everything", None, None))
        for text, a, b in picks:
            ttk.Button(quick, text=text, style="Small.TButton",
                       command=lambda a=a, b=b: self.pick(a, b)).pack(side="left", padx=(0, 6))
        for v in self.vars:
            v.trace_add("write", lambda *_: self.validate())

    def pick(self, days_back, days_back_end):
        fd, ft, td, tt = self.vars
        if days_back is None:
            fd.set("")
            td.set("")
            return
        now = datetime.now()
        fd.set((now - timedelta(days=days_back)).strftime(timeutil.DATE_FMT))
        td.set((now - timedelta(days=days_back_end)).strftime(timeutil.DATE_FMT))
        ft.set("00:00")
        tt.set("23:59")

    def validate(self):
        ok = True
        for e, v in zip(self.entries, [x for x in self.vars] if len(self.entries) == 4 else
                        [self.vars[0], self.vars[2]]):
            txt = v.get().strip()
            good = True
            if not txt:
                good = self.allow_blank
            elif ":" in txt:
                try:
                    datetime.strptime(txt, timeutil.TIME_FMT)
                except ValueError:
                    good = False
            else:
                try:
                    datetime.strptime(txt, timeutil.DATE_FMT)
                except ValueError:
                    good = False
            e.configure(style="TEntry" if good else "Bad.TEntry")
            ok = ok and good
        return ok


class Tooltip:
    def __init__(self, widget, text):
        self.w, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _e=None):
        if self.tip or not self.text:
            return
        x = self.w.winfo_rootx() + 10
        y = self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.tip = t = tk.Toplevel(self.w)
        t.wm_overrideredirect(True)
        t.wm_geometry("+%d+%d" % (x, y))
        tk.Label(t, text=self.text, bg=P["text"], fg=P["surface"], padx=8, pady=4, justify="left",
                 wraplength=360, font="MT.Small").pack()

    def hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class LogView(ttk.Frame):
    def __init__(self, parent, height=8, **kw):
        super().__init__(parent, style="Card.TFrame", **kw)
        self.text = tk.Text(self, height=height, wrap="word", state="disabled", font="MT.Mono", padx=10, pady=6,
                            highlightthickness=0, bg=P["surface"])
        sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("time", foreground=P["faint"])
        self.text.tag_configure("error", foreground=P["danger"])
        self.text.tag_configure("warn", foreground=P["warn"])
        self.text.tag_configure("ok", foreground=P["ok"])
        self.text.tag_configure("job", foreground=P["accent"])

    def add(self, text, level="info", prefix=None):
        t = self.text
        at_end = t.yview()[1] > 0.98
        t.configure(state="normal")
        t.insert("end", datetime.now().strftime("%H:%M:%S  "), ("time",))
        if prefix:
            t.insert("end", "%s  " % prefix, ("job",))
        t.insert("end", str(text) + "\n", (level,) if level != "info" else ())
        # keep memory bounded
        if int(t.index("end-1c").split(".")[0]) > 5000:
            t.delete("1.0", "1000.0")
        t.configure(state="disabled")
        if at_end:
            t.see("end")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


class PasswordDialog:
    """Modal password prompt. .result = (password, remember) or None."""

    def __init__(self, parent, user, server, can_remember, remember_default=False):
        self.result = None
        t = self.top = tk.Toplevel(parent)
        t.title("Password")
        t.transient(parent)
        t.resizable(False, False)
        t.configure(bg=P["surface"])
        f = ttk.Frame(t, style="Card.TFrame", padding=22)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Sign in to your mailbox", style="H2.TLabel").pack(anchor="w")
        ttk.Label(f, text="%s on %s" % (user, server), style="CardMuted.TLabel").pack(anchor="w", pady=(2, 14))
        self.pw = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.pw, show="•", width=36)
        e.pack(fill="x")
        self.rem = tk.BooleanVar(value=remember_default and can_remember)
        cb = ttk.Checkbutton(f, text="Remember in the system keyring" if can_remember else
                             "Remember (install 'keyring' to enable)", variable=self.rem)
        cb.pack(anchor="w", pady=(10, 0))
        if not can_remember:
            cb.state(["disabled"])
        ttk.Label(f, text="Never written to MailTool's settings file.", style="CardMuted.TLabel").pack(anchor="w")
        b = ttk.Frame(f, style="Card.TFrame")
        b.pack(fill="x", pady=(16, 0))
        ttk.Button(b, text="Sign in", style="Accent.TButton", command=self.ok).pack(side="right")
        ttk.Button(b, text="Cancel", command=t.destroy).pack(side="right", padx=8)
        e.bind("<Return>", lambda _e: self.ok())
        t.bind("<Escape>", lambda _e: t.destroy())
        t.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width() - t.winfo_reqwidth()) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - t.winfo_reqheight()) // 3
        t.geometry("+%d+%d" % (max(px, 0), max(py, 0)))
        e.focus_set()
        try:
            t.grab_set()
        except tk.TclError:
            pass
        parent.wait_window(t)

    def ok(self):
        if self.pw.get():
            self.result = (self.pw.get(), bool(self.rem.get()))
            self.top.destroy()


def bool_var(value):
    return tk.BooleanVar(value=bool(value))


def str_var(value):
    return tk.StringVar(value="" if value is None else str(value))


def section_label(parent, text):
    ttk.Label(parent, text=text.upper(), style="CardMuted.TLabel").pack(anchor="w", pady=(12, 4))
