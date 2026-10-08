"""Folder layout inside the library:  <library>/<YYYY-MM-DD>/<sender> - <subject>/"""
from __future__ import annotations

import os

from mailtool.core.util import sanitize_for_path

MESSAGE_MARKER = ".message-uid"   # inside each message folder: which email owns it


def claim_message_dir(root, date_folder, sender_email, subject, mailbox, uid):
    """Return (and create) the folder for ONE email: normally
    <date>/<sender> - <subject>; if another email already owns that name (same
    sender, subject and day) this one gets <sender>_UID<uid> - <subject>.
    Ownership is recorded in a small marker file, so every feature picks the
    same folder for the same email in any order or run."""
    me = "%s\n%s" % (mailbox, uid)
    date_dir = os.path.join(root, date_folder)
    folder = None
    for name in (sanitize_for_path("%s - %s" % (sender_email, subject)),
                 sanitize_for_path("%s_UID%s - %s" % (sender_email, uid, subject))):
        folder = os.path.join(date_dir, name)
        marker = os.path.join(folder, MESSAGE_MARKER)
        try:
            with open(marker, encoding="utf-8") as fh:
                owner = fh.read().strip()
        except OSError:
            owner = None
        if owner is not None and owner != me:
            continue
        os.makedirs(folder, exist_ok=True)
        if owner is None:
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(me)
        return folder
    return folder


def unique_name(folder, filename, taken):
    """filename inside folder, with _1, _2 ... only for repeats within THIS email
    (a same-named file from an earlier run of the same email is overwritten)."""
    path = os.path.join(folder, sanitize_for_path(filename, max_len=150))
    base, ext = os.path.splitext(path)
    n = 1
    while path in taken:
        path = "%s_%d%s" % (base, n, ext)
        n += 1
    taken.add(path)
    return path


def ordered_print_files(folder):
    """Files of one message folder in print order: the EMAILINFO page first, then
    the attachments by name. MERGED_* (a copy of the PDFs) and hidden files skipped."""
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return []
    info, rest = [], []
    for n in names:
        p = os.path.join(folder, n)
        if n.startswith(".") or not os.path.isfile(p) or n.upper().startswith("MERGED_"):
            continue
        (info if n.upper().startswith("EMAILINFO") else rest).append(p)
    return info + rest


def looks_like_message_folder(folder):
    return os.path.isfile(os.path.join(folder, MESSAGE_MARKER))
