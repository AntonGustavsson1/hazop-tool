// ── Standarddimensioner och materialdatabas ──────────────────────────────────
'use strict';

// Cirkulära kanaldiametrar enligt EN 1505 (mm)
const CIRC_DUCTS = [63,80,100,125,160,200,250,315,400,500,630,800,1000,1250];

// Rektangulära kanalstandardmått per sida (mm) enligt EN 1505
const RECT_SIDES = [100,150,200,250,300,400,500,600,800,1000,1200,1400,1600,2000,2500];

// Kanal-material med ytråhet (mm) och visningsnamn
const DUCT_MATS = {
  'Plåtkanal Lindab SR':  { eps: 0.09,  note: 'Förzinkad plåt, Lindab SR-standard' },
  'Plåtkanal Veloduct':   { eps: 0.15,  note: 'Förzinkad plåt, Veloduct-standard' },
  'Aluminiumkanal':       { eps: 0.02,  note: 'Aluminium, slät inneryta' },
  'Betongkanal':          { eps: 1.0,   note: 'Betong, ojämn yta' },
  'Anpassa ytråhet':      { eps: null,  note: 'Ange önskad ytråhet i mm' },
};

// Rörmaterial: ytråhet, typisk livslängd, kommentar
const PIPE_MATS = {
  'Stål, svart (EN 10255)':         { eps: 0.046, life: '40+ år',  note: 'Kolstål, ej galvaniserat' },
  'Stål, galvaniserat (EN 10255)':  { eps: 0.046, life: '40+ år',  note: 'Varmförzinkad stålrör, invändigt ej zink' },
  'Koppar (EN 1057)':               { eps: 0.0015,life: '50+ år',  note: 'Halvhård koppar, mjuklödd eller pressad' },
  'Rostfritt stål (EN 10217-7)':    { eps: 0.015, life: '50+ år',  note: 'AISI 316L, pressad fog' },
  'PEX (EN ISO 15875)':             { eps: 0.007, life: '50 år',   note: 'Tvärbunden polyeten, typ A/B/C' },
  'AluPEX (EN ISO 21003)':          { eps: 0.007, life: '50 år',   note: 'Komposit PEX-Al-PEX, diffusionstätt' },
  'PEM / PE100 (EN 12201)':         { eps: 0.007, life: '50+ år',  note: 'Polyeten hög täthet, HD-PE' },
  'PP-R (EN ISO 15874)':            { eps: 0.007, life: '50 år',   note: 'Polypropylen random copolymer' },
  'PVC-U (EN ISO 1452)':            { eps: 0.0015,life: '50 år',   note: 'Hårt PVC, kall vattenledning' },
  'Gjutjärn (EN 877)':              { eps: 0.25,  life: '60+ år',  note: 'Gråjärnsrör med intern beläggning' },
};

