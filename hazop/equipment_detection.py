#!/usr/bin/env python3
"""Equipment/valve detection and tag-scanning for P&ID PDFs.

Pure Python/PyMuPDF — no Qt dependency (mirrors symbol_geometry.py's own
stated goal), so this module can be imported by pid_viewer.py (for the
live app) or by standalone scripts/tests without pulling in the GUI
stack. Split out of pid_viewer.py (2026-08-06) once this analysis layer
had grown large enough on its own — see NOTES.md — while pid_viewer.py
keeps the Qt canvas/dialogs and imports what it needs from here.

Three layers, in the order a scan actually runs:
  1. Tag-text scanning (native text + OCR, tag-format regexes) —
     scan_pdf_for_equipment() and its support functions.
  2. Shape detection (bow-tie valve symbols via symbol_geometry) —
     detect_equipment_symbols(), find_valve_shapes().
  3. The unified pipeline that shares ONE vector extraction per page
     between tag-association and shape-hunting so the same physical
     valve can never be reported twice — detect_equipment_and_valves(),
     see NOTES.md "Fas 1+2" (2026-08-06).

Not moved here (stayed in pid_viewer.py, out of this module's scope):
Qt canvas/dialog code (obviously), the sheet-connector/media-coloring
subsystem (_sheet_ref_variants, _detect_dialect, _propose_layout, the
_MEDIA_*/_RE_*/_DIALECTS constants — a different analytical domain, not
equipment/valve detection), and the red-markup-symbol SVG icon lookup
(_get_red_symbol_svg/_RED_MARKUP_SYMBOLS — unrelated to this module).
ensure_ocr_available() also stayed — it shows a QMessageBox, so it's the
one OCR-related function that genuinely needs Qt; it calls ocr_status()
from here for the underlying availability check.
"""
import re
import math
import importlib.util

import fitz

import symbol_geometry

# ── Optional OCR engines ────────────────────────────────────────────────────
# Duplicated (not imported) from pid_viewer.py's own copy of this same
# detection block: pid_viewer.py's Qt classes (PIDGraphicsView,
# ConnectorAnalyzer) ALSO reference HAS_TESSERACT/HAS_EASYOCR/HAS_PIL
# directly, so both files need their own independent flags — but the
# actual OCR reader instances (_ocr_manager below) are a real shared
# resource and must live in exactly ONE place (here), not be duplicated.
try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    pytesseract = None
    HAS_TESSERACT = False

HAS_EASYOCR = importlib.util.find_spec('easyocr') is not None
HAS_RAPIDOCR = importlib.util.find_spec('rapidocr_onnxruntime') is not None

try:
    from PIL import Image as _PILImage, ImageFilter, ImageEnhance, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

HAS_PYMUPDF = True


# ── OCR Reader Lifecycle Manager ──────────────────────────────────────────────
# Centralized handling of OCR model caching and cleanup. Both EasyOCR and RapidOCR
# load large ML models (~100-500MB) that must be explicitly deallocated to avoid
# memory leaks on application exit.

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

    def get_easyocr_reader(self):
        """Get or create EasyOCR reader; lazily initialized on first use."""
        if self._easyocr_reader is None and HAS_EASYOCR:
            try:
                import easyocr as _easyocr_module
                self._easyocr_reader = _easyocr_module.Reader(['en'], gpu=False, verbose=False)
            except Exception:
                return None
        return self._easyocr_reader

    def get_rapidocr_instance(self):
        """Get or create RapidOCR instance; lazily initialized on first use."""
        if self._rapidocr_instance is None and HAS_RAPIDOCR:
            try:
                from rapidocr_onnxruntime import RapidOCR as _RapidOCR
                self._rapidocr_instance = _RapidOCR()
            except Exception:
                return None
        return self._rapidocr_instance

    def cleanup(self):
        """Explicitly release OCR resources. Safe to call multiple times."""
        if self._easyocr_reader is not None:
            try:
                # EasyOCR has no explicit cleanup, but we null the reference
                # to allow garbage collection of the model
                self._easyocr_reader = None
            except Exception:
                pass

        if self._rapidocr_instance is not None:
            try:
                # RapidOCR resources are released by Python's GC
                self._rapidocr_instance = None
            except Exception:
                pass

    def __del__(self):
        """Cleanup on object destruction (at app exit or manual cleanup)."""
        try:
            self.cleanup()
        except Exception:
            pass


# Global manager instance
_ocr_manager = _OCRLifecycleManager()


def _get_easyocr_reader():
    """Get EasyOCR reader from the global lifecycle manager."""
    return _ocr_manager.get_easyocr_reader()


def _get_rapidocr_instance():
    """Get RapidOCR instance from the global lifecycle manager."""
    return _ocr_manager.get_rapidocr_instance()


def cleanup_ocr_resources():
    """Public API to manually cleanup OCR resources (called on app exit)."""
    global _ocr_manager
    if _ocr_manager:
        _ocr_manager.cleanup()


def ocr_status() -> dict:
    """Return which OCR engines are available."""
    return {
        'tesseract': HAS_TESSERACT,
        'easyocr':   HAS_EASYOCR,
        'rapidocr':  HAS_RAPIDOCR,
        'pil':       HAS_PIL,
    }


def _ocr_page_rapidocr(pil_image, scale: float):
    """Run RapidOCR on a PIL image; return list of (text, x_pdf, y_pdf)."""
    if not HAS_RAPIDOCR:
        return []
    try:
        import numpy as np
        reader = _get_rapidocr_instance()
        if reader is None:
            return []
        result, _ = reader(np.array(pil_image.convert('RGB')))
        if not result:
            return []
        out = []
        for item in result:
            if len(item) < 3:
                continue
            box, text, conf = item[0], item[1], item[2]
            if not text or float(conf) < 0.3:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = sum(xs) / len(xs) / scale
            cy = sum(ys) / len(ys) / scale
            out.append((text.strip().upper(), cx, cy))
        return out
    except Exception:
        return []


def _preprocess_for_ocr(pil_image):
    """Multi-strategy preprocessing for P&ID OCR."""
    gray = pil_image.convert('L')
    # Check whether drawing is light-on-dark or dark-on-light
    import statistics
    sample = list(gray.getdata())[::50]
    median_lum = statistics.median(sample)
    if median_lum < 100:
        # Light text on dark background — invert
        gray = ImageOps.invert(gray)
    # Enhance contrast strongly
    gray = ImageEnhance.Contrast(gray).enhance(3.0)
    gray = ImageEnhance.Sharpness(gray).enhance(2.0)
    # Gentle denoise
    gray = gray.filter(ImageFilter.MedianFilter(size=3))
    return gray


def _fix_ocr_common_errors(text: str) -> str:
    """Correct typical OCR misreads in alphanumeric equipment tags."""
    # In the letter prefix part: 0→O, 1→I
    # In the number suffix part: O→0, I→1
    m = re.match(r'^([A-Z0-9]{1,6})-?([0-9A-Z]{1,6})$', text.upper().strip())
    if not m:
        return text.upper().strip()
    prefix, suffix = m.group(1), m.group(2)
    prefix = prefix.replace('0', 'O').replace('1', 'I')
    suffix = suffix.replace('O', '0').replace('I', '1').replace('o', '0')
    return f"{prefix}-{suffix}"


def _ocr_page_tesseract(pil_image, scale: float):
    """Run Tesseract with multiple PSM modes; return list of (text, x_pdf, y_pdf)."""
    if not HAS_TESSERACT:
        return []

    # PSM 11 = sparse text (best for P&IDs with scattered labels)
    # PSM  6 = uniform block (catches denser areas)
    seen: set = set()
    results: list = []

    for psm in (11, 6):
        cfg = (f'--oem 3 --psm {psm} '
               r'-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.')
        try:
            data = pytesseract.image_to_data(
                pil_image, config=cfg,
                output_type=pytesseract.Output.DICT)
        except Exception:
            continue

        n = len(data['text'])
        for i in range(n):
            word = data['text'][i].strip()
            if not word:
                continue
            try:
                conf = int(data['conf'][i])
            except (ValueError, TypeError):
                conf = 0
            if conf < 20:          # low bar — tag filter handles false positives
                continue
            key = (data['left'][i], data['top'][i], word)
            if key in seen:
                continue
            seen.add(key)
            x_pdf = (data['left'][i] + data['width'][i] / 2) / scale
            y_pdf = (data['top'][i] + data['height'][i] / 2) / scale
            results.append((word.upper(), x_pdf, y_pdf))

        # Also try to find tags split across adjacent tokens on the same line
        blocks: dict = {}
        for i in range(n):
            if not data['text'][i].strip():
                continue
            bk = (data['block_num'][i], data['line_num'][i])
            blocks.setdefault(bk, []).append(i)

        for indices in blocks.values():
            tokens = [data['text'][j].strip().upper() for j in indices]
            # Try pairs and triples of consecutive tokens
            for start in range(len(tokens)):
                for length in (2, 3):
                    if start + length > len(tokens):
                        break
                    combined = ''.join(tokens[start:start + length])
                    key = ('combined', combined)
                    if key in seen:
                        continue
                    seen.add(key)
                    j0 = indices[start]
                    j1 = indices[start + length - 1]
                    x_pdf = (data['left'][j0] + data['width'][j1] +
                             data['left'][j1]) / 2 / scale
                    y_pdf = (data['top'][j0] + data['height'][j0] / 2) / scale
                    results.append((combined, x_pdf, y_pdf))

    return results


def _ocr_page_easyocr(pil_image, scale: float):
    """Run EasyOCR on a PIL image; return list of (text, x_pdf, y_pdf)."""
    reader = _get_easyocr_reader()
    if reader is None:
        return []
    try:
        import numpy as np
        img_array = np.array(pil_image.convert('RGB'))
        ocr_results = reader.readtext(img_array, detail=1)
        results = []
        for (bbox, text, conf) in ocr_results:
            if conf < 0.3 or not text.strip():
                continue
            cx = sum(p[0] for p in bbox) / 4 / scale
            cy = sum(p[1] for p in bbox) / 4 / scale
            results.append((text.strip().upper(), cx, cy))
        return results
    except Exception:
        return []


def _ocr_page(fitz_page, scale: float = 3.0, engine: str = 'auto'):
    """Render a PyMuPDF page and OCR it.

    Returns list of (text, x_pdf, y_pdf) tuples and the engine name used.
    """
    if not HAS_PYMUPDF or not HAS_PIL:
        return [], None

    mat = fitz.Matrix(scale, scale)
    pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
    pil_img = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
    processed = _preprocess_for_ocr(pil_img)

    if engine == 'auto':
        # Priority: RapidOCR (small, fast) > Tesseract > EasyOCR
        if HAS_RAPIDOCR:
            engine = 'rapidocr'
        elif HAS_TESSERACT:
            engine = 'tesseract'
        elif HAS_EASYOCR:
            engine = 'easyocr'
        else:
            engine = None

    if engine == 'rapidocr':
        return _ocr_page_rapidocr(pil_img, scale), 'rapidocr'
    elif engine == 'tesseract':
        return _ocr_page_tesseract(processed, scale), 'tesseract'
    elif engine == 'easyocr':
        return _ocr_page_easyocr(processed, scale), 'easyocr'
    return [], None


