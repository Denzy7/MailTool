#!/usr/bin/env python3
"""Start MailTool from a source checkout (also the PyInstaller entry script).

    python run_mailtool.py            the app
    python run_mailtool.py --check    what's installed
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mailtool.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
