"""Image tools: compress, convert, resize, crop, rotate/flip, scanned look, watermark.

Every function takes (path on disk, original name) pairs and writes new files
into workdir. Results are (path on disk, download name) pairs.
"""

from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

import scanner
from tools import MAX_PIXELS, ToolError, check_pixels

# key -> (Pillow format, file extension)
FORMATS = {
    "jpg": ("JPEG", ".jpg"), "png": ("PNG", ".png"), "webp": ("WEBP", ".webp"),
    "gif": ("GIF", ".gif"), "bmp": ("BMP", ".bmp"), "tiff": ("TIFF", ".tiff"),
}
PIL_TO_KEY = {v[0]: k for k, v in FORMATS.items()} | {"MPO": "jpg"}

Item = tuple[Path, str]  # (path on disk, original file name)


# --- Helpers ---


def open_image(path: Path, name: str) -> tuple[Image.Image, str]:
    """Open an image upright (EXIF orientation applied). Returns (image, format key)."""
    try:
        im = Image.open(path)
        check_pixels(*im.size, name)
        fmt = PIL_TO_KEY.get(im.format or "", "jpg")  # other formats are saved as JPG
        icc = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im)
        im.load()
    except ToolError:
        raise
    except Image.DecompressionBombError:
        raise ToolError(f"{name} is too large (over {MAX_PIXELS // 1_000_000} megapixels).")
    except Exception:
        raise ToolError(f"Could not read image {name}.")
    if icc:
        im.info["icc_profile"] = icc
    return im, fmt


def _flatten(im: Image.Image, color=(255, 255, 255)) -> Image.Image:
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, color)
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("L" if im.mode in ("L", "1", "I;16", "I") else "RGB")


def _for_format(im: Image.Image, key: str) -> Image.Image:
    """Convert to a mode the output format can store."""
    if key in ("jpg", "bmp"):
        return _flatten(im)  # no transparency: flatten onto white
    if key == "gif":
        return im if im.mode in ("P", "L") else im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
    if im.mode in ("RGBA", "RGB", "L", "LA"):
        return im
    if im.mode == "P":
        return im.convert("RGBA")
    return im.convert("RGB")


def _exif_bytes(im: Image.Image) -> bytes | None:
    """EXIF to keep, with orientation reset (the pixels are already upright)."""
    exif = im.getexif()
    if not exif:
        return None
    exif[0x0112] = 1
    return exif.tobytes()


def encode(im: Image.Image, key: str, quality: int = 90, keep_exif: bool = True,
           optimize: bool = True) -> bytes:
    """Encode an image in format key. Returns the file bytes."""
    fmt = FORMATS[key][0]
    out = _for_format(im, key)
    kwargs = {}
    if icc := im.info.get("icc_profile"):
        kwargs["icc_profile"] = icc
    if keep_exif and key in ("jpg", "png", "webp") and (exif := _exif_bytes(im)):
        kwargs["exif"] = exif
    if key == "jpg":
        kwargs.update(quality=quality, optimize=optimize, progressive=True)
    elif key == "webp":
        kwargs.update(quality=quality, method=4)
    elif key == "png":
        kwargs.update(optimize=optimize)
    elif key == "gif":
        kwargs.update(optimize=optimize)
    elif key == "tiff":
        kwargs.update(compression="tiff_adobe_deflate")
    buf = io.BytesIO()
    out.save(buf, fmt, **kwargs)
    return buf.getvalue()


class Names:
    """Download names: original stem + suffix, made unique inside one job."""

    def __init__(self):
        self.used: set[str] = set()

    def __call__(self, name: str, suffix: str, ext: str) -> str:
        stem = Path(name).stem or "image"
        candidate, n = f"{stem}{suffix}{ext}", 2
        while candidate.lower() in self.used:
            candidate, n = f"{stem}{suffix}-{n}{ext}", n + 1
        self.used.add(candidate.lower())
        return candidate


def _write(workdir: Path, i: int, data: bytes, ext: str) -> Path:
    dest = workdir / f"out-{i}{ext}"
    dest.write_bytes(data)
    return dest


def _out_key(choice: str, source: str) -> str:
    return choice if choice in FORMATS else source


def _require(items: list[Item]) -> None:
    if not items:
        raise ToolError("Add at least one image.")


# --- Compress ---

COMPRESS_PRESETS = {"light": 85, "balanced": 72, "strong": 55}


