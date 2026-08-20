# HAZOP-appen — stilpatch (matchar mockupens design)

Jag kan inte skriva direkt till din lokala disk (bara läsa via länkad mapp). Nedan är
en färdig ersättning för `_get_windows11_stylesheet()` i `hazop.py` samt tre punktfixar.
Klistra in i din editor / låt Claude Code applicera filen.

## 1. Ersätt hela `_get_windows11_stylesheet()` (rad ~338) med:

```python
def _get_windows11_stylesheet():
    """Near-monochrome theme with one signal accent, matching the design mockup."""
    return """
    * {
        font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
        font-size: 9pt;
    }

    QMainWindow, QDialog, QWidget {
        background-color: #FBFBFA;
        color: #17191C;
    }

    QPushButton {
        background-color: #FFFFFF;
        color: #17191C;
        border: 1px solid #E2E3E1;
        border-radius: 4px;
        padding: 4px 10px;
        font-weight: 500;
    }
    QPushButton:hover {
        background-color: #F5F5F3;
        border-color: #CFD1CE;
    }
    QPushButton:pressed {
        background-color: #E8E9E6;
    }
    QPushButton:checked {
        background-color: #2F5FD0;
        color: #FFFFFF;
        border-color: #2F5FD0;
    }
    QPushButton:focus {
        outline: 2px solid #2F5FD0;
        outline-offset: 2px;
    }

    QLineEdit, QTextEdit, QPlainTextEdit {
        background-color: #FFFFFF;
        color: #17191C;
        border: 1px solid #CFD1CE;
        border-radius: 4px;
        padding: 4px 6px;
        selection-background-color: #2F5FD0;
        selection-color: #FFFFFF;
    }
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
        border: 2px solid #2F5FD0;
        padding: 3px 5px;
    }

    QComboBox {
        background-color: #FFFFFF;
        color: #17191C;
        border: 1px solid #CFD1CE;
        border-radius: 4px;
        padding: 4px 8px;
    }
    QComboBox:focus { border: 2px solid #2F5FD0; }
    QComboBox::drop-down { border: none; width: 20px; }
    QComboBox::down-arrow { image: none; }

    QTableWidget, QTableView {
        background-color: #FFFFFF;
        alternate-background-color: #F5F5F3;
        gridline-color: #EEEFEC;
        border: 1px solid #E2E3E1;
        border-radius: 4px;
    }
    QTableWidget::item, QTableView::item { padding: 4px; border: none; }
    QTableWidget::item:selected, QTableView::item:selected {
        background-color: #E6ECFA;
        color: #17191C;
    }
    QHeaderView::section {
        background-color: #F5F5F3;
        color: #8D9299;
        padding: 4px;
        border: none;
        border-bottom: 1px solid #E2E3E1;
        font-weight: 600;
        font-size: 8pt;
        letter-spacing: 0.5px;
    }

    QTreeWidget, QTreeView {
        background-color: #FFFFFF;
        alternate-background-color: #F5F5F3;
        border: 1px solid #E2E3E1;
        border-radius: 4px;
    }
    QTreeWidget::item:hover, QTreeView::item:hover { background-color: #F5F5F3; }
    QTreeWidget::item:selected, QTreeView::item:selected {
        background-color: #E6ECFA;
        color: #17191C;
    }

    QFrame { background-color: #FFFFFF; border: none; }
    QGroupBox {
        color: #17191C;
        border: 1px solid #E2E3E1;
        border-radius: 6px;
        margin-top: 8px;
        padding-top: 8px;
    }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; color: #8D9299; }

    QScrollBar:vertical { background-color: #F5F5F3; border: none; width: 12px; }
    QScrollBar::handle:vertical { background-color: #CFD1CE; border-radius: 6px; margin: 2px; min-height: 20px; }
    QScrollBar::handle:vertical:hover { background-color: #B3B7B2; }
    QScrollBar:horizontal { background-color: #F5F5F3; border: none; height: 12px; }
    QScrollBar::handle:horizontal { background-color: #CFD1CE; border-radius: 6px; margin: 2px; min-width: 20px; }
    QScrollBar::handle:horizontal:hover { background-color: #B3B7B2; }
    QScrollBar::add-line, QScrollBar::sub-line { border: none; background: none; }

    QDialog { background-color: #FFFFFF; }
    QSplitter::handle { background-color: #E2E3E1; }
    QSplitter::handle:hover { background-color: #CFD1CE; }

    QMenuBar { background-color: #FFFFFF; color: #17191C; border-bottom: 1px solid #E2E3E1; }
    QMenuBar::item:selected { background-color: #F5F5F3; }
    QMenu { background-color: #FFFFFF; color: #17191C; border: 1px solid #CFD1CE; border-radius: 4px; }
    QMenu::item:selected { background-color: #F5F5F3; }

    QToolTip {
        background-color: #17191C;
        color: #FFFFFF;
        border: none;
        border-radius: 4px;
        padding: 4px 8px;
    }
    """
```

## 2. Toggle-bar (rad ~17176) — byt de mörkblå tonerna mot appens grå/svart + accent:

```python
toggle_bar.setStyleSheet("background:#17191C;")
...
btn.setStyleSheet(
    "QPushButton{color:#fff;background:#2A2E34;border:none;"
    "border-radius:4px;padding:0 12px;font-weight:bold;}"
    "QPushButton:checked{background:#fff;color:#17191C;}")
...
lbl_db.setStyleSheet("color:#8D9299;font-size:11px;")
```

## 3. `RiskBadge` (rad ~4265) — matcha korten i mockupen (mörkare text, tunnare border):

```python
def set_empty(self):
    self.setText("—  (ingen frekvens)")
    self.setStyleSheet(
        "background:#F5F5F3; color:#8D9299; border-radius:4px; "
        "padding:2px 8px; border:1px solid #E2E3E1;")
```
(`update_risk()` kan stå kvar — den använder redan `risk_info()`-färger från risk­matrisen, vilket är rätt eftersom de är semantiska, inte tema-färger.)

## 4. Monospace för siffer-/kod-data (valfritt men rekommenderat)

Lägg till en `QFont("IBM Plex Mono", 9)` eller `QFont("Consolas", 9)` på fält som visar
taggar, ID:n och risknivåer (`RiskBadge`, `lbl_db`, cellerna i `ScenarioTablePanel` för
F/C-kolumnerna) — det är det som ger mockupens "precision engineering"-känsla. Om
IBM Plex Mono inte är installerat på klientmaskinen faller Qt tillbaka på Consolas/monospace
automatiskt.

## Sammanfattning av designval som överförs
- Nästan-monokrom grå/vit bakgrund, **en** signalfärg (`#2F5FD0`, samma blå som mockupen)
- Selektion/hover byts från Windows-blått (`#0078D4`) till en mjukare ljusblå highlight (`#E6ECFA`) + den mättade blåa bara på aktiva/checked-knappar
- Hairline-borders (`#E2E3E1`) istället för tyngre `#CACACA`
- Tabellrubriker gråa versaler med bokstavsavstånd, som i worksheet-mockupen

Detta ändrar *bara* utseendet (QSS) — ingen logik, inga scheman, inga signaler rörs.
