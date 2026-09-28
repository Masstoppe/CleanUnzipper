# 🗜️ CleanUnzipper

**Extract ZIP files and progressively free disk space — file by file.**

Solves a common problem: you have a large ZIP file but not enough free disk space to hold both the archive *and* the extracted contents at the same time.

## How It Works

Instead of extracting everything and *then* deleting the ZIP, CleanUnzipper processes each file individually:

1. **Extract** a single file from the ZIP to the destination
2. **Truncate** the ZIP archive to remove that file's compressed data
3. **Repeat** for the next file — disk space is freed progressively

```
ZIP:  [File A][File B][File C][Central Directory]
        ↓ extract C, truncate
ZIP:  [File A][File B][New CD]           💾 space freed!
        ↓ extract B, truncate
ZIP:  [File A][New CD]                   💾 more space freed!
        ↓ extract A, delete ZIP
Done! All files extracted, ZIP removed.
```

## Features

- **Progressive extraction** — frees disk space after each file
- **Deflate64 support** — falls back to 7-Zip for compression methods Python can't handle
- **Simple GUI** — dark-themed interface with progress bar and freed-space counter
- **Safe** — if extraction fails, already extracted files are preserved

## Requirements

- **Python 3.10+** (if running from source)
- **7-Zip** (optional, required only for Deflate64-compressed archives) — [download](https://7-zip.org)

## Usage

### Pre-built executable (recommended)

Download `CleanUnzipper.exe` from the [latest release](../../releases/latest) — no Python installation needed.

### From source

```bash
python clean_unzipper.py
```

## Building the EXE

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name CleanUnzipper clean_unzipper.py
```

The executable will be in the `dist/` folder.
