"""Main window: brand sidebar, the five screens, and the job bar + log drawer."""
from __future__ import annotations

import os
import queue
import time
import tkinter as tk
from tkinter import messagebox, ttk

from mailtool import APP_NAME, __version__
from mailtool.core import deps, secrets
from mailtool.core.config import Config
from mailtool.core.jobs import JobRunner
from mailtool.core.util import asset, log, open_in_file_manager, logs_dir
from mailtool.ui import theme
from mailtool.ui.theme import P
from mailtool.ui.widgets import DND_ERR, LogView, PasswordDialog, make_root, register_drop

NAV = [("fetch", "Fetch"), ("library", "Library"), ("sort", "Sort"), ("print", "Print"), ("settings", "Settings")]
KIND_LABEL = {"fetch": "Fetch", "sort": "Sort", "print": "Print", "test": "Connection"}


def default_library_dir():
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    return os.path.join(docs if os.path.isdir(docs) else home, "MailTool Library")


class App:
    def __init__(self, files=(), config=None):
        self.cfg = config or Config()
        if not self.cfg.get("general", "library_dir"):
            self.cfg.set("general", "library_dir", default_library_dir())
        self.caps = deps.detect(self.cfg["print"])
        self.runner = JobRunner()
        self._job_done = {}
        self._closing = False
        self.ui_q = queue.Queue()           # (fn, args) posted by worker threads

        self.root, self.dnd = make_root()
        self.root.withdraw()
        self.root.title(APP_NAME)
        theme.apply(self.root, self.cfg.get("general", "theme") or "system")
        self._set_icon()
        self.root.geometry(self.cfg.get("general", "geometry") or "1180x780")
        self.root.minsize(980, 640)

        self._images = {}
        self._build()
        from mailtool.ui.fetch_view import FetchView
        from mailtool.ui.library_view import LibraryView
        from mailtool.ui.print_view import PrintView
        from mailtool.ui.settings_view import SettingsView
        from mailtool.ui.sort_view import SortView
        self.views = {"fetch": FetchView(self), "library": LibraryView(self), "sort": SortView(self),
                      "print": PrintView(self), "settings": SettingsView(self)}
        self.current = None
        self.show(self.cfg.get("general", "last_view") or "fetch")

        if self.dnd:
            register_drop(self.root, self.on_drop)
        else:
            self.log("Drag & drop unavailable (%s). Use 'Add files' instead. Install: %s" % (
                DND_ERR or "tkdnd failed to load", deps.pip_cmd("tkinterdnd2")), "warn")
        for c in self.caps:
            if not c.ok and c.level == "core":
                self.log("Missing: %s. Install: %s" % (c.label, c.hint), "warn")

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self.poll)
        if files:
            self.root.after(300, lambda: (self.show("print"), self.views["print"].add_paths(files)))
        self.root.deiconify()

    # ------------------------------------------------------------------ layout
    def _set_icon(self):
        try:
            imgs = [tk.PhotoImage(file=asset("icon_%d.png" % s)) for s in (256, 64, 32, 16)]
            self.root.iconphoto(True, *imgs)
            self._icon_imgs = imgs
        except tk.TclError:
            pass
        if os.name == "nt":
            try:
                self.root.iconbitmap(default=asset("mailtool.ico"))
            except tk.TclError:
                pass

    def img(self, name):
        if name not in self._images:
            try:
                self._images[name] = tk.PhotoImage(file=asset(name))
            except tk.TclError:
                self._images[name] = None
        return self._images[name]

    def _build(self):
        r = self.root
        # sidebar ------------------------------------------------------------
        sb = self.sidebar = tk.Frame(r, bg=P["sidebar"], width=212)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)
        brand = tk.Frame(sb, bg=P["sidebar"])
        brand.pack(fill="x", padx=18, pady=(22, 26))
        logo = self._logo_image(34)
        if logo:
            tk.Label(brand, image=logo, bg=P["sidebar"]).pack(side="left")
        tk.Label(brand, text=APP_NAME, bg=P["sidebar"], fg="#FFFFFF", font="MT.Brand").pack(side="left", padx=(10, 0))
        self.nav_items = {}
        for key, label in NAV:
            self.nav_items[key] = self._nav_button(sb, key, label)
        foot = tk.Frame(sb, bg=P["sidebar"])
        foot.pack(side="bottom", fill="x", padx=18, pady=16)
        self.acct_lbl = tk.Label(foot, text="", bg=P["sidebar"], fg=P["sidebar_text"], font="MT.Small",
                                 justify="left", anchor="w", wraplength=176)
        self.acct_lbl.pack(fill="x")
        tk.Label(foot, text="v%s" % __version__, bg=P["sidebar"], fg=P["sidebar_text"], font="MT.Small",
                 anchor="w").pack(fill="x", pady=(6, 0))
        self.refresh_account_label()

        # main ----------------------------------------------------------------
        main = self.main = ttk.Frame(r)
        main.pack(side="left", fill="both", expand=True)
        self.header = ttk.Frame(main, padding=(28, 22, 28, 6))
        self.header.pack(fill="x")
        self.title_lbl = ttk.Label(self.header, text="", style="H1.TLabel")
        self.title_lbl.pack(anchor="w")
        self.sub_lbl = ttk.Label(self.header, text="", style="Sub.TLabel")
        self.sub_lbl.pack(anchor="w", pady=(2, 0))

        # job bar + log drawer (bottom) -----------------------------------------
        self.bottom = tk.Frame(main, bg=P["surface"], highlightthickness=1, highlightbackground=P["border"])
        self.bottom.pack(side="bottom", fill="x")
        bar = ttk.Frame(self.bottom, style="Bar.TFrame", padding=(16, 8))
        bar.pack(fill="x")
        self.dot = tk.Canvas(bar, width=10, height=10, bg=P["surface"], highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self._dot = self.dot.create_oval(1, 1, 9, 9, fill=P["faint"], outline="")
        self.status_lbl = ttk.Label(bar, text="Ready", style="Bar.TLabel")
        self.status_lbl.pack(side="left")
        self.log_btn = ttk.Button(bar, text="Hide log", style="Small.TButton", command=self.toggle_log)
        self.log_btn.pack(side="right")
        ttk.Button(bar, text="Log files", style="Small.TButton",
                   command=lambda: open_in_file_manager(logs_dir())).pack(side="right", padx=(0, 6))
        self.stop_btn = ttk.Button(bar, text="Stop", style="Small.TButton", command=self.stop_jobs)
        self.prog = ttk.Progressbar(bar, mode="determinate", length=220)
        self.prog_lbl = ttk.Label(bar, text="", style="Bar.TLabel")
        self.logview = LogView(self.bottom, height=6)
        self.log_open = bool(self.cfg.get("general", "log_open", True))
        if self.log_open:
            self.logview.pack(fill="x", padx=1, pady=(0, 1))
        else:
            self.log_btn.configure(text="Show log")

        self.content = ttk.Frame(main)
        self.content.pack(fill="both", expand=True)

    def _logo_image(self, size):
        try:
            from PIL import Image, ImageTk
            im = Image.open(asset("icon_256.png")).resize((size, size), Image.LANCZOS)
            self._logo = ImageTk.PhotoImage(im)
        except Exception:
            self._logo = self.img("icon_32.png")
        return self._logo

    def _nav_button(self, parent, key, label):
        f = tk.Frame(parent, bg=P["sidebar"], cursor="hand2")
        f.pack(fill="x", padx=10, pady=2)
        bar = tk.Frame(f, bg=P["sidebar"], width=4)
        bar.pack(side="left", fill="y")
        icon = tk.Label(f, image=self.img("nav_%s_off.png" % key), bg=P["sidebar"])
        icon.pack(side="left", padx=(12, 12), pady=10)
        text = tk.Label(f, text=label, bg=P["sidebar"], fg=P["sidebar_text"], font="MT.Nav", anchor="w")
        text.pack(side="left", fill="x", expand=True)
        badge = tk.Label(f, text="", bg=P["sidebar"], fg="#FFFFFF", font="MT.Small")
        badge.pack(side="right", padx=10)
        item = {"frame": f, "bar": bar, "icon": icon, "text": text, "badge": badge, "key": key}
        for w in (f, icon, text, badge, bar):
            w.bind("<Button-1>", lambda e, k=key: self.show(k))
            w.bind("<Enter>", lambda e, it=item: self._nav_paint(it, hover=True))
            w.bind("<Leave>", lambda e, it=item: self._nav_paint(it))
        return item

    def _nav_paint(self, it, hover=False):
        active = it["key"] == self.current
        bg = P["sidebar_active"] if active else (P["sidebar_hover"] if hover else P["sidebar"])
        for w in (it["frame"], it["icon"], it["text"], it["badge"]):
            w.configure(bg=bg)
        it["bar"].configure(bg="#FACC15" if active else bg)
        it["text"].configure(fg=P["sidebar_text_on"] if active or hover else P["sidebar_text"])
        it["icon"].configure(image=self.img("nav_%s_%s.png" % (it["key"], "on" if active or hover else "off")))

    def set_badge(self, key, text):
        self.nav_items[key]["badge"].configure(text=text or "")

    def show(self, key):
        if key not in self.views:
            key = "fetch"
        if self.current == key:
            return
        if self.current:
            self.views[self.current].frame.pack_forget()
            self.views[self.current].on_hide()
        self.current = key
        v = self.views[key]
        self.title_lbl.configure(text=v.title)
        self.sub_lbl.configure(text=v.subtitle)
        v.frame.pack(in_=self.content, fill="both", expand=True)
        for it in self.nav_items.values():
            self._nav_paint(it)
        self.cfg.set("general", "last_view", key)
        v.on_show()

    def refresh_account_label(self):
        a = self.cfg["account"]
        if a.get("server") and a.get("username"):
            txt = "%s\n%s · %s" % (a["username"], a["server"], a.get("mailbox") or "INBOX")
        else:
            txt = "No mail account yet\nSet one up in Settings"
        self.acct_lbl.configure(text=txt)

    def toggle_log(self):
        self.log_open = not self.log_open
        if self.log_open:
            self.logview.pack(fill="x", padx=1, pady=(0, 1))
            self.log_btn.configure(text="Hide log")
        else:
            self.logview.pack_forget()
            self.log_btn.configure(text="Show log")
        self.cfg.set("general", "log_open", self.log_open)

    # ------------------------------------------------------------------ messages
    def log(self, text, level="info", prefix=None):
        getattr(log, {"warn": "warning", "ok": "info"}.get(level, level), log.info)(text)
        self.logview.add(text, level, prefix)

    def status(self, text, level="info"):
        self.status_lbl.configure(text=text)
        self.dot.itemconfigure(self._dot, fill={"ok": P["ok"], "error": P["danger"], "warn": P["warn"],
                                                "busy": P["accent"]}.get(level, P["faint"]))

    def call_ui(self, fn, *args):
        """Schedule fn(*args) on the Tk thread (safe from any thread)."""
        self.ui_q.put((fn, args))

    # ------------------------------------------------------------------ jobs
    def start_job(self, kind, title, fn, on_done=None, **kwargs):
        if self.runner.running(kind):
            messagebox.showinfo(APP_NAME, "A %s job is already running." % KIND_LABEL.get(kind, kind).lower(),
                                parent=self.root)
            return None
        job = self.runner.start(kind, title, fn, **kwargs)
        if on_done:
            self._job_done[job.id] = on_done
        self.log("%s started" % title, "info", prefix=KIND_LABEL.get(kind, kind))
        if not self.log_open and kind != "print":
            self.toggle_log()
        return job

    def stop_jobs(self):
        self.runner.cancel_all()
        self.stop_btn.state(["disabled"])

    def _update_bar(self):
        running = self.runner.running()
        if running:
            j = running[-1]
            if not self.stop_btn.winfo_ismapped():
                self.prog_lbl.pack(side="right", padx=(8, 12))
                self.stop_btn.pack(side="right", padx=(0, 6), before=self.prog_lbl)
                self.prog.pack(side="right", padx=(12, 0), before=self.prog_lbl)
                self.stop_btn.state(["!disabled"])
            if j.total:
                self.prog.configure(mode="determinate", maximum=max(j.total, 1), value=j.current)
                self.prog_lbl.configure(text="%s%d / %d" % (j.note, j.current, j.total))
            else:
                self.prog.configure(mode="indeterminate")
                self.prog.step(3)
                self.prog_lbl.configure(text=j.note or "")
            more = " (+%d more)" % (len(running) - 1) if len(running) > 1 else ""
            self.status("%s…%s" % (j.title, more), "busy")
        elif self.stop_btn.winfo_ismapped():
            for w in (self.stop_btn, self.prog, self.prog_lbl):
                w.pack_forget()

    def poll(self):
        try:
            while True:
                fn, args = self.ui_q.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    log.exception("ui callback failed")
        except queue.Empty:
            pass
        try:
            for _ in range(400):
                ev = self.runner.events.get_nowait()
                kind = ev[0]
                job = ev[1]
                if kind == "log":
                    self.logview.add(ev[3], ev[2], prefix=KIND_LABEL.get(job.kind, job.kind))
                elif kind == "state":
                    state = ev[2]
                    if state != "running":
                        self._finished(job, state)
                    for v in self.views.values():
                        v.on_job_state(job)
        except queue.Empty:
            pass
        self._update_bar()
        for v in self.views.values():
            v.tick()
        self.root.after(100, self.poll)

    def _finished(self, job, state):
        secs = (job.finished or time.time()) - job.started
        label = {"done": "finished", "failed": "failed", "cancelled": "stopped"}[state]
        lvl = {"done": "ok", "failed": "error", "cancelled": "warn"}[state]
        self.status("%s %s in %s" % (job.title, label, _dur(secs)), lvl)
        if job.error is not None and "Login failed" in str(job.error):
            a = self.cfg["account"]
            secrets.forget(a.get("server"), a.get("username"))
            self.log("The password was rejected; you'll be asked for it again next time.", "warn")
        cb = self._job_done.pop(job.id, None)
        if cb:
            try:
                cb(job)
            except Exception:
                log.exception("job callback failed")

    # ------------------------------------------------------------------ account / password
    def account(self):
        return self.cfg.snapshot("account")

    def get_password(self, prompt=True, force=False):
        """Session/keyring password, else ask (unless prompt=False). force = always ask."""
        a = self.cfg["account"]
        server, user = a.get("server", ""), a.get("username", "")
        pw = "" if force else secrets.get_password(server, user)
        if pw or not prompt:
            return pw
        d = PasswordDialog(self.root, user, server, secrets.keyring_available(), bool(a.get("remember_password")))
        if not d.result:
            return ""
        pw, remember = d.result
        saved = secrets.set_password(server, user, pw, remember)
        self.cfg.set("account", "remember_password", bool(remember and saved))
        self.cfg.save()
        return pw

    def require_account(self):
        a = self.cfg["account"]
        if not (a.get("server") and a.get("username")):
            messagebox.showinfo(APP_NAME, "Set up your mail account first (Settings › Account).",
                                parent=self.root)
            self.show("settings")
            self.views["settings"].select_tab("account")
            return False
        return True

    def library_root(self):
        return self.cfg.get("general", "library_dir") or default_library_dir()

    def tz_text(self):
        return self.cfg.get("general", "timezone") or "Africa/Nairobi"

    def cap(self, key):
        return next((c for c in self.caps if c.key == key), None)

    def hint(self, key):
        c = self.cap(key)
        return c.hint if c else ""

    def recheck_caps(self):
        self.caps = deps.detect(self.cfg["print"])

    # ------------------------------------------------------------------ drag & drop
    def on_drop(self, event):
        """Files dropped anywhere go to the print queue."""
        pv = self.views["print"]
        if self.current != "print":
            self.show("print")
        return pv.on_drop(event)

    # ------------------------------------------------------------------ closing
    def close(self):
        if self.runner.running() and not self._closing:
            if not messagebox.askyesno(APP_NAME, "A job is still running. Stop it and quit?", parent=self.root):
                return
            self._closing = True
            self.runner.cancel_all()
            self._close_deadline = time.time() + 60
            self.status("Stopping jobs before closing…", "warn")
            self._wait_close()
            return
        self._really_close()

    def _wait_close(self):
        if self.runner.running() and time.time() < self._close_deadline:
            self.root.after(150, self._wait_close)
            return
        self._really_close()

    def _really_close(self):
        try:
            for v in self.views.values():
                v.save()
            if self.root.state() == "normal":
                self.cfg.set("general", "geometry", self.root.geometry())
            self.cfg.save()
        except Exception:
            log.exception("saving settings on close")
        try:
            self.views["print"].core.cleanup()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def _dur(secs):
    secs = int(secs)
    if secs < 60:
        return "%ds" % secs
    return "%dm %02ds" % (secs // 60, secs % 60)


class View:
    """Base for the five screens."""
    title = ""
    subtitle = ""

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.frame = ttk.Frame(app.content)

    def on_show(self):
        pass

    def on_hide(self):
        pass

    def on_job_state(self, job):
        pass

    def tick(self):
        pass

    def save(self):
        pass
