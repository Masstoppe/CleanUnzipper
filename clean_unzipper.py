"""
CleanUnzipper — Extract ZIP files and progressively free disk space.

For each file inside the ZIP archive:
  1. Extract that single file to the destination
  2. Remove that file's compressed data from the ZIP archive (truncation)
  3. Move on to the next file

This way disk space is freed incrementally, allowing extraction even when
total_free_space < zip_size + extracted_size.
"""

import os
import struct
import subprocess
import shutil
import zipfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path


# ──────────────────────────────────────────────
# Color palette & design tokens
# ──────────────────────────────────────────────
COLORS = {
    "bg":           "#1a1b26",
    "surface":      "#24283b",
    "surface_alt":  "#2f3347",
    "border":       "#414868",
    "text":         "#c0caf5",
    "text_dim":     "#565f89",
    "accent":       "#7aa2f7",
    "accent_hover": "#89b4fa",
    "success":      "#9ece6a",
    "error":        "#f7768e",
    "warning":      "#e0af68",
}

FONT_FAMILY = "Segoe UI"


# ──────────────────────────────────────────────
# Helper: human-readable file sizes
# ──────────────────────────────────────────────
def format_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


# ──────────────────────────────────────────────
# ZIP central-directory helpers
# ──────────────────────────────────────────────
_CD_SIGNATURE = b"PK\x01\x02"
_CD_STRUCT_FMT = "<4s4B4HL2L5H2L"  # 46-byte fixed part of a central-dir header
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_STRUCT_FMT = "<4s4H2LH"      # 22-byte end-of-central-directory record
_Z64_EOCD_SIGNATURE = b"PK\x06\x06"
_Z64_EOCD_STRUCT_FMT = "<4sQHHII4Q"  # 56-byte ZIP64 EOCD
_Z64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_Z64_LOCATOR_STRUCT_FMT = "<4sIQI"   # 20-byte ZIP64 EOCD locator


