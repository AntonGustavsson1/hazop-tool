# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the application

```
python hazop.py
```

Or double-click `starta_hazop.bat` (installs dependencies automatically on Windows).

Install dependencies manually:
```
pip install PyQt6 openpyxl reportlab PyMuPDF opencv-python numpy
```

Optional OCR (for scanning scanned P&ID PDFs):
```
pip install pytesseract   # also requires Tesseract binary from https://github.com/UB-Mannheim/tesseract/wiki
pip install easyocr       # pure pip, downloads ~1 GB models on first use
```

Syntax check without running the GUI:
```
python -m py_compile hazop.py && python -m py_compile pid_viewer.py && python -m py_compile equipment_detection.py && python -m py_compile symbol_geometry.py && python -m py_compile image_symbol_matching.py
```

## Session context

**Always read `NOTES.md` at the start of every session.** It contains decisions, deferred features, known limitations and user preferences that are not derivable from the code alone.

**Always update `NOTES.md` at the end of every session** (or after each meaningful change) — add new decisions, move completed items into the implemented table, and record anything deferred. Commit it together with the changed source files.

## Crash Reporting (Automatic)

At the start of EVERY session, Claude automatically checks for new crash reports in `hazop/crashes/` and reports them:

**What happens:**
1. Crash reports are saved as JSON files: `crash_YYYYMMDD_HHMMSS_ExceptionType.json`
2. Each report contains: exception type, message, full stack trace, local variables, Python version, OS, dependency versions
3. Claude reads these automatically and says: "I found 2 new crashes since last time"
4. User can ask Claude to analyze them in detail

**Why JSON format?** Machine-readable for automated analysis, includes full diagnostic context in one file.

**See also:** `CRASH_REPORTING.md` for detailed documentation.

## Git workflow

After every meaningful change, commit and push so no work is ever lost:

```
git add hazop.py pid_viewer.py equipment_detection.py symbol_geometry.py image_symbol_matching.py CLAUDE.md   # stage only source files, not .db/.pdf/.pyc
git commit -m "short descriptive message"
git push
```

Commit message conventions used in this repo:
- `feat: <what was added>` — new feature or panel
- `fix: <what was fixed>` — bug fix
- `refactor: <what changed>` — internal restructuring without behaviour change
- `db: <what changed>` — schema or migration changes

Never commit `hazop_project.db`, `*.pdf`, `*.xlsx`, or `__pycache__/`. If a `.gitignore` does not yet exist, create one with:
```
__pycache__/
*.pyc
*.db
*.pdf
*.xlsx
```

## Architecture

The application is split into five modules:

**`image_symbol_matching.py`** — pixel/image-based "hitta liknande symbol" (no Qt, 2026-08-15, see NOTES.md "Bildbaserad 'hitta liknande symbol' — vid sidan av vektorlogiken") — renders a reference region and each candidate page to grayscale bitmaps and matches with OpenCV normalized cross-correlation (`find_similar_shapes_visual`), instead of vector geometry. Lives alongside `equipment_detection.py`'s vector-based matching, not instead of it — for CAD exports where a symbol's own strokes are too fragmented for vector clustering to group back together.

**`equipment_detection.py`** — PDF/vector analysis for equipment & valve detection (no Qt, split out of `pid_viewer.py` 2026-08-06 once this layer grew large enough on its own — see NOTES.md)
- `scan_pdf_for_equipment(pdf_doc, use_ocr, ...)` — three-pass scanner: (1) full-text regex, (2) word-by-word with positions, (3) optional OCR. Returns `{prefix: {tags, pages, positions, ocr_pages}, '_meta': {...}}`.
- `detect_equipment_and_valves(pdf_doc, tag_points, ...)` — the unified Fas 1+2 pipeline: extracts each page's vector primitives/clusters ONCE and shares them between tag-association and bow-tie shape-hunting, so the same physical valve can never be reported twice.
- `find_valve_shapes`/`detect_equipment_symbols` — older, standalone shape-only / tag-only entry points, kept for direct testability.
- `KNOWN_PREFIXES` — dict mapping P&ID tag prefixes to `(display_name, COMPONENT_TYPES_key)`. Add entries here when new prefix types need to be recognised.
- Importable standalone (`import equipment_detection`) without pulling in PyQt6 — same rationale as `symbol_geometry.py`.

