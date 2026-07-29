# Crash Reporting System

Automated crash diagnostics with structured JSON reports for easy analysis.

## Features

✅ **Structured Crash Reports** — All crashes saved as JSON files with complete diagnostic data
✅ **Automatic Stack Trace** — Full traceback with local variables from each frame
✅ **Environment Data** — Python version, OS, platform, and dependency versions
✅ **Smart Truncation** — Large values safely truncated for readability
✅ **Legacy Log Support** — Still maintains `hazop_crash.log` for backward compatibility
✅ **User Notification** — Dialog with crash summary and file path

## File Locations

### Crash Reports
```
hazop/crashes/crash_20260729_181523_AttributeError.json
hazop/crashes/crash_20260729_164512_KeyError.json
...
```

**Format:** `crash_YYYYMMDD_HHMMSS_ExceptionType.json`

### Legacy Log
```
hazop/hazop_crash.log
```

## Report Structure

Each JSON file contains:

```json
{
  "timestamp": "2026-07-29T18:15:23.456789",
  "exception": {
    "type": "AttributeError",
    "message": "'NoneType' object has no attribute 'process'",
    "module": "builtins"
  },
  "traceback": {
    "frames": [
      {
        "filename": "/path/to/hazop.py",
        "function": "_rebuild",
        "lineno": 10512,
        "locals_preview": {
          "self": "<ScenarioTablePanel object>",
          "row_height": "34",
          "item": "<QTableWidgetItem>"
        }
      }
    ],
    "full_text": "Traceback (most recent call last):..."
  },
  "environment": {
    "python_version": "3.12.4",
    "platform": "Windows-11-10.0.26200-SP0",
    "machine": "AMD64",
    "processor": "Intel64"
  },
  "imports": {
    "PyQt6": "6.7.0",
    "fitz": "1.24.8",
    "PIL": "10.2.0"
  }
}
```

## Using Crash Reports

### For Debugging

Read the latest crash report:

```python
from hazop import CrashReporter
summary = CrashReporter.get_latest_crash_summary()
print(summary['timestamp'])
print(summary['type'], ':', summary['message'])
print(summary['location'])
```

### For Reporting

When submitting a bug:
1. Find the relevant JSON file in `hazop/crashes/`
2. Share the entire JSON content or the file itself
3. Include context about what you were doing when it crashed

## Key Diagnostic Data

Each report includes:

- **Exception Type & Message** — What went wrong
- **Stack Trace** — Full chain of function calls leading to crash
- **Local Variables** — Variables in each frame (truncated for safety)
- **File & Line Number** — Exact location of error
- **Python Version** — Version of Python running
- **OS & Platform** — Operating system information
- **Dependency Versions** — Versions of PyQt6, fitz, PIL, OCR engines, etc.

## Programmatic Access

### List all crashes (most recent first)

```python
crashes = CrashReporter.list_crashes()
for crash_file in crashes[:5]:
    print(crash_file)
```

### Get latest crash summary

```python
summary = CrashReporter.get_latest_crash_summary()
if summary:
    print(f"{summary['timestamp']}: {summary['type']}")
    print(f"  at {summary['location']}")
    print(f"  in {summary['function']}()")
    # Access full report:
    report = summary['full_report']
```

### Analyze a specific report

```python
import json

with open('crashes/crash_20260729_181523_KeyError.json') as f:
    report = json.load(f)

# Exception info
print(report['exception']['type'])
print(report['exception']['message'])

# Stack frames
for frame in report['traceback']['frames']:
    print(f"{frame['filename']}:{frame['lineno']} in {frame['function']}")
    print(f"  locals: {frame['locals_preview']}")

# Environment
print(f"Python {report['environment']['python_version']}")
print(f"Dependencies: {report['imports']}")
```

## Cleanup

To manage disk space, delete old crash reports:

```bash
# Windows PowerShell
Remove-Item hazop/crashes/*.json -Filter {$_.LastWriteTime -lt (Get-Date).AddDays(-30)}
```

Or manually delete from `hazop/crashes/`.

## Future Improvements

- [ ] Automatic crash aggregation (group similar crashes)
- [ ] Pattern detection (recurring error types)
- [ ] Performance metrics in crash reports
- [ ] Web dashboard for crash statistics
- [ ] Automatic bug report generation

---

**Why structured JSON?** JSON is:
- Human-readable
- Machine-parseable (easy for code analysis)
- Language-agnostic
- Compact and self-documenting
- Ideal for automated crash analysis tools
