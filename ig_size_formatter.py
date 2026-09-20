#!/usr/bin/env python3
"""Instagram Image Formatter — Standalone GUI

Self-contained single-file Tkinter application for batch-formatting images
to Instagram-compatible aspect ratios with configurable borders, live preview,
and per-image control.

=============================================================================
Architecture & Workspace Design Standards:
-----------------------------------------------------------------------------
1. Standalone Single-File Variant:
   - Self-contained script bundling all required logic (scanner, processor,
     manifest tracker, preview renderer, settings manager, and Tkinter UI).
   - Designed for zero-configuration portability: can run directly without
     package installation or as a standalone frozen PyInstaller executable.

2. Prerequisite Enforcement:
   - Evaluates third-party imaging dependencies (Pillow) at startup.
   - For standard script execution (.py), offers interactive 1-click pip
     installation if Pillow is absent.
   - For frozen bundles (sys.frozen), blocks runtime pip calls and displays
     a descriptive error dialog per packaging rules.

3. Path & File Handling:
   - Exclusively uses standard library `os.path` functions and plain strings
     for all file and directory manipulations. Avoids pathlib to guarantee
     maximum portability and freeze compatibility.

4. Thread Safety & UI Non-Blocking Execution:
   - Background tasks (folder scanning and batch image exporting) run on
     dedicated daemon threads.
   - Background threads NEVER interact directly with Tkinter widgets. All
     progress events, item completion notifications, and errors are posted
     to thread-safe `queue.Queue` instances and polled on the main UI thread
     via debounced `root.after()` timers.
=============================================================================
"""

from collections import namedtuple
from dataclasses import asdict, dataclass, fields
import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Any, Optional, Union


def ensure_prerequisites() -> str:
    """Verify required third-party libraries (Pillow) are available, or prompt to install.

    This function performs a pre-flight inspection before any imaging modules
    are imported. If PIL/Pillow is missing:
    - If running under PyInstaller (sys.frozen), alerts the user and halts
      (since pip cannot modify a frozen binary).
    - If running via standard Python interpreter, presents an interactive
      confirmation dialog offering to install Pillow via `sys.executable -m pip`.

    Returns:
        Status string describing the verified prerequisite and version (e.g. 'Pillow 12.3.0').
    """
    try:
        import PIL  # noqa: F401
        version = getattr(PIL, "__version__", "")
        return f"Pillow {version}".strip()
    except ImportError:
        pass

    is_frozen = getattr(sys, "frozen", False)
    if is_frozen:
        # Cannot run pip inside a compiled frozen executable
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Missing Prerequisite",
            "The Pillow library is required for image processing, but is missing from this executable bundle.",
            parent=root,
        )
        root.destroy()
        sys.exit(1)

    # Running from Python interpreter (.py): prompt user to auto-install
    root = tk.Tk()
    root.withdraw()
    confirm = messagebox.askyesno(
        "Missing Prerequisite: Pillow",
        "The required image library 'Pillow' is not installed in this Python environment.\n\n"
        "Would you like to install it automatically now via pip?",
        parent=root,
    )
    if not confirm:
        messagebox.showinfo(
            "Installation Skipped",
            "Please run 'pip install Pillow' in your terminal and restart the application.",
            parent=root,
        )
        root.destroy()
        sys.exit(1)

    try:
        print("Installing Pillow via pip... Please wait...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow"])
        messagebox.showinfo(
            "Installation Complete",
            "Pillow was installed successfully! Launching application...",
            parent=root,
        )
        root.destroy()
        import PIL
        version = getattr(PIL, "__version__", "")
        return f"Pillow {version} (installed)".strip()
    except Exception as exc:
        messagebox.showerror(
            "Installation Failed",
            f"Failed to install Pillow automatically:\n\n{exc}\n\n"
            "Please run 'pip install Pillow' manually in your terminal.",
            parent=root,
        )
        root.destroy()
        sys.exit(1)


# Run prerequisite check before importing Pillow modules
PREREQUISITES_STATUS: str = ensure_prerequisites()

from PIL import Image, ImageTk


# ===========================================================================
# 1. Aspect Ratio Definitions & Formats (formerly formats.py)
# ===========================================================================

# Standard Instagram aspect ratios: display label -> float width/height ratio
IG_RATIOS: dict[str, float] = {
    "1:1": 1.0,
    "4:5": 0.8,
    "5:4": 1.25,
    "9:16": 0.5625,
    "16:9": 1.7778,
}

# Target ratio options available in the GUI
TARGET_OPTIONS: list[str] = [
    "Auto (5:4 / 4:5)",
    "1:1",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
]
DEFAULT_TARGET: str = "Auto (5:4 / 4:5)"

# Image extensions supported for scanning and processing
SUPPORTED_EXTENSIONS: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".tiff",
    ".tif",
    ".bmp",
    ".webp",
)

# Format groups available for folder scanning: display_name -> tuple of lowercase extensions
SCAN_FORMAT_GROUPS: dict[str, tuple[str, ...]] = {
    "JPG / JPEG": (".jpg", ".jpeg"),
    "PNG": (".png",),
    "TIFF": (".tiff", ".tif"),
    "WEBP": (".webp",),
    "BMP": (".bmp",),
}

# Output format configuration: display_label -> (pillow_format, supports_quality_slider)
OUTPUT_FORMATS: dict[str, tuple[str, bool]] = {
    "JPEG": ("JPEG", True),
    "PNG": ("PNG", False),
    "TIFF": ("TIFF", False),
    "BMP": ("BMP", False),
    "WEBP": ("WEBP", True),
}

# Default tolerance for classifying an aspect ratio against standard IG targets
RATIO_TOLERANCE: float = 0.02


def get_orientation(w: int, h: int) -> str:
    """Return image orientation as 'Landscape', 'Portrait', or 'Square'."""
    if w <= 0 or h <= 0 or w == h:
        return "Square"
    return "Landscape" if w > h else "Portrait"


def resolve_target_ratio(target_label: str, w: int, h: int) -> float:
    """Resolve a target ratio label into a float width/height ratio.

    Supports 'Auto (5:4 / 4:5)' by selecting 5:4 for landscape, 4:5 for portrait,
    and 1:1 for square images.

    Args:
        target_label: Label string (e.g. 'Auto (5:4 / 4:5)', '4:5', '16:9').
        w: Image width in pixels.
        h: Image height in pixels.

    Returns:
        Float aspect ratio (width / height).
    """
    clean = target_label.strip() if target_label else ""
    if clean == "Auto (5:4 / 4:5)" or clean.startswith("Auto"):
        if w > h:
            return IG_RATIOS["5:4"]
        elif h > w:
            return IG_RATIOS["4:5"]
        else:
            return IG_RATIOS["1:1"]

    if clean in IG_RATIOS:
        return float(IG_RATIOS[clean])

    first_token = clean.split()[0] if clean else ""
    if first_token in IG_RATIOS:
        return float(IG_RATIOS[first_token])

    for k, v in IG_RATIOS.items():
        if clean.startswith(k):
            return float(v)

    return float(clean)



def classify_ratio(w: int, h: int, tolerance: float = RATIO_TOLERANCE) -> Optional[str]:
    """Return matching Instagram ratio label if (w / h) is within tolerance, else None.

    Args:
        w: Image width in pixels.
        h: Image height in pixels.
        tolerance: Allowed difference between current ratio and standard ratio.

    Returns:
        The matched ratio label (e.g. '4:5', '1:1') or None if not matching any target.
    """
    if w <= 0 or h <= 0:
        return None

    current_ratio = w / h
    best_match: Optional[str] = None
    min_diff = float("inf")

    for label, target_ratio in IG_RATIOS.items():
        diff = abs(current_ratio - target_ratio)
        if diff <= tolerance and diff < min_diff:
            min_diff = diff
            best_match = label

    return best_match


def suggest_target_ratio(w: int, h: int) -> str:
    """Auto-suggest the best Instagram ratio based on image dimensions and orientation.

    - If w > h: suggest closest landscape/square ratio among ('5:4', '16:9', '1:1')
    - If h > w: suggest closest portrait ratio among ('4:5', '9:16', '1:1')
    - If w == h: suggest '1:1'

    The closest ratio is determined by minimizing absolute difference to current ratio.

    Args:
        w: Image width in pixels.
        h: Image height in pixels.

    Returns:
        Recommended ratio string key from IG_RATIOS.
    """
    if w <= 0 or h <= 0 or w == h:
        return "1:1"

    current_ratio = w / h

    if w > h:
        candidates = ("5:4", "16:9", "1:1")
        return min(candidates, key=lambda c: abs(current_ratio - IG_RATIOS[c]))
    else:
        current_inv = h / w
        candidates = ("4:5", "9:16", "1:1")
        return min(candidates, key=lambda c: abs(current_inv - (1.0 / IG_RATIOS[c])))

def get_effective_image_info(
    info: Any, rotation: int = 0
) -> tuple[int, int, float, str, Optional[str], bool]:
    """Calculate effective (width, height, ratio, orientation, current_ig_format, needs_processing)
    after applying rotation in degrees clockwise (0, 90, 180, 270).
    """
    rot = rotation % 360
    if rot in (90, 270):
        eff_w, eff_h = info.height, info.width
    else:
        eff_w, eff_h = info.width, info.height

    eff_ratio = eff_w / eff_h if eff_h > 0 else 1.0
    eff_orientation = get_orientation(eff_w, eff_h)
    eff_current_ig = classify_ratio(eff_w, eff_h)
    eff_needs = eff_current_ig is None
    return eff_w, eff_h, eff_ratio, eff_orientation, eff_current_ig, eff_needs


def is_supported_extension(path_or_ext: str) -> bool:
    """Check if a file path or file extension is supported.

    Args:
        path_or_ext: File path (e.g. 'photo.jpg') or extension string (e.g. '.jpg', 'jpg').

    Returns:
        True if the extension is in SUPPORTED_EXTENSIONS, False otherwise.
    """
    clean = path_or_ext.strip()
    if clean.startswith("."):
        ext = clean.lower()
    else:
        basename = os.path.basename(clean)
        _, ext = os.path.splitext(basename)
        if not ext:
            ext = f".{clean.lower()}"
        else:
            ext = ext.lower()
    return ext in SUPPORTED_EXTENSIONS



# ===========================================================================
# 2. Directory Scanner & Manifest Tracking (formerly scanner.py)
# ===========================================================================
#
# Manifest & Processing History Architecture:
# -------------------------------------------
# To support persistent status tracking across sessions, the application
# maintains a hidden manifest file (`.ig_manifest.json`) in each scanned
# directory where images are discovered or processed.
#
# Key Characteristics:
# 1. Non-Intrusive & Isolated:
#    Each folder maintains its own standalone manifest recording the latest
#    export recipe and up to 50 historical exports per photo.
# 2. Windows Hidden Attribute Handling:
#    On Windows, files are assigned the FILE_ATTRIBUTE_HIDDEN (0x02) flag.
#    Crucially, Windows C runtime `open(filepath, "w")` will raise
#    [Errno 13] Permission Denied if targeting an existing hidden file.
#    Therefore, `save_folder_manifest` temporarily resets the attribute to
#    FILE_ATTRIBUTE_NORMAL (0x80) prior to opening, writes the JSON payload,
#    and re-applies the hidden attribute immediately upon completion.
# 3. 1-Click History Reset:
#    Deleting `.ig_manifest.json` from a folder safely and completely resets
#    all processing history for that folder back to default state.
# 4. Optional No-Manifest Bypass:
#    When the "No Manifest" setting is enabled, both reading from and writing
#    to `.ig_manifest.json` are completely bypassed.
# ===========================================================================

MANIFEST_FILENAME: str = ".ig_manifest.json"


def set_windows_hidden(filepath: str) -> None:
    """Set the Windows hidden file attribute (0x02) on a file if running on Windows.

    Args:
        filepath: Path to the target file.
    """
    if sys.platform == "win32" and os.path.exists(filepath):
        try:
            import ctypes
            # Win32 API: SetFileAttributesW(LPCWSTR lpFileName, DWORD dwFileAttributes)
            # FILE_ATTRIBUTE_HIDDEN = 0x02
            ctypes.windll.kernel32.SetFileAttributesW(str(filepath), 0x02)
        except Exception:
            pass


def load_folder_manifest(folder_path: str) -> dict[str, Any]:
    """Load the .ig_manifest.json file from a directory if it exists.

    If the manifest does not exist or fails to parse, returns a clean default
    manifest structure with schema version 1 and an empty photos dictionary.

    Args:
        folder_path: Directory to inspect for .ig_manifest.json.

    Returns:
        Dict representing manifest contents: {"version": 1, "photos": {...}}.
    """
    manifest_path = os.path.join(folder_path, MANIFEST_FILENAME)
    if not os.path.isfile(manifest_path):
        return {"version": 1, "photos": {}}
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "photos" in data:
                return data
            return {"version": 1, "photos": {}}
    except Exception as exc:
        print(f"Warning: Could not read manifest '{manifest_path}': {exc}", file=sys.stderr)
        return {"version": 1, "photos": {}}


def save_folder_manifest(folder_path: str, manifest_data: dict[str, Any]) -> None:
    """Save the .ig_manifest.json file in a directory and set hidden attribute on Windows.

    Windows File Attribute Safety:
    If `.ig_manifest.json` already has the Windows hidden attribute set, Python's
    built-in `open(..., 'w')` raises `[Errno 13] Permission denied`. To prevent
    this, we temporarily unhide the file by resetting attributes to NORMAL (0x80),
    write the new JSON contents, and then re-apply the HIDDEN (0x02) attribute.

    Args:
        folder_path: Directory path where manifest will be saved.
        manifest_data: Dictionary payload to serialize.
    """
    manifest_path = os.path.join(folder_path, MANIFEST_FILENAME)
    if sys.platform == "win32" and os.path.exists(manifest_path):
        try:
            import ctypes
            # Temporarily reset to FILE_ATTRIBUTE_NORMAL (0x80) before opening
            ctypes.windll.kernel32.SetFileAttributesW(str(manifest_path), 0x80)
        except Exception:
            pass
    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)
        set_windows_hidden(manifest_path)
    except Exception as exc:
        print(f"Warning: Could not write manifest '{manifest_path}': {exc}", file=sys.stderr)


def record_photo_export(
    source_filepath: str,
    output_filepath: str,
    recipe: dict[str, Any],
) -> None:
    """Record an export event in the source photo directory's .ig_manifest.json file.

    Updates the photo's 'latest' export recipe and appends the recipe to the
    photo's historical log (bounded to the last 50 entries).

    Args:
        source_filepath: Path to the original input photo.
        output_filepath: Path where the exported photo was written.
        recipe: Dictionary containing parameters used for the transformation.
    """
    parent_dir = os.path.dirname(os.path.abspath(source_filepath))
    filename = os.path.basename(source_filepath)
    manifest = load_folder_manifest(parent_dir)

    photos = manifest.setdefault("photos", {})
    photo_entry = photos.setdefault(filename, {"latest": {}, "history": []})

    # Prepare export record
    export_entry = dict(recipe)
    export_entry["output_file"] = os.path.basename(output_filepath)
    export_entry["output_path"] = os.path.abspath(output_filepath)

    photo_entry["latest"] = export_entry
    history_list = photo_entry.setdefault("history", [])
    history_list.append(export_entry)
    # Maintain a bounded history of up to 50 previous exports
    if len(history_list) > 50:
        photo_entry["history"] = history_list[-50:]

    save_folder_manifest(parent_dir, manifest)


