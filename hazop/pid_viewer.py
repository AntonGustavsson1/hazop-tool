#!/usr/bin/env python3
"""P&ID viewer module for the HAZOP tool."""

import re
import json
import os
import shutil
import tempfile
import datetime
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
    QGraphicsRectItem, QGraphicsSimpleTextItem, QFrame, QSpinBox, QAbstractSpinBox, QCheckBox, QGroupBox,
    QSlider, QColorDialog, QFileDialog, QMessageBox, QInputDialog,
    QSizePolicy, QMenu, QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressDialog, QApplication, QGridLayout, QTextEdit, QButtonGroup,
    QScrollArea,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRectF, QThread, QPoint
from PyQt6.QtGui import (
    QColor, QPen, QBrush, QPainterPath, QPixmap, QImage, QFont,
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

_SG_TYPES    = ['BPCS', 'SIS', 'Mekanisk', 'Administrativ', 'Övrigt']
_RRF_VALUES  = [1, 10, 100, 1000, 10000]
_RRF_LABELS  = ['1 – Ingen', '10 – RRF10', '100 – RRF100', '1000 – RRF1000', '10000 – RRF10000']

Z_PAGE      = 0
Z_HIGHLIGHT = 1   # tag highlights between page and connections
Z_CONNECT   = 3
Z_OVERLAY   = 5
Z_TEMP      = 10

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
    zone_drawn    = pyqtSignal(object, int)                # (QRectF pdf_coords, page)
    zone_resized  = pyqtSignal(str, int, float, float, float, float)  # type_,id_,cx,cy,w,h

    # Keys for QGraphicsItem.setData / .data
    _DATA_TYPE      = 0    # 'cause' | 'consequence' | 'safeguard' | 'markup'
    _DATA_ID        = 1    # database id
    _DATA_MARKUP_ID = 2    # markup id (for markup items)
    _DATA_MARKUP_PTS = 3   # stores PDF points list on path/text items
    _DATA_ZONE_KEY  = 4    # (marker_type, marker_id) on zone rect/handle items
    _DATA_ZONE_CIDX = 5    # corner index 0=TL,1=TR,2=BR,3=BL on zone handle items

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
        self._edit_mu_id            = None
        self._vertex_handles: list  = []
        self._drag_mode             = None   # 'vertex' | 'item' | None
        self._drag_vertex_idx       = None
        self._drag_start_scene      = None
        self._drag_original_pts: list = []
        self._drag_current_pts: list  = []
        self._drag_threshold_exceeded = False
        self._drag_item_origins: list = []  # [(QGraphicsItem, QPointF)] for text/comment
        self._inline_edit_widget = None

        self._smart_start_pdf   = None   # (pdf_x, pdf_y) first click
        self._smart_end_pdf     = None
        self._smart_paths       = []     # list of paths (each = [[pdf_x,pdf_y],...])
        self._smart_path_idx    = 0
        self._smart_preview     = []     # QGraphicsItem preview items on scene
        self._smart_tracer      = None   # SmartPipeTracer, cached per page
        self._smart_tracer_page = -1

        self._placeholder = None
        self._show_placeholder("Öppna en P&ID-fil (PDF) för att börja.")
        self.set_mode(MODE_NAV)

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

    def load_pdf(self, path, page=0):
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
        self._render_page()
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
        if n == self.current_page and self.page_item is not None:
            return
        self.current_page = n
        self._cancel_drawing()
        self._render_page()

    def scene_to_pdf(self, point):
        # With SVG rendering: scene coords == PDF coords (render_scale=1.0)
        # With raster fallback: apply the raster scale
        return (point.x() / self.render_scale, point.y() / self.render_scale)

    def pdf_to_scene(self, x, y):
        return QPointF(x * self.render_scale, y * self.render_scale)

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
                           color_hex, opacity, line_width, visible=True, font_size=12):
        """Render a node_markup item.  type_: polygon|polyline|text|comment"""
        items = []
        c = QColor(color_hex)
        border_alpha = int(opacity * 210)
        fill_alpha   = int(opacity * 52)
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

        self._markup_items[mu_id] = items
        self._markup_types[mu_id] = type_
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

    def _clear_edit_handles(self):
        for h in self._vertex_handles:
            try: self._scene.removeItem(h)
            except Exception: pass
        self._vertex_handles  = []
        self._edit_mu_id      = None
        self._drag_mode       = None
        self._drag_vertex_idx = None
        self._drag_original_pts = []
        self._drag_current_pts  = []
        self._drag_item_origins = []
        self._drag_threshold_exceeded = False

    def _select_for_edit(self, mu_id):
        """Select a markup item and show vertex handles."""
        self._clear_edit_handles()
        self._edit_mu_id = mu_id

        # Read stored PDF points from item
        pts_pdf = None
        for gi in self._markup_items.get(mu_id, []):
            pts_pdf = gi.data(self._DATA_MARKUP_PTS)
            if pts_pdf:
                break
        if not pts_pdf:
            return

        pts_scene = [self.pdf_to_scene(*p) for p in pts_pdf]
        self._drag_current_pts = list(pts_scene)

        typ = self._markup_types.get(mu_id, 'polygon')

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
                for gi in self._markup_items.get(mu_id, [])
            ]

        self.highlight_markup(mu_id)

    def _update_edit_path(self, pts_scene):
        """Rebuild the path/positions of the currently edited markup."""
        if self._edit_mu_id not in self._markup_items:
            return
        typ = self._markup_types.get(self._edit_mu_id, 'polygon')

        if typ in ('polygon', 'polyline') and pts_scene:
            for gi in self._markup_items[self._edit_mu_id]:
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

    def _update_handle_positions(self, pts_scene):
        for i, handle in enumerate(self._vertex_handles):
            if i < len(pts_scene):
                handle.setPos(pts_scene[i])

    def _finish_edit_drag(self):
        """Called on mouseRelease after a drag — save new points."""
        if self._edit_mu_id is None:
            return
        new_pdf_pts = [list(self.scene_to_pdf(pt)) for pt in self._drag_current_pts]
        # Update stored data on item
        for gi in self._markup_items.get(self._edit_mu_id, []):
            if gi.data(self._DATA_MARKUP_PTS) is not None:
                gi.setData(self._DATA_MARKUP_PTS, new_pdf_pts)
                break
        self.markup_moved.emit(self._edit_mu_id, new_pdf_pts)

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
            cx = scene_rect.center().x() / rs
            cy = scene_rect.center().y() / rs
            w  = scene_rect.width()       / rs
            h  = scene_rect.height()      / rs
            type_, id_ = key
            self.zone_resized.emit(type_, id_, cx, cy, w, h)
        self._zone_resize_key   = None
        self._zone_resize_cidx  = None
        self._zone_resize_start = None
        self._zone_resize_orig  = None
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def _show_context_menu(self, sp, global_pos):
        menu = QMenu(self.viewport())
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
            short = label[:30]
            txt = QGraphicsSimpleTextItem(short)
            f2 = QFont(); f2.setPointSize(8)
            txt.setFont(f2)
            txt.setBrush(QBrush(QColor(120, 0, 0)))
            txt.setPos(center.x() + r + 3, center.y() - 8)
            txt.setZValue(Z_OVERLAY + 1)
            self._add_tracked(txt, 'cause')
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
            txt = QGraphicsSimpleTextItem(description[:30])
            f2 = QFont(); f2.setPointSize(8)
            txt.setFont(f2)
            txt.setBrush(QBrush(QColor(20, 100, 20)))
            txt.setPos(center.x() + r + 3, center.y() - 8)
            txt.setZValue(Z_OVERLAY + 1)
            self._add_tracked(txt, 'safeguard')
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

    def clear_overlays(self):
        for item in list(self._scene.items()):
            if item is self.page_item or item is self._placeholder:
                continue
            if item.zValue() >= Z_CONNECT:
                try: self._scene.removeItem(item)
                except Exception: pass
        if self._pending_path_item is not None:
            try: self._scene.removeItem(self._pending_path_item)
            except Exception: pass
            self._pending_path_item = None
        # Clear per-type item lists and zone rect dict (items gone from scene)
        for key in self._type_items:
            self._type_items[key] = []
        self._zone_rects.clear()

    def mousePressEvent(self, event):
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
        elif self.mode == MODE_MARKUP_SELECT:
            if event.button() == Qt.MouseButton.LeftButton:
                view_pos = event.position().toPoint()
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

        if event.button() == Qt.MouseButton.RightButton and self.mode == MODE_NAV:
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
                pdf_rect = QRectF(rect.x() / rs, rect.y() / rs,
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
            pdf_rect = QRectF(rect.x() / rs, rect.y() / rs,
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
                self._finish_edit_drag()
            elif not self._drag_threshold_exceeded and self._edit_mu_id is not None:
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
                if self._drag_mode == 'vertex' and self._drag_vertex_idx is not None:
                    new_pts = list(self._drag_current_pts)
                    idx = self._drag_vertex_idx
                    new_pts[idx] = self._drag_original_pts[idx] + delta
                    self._drag_current_pts = new_pts
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

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        self._pen_color             = QColor(255, 140, 0)
        self._active_node_id              = None
        self._active_cause_id             = None
        self._active_consequence_id       = None
        self._active_deviation_id         = None   # set during MODE_CAUSE_TEMPLATE
        self._pending_markup_pts          = None
        self._pending_markup_page         = None
        self._pending_secondary_cause_id    = None   # set to queue secondary marker after instrument cause
        self._pending_secondary_comp_type  = ''
        self._pending_secondary_tag        = ''
        self._pending_secondary_deviation_id   = None   # for re-opening dialog after secondary placement
        self._pending_secondary_preselect_type = ''
        self._current_display_page  = 0
        self._sheet_map: dict       = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout(); bar.setSpacing(4)

        self.open_btn = QPushButton("📂 Öppna P&ID")
        self.open_btn.clicked.connect(self._open_pdf)
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
        self._active_place_type  = None   # 'cause' | 'consequence' | 'safeguard'
        self._active_place_id    = None
        self._pending_zone_pdf   = None   # QRectF while zone dialog chain is open
        self.viewer.marker_clicked.connect(
            lambda t, i: self.marker_navigated.emit(t, i))
        self.viewer.markup_moved.connect(self.markup_moved)
        self.viewer.markup_label_edited.connect(self.markup_label_edited)
        self.viewer.markup_duplicate_requested.connect(self.markup_duplicate_requested)

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

    def _open_pdf(self):
        if not HAS_PYMUPDF:
            QMessageBox.warning(self, "PyMuPDF saknas",
                "Installera med:\n    pip install PyMuPDF\nStarta sedan om.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Öppna P&ID", "", "PDF-dokument (*.pdf);;Alla filer (*.*)")
        if not path:
            return

        working    = self._working_pdf_path()
        has_existing = working.exists()
        created_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

        if has_existing:
            dlg = PIDImportDialog(has_existing=True, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            rev_label = dlg.label() or created_at
            rev_notes = dlg.notes()

            if dlg.is_new_revision():
                # Copy source to working path and reset
                try:
                    shutil.copy2(path, working)
                except Exception as e:
                    QMessageBox.critical(self, "Fel", f"Kunde inte kopiera PDF:\n{e}")
                    return
                if not self.viewer.load_pdf(str(working), page=0):
                    QMessageBox.warning(self, "Fel", "Kunde inte öppna PDF-filen.")
                    return
                self.db.set_pid_path(str(working))
                self.db.clear_sheets()
                self.db.add_revision(rev_label, rev_notes, str(working), created_at)
                self.db.ensure_sheets_initialized(self.viewer.page_count())
                self._current_display_page = 0
            else:
                # Nya blad — merge new pages into working copy via temp file
                try:
                    existing_doc    = fitz.open(str(working))
                    existing_pg_cnt = existing_doc.page_count
                    new_doc         = fitz.open(path)
                    n_new           = new_doc.page_count
                    existing_doc.insert_pdf(new_doc)
                    new_doc.close()

                    # Close viewer before replacing the file
                    if self.viewer.pdf_doc is not None:
                        try:
                            self.viewer.pdf_doc.close()
                        except Exception:
                            pass
                        self.viewer.pdf_doc = None

                    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.pdf', dir=str(working.parent))
                    os.close(tmp_fd)
                    existing_doc.save(tmp_path, garbage=4, deflate=True)
                    existing_doc.close()
                    shutil.move(tmp_path, str(working))
                except Exception as e:
                    QMessageBox.critical(self, "Fel vid sammanslagning",
                                         f"Kunde inte sammanfoga PDF:\n{e}")
                    return

                keep_phys = self.viewer.current_page
                if not self.viewer.load_pdf(str(working), page=keep_phys):
                    QMessageBox.warning(self, "Fel", "Kunde inte öppna sammanfogad PDF.")
                    return

                if self.db.get_display_page_count() == 0:
                    self.db.ensure_sheets_initialized(existing_pg_cnt)

                rev_id = self.db.add_revision(rev_label, rev_notes, str(working), created_at)
                physical_pages = list(range(existing_pg_cnt, existing_pg_cnt + n_new))
                sheet_names    = [f"Blad {existing_pg_cnt + i + 1}" for i in range(n_new)]
                self.db.append_sheets(physical_pages, sheet_names, rev_id)
        else:
            # First import — copy to working path, no dialog
            try:
                shutil.copy2(path, working)
            except Exception as e:
                QMessageBox.critical(self, "Fel", f"Kunde inte kopiera PDF:\n{e}")
                return
            if not self.viewer.load_pdf(str(working), page=0):
                QMessageBox.warning(self, "Fel", "Kunde inte öppna PDF-filen.")
                return
            self.db.set_pid_path(str(working))
            self.db.clear_sheets()
            self.db.add_revision(created_at, '', str(working), created_at)
            self.db.ensure_sheets_initialized(self.viewer.page_count())
            self._current_display_page = 0

        self._rebuild_sheet_map()
        self._update_page_label()
        self._load_overlays()
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
        self._load_overlays()

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
        scene_pt = self.viewer.pdf_to_scene(x_pdf, y_pdf)
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

        if chosen is a_cause:
            dev_id   = self._active_deviation_id or 0
            detected = self._db_comp_for_tag(tag) if tag else comp_type
            suggested_desc = self.viewer._text_in_rect(pdf_rect) if HAS_PYMUPDF and self.viewer.pdf_doc else ''
            self.cause_placement_requested.emit(dev_id, tag or '', detected, center_scene, page, suggested_desc)
        elif chosen is a_cons:
            self._on_consequence_click(center_scene, page, tag)
        elif chosen is a_sg:
            self._on_safeguard_click(center_scene, page, tag)

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
        page = self.viewer.current_page

        for node in self.db.nodes():
            nd       = dict(node)
            raw_pts  = nd.get('markup_points', '') or ''
            nd_page  = int(nd.get('pid_page', 0) or 0)
            if not raw_pts or nd_page != page:
                continue
            try:
                points = [(float(p[0]), float(p[1])) for p in json.loads(raw_pts)]
                style  = json.loads(nd.get('markup_style', '') or '{}')
            except Exception:
                continue
            if points:
                self.viewer.add_node_overlay(nd['id'], points, style, nd.get('name', ''))

        # New-style node markup items
        if hasattr(self.db, 'node_markups_for_page'):
            for mu in self.db.node_markups_for_page(page):
                m = dict(mu)
                try:
                    pts = json.loads(m.get('points', '[]') or '[]')
                except Exception:
                    pts = []
                self.viewer.add_markup_overlay(
                    m['id'], m.get('type', 'polygon'), pts,
                    m.get('label', ''), m.get('color', '#1565C0'),
                    float(m.get('opacity', 0.45)), int(m.get('line_width', 2)),
                    bool(m.get('visible', 1))
                )

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
            self.viewer.add_consequence_marker(md['consequence_id'], md['x'], md['y'], desc,
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

        # Draw connections — use lists so multiple markers per item all get lines
        cause_pos = {}
        for m in self.db.cause_markers_for_page(page):
            cause_pos.setdefault(m['cause_id'], []).append(
                self.viewer.pdf_to_scene(m['x'], m['y']))
        cons_pos = {}
        for m in self.db.consequence_markers_for_page(page):
            cons_pos.setdefault(m['consequence_id'], []).append(
                self.viewer.pdf_to_scene(m['x'], m['y']))
        sg_pos = {}
        for m in self.db.safeguard_markers_for_page(page):
            sg_pos.setdefault(m['safeguard_id'], []).append(
                self.viewer.pdf_to_scene(m['x'], m['y']))

        for cid, cpos_list in cons_pos.items():
            c = self.db.get_consequence(cid)
            if c and c['cause_id'] in cause_pos:
                for cpos in cpos_list:
                    for capos in cause_pos[c['cause_id']]:
                        self.viewer.add_connection_line(capos, cpos, '#c0392b')

        for sid, spos_list in sg_pos.items():
            s = self.db.get_safeguard(sid)
            if s and s['consequence_id'] in cons_pos:
                for spos in spos_list:
                    for kpos in cons_pos[s['consequence_id']]:
                        self.viewer.add_connection_line(kpos, spos, '#27ae60', dashed=True)

        # Draw tag highlights (yellow = known, green = defined as cause)
        self._draw_tag_highlights()

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
        page = self.viewer.current_page
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

    def _on_viewer_markup_drawn(self, type_, pts, page):
        """Called when user finishes drawing in the viewer; route to NodeMarkupPanel."""
        node_id = self._active_node_id
        if node_id is None:
            return
        if type_ == 'text':
            # Auto-fill with the node name from DB
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
            if self.viewer.load_pdf(path, page=0):
                self.db.ensure_sheets_initialized(self.viewer.page_count())
                self._rebuild_sheet_map()
                self._current_display_page = 0
                self._update_page_label()
                self._load_overlays()
                self.analyze_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
