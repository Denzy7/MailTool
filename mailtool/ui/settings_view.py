"""Settings screen: account, general, email info PDF, printing, dependencies."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk

from mailtool import APP_NAME, __version__
from mailtool.core import deps, secrets, timeutil
from mailtool.core.util import IS_WIN, open_in_file_manager
from mailtool.fetch import emailinfo as ei
from mailtool.mail.imap import MailSession
from mailtool.printing.tools import have_word_com
from mailtool.ui.app import View
from mailtool.ui.widgets import Card, Form, PathEntry, ScrollPage

THEMES = {"system": "Follow the system", "light": "Light", "dark": "Dark"}


class SettingsView(View):
    title = "Settings"
    subtitle = "Your mail account, where things are saved, and how emails print."

    def __init__(self, app):
        super().__init__(app)
        self.nb = ttk.Notebook(self.frame)
        self.nb.pack(fill="both", expand=True, padx=28, pady=(6, 0))
        self.tabs = {}
        self._account_tab()
        self._general_tab()
        self._info_tab()
        self._print_tab()
        self._deps_tab()
        foot = ttk.Frame(self.frame, padding=(28, 10, 28, 14))
        foot.pack(fill="x")
        ttk.Button(foot, text="Save settings", style="Accent.TButton", command=self.apply).pack(side="right")
        ttk.Button(foot, text="Revert", command=self.revert).pack(side="right", padx=8)
        self.saved_lbl = ttk.Label(foot, style="Muted.TLabel")
        self.saved_lbl.pack(side="right", padx=8)
        ttk.Button(foot, text="Open settings folder", style="Small.TButton",
                   command=lambda: open_in_file_manager(os.path.dirname(self.cfg.path))).pack(side="left")
        self.revert()

    def _tab(self, key, title):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="  %s  " % title)
        page = ScrollPage(frame)
        page.pack(fill="both", expand=True)
        page.content.configure(padding=(0, 14, 14, 14))
        self.tabs[key] = frame
        return page.content

    def select_tab(self, key):
        if key in self.tabs:
            self.nb.select(self.tabs[key])

    # ------------------------------------------------------------------ account
    def _account_tab(self):
        c = self._tab("account", "Account")
        self.a = {k: tk.StringVar() for k in ("server", "port", "username", "mailbox", "timeout")}
        self.a_ssl = tk.BooleanVar()
        self.a_selfsigned = tk.BooleanVar()
        card = Card(c, "Mail server", "IMAP access to your mailbox. MailTool only ever reads - the mailbox is opened "
                                      "read-only and nothing is marked as read.")
        card.pack(fill="x", pady=(0, 12))
        f = Form(card.body, label_width=18)
        f.add("IMAP server", ttk.Entry(card.body, textvariable=self.a["server"], width=40), sticky="w")
        port = ttk.Frame(card.body, style="Card.TFrame")
        ttk.Entry(port, textvariable=self.a["port"], width=7).pack(side="left")
        ttk.Checkbutton(port, text="Use SSL/TLS (993)", variable=self.a_ssl,
                        command=self._ssl_toggled).pack(side="left", padx=14)
        f.add("Port", port, sticky="w")
        f.full(ttk.Checkbutton(card.body, text="Allow a self-signed certificate (skip certificate checks - only for "
                                               "your own server on a network you trust)",
                               variable=self.a_selfsigned))
        f.add("Email / username", ttk.Entry(card.body, textvariable=self.a["username"], width=40), sticky="w")
        mb = ttk.Frame(card.body, style="Card.TFrame")
        self.mb_cb = ttk.Combobox(mb, textvariable=self.a["mailbox"], width=30)
        self.mb_cb.pack(side="left")
        ttk.Label(mb, text="Test the connection to list folders", style="CardMuted.TLabel").pack(side="left", padx=10)
        f.add("Mailbox", mb, sticky="w")
        f.add("Timeout (seconds)", ttk.Spinbox(card.body, from_=10, to=600, increment=10, width=7,
                                               textvariable=self.a["timeout"]), sticky="w")

        card = Card(c, "Password")
        card.pack(fill="x", pady=(0, 12))
        self.pw_lbl = ttk.Label(card.body, style="Card.TLabel", wraplength=700, justify="left")
        self.pw_lbl.pack(anchor="w")
        row = ttk.Frame(card.body, style="Card.TFrame")
        row.pack(anchor="w", pady=(10, 0))
        ttk.Button(row, text="Enter password…", command=self.enter_password).pack(side="left")
        ttk.Button(row, text="Forget password", command=self.forget_password).pack(side="left", padx=8)
        ttk.Button(row, text="Test connection", style="Accent.TButton", command=self.test).pack(side="left")
        self.test_lbl = ttk.Label(card.body, style="CardMuted.TLabel", wraplength=700, justify="left")
        self.test_lbl.pack(anchor="w", pady=(8, 0))

    def _ssl_toggled(self):
        p = self.a["port"].get().strip()
        if self.a_ssl.get() and p in ("", "143"):
            self.a["port"].set("993")
        elif not self.a_ssl.get() and p in ("", "993"):
            self.a["port"].set("143")

    def _pw_status(self):
        a = self.cfg["account"]
        if not secrets.keyring_available():
            txt = ("You'll be asked for the password when MailTool needs it; it is kept in memory until you quit. "
                   "Install 'keyring' to let MailTool remember it securely: %s" % self.app.hint("keyring"))
        elif a.get("remember_password") and secrets.get_password(a.get("server"), a.get("username")):
            txt = "The password is saved in your system keyring."
        elif secrets.get_password(a.get("server"), a.get("username")):
            txt = "The password is kept for this session only."
        else:
            txt = "You'll be asked for the password when it is needed. Tick 'Remember' then to keep it in the keyring."
        self.pw_lbl.configure(text=txt + "\nIt is never written to MailTool's settings file.")

    def enter_password(self):
        self.apply(quiet=True)
        if not self.app.require_account():
            return
        self.app.get_password(force=True)
        self._pw_status()

    def forget_password(self):
        a = self.cfg["account"]
        secrets.forget(a.get("server"), a.get("username"))
        self.cfg.set("account", "remember_password", False)
        self.cfg.save()
        self._pw_status()
        self.app.log("Password forgotten.", "ok")

    def test(self):
        self.apply(quiet=True)
        if not self.app.require_account():
            return
        pw = self.app.get_password()
        if not pw:
            return
        self.test_lbl.configure(text="Connecting…")

        def work(job, account, password):
            s = MailSession(account, password, job=job)
            try:
                s.connect(False)
                boxes = s.list_mailboxes()
                count = s.select(account.get("mailbox") or "INBOX")
                return {"boxes": boxes, "count": count}
            finally:
                s.close()
        self.app.start_job("test", "Testing the connection", work, on_done=self._tested,
                           account=self.app.account(), password=pw)

    def _tested(self, job):
        self._pw_status()
        if job.result:
            boxes = job.result["boxes"]
            self.mb_cb.configure(values=boxes)
            self.test_lbl.configure(text="Connected. '%s' has %s message(s). %d folder(s) available." % (
                self.a["mailbox"].get() or "INBOX", job.result["count"], len(boxes)), style="Ok.TLabel")
        else:
            self.test_lbl.configure(text="Could not connect: %s" % (job.error or job.state), style="Err.TLabel")

    # ------------------------------------------------------------------ general
    def _general_tab(self):
        c = self._tab("general", "General")
        self.g_lib = tk.StringVar()
        self.g_tz = tk.StringVar()
        self.g_theme = tk.StringVar()
        card = Card(c, "Library", "Where fetched emails are saved. Moving it? Move the whole folder (including "
                                  "the hidden .mailtool folder) and point MailTool at the new place.")
        card.pack(fill="x", pady=(0, 12))
        Form(card.body, label_width=18).add("Library folder", PathEntry(card.body, self.g_lib, kind="dir",
                                                                        title="Library folder"))
        card = Card(c, "Time")
        card.pack(fill="x", pady=(0, 12))
        f = Form(card.body, label_width=18)
        tzrow = ttk.Frame(card.body, style="Card.TFrame")
        ttk.Entry(tzrow, textvariable=self.g_tz, width=24).pack(side="left")
        self.tz_ok = ttk.Label(tzrow, style="CardMuted.TLabel")
        self.tz_ok.pack(side="left", padx=10)
        f.add("Timezone", tzrow, sticky="w",
              hint="An IANA name like Africa/Nairobi or Europe/London, or an offset like +03:00. Used for date "
                   "ranges, folder dates and CSV times.")
        self.g_tz.trace_add("write", lambda *_: self._check_tz())
        card = Card(c, "Appearance")
        card.pack(fill="x", pady=(0, 12))
        f = Form(card.body, label_width=18)
        f.add("Theme", ttk.Combobox(card.body, textvariable=self.g_theme, values=list(THEMES.values()),
                                    state="readonly", width=22), sticky="w",
              hint="Takes effect the next time MailTool starts.")

    def _check_tz(self):
        ok = timeutil.timezone_ok(self.g_tz.get())
        if ok:
            from datetime import datetime
            now = datetime.now(timeutil.resolve_timezone(self.g_tz.get()))
            self.tz_ok.configure(text="✓  now %s" % now.strftime("%H:%M  (UTC%z)"), style="Ok.TLabel")
        else:
            self.tz_ok.configure(text="Not recognised" + (" - install tzdata: " + self.app.hint("tzdata")
                                                          if IS_WIN else ""),
                                 style="Err.TLabel")

    # ------------------------------------------------------------------ email info
    def _info_tab(self):
        c = self._tab("emailinfo", "Email info PDF")
        self.i_left, self.i_right = tk.StringVar(), tk.StringVar()
        self.i_mode = tk.StringVar()
        self.i_remote = tk.BooleanVar()
        self.i_quality = tk.IntVar()
        self.i_workers = tk.StringVar()
        card = Card(c, "Page header", "Printed at the top of every page of an EMAILINFO PDF.")
        card.pack(fill="x", pady=(0, 12))
        f = Form(card.body, label_width=18)
        f.add("Top left", ttk.Entry(card.body, textvariable=self.i_left, width=60), sticky="w",
              hint="Empty = your username.")
        f.add("Top right", ttk.Entry(card.body, textvariable=self.i_right, width=60), sticky="w",
              hint="Placeholders: " + "  ".join("<%s>" % k for k in ei.TEMPLATE_SPECIFIERS) +
                   "\nA value starting with http(s):// becomes a clickable link, e.g.\n"
                   "https://mail.example.com/modern/email/conversation/-<UID>/")
        card = Card(c, "Email body")
        card.pack(fill="x", pady=(0, 12))
        f = Form(card.body, label_width=18)
        modes = list(ei.BODY_MODES.values()) if ei.GRAPHICAL else [ei.BODY_MODES["text"]]
        self.mode_cb = ttk.Combobox(card.body, textvariable=self.i_mode, values=modes, state="readonly", width=34)
        f.add("Layout", self.mode_cb, sticky="w",
              hint="Printed = like your webmail's Print: selectable text, working links. Image = exact pixels. "
                   "Plain text = fastest, no browser needed." +
                   ("" if ei.GRAPHICAL else "\nPrinted/Image need: pip install playwright pillow, then "
                                            "playwright install chromium"))
        self.mode_cb.bind("<<ComboboxSelected>>", lambda e: self._mode_sync())
        self.cb_remote = ttk.Checkbutton(card.body, text="Load remote images (http/https images only). Loading them "
                                                         "can tell the sender the email was opened.",
                                         variable=self.i_remote)
        f.full(self.cb_remote)
        q = ttk.Frame(card.body, style="Card.TFrame")
        self.q_scale = ttk.Scale(q, from_=1, to=len(ei.QUALITY_LEVELS), orient="horizontal", length=200,
                                 command=self._q_moved)
        self.q_scale.pack(side="left")
        self.q_lbl = ttk.Label(q, style="Card.TLabel", width=20)
        self.q_lbl.pack(side="left", padx=10)
        f.add("Image quality", q, sticky="w")
        w = ttk.Frame(card.body, style="Card.TFrame")
        ttk.Spinbox(w, from_=1, to=max(8, os.cpu_count() or 2), width=4, textvariable=self.i_workers,
                    state="readonly").pack(side="left")
        ttk.Label(w, text="each uses its own headless browser (~0.5 GB RAM); fewer are used if memory is short",
                  style="CardMuted.TLabel").pack(side="left", padx=10)
        f.add("Parallel renders", w, sticky="w")
        ttk.Label(card.body, text="The sender's JavaScript never runs, and nothing but images is ever downloaded.",
                  style="CardMuted.TLabel").grid(row=f.row, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _q_moved(self, value):
        level = int(round(float(value)))
        self.i_quality.set(level)
        if abs(float(self.q_scale.get()) - level) > 1e-6:
            self.q_scale.set(level)
        self.q_lbl.configure(text=ei.quality_label(level))

    def _mode_key(self):
        shown = self.i_mode.get()
        return next((k for k, v in ei.BODY_MODES.items() if v == shown), "text")

    def _mode_sync(self):
        m = self._mode_key()
        self.q_scale.state(["!disabled"] if m == "image" else ["disabled"])
        self.cb_remote.state(["!disabled"] if m in ("print", "image") else ["disabled"])

    # ------------------------------------------------------------------ printing
    def _print_tab(self):
        c = self._tab("print", "Printing")
        self.p = {k: tk.StringVar() for k in ("word_engine", "max_word", "max_parallel", "paper", "copy_timeout",
                                              "convert_timeout", "print_timeout", "sumatra", "soffice", "temp_dir")}
        card = Card(c, "Word documents")
        card.pack(fill="x", pady=(0, 12))
        b = card.body
        self.rb_lo = ttk.Radiobutton(b, text="LibreOffice", variable=self.p["word_engine"], value="libreoffice")
        self.rb_lo.pack(anchor="w")
        self.rb_word = ttk.Radiobutton(b, text="Microsoft Word", variable=self.p["word_engine"], value="word")
        self.rb_word.pack(anchor="w")
        row = ttk.Frame(b, style="Card.TFrame")
        row.pack(anchor="w", pady=(6, 0))
        ttk.Label(row, text="Word instances at once", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(row, from_=1, to=8, width=4, textvariable=self.p["max_word"]).pack(side="left", padx=8)
        ttk.Label(row, text="1 = one after another (recommended)", style="CardMuted.TLabel").pack(side="left")

        card = Card(c, "Queue")
        card.pack(fill="x", pady=(0, 12))
        f = Form(card.body, label_width=24)
        f.add("Files prepared at once", ttk.Spinbox(card.body, from_=1, to=32, width=5,
                                                    textvariable=self.p["max_parallel"]), sticky="w")
        f.add("Paper size for images", ttk.Combobox(card.body, textvariable=self.p["paper"], values=["A4", "Letter"],
                                                    state="readonly", width=8), sticky="w")
        for label, key in (("Fetch timeout (s)", "copy_timeout"), ("Convert timeout (s)", "convert_timeout"),
                           ("Print timeout (s)", "print_timeout")):
            f.add(label, ttk.Spinbox(card.body, from_=10, to=7200, increment=10, width=7,
                                     textvariable=self.p[key]), sticky="w")

        card = Card(c, "Programs", "Leave empty to find them automatically.")
        card.pack(fill="x", pady=(0, 12))
        f = Form(card.body, label_width=24)
        if IS_WIN:
            f.add("SumatraPDF.exe", PathEntry(card.body, self.p["sumatra"], kind="file", title="SumatraPDF.exe"))
        f.add("LibreOffice (soffice)", PathEntry(card.body, self.p["soffice"], kind="file", title="soffice"))
        f.add("Temp folder", PathEntry(card.body, self.p["temp_dir"], kind="dir", title="Temp folder"),
              hint="Takes effect the next time MailTool starts.")

    # ------------------------------------------------------------------ dependencies
    def _deps_tab(self):
        c = self._tab("deps", "Dependencies")
        card = Card(c, "What's installed", "Everything optional only switches a feature on or off. "
                                           "Missing items show the command that installs them.")
        card.pack(fill="both", expand=True)
        self.deps_text = tk.Text(card.body, height=26, wrap="word", font="MT.Mono", padx=10, pady=8)
        self.deps_text.pack(fill="both", expand=True)
        row = ttk.Frame(card.body, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Check again", command=self.refresh_deps).pack(side="left")
        self.chromium_btn = ttk.Button(row, text="Install Chromium for email bodies", command=self.install_chromium)
        if ei.PLAYWRIGHT:
            self.chromium_btn.pack(side="left", padx=8)
        ttk.Label(row, text="MailTool %s" % __version__, style="CardMuted.TLabel").pack(side="right")
        self.refresh_deps()

    def install_chromium(self):
        self.app.start_job("test", "Installing Chromium", deps.install_chromium,
                           on_done=lambda job: self.refresh_deps())

    def refresh_deps(self):
        self.app.recheck_caps()
        self.deps_text.configure(state="normal")
        self.deps_text.delete("1.0", "end")
        self.deps_text.insert("1.0", deps.format_caps(self.app.caps))
        self.deps_text.configure(state="disabled")

    # ------------------------------------------------------------------ load / save
    def revert(self):
        a = self.cfg["account"]
        for k in self.a:
            self.a[k].set("" if a.get(k) is None else str(a.get(k)))
        self.a_ssl.set(bool(a.get("use_ssl", True)))
        self.a_selfsigned.set(bool(a.get("allow_self_signed")))
        g = self.cfg["general"]
        self.g_lib.set(self.app.library_root())
        self.g_tz.set(g.get("timezone") or "Africa/Nairobi")
        self.g_theme.set(THEMES.get(g.get("theme") or "system", THEMES["system"]))
        i = self.cfg["emailinfo"]
        self.i_left.set(i.get("header_left") or "")
        self.i_right.set(i.get("header_right") or "")
        mode = i.get("body_mode") or "print"
        self.i_mode.set(ei.BODY_MODES.get(mode if ei.GRAPHICAL else "text"))
        self.i_remote.set(bool(i.get("remote_images", True)))
        q = int(i.get("quality") or 2)
        self.q_scale.set(q)
        self._q_moved(q)
        self.i_workers.set(str(i.get("workers") or 2))
        self._mode_sync()
        p = self.cfg["print"]
        for k in self.p:
            self.p[k].set("" if p.get(k) is None else str(p.get(k)))
        if not have_word_com():
            self.rb_word.configure(text="Microsoft Word  (needs Windows, Word and pywin32)")
            self.rb_word.state(["disabled"])
            if self.p["word_engine"].get() == "word":
                self.p["word_engine"].set("libreoffice")
        self._pw_status()
        self.saved_lbl.configure(text="")

    def apply(self, quiet=False):
        def num(var, lo, hi, default):
            try:
                return max(lo, min(hi, int(float(var.get()))))
            except (TypeError, ValueError):
                return default
        acc = {"server": self.a["server"].get().strip(), "username": self.a["username"].get().strip(),
               "mailbox": self.a["mailbox"].get().strip() or "INBOX",
               "port": num(self.a["port"], 1, 65535, 993 if self.a_ssl.get() else 143),
               "timeout": num(self.a["timeout"], 10, 600, 60),
               "use_ssl": bool(self.a_ssl.get()), "allow_self_signed": bool(self.a_selfsigned.get())}
        old = self.cfg["account"]
        if (old.get("server"), old.get("username")) != (acc["server"], acc["username"]):
            acc["remember_password"] = False
        self.cfg.update("account", acc)
        tz = self.g_tz.get().strip() or "Africa/Nairobi"
        if not timeutil.timezone_ok(tz) and not quiet:
            messagebox.showwarning(APP_NAME, "The timezone %r is not recognised - the computer's own timezone will "
                                             "be used until it is fixed." % tz, parent=self.app.root)
        theme_key = next((k for k, v in THEMES.items() if v == self.g_theme.get()), "system")
        self.cfg.update("general", {"library_dir": self.g_lib.get().strip(), "timezone": tz, "theme": theme_key})
        self.cfg.update("emailinfo", {"header_left": self.i_left.get().strip(), "header_right": self.i_right.get().strip(),
                                      "body_mode": self._mode_key(), "remote_images": bool(self.i_remote.get()),
                                      "quality": int(self.i_quality.get() or 2),
                                      "workers": num(self.i_workers, 1, 32, 2)})
        pr = {"word_engine": self.p["word_engine"].get() or "libreoffice", "paper": self.p["paper"].get() or "A4",
              "max_word": num(self.p["max_word"], 1, 8, 1), "max_parallel": num(self.p["max_parallel"], 1, 32, 4),
              "copy_timeout": num(self.p["copy_timeout"], 10, 7200, 600),
              "convert_timeout": num(self.p["convert_timeout"], 10, 7200, 180),
              "print_timeout": num(self.p["print_timeout"], 10, 7200, 300)}
        for k in ("sumatra", "soffice", "temp_dir"):
            pr[k] = self.p[k].get().strip() or None
        for k in ("sumatra", "soffice"):
            if pr[k] and not os.path.isfile(pr[k]) and not quiet:
                messagebox.showwarning(APP_NAME, "File not found:\n%s" % pr[k], parent=self.app.root)
                return
        self.cfg.update("print", pr)
        self.cfg.save()
        self.app.refresh_account_label()
        self.app.views["print"].apply_settings()
        if self.app.current == "fetch":
            self.app.views["fetch"].on_show()
        self.app.views["library"].mark_dirty()
        self._pw_status()
        if not quiet:
            self.saved_lbl.configure(text="Saved.")
            self.app.status("Settings saved", "ok")
            self.refresh_deps()

    def save(self):
        pass   # settings are saved explicitly with the button
