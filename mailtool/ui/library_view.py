"""Library screen: browse what has been downloaded, see groups, open, print, sort."""
from __future__ import annotations

import os
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from mailtool import APP_NAME
from mailtool.core import timeutil
from mailtool.core.util import human_size, open_in_file_manager
from mailtool.library.db import Library
from mailtool.library.folders import ordered_print_files
from mailtool.ui.app import View
from mailtool.ui.theme import P

ALL, UNSORTED, UNMATCHED = "All emails", "Not sorted yet", "Unmatched"


class LibraryView(View):
    title = "Library"
    subtitle = "Everything you have fetched. Select emails to print, sort or open them."

    def __init__(self, app):
        super().__init__(app)
        self.lib = None
        self.lib_root = None
        self.rows = {}
        self.only_ids = None
        self.dirty = True
        self._search_job = None

        top = ttk.Frame(self.frame, padding=(28, 6, 28, 8))
        top.pack(fill="x")
        self.q = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.q, width=30)
        e.pack(side="left")
        e.insert(0, "")
        self._placeholder(e, "Search subject, sender or group")
        self.q.trace_add("write", lambda *_: self._debounce())
        self.group = tk.StringVar(value=ALL)
        self.group_cb = ttk.Combobox(top, textvariable=self.group, state="readonly", width=22)
        self.group_cb.pack(side="left", padx=8)
        self.group_cb.bind("<<ComboboxSelected>>", lambda e: self.reload())
        ttk.Label(top, text="From").pack(side="left", padx=(8, 4))
        self.d_from = tk.StringVar()
        ttk.Entry(top, textvariable=self.d_from, width=12).pack(side="left")
        ttk.Label(top, text="to").pack(side="left", padx=4)
        self.d_to = tk.StringVar()
        ttk.Entry(top, textvariable=self.d_to, width=12).pack(side="left")
        ttk.Button(top, text="Apply", style="Small.TButton", command=self.reload).pack(side="left", padx=6)
        ttk.Button(top, text="Refresh", style="Small.TButton", command=self.reload).pack(side="right")
        self.filter_note = ttk.Frame(self.frame, padding=(28, 0, 28, 6))
        self.filter_lbl = ttk.Label(self.filter_note, style="Muted.TLabel")
        self.filter_lbl.pack(side="left")
        ttk.Button(self.filter_note, text="Show all", style="Small.TButton",
                   command=self.clear_only).pack(side="left", padx=8)

        pw = ttk.PanedWindow(self.frame, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=28, pady=(0, 12))
        left = ttk.Frame(pw)
        right = tk.Frame(pw, bg=P["surface"], highlightthickness=1, highlightbackground=P["border"])
        pw.add(left, weight=1)
        pw.add(right, weight=0)
        self.pw = pw

        cols = ("when", "sender", "subject", "att", "group")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        for col, text, w, anchor, stretch in (("when", "Received", 132, "w", False), ("sender", "From", 100, "w", True),
                                              ("subject", "Subject", 150, "w", True), ("att", "Files", 44, "center",
                                                                                       False),
                                              ("group", "Group", 90, "w", True)):
            self.tree.heading(col, text=text, command=lambda c=col: self.sort_by(c))
            self.tree.column(col, width=w, minwidth=40, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("alt", background=P["row_alt"])
        self.tree.tag_configure("unmatched", foreground=P["warn"])
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_detail())
        self.tree.bind("<Double-1>", lambda e: self.open_folder())
        self.tree.bind("<Button-3>", self._menu)
        self.empty = ttk.Label(left, text="", style="Muted.TLabel", justify="center")

        # details ---------------------------------------------------------------
        d = ttk.Frame(right, style="Card.TFrame", padding=18)
        d.pack(fill="both", expand=True)
        self.d_subject = ttk.Label(d, text="", style="H2.TLabel", wraplength=330, justify="left")
        self.d_subject.pack(anchor="w")
        self.d_meta = ttk.Label(d, text="Select an email.", style="CardMuted.TLabel", wraplength=330,
                                justify="left")
        self.d_meta.pack(anchor="w", pady=(6, 8))
        self.d_group = ttk.Label(d, text="", style="Card.TLabel", wraplength=330, justify="left")
        self.d_group.pack(anchor="w", pady=(0, 8))
        btns = ttk.Frame(d, style="Card.TFrame")
        btns.pack(fill="x", pady=(0, 10))
        self.b_print = ttk.Button(btns, text="Print", style="Accent.TButton", command=self.print_selected)
        self.b_print.pack(side="left")
        ttk.Button(btns, text="Sort", command=self.sort_selected).pack(side="left", padx=6)
        ttk.Button(btns, text="Open folder", command=self.open_folder).pack(side="left")
        ttk.Label(d, text="FILES", style="CardMuted.TLabel").pack(anchor="w")
        self.files = ttk.Treeview(d, columns=("name", "size"), show="headings", height=4, selectmode="browse")
        self.files.heading("name", text="Name")
        self.files.heading("size", text="Size")
        self.files.column("name", width=230)
        self.files.column("size", width=70, anchor="e", stretch=False)
        self.files.pack(fill="x", pady=(2, 10))
        self.files.bind("<Double-1>", self._open_file)
        ttk.Label(d, text="MESSAGE", style="CardMuted.TLabel").pack(anchor="w")
        self.body = tk.Text(d, height=10, width=40, wrap="word", state="disabled", bg=P["surface2"], padx=10, pady=8,
                            highlightthickness=0)
        self.body.pack(fill="both", expand=True, pady=(2, 0))

        foot = ttk.Frame(self.frame, padding=(28, 0, 28, 10))
        foot.pack(fill="x")
        self.count_lbl = ttk.Label(foot, style="Muted.TLabel")
        self.count_lbl.pack(side="left")
        self._sort_col, self._sort_rev = "when", True

    # ------------------------------------------------------------------ data
    def _placeholder(self, entry, text):
        entry._ph = text
        entry._ph_on = True
        entry.insert(0, text)
        entry.configure(foreground=P["faint"])

        def fin(_e):
            if entry._ph_on:
                entry._ph_on = False
                entry.delete(0, "end")
                entry.configure(foreground=P["text"])

        def fout(_e):
            if not entry.get():
                entry._ph_on = True
                entry.insert(0, text)
                entry.configure(foreground=P["faint"])
        entry.bind("<FocusIn>", fin)
        entry.bind("<FocusOut>", fout)
        self._search_entry = entry

    def _query_text(self):
        e = self._search_entry
        return "" if getattr(e, "_ph_on", False) else self.q.get().strip()

    def _debounce(self):
        if self._search_job:
            self.frame.after_cancel(self._search_job)
        self._search_job = self.frame.after(250, self.reload)

    def open_lib(self):
        root = self.app.library_root()
        if self.lib is not None and self.lib_root == root:
            return self.lib
        if self.lib is not None:
            self.lib.close()
            self.lib = None
        if not os.path.isdir(root):
            return None
        try:
            self.lib = Library(root)
            self.lib_root = root
        except Exception as e:
            self.app.log("Could not open the library at %s: %s" % (root, e), "error")
            self.lib = None
        return self.lib

    def mark_dirty(self):
        self.dirty = True
        if self.app.current == "library":
            self.reload()

    def on_show(self):
        if self.dirty or self.lib_root != self.app.library_root():
            self.reload()

    def reload(self):
        self.dirty = False
        lib = self.open_lib()
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        if lib is None:
            self._empty("Nothing here yet.\nFetch some mail and it will show up here.")
            self.count_lbl.configure(text="")
            return
        groups = lib.groups_in_use()
        self.group_cb.configure(values=[ALL, UNSORTED, UNMATCHED] + groups)
        if self.group.get() not in [ALL, UNSORTED, UNMATCHED] + groups:
            self.group.set(ALL)
        g = {ALL: None, UNSORTED: "", UNMATCHED: "*unmatched*"}.get(self.group.get(), self.group.get())
        start = end = None
        try:
            if self.d_from.get().strip():
                start = datetime.strptime(self.d_from.get().strip(), timeutil.DATE_FMT).strftime("%Y-%m-%d 00:00:00")
            if self.d_to.get().strip():
                end = datetime.strptime(self.d_to.get().strip(), timeutil.DATE_FMT).strftime("%Y-%m-%d 23:59:59")
        except ValueError:
            messagebox.showwarning(APP_NAME, "Dates look like 01-Aug-2026.", parent=self.app.root)
            return
        rows = lib.query(start=start, end=end, text=self._query_text() or None, group=g)
        if self.only_ids is not None:
            keep = set(self.only_ids)
            rows = [r for r in rows if r["id"] in keep]
            self.filter_lbl.configure(text="Showing the %d email(s) from the last fetch." % len(rows))
            self.filter_note.pack(fill="x", after=self.frame.winfo_children()[0])
        else:
            self.filter_note.pack_forget()
        counts = lib.attachment_counts([r["id"] for r in rows])
        rows = self._sorted(rows, counts)
        for n, r in enumerate(rows):
            self.rows[str(r["id"])] = r
            grp = r["group_name"] or ("unmatched" if r["sorted_at"] else "")
            tags = (["alt"] if n % 2 else []) + (["unmatched"] if r["sorted_at"] and not r["group_name"] else [])
            self.tree.insert("", "end", iid=str(r["id"]), tags=tags, values=(
                (r["received_local"] or "")[:16], r["sender_name"] or r["sender_email"] or "",
                r["subject"] or "(no subject)", counts.get(r["id"], "") or "", grp))
        if rows:
            self.empty.place_forget()
        else:
            self._empty("No emails match." if lib.stats().get("total") else
                        "Nothing here yet.\nFetch some mail and it will show up here.")
        st = lib.stats()
        self.count_lbl.configure(text="%d shown  ·  %d in library  ·  %d sorted  ·  %s" % (
            len(rows), st.get("total") or 0, st.get("sorted") or 0, lib.root))
        self.show_detail()

    def _empty(self, text):
        self.empty.configure(text=text)
        self.empty.place(relx=0.5, rely=0.4, anchor="center")

    def _sorted(self, rows, counts):
        key = {"when": lambda r: r["received_local"] or "", "sender": lambda r: (r["sender_name"] or
                                                                              r["sender_email"] or "").lower(),
               "subject": lambda r: (r["subject"] or "").lower(), "att": lambda r: counts.get(r["id"], 0),
               "group": lambda r: r["group_name"] or ""}[self._sort_col]
        return sorted(rows, key=key, reverse=self._sort_rev)

    def sort_by(self, col):
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col, self._sort_rev = col, col == "when"
        self.reload()

    def show_ids(self, ids):
        self.only_ids = list(ids)
        self.group.set(ALL)
        self.app.show("library")
        self.reload()

    def clear_only(self):
        self.only_ids = None
        self.reload()

    # ------------------------------------------------------------------ details
    def selected(self):
        return [self.rows[i] for i in self.tree.selection() if i in self.rows]

    def show_detail(self):
        sel = self.selected()
        self.files.delete(*self.files.get_children())
        self.body.configure(state="normal")
        self.body.delete("1.0", "end")
        if len(sel) != 1:
            self.d_subject.configure(text="%d emails selected" % len(sel) if sel else "")
            self.d_meta.configure(text="" if sel else "Select an email to see its details.")
            self.d_group.configure(text="")
            self.body.configure(state="disabled")
            return
        r = sel[0]
        self.d_subject.configure(text=r["subject"] or "(no subject)")
        frm = "%s <%s>" % (r["from_name"], r["from_email"]) if r["from_name"] else (r["from_email"] or "")
        meta = "From: %s" % frm
        if r["reply_email"] and r["reply_email"] != r["from_email"]:
            meta += "\nReply-To: %s" % r["reply_email"]
        meta += "\nTo: %s\nReceived: %s%s" % (r["to_addrs"] or "", r["received_local"] or "?",
                                             "" if r["time_source"] == "received" else "  (from Date: header)")
        self.d_meta.configure(text=meta)
        if r["group_name"]:
            self.d_group.configure(text="Group: %s   (%s: %s)" % (r["group_name"], r["group_stage"] or "",
                                                              r["group_term"] or ""), foreground=P["accent"])
        elif r["sorted_at"]:
            self.d_group.configure(text="Unmatched: %s" % (r["sort_reason"] or ""), foreground=P["warn"])
        else:
            self.d_group.configure(text="Not sorted yet.", foreground=P["muted"])
        lib = self.lib
        if r["emailinfo"]:
            p = lib.abs(r["emailinfo"])
            self.files.insert("", "end", iid="info", values=(os.path.basename(p) + "  (email print)",
                                                             human_size(_size(p))))
        for a in lib.attachments(r["id"]):
            p = lib.abs(a["path"]) if a["path"] else None
            self.files.insert("", "end", iid="a%d" % a["id"], values=(
                a["filename"] + ("" if p and os.path.exists(p) else "  (not downloaded)"), human_size(a["size"])))
        self.body.insert("1.0", (r["body"] or "(no text)")[:20000])
        self.body.configure(state="disabled")

    def _file_path(self, iid):
        r = self.selected()[0] if len(self.selected()) == 1 else None
        if r is None:
            return None
        if iid == "info":
            return self.lib.abs(r["emailinfo"])
        aid = int(iid[1:])
        for a in self.lib.attachments(r["id"]):
            if a["id"] == aid:
                return self.lib.abs(a["path"]) if a["path"] else None
        return None

    def _open_file(self, _e=None):
        sel = self.files.selection()
        if not sel:
            return
        p = self._file_path(sel[0])
        if p and os.path.exists(p):
            open_in_file_manager(p)
        else:
            self.app.log("That attachment was not downloaded (fetch with 'Save attachments' to get it).", "warn")

    def open_folder(self):
        sel = self.selected()
        if not sel:
            return
        r = sel[0]
        p = self.lib.abs(r["folder"]) if r["folder"] else None
        if p and os.path.isdir(p):
            open_in_file_manager(p)
        else:
            self.app.log("Nothing was saved to disk for that email.", "warn")

    def _menu(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        m = tk.Menu(self.tree, tearoff=0)
        m.add_command(label="Print", command=self.print_selected)
        m.add_command(label="Sort", command=self.sort_selected)
        m.add_command(label="Open folder", command=self.open_folder)
        m.add_separator()
        m.add_command(label="Clear group", command=self.clear_group)
        m.add_command(label="Remove from index (keeps files)", command=self.forget)
        m.tk_popup(e.x_root, e.y_root)

    # ------------------------------------------------------------------ actions
    def files_for(self, rows):
        out, skipped = [], 0
        for r in rows:
            p = self.lib.abs(r["folder"]) if r["folder"] else None
            files = ordered_print_files(p) if p and os.path.isdir(p) else []
            if files:
                out += files
            else:
                skipped += 1
        return out, skipped

    def print_selected(self):
        sel = self.selected()
        if not sel:
            self.app.log("Select one or more emails first.", "warn")
            return
        files, skipped = self.files_for(sorted(sel, key=lambda r: (r["received_local"] or "", r["id"])))
        if skipped:
            self.app.log("%d email(s) had nothing saved on disk to print." % skipped, "warn")
        if files:
            self.app.show("print")
            self.app.views["print"].add_paths(files)

    def print_ids(self, ids):
        if self.open_lib() is None:
            return
        rows = [r for r in (self.lib.message(i) for i in ids) if r is not None]
        files, skipped = self.files_for(rows)
        if skipped:
            self.app.log("%d email(s) had nothing saved on disk to print." % skipped, "warn")
        if files:
            self.app.show("print")
            self.app.views["print"].add_paths(files)

    def sort_selected(self):
        sel = self.selected()
        if sel:
            self.app.views["sort"].run_library(ids=[r["id"] for r in sel])

    def clear_group(self):
        sel = self.selected()
        if sel:
            self.lib.clear_groups([r["id"] for r in sel])
            self.reload()

    def forget(self):
        sel = self.selected()
        if sel and messagebox.askyesno(APP_NAME, "Remove %d email(s) from the index? Files on disk are kept; "
                                                 "fetching again re-adds them." % len(sel), parent=self.app.root):
            for r in sel:
                self.lib.forget_message(r["id"])
            self.reload()

    def on_job_state(self, job):
        if job.kind in ("fetch", "sort") and job.state != "running":
            self.mark_dirty()


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return None