ImageInfo = namedtuple(
    "ImageInfo",
    [
        "filepath",
        "filename",
        "width",
        "height",
        "ratio",
        "current_ig_format",
        "orientation",
        "needs_processing",
        "subfolder",
        "processed_status",
        "latest_export",
    ],
    defaults=["", None],
)
ImageInfo.__doc__ = (
    "Metadata about an image file scanned for Instagram formatting.\n\n"
    "Fields:\n"
    "    filepath (str): Full absolute path to the image file.\n"
    "    filename (str): Basename of the image file.\n"
    "    width (int): Image width in pixels.\n"
    "    height (int): Image height in pixels.\n"
    "    ratio (float): Aspect ratio (width / height).\n"
    "    current_ig_format (str or None): Label like '4:5' if already at an IG ratio, else None.\n"
    "    orientation (str): Image orientation ('Landscape', 'Portrait', or 'Square').\n"
    "    needs_processing (bool): True if current_ig_format is None, False otherwise.\n"
    "    subfolder (str): Relative subfolder path from the root scan directory, or '' for top-level.\n"
    "    processed_status (str): 'processed' (output exists), 'missing_output' (output missing), or ''.\n"
    "    latest_export (dict or None): Recipe dictionary of the latest export from manifest.\n"
)


def scan_folder(
    directory: str,
    recurse: bool = False,
    extensions: tuple = SUPPORTED_EXTENSIONS,
    suffix: str = "_ig",
    progress_callback: Optional[Any] = None,
    no_manifest: bool = False,
) -> list[ImageInfo]:
    """Scan a directory for image files and return metadata for each image.

    Traverses target folder (flat or recursive), reading image header dimensions
    rapidly via Pillow `Image.open().size` without decompressing full image pixel
    rasters. Inspects local `.ig_manifest.json` files for persistent export status
    and recipe history (unless bypassed by `no_manifest=True`).

    Args:
        directory: Root directory path to scan.
        recurse: If True, recursively scan subdirectories using os.walk.
            If False, only scan files directly in the top-level directory.
        extensions: Tuple of supported file extensions (case-insensitive).
        suffix: Suffix at the end of the filename (without extension) to skip
            (case-insensitive), typically identifying already processed files.
        progress_callback: Optional callback receiving (found_count, needs_count).
        no_manifest: If True, skips reading or evaluating .ig_manifest.json.

    Returns:
        List of ImageInfo namedtuples, sorted alphabetically by filepath.
    """
    abs_directory = os.path.abspath(directory)
    if not os.path.exists(abs_directory) or not os.path.isdir(abs_directory):
        print(
            f"Directory not found or is not a directory: {directory}",
            file=sys.stderr,
        )
        return []

    # Normalize configured extensions to lowercase with leading dots for O(1) matching
    norm_extensions = tuple(
        ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}"
        for ext in extensions
    )
    norm_suffix = suffix.lower() if suffix else ""

    candidate_files: list[str] = []
    try:
        if recurse:
            # Recursive traversal: gather all files across directory tree
            for root, _dirs, files in os.walk(abs_directory):
                for filename in files:
                    candidate_files.append(os.path.join(root, filename))
        else:
            # Non-recursive traversal: inspect only files directly in root directory
            for entry in os.listdir(abs_directory):
                entry_path = os.path.join(abs_directory, entry)
                if os.path.isfile(entry_path):
                    candidate_files.append(entry_path)
    except OSError as err:
        print(f"Error accessing directory '{abs_directory}': {err}", file=sys.stderr)
        return []

    results: list[ImageInfo] = []
    scanned_count = 0
    needs_count = 0
    # Directory-level manifest cache to prevent redundant disk I/O when many files share a folder
    manifest_cache: dict[str, dict[str, Any]] = {}

    for filepath in candidate_files:
        scanned_count += 1
        filename = os.path.basename(filepath)
        stem, ext = os.path.splitext(filename)

        # Skip manifest files themselves
        if filename == MANIFEST_FILENAME:
            continue

        # Step 1: Filter by file extension against normalized user selection
        if ext.lower() not in norm_extensions:
            if progress_callback is not None and scanned_count % 20 == 0:
                progress_callback(len(results), needs_count)
            continue

        # Step 2: Skip output files matching the configured export suffix (e.g. '_ig')
        if norm_suffix and stem.lower().endswith(norm_suffix):
            if progress_callback is not None and scanned_count % 20 == 0:
                progress_callback(len(results), needs_count)
            continue

        # Step 3: Fast header inspection — Pillow reads EXIF/JPEG headers without full raster decoding
        try:
            with Image.open(filepath) as img:
                width, height = img.size
        except Exception as exc:
            print(f"Could not open image '{filepath}': {exc}", file=sys.stderr)
            if progress_callback is not None and scanned_count % 10 == 0:
                progress_callback(len(results), needs_count)
            continue

        if width <= 0 or height <= 0:
            print(
                f"Invalid dimensions ({width}x{height}) for '{filepath}'",
                file=sys.stderr,
            )
            continue

        # Step 4: Calculate mathematical aspect ratio (w / h)
        ratio = width / height

        # Step 5: Check if image matches standard Instagram aspect ratio within tolerance (0.02)
        current_ig_format = classify_ratio(width, height)

        # Step 6: Determine orientation category ('Landscape', 'Portrait', 'Square')
        orientation = get_orientation(width, height)

        # Step 7: Calculate relative subfolder path from root scan directory
        abs_filepath = os.path.abspath(filepath)
        parent_dir = os.path.dirname(abs_filepath)
        rel_subfolder = os.path.relpath(parent_dir, abs_directory)
        subfolder = "" if rel_subfolder == "." else rel_subfolder

        # Step 8: Manifest inspection (unless bypassed by no_manifest)
        processed_status = ""
        latest_export = None

        if not no_manifest:
            if parent_dir not in manifest_cache:
                manifest_cache[parent_dir] = load_folder_manifest(parent_dir)
            manifest = manifest_cache[parent_dir]

            photos = manifest.get("photos", {})
            if filename in photos:
                entry = photos[filename]
                latest = entry.get("latest", {})
                # Photos recorded in the manifest are marked as processed;
                # disk output checks are evaluated on-demand in the History dropdown
                processed_status = "processed"
                latest_export = latest

        # Step 9: Needs processing logic: true if aspect ratio non-standard and not already exported
        needs_processing = current_ig_format is None
        if needs_processing and processed_status != "processed":
            needs_count += 1

        results.append(
            ImageInfo(
                filepath=abs_filepath,
                filename=filename,
                width=width,
                height=height,
                ratio=ratio,
                current_ig_format=current_ig_format,
                orientation=orientation,
                needs_processing=needs_processing,
                subfolder=subfolder,
                processed_status=processed_status,
                latest_export=latest_export,
            )
        )

        if progress_callback is not None:
            progress_callback(len(results), needs_count)

    if progress_callback is not None:
        progress_callback(len(results), needs_count)

    # Return a list of ImageInfo, sorted by filepath
    results.sort(key=lambda info: info.filepath)
    return results



# ===========================================================================
# 3. Image Processor (formerly processor.py)
# ===========================================================================

# Format extension mapping
_FORMAT_EXTENSIONS: dict[str, str] = {
    "JPEG": ".jpg",
    "JPG": ".jpg",
    "PNG": ".png",
    "TIFF": ".tiff",
    "TIF": ".tiff",
    "BMP": ".bmp",
    "WEBP": ".webp",
}


def _calculate_bars_size(width: int, height: int, target_ratio: float) -> tuple[int, int]:
    """Calculate canvas dimensions to pad an image of (width, height) to target_ratio.

    Args:
        width: Current image width in pixels.
        height: Current image height in pixels.
        target_ratio: Target aspect ratio (width / height).

    Returns:
        (canvas_w, canvas_h) in pixels.
    """
    current_ratio = width / height
    if current_ratio > target_ratio:
        # Image is wider than target ratio; add vertical bars (top/bottom)
        canvas_w = width
        canvas_h = round(width / target_ratio)
    else:
        # Image is taller than or equal to target ratio; add horizontal bars (left/right)
        canvas_h = height
        canvas_w = round(height * target_ratio)

    return max(canvas_w, width), max(canvas_h, height)


def pad_bars(
    img: Image.Image,
    target_ratio: float,
    fill: Union[str, tuple[int, ...]] = "white",
) -> Image.Image:
    """Pad an image with bars (top/bottom or left/right) to reach the target aspect ratio.

    If current ratio > target_ratio: image is wider than target, vertical bars
    (top/bottom) are added:
        new_w = img.width, new_h = round(img.width / target_ratio)
    Else: horizontal bars (left/right) are added:
        new_h = img.height, new_w = round(img.height * target_ratio)

    Creates a new RGB canvas with `fill` color and pastes the original image centered.

    Args:
        img: The source PIL Image.
        target_ratio: Desired aspect ratio (width / height).
        fill: Canvas fill color (name string, hex string, or RGB tuple).

    Returns:
        New RGB PIL Image with the original image centered and padded to target_ratio.
    """
    if target_ratio <= 0:
        raise ValueError(f"Target aspect ratio must be positive, got {target_ratio}")

    new_w, new_h = _calculate_bars_size(img.width, img.height, target_ratio)

    canvas = Image.new("RGB", (new_w, new_h), fill)
    offset_x = (new_w - img.width) // 2
    offset_y = (new_h - img.height) // 2

    img_rgb = img if img.mode == "RGB" else img.convert("RGB")
    canvas.paste(img_rgb, (offset_x, offset_y))
    return canvas


def pad_full_border(
    img: Image.Image,
    target_ratio: float,
    border_pct: float = 5.0,
    fill: Union[str, tuple[int, ...]] = "white",
) -> Image.Image:
    """Add a uniform border around all 4 sides, then pad with bars to reach target ratio.

    Step 1: Add uniform border (border_pct % of min(w, h)) around all 4 sides:
        border_px = max(1, round(min(img.width, img.height) * border_pct / 100))
        bordered_w = img.width + 2 * border_px
        bordered_h = img.height + 2 * border_px
        Create canvas at (bordered_w, bordered_h), paste image centered.
    Step 2: Apply pad_bars on the bordered image to reach the target ratio.

    Args:
        img: The source PIL Image.
        target_ratio: Desired aspect ratio (width / height).
        border_pct: Border thickness percentage based on min(width, height).
        fill: Canvas fill color (name string, hex string, or RGB tuple).

    Returns:
        New RGB PIL Image with uniform border and aspect-ratio padding bars.
    """
    pct = max(0.0, float(border_pct))
    if pct > 0:
        border_px = max(1, round(min(img.width, img.height) * pct / 100))
    else:
        border_px = 0

    bordered_w = img.width + 2 * border_px
    bordered_h = img.height + 2 * border_px

    bordered_canvas = Image.new("RGB", (bordered_w, bordered_h), fill)
    offset_x = (bordered_w - img.width) // 2
    offset_y = (bordered_h - img.height) // 2

    img_rgb = img if img.mode == "RGB" else img.convert("RGB")
    bordered_canvas.paste(img_rgb, (offset_x, offset_y))

    return pad_bars(bordered_canvas, target_ratio, fill=fill)


