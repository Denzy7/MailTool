"""EMAILINFO PDFs: a webmail-style print of one email (Subject/From/To/Date block,
body, attachments list) on A4, with a header/footer on every page.

Body modes:
  print  headless Chromium prints the real HTML (selectable text, live links)
  image  headless Chromium screenshots, cut into pages (exact pixels)
  text   plain text laid out by reportlab
The sender's JavaScript never runs; nothing but images is ever fetched remotely."""
from __future__ import annotations

import html as _h
import io
import os
import re

from mailtool.core.util import human_size

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.platypus import Image as RLImage
    REPORTLAB = True
except Exception:
    REPORTLAB = False
    rl_canvas = None

try:
    from PIL import Image as PILImage
except Exception:
    PILImage = None

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT = True
except Exception:
    sync_playwright = None
    PLAYWRIGHT = False

try:
    from pypdf import PdfReader, PdfWriter
    PYPDF = True
except Exception:
    PYPDF = False

GRAPHICAL = REPORTLAB and PLAYWRIGHT and PILImage is not None

TEMPLATE_SPECIFIERS = ("UID", "EMAIL", "SUBJECT", "MAILBOX", "DATE")

BODY_MODES = {
    "print": "Printed (selectable text & links)",
    "image": "Image snapshot",
    "text": "Plain text",
}

# Usable frame inside the page margins (A4 minus margins and reportlab's 6pt frame padding).
PDF_CONTENT_WIDTH_CM = 16.9
PDF_CONTENT_HEIGHT_CM = 24.8
RENDER_WIDTH_PX = round(PDF_CONTENT_WIDTH_CM / 2.54 * 96)

MAX_REMOTE_IMAGE_BYTES = 15 * 1024 * 1024
MAX_REMOTE_REDIRECTS = 5
MAX_RENDER_WIDTH_CSS = 2400
MAX_RENDER_PAGES = 60
RENDER_TILE_HEIGHT_CSS = 1000

# level -> (name, browser pixel scale, max output width px across the A4 text width)
QUALITY_LEVELS = {1: ("Draft", 1, 800), 2: ("Normal", 2, 1600), 3: ("High", 3, 2400), 4: ("Max", 4, 3200)}
DEFAULT_QUALITY = 2
WORKER_MEMORY_MB = {1: 450, 2: 700, 3: 800, 4: 900}     # image mode, worst case per worker
PRINT_WORKER_MEMORY_MB = 450

PRINT_WIDTH_CSS = round((21.0 - 2 * 1.8) / 2.54 * 96)
PRINT_MARGINS = {"top": "2.2cm", "bottom": "2.0cm", "left": "1.8cm", "right": "1.8cm"}


def quality_label(level):
    name, _, out_w = QUALITY_LEVELS[level]
    return f"{name} (~{round(out_w / (PDF_CONTENT_WIDTH_CM / 2.54))} dpi)"


def expand_template(template, values):
    """<UID> <EMAIL> <SUBJECT> <MAILBOX> <DATE> (any case) -> values. Unknown <...> kept."""
    def sub(m):
        key = m.group(1).upper()
        return str(values[key]) if key in values else m.group(0)
    return re.sub(r"<([A-Za-z]+)>", sub, template or "")


def available_memory_mb():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullAvailPhys // (1024 * 1024)
    except Exception:
        pass
    return None


def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_URL_RE = re.compile(r"(https?://[^\s<>\"]+)")


def _linkify(escaped):
    def sub(m):
        url = m.group(1).rstrip(".,;:!?)]'")
        return f'<a href="{url}" color="#0f6e6e">{url}</a>{m.group(1)[len(url):]}'
    return _URL_RE.sub(sub, escaped)


def _bold_stars(escaped):
    return re.sub(r"\*([^*\n]+?)\*", r"<b>\1</b>", escaped)


