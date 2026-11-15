"""10-s → 1-h Resampling (DuckDB, Europe/Berlin-Kalenderstunden).

Semantik (kanonisch, s. SCHEMA.md):
- ``ts`` ist STRING 'YYYY-MM-DD HH:MM:SS' (Wallclock Europe/Berlin, da der Logger
  lokale Zeit schreibt; DST-Fallback-Tag 2024-03-31 02:xx ist eine real kürzere
  Stunde = 120 statt 360 10-s-Rows, kein duplizierter Bucket).
- Kalenderstunde = Trunk der Wallclock ('YYYY-MM-DD HH:00'), identisch mit der
  Berliner Kalenderstunde (Logger-Wallclock IST Berliner Zeit).
- Temperaturen (analog): **mean**; ausgewählte analoge Kanäle zusätzlich
  **letzter Nicht-NULL-Wert** (``{feat}_last``, ``config.LAST_VALUE_FEATURES``);
  RAS-Offset-Δ (perc − ist): **median**;
  Digital: **An-Quotient** (mean) + **letzter Zustand** (last);
  RAS-Datenalter a11–a13 (``ras_age_*``): **max** (Sägezahn, Rohwert-Einheit);
  ``heat_min_*`` (DEPRECATED, keine Heiz-Minuten): Summe der positiven Deltas
  von a11–a13 innerhalb der Stunde (unveränderte m1.0-Berechnung, nur für
  v001-Kompatibilität).
- RAS-Maske (m1.1): alle RAS-Roh-Kanäle (``config.RAS_RAW_COLUMNS``) mit
  Roh-ts < ``config.RAS_INSTALL_TS`` sind bereits in der ``src``-View NULL
  (RAS-Sensoren erst 2021-10 eingebaut, Werte davor Müll).
- TA-Sentinel-Maske (m1.2): jeder analoge Roh-Wert mit ``v >= config.SENTINEL_MAX``
  oder ``v <= config.SENTINEL_MIN`` (TA-Fehlerwerte 3276.7 / -999.9) ist bereits in
  der ``src``-View NULL (Zeile bleibt erhalten → ``n_rows`` unverändert).
- Rücklaufminderer (m1.2): dev2 a10–a12 (binär 0/10) → ``hzrkl_{z}_on`` (AVG von
  Rohwert/10) + ``hzrkl_{z}_last``; dev2 a13/a15/a14 → ``hzrkl_{kell,eg,dg}_pos``
  (mean) + ``_pos_last``.
- ``gap`` = Kalenderstunde ohne 10-s-Rows → Flag 1, Werte NULL (kein Ffill > 30 min,
  kein Interpolieren; CMI-Log-Ausfall bleibt Lücke).

Quellen (read-only):
- ``data/winsol_pq`` (2020-08-30 → 2026-10-27 07:25:54)
- ``data/cmi_log_pq`` (2026-10-27 07:26:24 → live)
Schema identisch (geprüft, s. REPORT.md) → kein Column-Mapping nötig; Übergang =
``source``-Flag ('winsol_pq' / 'cmi_log_pq'), kein Overlap (geprüft).
"""

from __future__ import annotations

import duckdb

from . import config

# Roh-Spalten, die je Quelle vorhanden sein MÜSSEN (winsol + cmi, geprüfte Schnittmenge).
SOURCE_COLUMNS: list[str] = [
    "ts",
    "dev0_analog_01",
    "dev0_analog_02",
    "dev0_analog_03",
    "dev0_analog_04",
    "dev0_analog_05",
    "dev0_analog_06",
    "dev0_analog_07",
    "dev0_analog_08",
    "dev0_analog_09",
    "dev0_analog_10",
    "dev0_analog_15",
    "dev0_analog_16",
    "dev0_digital_02",
    "dev0_digital_03",
    "dev0_digital_04",
    "dev0_digital_05",
    "dev0_digital_06",
    "dev0_digital_07",
    "dev0_digital_08",
    "dev0_digital_09",
    "dev1_analog_01",
    "dev1_analog_02",
    "dev1_analog_04",
    "dev1_analog_05",
    "dev1_analog_07",
    "dev1_analog_08",
    "dev1_analog_11",
    "dev1_analog_12",
    "dev1_analog_13",
    "dev2_analog_01",
    "dev2_analog_02",
    "dev2_analog_03",
    "dev2_analog_04",
    "dev2_analog_05",
    "dev2_analog_06",
    "dev2_analog_07",
    "dev2_analog_08",
    "dev2_analog_09",
    # m1.2: Rücklaufminderer je Etage (a10–a12 Ventil 2 Ergebnis, a13–a15 Ventil 1)
    "dev2_analog_10",
    "dev2_analog_11",
    "dev2_analog_12",
    "dev2_analog_13",
    "dev2_analog_14",
    "dev2_analog_15",
    "dev2_analog_16",
    "dev2_digital_01",
]
# Analoge Roh-Spalten (Sentinel-Maske, m1.2).
ANALOG_SOURCE_COLUMNS: list[str] = [c for c in SOURCE_COLUMNS if "_analog_" in c]


