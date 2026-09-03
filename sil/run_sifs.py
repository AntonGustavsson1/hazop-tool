#!/usr/bin/env python3
"""
Beräkning av SIF-001 till SIF-020 för Hybrit-projektet
Exporterar resultat med avvikelser för Sensor, Logic solver, och Final element
"""

import json
from datetime import datetime
from pathlib import Path
from calc import (
    Architecture, ComponentParams, SubsystemParams, calc_sif, sil_from_pfd
)

# ── SIF-definitioner (baserat på typiska industriell-automatisering-värden) ──────
SIFS_CONFIG = {
    "SIF-001": {
        "name": "Low Nitrogen Pressure - Absorber (Hybrit Caloric)",
        "sil_req": 1,  # Changed from 2 to 1 (actual requirement from Hybrit)
        "sensor": {
            "name": "254PI330 - Endress+Hauser Cerabar S + PR Electronics 9106",
            "arch": "1oo1",
            "lambda_d": 3.27e-7,  # FROM HYBRIT: Endress+Hauser sensor
            "dc": 0.734,  # FROM HYBRIT: SFF = 73.4% = Diagnostic coverage proxy
            "beta": 0.02,
            "ti": 8760,  # 12 months
            "mttr": 24,  # CORRECTED from 8 to 24 hours (Hybrit data)
            "ptc": 0.99,  # FROM HYBRIT: 99% Proof Test Coverage
            "sff": 0.734,  # FROM HYBRIT: Safe Failure Fraction
            "comp_type": "B"
        },
        "logic": {
            "name": "Logic Solver - ABB AC800M High Integrity SIL3",
            "arch": "1oo1",
            "lambda_d": 1.46e-6,  # FROM HYBRIT: Total logic processor failure rate
            "dc": 1.0,  # FROM HYBRIT: 100% diagnostics via SIL3 architecture
            "beta": 0.02,
            "ti": 8760,
            "mttr": 24,  # CORRECTED from 8 to 24 hours
            "ptc": 0.99,  # FROM HYBRIT: 99% Proof Test Coverage
            "sff": 1.0,  # FROM HYBRIT: 100% Safe Failure Fraction
            "comp_type": "B"
        },
        "fe": {
            "name": "254SV559 + 254SV561 - Metso Ventiler (1oo2 arkitektur)",
            "arch": "1oo2",  # CRITICAL FIX: Changed from 1oo1 to 1oo2 (two valves in series)
            "lambda_d": 3.3e-6,  # FROM HYBRIT CALCULATION: Reverse-calculated from PFD_FE = 8.25E-05
            "dc": 0.0,  # Ventiler har ingen built-in diagnostik för tight shutoff
            "beta": 0.10,  # FROM HYBRIT: 10% CCF (Common Cause Failure) alpha-factor for shared environment
            "ti": 8760,
            "mttr": 24,  # CORRECTED from 8 to 24 hours
            "ptc": 0.85,  # CORRECTED from 1.0 to 0.85 (FROM HYBRIT: 85% Proof Test Coverage with PVST)
            "sff": 0.798,  # FROM HYBRIT: Safe Failure Fraction for 1oo2 architecture
            "comp_type": "A"
        }
    },
    "SIF-002": {
        "name": "Temperature Limit - Reactor",
        "sil_req": 2,
        "sensor": {"name": "TT-201", "arch": "1oo1", "lambda_d": 5.0e-7, "dc": 0.70, "beta": 0.02, "ti": 4380, "mttr": 4, "ptc": 0.95, "sff": 0.75, "comp_type": "B"},
        "logic": {"name": "Logic Solver", "arch": "1oo1", "lambda_d": 2.0e-8, "dc": 0.99, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.99, "comp_type": "B"},
        "fe": {"name": "XV-201 (Cooling Valve)", "arch": "1oo2", "lambda_d": 6.0e-6, "dc": 0.10, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.35, "comp_type": "A"},
    },
    "SIF-003": {
        "name": "Level High - Reaction Vessel",
        "sil_req": 2,
        "sensor": {"name": "LT-301", "arch": "1oo2", "lambda_d": 6.0e-7, "dc": 0.60, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.65, "comp_type": "B"},
        "logic": {"name": "PLC-Control", "arch": "1oo1", "lambda_d": 2.5e-8, "dc": 0.99, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.99, "comp_type": "B"},
        "fe": {"name": "XV-301 (Inlet Block)", "arch": "1oo1", "lambda_d": 5.0e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.15, "comp_type": "A"},
    },
    "SIF-004": {
        "name": "Oxygen Concentration Monitor",
        "sil_req": 1,
        "sensor": {"name": "AT-401", "arch": "1oo1", "lambda_d": 8.5e-7, "dc": 0.50, "beta": 0.02, "ti": 4380, "mttr": 6, "ptc": 0.90, "sff": 0.60, "comp_type": "B"},
        "logic": {"name": "Analyzer Module", "arch": "1oo1", "lambda_d": 3.0e-8, "dc": 0.95, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.98, "comp_type": "B"},
        "fe": {"name": "XV-401 (Vent)", "arch": "1oo1", "lambda_d": 3.0e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.05, "comp_type": "A"},
    },
    "SIF-005": {
        "name": "Flow Shutdown - Hazardous Feed",
        "sil_req": 2,
        "sensor": {"name": "FT-501", "arch": "1oo1", "lambda_d": 9.0e-7, "dc": 0.55, "beta": 0.02, "ti": 6570, "mttr": 8, "ptc": 0.95, "sff": 0.68, "comp_type": "B"},
        "logic": {"name": "PLC", "arch": "2oo2", "lambda_d": 2.0e-8, "dc": 0.98, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.99, "comp_type": "B"},
        "fe": {"name": "XV-501 (Feed Block)", "arch": "1oo2", "lambda_d": 7.0e-6, "dc": 0.05, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.25, "comp_type": "A"},
    },
    "SIF-006": {
        "name": "Pressure High - Reactor Outlet",
        "sil_req": 2,
        "sensor": {"name": "PT-601", "arch": "1oo1", "lambda_d": 7.0e-7, "dc": 0.68, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.72, "comp_type": "B"},
        "logic": {"name": "Safety PLC", "arch": "1oo1", "lambda_d": 2.0e-8, "dc": 0.99, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.99, "comp_type": "B"},
        "fe": {"name": "XV-601 (Depressure)", "arch": "1oo1", "lambda_d": 6.5e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.12, "comp_type": "A"},
    },
    "SIF-007": {
        "name": "Temperature Low - Exothermic Control",
        "sil_req": 1,
        "sensor": {"name": "TT-701", "arch": "1oo1", "lambda_d": 5.5e-7, "dc": 0.65, "beta": 0.02, "ti": 4380, "mttr": 6, "ptc": 0.95, "sff": 0.70, "comp_type": "B"},
        "logic": {"name": "Local Logic", "arch": "1oo1", "lambda_d": 3.0e-8, "dc": 0.97, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.98, "comp_type": "B"},
        "fe": {"name": "XV-701 (Cool. Feed)", "arch": "1oo1", "lambda_d": 4.5e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.08, "comp_type": "A"},
    },
    "SIF-008": {
        "name": "Level Low - Reaction Pot",
        "sil_req": 1,
        "sensor": {"name": "LT-801", "arch": "1oo1", "lambda_d": 6.5e-7, "dc": 0.60, "beta": 0.02, "ti": 4380, "mttr": 6, "ptc": 0.90, "sff": 0.63, "comp_type": "B"},
        "logic": {"name": "Hardwired", "arch": "1oo1", "lambda_d": 5.0e-8, "dc": 0.90, "beta": 0.03, "ti": 2190, "mttr": 4, "ptc": 0.85, "sff": 0.85, "comp_type": "A"},
        "fe": {"name": "XV-801 (Inlet)", "arch": "1oo1", "lambda_d": 3.5e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.06, "comp_type": "A"},
    },
    "SIF-009": {
        "name": "Stirrer Speed High - Runaway Prevention",
        "sil_req": 2,
        "sensor": {"name": "ST-901", "arch": "1oo1", "lambda_d": 8.0e-7, "dc": 0.55, "beta": 0.02, "ti": 6570, "mttr": 8, "ptc": 0.95, "sff": 0.65, "comp_type": "B"},
        "logic": {"name": "VFD Controller", "arch": "1oo2", "lambda_d": 1.5e-8, "dc": 0.98, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.98, "comp_type": "B"},
        "fe": {"name": "XV-901 (Motor Stop)", "arch": "1oo1", "lambda_d": 4.0e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.10, "comp_type": "A"},
    },
    "SIF-010": {
        "name": "Emergency Shutdown - Global",
        "sil_req": 3,
        "sensor": {"name": "Button/Sensors", "arch": "2oo3", "lambda_d": 5.0e-7, "dc": 0.90, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.85, "comp_type": "B"},
        "logic": {"name": "SIL3 Logic", "arch": "2oo3", "lambda_d": 1.0e-8, "dc": 0.99, "beta": 0.01, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.995, "comp_type": "B"},
        "fe": {"name": "Main Block Valve", "arch": "2oo2", "lambda_d": 5.0e-6, "dc": 0.05, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.30, "comp_type": "A"},
    },
    "SIF-011": {
        "name": "Cooling Water Failure",
        "sil_req": 1,
        "sensor": {"name": "PT-1101", "arch": "1oo1", "lambda_d": 7.0e-7, "dc": 0.60, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 0.95, "sff": 0.68, "comp_type": "B"},
        "logic": {"name": "Timer Logic", "arch": "1oo1", "lambda_d": 4.0e-8, "dc": 0.95, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.97, "comp_type": "B"},
        "fe": {"name": "XV-1101 (By-pass)", "arch": "1oo1", "lambda_d": 3.0e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.05, "comp_type": "A"},
    },
    "SIF-012": {
        "name": "Power Loss Detection",
        "sil_req": 1,
        "sensor": {"name": "UPS/Monitor", "arch": "1oo1", "lambda_d": 3.0e-7, "dc": 0.80, "beta": 0.02, "ti": 8760, "mttr": 4, "ptc": 1.0, "sff": 0.88, "comp_type": "B"},
        "logic": {"name": "PLC", "arch": "1oo1", "lambda_d": 2.0e-8, "dc": 0.99, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.99, "comp_type": "B"},
        "fe": {"name": "Solenoid Dump", "arch": "1oo1", "lambda_d": 2.0e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.03, "comp_type": "A"},
    },
    "SIF-013": {
        "name": "Catalyst Bed Temperature Runaway",
        "sil_req": 2,
        "sensor": {"name": "TT-1301", "arch": "1oo2", "lambda_d": 6.0e-7, "dc": 0.75, "beta": 0.02, "ti": 4380, "mttr": 6, "ptc": 0.98, "sff": 0.78, "comp_type": "B"},
        "logic": {"name": "Safety Module", "arch": "1oo1", "lambda_d": 2.5e-8, "dc": 0.99, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.99, "comp_type": "B"},
        "fe": {"name": "XV-1301 (Quench)", "arch": "1oo2", "lambda_d": 5.5e-6, "dc": 0.08, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.28, "comp_type": "A"},
    },
    "SIF-014": {
        "name": "Gas Detector - Toxic Leak",
        "sil_req": 1,
        "sensor": {"name": "GD-1401", "arch": "1oo1", "lambda_d": 9.5e-7, "dc": 0.45, "beta": 0.02, "ti": 1095, "mttr": 2, "ptc": 0.80, "sff": 0.55, "comp_type": "B"},
        "logic": {"name": "Alarm PLC", "arch": "1oo1", "lambda_d": 3.0e-8, "dc": 0.98, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.98, "comp_type": "B"},
        "fe": {"name": "XV-1401 (Vent Damper)", "arch": "1oo1", "lambda_d": 4.0e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.07, "comp_type": "A"},
    },
    "SIF-015": {
        "name": "Agitator Vibration High",
        "sil_req": 1,
        "sensor": {"name": "VT-1501", "arch": "1oo1", "lambda_d": 7.5e-7, "dc": 0.50, "beta": 0.02, "ti": 4380, "mttr": 6, "ptc": 0.90, "sff": 0.62, "comp_type": "B"},
        "logic": {"name": "Monitoring", "arch": "1oo1", "lambda_d": 2.5e-8, "dc": 0.97, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.98, "comp_type": "B"},
        "fe": {"name": "XV-1501 (Motor Off)", "arch": "1oo1", "lambda_d": 3.5e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.06, "comp_type": "A"},
    },
    "SIF-016": {
        "name": "Batch Cycle Timeout Watchdog",
        "sil_req": 1,
        "sensor": {"name": "Timer/PLC", "arch": "1oo1", "lambda_d": 2.5e-7, "dc": 0.85, "beta": 0.02, "ti": 2190, "mttr": 2, "ptc": 0.95, "sff": 0.87, "comp_type": "B"},
        "logic": {"name": "Watchdog", "arch": "1oo1", "lambda_d": 1.5e-8, "dc": 0.99, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.99, "comp_type": "B"},
        "fe": {"name": "XV-1601 (Safe State)", "arch": "1oo1", "lambda_d": 2.5e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.04, "comp_type": "A"},
    },
    "SIF-017": {
        "name": "Hydrogen Generation Rate High",
        "sil_req": 2,
        "sensor": {"name": "FT-1701", "arch": "1oo1", "lambda_d": 8.5e-7, "dc": 0.60, "beta": 0.02, "ti": 6570, "mttr": 8, "ptc": 0.95, "sff": 0.70, "comp_type": "B"},
        "logic": {"name": "PLC Monitor", "arch": "2oo2", "lambda_d": 2.0e-8, "dc": 0.99, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.99, "comp_type": "B"},
        "fe": {"name": "XV-1701 (Purge)", "arch": "1oo2", "lambda_d": 6.0e-6, "dc": 0.05, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.24, "comp_type": "A"},
    },
    "SIF-018": {
        "name": "Process Integrity Pressure Loss",
        "sil_req": 1,
        "sensor": {"name": "PT-1801", "arch": "1oo1", "lambda_d": 6.5e-7, "dc": 0.65, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 0.95, "sff": 0.72, "comp_type": "B"},
        "logic": {"name": "Timer", "arch": "1oo1", "lambda_d": 3.0e-8, "dc": 0.96, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.97, "comp_type": "B"},
        "fe": {"name": "XV-1801 (Depressure)", "arch": "1oo1", "lambda_d": 3.5e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.06, "comp_type": "A"},
    },
    "SIF-019": {
        "name": "Pump Discharge Backpressure High",
        "sil_req": 1,
        "sensor": {"name": "PT-1901", "arch": "1oo1", "lambda_d": 7.0e-7, "dc": 0.60, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 0.95, "sff": 0.68, "comp_type": "B"},
        "logic": {"name": "PLC", "arch": "1oo1", "lambda_d": 2.0e-8, "dc": 0.99, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.99, "comp_type": "B"},
        "fe": {"name": "XV-1901 (Relief)", "arch": "1oo1", "lambda_d": 4.5e-6, "dc": 0.0, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.08, "comp_type": "A"},
    },
    "SIF-020": {
        "name": "Reactor Isolation on Shutdown",
        "sil_req": 2,
        "sensor": {"name": "PT-2001/TT-2001", "arch": "1oo1", "lambda_d": 6.5e-7, "dc": 0.70, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.75, "comp_type": "B"},
        "logic": {"name": "Shutdown Logic", "arch": "1oo1", "lambda_d": 2.5e-8, "dc": 0.99, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.99, "comp_type": "B"},
        "fe": {"name": "XV-2001/2 (Block)", "arch": "2oo2", "lambda_d": 5.0e-6, "dc": 0.05, "beta": 0.02, "ti": 8760, "mttr": 8, "ptc": 1.0, "sff": 0.28, "comp_type": "A"},
    },
}

