# Complete Code Path Analysis: Cause Marker Click Crash

## Executive Summary
The program crashes when clicking on cause markers despite the fix in commit 5891e46. The issue is NOT in the marker click handlers themselves, but in **downstream connection-line drawing code** that accesses database row keys without proper validation.

---

## All Possible Cause Marker Interactions

### 1. Single Click on Cause Marker (Navigation Mode)

**Path:** User clicks cause marker in NAV mode on P&ID
```
PIDGraphicsView.mousePressEvent()    [line 6023]
  → save _press_pos [line 6079]
  
PIDGraphicsView.mouseReleaseEvent()  [line 6213]
  → check if small movement (< 5 pixels) [line 6341]
  → hit-test for items at click position [line 6343]
  → for each item, check if it's a cause/consequence/safeguard marker [line 6343-6348]
  → emit marker_clicked(type, id) [line 6347]

PIDPanel.viewer.marker_clicked → PIDPanel.marker_navigated [line 7234]
  → MainWindow._on_marker_navigate() [line 17215]
  → tree_panel.refresh(type_, id_) [line 17346]
  → MainWindow._on_selected(type_, id_) [line 17347]
```

**Safety:** This path is SAFE. No database queries that could crash.

---

### 2. Right-Click on Cause Marker (Context Menu)

**Path:** User right-clicks cause marker
```
PIDGraphicsView.mouseReleaseEvent()  [line 6213]
  → detects right-button drag release [line 6230]
  → if no drag occurred, show context menu [line 6250-6254]

PIDGraphicsView._show_context_menu()  [line 5473]
  → iterate scene items at click point [line 5497]
  → check if item is cause marker [line 5500]
  → if yes, add menu action "Lägg till ytterligare orsak här" [line 5504]
  → connect action to emit cause_at_marker_requested(cause_id) [line 5506]

Menu.triggered → PIDPanel.cause_at_marker_requested → PIDPanel._on_add_cause_at_marker() [line 9001]
  → get markers for current page [line 9004]
  → find marker matching cause_id [line 9005]
  → if marker found:
    → read marker['x'], marker['y'], marker.get('rect_w'), marker.get('rect_h') [lines 9009-9012]
    → query db.get_cause(cause_id) [line 9020]
    → extract fields: deviation_id, comp_tag, comp_type, node_id [lines 9022-9026]
    → emit cause_placement_requested(dev_id, comp_tag, comp_type, scene_pos, page, '') [line 9031]

MainWindow._on_cause_placement_requested() [line 17457]
  → look up dev_row = db.get_deviation(dev_id) [line 17460]
  → extract dev_row['description'] [line 17461]
  → create StandardCausesPickerPopup dialog
  → user picks a cause and clicks OK
  → _on_picked() handler executes [line 17507]
    → call place_cause_from_template() [line 17518]
```

**Safety:** SAFE until the last step (place_cause_from_template).

---

### 3. Left-Click to Place Cause Marker (Create Mode)

**Path:** User draws rubber-band in MODE_CAUSE and releases
```
PIDGraphicsView.mousePressEvent()  [line 6023]
  → if mode is MODE_CAUSE and left button, start rubber-band [line 6179-6183]

PIDGraphicsView.mouseReleaseEvent()  [line 6213]
  → if mode is MODE_CAUSE and left button [line 6258-6260]
  → convert to PDF coords [line 6280-6281]
  → extract suggested tag [line 6284-6288]
  → store drawn zone [line 6291-6292]
  → emit cause_clicked(center, current_page, suggested_tag) [line 6296]

PIDPanel._on_cause_click() [line 8318]
  → validate active node exists [line 8320-8328]
  → create ComponentPickerDialog [line 8336-8339]
  → if user cancels, return [line 8340-8341]
  → extract comp_type, tag, modes from dialog [line 8343-8345]
  → for each mode:
    → add cause to database [line 8357]
    → update cause with label and frequency [line 8362-8371]
    → add marker to database [line 8373]
    → add marker to viewer [line 8374]
  → emit cause_created(last_cause_id) [line 8378]

MainWindow._on_cause_created() [line 17177]
  → tree_panel.refresh(CAUSE_T, cid) [line 17178]
  → scenario_panel.refresh_placed() [line 17179]
```

**Safety:** SAFE. All database operations are wrapped in try/except.

---

### 4. Double-Click on Cause Marker

**Path:** User double-clicks cause marker
```
PIDGraphicsView.mouseDoubleClickEvent() [line 6194]
  → if not in MARKUP_SELECT mode, ignored
```

**Safety:** SAFE. Ignored in other modes.

---

### 5. Delete Cause via Tree Context Menu / Keyboard

**Path:** User selects cause in tree and presses Delete key
```
TreePanel.eventFilter() [line 7245-7289]
  → if key == Delete [line 7280]
  → call _delete_item(CAUSE_T, cause_id) [line 7287]
  → db.delete_cause(cause_id) [line 7298]
  → structure_changed.emit() [line 7217]

MainWindow._on_structure_changed()
  → pid_panel._load_overlays() [this is where it crashes]
```

**CRASH POINT:** This triggers _load_overlays() which draws connection lines.

---

## THE CRASH BUG: Four Vulnerable Code Paths

### Location 1: pid_viewer.py, line 8754 (CRITICAL)

```python
# In _load_overlays(), after drawing cause markers:
for cid, cpos_list in all_cons_pos.items():
    c = self.db.get_consequence(cid)
    if c and c['cause_id'] in all_cause_pos:  # ← CRASH HERE if 'cause_id' key missing
        for cpos in cpos_list:
            for capos in all_cause_pos[c['cause_id']]:
                self.viewer.add_connection_line(capos, cpos, '#c0392b')
```