def check_source_schema(con: duckdb.DuckDBPyConnection, path_glob: str, name: str) -> dict:
    """Prüft das Schema einer Quelle (read-only) + liefert min/max ts + Row-Count.

    Returns ``{'name', 'n_cols', 'missing', 'n_rows', 'first', 'last'}``.
    """
    cols = {
        r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path_glob}')").fetchall()
    }
    missing = [c for c in SOURCE_COLUMNS if c not in cols]
    n, first, last = con.execute(
        f"SELECT count(*), min(cast(ts AS timestamp)), max(cast(ts AS timestamp)) "
        f"FROM read_parquet('{path_glob}')"
    ).fetchone()
    return {
        "name": name,
        "n_cols": len(cols),
        "missing": missing,
        "n_rows": int(n),
        "first": str(first),
        "last": str(last),
    }


def _analog_agg_parts() -> list[str]:
    """AVG-Teile für alle (benannten) analogen Feature-Kanäle.

    Exkl. dev1 a11–a13 (RAS-Datenalter-Sägezahn): deren Features sind das
    Stunden-MAX (``ras_age_*``, siehe ``_ras_age_agg_parts``) und die DEPRECATED
    Summe der positiven Deltas (``heat_min_*``, siehe ``_heatmin_agg_parts``);
    ein AVG des Sägezahns wäre unsinnig und erzeugte einen Alias-Konflikt.
    """
    age_ch = {11, 12, 13}
    parts: list[str] = []
    for dev in (0, 1, 2):
        mapping = {
            0: config.DEV0_ANALOG,
            1: config.DEV1_ANALOG,
            2: config.DEV2_ANALOG,
        }[dev]
        for ch, feat in sorted(mapping.items()):
            if dev == 1 and ch in age_ch:
                continue  # Datenalter: MAX (+ DEPRECATED heat_min-Sum), kein AVG
            parts.append(f"AVG({config.dev_col(dev, 'analog', ch)}) AS {feat}")
    return parts


def _digital_agg_parts() -> list[str]:
    """An-Quotient (AVG) + letzter Zustand (LAST) je relevanter Digital-Kanal."""
    parts: list[str] = []
    for dev, mapping in ((0, config.DEV0_DIGITAL), (2, config.DEV2_DIGITAL)):
        for ch, feat in sorted(mapping.items()):
            col = config.dev_col(dev, "digital", ch)
            parts.append(f"AVG({col}) AS {feat}_on")
            parts.append(f"LAST({col} ORDER BY ts) AS {feat}_last")
    return parts


def _last_value_agg_parts() -> list[str]:
    """Letzter Nicht-NULL-10-s-Wert der Stunde je analogem Feature (``{feat}_last``).

    Reines Aggregat (kein Window) → läuft in ``hourly_agg`` neben den übrigen
    Aggregaten (DuckDB-2.0.0-Window+Aggregat-Bug betrifft nur Windows, s.
    ``build_hourly_sql``; Unit-Test sichert die Kombination ab). FILTER statt
    blankem LAST: eine NULL-Zeile am Stundenende liefert den letzten gültigen
    Wert, nicht NULL. Keine Namenskollision mit den Digital-``*_last``
    (``pump_*``/``mix_*``/``oel_burner``/``valve_hzrl``) — per Test geprüft.
    """
    parts: list[str] = []
    for feat in config.LAST_VALUE_FEATURES:
        col = config.analog_raw_col(feat)
        parts.append(f"LAST({col} ORDER BY ts) FILTER (WHERE {col} IS NOT NULL) AS {feat}_last")
    return parts