def make_subsystem(name, config):
    """Skapar SubsystemParams från config-dict."""
    ct = config.get("comp_type", "A")[0]
    comp = ComponentParams(
        name=config.get("name", name),
        lambda_d=float(config.get("lambda_d", 1e-6)),
        dc=float(config.get("dc", 0.0)),
        beta=float(config.get("beta", 0.02)),
        ti=float(config.get("ti", 8760)),
        mttr=float(config.get("mttr", 8)),
        ptc=float(config.get("ptc", 1.0)),
        sff=float(config.get("sff", 0.0)),
        comp_type=ct,
    )
    return SubsystemParams(
        name=config.get("name", name),
        architecture=Architecture(config.get("arch", "1oo1")),
        component=comp,
    )

def run_all_sifs():
    """Kör alla SIF:ar och returnerar resultat."""
    results = []

    for sif_id, config in SIFS_CONFIG.items():
        try:
            sensor_sub = make_subsystem("Sensor", config["sensor"])
            logic_sub = make_subsystem("Logic", config["logic"])
            fe_sub = make_subsystem("Final Element", config["fe"])

            result = calc_sif(sif_id, sensor_sub, logic_sub, fe_sub, config.get("sil_req", 2))

            results.append({
                "id": sif_id,
                "name": config.get("name", ""),
                "sil_req": config.get("sil_req", 2),
                "result": result,
                "sensor_config": config["sensor"],
                "logic_config": config["logic"],
                "fe_config": config["fe"],
            })
        except Exception as e:
            print(f"ERROR i {sif_id}: {e}")

    return results

