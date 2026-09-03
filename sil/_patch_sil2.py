import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\sil.py"
with open(path, encoding='utf-8') as f:
    content = f.read()

# Step 2: Insert FIT section and shift existing rows in _build()
# Find the separator at row=9 through the toggle button at row=12
old_build_end = '''        # Komponenttyp
        ttk.Separator(self, orient="horizontal").grid(
            row=9,column=0,columnspan=5,sticky="ew",pady=(2,4))
        ttk.Label(self, text="Komponenttyp (IEC 61508):", font=FONT_S,
                  foreground="#666").grid(row=10,column=0,columnspan=2,sticky="w")
        self.v_ctype = tk.StringVar(value="A (enkel)")
        ttk.Combobox(self, textvariable=self.v_ctype, values=COMP_TYPES,
                     width=14, state="readonly", font=FONT_S).grid(
            row=11,column=0,columnspan=2,sticky="w",pady=(2,4))
        ttk.Label(self, text="A=enkel/mekanisk  B=komplex/mikroprocessor",
                  font=FONT_S, foreground="#999").grid(
            row=11,column=2,columnspan=3,sticky="w",padx=(12,0))

        # ── Avancerade parametrar (fällbara) ───────────────────────────────────
        self._toggle_btn = ttk.Button(self, text="▶ Avancerade parametrar",
                                      command=self._toggle_adv)
        self._toggle_btn.grid(row=12,column=0,columnspan=5,sticky="w",pady=(4,0))

        self._adv_frame = tk.Frame(self, bg=BG_FRAME)
        # byggs men visas inte förrän toggle
        self._build_adv()

        for c in range(5): self.columnconfigure(c,weight=1)'''

new_build_end = '''        # ── FIT-ingång ─────────────────────────────────────────────────────────
        fit_frm = tk.Frame(self); fit_frm.grid(row=9, column=0, columnspan=5, sticky="ew", pady=(2,4))
        tk.Label(fit_frm, text="FIT-ingång:", font=FONT_S, fg="#666").pack(side="left")
        tk.Label(fit_frm, text="λDU:", font=FONT_S).pack(side="left", padx=(8,2))
        self.v_fit_du = tk.StringVar()
        ttk.Entry(fit_frm, textvariable=self.v_fit_du, width=9, font=FONT_M).pack(side="left")
        tk.Label(fit_frm, text="λDD:", font=FONT_S).pack(side="left", padx=(8,2))
        self.v_fit_dd = tk.StringVar()
        ttk.Entry(fit_frm, textvariable=self.v_fit_dd, width=9, font=FONT_M).pack(side="left")
        tk.Label(fit_frm, text="FIT", font=FONT_S, fg="#888").pack(side="left", padx=(2,8))
        ttk.Button(fit_frm, text="→ Applicera", command=self._apply_fit).pack(side="left")
        _Tip(tk.Label(fit_frm, text="(?)", font=FONT_S, fg="#aaa"),
             "Ange λDU och λDD direkt i FIT (fel per 10⁹ h) från FMEDA-data.\n"
             "Beräknar λD = (λDU+λDD)×10⁻⁹ och DC = λDD/(λDU+λDD).").pack(side="left")

        # Komponenttyp
        ttk.Separator(self, orient="horizontal").grid(
            row=10,column=0,columnspan=5,sticky="ew",pady=(2,4))
        ttk.Label(self, text="Komponenttyp (IEC 61508):", font=FONT_S,
                  foreground="#666").grid(row=11,column=0,columnspan=2,sticky="w")
        self.v_ctype = tk.StringVar(value="A (enkel)")
        ttk.Combobox(self, textvariable=self.v_ctype, values=COMP_TYPES,
                     width=14, state="readonly", font=FONT_S).grid(
            row=12,column=0,columnspan=2,sticky="w",pady=(2,4))
        ttk.Label(self, text="A=enkel/mekanisk  B=komplex/mikroprocessor",
                  font=FONT_S, foreground="#999").grid(
            row=12,column=2,columnspan=3,sticky="w",padx=(12,0))

        # ── Avancerade parametrar (fällbara) ───────────────────────────────────
        self._toggle_btn = ttk.Button(self, text="▶ Avancerade parametrar",
                                      command=self._toggle_adv)
        self._toggle_btn.grid(row=13,column=0,columnspan=5,sticky="w",pady=(4,0))

        self._adv_frame = tk.Frame(self, bg=BG_FRAME)
        # byggs men visas inte förrän toggle
        self._build_adv()

        for c in range(5): self.columnconfigure(c,weight=1)'''

if old_build_end in content:
    content = content.replace(old_build_end, new_build_end)
    print("Step 2 (FIT section + row shift) OK")
else:
    print("Step 2 FAILED")
    idx = content.find("# Komponenttyp")
    print(f"Komponenttyp found at {idx}")
    print(repr(content[idx:idx+200]))

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Saved step 2")
