"""Platzfrei: wöchentlicher Import neuer Sportplätze aus OpenStreetMap.

Läuft als GitHub Action (.github/workflows/wochen-import.yml), geht aber auch
von Hand:  SUPABASE_URL=… SUPABASE_SERVICE_KEY=… python import/wochen_import.py

1. Neueste Overture-Maps-Version suchen (kommt etwa einmal im Monat). Ist sie
   schon eingespielt, wird nichts importiert.
2. Sportplätze in Deutschland holen und einstufen (wie bisher gen.py):
   Park/Spielplatz/Name -> vermutlich öffentlich, Schulgelände -> Schulhof,
   Stadion/Vereinsname/Tennis daneben -> Verein, sonst unklar.
3. Untereinander zusammenfassen (40 m beliebig, 100 m gleiche Sportart).
4. In Häppchen an platzfrei_osm_import() schicken. Die Datenbank gleicht mit
   den vorhandenen Plätzen ab und legt nur Neues an.
5. doppelte_zusammenfuehren() aufrufen und alles im Protokoll festhalten.
6. Bei neuer Overture-Version (oder Option "namen"): Plätze, die nur
   "Fußballplatz [Ort]" o. ä. heißen, bekommen per namen.py einen Namen nach
   Schule/Park daneben bzw. die Straße (platzfrei_namen_setzen()).

Quelle: © OpenStreetMap-Mitwirkende (ODbL), über Overture Maps.
"""
import collections, json, math, os, re, sys, time, urllib.request, urllib.error

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.fs as fs

HIER = os.path.dirname(os.path.abspath(__file__))
URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ERZWINGEN = os.environ.get("ERZWINGEN", "") in ("1", "true", "ja")
NAMEN = os.environ.get("NAMEN", "") in ("1", "true", "ja")
PLATZHALTER = ["Fußballplatz", "Basketballplatz", "Tischtennisplatte", "Beachvolleyballfeld", "Laufbahn",
               "Skatepark", "Calisthenics-Park"]
TEIL = 1000          # Plätze pro Aufruf; bleibt sicher unter jeder Zeitgrenze
BUCKET = "overturemaps-us-west-2"


# ------------------------------------------------------------------ Supabase
def api(pfad, daten=None, methode=None):
    anfrage = urllib.request.Request(
        URL + "/rest/v1/" + pfad,
        data=None if daten is None else json.dumps(daten).encode(),
        method=methode or ("POST" if daten is not None else "GET"),
        headers={"apikey": KEY, "Content-Type": "application/json", "Prefer": "return=representation",
                 # alter service_role-Schlüssel (JWT) gehört auch hierhin, neuer sb_secret_… nur in apikey
                 **({} if KEY.startswith("sb_") else {"Authorization": "Bearer " + KEY})})
    for versuch in range(4):
        try:
            with urllib.request.urlopen(anfrage, timeout=300) as a:
                text = a.read().decode()
                return json.loads(text) if text else None
        except urllib.error.HTTPError as f:
            fehler = f.read().decode()
            if f.code < 500 or versuch == 3:
                sys.exit(f"Supabase-Fehler {f.code} bei {pfad}: {fehler}")
        except urllib.error.URLError as f:
            if versuch == 3:
                sys.exit(f"Supabase nicht erreichbar: {f}")
        time.sleep(5 * (versuch + 1))


# ------------------------------------------------------------------ Overture
def neueste_version(s3):
    infos = s3.get_file_info(fs.FileSelector(BUCKET + "/release/"))
    versionen = sorted(i.base_name for i in infos if i.type == fs.FileType.Directory)
    if not versionen:
        sys.exit("Keine Overture-Version gefunden.")
    return versionen[-1]


def stand_text(version):            # "2026-10-21.0" -> "21.10.2026"
    j, m, t = version.split(".")[0].split("-")
    return f"{t}.{m}.{j}"


def overture_laden(s3, version):
    d = ds.dataset(f"{BUCKET}/release/{version}/theme=base/type=land_use/", filesystem=s3, format="parquet")
    F = lambda k: pc.field("bbox", k)
    KL = ["pitch", "playground", "park", "recreation_ground", "school", "sports_centre", "track",
          "village_green", "stadium", "fitness_station"]
    f = (F("xmin") > 5.8) & (F("xmax") < 15.1) & (F("ymin") > 47.2) & (F("ymax") < 55.1) & pc.field("class").isin(KL)
    teile = [b for b in d.to_batches(columns=["id", "class", "subtype", "names", "source_tags", "bbox"],
                                     filter=f, batch_size=200000) if b.num_rows]
    return pa.Table.from_batches(teile).to_pylist()


