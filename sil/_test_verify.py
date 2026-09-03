import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r"C:\Users\AntonGustavsson\OneDrive - ProSa Process Safety Consulting AB\Desktop\ClaudeCodeTest\sil")
from calc import ComponentParams, Architecture, pfd_simplified, pfd_all_architectures

# SIF-001 Sensor: lambda_du_fit=57, lambda_dd_fit=17, beta=0.10, ti=8760, mttr=8, mission_time=87600, ptc=0.90
p_sensor = ComponentParams(
    name='Sensor', lambda_d=0.0, dc=0.0,
    beta=0.10, ti=8760.0, mttr=8.0, ptc=0.90, mission_time=87600.0,
    lambda_du_fit=57.0, lambda_dd_fit=17.0,
)
print(f"Sensor lambda_du: {p_sensor.lambda_du:.3e}  (expected 57e-9={57e-9:.3e})")
print(f"Sensor beta_d: {p_sensor.beta_d}  (expected 0.05)")

res = pfd_all_architectures(p_sensor)
print(f"Sensor 1oo1: {res['1oo1']:.3e}  (ref 4.74E-04)")
print(f"Sensor 1oo2: {res['1oo2']:.3e}  (ref 2.51E-05)")
print(f"Sensor 2oo2: {res['2oo2']:.3e}  (ref 5.00E-04)")
print(f"Sensor 2oo3: {res['2oo3']:.3e}  (ref 2.54E-05)")
print()

# Logic solver
p_logic = ComponentParams(
    name='Logic', lambda_d=0.0, dc=0.0,
    beta=0.05, ti=8760.0, mttr=8.0, ptc=0.90, mission_time=87600.0,
    lambda_du_fit=18.0, lambda_dd_fit=17.0,
)
res_l = pfd_all_architectures(p_logic)
print(f"Logic 1oo1: {res_l['1oo1']:.3e}  (ref 1.50E-04)")
print(f"Logic 1oo2: {res_l['1oo2']:.3e}  (ref 3.96E-06)")
print(f"Logic 2oo2: {res_l['2oo2']:.3e}  (ref 1.58E-04)")
print(f"Logic 2oo3: {res_l['2oo3']:.3e}  (ref 3.99E-06)")
print()

# Final element
p_fe = ComponentParams(
    name='FE', lambda_d=0.0, dc=0.0,
    beta=0.10, ti=8760.0, mttr=24.0, ptc=0.90, mission_time=87600.0,
    lambda_du_fit=26.0, lambda_dd_fit=57.0,
)
res_fe = pfd_all_architectures(p_fe)
print(f"FE 1oo1: {res_fe['1oo1']:.3e}  (ref 2.18E-04)")
print(f"FE 1oo2: {res_fe['1oo2']:.3e}  (ref 1.15E-05)")
print(f"FE 2oo2: {res_fe['2oo2']:.3e}  (ref 2.30E-04)")
print(f"FE 2oo3: {res_fe['2oo3']:.3e}  (ref 1.15E-05)")
print()

# SIF-001 total (2oo2 sensor + 1oo1 logic + 1oo1 FE)
sensor_sel = pfd_simplified(Architecture.OO2_2, p_sensor)  # 2oo2
logic_sel  = pfd_simplified(Architecture.OO1,   p_logic)   # 1oo1
fe_sel     = pfd_simplified(Architecture.OO1,   p_fe)      # 1oo1
total = 1.0 - (1-sensor_sel)*(1-logic_sel)*(1-fe_sel)
print(f"Total PFD: {total:.3e}  (ref 8.67E-04)")
print(f"RRF: {round(1/total)}  (ref 1153)")

# Test verification_db
import verification_db as vdb
vdb.init_db()
print(f"\nDB case IDs: {vdb.get_case_ids()}")
rows = vdb.run_verification(
    "SIF-001",
    res, sensor_sel,
    res_l, logic_sel,
    res_fe, fe_sel,
    total, 3
)
pass_count = sum(1 for r in rows if r.get("ok") is True)
fail_count = sum(1 for r in rows if r.get("ok") is False)
print(f"Verification: {pass_count} OK, {fail_count} FAIL")
for r in rows:
    if r.get("ok") is not None:
        status = "OK" if r["ok"] else "FAIL"
        print(f"  {r['label']:35s} calc={r['calc']:.3e}  ref={r['ref']:.3e}  diff={r['diff_pct']:.2f}%  {status}")
