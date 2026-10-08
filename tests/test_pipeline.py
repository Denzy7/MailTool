"""End-to-end: fetch from a fake mailbox into a library, sort it, sort CSVs."""
import csv
import os
from datetime import datetime

import pytest

from fakes import FakeSession, make_docx, make_email, make_pdf
from mailtool.fetch import fetcher
from mailtool.library.db import Library
from mailtool.sort import sorter

ACCOUNT = {"server": "imap.test", "port": 993, "username": "me@test", "mailbox": "INBOX"}
INFO = {"header_left": "", "header_right": "EMAIL UID: <UID>", "body_mode": "text", "workers": 1, "quality": 2}
SORT = {"groups": [{"group_name": "G1/1/1/2026/01", "keywords": ["G1: January", "jan, january"]},
                   {"group_name": "G2/2/1/2026/02", "keywords": ["G2: February", "feb, february"]}],
        "whole_word": True, "case_sensitive": False, "precedence": "keywords_first", "excluded": ["disclaimer"],
        "included": [], "dedupe": True, "search_attachments": True, "use_cache": True, "fallback_source": "imap",
        "timestamp_window_seconds": 60, "date_window_days": 1}


def install_fake_mailbox(setattr_fn):
    """Point the fetcher and sorter at an in-memory mailbox. Also used by gui_smoke.py."""
    FakeSession.store = {
        "101": make_email("Report for January", "Alice <alice@a.com>", "Tue, 06 Oct 2026 07:00:05 +0000",
                          body="See attached."),
        "102": make_email("Scan", "Bob <noreply@b.com>", "Tue, 06 Oct 2026 08:15:00 +0000", reply_to="bob@b.com",
                          html="<p>Scan <img src='cid:logo1'></p>", inline_image=True,
                          attachments=[("scan.pdf", make_pdf("Payroll for February 2026"), "application/pdf"),
                                       ("disclaimer.pdf", make_pdf("january"), "application/pdf")]),
        "103": make_email("Misc", "Carol <carol@c.com>", "Tue, 06 Oct 2026 09:00:00 +0000",
                          attachments=[("notes.docx", make_docx("nothing relevant"),
                                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")]),
        "104": make_email("Old", "Dan <dan@d.com>", "Mon, 05 Oct 2026 07:00:00 +0000"),
    }
    FakeSession.fetched_full = []
    setattr_fn(fetcher, "MailSession", FakeSession)
    setattr_fn(sorter, "MailSession", FakeSession)
    return FakeSession


@pytest.fixture
def mailbox(monkeypatch):
    return install_fake_mailbox(monkeypatch.setattr)


RANGE = (datetime(2026, 10, 6, 0, 0), datetime(2026, 10, 6, 23, 59, 59))


def do_fetch(job, root, csv_path=None, save=True, info=True):
    return fetcher.run_fetch(job, account=ACCOUNT, password="pw", library_root=str(root), tz_text="Africa/Nairobi",
                             start=RANGE[0], end=RANGE[1], info=INFO,
                             opts={"save_attachments": save, "merge_pdfs": True, "emailinfo": info,
                                   "export_csv": bool(csv_path), "csv_path": str(csv_path or "")})


def test_fetch_full_then_sort_library(job, tmp_path, mailbox):
    lib_root = tmp_path / "lib"
    summary = do_fetch(job, lib_root, csv_path=tmp_path / "out.csv")
    assert summary["messages"] == 3, job.text()
    assert summary["attachments"] == 3            # scan.pdf, disclaimer.pdf, notes.docx (inline logo excluded)
    assert summary["pdfs"] == 3, job.text()
    day = lib_root / "2026-10-06"
    bob = day / "bob@b.com - Scan"                # folder named by Reply-To, like the old fetcher
    assert sorted(os.listdir(bob)) == [".message-uid", "EMAILINFO_noreply@b.com.pdf", "MERGED_bob@b.com.pdf",
                                       "disclaimer.pdf", "scan.pdf"]
    assert (day / "alice@a.com - Report for January" / "EMAILINFO_alice@a.com.pdf").exists()

    with open(tmp_path / "out.csv", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["Date Received"] == "2026-10-06 10:00:05"   # Received time, in Nairobi
    assert {r["UID"] for r in rows} == {"101", "102", "103"}

    res = sorter.run_sort_library(job, library_root=str(lib_root), sopts=SORT, account=ACCOUNT, password="pw",
                                  tz_text="Africa/Nairobi")
    assert (res["matched"], res["unmatched"]) == (2, 1), job.text()
    lib = Library(str(lib_root))
    groups = {r["uid"]: r["group_name"] for r in lib.query()}
    assert groups == {"101": "G1/1/1/2026/01", "102": "G2/2/1/2026/02", "103": None}
    lib.close()
    assert "excluded by filter: disclaimer.pdf" in job.text()
    with open(os.path.join(res["out_dir"], "matched.csv"), encoding="utf-8-sig") as f:
        m = list(csv.DictReader(f))
    assert {r["Email"] for r in m} == {"alice@a.com", "bob@b.com"}
    assert os.path.exists(os.path.join(res["out_dir"], "unmatched_uniq.csv"))


def test_emailinfo_only_then_sort_downloads_by_uid(job, tmp_path, mailbox):
    lib_root = tmp_path / "lib"
    summary = do_fetch(job, lib_root, save=False)
    assert summary["attachments"] == 0 and summary["pdfs"] == 3
    assert mailbox.fetched_full == []                      # partial fetches only: attachments never downloaded
    lib = Library(str(lib_root))
    bob = [r for r in lib.query() if r["uid"] == "102"][0]
    assert sorted(a["filename"] for a in lib.attachments(bob["id"])) == ["disclaimer.pdf", "scan.pdf"]
    lib.close()
    res = sorter.run_sort_library(job, library_root=str(lib_root), sopts=SORT, account=ACCOUNT, password="pw",
                                  tz_text="Africa/Nairobi")
    assert res["matched"] == 2, job.text()
    assert "102" in mailbox.fetched_full and "101" not in mailbox.fetched_full
    # second run uses cached attachment text: no more downloads
    mailbox.fetched_full.clear()
    sorter.run_sort_library(job, library_root=str(lib_root), sopts=SORT, account=ACCOUNT, password="pw",
                            tz_text="Africa/Nairobi")
    assert mailbox.fetched_full == ["103"] or mailbox.fetched_full == []


def test_sort_foreign_csv_fuzzy_with_reply_to(job, tmp_path, mailbox):
    """A CSV without UIDs: the row's Email is the Reply-To address and its time is
    local - both used to break the old lookup."""
    p = tmp_path / "in.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Sender", "Email", "Date Received", "Subject", "Body"])
        w.writerow(["Bob", "bob@b.com", "2026-10-06 11:15:00", "Scan", "please see"])
        w.writerow(["Carol", "carol@c.com", "2026-10-06 12:00:00", "Misc", ""])
        w.writerow(["Alice", "alice@a.com", "2026-10-06 10:00:05", "Report", "about january"])
    res = sorter.run_sort_csv(job, csv_path=str(p), out_dir=str(tmp_path / "o"), sopts=SORT, account=ACCOUNT,
                              password="pw", tz_text="Africa/Nairobi")
    assert (res["matched"], res["unmatched"]) == (2, 1), job.text()
    # cached: a rerun needs no server
    mailbox.fetched_full.clear()
    job2 = type(job)()
    sorter.run_sort_csv(job2, csv_path=str(p), out_dir=str(tmp_path / "o"), sopts=dict(SORT, offline=True),
                        account=ACCOUNT, password="pw", tz_text="Africa/Nairobi")
    assert mailbox.fetched_full == [] and "(cached)" in job2.text()


def test_sort_mailtool_csv_uses_uid(job, tmp_path, mailbox):
    out = tmp_path / "f.csv"
    do_fetch(job, tmp_path / "lib", csv_path=out, save=False, info=False)
    mailbox.fetched_full.clear()
    res = sorter.run_sort_csv(job, csv_path=str(out), out_dir=str(tmp_path / "o"), sopts=dict(SORT, use_cache=False),
                              account=ACCOUNT, password="pw", tz_text="Africa/Nairobi")
    assert res["matched"] == 2, job.text()
    assert sorted(mailbox.fetched_full) == ["102", "103"]   # fetched by exact UID, no fuzzy search
