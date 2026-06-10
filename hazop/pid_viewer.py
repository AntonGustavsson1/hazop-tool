#!/usr/bin/env python3
"""P&ID viewer module for the HAZOP tool."""

import re
import json
import os
import shutil
import tempfile
import datetime
import math
from pathlib import Path

# Suppress Qt SVG parser warnings (font references, path truncations)
# These come from PyMuPDF's SVG output and are harmless display artefacts.
from PyQt6.QtCore import qInstallMessageHandler, QtMsgType

def _qt_msg_handler(mode, context, message):
    if message.startswith('qt.svg:'):
        return
    import sys
    print(message, file=sys.stderr)

qInstallMessageHandler(_qt_msg_handler)

from PyQt6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QListWidget, QListWidgetItem, QAbstractItemView,
    QLineEdit, QLabel, QPushButton, QDialogButtonBox, QRadioButton,
    QGraphicsView, QGraphicsScene, QGraphicsItem,
    QGraphicsPixmapItem, QGraphicsPathItem, QGraphicsEllipseItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem, QGraphicsLineItem, QGraphicsSimpleTextItem, QFrame, QSpinBox, QAbstractSpinBox, QCheckBox, QGroupBox,
    QSlider, QColorDialog, QFileDialog, QMessageBox, QInputDialog,
    QSizePolicy, QMenu, QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressDialog, QApplication, QGridLayout, QTextEdit, QButtonGroup,
    QScrollArea,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRectF, QThread, QPoint
from PyQt6.QtGui import (
    QColor, QPen, QBrush, QPainterPath, QPolygonF, QPixmap, QImage, QFont,
    QPainter, QPicture, QCursor,
)

# Optional OpenGL for GPU-accelerated rendering
try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    HAS_OPENGL = True
except ImportError:
    HAS_OPENGL = False

# Optional SVG vector rendering (preferred — stays sharp at any zoom)
try:
    from PyQt6.QtSvg import QSvgRenderer
    HAS_SVG_RENDERER = True
except ImportError:
    QSvgRenderer = None
    HAS_SVG_RENDERER = False

try:
    import fitz
    HAS_PYMUPDF = True
except Exception:
    fitz = None
    HAS_PYMUPDF = False

# ── Optional OCR engines ──────────────────────────────────────────────────────
try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    pytesseract = None
    HAS_TESSERACT = False

try:
    import easyocr as _easyocr_module
    HAS_EASYOCR = True
except ImportError:
    _easyocr_module = None
    HAS_EASYOCR = False

try:
    from rapidocr_onnxruntime import RapidOCR as _RapidOCR
    HAS_RAPIDOCR = True
except ImportError:
    _RapidOCR = None
    HAS_RAPIDOCR = False

_rapidocr_instance = None   # cached after first use

try:
    from PIL import Image as _PILImage, ImageFilter, ImageEnhance, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

_easyocr_reader_cache = None


def _get_easyocr_reader():
    global _easyocr_reader_cache
    if _easyocr_reader_cache is None and HAS_EASYOCR:
        _easyocr_reader_cache = _easyocr_module.Reader(['en'], gpu=False, verbose=False)
    return _easyocr_reader_cache


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
    global _rapidocr_instance
    if not HAS_RAPIDOCR:
        return []
    try:
        import numpy as np
        if _rapidocr_instance is None:
            _rapidocr_instance = _RapidOCR()
        result, _ = _rapidocr_instance(np.array(pil_image.convert('RGB')))
        if not result:
            return []
        out = []
        for item in result:
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


# ══════════════════════════════════════════════════════════════════════════════
# OCR AUTO-INSTALLER
# ══════════════════════════════════════════════════════════════════════════════

def ensure_ocr_available(parent=None) -> bool:
    """Check if RapidOCR is available. Show hint to run starta_hazop.bat if not."""
    if HAS_TESSERACT or HAS_EASYOCR or HAS_RAPIDOCR:
        return True
    QMessageBox.information(
        parent, "OCR saknas",
        "RapidOCR är inte installerat.\n\n"
        "Kör  starta_hazop.bat  en gång för att installera alla beroenden\n"
        "(inkl. rapidocr_onnxruntime, ~25 MB).\n\n"
        "Textextraktion ur PDF-filer med vektorgrafik fungerar inte utan OCR.")
    return False


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

CONSEQUENCE_TEMPLATES = [
    'Överfyllnad av {}',
    'Övertryck i {}',
    'Undertryck i {}',
    'Utsläpp / läcka från {}',
    'Inget flöde till {}',
    'För högt flöde till {}',
    'Felaktig temperatur i {}',
    'Kontaminering av {}',
    'Brand / explosion vid {}',
    'Driftstopp för {}',
    'Toxisk exponering vid {}',
    'Miljöutsläpp från {}',
]

# ── Consequence chain (mirrors hazop.py — no circular import) ────────────────
_PID_CHAIN_ITEMS = [
    ('loc',           'LOC — Utsläpp / läcka',            'Intermediär händelse'),
    ('fire',          'Brand',                             'Antändning / explosion'),
    ('flash_fire',    'Flash fire',                        None),
    ('explosion',     'Explosion (VCE / BLEVE)',           None),
    ('toxic',         'Toxisk exponering',                 'Toxisk / miljö'),
    ('environmental', 'Miljöutsläpp',                     None),
    ('personnel',     'Personskador',                      'Personell / tillgång'),
    ('fatality',      'Dödsfall',                          None),
    ('equipment',     'Utrustningsskador',                 None),
    ('production',    'Driftstopp',                        None),
]


def _pid_build_chain_text(base: str, chain: dict) -> str:
    parts = [base.strip()] if base.strip() else []
    for key, label, _ in _PID_CHAIN_ITEMS:
        if chain.get(key):
            short = label.split('(')[0].strip().split(' — ')[-1].strip()
            parts.append(short)
    return ' → '.join(parts)


MODE_NAV             = 0
MODE_NODE            = 1
MODE_CAUSE           = 2
MODE_CONSEQUENCE     = 3
MODE_SAFEGUARD       = 4
MODE_PLACE_EXISTING  = 5   # place a pre-existing item (no new item created)
MODE_CAUSE_TEMPLATE  = 6   # place cause from template list for a specific deviation
MODE_MARKUP_POLYGON  = 7   # draw closed polygon markup on a node
MODE_MARKUP_POLYLINE = 8   # draw open polyline markup on a node
MODE_MARKUP_TEXT     = 9   # click to place a text label markup
MODE_MARKUP_COMMENT  = 10  # click to place a comment box markup
MODE_MARKUP_SELECT   = 11  # click existing markup items to select/edit
MODE_SMART_POLYLINE  = 12  # click start+end, algorithm traces pipe path
MODE_RED_MARKUP_SYMBOL = 13  # click to place a red markup P&ID symbol
MODE_BOARD_LAYOUT    = 14  # drag pages to reposition on study board
MODE_ADD_SHEET_LINK  = 15  # click target page to create a manual inter-sheet link

# ── Off-page connector analysis ───────────────────────────────────────────────
_RE_TO_FROM = re.compile(
    r'\b(TO|FROM|CONT\'?D\.?(?:\s+ON)?|TILL|FR[ÅA]N|FRAN)\s+'
    r'([A-Z0-9\+][A-Z0-9\-/\.\+]{1,30})', re.IGNORECASE)
_RE_LINE_ID = re.compile(
    r'\b(\d{1,4}["\']\-[A-Z]{1,5}\-\d{3,6}[A-Z0-9\-]*|\d{1,4}\-[A-Z]{1,5}\-\d{3,6}[A-Z0-9\-]*)\b')
# Universal sheet-number regex — covers all known customer formats
_RE_SHEET_NUM = re.compile(
    r'\b('
    r'S\d{6,8}'                                 # LKAB:    S0000155
    r'|[A-Z]{2,6}_\d{4,8}'                      # ITS:     XFB_11338
    r'|[A-Z]{2,4}_[A-Z]{2,4}_\d{3,6}'          # Gryaab:  AD_PFS_0003
    r'|[A-Z]{2,4}[-_][A-Z]{2,4}[-_]\d{3,6}'   # Gryaab:  AD-PFS-0003
    r'|[A-Z]{1,5}\-\d{2,6}[A-Z]?\d?'           # classic: P-101
    r'|\d{3}-\d{4}-\d{3}(?:-[A-Z]{1,4})?'      # Hybrit:  242-0000-001, 253-0000-002-PS
    r'|[A-Z]\d{1,2}-\d{3}-\d{3,4}'             # Smurfit: R1-077-012
    r'|\+\d{2,4}[A-Z]\d{3}'                    # Loket:   +100D001
    r'|\d{4,6}\-\d{2,4}'                        # old:     1234-01
    r')\b', re.IGNORECASE)
# LKAB-specific: =M1.GPA3   S0000155
_RE_RDS_SHEET = re.compile(
    r'=([A-Z][A-Z0-9./\-]{1,25})\s+S(\d{6,8})', re.IGNORECASE)
# ITS: XFB_40208/001.2F  — extract the drawing code as the sheet reference
_RE_ITS_CONN = re.compile(
    r'\b([A-Z]{2,6}_\d{4,8})/\d{3,6}\.\w{1,4}\b', re.IGNORECASE)
# Gryaab: AD_DP76, AD_PFS_0003, AD_RR0032-350
_RE_GRYAAB_CONN = re.compile(
    r'\b([A-Z]{2,4}[_-][A-Z]{1,4}\d{2,5}(?:[_-]\d+)?)\b', re.IGNORECASE)

# ── Dialect definitions ────────────────────────────────────────────────────────
# Each dialect describes how to recognise sheet numbers and cross-references for
# a particular P&ID style.  ConnectorAnalyzer auto-detects the best-fitting
# dialect from the first few pages before parsing begins.
_DIALECTS = {
    'lkab': {
        'name':         'LKAB (RDS + S-nummer)',
        'score_re':     re.compile(r'\bS\d{6,8}\b|=[A-Z][A-Z0-9./\-]{1,10}\s+S\d{6,8}', re.I),
        'sheet_num_re': re.compile(r'\bS(\d{6,8})\b', re.I),
        'title_area':   (0.45, 0.86, 1.0, 1.0),
    },
    'its': {
        'name':         'ITS (XFB_NNNNN/NNN.NN)',
        'score_re':     re.compile(r'[A-Z]{2,6}_\d{4,8}/\d{3,6}\.\w', re.I),
        'sheet_num_re': re.compile(r'\b([A-Z]{2,6}_\d{4,8})\b', re.I),
        'title_area':   (0.5, 0.7, 1.0, 1.0),
    },
    'gryaab': {
        'name':         'Gryaab (AD_COMPONENT)',
        'score_re':     re.compile(r'\b[A-Z]{2,4}[_-][A-Z]{1,4}\d{2,5}\b', re.I),
        'sheet_num_re': re.compile(r'\b([A-Z]{2,4}[-_][A-Z]{2,4}[-_]\d{3,6})\b', re.I),
        'title_area':   (0.5, 0.7, 1.0, 1.0),
    },
    'hybrit': {
        'name':         'Hybrit (NNN-NNNN-NNN)',
        'score_re':     re.compile(r'\b\d{3}-\d{4}-\d{3}|\b(TILL|FR[ÅA]N)\b', re.I),
        'sheet_num_re': re.compile(r'\b(\d{3}-\d{4}-\d{3})(?:-[A-Z]{1,4})?\b'),
        'title_area':   (0.0, 0.85, 1.0, 1.0),
    },
    'classic': {
        'name':         'Classic (TO/FROM/TILL/FRÅN DWG)',
        'score_re':     re.compile(r'\b(TO|FROM|CONT\'?D|TILL|FR[ÅA]N)\b', re.I),
        'sheet_num_re': _RE_SHEET_NUM,
        'title_area':   (0.45, 0.75, 1.0, 1.0),
    },
}

def _detect_dialect(sample_texts):
    """Score each dialect against sample text from the first few pages."""
    scores = {d: 0 for d in _DIALECTS}
    for text in sample_texts:
        for dname, dconf in _DIALECTS.items():
            scores[dname] += len(dconf['score_re'].findall(text))
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else 'classic'

_MEDIA_PATTERNS = [
    # Slurry / pulp
    ('slurry',        re.compile(
        r'\b(SLURRY|PULP|MUD|SLUDGE|UNDERFLOW|THICKENER'
        r'|SLAM|MASSA|LERA)\b', re.I)),
    # Filtrate / leach liquor
    ('filtrate',      re.compile(
        r'\b(FILTRAT|FILTRATE|LEACH\s*FILTRAT|LAKFILTRAT|LAKVÄTSKA)\b', re.I)),
    # Chemicals
    ('chemical',      re.compile(
        r'\b(HCL|HCl|H[Cc][Ll]|CAUSTIC|ACID|MEG|NAOH|METHANOL|INHIBITOR'
        r'|GLYCOL|KCL|KCl|FLOCCULANT|AMMONIAK|AMMONIA|KALK|LIME'
        r'|KEMIKALIE|KEMIKALIER|SVAVELS?YRA|SALTSYRA)\b', re.I)),
    # Leach gas
    ('leach_gas',     re.compile(r'\b(LEACH\s*GAS|LEACHING\s*GAS|LAKGAS)\b', re.I)),
    # Process fluid (general)
    ('process',       re.compile(
        r'\b(FEED|PRODUCT|CRUDE|HC|PROCESS|RAW|APATITE|RESIDUE'
        r'|PANNVATTEN|KONDENSAT|CONDENSATE)\b', re.I)),
    # Flue gas / combustion gas / pure gases
    ('gas',           re.compile(
        r'\b(GAS|VAPOR|VAPOUR|VG|FLUE|RÖKGAS|RÖKGASER'
        r'|FÖRBRÄNNINGSGAS|AVGASER|KVÄVGAS|NITROGEN|VÄTGAS|HYDROGEN|H2'
        r'|SYRGAS|OXYGEN|O2|NATURGAS|BIOGAS|SYNGAS)\b', re.I)),
    # Liquid (general)
    ('liquid',        re.compile(r'\b(LIQ|LIQUID|VÄTSKA)\b', re.I)),
    # Steam
    ('utility_steam', re.compile(
        r'\b(STEAM|HP[\s\-]?STEAM|MP[\s\-]?STEAM|LP[\s\-]?STEAM'
        r'|ÅNGA|HÖGTRYCKSÅNGA|LÅGTRYCKSÅNGA|MELLANTRYCKSÅNGA'
        r'|SOTBLÅSNINGSÅNGA|ÖVERHETTAD)\b', re.I)),
    # Water (utility)
    ('utility_water', re.compile(
        r'\b(C\.?W\.?|F\.?W\.?|P\.?W\.?|BFW|COOLING\s*WATER|FIRE\s*WATER'
        r'|HEATING\s*WATER|PROCESS\s*WATER|MATARVATTEN|KONDENSATTANK'
        r'|KYLVATTEN|DRICKSVATTEN|RÅVATTEN|PROCESSVATTEN|FJÄRRVÄRME'
        r'|SPÄDVATTEN|KYLARVATTEN|SPOLVATTEN|AVJONISERAT)\b', re.I)),
    # Combustion / instrument air
    ('utility_air',   re.compile(
        r'\b(I\.?A\.?|P\.?A\.?|C\.?A\.?|INSTRUMENT\s*AIR|PLANT\s*AIR'
        r'|FÖRBRÄNNINGSLUFT|INSTRUMENTLUFT|TRYCKLUFT|VENTILATIONSLUFT)\b', re.I)),
    # Instrument / signal
    ('instrument',    re.compile(
        r'\b(SIG|SIGNAL|4[\.\-]20|ESD|SIS|INTERLOCK|STYRSIGNAL)\b', re.I)),
    # Biofuel / solid fuel
    ('process',       re.compile(
        r'\b(BIOBRÄNSLE|BRÄNSLE|BOTTENASKA|SANDSILO|SLAMINMATNING'
        r'|REJEKTSILO|SANDÅTERVINNING)\b', re.I)),
    # Drain / vent / flare
    ('drain_vent',    re.compile(
        r'\b(DRAIN|VENT|ATM|FLARE|BLOWDOWN|AVLOPP|AVLUFTNING|FACKEL)\b', re.I)),
]
_MEDIA_WEIGHTS = {
    'process': 1.0, 'gas': 0.9, 'chemical': 0.8, 'liquid': 0.7,
    'slurry': 0.6, 'filtrate': 0.6, 'leach_gas': 0.55,
    'utility_steam': 0.4, 'utility_water': 0.3,
    'utility_air': 0.2, 'instrument': 0.15, 'drain_vent': 0.1, 'unknown': 0.05,
}
# Display colors per media type (used for sheet-connection arcs on the board)
_MEDIA_COLORS = {
    'process':       '#2855d4',   # blue
    'gas':           '#9b59b6',   # purple
    'chemical':      '#e74c3c',   # red
    'filtrate':      '#e67e22',   # orange
    'leach_gas':     '#8e44ad',   # dark purple
    'liquid':        '#2980b9',   # steel blue
    'slurry':        '#a0522d',   # brown
    'utility_steam': '#c0392b',   # dark red
    'utility_water': '#17a589',   # teal
    'utility_air':   '#7f8c8d',   # grey
    'instrument':    '#f39c12',   # yellow-orange
    'drain_vent':    '#95a5a6',   # silver
    'unknown':       '#5d6d7e',   # slate
}

_SG_TYPES    = ['BPCS', 'SIS', 'Mekanisk', 'Administrativ', 'Övrigt']
_RRF_VALUES  = [1, 10, 100, 1000, 10000]
_RRF_LABELS  = ['1 – Ingen', '10 – RRF10', '100 – RRF100', '1000 – RRF1000', '10000 – RRF10000']

# ── Red Markup P&ID Symbols ───────────────────────────────────────────────────
_RED_MARKUP_SYMBOLS = {
    "Ventiler": [
        ("gate_valve",     "Spjällventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><polygon points="2,11 24,24 2,37"/><polygon points="46,11 24,24 46,37"/></g></svg>'),
        ("gate_valve_nc",  "Spjällventil NC",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5"><line x1="2" y1="24" x2="46" y2="24" fill="none"/><polygon points="2,11 24,24 2,37" fill="red"/><polygon points="46,11 24,24 46,37" fill="red"/></g></svg>'),
        ("butterfly_valve","Fjärilsventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><line x1="24" y1="7" x2="24" y2="41"/><line x1="9" y1="9" x2="39" y2="39"/><line x1="39" y1="9" x2="9" y2="39"/></g></svg>'),
        ("check_valve",    "Backventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><polygon points="10,11 32,24 10,37"/><line x1="32" y1="11" x2="32" y2="37"/></g></svg>'),
        ("globe_valve",    "Globventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><polygon points="2,11 24,24 2,37"/><polygon points="46,11 24,24 46,37"/><circle cx="24" cy="24" r="6"/></g></svg>'),
        ("ball_valve",     "Kulventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5"><line x1="2" y1="24" x2="46" y2="24" fill="none"/><polygon points="2,11 24,24 2,37" fill="red"/><polygon points="46,11 24,24 46,37" fill="red"/></g></svg>'),
        ("safety_valve",   "Säkerhetsventil (PSV)",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5"><line x1="24" y1="2" x2="24" y2="16" fill="none"/><polygon points="8,16 40,16 24,40" fill="red"/><line x1="24" y1="40" x2="24" y2="46" fill="none"/><line x1="14" y1="12" x2="34" y2="12" fill="none"/><line x1="14" y1="8" x2="34" y2="8" fill="none"/></g></svg>'),
        ("control_valve",  "Reglerventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5"><line x1="2" y1="30" x2="46" y2="30" fill="none"/><polygon points="2,18 24,30 2,42" fill="red"/><polygon points="46,18 24,30 46,42" fill="red"/><line x1="24" y1="30" x2="24" y2="18" fill="none"/><polygon points="16,4 24,18 32,4" fill="none" stroke="red"/></g></svg>'),
        ("hand_valve",     "Handventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="28" x2="46" y2="28"/><polygon points="2,16 24,28 2,40"/><polygon points="46,16 24,28 46,40"/><line x1="24" y1="28" x2="24" y2="15"/><line x1="16" y1="11" x2="32" y2="11"/><path d="M16,11 Q24,6 32,11"/></g></svg>'),
        ("motor_valve",    "Motorventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5"><line x1="2" y1="30" x2="46" y2="30" fill="none"/><polygon points="2,18 24,30 2,42" fill="red"/><polygon points="46,18 24,30 46,42" fill="red"/><line x1="24" y1="30" x2="24" y2="19" fill="none"/><rect x="17" y="7" width="14" height="12" fill="none" stroke="red"/></g></svg>'),
        ("three_way_valve","Trevägsventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><line x1="24" y1="24" x2="24" y2="46"/><polygon points="2,14 20,24 2,34"/><polygon points="46,14 28,24 46,34"/><polygon points="14,46 24,28 34,46"/></g></svg>'),
        ("angle_valve",    "Vinkelventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="24" y2="24"/><line x1="24" y1="24" x2="24" y2="46"/><polygon points="2,13 24,24 2,35"/><polygon points="13,46 24,24 35,46"/></g></svg>'),
        ("needle_valve",   "Nålventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><polygon points="24,10 44,17 44,31 24,38 4,31 4,17"/></g></svg>'),
    ],
    "Kärl": [
        ("horiz_vessel",   "Horisontell behållare",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><rect x="4" y="14" width="40" height="20" rx="10"/></g></svg>'),
        ("vert_vessel",    "Vertikal behållare",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><rect x="14" y="4" width="20" height="40" rx="10"/></g></svg>'),
        ("column",         "Kolonn",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><rect x="13" y="2" width="22" height="44" rx="3"/><line x1="13" y1="14" x2="35" y2="22"/><line x1="13" y1="22" x2="35" y2="14"/><line x1="13" y1="28" x2="35" y2="36"/><line x1="13" y1="36" x2="35" y2="28"/></g></svg>'),
        ("hopper",         "Tratt/Binge",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="5" y1="5" x2="43" y2="5"/><line x1="5" y1="5" x2="19" y2="35"/><line x1="43" y1="5" x2="29" y2="35"/><line x1="19" y1="35" x2="29" y2="35"/><line x1="24" y1="35" x2="24" y2="46"/></g></svg>'),
        ("separator",      "Separator",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><ellipse cx="24" cy="16" rx="18" ry="13"/><line x1="6" y1="16" x2="6" y2="30"/><line x1="42" y1="16" x2="42" y2="30"/><line x1="6" y1="30" x2="24" y2="44"/><line x1="42" y1="30" x2="24" y2="44"/></g></svg>'),
    ],
    "Utrustning": [
        ("pump",           "Pump (centrifugal)",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><circle cx="24" cy="24" r="18"/><polygon points="12,16 38,24 12,32"/></g></svg>'),
        ("heat_exchanger", "Värmeväxlare",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><rect x="3" y="12" width="42" height="24" rx="2"/><line x1="3" y1="21" x2="45" y2="21"/><line x1="3" y1="27" x2="45" y2="27"/><line x1="3" y1="21" x2="3" y2="15"/><line x1="45" y1="21" x2="45" y2="33"/><line x1="3" y1="27" x2="3" y2="33"/><line x1="45" y1="27" x2="45" y2="15"/></g></svg>'),
        ("instrument",     "Instrument (ISA)",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><circle cx="24" cy="24" r="18"/><line x1="8" y1="27" x2="40" y2="27"/><text x="24" y="22" text-anchor="middle" font-size="8" stroke="none" fill="red" font-family="sans-serif">XX</text><text x="24" y="38" text-anchor="middle" font-size="7" stroke="none" fill="red" font-family="sans-serif">XXXX</text></g></svg>'),
        ("mixer",          "Reaktor/Omrörare",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><rect x="12" y="4" width="24" height="36" rx="4"/><line x1="24" y1="4" x2="24" y2="40"/><line x1="14" y1="18" x2="34" y2="18"/><line x1="15" y1="27" x2="33" y2="27"/><line x1="24" y1="40" x2="24" y2="46"/><line x1="20" y1="40" x2="28" y2="40"/></g></svg>'),
        ("filter",         "Filter/Sil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><rect x="11" y="13" width="26" height="22" rx="2"/><line x1="17" y1="13" x2="17" y2="35"/><line x1="23" y1="13" x2="23" y2="35"/><line x1="29" y1="13" x2="29" y2="35"/><line x1="35" y1="13" x2="35" y2="35"/></g></svg>'),
        ("compressor",     "Kompressor",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><polygon points="2,40 24,8 46,40"/><circle cx="24" cy="30" r="8"/></g></svg>'),
        ("expansion_joint","Expansionskoppling",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="11" y2="24"/><line x1="37" y1="24" x2="46" y2="24"/><line x1="11" y1="13" x2="11" y2="35"/><line x1="37" y1="13" x2="37" y2="35"/><path d="M11,13 Q24,8 37,13"/><path d="M11,35 Q24,40 37,35"/></g></svg>'),
    ],
}


def _get_red_symbol_svg(symbol_id: str) -> str | None:
    """Return the SVG string for the given red markup symbol ID, or None."""
    for syms in _RED_MARKUP_SYMBOLS.values():
        for sid, _sname, svg in syms:
            if sid == symbol_id:
                return svg
    return None

Z_PAGE       = 0
Z_HIGHLIGHT  = 1   # tag highlights (cleared separately by clear_highlights)
Z_SHEET_CONN = 2   # inter-sheet connection arcs on the study board
Z_CONNECT    = 3   # HAZOP cause/consequence/safeguard lines
Z_OVERLAY    = 5
Z_TEMP       = 10

# Simple tag: 1-6 letters + separator + 1-5 digits + 0-3 suffix
# Examples: PCV-101, FT201A, V-1, ESDV-1001AB
_TAG_RE = re.compile(r'^[A-Z]{1,6}[-./]?\d{1,5}[A-Z]{0,3}$')

# Tag within continuous text
_FULL_TAG_RE = re.compile(r'(?<![A-Z0-9])([A-Z]{1,6})[-./]?(\d{1,5}[A-Z]{0,3})(?![A-Z0-9])')

# Extended tag including area/facility/unit prefix:
#   20-PCV-101,  K2.FT.201A,  A-20-HV-301,  HAV.PSV.101,  10.20.FT-201
# Pattern: 1-3 prefix sections (digits or 1-4 letters) + equipment code + number
_EXT_TAG_RE = re.compile(
    r'(?<![A-Z0-9])'
    # 1-3 area prefix sections: pure digits OR letter-led alphanumeric (K2, A1, HAV)
    r'((?:(?:\d{1,4}|[A-Z][A-Z0-9]{0,3})[-./]){1,3}'
    r'[A-Z]{1,6}[-./]?\d{1,5}[A-Z]{0,3})'       # equipment code + number
    r'(?![A-Z0-9])',
    re.IGNORECASE)

# Simple area prefix stripping: 20-PCV-101 → PCV + 101
_AREA_TAG_RE = re.compile(r'^\d{1,4}[-/]([A-Z]{1,6})[-./]?(\d{1,5}[A-Z]{0,3})$')


def _equip_prefix_from_tag(tag: str) -> str:
    """Extract the equipment letter code from a full/extended tag.

    '20-PCV-101'  → 'PCV'
    'K2.FT.201A'  → 'FT'
    'A-20-HV-301' → 'HV'
    'PSV-101'     → 'PSV'
    """
    parts = re.split(r'[-./]', tag.upper())
    # Prefer known KNOWN_PREFIXES keys (longest match first)
    for part in parts:
        if re.match(r'^[A-Z]{2,6}$', part) and part in KNOWN_PREFIXES:
            return part
    # Fall back: first all-letter part of 2+ chars
    for part in parts:
        if re.match(r'^[A-Z]{2,6}$', part):
            return part
    return parts[0] if parts else tag

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
    'DP':   ('Doseringspump',                    'Pump'),
    'CP':   ('Centrifugalpump',                  'Pump'),
    'VP':   ('Vakuumpump',                       'Pump'),
    'SP':   ('Skruvpump',                        'Pump'),
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
    # Instrument – Temperatur
    'TI':   ('Temperaturrgivare (lokal)',        'Instrument / Sensor'),
    'TE':   ('Temperaturelement',                'Instrument / Sensor'),
    'TT':   ('Temperaturtransmitter',            'Instrument / Sensor'),
    'TIT':  ('Temperaturtransm. + indik.',       'Instrument / Sensor'),
    'TIC':  ('Temperaturreglering',              'Instrument / Sensor'),
    'TSH':  ('Högt temperaturlarm',              'Instrument / Sensor'),
    'TSL':  ('Lågt temperaturlarm',              'Instrument / Sensor'),
    # Instrument – Analys
    'AI':   ('Analysinstrument',                 'Instrument / Sensor'),
    'AT':   ('Analystransmitter',                'Instrument / Sensor'),
    'AIC':  ('Analysreglering',                  'Instrument / Sensor'),
    'ASH':  ('Högt analysalarm',                 'Instrument / Sensor'),
    'ASL':  ('Lågt analysalarm',                 'Instrument / Sensor'),
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
    # Övrigt
    'M':    ('Motor / Drivverk',                 'Övrigt'),
    'AG':   ('Omrörare / Agitator',             'Övrigt'),
    'MX':   ('Blandare',                         'Övrigt'),
    'G':    ('Generator',                        'Övrigt'),
    'TR':   ('Transformator',                    'Övrigt'),
    'BRN':  ('Brännare',                         'Övrigt'),
    'IG':   ('Tändare',                          'Övrigt'),
}

def _spatial_combine(words: list, gap_limit: float = 18.0) -> list:
    """Combine spatially adjacent word-tokens into candidate tag strings.

    Words that lie on the same baseline and are separated by less than
    `gap_limit` PDF units (or are single-char separators like '-' or '.')
    are joined without space.  Yields combined strings for tag parsing.

    words: list of (x0, y0, x1, y1, text) tuples from page.get_text("words")
    """
    if not words:
        return []

    # Sort in reading order: row (rounded), then x
    sw = sorted(words, key=lambda w: (round((w[1] + w[3]) / 2 / 8) * 8, w[0]))

    results = []
    i = 0
    while i < len(sw):
        x0, y0, x1, y1, text = sw[i][:5]
        group = [text]
        grp_x1 = x1
        y_mid = (y0 + y1) / 2

        j = i + 1
        while j < len(sw):
            nx0, ny0, nx1, ny1, ntext = sw[j][:5]
            ny_mid = (ny0 + ny1) / 2

            # Must be on same line (within ~5 PDF units vertically)
            if abs(y_mid - ny_mid) > 5:
                break

            gap = nx0 - grp_x1
            is_sep = ntext.strip() in ('-', '.', '/', '_')

            # Combine if gap is small OR token is a separator char
            if gap <= gap_limit or is_sep:
                group.append(ntext)
                grp_x1 = nx1
                j += 1
            else:
                break

        combined = ''.join(group)
        if combined and combined not in results:
            results.append(combined)
        # Also yield the first token alone (in case only part is a tag)
        if text not in results:
            results.append(text)
        i = j if j > i + 1 else i + 1

    return results


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
        if matches:
            return f"{matches[0][0]}-{matches[0][1]}"
        tag, _ = _parse_tag(candidate)
        if tag:
            return tag
    return ''


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


def _words_from_native(fitz_page):
    """Extract (text, cx, cy) from a PDF page using PyMuPDF word list."""
    words = fitz_page.get_text("words")
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
    """Normalise an equipment tag string.

    Handles:
      - PCV-101, FT-201A, V-1, ESDV-1001AB   (with dash)
      - PCV101, FT201A, V1                     (no dash)
      - 20-PCV-101, 10/FT201                   (area prefix — stripped)
      - PCV.101                                 (dot separator)

    Returns (normalised_tag, prefix) or (None, None).
    """
    text = text.strip().upper()
    if not text:
        return None, None

    # Strip area prefix like "20-" or "10/"
    am = _AREA_TAG_RE.match(text)
    if am:
        text = f"{am.group(1)}-{am.group(2)}"

    # Already well-formed
    if _TAG_RE.match(text):
        # Normalise separator to dash
        norm = re.sub(r'[./]', '-', text)
        # Remove doubled dashes
        norm = re.sub(r'-+', '-', norm)
        # Ensure dash between letters and digits
        m2 = re.match(r'^([A-Z]{1,6})-(\d{1,5}[A-Z]{0,3})$', norm)
        if not m2:
            m2 = re.match(r'^([A-Z]{1,6})(\d{1,5}[A-Z]{0,3})$', norm)
            if m2:
                norm = f"{m2.group(1)}-{m2.group(2)}"
        return norm, _extract_prefix(norm)

    # No-dash: PCV101, FT201A
    m = re.match(r'^([A-Z]{1,6})(\d{1,5}[A-Z]{0,3})$', text)
    if m:
        tag = f"{m.group(1)}-{m.group(2)}"
        return tag, m.group(1)

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

    def _add(tag, prefix, page_num, cx, cy, from_ocr=False):
        if prefix not in result:
            result[prefix] = {'tags': [], 'pages': {}, 'positions': {}, 'ocr_pages': set()}
        if tag not in result[prefix]['tags']:
            result[prefix]['tags'].append(tag)
        # First-seen wins for page assignment
        result[prefix]['pages'].setdefault(tag, page_num)
        result[prefix]['positions'].setdefault(tag, (cx, cy))
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

        # ── Pass 2: word-by-word (gives precise x,y positions) ───────────────
        native_words = _words_from_native(page)
        tags_from_native = 0
        for text, cx, cy in native_words:
            tag, prefix = _parse_tag(text)
            if tag and prefix:
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
                corrected = _fix_ocr_common_errors(raw_text)
                for candidate in (corrected, raw_text.upper()):
                    tag, prefix = _parse_tag(candidate)
                    if tag and prefix and tag not in native_tag_set:
                        _add(tag, prefix, page_num, cx, cy, from_ocr=True)
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


class PDFVectorItem(QGraphicsItem):
    """Renders a PDF page as pure vector — crisp at any zoom.

    Performance strategy (layered):
    1. SVG is parsed ONCE into a QPicture at init (record draw commands).
    2. Each paint call just replays the QPicture — far cheaper than re-parsing SVG.
    3. DeviceCoordinateCache: Qt caches the QPicture replay as a screen-res pixmap.
       Pan  → copy cached pixmap (near-zero cost).
       Zoom → replay QPicture at new resolution (much faster than SVG parse).
    """

    def __init__(self, svg_bytes: bytes):
        super().__init__()
        renderer = QSvgRenderer(svg_bytes)
        vb = renderer.viewBoxF()
        self._rect = vb if (vb.isValid() and vb.width() > 0) \
                     else QRectF(0, 0,
                                 renderer.defaultSize().width(),
                                 renderer.defaultSize().height())

        # Pre-record SVG draw commands into QPicture (parse once, replay many)
        self._picture = QPicture()
        p = QPainter(self._picture)
        renderer.render(p, self._rect)
        p.end()
        # Keep renderer only for fallback; picture is what we actually paint
        del renderer

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption)
        # Qt caches QPicture replay at screen resolution:
        # panning reuses cache; zooming replays QPicture (fast)
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)

    def boundingRect(self):
        return self._rect

    def paint(self, painter, option, widget=None):
        if option and option.exposedRect.isValid():
            painter.setClipRect(option.exposedRect)
        painter.drawPicture(QPointF(0, 0), self._picture)

    def page_width(self):
        return self._rect.width()

    def page_height(self):
        return self._rect.height()


def find_tag_near_point(pdf_doc, page_num, x_pdf, y_pdf, radius=50):
    if pdf_doc is None or not HAS_PYMUPDF:
        return ''
    try:
        page = pdf_doc.load_page(page_num)
        rect = fitz.Rect(x_pdf - radius, y_pdf - radius,
                         x_pdf + radius, y_pdf + radius)
        words = page.get_text("words", clip=rect)
        if not words:
            return ''

        def dist(w):
            cx = (w[0] + w[2]) / 2
            cy = (w[1] + w[3]) / 2
            return ((cx - x_pdf) ** 2 + (cy - y_pdf) ** 2) ** 0.5

        words_sorted = sorted(words, key=dist)
        for w in words_sorted[:12]:
            text = w[4].strip()
            if _TAG_RE.match(text):
                return text
        return words_sorted[0][4].strip()
    except Exception:
        return ''


class EquipmentScanDialog(QDialog):
    """Two-tab dialog: grouped prefix view + individual editable tag table."""

    # Column indices for the tag table
    _C_CHK  = 0  # checkbox
    _C_TAG  = 1  # original tag (read-only)
    _C_EDIT = 2  # corrected tag (editable)
    _C_PFX  = 3  # derived prefix (read-only, updates on edit)
    _C_PAGE = 4  # page number
    _C_OCR  = 5  # OCR flag
    _C_TYPE = 6  # equipment type combo
    _C_DESC = 7  # free description

    _TYPE_ITEMS = [''] + sorted(COMPONENT_TYPES.keys()) + ['Rörledning', 'Övrigt / Okänd']

    def __init__(self, scan_result: dict, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Utrustningsskanning — P&ID")
        self.setMinimumSize(1000, 640)
        self.resize(1120, 720)

        self._meta       = scan_result.pop('_meta', {})
        self.scan_result = scan_result   # {prefix: {tags, pages, positions, ocr_pages}}

        outer = QVBoxLayout(self)

        # ── Header ────────────────────────────────────────────────────────────
        n_tags   = sum(len(v['tags']) for v in scan_result.values())
        n_groups = len(scan_result)
        ocr_eng  = self._meta.get('ocr_engine', '')
        ocr_used = self._meta.get('ocr_used', False)
        ocr_info = f"&nbsp;|&nbsp; 🔬 OCR: <b>{ocr_eng}</b>" if ocr_used else ""
        hdr = QLabel(
            f"Hittade <b>{n_tags} taggar</b> i <b>{n_groups} prefix-grupper</b>.{ocr_info}")
        hdr.setTextFormat(Qt.TextFormat.RichText)
        hdr.setStyleSheet("padding:5px; background:#e8f4fd; border:1px solid #bee3f8; "
                          "border-radius:4px;")
        outer.addWidget(hdr)

        # ── OCR status bar ────────────────────────────────────────────────────
        st = ocr_status()
        ocr_bar = QHBoxLayout()
        for label, avail, active in [
            ("pytesseract", st['tesseract'], ocr_eng == 'tesseract'),
            ("easyocr",     st['easyocr'],   ocr_eng == 'easyocr'),
        ]:
            icon   = '✅' if avail else '❌'
            suffix = ' (aktiv)' if avail and active else ''
            lbl    = QLabel(f"{icon} {label}{suffix}")
            lbl.setStyleSheet(f"color:{'#1a7a40' if avail else '#aaa'}; font-size:11px;")
            ocr_bar.addWidget(lbl)
        if not st['tesseract'] and not st['easyocr']:
            warn = QLabel("⚠️  Ingen OCR —  pip install pytesseract  eller  pip install easyocr")
            warn.setStyleSheet("color:#c0392b; font-size:11px;")
            ocr_bar.addWidget(warn)
        ocr_bar.addStretch()
        outer.addLayout(ocr_bar)

        # ── Tabs ──────────────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        outer.addWidget(self._tabs)

        self._tabs.addTab(self._build_prefix_tab(),  "📂  Prefix-grupper")
        self._tabs.addTab(self._build_tag_tab(),     "🏷️  Alla taggar (redigera)")

        # ── Bottom buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        save_btn   = QPushButton("💾 Spara typval")
        save_btn.setToolTip("Sparar prefix→typ-mappningar till DB")
        save_btn.clicked.connect(self._save_types)

        create_btn = QPushButton("🏭 Skapa noder (valda taggar)")
        create_btn.setToolTip("Skapar HAZOP-noder för ikryssade taggar i fliken 'Alla taggar'")
        create_btn.clicked.connect(self._create_nodes_from_tag_table)

        close_btn  = QPushButton("Stäng")
        close_btn.clicked.connect(self.accept)

        btn_row.addWidget(save_btn)
        btn_row.addWidget(create_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

        self._populate_prefix_table()
        self._populate_tag_table()

    # ── Tab builders ──────────────────────────────────────────────────────────

    def _build_prefix_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)

        frow = QHBoxLayout()
        frow.addWidget(QLabel("Filtrera:"))
        self._pfx_filter = QLineEdit()
        self._pfx_filter.setPlaceholderText("Sök prefix eller namn…")
        self._pfx_filter.textChanged.connect(self._apply_prefix_filter)
        frow.addWidget(self._pfx_filter)
        frow.addStretch()
        self._hide_unknown_btn = QPushButton("Dölj okända")
        self._hide_unknown_btn.setCheckable(True)
        self._hide_unknown_btn.toggled.connect(self._apply_prefix_filter)
        frow.addWidget(self._hide_unknown_btn)
        layout.addLayout(frow)

        self._pfx_table = QTableWidget(0, 5)
        self._pfx_table.setHorizontalHeaderLabels(
            ['Prefix', 'Antal', 'Exempeltaggar', 'Föreslagen/sparad typ', 'Bekräftad typ'])
        h = self._pfx_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self._pfx_table.setColumnWidth(0, 75)
        self._pfx_table.setColumnWidth(1, 50)
        self._pfx_table.setColumnWidth(3, 210)
        self._pfx_table.setColumnWidth(4, 200)
        self._pfx_table.verticalHeader().setVisible(False)
        self._pfx_table.setAlternatingRowColors(True)
        self._pfx_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._pfx_table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:4px;}")
        self._pfx_table.currentCellChanged.connect(self._pfx_show_tags)
        layout.addWidget(self._pfx_table)

        layout.addWidget(QLabel("Taggar i markerad grupp:"))
        self._pfx_tag_list = QListWidget()
        self._pfx_tag_list.setMaximumHeight(80)
        layout.addWidget(self._pfx_tag_list)
        return w

    def _build_tag_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)

        note = QLabel(
            "Redigera taggar direkt (t.ex. rätta OCR-fel). "
            "Kryssa i de taggar du vill inkludera när du skapar noder.")
        note.setStyleSheet("color:#555; font-size:11px; padding:2px;")
        layout.addWidget(note)

        # Filter + select-all row
        frow = QHBoxLayout()
        frow.addWidget(QLabel("Filtrera:"))
        self._tag_filter = QLineEdit()
        self._tag_filter.setPlaceholderText("Sök tagg, typ eller sida…")
        self._tag_filter.textChanged.connect(self._apply_tag_filter)
        frow.addWidget(self._tag_filter)
        frow.addStretch()

        sel_all_btn  = QPushButton("Välj alla")
        desel_btn    = QPushButton("Avmarkera alla")
        ocr_only_btn = QPushButton("Visa OCR-taggar")
        ocr_only_btn.setCheckable(True)
        sel_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        desel_btn.clicked.connect(lambda: self._set_all_checked(False))
        ocr_only_btn.toggled.connect(self._apply_tag_filter)
        self._ocr_only_btn = ocr_only_btn
        for b in [sel_all_btn, desel_btn, ocr_only_btn]:
            frow.addWidget(b)
        layout.addLayout(frow)

        # Tag table
        self._tag_table = QTableWidget(0, 8)
        self._tag_table.setHorizontalHeaderLabels([
            '✓', 'Original tagg', 'Korrigerad tagg', 'Prefix',
            'Sida', 'OCR', 'Utrustningstyp', 'Beskrivning'])
        h = self._tag_table.horizontalHeader()
        h.setSectionResizeMode(self._C_CHK,  QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(self._C_TAG,  QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(self._C_EDIT, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(self._C_PFX,  QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(self._C_PAGE, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(self._C_OCR,  QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(self._C_TYPE, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(self._C_DESC, QHeaderView.ResizeMode.Stretch)
        self._tag_table.setColumnWidth(self._C_CHK,  30)
        self._tag_table.setColumnWidth(self._C_TAG,  100)
        self._tag_table.setColumnWidth(self._C_EDIT, 110)
        self._tag_table.setColumnWidth(self._C_PFX,  60)
        self._tag_table.setColumnWidth(self._C_PAGE, 50)
        self._tag_table.setColumnWidth(self._C_OCR,  40)
        self._tag_table.setColumnWidth(self._C_TYPE, 185)
        self._tag_table.verticalHeader().setVisible(False)
        self._tag_table.setAlternatingRowColors(True)
        self._tag_table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:4px;}")
        # Update prefix when edited tag changes
        self._tag_table.cellChanged.connect(self._on_tag_cell_changed)
        layout.addWidget(self._tag_table)

        # Row count label
        self._tag_count_lbl = QLabel("")
        self._tag_count_lbl.setStyleSheet("color:#555; font-size:11px;")
        layout.addWidget(self._tag_count_lbl)
        return w

    # ── Populate ──────────────────────────────────────────────────────────────

    def _populate_prefix_table(self):
        self._pfx_table.setRowCount(0)
        for prefix in sorted(self.scan_result.keys()):
            data  = self.scan_result[prefix]
            known = KNOWN_PREFIXES.get(prefix)
            saved = (self.db.get_equipment_type(prefix)
                     if hasattr(self.db, 'get_equipment_type') else None)

            r = self._pfx_table.rowCount()
            self._pfx_table.insertRow(r)

            pfx_item = QTableWidgetItem(prefix)
            pfx_item.setData(Qt.ItemDataRole.UserRole, prefix)
            pfx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            pfx_item.setFlags(pfx_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            pfx_item.setBackground(QBrush(QColor('#e8f8e8' if known else '#fff8e8')))
            self._pfx_table.setItem(r, 0, pfx_item)

            cnt = QTableWidgetItem(str(len(data['tags'])))
            cnt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            cnt.setFlags(cnt.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._pfx_table.setItem(r, 1, cnt)

            ex = ', '.join(data['tags'][:6])
            if len(data['tags']) > 6:
                ex += f" … (+{len(data['tags'])-6})"
            self._pfx_table.setItem(r, 2, QTableWidgetItem(ex))

            if saved:
                si = QTableWidgetItem(f"✅ {saved}")
                si.setForeground(QBrush(QColor('#1a7a40')))
            elif known:
                si = QTableWidgetItem(f"💡 {known[0]}")
                si.setForeground(QBrush(QColor('#1F4E79')))
            else:
                si = QTableWidgetItem("— Okänd —")
                si.setForeground(QBrush(QColor('#888')))
            si.setFlags(si.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._pfx_table.setItem(r, 3, si)

            combo = QComboBox()
            for t in self._TYPE_ITEMS:
                combo.addItem(t)
            target = saved or (known[1] if known else '')
            if target:
                idx = combo.findText(target)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            # Propagate combo choice to tag table
            combo.currentTextChanged.connect(
                lambda typ, pfx=prefix: self._propagate_type_to_tags(pfx, typ))
            self._pfx_table.setCellWidget(r, 4, combo)
            self._pfx_table.setRowHeight(r, 28)

        self._apply_prefix_filter()

    def _populate_tag_table(self):
        self._tag_table.blockSignals(True)
        self._tag_table.setRowCount(0)

        # Collect all tags with metadata
        all_tags = []
        for prefix, data in self.scan_result.items():
            known = KNOWN_PREFIXES.get(prefix)
            saved = (self.db.get_equipment_type(prefix)
                     if hasattr(self.db, 'get_equipment_type') else None)
            suggested_type = saved or (known[1] if known else '')
            ocr_pages = data.get('ocr_pages', set())
            for tag in data['tags']:
                page   = data['pages'].get(tag, 0)
                is_ocr = page in ocr_pages
                all_tags.append((tag, prefix, page, is_ocr, suggested_type))

        all_tags.sort(key=lambda x: (x[0]))  # sort by tag name

        for tag, prefix, page, is_ocr, sug_type in all_tags:
            r = self._tag_table.rowCount()
            self._tag_table.insertRow(r)

            # Checkbox
            chk = QTableWidgetItem()
            chk.setCheckState(Qt.CheckState.Checked)
            chk.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            self._tag_table.setItem(r, self._C_CHK, chk)

            # Original tag (read-only reference)
            orig = QTableWidgetItem(tag)
            orig.setFlags(orig.flags() & ~Qt.ItemFlag.ItemIsEditable)
            orig.setForeground(QBrush(QColor('#666')))
            self._tag_table.setItem(r, self._C_TAG, orig)

            # Editable corrected tag
            edit = QTableWidgetItem(tag)
            if is_ocr:
                edit.setBackground(QBrush(QColor('#fff3cd')))
                edit.setToolTip("Identifierad via OCR — kontrollera att taggen är korrekt")
            self._tag_table.setItem(r, self._C_EDIT, edit)

            # Prefix (read-only, derived)
            pfx_item = QTableWidgetItem(prefix)
            pfx_item.setFlags(pfx_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            pfx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._tag_table.setItem(r, self._C_PFX, pfx_item)

            # Page
            pg_item = QTableWidgetItem(str(page + 1))
            pg_item.setFlags(pg_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            pg_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._tag_table.setItem(r, self._C_PAGE, pg_item)

            # OCR indicator
            ocr_item = QTableWidgetItem('🔬' if is_ocr else '')
            ocr_item.setFlags(ocr_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            ocr_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            ocr_item.setToolTip("Hittad via OCR" if is_ocr else "Hittad via PDF-textlager")
            self._tag_table.setItem(r, self._C_OCR, ocr_item)

            # Type combo
            combo = QComboBox()
            for t in self._TYPE_ITEMS:
                combo.addItem(t)
            if sug_type:
                idx = combo.findText(sug_type)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            self._tag_table.setCellWidget(r, self._C_TYPE, combo)

            # Description (editable free text)
            self._tag_table.setItem(r, self._C_DESC, QTableWidgetItem(''))

            self._tag_table.setRowHeight(r, 26)

        self._tag_table.blockSignals(False)
        self._update_tag_count()
        self._apply_tag_filter()

    # ── Signals / updates ─────────────────────────────────────────────────────

    def _on_tag_cell_changed(self, row, col):
        if col != self._C_EDIT:
            return
        new_tag = (self._tag_table.item(row, self._C_EDIT).text().strip().upper()
                   if self._tag_table.item(row, self._C_EDIT) else '')
        # Derive new prefix
        new_prefix = _extract_prefix(new_tag) if new_tag else ''
        pfx_item = self._tag_table.item(row, self._C_PFX)
        if pfx_item:
            self._tag_table.blockSignals(True)
            pfx_item.setText(new_prefix)
            self._tag_table.blockSignals(False)
        # Suggest type from new prefix if combo is empty
        combo = self._tag_table.cellWidget(row, self._C_TYPE)
        if combo and not combo.currentText() and new_prefix:
            known = KNOWN_PREFIXES.get(new_prefix)
            if known:
                idx = combo.findText(known[1])
                if idx >= 0:
                    combo.setCurrentIndex(idx)

    def _propagate_type_to_tags(self, prefix: str, typ: str):
        """When a type is set in the prefix table, update matching rows in tag table."""
        if not typ:
            return
        for r in range(self._tag_table.rowCount()):
            pfx_item = self._tag_table.item(r, self._C_PFX)
            if pfx_item and pfx_item.text() == prefix:
                combo = self._tag_table.cellWidget(r, self._C_TYPE)
                if combo:
                    idx = combo.findText(typ)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)

    # ── Filters ───────────────────────────────────────────────────────────────

    def _apply_prefix_filter(self):
        text     = self._pfx_filter.text().lower()
        hide_unk = self._hide_unknown_btn.isChecked()
        for r in range(self._pfx_table.rowCount()):
            pfx = (self._pfx_table.item(r, 0).text().lower()
                   if self._pfx_table.item(r, 0) else '')
            sug = (self._pfx_table.item(r, 3).text().lower()
                   if self._pfx_table.item(r, 3) else '')
            is_unknown = '—' in (self._pfx_table.item(r, 3).text()
                                 if self._pfx_table.item(r, 3) else '')
            hidden = (text and text not in pfx and text not in sug) or \
                     (hide_unk and is_unknown)
            self._pfx_table.setRowHidden(r, hidden)

    def _apply_tag_filter(self):
        text     = self._tag_filter.text().lower()
        ocr_only = self._ocr_only_btn.isChecked()
        for r in range(self._tag_table.rowCount()):
            tag_txt  = (self._tag_table.item(r, self._C_EDIT).text().lower()
                        if self._tag_table.item(r, self._C_EDIT) else '')
            type_w   = self._tag_table.cellWidget(r, self._C_TYPE)
            type_txt = type_w.currentText().lower() if type_w else ''
            pg_txt   = (self._tag_table.item(r, self._C_PAGE).text()
                        if self._tag_table.item(r, self._C_PAGE) else '')
            ocr_item = self._tag_table.item(r, self._C_OCR)
            is_ocr   = bool(ocr_item and ocr_item.text())
            hidden = (text and text not in tag_txt and text not in type_txt
                      and text not in pg_txt) or (ocr_only and not is_ocr)
            self._tag_table.setRowHidden(r, hidden)
        self._update_tag_count()

    def _update_tag_count(self):
        visible = sum(1 for r in range(self._tag_table.rowCount())
                      if not self._tag_table.isRowHidden(r))
        checked = sum(1 for r in range(self._tag_table.rowCount())
                      if not self._tag_table.isRowHidden(r) and
                      self._tag_table.item(r, self._C_CHK) and
                      self._tag_table.item(r, self._C_CHK).checkState() == Qt.CheckState.Checked)
        self._tag_count_lbl.setText(f"{visible} taggar visas  |  {checked} valda")

    def _set_all_checked(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for r in range(self._tag_table.rowCount()):
            if not self._tag_table.isRowHidden(r):
                item = self._tag_table.item(r, self._C_CHK)
                if item:
                    item.setCheckState(state)
        self._update_tag_count()

    def _pfx_show_tags(self, current_row, *_):
        self._pfx_tag_list.clear()
        if current_row < 0:
            return
        item = self._pfx_table.item(current_row, 0)
        if not item:
            return
        prefix = item.data(Qt.ItemDataRole.UserRole)
        data   = self.scan_result.get(prefix, {})
        for tag in data.get('tags', []):
            page = data['pages'].get(tag, 0)
            self._pfx_tag_list.addItem(f"{tag}   (sida {page + 1})")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _save_types(self):
        if not hasattr(self.db, 'save_equipment_type'):
            return
        saved = 0
        for r in range(self._pfx_table.rowCount()):
            pfx_item = self._pfx_table.item(r, 0)
            combo    = self._pfx_table.cellWidget(r, 4)
            if pfx_item and combo and combo.currentText():
                known = KNOWN_PREFIXES.get(pfx_item.text(), ('', ''))
                self.db.save_equipment_type(
                    pfx_item.text(), combo.currentText(),
                    known[0] if known else combo.currentText())
                saved += 1
        QMessageBox.information(self, "Sparat",
                                f"{saved} prefix-mappningar sparade till databasen.")

    def _create_nodes_from_tag_table(self):
        rows_to_create = []
        for r in range(self._tag_table.rowCount()):
            chk = self._tag_table.item(r, self._C_CHK)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                edit_item = self._tag_table.item(r, self._C_EDIT)
                pg_item   = self._tag_table.item(r, self._C_PAGE)
                combo     = self._tag_table.cellWidget(r, self._C_TYPE)
                desc_item = self._tag_table.item(r, self._C_DESC)

                tag       = edit_item.text().strip() if edit_item else ''
                page      = int(pg_item.text()) - 1 if pg_item else 0
                eq_type   = combo.currentText() if combo else ''
                desc      = desc_item.text().strip() if desc_item else ''
                if tag:
                    rows_to_create.append((tag, page, eq_type, desc))

        if not rows_to_create:
            QMessageBox.information(self, "Ingen vald",
                "Kryssa i minst en tagg i fliken 'Alla taggar'.")
            return

        created = 0
        for tag, page, eq_type, desc in rows_to_create:
            node_id = self.db.add_node_with_markup(
                tag, [], {'color': '#FF8C00', 'width': 2, 'alpha': 180}, page)
            pid_ref = f"Sida {page + 1}"
            self.db.conn.execute(
                "UPDATE nodes SET name=?, pid_ref=?, description=? WHERE id=?",
                (tag, pid_ref,
                 f"{eq_type}{': ' + desc if desc else ''}",
                 node_id))
            self.db.conn.commit()
            created += 1

        QMessageBox.information(self, "Klart",
            f"{created} noder skapade. Uppdatera trädet i HAZOP-vyn.")


class ComponentPickerDialog(QDialog):
    def __init__(self, parent=None, suggested_tag='',
                 component_types=None, mode_freqs=None, preselect_type=''):
        super().__init__(parent)
        self.setWindowTitle("Välj komponent och felmod")
        self.setMinimumWidth(460)
        self.selected_type   = ''
        self.selected_modes  = []
        self.selected_tag    = ''
        self.selected_freqs  = {}
        self._comp_types     = component_types or COMPONENT_TYPES
        self._mode_freqs     = mode_freqs or {}
        self._preselect_type = preselect_type  # auto-detected symbol type

        layout = QVBoxLayout(self)

        # Show pre-selected type (learned from previous use)
        if preselect_type:
            hint = QLabel(f"💡 Igenkänd typ (lärde mig): {preselect_type}")
            hint.setStyleSheet(
                "background:#fff8e1; border:1px solid #ffe082; border-radius:3px;"
                "padding:4px 8px; color:#795548; font-size:11px;")
            layout.addWidget(hint)
        form   = QFormLayout()

        self.tag_edit = QLineEdit(suggested_tag)
        self.tag_edit.setPlaceholderText("t.ex. V-01  (lästes från PDF, ändra vid behov)")
        form.addRow("Komponent-ID:", self.tag_edit)

        self.type_combo = QComboBox()
        self.type_combo.addItems(list(self._comp_types.keys()))
        self.type_combo.currentTextChanged.connect(self._update_modes)
        form.addRow("Komponenttyp:", self.type_combo)
        # Pre-select detected type from symbol classifier
        if self._preselect_type:
            idx = self.type_combo.findText(self._preselect_type)
            if idx >= 0:
                self.type_combo.setCurrentIndex(idx)
        layout.addLayout(form)

        layout.addWidget(QLabel("Felmod(er) — flerval möjligt:"))
        self.mode_list = QListWidget()
        self.mode_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.mode_list.setMinimumHeight(160)
        layout.addWidget(self.mode_list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_modes(self.type_combo.currentText())

    def _update_modes(self, type_name):
        if not hasattr(self, 'mode_list'):
            return   # called before mode_list is created (during __init__)
        self.mode_list.clear()
        freqs = self._mode_freqs.get(type_name, {})
        for mode in self._comp_types.get(type_name, []):
            freq = freqs.get(mode)
            if freq is not None:
                # Show frequency info alongside mode description
                f_lbl = self._freq_label(freq)
                display = f"{mode}  [{f_lbl}]"
            else:
                display = mode
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, mode)   # original desc without freq
            if freq is not None:
                item.setToolTip(f"Frekvens: {freq:.4g} händelser/år  →  {f_lbl}")
            self.mode_list.addItem(item)

    @staticmethod
    def _freq_label(freq):
        """Format a frequency value as a readable F-level string."""
        if freq is None or freq <= 0:
            return "—"
        from math import floor, log10
        # Compute F-level (same formula as freq_to_f_level in hazop.py)
        boundaries = [1e-5, 1e-4, 1e-3, 0.01, 0.1, 1.0]
        f_level = len(boundaries) - 1
        for i, b in enumerate(boundaries):
            if freq < b:
                f_level = i - 1
                break
        return f"F={f_level}  ({freq:.3g}/år)"

    def _on_accept(self):
        self.selected_type = self.type_combo.currentText()
        self.selected_tag  = self.tag_edit.text().strip()
        selected = self.mode_list.selectedItems()
        if not selected and self.mode_list.count() > 0:
            self.mode_list.item(0).setSelected(True)
            selected = [self.mode_list.item(0)]
        # Use UserRole (original desc) to strip the freq annotation added in display
        self.selected_modes = [
            item.data(Qt.ItemDataRole.UserRole) or item.text()
            for item in selected
        ]
        # Collect freq_per_year for each selected mode
        freqs = self._mode_freqs.get(self.selected_type, {})
        self.selected_freqs = {mode: freqs.get(mode) for mode in self.selected_modes}
        self.accept()


class TargetPickerDialog(QDialog):
    def __init__(self, parent=None, suggested_tag='', db=None):
        super().__init__(parent)
        self.setWindowTitle("Välj konsekvens")
        self.setMinimumWidth(500)
        self._db            = db
        self.template       = ''
        self.target         = ''
        self.selected_chain = {}
        self.link_to_id     = None   # set if user picks an existing consequence

        layout = QVBoxLayout(self)

        # Link to existing button (when db is available)
        if db is not None:
            link_btn = QPushButton("🔗 Länka till befintlig konsekvens")
            link_btn.setStyleSheet(
                "background:#2c7bb6; color:white; border:none; border-radius:4px; padding:4px 10px;")
            link_btn.clicked.connect(self._pick_existing)
            layout.addWidget(link_btn)
            sep = QLabel("— eller skapa ny konsekvens —")
            sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sep.setStyleSheet("color:#888; font-size:10px;")
            layout.addWidget(sep)

        # ── Template list ─────────────────────────────────────────────────────
        layout.addWidget(QLabel("Konsekvensmall:"))
        self.template_list = QListWidget()
        self.template_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.template_list.setMaximumHeight(130)
        for tpl in CONSEQUENCE_TEMPLATES:
            self.template_list.addItem(QListWidgetItem(tpl))
        self.template_list.setCurrentRow(0)
        self.template_list.currentRowChanged.connect(self._update_preview)
        layout.addWidget(self.template_list)

        form = QFormLayout()
        self.target_edit = QLineEdit(suggested_tag)
        self.target_edit.setPlaceholderText("t.ex. T-101")
        self.target_edit.textChanged.connect(self._update_preview)
        form.addRow("Målobjekt:", self.target_edit)
        layout.addLayout(form)

        # ── Consequence chain ─────────────────────────────────────────────────
        chain_box = QGroupBox("Konsekvenskedja  (valfritt)")
        chain_grid = QGridLayout(chain_box)
        chain_grid.setSpacing(3)
        self._chain_checks: dict = {}
        row_idx, col_idx, last_group = 0, 0, None

        for key, label, group in _PID_CHAIN_ITEMS:
            if group and group != last_group:
                if col_idx > 0:
                    row_idx += 1; col_idx = 0
                hdr = QLabel(group)
                hdr.setStyleSheet(
                    "color:#1F4E79; font-weight:bold; font-size:10px; margin-top:3px;")
                chain_grid.addWidget(hdr, row_idx, 0, 1, 3)
                row_idx += 1; col_idx = 0
                last_group = group
            chk = QCheckBox(label)
            chk.stateChanged.connect(self._update_preview)
            self._chain_checks[key] = chk
            chain_grid.addWidget(chk, row_idx, col_idx)
            col_idx += 1
            if col_idx >= 3:
                col_idx = 0; row_idx += 1

        layout.addWidget(chain_box)

        # ── Full preview ──────────────────────────────────────────────────────
        layout.addWidget(QLabel("Fullständig text:"))
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(
            "color:#1F4E79; font-weight:bold; padding:4px;"
            "background:#eef4fb; border:1px solid #bee3f8; border-radius:3px;")
        layout.addWidget(self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_preview()

    def set_target(self, name):
        self.target_edit.setText(name or '')

    def _current_template(self):
        item = self.template_list.currentItem()
        return item.text() if item else (CONSEQUENCE_TEMPLATES[0] if CONSEQUENCE_TEMPLATES else '{}')

    def _base_text(self):
        tpl  = self._current_template()
        name = self.target_edit.text().strip() or '[okänt objekt]'
        try:
            return tpl.format(name)
        except Exception:
            return tpl.replace('{}', name)

    def _update_preview(self, *_):
        chain = {k: chk.isChecked() for k, chk in self._chain_checks.items()}
        full  = _pid_build_chain_text(self._base_text(), chain)
        self.preview.setText(full or self._base_text())

    def _pick_existing(self):
        if self._db is None:
            return
        dlg = ExistingConsequencePicker(self._db, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_id:
            self.link_to_id = dlg.selected_id
            self.accept()

    def _on_accept(self):
        self.template = self._current_template()
        self.target   = self.target_edit.text().strip()
        self.selected_chain = {k: chk.isChecked()
                               for k, chk in self._chain_checks.items()}
        self.accept()


class ExistingConsequencePicker(QDialog):
    """Pick an existing consequence from the project to link to the current cause."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.selected_id = None
        self.setWindowTitle("Länka till befintlig konsekvens")
        self.setMinimumSize(640, 380)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Välj en befintlig konsekvens att länka den nya orsaken till:"))

        # Search
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filtrera…")
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        # Table
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(['Nod', 'Orsak', 'Konsekvens', 'Risk'])
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 90)
        self._table.setColumnWidth(3, 80)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.doubleClicked.connect(self._accept)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:3px;}")
        layout.addWidget(self._table)

        btns = QHBoxLayout()
        ok_btn = QPushButton("🔗 Länka till markerad")
        ok_btn.clicked.connect(self._accept)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(ok_btn); btns.addStretch(); btns.addWidget(cancel_btn)
        layout.addLayout(btns)

        self._populate()

    def _populate(self):
        self._rows = []   # list of (cons_id, node_name, cause_desc, cons_desc, risk_label, bg)
        try:
            for node in self.db.nodes():
                for cause in self.db.causes(node['id']):
                    for cons in self.db.consequences(cause['id']):
                        self._rows.append((
                            cons['id'],
                            node['name'],
                            cause['description'],
                            cons['description'],
                        ))
        except Exception:
            pass
        self._apply_filter()

    def _apply_filter(self):
        text = self._filter.text().lower()
        self._table.setRowCount(0)
        for cons_id, node, cause, cons_desc in self._rows:
            if text and text not in (node + cause + cons_desc).lower():
                continue
            r = self._table.rowCount()
            self._table.insertRow(r)
            for col, val in enumerate([node, cause, cons_desc, '']):
                item = QTableWidgetItem(val)
                item.setData(Qt.ItemDataRole.UserRole, cons_id)
                self._table.setItem(r, col, item)
            self._table.setRowHeight(r, 24)

    def _accept(self):
        row = self._table.currentRow()
        if row < 0:
            return
        item = self._table.item(row, 0)
        if item:
            self.selected_id = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


class ExistingSafeguardPicker(QDialog):
    """Pick an existing safeguard to link to the current consequence."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.selected_id = None
        self.setWindowTitle("Länka till befintlig safeguard")
        self.setMinimumSize(600, 340)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Välj en befintlig safeguard att länka till denna konsekvens:"))

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filtrera…")
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(['Konsekvens', 'Safeguard', 'RRF'])
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(2, 70)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.doubleClicked.connect(self._accept)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:3px;}")
        layout.addWidget(self._table)

        btns = QHBoxLayout()
        ok_btn = QPushButton("🔗 Länka till markerad")
        ok_btn.clicked.connect(self._accept)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(ok_btn); btns.addStretch(); btns.addWidget(cancel_btn)
        layout.addLayout(btns)

        self._populate()

    def _populate(self):
        self._rows = []
        try:
            for node in self.db.nodes():
                for cause in self.db.causes(node['id']):
                    for cons in self.db.consequences(cause['id']):
                        for sg in self.db.safeguards(cons['id']):
                            self._rows.append((
                                sg['id'],
                                cons['description'],
                                sg['description'],
                                sg['rrf'] or 1,
                            ))
        except Exception:
            pass
        self._apply_filter()

    def _apply_filter(self):
        text = self._filter.text().lower()
        self._table.setRowCount(0)
        for sg_id, cons_desc, sg_desc, rrf in self._rows:
            if text and text not in (cons_desc + sg_desc).lower():
                continue
            r = self._table.rowCount()
            self._table.insertRow(r)
            for col, val in enumerate([cons_desc, sg_desc, f"RRF {rrf}" if rrf > 1 else "—"]):
                item = QTableWidgetItem(val)
                item.setData(Qt.ItemDataRole.UserRole, sg_id)
                self._table.setItem(r, col, item)
            self._table.setRowHeight(r, 24)

    def _accept(self):
        row = self._table.currentRow()
        if row < 0:
            return
        item = self._table.item(row, 0)
        if item:
            self.selected_id = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


class SafeguardPickerDialog(QDialog):
    def __init__(self, parent=None, suggested_tag='', existing_safeguards=None, db=None,
                 existing_bpcs_count=0):
        super().__init__(parent)
        self.setWindowTitle("Markera safeguard / barriär")
        self.setMinimumWidth(440)
        self._db              = db
        self.tag              = ''
        self.description      = ''
        self.sg_type          = 'Övrigt'
        self.rrf              = 1
        self.add_more         = False
        self.link_to_id       = None
        self._existing_bpcs   = existing_bpcs_count

        layout = QVBoxLayout(self)

        # Link to existing button (shown when db is available)
        if db is not None:
            link_btn = QPushButton("🔗 Länka till befintlig safeguard")
            link_btn.setStyleSheet(
                "background:#2c7bb6; color:white; border:none; border-radius:4px; padding:4px 10px;")
            link_btn.clicked.connect(self._pick_existing)
            layout.addWidget(link_btn)

            sep = QLabel("— eller skapa ny —")
            sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sep.setStyleSheet("color:#888; font-size:10px;")
            layout.addWidget(sep)

        form = QFormLayout()

        self.tag_edit = QLineEdit(suggested_tag)
        self.tag_edit.setPlaceholderText("t.ex. PSV-101  (lästes från PDF)")
        form.addRow("ID / tag:", self.tag_edit)

        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("t.ex. Säkerhetsventil, Nivålarm LAH-101")
        form.addRow("Beskrivning:", self.desc_edit)

        self.type_combo = QComboBox()
        self.type_combo.addItems(_SG_TYPES)
        self.type_combo.setCurrentIndex(len(_SG_TYPES) - 1)  # default Övrigt
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("Typ av barriär:", self.type_combo)

        self.rrf_combo = QComboBox()
        self.rrf_combo.addItems(_RRF_LABELS)
        form.addRow("RRF:", self.rrf_combo)

        layout.addLayout(form)

        # BPCS warning (IEC 61511 — two BPCS layers ≤ RRF10 combined)
        self._bpcs_warn = QLabel(
            "⚠️  Redan en BPCS-barriär på denna konsekvens. "
            "Enligt IEC 61511 får två BPCS-skydd inte ge mer än RRF 10 totalt.")
        self._bpcs_warn.setWordWrap(True)
        self._bpcs_warn.setStyleSheet(
            "background:#fff3cd; color:#856404; border:1px solid #ffc107;"
            "border-radius:4px; padding:6px 8px; font-size:11px;")
        self._bpcs_warn.setVisible(False)
        layout.addWidget(self._bpcs_warn)

        if existing_safeguards:
            lbl = QLabel("Snabbval (befintliga safeguards för denna konsekvens):")
            lbl.setStyleSheet("font-size:10px; color:#555;")
            layout.addWidget(lbl)
            for sg_text in existing_safeguards[:6]:
                btn = QPushButton(sg_text)
                btn.setFlat(True)
                btn.setStyleSheet("text-align:left; color:#1a7a40; padding:2px; font-size:10px;")
                btn.clicked.connect(lambda _, s=sg_text: self.desc_edit.setText(s))
                layout.addWidget(btn)

        # Buttons: OK | + Lägg till ytterligare | Avbryt
        btn_row = QHBoxLayout()

        ok_btn = QPushButton("✓ Spara")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(lambda: self._on_accept(add_more=False))
        btn_row.addWidget(ok_btn)

        add_btn = QPushButton("➕ Spara och lägg till ytterligare")
        add_btn.setToolTip(
            "Sparar denna safeguard och håller läget klart\n"
            "för att lägga till ytterligare safeguard på kartan.")
        add_btn.setStyleSheet(
            "background:#1F4E79; color:white; border:none;"
            "border-radius:4px; padding:4px 10px; font-weight:bold;")
        add_btn.clicked.connect(lambda: self._on_accept(add_more=True))
        btn_row.addWidget(add_btn)

        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

        # Show warning immediately if BPCS already present and BPCS is pre-selected
        self._on_type_changed(self.type_combo.currentIndex())

    def _on_type_changed(self, idx):
        selected = _SG_TYPES[idx] if idx < len(_SG_TYPES) else 'Övrigt'
        self._bpcs_warn.setVisible(selected == 'BPCS' and self._existing_bpcs >= 1)

    def _pick_existing(self):
        if self._db is None:
            return
        dlg = ExistingSafeguardPicker(self._db, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_id:
            self.link_to_id = dlg.selected_id
            self.add_more   = False
            self.accept()

    def _on_accept(self, add_more=False):
        self.tag         = self.tag_edit.text().strip()
        self.description = self.desc_edit.text().strip()
        if not self.description:
            self.description = self.tag or 'Safeguard'
        self.sg_type  = _SG_TYPES[self.type_combo.currentIndex()]
        self.rrf      = _RRF_VALUES[self.rrf_combo.currentIndex()]
        self.add_more = add_more
        self.accept()


class _PageRenderer(QThread):
    """Background thread that pre-renders PDF pages to raw pixel data for the page cache."""
    page_ready = pyqtSignal(int, object, int, int, int)  # page_num, raw_bytes, w, h, stride

    def __init__(self, pdf_path, pages, scale, parent=None):
        super().__init__(parent)
        self._path  = pdf_path
        self._pages = pages
        self._scale = scale

    def run(self):
        if not HAS_PYMUPDF:
            return
        try:
            doc = fitz.open(self._path)
            for pn in self._pages:
                if self.isInterruptionRequested():
                    break
                page = doc.load_page(pn)
                mat  = fitz.Matrix(self._scale, self._scale)
                pix  = page.get_pixmap(matrix=mat, alpha=False)
                self.page_ready.emit(pn, bytes(pix.samples), pix.width, pix.height, pix.stride)
            doc.close()
        except Exception:
            pass


class SmartPipeTracer:
    """
    Traces pipe paths on a rendered P&ID page using A* on a greyscale image.
    Works on both colour and B&W P&IDs; detects dark pixels as pipe material.
    Gap-jumping handles crossings drawn with a small break between lines.
    """
    DARK_THRESHOLD = 110    # pixels darker than this count as "pipe"
    TRACE_SCALE    = 1.0    # render resolution multiplier for tracing
    MAX_GAP        = 7      # max white-pixel gap to jump across (crossing style)
    MAX_EXPLORE    = 300_000  # A* node limit (safety)
    GOAL_RADIUS_SQ = 25     # squared pixel distance that counts as "reached end"

    def __init__(self, pdf_doc, page_n):
        page = pdf_doc[page_n]
        mat  = fitz.Matrix(self.TRACE_SCALE, self.TRACE_SCALE)
        pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        self.width  = pix.width
        self.height = pix.height
        self._data  = bytearray(pix.samples)   # flat greyscale bytes
        self._tmask = self._build_text_mask(page)   # pixels inside text bboxes

    # ------------------------------------------------------------------ helpers

    def _build_text_mask(self, page):
        """
        Return a bytearray (same size as image) where 1 = inside a text bbox.
        Uses PyMuPDF text extraction — works on vector P&IDs natively.
        For scanned rasters with no text layer the result is all-zero (no effect).
        """
        mask = bytearray(self.width * self.height)
        pad  = max(2, int(4 * self.TRACE_SCALE))  # pixel padding around each bbox
        try:
            blocks = page.get_text("dict", flags=0)["blocks"]
            for block in blocks:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        bx0, by0, bx1, by1 = span["bbox"]
                        # Convert PDF coords → tracer pixel coords and expand
                        px0 = max(0,            int(bx0 * self.TRACE_SCALE) - pad)
                        py0 = max(0,            int(by0 * self.TRACE_SCALE) - pad)
                        px1 = min(self.width,   int(bx1 * self.TRACE_SCALE) + pad + 1)
                        py1 = min(self.height,  int(by1 * self.TRACE_SCALE) + pad + 1)
                        W = self.width
                        for y in range(py0, py1):
                            base = y * W
                            for x in range(px0, px1):
                                mask[base + x] = 1
        except Exception:
            pass  # if extraction fails, proceed without masking (graceful degradation)
        return mask

    def _is_dark(self, x, y):
        """Raw dark-pixel check (no text exclusion)."""
        if 0 <= x < self.width and 0 <= y < self.height:
            return self._data[y * self.width + x] < self.DARK_THRESHOLD
        return False

    def _is_pipe(self, x, y):
        """True only if pixel is dark AND not inside a text bounding box."""
        if 0 <= x < self.width and 0 <= y < self.height:
            idx = y * self.width + x
            return self._data[idx] < self.DARK_THRESHOLD and not self._tmask[idx]
        return False

    def _nearest_dark(self, x, y, radius=15):
        """Spiral-search for nearest dark pixel within radius."""
        x, y = int(x), int(y)
        if self._is_pipe(x, y):
            return (x, y)
        best, best_d2 = None, radius * radius + 1
        for r in range(1, radius + 1):
            for dx in range(-r, r + 1):
                for sign in (-1, 1):
                    nx, ny = x + dx, y + sign * r
                    if 0 <= nx < self.width and 0 <= ny < self.height:
                        d2 = dx * dx + r * r
                        if d2 < best_d2 and self._is_pipe(nx, ny):
                            best, best_d2 = (nx, ny), d2
                for dy in range(-r + 1, r):
                    ny, nx = y + dy, x + sign * r
                    if 0 <= nx < self.width and 0 <= ny < self.height:
                        d2 = r * r + dy * dy
                        if d2 < best_d2 and self._is_pipe(nx, ny):
                            best, best_d2 = (nx, ny), d2
        return best

    def _reconstruct(self, came_from, node):
        path = []
        cur  = node
        while cur is not None:
            path.append(cur)
            cur = came_from[cur]
        path.reverse()
        return path

    def _rdp(self, pts, eps=2.5):
        """Iterative Ramer-Douglas-Peucker path simplification."""
        if len(pts) < 3:
            return list(pts)
        keep  = {0, len(pts) - 1}
        stack = [(0, len(pts) - 1)]
        while stack:
            lo, hi = stack.pop()
            if hi - lo < 2:
                continue
            x1, y1 = pts[lo]
            x2, y2 = pts[hi]
            dx, dy  = x2 - x1, y2 - y1
            d2      = dx * dx + dy * dy
            max_d, max_i = 0.0, lo
            for i in range(lo + 1, hi):
                px, py = pts[i]
                if d2 == 0:
                    dist = ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
                else:
                    t    = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / d2))
                    dist = ((px - x1 - t * dx) ** 2 + (py - y1 - t * dy) ** 2) ** 0.5
                if dist > max_d:
                    max_d, max_i = dist, i
            if max_d > eps:
                keep.add(max_i)
                stack.append((lo, max_i))
                stack.append((max_i, hi))
        return [pts[i] for i in sorted(keep)]

    # ------------------------------------------------------------------ core A*

    def _astar(self, start, end, blocked):
        import heapq
        from itertools import count
        _cnt = count()

        ex, ey = end
        def h(x, y): return ((x - ex) ** 2 + (y - ey) ** 2) ** 0.5

        open_h    = [(h(*start), next(_cnt), start)]
        g         = {start: 0.0}
        came_from = {start: None}
        explored  = 0
        W = self.width
        data  = self._data
        tmask = self._tmask
        thr   = self.DARK_THRESHOLD

        while open_h and explored < self.MAX_EXPLORE:
            _, _, cur = heapq.heappop(open_h)
            explored += 1
            cx, cy = cur

            if (cx - ex) ** 2 + (cy - ey) ** 2 <= self.GOAL_RADIUS_SQ:
                return self._reconstruct(came_from, cur)

            gc = g[cur]
            prev = came_from.get(cur)

            # --- 8-directional dark neighbours ---
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == dy == 0:
                        continue
                    nx, ny = cx + dx, cy + dy
                    if nx < 0 or nx >= self.width or ny < 0 or ny >= self.height:
                        continue
                    if data[ny * W + nx] >= thr or tmask[ny * W + nx]:
                        continue
                    nb = (nx, ny)
                    if nb in blocked:
                        continue
                    step = 1.414 if dx and dy else 1.0
                    ng   = gc + step
                    if ng < g.get(nb, 1e18):
                        g[nb]         = ng
                        came_from[nb] = cur
                        f = ng + h(nx, ny)
                        heapq.heappush(open_h, (f, next(_cnt), nb))

            # --- gap jump: continue in last movement direction across short gap ---
            if prev is not None:
                pdx = cx - prev[0]
                pdy = cy - prev[1]
                ndx = (pdx // abs(pdx)) if pdx else 0
                ndy = (pdy // abs(pdy)) if pdy else 0
                if ndx or ndy:
                    # check if current pixel is at the edge of a dark region
                    step_base = 1.414 if ndx and ndy else 1.0
                    for gap in range(2, self.MAX_GAP + 1):
                        jx, jy = cx + ndx * gap, cy + ndy * gap
                        if not (0 <= jx < self.width and 0 <= jy < self.height):
                            break
                        if data[jy * W + jx] < thr and not tmask[jy * W + jx]:
                            jb = (jx, jy)
                            if jb not in blocked:
                                ng = gc + gap * step_base * 0.85  # slight bonus for jumping
                                if ng < g.get(jb, 1e18):
                                    g[jb]         = ng
                                    came_from[jb] = cur
                                    f = ng + h(jx, jy)
                                    heapq.heappush(open_h, (f, next(_cnt), jb))
                            break  # only jump to first dark pixel found

        return None  # path not found

    # ------------------------------------------------------------------ public

    def trace(self, start_pdf, end_pdf, n_alt=2):
        """
        Find up to (n_alt+1) paths from start_pdf to end_pdf.
        Returns list of paths; each path = list of [pdf_x, pdf_y].
        First element is the best path.  Empty list = nothing found.
        """
        sx = int(start_pdf[0] * self.TRACE_SCALE)
        sy = int(start_pdf[1] * self.TRACE_SCALE)
        ex = int(end_pdf[0]   * self.TRACE_SCALE)
        ey = int(end_pdf[1]   * self.TRACE_SCALE)

        start = self._nearest_dark(sx, sy, 15)
        end   = self._nearest_dark(ex, ey, 15)
        if not start or not end:
            return []

        results = []
        blocked = set()

        for _ in range(n_alt + 1):
            px_path = self._astar(start, end, frozenset(blocked))
            if not px_path:
                break
            simplified  = self._rdp(px_path, eps=2.5)
            pdf_path    = [[x / self.TRACE_SCALE, y / self.TRACE_SCALE]
                           for x, y in simplified]
            results.append(pdf_path)
            # Block the middle half of this path so next search takes a different route
            lo = len(px_path) // 4
            hi = 3 * len(px_path) // 4
            blocked.update(px_path[lo:hi])

        return results


class ConnectorAnalyzer(QThread):
    """Scans PDF pages for off-page connectors and proposes a board layout."""
    progress   = pyqtSignal(str)
    # connectors, connections, layout, page_sheet_nums
    finished_analysis = pyqtSignal(list, list, dict, dict)

    def __init__(self, pdf_path, page_count, page_widths_pdf, page_heights_pdf,
                 render_scale, active_pages=None, parent=None):
        super().__init__(parent)
        self._pdf_path        = str(pdf_path)
        self._page_count      = page_count
        self._page_widths_pdf = dict(page_widths_pdf)
        self._page_heights_pdf = dict(page_heights_pdf)
        self._render_scale    = render_scale
        # active_pages: only these get laid out (others are scanned for connectors only)
        self._active_pages    = list(active_pages) if active_pages is not None else None
        self._deadline        = 0.0

    def run(self):
        import time
        self._deadline = time.time() + 45.0   # longer for OCR-heavy sets
        all_connectors = []
        page_sheet_nums = {}  # pn → sheet number string (best guess)

        try:
            doc = fitz.open(self._pdf_path)
        except Exception as e:
            self.progress.emit(f"Kunde inte öppna PDF: {e}")
            self.finished_analysis.emit([], [], {}, {})
            return

        # ── Auto-detect dialect from first 5 pages ────────────────────────────
        sample_texts = []
        for pn in range(min(5, doc.page_count)):
            sample_texts.append(doc.load_page(pn).get_text("text"))
        self._dialect = _detect_dialect(sample_texts)
        dialect_conf  = _DIALECTS[self._dialect]
        self.progress.emit(f"Dialekt: {dialect_conf['name']}")

        for pn in range(doc.page_count):
            if time.time() > self._deadline:
                self.progress.emit(f"Tidsgräns — {pn}/{doc.page_count} blad klara")
                break
            self.progress.emit(f"Blad {pn + 1}/{doc.page_count}…")
            page = doc.load_page(pn)
            pw = float(page.rect.width)
            ph = float(page.rect.height)

            # ── Extract sheet number using dialect title area ──────────────────
            ta = dialect_conf['title_area']
            title_rect = fitz.Rect(pw*ta[0], ph*ta[1], pw*ta[2], ph*ta[3])
            title_text = page.get_text("text", clip=title_rect)
            m = dialect_conf['sheet_num_re'].search(title_text)
            if m:
                page_sheet_nums[pn] = m.group(1).upper().strip()

            # ── Native text in edge zones ──────────────────────────────────────
            spans = self._get_spans(page)
            native_word_count = len(spans)
            connectors = self._find_in_zones(spans, pn, pw, ph,
                                             ocr_used=False, page=page)

            # ── OCR: trigger when page has few native words (scanned PDF) ──────
            needs_ocr = (not connectors or native_word_count < 30)
            if needs_ocr and HAS_PYMUPDF and time.time() < self._deadline - 2.0:
                ocr_text = self._ocr_edges(page, pw, ph)
                if ocr_text:
                    ocr_spans = self._text_to_spans(ocr_text, pw, ph)
                    ocr_conns = self._find_in_zones(ocr_spans, pn, pw, ph,
                                                    ocr_used=True, page=page)
                    if ocr_conns:
                        connectors = ocr_conns
                    # Also try to extract sheet number from OCR if not found yet
                    if pn not in page_sheet_nums:
                        all_ocr = ' '.join(ocr_text.values())
                        m2 = dialect_conf['sheet_num_re'].search(all_ocr)
                        if m2:
                            page_sheet_nums[pn] = m2.group(1).upper().strip()

            all_connectors.extend(connectors)

        doc.close()

        # ── Build sheet-number lookup: sheet_str → pn ──
        sheet_lookup = {v.upper(): k for k, v in page_sheet_nums.items()}
        # Also try partial match: last component of "UNIT-P-101" → "P-101"
        for k, v in list(sheet_lookup.items()):
            parts = k.split('-')
            if len(parts) >= 2:
                sheet_lookup.setdefault('-'.join(parts[-2:]), list(sheet_lookup.values())[0])

        # ── Match connectors into connections ──
        connections = self._match_connections(all_connectors, sheet_lookup,
                                              self._page_count, page_sheet_nums)

        # ── Propose layout (active pages only) ──
        layout_pages = (self._active_pages if self._active_pages is not None
                        else list(range(self._page_count)))
        layout = _propose_layout(connections, layout_pages,
                                 self._page_widths_pdf, self._page_heights_pdf,
                                 self._render_scale)

        # Convert int keys to str for JSON serialisation
        sheet_num_map_str = {str(k): v for k, v in page_sheet_nums.items()}
        self.finished_analysis.emit(all_connectors, connections, layout, sheet_num_map_str)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_spans(self, page):
        spans = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    t = span.get("text", "").strip()
                    if not t:
                        continue
                    b = span["bbox"]
                    spans.append({"text": t, "x": (b[0]+b[2])/2, "y": (b[1]+b[3])/2,
                                  "x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]})
        return spans

    def _text_to_spans(self, text_dict, pw, ph):
        """Convert OCR output dict {edge: str} into pseudo-spans with edge positions."""
        spans = []
        for edge, text in text_dict.items():
            if not text.strip():
                continue
            if edge == 'left':
                cx, cy = pw * 0.12, ph * 0.5
            elif edge == 'right':
                cx, cy = pw * 0.88, ph * 0.5
            elif edge == 'top':
                cx, cy = pw * 0.5, ph * 0.06
            else:
                cx, cy = pw * 0.5, ph * 0.94
            for line in text.split('\n'):
                line = line.strip()
                if line:
                    spans.append({"text": line, "x": cx, "y": cy,
                                  "x0": cx-10, "y0": cy-5, "x1": cx+10, "y1": cy+5})
        return spans

    def _find_arrow_shapes(self, page, pw, ph):
        """Detect pentagon/arrow connector shapes near page edges using vector graphics.
        Returns list of (cx, cy, edge) tuples in PDF coordinates."""
        results = []
        try:
            drawings = page.get_drawings()
        except Exception:
            return results
        for d in drawings:
            r = d.get('rect')
            if r is None:
                continue
            w = r.x1 - r.x0
            h = r.y1 - r.y0
            # Skip tiny marks and huge blocks (title boxes etc.)
            if w < 15 or h < 6 or w > pw * 0.55 or h > ph * 0.25:
                continue
            # Must be near an edge
            at_left   = r.x1 < pw * 0.32
            at_right  = r.x0 > pw * 0.68
            at_top    = r.y1 < ph * 0.26
            at_bottom = r.y0 > ph * 0.74
            if not (at_left or at_right or at_top or at_bottom):
                continue
            # Arrow-like aspect ratio: for left/right edges wide>tall; top/bottom tall>wide
            if (at_left or at_right) and not (1.5 < w / max(h, 1) < 20):
                continue
            if (at_top or at_bottom) and not (0.3 < w / max(h, 1) < 3):
                continue
            n_items = len(d.get('items', []))
            if not (3 <= n_items <= 10):
                continue
            cx = (r.x0 + r.x1) / 2
            cy = (r.y0 + r.y1) / 2
            if at_left:
                edge = 'left'
            elif at_right:
                edge = 'right'
            elif at_top:
                edge = 'top'
            else:
                edge = 'bottom'
            results.append((cx, cy, edge, r))
        return results

    def _find_in_zones(self, spans, pn, pw, ph, ocr_used, page=None):
        edge_zones = {
            'left':   (0,        0,    pw*0.32, ph),
            'right':  (pw*0.68,  0,    pw,      ph),
            'top':    (0,        0,    pw,      ph*0.26),
            'bottom': (0,  ph*0.74,    pw,      ph),
        }
        results = []
        seen_refs = set()   # deduplicate same ref_sheet at same (edge, approx_y)

        # ── Pass 1: text-in-edge-zone (as before) ─────────────────────────────
        for edge, (x0, y0, x1, y1) in edge_zones.items():
            zone_spans = [s for s in spans
                          if x0 <= s["x"] <= x1 and y0 <= s["y"] <= y1]
            if not zone_spans:
                continue
            clusters = self._cluster_spans(zone_spans, gap=60.0)
            for cluster in clusters:
                combined = ' '.join(s["text"] for s in cluster)
                cx = sum(s["x"] for s in cluster) / len(cluster)
                cy = sum(s["y"] for s in cluster) / len(cluster)
                conn = self._parse_connector(combined, edge, pn, cx, cy,
                                             pw, ph, ocr_used)
                if conn:
                    key = (edge, conn['ref_sheet'], round(cy / max(ph, 1) * 20))
                    if key not in seen_refs:
                        seen_refs.add(key)
                        results.append(conn)

        # ── Pass 2: vector-shape anchored search ──────────────────────────────
        if page is not None:
            for shape_cx, shape_cy, edge, shape_rect in self._find_arrow_shapes(page, pw, ph):
                # Collect all text within 1.5× the shape bounding box
                margin_x = max(shape_rect.width * 1.5, 40)
                margin_y = max(shape_rect.height * 2.0, 40)
                nearby = [s for s in spans
                          if abs(s["x"] - shape_cx) < margin_x + shape_rect.width
                          and abs(s["y"] - shape_cy) < margin_y + shape_rect.height]
                if not nearby:
                    continue
                combined = ' '.join(s["text"] for s in nearby)
                conn = self._parse_connector(combined, edge, pn,
                                             shape_cx, shape_cy,
                                             pw, ph, ocr_used)
                if conn:
                    key = (edge, conn['ref_sheet'], round(shape_cy / max(ph, 1) * 20))
                    if key not in seen_refs:
                        seen_refs.add(key)
                        results.append(conn)
        return results

    def _cluster_spans(self, spans, gap=60.0):
        if not spans:
            return []
        spans = sorted(spans, key=lambda s: (s["y"], s["x"]))
        clusters, current = [], [spans[0]]
        for s in spans[1:]:
            prev = current[-1]
            dy = abs(s["y"] - prev["y"])
            dx = abs(s["x"] - prev["x"])
            if dy < gap and dx < gap * 3:
                current.append(s)
            else:
                clusters.append(current)
                current = [s]
        clusters.append(current)
        return clusters

    def _parse_connector(self, text, edge, pn, cx, cy, pw, ph, ocr_used):
        dialect = getattr(self, '_dialect', 'classic')
        keyword = ref_sheet = rds_code = None
        lkab_format = its_format = gryaab_format = False

        # ── Dialect-specific primary detection ────────────────────────────────
        if dialect == 'lkab':
            m_rds = _RE_RDS_SHEET.search(text)
            if m_rds:
                rds_code    = m_rds.group(1).upper()
                ref_sheet   = ('S' + m_rds.group(2)).upper()
                lkab_format = True
            else:
                m_kw = _RE_TO_FROM.search(text)
                if m_kw:
                    keyword   = m_kw.group(1).upper()
                    ref_sheet = m_kw.group(2).upper().strip()
                else:
                    m2 = re.search(r'\bS(\d{6,8})\b', text, re.I)
                    if m2:
                        ref_sheet = ('S' + m2.group(1)).upper()

        elif dialect == 'its':
            m_its = _RE_ITS_CONN.search(text)
            if m_its:
                ref_sheet  = m_its.group(1).upper()   # e.g. XFB_40208
                its_format = True
            else:
                m_kw = _RE_TO_FROM.search(text)
                if m_kw:
                    keyword   = m_kw.group(1).upper()
                    ref_sheet = m_kw.group(2).upper().strip()
                else:
                    m2 = _DIALECTS['its']['sheet_num_re'].search(text)
                    if m2:
                        ref_sheet = m2.group(1).upper()

        elif dialect == 'gryaab':
            m_kw = _RE_TO_FROM.search(text)
            if m_kw:
                keyword   = m_kw.group(1).upper()
                ref_sheet = m_kw.group(2).upper().strip()
            else:
                m_gr = _RE_GRYAAB_CONN.search(text)
                if m_gr:
                    ref_sheet     = m_gr.group(1).upper()
                    gryaab_format = True

        elif dialect == 'hybrit':
            m_kw = _RE_TO_FROM.search(text)
            if m_kw:
                keyword   = m_kw.group(1).upper()
                ref_sheet = m_kw.group(2).upper().strip()
            else:
                m2 = _DIALECTS['hybrit']['sheet_num_re'].search(text)
                if m2:
                    ref_sheet = m2.group(1).upper()

        else:  # classic / fallback
            m_kw = _RE_TO_FROM.search(text)
            if m_kw:
                keyword   = m_kw.group(1).upper()
                ref_sheet = m_kw.group(2).upper().strip()
            else:
                m_rds = _RE_RDS_SHEET.search(text)
                if m_rds:
                    rds_code    = m_rds.group(1).upper()
                    ref_sheet   = ('S' + m_rds.group(2)).upper()
                    lkab_format = True
                else:
                    m2 = _RE_SHEET_NUM.search(text)
                    if m2:
                        ref_sheet = m2.group(1).upper().strip()

        if not ref_sheet:
            return None

        # ── Direction ─────────────────────────────────────────────────────────
        kw_upper = (keyword or '').upper().replace('Å', 'A').replace('Ä', 'A')
        if kw_upper in ('TO', "CONT'D", 'CONTD', 'TILL'):
            dir_kw = 'out'
        elif kw_upper in ('FROM', 'FRAN', 'FRAAN'):
            dir_kw = 'in'
        else:
            dir_kw = None

        edge_dir_map = {'right': 'out', 'left': 'in', 'top': 'unknown', 'bottom': 'unknown'}
        direction  = dir_kw or edge_dir_map.get(edge, 'unknown')
        dir_factor = {'out': 1.0, 'in': 1.0, 'unknown': 0.4}.get(direction, 0.4)
        if edge in ('top', 'bottom'):
            dir_factor = 0.5

        # ── Line ID & media ───────────────────────────────────────────────────
        lm = _RE_LINE_ID.search(text)
        ref_line_id = lm.group(1) if lm else None

        media_type = 'unknown'
        for mname, pat in _MEDIA_PATTERNS:
            if pat.search(text):
                media_type = mname
                break

        # ── Confidence ────────────────────────────────────────────────────────
        if lkab_format or its_format:
            conf = 0.85
        elif keyword:
            conf = 0.90
        elif gryaab_format:
            conf = 0.70
        else:
            conf = 0.55
        if ref_line_id is None:
            conf -= 0.08
        if ocr_used:
            conf -= 0.10
        if media_type == 'unknown':
            conf -= 0.08
        conf = max(0.10, conf)

        mw = _MEDIA_WEIGHTS.get(media_type, 0.05)
        weight = round(mw * conf * dir_factor, 3)

        import datetime
        return {
            "pid_page": pn,
            "x_pdf": cx, "y_pdf": cy,
            "direction": direction,
            "edge": edge,
            "ref_text": text[:200],
            "ref_sheet": ref_sheet,
            "ref_line_id": ref_line_id,
            "media_type": media_type,
            "weight": weight,
            "confidence": conf,
            "raw_text": text[:500],
            "ocr_used": int(ocr_used),
            "analyzed_at": datetime.datetime.now().isoformat(),
        }

    def _ocr_edges(self, page, pw, ph):
        """OCR all four edges. Returns {edge: text}. Uses 3× scale + preprocessing."""
        if not HAS_PYMUPDF:
            return {}
        strips = {
            'left':   fitz.Rect(0,       0,       pw*0.28, ph),
            'right':  fitz.Rect(pw*0.72, 0,       pw,      ph),
            'top':    fitz.Rect(0,       0,       pw,      ph*0.22),
            'bottom': fitz.Rect(0,       ph*0.78, pw,      ph),
        }
        mat = fitz.Matrix(3.0, 3.0)
        result = {}
        for edge, clip in strips.items():
            try:
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                text = self._ocr_strip(pix)
                if text:
                    result[edge] = text
            except Exception:
                pass
        return result

    def _ocr_strip(self, pix):
        """OCR one pixmap strip. Tries tesseract (multi-PSM + preprocessing) then easyocr."""
        try:
            from PIL import Image as _PILImg, ImageEnhance, ImageFilter
            pil_img = _PILImg.frombytes("RGB", [pix.width, pix.height], pix.samples)
            gray    = pil_img.convert('L')
            gray    = ImageEnhance.Contrast(gray).enhance(2.5)
            gray    = gray.filter(ImageFilter.SHARPEN)

            try:
                import pytesseract
                for psm in (6, 11, 3):
                    text = pytesseract.image_to_string(
                        gray, config=f'--psm {psm} --oem 1').strip()
                    if text:
                        return text
            except Exception:
                pass

            # easyocr fallback (lazy-init reader on first use)
            try:
                import easyocr as _easyocr
                if not hasattr(self, '_easyocr_reader'):
                    self._easyocr_reader = _easyocr.Reader(['en', 'sv'],
                                                           verbose=False)
                hits = self._easyocr_reader.readtext(pil_img,
                                                     detail=0,
                                                     paragraph=True)
                text = ' '.join(hits).strip()
                if text:
                    return text
            except Exception:
                pass
        except Exception:
            pass
        return ''

    def _match_connections(self, connectors, sheet_lookup, page_count,
                          page_sheet_nums=None):
        """Match 'out' connectors to 'in' connectors via ref_sheet."""
        from collections import defaultdict
        import datetime

        # Build own-sheet reverse map: pn → own sheet num (upper)
        own_sheet = {}
        if page_sheet_nums:
            own_sheet = {int(k): v.upper() for k, v in page_sheet_nums.items()}

        out_by_ref = defaultdict(list)  # ref_sheet → list of connectors
        in_by_ref  = defaultdict(list)

        for i, c in enumerate(connectors):
            c['_idx'] = i
            ref = (c.get('ref_sheet') or '').upper().strip()
            if not ref:
                continue
            # Skip self-referential connectors (sheet references its own number)
            if own_sheet.get(c['pid_page'], '__NONE__') == ref:
                continue
            if c['direction'] == 'out':
                out_by_ref[ref].append(c)
            elif c['direction'] == 'in':
                in_by_ref[ref].append(c)
            else:
                # unknown direction — try both buckets
                out_by_ref[ref].append(c)

        connections = []
        seen_pairs = {}  # (from_page, to_page) → connection index for dedup

        def resolve_page(ref):
            if ref in sheet_lookup:
                return sheet_lookup[ref]
            # Fuzzy: try suffix match
            for k, v in sheet_lookup.items():
                if k.endswith(ref) or ref.endswith(k):
                    return v
            return None

        ts = datetime.datetime.now().isoformat()

        for ref, out_list in out_by_ref.items():
            # Find the page this ref points TO
            to_pn = resolve_page(ref)
            for oc in out_list:
                fp = oc['pid_page']
                # Skip self-referential pairs (e.g. recirculation loops on same page)
                if to_pn is not None and fp == to_pn:
                    continue
                is_ghost = 0
                ghost_ref = None
                if to_pn is None:
                    is_ghost = 1
                    ghost_ref = ref
                    tp = None
                else:
                    tp = to_pn

                w = oc['weight']
                conf = oc['confidence']
                key = (fp, tp)

                if key in seen_pairs:
                    idx = seen_pairs[key]
                    # Accumulate weight: w_new = 1 - (1-w_old)*(1-w_new)
                    old_w = connections[idx]['weight']
                    connections[idx]['weight'] = round(1.0 - (1.0 - old_w) * (1.0 - w), 3)
                    connections[idx]['warning'] = 'duplicate'
                else:
                    seen_pairs[key] = len(connections)
                    connections.append({
                        'from_page': fp,
                        'to_page': tp,
                        'from_connector': oc.get('_idx'),
                        'to_connector': None,
                        'media_type': oc['media_type'],
                        'weight': w,
                        'confidence': conf,
                        'is_bidirectional': 0,
                        'is_ghost': is_ghost,
                        'ghost_ref': ghost_ref,
                        'warning': None,
                    })

        # Check for bidirectional pairs
        existing_keys = set(seen_pairs.keys())
        for conn in connections:
            fp, tp = conn['from_page'], conn['to_page']
            if tp is not None and (tp, fp) in existing_keys:
                conn['is_bidirectional'] = 1
                connections[seen_pairs[(tp, fp)]]['is_bidirectional'] = 1

        return connections


def _propose_layout(connections, active_pages, page_widths_pdf, page_heights_pdf, render_scale):
    """Compact column layout for active_pages only — left-to-right, no overlap.

    Improvements over the original:
    1. Barycenter vertical ordering within each column (minimises arc crossings)
    2. Isolated pages (no connections) grouped into a separate rightmost column
    3. Automatic column split when a column exceeds MAX_COL_PAGES
    4. Multiple horizontal compaction passes until convergence
    5. Directed BFS — starts from source pages (no incoming connections)

    Returns {page_idx: (scene_x, scene_y)} for each page in active_pages.
    """
    from collections import deque

    if not active_pages:
        return {}

    rs        = render_scale
    GAP_X     = 700  # horizontal gap between columns (scene px)
    GAP_Y     = 350  # vertical gap between pages in the same column
    MAX_COL   = 8    # split a column if it exceeds this many pages

    page_set = set(active_pages)
    ws = {i: page_widths_pdf.get(i,  800) * rs for i in active_pages}
    hs = {i: page_heights_pdf.get(i, 600) * rs for i in active_pages}

    # ── Build adjacency (active pages only) ───────────────────────────────────
    adj_fwd = {i: set() for i in active_pages}   # directed forward
    adj_bwd = {i: set() for i in active_pages}   # directed backward (incoming)
    for c in connections:
        fp, tp = c.get('from_page'), c.get('to_page')
        if fp not in page_set or tp not in page_set or fp == tp:
            continue
        adj_fwd[fp].add(tp)
        adj_bwd[tp].add(fp)

    # Undirected union used for connectivity and barycenter
    adj_all = {i: adj_fwd[i] | adj_bwd[i] for i in active_pages}

    # ── Improvement 5: directed BFS from source pages ─────────────────────────
    # Sources = pages with no incoming connections (they start flows)
    sources = sorted(i for i in active_pages if not adj_bwd[i] and adj_all[i])
    if not sources:
        sources = [min(active_pages)]   # fallback: smallest index

    level = {}
    for start in sources:
        if start in level:
            continue
        queue = deque([(start, 0)])
        while queue:
            node, lv = queue.popleft()
            if node in level:
                continue
            level[node] = lv
            for nb in sorted(adj_all[node]):
                if nb not in level:
                    queue.append((nb, lv + 1))
    # Assign remaining (isolated or disconnected sub-components)
    for start in sorted(active_pages):
        if start in level:
            continue
        queue = deque([(start, 0)])
        while queue:
            node, lv = queue.popleft()
            if node in level:
                continue
            level[node] = lv
            for nb in sorted(adj_all[node]):
                if nb not in level:
                    queue.append((nb, lv + 1))

    # ── Improvement 2: separate isolated pages ────────────────────────────────
    isolated  = [i for i in active_pages if not adj_all[i]]
    connected = [i for i in active_pages if adj_all[i]]

    level_groups: dict = {}
    for i in connected:
        level_groups.setdefault(level[i], []).append(i)

    # ── Improvement 3: split tall columns ────────────────────────────────────
    expanded: dict = {}
    for lv in sorted(level_groups):
        pages = level_groups[lv]
        if len(pages) <= MAX_COL:
            expanded[float(lv)] = pages
        else:
            for chunk_idx, start in enumerate(range(0, len(pages), MAX_COL)):
                expanded[lv + chunk_idx * 0.01] = pages[start:start + MAX_COL]
    level_groups   = expanded
    sorted_levels  = sorted(level_groups)

    # ── Pass 1: initial column X + stack pages top-to-bottom ─────────────────
    col_x: dict = {}
    x = GAP_X
    for lv in sorted_levels:
        col_x[lv] = x
        col_w = max(ws[i] for i in level_groups[lv])
        x += col_w + GAP_X

    pos: dict = {}
    for lv in sorted_levels:
        y = GAP_Y
        for node in sorted(level_groups[lv]):
            pos[node] = [col_x[lv], y]
            y += hs[node] + GAP_Y

    # ── Improvement 1: barycenter reordering (forward sweep) ─────────────────
    for lv in sorted_levels:
        def _bary(node):
            ny = [pos[nb][1] + hs[nb] / 2
                  for nb in adj_all[node] if nb in pos]
            return sum(ny) / len(ny) if ny else pos[node][1]
        ordered = sorted(level_groups[lv], key=_bary)
        y = GAP_Y
        for node in ordered:
            pos[node] = [col_x[lv], y]
            y += hs[node] + GAP_Y

    # ── Isolated pages: rightmost dedicated column ────────────────────────────
    if isolated:
        if connected:
            iso_x = max(pos[i][0] + ws[i] for i in connected) + GAP_X * 2
        else:
            iso_x = GAP_X
        y = GAP_Y
        for node in sorted(isolated):
            pos[node] = [iso_x, y]
            y += hs[node] + GAP_Y

    # ── Improvement 4: multi-pass horizontal compaction ───────────────────────
    for _ in range(6):
        moved = False
        for idx, lv in enumerate(sorted_levels[1:], 1):
            earlier = [j for prev in sorted_levels[:idx] for j in level_groups[prev]]
            min_x = GAP_X
            for i in level_groups[lv]:
                yi, hi = pos[i][1], hs[i]
                for j in earlier:
                    xj, yj, wj, hj = pos[j][0], pos[j][1], ws[j], hs[j]
                    if yj < yi + hi + GAP_Y and yj + hj + GAP_Y > yi:
                        min_x = max(min_x, xj + wj + GAP_X)
            for i in level_groups[lv]:
                if abs(pos[i][0] - min_x) > 0.5:
                    moved = True
                pos[i][0] = min_x
        if not moved:
            break

    return {i: (round(pos[i][0], 1), round(pos[i][1], 1)) for i in active_pages}


class PIDGraphicsView(QGraphicsView):
    node_markup_finished    = pyqtSignal(list, int)
    # Third parameter = extracted tag text from drawn rectangle (may be empty)
    cause_clicked           = pyqtSignal(object, int, str)
    consequence_clicked     = pyqtSignal(object, int, str)
    safeguard_clicked       = pyqtSignal(object, int, str)
    place_existing_clicked  = pyqtSignal(object, int)
    cause_template_clicked  = pyqtSignal(object, int, str)
    context_action          = pyqtSignal(str, object, int)
    marker_clicked          = pyqtSignal(str, int)
    # Markup editing signals
    markup_draw_finished    = pyqtSignal(str, list, int)   # type_, pdf_pts, page
    markup_item_clicked     = pyqtSignal(int)              # markup_id
    markup_moved            = pyqtSignal(int, list)        # mu_id, new PDF points [[x,y],...]
    markup_label_edited     = pyqtSignal(int, str)         # mu_id, new_label
    markup_duplicate_requested = pyqtSignal(int)           # mu_id
    markup_symbol_dims_changed = pyqtSignal(int, float, float, float)  # mu_id, w, h, rot_deg
    board_layout_changed     = pyqtSignal(str)  # JSON {"0": [ox, oy], ...}
    sheet_conn_break_requested = pyqtSignal(int)          # connection row id
    sheet_conn_add_requested   = pyqtSignal(int, int)     # (from_page, to_page)
    zone_drawn    = pyqtSignal(object, int)                # (QRectF pdf_coords, page)
    zone_resized  = pyqtSignal(str, int, float, float, float, float)  # type_,id_,cx,cy,w,h
    cause_at_marker_requested       = pyqtSignal(int)   # cause_id
    consequence_at_marker_requested = pyqtSignal(int)   # cons_id
    safeguard_at_marker_requested   = pyqtSignal(int)   # sg_id

    # Keys for QGraphicsItem.setData / .data
    _DATA_TYPE      = 0    # 'cause' | 'consequence' | 'safeguard' | 'markup'
    _DATA_ID        = 1    # database id
    _DATA_MARKUP_ID = 2    # markup id (for markup items)
    _DATA_MARKUP_PTS = 3   # stores PDF points list on path/text items
    _DATA_ZONE_KEY  = 4    # (marker_type, marker_id) on zone rect/handle items
    _DATA_ZONE_CIDX = 5    # corner index 0=TL,1=TR,2=BR,3=BL on zone handle items
    _DATA_SYMBOL_W   = 6   # float PDF-unit width stored on symbol items
    _DATA_SYMBOL_H   = 7   # float PDF-unit height stored on symbol items
    _DATA_SYMBOL_ROT = 8   # float rotation degrees stored on symbol items

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        # White background — matches typical P&ID drawing background
        self.setBackgroundBrush(QBrush(QColor(255, 255, 255)))

        # ── GPU-accelerated rendering (OpenGL) ────────────────────────────────
        if HAS_OPENGL:
            gl = QOpenGLWidget()
            self.setViewport(gl)
            # With OpenGL, full-viewport update is required (GPU composites all)
            self.setViewportUpdateMode(
                QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        else:
            # CPU fallback: only repaint changed tiles
            self.setViewportUpdateMode(
                QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)

        # Avoid unnecessary bounding-rect adjustments during pan/zoom
        self.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)
        # Don't save/restore painter state per item — slight speedup
        self.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontSavePainterState, True)

        self._press_pos  = None
        self._rect_start = None
        self._rect_item  = None
        # Per-type marker tracking for visibility toggle
        self._type_items: dict = {'cause': [], 'consequence': [], 'safeguard': []}
        self._type_visible: dict = {'cause': True, 'consequence': True, 'safeguard': True}

        self.mode             = MODE_NAV
        self.pdf_doc          = None
        self.current_page     = 0
        self.page_item        = None
        self.render_scale     = 1.0
        self.page_rect_width  = 0.0
        self.page_rect_height = 0.0
        self._RASTER_SCALE    = 3.0
        self._pdf_path        = None
        self._page_cache: dict = {}
        self._cache_order: list = []
        self._CACHE_SIZE      = 10
        self._prefetch_thread = None

        self.draw_points        = []
        self.draw_pen           = QPen(QColor(255, 140, 0), 3)
        self.draw_pen.setCosmetic(True)
        self.draw_brush         = QBrush(QColor(255, 140, 0, 60))
        self.temp_items         = []
        self.rubber_line        = None

        # Right-drag rubber-band (NAV mode)
        self._rband_start_scene  = None
        self._rband_preview_item = None
        self._rband_dragging     = False

        # Zone rectangle overlays: (type_, id_) → {rect_item, handles:[4]}
        self._zone_rects: dict = {}
        # Label slot tracker: (qx, qy) → next free row index (reset on clear_overlays)
        self._label_slots: dict = {}
        # Zone corner resize state
        self._zone_resize_key   = None   # (type_, id_)
        self._zone_resize_cidx  = None   # 0=TL,1=TR,2=BR,3=BL
        self._zone_resize_start = None   # QPointF scene
        self._zone_resize_orig  = None   # QRectF scene
        self._pending_path_item = None

        # Markup overlay tracking: markup_id → list of QGraphicsItems
        self._markup_items: dict = {}
        self._markup_highlighted: int = -1
        self._snap_enabled: bool = True
        self._markup_types: dict   = {}   # mu_id → 'polygon'|'polyline'|'text'|'comment'
        # Red markup overlay tracking (separate from node markup)
        self._red_markup_items: dict = {}
        self._red_markup_types: dict = {}
        self._edit_mu_id            = None
        self._vertex_handles: list  = []
        self._drag_mode             = None   # 'vertex' | 'item' | 'symbol_transform' | None
        self._drag_vertex_idx       = None
        self._drag_start_scene      = None
        self._drag_original_pts: list = []
        self._drag_current_pts: list  = []
        self._drag_threshold_exceeded = False
        self._drag_item_origins: list = []  # [(QGraphicsItem, QPointF)] for text/comment
        # Symbol resize/rotate handles and live state
        self._corner_handles: list   = []
        self._rot_handle              = None   # QGraphicsEllipseItem
        self._rot_handle_line         = None   # QGraphicsLineItem
        self._symbol_bbox_proxy       = None   # QGraphicsPathItem (live preview)
        self._symbol_drag_mode        = None   # 'nw'|'ne'|'sw'|'se'|'rotate'
        self._symbol_orig_w           = 40.0
        self._symbol_orig_h           = 40.0
        self._symbol_orig_rot         = 0.0
        self._symbol_orig_center      = None   # QPointF scene coords
        self._symbol_live_w           = 40.0
        self._symbol_live_h           = 40.0
        self._symbol_live_rot         = 0.0
        self._inline_edit_widget = None

        self._smart_start_pdf   = None   # (pdf_x, pdf_y) first click
        self._smart_end_pdf     = None
        self._smart_paths       = []     # list of paths (each = [[pdf_x,pdf_y],...])
        self._smart_path_idx    = 0
        self._smart_preview     = []     # QGraphicsItem preview items on scene
        self._smart_tracer      = None   # SmartPipeTracer, cached per page
        self._smart_tracer_page = -1

        # Study board: multi-page layout
        self._all_page_items: dict  = {}   # page_idx → QGraphicsPixmapItem
        self._page_offsets: dict    = {}   # page_idx → (ox: float, oy: float)
        self._page_widths_pdf: dict = {}   # page_idx → float PDF width
        self._page_heights_pdf: dict = {}  # page_idx → float PDF height
        self._dragging_page         = None  # page_idx being dragged in MODE_BOARD_LAYOUT
        self._drag_page_start_scene = None  # QPointF where drag began
        self._drag_page_orig_offset = None  # (ox, oy) before drag
        self._add_link_source_page  = None  # page_idx chosen in MODE_ADD_SHEET_LINK

        self._lod_overview = None   # None = unset; True/False = current LOD

        self._placeholder = None
        self._show_placeholder("Öppna en P&ID-fil (PDF) för att börja.")
        self.set_mode(MODE_NAV)
        self.setBackgroundBrush(QBrush(QColor(160, 160, 160)))

    def _show_placeholder(self, text):
        self._clear_placeholder()
        item = self._scene.addSimpleText(text)
        f = QFont(); f.setPointSize(14)
        item.setFont(f)
        item.setBrush(QBrush(QColor(70, 70, 70)))
        item.setZValue(Z_TEMP)
        self._placeholder = item
        self._scene.setSceneRect(item.boundingRect().adjusted(-40, -40, 40, 40))

    def _clear_placeholder(self):
        if self._placeholder is not None:
            try:
                self._scene.removeItem(self._placeholder)
            except Exception:
                pass
            self._placeholder = None

    def load_pdf(self, path, page=0, layout_offsets=None, active_pages=None,
                 progress_cb=None):
        if not HAS_PYMUPDF:
            self._show_placeholder("Installera PyMuPDF:\n  pip install PyMuPDF")
            return False
        try:
            self.pdf_doc = fitz.open(str(path))
        except Exception as e:
            self._show_placeholder(f"Kunde inte öppna PDF:\n{e}")
            self.pdf_doc = None
            return False
        if self.pdf_doc.page_count == 0:
            self._show_placeholder("PDF saknar sidor.")
            return False
        self._pdf_path = str(path)
        self._page_cache.clear()
        self._cache_order.clear()
        self._cancel_prefetch()
        self.current_page = max(0, min(page, self.pdf_doc.page_count - 1))
        self._render_all_pages(layout_offsets=layout_offsets, active_pages=active_pages,
                               progress_cb=progress_cb)
        return True

    def _render_page(self):
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return
        self._clear_placeholder()
        page = self.pdf_doc.load_page(self.current_page)
        rect = page.rect
        self.page_rect_width  = float(rect.width)
        self.page_rect_height = float(rect.height)

        if self.page_item is not None:
            try:
                self._scene.removeItem(self.page_item)
            except Exception:
                pass
            self.page_item = None

        pn = self.current_page
        if pn in self._page_cache:
            pixmap = self._page_cache[pn]
            self._update_lru(pn)
        else:
            mat = fitz.Matrix(self._RASTER_SCALE, self._RASTER_SCALE)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = QImage(pix.samples, pix.width, pix.height,
                         pix.stride, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(img.copy())
            self._add_to_cache(pn, pixmap)

        self.page_item = QGraphicsPixmapItem(pixmap)
        self.page_item.setZValue(Z_PAGE)
        self.page_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self.page_item)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self.render_scale = self._RASTER_SCALE

        self._prefetch_adjacent()

    def _render_all_pages(self, layout_offsets=None, active_pages=None,
                          progress_cb=None):
        """Render PDF pages (only active_pages if given) as tiled scene items."""
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return
        self._clear_placeholder()
        for item in list(self._all_page_items.values()):
            try: self._scene.removeItem(item)
            except Exception: pass
        self._all_page_items.clear()
        self._page_offsets.clear()
        self._page_widths_pdf.clear()
        self._page_heights_pdf.clear()
        self.render_scale = self._RASTER_SCALE
        GAP = 100.0  # scene pixels between pages

        pages_to_render = (sorted(active_pages)
                           if active_pages is not None
                           else list(range(self.pdf_doc.page_count)))
        total_pages = len(pages_to_render)
        x_cursor = 0.0
        for render_idx, pn in enumerate(pages_to_render):
            fitz_page = self.pdf_doc.load_page(pn)
            rect = fitz_page.rect
            pw_pdf = float(rect.width)
            ph_pdf = float(rect.height)
            pw_scene = pw_pdf * self.render_scale
            self._page_widths_pdf[pn] = pw_pdf
            self._page_heights_pdf[pn] = ph_pdf

            if pn in self._page_cache:
                pixmap = self._page_cache[pn]
                self._update_lru(pn)
            else:
                mat = fitz.Matrix(self._RASTER_SCALE, self._RASTER_SCALE)
                pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
                img = QImage(pix.samples, pix.width, pix.height,
                             pix.stride, QImage.Format.Format_RGB888)
                pixmap = QPixmap.fromImage(img.copy())
                self._add_to_cache(pn, pixmap)

            if layout_offsets and pn in layout_offsets:
                ox, oy = float(layout_offsets[pn][0]), float(layout_offsets[pn][1])
            else:
                ox = x_cursor
                oy = 0.0
            x_cursor = ox + pw_scene + GAP
            self._page_offsets[pn] = (ox, oy)

            page_item = QGraphicsPixmapItem(pixmap)
            page_item.setZValue(Z_PAGE)
            page_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            page_item.setPos(ox, oy)
            self._scene.addItem(page_item)
            self._all_page_items[pn] = page_item

            if progress_cb is not None:
                progress_cb(render_idx + 1, total_pages)
                QApplication.processEvents()

        self.page_item = self._all_page_items.get(self.current_page)
        self.page_rect_width  = self._page_widths_pdf.get(self.current_page, 0.0)
        self.page_rect_height = self._page_heights_pdf.get(self.current_page, 0.0)
        self._update_board_scene_rect()

    def _update_board_scene_rect(self):
        if not self._page_offsets:
            return
        rs = self.render_scale
        min_x = min(ox for ox, oy in self._page_offsets.values()) - 40
        min_y = min(oy for ox, oy in self._page_offsets.values()) - 40
        max_x = max(ox + self._page_widths_pdf[p]  * rs
                    for p, (ox, oy) in self._page_offsets.items()) + 40
        max_y = max(oy + self._page_heights_pdf[p] * rs
                    for p, (ox, oy) in self._page_offsets.items()) + 40
        self._scene.setSceneRect(QRectF(min_x, min_y, max_x - min_x, max_y - min_y))

    def _cancel_prefetch(self):
        if self._prefetch_thread and self._prefetch_thread.isRunning():
            self._prefetch_thread.requestInterruption()
            self._prefetch_thread.wait(300)

    def _add_to_cache(self, pn, pixmap):
        if pn in self._page_cache:
            self._cache_order.remove(pn)
        elif len(self._page_cache) >= self._CACHE_SIZE:
            oldest = self._cache_order.pop(0)
            del self._page_cache[oldest]
        self._page_cache[pn] = pixmap
        self._cache_order.append(pn)

    def _update_lru(self, pn):
        if pn in self._cache_order:
            self._cache_order.remove(pn)
            self._cache_order.append(pn)

    def _prefetch_adjacent(self):
        if not self._pdf_path or self.pdf_doc is None:
            return
        total = self.pdf_doc.page_count
        to_fetch = []
        for offset in (1, -1, 2, -2):
            n = self.current_page + offset
            if 0 <= n < total and n not in self._page_cache:
                to_fetch.append(n)
                if len(to_fetch) >= 2:
                    break
        if not to_fetch:
            return
        self._cancel_prefetch()
        self._prefetch_thread = _PageRenderer(self._pdf_path, to_fetch, self._RASTER_SCALE)
        self._prefetch_thread.page_ready.connect(self._on_page_prefetched)
        self._prefetch_thread.start()

    def _on_page_prefetched(self, pn, raw, width, height, stride):
        if pn not in self._page_cache:
            img = QImage(raw, width, height, stride, QImage.Format.Format_RGB888)
            self._add_to_cache(pn, QPixmap.fromImage(img))

    def page_count(self):
        return self.pdf_doc.page_count if self.pdf_doc else 0

    def goto_page(self, n):
        if self.pdf_doc is None:
            return
        n = max(0, min(n, self.pdf_doc.page_count - 1))
        self.current_page = n
        self.page_item = self._all_page_items.get(n)
        self.page_rect_width  = self._page_widths_pdf.get(n, 0.0)
        self.page_rect_height = self._page_heights_pdf.get(n, 0.0)
        self._cancel_drawing()
        if n in self._page_offsets:
            ox, oy = self._page_offsets[n]
            rs = self.render_scale
            cx = ox + self._page_widths_pdf.get(n, 0.0) * rs / 2
            cy = oy + self._page_heights_pdf.get(n, 0.0) * rs / 2
            self.centerOn(QPointF(cx, cy))
        else:
            self._render_all_pages()

    def scene_to_pdf(self, point):
        p = self._hit_test_page(point)
        ox, oy = self._page_offsets.get(p, (0.0, 0.0))
        rs = self.render_scale
        return ((point.x() - ox) / rs, (point.y() - oy) / rs)

    def pdf_to_scene(self, x, y, page=None):
        p = page if page is not None else self.current_page
        ox, oy = self._page_offsets.get(p, (0.0, 0.0))
        return QPointF(x * self.render_scale + ox, y * self.render_scale + oy)

    def _hit_test_page(self, scene_pt):
        """Return the page index whose rendered area contains scene_pt, or current_page."""
        rs = self.render_scale
        for pn, (ox, oy) in self._page_offsets.items():
            pw = self._page_widths_pdf.get(pn, 0) * rs
            ph = self._page_heights_pdf.get(pn, 0) * rs
            if ox <= scene_pt.x() < ox + pw and oy <= scene_pt.y() < oy + ph:
                return pn
        return self.current_page

    def set_mode(self, mode):
        self.mode = mode
        if mode == MODE_NAV:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE,
                      MODE_MARKUP_TEXT, MODE_MARKUP_COMMENT):
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_MARKUP_SELECT:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._clear_edit_handles()
        elif mode in (MODE_CAUSE, MODE_CONSEQUENCE, MODE_SAFEGUARD,
                      MODE_PLACE_EXISTING, MODE_CAUSE_TEMPLATE):
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_SMART_POLYLINE:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
            self._cancel_smart()
        elif mode == MODE_RED_MARKUP_SYMBOL:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_BOARD_LAYOUT:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        elif mode == MODE_ADD_SHEET_LINK:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        if mode not in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE, MODE_SMART_POLYLINE):
            self._cancel_drawing()
        self.setFocus()

    def set_pen_style(self, color, width, alpha):
        c = QColor(color)
        self.draw_pen = QPen(QColor(c.red(), c.green(), c.blue(), alpha), width)
        self.draw_pen.setCosmetic(True)
        self.draw_brush = QBrush(QColor(c.red(), c.green(), c.blue(), max(30, alpha // 4)))

    def set_snap(self, enabled: bool):
        self._snap_enabled = enabled

    def _add_draw_point(self, sp):
        self.draw_points.append(sp)
        r = max(4.0, self.draw_pen.widthF() * 0.6)
        dot = self._scene.addEllipse(
            sp.x() - r, sp.y() - r, 2 * r, 2 * r,
            QPen(Qt.PenStyle.NoPen), QBrush(self.draw_pen.color())
        )
        dot.setZValue(Z_TEMP)
        self.temp_items.append(dot)
        if len(self.draw_points) >= 2:
            p0  = self.draw_points[-2]
            seg = self._scene.addLine(p0.x(), p0.y(), sp.x(), sp.y(), self.draw_pen)
            seg.setZValue(Z_TEMP)
            self.temp_items.append(seg)

    def _update_rubber_band(self, sp):
        if not self.draw_points:
            return
        last = self.draw_points[-1]
        pen  = QPen(self.draw_pen); pen.setStyle(Qt.PenStyle.DashLine)
        if self.rubber_line is None:
            self.rubber_line = self._scene.addLine(
                last.x(), last.y(), sp.x(), sp.y(), pen)
            self.rubber_line.setZValue(Z_TEMP)
        else:
            self.rubber_line.setPen(pen)
            self.rubber_line.setLine(last.x(), last.y(), sp.x(), sp.y())

    def _finish_drawing(self):
        """Legacy finish for MODE_NODE — creates node boundary polygon."""
        self._finish_markup_drawing()

    def _finish_markup_drawing(self):
        """Finish drawing for MODE_NODE, MARKUP_POLYGON and MARKUP_POLYLINE."""
        if len(self.draw_points) < 2:
            self._cancel_drawing()
            return
        mode = self.mode
        path = QPainterPath()
        path.moveTo(self.draw_points[0])
        for pt in self.draw_points[1:]:
            path.lineTo(pt)
        if mode in (MODE_NODE, MODE_MARKUP_POLYGON):
            path.closeSubpath()

        pdf_points = [list(self.scene_to_pdf(pt)) for pt in self.draw_points]
        self._remove_temp_items()

        item = QGraphicsPathItem(path)
        item.setPen(self.draw_pen)
        item.setBrush(self.draw_brush if mode in (MODE_NODE, MODE_MARKUP_POLYGON)
                      else QBrush(Qt.BrushStyle.NoBrush))
        item.setZValue(Z_OVERLAY)
        self._scene.addItem(item)
        self._pending_path_item = item

        self.draw_points = []
        if mode == MODE_NODE:
            self.node_markup_finished.emit(pdf_points, self.current_page)
        elif mode == MODE_MARKUP_POLYGON:
            self.markup_draw_finished.emit('polygon', pdf_points, self.current_page)
        elif mode == MODE_MARKUP_POLYLINE:
            self.markup_draw_finished.emit('polyline', pdf_points, self.current_page)

    def _cancel_drawing(self):
        self._remove_temp_items()
        self.draw_points = []

    def _remove_temp_items(self):
        for item in self.temp_items:
            try: self._scene.removeItem(item)
            except Exception: pass
        self.temp_items = []
        if self.rubber_line is not None:
            try: self._scene.removeItem(self.rubber_line)
            except Exception: pass
            self.rubber_line = None

    def add_node_overlay(self, node_id, points_pdf, style, label):
        if not points_pdf:
            return
        self._pending_path_item = None
        color = QColor(style.get('color', '#FF8C00'))
        width = int(style.get('width', 3))
        alpha = int(style.get('alpha', 220))
        pen   = QPen(QColor(color.red(), color.green(), color.blue(), alpha), width)
        pen.setCosmetic(True)
        brush = QBrush(QColor(color.red(), color.green(), color.blue(), max(25, alpha // 5)))

        path = QPainterPath()
        first = self.pdf_to_scene(*points_pdf[0])
        path.moveTo(first)
        for x, y in points_pdf[1:]:
            path.lineTo(self.pdf_to_scene(x, y))
        path.closeSubpath()

        item = QGraphicsPathItem(path)
        item.setPen(pen); item.setBrush(brush); item.setZValue(Z_OVERLAY)
        item.setToolTip(label or '')
        self._scene.addItem(item)

        if label:
            cx = sum(p[0] for p in points_pdf) / len(points_pdf)
            cy = sum(p[1] for p in points_pdf) / len(points_pdf)
            center = self.pdf_to_scene(cx, cy)
            txt = QGraphicsSimpleTextItem(label)
            f = QFont(); f.setBold(True); f.setPointSize(11)
            txt.setFont(f)
            txt.setBrush(QBrush(QColor(30, 30, 30)))
            br = txt.boundingRect()
            txt.setPos(center.x() - br.width() / 2, center.y() - br.height() / 2)
            txt.setZValue(Z_OVERLAY + 1)
            self._scene.addItem(txt)

    # ── Node markup overlays ──────────────────────────────────────────────────

    def add_markup_overlay(self, mu_id, type_, points_pdf, label,
                           color_hex, opacity, line_width, visible=True, font_size=12,
                           opaque_fill=False, symbol_svg=None,
                           symbol_w=40, symbol_h=40, symbol_rot=0,
                           _items_dict=None):
        """Render a node_markup or red_markup item.  type_: polygon|polyline|text|comment|symbol"""
        if _items_dict is None:
            _items_dict = self._markup_items
        items = []
        c = QColor(color_hex)
        border_alpha = int(opacity * 210)
        fill_alpha   = int(opacity * 210) if opaque_fill else int(opacity * 52)
        # Non-cosmetic pen: width in scene coords so lines scale proportionally with zoom
        pen = QPen(QColor(c.red(), c.green(), c.blue(), border_alpha), line_width)

        if type_ in ('polygon', 'polyline') and len(points_pdf) >= 2:
            path = QPainterPath()
            first = self.pdf_to_scene(*points_pdf[0])
            path.moveTo(first)
            for p in points_pdf[1:]:
                path.lineTo(self.pdf_to_scene(*p))
            if type_ == 'polygon':
                path.closeSubpath()
                brush = QBrush(QColor(c.red(), c.green(), c.blue(), fill_alpha))
            else:
                brush = QBrush(Qt.BrushStyle.NoBrush)
            gi = QGraphicsPathItem(path)
            gi.setPen(pen); gi.setBrush(brush)
            gi.setZValue(Z_OVERLAY)
            gi.setData(self._DATA_MARKUP_ID, mu_id)
            gi.setData(self._DATA_TYPE, 'markup')
            gi.setData(self._DATA_MARKUP_PTS, points_pdf)
            gi.setCursor(Qt.CursorShape.PointingHandCursor)
            gi.setToolTip(f"Klicka för att markera  [{type_}]" + (f": {label}" if label else ""))
            self._scene.addItem(gi)
            items.append(gi)
            if label and type_ == 'polygon':
                cx = sum(p[0] for p in points_pdf) / len(points_pdf)
                cy = sum(p[1] for p in points_pdf) / len(points_pdf)
                items.extend(self._add_markup_label(mu_id, label, cx, cy, c, border_alpha,
                                                    font_size))

        elif type_ in ('text', 'comment') and len(points_pdf) >= 1:
            px, py = points_pdf[0]
            items.extend(self._add_markup_text_item(mu_id, type_, label or '?',
                                                     px, py, c, opacity, line_width, font_size))
            if items:
                items[0].setData(self._DATA_MARKUP_PTS, [[points_pdf[0][0], points_pdf[0][1]]])

        elif type_ == 'symbol' and symbol_svg and len(points_pdf) >= 1:
            items.extend(self._add_markup_symbol_item(
                mu_id, symbol_svg, points_pdf[0], c, opacity,
                symbol_w, symbol_h, symbol_rot, label=label))
            if items:
                items[0].setData(self._DATA_MARKUP_PTS, [[points_pdf[0][0], points_pdf[0][1]]])

        _items_dict[mu_id] = items
        if _items_dict is self._markup_items:
            self._markup_types[mu_id] = type_
        else:
            self._red_markup_types[mu_id] = type_
        if not visible:
            for gi in items:
                gi.setVisible(False)

    def _add_markup_label(self, mu_id, label, cx_pdf, cy_pdf, color, alpha, font_size=12):
        center = self.pdf_to_scene(cx_pdf, cy_pdf)
        txt = QGraphicsSimpleTextItem(label)
        f = QFont(); f.setBold(True); f.setPointSize(font_size)
        txt.setFont(f)
        txt.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(),
                                   min(255, alpha + 30))))
        br = txt.boundingRect()
        txt.setPos(center.x() - br.width() / 2, center.y() - br.height() / 2)
        txt.setZValue(Z_OVERLAY + 1)
        txt.setData(self._DATA_MARKUP_ID, mu_id)
        txt.setData(self._DATA_TYPE, 'markup')
        self._scene.addItem(txt)
        return [txt]

    def _add_markup_text_item(self, mu_id, type_, label, px_pdf, py_pdf,
                              color, opacity, line_width, font_size=12):
        pos = self.pdf_to_scene(px_pdf, py_pdf)
        txt = QGraphicsSimpleTextItem(label)
        f = QFont()
        if type_ == 'comment':
            f.setItalic(True)
        f.setPointSize(font_size)
        txt.setFont(f)
        txt.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(),
                                   int(opacity * 230))))
        br = txt.boundingRect()
        txt.setPos(pos.x(), pos.y())
        txt.setZValue(Z_OVERLAY + 1)
        txt.setData(self._DATA_MARKUP_ID, mu_id)
        txt.setData(self._DATA_TYPE, 'markup')
        items = []
        if type_ == 'comment':
            # Draw a slightly rounded rect behind the text
            pad = 5
            bg_alpha = int(opacity * 90)
            border_alpha = int(opacity * 200)
            bg = QGraphicsRectItem(pos.x() - pad, pos.y() - pad,
                                   br.width() + 2*pad, br.height() + 2*pad)
            bg.setPen(QPen(QColor(color.red(), color.green(), color.blue(), border_alpha),
                           max(1, line_width - 1)))
            bg.setBrush(QBrush(QColor(255, 255, 200, bg_alpha)))
            bg.setZValue(Z_OVERLAY)
            bg.setData(self._DATA_MARKUP_ID, mu_id)
            bg.setData(self._DATA_TYPE, 'markup')
            bg.setCursor(Qt.CursorShape.PointingHandCursor)
            self._scene.addItem(bg)
            items.append(bg)
        self._scene.addItem(txt)
        items.append(txt)
        return items

    def _snap_to_nearest(self, scene_pos):
        """Return nearest existing markup path point within snap threshold, else original pos."""
        if not self._snap_enabled:
            return scene_pos
        SNAP_PX = 18.0
        best_dist = SNAP_PX
        best_pos = scene_pos
        for mu_id, items in self._markup_items.items():
            for gi in items:
                if not isinstance(gi, QGraphicsPathItem):
                    continue
                path = gi.path()
                for i in range(path.elementCount()):
                    el = path.elementAt(i)
                    pt = QPointF(el.x, el.y)
                    dx = pt.x() - scene_pos.x()
                    dy = pt.y() - scene_pos.y()
                    dist = (dx * dx + dy * dy) ** 0.5
                    if dist < best_dist:
                        best_dist = dist
                        best_pos = pt
        # Also snap to in-progress draw points
        for pt in self.draw_points:
            dx = pt.x() - scene_pos.x()
            dy = pt.y() - scene_pos.y()
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_pos = pt
        return best_pos

    def clear_markup_overlays(self):
        """Remove all node markup overlay items from the scene."""
        self._clear_edit_handles()
        self._markup_types.clear()
        for mu_id, items in self._markup_items.items():
            for gi in items:
                try: self._scene.removeItem(gi)
                except Exception: pass
        self._markup_items.clear()
        self._markup_highlighted = -1

    def set_markup_item_visible(self, mu_id, visible):
        for gi in self._markup_items.get(mu_id, []):
            try: gi.setVisible(visible)
            except Exception: pass

    # ── Red markup overlay methods ────────────────────────────────────────────

    def add_red_markup_overlay(self, mu_id, type_, points_pdf, label,
                               color_hex, opacity, line_width, visible=True, font_size=12,
                               symbol_w=40, symbol_h=40, symbol_rot=0):
        """Render a red markup item (same as node markup but fully filled and supports symbols)."""
        svg = _get_red_symbol_svg(label) if type_ == 'symbol' else None
        self.add_markup_overlay(
            mu_id, type_, points_pdf, label, color_hex, opacity, line_width,
            visible, font_size, opaque_fill=True, symbol_svg=svg,
            symbol_w=symbol_w, symbol_h=symbol_h, symbol_rot=symbol_rot,
            _items_dict=self._red_markup_items)

    def clear_red_markup_overlays(self):
        """Remove all red markup overlay items from the scene."""
        self._clear_edit_handles()
        self._red_markup_types.clear()
        for mu_id, items in self._red_markup_items.items():
            for gi in items:
                try: self._scene.removeItem(gi)
                except Exception: pass
        self._red_markup_items.clear()

    def set_red_markup_item_visible(self, mu_id, visible):
        for gi in self._red_markup_items.get(mu_id, []):
            try: gi.setVisible(visible)
            except Exception: pass

    def _add_markup_symbol_item(self, mu_id, svg_str, pos_pdf, color, opacity,
                                symbol_w=40, symbol_h=40, symbol_rot=0, label=''):
        """Render an SVG symbol at pos_pdf with given PDF-unit size and rotation."""
        if not HAS_SVG_RENDERER or QSvgRenderer is None:
            return []
        # Replace placeholder color in SVG with user's chosen color
        colored_svg = svg_str.replace('"red"', f'"{color.name()}"')
        colored_svg = colored_svg.replace("'red'", f"'{color.name()}'")
        renderer = QSvgRenderer()
        renderer.load(colored_svg.encode('utf-8'))
        if not renderer.isValid():
            return []
        # Compute scene coords and size
        scene_pos = self.pdf_to_scene(*pos_pdf)
        scene_pt2 = self.pdf_to_scene(pos_pdf[0] + symbol_w, pos_pdf[1] + symbol_h)
        sw = abs(scene_pt2.x() - scene_pos.x())
        sh = abs(scene_pt2.y() - scene_pos.y())
        pm = QPixmap(max(1, int(sw * 2)), max(1, int(sh * 2)))
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha_val = int(opacity * 255)
        painter.setOpacity(alpha_val / 255.0)
        renderer.render(painter)
        painter.end()
        gi = QGraphicsPixmapItem(pm)
        gi.setTransformOriginPoint(sw, sh)  # center
        gi.setRotation(symbol_rot)
        gi.setPos(scene_pos.x() - sw / 2, scene_pos.y() - sh / 2)
        gi.setScale(0.5)  # we rendered at 2× for crispness
        gi.setZValue(Z_OVERLAY)
        gi.setData(self._DATA_MARKUP_ID, mu_id)
        gi.setData(self._DATA_TYPE, 'red_markup')
        gi.setData(self._DATA_MARKUP_PTS, [[pos_pdf[0], pos_pdf[1]]])
        gi.setData(self._DATA_SYMBOL_W, float(symbol_w))
        gi.setData(self._DATA_SYMBOL_H, float(symbol_h))
        gi.setData(self._DATA_SYMBOL_ROT, float(symbol_rot))
        gi.setCursor(Qt.CursorShape.PointingHandCursor)
        gi.setToolTip(f"Symbol: {label}" if label else "P&ID-symbol")
        self._scene.addItem(gi)
        return [gi]

    def _clear_edit_handles(self):
        for h in self._vertex_handles:
            try: self._scene.removeItem(h)
            except Exception: pass
        for h in self._corner_handles:
            try: self._scene.removeItem(h)
            except Exception: pass
        if self._rot_handle is not None:
            try: self._scene.removeItem(self._rot_handle)
            except Exception: pass
            self._rot_handle = None
        if self._rot_handle_line is not None:
            try: self._scene.removeItem(self._rot_handle_line)
            except Exception: pass
            self._rot_handle_line = None
        if self._symbol_bbox_proxy is not None:
            try: self._scene.removeItem(self._symbol_bbox_proxy)
            except Exception: pass
            self._symbol_bbox_proxy = None
        self._corner_handles          = []
        self._vertex_handles          = []
        self._edit_mu_id              = None
        self._drag_mode               = None
        self._drag_vertex_idx         = None
        self._drag_original_pts       = []
        self._drag_current_pts        = []
        self._drag_item_origins       = []
        self._drag_threshold_exceeded = False
        self._symbol_drag_mode        = None

    def _select_for_edit(self, mu_id):
        """Select a markup item and show vertex handles."""
        self._clear_edit_handles()
        self._edit_mu_id = mu_id

        # Look in both node markup and red markup dicts
        items_dict = (self._markup_items if mu_id in self._markup_items
                      else self._red_markup_items)
        types_dict = (self._markup_types if mu_id in self._markup_types
                      else self._red_markup_types)

        pts_pdf = None
        for gi in items_dict.get(mu_id, []):
            pts_pdf = gi.data(self._DATA_MARKUP_PTS)
            if pts_pdf:
                break
        if not pts_pdf:
            return

        pts_scene = [self.pdf_to_scene(*p) for p in pts_pdf]
        self._drag_current_pts = list(pts_scene)

        typ = types_dict.get(mu_id, 'polygon')

        if typ in ('polygon', 'polyline'):
            HANDLE_R = 5
            for pt in pts_scene:
                h = QGraphicsEllipseItem(-HANDLE_R, -HANDLE_R, HANDLE_R * 2, HANDLE_R * 2)
                h.setBrush(QBrush(QColor(255, 255, 255, 220)))
                h.setPen(QPen(QColor(30, 120, 230), 1.5))
                h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                h.setPos(pt)
                h.setZValue(Z_OVERLAY + 5)
                self._scene.addItem(h)
                self._vertex_handles.append(h)
        elif typ in ('text', 'comment'):
            self._drag_item_origins = [
                (gi, QPointF(gi.pos()))
                for gi in items_dict.get(mu_id, [])
            ]
        elif typ == 'symbol':
            sw_pdf = sh_pdf = rot_deg = None
            for gi in items_dict.get(mu_id, []):
                sw_pdf  = gi.data(self._DATA_SYMBOL_W)
                sh_pdf  = gi.data(self._DATA_SYMBOL_H)
                rot_deg = gi.data(self._DATA_SYMBOL_ROT)
                break
            sw_pdf  = float(sw_pdf  if sw_pdf  is not None else 40.0)
            sh_pdf  = float(sh_pdf  if sh_pdf  is not None else 40.0)
            rot_deg = float(rot_deg if rot_deg is not None else 0.0)
            cx, cy = pts_scene[0].x(), pts_scene[0].y()
            self._symbol_orig_w      = sw_pdf
            self._symbol_orig_h      = sh_pdf
            self._symbol_orig_rot    = rot_deg
            self._symbol_orig_center = QPointF(cx, cy)
            self._symbol_live_w      = sw_pdf
            self._symbol_live_h      = sh_pdf
            self._symbol_live_rot    = rot_deg
            self._add_symbol_handles(cx, cy, sw_pdf, sh_pdf, rot_deg)
            self._drag_item_origins = [
                (gi, QPointF(gi.pos()))
                for gi in items_dict.get(mu_id, [])
            ]

        self.highlight_markup(mu_id)

    # ── Symbol resize/rotate helpers ─────────────────────────────────────────

    def _sym_rpt(self, cx, cy, lx, ly, rot_deg):
        """Rotate local (lx,ly) by rot_deg around (cx,cy) in screen coords."""
        a = math.radians(rot_deg)
        ca, sa = math.cos(a), math.sin(a)
        return QPointF(cx + lx * ca - ly * sa, cy + lx * sa + ly * ca)

    def _add_symbol_handles(self, cx, cy, sw_pdf, sh_pdf, rot_deg):
        """Create resize corner handles and rotation handle for a symbol."""
        rs = self.render_scale
        sw = sw_pdf * rs
        sh = sh_pdf * rs
        SZ = 5
        ROT_DIST = max(22.0, sh / 2 + 18.0)
        corners_local = [(-sw/2, -sh/2), (sw/2, -sh/2), (-sw/2, sh/2), (sw/2, sh/2)]
        for lx, ly in corners_local:
            pt = self._sym_rpt(cx, cy, lx, ly, rot_deg)
            h = QGraphicsRectItem(-SZ, -SZ, SZ * 2, SZ * 2)
            h.setBrush(QBrush(QColor(255, 140, 0, 220)))
            h.setPen(QPen(QColor(180, 80, 0), 1.5))
            h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            h.setPos(pt)
            h.setZValue(Z_OVERLAY + 6)
            self._scene.addItem(h)
            self._corner_handles.append(h)
        rot_pt = self._sym_rpt(cx, cy, 0, -ROT_DIST, rot_deg)
        line = QGraphicsLineItem(cx, cy, rot_pt.x(), rot_pt.y())
        line.setPen(QPen(QColor(100, 80, 200, 160), 1.5, Qt.PenStyle.DashLine))
        line.setZValue(Z_OVERLAY + 5)
        self._scene.addItem(line)
        self._rot_handle_line = line
        ROT_R = 6
        rh = QGraphicsEllipseItem(-ROT_R, -ROT_R, ROT_R * 2, ROT_R * 2)
        rh.setBrush(QBrush(QColor(100, 80, 200, 200)))
        rh.setPen(QPen(QColor(60, 40, 180), 1.5))
        rh.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        rh.setPos(rot_pt)
        rh.setZValue(Z_OVERLAY + 6)
        self._scene.addItem(rh)
        self._rot_handle = rh

    def _update_symbol_handles(self, cx, cy, sw_pdf, sh_pdf, rot_deg):
        """Reposition corner handles and rotation handle to match new symbol state."""
        rs = self.render_scale
        sw = sw_pdf * rs
        sh = sh_pdf * rs
        ROT_DIST = max(22.0, sh / 2 + 18.0)
        corners_local = [(-sw/2, -sh/2), (sw/2, -sh/2), (-sw/2, sh/2), (sw/2, sh/2)]
        for i, (lx, ly) in enumerate(corners_local):
            if i < len(self._corner_handles):
                self._corner_handles[i].setPos(self._sym_rpt(cx, cy, lx, ly, rot_deg))
        rot_pt = self._sym_rpt(cx, cy, 0, -ROT_DIST, rot_deg)
        if self._rot_handle is not None:
            self._rot_handle.setPos(rot_pt)
        if self._rot_handle_line is not None:
            self._rot_handle_line.setLine(cx, cy, rot_pt.x(), rot_pt.y())

    def _update_symbol_bbox_proxy(self, cx, cy, sw_pdf, sh_pdf, rot_deg):
        """Show/update the dashed bounding-box preview during resize/rotate."""
        rs = self.render_scale
        sw = sw_pdf * rs
        sh = sh_pdf * rs
        corners = [
            self._sym_rpt(cx, cy, -sw/2, -sh/2, rot_deg),
            self._sym_rpt(cx, cy,  sw/2, -sh/2, rot_deg),
            self._sym_rpt(cx, cy,  sw/2,  sh/2, rot_deg),
            self._sym_rpt(cx, cy, -sw/2,  sh/2, rot_deg),
        ]
        path = QPainterPath()
        path.moveTo(corners[0])
        for p in corners[1:]:
            path.lineTo(p)
        path.closeSubpath()
        if self._symbol_bbox_proxy is None:
            proxy = QGraphicsPathItem(path)
            proxy.setPen(QPen(QColor(255, 140, 0, 200), 2.0, Qt.PenStyle.DashLine))
            proxy.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            proxy.setZValue(Z_OVERLAY + 4)
            self._scene.addItem(proxy)
            self._symbol_bbox_proxy = proxy
        else:
            self._symbol_bbox_proxy.setPath(path)

    def _do_symbol_transform(self, sp):
        """Apply live resize or rotate of symbol during handle drag."""
        cx = self._symbol_orig_center.x()
        cy = self._symbol_orig_center.y()
        dx = sp.x() - cx
        dy = sp.y() - cy
        rs = self.render_scale
        if self._symbol_drag_mode == 'rotate':
            self._symbol_live_rot = math.degrees(math.atan2(dy, dx)) + 90.0
        else:
            a = math.radians(self._symbol_live_rot)
            ca, sa = math.cos(a), math.sin(a)
            lx =  dx * ca + dy * sa   # inverse rotation
            ly = -dx * sa + dy * ca
            min_pdf = 10.0
            if self._symbol_drag_mode == 'se':
                self._symbol_live_w = max(min_pdf, lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, ly * 2 / rs)
            elif self._symbol_drag_mode == 'nw':
                self._symbol_live_w = max(min_pdf, -lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, -ly * 2 / rs)
            elif self._symbol_drag_mode == 'ne':
                self._symbol_live_w = max(min_pdf, lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, -ly * 2 / rs)
            elif self._symbol_drag_mode == 'sw':
                self._symbol_live_w = max(min_pdf, -lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, ly * 2 / rs)
        self._update_symbol_bbox_proxy(
            cx, cy, self._symbol_live_w, self._symbol_live_h, self._symbol_live_rot)
        self._update_symbol_handles(
            cx, cy, self._symbol_live_w, self._symbol_live_h, self._symbol_live_rot)

    def _finish_symbol_transform(self):
        """Save new symbol dims/rotation, emit signal for DB save."""
        mu_id = self._edit_mu_id
        if mu_id is None:
            return
        # Update stored data on item so it survives until the next render
        items_dict = (self._red_markup_items if mu_id in self._red_markup_items
                      else self._markup_items)
        for gi in items_dict.get(mu_id, []):
            gi.setData(self._DATA_SYMBOL_W,   self._symbol_live_w)
            gi.setData(self._DATA_SYMBOL_H,   self._symbol_live_h)
            gi.setData(self._DATA_SYMBOL_ROT, self._symbol_live_rot)
        self.markup_symbol_dims_changed.emit(
            mu_id,
            float(self._symbol_live_w),
            float(self._symbol_live_h),
            float(self._symbol_live_rot))
        self._clear_edit_handles()

    # ── Edit path / drag helpers ──────────────────────────────────────────────

    def _update_edit_path(self, pts_scene):
        """Rebuild the path/positions of the currently edited markup."""
        mu_id = self._edit_mu_id
        if mu_id not in self._markup_items and mu_id not in self._red_markup_items:
            return
        items_dict = (self._markup_items if mu_id in self._markup_items
                      else self._red_markup_items)
        types_dict = (self._markup_types if mu_id in self._markup_types
                      else self._red_markup_types)
        typ = types_dict.get(mu_id, 'polygon')

        if typ in ('polygon', 'polyline') and pts_scene:
            for gi in items_dict[mu_id]:
                if isinstance(gi, QGraphicsPathItem):
                    path = QPainterPath()
                    path.moveTo(pts_scene[0])
                    for pt in pts_scene[1:]:
                        path.lineTo(pt)
                    if typ == 'polygon':
                        path.closeSubpath()
                    gi.setPath(path)
                    break

        elif typ in ('text', 'comment') and pts_scene and self._drag_original_pts:
            delta = pts_scene[0] - self._drag_original_pts[0]
            for gi, orig in self._drag_item_origins:
                gi.setPos(orig + delta)

        elif typ == 'symbol' and pts_scene and self._drag_original_pts:
            delta = pts_scene[0] - self._drag_original_pts[0]
            for gi, orig in self._drag_item_origins:
                gi.setPos(orig + delta)
            # Also move the resize/rotate handles to follow the symbol
            self._update_symbol_handles(
                pts_scene[0].x(), pts_scene[0].y(),
                self._symbol_live_w, self._symbol_live_h, self._symbol_live_rot)

    def _update_handle_positions(self, pts_scene):
        for i, handle in enumerate(self._vertex_handles):
            if i < len(pts_scene):
                handle.setPos(pts_scene[i])

    def _finish_edit_drag(self):
        """Called on mouseRelease after a drag — save new points."""
        if self._edit_mu_id is None:
            return
        mu_id = self._edit_mu_id
        is_red = mu_id in self._red_markup_items
        items_dict = self._red_markup_items if is_red else self._markup_items
        types_dict = self._red_markup_types if is_red else self._markup_types
        new_pdf_pts = [list(self.scene_to_pdf(pt)) for pt in self._drag_current_pts]
        for gi in items_dict.get(mu_id, []):
            if gi.data(self._DATA_MARKUP_PTS) is not None:
                gi.setData(self._DATA_MARKUP_PTS, new_pdf_pts)
                break
        # For symbols moved via 'item' drag, keep _symbol_orig_center in sync
        if types_dict.get(mu_id) == 'symbol' and new_pdf_pts:
            self._symbol_orig_center = self.pdf_to_scene(*new_pdf_pts[0])
        self.markup_moved.emit(mu_id, new_pdf_pts)

    def _start_inline_label_edit(self, mu_id):
        """Show a floating QLineEdit over the text item for in-place label editing."""
        txt_item = None
        for gi in self._markup_items.get(mu_id, []):
            if isinstance(gi, QGraphicsSimpleTextItem):
                txt_item = gi
                break
        if txt_item is None:
            return
        if self._inline_edit_widget is not None:
            self._inline_edit_widget.deleteLater()
            self._inline_edit_widget = None

        vp = self.mapFromScene(txt_item.pos())
        br = txt_item.boundingRect()
        edit = QLineEdit(self.viewport())
        edit.setFont(txt_item.font())
        edit.setText(txt_item.text())
        edit.move(vp.x(), vp.y())
        edit.resize(max(160, int(br.width()) + 20), int(br.height()) + 6)
        edit.selectAll()
        edit.setStyleSheet(
            "background:white;border:2px solid #1e78e6;border-radius:2px;padding:1px;")
        edit.show()
        edit.setFocus()
        self._inline_edit_widget = edit
        committed = [False]

        def commit():
            if committed[0]:
                return
            committed[0] = True
            new_text = edit.text().strip() or edit.text()
            if new_text:
                self.markup_label_edited.emit(mu_id, new_text)
            edit.deleteLater()
            self._inline_edit_widget = None

        edit.returnPressed.connect(commit)
        edit.editingFinished.connect(commit)

    # ---------------------------------------------------------------- smart polyline

    def _clear_smart_preview(self):
        for gi in self._smart_preview:
            try: self._scene.removeItem(gi)
            except Exception: pass
        self._smart_preview.clear()

    def _draw_smart_marker(self, scene_pos, role):
        """Draw start (green) or end (red) marker dot at scene_pos."""
        color  = QColor('#4CAF50') if role == 'start' else QColor('#F44336')
        r      = 6
        dot    = QGraphicsEllipseItem(-r, -r, r * 2, r * 2)
        dot.setBrush(QBrush(color))
        dot.setPen(QPen(color.darker(130), 1.5))
        dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        dot.setPos(scene_pos)
        dot.setZValue(Z_OVERLAY + 8)
        self._scene.addItem(dot)
        self._smart_preview.append(dot)

    def _run_smart_trace(self):
        """Run SmartPipeTracer between the two clicked PDF points."""
        if self._smart_start_pdf is None or self._smart_end_pdf is None:
            return
        # Re-use cached tracer if same page
        if self._smart_tracer is None or self._smart_tracer_page != self.current_page:
            self.setCursor(Qt.CursorShape.WaitCursor)
            QApplication.processEvents()
            self._smart_tracer      = SmartPipeTracer(self.pdf_doc, self.current_page)
            self._smart_tracer_page = self.current_page
        self.setCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            paths = self._smart_tracer.trace(self._smart_start_pdf, self._smart_end_pdf,
                                              n_alt=2)
        finally:
            self.setCursor(Qt.CursorShape.CrossCursor)

        if not paths:
            # No path found — inform user via a temporary scene text
            sp = self.pdf_to_scene(*self._smart_end_pdf)
            msg = QGraphicsSimpleTextItem("Ingen väg hittades – prova igen")
            f   = QFont(); f.setPointSize(9)
            msg.setFont(f)
            msg.setBrush(QBrush(QColor('#F44336')))
            msg.setPos(sp.x() + 10, sp.y() - 20)
            msg.setZValue(Z_OVERLAY + 9)
            self._scene.addItem(msg)
            self._smart_preview.append(msg)
            self._smart_start_pdf = None   # reset for new attempt
            return

        self._smart_paths    = paths
        self._smart_path_idx = 0
        self._show_smart_path(0)

    def _show_smart_path(self, idx):
        """Remove old path preview items and draw path[idx] as a dashed line."""
        # Remove only path items (not the start/end dot markers — keep first 2 items)
        markers = self._smart_preview[:2]
        for gi in self._smart_preview[2:]:
            try: self._scene.removeItem(gi)
            except Exception: pass
        self._smart_preview = markers

        if not self._smart_paths or idx >= len(self._smart_paths):
            return

        path_pdf   = self._smart_paths[idx]
        path_scene = [self.pdf_to_scene(*pt) for pt in path_pdf]

        qpath = QPainterPath()
        if path_scene:
            qpath.moveTo(path_scene[0])
            for pt in path_scene[1:]:
                qpath.lineTo(pt)

        # Use current draw_pen colour but dashed for preview
        preview_pen = QPen(self.draw_pen)
        preview_pen.setStyle(Qt.PenStyle.DashLine)
        preview_pen.setCosmetic(True)
        path_item = self._scene.addPath(qpath, preview_pen)
        path_item.setZValue(Z_OVERLAY + 7)
        self._smart_preview.append(path_item)

        # Navigation hint text near end point
        if len(self._smart_paths) > 1:
            ep  = path_scene[-1]
            lbl = f"Väg {idx + 1}/{len(self._smart_paths)}  ←  →  Enter=spara"
            txt = QGraphicsSimpleTextItem(lbl)
            f   = QFont(); f.setPointSize(8)
            txt.setFont(f)
            txt.setBrush(QBrush(QColor('#1565C0')))
            bg_rect = txt.boundingRect()
            bg = QGraphicsRectItem(ep.x() + 8, ep.y() - 18,
                                    bg_rect.width() + 6, bg_rect.height() + 2)
            bg.setBrush(QBrush(QColor(255, 255, 255, 200)))
            bg.setPen(QPen(Qt.PenStyle.NoPen))
            bg.setZValue(Z_OVERLAY + 8)
            txt.setPos(ep.x() + 11, ep.y() - 17)
            txt.setZValue(Z_OVERLAY + 9)
            self._scene.addItem(bg)
            self._scene.addItem(txt)
            self._smart_preview.extend([bg, txt])

    def _confirm_smart(self):
        """Accept current path and emit it as a polyline markup."""
        if not self._smart_paths or self._smart_path_idx >= len(self._smart_paths):
            return
        pts = self._smart_paths[self._smart_path_idx]
        self._clear_smart_preview()
        self._smart_start_pdf = None
        self._smart_end_pdf   = None
        self._smart_paths     = []
        self.markup_draw_finished.emit('polyline', pts, self.current_page)

    def _cancel_smart(self):
        """Cancel the smart trace — reset state and clear preview."""
        self._clear_smart_preview()
        self._smart_start_pdf = None
        self._smart_end_pdf   = None
        self._smart_paths     = []
        self._smart_path_idx  = 0

    def zoom_to_markup_items(self, mu_ids):
        """Zoom and pan the view to fit all given markup items."""
        combined = QRectF()
        for mu_id in mu_ids:
            for gi in self._markup_items.get(mu_id, []):
                br = gi.mapToScene(gi.boundingRect()).boundingRect()
                if combined.isNull():
                    combined = br
                else:
                    combined = combined.united(br)
        if not combined.isNull():
            combined.adjust(-60, -60, 60, 60)
            self.fitInView(combined, Qt.AspectRatioMode.KeepAspectRatio)

    def contextMenuEvent(self, event):
        if self.mode == MODE_MARKUP_SELECT and self._edit_mu_id is not None:
            sp = self.mapToScene(event.pos())
            for gi in self._scene.items(sp):
                if gi.data(self._DATA_MARKUP_ID) is not None:
                    menu = QMenu(self)
                    menu.addAction("📋 Duplicera",
                                   lambda: self.markup_duplicate_requested.emit(self._edit_mu_id))
                    menu.exec(event.globalPos())
                    event.accept()
                    return
        super().contextMenuEvent(event)

    def highlight_markup(self, mu_id):
        """Briefly pulse-highlight a markup item (thicken its border)."""
        if self._markup_highlighted == mu_id:
            return
        # Reset previous
        if self._markup_highlighted in self._markup_items:
            for gi in self._markup_items[self._markup_highlighted]:
                if isinstance(gi, (QGraphicsPathItem, QGraphicsRectItem)):
                    p = gi.pen(); p.setWidthF(max(1, p.widthF() - 2)); gi.setPen(p)
        self._markup_highlighted = mu_id
        if mu_id in self._markup_items:
            for gi in self._markup_items[mu_id]:
                if isinstance(gi, (QGraphicsPathItem, QGraphicsRectItem)):
                    p = gi.pen(); p.setWidthF(p.widthF() + 2); gi.setPen(p)

    def _add_tracked(self, item, marker_type: str):
        """Add item to scene and track it for visibility toggling."""
        self._scene.addItem(item)
        self._type_items.setdefault(marker_type, []).append(item)
        if not self._type_visible.get(marker_type, True):
            item.setVisible(False)

    def set_marker_visibility(self, marker_type: str, visible: bool):
        """Show or hide all markers of a given type."""
        self._type_visible[marker_type] = visible
        for item in self._type_items.get(marker_type, []):
            try:
                item.setVisible(visible)
            except Exception:
                pass

    # ── Zone rectangle overlays ───────────────────────────────────────────────

    def add_zone_rect(self, marker_type, marker_id, cx_pdf, cy_pdf, w_pdf, h_pdf):
        """Draw a transparent green rectangle centered at (cx_pdf, cy_pdf) with corner handles."""
        key = (marker_type, marker_id)
        if key in self._zone_rects:
            self._remove_zone_items(key)
        rs = self.render_scale
        cx = cx_pdf * rs;  cy = cy_pdf * rs
        w  = w_pdf  * rs;  h  = h_pdf  * rs
        scene_rect = QRectF(cx - w / 2, cy - h / 2, w, h)
        pen = QPen(QColor(0, 180, 80, 220), 2)
        pen.setCosmetic(True)
        rect_item = self._scene.addRect(scene_rect, pen, QBrush(QColor(0, 200, 100, 40)))
        rect_item.setZValue(Z_OVERLAY - 1)
        rect_item.setData(self._DATA_ZONE_KEY, key)
        handles = []
        HR = 6.0
        corners = [scene_rect.topLeft(), scene_rect.topRight(),
                   scene_rect.bottomRight(), scene_rect.bottomLeft()]
        for i, corner in enumerate(corners):
            h_item = QGraphicsEllipseItem(-HR, -HR, 2 * HR, 2 * HR)
            h_item.setBrush(QBrush(QColor(255, 255, 255, 220)))
            h_item.setPen(QPen(QColor(0, 160, 70), 1.5))
            h_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            h_item.setPos(corner)
            h_item.setZValue(Z_OVERLAY + 4)
            h_item.setData(self._DATA_ZONE_KEY, key)
            h_item.setData(self._DATA_ZONE_CIDX, i)
            h_item.setCursor(Qt.CursorShape.SizeFDiagCursor)
            self._scene.addItem(h_item)
            handles.append(h_item)
        self._zone_rects[key] = {'rect_item': rect_item, 'handles': handles}
        self._add_tracked(rect_item, marker_type)
        for h in handles:
            self._add_tracked(h, marker_type)

    def _remove_zone_items(self, key):
        if key not in self._zone_rects:
            return
        info = self._zone_rects.pop(key)
        for item in [info['rect_item']] + info['handles']:
            try: self._scene.removeItem(item)
            except Exception: pass

    def _zone_handle_hit(self, view_point):
        """Return (key, cidx) if view_point (QPoint) is within 12px of a zone corner handle."""
        for key, info in self._zone_rects.items():
            for cidx, h in enumerate(info['handles']):
                hvp = self.mapFromScene(h.pos())
                dx = view_point.x() - hvp.x()
                dy = view_point.y() - hvp.y()
                if dx * dx + dy * dy < 144:
                    return (key, cidx)
        return None

    def _do_zone_resize(self, scene_pos):
        key = self._zone_resize_key
        if key not in self._zone_rects:
            return
        orig  = self._zone_resize_orig
        delta = scene_pos - self._zone_resize_start
        x0, y0 = orig.left(),  orig.top()
        x1, y1 = orig.right(), orig.bottom()
        cidx = self._zone_resize_cidx
        if   cidx == 0: x0 += delta.x(); y0 += delta.y()
        elif cidx == 1: x1 += delta.x(); y0 += delta.y()
        elif cidx == 2: x1 += delta.x(); y1 += delta.y()
        elif cidx == 3: x0 += delta.x(); y1 += delta.y()
        new_rect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized()
        self._zone_rects[key]['rect_item'].setRect(new_rect)
        corners = [new_rect.topLeft(), new_rect.topRight(),
                   new_rect.bottomRight(), new_rect.bottomLeft()]
        for h, corner in zip(self._zone_rects[key]['handles'], corners):
            h.setPos(corner)

    def _finish_zone_resize(self):
        key = self._zone_resize_key
        if key and key in self._zone_rects:
            scene_rect = self._zone_rects[key]['rect_item'].rect()
            rs = self.render_scale
            ox, oy = self._page_offsets.get(self.current_page, (0.0, 0.0))
            cx = (scene_rect.center().x() - ox) / rs
            cy = (scene_rect.center().y() - oy) / rs
            w  = scene_rect.width()              / rs
            h  = scene_rect.height()             / rs
            type_, id_ = key
            self.zone_resized.emit(type_, id_, cx, cy, w, h)
        self._zone_resize_key   = None
        self._zone_resize_cidx  = None
        self._zone_resize_start = None
        self._zone_resize_orig  = None
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def _show_context_menu(self, sp, global_pos):
        menu = QMenu(self.viewport())

        # ── Check if right-click landed on a sheet-connection arc ──────────────
        for item in self._scene.items(sp):
            conn_id = getattr(item, '_sheet_conn_id', None)
            if conn_id is not None:
                act = menu.addAction("✂️ Bryt länk")
                _cid = conn_id
                act.triggered.connect(lambda: self.sheet_conn_break_requested.emit(_cid))
                menu.exec(global_pos)
                return

        # ── Check if right-click landed on a page (board layout) ───────────────
        clicked_page = self._hit_test_page(sp)
        if self.mode == MODE_BOARD_LAYOUT:
            act = menu.addAction("🔗 Lägg till länk till annat blad…")
            _cp = clicked_page
            act.triggered.connect(lambda: self._start_add_sheet_link(_cp))
            menu.exec(global_pos)
            return

        # If cursor is on an existing marker, offer "add another here" at the top
        hovered_type = hovered_id = None
        for item in self._scene.items(sp):
            t = item.data(self._DATA_TYPE)
            i = item.data(self._DATA_ID)
            if t in ('cause', 'consequence', 'safeguard') and i is not None:
                hovered_type, hovered_id = t, int(i)
                break
        if hovered_type == 'cause':
            act = menu.addAction("⚙️ Lägg till ytterligare orsak här")
            cid = hovered_id
            act.triggered.connect(lambda: self.cause_at_marker_requested.emit(cid))
            menu.addSeparator()
        elif hovered_type == 'consequence':
            act = menu.addAction("⚠️ Lägg till ytterligare konsekvens här")
            cid = hovered_id
            act.triggered.connect(lambda: self.consequence_at_marker_requested.emit(cid))
            menu.addSeparator()
        elif hovered_type == 'safeguard':
            act = menu.addAction("🛡️ Lägg till ytterligare safeguard här")
            cid = hovered_id
            act.triggered.connect(lambda: self.safeguard_at_marker_requested.emit(cid))
            menu.addSeparator()

        menu.addAction("⚙️ Orsak",
                       lambda: self.context_action.emit('cause', sp, self.current_page))
        menu.addAction("⚠️ Konsekvens",
                       lambda: self.context_action.emit('consequence', sp, self.current_page))
        menu.addAction("🛡️ Safeguard",
                       lambda: self.context_action.emit('safeguard', sp, self.current_page))
        menu.addSeparator()
        menu.addAction("🔀 Risk Scenario",
                       lambda: self.context_action.emit('risk_scenario', sp, self.current_page))
        menu.addSeparator()
        menu.addAction("✏️ Rita Nodgräns",
                       lambda: self.context_action.emit('node', sp, self.current_page))
        menu.exec(global_pos)

    def _start_add_sheet_link(self, source_page: int):
        """Enter MODE_ADD_SHEET_LINK — next left-click on a different page creates a link."""
        self._add_link_source_page = source_page
        self.set_mode(MODE_ADD_SHEET_LINK)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _place_label(self, text: str, x_pdf: float, y_pdf: float,
                     r: float, color: QColor, marker_type: str):
        """Add a text label to the right of a marker circle with white background
        and automatic vertical offset when multiple markers share the same position."""
        center = self.pdf_to_scene(x_pdf, y_pdf)
        slot_key = (round(x_pdf * 5), round(y_pdf * 5))
        slot = self._label_slots.get(slot_key, 0)
        self._label_slots[slot_key] = slot + 1
        ROW_H = 17.0
        x0 = center.x() + r + 3
        y0 = center.y() - 8 + slot * ROW_H

        txt = QGraphicsSimpleTextItem(text[:35])
        f = QFont(); f.setPointSize(8)
        txt.setFont(f)
        txt.setBrush(QBrush(color))
        txt.setPos(x0, y0)
        txt.setZValue(Z_OVERLAY + 2)

        br = txt.boundingRect()
        pad = 2.0
        bg = QGraphicsRectItem(x0 - pad, y0 - pad, br.width() + 2 * pad, br.height() + 2 * pad)
        bg.setPen(QPen(Qt.PenStyle.NoPen))
        bg.setBrush(QBrush(QColor(255, 255, 255, 230)))
        bg.setZValue(Z_OVERLAY + 1)

        self._add_tracked(bg, marker_type)
        self._add_tracked(txt, marker_type)

    def add_cause_marker(self, cause_id, x_pdf, y_pdf, comp_type, label, tag='',
                         rect_w=None, rect_h=None):
        center = self.pdf_to_scene(x_pdf, y_pdf)
        r = 14.0
        circle = QGraphicsEllipseItem(center.x() - r, center.y() - r, 2 * r, 2 * r)
        circle.setPen(QPen(QColor(160, 0, 0), 2))
        circle.setBrush(QBrush(QColor(231, 76, 60, 200)))
        circle.setZValue(Z_OVERLAY)
        tip = f"{tag + ': ' if tag else ''}{comp_type}" + (f"\n{label}" if label else '')
        tip += "\n🖱 Klicka för att navigera i trädet"
        circle.setToolTip(tip)
        circle.setData(self._DATA_TYPE, 'cause')
        circle.setData(self._DATA_ID,   cause_id)
        circle.setAcceptHoverEvents(True)
        circle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_tracked(circle, 'cause')

        display = tag if tag else comp_type[:3].upper()
        inner = QGraphicsSimpleTextItem(display)
        f = QFont(); f.setPointSize(7 if len(display) > 4 else 8); f.setBold(True)
        inner.setFont(f)
        inner.setBrush(QBrush(QColor(255, 255, 255)))
        ibr = inner.boundingRect()
        inner.setPos(center.x() - ibr.width() / 2, center.y() - ibr.height() / 2)
        inner.setZValue(Z_OVERLAY + 1)
        self._add_tracked(inner, 'cause')

        if label:
            self._place_label(label, x_pdf, y_pdf, r, QColor(120, 0, 0), 'cause')
        if rect_w is not None and rect_h is not None and rect_w > 0 and rect_h > 0:
            self.add_zone_rect('cause', cause_id, x_pdf, y_pdf, rect_w, rect_h)

    def add_consequence_marker(self, cons_id, x_pdf, y_pdf, target,
                               rect_w=None, rect_h=None):
        center = self.pdf_to_scene(x_pdf, y_pdf)
        r = 12.0
        circle = QGraphicsEllipseItem(center.x() - r, center.y() - r, 2 * r, 2 * r)
        circle.setPen(QPen(QColor(180, 100, 0), 2))
        circle.setBrush(QBrush(QColor(243, 156, 18, 190)))
        circle.setZValue(Z_OVERLAY)
        circle.setToolTip((target or '') + "\n🖱 Klicka för att navigera i trädet")
        circle.setData(self._DATA_TYPE, 'consequence')
        circle.setData(self._DATA_ID,   cons_id)
        circle.setAcceptHoverEvents(True)
        circle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_tracked(circle, 'consequence')

        inner = QGraphicsSimpleTextItem("K")
        f = QFont(); f.setPointSize(8); f.setBold(True)
        inner.setFont(f); inner.setBrush(QBrush(QColor(255, 255, 255)))
        ibr = inner.boundingRect()
        inner.setPos(center.x() - ibr.width() / 2, center.y() - ibr.height() / 2)
        inner.setZValue(Z_OVERLAY + 1)
        self._add_tracked(inner, 'consequence')
        if target:
            self._place_label(target, x_pdf, y_pdf, r, QColor(130, 70, 0), 'consequence')
        if rect_w is not None and rect_h is not None and rect_w > 0 and rect_h > 0:
            self.add_zone_rect('consequence', cons_id, x_pdf, y_pdf, rect_w, rect_h)

    def add_safeguard_marker(self, sg_id, x_pdf, y_pdf, tag, description,
                             rect_w=None, rect_h=None):
        center = self.pdf_to_scene(x_pdf, y_pdf)
        r = 12.0
        circle = QGraphicsEllipseItem(center.x() - r, center.y() - r, 2 * r, 2 * r)
        circle.setPen(QPen(QColor(20, 120, 20), 2))
        circle.setBrush(QBrush(QColor(39, 174, 96, 200)))
        circle.setZValue(Z_OVERLAY)
        tip = f"{tag + ': ' if tag else ''}{description}\n🖱 Klicka för att navigera i trädet"
        circle.setToolTip(tip)
        circle.setData(self._DATA_TYPE, 'safeguard')
        circle.setData(self._DATA_ID,   sg_id)
        circle.setAcceptHoverEvents(True)
        circle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_tracked(circle, 'safeguard')

        display = tag if tag else 'SG'
        inner = QGraphicsSimpleTextItem(display[:4])
        f = QFont(); f.setPointSize(7 if len(display) > 3 else 8); f.setBold(True)
        inner.setFont(f); inner.setBrush(QBrush(QColor(255, 255, 255)))
        ibr = inner.boundingRect()
        inner.setPos(center.x() - ibr.width() / 2, center.y() - ibr.height() / 2)
        inner.setZValue(Z_OVERLAY + 1)
        self._add_tracked(inner, 'safeguard')

        if description:
            self._place_label(description, x_pdf, y_pdf, r, QColor(20, 100, 20), 'safeguard')
        if rect_w is not None and rect_h is not None and rect_w > 0 and rect_h > 0:
            self.add_zone_rect('safeguard', sg_id, x_pdf, y_pdf, rect_w, rect_h)

    def _extract_tag_from_rect(self, pdf_rect: QRectF) -> tuple:
        """Extract tag text AND classify the P&ID symbol inside the rectangle.

        Returns (tag: str, comp_type: str, symbol_name: str)
        e.g. ('PSV-101', 'Säkerhetsventil (PSV)', 'Säkerhetsventil (PSV/PRV)')
        """
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return '', '', ''
        try:
            page  = self.pdf_doc.load_page(self.current_page)
            frect = fitz.Rect(pdf_rect.x(), pdf_rect.y(),
                               pdf_rect.x() + pdf_rect.width(),
                               pdf_rect.y() + pdf_rect.height())

            # ── 1. Native text extraction with spatial combining ──────────────
            raw_words = page.get_text("words", clip=frect)
            # Try spatially-combined strings first (catches 20 - PCV - 101)
            tag = ''
            for candidate in _spatial_combine(raw_words):
                t = _pick_best_tag(candidate)
                if t:
                    tag = t
                    break
            # Fallback: all words joined
            if not tag:
                native_text = ' '.join(w[4].strip() for w in raw_words if w[4].strip())
                tag = _pick_best_tag(native_text) or native_text.strip()

            # ── 2. OCR fallback ───────────────────────────────────────────────
            if not tag and HAS_PIL:
                min_dim  = max(pdf_rect.width(), pdf_rect.height(), 10.0)
                scale    = max(4.0, min(16.0, 300.0 / min_dim))
                mat      = fitz.Matrix(scale, scale)
                pix      = page.get_pixmap(matrix=mat, clip=frect, alpha=False)
                pil      = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
                pil      = _preprocess_for_ocr(pil)
                ocr_text = ''
                if HAS_TESSERACT:
                    try:
                        cfg = ('--oem 3 --psm 7 '
                               '-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.')
                        ocr_text = pytesseract.image_to_string(pil, config=cfg).strip()
                    except Exception:
                        pass
                if not ocr_text and HAS_EASYOCR:
                    try:
                        import numpy as np
                        reader = _get_easyocr_reader()
                        if reader:
                            results = reader.readtext(np.array(pil))
                            ocr_text = ' '.join(r[1] for r in results if r[2] > 0.3)
                    except Exception:
                        pass
                tag = _pick_best_tag(ocr_text) or ocr_text.strip()

            return tag

        except Exception:
            pass
        return ''

    def _text_in_rect(self, pdf_rect: QRectF) -> str:
        """Return all text inside pdf_rect. Uses native PDF text first, OCR as fallback."""
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return ''
        try:
            pg    = self.pdf_doc.load_page(self.current_page)
            frect = fitz.Rect(pdf_rect.x(), pdf_rect.y(),
                               pdf_rect.x() + pdf_rect.width(),
                               pdf_rect.y() + pdf_rect.height())
            raw_words = pg.get_text("words", clip=frect)
            native = ' '.join(w[4].strip() for w in raw_words if w[4].strip())
            if native.strip():
                return native.strip()
            # OCR fallback
            if not HAS_PIL:
                return ''
            min_dim = max(pdf_rect.width(), pdf_rect.height(), 10.0)
            scale   = max(4.0, min(16.0, 300.0 / min_dim))
            mat     = fitz.Matrix(scale, scale)
            pix     = pg.get_pixmap(matrix=mat, clip=frect, alpha=False)
            pil     = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
            pil     = _preprocess_for_ocr(pil)
            if HAS_TESSERACT:
                try:
                    ocr = pytesseract.image_to_string(pil).strip()
                    if ocr:
                        return ocr
                except Exception:
                    pass
            if HAS_EASYOCR:
                try:
                    import numpy as np
                    reader = _get_easyocr_reader()
                    if reader:
                        results = reader.readtext(np.array(pil))
                        txt = ' '.join(r[1] for r in results if r[2] > 0.3)
                        if txt.strip():
                            return txt.strip()
                except Exception:
                    pass
        except Exception:
            pass
        return ''

    def add_tag_highlight(self, bbox: 'fitz.Rect', color: str, tooltip: str = ''):
        """Draw a semi-transparent highlight rectangle at the tag's PDF position."""
        r = QRectF(bbox.x0, bbox.y0, bbox.width, bbox.height)
        pen = QPen(Qt.PenStyle.NoPen)
        brush = QBrush(QColor(color))
        item = self._scene.addRect(r, pen, brush)
        item.setOpacity(0.35)
        item.setZValue(Z_HIGHLIGHT)
        if tooltip:
            item.setToolTip(tooltip)
        return item

    def clear_highlights(self):
        """Remove all tag highlights (Z_HIGHLIGHT items)."""
        for item in list(self._scene.items()):
            if item.zValue() == Z_HIGHLIGHT:
                try: self._scene.removeItem(item)
                except Exception: pass

    def add_connection_line(self, start: QPointF, end: QPointF, color: str, dashed=False):
        pen = QPen(QColor(color), 1.5)
        pen.setCosmetic(True)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        line = self._scene.addLine(start.x(), start.y(), end.x(), end.y(), pen)
        line.setZValue(Z_CONNECT)

    def add_sheet_conn_arc(self, src: QPointF, dst: QPointF,
                           color_hex: str, confidence: float, label: str,
                           bidirectional: bool = False, conn_id: int = -1,
                           src_edge: str = 'right', dst_edge: str = 'left',
                           src_page: int = -1, dst_page: int = -1,
                           arc_index: int = 0, weight: float = 0.5):
        """Draw a cubic bezier connection over the P&ID pages at 50% opacity.

        Control points extend outward from each page edge so curves leave the
        page cleanly. Parallel connections are staggered perpendicular to the
        chord. Pen width scales with weight; drawn above all page pixmaps.
        """
        import math
        color = QColor(color_hex)
        color.setAlpha(128)   # 50 % transparent

        pen_width = round(max(2.0, min(6.0, 2.0 + weight * 5.0)), 1)
        pen = QPen(color, pen_width)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if confidence < 0.50:
            pen.setStyle(Qt.PenStyle.DashLine)
        elif confidence < 0.70:
            pen.setStyle(Qt.PenStyle.DotLine)

        sx, sy = src.x(), src.y()
        ex, ey = dst.x(), dst.y()

        # Stagger parallel connections perpendicular to the chord
        STEP = 20
        if arc_index == 0:
            slot_off = 0
        else:
            slot_off = ((arc_index + 1) // 2) * STEP * (1 if arc_index % 2 == 1 else -1)

        chord = math.hypot(ex - sx, ey - sy) or 1.0
        perp_x = -(ey - sy) / chord
        perp_y =  (ex - sx) / chord
        src_pt = QPointF(sx + perp_x * slot_off, sy + perp_y * slot_off)
        dst_pt = QPointF(ex + perp_x * slot_off, ey + perp_y * slot_off)

        # Bezier control points: extend outward from each page edge
        ctrl_dist = max(120.0, chord * 0.38)
        _edx = {'right': 1, 'left': -1, 'top': 0, 'bottom': 0}
        _edy = {'right': 0, 'left':  0, 'top': -1, 'bottom': 1}
        cp1 = QPointF(src_pt.x() + _edx.get(src_edge, 1)  * ctrl_dist,
                      src_pt.y() + _edy.get(src_edge, 0)  * ctrl_dist)
        cp2 = QPointF(dst_pt.x() + _edx.get(dst_edge, -1) * ctrl_dist,
                      dst_pt.y() + _edy.get(dst_edge,  0) * ctrl_dist)

        path = QPainterPath()
        path.moveTo(src_pt)
        path.cubicTo(cp1, cp2, dst_pt)

        pi = QGraphicsPathItem(path)
        pi.setPen(pen)
        pi.setZValue(Z_SHEET_CONN)   # above page pixmaps
        pi._sheet_conn_id = conn_id
        pi.setFlag(pi.GraphicsItemFlag.ItemIsSelectable, False)
        self._scene.addItem(pi)

        # Arrowhead tangent follows the bezier end-slope (dst_pt − cp2)
        arrow_angle = math.atan2(dst_pt.y() - cp2.y(), dst_pt.x() - cp2.x())

        def _arrowhead(tip_pt, angle, col):
            AL, AH = 16, 7
            lp = QPointF(tip_pt.x() - AL * math.cos(angle) + AH * math.sin(angle),
                         tip_pt.y() - AL * math.sin(angle) - AH * math.cos(angle))
            rp = QPointF(tip_pt.x() - AL * math.cos(angle) - AH * math.sin(angle),
                         tip_pt.y() - AL * math.sin(angle) + AH * math.cos(angle))
            ah = QGraphicsPolygonItem(QPolygonF([tip_pt, lp, rp]))
            ah.setBrush(QBrush(col))
            ah.setPen(QPen(Qt.PenStyle.NoPen))
            ah.setZValue(Z_SHEET_CONN)
            self._scene.addItem(ah)

        _arrowhead(dst_pt, arrow_angle, color)
        if bidirectional:
            src_angle = math.atan2(src_pt.y() - cp1.y(), src_pt.x() - cp1.x())
            _arrowhead(src_pt, src_angle, color)

        # Label at bezier midpoint with white background
        if label:
            mid = path.pointAtPercent(0.5)
            txt = QGraphicsSimpleTextItem(label)
            label_color = QColor(color_hex)   # opaque for readability
            label_color.setAlpha(200)
            txt.setBrush(QBrush(label_color.darker(130)))
            fnt = txt.font(); fnt.setPointSize(8); fnt.setBold(True); txt.setFont(fnt)
            tr = txt.boundingRect()
            txt.setPos(mid.x() - tr.width() / 2, mid.y() - tr.height() / 2 - 10)
            bg = QGraphicsRectItem(txt.pos().x() - 3, txt.pos().y() - 2,
                                   tr.width() + 6, tr.height() + 4)
            bg.setBrush(QBrush(QColor(250, 250, 250, 210)))
            bg.setPen(QPen(Qt.PenStyle.NoPen))
            bg.setZValue(Z_SHEET_CONN + 0.1)
            txt.setZValue(Z_SHEET_CONN + 0.2)
            self._scene.addItem(bg)
            self._scene.addItem(txt)

    def clear_overlays(self):
        _keep = set(self._all_page_items.values()) | {self._placeholder}
        for item in list(self._scene.items()):
            if item in _keep:
                continue
            if item.zValue() >= Z_SHEET_CONN or item.zValue() < Z_PAGE:
                try: self._scene.removeItem(item)
                except Exception: pass
        if self._pending_path_item is not None:
            try: self._scene.removeItem(self._pending_path_item)
            except Exception: pass
            self._pending_path_item = None
        # Clear per-type item lists, zone rect dict, and label slots
        for key in self._type_items:
            self._type_items[key] = []
        self._zone_rects.clear()
        self._label_slots.clear()

    def mousePressEvent(self, event):
        # Update current_page to whichever page was clicked
        _sp = self.mapToScene(event.position().toPoint())
        _detected = self._hit_test_page(_sp)
        if _detected != self.current_page:
            self.current_page = _detected
            self.page_item = self._all_page_items.get(_detected)

        # ── Add sheet link: click target page ────────────────────────────────
        if self.mode == MODE_ADD_SHEET_LINK and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            target = self._hit_test_page(sp)
            src = self._add_link_source_page
            self._add_link_source_page = None
            self.set_mode(MODE_BOARD_LAYOUT)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            if src is not None and target != src and target in self._all_page_items:
                self.sheet_conn_add_requested.emit(src, target)
            event.accept(); return
        if self.mode == MODE_ADD_SHEET_LINK and event.button() == Qt.MouseButton.RightButton:
            # Cancel add-link mode
            self._add_link_source_page = None
            self.set_mode(MODE_BOARD_LAYOUT)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            event.accept(); return

        # ── Board layout: drag page ───────────────────────────────────────────
        if self.mode == MODE_BOARD_LAYOUT and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            page = self._hit_test_page(sp)
            if page in self._all_page_items:
                self._dragging_page        = page
                self._drag_page_orig_offset = self._page_offsets.get(page, (0.0, 0.0))
                self._drag_page_start_scene = sp
                self.current_page           = page
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                # Hide connection lines during drag (re-drawn on release)
                for _ci in list(self._scene.items()):
                    if _ci.zValue() == Z_CONNECT:
                        _ci.setVisible(False)
                event.accept(); return
            super().mousePressEvent(event); return

        # ── Zone corner handle — intercept LEFT click in any mode ─────────────
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._zone_handle_hit(event.position().toPoint())
            if hit:
                key, cidx = hit
                sp2 = self.mapToScene(event.position().toPoint())
                self._zone_resize_key   = key
                self._zone_resize_cidx  = cidx
                self._zone_resize_start = sp2
                self._zone_resize_orig  = self._zone_rects[key]['rect_item'].rect()
                event.accept(); return

        if self.mode in (MODE_NAV, MODE_MARKUP_SELECT):
            self._press_pos = event.position()
        sp = self.mapToScene(event.position().toPoint())
        if self.mode == MODE_NODE:
            if event.button() == Qt.MouseButton.LeftButton:
                self._add_draw_point(sp); event.accept(); return
            elif event.button() == Qt.MouseButton.RightButton:
                self._cancel_drawing(); event.accept(); return
        elif self.mode in (MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE):
            if event.button() == Qt.MouseButton.LeftButton:
                self._add_draw_point(self._snap_to_nearest(sp)); event.accept(); return
            elif event.button() == Qt.MouseButton.RightButton:
                self._cancel_drawing(); event.accept(); return
        elif self.mode == MODE_SMART_POLYLINE:
            if event.button() == Qt.MouseButton.LeftButton:
                if self._smart_start_pdf is None:
                    self._smart_start_pdf = list(self.scene_to_pdf(sp))
                    self._smart_end_pdf   = None
                    self._clear_smart_preview()
                    self._draw_smart_marker(sp, 'start')
                else:
                    self._smart_end_pdf = list(self.scene_to_pdf(sp))
                    self._draw_smart_marker(sp, 'end')
                    self._run_smart_trace()
                event.accept(); return
            elif event.button() == Qt.MouseButton.RightButton:
                self._cancel_smart(); event.accept(); return
        elif self.mode in (MODE_MARKUP_TEXT, MODE_MARKUP_COMMENT):
            if event.button() == Qt.MouseButton.LeftButton:
                # Single-click immediately triggers finished signal
                pdf_pt = self.scene_to_pdf(sp)
                type_ = 'text' if self.mode == MODE_MARKUP_TEXT else 'comment'
                self.markup_draw_finished.emit(type_, [list(pdf_pt)], self.current_page)
                event.accept(); return
        elif self.mode == MODE_RED_MARKUP_SYMBOL:
            if event.button() == Qt.MouseButton.LeftButton:
                pdf_pt = self.scene_to_pdf(sp)
                self.markup_draw_finished.emit('symbol', [list(pdf_pt)], self.current_page)
                event.accept(); return
        elif self.mode == MODE_MARKUP_SELECT:
            if event.button() == Qt.MouseButton.LeftButton:
                view_pos = event.position().toPoint()
                # Priority 0a: corner resize handle (symbol)
                _corner_cursors = [
                    Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeBDiagCursor,
                    Qt.CursorShape.SizeBDiagCursor, Qt.CursorShape.SizeFDiagCursor,
                ]
                for i, handle in enumerate(self._corner_handles):
                    hvp = self.mapFromScene(handle.scenePos())
                    dx = view_pos.x() - hvp.x()
                    dy = view_pos.y() - hvp.y()
                    if dx * dx + dy * dy < 144:   # 12 screen-pixel radius
                        self._symbol_drag_mode = ['nw', 'ne', 'sw', 'se'][i]
                        self._drag_mode = 'symbol_transform'
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(_corner_cursors[i])
                        event.accept(); return
                # Priority 0b: rotation handle (symbol)
                if self._rot_handle is not None:
                    hvp = self.mapFromScene(self._rot_handle.scenePos())
                    dx = view_pos.x() - hvp.x()
                    dy = view_pos.y() - hvp.y()
                    if dx * dx + dy * dy < 196:   # 14 screen-pixel radius
                        self._symbol_drag_mode = 'rotate'
                        self._drag_mode = 'symbol_transform'
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(Qt.CursorShape.OpenHandCursor)
                        event.accept(); return
                # Priority 1: vertex handle hit
                for i, handle in enumerate(self._vertex_handles):
                    hvp = self.mapFromScene(handle.scenePos())
                    dx = view_pos.x() - hvp.x()
                    dy = view_pos.y() - hvp.y()
                    if dx * dx + dy * dy < 144:   # 12 screen-pixel radius
                        self._drag_mode = 'vertex'
                        self._drag_vertex_idx = i
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(Qt.CursorShape.CrossCursor)
                        event.accept(); return
                # Priority 2: markup item hit
                for item in self._scene.items(sp):
                    mu_id = item.data(self._DATA_MARKUP_ID)
                    if mu_id is not None:
                        mu_id_int = int(mu_id)
                        self._select_for_edit(mu_id_int)
                        self._drag_mode = 'item'
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(Qt.CursorShape.SizeAllCursor)
                        event.accept(); return
                # Priority 3: empty space → clear selection, fall through for panning
                self._clear_edit_handles()
        elif self.mode in (MODE_CAUSE, MODE_CONSEQUENCE, MODE_SAFEGUARD,
                           MODE_PLACE_EXISTING, MODE_CAUSE_TEMPLATE):
            if event.button() == Qt.MouseButton.LeftButton:
                # Start rubber-band rectangle selection (or simple click)
                self._rect_start = sp
                self._rect_item  = None
                event.accept(); return

        if event.button() == Qt.MouseButton.RightButton and self.mode in (MODE_NAV, MODE_BOARD_LAYOUT):
            # Start rubber-band drag; show context menu only if no drag occurs
            self._rband_start_scene = sp
            self._rband_dragging    = False
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.mode == MODE_MARKUP_SELECT and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            for gi in self._scene.items(sp):
                mu_id = gi.data(self._DATA_MARKUP_ID)
                if mu_id is not None:
                    mu_id_int = int(mu_id)
                    if self._markup_types.get(mu_id_int) in ('text', 'comment'):
                        self._start_inline_label_edit(mu_id_int)
                        event.accept()
                        return
        if self.mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE) \
                and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            self._add_draw_point(sp)
            self._finish_markup_drawing()
            event.accept(); return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        # ── Board layout page drag release ────────────────────────────────────
        if (self.mode == MODE_BOARD_LAYOUT and self._dragging_page is not None and
                event.button() == Qt.MouseButton.LeftButton):
            self._dragging_page = None
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self._update_board_scene_rect()
            layout = {str(p): [off[0], off[1]] for p, off in self._page_offsets.items()}
            self.board_layout_changed.emit(json.dumps(layout))
            event.accept(); return

        # ── Zone corner resize end ────────────────────────────────────────────
        if self._zone_resize_key is not None and event.button() == Qt.MouseButton.LeftButton:
            self._finish_zone_resize()
            event.accept(); return

        # ── Right-drag rubber band end ────────────────────────────────────────
        if event.button() == Qt.MouseButton.RightButton and self._rband_start_scene is not None:
            if self._rband_dragging:
                sp = self.mapToScene(event.position().toPoint())
                if self._rband_preview_item is not None:
                    try: self._scene.removeItem(self._rband_preview_item)
                    except Exception: pass
                    self._rband_preview_item = None
                rect = QRectF(self._rband_start_scene, sp).normalized()
                rs = self.render_scale
                ox, oy = self._page_offsets.get(self.current_page, (0.0, 0.0))
                pdf_rect = QRectF((rect.x() - ox) / rs, (rect.y() - oy) / rs,
                                   rect.width() / rs, rect.height() / rs)
                self._rband_start_scene = None
                self._rband_dragging    = False
                self.zone_drawn.emit(pdf_rect, self.current_page)
            else:
                # No drag — show context menu as usual
                sp = self.mapToScene(event.position().toPoint())
                self._rband_start_scene = None
                self._rband_dragging    = False
                self._show_context_menu(sp, event.globalPosition().toPoint())
            event.accept(); return

        # ── Rect-select release for cause/consequence/safeguard ───────────────
        if (event.button() == Qt.MouseButton.LeftButton and
                self.mode in (MODE_CAUSE, MODE_CONSEQUENCE, MODE_SAFEGUARD,
                              MODE_PLACE_EXISTING, MODE_CAUSE_TEMPLATE) and
                self._rect_start is not None):

            end_sp = self.mapToScene(event.position().toPoint())
            rect   = QRectF(self._rect_start, end_sp).normalized()

            # Remove rubber-band rect
            if self._rect_item is not None:
                try: self._scene.removeItem(self._rect_item)
                except Exception: pass
                self._rect_item = None
            self._rect_start = None

            # Convert to PDF coordinates
            rs = self.render_scale
            ox, oy = self._page_offsets.get(self.current_page, (0.0, 0.0))
            pdf_rect = QRectF((rect.x() - ox) / rs, (rect.y() - oy) / rs,
                               rect.width() / rs, rect.height() / rs)

            # Extract tag text from the selected rectangle
            suggested = self._extract_tag_from_rect(pdf_rect)

            center = rect.center()
            if self.mode == MODE_CAUSE:
                self.cause_clicked.emit(center, self.current_page, suggested)
            elif self.mode == MODE_CONSEQUENCE:
                self.consequence_clicked.emit(center, self.current_page, suggested)
            elif self.mode == MODE_SAFEGUARD:
                self.safeguard_clicked.emit(center, self.current_page, suggested)
            elif self.mode == MODE_PLACE_EXISTING:
                self.place_existing_clicked.emit(center, self.current_page)
            elif self.mode == MODE_CAUSE_TEMPLATE:
                self.cause_template_clicked.emit(center, self.current_page, suggested)
            event.accept()
            return

        # ── MARKUP_SELECT drag-aware release ──────────────────────────────────
        if self.mode == MODE_MARKUP_SELECT and self._drag_mode is not None and \
                event.button() == Qt.MouseButton.LeftButton:
            if self._drag_threshold_exceeded and self._edit_mu_id is not None:
                if self._drag_mode == 'symbol_transform':
                    self._finish_symbol_transform()
                else:
                    self._finish_edit_drag()
            elif not self._drag_threshold_exceeded and self._edit_mu_id is not None:
                if self._drag_mode != 'symbol_transform':
                    self.markup_item_clicked.emit(self._edit_mu_id)
            self._drag_mode = None
            self._drag_vertex_idx = None
            self._drag_threshold_exceeded = False
            self._press_pos = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return

        # ── NAV mode: click on marker navigates tree ──────────────────────────
        if (self.mode in (MODE_NAV,) and
                event.button() == Qt.MouseButton.LeftButton and
                self._press_pos is not None):
            p  = event.position()
            dx = p.x() - self._press_pos.x()
            dy = p.y() - self._press_pos.y()
            if dx * dx + dy * dy < 25:
                sp = self.mapToScene(p.toPoint())
                for item in self._scene.items(sp):
                    itype = item.data(self._DATA_TYPE)
                    iid   = item.data(self._DATA_ID)
                    if itype in ('cause', 'consequence', 'safeguard') and iid is not None:
                        self.marker_clicked.emit(itype, int(iid))
                        break
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        # ── Board layout page drag ────────────────────────────────────────────
        if self.mode == MODE_BOARD_LAYOUT and self._dragging_page is not None:
            sp = self.mapToScene(event.position().toPoint())
            dx = sp.x() - self._drag_page_start_scene.x()
            dy = sp.y() - self._drag_page_start_scene.y()
            orig_ox, orig_oy = self._drag_page_orig_offset
            new_ox = orig_ox + dx
            new_oy = orig_oy + dy
            self._page_offsets[self._dragging_page] = (new_ox, new_oy)
            self._all_page_items[self._dragging_page].setPos(new_ox, new_oy)
            event.accept(); return

        # ── Zone corner resize drag ───────────────────────────────────────────
        if self._zone_resize_key is not None:
            self._do_zone_resize(self.mapToScene(event.position().toPoint()))
            event.accept(); return

        # ── Right-drag rubber band ────────────────────────────────────────────
        if (self._rband_start_scene is not None and
                event.buttons() & Qt.MouseButton.RightButton):
            sp = self.mapToScene(event.position().toPoint())
            dx = sp.x() - self._rband_start_scene.x()
            dy = sp.y() - self._rband_start_scene.y()
            if not self._rband_dragging and dx * dx + dy * dy > 100:
                self._rband_dragging = True
            if self._rband_dragging:
                rect = QRectF(self._rband_start_scene, sp).normalized()
                if self._rband_preview_item is not None:
                    try: self._scene.removeItem(self._rband_preview_item)
                    except Exception: pass
                pen = QPen(QColor(0, 180, 80), 1.5)
                pen.setStyle(Qt.PenStyle.DashLine)
                pen.setCosmetic(True)
                self._rband_preview_item = self._scene.addRect(
                    rect, pen, QBrush(QColor(0, 200, 100, 40)))
                self._rband_preview_item.setZValue(Z_TEMP)
            event.accept(); return

        if self.mode == MODE_MARKUP_SELECT and self._drag_mode is not None:
            sp = self.mapToScene(event.position().toPoint())
            if not self._drag_threshold_exceeded:
                dx = sp.x() - self._drag_start_scene.x()
                dy = sp.y() - self._drag_start_scene.y()
                if dx * dx + dy * dy > 4.0:
                    self._drag_threshold_exceeded = True
            if self._drag_threshold_exceeded:
                delta = sp - self._drag_start_scene
                if self._drag_mode == 'symbol_transform':
                    self._do_symbol_transform(sp)
                elif self._drag_mode == 'vertex' and self._drag_vertex_idx is not None:
                    new_pts = list(self._drag_current_pts)
                    idx = self._drag_vertex_idx
                    new_pts[idx] = self._drag_original_pts[idx] + delta
                    self._drag_current_pts = new_pts
                    self._update_edit_path(self._drag_current_pts)
                    self._update_handle_positions(self._drag_current_pts)
                elif self._drag_mode == 'item':
                    self._drag_current_pts = [p + delta for p in self._drag_original_pts]
                    self._update_edit_path(self._drag_current_pts)
                    self._update_handle_positions(self._drag_current_pts)
                event.accept()
                return
        if self.mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE) \
                and self.draw_points:
            sp = self.mapToScene(event.position().toPoint())
            if self.mode in (MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE):
                sp = self._snap_to_nearest(sp)
            self._update_rubber_band(sp)
        elif self.mode in (MODE_CAUSE, MODE_CONSEQUENCE, MODE_SAFEGUARD,
                           MODE_PLACE_EXISTING, MODE_CAUSE_TEMPLATE) \
                and self._rect_start is not None:
            current = self.mapToScene(event.position().toPoint())
            rect = QRectF(self._rect_start, current).normalized()
            if self._rect_item is not None:
                try: self._scene.removeItem(self._rect_item)
                except Exception: pass
            pen = QPen(QColor(0, 100, 220), 1.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            self._rect_item = self._scene.addRect(
                rect, pen, QBrush(QColor(0, 100, 220, 30)))
            self._rect_item.setZValue(Z_TEMP)
            event.accept(); return
        super().mouseMoveEvent(event)

    _LOD_THRESHOLD = 0.12   # view scale below which overview mode activates

    def _apply_lod(self, scale: float, force: bool = False):
        """Switch rendering quality based on zoom level.

        Only iterates scene items when the LOD tier actually changes so there
        is no per-frame overhead on continuous zoom within the same tier.
        """
        overview = scale < self._LOD_THRESHOLD
        if not force and overview == self._lod_overview:
            return
        self._lod_overview = overview

        aa = not overview
        self.setRenderHint(QPainter.RenderHint.Antialiasing, aa)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, aa)

        mode = (Qt.TransformationMode.FastTransformation if overview
                else Qt.TransformationMode.SmoothTransformation)
        for item in self._all_page_items.values():
            item.setTransformationMode(mode)

        # Overlay items (markers, labels, handles): invisible at overview zoom
        for item in self._scene.items():
            if item.zValue() >= Z_OVERLAY:
                item.setVisible(not overview)

    def wheelEvent(self, event):
        # Smooth zoom: scale by a factor proportional to the wheel delta
        # so trackpad pinch-zoom gives fine-grained control
        delta = event.angleDelta().y()
        if delta == 0:
            event.accept(); return
        # 1.001^delta gives ~1.15× per 120-unit tick (standard wheel notch)
        factor = 1.001 ** delta
        # Clamp to prevent extreme zoom
        cur = self.transform().m11()
        if cur * factor < 0.02:
            factor = 0.02 / cur
        elif cur * factor > 200:
            factor = 200 / cur
        self.scale(factor, factor)
        self._apply_lod(self.transform().m11())
        event.accept()

    def keyPressEvent(self, event):
        if self.mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._finish_markup_drawing(); event.accept(); return
            elif event.key() == Qt.Key.Key_Escape:
                self._cancel_drawing(); event.accept(); return
        elif self.mode == MODE_SMART_POLYLINE and self._smart_paths:
            k = event.key()
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._confirm_smart(); event.accept(); return
            elif k == Qt.Key.Key_Escape:
                self._cancel_smart(); event.accept(); return
            elif k == Qt.Key.Key_Left:
                self._smart_path_idx = (self._smart_path_idx - 1) % len(self._smart_paths)
                self._show_smart_path(self._smart_path_idx); event.accept(); return
            elif k == Qt.Key.Key_Right:
                self._smart_path_idx = (self._smart_path_idx + 1) % len(self._smart_paths)
                self._show_smart_path(self._smart_path_idx); event.accept(); return
        super().keyPressEvent(event)


def _vline():
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


class PIDImportDialog(QDialog):
    """Dialog shown when opening a P&ID — choose Ny revision or Nya blad."""

    def __init__(self, has_existing=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Importera P&ID")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        if has_existing:
            layout.addWidget(QLabel("En P&ID är redan inläst. Vad vill du göra?"))
            self._new_rev_btn    = QRadioButton("Ny revision — ersätt befintlig PDF")
            self._new_sheets_btn = QRadioButton("Nya blad — sammanfoga och lägg till sidor sist")
            self._new_rev_btn.setChecked(True)
            layout.addWidget(self._new_rev_btn)
            layout.addWidget(self._new_sheets_btn)
        else:
            layout.addWidget(QLabel("Importera P&ID-ritning:"))
            self._new_rev_btn    = None
            self._new_sheets_btn = None

        form = QFormLayout()
        form.setSpacing(8)
        self._label_edit = QLineEdit()
        self._label_edit.setPlaceholderText("t.ex. Rev A, 2024-01-15")
        self._notes_edit = QLineEdit()
        self._notes_edit.setPlaceholderText("Valfri beskrivning")
        form.addRow("Revision/märkning:", self._label_edit)
        form.addRow("Anteckningar:", self._notes_edit)
        layout.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def is_new_revision(self):
        if self._new_rev_btn is None:
            return True
        return self._new_rev_btn.isChecked()

    def label(self):
        return self._label_edit.text().strip()

    def notes(self):
        return self._notes_edit.text().strip()


# ── PDF-export helpers ─────────────────────────────────────────────────────────

def _hex_to_fitz_rgb(hex_color):
    """Convert '#rrggbb' string to (r, g, b) float tuple (0..1)."""
    h = hex_color.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)


def _draw_pid_marker(page, x, y, rgb, letter, label):
    """Draw a filled circle at (x, y) with a centred letter and an outside label.

    Coordinates are in PyMuPDF page space (0,0 = top-left, y down).
    """
    R = 7.0
    # Filled circle
    shape = page.new_shape()
    shape.draw_circle(fitz.Point(x, y), R)
    try:
        shape.finish(color=rgb, fill=rgb, width=0.5, fill_opacity=0.80)
    except TypeError:
        shape.finish(color=rgb, fill=rgb, width=0.5)
    shape.commit()
    # Centred letter in white (baseline ≈ circle-centre + cap_height/2 ≈ +3.5 pts)
    try:
        page.insert_text(fitz.Point(x - 2.5, y + 3.5), letter,
                         fontsize=9, color=(1.0, 1.0, 1.0), fontname='helv')
    except Exception:
        pass
    # Label to the right of the circle
    if label:
        try:
            page.insert_text(fitz.Point(x + R + 3, y + 3.5),
                             str(label)[:40], fontsize=7, color=rgb, fontname='helv')
        except Exception:
            pass


_INSTR_SECONDARY_EFFECTS = [
    "Pump stoppar",
    "Kompressor stoppar",
    "Reglerventil stänger",
    "Reglerventil öppnar",
    "Spjäll stänger",
    "Spjäll öppnar",
    "Nödstoppar / ESD",
    "Larm aktiveras (ingen automatisk åtgärd)",
]

# Maps secondary effect description → component type for secondary marker
_INSTR_SEC_COMP_TYPES = {
    "Pump stoppar":           "Pump",
    "Kompressor stoppar":     "Kompressor",
    "Reglerventil stänger":   "Ventil",
    "Reglerventil öppnar":    "Ventil",
    "Spjäll stänger":         "Ventil",
    "Spjäll öppnar":          "Ventil",
}

# Simultaneous word-swap table for inverting cause descriptions.
# Sorted longest-first in the regex so e.g. 'stoppar' matches before 'stopp'.
_INVERSION_MAP = {
    # ── High / low ─────────────────────────────────────────────────────────────
    'högt': 'lågt',        'lågt': 'högt',
    'Högt': 'Lågt',        'Lågt': 'Högt',
    'hög': 'låg',          'låg': 'hög',
    'Hög': 'Låg',          'Låg': 'Hög',
    # ── Open / closed — verb (present tense) ────────────────────────────────────
    'stänger': 'öppnar',   'öppnar': 'stänger',
    'Stänger': 'Öppnar',   'Öppnar': 'Stänger',
    # ── Open / closed — adjective common gender ──────────────────────────────────
    'stängd': 'öppen',     'öppen': 'stängd',
    'Stängd': 'Öppen',     'Öppen': 'Stängd',
    # ── Open / closed — adjective neuter gender ──────────────────────────────────
    'stängt': 'öppet',     'öppet': 'stängt',
    'Stängt': 'Öppet',     'Öppet': 'Stängt',
    # ── Open / closed — past participle ("öppnat") ──────────────────────────────
    'öppnat': 'stängt',    'Öppnat': 'Stängt',
    # ── Open / closed — noun ────────────────────────────────────────────────────
    'stängning': 'öppning', 'öppning': 'stängning',
    'Stängning': 'Öppning', 'Öppning': 'Stängning',
    # ── Open / closed — English (fail-open / fail-closed) ───────────────────────
    'closed': 'open',      'open': 'closed',
    # ── Stop / start — verb (present tense) ─────────────────────────────────────
    'stoppar': 'startar',  'startar': 'stoppar',
    'Stoppar': 'Startar',  'Startar': 'Stoppar',
    # ── Stop / start — noun / compound ──────────────────────────────────────────
    'stopp': 'start',      'start': 'stopp',
    'Stopp': 'Start',      'Start': 'Stopp',
}
# Sort keys longest-first so longer tokens (e.g. 'stoppar') match before
# their shorter prefixes (e.g. 'stopp') in the alternation.
_INVERSION_RE = re.compile(
    '|'.join(re.escape(k) for k in sorted(_INVERSION_MAP, key=len, reverse=True))
)


def invert_cause_text(text):
    """Swap directional Swedish words for the inverse deviation.

    Returns the original string unchanged if no invertible words were found
    (caller can compare result == text to detect 'no inverse').
    """
    return _INVERSION_RE.sub(lambda m: _INVERSION_MAP[m.group(0)], text)


class TemplateCausePickerDialog(QDialog):
    """Shown after placing a cause marker — pick component, tag and cause from template.

    Cause list filters dynamically when the user changes component type.
    For Instrument / Sensor type an extra 'secondary effect' section is shown
    so the user can capture the full chain: instrument fails → valve/pump reacts.
    """

    def __init__(self, deviation_name, standard_causes,
                 component_types=None, suggested_tag='', preselect_type='', parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Orsak — {deviation_name}")
        self.setMinimumWidth(460)
        self._all_causes        = list(standard_causes)   # full list, each row has comp_type field
        self._std_rbs           = []                       # dynamically created primary radio buttons
        self._chosen            = None
        self._chosen_std_freq      = None   # frequency from chosen standard cause
        self._chosen_std_cause_id  = None   # id in standard_causes table
        self._comp_type         = ''
        self._comp_tag          = ''
        self._wants_secondary   = False
        self._chosen_secondary  = ''    # secondary effect text (e.g. "Pump stoppar (P-101)")
        self._sec_comp_type_out = ''    # comp_type for secondary marker

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>Avvikelse:</b> {deviation_name}"))

        # ── Component info ────────────────────────────────────────────────────
        form = QFormLayout()
        self._type_combo = QComboBox()
        self._type_combo.addItem("")
        for ct in (component_types or []):
            self._type_combo.addItem(ct)
        form.addRow("Komponenttyp:", self._type_combo)

        self._tag_edit = QLineEdit(suggested_tag)
        self._tag_edit.setPlaceholderText("t.ex. XV-101")
        form.addRow("Komponent-ID:", self._tag_edit)
        layout.addLayout(form)

        # ── Cause list (dynamic) ──────────────────────────────────────────────
        self._cause_header = QLabel("Välj standardorsak eller ange fritext:")
        layout.addWidget(self._cause_header)

        self._cause_container = QWidget()
        self._cause_layout = QVBoxLayout(self._cause_container)
        self._cause_layout.setContentsMargins(0, 0, 0, 0)
        self._cause_layout.setSpacing(3)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._cause_container)
        scroll.setMaximumHeight(180)
        layout.addWidget(scroll)

        # ── Primary button group (radio buttons added dynamically + free text) ─
        self._group = QButtonGroup(self)

        self._rb_free = QRadioButton("Annan:")
        self._rb_free.setProperty('cause_desc', None)
        self._group.addButton(self._rb_free, 9999)
        layout.addWidget(self._rb_free)
        self._free_edit = QLineEdit()
        self._free_edit.setPlaceholderText("Fritext orsak…")
        self._free_edit.textChanged.connect(lambda: self._rb_free.setChecked(True))
        layout.addWidget(self._free_edit)

        # ── Instrument secondary section (hidden unless Instrument type) ───────
        self._instr_group = QGroupBox("Sekundär verkan (vad händer som följd av instrumentfelet?)")
        instr_layout = QVBoxLayout(self._instr_group)
        instr_layout.setSpacing(3)
        self._sec_group = QButtonGroup(self)
        for i, eff in enumerate(_INSTR_SECONDARY_EFFECTS):
            rb = QRadioButton(eff)
            rb.setProperty('sec_desc', eff)
            self._sec_group.addButton(rb, i)
            instr_layout.addWidget(rb)
            if i == 0:
                rb.setChecked(True)
        rb_sec_free = QRadioButton("Annan sekundär verkan:")
        rb_sec_free.setProperty('sec_desc', None)
        self._sec_group.addButton(rb_sec_free, len(_INSTR_SECONDARY_EFFECTS))
        instr_layout.addWidget(rb_sec_free)
        self._sec_free_edit = QLineEdit()
        self._sec_free_edit.setPlaceholderText("t.ex. Reglerventil XV-201 stänger")
        self._sec_free_edit.textChanged.connect(lambda: rb_sec_free.setChecked(True))
        instr_layout.addWidget(self._sec_free_edit)

        sec_form = QFormLayout()
        self._sec_tag_edit = QLineEdit()
        self._sec_tag_edit.setPlaceholderText("t.ex. P-101  (valfri)")
        sec_form.addRow("Sekundär komponent-ID:", self._sec_tag_edit)
        instr_layout.addLayout(sec_form)

        mark_btn = QPushButton("📍 Markera objekt på P&ID")
        mark_btn.setToolTip(
            "Spara orsaken och gå direkt till P&ID för att klicka på sekundärkomponenten")
        mark_btn.setStyleSheet(
            "QPushButton{background:#6c3483;color:white;border:none;"
            "border-radius:4px;padding:5px 10px;font-weight:bold;}"
            "QPushButton:hover{background:#8e44ad;}")
        mark_btn.clicked.connect(self._accept_with_secondary)
        instr_layout.addWidget(mark_btn)

        self._instr_group.setVisible(False)
        layout.addWidget(self._instr_group)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        # ── Wire up type combo and initial render ─────────────────────────────
        self._type_combo.currentTextChanged.connect(self._on_type_changed)
        if preselect_type and preselect_type in (component_types or []):
            self._type_combo.setCurrentText(preselect_type)
        else:
            self._update_cause_list('')

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _on_type_changed(self, comp_type):
        self._update_cause_list(comp_type)
        is_instrument = 'Instrument' in comp_type
        self._instr_group.setVisible(is_instrument)

    def _update_cause_list(self, comp_type):
        # Remove old standard radio buttons from group and layout
        for rb in self._std_rbs:
            self._group.removeButton(rb)
        self._std_rbs = []
        while self._cause_layout.count():
            item = self._cause_layout.takeAt(0)
            if item and item.widget():
                item.widget().hide()
                item.widget().setParent(None)

        # Filter: only show causes marked for use in cause form.
        # When a component type is selected, prefer type-specific causes; fall back to generic.
        active = [c for c in self._all_causes if dict(c).get('use_in_cause_form', 1)]
        if comp_type:
            filtered = [c for c in active if dict(c).get('comp_type', '') == comp_type]
            if not filtered:
                filtered = [c for c in active if not dict(c).get('comp_type', '')]
        else:
            filtered = [c for c in active if not dict(c).get('comp_type', '')]

        first_rb = None
        for i, c in enumerate(filtered):
            freq = dict(c).get('frequency')
            label = c['description']
            if freq is not None:
                label += f"  ({freq:g}/år)"
            rb = QRadioButton(label)
            rb.setProperty('cause_desc', c['description'])
            rb.setProperty('cause_freq', freq)
            rb.setProperty('cause_id',   dict(c).get('id'))
            self._group.addButton(rb, i)
            self._cause_layout.addWidget(rb)
            self._std_rbs.append(rb)
            if first_rb is None:
                first_rb = rb

        if first_rb:
            first_rb.setChecked(True)
        else:
            self._rb_free.setChecked(True)

        if comp_type:
            self._cause_header.setText(f"Välj orsak för <b>{comp_type}</b>:")
        else:
            self._cause_header.setText("Välj standardorsak eller ange fritext:")

    # ── Accept ────────────────────────────────────────────────────────────────

    def _accept_with_secondary(self):
        self._wants_secondary = True
        self._accept()

    def _accept(self):
        btn = self._group.checkedButton()
        if btn is None:
            self.reject()
            return
        desc = btn.property('cause_desc')
        if desc is None:
            desc = self._free_edit.text().strip()
        if not desc:
            QMessageBox.warning(self, "Tom orsak", "Ange en orsak.")
            return

        # Instrument secondary: build combined description + store secondary info
        comp_type = self._type_combo.currentText().strip()
        if 'Instrument' in comp_type and self._instr_group.isVisible():
            sec_btn = self._sec_group.checkedButton()
            if sec_btn:
                sec_desc = sec_btn.property('sec_desc')
                if sec_desc is None:
                    sec_desc = self._sec_free_edit.text().strip()
                if sec_desc:
                    sec_tag = self._sec_tag_edit.text().strip()
                    suffix = f" ({sec_tag})" if sec_tag else ""
                    self._chosen_secondary  = f"{sec_desc}{suffix}"
                    self._sec_comp_type_out = _INSTR_SEC_COMP_TYPES.get(sec_desc, '')
                    desc = f"{desc} → {self._chosen_secondary}"

        self._chosen    = desc
        self._comp_type = comp_type
        self._comp_tag  = self._tag_edit.text().strip()
        # Frequency from standard cause (None for free-text entries)
        btn2 = self._group.checkedButton()
        raw_desc = btn2.property('cause_desc') if btn2 else None
        self._chosen_std_freq     = btn2.property('cause_freq') if (btn2 and raw_desc is not None) else None
        self._chosen_std_cause_id = btn2.property('cause_id')   if (btn2 and raw_desc is not None) else None
        self.accept()

    @property
    def chosen_description(self):
        return self._chosen

    @property
    def chosen_std_cause_freq(self):
        return self._chosen_std_freq

    @property
    def chosen_std_cause_id(self):
        return self._chosen_std_cause_id

    @property
    def component_type(self):
        return self._comp_type

    @property
    def component_tag(self):
        return self._comp_tag

    @property
    def wants_secondary_placement(self):
        return self._wants_secondary

    @property
    def secondary_description(self):
        """Short text for the secondary effect, e.g. 'Pump stoppar (P-101)'."""
        return self._chosen_secondary

    @property
    def secondary_comp_type(self):
        """Component type for the secondary marker, e.g. 'Pump'."""
        return self._sec_comp_type_out

    @property
    def secondary_component_tag(self):
        return self._sec_tag_edit.text().strip()


class PIDPanel(QWidget):
    node_created            = pyqtSignal(int)
    cause_created           = pyqtSignal(int)
    cause_template_created  = pyqtSignal(int)
    consequence_created     = pyqtSignal(int)
    safeguard_created       = pyqtSignal(int)
    existing_marker_placed  = pyqtSignal(str, int)
    risk_scenario_requested = pyqtSignal(int, object, int)
    marker_navigated        = pyqtSignal(str, int)
    pid_analysis_done       = pyqtSignal()
    # Emitted when user clicks P&ID in cause-template mode;
    # MainWindow shows CauseObjectPopup then calls place_cause_from_template()
    cause_placement_requested = pyqtSignal(int, str, str, object, int, str)
    # (deviation_id, suggested_tag, detected_comp_type, scene_pos, page)
    # Node markup signals
    markup_draw_finished    = pyqtSignal(str, int, list, int, str)  # type_, node_id, pts, page, label
    markup_item_selected    = pyqtSignal(int)                        # markup_id
    markup_moved            = pyqtSignal(int, list)                  # mu_id, new PDF pts
    markup_label_edited     = pyqtSignal(int, str)                   # mu_id, new_label
    markup_duplicate_requested = pyqtSignal(int)                     # mu_id
    # Red markup signals
    red_markup_draw_finished = pyqtSignal(str, int, list, int, str)  # type_, node_id, pts, page, label/symbol_id
    red_markup_item_selected = pyqtSignal(int)                        # mu_id
    red_markup_moved         = pyqtSignal(int, list)                  # mu_id, new PDF pts
    markup_symbol_dims_changed = pyqtSignal(int, float, float, float)  # mu_id, w, h, rot_deg
    board_layout_changed = pyqtSignal(str)

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        self._pen_color             = QColor(255, 140, 0)
        self._active_node_id              = None
        self._active_cause_id             = None
        self._active_consequence_id       = None
        self._active_deviation_id         = None   # set during MODE_CAUSE_TEMPLATE
        self._active_markup_class         = 'node' # 'node' or 'red'
        self._active_symbol_id            = None   # set when red markup symbol tool selected
        self._pending_markup_pts          = None
        self._pending_markup_page         = None
        self._pending_secondary_cause_id    = None   # set to queue secondary marker after instrument cause
        self._pending_secondary_comp_type  = ''
        self._pending_secondary_tag        = ''
        self._pending_secondary_deviation_id   = None   # for re-opening dialog after secondary placement
        self._pending_secondary_preselect_type = ''
        self._current_display_page  = 0
        self._smart_layout_prev      = None   # {page: (ox, oy)} for undo
        self._analyzer_thread        = None
        self._analyzer_progress_dlg  = None
        self._sheet_map: dict       = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout(); bar.setSpacing(4)

        self.open_btn = QPushButton("📂 Importera P&ID")
        self.open_btn.clicked.connect(self._import_pdf)
        bar.addWidget(self.open_btn)

        self.analyze_btn = QPushButton("📋 Analysera P&ID")
        self.analyze_btn.setToolTip(
            "Skannar hela P&ID:n, identifierar alla taggnummer-prefix\n"
            "och skapar en nyckel i Inställningar → Identifierade objekt.")
        self.analyze_btn.clicked.connect(self._analyze_pid)
        self.analyze_btn.setEnabled(False)
        bar.addWidget(self.analyze_btn)

        self.export_btn = QPushButton("📤 Exportera PDF")
        self.export_btn.setToolTip(
            "Exportera P&ID med alla HAZOP-markeringar (nodgränser, orsaker,\n"
            "konsekvenser, barriärer och kopplingslinjer) som en ny PDF-fil.")
        self.export_btn.clicked.connect(self._export_pdf)
        self.export_btn.setEnabled(False)
        bar.addWidget(self.export_btn)

        bar.addWidget(_vline())

        self.prev_btn = QPushButton("◀")
        self.prev_btn.setFixedWidth(28)
        self.prev_btn.clicked.connect(lambda: self._goto_page(self._current_display_page - 1))
        bar.addWidget(self.prev_btn)

        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setValue(1)
        self.page_spin.setFixedWidth(48)
        self.page_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.page_spin.setToolTip("Skriv sidnummer och tryck Enter för att navigera")
        self.page_spin.editingFinished.connect(self._on_page_spin_changed)
        bar.addWidget(self.page_spin)

        self.page_total_label = QLabel("/ —")
        self.page_total_label.setMinimumWidth(35)
        bar.addWidget(self.page_total_label)

        self.next_btn = QPushButton("▶")
        self.next_btn.setFixedWidth(28)
        self.next_btn.clicked.connect(lambda: self._goto_page(self._current_display_page + 1))
        bar.addWidget(self.next_btn)

        bar.addWidget(_vline())

        self.mode_buttons = {}
        mode_defs = [
            (MODE_NAV,         "🔍 Navigera"),
            (MODE_NODE,        "✏️ Nodgräns"),
            (MODE_CAUSE,       "⚙️ Orsak"),
            (MODE_CONSEQUENCE, "⚠️ Konsekvens"),
            (MODE_SAFEGUARD,   "🛡️ Safeguard"),
        ]
        for mode, label in mode_defs:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked, m=mode: self._set_mode(m))
            bar.addWidget(btn)
            self.mode_buttons[mode] = btn

        bar.addWidget(_vline())

        self.style_widget = QWidget()
        sl = QHBoxLayout(self.style_widget)
        sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(4)

        sl.addWidget(QLabel("Tjocklek:"))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 15); self.width_spin.setValue(3)
        self.width_spin.valueChanged.connect(self._update_pen)
        sl.addWidget(self.width_spin)

        sl.addWidget(QLabel("Transparens:"))
        self.alpha_slider = QSlider(Qt.Orientation.Horizontal)
        self.alpha_slider.setRange(20, 255); self.alpha_slider.setValue(220)
        self.alpha_slider.setFixedWidth(80)
        self.alpha_slider.valueChanged.connect(self._update_pen)
        sl.addWidget(self.alpha_slider)

        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(28, 28)
        self.color_btn.clicked.connect(self._pick_color)
        self._refresh_color_btn()
        sl.addWidget(self.color_btn)

        self.create_node_btn = QPushButton("✅ Skapa Nod")
        self.create_node_btn.setEnabled(False)
        self.create_node_btn.clicked.connect(self._create_node_from_markup)
        sl.addWidget(self.create_node_btn)

        self.style_widget.setVisible(False)
        bar.addWidget(self.style_widget)
        bar.addWidget(_vline())
        self.layout_btn = QPushButton("📐 Layout")
        self.layout_btn.setCheckable(True)
        self.layout_btn.setToolTip("Dra ritningsbladen för att ordna om dem")
        self.layout_btn.toggled.connect(self._on_layout_mode_toggled)
        bar.addWidget(self.layout_btn)

        self.smart_btn = QPushButton("✨ Smart layout")
        self.smart_btn.setToolTip(
            "Analyserar off-page connectors och föreslår optimal bladlayout (max 15 s)")
        self.smart_btn.clicked.connect(self._run_smart_layout)
        bar.addWidget(self.smart_btn)

        bar.addStretch()
        layout.addLayout(bar)

        # ── Scenario guided-mode banner ───────────────────────────────────────
        self._scenario_active = False
        self._scenario_step   = 0   # 1=cause 2=consequence 3+=safeguard

        self._scenario_banner = QFrame()
        self._scenario_banner.setStyleSheet(
            "QFrame{background:#1F4E79; border-radius:4px; padding:2px;}")
        self._scenario_banner.setFixedHeight(46)
        sb_lay = QVBoxLayout(self._scenario_banner)
        sb_lay.setContentsMargins(8, 2, 8, 2)
        sb_lay.setSpacing(2)

        # Top row: step pills
        pill_row = QHBoxLayout(); pill_row.setSpacing(4)
        self._step_pills = []
        _PILL_BASE = "border-radius:3px; padding:1px 6px; font-size:10px; font-weight:bold;"
        for txt in ["1 ⚙️ Orsak", "2 ⚠️ Konsekvens", "3 🛡️ Safeguard"]:
            lbl = QLabel(txt)
            lbl.setStyleSheet(_PILL_BASE + "background:#3a6fa3; color:#aac;")
            pill_row.addWidget(lbl)
            self._step_pills.append(lbl)
            if txt != "3 🛡️ Safeguard":
                pill_row.addWidget(QLabel("→").setStyleSheet and QLabel("→"))
                # (arrow is just cosmetic)
        pill_row.addStretch()

        self._sc_abort_btn = QPushButton("✕ Avbryt")
        self._sc_abort_btn.setFixedHeight(20)
        self._sc_abort_btn.setStyleSheet(
            "background:#c0392b; color:white; border:none; border-radius:3px; padding:0 8px;")
        self._sc_abort_btn.clicked.connect(self._scenario_abort)
        pill_row.addWidget(self._sc_abort_btn)
        sb_lay.addLayout(pill_row)

        # Bottom row: instruction + action buttons
        act_row = QHBoxLayout(); act_row.setSpacing(6)
        self._sc_instr = QLabel("")
        self._sc_instr.setStyleSheet("color:white; font-size:11px;")
        act_row.addWidget(self._sc_instr)
        act_row.addStretch()

        self._sc_add_sg_btn = QPushButton("+ Fler safeguards")
        self._sc_add_sg_btn.setFixedHeight(20)
        self._sc_add_sg_btn.setStyleSheet(
            "background:#27ae60; color:white; border:none; border-radius:3px; padding:0 8px;")
        self._sc_add_sg_btn.setVisible(False)
        self._sc_add_sg_btn.clicked.connect(
            lambda: (self._set_mode(MODE_SAFEGUARD),
                     self._sc_instr.setText("Klicka på nästa safeguard på P&ID:n")))
        act_row.addWidget(self._sc_add_sg_btn)

        self._sc_finish_btn = QPushButton("✓ Slutför")
        self._sc_finish_btn.setFixedHeight(20)
        self._sc_finish_btn.setStyleSheet(
            "background:#2ecc71; color:white; border:none; border-radius:3px; padding:0 8px; font-weight:bold;")
        self._sc_finish_btn.setVisible(False)
        self._sc_finish_btn.clicked.connect(self._scenario_finish)
        act_row.addWidget(self._sc_finish_btn)
        sb_lay.addLayout(act_row)

        self._scenario_banner.setVisible(False)
        layout.addWidget(self._scenario_banner)

        # ── Secondary placement banner ─────────────────────────────────────────
        self._secondary_banner = QFrame()
        self._secondary_banner.setStyleSheet(
            "QFrame{background:#6c3483; border-radius:4px; padding:2px;}")
        self._secondary_banner.setFixedHeight(34)
        sb2_lay = QHBoxLayout(self._secondary_banner)
        sb2_lay.setContentsMargins(8, 4, 8, 4)
        self._secondary_lbl = QLabel("")
        self._secondary_lbl.setStyleSheet("color:white; font-size:11px; font-weight:bold;")
        sb2_lay.addWidget(self._secondary_lbl)
        sb2_lay.addStretch()
        sb2_cancel = QPushButton("✕ Avbryt")
        sb2_cancel.setFixedHeight(20)
        sb2_cancel.setStyleSheet(
            "background:#c0392b; color:white; border:none; border-radius:3px; padding:0 8px;")
        sb2_cancel.clicked.connect(self._cancel_secondary_placement)
        sb2_lay.addWidget(sb2_cancel)
        self._secondary_banner.setVisible(False)
        layout.addWidget(self._secondary_banner)

        # ── Viewer ────────────────────────────────────────────────────────────
        self.viewer = PIDGraphicsView()
        self.viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.viewer.node_markup_finished.connect(self._on_markup_finished)
        self.viewer.cause_clicked.connect(self._on_cause_click)
        self.viewer.consequence_clicked.connect(self._on_consequence_click)
        self.viewer.safeguard_clicked.connect(self._on_safeguard_click)
        self.viewer.place_existing_clicked.connect(self._on_place_existing_click)
        self.viewer.cause_template_clicked.connect(self._on_cause_template_click)
        self.viewer.context_action.connect(self._on_context_action)
        self.viewer.zone_drawn.connect(self._on_zone_drawn)
        self.viewer.zone_resized.connect(self._on_zone_resized)
        self.viewer.cause_at_marker_requested.connect(self._on_add_cause_at_marker)
        self.viewer.consequence_at_marker_requested.connect(self._on_add_consequence_at_marker)
        self.viewer.safeguard_at_marker_requested.connect(self._on_add_safeguard_at_marker)
        self._active_place_type  = None   # 'cause' | 'consequence' | 'safeguard'
        self._active_place_id    = None
        self._pending_zone_pdf   = None   # QRectF while zone dialog chain is open
        self.viewer.marker_clicked.connect(
            lambda t, i: self.marker_navigated.emit(t, i))
        self.viewer.markup_moved.connect(self.markup_moved)
        self.viewer.markup_label_edited.connect(self.markup_label_edited)
        self.viewer.markup_duplicate_requested.connect(self.markup_duplicate_requested)
        self.viewer.markup_symbol_dims_changed.connect(self.markup_symbol_dims_changed)
        self.viewer.board_layout_changed.connect(self.board_layout_changed)
        self.viewer.board_layout_changed.connect(lambda _: self._load_overlays())
        self.viewer.sheet_conn_break_requested.connect(self._break_sheet_link)
        self.viewer.sheet_conn_add_requested.connect(self._add_sheet_link)

        # Connect existing signals for scenario auto-progression
        self.cause_created.connect(self._sc_on_cause)
        self.cause_template_created.connect(self._sc_on_cause)   # template flow also advances
        self.consequence_created.connect(self._sc_on_consequence)
        self.safeguard_created.connect(self._sc_on_safeguard)

        layout.addWidget(self.viewer)

        self._set_mode(MODE_NAV)
        self._update_pen()

    def _analyze_pid(self):
        """Scan all PDF pages, collect unique tag prefixes, cross-ref with database."""
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Ingen P&ID", "Öppna en P&ID-fil först.")
            return

        # Offer OCR auto-install — needed for vector-only P&IDs
        st = ocr_status()
        if not st['tesseract'] and not st['easyocr']:
            ensure_ocr_available(self)

        doc = self.viewer.pdf_doc
        n   = doc.page_count
        progress = QProgressDialog("Analyserar P&ID…", "Avbryt", 0, n, self)
        progress.setWindowTitle("Analyserar")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()

        found = {}   # prefix → set of full tags
        for pg in range(n):
            if progress.wasCanceled():
                break
            progress.setValue(pg)
            progress.setLabelText(f"Sida {pg+1}/{n}…")
            QApplication.processEvents()
            page = doc.load_page(pg)
            raw_words = page.get_text("words")
            # Use spatial combining to catch extended tags (20-PCV-101, K2.FT.201)
            seen_in_page = set()
            for candidate in _spatial_combine(raw_words, gap_limit=22.0):
                tag = _pick_best_tag(candidate)
                if not tag or tag in seen_in_page:
                    continue
                seen_in_page.add(tag)
                pfx = _equip_prefix_from_tag(tag)
                if pfx:
                    found.setdefault(pfx, set()).add(tag)
        progress.setValue(n); progress.close()

        if not found:
            QMessageBox.information(self, "Inga taggar",
                "Inga taggnummer hittades i P&ID:n."); return

        # Cross-reference with tag database and symbol knowledge
        _cat_map = {
            'instrument': 'Instrument / Sensor', 'givare': 'Instrument / Sensor',
            'reglerfunktion': 'Instrument / Sensor', 'larm': 'Instrument / Sensor',
            'ventil': 'Ventil', 'reglerventil': 'Ventil',
            'pump': 'Pump', 'kompressor': 'Kompressor',
            'tank': 'Tank / Kärl', 'kärl': 'Tank / Kärl',
            'värmeväxlare': 'Värmeväxlare',
            'säkerhetsventil': 'Säkerhetsventil (PSV)',
        }
        for pfx, tags in found.items():
            examples  = ', '.join(sorted(tags)[:6])
            db_entry  = self.db.tag_code_lookup(pfx) if hasattr(self.db, 'tag_code_lookup') else {}
            name_sv   = (db_entry or {}).get('name_sv', '')
            comp_type = ''
            if db_entry:
                cat = str(db_entry.get('category', '')).lower()
                for k, v in _cat_map.items():
                    if k in cat:
                        comp_type = v; break
            if not comp_type and pfx in KNOWN_PREFIXES:
                comp_type = KNOWN_PREFIXES[pfx][1]
            self.db.upsert_pid_tag(pfx, examples, name_sv, comp_type)

        QMessageBox.information(self, "Analys klar ✅",
            f"Hittade {len(found)} unika prefix.\n\n"
            "Öppna Inställningar → Identifierade objekt\n"
            "för att bekräfta typerna och aktivera 'Använd'.")
        self.pid_analysis_done.emit()

    def _refresh_color_btn(self):
        c = self._pen_color
        self.color_btn.setStyleSheet(
            f"background:{c.name()}; border:1px solid #555; border-radius:3px;")

    def _pick_color(self):
        c = QColorDialog.getColor(self._pen_color, self, "Välj färg")
        if c.isValid():
            self._pen_color = c
            self._refresh_color_btn()
            self._update_pen()

    def _working_pdf_path(self):
        """Returns the project-local working copy path, e.g. hazop_project_pid.pdf."""
        db_path = Path(self.db.path)
        return db_path.with_name(db_path.stem + '_pid.pdf')

    def _rebuild_sheet_map(self):
        sheets = self.db.get_sheets()
        self._sheet_map = {i: int(s['physical_page']) for i, s in enumerate(sheets)}

    def _export_pdf(self):
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Export", "Öppna ett P&ID-dokument först.")
            return
        working = self._working_pdf_path()
        if not working.exists():
            QMessageBox.warning(self, "Export", "Ingen P&ID-fil att exportera.")
            return

        out_path, _ = QFileDialog.getSaveFileName(
            self, "Exportera P&ID med markup", "", "PDF-dokument (*.pdf)")
        if not out_path:
            return
        if not out_path.lower().endswith('.pdf'):
            out_path += '.pdf'

        sheets = self.db.get_sheets()
        page_order = ([int(s['physical_page']) for s in sheets]
                      if sheets else list(range(self.viewer.page_count())))

        prog = QProgressDialog("Exporterar P&ID…", None, 0, len(page_order), self)
        prog.setWindowTitle("Export")
        prog.setMinimumDuration(0)
        prog.setValue(0)
        QApplication.processEvents()

        try:
            src_doc = fitz.open(str(working))
        except Exception as e:
            prog.close()
            QMessageBox.critical(self, "Export misslyckades",
                                 f"Kunde inte öppna PDF:\n{e}")
            return

        out_doc = fitz.open()
        for phys in page_order:
            out_doc.insert_pdf(src_doc, from_page=phys, to_page=phys)

        for out_idx, phys_page in enumerate(page_order):
            prog.setValue(out_idx)
            QApplication.processEvents()
            page = out_doc.load_page(out_idx)

            # ── Node markup polygons ──────────────────────────────────────
            for node in self.db.nodes():
                nd = dict(node)
                if int(nd.get('pid_page', 0) or 0) != phys_page:
                    continue
                raw_pts = nd.get('markup_points', '') or ''
                if not raw_pts:
                    continue
                try:
                    pts = [fitz.Point(float(p[0]), float(p[1]))
                           for p in json.loads(raw_pts)]
                    style = json.loads(nd.get('markup_style', '') or '{}')
                except Exception:
                    continue
                if len(pts) < 2:
                    continue
                color = _hex_to_fitz_rgb(style.get('color', '#ff8c00'))
                width = max(0.5, style.get('width', 2) * 0.4)
                alpha = style.get('alpha', 120) / 255
                close = len(pts) >= 3
                shape = page.new_shape()
                shape.draw_polyline(pts + [pts[0]] if close else pts)
                try:
                    shape.finish(color=color, width=width,
                                 fill=color, fill_opacity=alpha * 0.35)
                except TypeError:
                    shape.finish(color=color, width=width)
                name = nd.get('name', '')
                if name and pts:
                    cx = sum(p.x for p in pts) / len(pts)
                    cy = sum(p.y for p in pts) / len(pts)
                    try:
                        shape.insert_text(
                            fitz.Point(cx - len(name) * 2.5, cy + 3.5),
                            name, fontsize=8, color=color, fontname='helv')
                    except Exception:
                        pass
                shape.commit()

            # ── Node markup overlays as editable PDF annotations ──────────
            if hasattr(self.db, 'node_markups_for_page'):
                node_ocgs = {}  # node_id -> OCG xref (one layer per node)

                for mu in self.db.node_markups_for_page(phys_page):
                    m = dict(mu)
                    if not m.get('visible', 1):
                        continue

                    # ── OCG: one layer per node, named after the node ─────
                    node_id = m.get('node_id')
                    if node_id not in node_ocgs:
                        node_row = (self.db.get_node(node_id)
                                    if hasattr(self.db, 'get_node') else None)
                        nname = (dict(node_row)['name'] if node_row
                                 else f'Nod {node_id}')
                        try:
                            node_ocgs[node_id] = out_doc.add_ocg(nname, on=True)
                        except Exception:
                            node_ocgs[node_id] = None
                    ocg_xref = node_ocgs[node_id]

                    # ── Parse geometry & style ─────────────────────────────
                    try:
                        pts_raw = json.loads(m.get('points', '[]') or '[]')
                        pts = [fitz.Point(float(p[0]), float(p[1]))
                               for p in pts_raw]
                    except Exception:
                        pts = []

                    rgb      = _hex_to_fitz_rgb(m.get('color', '#1565C0'))
                    opacity  = float(m.get('opacity', 0.45))
                    width    = max(0.5, int(m.get('line_width', 12)) * 0.4)
                    font_sz  = max(6, int(m.get('font_size', 12)))
                    mu_type  = m.get('type', 'polygon')
                    label    = m.get('label', '') or ''
                    # Light fill: blend stroke colour with white at 30%
                    fill_rgb = tuple(min(1.0, 0.70 + 0.30 * c) for c in rgb)

                    annot = None
                    try:
                        if mu_type == 'polygon' and len(pts) >= 2:
                            annot = page.add_polygon_annot(pts)
                            annot.set_colors({"stroke": list(rgb),
                                              "fill":   list(fill_rgb)})
                            annot.set_border({"width": width})
                            if label:
                                annot.set_info(title=label, content=label)
                            annot.update(opacity=opacity)

                        elif mu_type == 'polyline' and len(pts) >= 2:
                            annot = page.add_polyline_annot(pts)
                            annot.set_colors({"stroke": list(rgb)})
                            annot.set_border({"width": width})
                            if label:
                                annot.set_info(title=label)
                            annot.update(opacity=opacity)

                        elif mu_type in ('text', 'comment') and pts:
                            txt = label or '?'
                            x, y = pts[0].x, pts[0].y
                            rect_w = len(txt) * font_sz * 0.58 + 8
                            rect_h = font_sz * 1.7
                            rect   = fitz.Rect(x, y - rect_h,
                                               x + rect_w, y + 2)
                            bg = ([1.0, 1.0, 0.82] if mu_type == 'comment'
                                  else list(fill_rgb))
                            annot = page.add_freetext_annot(
                                rect, txt,
                                fontsize=font_sz,
                                fontname='helv',
                                text_color=list(rgb),
                                fill_color=bg)
                            annot.set_info(
                                title=('Kommentar' if mu_type == 'comment'
                                       else 'Nodnamn'),
                                content=txt)
                            annot.update(opacity=opacity)

                    except Exception:
                        pass  # skip faulty item; never crash the export

                    if annot is not None and ocg_xref:
                        try:
                            annot.set_oc(ocg_xref)
                        except Exception:
                            pass

            # ── Cause markers ─────────────────────────────────────────────
            cause_pos = {}
            for m in self.db.cause_markers_for_page(phys_page):
                md = dict(m)
                x, y = float(md['x']), float(md['y'])
                cause_pos[md['cause_id']] = (x, y)
                cause  = self.db.get_cause(md['cause_id'])
                desc   = dict(cause).get('description', '') if cause else ''
                tag    = md.get('component_tag', '') or md.get('component_type', '')
                _draw_pid_marker(page, x, y, (0.75, 0.18, 0.09), 'C', tag or desc)

            # ── Consequence markers ───────────────────────────────────────
            cons_pos = {}
            for m in self.db.consequence_markers_for_page(phys_page):
                md = dict(m)
                x, y = float(md['x']), float(md['y'])
                cons_pos[md['consequence_id']] = (x, y)
                cons = self.db.get_consequence(md['consequence_id'])
                desc = dict(cons).get('description', '') if cons else ''
                _draw_pid_marker(page, x, y, (0.87, 0.42, 0.06), 'K', desc)

            # ── Safeguard markers ─────────────────────────────────────────
            sg_pos = {}
            for m in self.db.safeguard_markers_for_page(phys_page):
                md = dict(m)
                x, y = float(md['x']), float(md['y'])
                sg_pos[md['safeguard_id']] = (x, y)
                row = self.db.conn.execute(
                    "SELECT description FROM safeguards WHERE id=?",
                    (md['safeguard_id'],)).fetchone()
                desc = row['description'] if row else ''
                tag  = md.get('tag', '')
                _draw_pid_marker(page, x, y, (0.15, 0.62, 0.27), 'S', tag or desc)

            # ── Connection lines ──────────────────────────────────────────
            shape = page.new_shape()
            for cid, cpos in cons_pos.items():
                c = self.db.get_consequence(cid)
                if c and c['cause_id'] in cause_pos:
                    shape.draw_line(fitz.Point(*cause_pos[c['cause_id']]),
                                    fitz.Point(*cpos))
                    shape.finish(color=(0.75, 0.18, 0.09), width=0.8)
            for sid, spos in sg_pos.items():
                s = self.db.get_safeguard(sid)
                if s and s['consequence_id'] in cons_pos:
                    shape.draw_line(fitz.Point(*cons_pos[s['consequence_id']]),
                                    fitz.Point(*spos))
                    try:
                        shape.finish(color=(0.15, 0.62, 0.27), width=0.8,
                                     dashes="[3 3] 0")
                    except TypeError:
                        shape.finish(color=(0.15, 0.62, 0.27), width=0.8)
            shape.commit()

        src_doc.close()
        prog.setValue(len(page_order))
        QApplication.processEvents()

        try:
            out_doc.save(out_path, garbage=4, deflate=True)
            out_doc.close()
            prog.close()
            QMessageBox.information(self, "Export klar",
                                    f"P&ID exporterat med markup till:\n{out_path}")
        except Exception as e:
            out_doc.close()
            prog.close()
            QMessageBox.critical(self, "Export misslyckades",
                                 f"Kunde inte spara PDF:\n{e}")

    def _import_pdf(self):
        if not HAS_PYMUPDF:
            QMessageBox.warning(self, "PyMuPDF saknas",
                "Installera med:\n    pip install PyMuPDF\nStarta sedan om.")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Importera P&ID", "", "PDF-dokument (*.pdf);;Alla filer (*.*)")
        if not paths:
            return
        paths = sorted(paths)   # alphabetical merge order

        working      = self._working_pdf_path()
        has_existing = working.exists()
        created_at   = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

        if has_existing:
            dlg = PIDImportDialog(has_existing=True, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            rev_label = dlg.label() or created_at
            rev_notes = dlg.notes()

            if dlg.is_new_revision():
                # Merge all selected files into one new PDF → replace working copy
                try:
                    base_doc = fitz.open(paths[0])
                    for p in paths[1:]:
                        ext = fitz.open(p)
                        base_doc.insert_pdf(ext)
                        ext.close()
                    total_pages = base_doc.page_count
                    if self.viewer.pdf_doc is not None:
                        try: self.viewer.pdf_doc.close()
                        except Exception: pass
                        self.viewer.pdf_doc = None
                    tmp_fd, tmp_path = tempfile.mkstemp(
                        suffix='.pdf', dir=str(working.parent))
                    os.close(tmp_fd)
                    base_doc.save(tmp_path, garbage=4, deflate=True)
                    base_doc.close()
                    shutil.move(tmp_path, str(working))
                except Exception as e:
                    QMessageBox.critical(self, "Fel", f"Kunde inte skapa PDF:\n{e}")
                    return
                prog = QProgressDialog(
                    f"Renderar P&ID ({total_pages} sidor)…", None, 0, total_pages, self)
                prog.setWindowTitle("Importerar P&ID")
                prog.setWindowModality(Qt.WindowModality.WindowModal)
                prog.setMinimumDuration(0)
                prog.setValue(0)
                QApplication.processEvents()
                if not self.viewer.load_pdf(
                        str(working), page=0,
                        progress_cb=lambda cur, tot: prog.setValue(cur)):
                    prog.close()
                    QMessageBox.warning(self, "Fel", "Kunde inte öppna PDF-filen.")
                    return
                prog.setAutoClose(False)
                prog.setValue(total_pages)
                self.db.set_pid_path(str(working))
                self.db.clear_sheets()
                self.db.add_revision(rev_label, rev_notes, str(working), created_at)
                self.db.ensure_sheets_initialized(self.viewer.page_count())
                self._current_display_page = 0

            else:
                # Append all selected files to the existing working PDF
                try:
                    existing_doc    = fitz.open(str(working))
                    existing_pg_cnt = existing_doc.page_count
                    n_new = 0
                    for p in paths:
                        ext = fitz.open(p)
                        n_new += ext.page_count
                        existing_doc.insert_pdf(ext)
                        ext.close()
                    total_pages = existing_pg_cnt + n_new
                    if self.viewer.pdf_doc is not None:
                        try: self.viewer.pdf_doc.close()
                        except Exception: pass
                        self.viewer.pdf_doc = None
                    tmp_fd, tmp_path = tempfile.mkstemp(
                        suffix='.pdf', dir=str(working.parent))
                    os.close(tmp_fd)
                    existing_doc.save(tmp_path, garbage=4, deflate=True)
                    existing_doc.close()
                    shutil.move(tmp_path, str(working))
                except Exception as e:
                    QMessageBox.critical(self, "Fel vid sammanslagning",
                                         f"Kunde inte sammanfoga PDF:\n{e}")
                    return

                prog = QProgressDialog(
                    f"Renderar P&ID ({total_pages} sidor)…", None, 0, total_pages, self)
                prog.setWindowTitle("Importerar P&ID")
                prog.setWindowModality(Qt.WindowModality.WindowModal)
                prog.setMinimumDuration(0)
                prog.setValue(0)
                QApplication.processEvents()
                keep_phys = self.viewer.current_page
                if not self.viewer.load_pdf(
                        str(working), page=keep_phys,
                        progress_cb=lambda cur, tot: prog.setValue(cur)):
                    prog.close()
                    QMessageBox.warning(self, "Fel", "Kunde inte öppna sammanfogad PDF.")
                    return
                prog.setAutoClose(False)
                prog.setValue(total_pages)

                if self.db.get_display_page_count() == 0:
                    self.db.ensure_sheets_initialized(existing_pg_cnt)

                rev_id = self.db.add_revision(rev_label, rev_notes, str(working), created_at)
                physical_pages = list(range(existing_pg_cnt, existing_pg_cnt + n_new))
                sheet_names    = [f"Blad {existing_pg_cnt + i + 1}"
                                  for i in range(n_new)]
                self.db.append_sheets(physical_pages, sheet_names, rev_id)

        else:
            # First import — merge all selected files into working copy
            try:
                base_doc = fitz.open(paths[0])
                for p in paths[1:]:
                    ext = fitz.open(p)
                    base_doc.insert_pdf(ext)
                    ext.close()
                total_pages = base_doc.page_count
                tmp_fd, tmp_path = tempfile.mkstemp(
                    suffix='.pdf', dir=str(working.parent))
                os.close(tmp_fd)
                base_doc.save(tmp_path, garbage=4, deflate=True)
                base_doc.close()
                shutil.move(tmp_path, str(working))
            except Exception as e:
                QMessageBox.critical(self, "Fel", f"Kunde inte kopiera PDF:\n{e}")
                return
            prog = QProgressDialog(
                f"Renderar P&ID ({total_pages} sidor)…", None, 0, total_pages, self)
            prog.setWindowTitle("Importerar P&ID")
            prog.setWindowModality(Qt.WindowModality.WindowModal)
            prog.setMinimumDuration(0)
            prog.setValue(0)
            QApplication.processEvents()
            if not self.viewer.load_pdf(
                    str(working), page=0,
                    progress_cb=lambda cur, tot: prog.setValue(cur)):
                prog.close()
                QMessageBox.warning(self, "Fel", "Kunde inte öppna PDF-filen.")
                return
            prog.setAutoClose(False)
            prog.setValue(total_pages)
            self.db.set_pid_path(str(working))
            self.db.clear_sheets()
            self.db.add_revision(created_at, '', str(working), created_at)
            self.db.ensure_sheets_initialized(self.viewer.page_count())
            self._current_display_page = 0

        # Phase 2: apply active-page filter if needed, then load markers/connections
        self._rebuild_sheet_map()
        self._update_page_label()
        sheets = self.db.get_sheets()
        active = sorted(int(s['physical_page']) for s in sheets) if sheets else None
        already = sorted(self.viewer._all_page_items.keys())
        prog.setMaximum(0)
        prog.setLabelText("Laddar markeringar…")
        QApplication.processEvents()
        if active != already:
            # Active-page set differs from what was rendered (e.g. some sheets
            # were deleted before appending) — re-render to apply the filter.
            n_active = len(active) if active else 0
            prog.setMaximum(n_active)
            prog.setValue(0)
            prog.setLabelText(f"Bygger P&ID-vy ({n_active} sidor)…")
            QApplication.processEvents()
            self.viewer._render_all_pages(
                active_pages=active,
                progress_cb=lambda cur, tot: prog.setValue(cur))
            prog.setMaximum(0)
            prog.setLabelText("Laddar markeringar…")
            QApplication.processEvents()
        self._load_overlays()
        prog.close()
        self.analyze_btn.setEnabled(True)
        self.export_btn.setEnabled(True)

    def _goto_page(self, display_n):
        if self.viewer.pdf_doc is None:
            return
        total = len(self._sheet_map) if self._sheet_map else (
            self.db.get_display_page_count() or self.viewer.page_count())
        display_n = max(0, min(display_n, total - 1))
        if self._sheet_map:
            physical = self._sheet_map.get(display_n, display_n)
        elif self.db.get_display_page_count() > 0:
            physical = self.db.get_sheet_physical_page(display_n)
        else:
            physical = display_n
        self._current_display_page = display_n
        self.viewer.goto_page(physical)
        self._update_page_label()

    def _on_page_spin_changed(self):
        self._goto_page(self.page_spin.value() - 1)

    def _update_page_label(self):
        total = len(self._sheet_map) if self._sheet_map else (
            self.db.get_display_page_count() or self.viewer.page_count())
        if total > 0:
            self.page_spin.blockSignals(True)
            self.page_spin.setRange(1, total)
            self.page_spin.setValue(self._current_display_page + 1)
            self.page_spin.blockSignals(False)
            self.page_total_label.setText(f"/ {total}")
        else:
            self.page_spin.blockSignals(True)
            self.page_spin.setRange(1, 1)
            self.page_spin.setValue(1)
            self.page_spin.blockSignals(False)
            self.page_total_label.setText("/ —")

    def navigate_to_marker(self, physical_page, x_pdf, y_pdf):
        """Navigate to the page containing a marker and zoom in on it."""
        if self.viewer.pdf_doc is None:
            return
        display_n = physical_page
        if self._sheet_map:
            rev = {phys: disp for disp, phys in self._sheet_map.items()}
            display_n = rev.get(physical_page, physical_page)
        self._goto_page(display_n)
        scene_pt = self.viewer.pdf_to_scene(x_pdf, y_pdf, page=physical_page)
        self.viewer.resetTransform()
        self.viewer.scale(2.5, 2.5)
        self.viewer.centerOn(scene_pt)

    def start_place_existing(self, type_str, id_):
        """Enter placement mode for a pre-existing item (no new item created)."""
        self._active_place_type = type_str
        self._active_place_id   = id_
        self._set_mode(MODE_PLACE_EXISTING)

    def remove_existing_marker(self, type_str, id_):
        """Delete all P&ID markers for an existing item and refresh overlays."""
        if type_str == 'cause':
            self.db.remove_cause_marker(id_)
        elif type_str == 'consequence':
            self.db.remove_consequence_marker(id_)
        elif type_str == 'safeguard':
            self.db.remove_safeguard_marker(id_)
        self._load_overlays()

    def _on_place_existing_click(self, scene_pos, page):
        """Place a marker for an existing item without creating anything new."""
        type_str = self._active_place_type
        id_      = self._active_place_id
        self._active_place_type = None
        self._active_place_id   = None
        self._set_mode(MODE_NAV)

        if type_str is None or id_ is None:
            return

        pdf_x, pdf_y = self.viewer.scene_to_pdf(scene_pos)

        if type_str == 'cause':
            cause = self.db.get_cause(id_)
            desc  = cause['description'] if cause else ''
            self.db.add_cause_marker(id_, page, pdf_x, pdf_y, 'Orsak', '')
            self.viewer.add_cause_marker(id_, pdf_x, pdf_y, 'Orsak', desc, '')
            self.existing_marker_placed.emit(type_str, id_)
        elif type_str == 'consequence':
            cons  = self.db.get_consequence(id_)
            desc  = cons['description'] if cons else ''
            self.db.add_consequence_marker(id_, page, pdf_x, pdf_y, '')
            self.viewer.add_consequence_marker(id_, pdf_x, pdf_y, desc)
            self.existing_marker_placed.emit(type_str, id_)
        elif type_str == 'safeguard':
            sg   = self.db.get_safeguard(id_)
            desc = sg['description'] if sg else ''
            self.db.add_safeguard_marker(id_, page, pdf_x, pdf_y, '')
            self.viewer.add_safeguard_marker(id_, pdf_x, pdf_y, '', desc)
            self.existing_marker_placed.emit(type_str, id_)

        self._load_overlays()

    def _set_mode(self, mode):
        if mode != MODE_CAUSE_TEMPLATE:
            self._cancel_secondary_placement()
        for m, btn in self.mode_buttons.items():
            btn.setChecked(m == mode)
        self.viewer.set_mode(mode)
        self.style_widget.setVisible(mode == MODE_NODE)

    def _on_layout_mode_toggled(self, checked):
        if checked:
            self._set_mode(MODE_BOARD_LAYOUT)
        else:
            self._set_mode(MODE_NAV)

    def _break_sheet_link(self, conn_id: int):
        """Delete a sheet connection from DB and redraw arcs."""
        if conn_id < 0:
            return
        self.db.delete_pid_connection(conn_id)
        self._load_overlays()

    def _add_sheet_link(self, from_page: int, to_page: int):
        """Create a manual sheet connection in DB and redraw arcs."""
        self.db.add_manual_pid_connection(from_page, to_page)
        self._load_overlays()

    def _draw_sheet_connections(self):
        """Draw bezier arcs between sheets on the board for all known inter-sheet links."""
        import json as _json
        connections = self.db.get_pid_connections()
        if not connections:
            return
        connectors  = self.db.get_connectors()
        raw_map     = self.db.get_pid_config_value('sheet_num_map') or '{}'
        try:
            sheet_num_map = {int(k): v.upper()
                             for k, v in _json.loads(raw_map).items()}
        except Exception:
            sheet_num_map = {}

        # Group connectors: (pid_page, ref_sheet_upper) → list of connector dicts
        conn_by_page_ref = {}
        for c in connectors:
            cd = dict(c)
            key = (cd['pid_page'], (cd.get('ref_sheet') or '').upper())
            conn_by_page_ref.setdefault(key, []).append(cd)

        # drawn_pairs keys on (fp, tp, media) so each distinct connection type
        # gets its own line, but identical directional duplicates are suppressed.
        drawn_pairs = set()
        # Track slot index per undirected gap so parallel lines are staggered.
        gap_slot_counter = {}
        rs = self.viewer.render_scale

        for row in connections:
            conn = dict(row)
            fp = conn.get('from_page')
            tp = conn.get('to_page')
            if fp is None or tp is None:
                continue
            if fp == tp:
                continue
            if fp not in self.viewer._all_page_items or tp not in self.viewer._all_page_items:
                continue
            media = conn.get('media_type', 'unknown') or 'unknown'
            pair_key = (fp, tp, media)
            if pair_key in drawn_pairs:
                continue
            drawn_pairs.add(pair_key)
            drawn_pairs.add((tp, fp, media))   # suppress the exact reverse too

            pw_fp = self.viewer._page_widths_pdf.get(fp, 800) * rs
            ph_fp = self.viewer._page_heights_pdf.get(fp, 600) * rs
            pw_tp = self.viewer._page_widths_pdf.get(tp, 800) * rs
            ph_tp = self.viewer._page_heights_pdf.get(tp, 600) * rs
            ox_fp, oy_fp = self.viewer._page_offsets.get(fp, (0, 0))
            ox_tp, oy_tp = self.viewer._page_offsets.get(tp, (0, 0))

            fp_sheet = sheet_num_map.get(fp, '')
            tp_sheet = sheet_num_map.get(tp, '')

            # Connectors on fp pointing to tp's sheet → prefer 'out' (flow leaving fp)
            src_conns_all = conn_by_page_ref.get((fp, tp_sheet), [])
            src_conns = sorted(src_conns_all,
                               key=lambda c: 0 if c.get('direction') == 'out' else 1)
            # Connectors on tp pointing back to fp's sheet → prefer 'in' (flow arriving)
            dst_conns_all = conn_by_page_ref.get((tp, fp_sheet), [])
            dst_conns = sorted(dst_conns_all,
                               key=lambda c: 0 if c.get('direction') == 'in' else 1)

            def edge_point(page_conns, ox, oy, pw, ph, default_edge):
                if page_conns:
                    # Pick the median connector by Y so we land at the actual
                    # symbol position on the P&ID rather than just the page edge.
                    med = sorted(page_conns, key=lambda c: c.get('y_pdf', 0))
                    med = med[len(med) // 2]
                    return QPointF(ox + med['x_pdf'] * rs,
                                   oy + med['y_pdf'] * rs)
                # Fallback when no connector was detected: centre of the edge
                if default_edge == 'right':  return QPointF(ox + pw,     oy + ph / 2)
                if default_edge == 'left':   return QPointF(ox,           oy + ph / 2)
                if default_edge == 'top':    return QPointF(ox + pw / 2,  oy)
                return                              QPointF(ox + pw / 2,  oy + ph)

            # Guess default edges from relative page positions (horizontal or vertical)
            dx_pages = ox_tp - ox_fp
            dy_pages = oy_tp - oy_fp
            if abs(dx_pages) >= abs(dy_pages):
                def_src = 'right'  if dx_pages >= 0 else 'left'
                def_dst = 'left'   if dx_pages >= 0 else 'right'
            else:
                def_src = 'bottom' if dy_pages >= 0 else 'top'
                def_dst = 'top'    if dy_pages >= 0 else 'bottom'

            src_pt   = edge_point(src_conns, ox_fp, oy_fp, pw_fp, ph_fp, def_src)
            dst_pt   = edge_point(dst_conns, ox_tp, oy_tp, pw_tp, ph_tp, def_dst)
            src_edge = src_conns[0].get('edge', def_src) if src_conns else def_src
            dst_edge = dst_conns[0].get('edge', def_dst) if dst_conns else def_dst

            color_hex  = _MEDIA_COLORS.get(media, _MEDIA_COLORS['unknown'])
            confidence = float(conn.get('confidence', 0.5))
            bidir      = bool(conn.get('is_bidirectional'))

            # Build a clean label from raw_text of first source connector
            label = media.replace('_', ' ').upper()
            if src_conns:
                rt = src_conns[0].get('raw_text', '')
                rt_clean = re.sub(r'=[\w./\-]+', '', rt)
                rt_clean = re.sub(r'\bS\d{6,8}\b', '', rt_clean, flags=re.I)
                rt_clean = ' '.join(rt_clean.split())[:28].strip()
                if rt_clean:
                    label = rt_clean

            # Assign a slot index for this gap so lines don't overlap
            gap_key = (min(fp, tp), max(fp, tp))
            arc_idx = gap_slot_counter.get(gap_key, 0)
            gap_slot_counter[gap_key] = arc_idx + 1

            weight = float(conn.get('weight', 0.5) or 0.5)

            self.viewer.add_sheet_conn_arc(src_pt, dst_pt, color_hex,
                                           confidence, label, bidir,
                                           conn_id=conn.get('id', -1),
                                           src_edge=src_edge, dst_edge=dst_edge,
                                           arc_index=arc_idx, weight=weight)

    def _run_smart_layout(self):
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.information(self, "Smart layout",
                "Öppna en P&ID-fil (PDF) först.")
            return
        if self._analyzer_thread and self._analyzer_thread.isRunning():
            return
        # Disconnect old thread's signal to prevent stale double-fires
        if self._analyzer_thread is not None:
            try:
                self._analyzer_thread.finished_analysis.disconnect(self._on_smart_layout_done)
            except Exception:
                pass
        # Save current layout for undo
        self._smart_layout_prev = dict(self.viewer._page_offsets)
        self.smart_btn.setEnabled(False)
        self.smart_btn.setText("⏳ Analyserar…")

        path         = self.db.get_pid_path()
        active_pages = sorted(self.viewer._all_page_items.keys())
        self._analyzer_thread = ConnectorAnalyzer(
            path,
            self.viewer.pdf_doc.page_count,
            self.viewer._page_widths_pdf,
            self.viewer._page_heights_pdf,
            self.viewer.render_scale,
            active_pages=active_pages,
        )
        self._analyzer_thread.progress.connect(
            lambda msg: self.smart_btn.setText(f"⏳ {msg}"))
        self._analyzer_thread.finished_analysis.connect(self._on_smart_layout_done)
        self._analyzer_thread.start()

        self._analyzer_progress_dlg = QProgressDialog(
            "Analyserar P&ID-kopplingar…", None, 0, 0, self)
        self._analyzer_progress_dlg.setWindowTitle("Smart layout")
        self._analyzer_progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._analyzer_progress_dlg.setMinimumDuration(0)
        self._analyzer_progress_dlg.show()
        self._analyzer_thread.progress.connect(
            lambda msg: self._analyzer_progress_dlg.setLabelText(msg)
            if self._analyzer_progress_dlg else None)
        QApplication.processEvents()

    def _on_smart_layout_done(self, connectors, connections, layout, sheet_num_map):
        if self._analyzer_progress_dlg is not None:
            self._analyzer_progress_dlg.close()
            self._analyzer_progress_dlg = None
        self.smart_btn.setEnabled(True)
        self.smart_btn.setText("✨ Smart layout")

        if not layout:
            QMessageBox.information(self, "Smart layout",
                "Inga off-page connectors hittades — kan inte föreslå layout.")
            return

        # Save to DB
        self.db.clear_connector_analysis()
        self.db.save_connectors(connectors)
        self.db.save_pid_connections(connections)

        import json as _json
        # Save sheet-number map (page_idx → sheet_num_str) for visual arc drawing
        self.db.set_pid_config_value('sheet_num_map', _json.dumps(sheet_num_map))

        # Apply layout (scene coords = pdf_coords * render_scale already in layout dict)
        for pn, (x, y) in layout.items():
            if pn in self.viewer._all_page_items:
                self.viewer._page_offsets[pn] = (x, y)
                self.viewer._all_page_items[pn].setPos(x, y)

        self.viewer._update_board_scene_rect()
        self._load_overlays()

        # Save board layout to DB
        layout_data = {str(p): list(off)
                       for p, off in self.viewer._page_offsets.items()}
        self.db.set_pid_config_value('board_layout', _json.dumps(layout_data))

        n_conn   = sum(1 for c in connections if not c.get('is_ghost'))
        n_ghost  = sum(1 for c in connections if c.get('is_ghost'))
        n_sheets = self.viewer.pdf_doc.page_count
        msg = (f"Layout klar — {len(connectors)} connectors, "
               f"{n_conn} kopplingar, {n_ghost} externa")
        if n_ghost:
            msg += f"\n({n_ghost} ritningar refererade men ej i workboard)"

        box = QMessageBox(self)
        box.setWindowTitle("Smart layout")
        box.setText(msg)
        undo_btn = box.addButton("↩ Ångra", QMessageBox.ButtonRole.ResetRole)
        box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() == undo_btn:
            self._undo_smart_layout()

    def _undo_smart_layout(self):
        if self._smart_layout_prev is None:
            return
        for pn, (ox, oy) in self._smart_layout_prev.items():
            if pn in self.viewer._all_page_items:
                self.viewer._page_offsets[pn] = (ox, oy)
                self.viewer._all_page_items[pn].setPos(ox, oy)
        self.viewer._update_board_scene_rect()
        self._load_overlays()
        self._smart_layout_prev = None

    def _update_pen(self):
        self.viewer.set_pen_style(
            self._pen_color, self.width_spin.value(), self.alpha_slider.value())

    def _on_markup_finished(self, pts, page):
        self._pending_markup_pts  = pts
        self._pending_markup_page = page
        self.create_node_btn.setEnabled(True)

    def _create_node_from_markup(self):
        if not self._pending_markup_pts:
            return
        name, ok = QInputDialog.getText(self, "Ny nod", "Namn på nod:", text="Ny nod")
        if not ok:
            return
        name  = name.strip() or "Ny nod"
        style = {'color': self._pen_color.name(),
                 'width': self.width_spin.value(),
                 'alpha': self.alpha_slider.value()}
        node_id = self.db.add_node_with_markup(
            name, self._pending_markup_pts, style, self._pending_markup_page)
        self._pending_markup_pts  = None
        self._pending_markup_page = None
        self.create_node_btn.setEnabled(False)
        self._load_overlays()
        self.node_created.emit(node_id)

    def _on_cause_click(self, scene_pos, page, suggested_tag=''):
        # Validate node — might be None, 0, or deleted from DB
        if not self._active_node_id:
            QMessageBox.information(self, "Välj nod",
                "Välj en nod i trädet innan du placerar orsaker.")
            return
        if hasattr(self.db, 'get_node') and not self.db.get_node(self._active_node_id):
            QMessageBox.information(self, "Ogiltig nod",
                "Den valda noden finns inte längre. Välj en nod i trädet.")
            self._active_node_id = None
            return
        # suggested_tag comes from the drawn rectangle's OCR/text extraction

        comp_data  = (self.db.all_component_types_dict()
                      if hasattr(self.db, 'all_component_types_dict') else None)
        mode_freqs = self._load_mode_freqs()
        # Look up component type from database / confirmed mappings
        detected_type = self._db_comp_for_tag(suggested_tag)
        dlg = ComponentPickerDialog(self, suggested_tag=suggested_tag,
                                    component_types=comp_data,
                                    mode_freqs=mode_freqs,
                                    preselect_type=detected_type)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        comp_type = dlg.selected_type
        tag       = dlg.selected_tag
        modes     = dlg.selected_modes
        if not modes:
            return

        # ── Learn: remember this prefix → comp_type for future sessions ──────
        self._learn_tag_type(suggested_tag or tag, comp_type)

        pdf_x, pdf_y = self.viewer.scene_to_pdf(scene_pos)
        last_cause_id = None
        for mode in modes:
            label    = f"{tag + ' — ' if tag else ''}{comp_type}: {mode}"
            try:
                cause_id = self.db.add_cause(self._active_node_id)
            except Exception as e:
                QMessageBox.critical(self, "Databasfel",
                    f"Kunde inte skapa orsak:\n{e}\n\nKontrollera att noden finns i trädet.")
                return
            self.db.update_cause(cause_id, label)

            # Auto-set F-level AND store base_freq from component failure frequency
            freq = dlg.selected_freqs.get(mode)
            if freq is not None:
                f_level = self._compute_f_level(freq)
                self.db.update_cause(cause_id, likelihood=f_level, base_freq=freq)
            else:
                # No frequency defined — store None so CausePanel shows empty
                self.db.update_cause(cause_id, base_freq=None)

            self.db.add_cause_marker(cause_id, page, pdf_x, pdf_y, comp_type, tag)
            self.viewer.add_cause_marker(cause_id, pdf_x, pdf_y, comp_type, mode, tag)
            last_cause_id = cause_id

        if last_cause_id is not None:
            self.cause_created.emit(last_cause_id)

    def _on_consequence_click(self, scene_pos, page, suggested_tag=''):
        if self._active_cause_id is None:
            QMessageBox.information(self, "Välj orsak",
                "Välj en cause i trädet innan du placerar en konsekvens.")
            return

        dlg = TargetPickerDialog(self, suggested_tag=suggested_tag, db=self.db)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        pdf_x, pdf_y = self.viewer.scene_to_pdf(scene_pos)

        if dlg.link_to_id:
            # ── Link to existing consequence ──────────────────────────────────
            cons_id = self.db.copy_consequence(dlg.link_to_id, self._active_cause_id)
            if cons_id is None:
                return
            cons = self.db.get_consequence(cons_id)
            display = cons['description'] if cons else '🔗 Länkad konsekvens'
        else:
            # ── Create new consequence ────────────────────────────────────────
            template  = dlg.template
            target    = dlg.target
            chain     = dlg.selected_chain
            base_desc = template.format(target) if target else template.replace('{}', '[okänt objekt]')
            full_desc = _pid_build_chain_text(base_desc, chain) or base_desc
            display   = full_desc

            import json as _json
            chain_json = _json.dumps(chain) if chain else ''
            cons_id = self.db.add_consequence(self._active_cause_id)
            try:
                self.db.update_consequence(cons_id, full_desc, 1, '', chain_json)
            except TypeError:
                self.db.update_consequence(cons_id, full_desc, 1)

        zone = self._pending_zone_pdf
        rect_w = zone.width()  if zone else None
        rect_h = zone.height() if zone else None
        if zone:
            pdf_x, pdf_y = zone.center().x(), zone.center().y()
        self._pending_zone_pdf = None
        self.db.add_consequence_marker(cons_id, page, pdf_x, pdf_y,
                                       dlg.target if not dlg.link_to_id else '',
                                       rect_w=rect_w, rect_h=rect_h)
        self.viewer.add_consequence_marker(cons_id, pdf_x, pdf_y, display,
                                           rect_w=rect_w, rect_h=rect_h)
        self.consequence_created.emit(cons_id)

    def _on_safeguard_click(self, scene_pos, page, suggested_tag=''):
        if self._active_consequence_id is None:
            QMessageBox.information(self, "Välj konsekvens",
                "Välj en consequence i trädet innan du markerar en safeguard.")
            return

        all_sgs = self.db.safeguards(self._active_consequence_id)
        existing = [s['description'] for s in all_sgs]
        bpcs_count = sum(1 for s in all_sgs
                         if dict(s).get('sg_type', 'Övrigt') == 'BPCS')

        dlg = SafeguardPickerDialog(self, suggested_tag=suggested_tag,
                                    existing_safeguards=existing, db=self.db,
                                    existing_bpcs_count=bpcs_count)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        pdf_x, pdf_y = self.viewer.scene_to_pdf(scene_pos)

        if dlg.link_to_id:
            # ── Link to existing safeguard ────────────────────────────────────
            sg_id = self.db.copy_safeguard(dlg.link_to_id, self._active_consequence_id)
            if sg_id is None:
                return
            sg = self.db.get_safeguard(sg_id)
            tag         = ''
            description = sg['description'] if sg else '🔗 Länkad safeguard'
        else:
            # ── Create new safeguard ──────────────────────────────────────────
            tag         = dlg.tag
            description = dlg.description
            sg_id = self.db.add_safeguard(self._active_consequence_id)
            self.db.update_safeguard(sg_id, description, dlg.rrf, dlg.sg_type)

        zone = self._pending_zone_pdf
        rect_w = zone.width()  if zone else None
        rect_h = zone.height() if zone else None
        if zone:
            pdf_x, pdf_y = zone.center().x(), zone.center().y()
        self._pending_zone_pdf = None
        self.db.add_safeguard_marker(sg_id, page, pdf_x, pdf_y, tag,
                                     rect_w=rect_w, rect_h=rect_h)
        self.viewer.add_safeguard_marker(sg_id, pdf_x, pdf_y, tag, description,
                                         rect_w=rect_w, rect_h=rect_h)
        self.safeguard_created.emit(sg_id)

        # "Lägg till ytterligare" — reset banner to ready state for next safeguard
        if dlg.add_more and self._scenario_active:
            self._sc_instr.setText("Klicka på nästa safeguard på P&ID:n")
            self._sc_add_sg_btn.setVisible(False)
            self._sc_finish_btn.setVisible(False)
            self._set_mode(MODE_SAFEGUARD)

    def _on_zone_drawn(self, pdf_rect, page):
        """Right-drag rubber band completed — let user pick cause/consequence/safeguard."""
        menu = QMenu(self)
        a_cause = menu.addAction("⚙️ Orsak")
        a_cons  = menu.addAction("⚠️ Konsekvens")
        a_sg    = menu.addAction("🛡️ Safeguard")
        chosen  = menu.exec(QCursor.pos())
        if chosen is None:
            return

        self._pending_zone_pdf = pdf_rect
        rs = self.viewer.render_scale
        center_scene = QPointF(pdf_rect.center().x() * rs, pdf_rect.center().y() * rs)

        tag = comp_type = ''
        if HAS_PYMUPDF and self.viewer.pdf_doc:
            try:
                result = self.viewer._extract_tag_from_rect(pdf_rect)
                tag, comp_type = result[0], result[1]
            except Exception:
                pass

        # Extract full text from rubber-banded area (native PDF text first, OCR fallback)
        full_text = self.viewer._text_in_rect(pdf_rect) if HAS_PYMUPDF and self.viewer.pdf_doc else ''
        strip = hasattr(self.db, 'get_config') and self.db.get_config('tag_strip_spaces', '1') == '1'

        if chosen is a_cause:
            dev_id   = self._active_deviation_id or 0
            detected = self._db_comp_for_tag(tag) if tag else comp_type
            suggested = (full_text or tag or '').replace(' ', '') if strip else (full_text or tag or '')
            self.cause_placement_requested.emit(dev_id, tag or '', detected, center_scene, page, suggested)
        elif chosen is a_cons:
            suggested = (full_text or tag or '').replace(' ', '') if strip else (full_text or tag or '')
            self._on_consequence_click(center_scene, page, suggested)
        elif chosen is a_sg:
            suggested = (full_text or tag or '').replace(' ', '') if strip else (full_text or tag or '')
            self._on_safeguard_click(center_scene, page, suggested)

    def _on_zone_resized(self, marker_type, marker_id, cx, cy, w, h):
        """Zone corner was dragged — update DB marker center and rect dimensions."""
        if hasattr(self.db, 'update_marker_rect'):
            self.db.update_marker_rect(marker_type, marker_id,
                                       self.viewer.current_page, cx, cy, w, h)

    def _on_context_action(self, action, pos, page):
        if action == 'cause':
            tag = find_tag_near_point(self.viewer.pdf_doc, page,
                                      *self.viewer.scene_to_pdf(pos)) \
                  if self.viewer.pdf_doc else ''
            detected_type = self._db_comp_for_tag(tag) if tag else ''
            dev_id = self._active_deviation_id or 0
            self.cause_placement_requested.emit(dev_id, tag or '', detected_type, pos, page, '')
        elif action == 'consequence':
            self._set_mode(MODE_CONSEQUENCE)
            tag = find_tag_near_point(self.viewer.pdf_doc, page,
                                      *self.viewer.scene_to_pdf(pos)) \
                  if self.viewer.pdf_doc else ''
            self._on_consequence_click(pos, page, tag)
        elif action == 'safeguard':
            self._set_mode(MODE_SAFEGUARD)
            tag = find_tag_near_point(self.viewer.pdf_doc, page,
                                      *self.viewer.scene_to_pdf(pos)) \
                  if self.viewer.pdf_doc else ''
            self._on_safeguard_click(pos, page, tag)
        elif action == 'node':
            self._set_mode(MODE_NODE)
        elif action == 'risk_scenario':
            node_id = self._active_node_id or 0
            self.risk_scenario_requested.emit(node_id, pos, page)

    def _draw_tag_highlights(self):
        """Highlight complete tag numbers found on the current PDF page.

        Yellow  = tag recognised but not yet a HAZOP cause.
        Green   = tag has at least one defined HAZOP cause.
        Only runs when smart database is enabled OR a tag database is loaded.
        """
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            return
        if not hasattr(self.db, 'tag_db_setting'):
            return

        smart_on  = self.db.tag_db_setting('smart_enabled', '0') == '1'
        tag_codes = set(self.db.all_active_tag_codes()) \
                    if hasattr(self.db, 'all_active_tag_codes') else set()

        # Nothing to do if both sources are inactive
        if not smart_on and not tag_codes:
            return

        try:
            self.viewer.clear_highlights()
            page_num  = self.viewer.current_page
            fitz_page = self.viewer.pdf_doc.load_page(page_num)

            # Tags already used as HAZOP causes (→ green)
            used_tags: set = set()
            try:
                for m in self.db.cause_markers_for_page(page_num):
                    t = (m['component_tag'] or '').upper().strip()
                    if t:
                        used_tags.add(t)
                for node in self.db.nodes():
                    for cause in self.db.causes(node['id']):
                        t = _pick_best_tag(cause['description'])
                        if t:
                            used_tags.add(t.upper())
            except Exception:
                pass

            # Scan page text for complete tag numbers using spatial combining
            raw_words = fitz_page.get_text("words")
            seen: set = set()

            for candidate in _spatial_combine(raw_words, gap_limit=22.0):
                tag = _pick_best_tag(candidate)
                if not tag or tag in seen:
                    continue
                pfx = _equip_prefix_from_tag(tag)
                # Only highlight if prefix is known (in DB or confirmed mapping)
                known = (smart_on or pfx in tag_codes or
                         (hasattr(self.db, 'confirmed_comp_for_tag') and
                          self.db.confirmed_comp_for_tag(pfx)))
                if not known:
                    continue
                seen.add(tag)

                # Find exact bounding box on the page
                try:
                    hits = fitz_page.search_for(tag)
                    if not hits:
                        # Try just the code part (e.g., PSV-101 from 20-PSV-101)
                        simple = f"{pfx}-" + tag.split('-')[-1] if '-' in tag else tag
                        hits = fitz_page.search_for(simple)
                    for bbox in hits:
                        is_used = tag in used_tags or simple in used_tags \
                                  if 'simple' in dir() else tag in used_tags
                        color = '#90EE90' if is_used else '#FFFFE0'
                        label = f"{'✓ HAZOP-orsak' if is_used else '○ Tagg'}: {tag}"
                        self.viewer.add_tag_highlight(bbox, color, label)
                except Exception:
                    continue

        except Exception:
            pass  # Never crash during highlight drawing

    def _load_overlays(self):
        self.viewer.clear_overlays()
        self.viewer.clear_markup_overlays()
        self.viewer.clear_red_markup_overlays()
        if self.viewer.pdf_doc is None:
            return
        orig_page   = self.viewer.current_page
        active_pages = sorted(self.viewer._all_page_items.keys())
        all_nodes   = list(self.db.nodes())

        for page in active_pages:
            self.viewer.current_page = page  # ensures pdf_to_scene uses this page's offset

            for node in all_nodes:
                nd      = dict(node)
                raw_pts = nd.get('markup_points', '') or ''
                nd_page = int(nd.get('pid_page', 0) or 0)
                if not raw_pts or nd_page != page:
                    continue
                try:
                    points = [(float(p[0]), float(p[1])) for p in json.loads(raw_pts)]
                    style  = json.loads(nd.get('markup_style', '') or '{}')
                except Exception:
                    continue
                if points:
                    self.viewer.add_node_overlay(nd['id'], points, style, nd.get('name', ''))

            if hasattr(self.db, 'node_markups_for_page'):
                for mu in self.db.node_markups_for_page(page):
                    m = dict(mu)
                    try: pts = json.loads(m.get('points', '[]') or '[]')
                    except Exception: pts = []
                    self.viewer.add_markup_overlay(
                        m['id'], m.get('type', 'polygon'), pts,
                        m.get('label', ''), m.get('color', '#1565C0'),
                        float(m.get('opacity', 0.45)), int(m.get('line_width', 2)),
                        bool(m.get('visible', 1)))

            if hasattr(self.db, 'node_red_markups_for_page'):
                for mu in self.db.node_red_markups_for_page(page):
                    m = dict(mu)
                    try: pts = json.loads(m.get('points', '[]') or '[]')
                    except Exception: pts = []
                    self.viewer.add_red_markup_overlay(
                        m['id'], m.get('type', 'polygon'), pts,
                        m.get('label', ''), m.get('color', '#CC0000'),
                        float(m.get('opacity', 1.0)), int(m.get('line_width', 4)),
                        bool(m.get('visible', 1)), int(m.get('font_size', 12)),
                        float(m.get('symbol_w', 40)), float(m.get('symbol_h', 40)),
                        float(m.get('symbol_rot', 0)))

            for m in self.db.cause_markers_for_page(page):
                md    = dict(m)
                cause = self.db.get_cause(md['cause_id'])
                label = dict(cause).get('description', '') if cause else ''
                self.viewer.add_cause_marker(
                    md['cause_id'], md['x'], md['y'],
                    md.get('component_type', ''), label, md.get('component_tag', ''),
                    rect_w=md.get('rect_w'), rect_h=md.get('rect_h'))

            for m in self.db.consequence_markers_for_page(page):
                md   = dict(m)
                cons = self.db.get_consequence(md['consequence_id'])
                desc = dict(cons).get('description', '') if cons else md.get('target_name', '')
                self.viewer.add_consequence_marker(
                    md['consequence_id'], md['x'], md['y'], desc,
                    rect_w=md.get('rect_w'), rect_h=md.get('rect_h'))

            for m in self.db.safeguard_markers_for_page(page):
                md = dict(m)
                sg = self.db.conn.execute(
                    "SELECT description FROM safeguards WHERE id=?",
                    (md['safeguard_id'],)).fetchone()
                desc = sg['description'] if sg else ''
                self.viewer.add_safeguard_marker(
                    md['safeguard_id'], md['x'], md['y'], md.get('tag', ''), desc,
                    rect_w=md.get('rect_w'), rect_h=md.get('rect_h'))

        # ── Draw ALL connections after all pages, so cross-page lines work ──────
        all_cause_pos = {}
        all_cons_pos  = {}
        all_sg_pos    = {}
        for page in active_pages:
            for m in self.db.cause_markers_for_page(page):
                all_cause_pos.setdefault(m['cause_id'], []).append(
                    self.viewer.pdf_to_scene(m['x'], m['y'], page=page))
            for m in self.db.consequence_markers_for_page(page):
                all_cons_pos.setdefault(m['consequence_id'], []).append(
                    self.viewer.pdf_to_scene(m['x'], m['y'], page=page))
            for m in self.db.safeguard_markers_for_page(page):
                all_sg_pos.setdefault(m['safeguard_id'], []).append(
                    self.viewer.pdf_to_scene(m['x'], m['y'], page=page))

        for cid, cpos_list in all_cons_pos.items():
            c = self.db.get_consequence(cid)
            if c and c['cause_id'] in all_cause_pos:
                for cpos in cpos_list:
                    for capos in all_cause_pos[c['cause_id']]:
                        self.viewer.add_connection_line(capos, cpos, '#c0392b')

        for sid, spos_list in all_sg_pos.items():
            s = self.db.get_safeguard(sid)
            if s and s['consequence_id'] in all_cons_pos:
                for spos in spos_list:
                    for kpos in all_cons_pos[s['consequence_id']]:
                        self.viewer.add_connection_line(kpos, spos, '#27ae60', dashed=True)

        self.viewer.current_page = orig_page
        self._draw_tag_highlights()
        self._draw_sheet_connections()
        # Reapply LOD so newly added items get correct visibility at current zoom
        self.viewer._apply_lod(self.viewer.transform().m11(), force=True)

    def start_cause_template_mode(self, deviation_id):
        """Switch to template placement mode for the given deviation."""
        self._active_deviation_id = deviation_id
        self._set_mode(MODE_CAUSE_TEMPLATE)

    def reload_overlays(self):
        """Public helper to refresh all P&ID markers and connection lines."""
        self._load_overlays()

    # ── Node markup editing API ───────────────────────────────────────────────

    def enter_markup_edit(self, node_id):
        """Enter markup editing mode for a node: show existing markup + enable tools."""
        self._active_markup_class = 'node'
        self.set_active_node(node_id)
        self._set_mode(MODE_MARKUP_SELECT)
        self.viewer.markup_draw_finished.connect(self._on_viewer_markup_drawn)
        self.viewer.markup_item_clicked.connect(self._on_viewer_markup_clicked)

    def exit_markup_mode(self):
        """Return to normal navigation mode."""
        try: self.viewer.markup_draw_finished.disconnect(self._on_viewer_markup_drawn)
        except Exception: pass
        try: self.viewer.markup_item_clicked.disconnect(self._on_viewer_markup_clicked)
        except Exception: pass
        self._active_markup_class = 'node'
        self._active_symbol_id = None
        self._set_mode(MODE_NAV)

    def set_markup_tool(self, tool, color=None, opacity=None, width=None):
        """Set drawing tool: 'polygon'|'polyline'|'text'|'comment'|'select'|'smart'."""
        _map = {'polygon':  MODE_MARKUP_POLYGON,
                'polyline': MODE_MARKUP_POLYLINE,
                'text':     MODE_MARKUP_TEXT,
                'comment':  MODE_MARKUP_COMMENT,
                'select':   MODE_MARKUP_SELECT,
                'smart':    MODE_SMART_POLYLINE}
        if tool in _map:
            self._set_mode(_map[tool])
        if color is not None:
            self.viewer.set_pen_style(color, width or 3, int((opacity or 0.45) * 210))

    def refresh_markup_overlays(self):
        """Reload only the markup overlays (cheap — no cause/cons/sg reload)."""
        self.viewer.clear_markup_overlays()
        if self.viewer.pdf_doc is None:
            return
        orig_page = self.viewer.current_page
        for page in sorted(self.viewer._all_page_items.keys()):
            self.viewer.current_page = page
            if hasattr(self.db, 'node_markups_for_page'):
                for mu in self.db.node_markups_for_page(page):
                    m = dict(mu)
                    try: pts = json.loads(m.get('points', '[]') or '[]')
                    except Exception: pts = []
                    self.viewer.add_markup_overlay(
                        m['id'], m.get('type', 'polygon'), pts,
                        m.get('label', ''), m.get('color', '#1565C0'),
                        float(m.get('opacity', 0.45)), int(m.get('line_width', 12)),
                        bool(m.get('visible', 1)),
                        int(m.get('font_size', 12)))
        self.viewer.current_page = orig_page

    # ── Red markup editing API ────────────────────────────────────────────────

    def enter_red_markup_edit(self, node_id):
        """Enter red markup editing mode for a node."""
        self._active_markup_class = 'red'
        self._active_symbol_id = None
        self.set_active_node(node_id)
        self._set_mode(MODE_MARKUP_SELECT)
        self.viewer.markup_draw_finished.connect(self._on_viewer_markup_drawn)
        self.viewer.markup_item_clicked.connect(self._on_viewer_markup_clicked)

    def exit_red_markup_mode(self):
        """Return to normal navigation mode from red markup."""
        try: self.viewer.markup_draw_finished.disconnect(self._on_viewer_markup_drawn)
        except Exception: pass
        try: self.viewer.markup_item_clicked.disconnect(self._on_viewer_markup_clicked)
        except Exception: pass
        self._active_markup_class = 'node'
        self._active_symbol_id = None
        self._set_mode(MODE_NAV)

    def set_red_markup_tool(self, tool, color=None, opacity=None, width=None, symbol_id=None):
        """Set red markup tool: 'polygon'|'polyline'|'comment'|'select'|'smart'|'symbol'."""
        _map = {'polygon':  MODE_MARKUP_POLYGON,
                'polyline': MODE_MARKUP_POLYLINE,
                'comment':  MODE_MARKUP_COMMENT,
                'select':   MODE_MARKUP_SELECT,
                'smart':    MODE_SMART_POLYLINE,
                'symbol':   MODE_RED_MARKUP_SYMBOL}
        if tool in _map:
            self._set_mode(_map[tool])
        if tool == 'symbol' and symbol_id is not None:
            self._active_symbol_id = symbol_id
        elif tool != 'symbol':
            self._active_symbol_id = None
        if color is not None:
            self.viewer.set_pen_style(color, width or 4, int((opacity or 1.0) * 210))

    def refresh_red_markup_overlays(self):
        """Reload only the red markup overlays."""
        self.viewer.clear_red_markup_overlays()
        if self.viewer.pdf_doc is None:
            return
        orig_page = self.viewer.current_page
        for page in sorted(self.viewer._all_page_items.keys()):
            self.viewer.current_page = page
            if hasattr(self.db, 'node_red_markups_for_page'):
                for mu in self.db.node_red_markups_for_page(page):
                    m = dict(mu)
                    try: pts = json.loads(m.get('points', '[]') or '[]')
                    except Exception: pts = []
                    self.viewer.add_red_markup_overlay(
                        m['id'], m.get('type', 'polygon'), pts,
                        m.get('label', ''), m.get('color', '#CC0000'),
                        float(m.get('opacity', 1.0)), int(m.get('line_width', 4)),
                        bool(m.get('visible', 1)), int(m.get('font_size', 12)),
                        float(m.get('symbol_w', 40)), float(m.get('symbol_h', 40)),
                        float(m.get('symbol_rot', 0)))
        self.viewer.current_page = orig_page

    def _on_viewer_markup_drawn(self, type_, pts, page):
        """Called when user finishes drawing in the viewer; route to appropriate panel."""
        node_id = self._active_node_id
        if node_id is None:
            return
        if self._active_markup_class == 'red':
            # Red markup mode
            if type_ == 'comment':
                label, ok = QInputDialog.getText(self, 'Kommentar', 'Kommentar:')
                if not ok or not label.strip():
                    self.viewer.clear_red_markup_overlays()
                    self.refresh_red_markup_overlays()
                    return
            elif type_ == 'symbol':
                label = self._active_symbol_id or ''
                self._set_mode(MODE_MARKUP_SELECT)
            else:
                label = ''
            self.red_markup_draw_finished.emit(type_, node_id, pts, page, label)
        else:
            # Node markup mode
            if type_ == 'text':
                node = self.db.get_node(node_id) if hasattr(self.db, 'get_node') else None
                label = node['name'] if node else ''
            elif type_ == 'comment':
                label, ok = QInputDialog.getText(self, 'Kommentar', 'Kommentar:')
                if not ok or not label.strip():
                    self.viewer.clear_markup_overlays()
                    self.refresh_markup_overlays()
                    return
            else:
                label = ''
            self.markup_draw_finished.emit(type_, node_id, pts, page, label)

    def _on_viewer_markup_clicked(self, mu_id):
        if self._active_markup_class == 'red':
            self.red_markup_item_selected.emit(mu_id)
        else:
            self.markup_item_selected.emit(mu_id)
            self.viewer.highlight_markup(mu_id)

    def _on_cause_template_click(self, scene_pos, page, suggested_tag=''):
        # If secondary placement is pending, place secondary marker instead of opening dialog
        if self._pending_secondary_cause_id is not None:
            self._place_secondary_marker(scene_pos, page, suggested_tag)
            return

        dev_id = self._active_deviation_id
        if not dev_id:
            return

        detected_type = self._db_comp_for_tag(suggested_tag) if suggested_tag else ''
        self.cause_placement_requested.emit(
            dev_id, suggested_tag or '', detected_type, scene_pos, page, '')

    def place_cause_from_template(self, dev_id, scene_pos, page,
                                  comp_type, comp_tag, description, frequency):
        """Called by MainWindow after CauseObjectPopup is confirmed."""
        label = description or comp_tag or 'Ny orsak'

        try:
            cause_id = self.db.add_cause(dev_id)
        except Exception as e:
            QMessageBox.critical(self, "Databasfel", f"Kunde inte skapa orsak:\n{e}")
            return None
        self.db.update_cause(cause_id, label, comp_type=comp_type, comp_tag=comp_tag)
        if frequency is not None:
            self.db.update_cause(cause_id, base_freq=frequency)

        zone = self._pending_zone_pdf
        rect_w = zone.width()  if zone else None
        rect_h = zone.height() if zone else None
        if zone:
            pdf_x, pdf_y = zone.center().x(), zone.center().y()
        else:
            pdf_x, pdf_y = self.viewer.scene_to_pdf(scene_pos)
        self._pending_zone_pdf = None
        self.db.add_cause_marker(cause_id, page, pdf_x, pdf_y, comp_type, comp_tag,
                                  rect_w, rect_h)
        self.viewer.add_cause_marker(cause_id, pdf_x, pdf_y, comp_type, label, comp_tag,
                                     rect_w, rect_h)
        self._load_overlays()
        self.cause_template_created.emit(cause_id)
        return cause_id

    def _on_add_cause_at_marker(self, cause_id: int):
        """Right-click on existing cause marker → create another cause at the same position."""
        page = self.viewer.current_page
        markers = self.db.cause_markers_for_page(page)
        marker = next((dict(m) for m in markers if m['cause_id'] == cause_id), None)
        if not marker:
            return

        pdf_x  = marker['x']
        pdf_y  = marker['y']
        rect_w = marker.get('rect_w')
        rect_h = marker.get('rect_h')

        # Reuse the same zone rectangle
        if rect_w and rect_h:
            self._pending_zone_pdf = QRectF(
                pdf_x - rect_w / 2, pdf_y - rect_h / 2, rect_w, rect_h)

        # Pre-fill comp_type and comp_tag from the existing cause
        cause = self.db.get_cause(cause_id)
        cause_d = dict(cause) if cause else {}
        dev_id   = cause_d.get('deviation_id') or 0
        comp_tag = cause_d.get('comp_tag') or marker.get('component_tag', '')
        comp_type = cause_d.get('comp_type') or marker.get('component_type', '')

        node_id = cause_d.get('node_id')
        if node_id:
            self._active_node_id = node_id

        scene_pos = self.viewer.pdf_to_scene(pdf_x, pdf_y)
        self.cause_placement_requested.emit(dev_id, comp_tag, comp_type, scene_pos, page, '')

    def _on_add_consequence_at_marker(self, cons_id: int):
        """Right-click on existing consequence marker → add another consequence here."""
        page = self.viewer.current_page
        markers = self.db.consequence_markers_for_page(page)
        marker = next((dict(m) for m in markers if m['consequence_id'] == cons_id), None)
        if not marker:
            return

        pdf_x  = marker['x']
        pdf_y  = marker['y']
        rect_w = marker.get('rect_w')
        rect_h = marker.get('rect_h')

        if rect_w and rect_h:
            self._pending_zone_pdf = QRectF(
                pdf_x - rect_w / 2, pdf_y - rect_h / 2, rect_w, rect_h)

        # Set active cause to the cause of the clicked consequence
        cons = self.db.get_consequence(cons_id)
        if cons:
            self._active_cause_id = dict(cons).get('cause_id')

        scene_pos = self.viewer.pdf_to_scene(pdf_x, pdf_y)
        self._on_consequence_click(scene_pos, page)

    def _on_add_safeguard_at_marker(self, sg_id: int):
        """Right-click on existing safeguard marker → add another safeguard here."""
        page = self.viewer.current_page
        markers = self.db.safeguard_markers_for_page(page)
        marker = next((dict(m) for m in markers if m['safeguard_id'] == sg_id), None)
        if not marker:
            return

        pdf_x  = marker['x']
        pdf_y  = marker['y']
        rect_w = marker.get('rect_w')
        rect_h = marker.get('rect_h')

        if rect_w and rect_h:
            self._pending_zone_pdf = QRectF(
                pdf_x - rect_w / 2, pdf_y - rect_h / 2, rect_w, rect_h)

        # Set active consequence to the consequence of the clicked safeguard
        sg = self.db.get_safeguard(sg_id)
        if sg:
            self._active_consequence_id = dict(sg).get('consequence_id')

        scene_pos = self.viewer.pdf_to_scene(pdf_x, pdf_y)
        self._on_safeguard_click(scene_pos, page)

    def _place_secondary_marker(self, scene_pos, page, suggested_tag=''):
        """Place the queued secondary marker, then re-open the dialog for the same deviation."""
        cause_id     = self._pending_secondary_cause_id
        comp_type    = self._pending_secondary_comp_type
        tag          = suggested_tag or self._pending_secondary_tag
        reopen_dev   = self._pending_secondary_deviation_id
        reopen_type  = self._pending_secondary_preselect_type
        self._cancel_secondary_placement()

        pdf_x, pdf_y = self.viewer.scene_to_pdf(scene_pos)
        self.db.add_cause_marker(cause_id, page, pdf_x, pdf_y, comp_type, tag)
        row = self.db.get_cause(cause_id)
        label = row['description'] if row else ''
        self.viewer.add_cause_marker(cause_id, pdf_x, pdf_y, comp_type, label, tag)
        self._load_overlays()

        # Re-open dialog for same deviation so user can continue
        if reopen_dev:
            self._open_template_dialog_for_deviation(reopen_dev, reopen_type)

    def _open_template_dialog_for_deviation(self, dev_id, preselect_type=''):
        """Open TemplateCausePickerDialog without a click position (cause gets no P&ID marker)."""
        dev = self.db.get_deviation(dev_id)
        if not dev:
            return
        dev_name = dev['description']
        std_causes = (self.db.standard_causes_for_name(dev_name)
                      if hasattr(self.db, 'standard_causes_for_name') else [])
        comp_type_names = sorted({
            dict(c).get('comp_type', '') for c in std_causes
            if dict(c).get('comp_type', '')
        })
        dlg = TemplateCausePickerDialog(
            dev_name, std_causes,
            component_types=comp_type_names,
            suggested_tag='',
            preselect_type=preselect_type,
            parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        cause_desc = dlg.chosen_description
        comp_type  = dlg.component_type
        tag        = dlg.component_tag
        if not cause_desc:
            return

        label = cause_desc

        try:
            cause_id = self.db.add_cause(dev_id)
        except Exception as e:
            QMessageBox.critical(self, "Databasfel", f"Kunde inte skapa orsak:\n{e}")
            return
        self.db.update_cause(cause_id, label)
        std_freq     = dlg.chosen_std_cause_freq
        std_cause_id = dlg.chosen_std_cause_id
        if std_freq is not None or std_cause_id is not None:
            self.db.update_cause(cause_id, base_freq=std_freq,
                                 standard_cause_id=std_cause_id)
        self.cause_template_created.emit(cause_id)

        # If user wants secondary again, queue it
        if dlg.wants_secondary_placement and dlg.secondary_description:
            self._pending_secondary_cause_id       = cause_id
            self._pending_secondary_comp_type      = dlg.secondary_comp_type
            self._pending_secondary_tag            = dlg.secondary_component_tag
            self._pending_secondary_deviation_id   = dev_id
            self._pending_secondary_preselect_type = dlg.component_type
            self._secondary_lbl.setText(
                f"Klicka nu på sekundärkomponenten på P&ID:n  —  {dlg.secondary_description}")
            self._secondary_banner.setVisible(True)

    def _cancel_secondary_placement(self):
        self._pending_secondary_cause_id       = None
        self._pending_secondary_comp_type      = ''
        self._pending_secondary_tag            = ''
        self._pending_secondary_deviation_id   = None
        self._pending_secondary_preselect_type = ''
        self._secondary_banner.setVisible(False)

    def set_active_node(self, node_id):
        self._active_node_id        = node_id
        self._active_cause_id       = None
        self._active_consequence_id = None

    def set_active_cause(self, cause_id):
        self._active_cause_id       = cause_id
        self._active_consequence_id = None
        row = self.db.get_cause(cause_id)
        if row:
            self._active_node_id = dict(row).get('node_id')

    def set_active_consequence(self, cons_id):
        self._active_consequence_id = cons_id
        row = self.db.get_consequence(cons_id)
        if not row:
            return
        cause_id = dict(row).get('cause_id')
        self._active_cause_id = cause_id
        if cause_id:
            cause = self.db.get_cause(cause_id)
            if cause:
                self._active_node_id = dict(cause).get('node_id')

    def _scan_equipment(self):
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Ingen PDF", "Öppna en P&ID-fil först.")
            return

        # Offer OCR auto-install if no engine is present
        status = ocr_status()
        if not status['tesseract'] and not status['easyocr']:
            ensure_ocr_available(self)
            status = ocr_status()

        use_ocr   = False
        ocr_engine = 'auto'

        if status['tesseract'] or status['easyocr']:
            engines = []
            if status['tesseract']: engines.append("pytesseract")
            if status['easyocr']:   engines.append("easyocr")
            reply = QMessageBox.question(
                self, "OCR — skanningsalternativ",
                f"Tillgängliga OCR-motorer: {', '.join(engines)}\n\n"
                "Vill du använda OCR för sidor med lite text?\n"
                "(Ger bättre resultat på skannade P&ID-ritningar men tar längre tid.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            use_ocr = (reply == QMessageBox.StandardButton.Yes)
        else:
            # No OCR available — inform user
            native_test_pg = self.viewer.pdf_doc.load_page(0)
            if len(native_test_pg.get_text("words")) < 10:
                QMessageBox.information(
                    self, "OCR saknas",
                    "PDF:en verkar innehålla lite sökbar text.\n\n"
                    "Installera en OCR-motor för bättre resultat:\n"
                    "  • pip install pytesseract\n"
                    "    (+ ladda ner Tesseract: https://github.com/UB-Mannheim/tesseract/wiki)\n"
                    "  • pip install easyocr  (tyngre men enklare att installera)")

        # Progress dialog
        n_pages = self.viewer.pdf_doc.page_count
        progress = QProgressDialog("Förbereder…", "Avbryt", 0, n_pages, self)
        progress.setWindowTitle("Skannar P&ID")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        def _cb(page_num, total, msg):
            if progress.wasCanceled():
                return
            progress.setValue(page_num)
            progress.setLabelText(msg)
            QApplication.processEvents()

        result = scan_pdf_for_equipment(
            self.viewer.pdf_doc,
            use_ocr=use_ocr,
            ocr_engine=ocr_engine,
            progress_callback=_cb)
        progress.setValue(n_pages)
        progress.close()

        if progress.wasCanceled():
            return

        # Remove meta before checking emptiness
        meta = result.pop('_meta', {})
        real_result = {k: v for k, v in result.items() if not k.startswith('_')}
        result['_meta'] = meta  # put back for dialog

        if not real_result:
            QMessageBox.information(
                self, "Inga taggar",
                "Inga utrustningstaggar hittades.\n\n"
                + ("Försök aktivera OCR (installera pytesseract eller easyocr)."
                   if not use_ocr else
                   "Kontrollera att PDF-filens text är läsbar."))
            return

        dlg = EquipmentScanDialog(result, self.db, self)
        dlg.exec()

    # ── Guided Risk Scenario mode ─────────────────────────────────────────────

    def start_scenario_mode(self, node_id=None):
        """Start guided Risk Scenario: Välj avvikelse → Orsak → Konsekvens → Safeguard."""
        if node_id:
            self._active_node_id = node_id
        if not self._active_node_id:
            QMessageBox.information(None, "Välj nod",
                "Välj en nod i trädet eller på P&ID:n innan du startar Risk Scenario.")
            return

        # Pick deviation — standard causes are organised per deviation
        devs = self.db.deviations(self._active_node_id) if hasattr(self.db, 'deviations') else []
        if not devs:
            QMessageBox.information(None, "Inga avvikelser",
                "Noden har inga avvikelser. Lägg till avvikelser i trädet först.")
            return
        dev_labels = [d['description'] for d in devs]
        choice, ok = QInputDialog.getItem(
            None, "Välj avvikelse",
            "Vilken avvikelse gäller scenariot för?",
            dev_labels, 0, False)
        if not ok:
            return
        chosen = next((d for d in devs if d['description'] == choice), None)
        if not chosen:
            return
        self._active_deviation_id = chosen['id']

        self._scenario_active = True
        self._scenario_step   = 1
        self._scenario_banner.setVisible(True)
        self._sc_add_sg_btn.setVisible(False)
        self._sc_finish_btn.setVisible(False)
        self._update_scenario_ui()
        self._set_mode(MODE_CAUSE_TEMPLATE)

    def _update_scenario_ui(self):
        step = self._scenario_step
        _ACTIVE  = "border-radius:3px; padding:1px 6px; font-size:10px; font-weight:bold; background:#ffffff; color:#1F4E79;"
        _DONE    = "border-radius:3px; padding:1px 6px; font-size:10px; font-weight:bold; background:#27ae60; color:white;"
        _WAITING = "border-radius:3px; padding:1px 6px; font-size:10px; font-weight:bold; background:#3a6fa3; color:#aac;"

        for i, pill in enumerate(self._step_pills):
            s = i + 1
            if s < step:
                pill.setStyleSheet(_DONE)
            elif s == step:
                pill.setStyleSheet(_ACTIVE)
            else:
                pill.setStyleSheet(_WAITING)

        instructions = {
            1: "Klicka på utrustning på P&ID:n — välj orsak ur standardlistan",
            2: "Klicka på konsekvens/målobjekt på P&ID:n",
            3: "Klicka på safeguard/barriär på P&ID:n",
        }
        self._sc_instr.setText(instructions.get(step, ""))

    def _sc_on_cause(self, cause_id):
        if not self._scenario_active:
            return
        self._scenario_step = 2
        self.set_active_cause(cause_id)
        self._update_scenario_ui()
        self._set_mode(MODE_CONSEQUENCE)

    def _sc_on_consequence(self, cons_id):
        if not self._scenario_active:
            return
        self._scenario_step = 3
        self.set_active_consequence(cons_id)
        self._update_scenario_ui()
        self._set_mode(MODE_SAFEGUARD)

    def _sc_on_safeguard(self, _sg_id):
        if not self._scenario_active:
            return
        # Show action buttons — stay in safeguard mode for adding more
        self._sc_instr.setText("Safeguard markerad! Lägg till fler eller slutför.")
        self._sc_add_sg_btn.setVisible(True)
        self._sc_finish_btn.setVisible(True)
        self._set_mode(MODE_SAFEGUARD)

    def _scenario_finish(self):
        self._scenario_active = False
        self._scenario_banner.setVisible(False)
        self._sc_add_sg_btn.setVisible(False)
        self._sc_finish_btn.setVisible(False)
        self._set_mode(MODE_NAV)

    def _scenario_abort(self):
        self._scenario_active = False
        self._scenario_banner.setVisible(False)
        self._sc_add_sg_btn.setVisible(False)
        self._sc_finish_btn.setVisible(False)
        self._set_mode(MODE_NAV)

    # ── Tag-database component lookup ─────────────────────────────────────────

    # Maps Excel category strings → component_type keys used in the app
    _CAT_TO_COMP = {
        'instrument':        'Instrument / Sensor',
        'givare':            'Instrument / Sensor',
        'reglerfunktion':    'Instrument / Sensor',
        'larm':              'Instrument / Sensor',
        'brytare':           'Instrument / Sensor',
        'mätvärde':          'Instrument / Sensor',
        'transmitter':       'Instrument / Sensor',
        'reglerventil':      'Ventil',
        'ventil':            'Ventil',
        'pump':              'Pump',
        'kompressor':        'Kompressor',
        'blåsmaskin':        'Kompressor',
        'tank':              'Tank / Kärl',
        'kärl':              'Tank / Kärl',
        'behållare':         'Tank / Kärl',
        'kolonn':            'Tank / Kärl',
        'värmeväxlare':      'Värmeväxlare',
        'kylare':            'Värmeväxlare',
        'kondensor':         'Värmeväxlare',
        'filter':            'Övrigt',
        'sil':               'Övrigt',
        'säkerhetsventil':   'Säkerhetsventil (PSV)',
        'avlastningsventil': 'Säkerhetsventil (PSV)',
        'rörledning':        'Rörledning',
    }

    def _comp_from_db_entry(self, entry: dict) -> str:
        """Map a tag_database entry's category to a component type string."""
        if not entry:
            return ''
        cat = str(entry.get('category', '')).lower()
        for key, comp in self._CAT_TO_COMP.items():
            if key in cat:
                return comp
        name = str(entry.get('name_sv', '') + ' ' + entry.get('name_en', '')).lower()
        for key, comp in self._CAT_TO_COMP.items():
            if key in name:
                return comp
        return ''

    def _learn_tag_type(self, tag: str, comp_type: str):
        """Implicitly learn prefix → comp_type from user's own selection.

        Stored as a confirmed entry in pid_identified_tags so it's used
        automatically next time the same prefix is encountered.
        """
        if not tag or not comp_type:
            return
        pfx = _equip_prefix_from_tag(tag)
        if not pfx or len(pfx) < 2:
            return
        try:
            if hasattr(self.db, 'upsert_pid_tag') and hasattr(self.db, 'confirm_pid_tag'):
                self.db.upsert_pid_tag(pfx, tag, '', comp_type)
                self.db.confirm_pid_tag(pfx, comp_type, True)
        except Exception:
            pass

    def _db_comp_for_tag(self, tag: str) -> str:
        """Look up the tag prefix in confirmed PID analysis, then tag database."""
        if not tag:
            return ''
        # For extended tags like 20-PCV-101, extract equipment prefix PCV
        pfx = _equip_prefix_from_tag(tag)
        if not pfx:
            m = re.match(r'^([A-Z]+)', tag.upper())
            if not m:
                return ''
            pfx = m.group(1)
        # 1. Confirmed project-specific mapping (highest priority)
        if hasattr(self.db, 'confirmed_comp_for_tag'):
            confirmed = self.db.confirmed_comp_for_tag(pfx)
            if confirmed:
                return confirmed
        # 2. Tag database lookup
        if hasattr(self.db, 'tag_code_lookup'):
            entry = self.db.tag_code_lookup(pfx)
            result = self._comp_from_db_entry(entry)
            if result:
                return result
        return ''

    def _load_mode_freqs(self):
        """Return {comp_type: {mode_desc: freq_per_year}} from DB."""
        if not hasattr(self.db, 'component_types'):
            return {}
        result = {}
        for ct in self.db.component_types():
            freqs = {}
            for fm in self.db.failure_modes(ct['id']):
                if fm['freq_per_year'] is not None:
                    freqs[fm['description']] = fm['freq_per_year']
            result[ct['name']] = freqs
        return result

    def _compute_f_level(self, freq_per_year):
        """Convert frequency (events/year) to F-level using matrix boundaries."""
        if not freq_per_year or freq_per_year <= 0:
            return 3   # default
        cfg        = self.db.get_risk_matrix() if hasattr(self.db, 'get_risk_matrix') else {}
        boundaries = sorted(
            float(b) for b in (cfg or {}).get('freq_boundaries',
                                              [1e-5, 1e-4, 1e-3, 0.01, 0.1, 1.0]))
        for i, b in enumerate(boundaries):
            if float(freq_per_year) < b:
                return i - 1
        return len(boundaries) - 1

    def try_reload_pdf(self):
        path = self.db.get_pid_path()
        if path and Path(path).exists() and HAS_PYMUPDF:
            layout_offsets = None
            if hasattr(self.db, 'get_pid_config_value'):
                raw = self.db.get_pid_config_value('board_layout')
                if raw:
                    try:
                        data = json.loads(raw)
                        layout_offsets = {int(k): v for k, v in data.items()}
                    except Exception:
                        layout_offsets = None
            # Only render sheets that exist in pid_sheets; fall back to all pages
            sheets = self.db.get_sheets()
            active_pages = ([int(s['physical_page']) for s in sheets]
                            if sheets else None)
            if self.viewer.load_pdf(path, page=0, layout_offsets=layout_offsets,
                                    active_pages=active_pages):
                self.db.ensure_sheets_initialized(self.viewer.page_count())
                self._rebuild_sheet_map()
                self._current_display_page = 0
                self._update_page_label()
                self._load_overlays()
                self.analyze_btn.setEnabled(True)
        else:
            # No P&ID in database — clear the canvas completely
            if self.viewer.pdf_doc is not None:
                try:
                    self.viewer.pdf_doc.close()
                except Exception:
                    pass
                self.viewer.pdf_doc = None
            for item in list(self.viewer._all_page_items.values()):
                try:
                    self.viewer._scene.removeItem(item)
                except Exception:
                    pass
            self.viewer._all_page_items.clear()
            self.viewer._page_offsets.clear()
            self.viewer._page_cache.clear()
            self.viewer._cache_order.clear()
            self.viewer.page_item = None
            self._load_overlays()   # clears all overlay items (pdf_doc is None → returns early)
            self._rebuild_sheet_map()
            self._current_display_page = 0
            self._update_page_label()
            self.viewer._show_placeholder(
                "Ingen P&ID inläst.\nImportera en PDF-fil med knappen ovan.")
            self.analyze_btn.setEnabled(False)
        self.export_btn.setEnabled(True)
