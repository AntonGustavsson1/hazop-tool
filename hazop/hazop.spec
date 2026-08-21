# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build spec for the HAZOP Tool (2026-08-21, see NOTES.md
# "Paketera HAZOP-appen som en installationsfil"). --onedir build (faster
# startup / easier to debug than --onefile; the Inno Setup installer hides
# the folder structure from end users anyway).
#
# Build with:
#   pyinstaller hazop.spec
# Output lands in dist/HazopTool/ (the onedir bundle) -- HazopTool.exe is
# the entry point inside it.

import sys

block_cipher = None

# rapidocr_onnxruntime/onnxruntime dynamically load their execution
# providers and bundle non-Python data (ONNX model files, provider DLLs)
# that PyInstaller's static import analysis can't see on its own --
# collect_all() pulls in everything the package ships, not just what a
# `import` scan finds. This is the app's PRIMARY OCR engine (required
# dependency, tried before Tesseract/EasyOCR -- see equipment_detection.py)
# so it must actually work in the frozen build, not just import cleanly.
from PyInstaller.utils.hooks import collect_all

datas = [('icons', 'icons')]
binaries = []
hiddenimports = []

for pkg in ('rapidocr_onnxruntime', 'onnxruntime'):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

# equipment_detection.py imports easyocr/pytesseract conditionally
# (try/except ImportError, gated behind HAS_EASYOCR/HAS_TESSERACT) as
# optional FALLBACK OCR engines behind rapidocr_onnxruntime, the app's
# required, tried-first engine (see NOTES.md "Paketera HAZOP-appen...").
# If easyocr happens to be installed in the machine doing the build (as
# it is here, from earlier manual `pip install easyocr` testing per
# CLAUDE.md's OCR section), PyInstaller's static import analysis bundles
# it anyway -- and easyocr drags in torch+torchvision+scipy+matplotlib,
# ballooning the build from ~150-200MB to over 850MB for a feature that
# was explicitly decided NOT to ship (rapidocr alone is enough: no
# external binary, already a required dependency). Excluding them here is
# safe -- the app already handles HAS_EASYOCR/HAS_TESSERACT being False
# at runtime, that's the whole point of the try/except around the import.
EXCLUDE_OPTIONAL_OCR = [
    'easyocr', 'pytesseract',
    'torch', 'torchvision', 'scipy', 'matplotlib', 'sympy',
]

a = Analysis(
    ['hazop.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDE_OPTIONAL_OCR,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HazopTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='packaging/app_icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='HazopTool',
)
