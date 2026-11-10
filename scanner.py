"""Document scanning: photos of paper -> clean scanned pages, and a "scanned look" for PDFs.

Page detection is classical OpenCV (edges + brightness masks), so it needs no
model download and works offline.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import img2pdf
import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageOps

from tools import PDFIUM_LOCK, ToolError, check_pixels, images_to_pdf, page_count

SCAN_MODES = ("bw", "gray", "color", "original")
DETECT_SIZE = 800  # long side used for page detection
MAX_SIDE = 3500  # long side cap for the flattened page


# --- Loading ---


def load_image(path: Path, name: str) -> np.ndarray:
    """Read an image as upright BGR (EXIF orientation applied, alpha flattened on white)."""
    try:
        with Image.open(path) as im:
            check_pixels(*im.size, name)
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im, mask=im.split()[-1])
                im = bg
            rgb = np.asarray(im.convert("RGB"))
    except ToolError:
        raise
    except Exception:
        raise ToolError(f"Could not read image {name}.")
    if min(rgb.shape[:2]) < 32:
        raise ToolError(f"Image {name} is too small to scan.")
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


# --- Page detection ---


def order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    c = pts.mean(axis=0)
    angles = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    pts = pts[np.argsort(angles)]  # clockwise in image coordinates, starting near top-left
    start = int(np.argmin(pts.sum(axis=1)))
    return np.roll(pts, -start, axis=0)


def _intersect(p1, p2, p3, p4) -> np.ndarray | None:
    """Intersection of line p1-p2 with line p3-p4."""
    d1, d2 = p2 - p1, p4 - p3
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-6:
        return None
    t = ((p3[0] - p1[0]) * d2[1] - (p3[1] - p1[1]) * d2[0]) / den
    return p1 + t * d1


def _quads_from_contour(contour: np.ndarray) -> list[np.ndarray]:
    """Page-shaped quads for a contour.

    Besides a plain 4-point fit, a 5-8 sided outline (a folded or covered corner)
    is turned into a quad by extending its 4 longest sides.
    """
    hull = cv2.convexHull(contour)
    peri = cv2.arcLength(hull, True)
    quads = []
    for f in (0.01, 0.02, 0.03, 0.05, 0.07):
        poly = cv2.approxPolyDP(hull, f * peri, True).reshape(-1, 2).astype(np.float32)
        n = len(poly)
        if n == 4:
            quads.append(poly)
            break
        if n < 4:
            break
        if n <= 8:
            sides = [(np.linalg.norm(poly[(i + 1) % n] - poly[i]), i) for i in range(n)]
            keep = sorted(i for _, i in sorted(sides, reverse=True)[:4])
            pts = [_intersect(poly[keep[k]], poly[(keep[k] + 1) % n],
                              poly[keep[(k + 1) % 4]], poly[(keep[(k + 1) % 4] + 1) % n]) for k in range(4)]
            if all(p is not None for p in pts):
                quads.append(np.array(pts, np.float32))
    return quads


def _quad_ok(q: np.ndarray, w: int, h: int) -> bool:
    """Reject quads that are tiny, nearly the whole frame, or not page shaped."""
    area = cv2.contourArea(q)
    if not 0.12 * w * h <= area <= 0.985 * w * h:
        return False
    if not cv2.isContourConvex(q.astype(np.int32)):
        return False
    q = order_corners(q)
    for i in range(4):
        a, b, c = q[i - 1], q[i], q[(i + 1) % 4]
        v1, v2 = a - b, c - b
        cos = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-6)
        if abs(cos) > 0.6:  # corner angle outside ~53-127 degrees
            return False
    sides = [np.linalg.norm(q[i] - q[(i + 1) % 4]) for i in range(4)]
    return min(sides) > 0.12 * max(w, h)


def _candidates(small: np.ndarray) -> list[tuple[np.ndarray, float]]:
    """Find page-shaped quads. Returns (quad, fit) where fit is how well the region fills it."""
    h, w = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))

    masks = []
    # Paper is bright and unsaturated: threshold brightness and "whiteness".
    whiteness = cv2.subtract(hsv[:, :, 2], hsv[:, :, 1])
    for channel in (blur, cv2.GaussianBlur(whiteness, (5, 5), 0)):
        _, m = cv2.threshold(channel, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        masks.append(m)
    # Closing the text inside the page makes the mask solid.
    masks = [cv2.morphologyEx(cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel), cv2.MORPH_CLOSE, kernel, iterations=3)
             for m in masks]

    # Edges: the page outline as closed contours.
    med = float(np.median(blur))
    for lo, hi in ((0.66 * med, 1.33 * med), (20, 60)):
        edges = cv2.Canny(blur, lo, hi)
        edges = cv2.dilate(edges, kernel, iterations=1)
        masks.append(edges)

    found = []
    for i, m in enumerate(masks):
        is_edge = i >= 2
        mode = cv2.RETR_LIST if is_edge else cv2.RETR_EXTERNAL
        contours, _ = cv2.findContours(m, mode, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(contours, key=cv2.contourArea, reverse=True)[:8]:
            if cv2.contourArea(cv2.convexHull(c)) < 0.12 * w * h:
                break
            for q in _quads_from_contour(c):
                if not _quad_ok(q, w, h):
                    continue
                if is_edge:
                    fit = 1.0  # an outline: no region to compare against
                else:
                    fit = cv2.contourArea(c) / max(cv2.contourArea(q), 1)
                    if not 0.85 <= fit <= 1.05:
                        continue
                found.append((q, min(fit, 1.0)))
    return found


def _contrast(small_gray: np.ndarray, q: np.ndarray) -> float:
    """How much brighter the paper inside the quad is than a band just outside it."""
    h, w = small_gray.shape
    inside = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(inside, q.astype(np.int32), 255)
    k = max(5, int(0.04 * max(w, h)))
    ring = cv2.subtract(cv2.dilate(inside, np.ones((k, k), np.uint8)), inside)
    core = cv2.erode(inside, np.ones((k, k), np.uint8))
    if not ring.any() or not core.any():
        return 0.0
    # A high percentile inside ignores the text; the median outside is the surface below.
    return float(np.percentile(small_gray[core > 0], 75) - np.median(small_gray[ring > 0]))


def _edge_support(edges: np.ndarray, q: np.ndarray) -> float:
    """Fraction of the quad's outline that lies on image edges (sides on the frame border count 0)."""
    h, w = edges.shape
    q = order_corners(q)
    hits = []
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        t = np.linspace(0, 1, 100)[:, None]
        pts = np.round(a + (b - a) * t).astype(int)
        inside = (pts[:, 0] >= 0) & (pts[:, 0] < w) & (pts[:, 1] >= 0) & (pts[:, 1] < h)
        on_border = (pts[:, 0] <= 1) | (pts[:, 0] >= w - 2) | (pts[:, 1] <= 1) | (pts[:, 1] >= h - 2)
        pts = pts[inside & ~on_border]
        hits.append(float(edges[pts[:, 1], pts[:, 0]].mean() / 255) if len(pts) else 0.0)
    return sum(hits) / 4


