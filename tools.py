"""PDF tools: merge, split, organize, rotate, convert, number, watermark, protect.

Every function reads input files from disk and writes output files to disk.
User-facing problems raise ToolError with a message that is safe to show.
"""

from __future__ import annotations

import io
import threading
import zipfile
from pathlib import Path

import img2pdf
import pikepdf
import pypdfium2 as pdfium
from PIL import Image, ImageOps
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas

# pdfium is not thread-safe; FastAPI runs sync endpoints in a thread pool.
PDFIUM_LOCK = threading.Lock()


class ToolError(Exception):
    pass


# --- Helpers ---


def open_pdf(path: Path, password: str = "") -> pikepdf.Pdf:
    try:
        return pikepdf.open(path, password=password)
    except pikepdf.PasswordError:
        if password:
            raise ToolError("Wrong password.")
        raise ToolError("This PDF is password protected. Use Unlock PDF first.")
    except pikepdf.PdfError:
        raise ToolError("Could not read this PDF. It may be damaged.")


def save_pdf(pdf: pikepdf.Pdf, out: Path, **kwargs) -> None:
    pdf.save(
        out,
        compress_streams=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate,
        **kwargs,
    )


def zip_files(files: list[tuple[Path, str]], out: Path) -> None:
    """files: (path on disk, name inside the zip)."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path, name in files:
            z.write(path, name)


def parse_ranges(text: str, page_count: int) -> list[list[int]]:
    """Parse '1-3, 5, 8-' into groups of 0-based page indexes."""
    groups: list[list[int]] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                start = int(a) if a else 1
                end = int(b) if b else page_count
            else:
                start = end = int(part)
        except ValueError:
            raise ToolError(f"'{part}' is not a page number or range.")
        if start < 1 or end > page_count or start > end:
            raise ToolError(f"Range '{part}' is outside pages 1-{page_count}.")
        groups.append(list(range(start - 1, end)))
    if not groups:
        raise ToolError("Enter at least one page number or range.")
    return groups


def page_count(path: Path) -> int:
    with open_pdf(path) as pdf:
        return len(pdf.pages)


def _pdf_from_pages(sources: list[tuple[pikepdf.Pdf, int, int]]) -> pikepdf.Pdf:
    """Build a new PDF. sources: (source pdf, page index, extra rotation)."""
    out = pikepdf.new()
    for src, index, rotate in sources:
        out.pages.append(src.pages[index])
        if rotate % 360:
            out.pages[-1].rotate(rotate % 360, relative=True)
    return out


# --- Organize: merge, split, reorder, rotate, delete, extract ---


def merge(paths: list[Path], out: Path) -> None:
    if len(paths) < 2:
        raise ToolError("Add at least 2 PDF files to merge.")
    sources = [open_pdf(p) for p in paths]
    try:
        merged = pikepdf.new()
        for src in sources:
            merged.pages.extend(src.pages)
        save_pdf(merged, out)
    finally:
        for s in sources:
            s.close()


def split(path: Path, stem: str, mode: str, ranges: str, every: int, workdir: Path) -> list[tuple[Path, str]]:
    with open_pdf(path) as src:
        n = len(src.pages)
        if mode == "ranges":
            groups = parse_ranges(ranges, n)
        else:
            every = max(1, every)
            groups = [list(range(i, min(i + every, n))) for i in range(0, n, every)]
        results = []
        for gi, group in enumerate(groups, 1):
            label = f"{group[0] + 1}" if len(group) == 1 else f"{group[0] + 1}-{group[-1] + 1}"
            dest = workdir / f"part-{gi}.pdf"
            save_pdf(_pdf_from_pages([(src, i, 0) for i in group]), dest)
            results.append((dest, f"{stem}-pages-{label}.pdf"))
        return results


def organize(path: Path, pages: list[dict], out: Path) -> None:
    """pages: [{"index": 0-based source page, "rotate": degrees}] in the new order."""
    if not pages:
        raise ToolError("The result would have no pages.")
    with open_pdf(path) as src:
        n = len(src.pages)
        items = []
        for p in pages:
            i = int(p["index"])
            if not 0 <= i < n:
                raise ToolError("Page list does not match this PDF.")
            items.append((src, i, int(p.get("rotate", 0))))
        save_pdf(_pdf_from_pages(items), out)


def rotate_all(path: Path, angle: int, out: Path) -> None:
    if angle % 90:
        raise ToolError("Rotation must be 90, 180 or 270 degrees.")
    with open_pdf(path) as pdf:
        for page in pdf.pages:
            page.rotate(angle % 360, relative=True)
        save_pdf(pdf, out)


def select_pages(path: Path, selected: list[int], keep: bool, out: Path) -> None:
    """keep=True: keep only selected pages. keep=False: delete selected pages."""
    with open_pdf(path) as src:
        n = len(src.pages)
        chosen = {i for i in selected if 0 <= i < n}
        if not chosen:
            raise ToolError("Select at least one page.")
        indexes = [i for i in range(n) if (i in chosen) == keep]
        if not indexes:
            raise ToolError("You cannot remove every page.")
        save_pdf(_pdf_from_pages([(src, i, 0) for i in indexes]), out)


# --- Convert ---


def render_thumbnail(path: Path, index: int, width: int) -> bytes:
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(path)
        try:
            page = doc[index]
            scale = width / page.get_width()
            img = page.render(scale=scale, may_draw_forms=True).to_pil()  # applies /Rotate
        finally:
            doc.close()
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    return buf.getvalue()


def pdf_to_images(path: Path, stem: str, dpi: int, fmt: str, workdir: Path) -> list[tuple[Path, str]]:
    page_count(path)  # raises ToolError for encrypted/damaged files
    ext = "png" if fmt == "png" else "jpg"
    results = []
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(path)
        try:
            for i in range(len(doc)):
                page = doc[i]
                img = page.render(scale=dpi / 72, may_draw_forms=True).to_pil()
                dest = workdir / f"page-{i + 1}.{ext}"
                if ext == "png":
                    img.save(dest, "PNG", optimize=True)
                else:
                    img.convert("RGB").save(dest, "JPEG", quality=90)
                results.append((dest, f"{stem}-page-{i + 1}.{ext}"))
        finally:
            doc.close()
    return results


PAGE_SIZES = {"a4": (595.28, 841.89), "letter": (612.0, 792.0)}
MARGINS = {"none": 0, "small": 20, "big": 50}


def images_to_pdf(paths: list[Path], page_size: str, margin: str, workdir: Path, out: Path) -> None:
    if not paths:
        raise ToolError("Add at least one image.")
    prepared = []
    for i, p in enumerate(paths):
        try:
            img = Image.open(p)
            fmt = img.format
            img = ImageOps.exif_transpose(img)
        except Exception:
            raise ToolError(f"Could not read image {p.name}.")
        if fmt == "JPEG" and img.mode in ("RGB", "L", "CMYK"):
            prepared.append(str(p))  # embed original JPEG bytes: no quality loss
            continue
        # img2pdf refuses alpha channels and exotic formats: flatten onto white
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, "white")
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        dest = workdir / f"img-{i}.png"
        img.save(dest, "PNG")
        prepared.append(str(dest))

    kwargs = {"rotation": img2pdf.Rotation.ifvalid}
    if page_size in PAGE_SIZES:
        m = MARGINS.get(margin, 0)
        kwargs["layout_fun"] = img2pdf.get_layout_fun(PAGE_SIZES[page_size], border=(m, m), auto_orient=True)
    # otherwise img2pdf's default layout: each page is the size of its image
    out.write_bytes(img2pdf.convert(prepared, **kwargs))


# --- Stamp text on pages: page numbers, watermark ---


def _overlay_each_page(pdf: pikepdf.Pdf, draw) -> list[pikepdf.Pdf]:
    """Call draw(c, width, height, index, total) for every page.

    The canvas is set up so draw() works in the page's *visual* coordinates
    (after /Rotate), with the origin at the bottom-left as the reader sees it.
    Returns the overlay PDFs: keep them open until the target PDF is saved.
    """
    total = len(pdf.pages)
    overlays = []  # keep overlay PDFs open until the target is saved
    for i, page in enumerate(pdf.pages):
        x0, y0, x1, y1 = (float(v) for v in page.mediabox)
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        w, h = x1 - x0, y1 - y0
        rotation = int(page.obj.get("/Rotate", 0)) % 360

        # add_overlay() counter-rotates the overlay for /Rotate, so draw upright
        # on a canvas with the page's visual size.
        vw, vh = (h, w) if rotation in (90, 270) else (w, h)
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(vw, vh))
        draw(c, vw, vh, i, total)
        c.showPage()
        c.save()

        overlay = pikepdf.open(io.BytesIO(buf.getvalue()))
        overlays.append(overlay)
        page.add_overlay(overlay.pages[0], pikepdf.Rectangle(x0, y0, x1, y1))
    return overlays


NUMBER_FORMATS = {
    "n": "{n}",
    "page-n": "Page {n}",
    "n-of-total": "{n} of {total}",
    "page-n-of-total": "Page {n} of {total}",
}


def page_numbers(path: Path, out: Path, position: str, start: int, fmt: str, size: int,
                 skip_first: bool) -> None:
    template = NUMBER_FORMATS.get(fmt, "{n}")
    vertical, _, horizontal = position.partition("-")
    margin = 28

    with open_pdf(path) as pdf:
        first = 1 if skip_first else 0
        last_number = start + len(pdf.pages) - 1 - first

        def draw(c, w, h, i, total):
            if i < first:
                return
            text = template.format(n=start + i - first, total=last_number)
            c.setFont("Helvetica", size)
            c.setFillColor(HexColor("#222222"))
            y = h - margin - size if vertical == "top" else margin
            if horizontal == "left":
                c.drawString(margin, y, text)
            elif horizontal == "right":
                c.drawRightString(w - margin, y, text)
            else:
                c.drawCentredString(w / 2, y, text)

        overlays = _overlay_each_page(pdf, draw)
        save_pdf(pdf, out)
        for o in overlays:
            o.close()


def watermark(path: Path, out: Path, text: str, size: int, opacity: float, rotation: int,
              color: str, position: str) -> None:
    text = text.strip()
    if not text:
        raise ToolError("Enter watermark text.")
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        raise ToolError("Watermark text supports Latin letters only.")
    try:
        fill = HexColor(color)
    except Exception:
        fill = HexColor("#dc2626")

    with open_pdf(path) as pdf:

        def draw(c, w, h, i, total):
            c.saveState()
            c.setFillColor(fill, alpha=max(0.05, min(opacity, 1.0)))
            c.setFont("Helvetica-Bold", size)
            y = {"top": h - size * 2, "bottom": size * 1.5}.get(position, h / 2)
            c.translate(w / 2, y)
            c.rotate(rotation)
            c.drawCentredString(0, -size / 3, text)
            c.restoreState()

        overlays = _overlay_each_page(pdf, draw)
        save_pdf(pdf, out)
        for o in overlays:
            o.close()


# --- Security ---


def protect(path: Path, password: str, out: Path) -> None:
    if len(password) < 1:
        raise ToolError("Enter a password.")
    with open_pdf(path) as pdf:
        save_pdf(pdf, out, encryption=pikepdf.Encryption(owner=password, user=password, R=6))


def unlock(path: Path, password: str, out: Path) -> None:
    with open_pdf(path, password=password) as pdf:
        save_pdf(pdf, out)
