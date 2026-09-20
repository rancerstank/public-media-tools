# Photo Tools — Instagram Image Formatter version 1
Desktop utility for batch photo optimization, aspect ratio compliance, and Instagram-ready exports.

<img width="753" height="485" alt="2026-09-20 12_48_27-Instagram Image Formatter" src="https://github.com/user-attachments/assets/71ff5415-7a6c-475a-9a45-91a9a4ac4162" />

Available as a single-file standalone script (`ig_size_formatter_v1.py`).

This program places a hidden file in each scanned folder location to build a processing manifest of each photo. This file can be deleted and processing history will be reset. You can check "No Manifest" if you don't what this file created.  

---

## 1. Overview & Key Capabilities

Instagram requires specific aspect ratios for timeline posts and stories to prevent awkward cropping:
- **1:1** (Square) — 1080 × 1080 px
- **4:5** (Portrait) — 1080 × 1350 px *(recommended for portrait posts)*
- **5:4** (Landscape) — 1350 × 1080 px *(recommended for landscape posts)*
- **9:16** (Stories / Reels) — 1080 × 1920 px
- **16:9** (Widescreen Landscape) — 1920 × 1080 px

The **Instagram Image Formatter** takes any photo and fits it cleanly into the desired Instagram canvas using customizable background padding (letterbox or pillarbox bars), uniform border frames, solid colors, or tiled image textures. Original source photos are never modified.

---

## 2. Feature Highlights

### Aspect Ratio Padding & Auto-Mode
- **Pre-set Ratios**: `1:1`, `4:5`, `5:4`, `9:16`, and `16:9`.
- **Auto (5:4 / 4:5)**: Intelligently selects `5:4` for landscape images and `4:5` for portrait images.
- **Two Border Modes**:
  - **Bars (letterbox/pillarbox)**: Expands only the narrower dimension to reach the exact target ratio.
  - **Full Border + Bars**: Applies a uniform percentage border (0% to 15%) around all four sides, then adds ratio padding bars.
- **Smart Thickness Slider**: Automatically disables and grays out when *Bars* mode is active, re-enabling only when *Full Border + Bars* is selected.

### Background Customization
- **Solid Colors**: Custom Tkinter color picker with 1-click **W** (White `#ffffff`) and **B** (Black `#000000`) presets.
- **Texture Fills**: Load any image file (wood grain, canvas, paper texture) to use as background fill with **Tile** or **Stretch** modes.

### 90° Image Rotation
- Rotate images 90° Clockwise or Counter-Clockwise directly from the UI or via right-click context menu.
- Real-time recalculation of effective dimensions, aspect ratio, orientation label, and live preview.

### Extended Multi-Selection & Bulk Actions
- Select multiple images using `Shift + Click` or `Ctrl + Click`.
- The preview panel automatically swaps into a multi-selection table displaying dimensions and aspect ratios of all highlighted files.
- Apply rotation, target ratio, border mode, color, texture, or export format to all selected images in bulk.

### Interactive Table View & Column Sorting
- Displays **Status**, **Filename**, **Dimensions**, **Ratio**, **Current IG**, **Orientation**, and **Subfolder**.
- Click any column header to sort alphabetically, numerically by pixel resolution, or by status priority. Ascending and descending sort indicators (`▲` / `▼`) appear in header labels.

### Export History & Status Tracking
- **Persistent Hidden Manifests (`.ig_manifest.json`)**: Automatically created per folder and subfolder. On Windows, files receive the hidden attribute (`0x02`) and are safely unhidden during updates.
- **1-Click History Reset**: To reset processing history and clear all cached recipes for any folder, simply delete its `.ig_manifest.json` file.
- **Optional No Manifest Mode**: Check the **No Manifest** checkbox in the top toolbar to bypass reading from and writing to `.ig_manifest.json` entirely. When enabled, images are formatted and exported cleanly without creating any manifest files on disk.
- **Running Recipe History**: Manifest stores the latest export recipe and up to 50 iterations of history per photo.
- **Dedicated Status Column with Row Tags**:
  - `✓ Processed` (Green): Image has been exported and recorded in the folder manifest.
  - `Ready` (Blue): Image already matches an Instagram aspect ratio.
  - `Needs conversion` (Red): Aspect ratio does not match standard Instagram dimensions.