# ----------------------------------------------------------------------------- page decorations
def draw_page_decorations(c, page_no, total_pages, header_left, header_right, stamp):
    width, height = A4
    margin = 1.5 * cm
    y_top = height - 1.0 * cm
    c.setFont("Helvetica", 10)
    left_w = c.stringWidth(header_left, "Helvetica", 10)
    c.drawString(margin, y_top, header_left)
    room = width - 2 * margin - left_w - (0.8 * cm if header_left else 0)
    size = 10
    while size > 6 and c.stringWidth(header_right, "Helvetica", size) > room:
        size -= 0.5
    shown = header_right
    while shown and c.stringWidth(shown, "Helvetica", size) > room:
        shown = shown[:-2] + "…"
    c.setFont("Helvetica", size)
    c.drawRightString(width - margin, y_top, shown)
    if re.match(r"https?://", header_right or ""):
        tw = c.stringWidth(shown, "Helvetica", size)
        c.linkURL(header_right, (width - margin - tw, y_top - 2, width - margin, y_top + size), relative=0)
    c.setFont("Helvetica", 10)
    c.drawString(margin, 1.0 * cm, f"Page {page_no} of {total_pages}")
    c.drawRightString(width - margin, 1.0 * cm, stamp)


if REPORTLAB:
    class _InfoPdfCanvas(rl_canvas.Canvas):
        """Two-pass canvas so every page knows the total for 'Page X of Y'."""

        def __init__(self, *args, **kwargs):
            self._header_left = kwargs.pop("header_left", "")
            self._header_right = kwargs.pop("header_right", "")
            self._stamp = kwargs.pop("generated_stamp", "")
            rl_canvas.Canvas.__init__(self, *args, **kwargs)
            self._saved_page_states = []

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_page_states)
            for state in self._saved_page_states:
                # restoring would also rewind reportlab's link counter -> duplicate
                # link names on multi-page PDFs; carry it forward
                link_count = getattr(self, "_annotationCount", 0)
                self.__dict__.update(state)
                self._annotationCount = max(link_count, getattr(self, "_annotationCount", 0))
                draw_page_decorations(self, self._pageNumber, total, self._header_left, self._header_right,
                                      self._stamp)
                rl_canvas.Canvas.showPage(self)
            rl_canvas.Canvas.save(self)


def _field_table(subject, from_display, to_display, date_str):
    label_style = ParagraphStyle("Label", fontName="Helvetica-Bold", fontSize=9, leading=13)
    value_style = ParagraphStyle("Value", fontName="Helvetica", fontSize=9, leading=13)
    fields = [("Subject", subject or "(no subject)"), ("From", from_display or "(unknown)"),
              ("To", to_display or "(unknown)"), ("Date", date_str)]
    rows = [[Paragraph(_esc(a), label_style), Paragraph(_esc(b), value_style)] for a, b in fields]
    table = Table(rows, colWidths=[2.2 * cm, None])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -1), 0.75, colors.HexColor("#d9d9d9")),
    ]))
    return table


def first_page_room_cm(subject, from_display, to_display, date_str):
    _, h = _field_table(subject, from_display, to_display, date_str).wrap(
        PDF_CONTENT_WIDTH_CM * cm, PDF_CONTENT_HEIGHT_CM * cm)
    return PDF_CONTENT_HEIGHT_CM - h / cm - 0.5 - 0.2


