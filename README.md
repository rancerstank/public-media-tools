# media-tools

Skateboarding photo cataloging, facial & image recognition, contest database, and broadcast tooling for pair programming on `stein15`.

---

## 1. Overview & Key Capabilities

1. **Skate Photo Recognition & Metadata Catalog (`skaterDB_tools.database` / `skaterDB_tools.vision_pipeline`)**:
   - Maintains unique, permanent **Skater IDs** (`SK8-0001`, `SK8-AUTO-0012`).
   - Associates names, nicknames, aliases, rider stance (**Regular** vs. **Goofy**), hometowns, Instagram handles, and sponsors with photo assets.
   - Multimodal Gemini Vision Analyzer (`gemini-2.5-flash` / `gemini-3.7-flash` via `google-genai`): detects faces, rider stance (`Regular`/`Goofy`/`Switch`/`Nollie`/`Fakie`), trick performed, obstacle type, apparel, deck graphics, and sponsor text.
   - All AI suggestions are flagged with `confidence` (0.0–1.0) and marked for 1-click human verification.

2. **Multi-Source Contest & League Ingestion (`skaterDB_tools.scrapers`)**:
   - **Skatepark of Tampa (SPoT)** (`skaterDB_tools.scrapers.spot`): Dynamic crawler and placement parser for all pages of SPoT contest results (Tampa Pro, Tampa Am, Damn Am, and local contests from 1993 to 2026).
   - **Street League Skateboarding (SLS)** (`skaterDB_tools.scrapers.sls`): Historical Super Crown champions, tour stop placements, and season standings.
   - **Professional Skateboarding League (PSL)** (`skaterDB_tools.scrapers.psl`): Live Convex API integration for teams (Wolverines, SHS, Tropics, etc.), full player rosters, and in-depth performance statistics (offensive/defensive efficiency, fakie, switch).
   - **Pre-Compiled Seed Dataset (`skaterDB_tools.seed_data`)**: Offline-ready starter database containing verified pro skaters, historical Tampa Pro winners, and league rosters.

3. **Safe Merge Engine with 1-Click Rollback (`skaterDB_tools.skater_service`)**:
   - Merges duplicate or provisional profiles (`SK8-AUTO-xxxx` into `SK8-xxxx`) by re-linking all photo appearances and contest placements, aggregating aliases and sponsors, and marking the duplicate as `merged`.
   - Records an atomic snapshot before modification into `audit_log`, allowing any merge to be **undone with 1 click** from the GUI or CLI (`unmerge <log_id>`).

4. **Multi-Criteria Search & Photo Matcher (`skaterDB_tools.search_service`)**:
   - Filter and match photos by **Skater**, **Contest** (e.g. *Tampa Pro 2024–2026*), **PSL Team**, **Trick Name**, **Stance** (Regular vs. Goofy), and **Event Folder**.

5. **Desktop Review GUI & Standalone Variant (`skaterDB_tools.gui` / `skaterDB_tools.standalone`)**:
   - Tkinter GUI adhering to `GEMINI.md` worker thread and queue draining standards.
   - Single-file standalone executable variant (`skaterDB_tools/standalone/skater_catalog_standalone.py`) with PyInstaller `sys.frozen` path anchoring.

6. **Google Automation (`google_automation`)**:
   - Google Apps Script (`skate_photo_automation.js`) to scan Google Drive root folder `17XXXbo0XG3uB55yTXxpV8YvaGrNpYi91`, mirror the catalog to Google Sheets, and tag Google Drive file descriptions with Skater IDs.

---

## 2. Sub-Projects & Tools

### `skaterDB/`
The core skateboarding photo recognition, facial/image indexing, and contest history database. See [`skaterDB/README.md`](skaterDB/README.md) for full architecture and documentation.

```powershell
# Navigate into skaterDB
cd skaterDB

# Run CLI commands
python scripts/skate_catalog_cli.py list-skaters
python scripts/skate_catalog_cli.py search --skater "Nyjah Huston"

# Launch desktop GUI
python -m skaterDB_tools.gui.app
```

---

## 3. Database Schema (`data/skate_catalog.db`)

- **`skaters`**: `skater_id` (PK), `name`, `first_name`, `last_name`, `nickname`, `stance`, `hometown`, `nationality`, `instagram`, `sponsors`, `psl_team_name`, `tampa_pro_wins`, `tampa_am_wins`, `sls_wins`, `total_podiums`, `status` (`verified`, `provisional`, `unidentified`, `discarded`, `merged`), `merged_into_id`, `notes`, timestamps.
- **`tricks`**: `trick_name` (PK), `category` (`flip`, `grind`, `slide`, `grab`, `aerial`, `plant`, `transition`, `learned`), `aliases` (JSON array), `description`, `created_at`.
- **`shops`**: `shop_id` (PK), `name`, `aliases` (JSON), `address`, `city`, `state`, `country`, `zip_code`, `phone`, `email`, `website`, `instagram`, `google_maps_url`, `logo_url`, `description`, `notes`, timestamps.
- **`sponsors`**: `sponsor_id` (PK), `name`, `category` (`shoe`, `truck`, `wheel`, `bearing`, `grip`, `clothing`, `energy`, `shop`, `general`), `aliases` (JSON), `parent_company`, `linked_shop_id` (FK -> shops.shop_id), `website`, `instagram`, `logo_url`, `description`, `notes`, timestamps.
- **`locations`**: `location_id` (PK), `name`, `short_name`, `city`, `state`, `country`, `is_indoor`, `aliases` (JSON), `description`, `created_at`.
- **`contest_results`**: `result_id` (PK), `skater_id` (FK), `organization`, `contest_name`, `contest_year`, `division`, `place`, `score`, `source_url`.
- **`league_stats`**: `stat_id` (PK), `skater_id` (FK), `league`, `season`, `team_name`, `stats_json`.
- **`photos`**: `photo_id` (PK), `drive_file_id` (UNIQUE), `drive_file_name`, `drive_folder_path`, `location_id`, `location_name`, `web_view_link`, `thumbnail_link`, `date_taken`, `processed_status`, `detected_count`.
- **`appearances`**: `appearance_id` (PK), `photo_id` (FK), `skater_id` (FK), `bounding_box`, `confidence`, `verified_by_user`, `stance_observed`, `trick_name`, `obstacle_type`, `visual_attributes`, `notes`.
- **`articles`**: `article_id` (PK), `source`, `title`, `author`, `byline_raw`, `url` (UNIQUE), `published_date`, `location_name`, `skaters_mentioned`, `tricks_mentioned`, `image_urls`, `content_summary`.
- **`audit_log`**: `log_id` (PK), `action_type`, `source_id`, `target_id`, `photo_id`, `details_json` (full state snapshot for undo), `timestamp`.