- **Decoupled Output Check & Real-Time Disk Indicators**:
  - The primary table status is decoupled from physical disk presence: deleting output files from disk does not invalidate or reset the `✓ Processed` status because past recipes are preserved and can be recalled and re-exported with 1 click at any time.
  - **Single Photo Details Indicator**: A helper note directly beneath the History dropdown shows `✓ Output file on disk` (green) or `⚠ Output file not found on disk — select to re-export` (red).
  - **History Combobox Tagging**: Past recipes in the History dropdown automatically append ` • [Missing on disk]` if the physical file cannot be located on disk (e.g. `2026-09-20 11:45 • 4:5 • Bars • White • [Missing on disk]`).
  - **Hover Tooltips**: Hover over any table row to see a dark-themed card showing the export timestamp, target ratio, border mode, thickness, fill, format, quality, output file name, and disk status (`[On disk]` or `[Missing on disk — ready to re-export]`).
- **Interactive History Dropdown**: In the single-image preview details panel, the **History** field is an interactive dropdown listing all prior export recipes for the photo in reverse-chronological order (newest to oldest):
  `Timestamp • Target Ratio • Border Mode • Fill` (e.g. `2026-09-20 11:45 • 4:5 • Bars • White`). Selecting any past export recipe immediately restores all configuration settings (target ratio, border mode, border percentage, fill color or texture, output format, quality slider, suffix, and rotation) and updates the live preview canvas.
- **3-Way Filtering**: Toggle between **Needs Processing** (unprocessed non-standard photos), **Processed** (all exported files), and **Show All**.
- **3-Way Overwrite Confirmation**: When re-processing files whose outputs exist on disk, prompts **Yes** (overwrite & append history), **No** (skip existing), or **Cancel** (abort).

### Non-Destructive Export & Flexible Destinations
- Configurable filename suffix (default: `_ig`).
- Format conversion: **JPEG** (with quality slider 1–100), **PNG**, **TIFF**, **BMP**, and **WEBP**.
- Destination folder routing: "Same as source image" or custom export folder with full subfolder structure preservation.

---

## 3. Requirements

- **Python**: Version 3.9 or newer.
- **Pillow**: Python Imaging Library (`pip install Pillow`).
  - The standalone script includes an automatic prerequisite check on startup that offers to install Pillow if missing.

---

## 4. Quick Start

### Running the Standalone Script (Recommended)
Download `ig_size_formatter.py` and run:

```bash
python ig_size_formatter.py
```

---

## 5. Issues & Feature Backlog

### Fixes & Improvements
- [x] Auto (5:4 / 4:5) dynamic aspect ratio mode based on image orientation.
- [x] Extended multi-selection with detail panel swap and bulk application.
- [x] Clickable column header sorting for all table fields.
- [x] 90° CW and CCW rotation with live preview and orientation updates.
- [x] Prerequisite check with auto-install for Pillow on startup.
- [x] Compact progress bar with expanded status readout.
- [x] Individual format filter checkboxes (`JPG/JPEG`, `PNG`, `TIFF`, `WEBP`, `BMP`).
- [x] Thickness slider automatically disabled when Full Border is not active.
- [x] Hidden `.ig_manifest.json` tracking persistent export recipes and history across scans.
- [x] Status column with color-coded tags and hover recipe tooltips.
- [x] Overwrite confirmation dialog (Yes / No / Cancel).
- [x] Optional "No Manifest" checkbox to bypass reading/writing `.ig_manifest.json`.
- [x] Interactive History dropdown restoring past export settings and live preview on selection.
- [x] Decoupled disk presence checking from primary table status with real-time `[Missing on disk]` combobox and tooltip indicators.

### Planned Features
- [ ] Drag-and-drop support for folders and individual photo files into the GUI window.
- [ ] Preset profiles for quick 1-click recipe selection (e.g., "Clean White 4:5", "Framed Black 1:1").
- [ ] Optional automated EXIF metadata preservation across exports.