def _hzrkl_agg_parts() -> list[str]:
    """Rücklaufminderer "Ventil 2 Ergebnis" (dev2 a10–a12, binär 0/10) je Etage.

    ``hzrkl_{z}_on`` = AVG(Rohwert / ``HZRKL_ON_SCALE``) = An-Quotient 0..1 (wie die
    Digital-``_on``); ``hzrkl_{z}_last`` = letzter Nicht-NULL-Rohwert / Skala (wie
    ``{feat}_last``, FILTER statt blankem LAST). Reine Aggregate (kein Window).
    Die stetige Ventilstellung a13–a15 (``hzrkl_{z}_pos``) läuft über
    ``_analog_agg_parts`` (DEV2_ANALOG) bzw. ``_last_value_agg_parts``.
    """
    scale = config.HZRKL_ON_SCALE
    parts: list[str] = []
    for ch, feat in sorted(config.DEV2_HZRKL_ON.items()):
        col = config.dev_col(2, "analog", ch)
        parts.append(f"AVG({col} / {scale}) AS {feat}_on")
        parts.append(
            f"LAST({col} / {scale} ORDER BY ts) FILTER (WHERE {col} IS NOT NULL) AS {feat}_last"
        )
    return parts


def _hzrkl_rule_event_exprs() -> list[str]:
    """Regel-Übergänge: offen unter 20,1 °C, geschlossen darüber, sonst halten."""
    parts: list[str] = []
    for zone, ch in (("kell", 1), ("eg", 4), ("dg", 7)):
        col = config.dev_col(1, "analog", ch)
        parts.append(
            f"CASE WHEN {col} < {config.HZRKL_RULE_T_C} THEN 1.0 "
            f"WHEN {col} > {config.HZRKL_RULE_T_C} THEN 0.0 END AS hzrkl_{zone}_event"
        )
    return parts


def _hzrkl_rule_agg_parts() -> list[str]:
    """Stunden-Anteil und letzter Zustand aus der gehaltenen UVR-Regel."""
    parts: list[str] = []
    for zone in config.HZRKL_ZONES:
        col = f"hzrkl_{zone}_rule"
        parts.append(f"AVG({col}) FILTER (WHERE {col} IS NOT NULL) AS {col}")
        parts.append(f"LAST({col} ORDER BY ts) FILTER (WHERE {col} IS NOT NULL) AS {col}_last")
    return parts


def _wood_proxy_agg_parts() -> list[str]:
    """Holzbrand- und O1-Pumpen-Proxy; O1 selbst ist nicht geloggt."""
    wood_vl = config.analog_raw_col("wood_vl")
    buff_bot = config.analog_raw_col("buff_bot")
    flue = config.analog_raw_col("flue")
    pump = (
        f"CASE WHEN {wood_vl} IS NULL OR {buff_bot} IS NULL THEN NULL "
        f"WHEN {wood_vl} > {config.WOOD_PUMP_MIN_VL_C} "
        f"AND {wood_vl} > {buff_bot} + {config.WOOD_PUMP_MIN_DIFF_K} "
        f"AND {buff_bot} < {config.WOOD_PUMP_MAX_BUFFER_C} THEN 1.0 ELSE 0.0 END"
    )
    fire = (
        f"CASE WHEN {flue} IS NULL THEN NULL "
        f"WHEN {flue} > {config.WOOD_FIRE_MIN_FLUE_C} THEN 1.0 ELSE 0.0 END"
    )
    return [
        f"AVG({pump}) AS pump_wood_est_on",
        f"AVG({fire}) AS wood_fire_on",
    ]


def _ras_age_agg_parts() -> list[str]:
    """Stunden-MAX des RAS-Datenalters a11–a13 (``ras_age_kell/eg/dg``).

    Rohwert-Einheit (Annahme aus Rohdaten: +0,1 je 60 s ⇒ Wert × 10 ≈ Minuten
    seit letzter RAS-Übertragung); MAX ist robust gegen Resets innerhalb der Stunde.
    """
    parts: list[str] = []
    for ch in (11, 12, 13):
        parts.append(f"MAX({config.dev_col(1, 'analog', ch)}) AS {config.DEV1_ANALOG[ch]}")
    return parts


