import os
from datetime import datetime, timezone

import pytest

from mailtool.core import timeutil
from mailtool.core.config import Config
from mailtool.library.db import Library
from mailtool.library.folders import claim_message_dir, ordered_print_files
from mailtool.mail import bodystructure as bs
from mailtool.mail.mime import decode_mime_words, resolve_sender
from mailtool.printing.pdfops import compress_ranges, parse_ranges, sniff, UserError
from mailtool.printing.resolve import classify, expand_folder
from mailtool.sort.extract import filter_decision
from mailtool.sort.groups import compile_groups, match, split_keyword_lines, whole_word_near_misses


# ---------------------------------------------------------------- time
def test_timezone_names_and_offsets():
    nb = timeutil.resolve_timezone("Africa/Nairobi")
    assert datetime(2026, 1, 1, tzinfo=nb).utcoffset().total_seconds() == 3 * 3600
    assert timeutil.resolve_timezone("+05:30").utcoffset(None).total_seconds() == 5.5 * 3600
    assert timeutil.resolve_timezone("UTC-4").utcoffset(None).total_seconds() == -4 * 3600
    assert timeutil.timezone_ok("Africa/Nairobi") and timeutil.timezone_ok("+03:00")
    assert not timeutil.timezone_ok("Mars/Olympus")


def test_received_header_wins_over_date():
    import email
    m = email.message_from_string(
        "Received: from a by b; Tue, 6 Oct 2026 07:00:05 +0000\nDate: Tue, 6 Oct 2026 06:59:00 +0000\n\nx")
    dt, src = timeutil.get_received_datetime(m)
    assert src == "received" and dt.minute == 0 and dt.second == 5
    local = timeutil.to_local(dt, timeutil.resolve_timezone("Africa/Nairobi"))
    assert local.hour == 10


def test_naive_csv_time_compares_as_local():
    """The old grouper compared a naive local CSV time with a +0000 header by wall
    clock (3 h off in Nairobi). Naive must now count as local."""
    tz = timeutil.resolve_timezone("Africa/Nairobi")
    csv_time = datetime(2026, 10, 6, 10, 0, 5)                      # local, as MailTool writes it
    header = datetime(2026, 10, 6, 7, 0, 5, tzinfo=timezone.utc)
    assert timeutil.delta_seconds(csv_time, header, tz) == 0


def test_user_range_validation():
    s, e = timeutil.parse_user_range("01-Aug-2026", "09:00", "02-Aug-2026", "17:30")
    assert s < e and e.second == 59
    with pytest.raises(ValueError):
        timeutil.parse_user_range("02-Aug-2026", "00:00", "01-Aug-2026", "00:00")
    with pytest.raises(ValueError):
        timeutil.parse_user_range("2026-08-01", "00:00", "01-Aug-2026", "00:00")


# ---------------------------------------------------------------- mime / bodystructure
def test_reply_to_preferred():
    name, addr, used = resolve_sender('"Acme" <noreply@acme.com>', "help@acme.com")
    assert (name, addr, used) == ("Acme", "help@acme.com", True)
    assert resolve_sender("a@b.c", "")[1:] == ("a@b.c", False)


def test_decode_words():
    assert decode_mime_words("=?utf-8?q?Caf=C3=A9_menu?=") == "Café menu"
    assert decode_mime_words("=?bogus-charset?q?abc?=") == "abc"


SAMPLE = [(b'1 (UID 9 BODYSTRUCTURE ((("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 120 4 NIL NIL NIL NIL)'
           b'("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL "BASE64" 900 12 NIL NIL NIL NIL) "ALTERNATIVE" NIL NIL NIL NIL)'
           b'("IMAGE" "PNG" ("NAME" "logo.png") "<logo1>" NIL "BASE64" 300 NIL ("INLINE" ("FILENAME" "logo.png")) NIL NIL)'
           b'("APPLICATION" "PDF" ("NAME" {14}', b'Report (1).pdf'),
          b') NIL NIL "BASE64" 52000 NIL ("ATTACHMENT" ("FILENAME" "Report (1).pdf")) NIL NIL) "MIXED" NIL NIL NIL NIL))']


def test_bodystructure_with_literal_filename():
    st = bs.parse_response(SAMPLE)
    assert st is not None
    assert bs.pick_body_part(st)[:2] == ("1.1", "PLAIN")
    assert bs.pick_html_part(st)[0] == "1.2"
    atts = list(bs.find_attachment_parts(st, ("1.1",)))
    assert ("Report (1).pdf", 52000, "3") in atts
    assert bs.find_part_by_cid(st, "logo1") == ("2", "image/png", "BASE64")
    html, inl = bs.inline_cid_images('<img src="cid:logo1">', st, lambda p: b"iVBORw0KGgo=")
    assert html.startswith('<img src="data:image/png;base64,') and inl == {"2"}
    assert [a[0] for a in bs.find_attachment_parts(st, ("1.1",), skip_parts=inl)] == ["Report (1).pdf"]


# ---------------------------------------------------------------- groups
GROUPS = [
    {"group_name": "G1/1/1/2026/01", "keywords": ["G1: January", "jan, january, jaan", "first quarter notes"]},
    {"group_name": "G2/2/1/2026/02", "keywords": ["G2: February", "feb, february"]},
]


def test_keyword_lines():
    assert split_keyword_lines(["Desc", "a, b,, c,", "x, y"]) == ("Desc", ["a", "b", "c"], ["x, y"])