COMPONENT_TYPES = {
    'Ventil': ['Fullt öppen (fastnad)', 'Fullt stängd (fastnad)', 'Delvis öppen/stängd',
               'Intern läcka', 'Yttre läcka', 'Felaktig aktivering'],
    'Pump': ['Startar inte', 'Stannar oväntat', 'Reducerat flöde',
             'Backflöde', 'Kavitation', 'Mekaniskt haveri'],
    'Tank / Kärl': ['Överfyllnad', 'Tömning (låg nivå)', 'Övertryck',
                    'Undertryck', 'Yttre läcka', 'Korrosion'],
    'Värmeväxlare': ['Rörläcka (kors-kontam.)', 'Igensättning',
                     'Otillräcklig kylning', 'Överkylning', 'Yttre läcka'],
    'Kompressor': ['Startar inte', 'Stannar oväntat', 'Surging', 'Övertryckning', 'Läcka'],
    'Rörledning': ['Blockering', 'Yttre läcka / brott', 'Korrosion', 'Vibration'],
    'Instrument / Sensor': ['Falskt högt signal', 'Falskt lågt signal',
                             'Signalbortfall', 'Kalibreringsdrift'],
    'Säkerhetsventil (PSV)': ['Öppnar inte vid högt tryck',
                               'Stänger inte (förblir öppen)', 'Öppnar för tidigt'],
    'Övrigt': ['Mekaniskt haveri', 'Yttre läcka', 'Kontaminering', 'Felaktig manuell operation'],
}

# The COMPONENT_TYPES keys that count as "a valve" for scoping "🎯 Hitta på
# P&ID" — deliberately narrowed to valves only for now (2026-08-06); other
# equipment types (instruments, pumps, ...) are planned but not yet wired
# into the detection pipeline, so including their tags today would just
# mean noisy, unfiltered results in the review dialog for a type the
# pipeline doesn't actually know how to visually confirm.
VALVE_COMPONENT_TYPES = {'Ventil', 'Säkerhetsventil (PSV)'}


# Simple tag: 1-6 letters + separator + 1-5 digits + 0-3 suffix
# Examples: PCV-101, FT201A, V-1, ESDV-1001AB
# Separators used across different plant conventions:
#   -  (hyphen)   : ISA standard, Loket, Sunpine
#   .  (dot)      : LKAB RDS-PP (E1.M1.GPA4)
#   /  (slash)    : some older Swedish conventions
#   _  (underscore): ITS (XFB_31304)
#   (none)        : Gryaab/Sunpine legacy (HV0063, TA0058)
_SEP = r'[-./_ ]'

# Strict single-word tag: 2+ letters + optional sep + 1-5 digits + 0-3 letters
# OR single letter + mandatory sep + 2+ digits (P-101).
# Also handles underscore separator (ITS: XFB_31304).
_TAG_RE = re.compile(
    r'^(?:'
    r'[A-Z]{2,6}[_\-./]?\d{1,5}[A-Z]{0,3}'   # HV0063, PCV-101, XFB_31304
    r'|[A-Z]{1}[-./]\d{2,5}[A-Z]{0,3}'        # E-101, P-001
    r')$'
)

# Tag within continuous text (allows no separator between letters and digits)
_FULL_TAG_RE = re.compile(
    r'(?<![A-Z0-9])'
    r'([A-Z]{1,6})[_\-./]?(\d{1,5}[A-Z]{0,3})'
    r'(?![A-Z0-9])')

# Extended tag with area/unit prefix — handles all plant conventions:
#   LKAB RDS-PP  : E1.M1.GPA4,  M1.WPA182
#   Loket        : 60-RV-009,   LS60.002
#   Sunpine      : 2818-LX79,   100-MAS10A,  G45-100-EAS10A
#   ITS          : XFB_31304 (matches as simple tag, no area prefix needed)
#   Standard     : 20-PCV-101,  K2.FT.201A
# Area sections: pure digits OR alphanumeric starting with letter/digit
_EXT_TAG_RE = re.compile(
    r'(?<![A-Z0-9=])'
    r'(=?(?:(?:\d{1,4}|[A-Z][A-Z0-9]{0,3})[-./_ ]{1,2}){1,4}'
    r'[A-Z]{1,6}[_\-./]?\d{1,5}[A-Z]{0,3})'
    r'(?![A-Z0-9])',
    re.IGNORECASE)

# Simple numeric area prefix: 20-PCV-101 → PCV-101
_AREA_TAG_RE = re.compile(r'^\d{1,4}[-/]([A-Z]{1,6})[_\-./]?(\d{1,5}[A-Z]{0,3})$')


def _equip_prefix_from_tag(tag: str) -> str:
    """Extract the equipment-code letter prefix from a P&ID tag.

    Handles all plant naming conventions found in analysed P&IDs:

    LKAB RDS-PP (dot-hierarchy):
        'E1.M1.GPA4'  → 'GPA'    'M1.HXA1'  → 'HXA'
    Loket (area-hyphen-ISA):
        '60-RV-009'   → 'RV'     'LS60.002' → 'LS'
    Sunpine (drawing-nr + code, unit-name, zero-padded):
        '2818-LX79'   → 'LX'     '100-MAS10A' → 'MAS'
        'G45-100-EAS10A' → 'EAS'  'HV0063'   → 'HV'
    ITS (underscore separator):
        'XFB_31304'   → 'XFB'    'TK_11338' → 'TK'
    Standard ISA:
        'PCV-101'     → 'PCV'    '20-FT-201' → 'FT'
    Pipe-size prefix stripped:
        '2"LS60.002'  → 'LS'     'DN100-HV-101' → 'HV'
    RDS-PP '=' prefix stripped:
        '=E1.M1.GPA4' → 'GPA'

    KNOWN_PREFIXES is used only to identify which segment is the instrument
    code — never to determine the component type.
    """
    if not tag:
        return ''
    t = tag.strip().upper()

    # Strip RDS-PP '=' designation prefix
    t = t.lstrip('=')

    # Strip pipe-size prefix: 2", 4", 8" or DN100, DN125
    t = re.sub(r'^\d+["\']+', '', t)          # 2"LS → LS
    t = re.sub(r'^DN\d+[-./_ ]?', '', t)      # DN100-HV → HV

    # Split by all common separators including underscore
    parts = re.split(r'[-./_ ]', t)

    def _leading(s):
        """Leading letter block: 'PU0050' → 'PU', '300PU3222' → ''"""
        m = re.match(r'^([A-Z]+)', s)
        return m.group(1) if m else ''

    def _embedded(s):
        """Letter block embedded between digits: '300PU3222' → 'PU', 'PU0050' → ''
        Handles format: <area-digits><LETTERS><seq-digits>
        """
        m = re.match(r'^\d+([A-Z]{2,6})\d', s)
        return m.group(1) if m else ''

    # Skip 'DN' (pipe nominal diameter — not an instrument code)
    skip = {'DN'}

    # Build candidates: (leading_letters, embedded_letters) per part
    candidates = [(_leading(p), _embedded(p)) for p in parts if p]

    # 1. Leading 2+ letters in KNOWN_PREFIXES (highest confidence)
    for lead, _ in candidates:
        if len(lead) >= 2 and lead not in skip and lead in KNOWN_PREFIXES:
            return lead

    # 2. Embedded letters in KNOWN_PREFIXES: '300PU3222' → 'PU'
    for _, emb in candidates:
        if len(emb) >= 2 and emb not in skip and emb in KNOWN_PREFIXES:
            return emb

    # 3. First leading 2+ letter segment (instrument code beats single-letter area)
    for lead, _ in candidates:
        if len(lead) >= 2 and lead not in skip:
            return lead

    # 4. First embedded 2+ letter segment (no separator, digits sandwich)
    for _, emb in candidates:
        if len(emb) >= 2 and emb not in skip:
            return emb

    # 5. Single-letter known prefix (E, P, C, …)
    for lead, _ in candidates:
        if len(lead) == 1 and lead in KNOWN_PREFIXES:
            return lead

    # 6. Any leading letters from the first non-empty part
    first = next((lead for lead, _ in candidates if lead), '')
    return first or (parts[0] if parts else tag)

