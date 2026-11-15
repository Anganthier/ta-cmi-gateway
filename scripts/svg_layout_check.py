#!/usr/bin/env .venv/bin/python
"""Layout-QA für docs/hydraulic-schematic.svg (und andere SVGs).

Prüft mechanisch:
  1. TEXT-TEXT: zwei Texte überlappen sich (Bounding-Box-Intersektion).
  2. TEXT-BOX:  ein Text schneidet die KANTE eines Rechtecks (Text halb
                drüber/halb drunter) — Text, der vollständig in einer Box
                liegt, ist OK.
  3. LINE-TEXT: eine Leitung (path mit id pipe-*/ctl-* oder class
                vl/rl/ww/gas/ctl) läuft durch einen Text.
  4. USE-TEXT:  ein <use>-Symbol überlappt einen Text.

Breitenschätzung: width ≈ font_size * 0.52 * len(text) (system-ui, gemischt).
Exit 0 = sauber, 1 = Kollisionen.
"""
import sys
import xml.etree.ElementTree as ET

NS = {"svg": "http://www.w3.org/2000/svg"}

CLASS_FONT = {
    "txt": 14, "txt-s": 12, "txt-t": 21, "txt-h": 15,
    "txt-addr": 12, "txt-risk": 13, "txt-c": 12,
}


def classes_of(el):
    return (el.get("class") or "").split()


def font_size_of(el, gfs=None):
    for c in classes_of(el):
        if c in CLASS_FONT:
            return CLASS_FONT[c]
    fs = el.get("font-size")
    if fs:
        return float(fs)
    return gfs or 14.0


def text_width(t, fs):
    return fs * 0.52 * len(t)


def text_bbox(el, gfs=None):
    x = float(el.get("x", 0) or 0)
    y = float(el.get("y", 0) or 0)
    t = "".join(el.itertext()).strip()
    if not t:
        return None
    fs = font_size_of(el, gfs)
    w = text_width(t, fs)
    anchor = el.get("text-anchor", "start")
    if anchor == "middle":
        x0, x1 = x - w / 2, x + w / 2
    elif anchor == "end":
        x0, x1 = x - w, x
    else:
        x0, x1 = x, x + w
    y0, y1 = y - 0.85 * fs, y + 0.25 * fs
    return (x0, y0, x1, y1, t)


def intersects(a, b, pad=0.0):
    return not (a[0] - pad >= b[2] or b[0] - pad >= a[2]
                or a[1] - pad >= b[3] or b[1] - pad >= a[3])


def overlap_area(a, b):
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, dx) * max(0.0, dy)


def area(a):
    return max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])


# Symbol-Abmessungen je Def (x/y = Center des <use>)
SYMBOL_HALB = {
    "sym-pump": 13, "sym-pt": 5, "sym-valve": 12,
    "sym-rad": 26, "sym-heatex": 40, "sym-chimney": 26,
}


def parse_polyline(d):
    """M/L/H/V (und absolute) in Punktesequenz. C/Q/S/T werden linear
    über Start/Ende approxiniert (Genügend für Crossing-Check)."""
    pts, cx, cy = [], 0.0, 0.0
    i, n = 0, len(d)
    while i < n:
        while i < n and d[i] in " \t\n\r":
            i += 1
        if i >= n:
            break
        c = d[i]
        if c in "MLHVZmlhvz":
            i += 1
        elif c in "CSQcqs":
            # Kurve: 6 (C) bzw. 4 (Q/S) Zahlen → Endpunkt verwenden
            cnt = 6 if c in "CScs" else 4
            vals, j = [], i
            while j < n and len(vals) < cnt:
                while j < n and d[j] in " ,\t\n\r":
                    j += 1
                if j >= n:
                    break
                k = j
                while k < n and d[k] not in " ,\t\n\rMLHVZCSQcqs":
                    k += 1
                try:
                    vals.append(float(d[j:k]))
                except ValueError:
                    break
                j = k
            i = j
            if len(vals) >= 4:
                pts.append((cx, cy))
                cx, cy = vals[-2], vals[-1]
            continue
        elif c in "Aa":
            # Bogen: Zahlen lesen (7), Endpunkt übernehmen
            vals, j = [], i
            while j < n and len(vals) < 7:
                while j < n and d[j] in " ,\t\n\r":
                    j += 1
                if j >= n:
                    break
                k = j
                while k < n and d[k] not in " ,\t\n\rMLHVZCSQcqsAa":
                    k += 1
                try:
                    vals.append(float(d[j:k]))
                except ValueError:
                    break
                j = k
            i = j
            if len(vals) >= 7:
                pts.append((cx, cy))
                cx, cy = vals[5], vals[6]
            continue
        else:
            break
        # Zahl(en) nach Kommando
        rel = c.islower()
        vals, j = [], i
        while j < n:
            while j < n and d[j] in " ,\t\n\r":
                j += 1
            if j >= n or d[j] in "MLHVZCSQcqsAa":
                break
            k = j
            while k < n and d[k] not in " ,\t\n\rMLHVZCSQcqsAa":
                k += 1
            try:
                vals.append(float(d[j:k]))
            except ValueError:
                break
            j = k
        i = j
        cmd = c.upper()
        if cmd == "M" or cmd == "L":
            for idx in range(0, len(vals) - 1, 2):
                nx, ny = vals[idx], vals[idx + 1]
                if not rel:
                    cx, cy = nx, ny
                else:
                    cx += nx
                    cy += ny
                pts.append((cx, cy))
        elif cmd in "HV":
            val = vals[0]
            if not rel:
                cx = val if cmd == "H" else cx
                cy = val if cmd == "V" else cy
            else:
                cx += val if cmd == "H" else 0
                cy += val if cmd == "V" else 0
            pts.append((cx, cy))
    return pts


