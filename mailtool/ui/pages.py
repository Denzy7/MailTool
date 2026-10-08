"""Page picker for one queued document: thumbnails to include/exclude pages,
drag to reorder, and a full-size viewer."""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from mailtool.core.util import log
from mailtool.printing.pdfops import THUMB_W, UserError, parse_ranges, render_page, render_pages
from mailtool.ui.theme import P


class PageDialog:
    def __init__(self, view, it):
        self.view, self.it = view, it
        self.pcfg = view.cfg["print"]
        self.total = it.pages
        self.vars, self.cells, self.imgs, self.frame_idx = {}, {}, {}, {}
        self.order = []
        if it.page_order and self.total and sorted(it.page_order) == list(range(self.total)):
            self.order = list(it.page_order)
        self.q = queue.Queue()
        self.alive = True
        self.viewer = None
        self.width = max(100, min(420, int(self.pcfg.get("thumb_width") or THUMB_W)))
        self.gen = 0
        self._zoom_job = self._layout_job = None
        self._cols = None
        self._drag = None
        self._hl = None
        self._skip_release = False
        root = view.app.root
        t = self.top = tk.Toplevel(root)
        t.title("Pages - %s" % it.display)
        t.geometry("1040x740")
        t.transient(root)
        t.configure(bg=P["bg"])
        bar = ttk.Frame(t, padding=(12, 10, 12, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="Select all", command=lambda: self.setall(True)).pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: self.setall(False)).pack(side="left", padx=4)
        ttk.Label(bar, text="Exclude pages:").pack(side="left", padx=(14, 4))
        self.rng = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.rng, width=14)
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self.exclude_range())
        ttk.Button(bar, text="Exclude", command=self.exclude_range).pack(side="left", padx=4)
        ttk.Label(bar, text="Zoom:").pack(side="left", padx=(18, 4))
        self.zoom = tk.DoubleVar(value=self.width)
        ttk.Scale(bar, from_=100, to=420, orient="horizontal", length=150, variable=self.zoom,
                  command=self._zoom_moved).pack(side="left")
        self.zlabel = ttk.Label(bar, text="%d px" % self.width, width=7)
        self.zlabel.pack(side="left", padx=4)
        bar2 = ttk.Frame(t, padding=(12, 6, 12, 0))
        bar2.pack(fill="x")
        ttk.Label(bar2, text="Order:").pack(side="left")
        ttk.Button(bar2, text="Reverse", command=self.reverse).pack(side="left", padx=4)
        ttk.Button(bar2, text="Reset order", command=self.reset_order).pack(side="left")
        self.count = ttk.Label(bar2, style="Muted.TLabel")
        self.count.pack(side="right")
        self.info = ttk.Label(t, foreground=P["warn"], padding=(12, 2), wraplength=980)
        self.info.pack(fill="x")
        body = ttk.Frame(t)
        body.pack(fill="both", expand=True, padx=12)
        self.canvas = tk.Canvas(body, highlightthickness=0, bg=P["bg"])
        sb = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid = tk.Frame(self.canvas, bg=P["bg"])
        self.canvas.create_window((0, 0), window=self.grid, anchor="nw")
        self.grid.bind("<Configure>", lambda ev: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda ev: self._sched_layout())
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            t.bind_all(seq, self._wheel)
        foot = ttk.Frame(t, padding=12)
        foot.pack(fill="x")
        ttk.Button(foot, text="OK", style="Accent.TButton", command=self.ok).pack(side="right")
        ttk.Button(foot, text="Cancel", command=self.close).pack(side="right", padx=8)
        ttk.Label(foot, text="Click a page to include/exclude it. Drag (or use < >) to reorder. "
                             "Double-click or 'View' to look closer.", style="Muted.TLabel").pack(side="left")
        t.protocol("WM_DELETE_WINDOW", self.close)
        for i in range(self.total or 0):
            self.make_cell(i)
        self.refresh()
        self._start_render()
        t.after(100, self.pump)
        t.after(150, self.grab)

    def grab(self):
        try:
            self.top.grab_set()
            self.top.focus_set()
        except Exception:
            if self.alive:
                self.top.after(150, self.grab)

    def _wheel(self, e):
        d = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        self.canvas.yview_scroll(d * 2, "units")

    def make_cell(self, i):
        if i in self.cells:
            return
        f = tk.Frame(self.grid, bd=0, padx=4, pady=4, highlightthickness=1, highlightbackground=P["border"],
                     bg=P["surface"])
        lbl = tk.Label(f, text="…", width=20, height=9, cursor="hand2", bg=P["surface"], fg=P["muted"])
        lbl.pack()
        lbl.bind("<ButtonPress-1>", lambda ev: self._press(ev, i))
        lbl.bind("<B1-Motion>", self._motion)
        lbl.bind("<ButtonRelease-1>", lambda ev: self._release(ev, i))
        lbl.bind("<Double-ButtonPress-1>", lambda ev: self._dbl(i))
        row = tk.Frame(f, bg=P["surface"])
        row.pack()
        var = tk.BooleanVar(value=(i not in self.it.excluded))
        kw = dict(bg=P["surface"], fg=P["text"], activebackground=P["accent_soft"], relief="flat", bd=0,
                  highlightthickness=0, padx=4, pady=0)
        tk.Button(row, text="<", command=lambda: self.shift(i, -1), **kw).pack(side="left")
        cb = tk.Checkbutton(row, text="Page %d" % (i + 1), variable=var, command=self.refresh, bg=P["surface"],
                            fg=P["text"], selectcolor=P["field"], activebackground=P["surface"],
                            highlightthickness=0)
        cb.pack(side="left")
        tk.Button(row, text="View", command=lambda: self.inspect(i), **kw).pack(side="left", padx=2)
        tk.Button(row, text=">", command=lambda: self.shift(i, 1), **kw).pack(side="left")
        self.vars[i] = var
        self.cells[i] = (f, lbl, cb, row)
        self.frame_idx[str(f)] = i
        if i not in self.order:
            self.order.append(i)
        self.place(i)

    def cols(self):
        w = self.canvas.winfo_width()
        if w < 100:
            w = 900
        return max(1, w // (max(self.width, 190) + 34))

    def place(self, i):
        p = self.order.index(i)
        c = self._cols or self.cols()
        self.cells[i][0].grid(row=p // c, column=p % c, padx=5, pady=5)

    def _sched_layout(self):
        if self._layout_job:
            self.top.after_cancel(self._layout_job)
        self._layout_job = self.top.after(150, self.layout)

    def layout(self, force=False):
        self._layout_job = None
        if not self.alive:
            return
        c = self.cols()
        if c == self._cols and not force:
            return
        self._cols = c
        for p, i in enumerate(self.order):
            if i in self.cells:
                self.cells[i][0].grid(row=p // c, column=p % c, padx=5, pady=5)

    def toggle(self, i):
        self.vars[i].set(not self.vars[i].get())
        self.refresh()

    def _dbl(self, i):
        self._skip_release = True
        self._drag = None
        self.toggle(i)          # undo the first click of the double-click
        self.inspect(i)

    def setall(self, value):
        for v in self.vars.values():
            v.set(value)
        self.refresh()

    def exclude_range(self):
        n = self.total or len(self.cells)
        try:
            idx = parse_ranges(self.rng.get(), n)
        except ValueError as e:
            messagebox.showwarning("Pages", "Cannot understand '%s'. Use e.g. 2-4, 7, 10-" % e, parent=self.top)
            return
        for i in idx:
            if i in self.vars:
                self.vars[i].set(False)
        self.refresh()

    def shift(self, i, d):
        p = self.order.index(i)
        q = p + d
        if 0 <= q < len(self.order):
            self.order[p], self.order[q] = self.order[q], self.order[p]
            self.layout(force=True)
            self.refresh()

    def reverse(self):
        self.order.reverse()
        self.layout(force=True)
        self.refresh()

    def reset_order(self):
        self.order.sort()
        self.layout(force=True)
        self.refresh()

    def drop(self, src, tgt, x_root):
        f = self.cells[tgt][0]
        self.order.remove(src)
        pos = self.order.index(tgt)
        if x_root >= f.winfo_rootx() + f.winfo_width() / 2:
            pos += 1
        self.order.insert(pos, src)
        self.layout(force=True)
        self.refresh()

    def _cell_at(self, x, y):
        w = self.top.winfo_containing(x, y)
        while w is not None:
            i = self.frame_idx.get(str(w))
            if i is not None:
                return i
            w = getattr(w, "master", None)
        return None

    def _highlight(self, i):
        if self._hl is not None and self._hl in self.cells:
            self.cells[self._hl][0].config(highlightthickness=1, highlightbackground=P["border"])
        self._hl = i
        if i is not None:
            self.cells[i][0].config(highlightthickness=3, highlightbackground=P["accent"])

    def _press(self, ev, i):
        self._drag = {"i": i, "x": ev.x_root, "y": ev.y_root, "on": False}

    def _motion(self, ev):
        d = self._drag
        if not d:
            return
        if not d["on"]:
            if abs(ev.x_root - d["x"]) + abs(ev.y_root - d["y"]) < 8:
                return
            d["on"] = True
            self.top.config(cursor="fleur")
        t = self._cell_at(ev.x_root, ev.y_root)
        self._highlight(t if t != d["i"] else None)
        y = ev.y_root - self.canvas.winfo_rooty()
        if y < 30:
            self.canvas.yview_scroll(-1, "units")
        elif y > self.canvas.winfo_height() - 30:
            self.canvas.yview_scroll(1, "units")

    def _release(self, ev, i):
        if self._skip_release:
            self._skip_release = False
            return
        d, self._drag = self._drag, None
        if not d:
            return
        if d["on"]:
            self.top.config(cursor="")
            self._highlight(None)
            t = self._cell_at(ev.x_root, ev.y_root)
            if t is not None and t != d["i"]:
                self.drop(d["i"], t, ev.x_root)
        else:
            self.toggle(i)

    def paint(self, i):
        f, lbl, cb, row = self.cells[i]
        bg = P["surface"] if self.vars[i].get() else P["excluded"]
        if f.cget("bg") != bg:
            for w in (f, lbl, cb, row):
                w.config(bg=bg)
            cb.config(activebackground=bg)
            for b in row.winfo_children():
                b.config(bg=bg)

    def refresh(self):
        pos, n = {}, 0
        for i in self.order:
            if i in self.vars and self.vars[i].get():
                n += 1
                pos[i] = n
        reordered = self.order != sorted(self.order)
        self.count.config(text="%d of %d pages will print%s" % (n, len(self.vars),
                                                                " (custom order)" if reordered else ""))
        for i, cell in self.cells.items():
            txt = "Page %d" % (i + 1)
            if reordered and i in pos:
                txt += " → #%d" % pos[i]
            if cell[2].cget("text") != txt:
                cell[2].config(text=txt)
            self.paint(i)

    def inspect(self, i):
        if not self.cells:
            return
        if self.viewer and self.viewer.alive:
            self.viewer.goto(i)
            self.viewer.top.lift()
        else:
            self.viewer = PageViewer(self, i)

    def _zoom_moved(self, _v=None):
        w = int(round(float(self.zoom.get()) / 10.0) * 10)
        self.zlabel.config(text="%d px" % w)
        if self._zoom_job:
            self.top.after_cancel(self._zoom_job)
        self._zoom_job = self.top.after(450, self._zoom_apply)

    def _zoom_apply(self):
        self._zoom_job = None
        w = int(round(float(self.zoom.get()) / 10.0) * 10)
        if not self.alive or w == self.width:
            return
        self.width = w
        self._start_render()
        self.layout(force=True)

    def _start_render(self):
        self.gen += 1
        threading.Thread(target=self._work, args=(self.width, self.gen), daemon=True).start()

    def _work(self, width, gen):
        it = self.it
        try:
            cache = it.thumbs.setdefault(width, {})
            if width in it.thumbs_done:
                for i, pth in sorted(cache.items()):
                    self.q.put(("img", gen, i, pth))
                return
            outdir = os.path.join(it.dir, "thumbs", "w%d" % width)
            aborted = []

            def emit(i, pth):
                cache[i] = pth
                if gen != self.gen or not self.alive:
                    aborted.append(1)
                    return True
                self.q.put(("img", gen, i, pth))
                return False
            n = render_pages(it.pdf, outdir, int(self.pcfg.get("convert_timeout") or 180), emit, width=width)
            if not aborted:
                it.thumbs_done.add(width)
            if n:
                self.q.put(("total", gen, n))
        except UserError as e:
            self.q.put(("err", gen, str(e)))
        except Exception as e:
            log.exception("preview")
            self.q.put(("err", gen, str(e)))

    def set_total(self, n):
        if self.total is None:
            self.total = n
            self.it.pages = n
        for i in range(n):
            self.make_cell(i)

    def pump(self):
        if not self.alive:
            return
        changed = False
        try:
            while True:
                m = self.q.get_nowait()
                changed = True
                if m[0] == "total":
                    self.set_total(m[2])
                elif m[1] != self.gen:
                    continue
                elif m[0] == "img":
                    self.make_cell(m[2])
                    try:
                        img = tk.PhotoImage(file=m[3])
                        self.imgs[m[2]] = img
                        self.cells[m[2]][1].config(image=img, text="", width=img.width(), height=img.height())
                    except Exception as e:
                        log.warning("thumb load failed: %s", e)
                elif m[0] == "err":
                    txt = "No thumbnails (%s). Install: %s" % (m[2], self.view.app.hint("preview"))
                    if self.total is None:
                        txt += "  Page count is also unknown (install pypdf or PyMuPDF)."
                    self.info.config(text=txt)
        except queue.Empty:
            pass
        if changed:
            self.refresh()
        self.top.after(100, self.pump)

    def ok(self):
        if not self.vars:
            self.close()
            return
        if not any(v.get() for v in self.vars.values()):
            messagebox.showwarning("Pages", "Select at least one page, or remove the file from the list.",
                                   parent=self.top)
            return
        self.it.excluded = {i for i, v in self.vars.items() if not v.get()}
        self.it.page_order = None if self.order == sorted(self.order) else list(self.order)
        self.view.update_row(self.it)
        self.close()

    def close(self):
        if self.viewer and self.viewer.alive:
            self.viewer.close()
        self.alive = False
        self.pcfg["thumb_width"] = self.width
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                self.top.unbind_all(seq)
            except Exception:
                pass
        try:
            self.top.grab_release()
        except Exception:
            pass
        self.top.destroy()


class PageViewer:
    """Full-size view of one page with prev/next (in print order), zoom and an include box."""

    def __init__(self, dlg, index):
        self.dlg, self.i = dlg, index
        self.alive = True
        self.gen = 0
        self.q = queue.Queue()
        self.img = None
        self._job = None
        t = self.top = tk.Toplevel(dlg.top)
        t.geometry("920x780")
        t.transient(dlg.top)
        t.configure(bg=P["bg"])
        bar = ttk.Frame(t, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text="< Prev", command=lambda: self.step(-1)).pack(side="left")
        ttk.Button(bar, text="Next >", command=lambda: self.step(1)).pack(side="left", padx=4)
        self.lbl = ttk.Label(bar, width=16)
        self.lbl.pack(side="left", padx=6)
        self.chk = ttk.Checkbutton(bar, text="Include this page", variable=dlg.vars[index], command=dlg.refresh,
                                   style="Bg.TCheckbutton")
        self.chk.pack(side="left", padx=6)
        ttk.Label(bar, text="Size:").pack(side="left", padx=(12, 2))
        self.size = tk.DoubleVar(value=800)
        ttk.Scale(bar, from_=300, to=1800, orient="horizontal", length=170, variable=self.size,
                  command=self._size_moved).pack(side="left")
        ttk.Button(bar, text="Fit width", command=self.fit).pack(side="left", padx=6)
        self.msg = ttk.Label(t, foreground=P["warn"], padding=(10, 0), wraplength=880)
        self.msg.pack(fill="x")
        body = ttk.Frame(t)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, highlightthickness=0, bg="#6B7676")
        vs = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        hs = ttk.Scrollbar(body, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._wheel)
        for seq, fn in (("<Left>", lambda e: self.step(-1)), ("<Prior>", lambda e: self.step(-1)),
                        ("<Right>", lambda e: self.step(1)), ("<Next>", lambda e: self.step(1)),
                        ("<space>", lambda e: self.flip()), ("<Escape>", lambda e: self.close())):
            t.bind(seq, fn)
        t.protocol("WM_DELETE_WINDOW", self.close)
        self.goto(index)
        t.after(50, self.pump)
        t.after(150, self.grab)

    def grab(self):
        try:
            self.top.grab_set()
            self.top.focus_set()
        except Exception:
            if self.alive:
                self.top.after(150, self.grab)

    def _wheel(self, e):
        d = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        self.canvas.yview_scroll(d * 3, "units")
        return "break"

    def flip(self):
        v = self.dlg.vars[self.i]
        v.set(not v.get())
        self.dlg.refresh()

    def fit(self):
        self.size.set(max(300, self.canvas.winfo_width() - 24))
        self._render()

    def step(self, d):
        o = self.dlg.order
        if self.i in o:
            p = o.index(self.i) + d
            if 0 <= p < len(o):
                self.goto(o[p])

    def goto(self, n):
        if n not in self.dlg.vars:
            return
        self.i = n
        self.top.title("%s - page %d" % (self.dlg.it.display, n + 1))
        self.lbl.config(text="Page %d of %d" % (n + 1, len(self.dlg.vars)))
        self.chk.config(variable=self.dlg.vars[n])
        self._render()

    def _size_moved(self, _v=None):
        if self._job:
            self.top.after_cancel(self._job)
        self._job = self.top.after(400, self._render)

    def _render(self):
        self._job = None
        self.gen += 1
        gen, i, width = self.gen, self.i, int(round(float(self.size.get()) / 10.0) * 10)
        self.msg.config(text="Rendering…")
        it = self.dlg.it
        out = os.path.join(it.dir, "view", "p%d_w%d.png" % (i, width))
        timeout = int(self.dlg.pcfg.get("convert_timeout") or 180)

        def work():
            try:
                if not os.path.isfile(out):
                    render_page(it.pdf, i, out, width, timeout)
                self.q.put(("ok", gen, out))
            except UserError as e:
                self.q.put(("err", gen, str(e)))
            except Exception as e:
                log.exception("page render")
                self.q.put(("err", gen, str(e)))
        threading.Thread(target=work, daemon=True).start()

    def pump(self):
        if not self.alive:
            return
        try:
            while True:
                kind, gen, val = self.q.get_nowait()
                if gen != self.gen:
                    continue
                if kind == "ok":
                    try:
                        self.img = tk.PhotoImage(file=val)
                        self.canvas.delete("all")
                        self.canvas.create_image(0, 0, image=self.img, anchor="nw")
                        self.canvas.configure(scrollregion=(0, 0, self.img.width(), self.img.height()))
                        self.canvas.xview_moveto(0)
                        self.canvas.yview_moveto(0)
                        self.msg.config(text="")
                    except Exception as e:
                        self.msg.config(text="Could not display page: %s" % e)
                else:
                    self.msg.config(text="%s. Install: %s" % (val, self.dlg.view.app.hint("preview")))
        except queue.Empty:
            pass
        self.top.after(80, self.pump)

    def close(self):
        self.alive = False
        try:
            self.top.grab_release()
        except Exception:
            pass
        self.top.destroy()
        if self.dlg.alive:
            self.dlg.grab()