def _offset_parts() -> list[str]:
    """Median des RAS-Offset-Δ (perc − ist) je Zone."""
    parts: list[str] = []
    for _perc, _ist, zone in config.RAS_ZONES:
        pcol = config.dev_col(1, "analog", {"kell": 1, "eg": 4, "dg": 7}[zone])
        icol = config.dev_col(1, "analog", {"kell": 2, "eg": 5, "dg": 8}[zone])
        parts.append(f"MEDIAN({pcol} - {icol}) AS offset_{zone}")
    return parts


def _heatmin_delta_exprs() -> list[str]:
    """Window-Ausdrücke (positives Delta) für ``heat_min_*`` je Zone (CTE-Ebene).

    DEPRECATED (m1.1): a11–a13 sind KEINE Heiz-Minuten-Zähler, sondern ein
    Datenalter-Sägezahn (+0,1 je 60 s, Reset nach ≈1,1) → die Summe der
    positiven Deltas ist konstruktionsbedingt ≈ 6 je Stunde (informationslos).
    Berechnung unverändert zu m1.0 (nur v001-Kompatibilität): ``v − LAG(v)``,
    GREATEST(..., 0) ignoriert Abfälle (Resets).
    """
    parts: list[str] = []
    for ch, feat in config.HEAT_MIN_DEPRECATED:
        col = config.dev_col(1, "analog", ch)
        parts.append(
            f"GREATEST({col} - LAG({col}) OVER (PARTITION BY ts_bucket ORDER BY ts), 0) "
            f"AS {feat}_delta"
        )
    return parts


def _heatmin_agg_parts() -> list[str]:
    """DEPRECATED ``heat_min_*``: Summe der positiven Deltas je Zone (Aggregat-Ebene).

    FILTER auf Nicht-NULL-Rohwerte ändert die Summe nicht (NULL-Zeilen liefern
    Delta 0, da DuckDB ``GREATEST(NULL, 0) = 0``), macht aber Stunden ganz ohne
    Rohwert (z. B. RAS-Maske vor ``RAS_INSTALL_TS``) zu NULL statt 0.
    """
    parts: list[str] = []
    for ch, feat in config.HEAT_MIN_DEPRECATED:
        col = config.dev_col(1, "analog", ch)
        parts.append(f"SUM({feat}_delta) FILTER (WHERE {col} IS NOT NULL) AS {feat}")
    return parts


def _divergence_parts() -> list[str]:
    """Divergenz-Flag je Zone: |dev1_ist − dev2_ist| > 1 K → 1 (else 0), AVG je Stunde."""
    parts: list[str] = []
    for dev1_ist, dev2_ist in config.RAS2_ZONES:
        c1 = config.dev_col(
            1, "analog", {"ras_kell_ist": 2, "ras_eg_ist": 5, "ras_dg_ist": 8}[dev1_ist]
        )
        c2 = config.dev_col(
            2, "analog", {"ras2_kell_ist": 5, "ras2_eg_ist": 7, "ras2_dg_ist": 9}[dev2_ist]
        )
        zone = {"ras_kell_ist": "kell", "ras_eg_ist": "eg", "ras_dg_ist": "dg"}[dev1_ist]
        parts.append(
            f"AVG(CASE WHEN {c1} IS NULL OR {c2} IS NULL THEN NULL "
            f"ELSE IF(ABS({c1} - {c2}) > {config.RAS_DIVERGENCE_K}, 1.0, 0.0) END) "
            f"AS ras_divergence_{zone}"
        )
    return parts


def hourly_agg_columns() -> list[str]:
    """Spaltennamen von ``hourly_agg`` (``ts_bucket`` + Aliase von ``hourly_agg_parts``).

    Dient dem inkrementellen Build: die Kontext-Zeilen des Vormonats müssen exakt
    dieses Schema haben, um per ``UNION ALL`` an die frische ``hourly_agg``
    angehängt zu werden (gleiche Spaltenreihenfolge, s. M1-Inkremental).
    """
    cols = ["ts_bucket"]
    for expr in hourly_agg_parts():
        cols.append(expr.rsplit(" AS ", 1)[1])
    return cols