def export_html(results):
    """Exporterar resultat som HTML-rapport."""
    html = """
    <!DOCTYPE html>
    <html lang="sv">
    <head>
        <meta charset="utf-8">
        <title>SIL Beräkningar SIF-001 till SIF-020</title>
        <style>
            body {
                font-family: 'Segoe UI', Arial, sans-serif;
                margin: 30px;
                color: #222;
                background-color: #f5f5f5;
            }
            h1 {
                color: #1a5276;
                border-bottom: 3px solid #1a5276;
                padding-bottom: 10px;
            }
            .sif-block {
                background: white;
                border-left: 4px solid #1a5276;
                margin-bottom: 20px;
                padding: 15px;
                border-radius: 4px;
            }
            .sif-header {
                font-weight: bold;
                font-size: 16px;
                color: #1a5276;
                margin-bottom: 8px;
            }
            table {
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
                font-size: 13px;
            }
            th {
                background: #1a5276;
                color: white;
                padding: 8px;
                text-align: left;
                border: 1px solid #ddd;
            }
            td {
                padding: 7px 8px;
                border: 1px solid #ddd;
            }
            tr:nth-child(even) {
                background: #f9f9f9;
            }
            .pass {
                background: #d4edda;
                color: #1e8449;
                font-weight: bold;
            }
            .fail {
                background: #f8d7da;
                color: #c0392b;
                font-weight: bold;
            }
            .sil1 { color: #d35400; }
            .sil2 { color: #2e86c1; }
            .sil3 { color: #1e8449; }
            .sil4 { color: #1a5276; font-weight: bold; }
            .param-row {
                font-size: 12px;
            }
            .param-label {
                font-weight: bold;
                width: 80px;
                color: #555;
            }
            .deviation {
                color: #e74c3c;
                font-weight: bold;
            }
        </style>
    </head>
    <body>
        <h1>SIL Beräkningar - SIF-001 till SIF-020 (Hybrit-projektet)</h1>
        <p><strong>Datum:</strong> """ + datetime.now().strftime("%Y-%m-%d %H:%M") + """</p>
        <p><strong>Standard:</strong> IEC 61511 / IEC 61508</p>
    """

    for r in results:
        sif = r["result"]
        status_class = "pass" if sif.passed else "fail"
        status_text = "✓ GODKÄND" if sif.passed else "✗ EJ GODKÄND"
        sil_class = f"sil{sif.sil_achieved}"

        # Beräkna avvikelse (deviation) som procent mellan delsystemens PFD
        sensor_pfd = sif.sensor.pfd
        logic_pfd = sif.logic.pfd
        fe_pfd = sif.final_element.pfd

        # Avvikelse = (Max - Min) / Max * 100
        max_pfd = max(sensor_pfd, logic_pfd, fe_pfd)
        min_pfd = min(sensor_pfd, logic_pfd, fe_pfd)
        deviation = ((max_pfd - min_pfd) / max_pfd * 100) if max_pfd > 0 else 0

        html += f"""
        <div class="sif-block">
            <div class="sif-header">{r["id"]} — {r["name"]}</div>
            <table>
                <tr>
                    <td><strong>Delsystem</strong></td>
                    <td><strong>Arkitektur</strong></td>
                    <td><strong>λ_D [1/h]</strong></td>
                    <td><strong>DC</strong></td>
                    <td><strong>PFD_avg</strong></td>
                    <td><strong>PFH [1/h]</strong></td>
                    <td><strong>SIL (från PFD)</strong></td>
                </tr>
                <tr class="param-row">
                    <td>Sensor</td>
                    <td>{r["sensor_config"]["arch"]}</td>
                    <td>{r["sensor_config"]["lambda_d"]:.2e}</td>
                    <td>{r["sensor_config"]["dc"]:.2f}</td>
                    <td><strong>{sif.sensor.pfd:.3e}</strong></td>
                    <td>{sif.sensor.pfh:.3e}</td>
                    <td class="{sil_class}">SIL {sif.sensor.sil_pfd if sif.sensor.sil_pfd > 0 else '<1'}</td>
                </tr>
                <tr class="param-row">
                    <td>Logic solver</td>
                    <td>{r["logic_config"]["arch"]}</td>
                    <td>{r["logic_config"]["lambda_d"]:.2e}</td>
                    <td>{r["logic_config"]["dc"]:.2f}</td>
                    <td><strong>{sif.logic.pfd:.3e}</strong></td>
                    <td>{sif.logic.pfh:.3e}</td>
                    <td class="{sil_class}">SIL {sif.logic.sil_pfd if sif.logic.sil_pfd > 0 else '<1'}</td>
                </tr>
                <tr class="param-row">
                    <td>Final element</td>
                    <td>{r["fe_config"]["arch"]}</td>
                    <td>{r["fe_config"]["lambda_d"]:.2e}</td>
                    <td>{r["fe_config"]["dc"]:.2f}</td>
                    <td><strong>{sif.final_element.pfd:.3e}</strong></td>
                    <td>{sif.final_element.pfh:.3e}</td>
                    <td class="{sil_class}">SIL {sif.final_element.sil_pfd if sif.final_element.sil_pfd > 0 else '<1'}</td>
                </tr>
            </table>
            <table style="margin-top: 8px;">
                <tr style="background: #ecf0f1;">
                    <td style="width: 150px;"><strong>TOTALT SIF</strong></td>
                    <td><strong>PFD = {sif.pfd_total:.3e}</strong></td>
                    <td><strong>PFH = {sif.pfh_total:.3e}</strong></td>
                    <td><strong>MTTFS = {sif.mttfs/8760:.1f} år</strong></td>
                </tr>
                <tr style="background: #ecf0f1;">
                    <td><strong>SIL-resultat</strong></td>
                    <td colspan="2"><span class="{sil_class}"><strong>SIL {sif.sil_achieved}</strong></span> (krav: SIL {r["sil_req"]})</td>
                    <td class="{status_class}">{status_text}</td>
                </tr>
                <tr style="background: #f0f0f0;">
                    <td><strong>Avvikelse</strong></td>
                    <td colspan="3"><span class="deviation">{deviation:.1f}%</span> (mellan sensor, logic, FE)</td>
                </tr>
            </table>
        </div>
        """

    html += """
    </body>
    </html>
    """
    return html