def build_pdf(path, header_left, header_right, subject, from_display, to_display, date_str,
              body_text, attachments, stamp, body_strips=None):
    """Text or image-strip layout via reportlab. attachments: [(name, size)]."""
    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=2.2 * cm, bottomMargin=2.0 * cm,
                            leftMargin=1.8 * cm, rightMargin=1.8 * cm, title=subject or "", author="")
    body_style = ParagraphStyle("Body", fontName="Helvetica", fontSize=10, leading=14, spaceAfter=4)
    head_style = ParagraphStyle("AttachHeading", fontName="Helvetica-Bold", fontSize=9, leading=13, spaceBefore=10)
    item_style = ParagraphStyle("AttachItem", fontName="Helvetica", fontSize=9, leading=13)
    story = [_field_table(subject, from_display, to_display, date_str), Spacer(1, 0.5 * cm)]
    if body_strips:
        for png, height_cm in body_strips:
            story.append(RLImage(io.BytesIO(png), width=PDF_CONTENT_WIDTH_CM * cm, height=height_cm * cm))
    else:
        for line in (body_text or "(no body text)").splitlines() or [""]:
            story.append(Paragraph(_bold_stars(_linkify(_esc(line))) or "&nbsp;", body_style))
    if attachments:
        story.append(Paragraph("Attachments", head_style))
        for name, size in attachments:
            story.append(Paragraph(_esc(f"{name} ({human_size(size)})"), item_style))

    def make_canvas(*args, **kwargs):
        return _InfoPdfCanvas(*args, header_left=header_left, header_right=header_right,
                              generated_stamp=stamp, **kwargs)
    doc.build(story, canvasmaker=make_canvas)


_BLOCK_FONT = "font-family:Helvetica,Arial,'Liberation Sans',sans-serif;color:#000;"


def info_block_html(subject, from_display, to_display, date_str):
    rows = "".join(f'<tr><td class="l">{_h.escape(a)}</td><td>{_h.escape(b)}</td></tr>'
                   for a, b in (("Subject", subject or "(no subject)"), ("From", from_display or "(unknown)"),
                                ("To", to_display or "(unknown)"), ("Date", date_str)))
    return (f"<style>table{{{_BLOCK_FONT}font-size:9pt;width:100%;border-collapse:collapse;margin:0 0 16px 0}}"
            "td{padding:4px 0;border-bottom:0.75pt solid #d9d9d9;vertical-align:top;line-height:1.4}"
            "td.l{font-weight:bold;width:2.2cm;padding-right:8px}</style>"
            f"<table>{rows}</table>")


def attachments_block_html(attachments):
    if not attachments:
        return ""
    items = "".join(f"<div>{_h.escape(f'{n} ({human_size(s)})')}</div>" for n, s in attachments)
    return (f"<style>div.a{{{_BLOCK_FONT}font-size:9pt;line-height:1.45;margin-top:16px}}"
            "b{display:block;margin-bottom:2px}</style>"
            f'<div class="a"><b>Attachments</b>{items}</div>')


def compose_printed_pdf(path, body_pdf, header_left, header_right, stamp, title=""):
    """Lay header/footer over Chromium's printed pages (text and links kept as-is)."""
    body = PdfReader(io.BytesIO(body_pdf))
    total = len(body.pages)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    for i in range(total):
        draw_page_decorations(c, i + 1, total, header_left, header_right, stamp)
        c.showPage()
    c.save()
    overlay = PdfReader(io.BytesIO(buf.getvalue()))
    out = PdfWriter()
    for i, page in enumerate(body.pages):
        page.merge_page(overlay.pages[i])
        out.add_page(page)
    if title:
        out.add_metadata({"/Title": title})
    with open(path, "wb") as fh:
        out.write(fh)


# ----------------------------------------------------------------------------- Chromium renderer
def _looks_like_image(body):
    head = body[:16]
    return (head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") or head.startswith(b"GIF8")
            or (head.startswith(b"RIFF") and body[8:12] == b"WEBP") or head.startswith(b"BM")
            or head.startswith(b"\x00\x00\x01\x00") or b"<svg" in body[:512].lower())


_EXPAND_SCROLL_BOXES_JS = """() => {
  for (const e of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(e);
    const scrolls = /(auto|scroll)/.test(cs.overflowY + ' ' + cs.overflowX);
    if (scrolls && (e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1)) {
      e.style.setProperty('overflow', 'visible', 'important');
      e.style.setProperty('height', 'auto', 'important');
      e.style.setProperty('max-height', 'none', 'important');
    }
  }
}"""

