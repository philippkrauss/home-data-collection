"""
export_photos_library.py

Exports photos and videos from a macOS .photoslibrary to a clean
date-based folder structure: OUTPUT_DIR/YYYY/MM/filename.ext

Features:
- Reads EXIF date from JPEG/HEIC/TIFF; falls back to file modification date
- Skips duplicates (SHA-256 hash check)
- Optional HEIC -> JPEG conversion (requires pillow-heif)
- Dry-run mode (--dry-run) to preview without copying
- Summary report at the end

Usage:
    python export_photos_library.py "C:\\Path\\To\\Photos Library.photoslibrary" "D:\\Photos Archive"
    python export_photos_library.py "..." "..." --dry-run
    python export_photos_library.py "..." "..." --convert-heic

Requirements:
    pip install pillow
    pip install pillow-heif   # only needed for --convert-heic
"""

import os
import sys
import shutil
import hashlib
import argparse
import struct
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional imports
# ---------------------------------------------------------------------------
try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".tiff", ".tif", ".png",
                    ".bmp", ".gif", ".webp", ".dng", ".raw", ".arw", ".cr2", ".nef"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".3gp"}
ALL_EXTENSIONS   = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def exif_date(path: Path) -> datetime | None:
    """Try to read DateTimeOriginal from EXIF (JPEG / HEIC via pillow[-heif])."""
    if not PILLOW_AVAILABLE:
        return None
    try:
        img = Image.open(path)
        exif_data = img._getexif() if hasattr(img, "_getexif") else None
        if not exif_data:
            # Pillow >= 10 unified API
            info = img.getexif()
            exif_data = {TAGS.get(k, k): v for k, v in info.items()}
        else:
            exif_data = {TAGS.get(k, k): v for k, v in exif_data.items()}
        for tag in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
            raw = exif_data.get(tag)
            if raw:
                return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def mp4_creation_date(path: Path) -> datetime | None:
    """Parse creation_time from MP4/MOV moov box (no external deps)."""
    try:
        with open(path, "rb") as f:
            data = f.read(min(1 << 20, os.path.getsize(path)))  # first 1 MB
        i = 0
        while i < len(data) - 8:
            size = struct.unpack(">I", data[i:i+4])[0]
            box  = data[i+4:i+8]
            if box in (b"moov", b"mvhd"):
                if box == b"mvhd":
                    version = data[i+8]
                    if version == 1:
                        ts = struct.unpack(">Q", data[i+12:i+20])[0]
                    else:
                        ts = struct.unpack(">I", data[i+12:i+16])[0]
                    # Mac epoch: seconds since 1904-01-01
                    epoch_diff = (datetime(1970, 1, 1) - datetime(1904, 1, 1)).total_seconds()
                    unix_ts = ts - epoch_diff
                    return datetime.utcfromtimestamp(unix_ts)
            if size < 8:
                break
            i += size
    except Exception:
        pass
    return None


def best_date(path: Path) -> datetime:
    """Return the best available date for a media file."""
    ext = path.suffix.lower()
    dt = None
    if ext in PHOTO_EXTENSIONS:
        dt = exif_date(path)
    elif ext in VIDEO_EXTENSIONS:
        dt = mp4_creation_date(path)
    if dt is None:
        dt = datetime.fromtimestamp(path.stat().st_mtime)
    return dt


def unique_dest(dest: Path) -> Path:
    """If dest already exists (different file), append _2, _3, …"""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    counter = 2
    while True:
        candidate = dest.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def collect_originals(library_path: Path) -> list[Path]:
    originals_dir = library_path / "originals"
    if not originals_dir.is_dir():
        print(f"[ERROR] 'originals' folder not found inside library: {originals_dir}")
        sys.exit(1)
    files = [
        p for p in originals_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in ALL_EXTENSIONS
    ]
    return files


def export(
    library_path: Path,
    output_dir: Path,
    dry_run: bool = False,
    convert_heic: bool = False,
) -> None:
    files = collect_originals(library_path)
    total = len(files)
    print(f"\nFound {total} media files in originals/\n")

    seen_hashes: set[str] = set()
    stats = {"copied": 0, "skipped_duplicate": 0, "skipped_existing": 0,
             "converted": 0, "errors": 0}

    for i, src in enumerate(files, 1):
        try:
            # --- duplicate check ---
            digest = file_sha256(src)
            if digest in seen_hashes:
                stats["skipped_duplicate"] += 1
                print(f"[{i}/{total}] DUPLICATE  {src.name}")
                continue
            seen_hashes.add(digest)

            # --- determine output path ---
            dt  = best_date(src)
            ext = src.suffix.lower()

            if convert_heic and ext in {".heic", ".heif"} and HEIF_AVAILABLE:
                dest_name = src.stem + ".jpg"
                do_convert = True
            else:
                dest_name = src.name
                do_convert = False

            dest_dir  = output_dir / f"{dt.year:04d}" / f"{dt.month:02d}"
            dest_path = unique_dest(dest_dir / dest_name)

            # --- already present with same hash? ---
            if dest_path.exists() and file_sha256(dest_path) == digest:
                stats["skipped_existing"] += 1
                print(f"[{i}/{total}] EXISTS     {dest_path.relative_to(output_dir)}")
                continue

            print(f"[{i}/{total}] {'CONVERT' if do_convert else 'COPY':7s}  "
                  f"{src.name}  ->  {dest_path.relative_to(output_dir)}")

            if not dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                if do_convert:
                    img = Image.open(src)
                    img.save(dest_path, "JPEG", quality=95)
                    stats["converted"] += 1
                else:
                    shutil.move(src, dest_path)
                    stats["copied"] += 1

        except Exception as e:
            stats["errors"] += 1
            print(f"[{i}/{total}] ERROR      {src.name}: {e}")

    # --- summary ---
    print("\n" + "=" * 60)
    print("EXPORT SUMMARY" + (" (DRY RUN)" if dry_run else ""))
    print("=" * 60)
    print(f"  Total files found : {total}")
    print(f"  Copied            : {stats['copied']}")
    print(f"  Converted (HEIC)  : {stats['converted']}")
    print(f"  Skipped (duplicate): {stats['skipped_duplicate']}")
    print(f"  Skipped (existing) : {stats['skipped_existing']}")
    print(f"  Errors            : {stats['errors']}")
    print(f"  Output directory  : {output_dir}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Export macOS Photos Library to a date-based folder structure."
    )
    parser.add_argument("library", help="Path to the .photoslibrary folder")
    parser.add_argument("output",  help="Destination directory for the export")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Preview actions without copying any files")
    parser.add_argument("--convert-heic", action="store_true",
                        help="Convert HEIC files to JPEG (requires pillow-heif)")
    args = parser.parse_args()

    library_path = Path(args.library)
    output_dir   = Path(args.output)

    if not library_path.exists():
        print(f"[ERROR] Library not found: {library_path}")
        sys.exit(1)

    if args.convert_heic and not HEIF_AVAILABLE:
        print("[WARNING] --convert-heic requested but pillow-heif is not installed.")
        print("          Install it with:  pip install pillow-heif")
        print("          Continuing without conversion.\n")

    if not PILLOW_AVAILABLE:
        print("[WARNING] Pillow not installed. EXIF dates unavailable; "
              "falling back to file modification date.")
        print("          Install with:  pip install pillow\n")

    print(f"Library : {library_path}")
    print(f"Output  : {output_dir}")
    if args.dry_run:
        print("Mode    : DRY RUN (no files will be written)")

    export(library_path, output_dir, dry_run=args.dry_run, convert_heic=args.convert_heic)


if __name__ == "__main__":
    main()