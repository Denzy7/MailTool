"""Print screen: drag & drop queue of PDFs, Word documents and images.
Everything is copied locally, turned into PDF, and printed as one job (default)
or file by file."""
from __future__ import annotations

import os
import shutil
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from mailtool import APP_NAME
from mailtool.core.util import log
from mailtool.printing.backends import get_backend
from mailtool.printing.pdfops import UserError
from mailtool.printing.queue import ACTIVE, Item, PrintCore, merge_sig
from mailtool.printing.resolve import classify, expand_folder, parse_drop
from mailtool.ui.app import View
from mailtool.ui.pages import PageDialog
from mailtool.ui.theme import P
from mailtool.ui.widgets import register_drop

SYSTEM_DEFAULT = "(system default)"


class PrintView(View):
    title = "Print"
    subtitle = "Drop PDFs, Word documents, images or whole email folders. They print in this order."

    def __init__(self, app):
        super().__init__(app)
        self.pcfg = self.cfg["print"]
        self.core = PrintCore(self.pcfg, lambda ev: app.call_ui(self._core_event, ev), hint=app.hint)
        self.backend = get_backend(self.pcfg)
        self.items, self.keys = {}, {}
        self.printing = False
        self.print_job = None
        self.job_ids = []
        self._drag = None
        self.pre = self._no_pre()
        self.pre_thread = None
        self._sig_seen, self._sig_time, self._pre_fail = None, 0.0, None

        top = ttk.Frame(self.frame, padding=(28, 6, 28, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Printer").pack(side="left")
        self.printer_var = tk.StringVar(value=SYSTEM_DEFAULT)
        self.printer_cb = ttk.Combobox(top, textvariable=self.printer_var, state="readonly", values=[SYSTEM_DEFAULT],
                                       width=36)
        self.printer_cb.pack(side="left", padx=8)
        self.printer_cb.bind("<<ComboboxSelected>>", self._printer_changed)
        ttk.Button(top, text="Refresh", style="Small.TButton", command=self.load_printers).pack(side="left")
        ttk.Button(top, text="Add folder…", command=self.add_folder_dialog).pack(side="right")
        ttk.Button(top, text="Add files…", command=self.add_dialog).pack(side="right", padx=6)

        # bottom rows are packed before the table so a short window shrinks the table, not the buttons
        foot = ttk.Frame(self.frame, padding=(28, 10, 28, 14))
        foot.pack(side="bottom", fill="x")
        self.merge_var = tk.BooleanVar(value=bool(self.pcfg.get("merge_batch", True)))
        ttk.Checkbutton(foot, text="One print job (merge everything)", variable=self.merge_var, style="Bg.TCheckbutton",
                        command=lambda: self.pcfg.update(merge_batch=bool(self.merge_var.get()))).pack(side="left")
        self.clear_var = tk.BooleanVar(value=bool(self.pcfg.get("clear_after")))
        ttk.Checkbutton(foot, text="Clear list after printing", variable=self.clear_var, style="Bg.TCheckbutton",
                        command=lambda: self.pcfg.update(clear_after=bool(self.clear_var.get()))).pack(side="left",
                                                                                                     padx=16)
        self.b_print = ttk.Button(foot, text="Print", style="Accent.TButton", command=self.start_print)
        self.b_print.pack(side="right")
        self.b_cancel = ttk.Button(foot, text="Cancel", command=self.cancel_print, state="disabled")
        self.b_cancel.pack(side="right", padx=8)
        self.prep_lbl = ttk.Label(foot, text="", style="Muted.TLabel")
        self.prep_lbl.pack(side="right", padx=8)

        bar = ttk.Frame(self.frame, padding=(28, 8, 28, 0))
        bar.pack(side="bottom", fill="x")
        self.btns = []
        for text, cmd in (("Up", lambda: self.move(-1)), ("Down", lambda: self.move(1)),
                          ("Sort by name", self.sort_names), ("Pages…", self.open_pages),
                          ("Copies…", self.set_copies), ("Remove", self.remove_selected), ("Clear", self.clear)):
            b = ttk.Button(bar, text=text, style="Small.TButton", command=cmd)
            b.pack(side="left", padx=(0, 4))
            self.btns.append(b)

        mid = tk.Frame(self.frame, bg=P["surface"], highlightthickness=1, highlightbackground=P["border"])
        mid.pack(fill="both", expand=True, padx=28)
        cols = ("name", "status", "pages", "copies")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse", height=7)
        for c, t, w in (("name", "File", 340), ("status", "Status", 300), ("pages", "Pages", 70),
                        ("copies", "Copies", 70)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=c in ("name", "status"),
                             anchor="w" if c in ("name", "status") else "center")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("failed", foreground=P["danger"])
        self.tree.tag_configure("done", foreground=P["ok"])
        self.tree.bind("<ButtonPress-1>", self._press)
        self.tree.bind("<B1-Motion>", self._motion)
        self.tree.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.tree.bind("<Double-1>", self._dbl)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Alt-Up>", lambda e: self.move(-1))
        self.tree.bind("<Alt-Down>", lambda e: self.move(1))
        self.drop_hint = tk.Label(mid, text="Drop files or folders here\n\nor use Add files…  ·  "
                                            "Library › Print", bg=P["surface"], fg=P["faint"],
                                  font="MT.H2", justify="center")
        if app.dnd:
            register_drop(self.tree, self.on_drop)
            register_drop(self.drop_hint, self.on_drop)

        self._update_empty()
        self.load_printers()

    # ------------------------------------------------------------------ printers
    def load_printers(self):
        if not self.backend:
            return
        threading.Thread(target=self._load_printers, daemon=True).start()

    def _load_printers(self):
        try:
            names, default = self.backend.printers()
        except Exception as e:
            log.warning("printer list failed: %s", e)
            names, default = [], None
        self.app.call_ui(self._set_printers, names, default)

    def _set_printers(self, names, default):
        self.printer_cb.configure(values=[SYSTEM_DEFAULT] + names)
        want = self.pcfg.get("printer")
        self.printer_var.set(want if want in names else SYSTEM_DEFAULT)
        if not names:
            self.app.log("No printers found.", "warn")
        elif default and self.printer_var.get() == SYSTEM_DEFAULT:
            self.app.log("System default printer: %s" % default)

    def selected_printer(self):
        v = self.printer_var.get()
        return None if v == SYSTEM_DEFAULT else v

    def _printer_changed(self, _e=None):
        self.pcfg["printer"] = self.selected_printer()

    def apply_settings(self):
        self.core.apply_settings()
        self.backend = get_backend(self.pcfg)
        self.load_printers()

    # ------------------------------------------------------------------ adding
    def add_dialog(self):
        if self.printing:
            return
        types = [("Printable files", "*.pdf *.doc *.docx *.odt *.rtf *.png *.jpg *.jpeg *.gif *.bmp *.tif *.tiff "
                                     "*.webp"), ("All files", "*.*")]
        paths = filedialog.askopenfilenames(parent=self.app.root, filetypes=types)
        if paths:
            self.add_paths(list(paths))

    def add_folder_dialog(self):
        if self.printing:
            return
        d = filedialog.askdirectory(parent=self.app.root, title="Add every file in a folder",
                                    initialdir=self.app.library_root())
        if d:
            self.add_paths([d])

    def on_drop(self, event):
        raws = parse_drop(self.app.root.tk, event.data)
        if not raws:
            self.app.log("The drop contained nothing usable. Run 'mailtool --probe' to see what your file "
                         "manager sends.", "warn")
            return "copy"
        index = None
        try:
            y = event.y_root - self.tree.winfo_rooty()
            row = self.tree.identify_row(y)
            if row:
                index = self.tree.index(row)
                bb = self.tree.bbox(row)
                if bb and y > bb[1] + bb[3] // 2:
                    index += 1
        except Exception:
            index = None
        self.add_paths(raws, index)
        return "copy"   # never let the file manager treat this as a move

    def add_paths(self, raws, index=None):
        if self.printing:
            self.app.log("Printing in progress - files not added.", "warn")
            return
        added = 0
        for raw in raws:
            src = classify(raw)
            if not src:
                self.app.log("Ignored (not a file path or WebDAV link): %s" % raw[:100], "warn")
                continue
            if src.kind == "path" and os.path.isdir(src.path):
                files = expand_folder(src.path)
                if not files:
                    self.app.log("%s: no files inside" % src.display, "warn")
                for f in files:
                    added += self._add_one(classify(f), None if index is None else index + added)
                continue
            added += self._add_one(src, None if index is None else index + added)
        self._update_empty()

    def _add_one(self, src, pos):
        if src is None:
            return 0
        if src.key in self.keys:
            self.app.log("%s: already in the queue" % src.display)
            return 0
        it = Item(src)
        self.items[it.id] = it
        self.keys[src.key] = it
        self.tree.insert("", "end" if pos is None else pos, iid=it.id, values=self._vals(it))
        self.core.submit(it)
        return 1

    def _vals(self, it):
        return (it.display, it.text(), it.pages_text(), it.copies)

    def update_row(self, it):
        if it.id not in self.items:
            return
        tags = ("failed",) if it.status == "failed" else (("done",) if it.status == "done" else ())
        self.tree.item(it.id, values=self._vals(it), tags=tags)
        if it.status == "failed" and not it.noted:
            it.noted = True
            self.app.log("%s: %s" % (it.display, it.msg), "error")

    def _core_event(self, ev):
        kind, it = ev
        if kind == "upd":
            self.update_row(it)
        elif kind == "dup":
            self.drop_row(it)
            self.app.log("%s: already in the queue (same content as %s)" % (it.display, it.msg))

    def drop_row(self, it):
        if it.id in self.items:
            self.tree.delete(it.id)
            del self.items[it.id]
        if self.keys.get(it.src.key) is it:
            del self.keys[it.src.key]
        self.core.forget(it)
        self._update_empty()

    def remove_selected(self):
        if self.printing:
            return
        for iid in self.tree.selection():
            self.drop_row(self.items[iid])

    def clear(self):
        if self.printing:
            return
        for it in list(self.items.values()):
            self.drop_row(it)

    def _update_empty(self):
        if self.items:
            self.drop_hint.place_forget()
        else:
            self.drop_hint.place(relx=0.5, rely=0.45, anchor="center")
        n = len([i for i in self.items.values() if i.status in ACTIVE])
        self.app.set_badge("print", str(n) if n else "")

    # ------------------------------------------------------------------ ordering
    def move(self, delta):
        if self.printing:
            return
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0]) + delta
        if 0 <= idx < len(self.tree.get_children()):
            self.tree.move(sel[0], "", idx)

    def sort_names(self):
        if self.printing:
            return
        ids = sorted(self.tree.get_children(), key=lambda i: self.items[i].display.lower())
        for n, i in enumerate(ids):
            self.tree.move(i, "", n)

    def _press(self, e):
        self._drag = self.tree.identify_row(e.y) or None

    def _motion(self, e):
        if not self._drag or self.printing:
            return
        target = self.tree.identify_row(e.y)
        if target and target != self._drag:
            self.tree.move(self._drag, "", self.tree.index(target))
            return "break"

    def _dbl(self, e):
        if self.printing:
            return
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        if self.tree.identify_column(e.x) == "#4":
            self.set_copies(iid)
        else:
            self.open_pages(iid)

    def set_copies(self, iid=None):
        if self.printing:
            return
        iid = iid if isinstance(iid, str) else (self.tree.selection() or [None])[0]
        if not iid or iid not in self.items:
            self.app.log("Select a file first.")
            return
        it = self.items[iid]
        n = simpledialog.askinteger("Copies", "Copies of %s:" % it.display, initialvalue=it.copies, minvalue=1,
                                    maxvalue=99, parent=self.app.root)
        if n:
            it.copies = n
            self.update_row(it)

    def open_pages(self, iid=None):
        if self.printing:
            return
        iid = iid if isinstance(iid, str) else (self.tree.selection() or [None])[0]
        if not iid or iid not in self.items:
            self.app.log("Select a file first.")
            return
        it = self.items[iid]
        if it.status != "ready" or not it.pdf:
            self.app.log("%s is not ready yet (%s)." % (it.display, it.status))
            return
        PageDialog(self, it)

    # ------------------------------------------------------------------ printing
    def start_print(self):
        if self.printing:
            return
        if not self.backend:
            messagebox.showerror(APP_NAME, "No way to print was found.\nInstall: %s" % self.app.hint("print"),
                                 parent=self.app.root)
            return
        todo = [self.items[i] for i in self.tree.get_children() if self.items[i].status in ACTIVE]
        if not todo:
            return
        self.printing = True
        self.job_ids = []
        printer = self.selected_printer()
        merged = bool(self.merge_var.get())
        self.print_job = self.app.start_job("print", "Printing %d file(s)" % len(todo),
                                            self._print_merged if merged else self._print_each,
                                            on_done=self._print_done, todo=todo, printer=printer)
        if self.print_job is None:
            self.printing = False
        self.refresh_buttons()

    @staticmethod
    def _no_pre():
        return {"sig": None, "dir": None, "path": None, "engine": None}

    def ready_snapshot(self):
        return [self.items[i] for i in self.tree.get_children() if self.items[i].status == "ready"]

    def _maybe_prebuild(self):
        """Build the merged PDF in the background once the queue has been stable a moment."""
        if self.printing or not self.backend or not self.merge_var.get():
            return
        if self.pre_thread is not None and self.pre_thread.is_alive():
            return
        items = self.ready_snapshot()
        sig = merge_sig(items) if items else None
        now = time.time()
        if sig != self._sig_seen:
            self._sig_seen, self._sig_time = sig, now
            return
        pending = any(i.status in ("queued", "staging", "converting") for i in self.items.values())
        if sig is None or pending or sig == self.pre["sig"] or sig == self._pre_fail or now - self._sig_time < 1.0:
            return
        self.pre_thread = threading.Thread(target=self._prebuild, args=(sig, items), daemon=True)
        self.pre_thread.start()

    def _prebuild(self, sig, items):
        mdir = os.path.join(self.core.root, "pre-%d" % int(time.time() * 1000))
        try:
            path, engine = self.core.build_merged(items, mdir)
        except Exception as e:
            log.warning("background prepare failed: %s", e)
            self._pre_fail = sig
            shutil.rmtree(mdir, ignore_errors=True)
            return
        old = self.pre.get("dir")
        self.pre = {"sig": sig, "dir": mdir, "path": path, "engine": engine}
        if old:
            shutil.rmtree(old, ignore_errors=True)

    def _print_merged(self, job, todo, printer):
        for it in todo:
            while it.status in ("queued", "staging", "converting") and not job.cancelled:
                time.sleep(0.05)
            job.check()
        ready = [it for it in todo if it.status == "ready"]
        failed = [it.display for it in todo if it.status == "failed"]
        if not ready:
            return {"printed": 0, "failed": failed}
        sig = merge_sig(ready)
        for it in ready:
            self.core.set(it, "printing", "preparing")
        t = self.pre_thread
        if t is not None and t.is_alive():
            job.log("Finishing the prepared print job…")
            t.join()
        printed, mdir = 0, None
        try:
            pre = self.pre
            if pre.get("sig") == sig and pre.get("path") and os.path.isfile(pre["path"]):
                out, mdir, engine = pre["path"], pre["dir"], pre["engine"] + ", prepared in advance"
                self.pre = self._no_pre()
            else:
                job.log("Merging %d file(s)…" % len(ready))
                mdir = os.path.join(self.core.root, "merge-%d" % int(time.time() * 1000))
                out, engine = self.core.build_merged(ready, mdir)
            job.log("Merged %d file(s) with %s." % (len(ready), engine))
            if job.cancelled:
                for it in ready:
                    self.core.set(it, "ready")
                return {"printed": 0, "failed": failed}
            for it in ready:
                self.core.set(it, "printing", "submitting")
            jid = self.backend.submit(out, printer, 1, "MailTool batch (%d files)" % len(ready))
            if jid:
                self.job_ids.append(jid)
            for it in ready:
                self.core.set(it, "done", "sent in merged job%s" % ((" " + jid) if jid else ""))
                printed += 1
            job.log("Sent to %s%s." % (printer or "the default printer", (" as job " + jid) if jid else ""), "ok")
        except UserError as e:
            for it in ready:
                self.core.fail(it, str(e))
                failed.append(it.display)
        except Exception as e:
            log.exception("merged print failed")
            for it in ready:
                self.core.fail(it, "print error: %s" % e)
                failed.append(it.display)
        finally:
            if mdir:
                shutil.rmtree(mdir, ignore_errors=True)
        return {"printed": printed, "failed": failed}

    def _print_each(self, job, todo, printer):
        printed, failed = 0, []
        for n, it in enumerate(todo, 1):
            while it.status in ("queued", "staging", "converting") and not job.cancelled:
                time.sleep(0.2)
            if job.cancelled:
                break
            job.progress(n - 1, len(todo), "printing ")
            if it.status != "ready":
                if it.status == "failed":
                    failed.append(it.display)
                continue
            self.core.set(it, "printing")
            try:
                jid = self.backend.submit(self.core.effective_pdf(it), printer, it.copies, it.display)
                if jid:
                    self.job_ids.append(jid)
                self.core.set(it, "done")
                printed += 1
            except UserError as e:
                self.core.fail(it, str(e))
                failed.append(it.display)
            except Exception as e:
                log.exception("print failed")
                self.core.fail(it, "print error: %s" % e)
                failed.append(it.display)
        job.progress(len(todo), len(todo), "printing ")
        return {"printed": printed, "failed": failed}

    def cancel_print(self):
        if self.print_job is not None:
            self.print_job.cancel()
        ids = list(self.job_ids)
        if self.backend and ids:
            threading.Thread(target=self.backend.cancel, args=(ids,), daemon=True).start()

    def _print_done(self, job):
        self.printing = False
        self.print_job = None
        r = job.result or {"printed": 0, "failed": []}
        txt = "Printed %d file(s)." % r["printed"]
        if r["failed"]:
            txt += " Skipped/failed: %s." % ", ".join(r["failed"])
        if job.state == "cancelled":
            txt = "Cancelled. " + txt
            for it in self.items.values():
                if it.status == "printing":
                    self.core.set(it, "ready")
            if self.backend and self.job_ids:
                threading.Thread(target=self.backend.cancel, args=(list(self.job_ids),), daemon=True).start()
        if self.clear_var.get() and r["printed"]:
            done = [i for i in self.items.values() if i.status == "done"]
            for i in done:
                self.drop_row(i)
            txt += " Cleared %d from the list." % len(done)
        self.app.log(txt, "ok" if r["printed"] and not r["failed"] else ("warn" if r["printed"] else "error"))
        self.refresh_buttons()

    def refresh_buttons(self):
        busy = self.printing
        for b in self.btns:
            b.state(["disabled"] if busy else ["!disabled"])
        can = not busy and any(i.status in ACTIVE for i in self.items.values())
        self.b_print.state(["!disabled"] if can else ["disabled"])
        self.b_cancel.state(["!disabled"] if busy else ["disabled"])

    def tick(self):
        self._maybe_prebuild()
        if self.pre_thread is not None and self.pre_thread.is_alive():
            txt = "Preparing print job…"
        elif not self.printing and self._sig_seen is not None and self._sig_seen == self.pre["sig"]:
            txt = "Print job ready"
        else:
            txt = ""
        if self.prep_lbl.cget("text") != txt:
            self.prep_lbl.configure(text=txt)
        self.refresh_buttons()
        self._update_empty()

    def save(self):
        self.pcfg["printer"] = self.selected_printer()
