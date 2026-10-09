"""Check GitHub for a newer MailTool release (one quiet request; failures are silent)."""
from __future__ import annotations

import json
import re
import threading
import urllib.request

from mailtool import APP_NAME, __version__
from mailtool.core.util import log

REPO = "Denzy7/MailTool"
RELEASES_URL = "https://github.com/%s/releases" % REPO
LATEST_API = "https://api.github.com/repos/%s/releases/latest" % REPO


def parse_version(text):
    """'v1.2.10' -> (1, 2, 10); anything without a leading number -> None."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def check(current=__version__, timeout=8):
    """{"status": "current"|"available"|"error", "current", "latest", "url"}."""
    try:
        req = urllib.request.Request(LATEST_API, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "%s/%s" % (APP_NAME, current)})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        latest = (data.get("tag_name") or "").strip()
        if not parse_version(latest):
            raise ValueError("unexpected release tag %r" % latest)
        return {"status": "available" if _newer(latest, current) else "current", "current": current,
                "latest": latest.lstrip("v"), "url": data.get("html_url") or RELEASES_URL + "/latest"}
    except Exception as e:
        log.info("Update check failed: %s", e)
        return {"status": "error", "current": current, "latest": "", "url": RELEASES_URL}


def check_async(callback, current=__version__):
    """Run check() on a daemon thread and hand the result to callback (on that thread)."""
    threading.Thread(target=lambda: callback(check(current)), name="update-check", daemon=True).start()
