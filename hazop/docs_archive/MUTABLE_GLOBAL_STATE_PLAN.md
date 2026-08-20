# Implementation Plan: Mutable Global State & Resource Cleanup

## Executive Summary

Three distinct resource lifecycle management issues exist in the HAZOP application:

1. **OCR Reader Caching** — Global readers stay in memory at app exit (partially mitigated by existing `_OCRLifecycleManager`)
2. **Risk Matrix Cache Invalidation** — Manual cache refresh required after DB writes; not automatic
3. **Database Connection Leak** — sqlite3 connection in Database class has no cleanup mechanism

This plan provides step-by-step fixes with patterns suitable for each issue.

---

## Issue 1: OCR Reader Caching

### Current State

**Location:** `pid_viewer.py` lines 88–159

The code **already has a partially-implemented solution:**

```python
class _OCRLifecycleManager:
    """Singleton managing OCR reader lifecycle with automatic cleanup."""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance
    
    def _init(self):
        self._easyocr_reader = None
        self._rapidocr_instance = None
    
    def get_easyocr_reader(self): ...
    def get_rapidocr_instance(self): ...
    def cleanup(self): ...
    def __del__(self): ...

_ocr_manager = _OCRLifecycleManager()
```

**Old globals still exist (but unused):**
- Line 89: `_rapidocr_instance = None`
- Line 97: `_easyocr_reader_cache = None`
- Line 100–104: `_get_easyocr_reader()` function

### Problems

1. Old module-level globals `_rapidocr_instance` and `_easyocr_reader_cache` are dead code
2. `_get_easyocr_reader()` function is not used (superseded by `_ocr_manager.get_easyocr_reader()`)
3. No explicit cleanup call at app exit
4. `_ocr_manager` singleton is never cleaned up — relies on Python GC at process termination

### Solution: Three-Step Fix

#### Step 1: Remove dead code (lines 88–104)

Delete:
```python
_rapidocr_instance = None
_easyocr_reader_cache = None

def _get_easyocr_reader():
    global _easyocr_reader_cache
    if _easyocr_reader_cache is None and HAS_EASYOCR:
        _easyocr_reader_cache = _easyocr_module.Reader(['en'], gpu=False, verbose=False)
    return _easyocr_reader_cache
```

**Rationale:** Dead code clutters API surface. `_ocr_manager` is the canonical source.

#### Step 2: Export cleanup function (pid_viewer.py)

After the `_OCRLifecycleManager` class definition, add:

```python
def cleanup_ocr_resources():
    """Public API to release OCR readers at app shutdown.
    
    Called from MainWindow.closeEvent() or directly before sys.exit().
    Safe to call multiple times.
    """
    _ocr_manager.cleanup()
```

#### Step 3: Add MainWindow cleanup hook (hazop.py)

Add to MainWindow class (around line 16406):

```python
def closeEvent(self, event):
    """Cleanup resources before window closes."""
    try:
        # Release OCR readers
        from pid_viewer import cleanup_ocr_resources
        cleanup_ocr_resources()
    except Exception as e:
        logging.error('Error cleaning up OCR resources: %s', e)
    
    # Let the base class handle the event
    super().closeEvent(event)
```

**Alternative (non-invasive):** If closeEvent feels risky, call cleanup in `__main__`:

```python
if __name__ == '__main__':
    ...
    try:
        win = MainWindow()
        win.show()
        code = app.exec()
        logging.info('=== HAZOP Tool exited (code %d) ===', code)
        
        # Cleanup at app exit
        from pid_viewer import cleanup_ocr_resources
        cleanup_ocr_resources()
        
        sys.exit(code)
    except Exception:
        logging.exception('Fatal exception during startup or main loop')
        raise
```

### Testing

- Run app with `easyocr` or `rapidocr` installed
- Check system memory before/after app close (use `tasklist /v` on Windows)
- Verify OCR still works after calling cleanup (lazy re-init should restore reader on next use)

### Impact

- **Lines changed:** ~5 lines in `pid_viewer.py`, ~10 lines in `hazop.py`
- **Backwards compatibility:** Full (internal refactor only)
- **Risk:** Low (cleanup is idempotent; `_ocr_manager` can reinitialize if needed)

---

## Issue 2: Risk Matrix Cache Invalidation

### Current State

**Location:** `hazop.py` lines 433, 475–512

**Functions:**
- `_current_matrix = None` (line 433)
- `load_matrix(db)` (lines 475–481) — reads from DB, updates global
- `get_matrix()` (lines 484–485) — returns cached global
- `risk_info(frequency, consequence)` (lines 488–512) — uses cache
- `Database.set_risk_matrix(cfg)` (line 1809) — writes to DB but does NOT invalidate cache

