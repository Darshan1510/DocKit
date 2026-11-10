import atexit
import io
import os
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import pikepdf
import uvicorn
import webview
from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps

import compressor
import tools
from tools import ToolError

app = FastAPI()

PORT = int(os.environ.get("PORT", "8010"))
URL = f"http://127.0.0.1:{PORT}"

# Working files live in a per-process temp folder (the .app bundle is read-only).
TMP_DIR = Path(tempfile.gettempdir()) / "pdf-compressor" / uuid.uuid4().hex
FILES_DIR = TMP_DIR / "files"
JOBS_DIR = TMP_DIR / "jobs"
FILES_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}

# file_id -> {"path", "name", "kind", "pages", "encrypted", "size"}
FILES: dict[str, dict] = {}
# job_id -> {"path", "filename"}
JOBS: dict[str, dict] = {}


def get_static_path():
    # PyInstaller creates a temp folder and stores path in _MEIPASS
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "static"
    return Path(__file__).parent / "static"


def run_server():
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


def _public(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if k != "path"}


def _get_file(file_id: str, kind: str = "pdf") -> dict:
    meta = FILES.get(file_id)
    if meta is None:
        raise ToolError("File not found. Please add it again.")
    if meta["kind"] != kind:
        raise ToolError(f"{meta['name']} is not a {'PDF' if kind == 'pdf' else 'image'} file.")
    return meta


def _stem(meta: dict) -> str:
    return Path(meta["name"]).stem or "document"


# --- Pages ---

