// ── Fysikaliska formler ───────────────────────────────────────────────────────
'use strict';

// Darcy friktionsfaktor — Churchill (1977), exakt för alla Re
function frictionFactor(Re, epsilon, D) {
  if (Re < 1) return 1;
  if (Re < 2300) return 64 / Re;   // Laminärt
  const ed = epsilon / D;
  const A = Math.pow(-2.457 * Math.log(Math.pow(7/Re, 0.9) + 0.27*ed), 16);
  const B = Math.pow(37530 / Re, 16);
  return 8 * Math.pow(Math.pow(8/Re, 12) + 1/Math.pow(A + B, 1.5), 1/12);
}

// Reynolds-tal
function Re(v, D, nu) { return v * D / nu; }

// Strömningsregim
function flowRegime(re) {
  if (re < 2300)  return { text:'Laminärt',    cls:'regime-lam' };
  if (re < 4000)  return { text:'Övergång',    cls:'regime-trans' };
  return                  { text:'Turbulent',  cls:'regime-turb' };
}

// Tryckfall per meter (Pa/m) — Darcy-Weisbach
function dpPerMeter(v, D_m, lambda, rho) {
  return lambda * rho * v*v / (2 * D_m);
}

// Hastighet från flöde och area
function velocity(Q_m3s, A_m2) { return Q_m3s / A_m2; }

// ── Cirkulär kanal ────────────────────────────────────────────────────────────
function calcCircDuct(Q_ls, D_mm, T_air, eps_mm) {
  const Q = Q_ls / 1000;              // m³/s
  const D = D_mm / 1000;              // m
  const A = Math.PI * D*D / 4;
  const v = Q / A;
  const nu = airKinVis(T_air);
  const rho = airDensity(T_air);
  const re = Re(v, D, nu);
  const lam = frictionFactor(re, eps_mm/1000, D);
  const dp = dpPerMeter(v, D, lam, rho);
  return { v, dp, re, lam, regime: flowRegime(re) };
}

// ── Rektangulär kanal ─────────────────────────────────────────────────────────
// Hydraulisk diameter Dh = 4A/P = 2ab/(a+b)
function calcRectDuct(Q_ls, a_mm, b_mm, T_air, eps_mm) {
  const Q = Q_ls / 1000;
  const a = a_mm / 1000;
  const b = b_mm / 1000;
  const A = a * b;
  const Dh = 2*a*b/(a+b);           // hydraulisk diameter
  const v = Q / A;
  const nu = airKinVis(T_air);
  const rho = airDensity(T_air);
  const re = Re(v, Dh, nu);
  const lam = frictionFactor(re, eps_mm/1000, Dh);
  const dp = dpPerMeter(v, Dh, lam, rho);
  return { v, dp, re, lam, Dh: Dh*1000, regime: flowRegime(re) };
}

// ── Rörberäkning ──────────────────────────────────────────────────────────────
function calcPipe(Q_ls, id_mm, eps_mm, fluidProps) {
  const Q = Q_ls / 1000;
  const D = id_mm / 1000;
  const A = Math.PI * D*D / 4;
  const v = Q / A;
  const { nu, rho } = fluidProps;
  const re = Re(v, D, nu);
  const lam = frictionFactor(re, eps_mm/1000, D);
  const dp = dpPerMeter(v, D, lam, rho);
  return { v, dp, re, lam, regime: flowRegime(re) };
}

// Sök flöde för givet tryckfall per m (Newton-iteration)
function findFlowForDp(dp_target, id_mm, eps_mm, fluidProps) {
  const D = id_mm / 1000;
  const A = Math.PI * D*D / 4;
  const { nu, rho } = fluidProps;
  // Initial gissning: laminärt λ=0.04
  let Q = Math.sqrt(dp_target * 2*D / (0.04 * rho)) * A;
  for (let i = 0; i < 50; i++) {
    const v = Q / A;
    const re = Re(v, D, nu);
    const lam = frictionFactor(re, eps_mm/1000, D);
    const dp_calc = dpPerMeter(v, D, lam, rho);
    if (Math.abs(dp_calc - dp_target) / dp_target < 1e-7) break;
    Q *= Math.sqrt(dp_target / dp_calc);
  }
  return Q * 1000;  // l/s
}

// Sök flöde för given hastighet
function findFlowForV(v_target, id_mm) {
  const D = id_mm / 1000;
  const A = Math.PI * D*D / 4;
  return v_target * A * 1000;  // l/s
}

// Samma för cirkulär kanal
function findAirFlowForDp(dp_target, D_mm, T_air, eps_mm) {
  const D = D_mm / 1000;
  const A = Math.PI * D*D / 4;
  const nu = airKinVis(T_air);
  const rho = airDensity(T_air);
  let Q = Math.sqrt(dp_target * 2*D / (0.02 * rho)) * A;
  for (let i = 0; i < 50; i++) {
    const v = Q / A;
    const re = Re(v, D, nu);
    const lam = frictionFactor(re, eps_mm/1000, D);
    const dp_calc = dpPerMeter(v, D, lam, rho);
    if (Math.abs(dp_calc - dp_target) / dp_target < 1e-7) break;
    Q *= Math.sqrt(dp_target / dp_calc);
  }
  return Q * 1000;  // l/s
}