def make_texture_background(
    width: int,
    height: int,
    texture_img: Image.Image,
    mode: str = "tile",
) -> Image.Image:
    """Create a texture-filled background canvas of size (width, height).

    Modes:
        - 'stretch': Resizes texture_img to exactly (width, height) using LANCZOS resampling.
        - 'tile': Creates blank RGB canvas and tiles texture_img across it.

    Args:
        width: Canvas width in pixels.
        height: Canvas height in pixels.
        texture_img: The PIL Image texture to apply.
        mode: Texture mode, either 'tile' or 'stretch' (defaults to 'tile').

    Returns:
        New RGB PIL Image filled with the texture pattern.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"Canvas dimensions must be positive, got ({width}, {height})")

    tex_rgb = texture_img if texture_img.mode == "RGB" else texture_img.convert("RGB")
    clean_mode = (mode or "tile").strip().lower()

    if clean_mode == "stretch":
        resample_filter = getattr(Image.Resampling, "LANCZOS", getattr(Image, "LANCZOS", 1))
        return tex_rgb.resize((width, height), resample=resample_filter)

    # Default to 'tile' mode
    bg = Image.new("RGB", (width, height))
    tw, th = tex_rgb.size
    if tw <= 0 or th <= 0:
        return bg

    for y in range(0, height, th):
        for x in range(0, width, tw):
            bg.paste(tex_rgb, (x, y))

    return bg


def apply_texture_fill(
    canvas: Image.Image,
    texture_img: Image.Image,
    mode: str = "tile",
) -> Image.Image:
    """Generate a texture background matching canvas dimensions.

    Args:
        canvas: Reference image providing target (width, height).
        texture_img: Texture PIL Image to fill or stretch.
        mode: Fill mode ('tile' or 'stretch').

    Returns:
        New RGB PIL Image with texture background.
    """
    return make_texture_background(canvas.width, canvas.height, texture_img, mode=mode)


def build_output_path(
    filepath: str,
    suffix: str = "_ig",
    output_ext: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> str:
    """Construct the output filepath by inserting suffix before the file extension.

    If output_ext is provided (e.g. '.png' or 'png'), use that extension
    instead of the original.
    If output_dir is provided and non-empty, use that directory instead of
    the original file's folder.

    Examples:
        photo.jpg with suffix '_ig' -> photo_ig.jpg
        photo.jpg with suffix '_ig' and output_ext='.png' -> photo_ig.png
        photo.jpg with suffix '_ig' and output_dir='/out' -> /out/photo_ig.jpg

    Args:
        filepath: Original file path.
        suffix: Suffix to insert before the extension (default: '_ig').
        output_ext: Optional new extension string.
        output_dir: Optional destination directory path.

    Returns:
        Modified file path string.
    """
    dirname, filename = os.path.split(filepath)
    name, orig_ext = os.path.splitext(filename)

    if output_ext is not None and output_ext.strip():
        clean_ext = output_ext.strip()
        ext = clean_ext if clean_ext.startswith(".") else f".{clean_ext}"
    else:
        ext = orig_ext

    new_filename = f"{name}{suffix}{ext}"
    target_dir = output_dir if (output_dir is not None and output_dir.strip()) else dirname
    if target_dir:
        return os.path.join(target_dir, new_filename)
    return new_filename


def process_image(
    filepath: str,
    target_ratio_label: str,
    border_mode: str = "bars",
    border_pct: float = 5.0,
    fill: Union[str, tuple[int, ...]] = "white",
    texture_path: str = "",
    texture_mode: str = "tile",
    output_format: str = "JPEG",
    quality: int = 100,
    suffix: str = "_ig",
    rotation: int = 0,
    output_dir: Optional[str] = None,
    overwrite: bool = False,
) -> tuple[str, bool, str]:
    """Execute the full image transformation and export pipeline.

    Steps:
        1. Look up target_ratio from IG_RATIOS[target_ratio_label].
        2. Look up (pillow_format, has_quality) from OUTPUT_FORMATS[output_format].
        3. Determine output extension from format (JPEG->.jpg, PNG->.png, TIFF->.tiff, BMP->.bmp, WEBP->.webp).
        4. Build output path. If file already exists and not overwrite, return (path, False, 'Output file already exists').
        5. Open image with Image.open(filepath).convert('RGB') and apply rotation if needed.
        6. Load texture if texture_path is provided and file exists.
        7. If border_mode == 'bars':
            - If texture: make_texture_background at target canvas size, paste photo centered.
            - Else: pad_bars(img, target_ratio, fill).
        8. If border_mode == 'full_border':
            - If texture: make bordered canvas with texture background.
            - Else: pad_full_border(img, target_ratio, border_pct, fill).
        9. Save with appropriate quality settings.
        10. Return (output_path, True, '').

    Args:
        filepath: Absolute path to the source image file.
        target_ratio_label: Target ratio key (e.g. '4:5', '1:1') or ratio string.
        border_mode: 'bars' (letterbox/pillarbox) or 'full_border' (uniform border + bars).
        border_pct: Uniform border percentage for 'full_border' mode.
        fill: Background fill color (name string, hex code, or RGB tuple).
        texture_path: Optional path to an image texture file.
        texture_mode: 'tile' or 'stretch' for texture background.
        output_format: Output format key from OUTPUT_FORMATS ('JPEG', 'PNG', etc.).
        quality: Save quality (1-100) for formats supporting quality.
        suffix: Suffix appended to output filename before extension.
        rotation: Degrees clockwise to rotate image (0, 90, 180, 270).
        output_dir: Optional destination directory path.
        overwrite: If True, overwrite existing output files instead of returning an error.

    Returns:
        Tuple of (output_path, success, error_msg).
    """
    output_path = filepath
    try:
        # 1. Open image with Image.open(filepath).convert('RGB')
        with Image.open(filepath) as raw_img:
            img = raw_img.convert("RGB")

        # Apply rotation if specified (degrees clockwise)
        if rotation % 360 != 0:
            img = img.rotate(-rotation % 360, expand=True)

        # 2. Resolve target_ratio (supports Auto, explicit keys, or floats)
        try:
            target_ratio = resolve_target_ratio(target_ratio_label, img.width, img.height)
        except (ValueError, TypeError, KeyError):
            return (
                filepath,
                False,
                f"Unknown aspect ratio label: '{target_ratio_label}'",
            )

        if target_ratio <= 0:
            return (filepath, False, f"Invalid target ratio: {target_ratio}")

        # 3. Look up (pillow_format, has_quality) from OUTPUT_FORMATS
        format_info: Optional[Union[tuple[str, bool], list[Any]]] = None
        if output_format in OUTPUT_FORMATS:
            format_info = OUTPUT_FORMATS[output_format]
        elif output_format.upper() in OUTPUT_FORMATS:
            format_info = OUTPUT_FORMATS[output_format.upper()]
        elif output_format.upper() == "JPG" and "JPEG" in OUTPUT_FORMATS:
            format_info = OUTPUT_FORMATS["JPEG"]

        if format_info is None:
            return (filepath, False, f"Unsupported output format: '{output_format}'")

        pillow_format = str(format_info[0])
        has_quality = bool(format_info[1])

        # 4. Determine output extension from format
        fmt_upper = pillow_format.upper()
        output_ext = _FORMAT_EXTENSIONS.get(fmt_upper, f".{fmt_upper.lower()}")

        # 5. Build output path; if already exists and not overwrite, return failure
        output_path = build_output_path(
            filepath, suffix=suffix, output_ext=output_ext, output_dir=output_dir
        )
        if not overwrite and os.path.exists(output_path):
            return (output_path, False, "Output file already exists")

        # Ensure destination directory exists
        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)


        # 6. Load texture if texture_path is provided and file exists
        texture_img: Optional[Image.Image] = None
        if texture_path and os.path.isfile(texture_path):
            with Image.open(texture_path) as raw_tex:
                texture_img = raw_tex.convert("RGB")

        # 7 & 8. Apply border / texture transformation
        clean_border_mode = (border_mode or "bars").strip().lower()

        if clean_border_mode == "bars":
            if texture_img is not None:
                canvas_w, canvas_h = _calculate_bars_size(
                    img.width, img.height, target_ratio
                )
                bg = make_texture_background(
                    canvas_w, canvas_h, texture_img, mode=texture_mode
                )
                offset_x = (canvas_w - img.width) // 2
                offset_y = (canvas_h - img.height) // 2
                bg.paste(img, (offset_x, offset_y))
                result_img = bg
            else:
                result_img = pad_bars(img, target_ratio, fill=fill)

        elif clean_border_mode == "full_border":
            if texture_img is not None:
                pct = max(0.0, float(border_pct))
                border_px = (
                    max(1, round(min(img.width, img.height) * pct / 100))
                    if pct > 0
                    else 0
                )
                bordered_w = img.width + 2 * border_px
                bordered_h = img.height + 2 * border_px
                canvas_w, canvas_h = _calculate_bars_size(
                    bordered_w, bordered_h, target_ratio
                )
                bg = make_texture_background(
                    canvas_w, canvas_h, texture_img, mode=texture_mode
                )
                offset_x = (canvas_w - img.width) // 2
                offset_y = (canvas_h - img.height) // 2
                bg.paste(img, (offset_x, offset_y))
                result_img = bg
            else:
                result_img = pad_full_border(
                    img, target_ratio, border_pct=border_pct, fill=fill
                )

        else:
            return (output_path, False, f"Unknown border mode: '{border_mode}'")

        # 9. Save with appropriate quality settings
        out_dir = os.path.dirname(os.path.abspath(output_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        save_kwargs: dict[str, Any] = {"format": pillow_format}
        if has_quality:
            save_kwargs["quality"] = max(1, min(100, int(quality)))

        result_img.save(output_path, **save_kwargs)

        # 10. Return success
        return (output_path, True, "")

    except Exception as e:
        return (output_path, False, str(e))


# ===========================================================================
# 4. Preview Generator (formerly preview.py)
# ===========================================================================

def generate_preview(
    filepath: str,
    target_ratio_label: str,
    border_mode: str = "bars",
    border_pct: float = 5.0,
    fill: Union[str, tuple[int, ...]] = "white",
    texture_path: str = "",
    texture_mode: str = "tile",
    max_size: int = 400,
    rotation: int = 0,
) -> Optional[Image.Image]:
    """Generate a preview thumbnail showing the processed result.

    Works on a downscaled copy of the source image for speed. The preview
    is suitable for display in a Tkinter Canvas or Label widget.

    Args:
        filepath: Absolute path to the source image.
        target_ratio_label: Target IG ratio key (e.g. '4:5', '1:1').
        border_mode: 'bars' or 'full_border'.
        border_pct: Border thickness percentage for 'full_border' mode.
        fill: Background fill color (name, hex, or RGB tuple).
        texture_path: Optional path to a texture image file.
        texture_mode: 'tile' or 'stretch' for texture fill.
        max_size: Maximum dimension (width or height) of the preview thumbnail.
        rotation: Degrees clockwise to rotate image (0, 90, 180, 270).

    Returns:
        A Pillow RGB Image thumbnail of the processed result, or None on error.
    """
    try:
        # Open and downscale the source image for fast preview
        with Image.open(filepath) as raw_img:
            img = raw_img.convert("RGB")

        # Apply rotation if specified (degrees clockwise)
        if rotation % 360 != 0:
            img = img.rotate(-rotation % 360, expand=True)

        # Resolve target ratio (supports Auto, explicit keys, or floats)
        try:
            target_ratio = resolve_target_ratio(target_ratio_label, img.width, img.height)
        except (ValueError, TypeError, KeyError):
            return None

        if target_ratio <= 0:
            return None


        # Downscale source to fit within max_size before processing
        # This ensures all operations are fast on a small image
        preview_scale = min(max_size / img.width, max_size / img.height, 1.0)
        if preview_scale < 1.0:
            preview_w = max(1, round(img.width * preview_scale))
            preview_h = max(1, round(img.height * preview_scale))
            resample = getattr(
                Image.Resampling, "LANCZOS", getattr(Image, "LANCZOS", 1)
            )
            img = img.resize((preview_w, preview_h), resample=resample)

        # Load texture if provided
        texture_img = None
        if texture_path and os.path.isfile(texture_path):
            try:
                with Image.open(texture_path) as raw_tex:
                    texture_img = raw_tex.convert("RGB")
                # Scale texture proportionally for preview
                if preview_scale < 1.0:
                    tex_w = max(1, round(texture_img.width * preview_scale))
                    tex_h = max(1, round(texture_img.height * preview_scale))
                    resample = getattr(
                        Image.Resampling, "LANCZOS", getattr(Image, "LANCZOS", 1)
                    )
                    texture_img = texture_img.resize(
                        (tex_w, tex_h), resample=resample
                    )
            except Exception:
                texture_img = None

        # Apply the selected border mode
        clean_mode = (border_mode or "bars").strip().lower()

        if clean_mode == "bars":
            if texture_img is not None:
                canvas_w, canvas_h = _calculate_bars_size(
                    img.width, img.height, target_ratio
                )
                bg = make_texture_background(
                    canvas_w, canvas_h, texture_img, mode=texture_mode
                )
                offset_x = (canvas_w - img.width) // 2
                offset_y = (canvas_h - img.height) // 2
                bg.paste(img, (offset_x, offset_y))
                result = bg
            else:
                result = pad_bars(img, target_ratio, fill=fill)

        elif clean_mode == "full_border":
            if texture_img is not None:
                pct = max(0.0, float(border_pct))
                border_px = (
                    max(1, round(min(img.width, img.height) * pct / 100))
                    if pct > 0
                    else 0
                )
                bordered_w = img.width + 2 * border_px
                bordered_h = img.height + 2 * border_px
                canvas_w, canvas_h = _calculate_bars_size(
                    bordered_w, bordered_h, target_ratio
                )
                bg = make_texture_background(
                    canvas_w, canvas_h, texture_img, mode=texture_mode
                )
                offset_x = (canvas_w - img.width) // 2
                offset_y = (canvas_h - img.height) // 2
                bg.paste(img, (offset_x, offset_y))
                result = bg
            else:
                result = pad_full_border(
                    img, target_ratio, border_pct=border_pct, fill=fill
                )
        else:
            # Unknown mode, fall back to bars
            result = pad_bars(img, target_ratio, fill=fill)

        # Final fit to max_size (the processing may have expanded dimensions)
        final_scale = min(max_size / result.width, max_size / result.height, 1.0)
        if final_scale < 1.0:
            final_w = max(1, round(result.width * final_scale))
            final_h = max(1, round(result.height * final_scale))
            resample = getattr(
                Image.Resampling, "LANCZOS", getattr(Image, "LANCZOS", 1)
            )
            result = result.resize((final_w, final_h), resample=resample)

        return result

    except Exception:
        return None


def generate_original_thumbnail(
    filepath: str, max_size: int = 400
) -> Optional[Image.Image]:
    """Generate a simple thumbnail of the original image without any processing.

    Useful for displaying the source image alongside the processed preview.

    Args:
        filepath: Absolute path to the image file.
        max_size: Maximum dimension for the thumbnail.

    Returns:
        A Pillow RGB Image thumbnail, or None on error.
    """
    try:
        with Image.open(filepath) as raw_img:
            img = raw_img.convert("RGB")

        scale = min(max_size / img.width, max_size / img.height, 1.0)
        if scale < 1.0:
            thumb_w = max(1, round(img.width * scale))
            thumb_h = max(1, round(img.height * scale))
            resample = getattr(
                Image.Resampling, "LANCZOS", getattr(Image, "LANCZOS", 1)
            )
            img = img.resize((thumb_w, thumb_h), resample=resample)

        return img

    except Exception:
        return None


# ===========================================================================
# 5. Persistent Settings (formerly settings.py)
# ===========================================================================

@dataclass
class Settings:
    """User preferences and configuration for Instagram Image Formatter.

    Maintains application state between sessions via JSON file persistence.
    When compiled into a frozen executable (`sys.frozen`), the configuration
    file is anchored directly adjacent to `sys.executable` for full USB/portable
    independence. When running as a Python script, it is stored in the user's
    home directory (`~/.ig_formatter_settings.json`).
    """

    last_folder: str = ""
    recurse: bool = True
    no_manifest: bool = False  # If True, bypasses reading and writing .ig_manifest.json
    all_formats: bool = True  # Maintained for legacy compatibility
    scan_jpg: bool = True
    scan_png: bool = True
    scan_tiff: bool = True
    scan_webp: bool = True
    scan_bmp: bool = True
    default_target: str = "Auto (5:4 / 4:5)"
    default_border_mode: str = "bars"
    border_pct: float = 5.0
    fill_color: str = "#ffffff"
    texture_path: str = ""
    texture_mode: str = "tile"
    output_format: str = "JPEG"
    quality: int = 100
    suffix: str = "_ig"
    window_geometry: str = ""
    filter_mode: str = "needs_processing"
    dest_same_as_source: bool = True
    dest_folder: str = ""


    @classmethod
    def _config_path(cls) -> str:
        """Return the path to the JSON configuration file.

        If sys.frozen, anchors adjacent to sys.executable.
        Otherwise anchors to ~/.ig_formatter_settings.json.
        """
        if getattr(sys, "frozen", False):
            base_dir = os.path.dirname(os.path.abspath(sys.executable))
            return os.path.join(base_dir, ".ig_formatter_settings.json")
        return os.path.abspath(os.path.expanduser("~/.ig_formatter_settings.json"))

    def to_dict(self) -> dict[str, Any]:
        """Convert settings instance to a dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        """Instantiate Settings from a dictionary, ignoring any unknown keys.

        Includes schema migration logic to gracefully handle configuration files
        saved by older software versions.
        """
        valid_fields = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        # Backward compatibility for legacy all_formats setting
        if "all_formats" in filtered and not filtered["all_formats"]:
            if "scan_jpg" not in data:
                filtered.setdefault("scan_jpg", True)
                filtered.setdefault("scan_png", False)
                filtered.setdefault("scan_tiff", False)
                filtered.setdefault("scan_webp", False)
                filtered.setdefault("scan_bmp", False)
        return cls(**filtered)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Settings":
        """Read settings from a JSON file, falling back to defaults for missing keys or errors.

        Args:
            path: Optional custom path to load settings from. Defaults to cls._config_path().

        Returns:
            A populated Settings instance.
        """
        config_path = path or cls._config_path()

        if not os.path.isfile(config_path):
            # Check alternative filename without leading dot if frozen or portable
            if path is None:
                base_dir = os.path.dirname(config_path)
                alt_name = (
                    "ig_formatter_settings.json"
                    if os.path.basename(config_path) == ".ig_formatter_settings.json"
                    else ".ig_formatter_settings.json"
                )
                alt_path = os.path.join(base_dir, alt_name)
                if os.path.isfile(alt_path):
                    config_path = alt_path
                else:
                    return cls()
            else:
                return cls()

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return cls()
            return cls.from_dict(data)
        except (json.JSONDecodeError, OSError):
            return cls()

    def save(self, path: Optional[str] = None) -> None:
        """Write current settings to the JSON configuration file.

        Args:
            path: Optional custom path to write settings to. Defaults to self._config_path().
        """
        config_path = path or self._config_path()
        parent_dir = os.path.dirname(config_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)


# ===========================================================================
# 6. Tkinter GUI Application (formerly gui.py)
# ===========================================================================

_BORDER_MODES = {"Bars (letterbox/pillarbox)": "bars", "Full Border + Bars": "full_border"}
_BORDER_LABELS = {v: k for k, v in _BORDER_MODES.items()}
_RATIO_LABELS = {k: k for k in IG_RATIOS}

# Quality slider applies to these formats
_QUALITY_FORMATS = {k for k, (_, has_q) in OUTPUT_FORMATS.items() if has_q}