// DN-tabell: { dn_nominal: { id: invändig diameter mm, od: ytterdiameter mm } }
const DN_TABLES = {
  'Stål, svart (EN 10255)': {
    15:{id:16.1,od:21.3}, 20:{id:21.7,od:26.9}, 25:{id:27.3,od:33.7},
    32:{id:36.0,od:42.4}, 40:{id:42.3,od:48.3}, 50:{id:54.5,od:60.3},
    65:{id:70.3,od:76.1}, 80:{id:82.5,od:88.9}, 100:{id:107.1,od:114.3},
    125:{id:131.7,od:139.7}, 150:{id:159.3,od:168.3},
  },
  'Stål, galvaniserat (EN 10255)': {
    15:{id:16.1,od:21.3}, 20:{id:21.7,od:26.9}, 25:{id:27.3,od:33.7},
    32:{id:36.0,od:42.4}, 40:{id:42.3,od:48.3}, 50:{id:54.5,od:60.3},
    65:{id:70.3,od:76.1}, 80:{id:82.5,od:88.9}, 100:{id:107.1,od:114.3},
    125:{id:131.7,od:139.7}, 150:{id:159.3,od:168.3},
  },
  'Koppar (EN 1057)': {
    12:{id:10.0,od:12.0}, 15:{id:13.0,od:15.0}, 18:{id:16.0,od:18.0},
    22:{id:20.0,od:22.0}, 28:{id:26.0,od:28.0}, 35:{id:32.0,od:35.0},
    42:{id:39.0,od:42.0}, 54:{id:50.0,od:54.0}, 64:{id:60.0,od:64.0},
    76:{id:72.1,od:76.1}, 88:{id:84.9,od:88.9}, 108:{id:103.0,od:108.0},
  },
  'Rostfritt stål (EN 10217-7)': {
    15:{id:16.1,od:21.3}, 20:{id:21.7,od:26.9}, 25:{id:27.3,od:33.7},
    32:{id:36.0,od:42.4}, 40:{id:42.3,od:48.3}, 50:{id:54.5,od:60.3},
    65:{id:70.3,od:76.1}, 80:{id:82.5,od:88.9}, 100:{id:107.1,od:114.3},
    125:{id:131.7,od:139.7}, 150:{id:159.3,od:168.3},
  },
  'PEX (EN ISO 15875)': {
    16:{id:12.0,od:16.0}, 20:{id:16.0,od:20.0}, 25:{id:20.4,od:25.0},
    32:{id:26.2,od:32.0}, 40:{id:32.6,od:40.0}, 50:{id:40.8,od:50.0},
    63:{id:51.4,od:63.0}, 75:{id:61.2,od:75.0}, 90:{id:73.6,od:90.0},
    110:{id:90.0,od:110.0},
  },
  'AluPEX (EN ISO 21003)': {
    16:{id:11.8,od:16.0}, 20:{id:15.8,od:20.0}, 25:{id:20.4,od:25.0},
    32:{id:26.2,od:32.0}, 40:{id:32.6,od:40.0}, 50:{id:40.8,od:50.0},
    63:{id:51.4,od:63.0},
  },
  'PEM / PE100 (EN 12201)': {
    20:{id:16.0,od:20.0}, 25:{id:20.0,od:25.0}, 32:{id:26.0,od:32.0},
    40:{id:32.6,od:40.0}, 50:{id:40.8,od:50.0}, 63:{id:51.4,od:63.0},
    75:{id:61.4,od:75.0}, 90:{id:73.6,od:90.0}, 110:{id:90.0,od:110.0},
    125:{id:102.2,od:125.0}, 160:{id:130.8,od:160.0},
  },
  'PP-R (EN ISO 15874)': {
    20:{id:13.2,od:20.0}, 25:{id:16.6,od:25.0}, 32:{id:21.2,od:32.0},
    40:{id:26.6,od:40.0}, 50:{id:33.0,od:50.0}, 63:{id:41.8,od:63.0},
    75:{id:49.8,od:75.0}, 90:{id:59.6,od:90.0}, 110:{id:72.8,od:110.0},
  },
  'PVC-U (EN ISO 1452)': {
    20:{id:16.6,od:20.0}, 25:{id:21.2,od:25.0}, 32:{id:27.8,od:32.0},
    40:{id:35.2,od:40.0}, 50:{id:44.0,od:50.0}, 63:{id:55.8,od:63.0},
    75:{id:66.6,od:75.0}, 90:{id:80.0,od:90.0}, 110:{id:98.2,od:110.0},
    125:{id:111.8,od:125.0}, 160:{id:143.0,od:160.0},
  },
  'Gjutjärn (EN 877)': {
    50:{id:52.0,od:60.0}, 70:{id:72.0,od:82.0}, 100:{id:102.0,od:114.0},
    125:{id:127.0,od:140.0}, 150:{id:153.0,od:168.0}, 200:{id:204.0,od:220.0},
  },
};

// ── Glykol/köldmedia (förbättring 3) ──────────────────────────────────────────
// Etylenglykol: kinematisk viskositet (×10⁻⁶ m²/s) och densitet (kg/m³)
// Kolumner: [temp°C, 20vol%, 30vol%, 40vol%, 50vol%]
const EG_VISC = [
  [-20,null,14.0,32.0,null], [-10,5.8,9.0,18.0,null],
  [0,3.5,5.8,10.5,24.0],    [10,2.5,3.9,6.5,13.5],
  [20,1.8,2.8,4.5,8.5],     [30,1.4,2.1,3.3,5.8],
  [40,1.1,1.6,2.5,4.1],     [50,0.9,1.3,2.0,3.1],
  [60,0.8,1.0,1.6,2.5],     [70,0.7,0.9,1.3,2.0],
  [80,0.6,0.8,1.1,1.7],
];
const EG_DENS = [
  [-20,null,1051,1066,null], [-10,1029,1045,1061,null],
  [0,1033,1048,1063,1078],   [10,1030,1045,1060,1075],
  [20,1026,1041,1056,1071],  [30,1020,1035,1050,1065],
  [40,1014,1029,1044,1059],  [50,1006,1021,1036,1050],
  [60,997,1012,1027,1041],   [70,986,1001,1016,1030],
  [80,974,988,1003,1017],
];

