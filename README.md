# PDF Compressor

<p align="center"><img src="assets/app.png" width="160" alt="PDF Compressor icon"></p>

A small desktop app with everyday PDF tools: compress, merge, split, organize, convert, number, watermark, protect and unlock. Everything runs on your computer. Files are never uploaded anywhere.

## Install

### macOS

Open Terminal (Spotlight → type "Terminal" → Enter) and paste:

```sh
curl -LsSf https://raw.githubusercontent.com/Darshan1510/pdf-compressor/main/install.sh | sh
```

This downloads the latest release, installs it to `~/Downloads`, strips the macOS quarantine flag (so Gatekeeper doesn't block the unsigned app), and launches it.

To update, re-run the same command.

**Why is this needed?** The app isn't signed with an Apple Developer ID, so double-clicking the downloaded `.app` shows a "damaged" or "unidentified developer" dialog. Stripping the quarantine attribute is the standard workaround.

**Want to inspect the script before running it?** You should. View it here: [install.sh](./install.sh).

### Windows

A ready-made Windows download is not available yet. To run it on Windows, use the source code (needs [uv](https://docs.astral.sh/uv/)):

```sh
git clone https://github.com/Darshan1510/pdf-compressor.git
cd pdf-compressor
uv sync
uv run main.py
```

## Troubleshooting (macOS)

**"PDFCompressor is damaged and can't be opened"** after installing manually (not via the script): the quarantine attribute wasn't removed. Run:

```sh
xattr -cr ~/Downloads/PDFCompressor.app
```

**Installer says "Intel Macs are not currently supported"**: only Apple Silicon (arm64) builds are published.

## How to use

### Basic steps (all tools)

1. Open the app. You see a grid of tools, grouped as Optimize, Organize, Convert, Edit and Security.
2. Click a tool card, for example **Compress PDF**.
3. Drag and drop your file(s) into the box, or click the box to choose files.
4. Set the options in the **Options** panel on the right.
5. Click the blue action button (for example **Compress**).
6. When you see **Done!**, click **Download**. In the desktop app a Save dialog opens; choose where to save the file.
7. Click **Start over** to use the same tool again, or **All tools** (or **← All tools** at the top) to pick another tool.

Your original files are never changed. The app always saves a new file.

### Step-by-step for each tool

**Compress PDF**
1. Add one or more PDFs.
2. Choose a level: **Light** (best quality), **Balanced** (recommended) or **Strong** (smallest file).
3. For your own values, choose **Custom** and move the **Image DPI** and **JPEG quality** sliders.
4. Optional: tick **Convert images to grayscale** for an even smaller file.
5. Click **Compress**. The result shows the size before and after. Several files download as one `.zip`.

**Merge PDF**
1. Add two or more PDFs.
2. Set the order: drag the files, or use the ↑ and ↓ buttons. The top file comes first.
3. Click **Merge PDFs**.

**Split PDF**
1. Add one PDF. You see thumbnails of all pages.
2. Choose **Every N pages** and enter a number (1 = one file per page), or choose **Custom ranges** and enter ranges such as `1-3, 4, 5-` (`5-` means page 5 to the end).
3. Click **Split PDF**. The parts download as one `.zip`.

**Organize PDF**
1. Add one PDF.
2. Drag the page thumbnails to change the order.
3. Hover a page and click **↻** to rotate it, or **✕** to delete it. Use **Rotate all** or **Reset** if needed.
4. Click **Save changes**.

**Rotate PDF**
1. Add one PDF.
2. Choose **↻ 90°**, **180°** or **↺ 90°**. The thumbnails show a preview.
3. Click **Rotate PDF**.

**Remove pages**
1. Add one PDF.
2. Click each page you want to delete. Selected pages are marked with a red ✕.
3. Click **Remove pages**.

**Extract pages**
1. Add one PDF.
2. Click each page you want to keep. Selected pages are marked with a blue ✓.
3. Click **Extract pages**. Only the selected pages are saved in the new PDF.

**PDF to JPG**
1. Add one PDF.
2. Choose the format (**JPG** or **PNG**) and the quality (**Screen**, **Normal** or **High**).
3. Click **Convert to images**. Several pages download as one `.zip`.

**JPG to PDF**
1. Add one or more images (JPG, PNG, GIF, BMP, TIFF or WEBP).
2. Set the order: drag the images, or use the ↑ and ↓ buttons.
3. Choose the page size (**A4**, **Letter** or **Fit image**) and the margin.
4. Click **Create PDF**.

**Page numbers**
1. Add one PDF.
2. Choose the position (for example **bottom middle**) and the format (for example `Page 1 of 10`).
3. Optional: change the first number and the font size, or tick **Skip first page (cover)**.
4. Click **Add page numbers**.

**Watermark**
1. Add one PDF.
2. Enter the text (for example `CONFIDENTIAL`).
3. Choose the position, angle, size, opacity and color.
4. Click **Add watermark**.

**Protect PDF**
1. Add one PDF.
2. Enter a password two times.
3. Click **Protect PDF**. Keep the password safe: it cannot be recovered.

**Unlock PDF**
1. Add a password-protected PDF.
2. Enter its current password.
3. Click **Unlock PDF**. The saved copy has no password.

> A password-protected PDF must be unlocked with **Unlock PDF** before you can use it with the other tools.

## Tools

| Group    | Tool          | What it does                                                    |
|----------|---------------|-----------------------------------------------------------------|
| Optimize | Compress PDF  | Presets (Light / Balanced / Strong) or Custom DPI and quality   |
| Organize | Merge PDF     | Combine several PDFs in the order you choose                    |
| Organize | Split PDF     | Every N pages, or custom ranges such as `1-3, 4, 5-`             |
| Organize | Organize PDF  | Page thumbnails: drag to reorder, rotate or delete pages        |
| Organize | Rotate PDF    | Rotate all pages 90°, 180° or 270°                              |
| Organize | Remove pages  | Click pages to delete them                                      |
| Organize | Extract pages | Click pages to save as a new PDF                                |
| Convert  | PDF to JPG    | Every page as JPG or PNG (zip for several pages)                |
| Convert  | JPG to PDF    | Images to one PDF: A4, Letter or image size, with margins       |
| Edit     | Page numbers  | 6 positions, 4 formats, start number, skip cover page           |
| Edit     | Watermark     | Text, position, angle, size, opacity and color                  |
| Security | Protect PDF   | AES-256 password encryption                                     |
| Security | Unlock PDF    | Remove a password you know                                      |

### Compression presets

| Preset   | Image DPI | JPEG quality | Use for                        |
|----------|-----------|--------------|--------------------------------|
| Light    | 200       | 80           | Printing, best image quality   |
| Balanced | 144       | 65           | Email and uploads (default)    |
| Strong   | 96        | 45           | Smallest file, screen only     |
| Custom   | 50–300    | 10–95        | Your own values (sliders)      |

Compression only changes images, so text stays sharp and selectable. There is also a grayscale option.

## Development

```sh
uv sync
uv run main.py
```

### Running in a browser

To use your regular browser instead of the desktop window:

```sh
uv run main.py --browser
```

This starts the local server and opens the app at http://127.0.0.1:8010 in your default browser. Stop it with Ctrl+C.

The server uses port 8010 by default. If that port is busy, set `PORT`:

```sh
PORT=8123 uv run main.py --browser
```

### Building

```sh
make build-mac        # macOS .app bundle in dist/
make build-windows    # Windows .exe directory in dist/
make icon             # Regenerate the app icon (assets/make_icon.py)
```

### Releasing

Releases are built on a Mac and uploaded with the [GitHub CLI](https://cli.github.com/). This does not need GitHub Actions:

```sh
make release-mac VERSION=v0.1.1
```

This builds `PDFCompressor-macOS-arm64.zip` and creates a GitHub release with it. The install script always downloads the latest release.

The GitHub Actions workflow (`.github/workflows/build-release.yml`) also builds the Windows version, but it runs only when started by hand from the Actions tab, and needs Actions billing to be enabled on the account.

## Limitations

- Password-protected PDFs must be unlocked (Unlock PDF) before other tools can use them.
- Watermark and page-number text supports Latin letters only.
- PDFs that contain only text and vector graphics get small savings, because there are no images to recompress.
- 1-bit scanned images (fax/JBIG2) are left as they are; they are already very small.

## License

This project is licensed under the [MIT License](LICENSE).

The app uses open-source libraries with their own licenses, including
[pikepdf](https://github.com/pikepdf/pikepdf) (MPL-2.0),
[pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (Apache-2.0 / BSD-3-Clause),
[img2pdf](https://gitlab.mister-muffin.de/josch/img2pdf) (LGPL-3.0),
[ReportLab](https://www.reportlab.com/) (BSD),
[Pillow](https://python-pillow.org/) (MIT-CMU),
[pywebview](https://pywebview.flowrl.com/) (BSD-3-Clause),
[FastAPI](https://fastapi.tiangolo.com/) (MIT) and
[Uvicorn](https://www.uvicorn.org/) (BSD-3-Clause).