def _compress_one(im: Image.Image, key: str, quality: int, preset: str, keep_exif: bool,
                  target: int) -> bytes:
    lossy = key in ("jpg", "webp")

    def enc(img, q):
        if key == "png" and preset != "light":
            # Lossy PNG: reduce to 256 colors (keeps transparency).
            img = img.convert("RGBA").quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG)
            return encode(img, key, keep_exif=keep_exif)
        return encode(img, key, q, keep_exif=keep_exif)

    data = enc(im, quality)
    if not target or len(data) <= target:
        return data
    # Target size: the highest quality that fits, then smaller dimensions if needed.
    img = im
    for _ in range(8):
        if lossy:
            lo, hi, best = 10, 95, None
            while lo <= hi:
                mid = (lo + hi) // 2
                attempt = enc(img, mid)
                if len(attempt) <= target:
                    best, lo = attempt, mid + 1
                else:
                    hi = mid - 1
            if best is not None:
                return best
            data = enc(img, 10)
        else:
            data = enc(img, quality)
            if len(data) <= target:
                return data
        factor = max(0.3, min(0.9, math.sqrt(target / len(data)) * 0.95))
        w, h = max(1, round(img.width * factor)), max(1, round(img.height * factor))
        if w < 16 or h < 16:
            break
        img = img.resize((w, h), Image.Resampling.LANCZOS)
    return data  # smallest we could make; the caller reports the real size


def compress(items: list[Item], preset: str, quality: int, target_kb: int, fmt: str, strip: bool,
             workdir: Path) -> tuple[list[tuple[Path, str]], list[dict]]:
    _require(items)
    quality = COMPRESS_PRESETS.get(preset, max(5, min(quality, 95)))
    target = max(0, target_kb) * 1024
    names, outputs, stats = Names(), [], []
    for i, (path, name) in enumerate(items):
        im, src = open_image(path, name)
        key = fmt if fmt in ("jpg", "webp") else src
        data = _compress_one(im, key, quality, preset, not strip, target)
        original = path.stat().st_size
        # Same format as the file on disk: keep its extension (.jpeg stays .jpeg).
        native = key == src and path.suffix.lower() in (FORMATS[key][1], ".jpeg", ".tif")
        if native and len(data) >= original and not strip:
            data = path.read_bytes()  # already as small as we can make it
        ext = path.suffix.lower() if native else FORMATS[key][1]
        outputs.append((_write(workdir, i, data, ext), names(name, "-compressed", ext)))
        stats.append({"name": name, "original_bytes": original, "compressed_bytes": len(data)})
    return outputs, stats


# --- Convert ---


def convert(items: list[Item], fmt: str, workdir: Path) -> list[tuple[Path, str]]:
    _require(items)
    if fmt not in FORMATS:
        raise ToolError("Choose an output format.")
    names, outputs = Names(), []
    for i, (path, name) in enumerate(items):
        im, _ = open_image(path, name)
        ext = FORMATS[fmt][1]
        outputs.append((_write(workdir, i, encode(im, fmt, 92), ext), names(name, "", ext)))
    return outputs


# --- Resize ---


def resize(items: list[Item], mode: str, percent: float, width: int, height: int, keep_aspect: bool,
           workdir: Path) -> list[tuple[Path, str]]:
    _require(items)
    names, outputs = Names(), []
    for i, (path, name) in enumerate(items):
        im, key = open_image(path, name)
        w0, h0 = im.size
        if mode == "percent":
            if not 1 <= percent <= 1000:
                raise ToolError("Enter a percentage between 1 and 1000.")
            w, h = w0 * percent / 100, h0 * percent / 100
        else:
            if width <= 0 and height <= 0:
                raise ToolError("Enter a width or a height.")
            if keep_aspect:
                scales = [s for s in (width / w0 if width > 0 else 0, height / h0 if height > 0 else 0) if s]
                s = min(scales)  # fit inside the box
                w, h = w0 * s, h0 * s
            else:
                w, h = width if width > 0 else w0, height if height > 0 else h0
        w, h = max(1, round(w)), max(1, round(h))
        check_pixels(w, h, name)
        out = im.resize((w, h), Image.Resampling.LANCZOS) if (w, h) != (w0, h0) else im
        out.info = im.info
        ext = FORMATS[key][1]
        outputs.append((_write(workdir, i, encode(out, key, 92), ext), names(name, f"-{w}x{h}", ext)))
    return outputs


# --- Crop ---


def crop(item: Item, box: list, workdir: Path) -> tuple[Path, str]:
    """box: [x, y, width, height] in pixels of the upright image."""
    path, name = item
    im, key = open_image(path, name)
    try:
        x, y, w, h = (round(float(v)) for v in box)
    except (TypeError, ValueError):
        raise ToolError("Draw a crop area on the image.")
    x, y = max(0, x), max(0, y)
    w, h = min(w, im.width - x), min(h, im.height - y)
    if w < 1 or h < 1:
        raise ToolError("The crop area is outside the image.")
    out = im.crop((x, y, x + w, y + h))
    out.info = im.info
    ext = FORMATS[key][1]
    return _write(workdir, 0, encode(out, key, 92), ext), Names()(name, "-cropped", ext)