def export_json(results):
    """Exporterar resultat som JSON."""
    data = {
        "timestamp": datetime.now().isoformat(),
        "sifs": []
    }

    for r in results:
        sif = r["result"]

        # Beräkna avvikelse
        max_pfd = max(sif.sensor.pfd, sif.logic.pfd, sif.final_element.pfd)
        min_pfd = min(sif.sensor.pfd, sif.logic.pfd, sif.final_element.pfd)
        deviation = ((max_pfd - min_pfd) / max_pfd * 100) if max_pfd > 0 else 0

        data["sifs"].append({
            "id": r["id"],
            "name": r["name"],
            "sil_required": r["sil_req"],
            "sil_achieved": sif.sil_achieved,
            "passed": sif.passed,
            "pfd_total": float(sif.pfd_total),
            "pfh_total": float(sif.pfh_total),
            "str_total": float(sif.str_total),
            "mttfs_years": float(sif.mttfs / 8760),
            "deviation_percent": float(deviation),
            "components": {
                "sensor": {
                    "pfd": float(sif.sensor.pfd),
                    "pfh": float(sif.sensor.pfh),
                    "sil": sif.sensor.sil_pfd,
                    "architecture": sif.sensor.architecture,
                },
                "logic": {
                    "pfd": float(sif.logic.pfd),
                    "pfh": float(sif.logic.pfh),
                    "sil": sif.logic.sil_pfd,
                    "architecture": sif.logic.architecture,
                },
                "final_element": {
                    "pfd": float(sif.final_element.pfd),
                    "pfh": float(sif.final_element.pfh),
                    "sil": sif.final_element.sil_pfd,
                    "architecture": sif.final_element.architecture,
                }
            }
        })

    return json.dumps(data, indent=2)

if __name__ == "__main__":
    print("Kör SIL-beräkningar för SIF-001 till SIF-020...")
    results = run_all_sifs()

    print(f"Beräknade {len(results)} SIF:ar")

    # Exportera HTML
    html_content = export_html(results)
    html_file = Path(__file__).parent / "sil_results.html"
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"HTML-rapport sparad: {html_file}")

    # Exportera JSON
    json_content = export_json(results)
    json_file = Path(__file__).parent / "sil_results.json"
    with open(json_file, "w", encoding="utf-8") as f:
        f.write(json_content)
    print(f"JSON-export sparad: {json_file}")

    # Skriv ut sammanfattning till konsol
    print("\n" + "="*80)
    print("SAMMANFATTNING")
    print("="*80)
    passed = sum(1 for r in results if r["result"].passed)
    print(f"Godkända: {passed}/{len(results)}")
    for r in results:
        sif = r["result"]
        status = "✓" if sif.passed else "✗"
        print(f"{status} {r['id']:8s} {r['name']:40s} SIL {sif.sil_achieved} (krav {r['sil_req']})")
