"""Print backends: CUPS on Linux, SumatraPDF on Windows."""
from __future__ import annotations

import re

from mailtool.core.util import IS_WIN, run
from mailtool.printing.pdfops import UserError
from mailtool.printing.tools import find_sumatra, find_tool, win32print


class CupsBackend:
    name = "CUPS"

    def __init__(self, pcfg):
        self.cfg = pcfg
        self.lp = find_tool(["lp"])
        self.lpstat = find_tool(["lpstat"])
        self.cancel_bin = find_tool(["cancel"])

    def ok(self):
        return bool(self.lp and self.lpstat)

    def printers(self):
        names, default = [], None
        rc, out, _ = run([self.lpstat, "-e"], 15)
        if rc == 0:
            names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not names:
            rc, out, _ = run([self.lpstat, "-p"], 15)
            names = re.findall(r"^printer (\S+)", out or "", re.M)
        rc, out, _ = run([self.lpstat, "-d"], 15)
        m = re.search(r"destination:\s*(\S+)", out or "")
        if m:
            default = m.group(1)
        return names, default

    def submit(self, pdf, printer, copies, title):
        cmd = [self.lp, "-t", title[:100], "-n", str(copies)]
        if printer:
            cmd += ["-d", printer]
        cmd.append(pdf)
        rc, out, err = run(cmd, self.cfg.get("print_timeout") or 300)
        if rc is None:
            raise UserError("print command timed out")
        if rc != 0:
            raise UserError((err or out or "lp failed").strip()[:150])
        m = re.search(r"request id is (\S+)", out)
        return m.group(1) if m else None

    def cancel(self, job_ids):
        if self.cancel_bin and job_ids:
            run([self.cancel_bin] + list(job_ids), 30)


class SumatraBackend:
    name = "SumatraPDF"

    def __init__(self, pcfg):
        self.cfg = pcfg
        self.exe = find_sumatra(pcfg)

    def ok(self):
        return bool(self.exe)

    def printers(self):
        names, default = [], None
        if win32print is not None:
            try:
                names = [p[2] for p in win32print.EnumPrinters(6, None, 1)]
                default = win32print.GetDefaultPrinter()
                return names, default
            except Exception:
                pass
        rc, out, _ = run(["powershell", "-NoProfile", "-Command",
                          "Get-Printer | Select-Object -ExpandProperty Name"], 30)
        if rc == 0:
            names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        rc, out, _ = run(["powershell", "-NoProfile", "-Command",
                          "(Get-CimInstance Win32_Printer | Where-Object Default).Name"], 30)
        if rc == 0 and out.strip():
            default = out.strip().splitlines()[0]
        return names, default

    def submit(self, pdf, printer, copies, title):
        cmd = [self.exe, "-print-to", printer] if printer else [self.exe, "-print-to-default"]
        cmd += ["-print-settings", "%dx" % copies, "-silent", pdf]
        rc, out, err = run(cmd, self.cfg.get("print_timeout") or 300)
        if rc is None:
            raise UserError("SumatraPDF timed out")
        if rc != 0:
            raise UserError((err or out or "SumatraPDF exit code %s" % rc).strip()[:150])
        return None

    def cancel(self, job_ids):
        pass  # already with the Windows spooler; Cancel only stops further submissions


def get_backend(pcfg):
    b = SumatraBackend(pcfg) if IS_WIN else CupsBackend(pcfg)
    return b if b.ok() else None