# ── Equipment prefix knowledge base ──────────────────────────────────────────
# Format: prefix → (swedish_display_name, COMPONENT_TYPES key)
KNOWN_PREFIXES = {
    # Ventiler
    'V':    ('Ventil (allmän)',                  'Ventil'),
    'HV':   ('Handventil',                       'Ventil'),
    'MOV':  ('Motorventil',                      'Ventil'),
    'PCV':  ('Tryckreduceringsventil',           'Ventil'),
    'FCV':  ('Flödesreglerventil',               'Ventil'),
    'LCV':  ('Nivåreglerventil',                 'Ventil'),
    'TCV':  ('Temperaturreglerventil',           'Ventil'),
    'AV':   ('Automatisk ventil',                'Ventil'),
    'ON':   ('Avstängningsventil',               'Ventil'),
    'BV':   ('Kulventil',                        'Ventil'),
    'CV':   ('Reglerventil / Backventil',        'Ventil'),
    'SV':   ('Stängningsventil',                 'Ventil'),
    'SDV':  ('Stängningsventil (SDV)',           'Ventil'),
    'BDV':  ('Tryckavsäkringsventil',            'Ventil'),
    'XV':   ('Nödavstängningsventil',            'Ventil'),
    'ESV':  ('Nödavstängningsventil',            'Ventil'),
    'ESDV': ('Nödavstängningsventil (ESDV)',     'Ventil'),
    'NRV':  ('Backventil',                       'Ventil'),
    'ROV':  ('Fjärrstyrd ventil (ROV)',          'Ventil'),
    'IV':   ('Isoleringsventil',                 'Ventil'),
    # Säkerhetsventiler
    'PSV':  ('Säkerhetsventil (PSV)',            'Säkerhetsventil (PSV)'),
    'PRV':  ('Trycksäkringsventil (PRV)',        'Säkerhetsventil (PSV)'),
    'RV':   ('Säkerhetsventil (RV)',             'Säkerhetsventil (PSV)'),
    'SRV':  ('Fjädersäkringsventil',             'Säkerhetsventil (PSV)'),
    'TSV':  ('Temperatursäkringsventil',         'Säkerhetsventil (PSV)'),
    'RD':   ('Sprängskiva (Rupture Disk)',       'Säkerhetsventil (PSV)'),
    # Pumpar
    'P':    ('Pump',                             'Pump'),
    'PP':   ('Pump',                             'Pump'),
    'PU':   ('Pump',                             'Pump'),   # Gryaab / svenska konventionen
    'DP':   ('Doseringspump',                    'Pump'),
    'CP':   ('Centrifugalpump',                  'Pump'),
    'VP':   ('Vakuumpump',                       'Pump'),
    'SP':   ('Skruvpump',                        'Pump'),
    'GPA':  ('Pump (LKAB GPA)',                  'Pump'),   # LKAB RDS-PP
    'WPA':  ('Pump (LKAB WPA)',                  'Pump'),   # LKAB RDS-PP
    # Kompressorer / fläktar
    'C':    ('Kompressor',                       'Kompressor'),
    'K':    ('Kompressor',                       'Kompressor'),
    'COM':  ('Kompressor',                       'Kompressor'),
    'BL':   ('Blåsmaskin / Fläkt',              'Kompressor'),
    'FN':   ('Fläkt',                            'Kompressor'),
    'EJE':  ('Ejektor',                          'Kompressor'),
    # Tankar och kärl
    'T':    ('Tank',                             'Tank / Kärl'),
    'TK':   ('Tank',                             'Tank / Kärl'),
    'D':    ('Drum / Separator',                 'Tank / Kärl'),
    'S':    ('Separator',                        'Tank / Kärl'),
    'SEP':  ('Separator',                        'Tank / Kärl'),
    'R':    ('Reaktor',                          'Tank / Kärl'),
    'COL':  ('Kolonn',                           'Tank / Kärl'),
    'ACC':  ('Ackumulator',                      'Tank / Kärl'),
    'SK':   ('Skrubber',                         'Tank / Kärl'),
    'KO':   ('Knock-out drum',                   'Tank / Kärl'),
    'FL':   ('Flare-system',                     'Tank / Kärl'),
    # Värmeväxlare
    'E':    ('Värmeväxlare',                     'Värmeväxlare'),
    'HE':   ('Värmeväxlare',                     'Värmeväxlare'),
    'AHE':  ('Luftkylare',                       'Värmeväxlare'),
    'REB':  ('Ångpanna / Reboiler',             'Värmeväxlare'),
    'HX':   ('Värmeväxlare',                     'Värmeväxlare'),
    'CD':   ('Kondensor',                        'Värmeväxlare'),
    'H':    ('Heater / Ugn',                     'Värmeväxlare'),
    # Filter / avskiljare
    'F':    ('Filter',                           'Övrigt'),
    'STR':  ('Sil / Strainer',                  'Övrigt'),
    'Y':    ('Y-sil',                            'Övrigt'),
    'CL':   ('Cyklon',                           'Övrigt'),
    # Instrument – Tryck
    'PI':   ('Tryckmätare (lokal)',              'Instrument / Sensor'),
    'PT':   ('Trycktransmitter',                 'Instrument / Sensor'),
    'PIT':  ('Trycktransm. + indikering',        'Instrument / Sensor'),
    'PIC':  ('Tryckreglering',                   'Instrument / Sensor'),
    'PICA': ('Tryckreglering + larm',            'Instrument / Sensor'),
    'PSH':  ('Högtrycksalarm (PSH)',             'Instrument / Sensor'),
    'PSL':  ('Lågtrycksalarm (PSL)',             'Instrument / Sensor'),
    'PSHH': ('Högtrycksalarm HH',               'Instrument / Sensor'),
    'PSLL': ('Lågtrycksalarm LL',               'Instrument / Sensor'),
    'PDI':  ('Differenstrycksmätare',            'Instrument / Sensor'),
    'PDT':  ('Differenstrycktransm.',            'Instrument / Sensor'),
    'PDIT': ('Differenstrycktransm. + indik.',   'Instrument / Sensor'),
    # Instrument – Flöde
    'FI':   ('Flödesmätare (lokal)',             'Instrument / Sensor'),
    'FT':   ('Flödestransmitter',                'Instrument / Sensor'),
    'FIT':  ('Flödestransm. + indikering',       'Instrument / Sensor'),
    'FIC':  ('Flödesreglering',                  'Instrument / Sensor'),
    'FICA': ('Flödesreglering + larm',           'Instrument / Sensor'),
    'FSH':  ('Högt flödesalarm',                 'Instrument / Sensor'),
    'FSL':  ('Lågt flödesalarm',                 'Instrument / Sensor'),
    'FQ':   ('Flödesmängdsmätare',               'Instrument / Sensor'),
    'FM':   ('Flödesmätare',                     'Instrument / Sensor'),
    # Instrument – Nivå
    'LI':   ('Nivåmätare (lokal)',               'Instrument / Sensor'),
    'LT':   ('Nivåtransmitter',                  'Instrument / Sensor'),
    'LIT':  ('Nivåtransm. + indikering',         'Instrument / Sensor'),
    'LIC':  ('Nivåreglering',                    'Instrument / Sensor'),
    'LICA': ('Nivåreglering + larm',             'Instrument / Sensor'),
    'LSH':  ('Högnivåalarm',                     'Instrument / Sensor'),
    'LSL':  ('Lågnivåalarm',                     'Instrument / Sensor'),
    'LSHH': ('Högnivåalarm HH',                 'Instrument / Sensor'),
    'LSLL': ('Lågnivåalarm LL',                 'Instrument / Sensor'),
    'LG':   ('Nivåglas',                         'Instrument / Sensor'),
    'LS':   ('Nivåbrytare (Level Switch)',       'Instrument / Sensor'),
    'LW':   ('Nivåvarning (Level Warning)',      'Instrument / Sensor'),
    # Instrument – Temperatur
    'TI':   ('Temperaturrgivare (lokal)',        'Instrument / Sensor'),
    'TE':   ('Temperaturelement',                'Instrument / Sensor'),
    'TT':   ('Temperaturtransmitter',            'Instrument / Sensor'),
    'TIT':  ('Temperaturtransm. + indik.',       'Instrument / Sensor'),
    'TIC':  ('Temperaturreglering',              'Instrument / Sensor'),
    'TSH':  ('Högt temperaturlarm',              'Instrument / Sensor'),
    'TSL':  ('Lågt temperaturlarm',              'Instrument / Sensor'),
    'TIA':  ('Temperaturindik. + larm',          'Instrument / Sensor'),
    # Instrument – Analys
    'AI':   ('Analysinstrument',                 'Instrument / Sensor'),
    'AT':   ('Analystransmitter',                'Instrument / Sensor'),
    'AIC':  ('Analysreglering',                  'Instrument / Sensor'),
    'ASH':  ('Högt analysalarm',                 'Instrument / Sensor'),
    'ASL':  ('Lågt analysalarm',                 'Instrument / Sensor'),
    'QA':   ('Kvalitetsanalysator / larm',       'Instrument / Sensor'),
    'QMA':  ('Kvalitetsmätare (LKAB QMA)',       'Instrument / Sensor'),
    'QMB':  ('Kvalitetsmätare (LKAB QMB)',       'Instrument / Sensor'),
    # Instrument – Primärelement (saknade)
    'FE':   ('Flödesgivare / primärelement',     'Instrument / Sensor'),
    'LE':   ('Nivågivare / primärelement',       'Instrument / Sensor'),
    'PE':   ('Tryckelement / primärelement',     'Instrument / Sensor'),
    'TE':   ('Temperaturelement',                'Instrument / Sensor'),
    'AE':   ('Analyselement / primärelement',    'Instrument / Sensor'),
    'AIT':  ('Analysind. + transmitter',         'Instrument / Sensor'),
    # Instrument – Slutliga reglerenheter (saknade)
    'FV':   ('Flödesventil / slutlig enhet',     'Ventil'),
    'LV':   ('Nivåventil / slutlig enhet',       'Ventil'),
    'PV':   ('Tryckventil / slutlig enhet',      'Ventil'),
    'TV':   ('Temperaturventil / slutlig enhet', 'Ventil'),
    'XCV':  ('Projektdef. styr-/on-off-ventil',  'Ventil'),
    # Instrument – Lägesbrytare
    'ZSC':  ('Lägesbrytare stängd',              'Instrument / Sensor'),
    'ZSO':  ('Lägesbrytare öppen',               'Instrument / Sensor'),
    'ZT':   ('Lägegstransmitter',                'Instrument / Sensor'),
    # Instrument – Solenoid / pilot
    'SOV':  ('Magnetventil / pilotventil',       'Ventil'),
    # Övrigt / mekanisk utrustning
    'M':    ('Motor / Drivverk',                 'Övrigt'),
    'AG':   ('Omrörare / Agitator',             'Övrigt'),
    'MX':   ('Blandare',                         'Övrigt'),
    'G':    ('Generator',                        'Övrigt'),
    'TR':   ('Transformator',                    'Övrigt'),
    'BRN':  ('Brännare',                         'Övrigt'),
    'IG':   ('Tändare',                          'Övrigt'),
    # LKAB RDS-PP specifika koder
    'HXA':  ('Reaktor / omrörd tank (LKAB)',     'Övrigt'),
    'HMA':  ('Mixer / blandare (LKAB)',          'Övrigt'),
    'CMA':  ('Tank / kärl (LKAB)',              'Övrigt'),
    'EGC':  ('Värmeväxlare (LKAB)',             'Övrigt'),
    'HQB':  ('Filter (LKAB)',                    'Övrigt'),
    'HSB':  ('Skrubber (LKAB)',                  'Övrigt'),
    'GLD':  ('Matare / doserare (LKAB)',         'Övrigt'),
    'GLA':  ('Transportband (LKAB)',             'Övrigt'),
    'CLB':  ('Silo / ficka (LKAB)',             'Övrigt'),
    'GQB':  ('Fläkt / blåsmaskin (LKAB)',       'Övrigt'),
    'WPC':  ('Ledning / rörledning (LKAB)',      'Övrigt'),
    'FLA':  ('Flödesgivare (LKAB FLA)',          'Instrument / Sensor'),
    'HMC':  ('Cyklon (LKAB HMC)',               'Övrigt'),
    # ITS-specifika koder
    'XFB':  ('Rörledningssegment / block (ITS)', 'Övrigt'),
    'XSS':  ('Säkerhetssystem (ITS)',            'Övrigt'),
    # Sunpine-specifika koder (lärs in vid markering)
    'LX':   ('Nivågivare (Sunpine LX)',          'Instrument / Sensor'),
    'OX':   ('Syreanalysator (Sunpine OX)',      'Instrument / Sensor'),
    'AX':   ('Analysgivare (Sunpine AX)',        'Instrument / Sensor'),
    'GX':   ('Generell givare (Sunpine GX)',     'Instrument / Sensor'),
    'DX':   ('Differentialgivare (Sunpine DX)',  'Instrument / Sensor'),
    'MAS':  ('Processenhet (Sunpine MAS)',       'Övrigt'),
    'EAS':  ('Processenhet (Sunpine EAS)',       'Övrigt'),
    'MSS':  ('Processenhet (Sunpine MSS)',       'Övrigt'),
    'DCS':  ('Styrsystem (Sunpine DCS)',         'Övrigt'),
    'ESS':  ('Elsystem (Sunpine ESS)',           'Övrigt'),
    'TA':   ('Temperaturlarm (TA)',              'Instrument / Sensor'),
}

