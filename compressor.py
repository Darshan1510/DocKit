"""PDF compression: downsample and re-encode images as JPEG, then repack streams.

Text, vector graphics, links and bookmarks are kept as they are. Only raster
images change, so text stays sharp and selectable.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from pikepdf import Name, PdfImage
from PIL import Image

# Images smaller than this (longest side, px) are left alone: little to gain.
MIN_IMAGE_SIDE = 64


@dataclass
class Settings:
    dpi: int = 144          # target image resolution
    quality: int = 65       # JPEG quality, 1-95
    grayscale: bool = False


PRESETS: dict[str, Settings] = {
    "light": Settings(dpi=200, quality=80),
    "balanced": Settings(dpi=144, quality=65),
    "strong": Settings(dpi=96, quality=45),
}


@dataclass
class Result:
    original_bytes: int
    compressed_bytes: int
    images_total: int
    images_recompressed: int

    @property
    def smaller(self) -> bool:
        return self.compressed_bytes < self.original_bytes


def _page_longest_side_inches(page: pikepdf.Page) -> float:
    box = page.obj.get("/CropBox", page.mediabox)
    x0, y0, x1, y1 = (float(v) for v in box)
    return max(abs(x1 - x0), abs(y1 - y0)) / 72.0


def _iter_page_images(page: pikepdf.Page):
    """Yield image XObjects on a page, including those nested in Form XObjects."""
    stack = [page.obj.get("/Resources")]
    seen_forms: set[tuple[int, int]] = set()
    while stack:
        resources = stack.pop()
        if resources is None:
            continue
        xobjects = resources.get("/XObject")
        if xobjects is None:
            continue
        for _, xobj in xobjects.items():
            if not isinstance(xobj, pikepdf.Stream):
                continue
            subtype = xobj.get("/Subtype")
            if subtype == Name.Image:
                yield xobj
            elif subtype == Name.Form and xobj.objgen not in seen_forms:
                seen_forms.add(xobj.objgen)
                stack.append(xobj.get("/Resources"))


def _recompress_image(xobj: pikepdf.Stream, max_side: int, s: Settings) -> bool:
    """Replace the image stream with a smaller JPEG. Return True if replaced."""
    if xobj.get("/ImageMask", False):
        return False
    if int(xobj.get("/BitsPerComponent", 8)) < 8:
        return False  # 1-bit scans (CCITT/JBIG2) are already tiny
    width, height = int(xobj.Width), int(xobj.Height)
    if max(width, height) < MIN_IMAGE_SIDE:
        return False

    try:
        img = PdfImage(xobj).as_pil_image()
    except Exception:
        return False  # unsupported color space or filter: keep original

    if s.grayscale:
        img = img.convert("L")
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    scale = min(1.0, max_side / max(width, height))
    if scale < 1.0:
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        img = img.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=s.quality, optimize=True)
    data = buf.getvalue()

    original_len = len(xobj.read_raw_bytes())
    if len(data) >= original_len and scale >= 1.0:
        return False

    xobj.write(data, filter=Name.DCTDecode)
    xobj.Width, xobj.Height = img.size
    xobj.ColorSpace = Name.DeviceGray if img.mode == "L" else Name.DeviceRGB
    xobj.BitsPerComponent = 8
    for key in ("/DecodeParms", "/Decode", "/Intent"):
        if key in xobj:
            del xobj[key]
    # Soft mask (transparency) may have its own size; PDF viewers rescale it.
    return True


def compress(src: Path, dst: Path, s: Settings) -> Result:
    original = src.stat().st_size
    done: set[tuple[int, int]] = set()
    total = recompressed = 0

    with pikepdf.open(src) as pdf:
        for page in pdf.pages:
            max_side = max(MIN_IMAGE_SIDE, round(_page_longest_side_inches(page) * s.dpi))
            for xobj in _iter_page_images(page):
                if xobj.objgen in done:
                    continue
                done.add(xobj.objgen)
                total += 1
                if _recompress_image(xobj, max_side, s):
                    recompressed += 1

        pdf.remove_unreferenced_resources()
        pdf.save(
            dst,
            compress_streams=True,
            recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )

    return Result(original, dst.stat().st_size, total, recompressed)
