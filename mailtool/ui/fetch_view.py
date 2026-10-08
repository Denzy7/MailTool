"""Fetch screen: pick a date range, choose what to save, go."""
from __future__ import annotations

import os
from tkinter import messagebox, ttk

from mailtool import APP_NAME
from mailtool.core import timeutil
from mailtool.core.util import open_in_file_manager
from mailtool.fetch import emailinfo as ei
from mailtool.fetch.fetcher import run_fetch
from mailtool.ui.app import View
from mailtool.ui.widgets import Card, DateRange, Form, PathEntry, ScrollPage, bool_var, str_var


class FetchView(View):
    title = "Fetch mail"
    subtitle = "Download emails from your mailbox into the library. Nothing is ever marked as read."

    def __init__(self, app):
        super().__init__(app)
        f = self.cfg["fetch"]
        today = timeutil.today_str()
        self.v = {
            "from_date": str_var(f.get("from_date") or today), "from_time": str_var(f.get("from_time") or "00:00"),
            "to_date": str_var(f.get("to_date") or today), "to_time": str_var(f.get("to_time") or "23:59"),
            "save_attachments": bool_var(f.get("save_attachments")), "merge_pdfs": bool_var(f.get("merge_pdfs")),
            "emailinfo": bool_var(f.get("emailinfo") and ei.REPORTLAB), "export_csv": bool_var(f.get("export_csv")),
            "csv_path": str_var(f.get("csv_path")), "log_without_attachments": bool_var(f.get("log_without_attachments")),
            "sort_after": bool_var(f.get("sort_after")),
        }
        self.mailbox = str_var(self.cfg.get("account", "mailbox") or "INBOX")
        self.library = str_var(app.library_root())
        self.last_result = None

        act = ttk.Frame(self.frame, padding=(28, 10, 28, 12))
        act.pack(side="bottom", fill="x")
        page = ScrollPage(self.frame)
        page.pack(fill="both", expand=True)
        c = page.content

        self.banner = ttk.Frame(c)
        bl = ttk.Label(self.banner, text="No mail account is set up yet.", style="Banner.TLabel")
        bl.pack(side="left", fill="x", expand=True)
        ttk.Button(self.banner, text="Set up account", style="Accent.TButton",
                   command=lambda: (app.show("settings"), app.views["settings"].select_tab("account"))).pack(
            side="left", padx=(8, 0))

        # -- which emails
        card = Card(c, "Which emails", "Chosen by arrival time (the Received: header) in your timezone.")
        card.pack(fill="x", pady=(8, 14))
        form = Form(card.body, label_width=12)
        self.range = DateRange(card.body, self.v["from_date"], self.v["from_time"], self.v["to_date"],
                               self.v["to_time"])
        form.add("Date range", self.range, sticky="w")
        mb = ttk.Entry(card.body, textvariable=self.mailbox, width=28)
        form.add("Mailbox", mb, sticky="w")
        self.tz_hint = ttk.Label(card.body, style="CardMuted.TLabel")
        form.full(self.tz_hint)

        # -- what to save
        card = Card(c, "What to do with them")
        card.pack(fill="x", pady=(0, 14))
        b = card.body
        ttk.Checkbutton(b, text="Save attachments", variable=self.v["save_attachments"],
                        command=self._sync).grid(row=0, column=0, sticky="w")
        ttk.Label(b, text="into  Library / <date> / <sender> - <subject> /", style="CardMuted.TLabel").grid(
            row=0, column=1, sticky="w", padx=10)
        self.cb_merge = ttk.Checkbutton(b, text="Also merge each email's PDF attachments into one MERGED_ file",
                                        variable=self.v["merge_pdfs"])
        self.cb_merge.grid(row=1, column=0, columnspan=2, sticky="w", padx=(26, 0))
        self.cb_nolog = ttk.Checkbutton(b, text="Log emails that have no attachments",
                                        variable=self.v["log_without_attachments"])
        self.cb_nolog.grid(row=2, column=0, columnspan=2, sticky="w", padx=(26, 0))

        self.cb_info = ttk.Checkbutton(b, text="Create an Email info PDF for each email", variable=self.v["emailinfo"])
        self.cb_info.grid(row=3, column=0, sticky="w", pady=(10, 0))
        info_row = ttk.Frame(b, style="Card.TFrame")
        info_row.grid(row=3, column=1, sticky="w", padx=10, pady=(10, 0))
        self.info_desc = ttk.Label(info_row, style="CardMuted.TLabel")
        self.info_desc.pack(side="left")
        ttk.Button(info_row, text="Options", style="Link.TButton",
                   command=lambda: (app.show("settings"), app.views["settings"].select_tab("emailinfo"))).pack(
            side="left", padx=4)
        if not ei.REPORTLAB:
            self.cb_info.state(["disabled"])
            self.info_desc.configure(text="needs reportlab: " + app.hint("reportlab"))

        ttk.Checkbutton(b, text="Export a CSV list of the emails", variable=self.v["export_csv"],
                        command=self._sync).grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.csv_entry = PathEntry(b, self.v["csv_path"], kind="file", save=True, title="Save CSV as",
                                   defaultext=".csv", filetypes=[("CSV", "*.csv")])
        self.csv_entry.grid(row=4, column=1, sticky="ew", padx=10, pady=(10, 0))
        ttk.Checkbutton(b, text="Sort them into groups when done", variable=self.v["sort_after"]).grid(
            row=5, column=0, sticky="w", pady=(10, 0))
        ttk.Label(b, text="uses the groups on the Sort screen", style="CardMuted.TLabel").grid(
            row=5, column=1, sticky="w", padx=10, pady=(10, 0))
        b.columnconfigure(1, weight=1)

        # -- where
        card = Card(c, "Save to")
        card.pack(fill="x", pady=(0, 14))
        row = ttk.Frame(card.body, style="Card.TFrame")
        row.pack(fill="x")
        PathEntry(row, self.library, kind="dir", title="Choose the library folder").pack(side="left", fill="x",
                                                                                      expand=True)
        ttk.Button(row, text="Open", command=lambda: open_in_file_manager(self._ensure_lib())).pack(side="left",
                                                                                              padx=(6, 0))
        ttk.Label(card.body, text="The library is a normal folder; MailTool keeps its index in a hidden "
                                  ".mailtool folder inside it.", style="CardMuted.TLabel").pack(anchor="w",
                                                                                                pady=(6, 0))

        # -- go (pinned below the scrolling page, always visible)
        self.go = ttk.Button(act, text="Fetch mail", style="Accent.TButton", command=self.start)
        self.go.pack(side="left")
        self.result_lbl = ttk.Label(act, text="", style="Muted.TLabel")
        self.result_lbl.pack(side="left", padx=14)
        self.after_btns = ttk.Frame(act)
        self.after_btns.pack(side="left")
        for v in ("from_date", "to_date"):
            self.v[v].trace_add("write", lambda *_: self._sync())
        self._sync()

    # ------------------------------------------------------------------
    def on_show(self):
        a = self.cfg["account"]
        if a.get("server") and a.get("username"):
            self.banner.pack_forget()
        else:
            kids = [w for w in self.banner.master.winfo_children() if w is not self.banner]
            self.banner.pack(fill="x", pady=(8, 0), before=kids[0])
        self.mailbox.set(self.cfg.get("account", "mailbox") or "INBOX")
        self.library.set(self.app.library_root())
        self.tz_hint.configure(text="Times are in %s - change the timezone in Settings › General."
                                    % self.app.tz_text())
        self._sync()

    def _sync(self):
        on = self.v["save_attachments"].get()
        for w in (self.cb_merge, self.cb_nolog):
            w.state(["!disabled"] if on else ["disabled"])
        self.csv_entry.state(["!disabled"] if self.v["export_csv"].get() else ["disabled"])
        if ei.REPORTLAB:
            mode = self.cfg.get("emailinfo", "body_mode") or "print"
            if not ei.GRAPHICAL:
                mode = "text"
            self.info_desc.configure(text="layout: %s" % ei.BODY_MODES.get(mode, mode).lower())

    def _ensure_lib(self):
        p = self.library.get().strip()
        os.makedirs(p, exist_ok=True)
        return p

    def save(self):
        self.cfg.update("fetch", {k: v.get() for k, v in self.v.items()})
        mb = self.mailbox.get().strip() or "INBOX"
        self.cfg.set("account", "mailbox", mb)
        lib = self.library.get().strip()
        if lib:
            self.cfg.set("general", "library_dir", lib)

    def start(self):
        if not self.app.require_account():
            return
        try:
            start, end = timeutil.parse_user_range(self.v["from_date"].get(), self.v["from_time"].get(),
                                                   self.v["to_date"].get(), self.v["to_time"].get())
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self.app.root)
            return
        opts = {k: v.get() for k, v in self.v.items()}
        if not (opts["save_attachments"] or opts["emailinfo"] or opts["export_csv"] or opts["sort_after"]):
            if not messagebox.askyesno(APP_NAME, "Nothing is ticked to save. Just add the emails to the library "
                                                 "index (subject, sender, body text)?", parent=self.app.root):
                return
        if opts["export_csv"] and not opts["csv_path"].strip():
            messagebox.showerror(APP_NAME, "Choose where to save the CSV.", parent=self.app.root)
            return
        self.save()
        self.cfg.save()
        try:
            root = self._ensure_lib()
        except OSError as e:
            messagebox.showerror(APP_NAME, "Cannot use the library folder:\n%s" % e, parent=self.app.root)
            return
        pw = self.app.get_password()
        if not pw:
            return
        self.go.state(["disabled"])
        self.result_lbl.configure(text="")
        for w in self.after_btns.winfo_children():
            w.destroy()
        self.app.start_job("fetch", "Fetching mail", run_fetch, on_done=self._done,
                           account=self.app.account(), password=pw, library_root=root, tz_text=self.app.tz_text(),
                           start=start, end=end, opts=opts, info=self.cfg.snapshot("emailinfo"))

    def _done(self, job):
        self.go.state(["!disabled"])
        r = job.result
        if not r:
            self.result_lbl.configure(text="Fetch %s - see the log below." % job.state)
            return
        self.last_result = r
        bits = ["%d email(s)" % r["messages"]]
        if r["attachments"]:
            bits.append("%d attachment(s)" % r["attachments"])
        if r["pdfs"]:
            bits.append("%d info PDF(s)" % r["pdfs"])
        self.result_lbl.configure(text="Fetched " + ", ".join(bits) + ".")
        if r["messages"]:
            ttk.Button(self.after_btns, text="Show in Library", style="Small.TButton",
                       command=lambda: self.app.views["library"].show_ids(r["message_ids"])).pack(side="left", padx=2)
            ttk.Button(self.after_btns, text="Print them", style="Small.TButton",
                       command=lambda: self.app.views["library"].print_ids(r["message_ids"])).pack(side="left",
                                                                                                padx=2)
        self.app.views["library"].mark_dirty()
        if self.v["sort_after"].get() and r["messages"] and job.state == "done":
            self.app.views["sort"].run_library(ids=r["message_ids"])

    def on_job_state(self, job):
        if job.kind == "fetch" and job.state != "running":
            self.go.state(["!disabled"])
