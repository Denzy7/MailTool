"""Sort screen: groups & keywords, attachment filters, and running a sort."""
from __future__ import annotations

import json
import os
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from mailtool import APP_NAME
from mailtool.core import timeutil
from mailtool.core.util import open_in_file_manager
from mailtool.library.db import LookupCache
from mailtool.sort import sorter
from mailtool.sort.groups import split_keyword_lines
from mailtool.ui.app import View
from mailtool.ui.theme import P
from mailtool.ui.widgets import Card, DateRange, Form, PathEntry, ScrollPage, bool_var, str_var


class SortView(View):
    title = "Sort"
    subtitle = "Put emails into groups by keywords found in the subject, body or attached documents."

    def __init__(self, app):
        super().__init__(app)
        s = self.cfg["sort"]
        self.groups = [dict(g) for g in s.get("groups") or []]
        self.v = {
            "whole_word": bool_var(s.get("whole_word", True)), "case_sensitive": bool_var(s.get("case_sensitive")),
            "precedence": str_var(s.get("precedence") or "keywords_first"),
            "dedupe": bool_var(s.get("dedupe")), "input": str_var(s.get("input") or "library"),
            "library_from": str_var(s.get("library_from")), "library_to": str_var(s.get("library_to")),
            "csv_path": str_var(s.get("csv_path")), "out_dir": str_var(s.get("out_dir")),
            "fallback_source": str_var(s.get("fallback_source") or "imap"),
            "local_folder": str_var(s.get("local_folder")),
            "search_attachments": bool_var(s.get("search_attachments", True)), "offline": bool_var(s.get("offline")),
            "use_cache": bool_var(s.get("use_cache", True)),
            "date_window_days": str_var(s.get("date_window_days", 1)),
            "timestamp_window_seconds": str_var(s.get("timestamp_window_seconds", 60)),
        }
        self.last = None

        act = ttk.Frame(self.frame, padding=(28, 0, 28, 12))
        act.pack(side="bottom", fill="x")
        self.go = ttk.Button(act, text="Sort now", style="Accent.TButton", command=self.run)
        self.go.pack(side="right")
        self.result = ttk.Label(act, style="Muted.TLabel")
        self.result.pack(side="left")
        self.after_btns = ttk.Frame(act)
        self.after_btns.pack(side="left", padx=10)
        self.input_lbl = ttk.Label(act, style="Muted.TLabel")
        self.input_lbl.pack(side="right", padx=12)
        nb = self.nb = ttk.Notebook(self.frame)
        nb.pack(fill="both", expand=True, padx=28, pady=(6, 10))
        self._build_groups(nb)
        self._build_filters(nb, s)
        self._build_run(nb)
        self.refresh_tree()
        self._sync()
        for k in ("library_from", "library_to", "csv_path"):
            self.v[k].trace_add("write", lambda *_: self._sync())

    # ================================================================== groups tab
    def _build_groups(self, nb):
        tab = ttk.Frame(nb, style="Card.TFrame", padding=16)
        nb.add(tab, text="  Groups  ")
        left = ttk.Frame(tab, style="Card.TFrame")
        left.pack(side="left", fill="both", expand=True)
        bar = ttk.Frame(left, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="First matching group wins - order matters.", style="CardMuted.TLabel").pack(side="left")
        ttk.Button(bar, text="Export…", style="Small.TButton", command=self.export_groups).pack(side="right")
        ttk.Button(bar, text="Import…", style="Small.TButton", command=self.import_groups).pack(side="right",
                                                                                                padx=6)
        ttk.Button(bar, text="↓", width=3, style="Small.TButton", command=lambda: self.move(1)).pack(side="right")
        ttk.Button(bar, text="↑", width=3, style="Small.TButton", command=lambda: self.move(-1)).pack(
            side="right", padx=(0, 4))
        cols = ("group", "desc", "kw", "body")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c, t, w in (("group", "Group name", 140), ("desc", "Descriptive name", 150),
                        ("kw", "Keywords (attachments + text)", 220), ("body", "Text-only keywords", 160)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.tag_configure("alt", background=P["row_alt"])

        right = ttk.Frame(tab, style="Card.TFrame", padding=(20, 0, 0, 0))
        right.pack(side="left", fill="y")
        ttk.Label(right, text="Group name", style="Card.TLabel").pack(anchor="w")
        self.e_name = ttk.Entry(right, width=36)
        self.e_name.pack(fill="x", pady=(2, 2))
        ttk.Label(right, text="e.g. G1/1/1/2026/01", style="CardMuted.TLabel").pack(anchor="w")
        ttk.Label(right, text="Keywords", style="Card.TLabel").pack(anchor="w", pady=(12, 2))
        ttk.Label(right, style="CardMuted.TLabel", justify="left", wraplength=300, text=(
            "Line 1: descriptive name (goes in the CSVs).\n"
            "Line 2: comma-separated keywords, e.g. jan, january.\n"
            "Line 3+: one keyword per line, subject/body only.\n"
            "Lines 1-2 are also searched in attachments.")).pack(anchor="w")
        self.t_kw = tk.Text(right, width=36, height=7, wrap="word", padx=8, pady=6)
        self.t_kw.pack(fill="x", pady=6)
        b = ttk.Frame(right, style="Card.TFrame")
        b.pack(fill="x")
        ttk.Button(b, text="Save group", style="Accent.TButton", command=self.add_group).pack(side="left")
        ttk.Button(b, text="New", command=self.clear_form).pack(side="left", padx=6)
        ttk.Button(b, text="Delete", style="Danger.TButton", command=self.delete_group).pack(side="left")

        opt = ttk.LabelFrame(right, text="Matching", padding=10)
        opt.pack(fill="x", pady=(18, 0))
        ttk.Checkbutton(opt, text="Whole-word match", variable=self.v["whole_word"]).pack(anchor="w")
        ttk.Checkbutton(opt, text="Case sensitive", variable=self.v["case_sensitive"]).pack(anchor="w")
        ttk.Radiobutton(opt, text="Keywords first, then group name", variable=self.v["precedence"],
                        value="keywords_first").pack(anchor="w", pady=(6, 0))
        ttk.Radiobutton(opt, text="Group name first, then keywords", variable=self.v["precedence"],
                        value="groupname_first").pack(anchor="w")

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, g in enumerate(self.groups):
            d, kws, extra = split_keyword_lines(g.get("keywords", []))
            self.tree.insert("", "end", iid=str(i), tags=("alt",) if i % 2 else (),
                             values=(g.get("group_name", ""), d, ", ".join(kws), " | ".join(extra)))
        self.app.set_badge("sort", str(len(self.groups)) if self.groups else "")

    def on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        g = self.groups[int(sel[0])]
        self.e_name.delete(0, "end")
        self.e_name.insert(0, g.get("group_name", ""))
        self.t_kw.delete("1.0", "end")
        self.t_kw.insert("1.0", "\n".join(g.get("keywords", [])))

    def clear_form(self):
        self.e_name.delete(0, "end")
        self.t_kw.delete("1.0", "end")
        self.tree.selection_remove(self.tree.selection())
        self.e_name.focus_set()

    def add_group(self):
        name = self.e_name.get().strip()
        kws = [ln.strip() for ln in self.t_kw.get("1.0", "end").splitlines() if ln.strip()]
        if not name:
            messagebox.showwarning(APP_NAME, "A group needs a name.", parent=self.app.root)
            return
        if not kws:
            messagebox.showwarning(APP_NAME, "Enter the keywords: line 1 is the descriptive name, line 2 the "
                                             "comma-separated keywords.", parent=self.app.root)
            return
        entry = {"group_name": name, "keywords": kws}
        sel = self.tree.selection()
        idx = int(sel[0]) if sel else next((i for i, g in enumerate(self.groups) if g["group_name"] == name), None)
        if idx is None:
            self.groups.append(entry)
            idx = len(self.groups) - 1
        else:
            self.groups[idx] = entry
        self.save()
        self.cfg.save()
        self.refresh_tree()
        self.tree.selection_set(str(idx))
        self.tree.see(str(idx))

    def delete_group(self):
        sel = self.tree.selection()
        if not sel:
            return
        i = int(sel[0])
        if messagebox.askyesno(APP_NAME, "Delete group %r?" % self.groups[i]["group_name"], parent=self.app.root):
            del self.groups[i]
            self.save()
            self.cfg.save()
            self.refresh_tree()
            self.clear_form()

    def move(self, d):
        sel = self.tree.selection()
        if not sel:
            return
        i = int(sel[0])
        j = i + d
        if 0 <= j < len(self.groups):
            self.groups[i], self.groups[j] = self.groups[j], self.groups[i]
            self.refresh_tree()
            self.tree.selection_set(str(j))
            self.save()

    def export_groups(self):
        p = filedialog.asksaveasfilename(parent=self.app.root, title="Export groups", defaultextension=".json",
                                         initialfile="mailtool_groups.json", filetypes=[("JSON", "*.json")])
        if p:
            self.save()
            data = {"groups": self.groups, "excluded_attachments": self.cfg["sort"]["excluded"],
                    "included_attachments": self.cfg["sort"]["included"]}
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.app.log("Exported %d group(s) to %s" % (len(self.groups), p), "ok")

    def import_groups(self):
        p = filedialog.askopenfilename(parent=self.app.root, title="Import groups (MailTool or Email Grouper JSON)",
                                       filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            groups = data.get("groups") if isinstance(data, dict) else data
            groups = [{"group_name": g["group_name"], "keywords": list(g.get("keywords") or [])}
                      for g in groups if isinstance(g, dict) and g.get("group_name")]
        except Exception as e:
            messagebox.showerror(APP_NAME, "Could not read groups from that file:\n%s" % e, parent=self.app.root)
            return
        if not groups:
            messagebox.showinfo(APP_NAME, "No groups found in that file.", parent=self.app.root)
            return
        replace = not self.groups or messagebox.askyesno(
            APP_NAME, "Replace your %d group(s) with the %d imported?\n\nNo = add them to the end."
            % (len(self.groups), len(groups)), parent=self.app.root)
        self.groups = groups if replace else self.groups + [g for g in groups if g["group_name"] not in
                                                           {x["group_name"] for x in self.groups}]
        if isinstance(data, dict):
            for key, lb in (("excluded_attachments", self.ex_list), ("included_attachments", self.in_list)):
                for pat in data.get(key) or []:
                    if pat not in lb.get(0, "end"):
                        lb.insert("end", pat)
        self.save()
        self.cfg.save()
        self.refresh_tree()
        self.app.log("Imported %d group(s) from %s" % (len(groups), p), "ok")

    # ================================================================== filters tab
    def _build_filters(self, nb, s):
        tab = ttk.Frame(nb, style="Card.TFrame", padding=16)
        nb.add(tab, text="  Attachment filters  ")
        ttk.Label(tab, style="CardMuted.TLabel", wraplength=900, justify="left", text=(
            "Attachments matching an EXCLUDE pattern are skipped when searching for keywords. A file matching an "
            "INCLUDE pattern is always searched, even if it is excluded - e.g. exclude 'notes' but include "
            "'notes_reports*'. Case-insensitive; plain text matches anywhere in the name, * and ? are "
            "wildcards.")).pack(anchor="w", pady=(0, 12))
        row = ttk.Frame(tab, style="Card.TFrame")
        row.pack(fill="both", expand=True)
        self.ex_list = self._pattern_panel(row, "Exclude", s.get("excluded") or [])
        self.in_list = self._pattern_panel(row, "Include (overrides exclude)", s.get("included") or [])

    def _pattern_panel(self, parent, title, items):
        f = ttk.LabelFrame(parent, text=title, padding=10)
        f.pack(side="left", fill="both", expand=True, padx=(0, 12))
        lb = tk.Listbox(f, height=10, selectmode="extended", activestyle="none")
        lb.pack(fill="both", expand=True)
        for p in items:
            lb.insert("end", p)
        r = ttk.Frame(f, style="Card.TFrame")
        r.pack(fill="x", pady=(8, 0))
        e = ttk.Entry(r)
        e.pack(side="left", fill="x", expand=True)

        def add(_e=None):
            pat = e.get().strip()
            if pat and pat.lower() not in [x.lower() for x in lb.get(0, "end")]:
                lb.insert("end", pat)
                self.save()
            e.delete(0, "end")

        def remove():
            for i in reversed(lb.curselection()):
                lb.delete(i)
            self.save()
        e.bind("<Return>", add)
        ttk.Button(r, text="Add", command=add).pack(side="left", padx=6)
        ttk.Button(r, text="Remove", command=remove).pack(side="left")
        return lb

    # ================================================================== run tab
    def _build_run(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  Run  ")
        page = ScrollPage(tab)
        page.pack(fill="both", expand=True)
        page.content.configure(padding=(0, 12, 12, 12))
        c = page.content

        card = Card(c, "Emails to sort")
        card.pack(fill="x", pady=(0, 12))
        b = card.body
        ttk.Radiobutton(b, text="From the library", variable=self.v["input"], value="library",
                        command=self._sync).pack(anchor="w")
        self.lib_box = ttk.Frame(b, style="Card.TFrame", padding=(26, 4, 0, 8))
        self.lib_box.pack(fill="x")
        self._t1, self._t2 = tk.StringVar(), tk.StringVar()
        self.lib_range = DateRange(self.lib_box, self.v["library_from"], self._t1, self.v["library_to"], self._t2,
                                   times=False, allow_blank=True)
        self.lib_range.pack(anchor="w")
        ttk.Label(self.lib_box, text="Leave the dates empty to sort everything. Every email is matched by its exact "
                                     "UID, so nothing has to be searched for.", style="CardMuted.TLabel").pack(
            anchor="w", pady=(6, 0))
        ttk.Radiobutton(b, text="From a CSV file  (Sender, Email, Date Received, Subject, Body)",
                        variable=self.v["input"], value="csv", command=self._sync).pack(anchor="w", pady=(6, 0))
        self.csv_box = ttk.Frame(b, style="Card.TFrame", padding=(26, 4, 0, 0))
        self.csv_box.pack(fill="x")
        form = Form(self.csv_box, label_width=16)
        self.csv_pick = PathEntry(self.csv_box, self.v["csv_path"], kind="file", title="Choose the emails CSV",
                                  filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        form.add("CSV file", self.csv_pick)
        src = ttk.Frame(self.csv_box, style="Card.TFrame")
        self.rb_imap = ttk.Radiobutton(src, text="IMAP mailbox", variable=self.v["fallback_source"], value="imap",
                                       command=self._sync)
        self.rb_imap.pack(side="left")
        self.rb_local = ttk.Radiobutton(src, text="Folder of .eml files (e.g. extracted Zimbra export)",
                                        variable=self.v["fallback_source"], value="local", command=self._sync)
        self.rb_local.pack(side="left", padx=(14, 0))
        form.add("Find originals in", src, sticky="w",
                 hint="Rows with a UID column (MailTool's own CSV) are looked up exactly; the rest by "
                      "subject + sender + time.")
        self.local_pick = PathEntry(self.csv_box, self.v["local_folder"], kind="dir",
                                    title="Folder with .eml files (searched recursively)")
        form.add(".eml folder", self.local_pick)
        win = ttk.Frame(self.csv_box, style="Card.TFrame")
        ttk.Label(win, text="search ±", style="Card.TLabel").pack(side="left")
        self.e_days = ttk.Spinbox(win, from_=0, to=30, width=4, textvariable=self.v["date_window_days"])
        self.e_days.pack(side="left")
        ttk.Label(win, text="days, match time ±", style="Card.TLabel").pack(side="left", padx=(4, 0))
        self.e_secs = ttk.Spinbox(win, from_=0, to=86400, increment=30, width=7,
                                  textvariable=self.v["timestamp_window_seconds"])
        self.e_secs.pack(side="left")
        ttk.Label(win, text="seconds", style="Card.TLabel").pack(side="left", padx=4)
        form.add("Lookup window", win, sticky="w")
        cache = ttk.Frame(self.csv_box, style="Card.TFrame")
        ttk.Checkbutton(cache, text="Use the lookup cache", variable=self.v["use_cache"]).pack(side="left")
        ttk.Button(cache, text="Clear cache", style="Small.TButton", command=self.clear_cache).pack(side="left",
                                                                                               padx=10)
        self.cache_lbl = ttk.Label(cache, style="CardMuted.TLabel")
        self.cache_lbl.pack(side="left")
        form.add("Cache", cache, sticky="w")
        self.cb_offline = ttk.Checkbutton(self.csv_box, text="Offline: never contact the mailbox, use cached "
                                                             "lookups only", variable=self.v["offline"])
        form.full(self.cb_offline)

        card = Card(c, "Options")
        card.pack(fill="x", pady=(0, 12))
        form = Form(card.body, label_width=16)
        form.full(ttk.Checkbutton(card.body, text="Search attachments (.pdf, .docx, .doc) when subject/body don't "
                                                  "match", variable=self.v["search_attachments"]))
        form.full(ttk.Checkbutton(card.body, text="Also write matched_uniq.csv / unmatched_uniq.csv (one row per "
                                                  "email address)", variable=self.v["dedupe"]))
        form.add("Reports folder", PathEntry(card.body, self.v["out_dir"], kind="dir",
                                             title="Where to write matched.csv / unmatched.csv"),
                 hint="Empty = a 'reports' folder in the library (or next to the CSV).")


    def _sync(self):
        lib = self.v["input"].get() == "library"
        if lib:
            a, b = self.v["library_from"].get().strip(), self.v["library_to"].get().strip()
            what = "the library" + (" (%s to %s)" % (a or "start", b or "now") if a or b else "")
        else:
            what = os.path.basename(self.v["csv_path"].get()) or "a CSV file (choose it on the Run tab)"
        self.input_lbl.configure(text="Sorts %s" % what)
        for w in self.lib_range.entries:
            w.state(["!disabled"] if lib else ["disabled"])
        for w in (self.csv_pick, self.local_pick):
            w.state(["disabled"] if lib else ["!disabled"])
        if not lib:
            self.local_pick.state(["!disabled"] if self.v["fallback_source"].get() == "local" else ["disabled"])
        for w in (self.rb_imap, self.rb_local, self.e_days, self.e_secs, self.cb_offline):
            w.state(["disabled"] if lib else ["!disabled"])
        self._cache_info()

    def _cache_info(self):
        try:
            c = LookupCache()
            self.cache_lbl.configure(text="%d lookups cached" % c.count())
            c.close()
        except Exception:
            self.cache_lbl.configure(text="")

    def clear_cache(self):
        if messagebox.askyesno(APP_NAME, "Forget all cached CSV lookups?", parent=self.app.root):
            c = LookupCache()
            c.clear()
            c.close()
            self._cache_info()
            self.app.log("Lookup cache cleared.", "ok")

    # ================================================================== state / run
    def save(self):
        d = {k: v.get() for k, v in self.v.items()}
        for k, default in (("date_window_days", 1), ("timestamp_window_seconds", 60)):
            try:
                d[k] = max(0, int(float(d[k])))
            except (TypeError, ValueError):
                d[k] = default
        d["groups"] = self.groups
        d["excluded"] = list(self.ex_list.get(0, "end"))
        d["included"] = list(self.in_list.get(0, "end"))
        self.cfg.update("sort", d)

    def on_show(self):
        self._cache_info()

    def run_library(self, ids=None):
        """Called from Library / Fetch: sort these emails (or the configured range)."""
        self.save()
        if not self.groups:
            self.app.show("sort")
            self.nb.select(0)
            messagebox.showinfo(APP_NAME, "Add at least one group first.", parent=self.app.root)
            return
        pw = self.app.get_password(prompt=False)   # only needed for attachments never downloaded
        self.app.start_job("sort", "Sorting %s" % ("%d email(s)" % len(ids) if ids else "the library"),
                           sorter.run_sort_library, on_done=self._done, library_root=self.app.library_root(),
                           sopts=self.cfg.snapshot("sort"), account=self.app.account(), password=pw,
                           tz_text=self.app.tz_text(), ids=ids)
        self.go.state(["disabled"])

    def run(self):
        self.save()
        self.cfg.save()
        s = self.cfg.snapshot("sort")
        if not self.groups:
            messagebox.showwarning(APP_NAME, "Add at least one group first.", parent=self.app.root)
            self.nb.select(0)
            return
        if s["input"] == "library":
            for k in ("library_from", "library_to"):
                if s[k].strip():
                    try:
                        datetime.strptime(s[k].strip(), timeutil.DATE_FMT)
                    except ValueError:
                        messagebox.showerror(APP_NAME, "Dates look like 01-Aug-2026.", parent=self.app.root)
                        return
            self.run_library()
            return
        if not os.path.isfile(s["csv_path"]):
            messagebox.showwarning(APP_NAME, "Choose the CSV file to sort.", parent=self.app.root)
            return
        pw = ""
        needs_mail = s["search_attachments"] and not s["offline"]
        if needs_mail and s["fallback_source"] == "local":
            if not os.path.isdir(s["local_folder"]):
                messagebox.showwarning(APP_NAME, "Choose the folder with the .eml files.", parent=self.app.root)
                return
        elif needs_mail:
            if not self.app.require_account():
                return
            pw = self.app.get_password()
            if not pw:
                return
        self.go.state(["disabled"])
        self.app.start_job("sort", "Sorting %s" % os.path.basename(s["csv_path"]), sorter.run_sort_csv,
                           on_done=self._done, csv_path=s["csv_path"], out_dir=s["out_dir"] or None, sopts=s,
                           account=self.app.account(), password=pw, tz_text=self.app.tz_text(),
                           library_root=self.app.library_root())

    def _done(self, job):
        self.go.state(["!disabled"])
        for w in self.after_btns.winfo_children():
            w.destroy()
        r = job.result
        self._cache_info()
        if not r:
            self.result.configure(text="Sort %s - see the log." % job.state)
            return
        self.result.configure(text="%d matched, %d unmatched." % (r["matched"], r["unmatched"]))
        if r["paths"].get("matched.csv"):
            ttk.Button(self.after_btns, text="Open matched.csv", style="Small.TButton",
                       command=lambda: open_in_file_manager(r["paths"]["matched.csv"])).pack(side="left", padx=2)
        ttk.Button(self.after_btns, text="Open reports folder", style="Small.TButton",
                   command=lambda: open_in_file_manager(r["out_dir"])).pack(side="left", padx=2)
        self.app.views["library"].mark_dirty()

    def on_job_state(self, job):
        if job.kind == "sort" and job.state != "running":
            self.go.state(["!disabled"])
