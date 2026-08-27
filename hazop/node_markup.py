#!/usr/bin/env python3
"""Node markup / red markup ribbon panels and dialogs — split out of
hazop.py 2026-08-17, see NOTES.md "Förenkla koden + dela upp hazop.py i
fler filer"."""

from functools import partial

from PyQt6.QtWidgets import (
    QWidget, QDialog, QDialogButtonBox, QFrame, QLabel, QLineEdit,
    QPushButton, QFormLayout, QGridLayout, QHBoxLayout, QVBoxLayout,
    QTabWidget, QTabBar, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QTextEdit, QSlider, QSpinBox, QMenu, QApplication,
    QCheckBox, QColorDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QRect
from PyQt6.QtGui import QColor, QFont, QPen, QPainter, QPixmap, QIcon

from constants import NODE_T, CAUSE_T, CONS_T, SG_T, DEV_T, MARKUP_COLORS, CONFIG
from database import Database, freq_to_f_level
from pid_viewer import _icon, _mk_icon, _mk_pm, _EMOJI_ICON, _RED_MARKUP_SYMBOLS
from ui_helpers import freq_axis_label
from tree_panel import CauseObjectPopup, RRFPopup
from scenario_panel import ConsCategoryMatrixPopup


# ── Style popup ───────────────────────────────────────────────────────────────

class _StylePopup(QWidget):
    """Per-tool flyout popup — appears to the left of the clicked tool button."""

    _TOOL_NAMES = {
        'polygon':  'Rita polygon',
        'polyline': 'Rita polylinje',
        'text':     'Lägg ut nodnamn',
        'comment':  'Lägg till kommentar',
        # 'smart' ("Smart polylinje") removed 2026-08-26 -- see
        # NOTES.md and archive/smart_pipe_tracer.py.
    }

    def __init__(self, ribbon, parent=None):
        super().__init__(parent,
                         Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(
            "QWidget{background:#fff;border-radius:4px;}"
            "QLabel{font-size:10px;color:#444;border:none;}")
        self._ribbon = ribbon

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(6)

        # Title
        self._title_lbl = QLabel()
        f = QFont(); f.setBold(True); f.setPointSize(10)
        self._title_lbl.setFont(f)
        outer.addWidget(self._title_lbl)

        sep = QLabel(); sep.setFixedHeight(CONFIG['H_SEP_LINE'])
        sep.setStyleSheet("background:#E2E3E1;border:none;")
        outer.addWidget(sep)

        # Colour swatches (always shown)
        color_widget = QWidget()
        crow = QHBoxLayout(color_widget)
        crow.setContentsMargins(0, 0, 0, 0); crow.setSpacing(3)
        crow.addWidget(QLabel("Färg:"))
        self._cbts = []
        for hc in MARKUP_COLORS:
            cb = QPushButton(); cb.setFixedSize(22, 22)
            cb.setStyleSheet(f"background:{hc};border:2px solid transparent;"
                             f"border-radius:3px;")
            cb.clicked.connect(partial(self._pick, hc))
            crow.addWidget(cb); self._cbts.append((hc, cb))
        pal = QPushButton("···"); pal.setFixedSize(28, 22)
        pal.setStyleSheet("font-size:10px;border:1px solid #ccc;border-radius:3px;")
        pal.clicked.connect(self._open_palette)
        crow.addWidget(pal); crow.addStretch()
        outer.addWidget(color_widget)

        self._bar = QLabel(); self._bar.setFixedHeight(CONFIG['H_COLOR_STRIP'])
        self._bar.setStyleSheet("border:none;")
        outer.addWidget(self._bar)

        sep2 = QLabel(); sep2.setFixedHeight(CONFIG['H_SEP_LINE'])
        sep2.setStyleSheet("background:#eee;border:none;")
        outer.addWidget(sep2)

        # Opacity row (polygon, polyline, comment)
        self._opacity_row = QWidget()
        orow = QHBoxLayout(self._opacity_row)
        orow.setContentsMargins(0, 0, 0, 0)
        orow.addWidget(QLabel("Opacitet:"))
        self._op_sl = QSlider(Qt.Orientation.Horizontal)
        self._op_sl.setRange(10, 90)
        orow.addWidget(self._op_sl)
        self._op_lbl = QLabel(); self._op_lbl.setFixedWidth(CONFIG['W_OPACITY_LBL'])
        orow.addWidget(self._op_lbl)
        self._op_sl.valueChanged.connect(
            lambda v: (ribbon._apply_opacity(v), self._op_lbl.setText(f"{v}%")))
        outer.addWidget(self._opacity_row)

        # Line width row (polygon, polyline)
        self._width_row = QWidget()
        wrow = QHBoxLayout(self._width_row)
        wrow.setContentsMargins(0, 0, 0, 0); wrow.setSpacing(5)
        wrow.addWidget(QLabel("Tjocklek:"))
        self._w_sp = QSpinBox(); self._w_sp.setRange(1, 99)
        self._w_sp.setMaximumWidth(58)
        self._w_sp.valueChanged.connect(ribbon._apply_width)
        wrow.addWidget(self._w_sp); wrow.addStretch()
        outer.addWidget(self._width_row)

        # Font size row (text, comment)
        self._font_row = QWidget()
        frow = QHBoxLayout(self._font_row)
        frow.setContentsMargins(0, 0, 0, 0); frow.setSpacing(5)
        frow.addWidget(QLabel("Textstorlek:"))
        self._f_sp = QSpinBox(); self._f_sp.setRange(6, 99)
        self._f_sp.setMaximumWidth(58)
        self._f_sp.valueChanged.connect(ribbon._apply_font)
        frow.addWidget(self._f_sp); frow.addStretch()
        outer.addWidget(self._font_row)

        # Snap row (polygon, polyline)
        self._snap_row = QWidget()
        srow = QHBoxLayout(self._snap_row)
        srow.setContentsMargins(0, 0, 0, 0)
        self._snap_cb = QCheckBox("Snap till befintliga punkter")
        self._snap_cb.setChecked(True)
        self._snap_cb.toggled.connect(ribbon._apply_snap)
        srow.addWidget(self._snap_cb); srow.addStretch()
        outer.addWidget(self._snap_row)

        self.setMinimumWidth(CONFIG['W_DIALOG_MD'])

    def _configure_for(self, tool):
        self._title_lbl.setText(self._TOOL_NAMES.get(tool, tool))
        self._opacity_row.setVisible(tool in ('polygon', 'polyline', 'comment'))
        self._width_row.setVisible(tool in ('polygon', 'polyline'))
        self._font_row.setVisible(tool in ('text', 'comment'))
        self._snap_row.setVisible(tool in ('polygon', 'polyline'))

    def show_for(self, tool, btn):
        self._configure_for(tool)
        self._sync()
        self.adjustSize()
        # Position to the left of the tool button
        gp = btn.mapToGlobal(btn.rect().topLeft())
        self.move(gp.x() - self.width() - 4, gp.y())
        self.show()

    def _sync(self):
        r = self._ribbon
        self._bar.setStyleSheet(f"background:{r._color};border-radius:2px;border:none;")
        self._op_sl.blockSignals(True); self._op_sl.setValue(int(r._opacity * 100))
        self._op_sl.blockSignals(False)
        self._op_lbl.setText(f"{int(r._opacity * 100)}%")
        self._w_sp.blockSignals(True); self._w_sp.setValue(r._width)
        self._w_sp.blockSignals(False)
        self._f_sp.blockSignals(True); self._f_sp.setValue(r._font_size)
        self._f_sp.blockSignals(False)
        self._snap_cb.blockSignals(True); self._snap_cb.setChecked(r._snap)
        self._snap_cb.blockSignals(False)
        for hc, cb in self._cbts:
            cb.setStyleSheet(
                f"background:{hc};border:2px solid "
                f"{'#333' if hc == r._color else 'transparent'};border-radius:3px;")

    def _pick(self, hex_c):
        self._ribbon._apply_color(hex_c)
        self._bar.setStyleSheet(f"background:{hex_c};border-radius:2px;border:none;")
        for hc, cb in self._cbts:
            cb.setStyleSheet(
                f"background:{hc};border:2px solid "
                f"{'#333' if hc == hex_c else 'transparent'};border-radius:3px;")

    def _open_palette(self):
        self.hide()
        c = QColorDialog.getColor(QColor(self._ribbon._color), None, "Välj färg")
        if c.isValid():
            self._ribbon._apply_color(c.name())

    def showEvent(self, event):
        self._sync()
        super().showEvent(event)


class PropertiesRibbon(QWidget):
    """Narrow (62 px) vertical ribbon replacing the right detail panel.

    Shows icon buttons for each editable field of the selected item.
    Each button opens a small floating popup for editing that field.

    2026-08-19: the P&ID node-markup toolbar (drawing tools, "Lägg ut
    P&ID-symbol", color, visibility, bottom-panel switch — formerly the
    separate NodeMarkupPanel widget docked right next to this one) is
    merged straight into this ribbon's own NODE_T button set (see
    NOTES.md "Slå ihop nodmarkup i nodinställningar" — Anton: "jag vill
    att den för nodmarkup integreras i den med nodinställningar så det
    bara blir en"). It only appears while self._markup_active (toggled
    by the ✏️ button _build_markup_toggle() adds) — see
    _build_markup_tools() for the ported widgets/state and
    enter_markup_mode()/exit_markup_mode() for the public API MainWindow
    drives it with, mirroring NodeMarkupPanel's old load()/setVisible().
    """
    item_changed = pyqtSignal()   # emitted after any field is saved

    # ── Node markup toolbar signals (2026-08-19, merged in from the old
    # NodeMarkupPanel — same names/payloads, so MainWindow's existing
    # slots just get reconnected to this class instead) ────────────────
    tool_changed             = pyqtSignal(str)
    all_vis_toggled          = pyqtSignal(bool)
    style_changed            = pyqtSignal(str, float, int)   # color, opacity, line_width
    snap_changed             = pyqtSignal(bool)
    navigate_node_requested  = pyqtSignal(int)   # node_id
    bottom_panel_toggled     = pyqtSignal(bool)  # True = show HAZOP scenario, False = Nodmarkeringar
    place_symbol_requested   = pyqtSignal()
    # Replaces NodeMarkupPanel's one-shot `closed` signal — the toggle
    # button can flip markup-edit mode back on for the SAME node without
    # needing to leave/reselect it in the tree (a small improvement over
    # the old close-only button, which had no way back in).
    markup_mode_toggled      = pyqtSignal(bool)

    _BTN_SZ  = 50
    _WIDTH   = 62
    _BTN_SS  = (
        "QPushButton{border:1px solid #E2E3E1;border-radius:5px;"
        "background:#FFFFFF;padding:0px;font-size:15px;}"
        "QPushButton:hover{background:#F5F5F3;border-color:#CFD1CE;}"
        "QPushButton:pressed{background:#E8E9E6;}"
    )
    _GRP_SS  = "font-size:8px;color:#8D9299;margin:0px;padding:0px;"
    # Shared style for the OK button inside floating popups
    _OK_BTN_SS = ("background:#2F5FD0;color:white;border:none;"
                  "border-radius:4px;padding:4px 16px;")
    # Checkable-button style for the markup toggle + tool buttons —
    # ported from NodeMarkupPanel._btn_ss (2026-08-19).
    _TOOL_BTN_SS = (
        "QPushButton{border:1px solid #E2E3E1;border-radius:5px;"
        "background:#FFFFFF;padding:0px;}"
        "QPushButton:checked{background:#2F5FD0;border-color:#2F5FD0;}"
        "QPushButton:hover:!checked{background:#F5F5F3;border-color:#CFD1CE;}")

    _MARKUP_TOOLS = [
        ('select',   'select',   'Välj/flytta'),
        ('polygon',  'polygon',  'Rita polygon'),
        ('polyline', 'polyline', 'Rita polylinje'),
        # ('smart', 'smart', 'Smart polylinje') removed 2026-08-26 -- see
        # NOTES.md and archive/smart_pipe_tracer.py.
        ('text',     'text',     'Lägg ut nodnamn'),
        ('comment',  'comment',  'Lägg till kommentar'),
    ]

    def __init__(self, db, main_window=None, parent=None):
        super().__init__(parent)
        self.db          = db
        self._mw         = main_window
        self._type       = None
        self._id         = None
        self._btns       = []

        # ── Node markup toolbar state (2026-08-19, merged in from the
        # old NodeMarkupPanel — see class docstring) ────────────────────
        self._markup_active = False
        self._current_tool  = 'select'
        self._color         = MARKUP_COLORS[5]
        self._opacity       = 0.45
        self._width         = 12
        self._font_size     = 24
        self._snap          = True
        self._tool_btns     = {}
        self._style_popup   = None

        self.setFixedWidth(self._WIDTH)
        self.setStyleSheet("background:#FBFBFA;")
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(6, 8, 6, 8)
        self._outer.setSpacing(3)
        self._outer.addStretch()

    @property
    def node_id(self):
        """Convenience alias for MainWindow call sites that used to read
        NodeMarkupPanel.node_id — self._id already IS the node id whenever
        self._type == NODE_T (the generic "currently shown item" field
        every type uses)."""
        return self._id if self._type == NODE_T else None

    # ── Public API ────────────────────────────────────────────────────────────
    def set_item(self, type_: int, id_: int):
        old_type = self._type
        self._type = type_
        self._id   = id_
        if type_ != old_type:   # skip widget churn when type is unchanged
            self._rebuild()

    def clear(self):
        self._type = None
        self._id   = None
        self._rebuild()

    def enter_markup_mode(self, node_id):
        """Bind and show the P&ID markup toolbar for node_id — mirrors
        the old, separate NodeMarkupPanel.load() + setVisible(True)
        (2026-08-19, see NOTES.md "Slå ihop nodmarkup i
        nodinställningar"). Idempotent: safe to call again for the same
        or a different node while already active (matches
        MainWindow._on_edit_node_markup's own "rebinding is idempotent"
        note) — callers are expected to have already called
        set_item(NODE_T, node_id) so self._id/self._type are current."""
        self._markup_active = True
        if self._type == NODE_T:
            self._rebuild()
            self._on_tool(self._current_tool)

    def exit_markup_mode(self):
        """Mirrors the old NodeMarkupPanel.setVisible(False) — hides the
        markup toolbar section but leaves the rest of this ribbon (the
        plain node-settings buttons) untouched."""
        if not self._markup_active:
            return
        self._markup_active = False
        if self._type == NODE_T:
            self._rebuild()

    def set_bottom_toggle_checked(self, checked):
        """Programmatic set (e.g. on entering markup-edit mode) that must
        not re-emit bottom_panel_toggled — MainWindow already applies the
        resulting visibility directly wherever it calls this."""
        if getattr(self, '_bottom_toggle_btn', None) is None:
            return
        self._bottom_toggle_btn.blockSignals(True)
        self._bottom_toggle_btn.setChecked(checked)
        self._bottom_toggle_btn.blockSignals(False)

    def get_current_style(self):
        return self._color, self._opacity, self._width, self._font_size

    # ── Internal ──────────────────────────────────────────────────────────────
    def _rebuild(self):
        # Single-pass teardown: drain the layout, deleting widgets only.
        # Every markup-toolbar widget below is added as a real QWidget
        # (never a bare nested QLayout) specifically so this loop's
        # `item.widget().deleteLater()` can find and clean it up on the
        # next rebuild — a raw layout item has no widget for this to
        # find, which would leak its child buttons every time.
        while self._outer.count():
            item = self._outer.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._btns.clear()
        self._tool_btns = {}

        buttons = self._buttons_for_type()
        for spec in buttons:
            if spec is None:
                sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
                sep.setStyleSheet("background:#E2E3E1;max-height:1px;border:none;")
                sep.setFixedHeight(CONFIG['H_SEP_LINE'])
                self._outer.addWidget(sep)
                self._btns.append(sep)
            elif isinstance(spec, str):
                lbl = QLabel(spec); lbl.setStyleSheet(self._GRP_SS)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._outer.addWidget(lbl)
                self._btns.append(lbl)
            else:
                emoji, tip, slot = spec
                icon_name = _EMOJI_ICON.get(emoji)
                btn = QPushButton() if icon_name else QPushButton(emoji)
                if icon_name:
                    btn.setIcon(_icon(icon_name, 18))
                    btn.setIconSize(QSize(18, 18))
                btn.setFixedSize(self._BTN_SZ, self._BTN_SZ)
                btn.setToolTip(tip)
                btn.setStyleSheet(self._BTN_SS)
                btn.clicked.connect(lambda _, s=slot, b=btn: s(b))
                self._outer.addWidget(btn)
                self._btns.append(btn)

        if self._type == NODE_T:
            self._build_markup_toggle()
            if self._markup_active:
                self._build_markup_tools()

        self._outer.addStretch()

    def _build_markup_toggle(self):
        """✏️ checkable toggle — replaces NodeMarkupPanel's old one-shot
        "✕ Avsluta" button. See markup_mode_toggled's own docstring for
        why this needs to be a toggle rather than a close button."""
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background:#E2E3E1;max-height:1px;border:none;")
        sep.setFixedHeight(CONFIG['H_SEP_LINE'])
        self._outer.addWidget(sep)
        self._btns.append(sep)

        emoji = '✏️'
        icon_name = _EMOJI_ICON.get(emoji)
        btn = QPushButton() if icon_name else QPushButton(emoji)
        if icon_name:
            btn.setIcon(_icon(icon_name, 18))
            btn.setIconSize(QSize(18, 18))
        btn.setFixedSize(self._BTN_SZ, self._BTN_SZ)
        btn.setCheckable(True)
        btn.setChecked(self._markup_active)
        btn.setToolTip(
            "Sluta redigera markup (P&ID-objekt klickbara igen)"
            if self._markup_active else "Redigera markup på P&ID")
        btn.setStyleSheet(self._TOOL_BTN_SS)
        btn.toggled.connect(self._on_markup_toggle_clicked)
        self._outer.addWidget(btn)
        self._btns.append(btn)
        self._markup_toggle_btn = btn

    def _on_markup_toggle_clicked(self, checked):
        self.markup_mode_toggled.emit(checked)

    def _build_markup_tools(self):
        """P&ID node-markup toolbar — merged in from the old, separate
        NodeMarkupPanel widget (2026-08-19, see NOTES.md "Slå ihop
        nodmarkup i nodinställningar"). Only built while
        self._markup_active (the toggle button above is checked); torn
        down and rebuilt fresh by _rebuild()'s own teardown loop like
        every other button here."""
        ISZ = 28

        # ── Navigation row — a single container QWidget (not a bare
        # QHBoxLayout added straight to self._outer), so _rebuild()'s
        # teardown loop has a widget to find and delete on the next pass.
        nav_widget = QWidget()
        nav_lay = QHBoxLayout(nav_widget)
        nav_lay.setContentsMargins(0, 0, 0, 0)
        nav_lay.setSpacing(2)
        self._prev_btn = QPushButton()
        self._prev_btn.setFixedSize(24, self._BTN_SZ)
        self._prev_btn.setToolTip("Föregående nod (⬆)")
        self._prev_btn.setIcon(_mk_icon('arrow_up', 16))
        self._prev_btn.setIconSize(QSize(16, 16))
        self._prev_btn.setStyleSheet(self._TOOL_BTN_SS)
        self._prev_btn.clicked.connect(self._navigate_prev)
        nav_lay.addWidget(self._prev_btn)

        self._next_btn = QPushButton()
        self._next_btn.setFixedSize(24, self._BTN_SZ)
        self._next_btn.setToolTip("Nästa nod (⬇)")
        self._next_btn.setIcon(_mk_icon('arrow_down', 16))
        self._next_btn.setIconSize(QSize(16, 16))
        self._next_btn.setStyleSheet(self._TOOL_BTN_SS)
        self._next_btn.clicked.connect(self._navigate_next)
        nav_lay.addWidget(self._next_btn)
        self._outer.addWidget(nav_widget)
        self._btns.append(nav_widget)

        sep0 = QFrame(); sep0.setFrameShape(QFrame.Shape.HLine)
        sep0.setStyleSheet("background:#E2E3E1;max-height:1px;border:none;")
        self._outer.addWidget(sep0)
        self._btns.append(sep0)

        # ── Tool buttons — each click selects tool AND opens per-tool popup ──
        self._tool_btns = {}
        for tool, icon_name, tip in self._MARKUP_TOOLS:
            btn = QPushButton()
            btn.setFixedSize(self._BTN_SZ, self._BTN_SZ)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setIcon(_mk_icon(icon_name, ISZ))
            btn.setIconSize(QSize(ISZ, ISZ))
            btn.setStyleSheet(self._TOOL_BTN_SS)
            btn.clicked.connect(lambda _, t=tool, b=btn: self._on_tool(t, b))
            self._outer.addWidget(btn)
            self._btns.append(btn)
            self._tool_btns[tool] = btn

        # ── Lägg ut P&ID-symbol (moved in from Red markup, 2026-08-17) ────
        symbol_pm = QPixmap(ISZ, ISZ)
        symbol_pm.fill(Qt.GlobalColor.transparent)
        _p = QPainter(symbol_pm)
        _p.setRenderHint(QPainter.RenderHint.Antialiasing)
        _p.setPen(QPen(QColor("#CC0000"), 3))
        _p.drawText(QRect(0, 0, ISZ, ISZ), Qt.AlignmentFlag.AlignCenter, "⚙")
        _p.end()
        symbol_icon = QIcon()
        symbol_icon.addPixmap(symbol_pm, QIcon.Mode.Normal)
        self._place_symbol_btn = QPushButton()
        self._place_symbol_btn.setFixedSize(self._BTN_SZ, self._BTN_SZ)
        self._place_symbol_btn.setToolTip("Lägg ut P&ID-symbol")
        self._place_symbol_btn.setIcon(symbol_icon)
        self._place_symbol_btn.setIconSize(QSize(ISZ, ISZ))
        self._place_symbol_btn.setStyleSheet(self._TOOL_BTN_SS)
        self._place_symbol_btn.clicked.connect(self.place_symbol_requested.emit)
        self._outer.addWidget(self._place_symbol_btn)
        self._btns.append(self._place_symbol_btn)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("background:#E2E3E1;max-height:1px;border:none;")
        self._outer.addWidget(sep2)
        self._btns.append(sep2)

        # ── Color strip ────────────────────────────────────────────────
        self._color_strip = QLabel()
        self._color_strip.setFixedHeight(CONFIG['H_COLOR_STRIP'])
        self._color_strip.setStyleSheet(
            f"background:{self._color};border-radius:3px;border:none;")
        self._outer.addWidget(self._color_strip)
        self._btns.append(self._color_strip)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setStyleSheet("background:#E2E3E1;max-height:1px;border:none;")
        self._outer.addWidget(sep3)
        self._btns.append(sep3)

        # ── Visibility toggle ──────────────────────────────────────────
        self._all_vis_btn = QPushButton()
        self._all_vis_btn.setFixedSize(self._BTN_SZ, self._BTN_SZ)
        self._all_vis_btn.setCheckable(True)
        self._all_vis_btn.setChecked(True)
        self._all_vis_btn.setToolTip("Dölj/visa alla markeringar")
        eye_icon = QIcon()
        eye_icon.addPixmap(_mk_pm('eye', ISZ, QColor("#ffffff")),
                           QIcon.Mode.Normal, QIcon.State.Off)
        eye_icon.addPixmap(_mk_pm('eye', ISZ, QColor("#ffffff")),
                           QIcon.Mode.Normal, QIcon.State.On)
        self._all_vis_btn.setIcon(eye_icon)
        self._all_vis_btn.setIconSize(QSize(ISZ, ISZ))
        self._all_vis_btn.setStyleSheet(
            "QPushButton{border:none;border-radius:5px;padding:0px;"
            "background:#27AE60;}"
            "QPushButton:!checked{background:#E74C3C;}")
        self._all_vis_btn.clicked.connect(self._on_all_vis)
        self._outer.addWidget(self._all_vis_btn)
        self._btns.append(self._all_vis_btn)

        sep4 = QFrame(); sep4.setFrameShape(QFrame.Shape.HLine)
        sep4.setStyleSheet("background:#E2E3E1;max-height:1px;border:none;")
        self._outer.addWidget(sep4)
        self._btns.append(sep4)

        # ── Bottom-panel switch: Nodmarkeringar <-> HAZOP scenario ──────
        self._bottom_toggle_btn = QPushButton("⇄")
        self._bottom_toggle_btn.setFixedSize(self._BTN_SZ, self._BTN_SZ)
        self._bottom_toggle_btn.setCheckable(True)
        self._bottom_toggle_btn.setToolTip(
            "Växla nedre fältet: Nodmarkeringar / HAZOP scenario")
        self._bottom_toggle_btn.setStyleSheet(self._TOOL_BTN_SS)
        self._bottom_toggle_btn.toggled.connect(self.bottom_panel_toggled.emit)
        self._outer.addWidget(self._bottom_toggle_btn)
        self._btns.append(self._bottom_toggle_btn)

    def _on_tool(self, tool, btn=None):
        self._current_tool = tool
        for t, b in self._tool_btns.items():
            b.setChecked(t == tool)
        self.tool_changed.emit(tool)
        # Open per-tool popup for all drawing tools
        if tool != 'select' and btn is not None:
            self._show_tool_popup(tool, btn)

    def _show_tool_popup(self, tool, btn):
        if self._style_popup is None:
            self._style_popup = _StylePopup(self)
        self._style_popup.show_for(tool, btn)

    def _on_all_vis(self, checked):
        if self._type != NODE_T or self._id is None:
            return
        self.db.set_all_node_markups_visible(self._id, checked)
        self.all_vis_toggled.emit(checked)

    def _apply_color(self, hex_c):
        self._color = hex_c
        if getattr(self, '_color_strip', None) is not None:
            self._color_strip.setStyleSheet(f"background:{hex_c};border-radius:3px;")
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_opacity(self, val):
        self._opacity = val / 100.0
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_width(self, val):
        self._width = val
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_font(self, val):
        self._font_size = val

    def _apply_snap(self, enabled):
        self._snap = enabled
        self.snap_changed.emit(enabled)

    def _navigate_prev(self):
        if self._type != NODE_T or self._id is None:
            return
        all_nodes = [r[0] for r in self.db.nodes()]
        try:
            current_idx = all_nodes.index(self._id)
            if current_idx > 0:
                self.navigate_node_requested.emit(all_nodes[current_idx - 1])
        except (ValueError, IndexError):
            pass

    def _navigate_next(self):
        if self._type != NODE_T or self._id is None:
            return
        all_nodes = [r[0] for r in self.db.nodes()]
        try:
            current_idx = all_nodes.index(self._id)
            if current_idx < len(all_nodes) - 1:
                self.navigate_node_requested.emit(all_nodes[current_idx + 1])
        except (ValueError, IndexError):
            pass

    def _buttons_for_type(self) -> list:
        # Returns bound-method references (self.method) so the lambda in
        # _rebuild fires correctly.  A class-level dict with cls.method
        # (unbound) was tried but broke: s(btn) passed btn as self.
        T = self._type
        if T == 1:   # NODE_T
            return [
                "NOD",
                ("🏷", "Redigera namn och P&ID-referens",    self._edit_node_name),
                ("📄", "Redigera beskrivning",                self._edit_node_desc),
                ("⚗", "Redigera processparametrar\n(media, tryck, temperatur)",
                                                              self._edit_node_params),
                None,
                ("✅", "Sätt status / godkänn nod",          self._edit_node_status),
                ("📍", "Visa nod på P&ID",                   self._zoom_to_node),
            ]
        if T == 5:   # DEV_T
            return [
                "AVVIK.",
                ("📝", "Redigera avvikelsebeskrivning",       self._edit_dev_desc),
            ]
        if T == 2:   # CAUSE_T
            return [
                "ORSAK",
                ("📝", "Redigera orsak (beskrivning, objekt, tag)", self._edit_cause_obj),
                ("📊", "Ange frekvens / F-nivå",              self._edit_cause_freq),
                ("💬", "Redigera kommentar",                  self._edit_cause_comment),
                None,
                ("📍", "Visa orsak på P&ID",                 self._zoom_to_cause),
            ]
        if T == 3:   # CONS_T
            return [
                "KONS.",
                ("📋", "Redigera konsekvenskedja (Del1–Del5)", self._edit_cons_chain),
                ("📊", "Sätt allvarlighet per kategori",      self._edit_cons_sev),
                None,
                ("📍", "Visa konsekvens på P&ID",            self._zoom_to_cons),
            ]
        if T == 4:   # SG_T
            return [
                "BARRIÄR",
                ("📝", "Redigera barriärsbeskrivning",        self._edit_sg_desc),
                ("⚡", "Ange RRF och typ",                    self._edit_sg_rrf),
                None,
                ("📍", "Visa barriär på P&ID",               self._zoom_to_sg),
            ]
        return []

    # ── Popup helper ──────────────────────────────────────────────────────────
    def _popup_near(self, btn):
        """Return global position to anchor a popup to the left of the ribbon."""
        gp = btn.mapToGlobal(btn.rect().topLeft())
        scr = (QApplication.screenAt(gp) or QApplication.primaryScreen()).availableGeometry()
        return gp, scr

    def _show_popup(self, btn, popup):
        popup.adjustSize()
        gp, scr = self._popup_near(btn)
        pw, ph  = popup.sizeHint().width(), popup.sizeHint().height()
        x = gp.x() - pw - 6
        y = gp.y()
        if x < scr.left(): x = gp.x() + self._WIDTH + 6
        if y + ph > scr.bottom(): y = scr.bottom() - ph
        popup.move(max(scr.left(), x), max(scr.top(), y))
        return popup.exec()

    def _text_popup(self, btn, title: str, current: str,
                    multiline: bool = False, placeholder: str = ''):
        """Generic text-editing popup. Returns new text or None on cancel."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dlg.setMinimumWidth(CONFIG['W_DIALOG_LG'])
        lay = QVBoxLayout(dlg)
        lay.setSpacing(6); lay.setContentsMargins(10, 10, 10, 10)
        hdr = QLabel(f"<b>{title}</b>")
        hdr.setStyleSheet("color:#8D9299;")
        lay.addWidget(hdr)
        if multiline:
            ed = QTextEdit(); ed.setPlainText(current)
            ed.setPlaceholderText(placeholder)
            ed.setFixedHeight(CONFIG['H_EDIT_LG'])
        else:
            ed = QLineEdit(current)
            ed.setPlaceholderText(placeholder)
        lay.addWidget(ed)
        row = QHBoxLayout()
        ok = QPushButton("OK"); ok.setDefault(True)
        ok.setStyleSheet(self._OK_BTN_SS)
        ok.clicked.connect(dlg.accept)
        cancel = QPushButton("Avbryt"); cancel.clicked.connect(dlg.reject)
        row.addStretch(); row.addWidget(cancel); row.addWidget(ok)
        lay.addLayout(row)
        if isinstance(ed, QLineEdit):
            ed.returnPressed.connect(dlg.accept)
        if self._show_popup(btn, dlg) == QDialog.DialogCode.Accepted:
            return (ed.toPlainText() if multiline else ed.text()).strip()
        return None

    # ── NODE actions ──────────────────────────────────────────────────────────
    def _edit_node_name(self, btn):
        if not self._id: return
        n = self.db.get_node(self._id)
        if not n: return
        n = dict(n)
        dlg = QDialog(self)
        dlg.setWindowTitle("Nod")
        dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dlg.setMinimumWidth(CONFIG['W_DIALOG_MD'])
        lay = QFormLayout(dlg); lay.setContentsMargins(10,10,10,10)
        name_e = QLineEdit(n['name'] or '')
        pid_e  = QLineEdit(n.get('pid_ref') or '')
        lay.addRow("<b>Namn:</b>", name_e)
        lay.addRow("P&ID-ref:", pid_e)
        row = QHBoxLayout()
        ok = QPushButton("OK"); ok.setDefault(True)
        ok.setStyleSheet(self._OK_BTN_SS)
        ok.clicked.connect(dlg.accept)
        cancel = QPushButton("Avbryt"); cancel.clicked.connect(dlg.reject)
        row.addStretch(); row.addWidget(cancel); row.addWidget(ok)
        lay.addRow(row)
        name_e.returnPressed.connect(dlg.accept)
        if self._show_popup(btn, dlg) == QDialog.DialogCode.Accepted:
            name = name_e.text().strip() or 'Ny nod'
            self.db.update_node(self._id, name, n.get('description',''),
                                pid_e.text().strip(),
                                n.get('media',''), n.get('pressure',''),
                                n.get('temperature',''))
            self.item_changed.emit()

    def _edit_node_desc(self, btn):
        if not self._id: return
        n = self.db.get_node(self._id)
        if not n: return
        n = dict(n)
        val = self._text_popup(btn, "Beskrivning", n.get('description','') or '',
                               multiline=True, placeholder="Beskriv noden...")
        if val is not None:
            self.db.update_node(self._id, n['name'], val,
                                n.get('pid_ref',''), n.get('media',''),
                                n.get('pressure',''), n.get('temperature',''))
            self.item_changed.emit()

    def _edit_node_params(self, btn):
        if not self._id: return
        n = self.db.get_node(self._id)
        if not n: return
        n = dict(n)
        dlg = QDialog(self)
        dlg.setWindowTitle("Processparametrar")
        dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dlg.setMinimumWidth(CONFIG['W_DIALOG_MD'])
        lay = QFormLayout(dlg); lay.setContentsMargins(10,10,10,10)
        me = QLineEdit(n.get('media','') or '')
        pe = QLineEdit(n.get('pressure','') or '')
        te = QLineEdit(n.get('temperature','') or '')
        me.setPlaceholderText("t.ex. Vätgas, Vatten")
        pe.setPlaceholderText("t.ex. 10 bar g")
        te.setPlaceholderText("t.ex. 150 °C")
        lay.addRow("Media:", me)
        lay.addRow("Tryck:", pe)
        lay.addRow("Temperatur:", te)
        row = QHBoxLayout()
        ok = QPushButton("OK"); ok.setDefault(True)
        ok.setStyleSheet(self._OK_BTN_SS)
        ok.clicked.connect(dlg.accept); cancel = QPushButton("Avbryt")
        cancel.clicked.connect(dlg.reject)
        row.addStretch(); row.addWidget(cancel); row.addWidget(ok)
        lay.addRow(row)
        if self._show_popup(btn, dlg) == QDialog.DialogCode.Accepted:
            self.db.update_node(self._id, n['name'], n.get('description',''),
                                n.get('pid_ref',''),
                                me.text().strip(), pe.text().strip(), te.text().strip())
            self.item_changed.emit()

    def _edit_node_status(self, btn):
        if not self._mw or not self._id: return
        self._mw._approve_node(node_id=self._id)

    def _zoom_to_node(self, btn):
        if not self._mw or not self._id: return
        self._mw._switch_view(1)
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(CONFIG['TIMER_NAV_QUICK_MS'],
                         partial(self._mw.zoom_to_node, self._id))

    # ── DEVIATION actions ─────────────────────────────────────────────────────
    def _edit_dev_desc(self, btn):
        if not self._id: return
        d = self.db.get_deviation(self._id)
        if not d: return
        d = dict(d)
        val = self._text_popup(btn, "Avvikelse", d['description'] or '')
        if val is not None:
            self.db.conn.execute(
                "UPDATE deviations SET description=? WHERE id=?", (val, self._id))
            self.db.commit()
            self.item_changed.emit()

    # ── CAUSE actions ─────────────────────────────────────────────────────────
    def _edit_cause_obj(self, btn):
        """Open the combined CauseObjectPopup for editing description,
        comp_type and comp_tag together — replaces the old split of a
        separate free-text description popup and a separate object/tag
        popup, so cause editing is consistent with every other entry point.
        """
        if not self._id or not self._mw: return
        c = dict(self.db.get_cause(self._id) or {})
        dev = self.db.get_deviation(c.get('deviation_id')) if c.get('deviation_id') else None
        popup = CauseObjectPopup(
            c.get('comp_type',''), c.get('comp_tag',''),
            self.db, dev_description=dev['description'] if dev else None,
            current_description=c.get('description',''), parent=self)
        popup.setWindowFlags(popup.windowFlags() | Qt.WindowType.FramelessWindowHint)
        gp, scr = self._popup_near(btn)
        popup.adjustSize()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = gp.x() - pw - 6
        y = gp.y()
        if x < scr.left(): x = gp.x() + self._WIDTH + 6
        if y + ph > scr.bottom(): y = scr.bottom() - ph
        popup.move(max(scr.left(), x), max(scr.top(), y))
        def _on_committed(ct, tag, desc, freq):
            self.db.update_cause(self._id, comp_type=ct, comp_tag=tag)
            if desc is not None: self.db.update_cause(self._id, description=desc)
            if freq is not None: self.db.update_cause(self._id, base_frequency=freq)
            self.item_changed.emit()
        popup.committed.connect(_on_committed)
        popup.exec()

    def _edit_cause_freq(self, btn):
        if not self._id: return
        c = dict(self.db.get_cause(self._id) or {})
        current_freq = c.get('base_frequency')
        dlg = QDialog(self)
        dlg.setWindowTitle("Frekvens")
        dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dlg.setMinimumWidth(CONFIG['W_DIALOG_MIN'])
        lay = QFormLayout(dlg); lay.setContentsMargins(10,10,10,10)
        freq_e = QLineEdit(f"{current_freq:g}" if current_freq else '')
        freq_e.setPlaceholderText("t.ex. 0.01")
        level_lbl = QLabel('')
        level_lbl.setStyleSheet("color:#8D9299;font-size:10px;")
        def _upd(txt):
            try: level_lbl.setText(freq_axis_label(freq_to_f_level(float(txt))))
            except: level_lbl.setText('')
        freq_e.textChanged.connect(_upd)
        _upd(freq_e.text())
        lay.addRow("Frekvens (/år):", freq_e)
        lay.addRow("F-nivå:", level_lbl)
        row = QHBoxLayout()
        ok = QPushButton("OK"); ok.setDefault(True)
        ok.setStyleSheet(self._OK_BTN_SS)
        ok.clicked.connect(dlg.accept)
        cancel = QPushButton("Avbryt"); cancel.clicked.connect(dlg.reject)
        row.addStretch(); row.addWidget(cancel); row.addWidget(ok)
        lay.addRow(row)
        freq_e.returnPressed.connect(dlg.accept)
        if self._show_popup(btn, dlg) == QDialog.DialogCode.Accepted:
            try:
                freq = float(freq_e.text().strip()) if freq_e.text().strip() else None
            except ValueError:
                freq = None
            self.db.update_cause(self._id, base_frequency=freq)
            self.item_changed.emit()

    def _edit_cause_comment(self, btn):
        if not self._id: return
        current = self.db.get_cause_comment(self._id) or ''
        val = self._text_popup(btn, "Kommentar", current,
                               multiline=True, placeholder="Notering, beslut, referens...")
        if val is not None:
            self.db.set_cause_comment(self._id, val)
            self.item_changed.emit()

    def _zoom_to_cause(self, btn):
        if not self._id or not self._mw: return
        self._mw._switch_view(1)
        markers = self.db.cause_markers_for_cause(self._id)
        if markers:
            m = markers[0]
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(CONFIG['TIMER_NAV_QUICK_MS'],
                             partial(self._mw.pid_panel.navigate_to_marker,
                                    m['pid_page'], m['x'], m['y']))

    # ── CONSEQUENCE actions ───────────────────────────────────────────────────
    def _edit_cons_chain(self, btn):
        if not self._id or not self._mw: return
        self._mw._open_consequence_step_picker(self._id)

    def _edit_cons_sev(self, btn):
        if not self._id or not self._mw: return
        popup = ConsCategoryMatrixPopup(self.db, self._id, self)
        if self._show_popup(btn, popup) == QDialog.DialogCode.Accepted:
            self.item_changed.emit()

    def _zoom_to_cons(self, btn):
        if not self._id or not self._mw: return
        self._mw._switch_view(1)
        rows = self.db.conn.execute(
            "SELECT pid_page,x,y FROM consequence_markers WHERE consequence_id=? LIMIT 1",
            (self._id,)).fetchone()
        if rows:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(CONFIG['TIMER_NAV_QUICK_MS'],
                             partial(self._mw.pid_panel.navigate_to_marker,
                                    rows[0], rows[1], rows[2]))

    # ── SAFEGUARD actions ─────────────────────────────────────────────────────
    def _edit_sg_desc(self, btn):
        if not self._id: return
        sg = self.db.get_safeguard(self._id)
        if not sg: return
        sg = dict(sg)
        val = self._text_popup(btn, "Barriär", dict(sg).get('description','') or '',
                               multiline=True, placeholder="Beskriv barriären...")
        if val is not None:
            self.db.update_safeguard(self._id, description=val)
            self.item_changed.emit()

    def _edit_sg_rrf(self, btn):
        if not self._id or not self._mw: return
        sg = self.db.get_safeguard(self._id)
        if not sg: return
        sgd = dict(sg)
        sg_id = self._id   # capture by value for the signal lambda
        popup = RRFPopup(int(sgd.get('rrf', 1)), sgd.get('sg_type', 'Övrigt'), self)
        popup.rrf_selected.connect(
            lambda v, t, sid=sg_id: (self.db.update_safeguard(sid, rrf=v, sg_type=t),
                                     self.item_changed.emit()))
        self._show_popup(btn, popup)

    def _zoom_to_sg(self, btn):
        if not self._id or not self._mw: return
        self._mw._switch_view(1)
        rows = self.db.conn.execute(
            "SELECT pid_page,x,y FROM safeguard_markers WHERE safeguard_id=? LIMIT 1",
            (self._id,)).fetchone()
        if rows:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(CONFIG['TIMER_NAV_QUICK_MS'],
                             partial(self._mw.pid_panel.navigate_to_marker,
                                    rows[0], rows[1], rows[2]))


class _MarkupStyleDialog(QDialog):
    def __init__(self, mu_type, color, opacity, line_width, font_size, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ändra stil")
        self.setFixedWidth(CONFIG['W_DIALOG_LG'])
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        # Color row
        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("Färg:"))
        self._color = color
        self._color_btns = []
        for hc in MARKUP_COLORS:
            btn = QPushButton()
            btn.setFixedSize(22, 22)
            sel = hc.lower() == color.lower()
            btn.setStyleSheet(
                f"background:{hc};border:2px solid {'#222' if sel else 'transparent'};"
                f"border-radius:3px;")
            btn.clicked.connect(partial(self._pick, hc))
            color_row.addWidget(btn)
            self._color_btns.append((hc, btn))
        color_row.addStretch()
        outer.addLayout(color_row)

        # Opacity
        self._opacity_row = QWidget()
        op_lay = QHBoxLayout(self._opacity_row)
        op_lay.setContentsMargins(0, 0, 0, 0)
        op_lay.addWidget(QLabel("Opacitet:"))
        self._opacity_sl = QSlider(Qt.Orientation.Horizontal)
        self._opacity_sl.setRange(10, 100)
        self._opacity_sl.setValue(int(opacity * 100))
        op_lay.addWidget(self._opacity_sl)
        self._opacity_row.setVisible(mu_type in ('polygon', 'polyline', 'comment'))
        outer.addWidget(self._opacity_row)

        # Line width
        self._width_row = QWidget()
        w_lay = QHBoxLayout(self._width_row)
        w_lay.setContentsMargins(0, 0, 0, 0)
        w_lay.addWidget(QLabel("Tjocklek:"))
        self._width_sp = QSpinBox()
        self._width_sp.setRange(1, 20)
        self._width_sp.setValue(int(line_width))
        w_lay.addWidget(self._width_sp)
        w_lay.addStretch()
        self._width_row.setVisible(mu_type in ('polygon', 'polyline'))
        outer.addWidget(self._width_row)

        # Font size
        self._font_row = QWidget()
        f_lay = QHBoxLayout(self._font_row)
        f_lay.setContentsMargins(0, 0, 0, 0)
        f_lay.addWidget(QLabel("Teckenstorlek:"))
        self._font_sp = QSpinBox()
        self._font_sp.setRange(6, 72)
        self._font_sp.setValue(int(font_size))
        f_lay.addWidget(self._font_sp)
        f_lay.addStretch()
        self._font_row.setVisible(mu_type in ('text', 'comment'))
        outer.addWidget(self._font_row)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _pick(self, hc):
        self._color = hc
        for c, btn in self._color_btns:
            btn.setStyleSheet(
                f"background:{c};border:2px solid {'#222' if c.lower()==hc.lower() else 'transparent'};"
                f"border-radius:3px;")

    def get_style(self):
        return (self._color,
                self._opacity_sl.value() / 100.0,
                self._width_sp.value(),
                self._font_sp.value())


# ══════════════════════════════════════════════════════════════════════════════
# MARKUP TABLE PANEL  (bottom panel, shown during markup edit mode)
# ══════════════════════════════════════════════════════════════════════════════

class MarkupTablePanel(QWidget):
    """Table of markups for the active node — lives in bottom splitter alongside scenario panel."""
    item_deleted     = pyqtSignal(int)        # mu_id
    item_vis_toggled = pyqtSignal(int, bool)  # mu_id, visible
    item_selected    = pyqtSignal(int)        # mu_id
    item_style_changed = pyqtSignal(int)      # mu_id
    item_duplicated  = pyqtSignal(int)        # mu_id

    _TYPE_ICON = {'polygon': '◻', 'polyline': '〰', 'text': '𝐀', 'comment': '💬'}
    _COLS      = ['Typ', 'Etikett', 'Färg', 'Opacitet', 'Tjocklek', 'Font', '👁']

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db      = db
        self.node_id = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)

        hdr = QHBoxLayout()
        title = QLabel("Nodmarkeringar")
        f = QFont(); f.setBold(True); f.setPointSize(9)
        title.setFont(f)
        hdr.addWidget(title)
        hdr.addStretch()
        lay.addLayout(hdr)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_ctx_menu)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.setStyleSheet(
            "QTableWidget{border:1px solid #E2E3E1;font-size:10px;}"
            "QTableWidget::item:selected{background:#E6ECFA;color:#17191C;}")

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self._table)

    def load(self, node_id):
        self.node_id = node_id
        self.refresh()

    def refresh(self):
        self._table.setRowCount(0)
        if self.node_id is None:
            return
        for mu in self.db.node_markups_for_node(self.node_id):
            m = dict(mu)
            row = self._table.rowCount()
            self._table.insertRow(row)
            mu_id   = m['id']
            typ     = m.get('type', 'polygon')
            label   = m.get('label', '') or ''
            color   = m.get('color', '#1565C0')
            opacity = m.get('opacity', 0.45)
            width   = m.get('line_width', 12)
            font_sz = m.get('font_size', 12)
            visible = bool(m.get('visible', 1))

            icon_item = QTableWidgetItem(self._TYPE_ICON.get(typ, '◻'))
            icon_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            icon_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 0, icon_item)

            lbl_item = QTableWidgetItem(label)
            lbl_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            self._table.setItem(row, 1, lbl_item)

            color_item = QTableWidgetItem(color)
            color_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            color_item.setBackground(QColor(color))
            color_item.setForeground(QColor(color))
            color_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 2, color_item)

            op_item = QTableWidgetItem(f"{int(opacity * 100)}%")
            op_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            op_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 3, op_item)

            w_item = QTableWidgetItem(str(width))
            w_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            w_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 4, w_item)

            f_item = QTableWidgetItem(str(font_sz))
            f_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            f_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 5, f_item)

            vis_item = QTableWidgetItem('👁' if visible else '○')
            vis_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            vis_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 6, vis_item)

    def select_markup(self, mu_id):
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == mu_id:
                self._table.selectRow(row)
                break

    def clear(self):
        self.node_id = None
        self._table.setRowCount(0)

    def _on_cell_clicked(self, row, col):
        item = self._table.item(row, 0)
        if item is None:
            return
        mu_id = item.data(Qt.ItemDataRole.UserRole)
        if col == 6:
            self._toggle_visibility(row, mu_id)
        else:
            self.item_selected.emit(mu_id)

    def _toggle_visibility(self, row, mu_id):
        mu = self.db.get_node_markup(mu_id)
        if not mu:
            return
        new_vis = not bool(dict(mu).get('visible', 1))
        self.db.update_node_markup(mu_id, visible=new_vis)
        vis_item = self._table.item(row, 6)
        if vis_item:
            vis_item.setText('👁' if new_vis else '○')
        self.item_vis_toggled.emit(mu_id, new_vis)

    def _on_ctx_menu(self, pos):
        seen, rows = set(), []
        for idx in self._table.selectedIndexes():
            r = idx.row()
            if r not in seen:
                seen.add(r)
                item = self._table.item(r, 0)
                if item:
                    rows.append(item)
        if not rows:
            return
        menu = QMenu(self)
        n = len(rows)
        lbl = f"Ta bort ({n} valda)" if n > 1 else "Ta bort"
        act_del = menu.addAction(_icon('delete'), lbl)
        act_style = None
        act_dup   = None
        if n == 1:
            act_style = menu.addAction(_icon('edit'), "Ändra stil...")
            act_dup   = menu.addAction(_icon('clipboard'), "Duplicera")
        result = menu.exec(self._table.viewport().mapToGlobal(pos))
        if result == act_del:
            for item in rows:
                mu_id = item.data(Qt.ItemDataRole.UserRole)
                self.db.delete_node_markup(mu_id)
                self.item_deleted.emit(mu_id)
            self.refresh()
        elif act_style is not None and result == act_style:
            mu_id = rows[0].data(Qt.ItemDataRole.UserRole)
            mu = self.db.get_node_markup(mu_id)
            if mu:
                mu = dict(mu)
                dlg = _MarkupStyleDialog(
                    mu.get('type', 'polygon'),
                    mu.get('color', '#E53935'),
                    float(mu.get('opacity', 0.7)),
                    int(mu.get('line_width', 2)),
                    int(mu.get('font_size', 12)),
                    self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    c, op, lw, fs = dlg.get_style()
                    self.db.update_node_markup(mu_id, color=c, opacity=op,
                                               line_width=lw, font_size=fs)
                    self.item_style_changed.emit(mu_id)
                    self.refresh()
        elif act_dup is not None and result == act_dup:
            mu_id = rows[0].data(Qt.ItemDataRole.UserRole)
            self.item_duplicated.emit(mu_id)


# ══════════════════════════════════════════════════════════════════════════════
# RED MARKUP PANELS
# ══════════════════════════════════════════════════════════════════════════════

def _mk_symbol_icon(svg_str: str, sz: int = 32) -> QIcon:
    """Render an SVG string to a QIcon for the symbol selector buttons."""
    from PyQt6.QtSvg import QSvgRenderer
    from PyQt6.QtCore import QByteArray
    renderer = QSvgRenderer(QByteArray(svg_str.encode('utf-8')))
    pm = QPixmap(sz, sz)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if renderer.isValid():
        renderer.render(p)
    p.end()
    return QIcon(pm)


class _SymbolSelectorPopup(QFrame):
    """Floating popup with P&ID symbol buttons grouped by category."""
    symbol_selected = pyqtSignal(str)  # symbol_id

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet(
            "QFrame{background:#FFFFFF;border:1px solid #CFD1CE;border-radius:6px;}"
            "QPushButton{border:1px solid #E2E3E1;border-radius:4px;background:#FAFAFA;"
            "padding:2px;}"
            "QPushButton:hover{background:#F5F5F3;border-color:#CFD1CE;}"
            "QPushButton:checked{background:#2F5FD0;border-color:#2F5FD0;}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        lbl = QLabel("Välj P&ID-symbol")
        f = QFont(); f.setBold(True); f.setPointSize(9)
        lbl.setFont(f)
        outer.addWidget(lbl)

        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabBar::tab{padding:4px 10px;font-size:9px;}"
            "QTabBar::tab:selected{background:#E6ECFA;}")
        outer.addWidget(tabs)

        for cat, syms in _RED_MARKUP_SYMBOLS.items():
            tab = QWidget()
            grid = QGridLayout(tab)
            grid.setContentsMargins(4, 4, 4, 4)
            grid.setSpacing(4)
            for i, (sid, sname, svg) in enumerate(syms):
                btn = QPushButton()
                btn.setFixedSize(40, 40)
                btn.setIcon(_mk_symbol_icon(svg, 28))
                btn.setIconSize(QSize(28, 28))
                btn.setToolTip(sname)
                btn.clicked.connect(lambda _, s=sid: (self.symbol_selected.emit(s), self.hide()))
                row, col = divmod(i, 4)
                grid.addWidget(btn, row, col)
            tabs.addTab(tab, cat)

        self.setFixedSize(220, 280)

    def show_near(self, btn):
        gp = btn.mapToGlobal(btn.rect().bottomLeft())
        self.move(gp.x() - self.width() - 4, gp.y())
        self.show()


class RedMarkupPanel(QWidget):
    """Narrow vertical ribbon for red markup — trimmed 2026-08-17 (see
    NOTES.md "Red markup konsolideras") to just Välj/flytta (needed to
    select an already-placed symbol for size/rotation editing) and Lägg
    ut P&ID-symbol; every other drawing tool (polygon/polyline/smart/
    comment), the color/opacity/width popup, and the show/hide-all toggle
    are gone per explicit request ("skrota allt utom 'Välj P&ID-symbol'").
    This panel is no longer reachable from the tree's own context menu —
    NodeMarkupPanel's own "Lägg ut P&ID-symbol" button is the sole entry
    point now (see MainWindow._on_place_symbol_requested), which is why
    the two edit-mode state machines stayed technically separate rather
    than being merged: lower regression risk for the heavily-tested P&ID
    drawing code, at the cost of a brief "you're now in a different mode"
    transition under the hood that the user never has to think about."""
    closed          = pyqtSignal()
    tool_changed    = pyqtSignal(str)
    symbol_selected = pyqtSignal(str)   # symbol_id
    symbol_dims_changed = pyqtSignal(float, float, float)  # w, h, rot

    _TOOLS = [
        ('select', 'select', 'Välj/flytta'),
        ('symbol', 'symbol', 'Lägg ut P&ID-symbol'),
    ]

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db            = db
        self.node_id       = None
        self._current_tool = 'select'
        self._selected_symbol_id = None
        self._sym_popup    = None

        SZ = 48
        ISZ = 28
        self.setFixedWidth(CONFIG['W_SPINNER'])
        self.setStyleSheet("background:#FFFFFF; border-right: 1px solid #E2E3E1;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 6, 5, 6)
        outer.setSpacing(3)

        _btn_ss = (
            "QPushButton{border:1px solid #D0D4DA;border-radius:5px;"
            "background:#FFFFFF;padding:0px;}"
            "QPushButton:checked{background:#C62828;border-color:#C62828;}"
            "QPushButton:hover:!checked{background:#FFEBEE;border-color:#EF9A9A;}")

        # Close button
        close_btn = QPushButton()
        close_btn.setFixedSize(SZ, SZ)
        close_btn.setToolTip("Avsluta redigering")
        close_icon = QIcon()
        close_icon.addPixmap(_mk_pm('close', ISZ, QColor("#ffffff")))
        close_btn.setIcon(close_icon)
        close_btn.setIconSize(QSize(ISZ, ISZ))
        close_btn.setStyleSheet(
            "QPushButton{background:#546E7A;border:none;border-radius:5px;padding:0px;}"
            "QPushButton:hover{background:#37474F;}")
        close_btn.clicked.connect(self.closed.emit)
        outer.addWidget(close_btn)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setStyleSheet("background:#E2E3E1;max-height:1px;border:none;")
        outer.addWidget(sep1)

        self._tool_btns = {}
        for tool, icon_name, tip in self._TOOLS:
            btn = QPushButton()
            btn.setFixedSize(SZ, SZ)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            if tool == 'symbol':
                # Custom red X icon for symbol tool
                sym_pm = QPixmap(ISZ, ISZ)
                sym_pm.fill(Qt.GlobalColor.transparent)
                p = QPainter(sym_pm)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setPen(QPen(QColor("#CC0000"), 3))
                p.drawText(QRect(0, 0, ISZ, ISZ), Qt.AlignmentFlag.AlignCenter, "⚙")
                p.end()
                sym_icon = QIcon()
                sym_icon.addPixmap(sym_pm, QIcon.Mode.Normal)
                btn.setIcon(sym_icon)
            else:
                btn.setIcon(_mk_icon(icon_name, ISZ))
            btn.setIconSize(QSize(ISZ, ISZ))
            btn.setStyleSheet(_btn_ss)
            btn.clicked.connect(lambda _, t=tool, b=btn: self._on_tool(t, b))
            outer.addWidget(btn)
            self._tool_btns[tool] = btn

        outer.addStretch()
        self._on_tool('select')

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, node_id):
        self.node_id = node_id
        self._on_tool('select')

    def get_current_style(self):
        # Select/symbol don't use a drawn-shape color/opacity/width — fixed
        # defaults kept only because callers still unpack this 4-tuple.
        return '#CC0000', 1.0, 4, 16

    def get_selected_symbol(self):
        return self._selected_symbol_id

    def get_symbol_dims(self):
        """Returns (w, h, rot) for the currently selected symbol."""
        return 40.0, 40.0, 0.0

    def open_symbol_picker(self):
        """Entry point for NodeMarkupPanel's "Lägg ut P&ID-symbol" button
        (2026-08-17) — opens the same picker a click on this panel's own
        symbol button would, without requiring that extra click."""
        self._on_tool('symbol', self._tool_btns['symbol'])

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_tool(self, tool, btn=None):
        self._current_tool = tool
        for t, b in self._tool_btns.items():
            b.setChecked(t == tool)
        if tool == 'symbol' and btn is not None:
            if self._sym_popup is None:
                self._sym_popup = _SymbolSelectorPopup(self)
                self._sym_popup.symbol_selected.connect(self._on_symbol_selected)
            self._sym_popup.show_near(btn)
            return
        self.tool_changed.emit(tool)

    def _on_symbol_selected(self, symbol_id):
        self._selected_symbol_id = symbol_id
        self.symbol_selected.emit(symbol_id)
        self.tool_changed.emit('symbol')


class RedMarkupTablePanel(QWidget):
    """Table of red markups for the active node."""
    item_deleted     = pyqtSignal(int)
    item_vis_toggled = pyqtSignal(int, bool)
    item_selected    = pyqtSignal(int)
    item_style_changed = pyqtSignal(int)

    _TYPE_ICON = {'polygon': '◻', 'polyline': '〰', 'text': '𝐀',
                  'comment': '💬', 'symbol': '⚙'}
    _COLS      = ['Typ', 'Etikett', 'Färg', 'Opacitet', 'Tjocklek', '👁']

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db      = db
        self.node_id = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)

        hdr = QHBoxLayout()
        title = QLabel("🔴 Redmarkeringar")
        f = QFont(); f.setBold(True); f.setPointSize(9)
        title.setFont(f)
        hdr.addWidget(title)
        hdr.addStretch()
        lay.addLayout(hdr)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_ctx_menu)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.setStyleSheet(
            "QTableWidget{border:1px solid #FFCDD2;font-size:10px;}"
            "QTableWidget::item:selected{background:#FFEBEE;color:#C62828;}")

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self._table)

    def load(self, node_id):
        self.node_id = node_id
        self.refresh()

    def refresh(self):
        self._table.setRowCount(0)
        if self.node_id is None:
            return
        for mu in self.db.node_red_markups_for_node(self.node_id):
            m = dict(mu)
            row = self._table.rowCount()
            self._table.insertRow(row)
            mu_id   = m['id']
            typ     = m.get('type', 'polygon')
            label   = m.get('label', '') or ''
            color   = m.get('color', '#CC0000')
            opacity = m.get('opacity', 1.0)
            width   = m.get('line_width', 4)
            visible = bool(m.get('visible', 1))

            icon_item = QTableWidgetItem(self._TYPE_ICON.get(typ, '◻'))
            icon_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            icon_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 0, icon_item)

            display_label = label if typ != 'symbol' else f"⚙ {label}"
            lbl_item = QTableWidgetItem(display_label)
            lbl_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            self._table.setItem(row, 1, lbl_item)

            color_item = QTableWidgetItem(color)
            color_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            color_item.setBackground(QColor(color))
            color_item.setForeground(QColor(color))
            color_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 2, color_item)

            op_item = QTableWidgetItem(f"{int(opacity * 100)}%")
            op_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            op_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 3, op_item)

            w_item = QTableWidgetItem(str(width))
            w_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            w_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 4, w_item)

            vis_item = QTableWidgetItem('👁' if visible else '○')
            vis_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            vis_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 5, vis_item)

    def select_markup(self, mu_id):
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == mu_id:
                self._table.selectRow(row)
                break

    def clear(self):
        self.node_id = None
        self._table.setRowCount(0)

    def _on_cell_clicked(self, row, col):
        item = self._table.item(row, 0)
        if item is None:
            return
        mu_id = item.data(Qt.ItemDataRole.UserRole)
        if col == 5:
            self._toggle_visibility(row, mu_id)
        else:
            self.item_selected.emit(mu_id)

    def _toggle_visibility(self, row, mu_id):
        mu = self.db.get_node_red_markup(mu_id)
        if not mu:
            return
        new_vis = not bool(dict(mu).get('visible', 1))
        self.db.update_node_red_markup(mu_id, visible=new_vis)
        vis_item = self._table.item(row, 5)
        if vis_item:
            vis_item.setText('👁' if new_vis else '○')
        self.item_vis_toggled.emit(mu_id, new_vis)

    def _on_ctx_menu(self, pos):
        seen, rows = set(), []
        for idx in self._table.selectedIndexes():
            r = idx.row()
            if r not in seen:
                seen.add(r)
                item = self._table.item(r, 0)
                if item:
                    rows.append(item)
        if not rows:
            return
        menu = QMenu(self)
        n = len(rows)
        lbl = f"Ta bort ({n} valda)" if n > 1 else "Ta bort"
        act_del = menu.addAction(_icon('delete'), lbl)
        act_style = None
        if n == 1:
            mu = self.db.get_node_red_markup(rows[0].data(Qt.ItemDataRole.UserRole))
            if mu and dict(mu).get('type') == 'symbol':
                act_style = menu.addAction("📐 Ändra storlek/rotation...")
            else:
                act_style = menu.addAction(_icon('edit'), "Ändra stil...")
        result = menu.exec(self._table.viewport().mapToGlobal(pos))
        if result == act_del:
            for item in rows:
                mu_id = item.data(Qt.ItemDataRole.UserRole)
                self.db.delete_node_red_markup(mu_id)
                self.item_deleted.emit(mu_id)
            self.refresh()
        elif act_style is not None and result == act_style:
            mu_id = rows[0].data(Qt.ItemDataRole.UserRole)
            mu = self.db.get_node_red_markup(mu_id)
            if mu:
                mu = dict(mu)
                if mu.get('type') == 'symbol':
                    dlg = _SymbolDimsDialog(
                        float(mu.get('symbol_w', 40)),
                        float(mu.get('symbol_h', 40)),
                        float(mu.get('symbol_rot', 0)),
                        self)
                    if dlg.exec() == QDialog.DialogCode.Accepted:
                        w, h, rot = dlg.get_dims()
                        self.db.update_node_red_markup(mu_id, symbol_w=w, symbol_h=h, symbol_rot=rot)
                        self.item_style_changed.emit(mu_id)
                        self.refresh()
                else:
                    dlg = _MarkupStyleDialog(
                        mu.get('type', 'polygon'),
                        mu.get('color', '#CC0000'),
                        float(mu.get('opacity', 1.0)),
                        int(mu.get('line_width', 4)),
                        int(mu.get('font_size', 12)),
                        self)
                    if dlg.exec() == QDialog.DialogCode.Accepted:
                        c, op, lw, fs = dlg.get_style()
                        self.db.update_node_red_markup(mu_id, color=c, opacity=op,
                                                       line_width=lw, font_size=fs)
                        self.item_style_changed.emit(mu_id)
                        self.refresh()


class _SymbolDimsDialog(QDialog):
    """Dialog to adjust symbol width, height, and rotation."""
    def __init__(self, w=40, h=40, rot=0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Symbolstorlek och rotation")
        self.setFixedWidth(CONFIG['W_PANEL_MIN'])
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        form = QFormLayout()
        self._w_sp = QSpinBox(); self._w_sp.setRange(5, 500); self._w_sp.setValue(int(w))
        self._w_sp.setSuffix(" pt")
        self._h_sp = QSpinBox(); self._h_sp.setRange(5, 500); self._h_sp.setValue(int(h))
        self._h_sp.setSuffix(" pt")
        self._r_sp = QSpinBox(); self._r_sp.setRange(-360, 360); self._r_sp.setValue(int(rot))
        self._r_sp.setSuffix(" °")
        form.addRow("Bredd:", self._w_sp)
        form.addRow("Höjd:", self._h_sp)
        form.addRow("Rotation:", self._r_sp)
        outer.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def get_dims(self):
        return float(self._w_sp.value()), float(self._h_sp.value()), float(self._r_sp.value())