def hourly_agg_parts() -> list[str]:
    """Alle Aggregat-Ausdrücke (Aggregat-Ebene, für Tests + ``build_hourly_sql``)."""
    return (
        _analog_agg_parts()
        + _last_value_agg_parts()
        + _digital_agg_parts()
        + _hzrkl_agg_parts()
        + _hzrkl_rule_agg_parts()
        + _wood_proxy_agg_parts()
        + _offset_parts()
        + _ras_age_agg_parts()
        + _heatmin_agg_parts()
        + _divergence_parts()
        + [
            "count(*) AS n_rows",
            "MIN(source) AS source",
            "MAX(dev1_analog_01 IS NOT NULL) AS offset_known",
        ]
    )


def sentinel_condition(col: str) -> str:
    """SQL-Bedingung "Roh-Wert ist TA-Sentinel" (zentrale Regel, ``config.SENTINEL_*``)."""
    return f"{col} >= {config.SENTINEL_MAX} OR {col} <= {config.SENTINEL_MIN}"


def _source_select_list() -> str:
    """Spaltenliste der ``src``-View inkl. RAS-Maske (m1.1) und Sentinel-Maske (m1.2).

    RAS-Roh-Kanäle (``config.RAS_RAW_COLUMNS``) mit ``ts < RAS_INSTALL_TS`` → NULL
    (gleicher Spaltenname). ``ts`` ist ein Wallclock-String fester Länge
    ('YYYY-MM-DD HH:MM:SS') → lexikographischer Vergleich ist exakt. Dadurch sind
    perc/ist/offset/ras2_*/ras_divergence_*/offset_known/ras_age_*/heat_min_*/
    ``*_last`` und alle Lags konsistent NULL bzw. 0 vor dem RAS-Einbau.

    Alle analogen Roh-Kanäle (``ANALOG_SOURCE_COLUMNS``) mit TA-Sentinel-Wert
    (``sentinel_condition``) → NULL; alle daraus abgeleiteten Aggregate (mean,
    ``_last``, median, max, Lags) ignorieren den Wert. Die ``WHERE``-Zeilenfilter
    der View referenzieren weiterhin die Roh-Spalten (Zeile zählt in ``n_rows``).
    """
    ras = set(config.RAS_RAW_COLUMNS)
    analog = set(ANALOG_SOURCE_COLUMNS)
    flue_col = config.dev_col(0, "analog", 10)
    parts: list[str] = []
    for col in SOURCE_COLUMNS[1:]:
        whens: list[str] = []
        if col in ras:
            whens.append(f"WHEN ts < '{config.RAS_INSTALL_TS}' THEN NULL")
        if col in analog:
            whens.append(f"WHEN {sentinel_condition(col)} THEN NULL")
        if col == flue_col:
            whens.extend(
                [
                    f"WHEN ts >= '{config.FLUE_DEFECT_START}' AND ts < '{config.FLUE_DEFECT_END}' THEN NULL",
                    f"WHEN {col} * {config.FLUE_SCALE} > {config.FLUE_MAX_C} THEN NULL",
                ]
            )
        if whens:
            value = f"{col} * {config.FLUE_SCALE}" if col == flue_col else col
            parts.append(f"CASE {' '.join(whens)} ELSE {value} END AS {col}")
        elif col == flue_col:
            parts.append(f"{col} * {config.FLUE_SCALE} AS {col}")
        else:
            parts.append(col)
    return ", ".join(parts)


def sentinel_count_sql(path_glob: str) -> str:
    """SQL: Anzahl TA-Sentinel-Samples je analogem Roh-Kanal (``{col}|hi`` / ``{col}|lo``).

    Gleicher Zeilenfilter wie die ``src``-View; read-only (Verifikation/REPORT).
    """
    exprs: list[str] = []
    for col in ANALOG_SOURCE_COLUMNS:
        exprs.append(f'count(*) FILTER (WHERE {col} >= {config.SENTINEL_MAX}) AS "{col}|hi"')
        exprs.append(f'count(*) FILTER (WHERE {col} <= {config.SENTINEL_MIN}) AS "{col}|lo"')
    return (
        f"SELECT {', '.join(exprs)} FROM read_parquet('{path_glob}') "
        "WHERE dev0_analog_01 IS NOT NULL OR dev0_digital_02 IS NOT NULL"
    )


