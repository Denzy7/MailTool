"""Draws MailTool's app icon and sidebar icons with Pillow.

    python tools/make_assets.py

Writes mailtool/assets/*.png and mailtool.ico. Shapes are drawn at 4x and scaled
down, so they stay crisp. Run again after changing the brand colours."""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "mailtool", "assets")

TEAL_DARK = (11, 79, 74)
TEAL = (15, 118, 110)
TEAL_LIGHT = (45, 179, 166)
WHITE = (255, 255, 255)
ACCENT = (250, 204, 21)   # small amber detail on the app icon


def app_icon(size=512):
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # rounded square with a vertical teal gradient
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / S
        c = tuple(int(TEAL_LIGHT[i] * (1 - t) + TEAL_DARK[i] * t) for i in range(3)) + (255,)
        gd.line([(0, y), (S, y)], fill=c)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)
    # envelope
    m = S * 0.2
    top, bot = S * 0.36, S * 0.80
    w = S * 0.024
    d.rounded_rectangle([m, top, S - m, bot], radius=int(S * 0.05), outline=WHITE, width=int(w))
    d.line([(m + w, top + w), (S / 2, top + (bot - top) * 0.55), (S - m - w, top + w)], fill=WHITE,
           width=int(w), joint="curve")
    # down arrow above, in a circle badge
    cx, cy, r = S * 0.5, S * 0.24, S * 0.13
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)
    aw = S * 0.028
    d.line([(cx, cy - r * 0.55), (cx, cy + r * 0.45)], fill=TEAL_DARK, width=int(aw))
    d.line([(cx - r * 0.45, cy + r * 0.02), (cx, cy + r * 0.5), (cx + r * 0.45, cy + r * 0.02)],
           fill=TEAL_DARK, width=int(aw), joint="curve")
    return img.resize((size, size), Image.LANCZOS)


def glyph(kind, color, size=22):
    S = size * 8
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = int(S * 0.09)
    c = color
    if kind == "fetch":          # arrow into a tray
        d.line([(S / 2, S * 0.08), (S / 2, S * 0.6)], fill=c, width=w)
        d.line([(S * 0.27, S * 0.38), (S / 2, S * 0.62), (S * 0.73, S * 0.38)], fill=c, width=w, joint="curve")
        d.line([(S * 0.1, S * 0.62), (S * 0.1, S * 0.9), (S * 0.9, S * 0.9), (S * 0.9, S * 0.62)], fill=c, width=w,
               joint="curve")
    elif kind == "library":      # stacked drawers
        for i in range(3):
            y0 = S * (0.1 + i * 0.28)
            d.rounded_rectangle([S * 0.1, y0, S * 0.9, y0 + S * 0.22], radius=int(S * 0.05), outline=c, width=w)
            d.line([(S * 0.4, y0 + S * 0.11), (S * 0.6, y0 + S * 0.11)], fill=c, width=w)
    elif kind == "sort":         # funnel
        d.line([(S * 0.08, S * 0.14), (S * 0.92, S * 0.14), (S * 0.58, S * 0.52), (S * 0.58, S * 0.88),
                (S * 0.42, S * 0.78), (S * 0.42, S * 0.52), (S * 0.08, S * 0.14)], fill=c, width=w, joint="curve")
    elif kind == "print":        # printer
        d.rectangle([S * 0.27, S * 0.08, S * 0.73, S * 0.32], outline=c, width=w)
        d.rounded_rectangle([S * 0.07, S * 0.32, S * 0.93, S * 0.72], radius=int(S * 0.08), outline=c, width=w)
        d.rectangle([S * 0.27, S * 0.58, S * 0.73, S * 0.92], fill=(0, 0, 0, 0), outline=c, width=w)
        d.ellipse([S * 0.74, S * 0.42, S * 0.82, S * 0.5], fill=c)
    elif kind == "settings":     # gear
        import math
        cx = cy = S / 2
        for k in range(8):
            a = k * math.pi / 4
            x, y = cx + math.cos(a) * S * 0.36, cy + math.sin(a) * S * 0.36
            d.line([(cx, cy), (x, y)], fill=c, width=int(S * 0.16))
        d.ellipse([cx - S * 0.3, cy - S * 0.3, cx + S * 0.3, cy + S * 0.3], fill=c)
        d.ellipse([cx - S * 0.13, cy - S * 0.13, cx + S * 0.13, cy + S * 0.13], fill=(0, 0, 0, 0))
        # punch the hole through
        hole = Image.new("L", (S, S), 255)
        ImageDraw.Draw(hole).ellipse([cx - S * 0.13, cy - S * 0.13, cx + S * 0.13, cy + S * 0.13], fill=0)
        img.putalpha(Image.composite(img.getchannel("A"), Image.new("L", (S, S), 0), hole))
    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    big = app_icon(512)
    big.save(os.path.join(OUT, "icon_512.png"))
    for s in (256, 64, 48, 32, 16):
        big.resize((s, s), Image.LANCZOS).save(os.path.join(OUT, "icon_%d.png" % s))
    big.save(os.path.join(OUT, "mailtool.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                         (128, 128), (256, 256)])
    for kind in ("fetch", "library", "sort", "print", "settings"):
        glyph(kind, (255, 255, 255, 255)).save(os.path.join(OUT, "nav_%s_on.png" % kind))
        glyph(kind, (159, 197, 193, 255)).save(os.path.join(OUT, "nav_%s_off.png" % kind))
    print("assets written to", OUT)


if __name__ == "__main__":
    main()