def _spatial_combine(words: list, gap_limit: float = 18.0) -> list:
    """Combine spatially adjacent word-tokens into candidate tag strings.

    Words that lie on the same baseline and are separated by less than
    `gap_limit` PDF units (or are single-char separators like '-' or '.')
    are joined without space.  Yields (text, x0, y0, x1, y1) tuples — the
    bounding box is the combined group's box (first token's x0/y0, last
    token's x1, first token's y1), so callers that need a placement
    position (e.g. scan_pdf_for_equipment) don't lose it, same as callers
    that only want the text and ignore the trailing coordinates.

    Some CAD exports render the SAME text 2-3 times at the byte-for-byte
    identical bounding box (a bold-simulation trick, confirmed on a real
    ITS P&ID title block: "Checked"/"Drawn"/etc. each appeared 3 times
    at identical coordinates). Their gap (next token's x0 minus the
    group's x1) is then negative and well under gap_limit, so without a
    check these get silently concatenated into e.g. "CHECKEDCHECKEDCHECKED"
    — a real, previously-unnoticed source of garbage tag-candidate noise.
    Such an exact-position repeat of the immediately preceding token is
    treated as a rendering duplicate and skipped, not appended.

    words: list of (x0, y0, x1, y1, text) tuples from page.get_text("words")
    """
    if not words:
        return []

    # Sort in reading order: row (rounded), then x
    sw = sorted(words, key=lambda w: (round((w[1] + w[3]) / 2 / 8) * 8, w[0]))

    results = []
    seen = set()
    i = 0
    while i < len(sw):
        x0, y0, x1, y1, text = sw[i][:5]
        group = [text]
        grp_x1 = x1
        grp_y1 = y1
        y_mid = (y0 + y1) / 2
        prev_x0, prev_y0, prev_x1 = x0, y0, x1   # bbox of the last token
                                                   # actually appended (for
                                                   # duplicate detection —
                                                   # NOT the group's overall
                                                   # start, which stays fixed)

        j = i + 1
        while j < len(sw):
            nx0, ny0, nx1, ny1, ntext = sw[j][:5]
            ny_mid = (ny0 + ny1) / 2

            # Must be on same line (within ~5 PDF units vertically)
            if abs(y_mid - ny_mid) > 5:
                break

            # Exact-position duplicate of the token just processed (a
            # bold-simulation rendering artifact, see docstring) — skip
            # it entirely rather than treating it as an adjacent word.
            # Compared against the PREVIOUS token's own bbox, not the
            # group's overall start — a duplicate can occur anywhere in
            # an already multi-word group (e.g. "MANIFOLD pressure
            # pressure PHC PHC..."), not just right after the first word.
            if (ntext == group[-1] and abs(nx0 - prev_x0) <= 0.5
                    and abs(ny0 - prev_y0) <= 0.5 and abs(nx1 - prev_x1) <= 0.5):
                j += 1
                continue

            gap = nx0 - grp_x1
            is_sep = ntext.strip() in ('-', '.', '/', '_')

            # Combine if gap is small OR token is a separator char
            if gap <= gap_limit or is_sep:
                group.append(ntext)
                grp_x1 = nx1
                grp_y1 = ny1
                prev_x0, prev_y0, prev_x1 = nx0, ny0, nx1
                j += 1
            else:
                break

        combined = ''.join(group)
        if combined and combined not in seen:
            seen.add(combined)
            results.append((combined, x0, y0, grp_x1, grp_y1))
        # Also yield the first token alone (in case only part is a tag)
        if text not in seen:
            seen.add(text)
            results.append((text, x0, y0, x1, y1))
        i = j if j > i + 1 else i + 1

    return results


def _rotate_words(words, page):
    """Transform page.get_text("words") tuples from PyMuPDF's raw
    (unrotated mediabox) coordinate space into this app's "PDF space" —
    the ROTATED space matching page.rect, which is what page.get_pixmap()
    renders and what PIDGraphicsView.pdf_to_scene()/scene_to_pdf() assume
    for every marker placed in this app.

    get_text() (like get_drawings(), see symbol_geometry.extract_primitives)
    never applies page rotation itself — confirmed by rendering a crop at
    an un-transformed word position on a real rotated P&ID (182036 Hybrit,
    which uses /Rotate 270) and finding it did not line up with the text
    until page.rotation_matrix was applied. A no-op for the (far more
    common) unrotated-page case, since rotation_matrix is then the
    identity matrix.
    """
    mat = page.rotation_matrix
    out = []
    for w in words:
        p0 = fitz.Point(w[0], w[1]) * mat
        p1 = fitz.Point(w[2], w[3]) * mat
        out.append((min(p0.x, p1.x), min(p0.y, p1.y),
                    max(p0.x, p1.x), max(p0.y, p1.y)) + tuple(w[4:]))
    return out


def _clean_for_tag(text: str) -> str:
    """Strip OCR artefacts — keep only characters that can appear in P&ID tags.

    '###HV#####'  →  'HV'
    '##PSV-101##' →  'PSV-101'
    'V - 101'     →  'V-101'
    """
    # Uppercase first
    text = text.upper()
    # Remove anything that can't be part of a tag (only A-Z, 0-9, -, ., /)
    cleaned = re.sub(r'[^A-Z0-9\-./]', ' ', text)
    # Collapse multiple spaces and strip
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def _collapse_spaces(text: str) -> str:
    """Remove spaces between individual letters/digits — fixes OCR spacing.

    'P C V - 1 0 1'  →  'PCV-101'
    'F T 2 0 1 A'    →  'FT201A'   (then _parse_tag normalises to 'FT-201A')
    Multi-word text is left alone (spaces between real words are kept).
    """
    # Only collapse if every token is 1-2 chars (= spaced-out tag, not words)
    tokens = text.strip().split()
    if not tokens:
        return text
    if all(len(t) <= 2 for t in tokens):
        return ''.join(tokens)
    # Also try just removing spaces around dashes and dots
    collapsed = re.sub(r'\s*[-./]\s*', '-', text)
    collapsed = re.sub(r'(?<=[A-Z0-9])\s+(?=[A-Z0-9])', '', collapsed)
    return collapsed


def _pick_best_tag(text: str) -> str:
    """Return the best equipment-tag match from arbitrary text, or ''.

    Prefers full extended tags (with area prefix) over bare tags.
    """
    if not text:
        return ''
    text = text.strip().upper()

    for candidate in _tag_candidates(text):
        # 1. Extended tag with area prefix — return as-is (preserve original separators)
        m = _EXT_TAG_RE.search(candidate)
        if m:
            return m.group(1)
        # 2. Simple tag (letter code + number)
        matches = _FULL_TAG_RE.findall(candidate)
        if matches and len(matches[0]) >= 2:
            return f"{matches[0][0]}-{matches[0][1]}"
        tag, _ = _parse_tag(candidate)
        if tag:
            return tag
    return ''


_OCR_CONFUSION_PAIRS = [
    ('O', '0'), ('I', '1'), ('L', '1'), ('S', '5'), ('B', '8'), ('G', '6'), ('Z', '2'),
]


def _ocr_fuzzy_variants(text: str) -> list:
    """Generate additional OCR-confusion candidate strings, one character
    position varied at a time against _OCR_CONFUSION_PAIRS — linear in tag
    length, not combinatorial. Broader than _fix_ocr_common_errors (which
    only handles 0<->O/1<->I on a single fixed prefix/suffix shape).
    OCR-only: native PDF text doesn't have this failure mode."""
    text = text.upper().strip()
    variants = []
    seen = {text}
    for i, ch in enumerate(text):
        for a, b in _OCR_CONFUSION_PAIRS:
            if ch == a:
                repl = b
            elif ch == b:
                repl = a
            else:
                continue
            variant = text[:i] + repl + text[i + 1:]
            if variant not in seen:
                seen.add(variant)
                variants.append(variant)
    return variants


def _ocr_tag_candidates(raw_text: str, known_prefixes=None) -> list:
    """All candidate strings to try for one OCR word, in preference order:
    the existing single-answer correction and raw uppercased text first
    (unchanged from before — same two candidates, same order), then
    broader single-character-confusion variants, ranked so a variant
    whose prefix is a known equipment prefix comes first — a plausibility
    tie-break, never a semantic guess."""
    corrected = _fix_ocr_common_errors(raw_text)
    base = [corrected, raw_text.upper()]
    fuzzy = _ocr_fuzzy_variants(raw_text)
    if known_prefixes:
        fuzzy.sort(key=lambda v: 0 if _equip_prefix_from_tag(v) in known_prefixes else 1)
    candidates = []
    for c in base + fuzzy:
        if c not in candidates:
            candidates.append(c)
    return candidates


def _tag_candidates(text: str) -> list:
    """Return a prioritised list of text variants to try when parsing a tag."""
    candidates = [text]
    # 1. Strip OCR artefacts (###HV### → HV)
    cleaned = _clean_for_tag(text)
    if cleaned and cleaned != text:
        candidates.append(cleaned)
    # 2. Collapse spaces (P C V - 1 0 1 → PCV-101)
    collapsed = _collapse_spaces(text)
    if collapsed and collapsed not in candidates:
        candidates.append(collapsed)
    # 3. Collapse spaces on already-cleaned text
    if cleaned:
        cc = _collapse_spaces(cleaned)
        if cc and cc not in candidates:
            candidates.append(cc)
    return candidates


def _extract_prefix(tag: str) -> str:
    """Extract the letter prefix from an equipment tag like 'PCV-101' → 'PCV'."""
    m = re.match(r'^([A-Z]+)', tag)
    return m.group(1) if m else tag


def _clean_tag_for_popup(tag: str) -> str:
    """Strip leading area/module codes so the Tag-ID field shows only the
    instrument-code part that actually identifies the equipment type.

    Numbers and single-letter+digit area codes are purely location metadata
    and should not appear in the HAZOP tag field.

        '=E1.M1.GPA4'    -> 'GPA4'   LKAB module hierarchy stripped
        '=M1.WPA001'     -> 'WPA001'
        '2818-LX79'      -> 'LX79'   Sunpine drawing-number stripped
        '100-MAS10A'     -> 'MAS10A' Sunpine area code stripped
        'G45-100-EAS10A' -> 'EAS10A' two area segments stripped
        '60-RV-009'      -> 'RV-009' numeric area stripped
        'PU0050'         -> 'PU0050' already clean
        'HV-101'         -> 'HV-101' already clean
        'XFB_31304'      -> 'XFB_31304' already clean
    """
    t = tag.lstrip('=').strip()
    if not t:
        return tag

    sep_re = re.compile(r'[-./_ ]')
    parts = sep_re.split(t)

    def _leading(s):
        m = re.match(r'^([A-Z]+)', s.upper())
        return m.group(1) if m else ''

    # Peel off leading segments whose letter portion is < 2 chars
    # (pure digits, single-letter area codes like E1/M1/K2, short area codes)
    while len(parts) > 1 and len(_leading(parts[0])) < 2:
        parts = parts[1:]

    return '-'.join(parts) if len(parts) > 1 else parts[0]