def test_match_scopes_and_precedence():
    gs = compile_groups(GROUPS)
    assert match("this is for january", gs)[0].group_name == "G1/1/1/2026/01"
    # line 3+ keywords are subject/body only
    assert match("first quarter notes", gs)[0] is not None
    assert match("first quarter notes", gs, scope="attach")[0] is None
    # group name fallback, and precedence order
    g, term, stage = match("ref G2/2/1/2026/02 jan", gs)
    assert (g.group_name, stage) == ("G1/1/1/2026/01", "keyword")
    g, term, stage = match("ref G2/2/1/2026/02 jan", gs, precedence="groupname_first")
    assert (g.group_name, stage) == ("G2/2/1/2026/02", "group name")
    # whole word
    assert match("jaan2026", gs, scope="attach")[0] is None
    assert whole_word_near_misses("jaan2026", gs) == ["jaan"]
    assert match("jaan2026", compile_groups(GROUPS, whole_word=False), scope="attach")[0] is not None


def test_attachment_filters():
    assert filter_decision("notes_reports_and_work.pdf", ["notes"], []) == "skip"
    assert filter_decision("notes_reports_and_work.pdf", ["notes"], ["notes_reports*"]) == "override"
    assert filter_decision("Invoice.PDF", ["*_logo.*"], []) == "keep"
    assert filter_decision("company_logo.png", ["*_logo.*"], []) == "skip"


# ---------------------------------------------------------------- printing helpers
def test_page_ranges():
    assert parse_ranges("2-4, 7, 9-", 10) == {1, 2, 3, 6, 8, 9}
    assert parse_ranges("-2", 5) == {0, 1}
    with pytest.raises(ValueError):
        parse_ranges("abc", 3)
    assert compress_ranges([0, 2, 3, 4]) == "1,3-5"


def test_classify_drops():
    s = classify("file:///home/a/My%20File.pdf")
    assert s.kind == "path" and s.path == "/home/a/My File.pdf"
    w = classify("webdavs://user@host/dav/a #1.pdf")
    assert w.kind == "url" and w.display == "a #1.pdf" and w.url.startswith("webdavs://user@host/dav/a%20%231.pdf")
    assert classify("https://example.com/x.pdf") is None


def test_sniff(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"%PDF-1.4 ...")
    assert sniff(str(p)) == ("pdf", ".pdf")
    p.write_bytes(b"\x89PNG....")
    assert sniff(str(p))[0] == "image"
    p.write_bytes(b"hello")
    with pytest.raises(UserError):
        sniff(str(p))


# ---------------------------------------------------------------- library + folders
def test_claim_message_dir_unique(tmp_path):
    a = claim_message_dir(str(tmp_path), "2026-10-06", "x@y.z", "Same subject", "INBOX", "1")
    b = claim_message_dir(str(tmp_path), "2026-10-06", "x@y.z", "Same subject", "INBOX", "2")
    a2 = claim_message_dir(str(tmp_path), "2026-10-06", "x@y.z", "Same subject", "INBOX", "1")
    assert a == a2 and a != b and "_UID2" in b


def test_print_order_and_folder_expansion(tmp_path):
    d = claim_message_dir(str(tmp_path), "2026-10-06", "x@y.z", "Subj", "INBOX", "1")
    for n in ("b.pdf", "a.docx", "EMAILINFO_x@y.z.pdf", "MERGED_x@y.z.pdf"):
        open(os.path.join(d, n), "wb").write(b"x")
    names = [os.path.basename(p) for p in ordered_print_files(d)]
    assert names == ["EMAILINFO_x@y.z.pdf", "a.docx", "b.pdf"]
    assert [os.path.basename(p) for p in expand_folder(str(tmp_path))] == names


def test_library_roundtrip(tmp_path):
    lib = Library(str(tmp_path))
    ident = {"account": "me@srv", "mailbox": "INBOX", "uidvalidity": "1", "uid": "5"}
    mid = lib.upsert_message(dict(ident, subject="Hi", received_local="2026-10-06 10:00:00", sender_email="a@b"))
    assert lib.upsert_message(dict(ident, subject=None, body="text")) == mid
    row = lib.message(mid)
    assert row["subject"] == "Hi" and row["body"] == "text"
    lib.set_attachments(mid, [{"filename": "a.pdf", "size": 10, "path": str(tmp_path / "a.pdf")}])
    att = lib.attachments(mid)[0]
    lib.set_attachment_text(att["id"], "hello", "ok")
    lib.set_attachments(mid, [{"filename": "a.pdf", "size": 10, "part": "2"}])   # metadata refresh keeps text
    att = lib.attachments(mid)[0]
    assert att["text"] == "hello" and att["path"] == "a.pdf" and att["part"] == "2"
    lib.set_group(mid, "G1")
    assert lib.groups_in_use() == ["G1"]
    assert len(lib.query(start="2026-10-06 00:00:00", end="2026-10-06 23:59:59")) == 1
    assert len(lib.query(group="")) == 0
    lib.close()


def test_config_never_stores_password(tmp_path):
    c = Config(str(tmp_path / "s.json"))
    c["account"]["password"] = "secret"
    c.set("general", "timezone", "+03:00")
    c.save()
    text = open(c.path).read()
    assert "secret" not in text
    assert Config(str(tmp_path / "s.json")).get("general", "timezone") == "+03:00"