_MEASURE_CONTENT_JS = """() => {
  let bottom = 0;
  for (const e of document.body.querySelectorAll('*')) {
    const r = e.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) bottom = Math.max(bottom, r.bottom);
  }
  const pad = parseFloat(getComputedStyle(document.body).paddingBottom) || 0;
  const height = Math.ceil(bottom + window.scrollY + pad) || document.documentElement.scrollHeight;
  const width = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
  return [width, Math.max(height, 1)];
}"""

_EAGER_IMAGES_JS = """() => { for (const i of document.querySelectorAll('img[loading]')) i.loading = 'eager'; }"""
_IMAGES_SETTLED_JS = "() => Array.from(document.images).every(i => i.complete)"

# Our header block and attachments list go into the printed page as real text,
# inside a shadow root so the email's CSS can't restyle them (and theirs can't leak).
_INJECT_BLOCKS_JS = """(a) => {
  const make = (html) => {
    const host = document.createElement('div');
    host.style.cssText = 'all:initial;display:block;zoom:' + a.zoom + ';width:' + (100 / a.zoom) + '%;';
    host.attachShadow({mode: 'open'}).innerHTML = html;
    return host;
  };
  if (a.top) document.body.insertBefore(make(a.top), document.body.firstChild);
  if (a.bottom) document.body.appendChild(make(a.bottom));
}"""


