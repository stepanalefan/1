"""Generates assets/RemoteDesk.ico and assets/RemoteDeskServer.ico (+ PNG previews).

  python make_icons.py
"""
import os
from PIL import Image, ImageDraw, ImageFilter

S = 1024                      # drawn large, then downscaled for crisp edges
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def gradient(top, bottom):
    g = Image.new("RGB", (1, S))
    for y in range(S):
        t = y / (S - 1)
        g.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    return g.resize((S, S))


def base(top, bottom, small=False):
    pad, r = (0, 190) if small else (40, 210)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad, S - pad, S - pad), r, fill=255)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((pad, pad + 18, S - pad, S - pad + 18), r,
                                             fill=(0, 0, 0, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    if not small:
        img.alpha_composite(shadow)
    tile = gradient(top, bottom).convert("RGBA")
    tile.putalpha(mask)
    img.alpha_composite(tile)
    # soft top highlight
    hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(hl).ellipse((-200, -620, S + 200, 420), fill=(255, 255, 255, 34))
    hm = Image.new("L", (S, S), 0)
    hm.paste(hl.getchannel("A"), mask=mask)
    hl.putalpha(hm)
    img.alpha_composite(hl)
    return img


def client_art():
    """Monitor + arrow + cursor on a transparent layer (S x S)."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    white, red = (255, 255, 255, 255), (214, 58, 48, 255)
    # monitor
    d.rounded_rectangle((210, 250, 814, 650), 46, fill=white)
    d.rounded_rectangle((250, 290, 774, 610), 22, fill=(255, 236, 233, 255))
    d.rectangle((470, 650, 554, 740), fill=white)
    d.rounded_rectangle((350, 730, 674, 786), 28, fill=white)
    # arrow = remote connection
    d.polygon([(330, 410), (520, 410), (520, 360), (620, 450), (520, 540), (520, 490), (330, 490)],
              fill=red)
    d.polygon([(694, 470), (604, 470), (604, 420)], fill=red)
    # mouse cursor
    cur = [(600, 380), (600, 600), (650, 555), (690, 640), (724, 624), (684, 540), (750, 535)]
    d.polygon(cur, fill=(40, 40, 40, 255))
    d.line(cur + [cur[0]], fill=white, width=14, joint="curve")
    return img


def client_icon(small=False):
    img = base((246, 98, 84), (196, 44, 38), small)
    img.alpha_composite(client_art())
    return img


def server_icon(small=False):
    img = base((84, 140, 245), (36, 86, 196), small)
    d = ImageDraw.Draw(img)
    white, blue = (255, 255, 255, 255), (47, 111, 219, 255)
    for i in range(3):                      # three rack units
        y = 235 + i * 190
        d.rounded_rectangle((230, y, 794, y + 150), 34, fill=white)
        for k in range(4):                  # vents
            x = 300 + k * 52
            d.rounded_rectangle((x, y + 45, x + 24, y + 105), 10, fill=(214, 226, 248, 255))
        d.ellipse((640, y + 50, 690, y + 100), fill=(46, 200, 110, 255))   # status LED
        d.ellipse((706, y + 50, 756, y + 100), fill=blue)
    return img


def save(draw, name):
    """Sizes up to 32 px use the edge-to-edge variant (no shadow margin)."""
    os.makedirs(OUT, exist_ok=True)
    big, small = draw(), draw(small=True)
    frames = [(small if s <= 32 else big).resize((s, s), Image.LANCZOS) for s in SIZES]
    frames[-1].save(os.path.join(OUT, name + ".png"))
    frames[-1].save(os.path.join(OUT, name + ".ico"), sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])
    print("wrote", name)


ANDROID_RES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "android", "app", "src", "main", "res")
DENSITIES = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}


def android_icons():
    """Launcher icons: adaptive (API 26+) and legacy PNGs."""
    # adaptive foreground: art scaled into the 66/108 safe zone
    art = client_art()
    k = 0.70
    small = art.resize((int(S * k), int(S * k)), Image.LANCZOS)
    fg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    off = (S - small.width) // 2
    fg.alpha_composite(small, (off, off + int(S * 0.01)))
    legacy = client_icon()
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse((40, 40, S - 40, S - 40), fill=255)
    rnd = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile = gradient((246, 98, 84), (196, 44, 38)).convert("RGBA")
    tile.alpha_composite(art)
    rnd.paste(tile, mask=mask)
    for name, scale in DENSITIES.items():
        d = os.path.join(ANDROID_RES, "mipmap-" + name)
        os.makedirs(d, exist_ok=True)
        fg.resize((int(108 * scale),) * 2, Image.LANCZOS).save(
            os.path.join(d, "ic_launcher_foreground.png"))
        legacy.resize((int(48 * scale),) * 2, Image.LANCZOS).save(os.path.join(d, "ic_launcher.png"))
        rnd.resize((int(48 * scale),) * 2, Image.LANCZOS).save(
            os.path.join(d, "ic_launcher_round.png"))
    d = os.path.join(ANDROID_RES, "mipmap-anydpi-v26")
    os.makedirs(d, exist_ok=True)
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
           '    <background android:drawable="@color/launcher_bg" />\n'
           '    <foreground android:drawable="@mipmap/ic_launcher_foreground" />\n'
           '    <monochrome android:drawable="@mipmap/ic_launcher_foreground" />\n'
           '</adaptive-icon>\n')
    for n in ("ic_launcher.xml", "ic_launcher_round.xml"):
        with open(os.path.join(d, n), "w") as f:
            f.write(xml)
    print("wrote Android launcher icons")


if __name__ == "__main__":
    save(client_icon, "RemoteDesk")
    save(server_icon, "RemoteDeskServer")
    android_icons()
