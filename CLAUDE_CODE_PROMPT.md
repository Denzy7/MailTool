# Prompt for Claude Code - finish and verify MailTool on this machine

Paste everything below the line into Claude Code, started inside the unzipped `MailTool` folder.

---

You are working on **MailTool**, a Python/Tk desktop app in this folder that downloads email
over IMAP, sorts it into groups by keyword, and prints PDFs/Word files/images. Read
`README.md` first; it explains the layout and how the parts connect.

It was written and tested in a Linux sandbox **without** a real mail server, printer,
Windows, KDE, or OS keyring. Your job is to make it work for real on this machine, fix what
is platform-specific, and build the executable. The target platforms are **Linux (KDE
Plasma)** and **Windows 10**; work on whichever this machine is.

## Ground rules

* **Never change anything in the mailbox.** The mailbox must stay opened read-only
  (`select(..., readonly=True)`) and bodies fetched with `BODY.PEEK`. Do not add code that
  sets flags, moves, deletes or appends messages.
* **Never write the IMAP password to disk**, logs, or the settings file. It may only live in
  memory or the OS keyring (`mailtool/core/secrets.py`).
* Keep the engines free of Tk: nothing in `core/ mail/ library/ fetch/ sort/ printing/` may
  import tkinter. UI code goes in `mailtool/ui/`.
* Worker threads must never touch Tk widgets; they report through `Job.log/progress` or
  `App.call_ui`.
* After every change run `python -m pytest` and keep it green. Add a test when you fix a bug
  in an engine.
* Ask me before anything that sends mail to a printer, installs system packages with
  sudo/admin rights, or connects to my mail server. Tell me what you are about to do and wait.
* Keep the brand: name **MailTool**, deep teal (`mailtool/ui/theme.py`), the existing icons.

## Steps

1. **Environment.** Create a venv (`python -m venv .venv`), install `requirements-dev.txt`, then
   `python -m playwright install chromium`. Run `python run_mailtool.py --check` and show me the
   result. For anything missing, tell me the exact install command and ask before running it.
   - Windows: confirm `tzdata` imports and `ZoneInfo("Africa/Nairobi")` works.
   - Windows: check SumatraPDF is found; if not, ask whether to `winget install SumatraPDF.SumatraPDF`.

2. **Unit tests.** `python -m pytest -q`. Fix any failures (path separators, temp-file
   locking and `os.replace` on Windows are the likely ones).

3. **GUI smoke test.** Linux: `xvfb-run -a python tests/gui_smoke.py shots light` (or without
   xvfb-run in a desktop session). Windows: `python tests/gui_smoke.py shots light` and
   `... shots dark`. Open the screenshots and check every screen for clipped text, unreadable
   colours or overlapping widgets at this machine's DPI/scaling, and fix layout problems
   (Windows uses Segoe UI and may be scaled 125-150%).

4. **Theme detection.** Confirm "Follow the system" picks light/dark correctly here
   (`mailtool/ui/theme.py: system_prefers_dark` - Windows registry, KDE `kdeglobals`,
   GNOME `gsettings`).

5. **Keyring.** Ask me for a test value, then check `keyring` stores and reads it
   (KWallet / Secret Service on Linux, Credential Manager on Windows) via
   `mailtool.core.secrets.set_password/get_password`, and delete it afterwards. If no
   backend works on Linux, explain the options (e.g. `python3-secretstorage`, KWallet
   running) - the app must still work with session-only passwords.

6. **Live IMAP check (read-only) - ask me first.** With my account (I'll type the password
   into the app's own prompt, not into this chat):
   - Settings › Account › Test connection; confirm the mailbox list loads.
   - Fetch a small range (one day) into a throw-away library folder, with attachments, EMAILINFO
     in each layout (Printed, Image, Plain text) and the CSV export.
   - Afterwards, in my webmail, confirm the fetched messages are **still unread** if they
     were unread before.
   - Check: folder names, file timestamps (= arrival time), `EMAILINFO_*.pdf` look right,
     the CSV has UID/Mailbox columns and times in Africa/Nairobi.
   - Sort that library with a test group; then sort the exported CSV (it has UIDs);
     then remove the UID column from a copy and sort it to exercise the fuzzy lookup
     (IMAP and, if I give you one, an extracted Zimbra `.eml` folder).

7. **Printing - ask me first, and print to a PDF/virtual printer if one exists.**
   - Linux: `lpstat -e` lists printers; drop a PDF, a .docx and a PNG into the Print screen,
     print as one merged job, then file by file. Check `Cancel` works.
   - Windows: same with SumatraPDF; test the Microsoft Word engine if Word is installed.
   - Library › select emails › Print: confirm the order is EMAILINFO then attachments.

8. **Drag & drop.** Run `python run_mailtool.py --probe` and drag files from Dolphin (Linux)
   or Explorer (Windows) onto each zone, including a file from a WebDAV/network share if I
   have one. Then drop the same onto the Print screen and a whole email folder.

9. **Build.** `pyinstaller --noconfirm MailTool.spec`. Start `dist/MailTool/MailTool`
   (`MailTool.exe`), then confirm: icon and theme load, drag & drop works, `Settings ›
   Dependencies` is all green for what's installed, "Install Chromium" works from the frozen
   build, and a Printed-layout EMAILINFO renders. On Windows put `SumatraPDF.exe` next to the
   spec first so it gets bundled. Zip `dist/MailTool` as `MailTool-<version>-<platform>.zip`.

10. **Report.** Summarise what you tested, what you changed (with file names), anything still
    not working, and what I should check by hand.

## Known gaps to look at

* Nothing has run against a real IMAP server yet (only a fake one in `tests/fakes.py`).
  Watch for: servers that return BODYSTRUCTURE with literals, mailbox names needing quotes or
  modified UTF-7, very large mailboxes (search is padded by a day each side, then filtered
  locally in batches of 250).
* Windows has not been run at all: check `os.startfile`, CREATE_NO_WINDOW subprocesses,
  long UNC paths (`longpath()`), and that the frozen build finds `SumatraPDF.exe`.
* `tkinterdnd2` on KDE Wayland may only accept drops from some apps; `--probe` shows what arrives.
* The Settings theme choice applies on the next start.
* `deps.chromium_installed()` only checks that some Chromium download exists, not that it
  matches the installed Playwright version; the fetch job falls back to the text layout
  with a clear message if the browser can't start.
