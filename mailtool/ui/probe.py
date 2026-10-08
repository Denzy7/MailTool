"""--probe: shows exactly what a file manager sends on drag & drop."""
from __future__ import annotations

import logging
import sys
import time
import tkinter as tk

from mailtool.printing.resolve import classify, parse_drop
from mailtool.ui.widgets import DND_ERR, DND_FILES, DND_TEXT, make_root

log = logging.getLogger("mailtool")


def run_probe():
    root, dnd = make_root()
    root.title("MailTool drag & drop probe")
    root.geometry("780x560")
    if not dnd:
        print("tkinterdnd2 unavailable: %s" % DND_ERR, file=sys.stderr)
    box = tk.Text(root, wrap="word", font=("Courier", 9))

    def emit(text):
        log.info("PROBE %s", text)
        print(text, flush=True)
        box.insert("end", text + "\n")
        box.see("end")

    frame = tk.Frame(root)
    frame.pack(fill="x")

    def handler(e, label):
        emit("---- DROP on [%s] at %s" % (label, time.strftime("%H:%M:%S")))
        for a in ("action", "actions", "type", "types", "modifiers"):
            emit("  %s = %r" % (a, getattr(e, a, None)))
        emit("  data(raw) = %r" % (e.data,))
        for p in parse_drop(root.tk, e.data):
            s = classify(p)
            emit("  -> %r  => %s" % (p, ("%s key=%s" % (s.kind, s.key)) if s else "NOT RECOGNISED"))
        return "copy"

    def enter(e, label):
        emit("enter [%s] offered types: %r" % (label, getattr(e, "types", None)))
        return getattr(e, "action", "copy")

    if dnd:
        for label, types in (("DND_Files", (DND_FILES,)), ("text/uri-list", ("text/uri-list",)),
                             ("DND_Text", (DND_TEXT,))):
            z = tk.Label(frame, text="Drop here:\n" + label, relief="groove", height=5)
            z.pack(side="left", fill="x", expand=True, padx=4, pady=4)
            z.drop_target_register(*types)
            z.dnd_bind("<<Drop>>", lambda e, l=label: handler(e, l))
            z.dnd_bind("<<DropEnter>>", lambda e, l=label: enter(e, l))
    box.pack(fill="both", expand=True)
    emit("Drag files from Dolphin / Nautilus / Explorer onto each zone. Output also goes to the log file.")
    root.mainloop()