class HtmlRenderer:
    """One headless Chromium per worker thread. Sender JavaScript is disabled.
    Network is blocked except, optionally, http(s) images (redirects followed,
    used only if the final response really is an image)."""

    def __init__(self, width_px=RENDER_WIDTH_PX, allow_remote_images=False):
        self.width_px = width_px
        self.allow_remote_images = allow_remote_images
        self.stats = {}
        self._pw = None
        self._browser = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch()
        except Exception:
            self._pw.stop()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _handle_request(self, route):
        req = route.request
        if (self.allow_remote_images and req.resource_type == "image"
                and req.url.lower().startswith(("http://", "https://"))):
            try:
                resp = route.fetch(max_redirects=MAX_REMOTE_REDIRECTS, timeout=10000)
                body = resp.body()
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if (resp.ok and 0 < len(body) <= MAX_REMOTE_IMAGE_BYTES
                        and (ctype.startswith("image/") or _looks_like_image(body))):
                    route.fulfill(status=200, body=body, content_type=ctype if ctype.startswith("image/") else None)
                    self.stats["remote_images"] = self.stats.get("remote_images", 0) + 1
                    return
            except Exception:
                pass
        if req.url.startswith("data:"):
            route.continue_()
            return
        self.stats["blocked"] = self.stats.get("blocked", 0) + 1
        route.abort()

    @staticmethod
    def _document(html):
        base = ('<meta charset="utf-8"><style>'
                "html,body{background:#fff;height:auto!important;min-height:0!important;"
                "overflow:visible!important;}"
                "body{margin:0;padding:6px;font-family:Arial,Helvetica,sans-serif;}"
                "img{max-width:100%;height:auto;}</style>")
        # keep the email's <!DOCTYPE> first, or Chromium switches to quirks mode
        m = re.match(r"\s*(<!doctype[^>]*>)", html, flags=re.IGNORECASE)
        return (m.group(1) + base + html[m.end():]) if m else (base + html)

    def print_pdf(self, html, top_html="", bottom_html=""):
        self.stats = {"remote_images": 0, "blocked": 0, "truncated": False}
        context = self._browser.new_context(java_script_enabled=False,
                                            viewport={"width": PRINT_WIDTH_CSS, "height": RENDER_TILE_HEIGHT_CSS})
        try:
            context.route("**/*", self._handle_request)
            page = context.new_page()
            page.set_content(self._document(html), wait_until="domcontentloaded", timeout=30000)
            page.evaluate(_EAGER_IMAGES_JS)
            try:
                page.wait_for_load_state("load", timeout=30000)
            except Exception:
                pass
            for _ in range(75):        # up to 15 s for remote images
                if page.evaluate(_IMAGES_SETTLED_JS):
                    break
                page.wait_for_timeout(200)
            page.evaluate(_EXPAND_SCROLL_BOXES_JS)
            width, _ = page.evaluate(_MEASURE_CONTENT_JS)
            width = min(max(width, PRINT_WIDTH_CSS), MAX_RENDER_WIDTH_CSS)
            scale = max(0.1, min(1.0, PRINT_WIDTH_CSS / width))
            page.evaluate(_INJECT_BLOCKS_JS, {"top": top_html, "bottom": bottom_html, "zoom": 1 / scale})
            return page.pdf(format="A4", print_background=True, scale=scale,
                            margin=PRINT_MARGINS, prefer_css_page_size=False)
        finally:
            context.close()

    def _load(self, html, scale):
        context = self._browser.new_context(java_script_enabled=False,
                                            viewport={"width": self.width_px, "height": RENDER_TILE_HEIGHT_CSS},
                                            device_scale_factor=scale)
        try:
            context.route("**/*", self._handle_request)
            page = context.new_page()
            page.set_content(self._document(html), wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("load", timeout=30000)
            except Exception:
                pass
            page.evaluate(_EXPAND_SCROLL_BOXES_JS)
            width, height = page.evaluate(_MEASURE_CONTENT_JS)
            if width > self.width_px:
                width = min(width, MAX_RENDER_WIDTH_CSS)
                page.set_viewport_size({"width": width, "height": RENDER_TILE_HEIGHT_CSS})
                width, height = page.evaluate(_MEASURE_CONTENT_JS)
                width = min(width, MAX_RENDER_WIDTH_CSS)
            return context, page, max(width, self.width_px), height
        except Exception:
            context.close()
            raise

    def render_strips(self, html, first_room_cm, quality=DEFAULT_QUALITY):
        """Render and return page-sized PNG strips [(png_bytes, height_cm)], captured
        one viewport tile at a time and cut into pages as they arrive."""
        _, level_scale, max_out_w = QUALITY_LEVELS.get(quality, QUALITY_LEVELS[DEFAULT_QUALITY])
        self.stats = {"remote_images": 0, "blocked": 0, "truncated": False}
        context, page, width, height = self._load(html, level_scale)
        try:
            # a wide email gets shrunk anyway: render at just enough scale
            scale = round(min(level_scale, max(1.0, max_out_w * 1.25 / width)), 3)
            if scale < level_scale - 0.01:
                context.close()
                self.stats["remote_images"] = self.stats["blocked"] = 0
                context, page, width, height = self._load(html, scale)

            def px(css):
                return int(round(css * scale))

            out_w = min(px(width), max_out_w)
            factor = out_w / px(width)
            slicer = _PageSlicer(out_w, first_room_cm)
            tile = RENDER_TILE_HEIGHT_CSS
            y = 0
            while y < height and not slicer.full:
                scroll_to = min(y, max(0, height - tile))
                page.evaluate(f"window.scrollTo(0, {scroll_to})")
                shot = PILImage.open(io.BytesIO(page.screenshot(type="png"))).convert("RGB")
                keep_css = min(tile - (y - scroll_to), height - y)
                top_px = px(y - scroll_to)
                part = shot.crop((0, top_px, min(shot.width, px(width)), min(shot.height, top_px + px(keep_css))))
                del shot
                if factor < 1:
                    part = part.resize((out_w, max(1, round(part.height * factor))), PILImage.LANCZOS)
                slicer.add(part)
                y += keep_css
            strips = slicer.finish()
            if slicer.full and (y < height or slicer.buf is not None):
                self.stats["truncated"] = True
            return strips
        finally:
            context.close()


class _PageSlicer:
    """Cuts rendered rows into page-height PNG strips, snapping each cut to a blank
    pixel row so a line of text is never split across pages."""

    def __init__(self, width_px, first_room_cm):
        self.w = width_px
        self.px_per_cm = width_px / PDF_CONTENT_WIDTH_CM
        room = first_room_cm if first_room_cm >= 3 else PDF_CONTENT_HEIGHT_CM
        self.room_px = int(room * self.px_per_cm)
        self.buf = None
        self.strips = []

    @property
    def full(self):
        return len(self.strips) >= MAX_RENDER_PAGES

    def add(self, part):
        if self.buf is None:
            self.buf = part
        else:
            merged = PILImage.new("RGB", (self.w, self.buf.height + part.height), "white")
            merged.paste(self.buf, (0, 0))
            merged.paste(part, (0, self.buf.height))
            self.buf = merged
        while self.buf is not None and self.buf.height > self.room_px and not self.full:
            self._cut(self._snap(self.room_px))

    def finish(self):
        while self.buf is not None and self.buf.height > 0 and not self.full:
            cut = self._snap(self.room_px) if self.buf.height > self.room_px else self.buf.height
            self._cut(cut)
        return self.strips

    def _snap(self, cut):
        limit = int(cut * 0.2)
        region = self.buf.crop((0, cut - limit, self.w, cut + 1)).convert("L")
        for back in range(limit):
            row = region.height - 1 - back
            lo, hi = region.crop((0, row, self.w, row + 1)).getextrema()
            if hi - lo < 10:
                return cut - back
        return cut

    def _cut(self, cut):
        strip = self.buf.crop((0, 0, self.w, cut))
        out = io.BytesIO()
        strip.save(out, format="PNG", optimize=False)
        self.strips.append((out.getvalue(), cut / self.px_per_cm))
        rest = self.buf.height - cut
        self.buf = self.buf.crop((0, cut, self.w, self.buf.height)) if rest > 0 else None
        self.room_px = int((PDF_CONTENT_HEIGHT_CM - 0.1) * self.px_per_cm)


# ----------------------------------------------------------------------------- one email -> pdf
def render_job(job, renderer, mode, quality, log):
    """job: dict built by the fetcher. Writes job['pdf_path']. Returns the mode used."""
    os.makedirs(os.path.dirname(job["pdf_path"]), exist_ok=True)
    html = job.get("html")
    if html is not None and renderer is not None and mode == "print" and PYPDF:
        try:
            pdf = renderer.print_pdf(html,
                                     info_block_html(job["subject"], job["from_display"], job["to_display"],
                                                     job["date_str"]),
                                     attachments_block_html(job["attachments"]))
            _notes(job, renderer, log)
            compose_printed_pdf(job["pdf_path"], pdf, job["header_left"], job["header_right"], job["stamp"],
                                title=job["subject"])
            return "print"
        except Exception as e:
            log("  Printing failed for UID %s (%s); using image snapshot." % (job["uid"], e), "warn")
    strips = None
    body = job.get("body") or ""
    if html is not None:
        if renderer is not None:
            try:
                room = first_page_room_cm(job["subject"], job["from_display"], job["to_display"], job["date_str"])
                strips = renderer.render_strips(html, room, quality)
                _notes(job, renderer, log)
            except Exception as e:
                log("  HTML render failed for UID %s (%s); using text." % (job["uid"], e), "warn")
        if strips is None and not body:
            from mailtool.mail.mime import html_to_text
            body = html_to_text(html)
    build_pdf(job["pdf_path"], job["header_left"], job["header_right"], job["subject"], job["from_display"],
              job["to_display"], job["date_str"], body, job["attachments"], job["stamp"], body_strips=strips)
    return "image" if strips else "text"


def _notes(job, renderer, log):
    st = renderer.stats
    notes = []
    if st.get("remote_images"):
        notes.append("%d remote image(s) loaded" % st["remote_images"])
    if st.get("blocked"):
        notes.append("%d remote request(s) blocked" % st["blocked"])
    if st.get("truncated"):
        notes.append("body very long - cut at %d pages" % MAX_RENDER_PAGES)
    if notes:
        log("  UID %s: %s" % (job["uid"], ", ".join(notes)))
