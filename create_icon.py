"""
Generates icon.ico for Portfolio Tracker.
Run automatically by build.bat — requires Pillow (pip install pillow).
"""
from PIL import Image, ImageDraw
import math, os

def make_frame(size):
    img  = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Dark rounded background ──────────────────────────────
    pad = max(1, size // 16)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=size // 5,
        fill=(11, 17, 32, 255),
    )

    # ── Chart line (stock chart shape) ───────────────────────
    # Points as fractions of size
    pts_rel = [
        (0.12, 0.72),
        (0.28, 0.52),
        (0.44, 0.62),
        (0.62, 0.34),
        (0.78, 0.42),
        (0.88, 0.24),
    ]
    pts = [(x * size, y * size) for x, y in pts_rel]
    lw  = max(1, size // 18)

    # Glow effect — draw thicker, semi-transparent layer first
    if size >= 48:
        glow_col = (0, 200, 240, 60)
        draw.line(pts, fill=glow_col, width=lw * 3)

    draw.line(pts, fill=(0, 200, 240, 255), width=lw)

    # ── Green dot at the tip ──────────────────────────────────
    ex, ey = pts[-1]
    dr = max(2, size // 11)
    # glow ring
    if size >= 32:
        draw.ellipse([ex - dr*1.8, ey - dr*1.8, ex + dr*1.8, ey + dr*1.8],
                     fill=(0, 230, 118, 45))
    draw.ellipse([ex - dr, ey - dr, ex + dr, ey + dr],
                 fill=(0, 230, 118, 255))

    return img


def build_ico(path='icon.ico'):
    sizes  = [16, 24, 32, 48, 64, 128, 256]
    frames = [make_frame(s) for s in sizes]
    frames[0].save(
        path,
        format='ICO',
        sizes=[(s, s) for s in sizes],
        append_images=frames[1:],
    )
    print(f'  Created {path}  ({len(sizes)} sizes: {sizes})')


if __name__ == '__main__':
    build_ico()
