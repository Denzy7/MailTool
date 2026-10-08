"""Type sniffing, conversion to PDF, and PDF repair/merge/subset/preview."""
from __future__ import annotations

import os
import re
import shutil
import threading
import zipfile

from mailtool.core.util import log, run
from mailtool.printing.tools import find_gs, find_tool, get_fitz

try:
    from PIL import Image, ImageOps, ImageSequence
except Exception:
    Image = ImageOps = ImageSequence = None

try:
    import pypdf
except Exception:
    pypdf = None

MAX_FRAMES = 40
THUMB_W = 170
PAPER_PX = {"A4": (1654, 2339), "LETTER": (1700, 2200)}  # 200 dpi


class UserError(Exception):
    """An error whose message is safe and useful to show the user."""


# ----------------------------------------------------------------------------- sniffing
def sniff(path, display=""):
    """(kind, ext) with kind pdf|image|word. Raises UserError."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if not head:
        raise UserError("file is empty")
    if b"%PDF-" in head[:1024]:
        return "pdf", ".pdf"
    if head.startswith(b"\x89PNG"):
        return "image", ".png"
    if head[:3] == b"\xff\xd8\xff":
        return "image", ".jpg"
    if head[:4] == b"GIF8":
        return "image", ".gif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image", ".tif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", ".webp"
    if head[:2] == b"BM" and display.lower().endswith(".bmp"):
        return "image", ".bmp"
    if head.startswith(b"{\\rtf"):
        return "word", ".rtf"
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if "word/document.xml" in names:
                    return "word", ".docx"
                if "mimetype" in names and z.read("mimetype").strip() == b"application/vnd.oasis.opendocument.text":
                    return "word", ".odt"
                if "xl/workbook.xml" in names:
                    raise UserError("Excel files are not supported")
                if any(n.startswith("ppt/") for n in names):
                    raise UserError("PowerPoint files are not supported")
        except zipfile.BadZipFile:
            raise UserError("corrupt or unsupported archive/document")
        raise UserError("unsupported file type")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        with open(path, "rb") as f:
            blob = f.read(4 << 20)
        if "WordDocument".encode("utf-16-le") in blob:
            return "word", ".doc"
        raise UserError("unsupported Office file (only Word documents are supported)")
    raise UserError("unsupported file type (accepted: PDF, Word, ODT, RTF, images)")


# ----------------------------------------------------------------------------- conversion
def _flatten(im):
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


def image_to_pdf(src, out, paper):
    if Image is None:
        raise UserError("images need Pillow")
    W, H = PAPER_PX.get(str(paper).upper(), PAPER_PX["A4"])
    margin = 50
    pages = []
    try:
        with Image.open(src) as im:
            for i, frame in enumerate(ImageSequence.Iterator(im)):
                if i >= MAX_FRAMES:
                    break
                f = _flatten(ImageOps.exif_transpose(frame.copy()))
                pw, ph = (W, H) if f.height >= f.width else (H, W)
                scale = min((pw - 2 * margin) / f.width, (ph - 2 * margin) / f.height)
                nw, nh = max(1, int(f.width * scale)), max(1, int(f.height * scale))
                f = f.resize((nw, nh), Image.LANCZOS)
                page = Image.new("RGB", (pw, ph), "white")
                page.paste(f, ((pw - nw) // 2, (ph - nh) // 2))
                pages.append(page)
    except UserError:
        raise
    except Exception as e:
        raise UserError("image could not be read (%s)" % (str(e)[:80] or e.__class__.__name__))
    if not pages:
        raise UserError("image has no frames")
    pages[0].save(out, "PDF", resolution=200.0, save_all=True, append_images=pages[1:])
    return out


def word_com_convert(src, out, timeout):
    res = {}

    def job():
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            w = doc = None
            try:
                w = win32com.client.DispatchEx("Word.Application")
                w.Visible = False
                w.DisplayAlerts = 0
                doc = w.Documents.Open(os.path.abspath(src), ReadOnly=True, AddToRecentFiles=False,
                                       ConfirmConversions=False)
                doc.ExportAsFixedFormat(os.path.abspath(out), 17)  # wdExportFormatPDF
            finally:
                if doc is not None:
                    try:
                        doc.Close(False)
                    except Exception:
                        pass
                if w is not None:
                    try:
                        w.Quit()
                    except Exception:
                        pass
                pythoncom.CoUninitialize()
        except Exception as e:
            res["err"] = e

    t = threading.Thread(target=job, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise UserError("Word did not respond (a dialog may be open)")
    if "err" in res:
        raise UserError("Word failed: %s" % str(res["err"])[:100])
    if not os.path.isfile(out):
        raise UserError("Word produced no PDF")


def libreoffice_convert(soffice, src, out, timeout):
    import pathlib
    profile = os.path.join(os.path.dirname(src), "lo-profile")  # own profile: parallel conversions work
    cmd = [soffice, "-env:UserInstallation=" + pathlib.Path(os.path.abspath(profile)).as_uri(),
           "--headless", "--convert-to", "pdf", src, "--outdir", os.path.dirname(src)]
    try:
        rc, _o, _e = run(cmd, timeout)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if rc is None:
        raise UserError("LibreOffice timed out converting the document")
    if not os.path.isfile(out):
        raise UserError("LibreOffice could not convert this file (password-protected or corrupt?)")
    return out


# ----------------------------------------------------------------------------- inspection / repair
def inspect_pdf(path):
    """Page count (None if pypdf is missing). Raises UserError for bad PDFs."""
    if pypdf is None:
        return None
    try:
        r = pypdf.PdfReader(path)
        if r.is_encrypted:
            try:
                if not r.decrypt(""):
                    raise UserError("PDF is password-protected")
            except UserError:
                raise
            except Exception:
                return None  # e.g. AES without 'cryptography'; CUPS/Sumatra may still cope
        n = len(r.pages)
    except UserError:
        raise
    except Exception as e:
        raise UserError("PDF is unreadable (%s)" % (str(e)[:60] or e.__class__.__name__))
    if n == 0:
        raise UserError("PDF has no pages")
    return n


def _ap_ok(ap):
    try:
        n = ap.get("/N")
        if n is None:
            return True
        n = n.get_object()
        streams = [n] if hasattr(n, "get_data") else [v.get_object() for v in n.values()]
        return all("/BBox" in s for s in streams)
    except Exception:
        return True


def _annot_bad(a):
    ap = a.get_object().get("/AP")
    return ap is not None and not _ap_ok(ap.get_object())


def sanitize_pdf(path):
    """Drop annotations whose appearance streams lack /BBox (CUPS pdftopdf aborts on them).
    Rewrites only if something was wrong. Returns the number removed."""
    if pypdf is None:
        return 0
    try:
        r = pypdf.PdfReader(path)
        if r.is_encrypted and not r.decrypt(""):
            return 0
        bad = 0
        for page in r.pages:
            annots = page.get("/Annots")
            for a in (annots.get_object() if annots is not None else []):
                bad += _annot_bad(a)
        if not bad:
            return 0
        try:
            w = pypdf.PdfWriter(clone_from=r)
        except TypeError:
            w = pypdf.PdfWriter()
            w.append_pages_from_reader(r)
        for page in w.pages:
            annots = page.get("/Annots")
            if annots is None:
                continue
            keep = pypdf.generic.ArrayObject([a for a in annots.get_object() if not _annot_bad(a)])
            page[pypdf.generic.NameObject("/Annots")] = keep
        tmp = path + ".fixed"
        with open(tmp, "wb") as f:
            w.write(f)
        os.replace(tmp, path)
        log.info("repaired %d broken annotation(s) in %s", bad, path)
        return bad
    except Exception as e:
        log.warning("PDF repair skipped for %s: %s", path, e)
        return 0


def merge_pdfs(paths, out, timeout):
    """Merge PDFs (paths may repeat for copies). Returns engine name. Raises UserError."""
    errs = []
    gs = find_gs()
    if gs:  # full re-render: also cleans structural damage that breaks CUPS filters
        rc, o, e = run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite", "-dPrinted=true",
                        "-sOutputFile=" + out] + list(paths), timeout)
        if rc == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            return "Ghostscript"
        errs.append("Ghostscript failed: %s" % ((e or o or "timeout").strip()[:120]))
        log.warning(errs[-1])
    if pypdf is None:
        raise UserError("; ".join(errs + ["cannot merge PDFs without Ghostscript or pypdf"]))
    try:
        w = pypdf.PdfWriter()
        for pth in paths:
            r = pypdf.PdfReader(pth)
            if r.is_encrypted:
                r.decrypt("")
            for page in r.pages:
                if "/Annots" in page:
                    del page["/Annots"]  # annotations are what breaks pdftopdf; drop them here
                w.add_page(page)
        with open(out, "wb") as f:
            w.write(f)
    except Exception as e:
        raise UserError("merge failed: %s" % str(e)[:120])
    return "pypdf (annotations removed)"


def parse_ranges(text, n):
    """'2-4, 7, 9-' -> {1,2,3,6,8,...} (0-based, clipped to n). Raises ValueError."""
    out = set()
    for tok in re.split(r"[,\s;]+", text.strip()):
        if not tok:
            continue
        m = re.match(r"^(\d*)\s*-\s*(\d*)$", tok)
        if m and (m.group(1) or m.group(2)):
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else n
        elif tok.isdigit():
            a = b = int(tok)
        else:
            raise ValueError(tok)
        if a > b:
            a, b = b, a
        out.update(range(max(a, 1) - 1, min(b, n)))
    return out


def compress_ranges(keep):
    """[0,2,3,4] -> '1,3-5' (1-based, for Ghostscript -sPageList)."""
    keep = sorted(keep)
    parts, i = [], 0
    while i < len(keep):
        j = i
        while j + 1 < len(keep) and keep[j + 1] == keep[j] + 1:
            j += 1
        parts.append(str(keep[i] + 1) if i == j else "%d-%d" % (keep[i] + 1, keep[j] + 1))
        i = j + 1
    return ",".join(parts)


def subset_pdf(src, out, keep, timeout):
    """Write pages `keep` (0-based, in that order) of src to out."""
    if pypdf is not None:
        try:
            r = pypdf.PdfReader(src)
            if r.is_encrypted:
                r.decrypt("")
            w = pypdf.PdfWriter()
            for i in keep:
                w.add_page(r.pages[i])
            with open(out, "wb") as f:
                w.write(f)
            return
        except Exception as e:
            log.warning("pypdf subset failed (%s); trying Ghostscript", e)
    gs = find_gs()
    if not gs:
        raise UserError("excluding/reordering pages needs pypdf or Ghostscript")
    base = [gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite"]
    if list(keep) == sorted(set(keep)):
        rc, o, e = run(base + ["-sPageList=" + compress_ranges(keep), "-sOutputFile=" + out, src], timeout * 3)
        if rc != 0 or not os.path.isfile(out):
            raise UserError("could not drop pages: %s" % (e or o or "timeout").strip()[:100])
        return
    tmpd = out + ".pages"
    os.makedirs(tmpd, exist_ok=True)
    try:
        files = []
        for n, i in enumerate(keep):
            f = os.path.join(tmpd, "%04d.pdf" % n)
            rc, o, e = run(base + ["-dFirstPage=%d" % (i + 1), "-dLastPage=%d" % (i + 1), "-sOutputFile=" + f, src],
                           timeout)
            if rc != 0 or not os.path.isfile(f):
                raise UserError("could not extract page %d: %s" % (i + 1, (e or o or "timeout").strip()[:80]))
            files.append(f)
        merge_pdfs(files, out, timeout * 5)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


# ----------------------------------------------------------------------------- previews
def _collect_pngs(outdir, pat):
    found = []
    for f in os.listdir(outdir):
        m = re.search(pat, f)
        if m:
            found.append((int(m.group(1)) - 1, os.path.join(outdir, f)))
    return sorted(found)


def render_pages(pdf, outdir, timeout, emit, limit=300, width=THUMB_W):
    """Thumbnails `width` px wide, emit(index, path) for each; emit returning True
    aborts. Returns the page count (or None if unknown). Raises UserError."""
    os.makedirs(outdir, exist_ok=True)
    fz = get_fitz()
    if fz:
        try:
            doc = fz.open(pdf)
            if getattr(doc, "needs_pass", False):
                doc.authenticate("")
            total = len(doc)
            for i in range(min(total, limit)):
                pg = doc[i]
                z = width / max(pg.rect.width, 1.0)
                path = os.path.join(outdir, "p%04d.png" % i)
                pg.get_pixmap(matrix=fz.Matrix(z, z)).save(path)
                if emit(i, path):
                    break
            doc.close()
            return total
        except Exception as e:
            raise UserError("preview failed: %s" % str(e)[:100])
    pp = find_tool(["pdftoppm"])
    if pp:
        base = [pp, "-png", "-l", str(limit)]
        run(base + ["-scale-to-x", str(width), "-scale-to-y", "-1", pdf, os.path.join(outdir, "p")], timeout)
        pat = r"p-(\d+)\.png$"
        if not _collect_pngs(outdir, pat):  # older poppler without -scale-to-x
            run(base + ["-scale-to", str(int(width * 1.35)), pdf, os.path.join(outdir, "p")], timeout)
    else:
        gs = find_gs()
        if not gs:
            raise UserError("no page renderer found")
        run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dLastPage=%d" % limit, "-sDEVICE=png16m",
             "-r%d" % max(10, int(width / 8.27)), "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
             "-sOutputFile=" + os.path.join(outdir, "g%04d.png"), pdf], timeout)
        pat = r"g(\d+)\.png$"
    found = _collect_pngs(outdir, pat)
    if not found:
        raise UserError("preview rendering produced no images")
    for i, path in found:
        if emit(i, path):
            break
    return None


def render_page(pdf, index, out, width, timeout):
    """One page (0-based) `width` px wide to PNG `out`. Raises UserError."""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fz = get_fitz()
    if fz:
        try:
            doc = fz.open(pdf)
            if getattr(doc, "needs_pass", False):
                doc.authenticate("")
            pg = doc[index]
            z = width / max(pg.rect.width, 1.0)
            pg.get_pixmap(matrix=fz.Matrix(z, z)).save(out)
            doc.close()
            return out
        except Exception as e:
            raise UserError("render failed: %s" % str(e)[:100])
    pp = find_tool(["pdftoppm"])
    if pp:
        run([pp, "-png", "-f", str(index + 1), "-l", str(index + 1), "-singlefile", "-scale-to-x", str(width),
             "-scale-to-y", "-1", pdf, out[:-4]], timeout)
        if os.path.isfile(out):
            return out
        raise UserError("pdftoppm could not render this page")
    gs = find_gs()
    if gs:
        run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dFirstPage=%d" % (index + 1),
             "-dLastPage=%d" % (index + 1), "-sDEVICE=png16m", "-r%d" % max(20, int(width / 8.27)),
             "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4", "-sOutputFile=" + out, pdf], timeout)
        if os.path.isfile(out):
            return out
        raise UserError("Ghostscript could not render this page")
    raise UserError("no page renderer found")