def _read_parquet_arg(glob: str | list[str]) -> str:
    """SQL-Literal für ``read_parquet``: 1 Pfad → `'path'`, mehrere → ``['a', 'b']``.

    ``list[str]`` (R4'-Chunked-Full: Monatsdatei(en) eines Chunks) wird als
    SQL-Array-Literal übergeben (keine Glob-Charakteristik in den Pfaden).
    """
    if isinstance(glob, list):
        return "[" + ", ".join(f"'{p}'" for p in glob) + "]"
    return f"'{glob}'"


def build_source_view_sql(
    winsol_glob: str | list[str] | None,
    cmi_glob: str | list[str] | None = None,
    *,
    ts_from: str | None = None,
    ts_to: str | None = None,
) -> str:
    """SQL: view ``src`` = vereinigte Roh-Quellen (nur benötigte Spalten + source-Flag).

    RAS-Kanäle vor ``config.RAS_INSTALL_TS`` und TA-Sentinel-Werte aller analogen
    Kanäle sind NULL (s. ``_source_select_list``).

    ``winsol_glob``/``cmi_glob`` dürfen leer sein (``None``) — inkrementeller
    Build liest pro Quelle nur den aktuellen Monat; ein fehlender Monats-Parquet
    (z. B. WinSol im aktuellen Monat, da Bestand frozen) entfällt hier einfach.
    Beide None → ``ValueError``. ``list[str]`` (R4'-Chunked-Full) = konkrete
    Monatsdatei(en) des Chunks → ``read_parquet(['a', 'b'])``
    (s. ``_read_parquet_arg``).

    ``ts_from``/``ts_to`` (R4'-Chunked-Full): optionaler ts-Range-Prädikat auf
    die Chunk-Grenzen, ``'YYYY-MM-DD HH:00:00'`` (``ts`` ist Wallclock-String
    fester Länge → lexicografisch = chronologisch; eine 10-s-Row fällt exakt in
    EINEN Bucket/Chunk, die Zuordnung ist verlustfrei):
    ``ts >= ts_from AND ts < ts_to`` (einseitig offen, keine Doppel-/Lücken-
    Zeilen an der Chunk-Grenze). Beide None → unverändert (Full/Inkremental).
    """
    range_pred = ""
    if ts_from:
        range_pred += f" AND ts >= '{ts_from}'"
    if ts_to:
        range_pred += f" AND ts < '{ts_to}'"
    col_list = _source_select_list()
    parts: list[str] = []
    if winsol_glob:
        parts.append(
            f"""SELECT ts, {col_list}, 'winsol_pq' AS source
FROM read_parquet({_read_parquet_arg(winsol_glob)})
WHERE (dev0_analog_01 IS NOT NULL OR dev0_digital_02 IS NOT NULL){range_pred}"""
        )
    if cmi_glob:
        parts.append(
            f"""SELECT ts, {col_list}, 'cmi_log_pq' AS source
FROM read_parquet({_read_parquet_arg(cmi_glob)})
WHERE (dev0_analog_01 IS NOT NULL OR dev0_digital_02 IS NOT NULL){range_pred}"""
        )
    if not parts:
        raise ValueError("build_source_view_sql: beide Quell-Globs leer")
    return "CREATE OR REPLACE VIEW src AS\n" + "\nUNION ALL\n".join(parts) + "\n;"


def _hzrkl_rule_expr(zone: str, seed: float | None = None) -> str:
    """Forward-Fill-Ausdruck für ``hzrkl_{zone}_rule`` (Minderer-Zustandserhalt).

    Ohne ``seed`` (Full-Build): reiner ``LAST_VALUE(… IGNORE NULLS)`` über die ganze
    Roh-Historie. Mit ``seed`` (Inkremental-Build, nur aktueller Monat): ``COALESCE``
    mit dem gehaltenen Regelzustand des Vormonats — für Roh-Zeiten VOR dem ersten
    Event des Monats liefert das exakt den Full-Build-Wert (Zustand wird über die
    Monatsgrenze gehalten; s. M1-Inkremental). ``seed=None`` → ``COALESCE(…, NULL)``
    ist semantisch identisch zum Full-Expression.
    """
    ff = (
        "LAST_VALUE(hzrkl_"
        f"{zone}_event IGNORE NULLS) OVER "
        "(ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
    )
    if seed is None:
        return f"{ff} AS hzrkl_{zone}_rule"
    return f"COALESCE({ff}, {seed!r}) AS hzrkl_{zone}_rule"