**Call site:** `MainWindow._on_save_risk_matrix()` (line 15038–15039)

```python
def _on_save_risk_matrix(self, ...):
    ...
    self.db.set_risk_matrix(cfg)
    load_matrix(self.db)  # Manual reload — required for UI to see new data
    QMessageBox.information(self, "Sparat", "Riskmatris sparad.")
    self.matrix_changed.emit()
```

### Problems

1. **Manual reload required:** Caller must remember to call `load_matrix()` after `set_risk_matrix()`
2. **Silent staleness:** If `set_risk_matrix()` is called elsewhere, cache becomes stale without warning
3. **No transactional guarantee:** DB write succeeds, but cache refresh fails → UI shows wrong data

### Solution: Setter/Getter Pattern with Auto-Invalidation

#### Approach: Add setter to Database class that invalidates cache

**File:** `hazop.py`

Replace the current `set_risk_matrix()` method (line 1809):

```python
# OLD (lines 1809–1810)
def set_risk_matrix(self, cfg):
    self.set_config('risk_matrix', json.dumps(cfg))
```

With:

```python
# NEW
def set_risk_matrix(self, cfg):
    """Store risk matrix config to DB and invalidate module-level cache."""
    self.set_config('risk_matrix', json.dumps(cfg))
    # Invalidate global cache so next get_matrix() call reloads from DB
    _invalidate_matrix_cache()
```

Add this function at module level (near line 433):

```python
def _invalidate_matrix_cache():
    """Clear the cached risk matrix, forcing next get_matrix() to reload."""
    global _current_matrix
    _current_matrix = None


def get_matrix():
    """Return cached risk matrix, reloading from DB if invalidated."""
    global _current_matrix
    if _current_matrix is None:
        # Lazy load from a provided db or use a fallback
        # NOTE: This assumes a global Database is available (MainWindow.db)
        # For now, return DEFAULT_MATRIX; production code should pass db
        _current_matrix = DEFAULT_MATRIX
    return _current_matrix
```

#### Option B (Safer): Pass database to set_risk_matrix()

If you want strict decoupling, modify the signature:

```python
def set_risk_matrix(self, cfg):
    """Store risk matrix config and reload into module cache."""
    self.set_config('risk_matrix', json.dumps(cfg))
    # Reload cache from this database instance
    load_matrix(self)  # Already defined; reloads _current_matrix
```

Update call site (line 15038):

```python
# OLD
self.db.set_risk_matrix(cfg)
load_matrix(self.db)

# NEW (Option B)
self.db.set_risk_matrix(cfg)  # Now includes auto-reload
```

### Comparison of Approaches

| Approach | Pros | Cons |
|----------|------|------|
| **Option A** (invalidate flag) | Simple; setter not responsible for side effects | Lazy load requires global db reference |
| **Option B** (auto-reload) | Guaranteed consistency; setter is self-contained | Adds implicit side effect; setter now does two things |

**Recommendation:** Use **Option B** (auto-reload inside `set_risk_matrix()`). It's simpler and more robust.

### Testing

1. Open Settings → Risk Matrix
2. Change colors/labels
3. Save
4. Open Worksheet and verify UI uses new colors immediately
5. Close and reopen app → verify new colors persist

### Impact

- **Lines changed:** ~5 lines in `hazop.py`
- **Backwards compatibility:** Full (existing callers work; behavior is transparent)
- **Risk:** Very low (only affects risk matrix display; business logic unchanged)

---

## Issue 3: Database Connection Leak

### Current State

**Location:** `hazop.py` lines 1214–1224

```python
class Database:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path))  # ← Opened
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.commit()
        self._migrate()
        self._write_backup(startup=True)
    
    # No __del__, __enter__, or __exit__
```

**Manual close locations:**
- Line 17761: In MainWindow shutdown
- Line 17911: In MainWindow after DB reset

### Problems

1. **No __del__ method:** If Database object is replaced or goes out of scope, connection is never closed
2. **Manual cleanup scattered:** Two hardcoded `.close()` calls in MainWindow (lines 17761, 17911)
3. **No context manager support:** Can't use `with Database(...) as db:` pattern
4. **WAL mode:** Leaving connection open may leave `-wal` and `-shm` files behind

### Solution: Add __del__ with Fallback Context Manager

#### Step 1: Add __del__ to Database class (line 1224, after __init__)

```python
def __del__(self):
    """Cleanup connection on object destruction.
    
    This is a safety net; explicit close() is still preferred.
    """
    try:
        if self.conn:
            self.conn.close()
    except Exception:
        pass
```

**Rationale:** Ensures connection is closed even if caller forgets. Non-fatal if called multiple times.

