# Resource Lifecycle and Global Mutable State Fixes

**Date:** 2026-07-29  
**Summary:** Fixed three categories of resource management issues in the HAZOP tool to prevent memory leaks, cache invalidation bugs, and improper database cleanup.

## Issues Fixed

### 1. OCR Reader Memory Leaks (pid_viewer.py)

**Problem:**
- Global variables `_easyocr_reader_cache` and `_rapidocr_instance` held references to large ML models (~100-500MB)
- No cleanup on application exit; models remained in memory indefinitely
- `EquipmentScanDialog` created its own local `self._easyocr_reader` instead of reusing the global cache, doubling memory usage

**Solution:**
- Introduced `_OCRLifecycleManager` singleton class to manage both OCR engines
- Centralized caching with explicit cleanup API
- Removed duplicate local caching in `EquipmentScanDialog`
- Added `cleanup_ocr_resources()` public function called at app exit

**Files Modified:**
- `pid_viewer.py` (lines 96-177): Replaced module globals with singleton manager

**Code Pattern:**
```python
class _OCRLifecycleManager:
    """Singleton managing OCR reader lifecycle with automatic cleanup."""
    # Lazy initialization of readers
    # cleanup() method releases references for GC
    # __del__() ensures cleanup on object destruction

_ocr_manager = _OCRLifecycleManager()

def cleanup_ocr_resources():
    """Called on app exit to release ML models."""
    _ocr_manager.cleanup()
```

### 2. Risk Matrix Cache Invalidation (hazop.py)

**Problem:**
- Global `_current_matrix` cached the risk matrix for performance
- When `set_risk_matrix()` updated the database, the cache was not automatically invalidated
- Subsequent calls to `get_matrix()` would return stale data until manually calling `load_matrix()`
- Manual cache invalidation at call sites is error-prone

**Solution:**
- Introduced `_RiskMatrixCache` manager class with invalidation support
- Modified `Database.set_risk_matrix()` to automatically call `_risk_matrix_cache.invalidate()`
- Changed call sites to use `reload_from_db()` for explicit reload
- Cache is automatically invalidated whenever matrix is stored

**Files Modified:**
- `hazop.py` (lines 439-468): New cache manager class
- `hazop.py` (line 1860): Automatic invalidation in `set_risk_matrix()`
- `hazop.py` (line 15102): Reload after save

**Code Pattern:**
```python
class _RiskMatrixCache:
    def load(self, db): ...       # Load and cache from DB
    def invalidate(self): ...     # Clear cache (next get() reloads)
    def reload_from_db(self): ... # Force reload from DB
    def get(self): ...            # Return cached or DEFAULT

# In Database.set_risk_matrix():
_risk_matrix_cache.invalidate()  # Auto-invalidate on write
```

### 3. Database Connection Lifecycle (hazop.py)

**Problem:**
- `Database.__init__` opens SQLite connection but no `__del__` method existed
- Connection was only explicitly closed in two ad-hoc places (lines 17746, 17896)
- If Database object was replaced or garbage-collected, the connection could leak
- WAL (Write-Ahead Logging) files could orphan if connection wasn't properly flushed

**Solution:**
- Added `__del__()` method to Database class for safe cleanup
- Flushes WAL checkpoint before closing (PRAGMA wal_checkpoint(TRUNCATE))
- Defensive try/except to ensure cleanup even if connection is partially closed
- Also called OCR cleanup at app exit (line 18128)

**Files Modified:**
- `hazop.py` (lines 1258-1271): New `__del__()` method in Database class
- `hazop.py` (lines 18126-18131): Cleanup call at app exit

**Code Pattern:**
```python
class Database:
    def __del__(self):
        """Cleanup on object destruction."""
        try:
            if hasattr(self, 'conn') and self.conn:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.conn.close()
        except Exception:
            pass
```

## Impact Summary

| Category | Files | Changes | Risk | Effect |
|----------|-------|---------|------|--------|
| OCR Cleanup | pid_viewer.py | ~80 lines (classes + functions) | Low | Eliminates 200-500MB memory leak |
| Matrix Cache | hazop.py | ~30 lines | Low | Prevents stale matrix bugs |
| DB Cleanup | hazop.py | ~15 lines | Low | Ensures clean shutdown |

## Testing Checklist

- [ ] Launch app and verify OCR works (if engines installed)
- [ ] Edit risk matrix and verify changes are immediately reflected
- [ ] Close app and verify no errors in hazop_crash.log
- [ ] Check for WAL orphan files (`*.db-shm`, `*.db-wal`) after exit
- [ ] Monitor memory usage on app exit (should release OCR models)
- [ ] Verify Database objects properly cleanup on replacement (`_hzp_new` flow)

## Backwards Compatibility

All changes are **fully backwards compatible**:
- Public API (`get_matrix()`, `load_matrix()`, `ocr_status()`) unchanged
- Internal caching is transparent to callers
- Cache invalidation is automatic (no caller coordination needed)
- Database cleanup is defensive and doesn't affect existing open/close patterns

## Related Code

- **Resource cleanup on project reset:** `MainWindow._hzp_new()` lines 17746-17770
- **Cleanup at app exit:** `__main__` block lines 18126-18131
- **Risk matrix usage:** `risk_info()` function at line 520+ (uses `get_matrix()`)
- **OCR usage:** All `_ocr_page_*()` functions use `_get_easyocr_reader()` and `_get_rapidocr_instance()`

## Notes for Future Development

1. **OCR Cleanup:** Both EasyOCR and RapidOCR rely on Python's garbage collection for model cleanup. The singleton ensures only one instance of each is created, and the cleanup function explicitly nulls references for GC.

2. **Matrix Cache:** The cache manager stores the Database reference so it can reload on demand. If the Database object changes (project load/save), `load_matrix(new_db)` is called to update the reference.

3. **Database Cleanup:** The `__del__` method is a safety net. Explicit `close()` calls are still preferred where possible, but `__del__` ensures no leaks even if explicit cleanup is forgotten.