def build_hourly_sql(
    winsol_glob: str | list[str],
    cmi_glob: str | list[str] | None = None,
    rule_seed: dict[str, float | None] | None = None,
    *,
    ts_from: str | None = None,
    ts_to: str | None = None,
) -> str:
    """Kompletes SQL: view ``src`` → ``src_bucketed`` → ``src_delta`` → ``hourly_agg``.

    Bucket = 'YYYY-MM-DD HH:00' (Wallclock = Europe/Berlin). ``n_rows = 0`` kann
    hier nicht auftreten (Gruppierung existiert nur mit ≥1 Row) — fehlende Stunden
    werden im Feature-Store per CROSS-JOIN auf das Stunden-Raster erkannt (gap).

    ``winsol_glob``/``cmi_glob``: Glob (``<root>/*/*/*.parquet``) oder
    konkrete Monatsdatei(en) als ``list[str]`` (M1-Inkremental: aktueller
    Monat; R4'-Chunked-Full: Chunks) mit ggf. leeren Quelle = ``None``.

    ``rule_seed`` (optional, nur inkrementeller Build): je Zone der gehaltene
    UVR-Regelzustand am Ende des Vormonats → seedet das ``hzrkl_{z}_rule``
    Forward-Fill (s. ``_hzrkl_rule_expr``). ``None`` (Default, Full-Build) →
    unveränderte Expression über die komplette Roh-Historie.

    ``ts_from``/``ts_to`` (R4'-Chunked-Full): ts-Range-Prädikat auf die Chunk-
    Grenzen in der ``src``-View (s. ``build_source_view_sql``); ``None`` →
    unverändert (monolithischer Full-Build / inkrementeller Monats-Build).

    **Warum 4 Stufen?** Die (DEPRECATED) ``heat_min_*``-Deltas (LAG-Window) werden
    in einer eigenen View ``src_delta`` berechnet, ABGETRENNT von der Aggregation
    (``hourly_agg``). In DuckDB 2.0.0 führt das Kombinieren von Window-Funktionen
    mit vielen Aggregaten in EINER Query zu einem Planungsfehler (Deltas wurden
    halbiert); die getrennte View ist korrekt + nachvollziehbarer (Unit-Test
    test_resample.py deckt das ab). ``ras_age_*`` (MAX) und ``{feat}_last``
    (LAST … ORDER BY ts FILTER) sind reine Aggregate in ``hourly_agg`` — kein
    Window; die Kombination mit den Deltas ist per Unit-Test abgesichert. Gleiches
    gilt für ``hzrkl_*`` (AVG / LAST … FILTER, m1.2).
    """
    return build_source_view_sql(
        winsol_glob, cmi_glob, ts_from=ts_from, ts_to=ts_to
    ) + _hourly_agg_pipeline_sql(rule_seed)


def _hourly_agg_pipeline_sql(
    rule_seed: dict[str, float | None] | None = None,
) -> str:
    """SQL: 4 Stufen ``src`` → ``src_bucketed`` → ``src_delta`` → ``src_rules``
    → ``hourly_agg`` (vorausgesetzt die View ``src`` existiert).

    Geteilter Baustein: sowohl ``build_hourly_sql`` (echte Rohdaten) als auch
    ``build_empty_hourly_sql`` (0-Row-``src``) laufen über dieselbe Pipeline →
    die Aggregat-``hourly_agg``-Typen sind in beiden Fällen exakt identisch
    (z. B. ``hzrkl_{z}_rule_last`` DECIMAL(2,1), da die Event-Literale
    1.0/0.0 im ``LAST(…)`` den DECIMAL-Typ halten). Ein hartkodierter
    ``NULL::DOUBLE``-Kollage-Zweig würde diese DECIMAL-Spalten in DOUBLE
    verschieben und ``pa.concat_tables`` der Monatsdateien brechen.
    """
    agg = ",\n  ".join(hourly_agg_parts())
    deltas = ",\n    ".join(_heatmin_delta_exprs())
    events = ",\n    ".join(_hzrkl_rule_event_exprs())
    seed = rule_seed or {}
    rules = ",\n    ".join(_hzrkl_rule_expr(z, seed.get(z)) for z in config.HZRKL_ZONES)
    return f"""
CREATE OR REPLACE VIEW src_bucketed AS
SELECT *, substr(ts, 1, 13) || ':00' AS ts_bucket FROM src;

CREATE OR REPLACE VIEW src_delta AS
SELECT *,
    {deltas},
    {events}
FROM src_bucketed;

CREATE OR REPLACE VIEW src_rules AS
SELECT *,
    {rules}
FROM src_delta;

CREATE OR REPLACE VIEW hourly_agg AS
SELECT
  ts_bucket,
  {agg}
FROM src_rules
GROUP BY ts_bucket
"""