_PREVIEW_MAX = 350
_DRAIN_MS = 100
_PREVIEW_DEBOUNCE_MS = 200
class TreeviewTooltip:
    """Floating tooltip for Treeview rows displaying export recipe and history."""

    def __init__(self, tree: ttk.Treeview, get_text_callback: Any) -> None:
        self.tree = tree
        self.get_text = get_text_callback
        self.tip_window: Optional[tk.Toplevel] = None
        self.current_iid: Optional[str] = None
        self.tree.bind("<Motion>", self._on_motion, add="+")
        self.tree.bind("<Leave>", self._on_leave, add="+")
        self.tree.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_motion(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            self._hide()
            return
        if iid == self.current_iid and self.tip_window:
            return
        self.current_iid = iid
        text = self.get_text(iid)
        if not text:
            self._hide()
            return
        self._show(event.x_root + 16, event.y_root + 12, text)

    def _show(self, x: int, y: int, text: str) -> None:
        self._hide()
        self.tip_window = tw = tk.Toplevel(self.tree)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        frame = tk.Frame(tw, background="#202020", borderwidth=1, relief="solid")
        frame.pack()
        lbl = tk.Label(
            frame,
            text=text,
            justify=tk.LEFT,
            background="#252526",
            foreground="#f0f0f0",
            font=("Segoe UI", 9),
            padx=10,
            pady=8,
        )
        lbl.pack()

    def _hide(self, event: Optional[tk.Event] = None) -> None:
        if self.tip_window:
            try:
                self.tip_window.destroy()
            except tk.TclError:
                pass
            self.tip_window = None
        self.current_iid = None

    def _on_leave(self, event: Optional[tk.Event] = None) -> None:
        self._hide()


class IgFormatterGui:
    """Main application window for Instagram Image Formatter."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Instagram Image Formatter")
        self.root.minsize(1100, 700)

        # State
        self.settings = Settings.load()
        self.scan_results: list[ImageInfo] = []
        self.filtered_results: list[ImageInfo] = []
        self.selected_indices: list[int] = []  # indices into filtered_results
        self._sort_col: str = ""
        self._sort_descending: bool = False
        self._per_image_overrides: dict[str, dict] = {}  # filepath -> {target, border_mode, ...}
        self._manifest_cache: dict[str, dict[str, Any]] = {}  # folder_path -> manifest_dict
        self._preview_job_id: Optional[str] = None
        self._current_history_recipes: list[dict[str, Any]] = []
        self._scan_queue: queue.Queue = queue.Queue()
        self._process_queue: queue.Queue = queue.Queue()
        self._is_scanning = False
        self._is_processing = False
        self._process_cancel = threading.Event()
        self._photo_preview: Optional[ImageTk.PhotoImage] = None  # prevent GC

        # Restore window geometry
        if self.settings.window_geometry:
            try:
                self.root.geometry(self.settings.window_geometry)
            except tk.TclError:
                pass

        self._build_ui()
        self._restore_settings()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -----------------------------------------------------------------------
    # UI Construction
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Build all GUI widgets."""
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        self._build_top_bar()
        self._build_main_area()
        self._build_bottom_bar()

    def _build_top_bar(self) -> None:
        """Folder picker, destination picker, format checkboxes, recurse, and scan button."""
        top = ttk.Frame(self.root, padding=5)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        # Row 0: Source folder
        ttk.Label(top, text="Folder:").grid(row=0, column=0, padx=(0, 5))

        self.var_folder = tk.StringVar(value=self.settings.last_folder)
        folder_entry = ttk.Entry(top, textvariable=self.var_folder)
        folder_entry.grid(row=0, column=1, sticky="ew", padx=(0, 5))

        ttk.Button(top, text="Browse", command=self._browse_folder).grid(
            row=0, column=2, padx=(0, 10)
        )

        self.var_recurse = tk.BooleanVar(value=self.settings.recurse)
        ttk.Checkbutton(top, text="Recurse", variable=self.var_recurse).grid(
            row=0, column=3, padx=(0, 10)
        )

        self.var_no_manifest = tk.BooleanVar(value=self.settings.no_manifest)
        ttk.Checkbutton(top, text="No Manifest", variable=self.var_no_manifest).grid(
            row=0, column=4, padx=(0, 10)
        )

        self.btn_scan = ttk.Button(top, text="Scan Folder", command=self._start_scan)
        self.btn_scan.grid(row=0, column=5)

        # Row 1: Destination folder
        ttk.Label(top, text="Destination:").grid(row=1, column=0, padx=(0, 5), pady=(4, 0))

        self.var_dest_folder = tk.StringVar(value=self.settings.dest_folder)
        self.entry_dest = ttk.Entry(top, textvariable=self.var_dest_folder)
        self.entry_dest.grid(row=1, column=1, sticky="ew", padx=(0, 5), pady=(4, 0))
        self.entry_dest.bind("<FocusOut>", lambda e: self._on_dest_folder_change())

        self.btn_browse_dest = ttk.Button(top, text="Browse", command=self._browse_dest_folder)
        self.btn_browse_dest.grid(row=1, column=2, padx=(0, 10), pady=(4, 0))

        self.var_dest_same_as_source = tk.BooleanVar(value=self.settings.dest_same_as_source)
        self.chk_same_dest = ttk.Checkbutton(
            top,
            text="Same as source image",
            variable=self.var_dest_same_as_source,
            command=self._on_toggle_same_dest,
        )
        self.chk_same_dest.grid(row=1, column=3, columnspan=3, sticky="w", pady=(4, 0))

        # Row 2: Format filter checkboxes
        ttk.Label(top, text="Formats:").grid(row=2, column=0, padx=(0, 5), pady=(4, 0))

        fmt_frame = ttk.Frame(top)
        fmt_frame.grid(row=2, column=1, columnspan=5, sticky="w", pady=(4, 0))

        self.var_scan_jpg = tk.BooleanVar(value=self.settings.scan_jpg)
        self.var_scan_png = tk.BooleanVar(value=self.settings.scan_png)
        self.var_scan_tiff = tk.BooleanVar(value=self.settings.scan_tiff)
        self.var_scan_webp = tk.BooleanVar(value=self.settings.scan_webp)
        self.var_scan_bmp = tk.BooleanVar(value=self.settings.scan_bmp)

        self.scan_format_vars: dict[str, tuple[tuple[str, ...], tk.BooleanVar]] = {
            "JPG / JPEG": (SCAN_FORMAT_GROUPS["JPG / JPEG"], self.var_scan_jpg),
            "PNG": (SCAN_FORMAT_GROUPS["PNG"], self.var_scan_png),
            "TIFF": (SCAN_FORMAT_GROUPS["TIFF"], self.var_scan_tiff),
            "WEBP": (SCAN_FORMAT_GROUPS["WEBP"], self.var_scan_webp),
            "BMP": (SCAN_FORMAT_GROUPS["BMP"], self.var_scan_bmp),
        }

        for label, (_exts, var) in self.scan_format_vars.items():
            ttk.Checkbutton(fmt_frame, text=label, variable=var).pack(
                side=tk.LEFT, padx=(0, 12)
            )

        ttk.Button(
            fmt_frame, text="All", width=4, command=self._select_all_formats
        ).pack(side=tk.LEFT, padx=(6, 2))
        ttk.Button(
            fmt_frame, text="None", width=5, command=self._select_no_formats
        ).pack(side=tk.LEFT, padx=(2, 0))

        # Apply initial enabled/disabled state to destination input
        self._on_toggle_same_dest()

    def _select_all_formats(self) -> None:
        """Check all scan format checkboxes."""
        for _label, (_exts, var) in self.scan_format_vars.items():
            var.set(True)

    def _select_no_formats(self) -> None:
        """Uncheck all scan format checkboxes."""
        for _label, (_exts, var) in self.scan_format_vars.items():
            var.set(False)

    def _get_selected_extensions(self) -> tuple[str, ...]:
        """Return tuple of extensions selected by the format checkboxes."""
        exts: list[str] = []
        for _label, (group_exts, var) in self.scan_format_vars.items():
            if var.get():
                exts.extend(group_exts)
        return tuple(exts)

    def _build_main_area(self) -> None:
        """Left panel (table view) + Right panel (preview/multi-selection & controls)."""
        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        # ----- Left Panel -----
        left = ttk.Frame(main)
        main.add(left, weight=3)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        # Filter bar with Needs Processing, Processed, and Show All
        filter_bar = ttk.Frame(left)
        filter_bar.grid(row=0, column=0, sticky="ew", pady=(0, 3))

        self.var_filter = tk.StringVar(value=self.settings.filter_mode)
        ttk.Radiobutton(
            filter_bar, text="Needs Processing", variable=self.var_filter,
            value="needs_processing", command=self._apply_filter
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(
            filter_bar, text="Processed", variable=self.var_filter,
            value="processed", command=self._apply_filter
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(
            filter_bar, text="Show All", variable=self.var_filter,
            value="all", command=self._apply_filter
        ).pack(side=tk.LEFT, padx=(0, 20))

        # Table view (Treeview with extended multi-selection)
        self.table_frame = ttk.Frame(left)
        self.table_frame.grid(row=1, column=0, sticky="nsew")
        self.table_frame.columnconfigure(0, weight=1)
        self.table_frame.rowconfigure(0, weight=1)

        cols = ("status", "filename", "dimensions", "ratio", "current", "orientation", "subfolder")
        self.tree = ttk.Treeview(self.table_frame, columns=cols, show="headings", selectmode="extended")

        self.tree.heading("status", text="Status", command=lambda: self._sort_column("status"))
        self.tree.heading("filename", text="Filename", command=lambda: self._sort_column("filename"))
        self.tree.heading("dimensions", text="Dimensions", command=lambda: self._sort_column("dimensions"))
        self.tree.heading("ratio", text="Ratio", command=lambda: self._sort_column("ratio"))
        self.tree.heading("current", text="Current IG", command=lambda: self._sort_column("current"))
        self.tree.heading("orientation", text="Orientation", command=lambda: self._sort_column("orientation"))
        self.tree.heading("subfolder", text="Subfolder", command=lambda: self._sort_column("subfolder"))

        self.tree.column("status", width=115, minwidth=95)
        self.tree.column("filename", width=190, minwidth=120)
        self.tree.column("dimensions", width=95, minwidth=80)
        self.tree.column("ratio", width=60, minwidth=50)
        self.tree.column("current", width=75, minwidth=60)
        self.tree.column("orientation", width=85, minwidth=70)
        self.tree.column("subfolder", width=110, minwidth=80)

        # Tags for colored status display
        self.tree.tag_configure("tag_processed", foreground="#2e7d32")
        self.tree.tag_configure("tag_missing", foreground="#e65100")
        self.tree.tag_configure("tag_needs", foreground="#c62828")
        self.tree.tag_configure("tag_ready", foreground="#1565c0")

        # Row hover tooltip for export recipes & history
        self.tree_tooltip = TreeviewTooltip(self.tree, self._get_row_tooltip_text)

        tree_scroll = ttk.Scrollbar(self.table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Context menu for right-click on table rows
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="↻  Rotate 90° CW", command=self._rotate_cw)
        self.context_menu.add_command(label="↺  Rotate 90° CCW", command=self._rotate_ccw)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Reset Rotation", command=self._reset_rotation)
        self.tree.bind("<Button-3>", self._on_tree_right_click)

        # ----- Right Panel -----
        right = ttk.Frame(main)
        main.add(right, weight=2)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        # Preview Container (Swappable between Single Image Preview & Multi-Selection Summary)
        self.preview_container = ttk.Frame(right)
        self.preview_container.grid(row=0, column=0, sticky="nsew", pady=(0, 5))
        self.preview_container.columnconfigure(0, weight=1)
        self.preview_container.rowconfigure(0, weight=1)

        # 1. Single-Image Preview Frame
        self.single_frame = ttk.Frame(self.preview_container)
        self.single_frame.columnconfigure(0, weight=1)
        self.single_frame.rowconfigure(0, weight=1)

        preview_lf = ttk.LabelFrame(self.single_frame, text="Preview", padding=5)
        preview_lf.grid(row=0, column=0, sticky="nsew", pady=(0, 5))
        preview_lf.columnconfigure(0, weight=1)
        preview_lf.rowconfigure(0, weight=1)

        self.preview_canvas = tk.Canvas(preview_lf, bg="#e0e0e0", width=_PREVIEW_MAX, height=_PREVIEW_MAX)
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        self.preview_canvas.bind("<Configure>", self._on_canvas_configure)

        # Rotation controls toolbar below preview canvas
        rot_frame = ttk.Frame(preview_lf)
        rot_frame.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.btn_rot_ccw = ttk.Button(rot_frame, text="↺  Rotate 90° CCW", command=self._rotate_ccw)
        self.btn_rot_ccw.pack(side=tk.LEFT, padx=(0, 5))
        self.btn_rot_cw = ttk.Button(rot_frame, text="↻  Rotate 90° CW", command=self._rotate_cw)
        self.btn_rot_cw.pack(side=tk.LEFT, padx=(0, 5))
        self.lbl_rot_status = ttk.Label(rot_frame, text="", foreground="#666666")
        self.lbl_rot_status.pack(side=tk.LEFT, padx=(5, 0))

        meta_lf = ttk.LabelFrame(self.single_frame, text="Image Details", padding=5)
        meta_lf.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        meta_lf.columnconfigure(1, weight=1)

        self.lbl_meta_filename = ttk.Label(meta_lf, text="—")
        self.lbl_meta_dims = ttk.Label(meta_lf, text="—")
        self.lbl_meta_ratio = ttk.Label(meta_lf, text="—")
        self.lbl_meta_orientation = ttk.Label(meta_lf, text="—")
        self.lbl_meta_status = ttk.Label(meta_lf, text="—")
        self.lbl_meta_rotation = ttk.Label(meta_lf, text="—")
        # History Dropdown: displays past export recipes (reverse-chronological); selecting one restores settings
        self.cmb_meta_history = ttk.Combobox(meta_lf, state="disabled", width=28)
        self.cmb_meta_history.set("No export history")
        self.lbl_meta_history = self.cmb_meta_history  # Attribute compatibility alias

        ttk.Label(meta_lf, text="File:").grid(row=0, column=0, sticky="w")
        self.lbl_meta_filename.grid(row=0, column=1, sticky="w", padx=(5, 0))
        ttk.Label(meta_lf, text="Size:").grid(row=1, column=0, sticky="w")
        self.lbl_meta_dims.grid(row=1, column=1, sticky="w", padx=(5, 0))
        ttk.Label(meta_lf, text="Ratio:").grid(row=2, column=0, sticky="w")
        self.lbl_meta_ratio.grid(row=2, column=1, sticky="w", padx=(5, 0))
        ttk.Label(meta_lf, text="Orientation:").grid(row=3, column=0, sticky="w")
        self.lbl_meta_orientation.grid(row=3, column=1, sticky="w", padx=(5, 0))
        ttk.Label(meta_lf, text="Status:").grid(row=4, column=0, sticky="w")
        self.lbl_meta_status.grid(row=4, column=1, sticky="w", padx=(5, 0))
        ttk.Label(meta_lf, text="Rotation:").grid(row=5, column=0, sticky="w")
        self.lbl_meta_rotation.grid(row=5, column=1, sticky="w", padx=(5, 0))
        ttk.Label(meta_lf, text="History:").grid(row=6, column=0, sticky="w")
        self.cmb_meta_history.grid(row=6, column=1, sticky="ew", padx=(5, 0), pady=1)
        self.cmb_meta_history.bind("<<ComboboxSelected>>", self._on_history_selected)
        self.lbl_history_file_status = ttk.Label(meta_lf, text="", font=("TkDefaultFont", 8))
        self.lbl_history_file_status.grid(row=7, column=1, sticky="w", padx=(5, 0), pady=(0, 2))

        # 2. Multi-Selection Frame (Swapped in when >1 items selected)
        self.multi_frame = ttk.Frame(self.preview_container)
        self.multi_frame.columnconfigure(0, weight=1)
        self.multi_frame.rowconfigure(0, weight=1)

        multi_lf = ttk.LabelFrame(self.multi_frame, text="Multiple Items Selected", padding=5)
        multi_lf.grid(row=0, column=0, sticky="nsew")
        multi_lf.columnconfigure(0, weight=1)
        multi_lf.rowconfigure(3, weight=1)

        self.lbl_multi_count = ttk.Label(multi_lf, text="0 files selected", font=("TkDefaultFont", 9, "bold"))
        self.lbl_multi_count.grid(row=0, column=0, sticky="w", pady=(0, 2))

        ttk.Label(
            multi_lf, text="Settings below apply to all selected files in bulk:", foreground="#555555"
        ).grid(row=1, column=0, sticky="w", pady=(0, 3))

        multi_rot_frame = ttk.Frame(multi_lf)
        multi_rot_frame.grid(row=2, column=0, sticky="w", pady=(0, 5))
        ttk.Button(
            multi_rot_frame, text="↺  Rotate Selected 90° CCW", command=self._rotate_ccw
        ).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(
            multi_rot_frame, text="↻  Rotate Selected 90° CW", command=self._rotate_cw
        ).pack(side=tk.LEFT)

        multi_tree_frame = ttk.Frame(multi_lf)
        multi_tree_frame.grid(row=3, column=0, sticky="nsew")
        multi_tree_frame.columnconfigure(0, weight=1)
        multi_tree_frame.rowconfigure(0, weight=1)

        m_cols = ("filename", "orientation", "dimensions", "ratio")
        self.multi_tree = ttk.Treeview(multi_tree_frame, columns=m_cols, show="headings", selectmode="none")
        self.multi_tree.heading("filename", text="Filename")
        self.multi_tree.heading("orientation", text="Orientation")
        self.multi_tree.heading("dimensions", text="Dimensions")
        self.multi_tree.heading("ratio", text="Ratio")

        self.multi_tree.column("filename", width=140, minwidth=90)
        self.multi_tree.column("orientation", width=75, minwidth=60)
        self.multi_tree.column("dimensions", width=80, minwidth=65)
        self.multi_tree.column("ratio", width=55, minwidth=45)

        m_scroll = ttk.Scrollbar(multi_tree_frame, orient=tk.VERTICAL, command=self.multi_tree.yview)
        self.multi_tree.configure(yscrollcommand=m_scroll.set)
        self.multi_tree.grid(row=0, column=0, sticky="nsew")
        m_scroll.grid(row=0, column=1, sticky="ns")

        # Initially show single_frame
        self.single_frame.pack(fill=tk.BOTH, expand=True)

        # Settings Controls Frame
        ctrl_lf = ttk.LabelFrame(right, text="Format & Export Settings", padding=5)
        ctrl_lf.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        ctrl_lf.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(ctrl_lf, text="Target:").grid(row=row, column=0, sticky="w")
        self.var_target = tk.StringVar(value=self.settings.default_target)
        self.cmb_target = ttk.Combobox(
            ctrl_lf, textvariable=self.var_target,
            values=TARGET_OPTIONS, state="readonly", width=16
        )
        self.cmb_target.grid(row=row, column=1, sticky="w", padx=(5, 0))
        self.cmb_target.bind("<<ComboboxSelected>>", self._on_target_change)

        row += 1
        ttk.Label(ctrl_lf, text="Border:").grid(row=row, column=0, sticky="w")
        self.var_border_mode = tk.StringVar(value="Bars (letterbox/pillarbox)")
        self.cmb_border = ttk.Combobox(
            ctrl_lf, textvariable=self.var_border_mode,
            values=list(_BORDER_MODES.keys()), state="readonly", width=22
        )
        self.cmb_border.grid(row=row, column=1, sticky="w", padx=(5, 0))
        self.cmb_border.bind("<<ComboboxSelected>>", self._on_border_change)

        row += 1
        self.lbl_thickness_title = ttk.Label(ctrl_lf, text="Thickness:")
        self.lbl_thickness_title.grid(row=row, column=0, sticky="w")
        self.var_border_pct = tk.DoubleVar(value=self.settings.border_pct)
        thickness_frame = ttk.Frame(ctrl_lf)
        thickness_frame.grid(row=row, column=1, sticky="ew", padx=(5, 0))
        thickness_frame.columnconfigure(0, weight=1)
        self.scale_thickness = ttk.Scale(
            thickness_frame, from_=0, to=15, variable=self.var_border_pct,
            orient=tk.HORIZONTAL, command=self._on_thickness_scale
        )
        self.scale_thickness.grid(row=0, column=0, sticky="ew")
        self.lbl_thickness_val = ttk.Label(thickness_frame, text="5.0%", width=6)
        self.lbl_thickness_val.grid(row=0, column=1, padx=(5, 0))
        self.var_border_pct.trace_add("write", self._on_thickness_change)

        # Color controls row
        row += 1
        ttk.Label(ctrl_lf, text="Color:").grid(row=row, column=0, sticky="w")
        color_frame = ttk.Frame(ctrl_lf)
        color_frame.grid(row=row, column=1, sticky="w", padx=(5, 0))

        self.color_swatch = tk.Canvas(color_frame, width=24, height=24, bg=self.settings.fill_color,
                                       highlightthickness=1, highlightbackground="gray")
        self.color_swatch.pack(side=tk.LEFT, padx=(0, 5))
        self.color_swatch.bind("<Button-1>", lambda e: self._pick_color())

        ttk.Button(color_frame, text="W", width=3, command=lambda: self._set_color("#ffffff")).pack(side=tk.LEFT, padx=1)
        ttk.Button(color_frame, text="B", width=3, command=lambda: self._set_color("#000000")).pack(side=tk.LEFT, padx=1)
        ttk.Button(color_frame, text="Pick...", command=self._pick_color).pack(side=tk.LEFT, padx=(5, 0))

        # Texture controls row
        row += 1
        ttk.Label(ctrl_lf, text="Texture:").grid(row=row, column=0, sticky="w")
        tex_frame = ttk.Frame(ctrl_lf)
        tex_frame.grid(row=row, column=1, sticky="ew", padx=(5, 0))

        self.var_texture_path = tk.StringVar(value=self.settings.texture_path)
        self.lbl_texture = ttk.Label(tex_frame, text="None", width=18, anchor="w")
        self.lbl_texture.pack(side=tk.LEFT)
        ttk.Button(tex_frame, text="Load", command=self._load_texture).pack(side=tk.LEFT, padx=2)
        ttk.Button(tex_frame, text="Clear", command=self._clear_texture).pack(side=tk.LEFT, padx=2)

        self.var_texture_mode = tk.StringVar(value=self.settings.texture_mode)
        ttk.Radiobutton(tex_frame, text="Tile", variable=self.var_texture_mode,
                        value="tile", command=self._on_texture_mode_change).pack(side=tk.LEFT, padx=(10, 2))
        ttk.Radiobutton(tex_frame, text="Stretch", variable=self.var_texture_mode,
                        value="stretch", command=self._on_texture_mode_change).pack(side=tk.LEFT)

        # Output format & quality
        row += 1
        ttk.Label(ctrl_lf, text="Output:").grid(row=row, column=0, sticky="w")
        out_frame = ttk.Frame(ctrl_lf)
        out_frame.grid(row=row, column=1, sticky="ew", padx=(5, 0))
        out_frame.columnconfigure(2, weight=1)

        self.var_output_format = tk.StringVar(value=self.settings.output_format)
        self.cmb_output = ttk.Combobox(
            out_frame, textvariable=self.var_output_format,
            values=list(OUTPUT_FORMATS.keys()), state="readonly", width=8
        )
        self.cmb_output.pack(side=tk.LEFT, padx=(0, 10))
        self.cmb_output.bind("<<ComboboxSelected>>", self._on_output_format_change)

        self.lbl_quality_label = ttk.Label(out_frame, text="Quality:")
        self.lbl_quality_label.pack(side=tk.LEFT)

        self.var_quality = tk.IntVar(value=self.settings.quality)
        self.scale_quality = ttk.Scale(
            out_frame, from_=1, to=100, variable=self.var_quality,
            orient=tk.HORIZONTAL, length=120
        )
        self.scale_quality.pack(side=tk.LEFT, padx=(5, 0))

        self.lbl_quality_val = ttk.Label(out_frame, text="100", width=4)
        self.lbl_quality_val.pack(side=tk.LEFT, padx=(5, 0))
        self.var_quality.trace_add("write", self._on_quality_change)

        self._update_quality_visibility()

        # Suffix
        row += 1
        ttk.Label(ctrl_lf, text="Suffix:").grid(row=row, column=0, sticky="w")
        self.var_suffix = tk.StringVar(value=self.settings.suffix)
        suffix_entry = ttk.Entry(ctrl_lf, textvariable=self.var_suffix, width=15)
        suffix_entry.grid(row=row, column=1, sticky="w", padx=(5, 0))
        suffix_entry.bind("<FocusOut>", lambda e: self._on_suffix_change())

        # Update texture label and thickness state
        self._update_texture_label()
        self._update_thickness_state()

    def _build_bottom_bar(self) -> None:
        """Bulk controls, process buttons, progress bar."""
        bottom = ttk.Frame(self.root, padding=5)
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(4, weight=1)

        # Bulk controls
        ttk.Label(bottom, text="Bulk Target:").grid(row=0, column=0, padx=(0, 3))
        self.var_bulk_target = tk.StringVar(value=DEFAULT_TARGET)
        cmb_bulk_target = ttk.Combobox(
            bottom, textvariable=self.var_bulk_target,
            values=TARGET_OPTIONS, state="readonly", width=16
        )
        cmb_bulk_target.grid(row=0, column=1, padx=(0, 10))

        ttk.Label(bottom, text="Bulk Border:").grid(row=0, column=2, padx=(0, 3))
        self.var_bulk_border = tk.StringVar(value="Bars (letterbox/pillarbox)")
        cmb_bulk_border = ttk.Combobox(
            bottom, textvariable=self.var_bulk_border,
            values=list(_BORDER_MODES.keys()), state="readonly", width=22
        )
        cmb_bulk_border.grid(row=0, column=3, padx=(0, 5))

        ttk.Button(bottom, text="Apply to All", command=self._apply_bulk).grid(
            row=0, column=4, sticky="w", padx=(0, 20)
        )

        # Process buttons
        self.btn_process_sel = ttk.Button(
            bottom, text="Process Selected", command=self._process_selected
        )
        self.btn_process_sel.grid(row=0, column=5, padx=(0, 5))

        self.btn_process_all = ttk.Button(
            bottom, text="Process All", command=self._process_all
        )
        self.btn_process_all.grid(row=0, column=6, padx=(0, 10))

        self.btn_cancel = ttk.Button(
            bottom, text="Cancel", command=self._cancel_processing, state=tk.DISABLED
        )
        self.btn_cancel.grid(row=0, column=7, padx=(0, 10))

        # Progress bar (compact length to make room for status text)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            bottom, variable=self.progress_var, maximum=100, mode="determinate", length=130
        )
        self.progress_bar.grid(row=0, column=8, padx=(0, 8))

        self.lbl_progress = ttk.Label(
            bottom,
            text=f"Requirements met ({PREREQUISITES_STATUS}) | Ready",
            width=50,
            anchor="w",
        )
        self.lbl_progress.grid(row=0, column=9, sticky="w")
        bottom.columnconfigure(9, weight=1)

    # -----------------------------------------------------------------------
    # Column Sorting
    # -----------------------------------------------------------------------

    def _sort_column(self, col: str) -> None:
        """Sort the table by clicking on a column header."""
        if self._sort_col == col:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_col = col
            self._sort_descending = False

        # Remember currently selected filepaths to restore selection after sort
        selected_filepaths = {
            self.filtered_results[i].filepath
            for i in self.selected_indices
            if 0 <= i < len(self.filtered_results)
        }

        def sort_key(info: ImageInfo):
            overrides = self._per_image_overrides.get(info.filepath, {})
            rot = overrides.get("rotation", 0)
            eff_w, eff_h, eff_ratio, eff_orient, eff_ig, _ = get_effective_image_info(info, rot)
            if col == "status":
                status_prio = {"processed": 0, "needs_processing": 1, "ready": 2}
                p_status = self._get_processed_status(info)
                eff_status = p_status if p_status else ("needs_processing" if info.needs_processing else "ready")
                return (status_prio.get(eff_status, 9), info.filename.lower())
            elif col == "filename":
                return info.filename.lower()
            elif col == "dimensions":
                return eff_w * eff_h
            elif col == "ratio":
                return eff_ratio
            elif col == "current":
                return (eff_ig or "").lower()
            elif col == "orientation":
                return eff_orient.lower()
            elif col == "subfolder":
                return (info.subfolder or "").lower()
            return ""

        self.filtered_results.sort(key=sort_key, reverse=self._sort_descending)

        # Update header text indicators
        col_labels = {
            "status": "Status",
            "filename": "Filename",
            "dimensions": "Dimensions",
            "ratio": "Ratio",
            "current": "Current IG",
            "orientation": "Orientation",
            "subfolder": "Subfolder",
        }
        arrow = " ▼" if self._sort_descending else " ▲"
        for c, text in col_labels.items():
            header_text = f"{text}{arrow}" if c == col else text
            self.tree.heading(c, text=header_text)

        self._populate_table()

        # Restore selection
        new_selection = []
        for i, info in enumerate(self.filtered_results):
            if info.filepath in selected_filepaths:
                new_selection.append(str(i))
        if new_selection:
            self.tree.selection_set(new_selection)
            self.selected_indices = [int(x) for x in new_selection]
            self._update_selection_view()
        else:
            self.selected_indices = []
            self._update_selection_view()

    # -----------------------------------------------------------------------
    # Folder & Scan
    # -----------------------------------------------------------------------

    def _browse_folder(self) -> None:
        """Open folder picker dialog."""
        initial = self.var_folder.get() or os.path.expanduser("~")
        folder = filedialog.askdirectory(initialdir=initial, title="Select Image Folder")
        if folder:
            self.var_folder.set(folder)
            self.settings.last_folder = folder
            self.settings.save()

    def _browse_dest_folder(self) -> None:
        """Open folder picker dialog for destination folder."""
        current = self.var_dest_folder.get().strip() or self.var_folder.get().strip() or os.path.expanduser("~")
        initial = current if os.path.isdir(current) else os.path.expanduser("~")
        folder = filedialog.askdirectory(initialdir=initial, title="Select Destination Folder")
        if folder:
            self.var_dest_folder.set(folder)
            self.settings.dest_folder = folder
            self.settings.save()

    def _on_toggle_same_dest(self) -> None:
        """Enable or disable destination input based on 'Same as source image'."""
        same = self.var_dest_same_as_source.get()
        state = tk.DISABLED if same else tk.NORMAL
        self.entry_dest.configure(state=state)
        self.btn_browse_dest.configure(state=state)
        self.settings.dest_same_as_source = same
        self.settings.save()

    def _on_dest_folder_change(self) -> None:
        """Save destination folder entry changes."""
        self.settings.dest_folder = self.var_dest_folder.get().strip()
        self.settings.save()

    def _start_scan(self) -> None:
        """Launch folder scan on a worker thread."""
        folder = self.var_folder.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("Invalid Folder", "Please select a valid folder to scan.")
            return

        if self._is_scanning:
            return

        self._is_scanning = True
        self.btn_scan.configure(state=tk.DISABLED)
        self.lbl_progress.configure(text="Scanning...")
        self.progress_var.set(0)
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(15)

        # Determine extensions from format checkboxes
        extensions = self._get_selected_extensions()
        if not extensions:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self._is_scanning = False
            self.btn_scan.configure(state=tk.NORMAL)
            self.lbl_progress.configure(text=f"Requirements met ({PREREQUISITES_STATUS}) | Ready")
            messagebox.showwarning(
                "No Formats Selected",
                "Please select at least one image format checkbox before scanning.",
                parent=self.root,
            )
            return

        suffix = self.var_suffix.get() or "_ig"
        no_manifest = self.var_no_manifest.get()

        # Thread isolation: Folder scanning executes on a background daemon thread
        # to ensure the Tkinter GUI loop remains completely responsive during large scans.
        thread = threading.Thread(
            target=self._scan_worker,
            args=(folder, self.var_recurse.get(), extensions, suffix, no_manifest),
            daemon=True,
        )
        thread.start()
        self.root.after(_DRAIN_MS, self._drain_scan_queue)

    def _scan_worker(
        self,
        folder: str,
        recurse: bool,
        extensions: tuple,
        suffix: str,
        no_manifest: bool = False,
    ) -> None:
        """Worker thread: scan folder and post progress and completion results to queue.

        Thread-safety rule: This worker must never call any Tkinter widget methods directly.
        All data is transferred across thread boundaries via `self._scan_queue`.
        """
        def progress_cb(found: int, needs: int) -> None:
            # Yield intermittent discovery counts to UI thread without flooding the queue
            self._scan_queue.put(("progress", found, needs))

        try:
            results = scan_folder(
                folder,
                recurse=recurse,
                extensions=extensions,
                suffix=suffix,
                progress_callback=progress_cb,
                no_manifest=no_manifest,
            )
            self._scan_queue.put(("done", results))
        except Exception as exc:
            self._scan_queue.put(("error", str(exc)))

    def _drain_scan_queue(self) -> None:
        """Drain scan queue on main thread."""
        try:
            msg = self._scan_queue.get_nowait()
        except queue.Empty:
            if self._is_scanning:
                self.root.after(_DRAIN_MS, self._drain_scan_queue)
            return

        status = msg[0]

        if status == "progress":
            _, found, needs = msg
            self.lbl_progress.configure(
                text=f"Scanning... Found {found} images ({needs} need processing)"
            )
            if self._is_scanning:
                self.root.after(_DRAIN_MS, self._drain_scan_queue)
            return

        self._is_scanning = False
        self.btn_scan.configure(state=tk.NORMAL)
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_var.set(0)

        if status == "error":
            data = msg[1]
            self.lbl_progress.configure(text="Scan failed")
            messagebox.showerror("Scan Error", f"Scan failed: {data}")
            return

        data = msg[1]
        self.scan_results = data
        self._per_image_overrides.clear()
        self._manifest_cache.clear()
        self._apply_filter()

        total = len(self.scan_results)
        needs = sum(
            1 for r in self.scan_results
            if r.needs_processing and self._get_processed_status(r) != "processed"
        )
        self.lbl_progress.configure(text=f"Found {total} images, {needs} need processing")


    # -----------------------------------------------------------------------
    # Filtering & Population
    # -----------------------------------------------------------------------

    def _get_manifest_entry(self, filepath: str) -> Optional[dict[str, Any]]:
        """Get the manifest entry for a photo from cache or disk."""
        parent_dir = os.path.dirname(os.path.abspath(filepath))
        filename = os.path.basename(filepath)
        if parent_dir not in self._manifest_cache:
            self._manifest_cache[parent_dir] = load_folder_manifest(parent_dir)
        manifest = self._manifest_cache[parent_dir]
        photos = manifest.get("photos", {})
        return photos.get(filename)

    def _get_processed_status(self, info: ImageInfo) -> str:
        """Return 'processed' if photo has an entry in the manifest, else ''."""
        entry = self._get_manifest_entry(info.filepath)
        if not entry:
            return ""
        return "processed"

    def _get_display_row_values_and_tags(
        self, info: ImageInfo
    ) -> tuple[tuple[Any, ...], tuple[str, ...]]:
        """Compute the 7-column table values and styling tag for an image."""
        overrides = self._per_image_overrides.get(info.filepath, {})
        rot = overrides.get("rotation", 0)
        eff_w, eff_h, eff_ratio, eff_orient, eff_ig, eff_needs = get_effective_image_info(info, rot)

        p_status = self._get_processed_status(info)
        if p_status == "processed":
            status_text = "✓ Processed"
            tag = "tag_processed"
        elif eff_ig:
            status_text = "Ready"
            tag = "tag_ready"
        else:
            status_text = "Needs conversion"
            tag = "tag_needs"

        values = (
            status_text,
            info.filename,
            f"{eff_w}×{eff_h}",
            f"{eff_ratio:.3f}",
            eff_ig or "—",
            eff_orient,
            info.subfolder or "—",
        )
        return values, (tag,)

    def _get_row_tooltip_text(self, item_id: str) -> Optional[str]:
        """Generate tooltip text for a table row showing export recipe and history."""
        if not item_id or not item_id.isdigit():
            return None
        idx = int(item_id)
        if idx < 0 or idx >= len(self.filtered_results):
            return None

        info = self.filtered_results[idx]
        entry = self._get_manifest_entry(info.filepath)
        p_status = self._get_processed_status(info)

        if entry and entry.get("latest"):
            latest = entry["latest"]
            history = entry.get("history", [])
            count = len(history)

            status_header = "✓ Processed"
            time_str = latest.get("processed_at", "Unknown")
            if "T" in time_str:
                time_str = time_str.replace("T", " ")[:19]

            mode_str = latest.get("border_mode", "bars")
            if mode_str == "full_border":
                pct = latest.get("border_pct", 5.0)
                mode_str = f"Full Border ({pct:.1f}%)"
            else:
                mode_str = "Bars (Letterbox/Pillarbox)"

            fill_str = latest.get("fill_color", "#ffffff")
            tex = latest.get("texture_path", "")
            if tex:
                fill_str = f"Texture ({os.path.basename(tex)})"

            rot = latest.get("rotation", 0)
            rot_str = f" • Rot: {rot}° CW" if rot != 0 else ""

            out_file = latest.get("output_file", "—")
            out_path = latest.get("output_path", "")
            parent_dir = os.path.dirname(os.path.abspath(info.filepath))
            file_exists = False
            if out_path and os.path.isfile(out_path):
                file_exists = True
            elif out_file and parent_dir and os.path.isfile(os.path.join(parent_dir, out_file)):
                file_exists = True

            out_status_tag = "[On disk]" if file_exists else "[Missing on disk — ready to re-export]"

            lines = [
                f"{status_header} ({count} export{'s' if count != 1 else ''})",
                f"Exported: {time_str}",
                f"Target: {latest.get('target_ratio', '—')}{rot_str}",
                f"Border: {mode_str}",
                f"Fill: {fill_str}",
                f"Format: {latest.get('output_format', 'JPEG')} (Q: {latest.get('quality', 100)})",
                f"Output: {out_file} {out_status_tag}",
            ]
            return "\n".join(lines)
        else:
            overrides = self._per_image_overrides.get(info.filepath, {})
            rot = overrides.get("rotation", 0)
            eff_w, eff_h, eff_ratio, eff_orient, eff_ig, eff_needs = get_effective_image_info(info, rot)
            if eff_ig:
                return (
                    f"Ready for Instagram\n"
                    f"Format: {eff_ig} ({eff_orient})\n"
                    f"Dimensions: {eff_w}×{eff_h}\n"
                    f"Ratio: {eff_ratio:.4f}"
                )
            else:
                sug = suggest_target_ratio(eff_w, eff_h)
                return (
                    f"Needs Processing\n"
                    f"Current: {eff_ratio:.4f} ({eff_orient})\n"
                    f"Suggested: {sug}\n"
                    f"Dimensions: {eff_w}×{eff_h}"
                )

    def _apply_filter(self) -> None:
        """Apply the current filter and repopulate the view."""
        mode = self.var_filter.get()
        if mode == "needs_processing":
            filtered = []
            for r in self.scan_results:
                p_status = self._get_processed_status(r)
                if p_status == "processed":
                    continue
                rot = self._per_image_overrides.get(r.filepath, {}).get("rotation", 0)
                _, _, _, _, _, eff_needs = get_effective_image_info(r, rot)
                if eff_needs:
                    filtered.append(r)
            self.filtered_results = filtered
        elif mode == "processed":
            self.filtered_results = [
                r for r in self.scan_results
                if self._get_processed_status(r) == "processed"
            ]
        else:
            self.filtered_results = list(self.scan_results)

        self.selected_indices = []
        self._populate_table()
        self._update_selection_view()

    def _populate_table(self) -> None:
        """Fill the Treeview with filtered results."""
        self.tree.delete(*self.tree.get_children())
        for i, info in enumerate(self.filtered_results):
            values, tags = self._get_display_row_values_and_tags(info)
            self.tree.insert("", tk.END, iid=str(i), values=values, tags=tags)

    # -----------------------------------------------------------------------
    # Selection & Preview
    # -----------------------------------------------------------------------

    def _on_tree_select(self, event: tk.Event) -> None:
        """Handle Treeview row selection (single or multi)."""
        selection = self.tree.selection()
        self.selected_indices = [int(iid) for iid in selection if iid.isdigit()]
        self._update_selection_view()

    def _update_selection_view(self) -> None:
        """Switch right-hand panel between single preview and multi-selection table."""
        count = len(self.selected_indices)
        if count == 0:
            self.multi_frame.pack_forget()
            self.single_frame.pack(fill=tk.BOTH, expand=True)
            self._clear_preview()
            self.btn_rot_ccw.configure(state=tk.DISABLED)
            self.btn_rot_cw.configure(state=tk.DISABLED)
        elif count == 1:
            self.multi_frame.pack_forget()
            self.single_frame.pack(fill=tk.BOTH, expand=True)
            self.btn_rot_ccw.configure(state=tk.NORMAL)
            self.btn_rot_cw.configure(state=tk.NORMAL)
            idx = self.selected_indices[0]
            if 0 <= idx < len(self.filtered_results):
                info = self.filtered_results[idx]
                overrides = self._per_image_overrides.get(info.filepath, {})
                rot = overrides.get("rotation", 0)
                eff_w, eff_h, eff_ratio, eff_orient, eff_ig, _ = get_effective_image_info(info, rot)

                self.lbl_meta_filename.configure(text=info.filename)
                self.lbl_meta_dims.configure(text=f"{eff_w} × {eff_h}")
                self.lbl_meta_ratio.configure(text=f"{eff_ratio:.4f}")
                self.lbl_meta_orientation.configure(text=eff_orient)

                p_status = self._get_processed_status(info)
                entry = self._get_manifest_entry(info.filepath)

                if p_status == "processed":
                    self.lbl_meta_status.configure(text="✓ Processed", foreground="#2e7d32")
                elif p_status == "missing_output":
                    self.lbl_meta_status.configure(text="⚠ Output Missing", foreground="#e65100")
                elif eff_ig:
                    self.lbl_meta_status.configure(text=f"✓ Standard ({eff_ig})", foreground="#1565c0")
                else:
                    self.lbl_meta_status.configure(text="✗ Needs conversion", foreground="#c62828")

                parent_dir = os.path.dirname(os.path.abspath(info.filepath))

                # Populate export history dropdown from persistent manifest
                if entry and (entry.get("history") or entry.get("latest")):
                    raw_history = list(entry.get("history", []))
                    if not raw_history and entry.get("latest"):
                        raw_history = [entry["latest"]]
                    # Reverse-chronological order: newest-to-oldest
                    history = list(reversed(raw_history))
                    self._current_history_recipes = history
                    display_items = [self._format_history_entry(r, parent_dir) for r in history]
                    self.cmb_meta_history.configure(values=display_items, state="readonly")
                    self.cmb_meta_history.current(0)
                    self._update_history_file_status(history[0], parent_dir)
                else:
                    self._current_history_recipes = []
                    self.cmb_meta_history.configure(values=["No export history"], state="disabled")
                    self.cmb_meta_history.set("No export history")
                    if hasattr(self, "lbl_history_file_status"):
                        self.lbl_history_file_status.configure(text="")

                rot_str = f"{rot}° CW" if rot != 0 else "—"
                self.lbl_meta_rotation.configure(text=rot_str)
                self.lbl_rot_status.configure(text=f"({rot}° CW)" if rot != 0 else "")

                # Load overrides or defaults
                target = overrides.get("target", self.settings.default_target or DEFAULT_TARGET)
                self.var_target.set(target)
                border_key = overrides.get("border_mode", self.settings.default_border_mode)
                self.var_border_mode.set(_BORDER_LABELS.get(border_key, "Bars (letterbox/pillarbox)"))
                self._update_thickness_state()
                if "border_pct" in overrides:
                    self.var_border_pct.set(overrides["border_pct"])
                if "fill" in overrides:
                    self.color_swatch.configure(bg=overrides["fill"])
                if "texture_path" in overrides:
                    self.var_texture_path.set(overrides["texture_path"])
                    self._update_texture_label()
                if "texture_mode" in overrides:
                    self.var_texture_mode.set(overrides["texture_mode"])

                self._schedule_preview()
        else:
            # Multiple items selected
            self.single_frame.pack_forget()
            self.multi_frame.pack(fill=tk.BOTH, expand=True)
            self.lbl_multi_count.configure(text=f"{count} files selected")

            self.multi_tree.delete(*self.multi_tree.get_children())
            for idx in self.selected_indices:
                if 0 <= idx < len(self.filtered_results):
                    info = self.filtered_results[idx]
                    overrides = self._per_image_overrides.get(info.filepath, {})
                    rot = overrides.get("rotation", 0)
                    eff_w, eff_h, eff_ratio, eff_orient, _, _ = get_effective_image_info(info, rot)
                    self.multi_tree.insert(
                        "",
                        tk.END,
                        values=(
                            info.filename,
                            eff_orient,
                            f"{eff_w}×{eff_h}",
                            f"{eff_ratio:.3f}",
                        ),
                    )

    def _format_history_entry(self, recipe: dict[str, Any], parent_dir: str = "") -> str:
        """Format an export recipe dictionary into a standardized history dropdown item.

        Converts a recorded transformation configuration from .ig_manifest.json into
        the user-specified summary format:
        'Timestamp • Target Ratio • Border Mode • Fill'
        (e.g., '2026-09-20 11:45 • 4:5 • Bars • White').
        If the output file cannot be located on disk, appends ' • [Missing on disk]'.

        Explainer:
        - Timestamp: Truncated to YYYY-MM-DD HH:MM (16 chars) replacing ISO 'T' with a space.
        - Target Ratio: Clean ratio label like '4:5', '1:1', 'Auto (5:4 / 4:5)', etc.
        - Border Mode: Maps 'bars' -> 'Bars' and 'full_border' -> 'Full Border'.
        - Fill: Displays 'Texture (<filename>)' if texture fill was used, or common color
          names ('White', 'Black') / uppercase hex code ('#FF5500') for solid colors.
        - Live Disk Check: Checks whether 'output_path' or 'output_file' exists on disk.
          If not found, appends ' • [Missing on disk]'.

        Args:
            recipe: Transformation settings dictionary from manifest history.
            parent_dir: Directory of the source image to resolve relative output paths.

        Returns:
            Concise, human-readable summary string for the History dropdown combobox.
        """
        processed_at = recipe.get("processed_at", "Unknown")
        dt = processed_at[:16].replace("T", " ") if "T" in processed_at else processed_at
        if len(dt) > 16:
            dt = dt[:16]

        target = recipe.get("target_ratio", "1:1")

        border_mode_raw = recipe.get("border_mode", "bars")
        border_label = "Bars" if border_mode_raw == "bars" else "Full Border"

        tex = recipe.get("texture_path", "")
        if tex:
            tex_name = os.path.basename(tex)
            fill_label = f"Texture ({tex_name})"
        else:
            col = recipe.get("fill_color", "#ffffff").lower()
            if col in ("#ffffff", "white"):
                fill_label = "White"
            elif col in ("#000000", "black"):
                fill_label = "Black"
            else:
                fill_label = col.upper()

        summary = f"{dt} • {target} • {border_label} • {fill_label}"

        # Check if output file physically exists on disk
        out_path = recipe.get("output_path", "")
        out_file = recipe.get("output_file", "")
        exists = False
        if out_path and os.path.isfile(out_path):
            exists = True
        elif out_file and parent_dir and os.path.isfile(os.path.join(parent_dir, out_file)):
            exists = True

        if not exists and (out_path or out_file):
            summary += " • [Missing on disk]"

        return summary

    def _update_history_file_status(self, recipe: dict[str, Any], parent_dir: str = "") -> None:
        """Update the subtle helper note below the History dropdown indicating disk presence."""
        if not hasattr(self, "lbl_history_file_status"):
            return

        out_path = recipe.get("output_path", "")
        out_file = recipe.get("output_file", "")
        exists = False
        if out_path and os.path.isfile(out_path):
            exists = True
        elif out_file and parent_dir and os.path.isfile(os.path.join(parent_dir, out_file)):
            exists = True

        if exists:
            self.lbl_history_file_status.configure(
                text="✓ Output file on disk", foreground="#2e7d32"
            )
        else:
            self.lbl_history_file_status.configure(
                text="⚠ Output file not found on disk — select to re-export", foreground="#c62828"
            )

    def _on_history_selected(self, event: Optional[tk.Event] = None) -> None:
        """Handle selection of a recipe from the History dropdown.

        Applies historical transformation settings (target ratio, border mode,
        thickness percentage, fill color, texture path/mode, output format, quality,
        suffix, and rotation) to the active single-image controls and immediately
        refreshes the live preview canvas.

        Explainer:
        - When the user picks a historical recipe from self.cmb_meta_history,
          this callback retrieves the exact configuration snapshot from self._current_history_recipes.
        - All UI controls are updated to reflect the historical recipe.
        - Rotation is restored, triggering re-calculation of effective image dimensions,
          aspect ratio, and orientation labels, as well as refreshing the main Treeview row.
        - The restored settings are recorded into self._per_image_overrides for the selected file
          so that subsequent 'Process' runs will export using this restored recipe.
        - Finally, self._schedule_preview() generates and renders an updated live preview canvas.
        """
        if not hasattr(self, "_current_history_recipes") or not self._current_history_recipes:
            return

        idx = self.cmb_meta_history.current()
        if idx < 0 or idx >= len(self._current_history_recipes):
            return

        recipe = self._current_history_recipes[idx]

        if not self.selected_indices or len(self.selected_indices) != 1:
            return

        sel_idx = self.selected_indices[0]
        if sel_idx < 0 or sel_idx >= len(self.filtered_results):
            return

        info = self.filtered_results[sel_idx]

        # 1. Target Ratio
        target = recipe.get("target_ratio", self.settings.default_target or DEFAULT_TARGET)
        self.var_target.set(target)

        # 2. Border Mode & Thickness
        border_mode = recipe.get("border_mode", self.settings.default_border_mode)
        border_label = _BORDER_LABELS.get(border_mode, "Bars (letterbox/pillarbox)")
        self.var_border_mode.set(border_label)
        self._update_thickness_state()

        if "border_pct" in recipe:
            pct = float(recipe["border_pct"])
            self.var_border_pct.set(pct)
            self._on_thickness_change(pct)

        # 3. Fill Color
        if "fill_color" in recipe:
            self.color_swatch.configure(bg=recipe["fill_color"])

        # 4. Texture Fill
        if "texture_path" in recipe:
            self.var_texture_path.set(recipe["texture_path"])
            self._update_texture_label()
        if "texture_mode" in recipe:
            self.var_texture_mode.set(recipe["texture_mode"])

        # 5. Output Format & Quality
        if "output_format" in recipe:
            self.var_output_format.set(recipe["output_format"])
            self._update_quality_visibility()
        if "quality" in recipe:
            q = int(recipe["quality"])
            self.var_quality.set(q)
            self._on_quality_change(q)

        # 6. Suffix
        if "suffix" in recipe:
            self.var_suffix.set(recipe["suffix"])

        # 7. Rotation
        rot = int(recipe.get("rotation", 0)) % 360
        if info.filepath not in self._per_image_overrides:
            self._per_image_overrides[info.filepath] = {}
        self._per_image_overrides[info.filepath]["rotation"] = rot
        self.lbl_meta_rotation.configure(text=f"{rot}° CW" if rot != 0 else "—")
        self.lbl_rot_status.configure(text=f"({rot}° CW)" if rot != 0 else "")

        # Recalculate effective dimensions & orientation with restored rotation
        eff_w, eff_h, eff_ratio, eff_orient, eff_ig, _ = get_effective_image_info(info, rot)
        self.lbl_meta_dims.configure(text=f"{eff_w} × {eff_h}")
        self.lbl_meta_ratio.configure(text=f"{eff_ratio:.4f}")
        self.lbl_meta_orientation.configure(text=eff_orient)

        # Update Treeview row display
        values, tags = self._get_display_row_values_and_tags(info)
        if self.tree.exists(str(sel_idx)):
            self.tree.item(str(sel_idx), values=values, tags=tags)

        # 8. Record in per-image overrides so export worker uses these exact settings
        self._per_image_overrides[info.filepath]["target"] = target
        self._per_image_overrides[info.filepath]["border_mode"] = border_mode
        if "border_pct" in recipe:
            self._per_image_overrides[info.filepath]["border_pct"] = float(recipe["border_pct"])
        if "fill_color" in recipe:
            self._per_image_overrides[info.filepath]["fill"] = recipe["fill_color"]
        if "texture_path" in recipe:
            self._per_image_overrides[info.filepath]["texture_path"] = recipe["texture_path"]
        if "texture_mode" in recipe:
            self._per_image_overrides[info.filepath]["texture_mode"] = recipe["texture_mode"]
        if "output_format" in recipe:
            self._per_image_overrides[info.filepath]["output_format"] = recipe["output_format"]
        if "quality" in recipe:
            self._per_image_overrides[info.filepath]["quality"] = int(recipe["quality"])
        if "suffix" in recipe:
            self._per_image_overrides[info.filepath]["suffix"] = recipe["suffix"]

        # 9. Update live helper note indicating whether output file is on disk
        parent_dir = os.path.dirname(os.path.abspath(info.filepath))
        self._update_history_file_status(recipe, parent_dir)

        # 10. Trigger immediate live preview update
        self._schedule_preview()

    def _schedule_preview(self) -> None:
        """Debounce preview generation."""
        if self._preview_job_id:
            self.root.after_cancel(self._preview_job_id)
        self._preview_job_id = self.root.after(_PREVIEW_DEBOUNCE_MS, self._generate_preview)

    def _generate_preview(self) -> None:
        """Generate and display preview for the selected single image."""
        self._preview_job_id = None
        if len(self.selected_indices) != 1:
            self._clear_preview()
            return

        idx = self.selected_indices[0]
        if idx < 0 or idx >= len(self.filtered_results):
            self._clear_preview()
            return

        info = self.filtered_results[idx]
        target = self.var_target.get()
        border_label = self.var_border_mode.get()
        border_mode = _BORDER_MODES.get(border_label, "bars")
        border_pct = self.var_border_pct.get()
        fill = self.color_swatch.cget("bg")
        texture_path = self.var_texture_path.get()
        texture_mode = self.var_texture_mode.get()
        rotation = self._per_image_overrides.get(info.filepath, {}).get("rotation", 0)

        # Save override for this image
        if info.filepath not in self._per_image_overrides:
            self._per_image_overrides[info.filepath] = {}
        self._per_image_overrides[info.filepath]["target"] = target
        self._per_image_overrides[info.filepath]["border_mode"] = border_mode

        # Generate preview with rotation
        preview_img = generate_preview(
            info.filepath,
            target_ratio_label=target,
            border_mode=border_mode,
            border_pct=border_pct,
            fill=fill,
            texture_path=texture_path,
            texture_mode=texture_mode,
            max_size=_PREVIEW_MAX,
            rotation=rotation,
        )

        if preview_img:
            self._photo_preview = ImageTk.PhotoImage(preview_img, master=self.preview_canvas)
            self.preview_canvas.delete("all")
            cw = self.preview_canvas.winfo_width()
            ch = self.preview_canvas.winfo_height()
            cx = cw // 2 if cw > 10 else _PREVIEW_MAX // 2
            cy = ch // 2 if ch > 10 else _PREVIEW_MAX // 2
            self.preview_canvas.create_image(cx, cy, image=self._photo_preview, anchor="center")
        else:
            self._clear_preview()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        """Keep preview centered when canvas is resized."""
        if self._photo_preview and self.preview_canvas.find_all():
            cx = event.width // 2
            cy = event.height // 2
            for item in self.preview_canvas.find_all():
                self.preview_canvas.coords(item, cx, cy)

    def _clear_preview(self) -> None:
        """Clear the preview canvas and metadata."""
        self.preview_canvas.delete("all")
        self._photo_preview = None
        self.lbl_meta_filename.configure(text="—")
        self.lbl_meta_dims.configure(text="—")
        self.lbl_meta_ratio.configure(text="—")
        self.lbl_meta_orientation.configure(text="—")
        self.lbl_meta_status.configure(text="—", foreground="")
        if hasattr(self, "lbl_meta_rotation"):
            self.lbl_meta_rotation.configure(text="—")
        if hasattr(self, "cmb_meta_history"):
            self._current_history_recipes = []
            self.cmb_meta_history.configure(values=["No export history"], state="disabled")
            self.cmb_meta_history.set("No export history")
        elif hasattr(self, "lbl_meta_history"):
            self.lbl_meta_history.configure(text="—")
        if hasattr(self, "lbl_history_file_status"):
            self.lbl_history_file_status.configure(text="")
        if hasattr(self, "lbl_rot_status"):
            self.lbl_rot_status.configure(text="")

    # -----------------------------------------------------------------------
    # Image Rotation
    # -----------------------------------------------------------------------

    def _rotate_cw(self) -> None:
        """Rotate currently selected image(s) 90 degrees clockwise."""
        self._apply_rotation_delta(90)

    def _rotate_ccw(self) -> None:
        """Rotate currently selected image(s) 90 degrees counter-clockwise."""
        self._apply_rotation_delta(-90)

    def _reset_rotation(self) -> None:
        """Reset rotation for selected image(s) back to 0 degrees."""
        if not self.selected_indices:
            return

        for idx in self.selected_indices:
            if 0 <= idx < len(self.filtered_results):
                info = self.filtered_results[idx]
                if info.filepath in self._per_image_overrides:
                    self._per_image_overrides[info.filepath]["rotation"] = 0

                values, tags = self._get_display_row_values_and_tags(info)
                if self.tree.exists(str(idx)):
                    self.tree.item(str(idx), values=values, tags=tags)

        self._update_selection_view()
        if len(self.selected_indices) == 1:
            self._schedule_preview()

    def _apply_rotation_delta(self, delta: int) -> None:
        """Apply a rotation delta (+90 or -90) to currently selected image(s)."""
        if not self.selected_indices:
            return

        for idx in self.selected_indices:
            if 0 <= idx < len(self.filtered_results):
                info = self.filtered_results[idx]
                if info.filepath not in self._per_image_overrides:
                    self._per_image_overrides[info.filepath] = {}
                current_rot = self._per_image_overrides[info.filepath].get("rotation", 0)
                new_rot = (current_rot + delta) % 360
                self._per_image_overrides[info.filepath]["rotation"] = new_rot

                # Update row in main Treeview with effective dimensions & orientation
                values, tags = self._get_display_row_values_and_tags(info)
                if self.tree.exists(str(idx)):
                    self.tree.item(str(idx), values=values, tags=tags)

        self._update_selection_view()
        if len(self.selected_indices) == 1:
            self._schedule_preview()

    def _on_tree_right_click(self, event: tk.Event) -> None:
        """Show context menu on right-click on tree row."""
        row_id = self.tree.identify_row(event.y)
        if row_id:
            if row_id not in self.tree.selection():
                self.tree.selection_set(row_id)
                self.selected_indices = [int(row_id)]
                self._update_selection_view()
            self.context_menu.tk_popup(event.x_root, event.y_root)

    # -----------------------------------------------------------------------
    # Settings Changes & Overrides (applied to all selected items)
    # -----------------------------------------------------------------------

    def _apply_override_to_selected(self, key: str, value: Any) -> None:
        """Apply a setting change to all currently selected files."""
        for idx in self.selected_indices:
            if 0 <= idx < len(self.filtered_results):
                filepath = self.filtered_results[idx].filepath
                if filepath not in self._per_image_overrides:
                    self._per_image_overrides[filepath] = {}
                self._per_image_overrides[filepath][key] = value

    def _on_target_change(self, event: Optional[tk.Event] = None) -> None:
        target = self.var_target.get()
        self._apply_override_to_selected("target", target)
        if len(self.selected_indices) == 1:
            self._schedule_preview()

    def _on_border_change(self, event: Optional[tk.Event] = None) -> None:
        border_label = self.var_border_mode.get()
        border_mode = _BORDER_MODES.get(border_label, "bars")
        self._apply_override_to_selected("border_mode", border_mode)
        self._update_thickness_state()
        if len(self.selected_indices) == 1:
            self._schedule_preview()

    def _update_thickness_state(self) -> None:
        """Enable thickness slider only when 'Full Border + Bars' is selected."""
        border_label = self.var_border_mode.get()
        border_mode = _BORDER_MODES.get(border_label, "bars")
        state = tk.NORMAL if border_mode == "full_border" else tk.DISABLED
        if hasattr(self, "lbl_thickness_title"):
            self.lbl_thickness_title.configure(state=state)
        if hasattr(self, "scale_thickness"):
            self.scale_thickness.configure(state=state)
        if hasattr(self, "lbl_thickness_val"):
            self.lbl_thickness_val.configure(state=state)

    def _on_thickness_scale(self, val: Any = None) -> None:
        try:
            pct = float(self.var_border_pct.get())
        except (tk.TclError, ValueError):
            return
        self._apply_override_to_selected("border_pct", pct)
        if len(self.selected_indices) == 1:
            self._schedule_preview()

    def _on_thickness_change(self, *args: Any) -> None:
        try:
            val = self.var_border_pct.get()
            self.lbl_thickness_val.configure(text=f"{val:.1f}%")
        except (tk.TclError, ValueError):
            pass

    def _pick_color(self) -> None:
        """Open color chooser dialog."""
        current = self.color_swatch.cget("bg")
        result = colorchooser.askcolor(color=current, title="Choose Border Color")
        if result and result[1]:
            self._set_color(result[1])

    def _set_color(self, hex_color: str) -> None:
        """Set the fill color and update swatch."""
        self.color_swatch.configure(bg=hex_color)
        self._apply_override_to_selected("fill", hex_color)
        if len(self.selected_indices) == 1:
            self._schedule_preview()

    def _load_texture(self) -> None:
        """Open file dialog to load a texture image."""
        filetypes = [
            ("Image files", "*.jpg *.jpeg *.png *.bmp *.tiff *.tif *.webp"),
            ("All files", "*.*"),
        ]
        path = filedialog.askopenfilename(
            title="Load Texture Image", filetypes=filetypes
        )
        if path:
            self.var_texture_path.set(path)
            self._update_texture_label()
            self._apply_override_to_selected("texture_path", path)
            if len(self.selected_indices) == 1:
                self._schedule_preview()

    def _clear_texture(self) -> None:
        """Clear the loaded texture."""
        self.var_texture_path.set("")
        self._update_texture_label()
        self._apply_override_to_selected("texture_path", "")
        if len(self.selected_indices) == 1:
            self._schedule_preview()

    def _update_texture_label(self) -> None:
        """Update the texture filename label."""
        path = self.var_texture_path.get()
        if path and os.path.isfile(path):
            self.lbl_texture.configure(text=os.path.basename(path))
        else:
            self.lbl_texture.configure(text="None")

    def _on_texture_mode_change(self) -> None:
        mode = self.var_texture_mode.get()
        self._apply_override_to_selected("texture_mode", mode)
        if len(self.selected_indices) == 1:
            self._schedule_preview()

    def _on_suffix_change(self) -> None:
        suf = self.var_suffix.get()
        self._apply_override_to_selected("suffix", suf)

    # -----------------------------------------------------------------------
    # Output Format & Quality
    # -----------------------------------------------------------------------

    def _on_output_format_change(self, event: tk.Event) -> None:
        """Handle output format change — show/hide quality slider."""
        self._update_quality_visibility()
        fmt = self.var_output_format.get()
        self._apply_override_to_selected("output_format", fmt)

    def _update_quality_visibility(self) -> None:
        """Show quality slider only for formats that support it."""
        fmt = self.var_output_format.get()
        if fmt in _QUALITY_FORMATS:
            self.lbl_quality_label.configure(state=tk.NORMAL)
            self.scale_quality.configure(state=tk.NORMAL)
            self.lbl_quality_val.configure(state=tk.NORMAL)
        else:
            self.lbl_quality_label.configure(state=tk.DISABLED)
            self.scale_quality.configure(state=tk.DISABLED)
            self.lbl_quality_val.configure(state=tk.DISABLED)

    def _on_quality_change(self, *args: Any) -> None:
        """Update quality value label."""
        try:
            val = self.var_quality.get()
            self.lbl_quality_val.configure(text=str(val))
            self._apply_override_to_selected("quality", val)
        except (tk.TclError, ValueError):
            pass

    # -----------------------------------------------------------------------
    # Bulk Operations
    # -----------------------------------------------------------------------

    def _apply_bulk(self) -> None:
        """Apply bulk target & border mode to all filtered images."""
        target = self.var_bulk_target.get()
        border_label = self.var_bulk_border.get()
        border_mode = _BORDER_MODES.get(border_label, "bars")

        for info in self.filtered_results:
            if info.filepath not in self._per_image_overrides:
                self._per_image_overrides[info.filepath] = {}
            self._per_image_overrides[info.filepath]["target"] = target
            self._per_image_overrides[info.filepath]["border_mode"] = border_mode

        # Update current selection display
        self.var_target.set(target)
        self.var_border_mode.set(border_label)
        self._update_thickness_state()
        if len(self.selected_indices) == 1:
            self._schedule_preview()

        self.lbl_progress.configure(
            text=f"Applied {target} / {border_mode} to {len(self.filtered_results)} images"
        )

    # -----------------------------------------------------------------------
    # Processing
    # -----------------------------------------------------------------------

    def _process_selected(self) -> None:
        """Process all currently selected images."""
        if not self.selected_indices:
            messagebox.showinfo("No Selection", "Please select one or more images from the list first.")
            return
        items = [
            self.filtered_results[i]
            for i in self.selected_indices
            if 0 <= i < len(self.filtered_results)
        ]
        if not items:
            return
        self._start_processing(items)

    def _process_all(self) -> None:
        """Process all filtered images."""
        mode = self.var_filter.get()
        if mode == "needs_processing":
            to_process = [
                r for r in self.filtered_results
                if (r.needs_processing and self._get_processed_status(r) != "processed")
            ]
        else:
            to_process = list(self.filtered_results)

        if not to_process:
            messagebox.showinfo("Nothing to Process", "No images to process in the current view.")
            return
        self._start_processing(to_process)

    def _start_processing(self, items: list[ImageInfo]) -> None:
        """Launch processing on a worker thread with overwrite confirmation."""
        if self._is_processing:
            return

        # Base fallback settings
        fill = self.color_swatch.cget("bg")
        texture_path = self.var_texture_path.get()
        texture_mode = self.var_texture_mode.get()
        output_format = self.var_output_format.get()
        quality = self.var_quality.get()
        suffix = self.var_suffix.get() or "_ig"
        border_pct = self.var_border_pct.get()

        # Determine destination folder configuration
        dest_same = self.var_dest_same_as_source.get()
        custom_dest = self.var_dest_folder.get().strip()
        use_custom_dest = (not dest_same) and bool(custom_dest)

        # Build job list with per-image overrides and detect conflicts
        jobs = []
        conflicts = []
        for info in items:
            overrides = self._per_image_overrides.get(info.filepath, {})
            target = overrides.get("target", self.var_target.get() or DEFAULT_TARGET)
            border_mode = overrides.get("border_mode", _BORDER_MODES.get(self.var_border_mode.get(), "bars"))
            item_border_pct = overrides.get("border_pct", border_pct)
            item_fill = overrides.get("fill", fill)
            item_texture_path = overrides.get("texture_path", texture_path)
            item_texture_mode = overrides.get("texture_mode", texture_mode)
            item_output_format = overrides.get("output_format", output_format)
            item_quality = overrides.get("quality", quality)
            item_suffix = overrides.get("suffix", suffix)
            item_rotation = overrides.get("rotation", 0)

            if use_custom_dest:
                if info.subfolder:
                    item_output_dir = os.path.join(custom_dest, info.subfolder)
                else:
                    item_output_dir = custom_dest
            else:
                item_output_dir = None

            # Calculate projected output path
            fmt_info = OUTPUT_FORMATS.get(item_output_format, OUTPUT_FORMATS.get(item_output_format.upper(), ("JPEG", True)))
            fmt_upper = str(fmt_info[0]).upper()
            output_ext = _FORMAT_EXTENSIONS.get(fmt_upper, f".{fmt_upper.lower()}")
            proj_output = build_output_path(
                info.filepath,
                suffix=item_suffix,
                output_ext=output_ext,
                output_dir=item_output_dir,
            )

            job = {
                "filepath": info.filepath,
                "target": target,
                "border_mode": border_mode,
                "border_pct": item_border_pct,
                "fill": item_fill,
                "texture_path": item_texture_path,
                "texture_mode": item_texture_mode,
                "output_format": item_output_format,
                "quality": item_quality,
                "suffix": item_suffix,
                "rotation": item_rotation,
                "output_dir": item_output_dir,
                "proj_output": proj_output,
            }
            jobs.append(job)

            if os.path.exists(proj_output):
                conflicts.append(job)

        overwrite = False
        if conflicts:
            conflict_count = len(conflicts)
            answer = messagebox.askyesnocancel(
                "Existing Files Found",
                f"{conflict_count} output file(s) already exist on disk.\n\n"
                "Do you want to overwrite existing files and update their export history?\n\n"
                "• Click 'Yes' to overwrite existing files and update history.\n"
                "• Click 'No' to skip existing files and only process new ones.\n"
                "• Click 'Cancel' to abort processing.",
            )
            if answer is None:
                # Cancel or closed
                return
            elif answer is True:
                # Yes: overwrite
                overwrite = True
            else:
                # No: skip existing
                conflict_paths = {c["filepath"] for c in conflicts}
                jobs = [j for j in jobs if j["filepath"] not in conflict_paths]
                if not jobs:
                    messagebox.showinfo(
                        "Nothing to Process",
                        "All selected files already exist and skipping was chosen.",
                    )
                    return
        else:
            overwrite = True

        self._is_processing = True
        self._process_cancel.clear()
        self.btn_process_sel.configure(state=tk.DISABLED)
        self.btn_process_all.configure(state=tk.DISABLED)
        self.btn_cancel.configure(state=tk.NORMAL)
        self.progress_var.set(0)

        no_manifest = self.var_no_manifest.get()
        thread = threading.Thread(
            target=self._process_worker, args=(jobs, overwrite, no_manifest), daemon=True
        )
        thread.start()
        self.root.after(_DRAIN_MS, self._drain_process_queue)

    def _process_worker(
        self, jobs: list[dict], overwrite: bool = False, no_manifest: bool = False
    ) -> None:
        """Worker thread: process images, save manifests (unless bypassed), and post progress to queue.

        Thread-safety rule: Never interact directly with Tkinter UI widgets from this thread.
        All progress counters and processed item events are transmitted via `self._process_queue`.
        """
        total = len(jobs)
        success_count = 0
        fail_count = 0

        for i, job in enumerate(jobs):
            if self._process_cancel.is_set():
                self._process_queue.put(("cancelled", i, total, success_count, fail_count))
                return

            output_path, ok, err = process_image(
                filepath=job["filepath"],
                target_ratio_label=job["target"],
                border_mode=job["border_mode"],
                border_pct=job["border_pct"],
                fill=job["fill"],
                texture_path=job["texture_path"],
                texture_mode=job["texture_mode"],
                output_format=job["output_format"],
                quality=job["quality"],
                suffix=job["suffix"],
                rotation=job.get("rotation", 0),
                output_dir=job.get("output_dir"),
                overwrite=overwrite,
            )

            if ok:
                success_count += 1
                recipe = {
                    "processed_at": datetime.datetime.now().isoformat(timespec="seconds"),
                    "output_file": os.path.basename(output_path),
                    "output_path": os.path.abspath(output_path),
                    "target_ratio": job["target"],
                    "border_mode": job["border_mode"],
                    "border_pct": job["border_pct"],
                    "fill_color": job["fill"],
                    "texture_path": job["texture_path"],
                    "texture_mode": job["texture_mode"],
                    "output_format": job["output_format"],
                    "quality": job["quality"],
                    "suffix": job["suffix"],
                    "rotation": job.get("rotation", 0),
                }
                # Record to persistent manifest unless No Manifest mode is enabled
                if not no_manifest:
                    try:
                        record_photo_export(job["filepath"], output_path, recipe)
                    except Exception as exc:
                        print(f"Error recording manifest for {job['filepath']}: {exc}", file=sys.stderr)

                self._process_queue.put(("item_done", job["filepath"], output_path, recipe))
            else:
                fail_count += 1

            self._process_queue.put(("progress", i + 1, total, success_count, fail_count))

        self._process_queue.put(("done", total, total, success_count, fail_count))

    def _drain_process_queue(self) -> None:
        """Drain process queue on main thread with robust exception resilience."""
        try:
            while True:
                try:
                    msg = self._process_queue.get_nowait()
                except queue.Empty:
                    break

                status = msg[0]

                if status == "item_done":
                    _, filepath, output_path, recipe = msg
                    try:
                        self._on_item_processed(filepath, output_path, recipe)
                    except Exception as exc:
                        print(f"Warning: Failed to update UI for '{filepath}': {exc}", file=sys.stderr)

                elif status == "progress":
                    _, current, total, ok, fail = msg
                    pct = (current / total * 100) if total > 0 else 0
                    self.progress_var.set(pct)
                    self.lbl_progress.configure(text=f"Processing {current}/{total}...")

                elif status in ("done", "cancelled"):
                    _, current, total, ok, fail = msg
                    self._is_processing = False
                    self.btn_process_sel.configure(state=tk.NORMAL)
                    self.btn_process_all.configure(state=tk.NORMAL)
                    self.btn_cancel.configure(state=tk.DISABLED)
                    self.progress_var.set(100 if status == "done" else (current / total * 100 if total > 0 else 0))

                    if status == "cancelled":
                        self.lbl_progress.configure(
                            text=f"Cancelled. {ok} OK, {fail} failed, {total - current} skipped"
                        )
                    else:
                        self.lbl_progress.configure(text=f"Done! {ok} processed, {fail} failed")
                        if fail > 0:
                            messagebox.showwarning(
                                "Processing Complete",
                                f"Processed {ok} images successfully.\n{fail} images failed (may already exist).",
                            )
        except Exception as exc:
            print(f"Error in _drain_process_queue: {exc}", file=sys.stderr)
        finally:
            if self._is_processing:
                self.root.after(_DRAIN_MS, self._drain_process_queue)

    def _on_item_processed(
        self, filepath: str, output_path: str, recipe: dict[str, Any]
    ) -> None:
        """Handle live UI update when a single image has been processed.

        Updates in-memory scan results and the Treeview table row in real time
        so the user receives immediate visual feedback (`✓ Processed` checkmark).
        If manifest tracking is enabled, also updates the in-memory manifest cache.

        Args:
            filepath: Absolute path to the source image.
            output_path: Absolute path to the exported image.
            recipe: Transformation settings applied to the export.
        """
        # Update manifest cache if manifest tracking is enabled
        if not self.var_no_manifest.get():
            parent_dir = os.path.dirname(os.path.abspath(filepath))
            filename = os.path.basename(filepath)
            if parent_dir in self._manifest_cache:
                photos = self._manifest_cache[parent_dir].setdefault("photos", {})
                p_entry = photos.setdefault(filename, {"latest": {}, "history": []})
                p_entry["latest"] = recipe
                p_entry.setdefault("history", []).append(recipe)
            else:
                self._manifest_cache[parent_dir] = load_folder_manifest(parent_dir)

        # Update in scan_results
        for idx, info in enumerate(self.scan_results):
            if info.filepath == filepath:
                self.scan_results[idx] = info._replace(
                    processed_status="processed",
                    latest_export=recipe,
                    needs_processing=False,
                )
                break

        # Update in filtered_results and table row
        for idx, info in enumerate(self.filtered_results):
            if info.filepath == filepath:
                updated_info = info._replace(
                    processed_status="processed",
                    latest_export=recipe,
                    needs_processing=False,
                )
                self.filtered_results[idx] = updated_info
                values, tags = self._get_display_row_values_and_tags(updated_info)
                if self.tree.exists(str(idx)):
                    self.tree.item(str(idx), values=values, tags=tags)
                break

        # If currently selected in single view, refresh selection view
        if len(self.selected_indices) == 1:
            sel_idx = self.selected_indices[0]
            if (
                0 <= sel_idx < len(self.filtered_results)
                and self.filtered_results[sel_idx].filepath == filepath
            ):
                self._update_selection_view()

    def _cancel_processing(self) -> None:
        """Signal the processing worker to stop."""
        self._process_cancel.set()

    # -----------------------------------------------------------------------
    # Settings Persistence
    # -----------------------------------------------------------------------

    def _restore_settings(self) -> None:
        """Restore UI state from loaded settings."""
        self.var_target.set(self.settings.default_target or DEFAULT_TARGET)
        self.var_no_manifest.set(self.settings.no_manifest)
        border_key = self.settings.default_border_mode
        self.var_border_mode.set(_BORDER_LABELS.get(border_key, "Bars (letterbox/pillarbox)"))
        self._update_thickness_state()
        self._update_texture_label()
        self._on_quality_change()
        self._on_thickness_change()

    def _save_settings(self) -> None:
        """Save current UI state to settings file."""
        self.settings.last_folder = self.var_folder.get()
        self.settings.recurse = self.var_recurse.get()
        self.settings.no_manifest = self.var_no_manifest.get()
        self.settings.scan_jpg = self.var_scan_jpg.get()
        self.settings.scan_png = self.var_scan_png.get()
        self.settings.scan_tiff = self.var_scan_tiff.get()
        self.settings.scan_webp = self.var_scan_webp.get()
        self.settings.scan_bmp = self.var_scan_bmp.get()
        self.settings.all_formats = all(
            v.get()
            for v in (
                self.var_scan_jpg,
                self.var_scan_png,
                self.var_scan_tiff,
                self.var_scan_webp,
                self.var_scan_bmp,
            )
        )
        self.settings.dest_same_as_source = self.var_dest_same_as_source.get()
        self.settings.dest_folder = self.var_dest_folder.get()
        self.settings.default_target = self.var_target.get() or DEFAULT_TARGET
        border_label = self.var_border_mode.get()
        self.settings.default_border_mode = _BORDER_MODES.get(border_label, "bars")
        self.settings.border_pct = self.var_border_pct.get()
        self.settings.fill_color = self.color_swatch.cget("bg")
        self.settings.texture_path = self.var_texture_path.get()
        self.settings.texture_mode = self.var_texture_mode.get()
        self.settings.output_format = self.var_output_format.get()
        try:
            self.settings.quality = self.var_quality.get()
        except (tk.TclError, ValueError):
            pass
        self.settings.suffix = self.var_suffix.get()
        self.settings.window_geometry = self.root.geometry()
        self.settings.filter_mode = self.var_filter.get()
        self.settings.save()

    def _on_close(self) -> None:
        """Handle window close — save settings and quit."""
        self._save_settings()
        self.root.destroy()



def main() -> None:
    """Entry point for the Instagram Image Formatter GUI."""
    root = tk.Tk()
    _app = IgFormatterGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
