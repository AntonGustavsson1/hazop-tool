import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\sil.py"
with open(path, encoding='utf-8') as f:
    content = f.read()

# Step 8: Update Databas menu to add verification
old_dm = '''        dm = tk.Menu(mb, tearoff=0)
        dm.add_command(label="Hantera komponenter...", command=lambda: ComponentManagerDialog(self))
        mb.add_cascade(label="Databas", menu=dm)'''

new_dm = '''        dm = tk.Menu(mb, tearoff=0)
        dm.add_command(label="Hantera komponenter...", command=lambda: ComponentManagerDialog(self))
        dm.add_separator()
        dm.add_command(label="Verifiera mot referensfall...", command=self._open_verification)
        mb.add_cascade(label="Databas", menu=dm)'''

if old_dm in content:
    content = content.replace(old_dm, new_dm)
    print("Step 8 (Databas menu) OK")
else:
    print("Step 8 FAILED")

# Step 9: Add _open_verification method and vdb.init_db() call
# Add vdb.init_db() call in App.__init__ after cdb.init_db()
old_init = '''        cdb.init_db()
        self._sifs: list[dict] = []'''

new_init = '''        cdb.init_db()
        if _VDB_AVAILABLE:
            try:
                vdb.init_db()
            except Exception:
                pass
        self._sifs: list[dict] = []'''

if old_init in content:
    content = content.replace(old_init, new_init)
    print("Step 9a (vdb.init_db) OK")
else:
    print("Step 9a FAILED")

# Step 9b: Add _open_verification after _sensitivity method
old_sensitivity_end = '''    def _sensitivity(self):
        self._save_current()
        try:
            sp = self.frm_sensor.get_params()
            lp = self.frm_logic.get_params()
            fp = self.frm_fe.get_params()
        except ValueError as e:
            messagebox.showerror("Inmatningsfel", str(e)); return
        SensitivityWindow(self, sp, lp, fp, int(self.v_sil_req.get()))

    # ── Fil ─────────────────────────────────────────────────────────────────────'''

new_sensitivity_end = '''    def _sensitivity(self):
        self._save_current()
        try:
            sp = self.frm_sensor.get_params()
            lp = self.frm_logic.get_params()
            fp = self.frm_fe.get_params()
        except ValueError as e:
            messagebox.showerror("Inmatningsfel", str(e)); return
        SensitivityWindow(self, sp, lp, fp, int(self.v_sil_req.get()))

    def _open_verification(self):
        if not _VDB_AVAILABLE:
            messagebox.showinfo(
                "Saknas",
                "verification_db.py saknas.\nKopiera filen till samma mapp som sil.py.",
                parent=self)
            return
        self._save_current()
        VerificationDialog(self, self._sifs, self._current)

    # ── Fil ─────────────────────────────────────────────────────────────────────'''

if old_sensitivity_end in content:
    content = content.replace(old_sensitivity_end, new_sensitivity_end)
    print("Step 9b (_open_verification) OK")
else:
    print("Step 9b FAILED")
    idx = content.find("def _sensitivity")
    print(repr(content[idx:idx+300]))

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Saved steps 8-9")
