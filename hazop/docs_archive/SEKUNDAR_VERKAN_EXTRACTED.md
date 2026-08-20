# Sekundär verkan — extracted reference (for future merged cause-picker popup)

**Extracted:** 2026-08-02, during dead-code removal (phase 1 of cause-creation-UI
consolidation). This snippet was copied out of `TemplateCausePickerDialog` in
`pid_viewer.py` **before that class was deleted** as confirmed-dead code (zero
live construction call sites — superseded by `StandardCausesPickerPopup` in
`hazop.py`).

**Purpose / when triggered:** This is the "Sekundär verkan" (secondary effect)
feature. It applied specifically when the user selected component type
**"Instrument / Sensor"** in the cause-picker dialog's "Komponenttyp" combo box
(checked via `'Instrument' in comp_type`). When that type was selected, an extra
`QGroupBox` section appeared letting the user capture the full causal chain:
instrument fails → some other equipment (pump/valve/damper/ESD/alarm) reacts as
a consequence. The user picked one of several canned secondary effects (radio
buttons) or typed free text, optionally tagged with a secondary component ID
(e.g. "P-101"), and the dialog combined it with the primary cause description
into a single string like `"Signalfel högt → Pump stoppar (P-101)"`. It also
tracked a `secondary_comp_type` (e.g. "Pump") so a second marker of the right
type could be placed on the P&ID for the secondary component
(`wants_secondary_placement` / "📍 Markera objekt på P&ID" button).

This phase does NOT rebuild this UI — it is purely preserved here so the data
and logic aren't lost when the dead class is deleted. A later phase will port
it into the new merged cause-creation popup.

---

## 1. Data constants (verbatim, from pid_viewer.py, defined just above the class)

```python
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
```

Note: "Nödstoppar / ESD" and "Larm aktiveras (ingen automatisk åtgärd)" have no
entry in `_INSTR_SEC_COMP_TYPES` — they intentionally fall through to `.get(sec_desc, '')`
(empty component type), since those two effects don't correspond to a specific
secondary equipment type to mark on the P&ID.

## 2. UI-building code (verbatim, from `TemplateCausePickerDialog.__init__`)

```python
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
self._sec_free_edit.textChanged.connect(partial(rb_sec_free.setChecked, True))
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
```

Toggling visibility when the component-type combo changes (from `_on_type_changed`):

```python
def _on_type_changed(self, comp_type):
    self._update_cause_list(comp_type)
    is_instrument = 'Instrument' in comp_type
    self._instr_group.setVisible(is_instrument)
```

## 3. Text-combination logic (verbatim, from `_accept_with_secondary` / `_accept`)

```python
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
```

Exposed as properties for the caller to read after `exec()`/`accept()`:

```python
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
```

---

## Result example

Primary cause "Signalfel högt" + secondary effect "Pump stoppar" + secondary tag
"P-101" combine into:

```
Signalfel högt → Pump stoppar (P-101)
```

...with `secondary_comp_type == "Pump"` available so a second P&ID marker of type
Pump can be placed for the secondary component, if the user clicked "📍 Markera
objekt på P&ID" (`wants_secondary_placement == True`) instead of plain OK.