**`symbol_geometry.py`** — pure vector-drawing geometry (no Qt): `extract_primitives`, `cluster_primitives`, `bowtie_score`, `find_symbol_clusters`, tag↔symbol leader-line resolution. The lower layer `equipment_detection.py` builds on.

**`pid_viewer.py`** — P&ID canvas and dialogs (Qt)
- `PIDGraphicsView` — QGraphicsView subclass handling pan/zoom, draw modes, and right-click context menu. Emits `context_action(str, QPointF, int)` for menu selections.
- `PIDPanel` — wrapper widget with toolbar. Holds the active node/cause/consequence IDs and orchestrates marker placement. Signals: `node_created`, `cause_created`, `consequence_created`, `safeguard_created`, `risk_scenario_requested`.
- `EquipmentScanDialog` / `EquipmentMarkerReviewDialog` — dialogs built on top of `equipment_detection.py`'s functions.
- Re-exports several `equipment_detection.py` names (`scan_pdf_for_equipment`, `KNOWN_PREFIXES`, `COMPONENT_TYPES`, etc.) so `hazop.py`'s existing `from pid_viewer import ...` calls keep working unchanged.

**`hazop.py`** — main window, database, all panels
- `Database` — SQLite wrapper around `hazop_project.db`. Schema is defined in `SCHEMA` string + idempotent `_migrate()`. All DB access goes through this class.
- `MainWindow` — five-page `QStackedWidget`: P&ID view (0), Worksheet (1), Equipment (2), Administration (3), Settings (4). Toggle bar buttons select pages.
- Risk matrix is stored as JSON in `app_config` table (key `'risk_matrix'`). Module-level `_current_matrix` is loaded at startup via `load_matrix(db)` and consumed by `risk_info(severity, likelihood)`.
- `effective_likelihood(base, rrf)` — reduces likelihood by `floor(log10(rrf))` steps.
- Tree types: `NODE_T=1`, `CAUSE_T=2`, `CONS_T=3`, `SG_T=4`, `DEV_T=5` (Avvikelse — between Node and Cause).

## Database schema summary

| Table | Key columns |
|---|---|
| `nodes` | `id`, `name`, `markup_points` (JSON), `markup_style` (JSON), `pid_page` |
| `deviations` | `id`, `node_id`, `description` — one per HAZOP deviation under a node |
| `causes` | `id`, `node_id`, `deviation_id`, `description`, `likelihood` |
| `consequences` | `id`, `cause_id`, `description`, `severity`, `category` |
| `safeguards` | `id`, `consequence_id`, `description`, `rrf` |
| `actions` | `id`, `consequence_id`, `description`, `responsible`, `due_date`, `status` |
| `equipment_catalog` | `id`, `tag`, `prefix`, `pid_page`, `equipment_type`, `description`, `is_ocr`, `include` |
| `equipment_types` | `prefix` (PK), `equipment_type`, `display_name` |
| `consequence_categories` | `id`, `name`, `sort_order` |
| `app_config` | `key` (PK), `value` |
| `pid_config` | `key` (PK), `value` — stores PDF path under key `'path'` |
| `cause_markers` / `consequence_markers` / `safeguard_markers` | marker positions on P&ID pages |
| `pid_page_rotation` | `physical_page` (PK), `rotation` — manual per-page rotation override (0/90/180/270), composed with the PDF's own `/Rotate` flag, see NOTES.md 2026-08-12 |

## Key design decisions

- **Likelihood lives on `causes`**, severity on `consequences`. RRF on safeguards reduces effective likelihood by `floor(log10(rrf))` steps (RRF 10 = −1, RRF 100 = −2).
- `Database.update_cause(id_, description=None, likelihood=None)` — both params optional so legacy callers passing only description still work.
- `Database.update_consequence(id_, description, severity, category='')` — no `likelihood` param (moved to causes).
- `Database.update_safeguard(id_, description, rrf=1)`.
- P&ID connection lines are drawn in `PIDPanel._load_overlays()` after all markers are placed, using `viewer.add_connection_line()`.
- OCR `_ocr_page_tesseract()` combines PSM 11 + PSM 6 results and attempts to join adjacent tokens that together form a valid tag.
- The `EquipmentPanel._scan()` always runs OCR on every page when OCR is enabled (not gated on word count), because many P&IDs have text in the native layer but tags only in vector graphics.