def _words_from_native(fitz_page):
    """Extract (text, cx, cy) from a PDF page using PyMuPDF word list."""
    words = _rotate_words(fitz_page.get_text("words"), fitz_page)
    return [(w[4].strip().upper(), (w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
            for w in words if w[4].strip()]


def _tags_from_full_text(fitz_page, page_num: int) -> list:
    """Search the full text of a page with regex; returns [(tag, prefix, cx, cy)]."""
    # Use 'rawdict' to get character-level positions for precise x,y
    full_text = fitz_page.get_text("text")
    results = []
    for m in _FULL_TAG_RE.finditer(full_text):
        raw = m.group(0)
        tag, prefix = _parse_tag(raw)
        if tag and prefix:
            results.append((tag, prefix, 0.0, 0.0))  # positions approximated below
    return results


def _parse_tag(text: str):
    """Normalise an equipment tag string to (normalised_tag, prefix).

    Handles all plant conventions found in analysed P&IDs:
      Standard:   PCV-101, FT201A, V-1, PSV-101A
      Area-prefix: 20-PCV-101, 10/FT201, 60-RV-009
      Dot:        PCV.101, M1.GPA4, E1.M1.WPA001
      Underscore: XFB_31304, TK_11338
      Sunpine:    2818-LX79, 100-MAS10A, G45-100-EAS10A, HV0063
      Zero-padded: HV0063, TA0058, PU0050
      Pipe-size:  2"LS60.002 (stripped)
      RDS-PP:     =E1.M1.GPA4 (= stripped)

    Returns (normalised_tag, prefix) or (None, None).
    """
    text = text.strip().upper()
    if not text:
        return None, None

    # Strip RDS-PP '=' prefix and pipe-size prefix
    text = text.lstrip('=')
    text = re.sub(r'^\d+["\']+', '', text)
    text = re.sub(r'^DN\d+[-./_ ]?', '', text)

    if not text:
        return None, None

    # --- Extended compound tags (area prefix + instrument code) ---
    m = _EXT_TAG_RE.search(text)
    if m:
        candidate = m.group(1).lstrip('=')
        pfx = _equip_prefix_from_tag(candidate)
        if pfx:
            # Normalise separators in the instrument portion only
            norm = candidate.replace('_', '-').replace('.', '-').replace('/', '-')
            norm = re.sub(r'-+', '-', norm).strip('-')
            return norm, pfx

    # --- Strip numeric area prefix: 20-PCV-101 → PCV-101 ---
    am = _AREA_TAG_RE.match(text)
    if am:
        text = f"{am.group(1)}-{am.group(2)}"

    # --- Simple well-formed tag (with or without separator) ---
    if _TAG_RE.match(text):
        norm = re.sub(r'[./_]', '-', text)
        norm = re.sub(r'-+', '-', norm)
        m2 = re.match(r'^([A-Z]{1,6})-(\d{1,5}[A-Z]{0,3})$', norm)
        if not m2:
            m2 = re.match(r'^([A-Z]{1,6})(\d{1,5}[A-Z]{0,3})$', norm)
            if m2:
                norm = f"{m2.group(1)}-{m2.group(2)}"
        return norm, _extract_prefix(norm)

    # --- No separator: PCV101, FT201A, HV0063 ---
    m = re.match(r'^([A-Z]{2,6})(\d{1,5}[A-Z]{0,3})$', text)
    if m:
        return f"{m.group(1)}-{m.group(2)}", m.group(1)

    # --- Embedded area number: LS60.002, TIA46.003 (letters+area+sep+number) ---
    m = re.match(r'^([A-Z]{2,6})(\d{1,3})[./_ -](\d{1,4}[A-Z]{0,2})$', text)
    if m:
        code, area, num = m.group(1), m.group(2), m.group(3)
        return f"{code}-{area}.{num}", code

    # --- Last resort: extract prefix and use raw tag ---
    pfx = _equip_prefix_from_tag(text)
    if pfx and len(pfx) >= 2:
        return text, pfx

    return None, None


def scan_pdf_for_equipment(pdf_doc, use_ocr: bool = False,
                           ocr_engine: str = 'auto',
                           progress_callback=None) -> dict:
    """Scan all pages of a PDF for equipment tags.

    Strategy per page:
      1. Full-text regex search (catches tags in paragraphs / annotations).
      2. Word-by-word matching (standalone tags with precise positions).
      3. If use_ocr=True: always run OCR and merge results.
         OCR finds tags that are part of raster graphics or vector-only layers.

    Returns:
        {prefix: {'tags': [str], 'pages': {tag: int}, 'positions': {tag: (x,y)},
                  'ocr_pages': set_of_page_nums},
         '_meta': {...}}
    """
    if not HAS_PYMUPDF or pdf_doc is None:
        return {}

    result: dict = {}
    ocr_engine_used = None

    def _add(tag, prefix, page_num, cx, cy, from_ocr=False, source='native'):
        if prefix not in result:
            result[prefix] = {'tags': [], 'pages': {}, 'positions': {},
                              'ocr_pages': set(), 'tag_source': {}}
        if tag not in result[prefix]['tags']:
            result[prefix]['tags'].append(tag)
        # First-seen wins for page assignment
        result[prefix]['pages'].setdefault(tag, page_num)
        result[prefix]['positions'].setdefault(tag, (cx, cy))
        # 'native' | 'ocr' | 'ocr_fuzzy' — how this tag was actually read;
        # feeds tag_reading_confidence in detect_equipment_and_valves()
        # without needing per-word confidence scores from any OCR engine.
        result[prefix]['tag_source'].setdefault(tag, source)
        if from_ocr:
            result[prefix]['ocr_pages'].add(page_num)

    for page_num in range(pdf_doc.page_count):
        page = pdf_doc.load_page(page_num)
        if progress_callback:
            progress_callback(
                page_num, pdf_doc.page_count,
                f"Sida {page_num + 1}/{pdf_doc.page_count} — nativ text…")

        # ── Pass 1: full-text regex ───────────────────────────────────────────
        full_text = page.get_text("text")
        for m in _FULL_TAG_RE.finditer(full_text):
            raw = m.group(0)
            tag, prefix = _parse_tag(raw)
            if tag and prefix:
                _add(tag, prefix, page_num, 0.0, 0.0, from_ocr=False)

        # ── Pass 2: spatially-combined words (precise x,y positions) ─────────
        # Rejoins tags the PDF split into several text objects (e.g.
        # "20" "-" "PCV" "-" "101") and matches via _pick_best_tag, which
        # has no minimum-prefix-length gate — unlike a bare _parse_tag call,
        # this also catches single-letter-prefix tags with no separator
        # (P101, T12, E205), which used to be silently dropped here. Same
        # technique "Analysera P&ID" (_analyze_pid) already uses.
        raw_words = _rotate_words(page.get_text("words"), page)
        tags_from_native = 0
        for candidate, cx0, cy0, cx1, cy1 in _spatial_combine(raw_words, gap_limit=22.0):
            tag = _pick_best_tag(candidate)
            if not tag:
                continue
            prefix = _equip_prefix_from_tag(tag)
            if not prefix:
                continue
            cx, cy = (cx0 + cx1) / 2, (cy0 + cy1) / 2
            # Overwrite position with precise coords
            if prefix in result and tag in result[prefix]['tags']:
                result[prefix]['positions'][tag] = (cx, cy)
            else:
                _add(tag, prefix, page_num, cx, cy, from_ocr=False)
            tags_from_native += 1

        # ── Pass 3: OCR ───────────────────────────────────────────────────────
        # Always run when requested — OCR finds tags that live in vector graphics
        if use_ocr:
            if progress_callback:
                progress_callback(
                    page_num, pdf_doc.page_count,
                    f"Sida {page_num + 1}/{pdf_doc.page_count} — OCR (skala 4×)…")

            # Use 4× scale for better small-text recognition
            ocr_words, engine_name = _ocr_page(page, scale=4.0, engine=ocr_engine)
            if engine_name:
                ocr_engine_used = engine_name

            native_tag_set = {
                result[p]['tags'][i]
                for p in result
                for i in range(len(result[p].get('tags', [])))
                if result[p]['pages'].get(result[p]['tags'][i]) == page_num
            }

            for raw_text, cx, cy in (ocr_words or []):
                candidates = _ocr_tag_candidates(raw_text, known_prefixes=KNOWN_PREFIXES)
                for i, candidate in enumerate(candidates):
                    tag, prefix = _parse_tag(candidate)
                    if tag and prefix and tag not in native_tag_set:
                        # First 2 candidates are the pre-existing
                        # (corrected, raw_text.upper()) pair — unchanged
                        # behaviour; anything after that is a broader
                        # fuzzy character-confusion guess (item 2).
                        source = 'ocr' if i < 2 else 'ocr_fuzzy'
                        _add(tag, prefix, page_num, cx, cy, from_ocr=True, source=source)
                        native_tag_set.add(tag)
                        break

    for prefix in result:
        result[prefix]['tags'].sort(key=lambda t: (t[:re.search(r'\d', t).start()],
                                                    int(re.search(r'\d+', t).group()))
                                    if re.search(r'\d', t) else (t, 0))

    result['_meta'] = {
        'ocr_used':   any(result[p].get('ocr_pages') for p in result if not p.startswith('_')),
        'ocr_engine': ocr_engine_used,
        'total_tags': sum(len(result[p]['tags']) for p in result if not p.startswith('_')),
    }
    return result


# Prefix → component-category mapping used to cross-reference a scanned
# prefix against the tag database's free-text 'category' field. Shared by
# both apply_scan_result_to_equipment_catalog and
# upsert_identified_tags_from_scan so "🔍 Skanna P&ID" and "📋 Analysera
# P&ID" derive the same suggested component type from the same scan.
_SCAN_CAT_MAP = {
    'instrument': 'Instrument / Sensor', 'givare': 'Instrument / Sensor',
    'reglerfunktion': 'Instrument / Sensor', 'larm': 'Instrument / Sensor',
    'ventil': 'Ventil', 'reglerventil': 'Ventil',
    'pump': 'Pump', 'kompressor': 'Kompressor',
    'tank': 'Tank / Kärl', 'kärl': 'Tank / Kärl',
    'värmeväxlare': 'Värmeväxlare',
    'säkerhetsventil': 'Säkerhetsventil (PSV)',
}


def apply_scan_result_to_equipment_catalog(db, scan_result):
    """Replace equipment_catalog with a scan_pdf_for_equipment() result.

    Shared by "🔍 Skanna P&ID" (EquipmentPanel._scan, hazop.py) and
    "📋 Analysera P&ID" (PIDPanel._analyze_pid) now that both trigger the
    same underlying scan — whichever button is used, the equipment
    register ends up with the same rows. Matches EquipmentPanel._scan's
    pre-existing full-rescan-replaces-catalog behavior: per-tag
    descriptions typed manually are not preserved across a rescan (this
    was already true for "Skanna P&ID" before the two scans were merged).
    """
    real = {k: v for k, v in scan_result.items() if not k.startswith('_')}
    db.clear_equipment_catalog()
    for prefix, data in real.items():
        known      = KNOWN_PREFIXES.get(prefix, ('', ''))
        saved_type = db.get_equipment_type(prefix) if hasattr(db, 'get_equipment_type') else ''
        eq_type    = saved_type or (known[1] if known else '')
        ocr_pages  = data.get('ocr_pages', set())
        for tag in data['tags']:
            page   = data['pages'].get(tag, 0)
            is_ocr = int(page in ocr_pages)
            db.add_equipment_item(tag, tag, prefix, page, eq_type, '', is_ocr)


def upsert_identified_tags_from_scan(db, scan_result):
    """Cross-reference a scan_pdf_for_equipment() result into the per-prefix
    pid_identified_tags table (Settings → "Identifierade objekt").

    Shared by both scan entry points so that panel stays in sync
    regardless of whether the scan was triggered from "🔍 Skanna P&ID" or
    "📋 Analysera P&ID" — previously only the latter wrote here.
    """
    real = {k: v for k, v in scan_result.items() if not k.startswith('_')}
    for prefix, data in real.items():
        tags = data.get('tags') or []
        if not tags:
            continue
        examples  = ', '.join(sorted(tags)[:6])
        db_entry  = db.tag_code_lookup(prefix) if hasattr(db, 'tag_code_lookup') else {}
        name_sv   = (db_entry or {}).get('name_sv', '')
        comp_type = ''
        if db_entry:
            cat = str(db_entry.get('category', '')).lower()
            for k, v in _SCAN_CAT_MAP.items():
                if k in cat:
                    comp_type = v; break
        if not comp_type and prefix in KNOWN_PREFIXES:
            comp_type = KNOWN_PREFIXES[prefix][1]
        db.upsert_pid_tag(prefix, examples, name_sv, comp_type)


def find_tag_position_on_page(pdf_doc, page_num, tag):
    """Locate where a specific (already-known) tag is printed on a page.

    Mirrors scan_pdf_for_equipment's pass-2 word matching. Used to get a
    live x,y for a tag already sitting in equipment_catalog — that table
    does not persist positions itself — before searching for vector-drawn
    symbol clusters around it. Returns (x, y) in PDF points, or None.
    """
    if not HAS_PYMUPDF or pdf_doc is None:
        return None
    try:
        page = pdf_doc[page_num]
    except Exception:
        return None
    target = tag.strip().upper()
    for text, cx, cy in _words_from_native(page):
        parsed_tag, _prefix = _parse_tag(text)
        if parsed_tag and parsed_tag.upper() == target:
            return (cx, cy)
    return None


def find_nearby_tag_text(page, point, radius=150.0):
    """Find the closest recognizable tag-like text near a point on a page.

    The inverse of find_tag_position_on_page: that one starts from an
    ALREADY-KNOWN tag and finds its symbol; this one starts from a symbol
    (e.g. a bow-tie-shaped cluster found by find_valve_shapes) that has no
    linked tag yet, and suggests one from whatever text is printed nearby.
    Uses the same spatially-combined matching as scan_pdf_for_equipment so
    it catches tags split across multiple text objects.

    Returns (tag, prefix), or (None, None) if nothing plausible is within
    radius.
    """
    raw_words = _rotate_words(page.get_text("words"), page)
    best = (None, None)
    best_dist = radius
    for candidate, x0, y0, x1, y1 in _spatial_combine(raw_words, gap_limit=22.0):
        tag = _pick_best_tag(candidate)
        if not tag:
            continue
        prefix = _equip_prefix_from_tag(tag)
        if not prefix:
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        d = math.hypot(cx - point[0], cy - point[1])
        if d <= best_dist:
            best_dist = d
            best = (tag, prefix)
    return best


def detect_equipment_symbols(pdf_doc, requests, min_confidence=0.3):
    """Run geometric symbol detection for a batch of (tag, page, comp_type)
    requests — one entry per checked row in the Utrustningsregister.

    For each request: locate the tag's current position on its page, find
    nearby vector-drawn symbol clusters (symbol_geometry.find_symbol_clusters),
    and resolve which one (if any) the tag is linked to via
    leader-line > containment > nearest > none
    (symbol_geometry.resolve_tag_symbol).

    Type (valve/pump/...) is NOT guessed from the shape here — comp_type is
    passed straight through from the caller's KNOWN_PREFIXES-derived value
    for display in the review dialog. The geometry only answers "is there a
    drawn symbol here, and exactly where/what shape" for placing an accurate
    marker and confirming the tag-symbol link.

    Returns a list of dicts (one per request, same order):
        {tag, page, comp_type, x, y, confidence, link_method, outline}
    link_method is one of 'leader'|'contain'|'nearest'|'none'|'not_found'
    ('not_found' means the tag text itself could not be located on the page —
    e.g. it moved or the page number is stale).
    """
    if not HAS_PYMUPDF or pdf_doc is None:
        return []

    results = []
    clusters_by_page = {}   # page_num -> (primitives, clusters), computed once per page
    for tag, page_num, comp_type in requests:
        if page_num not in clusters_by_page:
            try:
                page = pdf_doc[page_num]
                prims = symbol_geometry.extract_primitives(page)
                clusters = symbol_geometry.find_symbol_clusters(page, min_confidence=min_confidence)
            except Exception:
                prims, clusters = [], []
            clusters_by_page[page_num] = (prims, clusters)
        prims, clusters = clusters_by_page[page_num]

        tag_pos = find_tag_position_on_page(pdf_doc, page_num, tag)
        if tag_pos is None:
            results.append({'tag': tag, 'page': page_num, 'comp_type': comp_type,
                             'x': 0.0, 'y': 0.0, 'confidence': 0.0,
                             'link_method': 'not_found', 'outline': []})
            continue

        cluster, method = symbol_geometry.resolve_tag_symbol(tag_pos, clusters, prims)
        if cluster is not None:
            x0, y0, x1, y1 = cluster['bbox']
            results.append({'tag': tag, 'page': page_num, 'comp_type': comp_type,
                             'x': (x0 + x1) / 2, 'y': (y0 + y1) / 2,
                             'confidence': cluster['confidence'],
                             'link_method': method, 'outline': cluster['outline']})
        else:
            # No symbol cluster nearby at all — fall back to the tag's own
            # text position so a marker can still be placed and reviewed.
            results.append({'tag': tag, 'page': page_num, 'comp_type': comp_type,
                             'x': tag_pos[0], 'y': tag_pos[1], 'confidence': 0.0,
                             'link_method': 'none', 'outline': []})
    return results


def find_valve_shapes(pdf_doc, pages=None, min_bowtie_score=0.5, progress_callback=None):
    """Scan pages for bow-tie-shaped (valve) symbols, independent of
    whether a tag is already known nearby.

    The counterpart to detect_equipment_symbols(): that one starts from an
    already-scanned tag and looks for its symbol; this one starts from the
    SHAPE (symbol_geometry.bowtie_score) and works backwards to suggest a
    tag via find_nearby_tag_text() when something is printed close enough
    — leaving the tag empty (for the review dialog to fill in manually)
    when nothing plausible is nearby, e.g. a valve whose tag wasn't
    picked up by text scanning at all, or that genuinely has none.

    pages: iterable of 0-based page numbers, or None for all pages.

    Returns a list of dicts in the same shape detect_equipment_symbols()
    uses (so EquipmentMarkerReviewDialog needs no changes to display
    them): {tag, page, comp_type, x, y, confidence, link_method, outline}.
    link_method is always 'shape' here; confidence is the bow-tie score
    itself (0..1) — NOT find_symbol_clusters' generic "is this a symbol
    at all" score, which is only used as a pre-filter via
    min_confidence=0.0 (consider every cluster; some real bow-ties score
    low on the generic classifier's own unrelated features).

    bowtie_score alone is not enough on real drawings: title-block grid
    intersections and stray small line-crossings can also produce a
    "wide-narrow-wide" point cloud and score just as high as a real valve
    (found while verifying against real P&ID ref/ files — a title block
    on one Hybrit sheet alone produced 13 shapes scoring 0.9-1.0). What
    reliably tells them apart is size and shape *relative to the page*,
    which bowtie_score deliberately ignores: those false positives were
    all far smaller than the page's own text (norm_size well under 1.5)
    or wildly elongated (a long pipe run's aspect ratio, not a compact
    symbol's). aspect<=3.0 and 1.5<=norm_size<=40.0 reuse
    classify_cluster's own established "plausible discrete symbol" bounds
    (not new thresholds) to filter those out while keeping real bow-ties,
    which are always compact and comparable in size to the page's text —
    confirmed against several real files after this filter was added:
    correctly dropped to 0 false hits on the noisy Hybrit title-block
    page, and correctly kept real valve symbols (visually confirmed) on
    ITS/Smurfit Kappa/Loket P&IDs.

    A second, independent filter requires at least one diagonal line
    segment (symbol_geometry.cluster_features' has_diagonal) — piping is
    drawn strictly horizontally/vertically by P&ID convention, so only a
    symbol's own geometry (a bow-tie's triangle edges) can ever contain a
    diagonal segment; pipe crossings, title-block grids, and instrument-
    bubble stems cannot.

    A third filter requires has_closed_loop (symbol_geometry._has_closed_loop)
    — a real bow-tie always contains at least one closed loop of edges,
    however it's drawn (a single self-intersecting quad, closed/filled
    triangle paths, or — confirmed on real Sunpine/Swerim/ITS P&IDs — two
    triangles drawn as separate UNCLOSED line segments with no fill,
    where only the segments' endpoints forming a cycle prove it's really
    closed). Found on a real LKAB P&ID: the vector-drawn letter "M"
    (motor label text drawn as outline strokes, not searchable text) has
    two verticals meeting at a point in the middle — an open zigzag that
    reads as a textbook wide-narrow-wide silhouette (bowtie_score 0.85)
    but never closes into a loop, unlike every confirmed real valve on
    that page. The same check also rejected a nearby pump assembly
    (motor + impeller circle) that had bridged into one cluster with
    that same "M". An earlier, simpler version of this filter
    (has_closed_or_filled — require an explicit closed=True/filled=True
    primitive) rejected those false positives too, but ALSO rejected a
    large fraction of genuine valves on Sunpine/Swerim/ITS files that use
    the open-stroke convention — has_closed_loop replaced it for that
    reason.
    """
    if not HAS_PYMUPDF or pdf_doc is None:
        return []
    if pages is None:
        pages = range(pdf_doc.page_count)

    results = []
    for page_num in pages:
        if progress_callback:
            progress_callback(
                page_num, pdf_doc.page_count,
                f"Sida {page_num + 1}/{pdf_doc.page_count} — söker ventilformer…")
        try:
            page = pdf_doc[page_num]
            clusters = symbol_geometry.find_symbol_clusters(page, min_confidence=0.0)
        except Exception:
            continue

        for cluster in clusters:
            score = cluster.get('bowtie_score', 0.0)
            if score < min_bowtie_score:
                continue
            if cluster['aspect'] > 3.0 or not (1.5 <= cluster['norm_size'] <= 40.0):
                continue
            if not cluster['has_diagonal']:
                # Piping is drawn strictly horizontally/vertically by P&ID
                # convention — a cluster with no diagonal line at all can
                # only be pipe crossings, a title-block grid, or an
                # instrument-bubble's straight stems, never an actual
                # bow-tie valve body (whose triangle edges are diagonal).
                continue
            if not cluster['has_closed_loop']:
                # A real bow-tie always contains a closed loop of edges.
                # Rejects e.g. a vector-drawn "M" (motor) label glyph — its open zigzag
                # strokes coincidentally read as wide-narrow-wide too.
                continue
            x0, y0, x1, y1 = cluster['bbox']
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            tag, _prefix = find_nearby_tag_text(page, (cx, cy))
            results.append({
                'tag': tag or '', 'page': page_num, 'comp_type': 'Ventil',
                'x': cx, 'y': cy, 'confidence': score,
                'link_method': 'shape', 'outline': cluster['outline'],
            })
    results.sort(key=lambda r: -r['confidence'])
    return results


# ══════════════════════════════════════════════════════════════════════════
# Unified tag+shape valve detection — replaces detect_equipment_symbols()
# and find_valve_shapes() as the UI's entry point (both functions above are
# left unchanged and still directly callable/testable). Those two used to
# extract vector primitives/clusters independently per page and never
# cross-checked results, so the same physical valve could be reported
# twice; here every page's clusters are extracted exactly ONCE and shared
# between tag-association and shape-hunting, so that can't happen by
# construction. See NOTES.md, "Fas 1+2" (2026-08-06).
# ══════════════════════════════════════════════════════════════════════════

_ASSOC_KNOWN_PREFIX_BONUS = 0.10
_ASSOC_MIN_SCORE = 0.25


def _dominant_link_method(tag_point, cluster, primitives):
    """Which geometric signal explains an accepted (tag, cluster) pair —
    for display continuity with resolve_tag_symbol's existing
    leader/contain/nearest vocabulary in EquipmentMarkerReviewDialog."""
    if symbol_geometry.find_leader_line(tag_point, [cluster], primitives) is not None:
        return 'leader'
    x0, y0, x1, y1 = cluster['bbox']
    tol = 5.0
    if (x0 - tol) <= tag_point[0] <= (x1 + tol) and (y0 - tol) <= tag_point[1] <= (y1 + tol):
        return 'contain'
    return 'nearest'


def associate_tags_to_clusters(tag_points, clusters, primitives,
                                known_prefixes=None, search_radius=220.0,
                                min_score=_ASSOC_MIN_SCORE):
    """Global (not independent-per-tag) tag<->symbol-cluster assignment for
    one page — replaces resolve_tag_symbol's fixed leader>contain>nearest
    cascade, which resolves each tag on its own and so has no way to stop
    two different tags both claiming the same cluster.

    tag_points: [(tag, prefix, (x, y)), ...] for ONE page.
    clusters/primitives: symbol_geometry.find_symbol_clusters()/
        extract_primitives() output for that SAME page.

    Scores every (tag, cluster) pair via symbol_geometry.score_tag_cluster_link,
    adding +_ASSOC_KNOWN_PREFIX_BONUS when the tag's prefix is in
    known_prefixes (tag-format plausibility — pure geometry can't see
    this, so the bonus is applied here rather than in symbol_geometry.py,
    which stays free of any tag-naming-convention knowledge). Then assigns
    greedily by descending score, removing BOTH the tag and the cluster
    from the pool the instant a pair is accepted — this is what actually
    prevents double-assignment.

    No scipy/Hungarian-algorithm dependency: the leader-line term (weight
    0.50 of 1.0) dominates so strongly that a formally optimal assignment
    solver would essentially never disagree with this greedy-with-
    exclusion approach for realistic per-page tag/symbol counts (tens, not
    thousands) — the real value of "global" over "independent" here is
    simply the mutual-exclusion guarantee, which this already provides.

    Returns {tag: (cluster_or_None, method, score)} for every tag in
    tag_points; unmatched tags map to (None, 'none', 0.0).
    """
    indexed_points = [(i, pos) for i, (_tag, _prefix, pos) in enumerate(tag_points)]
    pair_scores = symbol_geometry.build_pair_scores(
        indexed_points, clusters, primitives, search_radius=search_radius)

    if known_prefixes:
        for (ti, ci), score in list(pair_scores.items()):
            _tag, prefix, _pos = tag_points[ti]
            if prefix in known_prefixes:
                pair_scores[(ti, ci)] = min(1.0, score + _ASSOC_KNOWN_PREFIX_BONUS)

    ordered = sorted(pair_scores.items(), key=lambda kv: -kv[1])
    used_tags, used_clusters = set(), set()
    assigned = {}
    for (ti, ci), score in ordered:
        if score < min_score or ti in used_tags or ci in used_clusters:
            continue
        cluster = clusters[ci]
        tag_point = tag_points[ti][2]
        method = _dominant_link_method(tag_point, cluster, primitives)
        assigned[ti] = (cluster, method, score)
        used_tags.add(ti)
        used_clusters.add(ci)

    result = {}
    for i, (tag, _prefix, _pos) in enumerate(tag_points):
        result[tag] = assigned.get(i, (None, 'none', 0.0))
    return result


def _valve_rejection_reason(cluster, min_bowtie_score=0.5):
    """Human-readable reason iff `cluster` fails EXACTLY ONE of
    find_valve_shapes' five valve-shape filters — a near-miss worth
    surfacing to the user in the review dialog's "avvisade kandidater"
    section. None if it passes everything (not rejected) or fails more
    than one filter (too far off to be an interesting near-miss)."""
    checks = [
        (cluster.get('bowtie_score', 0.0) < min_bowtie_score,
         f"Ventilformspoäng {cluster.get('bowtie_score', 0.0):.2f} under tröskeln "
         f"{min_bowtie_score:.2f}"),
        (cluster['aspect'] > 3.0, f"För avlångt (proportion {cluster['aspect']:.1f} > 3.0)"),
        (not (1.5 <= cluster['norm_size'] <= 40.0),
         f"Fel storlek relativt sidans text (norm_size {cluster['norm_size']:.1f})"),
        (not cluster['has_diagonal'], "Ingen diagonal linje (kan inte vara en bow-tie-ventil)"),
        (not cluster['has_closed_loop'],
         "Ingen sluten form (kan inte vara en bow-tie-ventil)"),
    ]
    failed = [reason for is_failed, reason in checks if is_failed]
    return failed[0] if len(failed) == 1 else None


# ── Line-number / medium / DN tracing — vector-only, no rendering ─────────

_DN_TEXT_SEARCH_RADIUS = 60.0   # pt around each traced pipe point
_DN_RE = re.compile(r'(?<![A-Z0-9])DN\s?-?(\d{2,4})(?![0-9])', re.IGNORECASE)
_LINE_OBJREF_RE = re.compile(r'(?<![A-Z0-9=])=[A-Z0-9]{1,4}(?:[.\-][A-Z0-9]{1,8}){1,6}(?![A-Z0-9])')
_MEDIUM_CODE_RE = re.compile(r'(?<![A-Za-z0-9])[A-Za-z]{1,4}\d{1,4}(?![A-Za-z0-9])')


def _parse_line_callout(text: str) -> dict:
    """Verbatim extraction only — NEVER interprets medium_code
    semantically. A project-specific medium/class code (e.g. 'KX200' in
    '=E1.M1.WPA041 KX200 DN10') has no fixed meaning across projects; the
    right answer when its meaning isn't independently verified (e.g. via
    an explicit legend) is to say so, not to guess. Returns whichever of
    {'line_number', 'nominal_size', 'medium_code'+'medium_code_verified'}
    is present in `text`; {} if none.
    """
    out = {}
    remaining = text

    m = _LINE_OBJREF_RE.search(text)
    if m:
        out['line_number'] = m.group(0)
        remaining = remaining.replace(m.group(0), ' ')

    m = _DN_RE.search(remaining)
    if m:
        out['nominal_size'] = f"DN{m.group(1)}"
        remaining = remaining.replace(m.group(0), ' ')

    m = _MEDIUM_CODE_RE.search(remaining)
    if m:
        out['medium_code'] = m.group(0).upper()
        out['medium_code_verified'] = False

    return out


def trace_line_info_for_cluster(cluster, primitives, page_words_combined, line_index=None) -> dict:
    """Best-effort line-number/medium/DN association for one valve
    cluster: walks connected pipe primitives outward from its bbox
    (symbol_geometry.trace_pipe_points_from_bbox — vector-only, no
    rendering) and checks native text near each traced point, nearest
    first. First match wins; line_assignment_confidence decays with how
    far along the trace it was found. Returns {} if nothing plausible
    turns up — never raises, never guesses.

    page_words_combined: _spatial_combine(_rotate_words(page.get_text(
    "words"), page)) output for the SAME page as `cluster`, computed ONCE
    per page by the caller and reused across every valve on it (avoids
    re-extracting/re-combining native words per valve).

    line_index: optional symbol_geometry.build_line_index(primitives)
    result for the SAME page, computed ONCE per page by the caller —
    without it, trace_pipe_points_from_bbox rebuilds its own spatial index
    from `primitives` on every call, which is correct but redoes an
    O(lines) build for every single valve on the page.
    """
    try:
        points = symbol_geometry.trace_pipe_points_from_bbox(
            cluster['bbox'], primitives, line_index=line_index)
    except Exception:
        return {}
    if not points:
        return {}

    max_hops = max(1, len(points))
    for hop_idx, pt in enumerate(points):
        best, best_dist = None, _DN_TEXT_SEARCH_RADIUS
        for candidate, x0, y0, x1, y1 in page_words_combined:
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            d = math.hypot(cx - pt[0], cy - pt[1])
            if d <= best_dist:
                parsed = _parse_line_callout(candidate)
                if parsed:
                    best, best_dist = parsed, d
        if best:
            decay = max(0.0, 1.0 - hop_idx / max_hops)
            conf = (1.0 if 'line_number' in best else 0.6) * (0.5 + 0.5 * decay)
            best['line_assignment_confidence'] = round(conf, 2)
            return best
    return {}


# ── Tag-source -> confidence, and flattening scan_pdf_for_equipment's
#    per-prefix result dict into detect_equipment_and_valves' flat input ──

_TAG_READING_CONF_BY_SOURCE = {'native': 1.0, 'ocr': 0.6, 'ocr_fuzzy': 0.4}


def scan_result_to_tag_points(scan_result):
    """Flatten scan_pdf_for_equipment()'s per-prefix dict into
    detect_equipment_and_valves()'s flat input shape:
    [(tag, prefix, page, x, y, tag_reading_confidence), ...].
    Positions come straight from the scan (already precise from pass 2's
    spatial-combine matching), so detect_equipment_and_valves never needs
    to re-look them up for this path."""
    out = []
    real = {k: v for k, v in (scan_result or {}).items() if not k.startswith('_')}
    for prefix, data in real.items():
        for tag in data.get('tags', []):
            page = data['pages'].get(tag, 0)
            x, y = data['positions'].get(tag, (0.0, 0.0))
            source = data.get('tag_source', {}).get(tag, 'native')
            out.append((tag, prefix, page, x, y, _TAG_READING_CONF_BY_SOURCE.get(source, 1.0)))
    return out


def _comp_type_for_prefix(prefix, known_prefixes):
    known = known_prefixes.get(prefix)
    return known[1] if known else ''


def detect_equipment_and_valves(pdf_doc, tag_points, pages=None,
                                min_bowtie_score=0.5, min_confidence=0.3,
                                known_prefixes=None, progress_callback=None,
                                should_cancel=None):
    """Unified per-page pipeline: tag<->symbol association (weighted,
    global — see associate_tags_to_clusters) AND shape-anchored valve
    hunting (the same bow-tie/aspect/size/diagonal filter find_valve_shapes
    uses) against ONE shared per-page cluster extraction, plus line-
    tracing for line number/medium/DN. Replaces the UI's separate calls to
    detect_equipment_symbols()/find_valve_shapes() (both left unchanged
    below, still directly usable/testable on their own).

    tag_points: [(tag, prefix, page_num, x_or_None, y_or_None,
                 tag_reading_confidence), ...] — flat across all pages.
    Position may be None (resolved via find_tag_position_on_page) for
    callers that don't already know it, e.g. tags read from the
    Utrustningsregister, which persists tags but not positions;
    scan_result_to_tag_points() above supplies precise positions already
    computed during scanning, so no re-lookup happens for that path.

    pages: iterable of 0-based page numbers to analyse, or None for every
    page in the document — NOT restricted to pages that happen to have a
    known tag, since an untagged valve can be on any page.

    progress_callback(page_num, total_pages, msg) — same 3-arg contract
    scan_pdf_for_equipment/find_valve_shapes already use.
    should_cancel() is checked once per page (cooperative cancellation).

    Returns (results, rejected):
      results: list[dict] — tag, page, comp_type, x, y, outline,
        link_method, tag_status ('tagged'|'untagged'), temporary_id,
        detection_confidence, tag_reading_confidence,
        tag_assignment_confidence, line_assignment_confidence,
        line_number, medium_code, medium_code_verified, nominal_size.
      rejected: list[dict] — page, x, y, outline, reason. In-memory only —
        never written to the database, purely for the review dialog's
        optional "avvisade kandidater" section.
    """
    if not HAS_PYMUPDF or pdf_doc is None:
        return [], []
    if known_prefixes is None:
        known_prefixes = KNOWN_PREFIXES
    if pages is None:
        pages = range(pdf_doc.page_count)
    pages = list(pages)
    total = pdf_doc.page_count

    tag_points_by_page = {}
    for tag, prefix, page_num, x, y, conf in tag_points:
        tag_points_by_page.setdefault(page_num, []).append((tag, prefix, x, y, conf))

    results, rejected = [], []
    for page_num in pages:
        if should_cancel and should_cancel():
            break
        if progress_callback:
            progress_callback(page_num, total,
                              f"Sida {page_num + 1}/{total} — analyserar ventiler…")
        try:
            page = pdf_doc[page_num]
            primitives = symbol_geometry.extract_primitives(page)
            clusters = symbol_geometry.find_symbol_clusters(page, min_confidence=0.0)
        except Exception:
            continue

        resolved_tags = []   # (tag, prefix, (x,y), tag_reading_confidence)
        for tag, prefix, x, y, conf in tag_points_by_page.get(page_num, []):
            if x is None or y is None:
                pos = find_tag_position_on_page(pdf_doc, page_num, tag)
                if pos is None:
                    # Known to exist but its text can't be located on this
                    # page right now — still a row, just with no geometry.
                    results.append({
                        'tag': tag, 'page': page_num,
                        'comp_type': _comp_type_for_prefix(prefix, known_prefixes),
                        'x': 0.0, 'y': 0.0, 'outline': [], 'link_method': 'not_found',
                        'tag_status': 'tagged', 'temporary_id': '',
                        'detection_confidence': 0.0, 'tag_reading_confidence': conf,
                        'tag_assignment_confidence': 0.0, 'line_assignment_confidence': 0.0,
                        'line_number': '', 'medium_code': '', 'medium_code_verified': False,
                        'nominal_size': '',
                    })
                    continue
                x, y = pos
            resolved_tags.append((tag, prefix, (x, y), conf))

        assoc = (associate_tags_to_clusters(
                    [(t, p, pos) for t, p, pos, _c in resolved_tags],
                    clusters, primitives, known_prefixes=known_prefixes)
                 if resolved_tags else {})
        assigned_cluster_ids = {id(cl) for cl, _m, _s in assoc.values() if cl is not None}

        page_words_combined = _spatial_combine(
            _rotate_words(page.get_text("words"), page), gap_limit=22.0)
        # Built once per page, reused for every valve's line-trace below —
        # without this, trace_pipe_points_from_bbox rebuilds its own
        # spatial index from `primitives` on every single call.
        line_index = symbol_geometry.build_line_index(primitives)

        for tag, prefix, _pos, tag_read_conf in resolved_tags:
            cluster, method, assoc_score = assoc.get(tag, (None, 'none', 0.0))
            comp_type = _comp_type_for_prefix(prefix, known_prefixes)
            if cluster is not None:
                x0, y0, x1, y1 = cluster['bbox']
                x, y = (x0 + x1) / 2, (y0 + y1) / 2
                detection_conf, outline = cluster['confidence'], cluster['outline']
                line_info = trace_line_info_for_cluster(
                    cluster, primitives, page_words_combined, line_index=line_index)
            else:
                x, y = _pos
                detection_conf, outline, line_info = 0.0, [], {}
            results.append({
                'tag': tag, 'page': page_num, 'comp_type': comp_type,
                'x': x, 'y': y, 'outline': outline, 'link_method': method,
                'tag_status': 'tagged', 'temporary_id': '',
                'detection_confidence': detection_conf,
                'tag_reading_confidence': tag_read_conf,
                'tag_assignment_confidence': assoc_score,
                'line_assignment_confidence': line_info.get('line_assignment_confidence', 0.0),
                'line_number': line_info.get('line_number', ''),
                'medium_code': line_info.get('medium_code', ''),
                'medium_code_verified': line_info.get('medium_code_verified', False),
                'nominal_size': line_info.get('nominal_size', ''),
            })

        # Shape-anchored: clusters passing the bow-tie/valve-shape filter
        # that no tag above claimed.
        n_untagged = 0
        for cluster in clusters:
            if id(cluster) in assigned_cluster_ids:
                continue
            passes = (cluster.get('bowtie_score', 0.0) >= min_bowtie_score
                      and cluster['aspect'] <= 3.0
                      and 1.5 <= cluster['norm_size'] <= 40.0
                      and cluster['has_diagonal']
                      and cluster['has_closed_loop'])
            if not passes:
                reason = _valve_rejection_reason(cluster, min_bowtie_score)
                if reason:
                    x0, y0, x1, y1 = cluster['bbox']
                    rejected.append({'page': page_num, 'x': (x0 + x1) / 2, 'y': (y0 + y1) / 2,
                                     'outline': cluster['outline'], 'reason': reason})
                continue

            x0, y0, x1, y1 = cluster['bbox']
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            suggested_tag, _pfx = find_nearby_tag_text(page, (cx, cy))
            line_info = trace_line_info_for_cluster(
                cluster, primitives, page_words_combined, line_index=line_index)
            n_untagged += 1
            results.append({
                'tag': suggested_tag or '', 'page': page_num, 'comp_type': 'Ventil',
                'x': cx, 'y': cy, 'outline': cluster['outline'], 'link_method': 'shape',
                'tag_status': 'untagged',
                'temporary_id': f'UNASSIGNED-VALVE-{page_num}-{n_untagged}',
                'detection_confidence': cluster['confidence'],
                'tag_reading_confidence': 0.0, 'tag_assignment_confidence': 0.0,
                'line_assignment_confidence': line_info.get('line_assignment_confidence', 0.0),
                'line_number': line_info.get('line_number', ''),
                'medium_code': line_info.get('medium_code', ''),
                'medium_code_verified': line_info.get('medium_code_verified', False),
                'nominal_size': line_info.get('nominal_size', ''),
            })

    return results, rejected


def _score_tag_word(raw: str):
    """Return (tag_string, score) for a raw word from the PDF, or (None, 0).
    Higher score = more confident this is an equipment tag.
    """
    text = raw.strip().lstrip('=')
    text = re.sub(r'^\d+["\']+', '', text)
    if not text:
        return None, 0
    # Simple exact match
    if _TAG_RE.match(text):
        return text, 3
    # Compound/area-prefix tag
    m = _EXT_TAG_RE.search(text)
    if m:
        candidate = m.group(1).lstrip('=')
        pfx = _equip_prefix_from_tag(candidate)
        if pfx and len(pfx) >= 2:
            return candidate, 2
    # Any word with a recognisable 2+ letter prefix
    pfx = _equip_prefix_from_tag(text)
    if pfx and len(pfx) >= 2:
        return text, 1
    return None, 0


def find_tag_near_point(pdf_doc, page_num, x_pdf, y_pdf, radius=100):
    """Find the nearest equipment tag in the PDF at the given point.

    Strategy:
    1. Search within `radius` — return immediately on a high-confidence match.
    2. If nothing found, search the full page and return the nearest tag.

    Handles all plant tag conventions:
    - Simple: HV-101, PCV101, XFB_31304, HV0063, 300PU3222
    - Compound dot: =E1.M1.GPA4, M1.HXA1
    - Area-hyphen: 60-RV-009, 2818-LX79, 100-MAS10A, G45-100-EAS10A
    - Pipe-size prefix stripped: 2"LS60.002
    """
    if pdf_doc is None or not HAS_PYMUPDF:
        return ''
    try:
        page = pdf_doc.load_page(page_num)

        def dist(w):
            cx = (w[0] + w[2]) / 2
            cy = (w[1] + w[3]) / 2
            return ((cx - x_pdf) ** 2 + (cy - y_pdf) ** 2) ** 0.5

        # --- Pass 1: restricted radius ---
        clip = fitz.Rect(x_pdf - radius, y_pdf - radius,
                         x_pdf + radius, y_pdf + radius)
        words = page.get_text("words", clip=clip)
        if words:
            for w in sorted(words, key=dist)[:20]:
                tag, score = _score_tag_word(w[4])
                if score >= 2:          # confident match → return immediately
                    return tag
            # Collect score-1 candidates from this radius
            candidates = [(dist(w), t) for w in words
                          for t, s in [_score_tag_word(w[4])] if s >= 1]
            if candidates:
                return min(candidates)[1]

        # --- Pass 2: full page (tag label may be positioned away from symbol) ---
        all_words = page.get_text("words")
        if not all_words:
            return ''
        tag_words = []
        for w in all_words:
            tag, score = _score_tag_word(w[4])
            if score >= 1:
                d = dist(w)
                tag_words.append((d, tag))
        if tag_words:
            return min(tag_words)[1]   # nearest tag anywhere on page

        return ''
    except Exception:
        return ''


def _row_confidence(res: dict) -> float:
    """Single headline confidence for a result row. Rows from the older
    detect_equipment_symbols()/find_valve_shapes() carry one 'confidence'
    scalar directly; rows from the unified detect_equipment_and_valves()
    carry four separate confidences instead — the headline value there is
    the weakest link (min), matching the existing 70%/40% colour
    thresholds without needing a table redesign. See _row_confidence_breakdown
    for the full per-field tooltip."""
    if 'confidence' in res:
        return res['confidence']
    fields = ['detection_confidence', 'tag_reading_confidence',
              'tag_assignment_confidence', 'line_assignment_confidence']
    values = [res.get(f) for f in fields if res.get(f) is not None]
    return min(values) if values else 0.0


def _row_confidence_breakdown(res: dict) -> str:
    """Tooltip text listing all four per-field confidences, when present."""
    labels = [
        ('detection_confidence', 'Symboldetektering'),
        ('tag_reading_confidence', 'Taggläsning'),
        ('tag_assignment_confidence', 'Tagg-koppling'),
        ('line_assignment_confidence', 'Ledningskoppling'),
    ]
    lines = [f"{label}: {int(round(res[key] * 100))}%"
             for key, label in labels if res.get(key) is not None]
    return '\n'.join(lines) if lines else ''