**Issue:** If `db.get_consequence(cid)` returns a row without `'cause_id'` key, accessing `c['cause_id']` raises `KeyError`.

**When it happens:**
- Consequence record is corrupted or schema mismatch
- Consequence was deleted from DB but marker still references it
- Race condition during concurrent database access

---

### Location 2: pid_viewer.py, line 8761 (CRITICAL)

```python
# In _load_overlays(), drawing safeguard connections:
for sid, spos_list in all_sg_pos.items():
    s = self.db.get_safeguard(sid)
    if s and s['consequence_id'] in all_cons_pos:  # ← CRASH HERE if 'consequence_id' missing
        for spos in spos_list:
            for kpos in all_cons_pos[s['consequence_id']]:
                self.viewer.add_connection_line(kpos, spos, '#27ae60', dashed=True)
```

**Same issue:** `s['consequence_id']` raises `KeyError` if field missing.

---

### Location 3: pid_viewer.py, line 7559 (EXPORT feature)

```python
# In _export_pdf_with_markup(), drawing connections for PDF export:
for cid, cpos in cons_pos.items():
    c = self.db.get_consequence(cid)
    if c and c['cause_id'] in cause_pos:  # ← CRASH HERE
        shape.draw_line(...)
```

**Same vulnerability.**

---

### Location 4: pid_viewer.py, line 7565 (EXPORT feature)

```python
# In _export_pdf_with_markup(), drawing safeguard connections:
for sid, spos in sg_pos.items():
    s = self.db.get_safeguard(sid)
    if s and s['consequence_id'] in cons_pos:  # ← CRASH HERE
        shape.draw_line(...)
```

**Same vulnerability.**

---

## Root Cause

The database queries (`get_consequence()`, `get_safeguard()`) may return:
1. **A row object with the expected keys** (normal case)
2. **A row object with MISSING keys** (corrupted data)
3. **None** (if deleted)

The code checks `if c and ...` to catch case 3 (None), but **does not handle case 2** (missing keys).

This most likely happens when:
- A cause is deleted from the database
- Consequence markers still reference that deleted cause via `consequence.cause_id`
- When `_load_overlays()` is called, it tries to draw connection lines
- The query `get_consequence()` returns a row, but that row might have been created with inconsistent schema or corrupted referential integrity

---

## Trigger Sequence (Most Likely)

1. User creates cause → consequence → safeguard markers on P&ID
2. User deletes a cause from the tree (which should also delete its consequences)
3. Database delete doesn't cascade properly OR markers aren't cleaned up
4. User interacts with P&ID or refreshes view
5. `_load_overlays()` is called
6. It tries to draw connections between surviving consequence → cause
7. The query returns a consequence row with `cause_id=NULL` or missing
8. Line 8754: `c['cause_id']` crashes with KeyError

---

## All Affected Functions

| Function | File | Line | Issue |
|----------|------|------|-------|
| `_load_overlays()` | pid_viewer.py | 8754, 8761 | Accessing database row keys without `.get()` |
| `_export_pdf_with_markup()` | pid_viewer.py | 7559, 7565 | Accessing database row keys without `.get()` |

---

## Fix Required

Replace all four locations with safe key access:

### Location 1 (line 8754):
```python
# BEFORE:
if c and c['cause_id'] in all_cause_pos:

# AFTER:
if c and c.get('cause_id') in all_cause_pos:
```

### Location 2 (line 8761):
```python
# BEFORE:
if s and s['consequence_id'] in all_cons_pos:

# AFTER:
if s and s.get('consequence_id') in all_cons_pos:
```

### Location 3 (line 7559):
```python
# BEFORE:
if c and c['cause_id'] in cause_pos:

# AFTER:
if c and c.get('cause_id') in cause_pos:
```

### Location 4 (line 7565):
```python
# BEFORE:
if s and s['consequence_id'] in cons_pos:

# AFTER:
if s and s.get('consequence_id') in cons_pos:
```

---

## Why The Previous Fix (5891e46) Didn't Work

Commit 5891e46 only added `None` checks in hazop.py when accessing consequence/cause records:

```python
_cr = self.db.get_cause(cons['cause_id']) if cons.get('cause_id') else None
```

This addressed **one** code path (consequence detail panel), but did NOT fix the root issue in **connection-line drawing**, which happens in different code paths triggered by marker deletion, P&ID refresh, or view changes.

The crash still occurs because:
1. Deleting a cause triggers `structure_changed.emit()`
2. This calls `_load_overlays()`
3. Which tries to draw connections but crashes on line 8754 or 8761

---

## Secondary Issues Found

### Issue A: Incomplete Marker Cleanup on Delete
When deleting a cause, the code should also delete its consequence and safeguard markers. Check:
- `Database.delete_cause()` implementation
- Whether it cascades to delete consequence markers
- Whether consequence deletes cascade to safeguard markers

### Issue B: Potential Race Condition
If database queries can return rows with inconsistent schema (missing columns), this suggests:
- Schema migration issues
- Concurrent database access without proper locking
- SQLite returning partial rows on corruption

### Issue C: Missing Foreign Key Constraints
Consequence should have a foreign key to Cause with ON DELETE CASCADE. Check schema in Database class.

---

## Verification

To verify the fix works:
1. Create a node → deviation → cause on P&ID
2. Add consequence and safeguard markers for that cause
3. Delete the cause from the tree
4. Observe that program does NOT crash and markers are properly cleaned up
5. Refresh P&ID view
6. Cause marker context menu still works
7. Can place new causes without errors