def empty_source_view_sql() -> str:
    """SQL: 0-Row-View ``src`` mit korrekten Typen (Chunk ohne Rohdaten).

    ``ts``/``source`` VARCHAR, alle 46 Roh-Spalten DOUBLE (s.
    ``SOURCE_COLUMNS``; geprüft: exakt diese Typen in den Monats-Parquets).
    ``WHERE FALSE`` → 0 Zeilen; die Typen bestimmen die Aggregat-``hourly_agg``-
    Schema-Typen (kanonische Pipeline über leeren ``src`` statt hartkodierter
    ``NULL::DOUBLE``-Kollage, s. ``build_empty_hourly_sql``).
    """
    cols = ",\n  ".join(
        ["NULL::VARCHAR AS ts"]
        + [f"NULL::DOUBLE AS {c}" for c in SOURCE_COLUMNS[1:]]
        + ["NULL::VARCHAR AS source"]
    )
    # Abschließendes ";" (wie ``build_source_view_sql``): ``build_empty_hourly_
    # sql`` hängt die Pipeline-Statements direkt an → Trenner erforderlich.
    return f"CREATE OR REPLACE VIEW src AS\nSELECT\n  {cols}\nWHERE FALSE\n;"


def build_empty_hourly_sql(
    rule_seed: dict[str, float | None] | None = None,
) -> str:
    """SQL: leeres ``hourly_agg`` (0 Rows, korrektes Schema) für Chunks ohne
    Rohdaten (z. B. vor dem Rohdaten-Start / fehlender Monatsdatei).

    Kanonische 4-Stufen-Pipeline über eine 0-Row-``src`` (s.
    ``empty_source_view_sql``) → ``hourly_agg``-Typen exakt wie beim echten
    Build (keine hartkodierten NULL-Typen, die DECIMAL-Spalten verschieben).
    ``rule_seed`` wirkt hier nicht (0 Rows), bleibt aber signaturrelevant für
    Konsistenz mit ``build_hourly_sql``.
    """
    return empty_source_view_sql() + _hourly_agg_pipeline_sql(rule_seed)


RESAMPLE_NOTES = """
Resampling-Regeln (M1, Details in SCHEMA.md):
- Bucket = Kalenderstunde Europe/Berlin = Trunk der Wallclock ('YYYY-MM-DD HH:00').
- mean (analog) + last-non-NULL (``{feat}_last``) / median (Offset-Δ) / mean+last (Digital) /
  max (RAS-Datenalter ``ras_age_*``) / sum-positiver-Deltas (``heat_min_*``, DEPRECATED).
- RAS-Maske: RAS-Roh-Kanäle vor RAS_INSTALL_TS (2021-10-20 17:00) = NULL (``src``-View).
- Sentinel-Maske: analoge Roh-Werte >= 3276.0 oder <= -999.0 (TA-Fehlerwerte) = NULL.
- hzrkl_{z}_on = AVG(dev2 a10–a12 / 10), hzrkl_{z}_pos = mean dev2 a13/a15/a14 (+ ``_last``).
- gap = fehlende Kalenderstunde (kein 10-s-Row) → Flag 1, Feature-Werte NULL.
- Übergang winsol→cmi: kein Overlap (geprüft), source-Flag je Zeile, Schema identisch.
"""

__all__ = [
    "SOURCE_COLUMNS",
    "ANALOG_SOURCE_COLUMNS",
    "check_source_schema",
    "sentinel_condition",
    "sentinel_count_sql",
    "build_source_view_sql",
    "build_hourly_sql",
    "empty_source_view_sql",
    "build_empty_hourly_sql",
    "hourly_agg_parts",
    "hourly_agg_columns",
    "RESAMPLE_NOTES",
]
