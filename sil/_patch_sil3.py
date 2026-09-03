import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\sil.py"
with open(path, encoding='utf-8') as f:
    content = f.read()

# Step 3: Fix _toggle_adv row (was 13, needs to be 14 now)
old_toggle = '            self._adv_frame.grid(row=13,column=0,columnspan=5,sticky="ew",pady=(2,4))'
new_toggle = '            self._adv_frame.grid(row=14,column=0,columnspan=5,sticky="ew",pady=(2,4))'
if old_toggle in content:
    content = content.replace(old_toggle, new_toggle)
    print("Step 3 (_toggle_adv row fix) OK")
else:
    print("Step 3 FAILED")

# Step 4: Add _apply_fit method after _toggle_adv
old_toggle_method = '''    def _toggle_adv(self):
        self._adv_open = not self._adv_open
        if self._adv_open:
            self._toggle_btn.config(text="▼ Avancerade parametrar")
            self._adv_frame.grid(row=14,column=0,columnspan=5,sticky="ew",pady=(2,4))
        else:
            self._toggle_btn.config(text="▶ Avancerade parametrar")
            self._adv_frame.grid_remove()'''

new_toggle_method = '''    def _toggle_adv(self):
        self._adv_open = not self._adv_open
        if self._adv_open:
            self._toggle_btn.config(text="▼ Avancerade parametrar")
            self._adv_frame.grid(row=14,column=0,columnspan=5,sticky="ew",pady=(2,4))
        else:
            self._toggle_btn.config(text="▶ Avancerade parametrar")
            self._adv_frame.grid_remove()

    def _apply_fit(self):
        """Konverterar FIT-värden (λDU, λDD) till λD och DC och fyller i fälten."""
        try:
            ldu = float(self.v_fit_du.get()) if self.v_fit_du.get().strip() else 0.0
            ldd = float(self.v_fit_dd.get()) if self.v_fit_dd.get().strip() else 0.0
        except ValueError:
            messagebox.showerror("Fel", "Ange giltiga tal för λDU och λDD i FIT.", parent=self.winfo_toplevel())
            return
        if ldu < 0 or ldd < 0:
            messagebox.showerror("Fel", "FIT-värden måste vara ≥ 0.", parent=self.winfo_toplevel())
            return
        total_fit = ldu + ldd
        if total_fit <= 0:
            messagebox.showinfo("Info", "Ange minst ett FIT-värde > 0.", parent=self.winfo_toplevel())
            return
        lambda_d_h = total_fit * 1e-9    # [1/h]
        dc = ldd / total_fit if total_fit > 0 else 0.0
        self._vars["lambda_d"].set(f"{lambda_d_h:.3g}")
        self._vars["dc"].set(f"{dc:.4f}")'''

if old_toggle_method in content:
    content = content.replace(old_toggle_method, new_toggle_method)
    print("Step 4 (_apply_fit) OK")
else:
    print("Step 4 FAILED")
    idx = content.find("def _toggle_adv")
    print(repr(content[idx:idx+350]))

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Saved steps 3-4")
