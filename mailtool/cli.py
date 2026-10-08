"""Command line entry point.

    mailtool                 start the app
    mailtool a.pdf b.docx    start with files in the print queue
    mailtool --check         print what's installed and exit
    mailtool --probe         show raw drag & drop data (for debugging file managers)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from mailtool import APP_NAME, __version__
from mailtool.core.util import logs_dir

log = logging.getLogger("mailtool")


def setup_logging(verbose=False):
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
    try:
        fh = RotatingFileHandler(os.path.join(logs_dir(), "mailtool.log"), maxBytes=1_000_000, backupCount=3,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO if not verbose else logging.DEBUG)
        log.addHandler(fh)
    except OSError:
        pass
    if verbose:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mailtool", description="%s - download, sort and print email" % APP_NAME)
    ap.add_argument("--check", action="store_true", help="show what's installed and exit")
    ap.add_argument("--probe", action="store_true", help="show raw drag & drop data")
    ap.add_argument("--install-browser", action="store_true",
                    help="download the headless Chromium used for printed/image email bodies")
    ap.add_argument("--verbose", action="store_true", help="also log to the terminal")
    ap.add_argument("--version", action="version", version="%s %s" % (APP_NAME, __version__))
    ap.add_argument("files", nargs="*", help="files to put in the print queue")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)

    if args.check:
        from mailtool.core import deps
        from mailtool.core.config import Config
        caps = deps.detect(Config()["print"])
        print("%s %s - %s %s\n" % (APP_NAME, __version__, sys.executable, sys.version.split()[0]))
        print(deps.format_caps(caps))
        return 0 if all(c.ok for c in caps if c.level == "core") else 1

    if args.install_browser:
        from mailtool.core.deps import install_chromium

        class _Print:
            cancelled = False

            def log(self, text, level="info"):
                print(text, flush=True)
        try:
            install_chromium(_Print())
            return 0
        except Exception as e:
            print("Failed: %s" % e, file=sys.stderr)
            return 1

    try:
        import tkinter  # noqa: F401
    except Exception as e:
        from mailtool.core import deps
        tk_cap = deps.detect({})[0]
        sys.stderr.write("Tk is not available: %s\nInstall: %s\n" % (e, tk_cap.hint))
        return 2

    if args.probe:
        from mailtool.ui.probe import run_probe
        run_probe()
        return 0

    from mailtool.ui.app import App
    App(files=[os.path.abspath(f) for f in args.files]).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
