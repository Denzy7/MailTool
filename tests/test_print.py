"""Print queue engine: staging, type detection, duplicates, conversion, merging."""
import io
import os
import time

import pytest

from fakes import make_docx, make_pdf
from mailtool.printing import pdfops
from mailtool.printing.queue import Item, PrintCore
from mailtool.printing.resolve import classify
from mailtool.printing.tools import find_soffice


def wait(items, timeout=120):
    end = time.time() + timeout
    while time.time() < end and any(i.status in ("queued", "staging", "converting") for i in items):
        time.sleep(0.1)


@pytest.fixture
def core(tmp_path):
    events = []
    c = PrintCore({"temp_dir": str(tmp_path / "tmp"), "convert_timeout": 120, "copy_timeout": 60,
                   "paper": "A4", "max_parallel": 4}, events.append)
    c.events = events
    yield c
    c.cleanup()


def test_queue_pipeline_and_merge(core, tmp_path):
    from PIL import Image
    files = {}
    files["a.pdf"] = make_pdf("first")
    files["b copy.pdf"] = files["a.pdf"]                         # same bytes -> duplicate
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), "teal").save(buf, "PNG")
    files["pic.png"] = buf.getvalue()
    files["bad.txt"] = b"just text"
    if find_soffice({}):
        files["doc.docx"] = make_docx("hello from word")
    paths = []
    for n, data in files.items():
        p = tmp_path / n
        p.write_bytes(data)
        paths.append(str(p))
    items = [Item(classify(p)) for p in paths]
    for it in items:
        core.submit(it)
    wait(items)
    st = {it.display: it.status for it in items}
    assert st["a.pdf"] == "ready" and st["b copy.pdf"] == "dup"
    assert st["pic.png"] == "ready" and st["bad.txt"] == "failed"
    if "doc.docx" in st:
        assert st["doc.docx"] == "ready", [(i.display, i.msg) for i in items]
    ready = [i for i in items if i.status == "ready"]
    ready[0].copies = 2
    out, engine = core.build_merged(ready, str(tmp_path / "m"))
    assert pdfops.inspect_pdf(out) == len(ready) + 1


def test_evicted_duplicate_stays_dup(core, tmp_path, monkeypatch):
    """The later-dropped copy wins the race to the hash, then the first one evicts it mid-processing:
    its worker must not flip it back to ready."""
    data, paths = make_pdf("same"), []
    for n in ("a.pdf", "b.pdf"):
        p = tmp_path / n
        p.write_bytes(data)
        paths.append(str(p))
    a, b = (Item(classify(p)) for p in paths)
    stage, sanitize = core.stage, pdfops.sanitize_pdf

    def slow_stage(src, dst):
        if src.path == paths[0]:
            time.sleep(0.3)                     # a hashes after b ...
        stage(src, dst)

    def slow_sanitize(pdf):
        if b.dir and pdf.startswith(b.dir):
            time.sleep(0.8)                     # ... while b is still being processed
        return sanitize(pdf)
    monkeypatch.setattr(core, "stage", slow_stage)
    monkeypatch.setattr(pdfops, "sanitize_pdf", slow_sanitize)
    core.submit(a)
    core.submit(b)
    wait([a, b])
    time.sleep(1.0)                             # let b's worker finish
    assert (a.status, b.status) == ("ready", "dup")
    assert ("dup", b) in core.events and ("upd", b) not in core.events[core.events.index(("dup", b)):]


def test_subset_reorder(core, tmp_path):
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for t in ("one", "two", "three"):
        w.append(io.BytesIO(make_pdf(t)))
    p = tmp_path / "three.pdf"
    with open(p, "wb") as f:
        w.write(f)
    it = Item(classify(str(p)))
    core.submit(it)
    wait([it])
    assert it.pages == 3
    it.excluded = {1}
    it.page_order = [2, 0, 1]
    out = core.effective_pdf(it)
    texts = [pg.extract_text().strip() for pg in PdfReader(out).pages]
    assert texts == ["three", "one"]


def test_folder_expansion_print_order(tmp_path):
    from mailtool.library.folders import claim_message_dir
    from mailtool.printing.resolve import expand_folder
    d = claim_message_dir(str(tmp_path), "2026-10-06", "a@b.c", "S", "INBOX", "9")
    for n in ("z.pdf", "EMAILINFO_a@b.c.pdf", "MERGED_a@b.c.pdf"):
        open(os.path.join(d, n), "wb").write(b"%PDF-1.4")
    assert [os.path.basename(x) for x in expand_folder(d)] == ["EMAILINFO_a@b.c.pdf", "z.pdf"]