def sample_segment(p1, p2, step=5.0):
    import math
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return [p1]
    nseg = max(1, int(dist / step))
    return [(p1[0] + dx * i / nseg, p1[1] + dy * i / nseg) for i in range(nseg + 1)]


def main(path):
    tree = ET.parse(path)
    root = tree.getroot()

    texts, boxes, lines, uses = [], [], [], []

    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "text":
            bb = text_bbox(el)
            if bb:
                texts.append(bb)
        elif tag == "rect":
            if el.get("x") is not None and el.get("width") is not None:
                x, y = float(el.get("x")), float(el.get("y"))
                w, h = float(el.get("width")), float(el.get("height"))
                boxes.append((x, y, x + w, y + h,
                              " ".join(el.itertext()).strip() or "(empty-box)"))
        elif tag == "path":
            pid = el.get("id") or ""
            cls = el.get("class") or ""
            d = el.get("d") or ""
            relevant = (pid.startswith("pipe-") or pid.startswith("ctl-")
                        or any(k in cls for k in ("vl", "rl", "ww", "gas", "ctl")))
            if relevant and d:
                pts = parse_polyline(d)
                for i in range(len(pts) - 1):
                    lines.extend(sample_segment(pts[i], pts[i + 1]))
        elif tag == "use" and el.get("x") is not None and el.get("y") is not None:
            x, y = float(el.get("x")), float(el.get("y"))
            href = (el.get("href") or "").lstrip("#") or el.get("id") or "use"
            hw = SYMBOL_HALB.get(href, 26)
            # chimney: Asymmetrisch (steigt nach oben)
            if href == "sym-chimney":
                box = (x - 26, y - 86, x + 26, y + 2)
            else:
                hh = 24 if href in ("sym-rad", "sym-heatex") else hw
                box = (x - hw, y - hh, x + hw, y + hh)
            if href == "sym-rad":
                box = (x - 26, y - 14, x + 26, y + 14)
            if href == "sym-heatex":
                box = (x - 40, y - 24, x + 40, y + 24)
            uses.append(box + (href,))

    v = []
    # 1) TEXT-TEXT (nur echte Überdeckung, nicht nur 1px Berührung)
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = texts[i], texts[j]
            if intersects(a, b, pad=-2.0):
                ov = overlap_area(a, b)
                if ov > 0.08 * min(area(a), area(b)):
                    v.append(f"TEXT-TEXT  [{a[4][:34]}] × [{b[4][:34]}] "
                             f"@ ({a[0]:.0f},{a[1]:.0f}) / ({b[0]:.0f},{b[1]:.0f})")
    # 2) TEXT-BOX: Text überquert Box-Rand (Text-ZENTRUM außerhalb der Box)
    for t in texts:
        cx, cy = (t[0] + t[2]) / 2, (t[1] + t[3]) / 2
        for b in boxes:
            center_inside = b[0] <= cx <= b[2] and b[1] <= cy <= b[3]
            if intersects(t, b, pad=-2.0) and not center_inside:
                v.append(f"TEXT-BOX   [{t[4][:34]}] überquert Box '{b[4][:34]}' "
                         f"@ ({b[0]:.0f},{b[1]:.0f}) {b[2]-b[0]:.0f}×{b[3]-b[1]:.0f}")
    # 3) LINE-TEXT (≥2 Stützpunkte in Text-BBox → Leitung läuft durch)
    for t in texts:
        hits = sum(1 for px, py in lines
                   if t[0] - 1 <= px <= t[2] + 1 and t[1] - 1 <= py <= t[3] + 1)
        if hits >= 2:
            v.append(f"LINE-TEXT  Leitung durch Text [{t[4][:34]}] "
                     f"@ ({t[0]:.0f},{t[1]:.0f})")
    # 4) USE-TEXT (Symbol deckt >25 % des Textes)
    for u in uses:
        for t in texts:
            ov = overlap_area(u, t)
            if ov > 0.25 * area(t):
                v.append(f"USE-TEXT   Symbol {u[4]} × [{t[4][:34]}] "
                         f"@ ({t[0]:.0f},{t[1]:.0f})")

    if v:
        print(f"{len(v)} Kollision(en):")
        for line in v:
            print("  " + line)
        return 1
    print("OK: keine Kollisionen "
          f"({len(texts)} Texte, {len(boxes)} Boxen, {len(lines)} Leitungs-Punkte, {len(uses)} Symbole)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