// ── Kv-beräkning ──────────────────────────────────────────────────────────────
// Q [m³/h] = Kv * √(ΔP [bar])
function kvCalc({ Kv, Q_m3h, dp_bar }) {
  if (Kv == null)    return { Kv: Q_m3h / Math.sqrt(dp_bar) };
  if (Q_m3h == null) return { Q_m3h: Kv * Math.sqrt(dp_bar), Q_ls: Kv * Math.sqrt(dp_bar) / 3.6 };
  if (dp_bar == null)return { dp_bar: Math.pow(Q_m3h / Kv, 2), dp_kPa: Math.pow(Q_m3h/Kv,2)*100 };
}

// Fe → Kv (gammalt nordiskt beteckningssystem)
// q [m³/h] = Fe * (d_mm)² * √(ΔP [mH₂O])   →   Kv = Fe * d² * √(ρ/ρ_ref) * konv
// Konvertering mH₂O till bar: 1 mH₂O = 0.09807 bar
// → Kv ≈ Fe * d² / sqrt(0.09807)^(-1) ... eller: Kv = Fe * d² * sqrt(1/0.09807)
function feToKv(Fe, d_mm) { return Fe * d_mm * d_mm * Math.sqrt(1/9807) * 1000; }
// Enklare direkt: (exakt ur def) Kv = Fe * d² * 0.03192 (d i mm)
// Verifierat: om Q(m³/h) = Fe * d² * sqrt(ΔP_mH2O) och Q=Kv*sqrt(ΔP_bar)
// sqrt(ΔP_bar) = sqrt(ΔP_mH2O * 0.09807) => Kv = Fe * d² * sqrt(0.09807) ≈ Fe * d² * 0.31318 / 1000 * 1000
// Korrekt: Kv = Fe * d_mm² * 0.000313
function feToKvCorr(Fe, d_mm) { return Fe * d_mm * d_mm * 0.000313; }

function calcFe({ Fe, d_mm, Q_m3h, dp_mH2O }) {
  if (Fe == null)      return { Fe: Q_m3h / (d_mm*d_mm * Math.sqrt(dp_mH2O)) };
  if (Q_m3h == null)   return { Q_m3h: Fe * d_mm*d_mm * Math.sqrt(dp_mH2O), Q_ls: Fe*d_mm*d_mm*Math.sqrt(dp_mH2O)/3.6 };
  if (dp_mH2O == null) return { dp_mH2O: Math.pow(Q_m3h/(Fe*d_mm*d_mm),2) };
}

// ── Effekt/flöde ──────────────────────────────────────────────────────────────
// Q = P / (ρ * cp * ΔT)   [l/s = W / (kg/m³ * J/kgK * K) * 1000]
function calcEffekt({ P_W, Q_ls, dT, cp = 4186, rho = null, T = 20 }) {
  if (rho === null) rho = waterDensity(T);
  if (P_W == null)  return { P_W: Q_ls/1000 * rho * cp * dT, P_kW: Q_ls/1000*rho*cp*dT/1000 };
  if (Q_ls == null) return { Q_ls: P_W / (rho * cp * dT) * 1000, Q_m3h: P_W/(rho*cp*dT)*3.6 };
}

// ── Värmeförlust isolerat rör (förbättring 9) ─────────────────────────────────
// Q' [W/m] = 2π * λ_ins * (Tf - Ta) / ln(r2/r1)
// r1 = ytterradien på röret, r2 = ytterradien på isoleringen
function heatLossInsulated(T_fluid, T_amb, od_mm, ins_mm, lambda_ins) {
  const r1 = od_mm / 2000;           // m (halva ytterdiameter)
  const r2 = r1 + ins_mm / 1000;     // m
  if (r2 <= r1) return null;
  return 2 * Math.PI * lambda_ins * (T_fluid - T_amb) / Math.log(r2 / r1);
}

// Oisolerat rör — fri konvektion + strålning inomhus (h ≈ 10 W/m²K)
function heatLossUninsulated(T_fluid, T_amb, od_mm, h = 10) {
  const D = od_mm / 1000;
  return h * Math.PI * D * (T_fluid - T_amb);
}

// ── Hjälpfunktion: summera uttryck t.ex. "150+50+75" (förbättring 16) ────────
function sumExpr(str) {
  try {
    const clean = str.replace(/[^0-9+.\-]/g, '');
    const val = clean.split('+').reduce((s, x) => s + (parseFloat(x)||0), 0);
    return isFinite(val) ? val : null;
  } catch { return null; }
}

// ── Rekommendationsindikator ──────────────────────────────────────────────────
function getRating(v, dp, type) {
  // type: 'duct_supply', 'duct_extract', 'pipe_heat', 'pipe_cool'
  if (type === 'duct_supply' || type === 'duct_extract') {
    if (v <= 5 && dp <= 1.0) return 'green';
    if (v <= 8 && dp <= 2.0) return 'yellow';
    return 'red';
  }
  if (type === 'pipe_heat' || type === 'pipe_cool') {
    if (v <= 0.8) return 'green';
    if (v <= 1.2) return 'yellow';
    return 'red';
  }
  return 'neutral';
}
