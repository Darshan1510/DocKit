"""Image tools: compress, convert, resize, crop, rotate/flip, scanned look, watermark,
remove watermark.

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


# --- Remove watermark ---

ENHANCE = ("off", "sharpen", "upscale")


def _stroke_mask(size: tuple[int, int], strokes: list) -> np.ndarray:
    """Paint brush strokes into a mask (255 = remove). Points are 0..1 fractions,
    "size" is the brush diameter as a fraction of the image's shorter side."""
    w, h = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    short = min(w, h)
    for stroke in strokes[:500]:
        try:
            d = max(2, round(float(stroke.get("size", 0.04)) * short))
            pts = [(float(u) * w, float(v) * h) for u, v in list(stroke.get("points", []))[:5000]]
        except (TypeError, ValueError, AttributeError):
            raise ToolError("Paint over the watermark first.")
        if not pts:
            continue
        if len(pts) > 1:
            draw.line(pts, fill=255, width=d, joint="curve")
        r = d / 2
        for x, y in (pts[0], pts[-1]) if len(pts) > 1 else pts:
            draw.ellipse((x - r, y - r, x + r, y + r), fill=255)
    return np.asarray(mask)


def _pull_push(img: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Smooth fill of the invalid pixels: average the known pixels over a pyramid of
    ever smaller sizes, then blend back up. No seams, even in big areas."""
    import cv2

    h, w = valid.shape
    weight = valid.astype(np.float32)
    if h <= 4 or w <= 4 or weight.all():
        known = weight > 0
        out = img.copy()
        if known.any() and not known.all():
            out[~known] = img[known].mean(axis=0)
        return out
    sw, sh = (w + 1) // 2, (h + 1) // 2
    num = cv2.resize(img * weight[..., None], (sw, sh), interpolation=cv2.INTER_AREA)
    den = cv2.resize(weight, (sw, sh), interpolation=cv2.INTER_AREA)
    small = num / np.maximum(den, 1e-6)[..., None]
    small = _pull_push(small, den > 0.01)
    up = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.where(valid[..., None], img, up)


def _inpaint(rgb: np.ndarray, mask: np.ndarray, seed: int = 0) -> np.ndarray:
    """Fill the masked area from its surroundings. Works on the mask's bounding box
    only (fast on big photos), then adds matching grain so the fill is not too flat."""
    import cv2

    ys, xs = np.nonzero(mask)
    h, w = mask.shape
    short = min(w, h)
    grow = max(2, round(short * 0.004))  # cover the soft edge of the watermark too
    pad = max(16, round(short * 0.05))
    x0, x1 = max(0, xs.min() - pad), min(w, xs.max() + pad + 1)
    y0, y1 = max(0, ys.min() - pad), min(h, ys.max() + pad + 1)
    crop = np.ascontiguousarray(rgb[y0:y1, x0:x1])
    m = cv2.dilate(mask[y0:y1, x0:x1], cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1,) * 2))
    src = crop.astype(np.float32)
    if hasattr(cv2, "xphoto"):
        # Shift-map: fill the hole with real pixels copied from nearby, so texture
        # and grain stay sharp instead of being smeared.
        filled = np.zeros_like(crop)
        cv2.xphoto.inpaint(cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), cv2.bitwise_not(m), filled,
                           cv2.xphoto.INPAINT_SHIFTMAP)
        filled = cv2.cvtColor(filled, cv2.COLOR_BGR2RGB).astype(np.float32)
        alpha = cv2.GaussianBlur(m.astype(np.float32) / 255, (0, 0), 1.0)[..., None]
        alpha = np.maximum(alpha, (m[..., None] > 0).astype(np.float32))
        out = rgb.copy()
        out[y0:y1, x0:x1] = np.clip(src * (1 - alpha) + filled * alpha, 0, 255).astype(np.uint8)
        return out
    # Fallback without opencv-contrib. Near the edge of the mask: Telea inpainting, which continues edges and lines
    # into the hole. Deep inside a wide mask Telea leaves seams, so blend towards a
    # smooth multi-scale fill there.
    bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
    telea = cv2.cvtColor(cv2.inpaint(bgr, m, 3, cv2.INPAINT_TELEA), cv2.COLOR_BGR2RGB).astype(np.float32)
    smooth = _pull_push(src, m == 0)
    dist = cv2.distanceTransform(m, cv2.DIST_L2, 5)
    deep = np.clip((dist - 4) / 10, 0, 1)[..., None]
    filled = telea * (1 - deep) + smooth * deep

    # Grain: match the noise of the untouched pixels around the mask. Median-based,
    # so edges and text nearby do not count as noise.
    ring = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * pad + 1,) * 2)) & ~m
    if ring.any():
        detail = (src - cv2.GaussianBlur(src, (0, 0), 1.5)).mean(axis=2)[ring > 0]
        sigma = 1.4826 * float(np.median(np.abs(detail - np.median(detail))))
        if sigma > 0.5:
            noise = np.random.default_rng(seed).normal(0, sigma, filled.shape[:2]).astype(np.float32)
            noise = cv2.GaussianBlur(noise, (0, 0), 0.6) * 1.6  # fine film-like grain, not speckles
            filled = filled + (noise * (m > 0))[..., None]

    # Feather the edge so the fill blends into the photo.
    alpha = cv2.GaussianBlur(m.astype(np.float32) / 255, (0, 0), max(1.0, grow / 2))[..., None]
    alpha = np.maximum(alpha, (m[..., None] > 0).astype(np.float32))
    blended = crop.astype(np.float32) * (1 - alpha) + filled * alpha
    out = rgb.copy()
    out[y0:y1, x0:x1] = np.clip(blended, 0, 255).astype(np.uint8)
    return out


def _enhance(im: Image.Image, mode: str) -> Image.Image:
    """Optional clean-up after removal: sharpen, or upscale 2x then sharpen."""
    from PIL import ImageFilter

    if mode == "upscale":
        w, h = im.width * 2, im.height * 2
        check_pixels(w, h, "The upscaled image")
        im = im.resize((w, h), Image.Resampling.LANCZOS)
        return im.filter(ImageFilter.UnsharpMask(radius=2, percent=90, threshold=2))
    if mode == "sharpen":
        return im.filter(ImageFilter.UnsharpMask(radius=1.5, percent=70, threshold=2))
    return im


def remove_watermark(item: Item, strokes: list, enhance: str, workdir: Path) -> tuple[Path, str]:
    path, name = item
    if not isinstance(strokes, list) or not strokes:
        raise ToolError("Paint over the watermark first.")
    im, key = open_image(path, name)
    info = im.info
    mask = _stroke_mask(im.size, strokes)
    if not mask.any():
        raise ToolError("Paint over the watermark first.")
    alpha = im.getchannel("A") if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info) else None
    gray = im.mode in ("L", "LA", "1", "I;16", "I")
    rgb = np.asarray(im.convert("RGB"))
    out = Image.fromarray(_inpaint(rgb, mask, seed=len(strokes)))
    out = _enhance(out, enhance if enhance in ENHANCE else "off")
    if alpha is not None:
        out.putalpha(alpha.resize(out.size, Image.Resampling.LANCZOS))
    elif gray:
        out = out.convert("L")
    out.info = info
    ext = FORMATS[key][1]
    # Save at high quality so removal does not add compression artifacts.
    data = encode(out, key, 95) if key != "jpg" else _encode_jpeg_max(out)
    return _write(workdir, 0, data, ext), Names()(name, "-no-watermark", ext)


def _encode_jpeg_max(im: Image.Image) -> bytes:
    """JPEG at quality 95 with full color resolution (no chroma subsampling)."""
    kwargs = {"quality": 95, "subsampling": 0, "optimize": True, "progressive": True}
    if icc := im.info.get("icc_profile"):
        kwargs["icc_profile"] = icc
    if exif := _exif_bytes(im):
        kwargs["exif"] = exif
    buf = io.BytesIO()
    _flatten(im).save(buf, "JPEG", **kwargs)
    return buf.getvalue()