app.mount("/static", StaticFiles(directory=get_static_path()), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(get_static_path() / "index.html")


# --- Files ---


@app.post("/upload")
def upload(files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        name = Path(f.filename or "file").name
        ext = Path(name).suffix.lower()
        file_id = uuid.uuid4().hex
        path = FILES_DIR / f"{file_id}{ext}"
        with open(path, "wb") as out:
            shutil.copyfileobj(f.file, out)
        meta = {"id": file_id, "name": name, "size": path.stat().st_size,
                "path": path, "pages": None, "encrypted": False}

        if ext == ".pdf":
            meta["kind"] = "pdf"
            try:
                with pikepdf.open(path) as pdf:
                    meta["pages"] = len(pdf.pages)
            except pikepdf.PasswordError:
                meta["encrypted"] = True
            except pikepdf.PdfError:
                path.unlink(missing_ok=True)
                results.append({"name": name, "error": "Could not read this PDF. It may be damaged."})
                continue
        elif ext in IMAGE_EXTS:
            meta["kind"] = "image"
            try:
                with Image.open(path) as im:
                    im.verify()
            except Exception:
                path.unlink(missing_ok=True)
                results.append({"name": name, "error": "Could not read this image."})
                continue
        else:
            path.unlink(missing_ok=True)
            results.append({"name": name, "error": "Unsupported file type."})
            continue

        FILES[file_id] = meta
        results.append(_public(meta))
    return results


@app.get("/thumb/{file_id}/{page}")
def thumb(file_id: str, page: int, width: int = 180):
    meta = FILES.get(file_id)
    if meta is None:
        raise HTTPException(404, "File not found")
    width = max(40, min(width, 800))
    if meta["kind"] == "image":
        with Image.open(meta["path"]) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((width, width * 2))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im, mask=im.split()[-1])
                im = bg
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            data = buf.getvalue()
    else:
        if meta["encrypted"] or meta["pages"] is None or not 0 <= page < meta["pages"]:
            raise HTTPException(404, "Page not found")
        data = tools.render_thumbnail(meta["path"], page, width)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


# --- Tools ---


def _run_tool(tool: str, ids: list[str], o: dict, work: Path) -> tuple[Path, str, dict]:
    """Run one tool. Returns (output path, download filename, extra details)."""
    out = work / "output.pdf"
    first = _get_file(ids[0], "image" if tool == "images-to-pdf" else "pdf") if ids else None
    if first is None:
        raise ToolError("Add a file first.")
    stem = _stem(first)

    if tool == "compress":
        preset = o.get("preset", "balanced")
        gray = bool(o.get("grayscale", False))
        if preset == "custom":
            s = compressor.Settings(max(36, min(int(o.get("dpi", 144)), 600)),
                                    max(5, min(int(o.get("quality", 65)), 95)), gray)
        else:
            base = compressor.PRESETS.get(preset, compressor.PRESETS["balanced"])
            s = compressor.Settings(base.dpi, base.quality, gray)
        per_file, outputs = [], []
        for i, fid in enumerate(ids):
            meta = _get_file(fid)
            tools.open_pdf(meta["path"]).close()  # friendly error for locked/damaged files
            dest = work / f"compressed-{i}.pdf"
            r = compressor.compress(meta["path"], dest, s)
            if not r.smaller:
                shutil.copyfile(meta["path"], dest)
            per_file.append({"name": meta["name"], "original_bytes": r.original_bytes,
                             "compressed_bytes": min(r.compressed_bytes, r.original_bytes)})
            outputs.append((dest, f"{_stem(meta)}-compressed.pdf"))
        before = sum(f["original_bytes"] for f in per_file)
        after = sum(f["compressed_bytes"] for f in per_file)
        details = {"files": per_file, "original_bytes": before, "compressed_bytes": after}
        if after >= before:
            raise ToolError("These PDFs are already optimized. Try a stronger setting.")
        if len(outputs) == 1:
            return outputs[0][0], outputs[0][1], details
        tools.zip_files(outputs, work / "output.zip")
        return work / "output.zip", "compressed-pdfs.zip", details

    if tool == "merge":
        tools.merge([_get_file(i)["path"] for i in ids], out)
        return out, "merged.pdf", {}

    if tool == "split":
        parts = tools.split(first["path"], stem, o.get("mode", "every"), str(o.get("ranges", "")),
                            int(o.get("every", 1) or 1), work)
        if len(parts) == 1:
            return parts[0][0], parts[0][1], {"parts": 1}
        tools.zip_files(parts, work / "output.zip")
        return work / "output.zip", f"{stem}-split.zip", {"parts": len(parts)}

    if tool == "organize":
        tools.organize(first["path"], o.get("pages", []), out)
        return out, f"{stem}-organized.pdf", {}

    if tool == "rotate":
        tools.rotate_all(first["path"], int(o.get("angle", 90)), out)
        return out, f"{stem}-rotated.pdf", {}

    if tool in ("remove", "extract"):
        tools.select_pages(first["path"], [int(i) for i in o.get("selected", [])], tool == "extract", out)
        return out, f"{stem}-{'extracted' if tool == 'extract' else 'edited'}.pdf", {}

    if tool == "pdf-to-images":
        dpi = max(36, min(int(o.get("dpi", 150)), 600))
        images = tools.pdf_to_images(first["path"], stem, dpi, o.get("format", "jpg"), work)
        if len(images) == 1:
            return images[0][0], images[0][1], {"images": 1}
        tools.zip_files(images, work / "output.zip")
        return work / "output.zip", f"{stem}-images.zip", {"images": len(images)}

    if tool == "images-to-pdf":
        paths = [_get_file(i, "image")["path"] for i in ids]
        tools.images_to_pdf(paths, o.get("page_size", "a4"), o.get("margin", "small"), work, out)
        return out, "images.pdf", {}

    if tool == "page-numbers":
        tools.page_numbers(first["path"], out, o.get("position", "bottom-center"),
                           int(o.get("start", 1) or 1), o.get("format", "n"),
                           max(6, min(int(o.get("size", 11) or 11), 72)), bool(o.get("skip_first", False)))
        return out, f"{stem}-numbered.pdf", {}

    if tool == "watermark":
        tools.watermark(first["path"], out, str(o.get("text", "")),
                        max(8, min(int(o.get("size", 60) or 60), 200)),
                        float(o.get("opacity", 0.25)), int(o.get("rotation", 45)),
                        str(o.get("color", "#dc2626")), o.get("position", "center"))
        return out, f"{stem}-watermarked.pdf", {}

    if tool == "protect":
        pw = str(o.get("password", ""))
        if pw != str(o.get("confirm", "")):
            raise ToolError("Passwords do not match.")
        tools.protect(first["path"], pw, out)
        return out, f"{stem}-protected.pdf", {}

    if tool == "unlock":
        tools.unlock(first["path"], str(o.get("password", "")), out)
        return out, f"{stem}-unlocked.pdf", {}

    raise ToolError(f"Unknown tool: {tool}")


@app.post("/tools/{tool}")
def run_tool(tool: str, body: dict = Body(...)):
    job_id = uuid.uuid4().hex
    work = JOBS_DIR / job_id
    work.mkdir()
    try:
        path, filename, details = _run_tool(tool, list(body.get("file_ids", [])), dict(body.get("options", {})), work)
    except ToolError as e:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(400, str(e))
    except (ValueError, TypeError, KeyError):
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(400, "Invalid options.")
    except pikepdf.PdfError:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(400, "Could not process this PDF. It may be damaged.")
    JOBS[job_id] = {"path": path, "filename": filename}
    return {"job_id": job_id, "filename": filename, "size": path.stat().st_size, **details}


@app.get("/download/{job_id}")
async def download(job_id: str):
    job = JOBS.get(job_id)
    if job is None or not job["path"].exists():
        raise HTTPException(404, "Result not found")
    return FileResponse(job["path"], filename=job["filename"])


class Api:
    """Methods exposed to the page as window.pywebview.api.* in the desktop window.

    The native webview ignores <a download>, so saving goes through a Save dialog.
    """

    def save_result(self, job_id: str):
        job = JOBS.get(job_id)
        if job is None or not job["path"].exists():
            return {"ok": False, "error": "Result not found"}
        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.SAVE, save_filename=job["filename"]
        )
        if not result:
            return {"ok": False, "cancelled": True}
        dest = result if isinstance(result, str) else result[0]
        try:
            shutil.copyfile(job["path"], dest)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "path": dest}


if __name__ == "__main__":
    if "--browser" in sys.argv:
        # Run without the desktop window and open the UI in the default browser
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=[URL]).start()
        run_server()
        sys.exit(0)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    window = webview.create_window(
        "PDF Tools", URL, width=1000, height=760, min_size=(640, 520), js_api=Api()
    )

    webview.start()
