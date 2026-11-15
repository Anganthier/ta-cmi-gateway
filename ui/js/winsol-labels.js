// WinSol-Kanal-Labels (S4-3) -- kuratiert aus Setup.xml <Bezeichnungen>.
//
// Quelle (read-only):
//   /home/user/Documents/Technische-Alternative/Winsol/Infosol/example-plant/Setup.xml
//
// Konvention (1:1 mit dem Wide-Store /api/v1/history):
//   * Kanal-Nummer = Setup.xml-Nummer: analog a1..a16 -> Kanal 1..16,
//     digital d1..d13 -> Kanal 1..13.
//   * dev0 digital: der Logger zeichnet d1 nicht auf -> Wide-Store beginnt
//     bei Kanal 2 (d2..d9). Die Abbildung wurde 100% gegen die WinSol-CSV-
//     Rosetta-Steine validiert (2026-10-27, S2-Parquet-Pivot).
//   * Platzhalter-Labels ("  -----") wurden weggelassen (ungenutzte Kanäle).
//
// Geräte:
//   dev0 = Heizung (UVR1611)
//   dev1 = Keller/EG/DG Raumtemperaturen RAS (UVR1611)
//   dev2 = Solar (UVR610 HzRl)

const WINSOL_DEVICES = [
  { id: "winsol:0", name: "Heizung (UVR1611)", short: "Heizung" },
  { id: "winsol:1", name: "RAS-Räume Keller/EG/DG (UVR1611)", short: "RAS-Räume" },
  { id: "winsol:2", name: "Solar (UVR610 HzRl)", short: "Solar" },
];

const WINSOL_LABELS = {
  "winsol:0": {
    analog: {
      1: "Speicher Oben",
      2: "Speicher Mitte",
      3: "Speicher Unten",
      4: "Warmwasser",
      5: "Oelkessel Vorlauf",
      6: "Heizkreis Vorlauf",
      7: "Heizkreis Ruecklauf",
      8: "Temperatur Aussen",
      9: "Holzkessel Vorlauf",
      10: "Abgastemperatur Holz",
      15: "Test",
      16: "Stroemungsschalter",
    },
    digital: {
      // d1 ("Pumpe Holzofen") wird vom Logger nicht aufgezeichnet.
      2: "Pumpe Warmwasser zum Puffer",
      3: "Mischkreis Auf",
      4: "Mischkreis Zu",
      5: "Anfoerdung Brenner",
      6: "Pumpe Warmwasser Zirkulation",
      7: "Zirkulationspumpe",
      8: "Pumpe Heizkreis",
      9: "Ventil Heizungsruecklauf",
    },
  },
  "winsol:1": {
    analog: {
      1: "Offset Keller",
      2: "Temp Keller",
      3: "Analog 3",
      4: "Offset Erdgeschoss",
      5: "Temp Erdgeschoss",
      6: "Analog 6",
      7: "Offset Dachgeschoss",
      8: "Temp Dachgeschoss",
      9: "Analog 9",
      10: "Analog 10",
      11: "Keller Minuten",
      12: "EG Minuten",
      13: "DG Minuten",
      14: "Analog 14",
      15: "Analog 15",
      16: "Analog 16",
    },
    digital: {
      1: "Digital 1",
      2: "Digital 2",
      3: "Digital 3",
      4: "Digital 4",
      5: "Digital 5",
      6: "Digital 6",
      7: "Digital 7",
      8: "Digital 8",
      9: "Digital 9",
      10: "Digital 10",
      11: "Digital 11",
      12: "Digital 12",
      13: "Digital 13",
    },
  },
  "winsol:2": {
    analog: {
      1: "T.Speicher oben 1",
      2: "T.Speicher mitte",
      3: "T.Speicher unten",
      4: "Keller RAS 1",
      5: "Keller Temp 1",
      6: "Erdgeschoss RAS 1",
      7: "Erdgeschoss Temp 1",
      8: "Dachgeschoss RAS 1",
      9: "Dachgeschoss Temp 1",
      10: "Keller HzRkl Ventil 2 Ergebnis",
      11: "EG HzRkl Ventil 2 Ergebnis",
      12: "OG HzRkl Ventil 2 Ergebnis",
      13: "Keller HzRkl Ventil 1",
      14: "OG HzRkl Ventil 1",
      15: "EG HzRkl Ventil 1",
      16: "T. Kollektor",
    },
    digital: {
      1: "Solarpumpe",
      7: "Ventil-HzRL 1",
      8: "Ventil-HzRL 2",
      9: "Ventil-HzRL 3",
      10: "Solarpumpe 1",
    },
  },
};

export { WINSOL_DEVICES, WINSOL_LABELS };
