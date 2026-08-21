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

Syntax check without running the GUI (all modules):
```
python -m py_compile constants.py database.py ui_helpers.py tree_panel.py node_markup.py worksheet.py scenario_panel.py equipment_panel.py settings_panels.py standard_causes_panel.py standard_objects_panel.py tag_memory_panel.py participant_matrix_panel.py hazop_preparation_panel.py hazop.py pid_viewer.py pid_graphics_view.py pid_panel_mod.py equipment_detection.py symbol_geometry.py image_symbol_matching.py
```

## Testing during iterative development (2026-08-18, files split 2026-08-20)

The regression suite (818 tests) is split into 14 per-module files —
`test_database.py`, `test_scenario_panel.py`, `test_pid_viewer.py`,
`test_pid_panel_mod.py`, `test_pid_graphics_view.py`, `test_tree_panel.py`,
`test_equipment_panel.py`, `test_equipment_detection.py`,
`test_settings_panels.py`, `test_worksheet.py`, `test_node_markup.py`,
`test_hazop.py`, `test_ui_helpers.py`, and `test_integration.py` (MainWindow/
cross-panel glue tests that genuinely span multiple modules — don't try to
force one of these into a single-module file) — plus `test_helpers.py` for
shared fixtures (`_ensure_qapp`, `_TempDbMainWindow`, `_find_tree_item`,
`_menu_action_labels`, `_fake_pdf_loaded`). The old monolithic
`test_regression.py` (18,611 lines, 136 `TestCase` classes) was mechanically
split by class boundary — see NOTES.md "Dela upp test_regression.py i
per-modul testfiler" for how the class→file mapping was chosen. Running the
full set still takes ~4-5 minutes total — too slow after every single edit.
Use a tiered approach instead:

1. **After every code change** (the default — do this, not the full suite):
   ```
   python -m unittest test_smoke -v
   ```
   ~11 tests, runs in well under a second. Seeds a small but REALISTIC
   dataset (a node/deviation/cause/consequence/safeguard, a revision with
   `pdf_path` set, an equipment item) and constructs every major panel
   against it — including clicking every node-markup/red-markup tool
   button for real. This is deliberate, not arbitrary: every real crash
   found during the 2026-08-17/18 module-split session (`_StylePopup`,
   `ConsCategoryMatrixPopup`, missing `equipment_detection` OCR helpers,
   `pathlib.Path` in `PIDManagementPanel.refresh()`) only triggered
   against real data or a real button click — an empty-DB construction-only
   smoke test would have missed every one of them. If you add a new panel
   or a new "only crashes with real data" code path, add a case here too.
   **Gap to be aware of:** this does NOT exercise deep interaction paths
   like OCR/tag-scanning — those still need the full suite.
2. **When a change is confined to one module, run just that module's test
   file** (seconds, not minutes) instead of the full 14-file suite — e.g.
   a `scenario_panel.py` tweak only needs:
   ```
   python -m unittest test_scenario_panel
   ```
   This is the main payoff of the 2026-08-20 split — previously "targeted
   testing" still meant loading/grepping one 18,611-line file to find the
   right class; now it's a small, purpose-scoped file.
3. **Run the full suite when the change is actually large/risky, or
   whenever asked for real confidence** — NOT automatically just because
   you're about to commit (2026-08-20 follow-up: a one-line non-bold-font
   tweak still triggered a full ~5.5-minute/818-test run before commit;
   "Att du kör full regression test är bra i vissa fall men här gör du en
   väldigt liten ändring... begränsa full regression test"):
   ```
   python -m unittest test_database test_scenario_panel test_pid_viewer test_pid_panel_mod test_pid_graphics_view test_tree_panel test_equipment_panel test_equipment_detection test_settings_panels test_worksheet test_node_markup test_hazop test_ui_helpers test_integration
   ```
   (Explicit file list, not `unittest discover` — discover would also sweep
   in `test_smoke.py`/`test_symbol_geometry.py`/`test_image_symbol_matching.py`,
   which are run separately.) A small, well-isolated change (a single-line
   tweak, a cosmetic/styling fix, a config value change, anything confined
   to one obviously-isolated code path with no fan-out) should commit after
   `test_smoke` + the specific module test file covering the change — skip
   the full suite. Reserve it for new features, multi-file refactors,
   changes touching shared/widely-reused code paths, or anything whose
   blast radius isn't obvious. When unsure which bucket a change falls in,
   default to targeted + smoke and say so in the summary, rather than
   silently upgrading to a full run "just in case."
