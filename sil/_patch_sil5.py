import sys
sys.stdout.reconfigure(encoding='utf-8')

path = r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil\sil.py"
with open(path, encoding='utf-8') as f:
    content = f.read()

# Step 7: Add VerificationDialog before App class and add import for verification_db
# First add import at top (after "import components_db as cdb")
old_import_cdb = 'import components_db as cdb'
new_import_cdb = '''import components_db as cdb
try:
    import verification_db as vdb
    _VDB_AVAILABLE = True
except ImportError:
    _VDB_AVAILABLE = False'''

if old_import_cdb in content:
    content = content.replace(old_import_cdb, new_import_cdb, 1)
    print("Step 7a (vdb import) OK")
else:
    print("Step 7a FAILED")

# Step 7b: Add VerificationDialog class before App class
verif_dialog = '''
# ── Verifieringsdialog ─────────────────────────────────────────────────────────
class VerificationDialog(tk.Toplevel):
    """Verifierar beräkningsresultat mot referensfall i databasen."""

    def __init__(self, parent, sifs_data: list, current_sif_index: int):
        super().__init__(parent)
        self.title("Verifiera mot referensfall — SIF-001")
        self.geometry("860x600"); self.resizable(True, True)
        self.transient(parent); self.grab_set()
        self._data = sifs_data
        self._idx  = current_sif_index
        self._build(); self.wait_window()

    def _build(self):
        if not _VDB_AVAILABLE:
            tk.Label(self, text="verification_db.py saknas.", font=FONT_N, fg=RED).pack(pady=20)
            ttk.Button(self, text="Stäng", command=self.destroy).pack()
            return

        top = tk.Frame(self, bg=BG, padx=8, pady=6); top.pack(fill="x")
        tk.Label(top, text="Referensfall:", font=FONT_N, bg=BG).pack(side="left")
        self._v_case = tk.StringVar()
        try:
            cases = vdb.get_case_ids()
        except Exception:
            vdb.init_db(); cases = vdb.get_case_ids()
        self._v_case.set(cases[0] if cases else "")
        ttk.Combobox(top, textvariable=self._v_case, values=cases,
                     width=18, state="readonly", font=FONT_N).pack(side="left", padx=(4,12))
        ttk.Button(top, text="Kör verifiering", command=self._run).pack(side="left")

        frm = tk.Frame(self); frm.pack(fill="both", expand=True, padx=8, pady=(4,8))
        cols = ("label","calc","ref","diff","ok")
        hdrs = ("Kontroll","Beräknat","Referens","Avv. %","Status")
        ws   = (260,110,110,80,70)
        self._tv = ttk.Treeview(frm, columns=cols, show="headings", selectmode="none")
        for c,h,w in zip(cols,hdrs,ws):
            self._tv.heading(c, text=h); self._tv.column(c, width=w, minwidth=40)
        self._tv.tag_configure("ok",    foreground="#1e8449")
        self._tv.tag_configure("fail",  foreground="#c0392b")
        self._tv.tag_configure("header",foreground="#1a5276", font=("Segoe UI",9,"bold"))
        self._tv.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frm, orient="vertical", command=self._tv.yview)
        sb.pack(side="right", fill="y"); self._tv.configure(yscrollcommand=sb.set)

        bf = tk.Frame(self, bg=BG, padx=8, pady=6); bf.pack(fill="x")
        self._summary = tk.Label(bf, text="", font=FONT_N, bg=BG, anchor="w")
        self._summary.pack(side="left", fill="x", expand=True)
        ttk.Button(bf, text="Stäng", command=self.destroy).pack(side="right")

    def _run(self):
        if not _VDB_AVAILABLE:
            return
        case_id = self._v_case.get()
        if not case_id:
            messagebox.showinfo("Info", "Välj ett referensfall.", parent=self)
            return

        # Build params from current SIF data using simplified method + FIT inputs
        sif = self._data[self._idx] if 0 <= self._idx < len(self._data) else {}

        def mk_comp_fit(d: dict) -> "ComponentParams":
            """Build ComponentParams from subsystem dict with FIT support."""
            ct = d.get("comp_type", "A (enkel)")[0]
            # Get FIT values if present
            fit_du_str = d.get("fit_du", "")
            fit_dd_str = d.get("fit_dd", "")
            try:
                fit_du = float(fit_du_str) if fit_du_str.strip() else 0.0
            except (ValueError, AttributeError):
                fit_du = 0.0
            try:
                fit_dd = float(fit_dd_str) if fit_dd_str.strip() else 0.0
            except (ValueError, AttributeError):
                fit_dd = 0.0
            return ComponentParams(
                name=d.get("name", ""),
                lambda_d=float(d.get("lambda_d", 1e-6)),
                dc=float(d.get("dc", 0)),
                beta=float(d.get("beta", 0.02)),
                ti=float(d.get("ti", 8760)),
                mttr=float(d.get("mttr", 8)),
                ptc=float(d.get("ptc", 1.0)),
                sff=float(d.get("sff", 0)),
                comp_type=ct,
                sc=int(float(d.get("sc", 0))),
                st=float(d.get("st", 0)),
                mission_time=float(d.get("mission_time", 87600)),
                pst_coverage=float(d.get("pst_coverage", 0)),
                pst_interval=float(d.get("pst_interval", 720)),
                ccf_model=d.get("ccf_model", "beta"),
                lambda_du_fit=fit_du,
                lambda_dd_fit=fit_dd,
            )

        try:
            p_sensor = mk_comp_fit(sif.get("sensor", {}))
            p_logic  = mk_comp_fit(sif.get("logic",  {}))
            p_fe     = mk_comp_fit(sif.get("fe",     {}))

            sensor_all = pfd_all_architectures(p_sensor)
            logic_all  = pfd_all_architectures(p_logic)
            fe_all     = pfd_all_architectures(p_fe)

            arch_s = sif.get("sensor", {}).get("arch", "1oo1")
            arch_l = sif.get("logic",  {}).get("arch", "1oo1")
            arch_fe= sif.get("fe",     {}).get("arch", "1oo1")

            sensor_sel = pfd_simplified(Architecture(arch_s), p_sensor)
            logic_sel  = pfd_simplified(Architecture(arch_l), p_logic)
            fe_sel     = pfd_simplified(Architecture(arch_fe), p_fe)

            total_pfd = 1.0 - (1.0 - sensor_sel) * (1.0 - logic_sel) * (1.0 - fe_sel)
            sil_ach = sil_from_pfd(total_pfd)

            rows = vdb.run_verification(
                case_id,
                sensor_all, sensor_sel,
                logic_all,  logic_sel,
                fe_all,     fe_sel,
                total_pfd,  sil_ach)

        except Exception as e:
            messagebox.showerror("Fel", str(e), parent=self)
            return

        # Display results
        self._tv.delete(*self._tv.get_children())
        pass_count = fail_count = 0
        for row in rows:
            calc_v = row.get("calc")
            ref_v  = row.get("ref")
            diff_v = row.get("diff_pct")
            ok     = row.get("ok")

            if calc_v is None and ref_v is None:
                # Header row
                self._tv.insert("", "end",
                    values=(row["label"], "", "", "", ""),
                    tags=("header",))
                continue

            calc_str = f"{calc_v:.4e}" if isinstance(calc_v, float) else str(calc_v) if calc_v is not None else "—"
            ref_str  = f"{ref_v:.4e}"  if isinstance(ref_v,  float) else str(ref_v)  if ref_v  is not None else "—"
            diff_str = f"{diff_v:.2f}%" if diff_v is not None else "—"
            ok_str   = "OK" if ok else ("FAIL" if ok is not None else "—")
            tag = "ok" if ok else ("fail" if ok is not None else "")
            self._tv.insert("", "end",
                values=(row["label"], calc_str, ref_str, diff_str, ok_str),
                tags=(tag,))
            if ok is True:  pass_count += 1
            elif ok is False: fail_count += 1

        total_checks = pass_count + fail_count
        col = GREEN if fail_count == 0 else RED
        self._summary.config(
            text=f"{pass_count}/{total_checks} kontroller OK" + (f"  — {fail_count} fel" if fail_count else "  — Alla godkända"),
            fg=col)


'''

old_app_class = '# ── Huvud-app ──────────────────────────────────────────────────────────────────\nclass App(tk.Tk):'
new_app_class = verif_dialog + '# ── Huvud-app ──────────────────────────────────────────────────────────────────\nclass App(tk.Tk):'

if old_app_class in content:
    content = content.replace(old_app_class, new_app_class)
    print("Step 7b (VerificationDialog) OK")
else:
    print("Step 7b FAILED")
    idx = content.find('# ── Huvud-app')
    print(f"Found at {idx}")

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Saved step 7")
