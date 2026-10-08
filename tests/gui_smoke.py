"""GUI smoke test: start MailTool against a fake mailbox, visit every screen,
run a fetch + sort through the UI, queue files for printing, and save screenshots.

    xvfb-run -s "-screen 0 1280x860x24" python tests/gui_smoke.py OUTDIR [light|dark]

Not part of the pytest run (needs a display)."""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
theme = sys.argv[2] if len(sys.argv) > 2 else "light"
os.makedirs(out_dir, exist_ok=True)
tmp = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "cfg")
os.environ["XDG_DATA_HOME"] = os.path.join(tmp, "data")

import test_pipeline as tp  # noqa: E402
from mailtool.core import secrets  # noqa: E402
from mailtool.core.config import Config  # noqa: E402
from mailtool.ui.app import App  # noqa: E402


tp.install_fake_mailbox(setattr)       # fetcher/sorter use the fake mailbox
cfg = Config()
cfg.update("general", {"library_dir": os.path.join(tmp, "Library"), "theme": theme})
cfg.update("account", {"server": "imap.example.com", "username": "office@example.com"})
cfg.update("fetch", {"from_date": "06-Oct-2026", "to_date": "06-Oct-2026", "emailinfo": True,
                     "save_attachments": True})
cfg.update("emailinfo", {"body_mode": "text"})
cfg.update("sort", {"groups": tp.SORT["groups"] + [
    {"group_name": "G3/3/1/2026/03", "keywords": ["G3: March", "mar, march", "spring report"]}]})
cfg.save()
secrets.set_password("imap.example.com", "office@example.com", "pw", False)

app = App(config=cfg)
root = app.root
shots = []
errors = []


def shot(name):
    root.update()
    time.sleep(0.25)
    root.update()
    from PIL import ImageGrab
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    p = os.path.join(out_dir, "%s_%s.png" % (theme, name))
    ImageGrab.grab(bbox=(x, y, x + w, y + h), xdisplay=os.environ.get("DISPLAY")).save(p)
    shots.append(p)


def wait_jobs(then, limit=60):
    start = time.time()

    def check():
        if app.runner.running() and time.time() - start < limit:
            root.after(200, check)
        else:
            root.after(400, then)
    root.after(300, check)


steps = []


def step(fn):
    steps.append(fn)
    return fn


def run_next():
    if not steps:
        root.after(200, root.destroy)
        return
    fn = steps.pop(0)
    try:
        fn()
    except Exception as e:
        import traceback
        traceback.print_exc()
        errors.append("%s: %s" % (fn.__name__, e))
        root.after(100, run_next)


@step
def s_fetch():
    app.show("fetch")
    root.geometry("1240x820")
    root.after(500, lambda: (shot("1_fetch"), app.views["fetch"].start(), wait_jobs(run_next)))


@step
def s_fetched():
    shot("2_fetch_done")
    assert "Fetched 3" in app.views["fetch"].result_lbl.cget("text"), app.views["fetch"].result_lbl.cget("text")
    root.after(100, run_next)


@step
def s_sort():
    app.show("sort")
    root.after(300, lambda: (shot("3_sort_groups"), app.views["sort"].nb.select(2),
                             root.after(300, lambda: (shot("4_sort_run"), app.views["sort"].run(),
                                                      wait_jobs(run_next)))))


@step
def s_library():
    app.show("library")
    lv = app.views["library"]
    kids = lv.tree.get_children()
    assert len(kids) == 3, kids
    lv.tree.selection_set(kids[1])
    root.after(500, lambda: (shot("5_library"), run_next()))


@step
def s_print():
    lv = app.views["library"]
    lv.tree.selection_set(lv.tree.get_children())
    lv.print_selected()
    root.after(4000, lambda: (shot("6_print"), run_next()))


@step
def s_pages():
    pv = app.views["print"]
    ready = [it for it in pv.items.values() if it.status == "ready"]
    assert ready, [(i.display, i.status, i.msg) for i in pv.items.values()]
    from mailtool.ui.pages import PageDialog
    d = PageDialog(pv, ready[0])

    def done():
        root.update()
        from PIL import ImageGrab
        t = d.top
        p = os.path.join(out_dir, "%s_7_pages.png" % theme)
        ImageGrab.grab(bbox=(t.winfo_rootx(), t.winfo_rooty(), t.winfo_rootx() + t.winfo_width(),
                             t.winfo_rooty() + t.winfo_height()), xdisplay=os.environ.get("DISPLAY")).save(p)
        shots.append(p)
        d.close()
        run_next()
    root.after(2500, done)


@step
def s_settings():
    app.show("settings")
    root.after(400, lambda: (shot("8_settings"), app.views["settings"].select_tab("emailinfo"),
                             root.after(300, lambda: (shot("9_settings_info"), run_next()))))


root.after(800, run_next)
app.run()
print("screenshots:", *shots, sep="\n  ")
if errors:
    print("ERRORS:", *errors, sep="\n  ")
    sys.exit(1)
print("GUI smoke OK")