// Propylenglykol: kinematisk viskositet och densitet
const PG_VISC = [
  [-10,14.0,40.0,null,null], [0,6.5,18.0,60.0,null],
  [10,4.0,10.0,28.0,null],   [20,2.8,6.0,15.0,55.0],
  [30,2.1,4.1,9.0,30.0],     [40,1.6,2.9,5.8,17.0],
  [50,1.3,2.2,4.0,10.5],     [60,1.0,1.7,2.9,7.0],
  [70,0.9,1.3,2.2,5.0],      [80,0.7,1.1,1.8,3.8],
];
const PG_DENS = [
  [0,1021,1034,1046,1057],   [10,1018,1031,1043,1054],
  [20,1014,1027,1039,1050],  [30,1009,1022,1034,1045],
  [40,1003,1016,1028,1039],  [50,996,1009,1021,1032],
  [60,988,1001,1013,1024],   [70,979,992,1004,1015],
  [80,969,982,994,1005],
];

// Isolering för värmeförlustberäkning (förbättring 9)
const INSUL_TYPES = {
  'Mineralull (λ=0.040)':  { lambda: 0.040 },
  'Glasull (λ=0.036)':     { lambda: 0.036 },
  'PUR/PIR (λ=0.030)':     { lambda: 0.030 },
  'Cellgummi (λ=0.040)':   { lambda: 0.040 },
  'PEF/EPS (λ=0.033)':     { lambda: 0.033 },
};

// Interpolera i en 2D-tabell [temp, c1, c2, c3, c4] vid index col (1-4)
function interpTable(table, T, col) {
  const vals = table.filter(r => r[col] !== null);
  if (vals.length === 0) return null;
  if (T <= vals[0][0]) return vals[0][col];
  if (T >= vals[vals.length-1][0]) return vals[vals.length-1][col];
  for (let i = 0; i < vals.length-1; i++) {
    if (T >= vals[i][0] && T <= vals[i+1][0]) {
      const t = (T - vals[i][0]) / (vals[i+1][0] - vals[i][0]);
      return vals[i][col] + t*(vals[i+1][col] - vals[i][col]);
    }
  }
  return null;
}

// Returnerar { nu (m²/s), rho (kg/m³) } för givet medium
function getFluidProps(medium, T) {
  if (medium === 'Vatten') {
    return { nu: waterKinVis(T), rho: waterDensity(T) };
  }
  // medium t.ex. 'EG 30%', 'PG 40%'
  const match = medium.match(/^(EG|PG)\s+(\d+)%$/);
  if (!match) return null;
  const type = match[1];
  const conc = parseInt(match[2]);
  const colMap = {20:1, 30:2, 40:3, 50:4};
  const col = colMap[conc];
  if (!col) return null;
  const viscTable = type === 'EG' ? EG_VISC : PG_VISC;
  const densTable = type === 'EG' ? EG_DENS : PG_DENS;
  const nu = interpTable(viscTable, T, col);
  const rho = interpTable(densTable, T, col);
  if (!nu || !rho) return null;
  return { nu: nu * 1e-6, rho };
}

// Vattenegenskaper (polynom, 0–100 °C)
function waterDensity(T) {
  return 999.83 + 6.793e-2*T - 9.095e-3*T*T + 1.001e-4*T*T*T - 1.120e-6*T*T*T*T;
}
function waterKinVis(T) {
  // Vogel-ekv för dynamisk viskositet (Pa·s)
  const mu = 2.414e-5 * Math.pow(10, 247.8 / (T + 133.15));
  return mu / waterDensity(T);
}

// Luftegenskaper vid temperatur T (°C)
function airDensity(T) { return 353.05 / (273.15 + T); }
function airKinVis(T) {
  const Tk = T + 273.15;
  const mu = 1.458e-6 * Math.pow(Tk, 1.5) / (Tk + 110.4);
  return mu / airDensity(T);
}
