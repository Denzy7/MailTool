"""Command line entry point.

    mailtool                 start the app
    mailtool a.pdf b.docx    start with files in the print queue
    mailtool --check         print what's installed and exit
    mailtool --probe         show raw drag & drop data (for debugging file managers)
    mailtool --web           run in the browser instead (see mailtool/web/server.py)
"""
from __future__ import annotations

import argparse
import importlib
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


def load_config(path=None):
    """The settings to use: the default file, or `path` (a folder means <folder>/settings.json).
    A file that doesn't exist yet starts from the defaults and is created on the first save."""
    from mailtool.core.config import Config
    if not path:
        return Config()
    folder_like = path.endswith(("/", os.sep))
    path = os.path.abspath(os.path.expanduser(path))
    if folder_like or os.path.isdir(path):
        path = os.path.join(path, "settings.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        log.info("settings file %s does not exist yet - starting from the defaults", path)
    return Config(path)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mailtool", description="%s - download, sort and print email" % APP_NAME)
    ap.add_argument("--check", action="store_true", help="show what's installed and exit")
    ap.add_argument("--probe", action="store_true", help="show raw drag & drop data")
    ap.add_argument("--install-browser", action="store_true",
                    help="download the headless Chromium used for printed/image email bodies")
    ap.add_argument("--verbose", action="store_true", help="also log to the terminal")
    ap.add_argument("--config", metavar="PATH", default=None,
                    help="settings file to use instead of the default one (a folder means PATH/settings.json); "
                         "lets the desktop app and the web server run side by side")
    web = ap.add_argument_group("web server")
    web.add_argument("--web", action="store_true", help="serve MailTool to browsers instead of opening a window")
    web.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765)")
    web.add_argument("--host", default="0.0.0.0",
                     help="address to listen on (default: all interfaces; 127.0.0.1 = this machine only)")
    web.add_argument("--token", default=None,
                     help="access code (default: $MAILTOOL_WEB_TOKEN, else a random one shown at start-up)")
    web.add_argument("--cert", default=None, help="TLS certificate (PEM) to serve HTTPS")
    web.add_argument("--key", default=None, help="TLS private key (PEM), if not inside --cert")
    web.add_argument("--max-upload", type=int, default=512, metavar="MB", help="largest upload (default 512 MB)")
    ap.add_argument("--version", action="version", version="%s %s" % (APP_NAME, __version__))
    ap.add_argument("files", nargs="*", help="files to put in the print queue")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except OSError as e:
        sys.stderr.write("Cannot use the settings file %s: %s\n" % (args.config, e))
        return 2

    if args.check:
        from mailtool.core import deps
        caps = deps.detect(cfg["print"])
        print("%s %s - %s %s" % (APP_NAME, __version__, sys.executable, sys.version.split()[0]))
        print("Settings: %s\n" % cfg.path)
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

    if args.web:
        from mailtool.web.server import serve
        return serve(host=args.host, port=args.port, code=args.token, cert=args.cert, key=args.key,
                     max_upload_mb=args.max_upload, config=cfg)

    try:
        importlib.import_module("tkinter")
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
    App(files=[os.path.abspath(f) for f in args.files], config=cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