def detect_page(img: np.ndarray) -> np.ndarray | None:
    """Return the page corners (TL, TR, BR, BL) in image pixels, or None."""
    h, w = img.shape[:2]
    scale = min(1.0, DETECT_SIZE / max(h, w))
    small = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else img
    sgray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(sgray, (5, 5), 0), 8, 24)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8))

    best, best_score = None, 0.0
    sh, sw = small.shape[:2]
    found = [(order_corners(q), fit) for q, fit in _candidates(small)]
    near = 0.02 * max(sw, sh)
    for q, fit in found:
        # Quads that several masks agree on are more likely the real page.
        votes = sum(np.abs(q - other).max() < near for other, _ in found)
        contrast = _contrast(sgray, q)
        support = _edge_support(edges, q)
        if contrast < 3 or support < 0.5:  # a page is lighter than what it lies on, with visible edges
            continue
        area = cv2.contourArea(q) / (sw * sh)
        # Clear page edges matter more than how much lighter the page is (paper on paper).
        score = np.sqrt(area) * fit * support ** 2 * min(contrast, 12) / 12 * (1 + 0.15 * min(votes, 6))
        if score > best_score:
            best, best_score = q, score
    if best is None:
        return None
    return _refine(img, order_corners(best) / scale)


def _refine(img: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Snap corners to sub-pixel accuracy on the full image."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    win = max(3, int(0.004 * max(img.shape[:2])))
    pts = corners.reshape(-1, 1, 2).astype(np.float32).copy()
    try:
        refined = cv2.cornerSubPix(gray, pts, (win, win), (-1, -1),
                                   (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01))
    except cv2.error:
        return corners
    refined = refined.reshape(4, 2)
    # Keep the refinement only when it stays close (it can jump to nearby text corners).
    if np.max(np.linalg.norm(refined - corners, axis=1)) > 3 * win:
        return corners
    return refined


# --- Flatten and enhance ---


def warp(img: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Perspective-warp the quad to a flat rectangle."""
    tl, tr, br, bl = order_corners(corners)
    width = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    height = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    s = min(1.0, MAX_SIDE / max(width, height))
    width, height = max(1, round(width * s)), max(1, round(height * s))
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], np.float32)
    m = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], np.float32), dst)
    return cv2.warpPerspective(img, m, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _background(channel: np.ndarray) -> np.ndarray:
    """Estimate the paper (lighting and shadows) by removing dark details such as text."""
    h, w = channel.shape
    s = 1000 / max(h, w)
    small = cv2.resize(channel, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    small = cv2.dilate(small, np.ones((9, 9), np.uint8))
    small = cv2.medianBlur(small, 21)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def _flatten(channel: np.ndarray) -> np.ndarray:
    """Divide by the background: removes shadows and makes the paper white."""
    bg = _background(channel).astype(np.float32)
    out = channel.astype(np.float32) / np.maximum(bg, 1) * 255
    return np.clip(out, 0, 255).astype(np.uint8)


def _stretch(gray: np.ndarray, low_pct: float = 1, high_pct: float = 99) -> np.ndarray:
    lo, hi = np.percentile(gray, (low_pct, high_pct))
    if hi - lo < 10:
        return gray
    out = (gray.astype(np.float32) - lo) * (255 / (hi - lo))
    return np.clip(out, 0, 255).astype(np.uint8)


def enhance(img: np.ndarray, mode: str) -> Image.Image:
    """Apply a scan mode to a flattened page. Returns a PIL image ready to save."""
    if mode == "original":
        return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    if mode == "color":
        chans = [_flatten(c) for c in cv2.split(img)]
        out = cv2.merge(chans)
        hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * 1.25, 0, 255)  # flattening washes colors out a little
        out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
        lut = np.clip((np.arange(256, dtype=np.float32) / 255) ** 1.15 * 255, 0, 255).astype(np.uint8)
        return Image.fromarray(cv2.cvtColor(cv2.LUT(out, lut), cv2.COLOR_BGR2RGB))

    gray = _flatten(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    if mode == "gray":
        return Image.fromarray(_stretch(gray, 0.5, 99.5))
    # Black & white: adaptive threshold on the shadow-free page.
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    block = max(15, (min(gray.shape) // 40) | 1)
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, 12)
    bw[gray > 235] = 255  # adaptive threshold speckles flat paper; clear it
    bw = cv2.medianBlur(bw, 3)
    return Image.fromarray(bw).convert("1", dither=Image.Dither.NONE)


# --- Scan to PDF ---


def _corners_from_option(value, w: int, h: int) -> np.ndarray | None:
    """Manual corners from the UI: 4 [x, y] pairs normalized to 0..1."""
    if not value:
        return None
    try:
        pts = np.array(value, dtype=np.float32).reshape(4, 2)
    except (ValueError, TypeError):
        raise ToolError("Invalid page corners.")
    if not np.all(np.isfinite(pts)) or pts.min() < -0.01 or pts.max() > 1.01:
        raise ToolError("Invalid page corners.")
    pts = np.clip(pts, 0, 1) * [w - 1, h - 1]
    if cv2.contourArea(order_corners(pts)) < 0.01 * w * h:
        raise ToolError("The page corners are too close together.")
    return order_corners(pts)


def detect_corners_normalized(path: Path, name: str) -> tuple[list[list[float]], bool]:
    """For the corner editor: detected corners as 0..1 fractions, and whether edges were found."""
    img = load_image(path, name)
    h, w = img.shape[:2]
    corners = detect_page(img)
    if corners is None:
        return [[0, 0], [1, 0], [1, 1], [0, 1]], False
    return [[float(x) / (w - 1), float(y) / (h - 1)] for x, y in corners], True


def scan_pages(paths: list[Path], names: list[str], mode: str, manual: list, fmt: str,
               workdir: Path) -> tuple[list[Path], list[str]]:
    """Flatten and enhance each photo. manual[i]: corners or None. fmt: 'jpg', 'png' or 'auto'.

    Returns (page image files, names of photos where no page edges were found).
    """
    if not paths:
        raise ToolError("Add at least one photo.")
    if mode not in SCAN_MODES:
        mode = "bw"
    pages, not_found = [], []
    for i, (p, name) in enumerate(zip(paths, names)):
        img = load_image(p, name)
        h, w = img.shape[:2]
        corners = _corners_from_option(manual[i] if i < len(manual) else None, w, h)
        if corners is None:
            corners = detect_page(img)
            if corners is None:
                not_found.append(name)
        if corners is None:
            page = img
        else:
            page = warp(img, corners)
            if not_found[-1:] != [name] and (i >= len(manual) or not manual[i]):
                # Auto-detected edges often keep a sliver of the background: trim it.
                ph, pw = page.shape[:2]
                dy, dx = round(ph * 0.004), round(pw * 0.004)
                page = page[dy:ph - dy, dx:pw - dx]
        result = enhance(page, mode)
        # Black & white pages are 1-bit PNGs (small and crisp) unless JPG is asked for.
        if fmt == "png" or (fmt != "jpg" and result.mode == "1"):
            dest = workdir / f"scan-{i}.png"
            result.save(dest, "PNG", optimize=True)
        else:
            dest = workdir / f"scan-{i}.jpg"
            result.convert("L" if result.mode == "1" else result.mode).save(dest, "JPEG", quality=85, optimize=True)
        pages.append(dest)
    return pages, not_found


def scan_to_pdf(paths: list[Path], names: list[str], mode: str, page_size: str, manual: list,
                workdir: Path, out: Path) -> list[str]:
    """Scan photos into one PDF. Returns the names of photos where no page edges were found."""
    pages, not_found = scan_pages(paths, names, mode, manual, "auto", workdir)
    images_to_pdf(pages, page_size, "none", workdir, out)
    return not_found


# --- Make a PDF look scanned ---

INTENSITY = {
    # max rotation (degrees), blur sigma, noise sigma, contrast, uneven lighting
    "light": (0.4, 0.35, 3.0, 1.04, 0.03),
    "medium": (0.7, 0.6, 6.0, 1.10, 0.06),
    "strong": (1.0, 0.9, 10.0, 1.18, 0.10),
}
PAPER_TINT = np.array([236, 244, 250], np.float32) / 255  # BGR, slightly warm


def scanned_look(page: Image.Image, intensity: str, color: str, rng: np.random.Generator) -> Image.Image:
    """Make one rendered page look like it went through a scanner."""
    max_rot, blur, noise, contrast, light = INTENSITY.get(intensity, INTENSITY["medium"])
    img = cv2.cvtColor(np.asarray(page.convert("RGB")), cv2.COLOR_RGB2BGR).astype(np.float32)
    h, w = img.shape[:2]

    if color in ("gray", "bw"):
        img = cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)

    # Contrast: darken ink a little, keep paper near white.
    img = np.clip((img - 255) * contrast + 255, 0, 255)

    if color == "color":
        img *= PAPER_TINT

    # Uneven lighting: a soft gradient across the page.
    if light:
        gx, gy = rng.uniform(-1, 1, 2)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        grad = 1 - light * (0.5 + 0.5 * ((xx / w - 0.5) * gx + (yy / h - 0.5) * gy))
        img *= grad[:, :, None]

    # Slightly crooked page.
    angle = rng.uniform(0.3, max(max_rot, 0.31)) * rng.choice([-1, 1])
    fill = tuple(float(v) for v in (img[2, 2] if color != "bw" else (255, 255, 255)))
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    m[:, 2] += rng.uniform(-0.004, 0.004, 2) * [w, h]  # and a little off-center
    img = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=fill)

    sigma = blur * max(w, h) / 1700  # scale with render DPI (1700 px ~ A4 at 200 DPI)
    if sigma > 0.1:
        img = cv2.GaussianBlur(img, (0, 0), sigma)

    grain = rng.normal(0, noise, (h, w, 1)).astype(np.float32)
    img = np.clip(img + grain, 0, 255).astype(np.uint8)

    if color == "bw":
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        bw = cv2.GaussianBlur(bw, (0, 0), max(0.5, sigma * 0.6))  # soft edges like a real 1-bit scan in JPEG
        return Image.fromarray(bw)
    if color == "gray":
        return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))


def make_scanned(path: Path, out: Path, intensity: str, color: str, dpi: int, workdir: Path,
                 seed: int = 0) -> dict:
    """Render every page, apply the scanned look, and rebuild a PDF of JPEG pages."""
    n = page_count(path)  # raises ToolError for encrypted/damaged files
    if n > 500:
        raise ToolError("This PDF has too many pages (500 max).")
    if color not in ("gray", "color", "bw"):
        color = "gray"
    rng = np.random.default_rng(seed)
    pages, sizes = [], []
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(path)
        try:
            for i in range(len(doc)):
                # Processed inside the lock so only one rendered page is in memory at a time.
                page = doc[i]
                w, h = page.get_size()
                if int(page.get_rotation()) % 180:
                    w, h = h, w
                sizes.append((w, h))
                rendered = page.render(scale=dpi / 72, may_draw_forms=True).to_pil()
                img = scanned_look(rendered, intensity, color, rng)
                dest = workdir / f"page-{i}.jpg"
                img.save(dest, "JPEG", quality=80, optimize=True, dpi=(dpi, dpi))
                pages.append(str(dest))
        finally:
            doc.close()
    # Give every page its exact original size (pixel sizes are rounded).
    queue = iter(sizes)

    def layout(*_):
        w, h = next(queue)
        return w, h, w, h

    out.write_bytes(img2pdf.convert(pages, layout_fun=layout))
    return {"pages": n}