# ------------------------------------------------------------------ Einstufen (wie gen.py)
G = json.load(open(os.path.join(HIER, "grenzen.json")))
RINGE = []
for land, fl in G.items():
    for f in fl:
        r = f[0]; xs = [p[0] for p in r]; ys = [p[1] for p in r]
        RINGE.append((land, r, min(ys), max(ys), min(xs), max(xs)))


def im_ring(r, lat, lon):
    d = False; j = len(r) - 1
    for i in range(len(r)):
        xi, yi = r[i]; xj, yj = r[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi: d = not d
        j = i
    return d


def in_de(lat, lon):
    aus = False
    for land, r, s, n, w, e in RINGE:
        if lat < s or lat > n or lon < w or lon > e or not im_ring(r, lat, lon): continue
        if land == "DEU": return True
        aus = True
    return not aus


OSM_SPORT = {"soccer": "Fußball", "football": "Fußball", "basketball": "Basketball", "streetball": "Basketball",
             "table_tennis": "Tischtennis", "beachvolleyball": "Beachvolleyball", "volleyball": "Beachvolleyball",
             "running": "Laufen", "athletics": "Laufen", "fitness": "Calisthenics", "calisthenics": "Calisthenics",
             "exercise": "Calisthenics", "skateboard": "Skaten", "multi": "Fußball"}
OSM_BELAG = {"grass": "Naturrasen", "artificial_turf": "Kunstrasen", "artificial_grass": "Kunstrasen",
             "asphalt": "Beton/Asphalt", "concrete": "Beton/Asphalt", "paved": "Beton/Asphalt", "clay": "Asche",
             "dirt": "Asche", "earth": "Asche", "fine_gravel": "Asche", "compacted": "Asche", "tartan": "Tartan",
             "sand": "Sand"}
VEREIN_SPORT = {"tennis", "equestrian", "shooting", "golf", "archery", "ice_stock", "field_hockey", "baseball",
                "american_football", "padel", "miniature_golf", "paintball", "horse_racing"}
OEFF_NAME = re.compile(r"bolz|streetball|basketballplatz|basketballfeld|skate|pumptrack|dirt ?park|calisthenics|street ?workout|fitness ?park|trimm|freizeit(anlage|gelände|platz|fläche)|jugendplatz|aktivpark|mehrgenerationen|spielplatz|spielwiese|bürgerpark|stadtpark", re.I)
VEREIN_TXT = re.compile(r"\b(e\.? ?v\.?|verein|tsv|sv|fc|sc|tus|vfb|vfl|tsg|sg|spvgg|djk|fsv|ssv|tv|asv|sportfreunde|turnverein|sportclub|sportverein)\b", re.I)
AMT = re.compile(r"\b(stadt|gemeinde|markt|bezirksamt|landeshauptstadt)\b", re.I)


def meter(a, b, c, d): return math.hypot((a - c) * 111320, (b - d) * 111320 * math.cos(math.radians(a)))
def tags(r): return dict(r["source_tags"] or [])
def mitte(b): return ((b["ymin"] + b["ymax"]) / 2, (b["xmin"] + b["xmax"]) / 2)


def einstufen(t):
    Z = 0.01
    umg = collections.defaultdict(list)
    for r in t:
        k = r["class"]
        art = "schule" if k == "school" else "park" if k in ("park", "playground", "recreation_ground", "village_green") \
            else "stadion" if k == "stadium" else None
        if not art: continue
        b = r["bbox"]
        if b["ymax"] - b["ymin"] > 0.05 or b["xmax"] - b["xmin"] > 0.07: continue
        nm = (r["names"] or {}).get("primary") or ""
        e = (art, b, nm, k == "playground")
        for a in range(math.floor(b["ymin"] / Z), math.floor(b["ymax"] / Z) + 1):
            for c in range(math.floor(b["xmin"] / Z), math.floor(b["xmax"] / Z) + 1):
                umg[(a, c)].append(e)

    def drin(lat, lon):
        return [u for u in umg.get((math.floor(lat / Z), math.floor(lon / Z)), [])
                if u[1]["ymin"] <= lat <= u[1]["ymax"] and u[1]["xmin"] <= lon <= u[1]["xmax"]]

    R = 0.003
    vsp = collections.defaultdict(list)
    for r in t:
        if r["class"] != "pitch": continue
        s = {x.strip() for x in (tags(r).get("sport") or "").split(";")}
        if s & VEREIN_SPORT:
            la, lo = mitte(r["bbox"]); vsp[(math.floor(la / R), math.floor(lo / R))].append((la, lo))

    def verein_nah(la, lo, m=150):
        a0, c0 = math.floor(la / R), math.floor(lo / R)
        return any(meter(la, lo, *p) <= m for a in (a0 - 1, a0, a0 + 1) for c in (c0 - 1, c0, c0 + 1)
                   for p in vsp.get((a, c), []))

    out = []
    for r in t:
        if r["class"] not in ("pitch", "track"): continue
        tg = tags(r)
        roh = [x.strip().lower() for x in (tg.get("sport") or "").split(";") if x.strip()]
        sp = []
        for x in roh:
            if x in OSM_SPORT and OSM_SPORT[x] not in sp: sp.append(OSM_SPORT[x])
            if x == "multi" and "Basketball" not in sp: sp.append("Basketball")
        if not sp: continue
        la, lo = mitte(r["bbox"])
        if not in_de(la, lo): continue
        priv = bool(tg.get("indoor") == "yes" or tg.get("building")
                    or tg.get("access") in ("private", "no", "customers", "members") or tg.get("fee") == "yes")
        nm = ((r["names"] or {}).get("primary") or tg.get("name") or "").strip()
        hier = drin(la, lo)
        verein = any(u[0] == "stadion" for u in hier) or bool(VEREIN_TXT.search(tg.get("operator", "") + " " + nm)) \
            or verein_nah(la, lo) or tg.get("club")
        zug = grund = None
        if not priv:
            schule = next((u for u in hier if u[0] == "schule"), None)
            park = next((u for u in hier if u[0] == "park" and u[3]), None) or next((u for u in hier if u[0] == "park"), None)
            if schule:
                zug = "vermutlich Schulhof"; grund = ("schule", schule[2])
            elif not verein:
                if park: grund = ("spielplatz" if park[3] else "park", park[2])
                elif AMT.search(tg.get("operator", "")): grund = ("amt", tg["operator"])
                elif tg.get("access") in ("yes", "public", "permissive"): grund = ("offen", "")
                elif nm and OEFF_NAME.search(nm): grund = ("name", nm)
                elif "skateboard" in roh or "bmx" in roh: grund = ("skate", "")
                elif tg.get("fee") == "no": grund = ("gratis", "")
                if grund: zug = "vermutlich öffentlich"
        out.append(dict(name=nm or sp[0] + "-Fläche", lat=round(la, 6), lon=round(lo, 6), sp=sp,
                        belag=OSM_BELAG.get((tg.get("surface") or "").lower()), licht=tg.get("lit") == "yes",
                        zug=zug, grund=grund, verein=bool(verein) and zug is None))
    return out


# ------------------------------------------------------------------ Zusammenfassen (wie merge.py)
def zusammenfassen(P):
    RANG = {"vermutlich öffentlich": 0, "vermutlich Schulhof": 1, None: 2}
    P.sort(key=lambda p: (RANG[p["zug"]], -len(p["sp"])))
    R = 0.0015; grid = collections.defaultdict(list); orte = []
    for p in P:
        a0, c0 = math.floor(p["lat"] / R), math.floor(p["lon"] / R); hit = None
        for a in (a0 - 1, a0, a0 + 1):
            for c in (c0 - 1, c0, c0 + 1):
                for o in grid.get((a, c), []):
                    d = meter(p["lat"], p["lon"], o["lat"], o["lon"])
                    if d <= 40 or (d <= 100 and set(p["sp"]) & set(o["sp"])): hit = o; break
                if hit: break
            if hit: break
        if hit:
            for s in p["sp"]:
                if s not in hit["sp"]: hit["sp"].append(s)
            hit["belag"] = hit["belag"] or p["belag"]; hit["licht"] = hit["licht"] or p["licht"]
            hit["verein"] = hit["verein"] or (p["verein"] and hit["zug"] is None)
            if hit["name"].endswith("-Fläche") and not p["name"].endswith("-Fläche"): hit["name"] = p["name"]
        else:
            orte.append(p); grid[(a0, c0)].append(p)
    orte.sort(key=lambda o: -o["lat"])
    return orte


# ------------------------------------------------------------------ Kurzform (wie sqlgen2.py)
SP = {"Fußball": "F", "Basketball": "B", "Tischtennis": "T", "Beachvolleyball": "V", "Skaten": "S", "Laufen": "L", "Calisthenics": "C"}
BE = {"Naturrasen": "n", "Kunstrasen": "k", "Beton/Asphalt": "a", "Asche": "c", "Tartan": "t", "Sand": "s"}
ZU = {"vermutlich öffentlich": "o", "vermutlich Schulhof": "s"}


def zeile(o):
    nm = "" if o["name"].endswith("-Fläche") else o["name"]
    g = o["grund"] or ["", ""]
    r = [nm, o["lat"], o["lon"], "".join(SP[s] for s in o["sp"]), BE.get(o["belag"], ""), 1 if o["licht"] else 0,
         ZU.get(o["zug"], ""), g[0], g[1] or "", 1 if o["verein"] else 0]
    while r and r[-1] in ("", 0) and len(r) > 4: r.pop()
    return r


# ------------------------------------------------------------------ Namen
def namen_auffrischen(s3, version):
    import urllib.parse
    import namen
    plaetze = []
    for pre in PLATZHALTER:
        letzte = ""
        while True:
            seite = api(f"orte?select=id,name,ort,lat,lon&name=like.{urllib.parse.quote(pre)}*"
                        f"&ort=not.like.*,*&doppelt_von=is.null&order=id&limit=1000"
                        + (f"&id=gt.{letzte}" if letzte else "")) or []
            plaetze += [o for o in seite if o["ort"] and o["name"] == pre + " " + o["ort"]]
            if len(seite) < 1000: break
            letzte = seite[-1]["id"]
    print(f"{len(plaetze)} Plätze mit Platzhalter-Namen")
    if not plaetze: return {}
    gefunden = namen.berechnen(s3, version, plaetze)
    daten = [[i, f, st] for i, (f, st) in gefunden.items()]
    summe = collections.Counter()
    for i in range(0, len(daten), TEIL):
        for z in api("rpc/platzfrei_namen_setzen", {"daten": daten[i:i + TEIL]}) or []:
            summe[z["was"]] += z["anzahl"]
    return dict(summe)


# ------------------------------------------------------------------ Ablauf
def main():
    if not URL or not KEY:
        sys.exit("SUPABASE_URL und SUPABASE_SERVICE_KEY fehlen (GitHub: Settings → Secrets → Actions).")
    s3 = fs.S3FileSystem(anonymous=True, region="us-west-2")
    version = neueste_version(s3)
    letzte = api("platzfrei_importlauf?select=stand&quelle=eq.overture&order=id.desc&limit=1") or []
    ergebnis = {}
    if letzte and letzte[0]["stand"] == version and not ERZWINGEN:
        print(f"Overture {version} ist schon eingespielt – kein neuer Import.")
    else:
        t0 = time.time()
        roh = overture_laden(s3, version)
        orte = zusammenfassen(einstufen(roh))
        print(f"{len(roh)} Overture-Zeilen → {len(orte)} Plätze in {time.time() - t0:.0f} s")
        summe = collections.Counter()
        for i in range(0, len(orte), TEIL):
            antwort = api("rpc/platzfrei_osm_import",
                          {"daten": [zeile(o) for o in orte[i:i + TEIL]], "stand": stand_text(version)})
            for z in antwort or []: summe[z["was"]] += z["anzahl"]
            print(f"  {min(i + TEIL, len(orte))}/{len(orte)}", flush=True)
        ergebnis = dict(summe)
        api("platzfrei_importlauf", {"quelle": "overture", "stand": version, "ergebnis": ergebnis})

    doppelte = {z["was"]: z["anzahl"] for z in api("rpc/doppelte_zusammenfuehren", {}) or []}
    api("platzfrei_importlauf", {"quelle": "doppelte", "stand": version, "ergebnis": doppelte})

    namen_erg = {}
    if ergebnis or NAMEN:
        namen_erg = namen_auffrischen(s3, version)
        api("platzfrei_importlauf", {"quelle": "namen", "stand": version, "ergebnis": namen_erg})
        print("Namen:", json.dumps(namen_erg, ensure_ascii=False, indent=1))

    print("\nImport:", json.dumps(ergebnis, ensure_ascii=False, indent=1))
    print("Doppelte:", json.dumps(doppelte, ensure_ascii=False, indent=1))
    neu = sum(v for k, v in ergebnis.items() if k.startswith("neu angelegt"))
    zusammenfassung = os.environ.get("GITHUB_STEP_SUMMARY")
    if zusammenfassung:
        with open(zusammenfassung, "a") as f:
            f.write(f"## Platzfrei-Import {stand_text(version)}\n\n**{neu} neue Plätze**\n\n")
            for k, v in sorted({**ergebnis, **doppelte, **namen_erg}.items(), key=lambda x: -x[1]):
                f.write(f"- {k}: {v}\n")


if __name__ == "__main__":
    main()
