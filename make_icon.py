"""Generate KaliClaude icon — Claude-style orange spark with a 'K' hub.

A radiating spark (Claude vibe) in warm orange on a deep warm-dark
rounded square, with Kalikot's bold 'K' on the central hub.
"""
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

BG    = (23, 20, 15)      # #17140F deep warm dark
SPARK = (217, 115, 64)    # #D97340 Claude orange
SS    = 4                 # supersample factor for smooth edges

N_RAYS  = 12
RAY_LEN = 0.96            # ray tip radius (fraction of R)
RAY_W   = 0.075           # ray thickness (fraction of R)


def _font(px: int):
    for name in ("arialbd.ttf", "segoeuib.ttf", "ariblk.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except Exception:
            pass
    return ImageFont.load_default()


def make_frame(size: int) -> Image.Image:
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    rad = int(S * 0.22)
    d.rounded_rectangle([(0, 0), (S - 1, S - 1)], radius=rad, fill=BG)

    cx = cy = S / 2
    R = S * 0.40
    w = R * RAY_W
    hub = R * 0.46          # central hub holding the K

    # rays radiate from just outside the hub
    for i in range(N_RAYS):
        a = 2 * math.pi * i / N_RAYS - math.pi / 2
        length = RAY_LEN if i % 2 == 0 else RAY_LEN * 0.74
        x0, y0 = cx + hub * 0.92 * math.cos(a), cy + hub * 0.92 * math.sin(a)
        x1, y1 = cx + R * length * math.cos(a), cy + R * length * math.sin(a)
        d.line([(x0, y0), (x1, y1)], fill=SPARK, width=int(w * 2))
        d.ellipse([x1 - w, y1 - w, x1 + w, y1 + w], fill=SPARK)

    # hub + K
    d.ellipse([cx - hub, cy - hub, cx + hub, cy + hub], fill=SPARK)
    fnt = _font(int(hub * 1.55))
    bb = d.textbbox((0, 0), "K", font=fnt)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    d.text((cx - tw / 2 - bb[0], cy - th / 2 - bb[1]), "K", fill=BG, font=fnt)

    return img.resize((size, size), Image.LANCZOS)


sizes = [16, 24, 32, 48, 64, 128, 256]
# Render one high-res master and let PIL emit every size from it.
# (Passing the smallest frame as the base produces a 16x16-only .ico — wrong.)
master = make_frame(256)
out = Path(__file__).parent / "app.ico"
master.save(out, format="ICO", sizes=[(s, s) for s in sizes])
master.save(Path(__file__).parent / "icon_preview.png")
print(f"Saved {out} with sizes {sizes}")