# --- Rotate / flip ---


def rotate_flip(items: list[Item], angle: int, flip_h: bool, flip_v: bool,
                workdir: Path) -> list[tuple[Path, str]]:
    """Flip first, then rotate clockwise (the same order as the preview)."""
    _require(items)
    if angle % 90:
        raise ToolError("Rotation must be 0, 90, 180 or 270 degrees.")
    if not angle % 360 and not flip_h and not flip_v:
        raise ToolError("Choose a rotation or a flip.")
    rot = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    names, outputs = Names(), []
    for i, (path, name) in enumerate(items):
        im, key = open_image(path, name)
        info = im.info
        if flip_h:
            im = im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if flip_v:
            im = im.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        if angle % 360:
            im = im.transpose(rot[angle % 360])
        im.info = info
        ext = FORMATS[key][1]
        outputs.append((_write(workdir, i, encode(im, key, 92), ext), names(name, "-rotated", ext)))
    return outputs


# --- Scanned look ---


def scanned(items: list[Item], intensity: str, color: str, fmt: str, seed: int,
            workdir: Path) -> list[tuple[Path, str]]:
    _require(items)
    rng = np.random.default_rng(seed)
    names, outputs = Names(), []
    for i, (path, name) in enumerate(items):
        im, src = open_image(path, name)
        info = im.info
        out = scanner.scanned_look(_flatten(im), intensity, color, rng)
        out.info = info
        key = _out_key(fmt, src)
        if key in ("gif", "bmp", "tiff"):
            key = "png"
        ext = FORMATS[key][1]
        outputs.append((_write(workdir, i, encode(out, key, 85), ext), names(name, "-scanned", ext)))
    return outputs


# --- Watermark ---

POSITIONS = {
    "top-left": (0, 0), "top-center": (0.5, 0), "top-right": (1, 0),
    "middle-left": (0, 0.5), "center": (0.5, 0.5), "middle-right": (1, 0.5),
    "bottom-left": (0, 1), "bottom-center": (0.5, 1), "bottom-right": (1, 1),
}


def _text_stamp(text: str, px: int, rgb: tuple, opacity: float, angle: int) -> Image.Image:
    font = ImageFont.load_default(size=max(6, px))
    left, top, right, bottom = font.getbbox(text)
    stamp = Image.new("RGBA", (right - left + 4, bottom - top + 4), (0, 0, 0, 0))
    ImageDraw.Draw(stamp).text((2 - left, 2 - top), text, font=font, fill=(*rgb, round(255 * opacity)))
    return stamp.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC) if angle else stamp


def _put(layer: Image.Image, stamp: Image.Image, x: int, y: int) -> None:
    """alpha_composite that allows the stamp to hang over the edges."""
    sx, sy = max(0, -x), max(0, -y)
    if sx >= stamp.width or sy >= stamp.height or x >= layer.width or y >= layer.height:
        return
    layer.alpha_composite(stamp, (max(0, x), max(0, y)), (sx, sy))


def watermark(items: list[Item], text: str, position: str, size: int, opacity: float, color: str,
              angle: int, workdir: Path) -> list[tuple[Path, str]]:
    """size: text height as a percentage of the image's shorter side."""
    _require(items)
    text = text.strip()
    if not text:
        raise ToolError("Enter watermark text.")
    try:
        rgb = ImageColor.getrgb(color)[:3]
    except ValueError:
        rgb = (220, 38, 38)
    opacity = max(0.05, min(opacity, 1.0))
    names, outputs = Names(), []
    for i, (path, name) in enumerate(items):
        im, key = open_image(path, name)
        info = im.info
        base = im.convert("RGBA")
        px = round(min(im.size) * max(1, min(size, 50)) / 100)
        stamp = _text_stamp(text, px, rgb, opacity, angle)
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        if position == "tiled":
            sx, sy = stamp.width + 2 * px, stamp.height + 2 * px
            for row, y in enumerate(range(-sy // 2, base.height, sy)):
                for x in range(-sx // 2 + (row % 2) * sx // 2, base.width, sx):
                    _put(layer, stamp, x, y)
        else:
            fx, fy = POSITIONS.get(position, (0.5, 0.5))
            margin = round(min(im.size) * 0.03)
            x = round(margin + fx * (base.width - stamp.width - 2 * margin))
            y = round(margin + fy * (base.height - stamp.height - 2 * margin))
            _put(layer, stamp, x, y)
        out = Image.alpha_composite(base, layer)
        if im.mode not in ("RGBA", "LA", "P"):
            out = out.convert("RGB")
        out.info = info
        ext = FORMATS[key][1]
        outputs.append((_write(workdir, i, encode(out, key, 92), ext), names(name, "-watermarked", ext)))
    return outputs