4. **Keep writing full regression tests as before** whenever you fix a bug
   or add a feature, in whichever of the 14 files matches the module you
   changed (or `test_integration.py` for cross-panel behavior) —
   `test_smoke.py` is a fast pre-check, not a replacement for their
   thoroughness.

Note on PyQt6 exceptions raised inside a signal/slot call (e.g.
`button.click()`, not a direct method call): by default PyQt6 prints them
via `sys.excepthook` and then **aborts the whole process** instead of
letting the caller catch them — a bug there can silently kill an entire
test run rather than failing one test. Swap in a capturing `sys.excepthook`
for the duration of the click (see `test_smoke.py`'s
`_click_every_tool_button`) if you write a test that clicks a button.

## Session context

**Always read `NOTES.md` at the start of every session.** It contains decisions, deferred features, known limitations and user preferences that are not derivable from the code alone. Session logs older than 2026-08-17 live in `NOTES_ARCHIVE.md` (moved there 2026-08-20 to keep NOTES.md short — it's read in full every session) — only open it when you need historical context from before that date.

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
git add <changed .py files> CLAUDE.md   # stage only source files, not .db/.pdf/.pyc
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

The application was originally two files (`hazop.py`, `pid_viewer.py`) that grew into ~22,000 and ~11,000 line "god files". Both were split into layered modules 2026-08-17/18 (see NOTES.md "Förenkla koden + dela upp hazop.py i fler filer") using a **layer + re-export** pattern throughout: every module only imports from layers *below* it, and each layer re-exports the names its callers already relied on, so `from hazop import X` / `from pid_viewer import Y` keep working unchanged regardless of which file `X`/`Y` now actually lives in. The test suite (then still one monolithic `test_regression.py`) needed essentially zero changes as a result — it still imported everything the same way it always did. (That file was itself later split into 14 per-module files 2026-08-20 — see the Testing section above.)

Import layers, lowest first (each layer imports only from layers above it in this list):

1. **`constants.py`** — pure Python, zero imports. `CONFIG` (magic-number dict), `NODE_T`/`CAUSE_T`/`CONS_T`/`SG_T`/`DEV_T`/`EQUIP_T`/`LEDORD_T` (tree-item type tags), `DEVIATION_TYPES`, `MARKUP_COLORS`, `RISK_ICON`, `SG_TYPES`, `RRF_VALUES`/`RRF_LABELS`, `SEV_LABELS`.
2. **`symbol_geometry.py`**, **`image_symbol_matching.py`**, **`equipment_detection.py`** — the pre-existing no-Qt PDF/vector analysis layer (unchanged by this split; see their own entries below).
3. **`database.py`** — the `Database` class (SQLite wrapper, ~4000 lines, still 100% Qt-free) plus `SCHEMA`, the risk-matrix cache (`load_matrix`/`get_matrix`/`risk_info`/`_normalise_matrix`/`DEFAULT_MATRIX`), `freq_to_f_level`, seed/migrate helpers, and a few small tag-text helpers (`append_tag_to_text`/`parse_tag_refs`/`add_tag_ref`) that the `Database` class itself calls.
4. **`pid_viewer.py`** (base layer, ~5100 lines after the split) — P&ID-related dialogs, `QThread` workers, small canvas-item classes, and shared module-level constants/helpers (`MODE_*` draw-mode constants, `Z_*` z-order constants, `CONFIG`, `_icon`/`_mk_icon`/`_mk_pm`, `_get_red_symbol_svg`, OCR helpers, etc.). Re-exports `equipment_detection.py` names so old `from pid_viewer import scan_pdf_for_equipment` calls keep working.
5. **`pid_graphics_view.py`** — `PIDGraphicsView`, moved out **whole** (not split internally — its ~3000 lines are already organized into clearly named method groups; breaking a single class's methods across files would need mixin inheritance, a materially bigger and riskier change). Imports shared constants back from `pid_viewer.py`.
6. **`pid_panel_mod.py`** — `PIDPanel` + `EquipmentDeviationBar`, also moved whole. Imports `PIDGraphicsView` from `pid_graphics_view.py` and shared constants/dialogs from `pid_viewer.py`.
   - `pid_viewer.py` imports `PIDGraphicsView`/`PIDPanel`/`EquipmentDeviationBar` back from these two files, but only at the **bottom** of the file (after every name they depend on is already defined) — putting it at the top would be a circular import.
7. **`ui_helpers.py`** — small Qt-dependent (but widget-independent) functions shared across panels: `freq_axis_label`/`freq_axis_label_full`/`cons_axis_label`, `_equipment_type_options`/`_EQ_TYPE_ITEMS`, `_lookup_comp_type_for_tag`/`_make_tag_completer`/`_resolve_std_deviation_id`/`_create_cause_from_pick`/`_maybe_save_as_standard_cause`, `find_tag_bold_ranges`/`_draw_text_with_bold_tags`, `total_freq_reduction`/`CHAIN_ITEMS`/`build_consequence_text`/`parse_chain_from_json`.
8. **`tree_panel.py`** — `TreePanel` (the HAZOP hierarchy tree) plus its cause/deviation picker dialogs (`StandardCausesPickerPopup`, `CauseObjectPopup`, `CauseTagPopup`, `RRFPopup`, `FrequencyPickerPopup`, `DeviationPickerPopup`).
9. **`node_markup.py`** — `PropertiesRibbon` (the right-side detail ribbon; also carries the P&ID node-markup toolbar for NODE_T since 2026-08-19, see NOTES.md "Slå ihop nodmarkup i nodinställningar" — the old, separate `NodeMarkupPanel` widget is gone), `MarkupTablePanel`, `RedMarkupPanel`, `RedMarkupTablePanel` and their style/symbol-picker dialogs.
10. **`worksheet.py`** — `HAZOPWorksheet` (the "Blad" worksheet page). Its `__init__` does a deferred `from hazop import ScenarioTablePanel` (not a module-level import) since `ScenarioTablePanel` itself is defined further up the layer graph, in a module that in turn imports `HAZOPWorksheet` from here — the same circular-import-avoidance pattern as `pid_viewer.py`'s bottom-of-file re-export.
11. **`scenario_panel.py`** — `ScenarioTablePanel` (the HAZOP scenario table — the single biggest extracted class, ~5000 lines) plus its cluster of delegates/popups (`RiskMatrixPopup`, `ConsequenceChainDialog`, `ConsequenceStepPickerDialog`, `ReductionFactorsDialog`, `_ScenarioDelegate`, `_PidDelegate`, `SgRRFCategoryPopup`, `CatSGSelectionPopup`, `ConsCategoryMatrixPopup`, `_LopaWidget`). Its `_open_recommendation_editor` does a deferred `from hazop import RecommendationEditorDialog` for the same reason as `worksheet.py` above.
12. **`equipment_panel.py`** — `EquipmentPanel` (the equipment register), `_EquipmentTableModel`/`_EquipmentFilterProxy`, `ComponentEditorPanel`, `ObjectPickerPopup`, `EquipmentTagPopup`, `PIDAnalysisPanel`, `TagDatabasePanel`, `_IdentifiedTagsModel`.
13. **`settings_panels.py`** — umbrella module: `SettingsPanel`, `SeverityDefinitionsPanel` (currently unused — no live construction call site, kept rather than deleted since nothing has confirmed it's safe to remove), `PIDManagementPanel`, `StudyManagementPanel` (alias `AdminPanel`) live here directly; `HAZOPPreparationPanel`, `StandardCausesSettingsPanel`, `StandardObjectsSettingsPanel`, `TagMemoryPanel`, `ParticipantMatrixPanel` were split out 2026-08-21 (see NOTES.md "Dela upp settings_panels.py" — same layer + re-export pattern as everywhere else in this list) into their own files, listed as 13a–13e below, and re-exported from here so `hazop.py`'s `from settings_panels import ...` needed zero changes.
    - 13a. **`standard_causes_panel.py`** — `StandardCausesSettingsPanel`.
    - 13b. **`standard_objects_panel.py`** — `StandardObjectsSettingsPanel`.
    - 13c. **`tag_memory_panel.py`** — `TagMemoryPanel`.
    - 13d. **`participant_matrix_panel.py`** — `ParticipantMatrixPanel` + its private `_InlineHeaderEdit`.
    - 13e. **`hazop_preparation_panel.py`** — `HAZOPPreparationPanel` (Projekt/Deltagare/Riskmatris & Kategorier/Avvikelser & Orsaker tabs) + its private `DraggableColorSwatch`/`MatrixCellButton` drag-and-drop helpers. Imports `ParticipantMatrixPanel` (13d) and `StandardCausesSettingsPanel` (13a) directly, since `HAZOPPreparationPanel` hosts both as tabs.
14. **`hazop.py`** (~3050 lines, down from ~22,000) — `MainWindow`, `SplashScreen`, `CrashReporter`, `GlobalSearchDialog`, `ActionEditor`/`RecommendationEditorDialog`, and a handful of small helpers not yet worth their own module. Imports and re-exports everything from layers 1–13 above, so all pre-existing `from hazop import X` call sites (including throughout the `test_*.py` suite) continue to work unchanged.

**A recurring gotcha worth knowing if you move code between these files again:** tests that do `unittest.mock.patch('hazop.SomeClass', ...)` or `patch('pid_viewer.SomeClass', ...)` to intercept a constructor call only work if the code doing the constructing is *in that same module*. Moving a class to a new file without updating the string in a matching `patch(...)` call doesn't raise an error — the patch just silently stops intercepting anything, and a real (often modal, `.exec()`-blocking) dialog gets constructed instead, which can hang the test suite rather than fail it cleanly. `patch.object(hazop.SomeClass, 'method', ...)` and `patch('hazop.SomeClass.method', ...)` are **not** affected by this, since they mutate the shared class object itself rather than a per-module name binding — safe to leave pointed at any module that re-exports the class.

### Known traps (found the hard way — read before touching related code)

- **A spanned QTableWidget cell does NOT mean only one physical row has real content.** `ScenarioTablePanel._apply_spans()`'s `setSpan(anchor_row, col, span, 1)` only changes how Qt *paints* the covered rows — `_add_row()` still calls `setItem`/`setCellWidget` on **every** physical row in the group (ORS/KON/LOPA/REK all get their own fresh, duplicate item/widget per row, with identical content). `table.item(row, col) is not None` and `table.cellWidget(row, col) is not None` are therefore **always true** and useless for asking "does this row have its own content, or is it a covered continuation?" — this exact mistake caused three separate real bugs in the 2026-08-18/19 safeguard-row-height work (a "compaction" fix that silently never compacted anything, a floor-reapplication pass that undid the fix that came after it, and a shared-requirement — LOPA widget height, ORS readability floor — that landed entirely on the anchor row instead of being divided across the span). The only reliable signal is comparing each column's own span key (`_row_meta`/`_row_cat_info`, keyed by cause_id/cons_id/(cons_id, cat_id) depending on the column) against the previous row. See `ScenarioTablePanel._compute_row_height()`'s own docstring for the full reasoning and the `_span_group_size()` helper this pattern now goes through.
- **PyQt6's `.connect(slot, Qt.ConnectionType.UniqueConnection)` raises `TypeError` on a duplicate attempt** — unlike C++ Qt, which just returns `False`. If a code path can legitimately be entered more than once without an intervening disconnect (e.g. `PIDPanel._enter_markup_mode`, re-enterable via the `PropertiesRibbon` markup toggle), guard the connect call with `try/except TypeError: pass`, not just the `UniqueConnection` flag alone — verified empirically, not from memory, before relying on it.

- `Database` — SQLite wrapper around `hazop_project.db`, in `database.py` (see layer 3 above). Schema is defined in `SCHEMA` string + idempotent `_migrate()`. All DB access goes through this class.
- `MainWindow` — six-page main-content stack: HAZOP-förberedelse (0), P&ID view (1), Worksheet (2), Equipment (3), Administration (4), Settings (5). Nav-rail buttons select pages.
- Risk matrix is stored as JSON in `app_config` table (key `'risk_matrix'`). Module-level `_risk_matrix_cache` (in `database.py`) is loaded at startup via `load_matrix(db)` and consumed by `risk_info(severity, likelihood)`.
- `effective_f_level(f_level, rrf)` (in `ui_helpers.py`; aliased as `effective_frequency`/`effective_likelihood`) — reduces F-level by `floor(log10(rrf))` steps.
- Tree types (in `constants.py`): `NODE_T=1`, `CAUSE_T=2`, `CONS_T=3`, `SG_T=4`, `DEV_T=5` (Avvikelse — between Node and Cause), `EQUIP_T=6`, `LEDORD_T=7` (guide-word grouping level, no DB row of its own).

**`image_symbol_matching.py`** — pixel/image-based "hitta liknande symbol" (no Qt, 2026-08-15, see NOTES.md "Bildbaserad 'hitta liknande symbol' — vid sidan av vektorlogiken") — renders a reference region and each candidate page to grayscale bitmaps and matches with OpenCV normalized cross-correlation (`find_similar_shapes_visual`), instead of vector geometry. Lives alongside `equipment_detection.py`'s vector-based matching, not instead of it — for CAD exports where a symbol's own strokes are too fragmented for vector clustering to group back together.

**`equipment_detection.py`** — PDF/vector analysis for equipment & valve detection (no Qt, split out of `pid_viewer.py` 2026-08-06 once this layer grew large enough on its own — see NOTES.md)
- `scan_pdf_for_equipment(pdf_doc, use_ocr, ...)` — three-pass scanner: (1) full-text regex, (2) word-by-word with positions, (3) optional OCR. Returns `{prefix: {tags, pages, positions, ocr_pages}, '_meta': {...}}`.
- `detect_equipment_and_valves(pdf_doc, tag_points, ...)` — the unified Fas 1+2 pipeline: extracts each page's vector primitives/clusters ONCE and shares them between tag-association and bow-tie shape-hunting, so the same physical valve can never be reported twice.
- `find_valve_shapes`/`detect_equipment_symbols` — older, standalone shape-only / tag-only entry points, kept for direct testability.
- `KNOWN_PREFIXES` — dict mapping P&ID tag prefixes to `(display_name, COMPONENT_TYPES_key)`. Add entries here when new prefix types need to be recognised.
- Importable standalone (`import equipment_detection`) without pulling in PyQt6 — same rationale as `symbol_geometry.py`.

**`symbol_geometry.py`** — pure vector-drawing geometry (no Qt): `extract_primitives`, `cluster_primitives`, `bowtie_score`, `find_symbol_clusters`, tag↔symbol leader-line resolution. The lower layer `equipment_detection.py` builds on.

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