#### Step 2: Add context manager support (optional, but recommended)

```python
def __enter__(self):
    """Support 'with Database() as db:' pattern."""
    return self

def __exit__(self, exc_type, exc_val, exc_tb):
    """Cleanup on context exit."""
    try:
        self.close()
    except Exception:
        pass
    return False  # Don't suppress exceptions
```

Also add a safe `close()` method (if not already present):

```python
def close(self):
    """Explicitly close the connection. Safe to call multiple times."""
    if self.conn:
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = None
```

#### Step 3: Update MainWindow shutdown (lines 17761, 17911)

Change:

```python
# OLD
self.db.conn.close()

# NEW (more defensive)
try:
    if hasattr(self.db, 'close'):
        self.db.close()
    elif self.db.conn:
        self.db.conn.close()
except Exception:
    pass
```

#### Step 4: Document the pattern

Add to CLAUDE.md:

```markdown
## Database Lifecycle

The Database class manages a persistent sqlite3 connection. Use one of:

1. **Explicit close (recommended in MainWindow):**
   ```python
   db.close()  # Idempotent; safe to call multiple times
   ```

2. **Context manager (for temporary scopes):**
   ```python
   with Database() as db:
       db.insert_node(...)  # Auto-closes on exit
   ```

3. **Automatic __del__ (fallback):**
   ```python
   db = Database()  # Closes when garbage-collected
   ```
```

### Testing

1. Open app, run a scenario, close app → check no `-wal`/`-shm` files are orphaned
2. Create a test script:
   ```python
   from hazop import Database
   db = Database('test.db')
   db.create_node('test')
   del db  # Should close cleanly
   assert not Path('test.db-wal').exists()  # WAL file should be gone
   ```
3. Verify context manager:
   ```python
   with Database('test.db') as db:
       db.create_node('test')
   # Connection closed automatically
   ```

### Impact

- **Lines changed:** ~15 lines in `hazop.py`
- **Backwards compatibility:** Full (existing code still works; new methods are additive)
- **Risk:** Very low (`__del__` is defensive; close() is idempotent)

---

## Implementation Schedule

### Phase 1: OCR Cleanup (1–2 hours)

1. Remove dead globals + function from `pid_viewer.py`
2. Add `cleanup_ocr_resources()` export
3. Add cleanup call in `hazop.py` `__main__` (safest initially)
4. Test OCR still works

### Phase 2: Risk Matrix Invalidation (30 minutes)

1. Modify `Database.set_risk_matrix()` to call `load_matrix(self)`
2. Update call site to remove redundant `load_matrix()` call
3. Test settings → risk matrix flow

### Phase 3: Database Connection Cleanup (1 hour)

1. Add `__del__`, `close()`, `__enter__`, `__exit__` to Database class
2. Update existing `.conn.close()` calls to use `db.close()`
3. Test app exit with Windows Process Monitor

### Phase 4: Integration & Testing (1–2 hours)

1. End-to-end app lifecycle test
2. Verify no resource leaks (Memory Monitor, lsof)
3. Commit changes with test instructions in NOTES.md

---

## Git Commit Strategy

```bash
# Commit 1: Remove dead OCR globals
git add pid_viewer.py
git commit -m "refactor: remove dead OCR globals; use _OCRLifecycleManager singleton"

# Commit 2: Add OCR cleanup export & integration
git add pid_viewer.py hazop.py
git commit -m "feat: explicit OCR resource cleanup at app shutdown"

# Commit 3: Risk matrix auto-invalidation
git add hazop.py
git commit -m "fix: risk matrix cache auto-invalidates on set_risk_matrix()"

# Commit 4: Database lifecycle management
git add hazop.py
git commit -m "feat: add __del__ and context manager support to Database class"

# Commit 5: Documentation
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with resource lifecycle patterns"
```

---

## Verification Checklist

- [ ] OCR models unload on app close (check memory profile)
- [ ] Risk matrix color changes take effect immediately after save
- [ ] Database connection closes on app exit (no `-wal`/`-shm` orphans)
- [ ] Existing code paths still work (no regression)
- [ ] NOTES.md updated with decisions
- [ ] No new dependencies added
- [ ] Code follows project style (comments, structure)

---

## Files to Modify

| File | Lines | Change Type | Reason |
|------|-------|-------------|--------|
| `pid_viewer.py` | 88–159 | Cleanup + refactor | Remove dead code, add public API |
| `hazop.py` | 1809–1810, 18090–18094 | Enhancement | Add cache invalidation, cleanup on exit |
| `hazop.py` | 1214–1224 | Enhancement | Add `__del__`, `close()`, context manager |
| `CLAUDE.md` | End | Documentation | Record patterns and lifecycle guidelines |

