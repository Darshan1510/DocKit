# Build targets for PDF Compressor (PDF Tools).
#
# Usage:
#   make build-mac        # Build the macOS .app bundle into dist/
#   make build-windows    # Build the Windows .exe directory into dist/
#   make release-mac VERSION=v0.1.0   # Build and publish a GitHub release (macOS)
#   make icon             # Regenerate assets/app.icns and assets/app.ico
#   make clean            # Remove build artifacts

PYI := uv run pyinstaller
NAME := PDFCompressor
ENTRY := main.py
ARCH := $(shell uname -m)

COLLECT := \
	--collect-all uvicorn \
	--collect-all fastapi \
	--collect-all webview \
	--collect-all pikepdf \
	--collect-all pypdfium2 \
	--collect-all pypdfium2_raw \
	--collect-all reportlab \
	--collect-all img2pdf

.PHONY: build-mac build-windows build-mac-ci build-windows-ci release-mac icon clean

build-mac:
	$(PYI) --noconfirm --onedir --windowed \
		--add-data "static:static" \
		$(COLLECT) \
		--name "$(NAME)" \
		--icon assets/app.icns \
		$(ENTRY)

build-windows:
	$(PYI) --noconfirm --onedir --windowed \
		--add-data "static;static" \
		$(COLLECT) \
		--name "$(NAME)" \
		--icon assets/app.ico \
		$(ENTRY)

build-mac-ci: build-mac
	ditto -c -k --sequesterRsrc --keepParent "dist/$(NAME).app" "$(NAME)-macOS-$(ARCH).zip"

build-windows-ci: build-windows
	powershell Compress-Archive -Path "dist/$(NAME)" -DestinationPath "$(NAME)-Windows.zip" -Force

# Builds on this Mac and uploads to GitHub Releases with the gh CLI.
# Does not need GitHub Actions. The tag is created on GitHub if it does not exist.
release-mac:
	@test -n "$(VERSION)" || (echo "Usage: make release-mac VERSION=v0.1.0" && exit 1)
	rm -rf build/ dist/ "$(NAME)-macOS-$(ARCH).zip"
	$(MAKE) build-mac-ci
	gh release create "$(VERSION)" "$(NAME)-macOS-$(ARCH).zip" --title "$(VERSION)" --generate-notes

icon:
	uv run assets/make_icon.py

clean:
	rm -rf build/ dist/