def _build_cd_entry(entry: zipfile.ZipInfo) -> bytes:
    """Build a central-directory file header from a ZipInfo object."""
    dt = entry.date_time
    dosdate = (dt[0] - 1980) << 9 | dt[1] << 5 | dt[2]
    dostime = dt[3] << 11 | dt[4] << 5 | (dt[5] // 2)

    fname = entry.filename
    if isinstance(fname, str):
        fname = fname.encode("utf-8")

    extra = entry.extra or b""

    # Use 0xFFFFFFFF markers for values that exceed 32-bit range (ZIP64)
    compress_size = entry.compress_size if entry.compress_size < 0xFFFFFFFF else 0xFFFFFFFF
    file_size = entry.file_size if entry.file_size < 0xFFFFFFFF else 0xFFFFFFFF
    header_offset = entry.header_offset if entry.header_offset < 0xFFFFFFFF else 0xFFFFFFFF

    header = struct.pack(
        _CD_STRUCT_FMT,
        _CD_SIGNATURE,
        entry.create_version,
        entry.create_system,
        entry.extract_version,
        entry.reserved,
        entry.flag_bits,
        entry.compress_type,
        dostime,
        dosdate,
        entry.CRC,
        compress_size,
        file_size,
        len(fname),
        len(extra),
        0,  # file comment length
        0,  # disk number start
        entry.internal_attr,
        entry.external_attr,
        header_offset,
    )
    return header + fname + extra


def _write_eocd(f, num_entries: int, cd_size: int, cd_offset: int):
    """Write EOCD (and ZIP64 EOCD + locator if required) at the current position."""
    need_zip64 = (
        num_entries > 0xFFFF
        or cd_size > 0xFFFFFFFF
        or cd_offset > 0xFFFFFFFF
    )

    if need_zip64:
        z64_eocd_offset = f.tell()
        f.write(struct.pack(
            _Z64_EOCD_STRUCT_FMT,
            _Z64_EOCD_SIGNATURE,
            44,      # size of remaining record
            45, 45,  # version made-by / needed
            0, 0,    # disk numbers
            num_entries, num_entries,
            cd_size, cd_offset,
        ))
        f.write(struct.pack(
            _Z64_LOCATOR_STRUCT_FMT,
            _Z64_LOCATOR_SIGNATURE,
            0,
            z64_eocd_offset,
            1,
        ))
        # Regular EOCD with overflow markers
        f.write(struct.pack(
            _EOCD_STRUCT_FMT,
            _EOCD_SIGNATURE,
            0xFFFF, 0xFFFF,
            0xFFFF, 0xFFFF,
            0xFFFFFFFF, 0xFFFFFFFF,
            0,
        ))
    else:
        f.write(struct.pack(
            _EOCD_STRUCT_FMT,
            _EOCD_SIGNATURE,
            0, 0,
            num_entries, num_entries,
            cd_size, cd_offset,
            0,
        ))


def truncate_zip(zip_path: str, remaining_entries: list[zipfile.ZipInfo], truncate_at: int):
    """Truncate a ZIP archive at *truncate_at* and rewrite its central directory.

    *remaining_entries* must contain only the entries whose local-file-header
    offset is strictly less than *truncate_at* (i.e. data that is kept).
    """
    with open(zip_path, "r+b") as f:
        f.seek(truncate_at)
        f.truncate()

        if not remaining_entries:
            return

        cd_offset = f.tell()
        for entry in remaining_entries:
            f.write(_build_cd_entry(entry))
        cd_size = f.tell() - cd_offset

        _write_eocd(f, len(remaining_entries), cd_size, cd_offset)


# ──────────────────────────────────────────────
# Main application
# ──────────────────────────────────────────────
class CleanUnzipperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CleanUnzipper")
        self.root.configure(bg=COLORS["bg"])
        self.root.resizable(False, False)

        # Center the window
        win_w, win_h = 620, 540
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = (screen_w - win_w) // 2
        y = (screen_h - win_h) // 2
        self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")

        # State
        self.zip_path = tk.StringVar()
        self.dest_path = tk.StringVar()
        self.is_running = False

        self._setup_styles()
        self._build_ui()

    # ── Styles ────────────────────────────────
    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("Card.TFrame", background=COLORS["surface"])
        style.configure(
            "Title.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["accent"],
            font=(FONT_FAMILY, 20, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["text_dim"],
            font=(FONT_FAMILY, 10),
        )
        style.configure(
            "Field.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            font=(FONT_FAMILY, 10, "bold"),
        )
        style.configure(
            "Path.TLabel",
            background=COLORS["surface_alt"],
            foreground=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
            padding=(8, 6),
        )
        style.configure(
            "Status.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        )
        style.configure(
            "Freed.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["success"],
            font=(FONT_FAMILY, 9),
        )
        style.configure(
            "Accent.TButton",
            background=COLORS["accent"],
            foreground="#1a1b26",
            font=(FONT_FAMILY, 10, "bold"),
            padding=(12, 6),
        )
        style.map(
            "Accent.TButton",
            background=[("active", COLORS["accent_hover"]), ("disabled", COLORS["border"])],
            foreground=[("disabled", COLORS["text_dim"])],
        )
        style.configure(
            "Browse.TButton",
            background=COLORS["surface_alt"],
            foreground=COLORS["text"],
            font=(FONT_FAMILY, 9),
            padding=(10, 4),
        )
        style.map(
            "Browse.TButton",
            background=[("active", COLORS["border"])],
        )
        style.configure(
            "Custom.Horizontal.TProgressbar",
            troughcolor=COLORS["surface_alt"],
            background=COLORS["accent"],
            thickness=8,
        )

    # ── UI Construction ───────────────────────
    def _build_ui(self):
        pad = {"padx": 24}

        # Title
        ttk.Label(
            self.root, text="🗜️  CleanUnzipper", style="Title.TLabel"
        ).pack(pady=(28, 2), **pad)

        ttk.Label(
            self.root,
            text="Extrahera fil för fil — frigör utrymme stegvis",
            style="Subtitle.TLabel",
        ).pack(pady=(0, 20), **pad)

        # ── ZIP file card ─────────────────────
        self._build_card(
            label_text="ZIP-fil",
            var=self.zip_path,
            placeholder="Välj en ZIP-fil…",
            browse_cmd=self._browse_zip,
        )

        # ── Destination card ──────────────────
        self._build_card(
            label_text="Destination",
            var=self.dest_path,
            placeholder="Välj en mapp…",
            browse_cmd=self._browse_dest,
        )

        # ── Extract button ────────────────────
        self.extract_btn = ttk.Button(
            self.root,
            text="⚡  Extrahera progressivt",
            style="Accent.TButton",
            command=self._start_extraction,
        )
        self.extract_btn.pack(pady=(24, 12), ipady=4, ipadx=16)

        # ── Progress bar ──────────────────────
        self.progress = ttk.Progressbar(
            self.root,
            orient="horizontal",
            length=560,
            mode="determinate",
            style="Custom.Horizontal.TProgressbar",
        )
        self.progress.pack(pady=(4, 6), padx=24)

        # ── Status label ──────────────────────
        self.status_var = tk.StringVar(value="Redo")
        self.status_label = ttk.Label(
            self.root, textvariable=self.status_var, style="Status.TLabel"
        )
        self.status_label.pack(pady=(0, 2))

        # ── Freed-space label ─────────────────
        self.freed_var = tk.StringVar(value="")
        self.freed_label = ttk.Label(
            self.root, textvariable=self.freed_var, style="Freed.TLabel"
        )
        self.freed_label.pack(pady=(0, 16))

    def _build_card(self, label_text: str, var: tk.StringVar, placeholder: str, browse_cmd):
        """Build a labeled input card with a browse button."""
        card = ttk.Frame(self.root, style="Card.TFrame")
        card.pack(fill="x", padx=24, pady=(0, 12))

        inner = ttk.Frame(card, style="Card.TFrame")
        inner.pack(fill="x", padx=16, pady=12)

        header = ttk.Frame(inner, style="Card.TFrame")
        header.pack(fill="x")

        ttk.Label(header, text=label_text, style="Field.TLabel").pack(side="left")
        ttk.Button(header, text="Bläddra…", style="Browse.TButton", command=browse_cmd).pack(
            side="right"
        )

        # Path display
        path_label = ttk.Label(inner, text=placeholder, style="Path.TLabel", anchor="w")
        path_label.pack(fill="x", pady=(8, 0))

        def _update_label(*_args):
            val = var.get()
            path_label.configure(
                text=val if val else placeholder,
                foreground=COLORS["text"] if val else COLORS["text_dim"],
            )

        var.trace_add("write", _update_label)

    # ── Browse dialogs ────────────────────────
    def _browse_zip(self):
        path = filedialog.askopenfilename(
            title="Välj ZIP-fil",
            filetypes=[("ZIP-filer", "*.zip"), ("Alla filer", "*.*")],
        )
        if path:
            self.zip_path.set(path)
            if not self.dest_path.get():
                self.dest_path.set(str(Path(path).parent))

    def _browse_dest(self):
        path = filedialog.askdirectory(title="Välj destination")
        if path:
            self.dest_path.set(path)

    # ── 7-Zip detection ──────────────────────
    _7ZIP_PATHS = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]

    def _find_7zip(self) -> str | None:
        for p in self._7ZIP_PATHS:
            if os.path.isfile(p):
                return p
        return shutil.which("7z")

    # ── Extraction logic ──────────────────────
    def _start_extraction(self):
        zip_file = self.zip_path.get().strip()
        dest_dir = self.dest_path.get().strip()

        if not zip_file:
            messagebox.showwarning("Saknas", "Välj en ZIP-fil först.")
            return
        if not os.path.isfile(zip_file):
            messagebox.showerror("Fel", f"Filen hittades inte:\n{zip_file}")
            return
        if not zipfile.is_zipfile(zip_file):
            messagebox.showerror("Fel", "Den valda filen är inte en giltig ZIP-fil.")
            return
        if not dest_dir:
            messagebox.showwarning("Saknas", "Välj en destination först.")
            return

        os.makedirs(dest_dir, exist_ok=True)

        zip_size = format_size(os.path.getsize(zip_file))
        confirm = messagebox.askyesno(
            "Bekräfta",
            f"ZIP-filen ({zip_size}) kommer att extraheras progressivt till:\n"
            f"{dest_dir}\n\n"
            f"Varje fil extraheras och raderas ur arkivet en i taget\n"
            f"för att löpande frigöra diskutrymme.\n\n"
            f"Vill du fortsätta?",
        )
        if not confirm:
            return

        self.is_running = True
        self.extract_btn.configure(state="disabled")
        self.progress["value"] = 0
        self.freed_var.set("")
        self._set_status("Förbereder…", COLORS["accent"])

        thread = threading.Thread(
            target=self._extract_worker, args=(zip_file, dest_dir), daemon=True
        )
        thread.start()

    def _extract_worker(self, zip_file: str, dest_dir: str):
        """Progressive extraction: extract each file, then truncate the ZIP to free space."""
        seven_zip = self._find_7zip()

        try:
            # Read the central directory (metadata only — no decompression)
            with zipfile.ZipFile(zip_file, "r") as zf:
                all_entries = sorted(zf.infolist(), key=lambda e: e.header_offset)

            # Count only actual files (not directory entries)
            file_entries = [e for e in all_entries if not e.is_dir()]
            total_files = len(file_entries)

            if total_files == 0:
                os.remove(zip_file)
                self.root.after(0, self._extraction_done, True, "")
                return

            original_zip_size = os.path.getsize(zip_file)
            total_compressed_bytes = sum(e.compress_size for e in file_entries)
            bytes_processed = 0
            total_freed = 0
            processed = 0

            # Process file entries from BACK to FRONT (highest offset first).
            # This lets us truncate the archive from the end — no rewriting needed.
            for file_entry in reversed(file_entries):
                processed += 1
                filename = file_entry.filename
                short = filename if len(filename) <= 48 else "…" + filename[-45:]

                self.root.after(
                    0, self._update_progress,
                    int(bytes_processed / total_compressed_bytes * 100) if total_compressed_bytes else 0,
                    processed, total_files, short,
                )

                # ── 1) Extract this single file ──────────────────
                self._extract_single_file(zip_file, dest_dir, file_entry, seven_zip)

                # ── 2) Truncate the ZIP to remove this file's data ─
                remaining = [e for e in all_entries if e.header_offset < file_entry.header_offset]
                truncate_zip(zip_file, remaining, file_entry.header_offset)
                all_entries = remaining

                # Track progress by bytes
                bytes_processed += file_entry.compress_size
                new_size = os.path.getsize(zip_file)
                total_freed = original_zip_size - new_size
                self.root.after(
                    0, self._update_progress,
                    int(bytes_processed / total_compressed_bytes * 100) if total_compressed_bytes else 100,
                    processed, total_files, short,
                )
                self.root.after(
                    0, self._update_freed, total_freed,
                )

            # ── 3) Delete the remaining (now empty) ZIP shell ─
            self.root.after(0, self._set_status, "Raderar kvarvarande ZIP…", COLORS["warning"])
            if os.path.exists(zip_file):
                os.remove(zip_file)

            self.root.after(0, self._extraction_done, True, "")

        except Exception as exc:
            self.root.after(0, self._extraction_done, False, str(exc))

    def _extract_single_file(
        self,
        zip_file: str,
        dest_dir: str,
        entry: zipfile.ZipInfo,
        seven_zip: str | None,
    ):
        """Extract one file from the archive, falling back to 7-Zip if needed."""
        try:
            with zipfile.ZipFile(zip_file, "r") as zf:
                zf.extract(entry, dest_dir)
        except NotImplementedError:
            # Unsupported compression (e.g. Deflate64) — fall back to 7-Zip
            if not seven_zip:
                raise RuntimeError(
                    f"Komprimeringen '{entry.compress_type}' stöds ej av Python.\n"
                    "Installera 7-Zip för att hantera dessa filer: https://7-zip.org"
                )
            result = subprocess.run(
                [seven_zip, "x", f"-o{dest_dir}", "-y", zip_file, entry.filename],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                error = result.stderr.strip() or result.stdout.strip() or "Okänt fel"
                raise RuntimeError(f"7-Zip kunde inte extrahera '{entry.filename}':\n{error}")

    # ── UI update helpers ─────────────────────
    def _update_progress(self, pct: int, current: int, total: int, filename: str):
        self.progress["value"] = pct
        self._set_status(f"Extraherar ({current}/{total}): {filename}", COLORS["text"])

    def _update_freed(self, freed_bytes: int):
        self.freed_var.set(f"💾 Frigjort: {format_size(freed_bytes)}")

    def _extraction_done(self, success: bool, error_msg: str):
        self.is_running = False
        self.extract_btn.configure(state="normal")

        if success:
            self.progress["value"] = 100
            self._set_status(
                "✅  Klart! Alla filer extraherade — ZIP raderad.",
                COLORS["success"],
            )
            self.zip_path.set("")
        else:
            self.progress["value"] = 0
            self._set_status(f"❌  Fel: {error_msg}", COLORS["error"])
            messagebox.showerror(
                "Extrahering misslyckades",
                f"Ett fel uppstod:\n{error_msg}\n\n"
                f"Redan extraherade filer finns kvar i destinationen.\n"
                f"ZIP-filen kan vara delvis trunkerad.",
            )

    def _set_status(self, text: str, color: str):
        self.status_var.set(text)
        self.status_label.configure(foreground=color)


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    app = CleanUnzipperApp(root)
    root.mainloop()
