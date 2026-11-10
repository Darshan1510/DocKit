"""Draw the app icon and write assets/app.icns (macOS) and assets/app.ico (Windows).

Run from the project root:  uv run assets/make_icon.py
macOS only for the .icns step (uses the built-in `iconutil`).
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
SIZE = 1024


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arialbd.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def vertical_gradient(size, top, bottom) -> Image.Image:
    grad = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        grad.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return grad.resize(size)


def draw_icon() -> Image.Image:
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # Rounded tile (macOS-style inset so the icon matches others in the Dock)
    inset, radius = 100, 185
    tile_box = (inset, inset, SIZE - inset, SIZE - inset)
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((inset, inset + 18, SIZE - inset, SIZE - inset + 18),
                                             radius, fill=(0, 0, 0, 110))
    icon.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(22)))

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(tile_box, radius, fill=255)
    tile = vertical_gradient((SIZE, SIZE), (59, 130, 246), (29, 78, 216)).convert("RGBA")
    icon.paste(tile, (0, 0), mask)

    d = ImageDraw.Draw(icon)

    # Document with folded corner
    dx0, dy0, dx1, dy1 = 312, 210, 712, 720
    fold = 110
    doc_shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(doc_shadow).polygon(
        [(dx0, dy0 + 14), (dx1 - fold, dy0 + 14), (dx1, dy0 + fold + 14), (dx1, dy1 + 14), (dx0, dy1 + 14)],
        fill=(0, 0, 40, 90))
    icon.alpha_composite(doc_shadow.filter(ImageFilter.GaussianBlur(14)))
    d.polygon([(dx0, dy0), (dx1 - fold, dy0), (dx1, dy0 + fold), (dx1, dy1), (dx0, dy1)], fill="white")
    d.polygon([(dx1 - fold, dy0), (dx1 - fold, dy0 + fold), (dx1, dy0 + fold)], fill=(191, 208, 245))

    # Picture on the document (DocKit handles images as well as PDFs)
    px0, py0, px1, py1 = dx0 + 55, 300, dx1 - 55, 470
    pic_mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(pic_mask).rounded_rectangle((px0, py0, px1, py1), 18, fill=255)
    pic = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    p = ImageDraw.Draw(pic)
    p.rectangle((px0, py0, px1, py1), fill=(219, 234, 254))
    p.ellipse((px1 - 95, py0 + 25, px1 - 45, py0 + 75), fill=(250, 204, 21))  # sun
    p.polygon([(px0, py1), (px0 + 95, py0 + 70), (px0 + 175, py1)], fill=(59, 130, 246))  # mountains
    p.polygon([(px0 + 110, py1), (px0 + 205, py0 + 95), (px1, py1)], fill=(29, 78, 216))
    icon.paste(pic, (0, 0), pic_mask)

    # Text lines
    for i, y in enumerate((530, 575, 620)):
        x_end = dx1 - 55 - (80 if i == 2 else 0)
        d.rounded_rectangle((dx0 + 55, y - 10, x_end, y + 10), 10, fill=(203, 213, 225))

    # Check mark badge: "done" for any file task
    bx, by, r = dx1 - 20, dy1 - 35, 62
    d.ellipse((bx - r, by - r, bx + r, by + r), fill=(22, 163, 74), outline="white", width=10)
    d.line([(bx - 28, by + 2), (bx - 6, by + 24), (bx + 30, by - 22)], fill="white", width=16, joint="curve")

    # Name banner at the bottom of the tile
    d.text((SIZE / 2, 812), "DocKit", font=font(84), fill="white", anchor="mm")
    return icon


def main():
    icon = draw_icon()
    icon.save(HERE / "app.png")
    icon.save(HERE / "app.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "app.iconset"
            iconset.mkdir()
            for s in (16, 32, 128, 256, 512):
                icon.resize((s, s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
                icon.resize((s * 2, s * 2), Image.LANCZOS).save(iconset / f"icon_{s}x{s}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "app.icns")], check=True)
    print("Wrote", ", ".join(p.name for p in HERE.glob("app.*")))


if __name__ == "__main__":
    main()
