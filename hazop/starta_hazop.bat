@echo off
echo Kontrollerar Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo Python hittades inte. Installera Python 3.10+ fran python.org
    pause
    exit /b 1
)

echo Installerar/uppdaterar beroenden...
pip install -q PyQt6 openpyxl reportlab PyMuPDF rapidocr_onnxruntime

echo Startar HAZOP Tool...
python "%~dp0hazop.py"
pause
