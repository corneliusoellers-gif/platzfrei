"""Bessere Namen für Plätze, die nur "Fußballplatz [Ort]" o. ä. heißen.

Ohne Nominatim: Straßen (transportation/segment) und benannte Flächen
(base/land_use: Schule, Park, Stadion, Spielplatz …) kommen als Datei aus
Overture, also aus OSM. Für jeden Platz:
  - Schule/Park/Stadion/Spielplatz bis 50 m  -> "Fußballplatz Ort – Grundschule X"
  - nächste benannte Straße bis 100 m        -> Ort wird "Ort, Straße"
Ergebnis je Platz: [id, flaechenname, strasse] (leer = nichts gefunden).
"""
import math, re, time
import numpy as np
import pyarrow.dataset as ds, pyarrow.compute as pc
import shapely

BUCKET = "overturemaps-us-west-2"
K = math.cos(math.radians(51))          # Längengrade in Deutschland grob auf Meter bringen
M = 111320
FLAECHEN = ["school", "park", "stadium", "playground", "recreation_ground", "village_green"]
RANG = {k: i for i, k in enumerate(FLAECHEN)}
STRASSEN = ["primary", "secondary", "tertiary", "residential", "unclassified", "living_street",
            "pedestrian", "service"]
NICHT = re.compile(r"kita|kinderg|kindertages|krippe|hort\b", re.I)
ALLGEMEIN = {"spielplatz", "park", "sportplatz", "schule", "schulhof", "bolzplatz", "grünanlage",
             "grünfläche", "stadion", "sportanlage", "spielfläche"}


def _de(F):
    return (F("xmin") > 5.8) & (F("xmax") < 15.1) & (F("ymin") > 47.2) & (F("ymax") < 55.1)


def _skaliert(g):
    return shapely.transform(g, lambda c: c * np.array([K, 1.0]))


def berechnen(s3, version, plaetze, log=print):
    """plaetze: Liste von dict(id, lat, lon). Gibt {id: (flaeche, strasse)} zurück."""
    pkt = _skaliert(shapely.points([p["lon"] for p in plaetze], [p["lat"] for p in plaetze]))
    baum = shapely.STRtree(pkt)
    beste_f = {}                        # index -> (rang, meter, name)
    beste_s = {}                        # index -> (meter, name)
    F = lambda k: pc.field("bbox", k)

    def durchgehen(thema, filt, cols, rand_m, verarbeiten):
        t0 = time.time(); n = 0
        d = ds.dataset(f"{BUCKET}/release/{version}/{thema}/", filesystem=s3, format="parquet")
        for b in d.to_batches(columns=cols, filter=filt, batch_size=100000):
            if not b.num_rows: continue
            n += b.num_rows
            bb = b.column("bbox")
            x0 = pc.struct_field(bb, "xmin").to_numpy(); x1 = pc.struct_field(bb, "xmax").to_numpy()
            y0 = pc.struct_field(bb, "ymin").to_numpy(); y1 = pc.struct_field(bb, "ymax").to_numpy()
            r = rand_m / M
            kasten = shapely.box((x0 - r / K) * K, y0 - r, (x1 + r / K) * K, y1 + r)
            zeile, platz = baum.query(kasten)
            if not len(zeile): continue
            namen = [((x or {}).get("primary") or "").strip() for x in b.column("names").to_pylist()]
            gut = np.array([bool(namen[i]) for i in zeile], dtype=bool)
            zeile, platz = zeile[gut], platz[gut]
            if not len(zeile): continue
            geo = _skaliert(shapely.from_wkb(b.column("geometry").take(zeile).to_numpy(zero_copy_only=False)))
            meter = shapely.distance(geo, pkt[platz]) * M
            klasse = b.column("class").take(zeile).to_pylist()
            for z, p, m, kl in zip(zeile, platz, meter, klasse):
                verarbeiten(int(p), float(m), namen[z], kl)
        log(f"  {thema}: {n} Zeilen in {time.time() - t0:.0f} s")

    def flaeche(p, m, name, kl):
        if m > 50 or NICHT.search(name) or name.lower() in ALLGEMEIN or len(name) > 60: return
        k = (0 if m < 1 else 1, RANG[kl], m)
        if p not in beste_f or k < beste_f[p][0]: beste_f[p] = (k, name)

    def strasse(p, m, name, kl):
        if m > 100 or len(name) > 60: return
        if p not in beste_s or m < beste_s[p][0]: beste_s[p] = (m, name)

    durchgehen("theme=base/type=land_use", _de(F) & pc.field("class").isin(FLAECHEN),
               ["names", "class", "geometry", "bbox"], 50, flaeche)
    durchgehen("theme=transportation/type=segment",
               _de(F) & (pc.field("subtype") == "road") & pc.field("class").isin(STRASSEN),
               ["names", "class", "geometry", "bbox"], 100, strasse)

    erg = {}
    for i, p in enumerate(plaetze):
        f = beste_f.get(i, (None, ""))[1]
        s = beste_s.get(i, (None, ""))[1]
        if f or s: erg[p["id"]] = (f, s)
    return erg
