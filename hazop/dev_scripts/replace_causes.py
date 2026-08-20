"""Script to replace _COMP_STD_CAUSES with generic causes + frequencies."""
import os

with open('hazop.py', encoding='utf-8') as f:
    content = f.read()

new_dict_body = '''
    # ── Lågt flöde ────────────────────────────────────────────────────────────
    "Lågt flöde": {
        "Manuell ventil":     [("Ventil stängd / delvis stängd",       1e-3),
                               ("Ventil blockerad (igensättning)",      5e-4),
                               ("Blind platta / blindning kvarglömd",   1e-4)],
        "On-off ventil":      [("Ventil felar stängd (fail-closed)",    1e-2),
                               ("Ventil fastnar i stängt läge",         5e-3),
                               ("Manöversignal uteblir",                1e-2)],
        "Reglerventil":       [("Reglerventil felar stängd",            2e-2),
                               ("Ventil fastnar / stiction",            1e-2),
                               ("Felaktig styrsignal — lågt utflöde",   5e-3)],
        "Backventil":         [("Backventil fastnar stängd",            1e-2),
                               ("Backventil monterad baklänges",        1e-4)],
        "Pump":               [("Pump stopp",                           2e-2),
                               ("Reducerad pumpkapacitet",              1e-2),
                               ("Kavitation",                           5e-3),
                               ("Inlopp blockerat",                     5e-3)],
        "Kompressor / fläkt": [("Kompressor / fläkt stopp",            2e-2),
                               ("Reducerad kapacitet",                  1e-2),
                               ("Inloppsfilter igensatt",               5e-2)],
        "Filter / sil":       [("Filter / sil igensatt",               0.1),
                               ("Filterelement felaktigt monterat",     1e-3)],
        "Värmeväxlare / kylare / värmare": [
                               ("Rör igensatta — fouling",              5e-2),
                               ("Vakuumbrott / tömning",                1e-3)],
        "Tank / kärl / kolonn":[("Låg nivå i matningskärl",            5e-2),
                               ("Utlopp stängt / nivåstyrning",         1e-2)],
        "Rörledning / slang": [("Igensatt rörledning",                  5e-3),
                               ("Luftlås / hydrater / is",              1e-3)],
        "Instrument":         [("Flödesgivare felar — styrventil stänger", 0.1),
                               ("Börvärde felaktigt inställt",          1e-2)],
        "Styrsystem / PLC / DCS": [("Styrsignal felar — ventil stänger", 5e-3),
                               ("Kommunikationsavbrott",                1e-2)],
    },

    # ── Högt flöde ────────────────────────────────────────────────────────────
    "Högt flöde": {
        "Manuell ventil":     [("Ventil öppnad felaktigt",              1e-3),
                               ("Bypassventil öppnad",                  5e-4)],
        "On-off ventil":      [("Ventil felar öppen (fail-open)",       1e-2),
                               ("Ventil fastnar i öppet läge",          5e-3)],
        "Reglerventil":       [("Reglerventil felar öppen",             2e-2),
                               ("Felaktig styrsignal — högt utflöde",   5e-3)],
        "Pump":               [("Pumpkapacitet för hög",                5e-3),
                               ("Frekvensomformare — fel varvtal",      1e-2)],
        "Kompressor / fläkt": [("Kompressor — för hög kapacitet",       5e-3)],
        "Tank / kärl / kolonn":[("Övertryck driver högre flöde",        1e-2)],
        "Instrument":         [("Flödesgivare felar — styrventil öppnar", 0.1),
                               ("Börvärde felaktigt högt",              1e-2)],
        "Styrsystem / PLC / DCS": [("Styrsignal felar — ventil öppnar", 5e-3)],
    },

    # ── Högt tryck ────────────────────────────────────────────────────────────
    "Högt tryck": {
        "Manuell ventil":     [("Utloppsventil stängd",                 1e-3),
                               ("Ventil blockerad",                     5e-4)],
        "On-off ventil":      [("Utloppsventil felar stängd",           1e-2),
                               ("Ventil fastnar stängd på utlopp",      5e-3)],
        "Reglerventil":       [("Reglerventil på utlopp felar stängd",  2e-2),
                               ("Felaktig tryckreglering",              5e-3)],
        "Pump":               [("Pump deadhead — utlopp blockerat",     5e-3)],
        "Värmeväxlare / kylare / värmare": [
                               ("Kylningsbortfall",                     5e-3),
                               ("Termisk expansion utan ventilering",   1e-3)],
        "Tank / kärl / kolonn":[("Blockerat avluftningssystem",         5e-4)],
        "Säkerhetsventil / sprängbleck": [
                               ("Säkerhetsventil felar stängd",         1e-3),
                               ("Sprängbleck defekt",                   5e-4)],
        "Instrument":         [("Trycktransmitter felar — styrventil stänger", 0.1),
                               ("Börvärde tryckreglering felaktigt",    1e-2)],
        "Styrsystem / PLC / DCS": [("Tryckreglering felar",             5e-3)],
        "Rörledning / slang": [("Blockerad utloppsledning",             5e-4)],
        "Kompressor / fläkt": [("Kompressorsurge",                      1e-2)],
        "Backventil":         [("Backventil blockerar utflöde",         5e-3)],
        "Filter / sil":       [("Filter igensatt — tryckstegring uppströms", 0.1)],
    },

    # ── Lågt tryck ────────────────────────────────────────────────────────────
    "Lågt tryck": {
        "Manuell ventil":     [("Dräneringsventil öppnad",              5e-4),
                               ("Läckage via öppen ventil",             1e-3)],
        "On-off ventil":      [("Utloppsventil felar öppen",            1e-2),
                               ("Avblåsningsventil fastnar öppen",      5e-3)],
        "Reglerventil":       [("Reglerventil felar öppen",             2e-2)],
        "Pump":               [("Pump stopp — tryckfall",               2e-2)],
        "Rörledning / slang": [("Rörläckage / slangbrott",              5e-4),
                               ("Packningsläckage",                     1e-3)],
        "Fläns / koppling / packning": [
                               ("Packningsläckage",                     2e-3),
                               ("Flänsläckage",                         5e-4)],
        "Säkerhetsventil / sprängbleck": [
                               ("Säkerhetsventil öppnar för tidigt",    1e-3),
                               ("Sprängbleck utlöst",                   1e-4)],
        "Instrument":         [("Tryckmätare felar — styrventil öppnar", 0.1)],
        "Tank / kärl / kolonn":[("Kärl dränerat",                       5e-3)],
    },

    # ── Hög nivå ──────────────────────────────────────────────────────────────
    "Hög nivå": {
        "Manuell ventil":     [("Utloppsventil stängd",                 1e-3),
                               ("Inloppsventil öppnad utan utlopp",     5e-4)],
        "On-off ventil":      [("Utloppsventil felar stängd",           1e-2),
                               ("Inloppsventil felar öppen",            1e-2)],
        "Reglerventil":       [("Utloppsreglering felar stängd",        2e-2),
                               ("Inloppsreglering felar öppen",         1e-2)],
        "Pump":               [("Utloppspump stopp",                    2e-2)],
        "Instrument":         [("Nivågivare felar — reglering stänger utlopp", 0.1),
                               ("Börvärde nivå felaktigt",              1e-2)],
        "Tank / kärl / kolonn":[("Inflöde > utflöde",                  5e-3)],
        "Styrsystem / PLC / DCS": [("Nivåreglering felar",              5e-3)],
        "Backventil":         [("Backventil läcker — backflöde till kärl", 5e-3)],
    },

    # ── Låg nivå ──────────────────────────────────────────────────────────────
    "Låg nivå": {
        "Manuell ventil":     [("Inloppsventil stängd",                 1e-3),
                               ("Dräneringsventil öppnad",              5e-4)],
        "On-off ventil":      [("Inloppsventil felar stängd",           1e-2),
                               ("Utloppsventil felar öppen",            1e-2)],
        "Reglerventil":       [("Inloppsreglering felar stängd",        2e-2),
                               ("Utloppsreglering felar öppen",         1e-2)],
        "Pump":               [("Inloppspump stopp",                    2e-2),
                               ("Pumpläckage / tätningsfel",            5e-3)],
        "Rörledning / slang": [("Rörläckage",                           5e-4)],
        "Instrument":         [("Nivågivare felar — reglering öppnar utlopp", 0.1)],
        "Tank / kärl / kolonn":[("Läckage via botten / sida",           5e-4)],
    },

    # ── Hög temperatur ────────────────────────────────────────────────────────
    "Hög temperatur": {
        "Manuell ventil":     [("Kylmediumventil stängd",               5e-4),
                               ("Värmemediumventil öppnad",             5e-4)],
        "Reglerventil":       [("Kylventil felar stängd",               2e-2),
                               ("Värmeventil felar öppen",              1e-2)],
        "Värmeväxlare / kylare / värmare": [
                               ("Kylningsbortfall",                     5e-3),
                               ("Värmetillförsel okontrollerad",        1e-3)],
        "Instrument":         [("Temperaturgivare felar — kylning stängs", 0.1)],
        "Tank / kärl / kolonn":[("Exoterm reaktion",                    1e-4),
                               ("Extern värmetillförsel",               1e-4)],
        "Rörledning / slang": [("Isolationsfel / brandpåverkan",        5e-4)],
        "Styrsystem / PLC / DCS": [("Temperaturreglering felar",        5e-3)],
        "Pump":               [("Pumpfriktionsvärme",                   5e-3)],
        "Kompressor / fläkt": [("Kompressionsöverhettning",             1e-2)],
    },

    # ── Låg temperatur ────────────────────────────────────────────────────────
    "Låg temperatur": {
        "Manuell ventil":     [("Värmemediumventil stängd",             5e-4),
                               ("Kylmediumventil öppnad",               5e-4)],
        "Reglerventil":       [("Värmeventil felar stängd",             2e-2),
                               ("Kylventil felar öppen",                1e-2)],
        "Värmeväxlare / kylare / värmare": [
                               ("Värmebortfall",                        5e-3),
                               ("Överkylning",                          1e-3)],
        "Instrument":         [("Temperaturgivare felar — värmning stängs", 0.1)],
        "Rörledning / slang": [("Frysrisk — isolationsbortfall",        1e-3)],
        "Tank / kärl / kolonn":[("Endoterm reaktion / avdunstning",     1e-4)],
    },

    # ── Omvänt flöde ─────────────────────────────────────────────────────────
    "Omvänt flöde": {
        "Backventil":         [("Backventil defekt — läcker",           1e-2),
                               ("Backventil saknas",                    1e-4)],
        "Manuell ventil":     [("Ventil öppnas mot tryckkälla",         5e-4)],
        "Pump":               [("Pump stopp — backflöde via pump",      2e-2),
                               ("Pump roterar baklänges",               1e-3)],
        "Rörledning / slang": [("Felkopplad ledning",                   1e-4)],
        "Styrsystem / PLC / DCS": [("Ventilstyrning felar", 5e-3)],
    },

    # ── Missriktat flöde ──────────────────────────────────────────────────────
    "Missriktat flöde": {
        "Manuell ventil":     [("Fel ventil öppnad",                    1e-3),
                               ("Bypassventil öppnad",                  5e-4)],
        "On-off ventil":      [("Automatstyrd ventil öppnar fel väg",   1e-2)],
        "Reglerventil":       [("Styrventil öppnar alternativ väg",     5e-3)],
        "Rörledning / slang": [("Felkopplad ledning",                   1e-4)],
        "Styrsystem / PLC / DCS": [("Felaktig ventilstyrning",          5e-3)],
        "Instrument":         [("Flödesgivare i fel linje",             0.1)],
    },

    # ── Avvikande sammansättning ──────────────────────────────────────────────
    "Avvikande sammansättning": {
        "Manuell ventil":     [("Fel ventil öppnad — korsflöde",        1e-3)],
        "Reglerventil":       [("Dos- / blandningsventil i fel läge",   5e-3)],
        "Tank / kärl / kolonn":[("Kontamination i kärl",                5e-4),
                               ("Fel råmaterial / kemikalie",           1e-3)],
        "Rörledning / slang": [("Felkopplad ledning",                   1e-4)],
        "Instrument":         [("Analysgivare felar — doseringsstyrning", 0.1)],
        "Pump":               [("Felaktigt pumpmedium",                 5e-4)],
    },

    # ── Bortfall av hjälpsystem ───────────────────────────────────────────────
    "Bortfall av hjälpsystem": {
        "Elförsörjning":      [("Strömavbrott",                         0.1),
                               ("Säkring / skydd löser ut",             0.5)],
        "Tryckluft / instrumentluft": [
                               ("Lufttrycksfall",                       5e-2),
                               ("Luftkompressor stopp",                 0.1)],
        "Kylsystem / värmesystem": [
                               ("Kylvattenpump stopp",                  2e-2),
                               ("Kylvattentryck faller",                5e-2)],
        "Styrsystem / PLC / DCS": [("DCS / PLC haveri",                 1e-2),
                               ("Kommunikationsavbrott",                0.1)],
    },

    # ── Drift ─────────────────────────────────────────────────────────────────
    "Drift": {
        "Manuell ventil":     [("Felaktig manöver — fel ventil",        1e-2),
                               ("Ventil glömd i fel läge",              5e-3)],
        "Operatör / procedur / underhåll": [
                               ("Felaktig procedur / fel sekvens",      5e-2),
                               ("Procedur saknas eller otydlig",        None),
                               ("Kommunikationsfel",                    None)],
        "Instrument":         [("Felläsning av mätvärde",               5e-2)],
    },

    # ── Underhåll ─────────────────────────────────────────────────────────────
    "Underhåll": {
        "Manuell ventil":     [("Isolationsventil felaktigt ställd",    5e-3),
                               ("Ventil i fel läge efter arbete",       1e-3)],
        "Fläns / koppling / packning": [
                               ("Felaktig packning installerad",        1e-3),
                               ("Flansbultar ej åtdragna",              5e-4)],
        "Operatör / procedur / underhåll": [
                               ("Felaktig isolering (LOTO)",            5e-3),
                               ("Arbete på trycksatt system",           1e-3),
                               ("Fel komponent installerad",            5e-4)],
        "Rörledning / slang": [("Blind platta kvarglömd",               5e-4)],
        "Instrument":         [("Instrument ej återdriftsatt",          1e-2)],
    },

    # ── Start-up / Shut-down ──────────────────────────────────────────────────
    "Start-up / Shut-down": {
        "Manuell ventil":     [("Fel ventilsekvens",                    1e-2),
                               ("Ventil stängd vid pumpstart",          5e-3)],
        "Rörledning / slang": [("Kondensatbank — vätskeslag",           1e-3),
                               ("Luftlås vid start",                    5e-4)],
        "Pump":               [("Pump startas mot stängt utlopp",       5e-3),
                               ("Pump startas utan inloppstryck",       5e-3)],
        "Operatör / procedur / underhåll": [
                               ("Felaktig start-/stoppsekvens",         1e-2),
                               ("Procedur ej följd",                    5e-2)],
        "Tank / kärl / kolonn":[("Kärl ej förberett vid start",         1e-3)],
        "Reglerventil":       [("Reglerventil i manuellt läge vid start", 5e-3)],
        "Värmeväxlare / kylare / värmare": [
                               ("Termisk chock vid uppstart",           1e-3)],
    },
}'''

start_marker = '_COMP_STD_CAUSES = {'
end_marker = '}\n\n_STD_OBJECTS'
start_idx = content.index(start_marker) + len(start_marker)
end_idx = content.index(end_marker) + 1

new_content = content[:start_idx] + new_dict_body + content[end_idx:]
with open('hazop.py', 'w', encoding='utf-8') as f:
    f.write(new_content)
print('Done')
