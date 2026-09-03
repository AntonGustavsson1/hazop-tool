# K10 Gränsbelopppskalkylator

**Webbaserad gränsbelopppskalkylator för 3:12-reglerna**

En modern webbapplikation för att beräkna utdelningsutrymme enligt 3:12-reglerna för svenska aktiebolag.

## 🚀 Starta programmet

### Med HTTP-server (rekommenderat)
```bash
cd k10
python -m http.server 8000
```

Öppna sedan: **http://localhost:8000**

### Direkt från fil
Dubbelklicka på `index.html` eller öppna den i webläsaren.

## ✨ Funktioner

✅ **Förifyllda värden från PDF**
- Personnummer: 890508-4979
- Namn: Anton Gustavsson
- Bolag: Confolio AB
- År: 2025

✅ **Beräknar gränsbelopppet**
- Lönebaserat utrymme
- Omkostnadsbelopp
- Sparat gränsbelopppsutrymme från tidigare år

✅ **Skatteberäkning**
- 20% på utdelning inom gränsbelopppet
- 30% på utdelning över gränsbelopppet

✅ **Hantera flera bolag**
- Lägg till/ta bort bolag enkelt
- Beräkna totalt gränsbelopp för alla bolag

✅ **Exportera resultat**
- HTML-rapport
- JSON-export för vidare bearbetning

✅ **Lagra lokalt**
- Data sparas automatiskt i webbläsarens lagring
- Du kan komma tillbaka senare och dina värden finns kvar

## 📋 Använda programmet

1. **Fyll i personuppgifter** (redan förifyllda)
2. **Lägg till/redigera bolag**:
   - Bolagsnamn
   - Ägarandel (%)
   - Total lön från bolaget
   - Din personliga lön
   - Omkostnadsunderlag
   - Sparad gränsbelopppsutrymme
   - Planerad utdelning

3. **Klicka "Beräkna"** för att se resultaten
4. **Exportera** HTML eller JSON om du vill

## 🔍 Verifiering mot PDF (Confolio AB, 2025)

**Från PDF:en:**
- ✅ Gränsbelopp enligt huvudregeln: **384 826 SEK**
- ✅ Lönebaserat utrymme: **305 812 SEK**
- ✅ Omkostnadsbelopp: **5 480 SEK**
- ✅ Sparat utdelningsutrymme: **73 534 SEK**
- ✅ Planerad utdelning: **384 827 SEK**
- ✅ Din lön 2024: **611 625 SEK**

**Förväntade skatteresultat:**
```
Gränsbelopp:              384 826 SEK
Planerad utdelning:       384 827 SEK
Inom gränsbelopp (20%):   384 826 SEK
Över gränsbelopp (30%):   1 SEK
─────────────────────────────────
Skatt (20%):              76 965 SEK
Skatt (30%):              0 SEK
TOTAL SKATT:              76 965 SEK
NETTO UTDELNING:          307 862 SEK
```

## 🛠️ Teknik

- **HTML5** — Semantisk struktur
- **CSS3** — Modern design med gradient och responsiv layout
- **Vanilla JavaScript** — Beräkningar, interaktivitet, data-lagring
- **LocalStorage API** — Lagring av användardata i webbläsaren
- **Responsive Design** — Fungerar på desktop, tablet och mobil

## 📁 Filstruktur

```
k10/
├── index.html                    # Huvudwebbsida
├── k10.js                        # Beräkningslogik och interaktivitet
├── README.md                     # Denna fil
└── 890508-4979 Gustavsson, Anton Skatt2026.pdf  # Referens-PDF
```

## ⚠️ Viktiga noteringar

⚠️ **Denna beräkning är preliminär**
- Baseras på aktuella IBB-värden och skattesatser
- Måste verifieras mot Skatteverkets K10-formulär
- Konsultera en skatteexpert före rapportering till Skatteverket

✅ **Verifierad mot PDF**
- Confolio AB-exemplet matchar PDF:ens värden
- Programmet visar skillnaden för kontroll

## 📞 Support

Frågor om K10 och gränsbelopppet?
- **Skatteverket:** 0771-567 567
- **Webbplats:** https://www.skatteverket.se
- **Mina sidor:** https://www7.skatteverket.se/portal/

---

**Version:** 2.0 (Webbaserad HTML/CSS/JavaScript)  
**Senast uppdaterad:** 2026-07-03  
**Status:** ✅ Produktionsklar
