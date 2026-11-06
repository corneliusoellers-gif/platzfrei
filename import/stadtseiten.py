"""Platzfrei – Stadtseiten (platzfrei.app/bolzplaetze/<stadt>/)

Läuft jede Woche per GitHub Action (.github/workflows/stadtseiten.yml):
1. liest alle freigegebenen Plätze aus Supabase (nur lesen, öffentlicher Schlüssel),
2. holt die Gemeindegrenzen aus Overture Maps (OpenStreetMap) und ordnet jeden Platz einer Stadt zu,
3. schreibt für jede Stadt mit mindestens 10 Plätzen (öffentlich, vermutlich öffentlich,
   Schulhof, Verein) eine Seite, dazu die Übersicht, sitemap.xml und robots.txt.
Von Hand: python import/stadtseiten.py [Zielordner]   (Standard: Repo-Hauptordner)
"""
import json, os, re, sys, html, math, shutil, datetime, collections, urllib.request
import concurrent.futures as cf
import pyarrow.fs as pf, pyarrow.dataset as ds, pyarrow.compute as pc
import shapely
from shapely import wkb

SUPABASE_URL = os.environ.get("SUPABASE_URL") or "https://swagzrwtfbaxouunudsp.supabase.co"
OEFFENTLICHER_SCHLUESSEL = "sb_publishable_2ENolaXEdMAJASGfvodbXA_DKTxvwD1"  # steht auch in index.html

# ---------- 1. Plätze aus Supabase ----------
SPALTEN = "id,name,ort,lat,lon,sportarten,zugang,zeiten,bestaetigt_durch,untergrund,beleuchtung,verein,freigegeben,doppelt_von"
def seite(von):
    url = f"{SUPABASE_URL}/rest/v1/orte?select={SPALTEN}&order=id&offset={von}&limit=1000"
    anfrage = urllib.request.Request(url, headers={"apikey": OEFFENTLICHER_SCHLUESSEL})
    for versuch in range(4):
        try: return json.load(urllib.request.urlopen(anfrage, timeout=60))
        except Exception as fehler: letzter = fehler
    raise letzter
alle, von = [], 0
with cf.ThreadPoolExecutor(6) as ex:
    while True:
        teile = list(ex.map(seite, range(von, von + 6000, 1000)))
        for t in teile: alle += t
        if len(teile[-1]) < 1000: break
        von += 6000
print("Plätze gelesen:", len(alle))

# ---------- 2. Gemeindegrenzen aus Overture ----------
s3 = pf.S3FileSystem(anonymous=True, region="us-west-2")
release = sorted(x.path for x in s3.get_file_info(pf.FileSelector("overturemaps-us-west-2/release")))[-1]
flaechen = ds.dataset(f"{release}/theme=divisions/type=division_area/", filesystem=s3, format="parquet")
spalten = ["names", "subtype", "region", "geometry", "is_land"]
loc = flaechen.to_table(filter=(pc.field("country") == "DE") & (pc.field("subtype").isin(["locality", "county"])), columns=spalten).to_pylist()
reg = [x for x in flaechen.to_table(filter=(pc.field("country") == "DE") & (pc.field("subtype") == "region"), columns=spalten).to_pylist()
       if x["region"] in ("DE-BE", "DE-HH") and x["is_land"]]
print("Grenzen aus", release.split("/")[-1], ":", len(loc), "Flächen")

# Stadt eines Platzes: Berlin/Hamburg als Ganzes, sonst kreisfreie Stadt, sonst die größte Gemeinde-Fläche,
# in der er liegt (Verwaltungsverbände, Ämter und Bezirke zählen nicht).
geoms=[];info=[]
VERBUND=re.compile(r'^(VVG|GVV|Samtgemeinde|Verwaltungsgemeinschaft|Verbandsgemeinde|VG|Amt|Stadtbezirk|Ortsbeirat|Ortschaft|Gemarkung|Dorfgemeinschaft|Gemeindeverwaltungsverband)\b')
AEMTER={'Probstei','Schlei-Ostsee','Südtondern','Südeifel','Hanstein-Rusteberg','Langenlonsheim-Stromberg','Saarburg-Kell'}
loc=[x for x in loc if not (x['subtype']=='locality' and (VERBUND.match(x['names']['primary']) or re.search(r'VGem|\(VGem\)| Land$|-Land$',x['names']['primary']) or x['names']['primary'] in AEMTER))]
for x in loc:
    g=wkb.loads(x['geometry']); geoms.append(g)
    info.append((x['names']['primary'],x['subtype'],x['region'],g.area))
for x in reg:
    g=wkb.loads(x['geometry']); geoms.append(g)
    info.append(({'DE-BE':'Berlin','DE-HH':'Hamburg'}[x['region']],'stadtstaat',x['region'],g.area))
NICHT={'Barnim','Börde','Bautzen - Budyšin','Dahme-Spreewald','Elbe-Elster','Görlitz','Greiz','Havelland','Jerichower Land','Ludwigslust-Parchim','Mansfeld-Südharz','Mecklenburgische Seenplatte','Meißen','Mittelsachsen','Märkisch-Oderland','Nordsachsen','Nordwestmecklenburg','Oberhavel','Oberspreewald-Lausitz - Górne Błota-Łužyca','Oder-Spree','Ostprignitz-Ruppin','Potsdam-Mittelmark','Prignitz','Sächsische Schweiz-Osterzgebirge','Südwestpfalz','Teltow-Fläming','Uckermark','Vorpommern-Greifswald','Vorpommern-Rügen','Wittenberg','Stendal','Anhalt-Bitterfeld','Zwickau','Saalekreis','Altenburger Land','Spree-Neiße - Sprjewja-Nysa','Burgenlandkreis','Städteregion Aachen','Salzlandkreis'}
KREISFREI={x['names']['primary'] for x in loc if x['subtype']=='county'}
KREISFREI={n for n in KREISFREI if n not in NICHT and not re.search(r'[Kk]reis|Landkreis|Region|Land$',n)}
KREISFREI|={'Cottbus - Chóśebuz'}
tree=shapely.STRtree(geoms)
orte=[o for o in alle if o['freigegeben'] and not o['doppelt_von']]
pts=shapely.points([o['lon'] for o in orte],[o['lat'] for o in orte])
pi,gi=tree.query(pts,predicate='within')
best={}
for p,g in zip(pi,gi):
    n,st,rg,a=info[g]
    # Rang: Stadtstaat > Gemeinde(größte locality) ; county nur als Rückfall
    if st=='county' and n not in KREISFREI: continue
    rank=(3 if st=='stadtstaat' else 2 if st=='county' else 1, a)
    if p not in best or rank>best[p][0]: best[p]=(rank,n,rg,st)
lk={}
for p,g in zip(pi,gi):
    n,st,rg,a=info[g]
    if st=='county': lk[p]=n
out=[]
for i,o in enumerate(orte):
    b=best.get(i)
    o['stadt']=b[1] if b else None; o['land']=b[2] if b else None; o['kreis']=lk.get(i)
    out.append(o)

# ---------- 3. Seiten schreiben ----------

AUS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MIN_PLAETZE = 10
BASIS = "https://platzfrei.app"
HEUTE = datetime.date.today()
STAND = f"{HEUTE.day}.{HEUTE.month}.{HEUTE.year}"

LAENDER = {"DE-BW":"Baden-Württemberg","DE-BY":"Bayern","DE-BE":"Berlin","DE-BB":"Brandenburg",
  "DE-HB":"Bremen","DE-HH":"Hamburg","DE-HE":"Hessen","DE-MV":"Mecklenburg-Vorpommern",
  "DE-NI":"Niedersachsen","DE-NW":"Nordrhein-Westfalen","DE-RP":"Rheinland-Pfalz","DE-SL":"Saarland",
  "DE-SN":"Sachsen","DE-ST":"Sachsen-Anhalt","DE-SH":"Schleswig-Holstein","DE-TH":"Thüringen"}

GRUPPEN = [  # (Schlüssel, Überschrift, Erklärung) – gleiche Wörter und Farben wie in der App
  ("frei", "Frei zum Spielen", "Bestätigt: Die Gemeinde oder ein Schild vor Ort sagt, hier darf jeder spielen."),
  ("vermutlich", "Frei zum Spielen, noch nicht bestätigt", "Liegt in einem Park, auf einem Spielplatz oder gehört der Stadt."),
  ("schulhof", "Vorher fragen: Schulhöfe", "Oft nach Schulschluss, am Wochenende und in den Ferien offen. Schilder vor Ort beachten."),
  ("verein", "Vorher fragen: Vereinsgelände", "Gehört einem Verein. Nur nach Absprache oder zu bestimmten Zeiten."),
]
SPORT_ICON = {"Fußball":"⚽","Basketball":"🏀","Tischtennis":"🏓","Beachvolleyball":"🏐","Laufen":"🏃","Calisthenics":"💪","Skaten":"🛹"}

def lesbar(name):
    """Abkürzungen ausschreiben wie in der App ("Lothringerstr." -> "Lothringerstraße")."""
    name = re.sub(r"(\w)str\.(?=\s|,|$)", r"\1straße", name or "")
    name = re.sub(r"\bStr\.(?=\s|,|$)", "Straße", name)
    return re.sub(r"\bggü\.\s*", "gegenüber ", name).strip()

def gruppe(o):
    z = o["zugang"] or ""
    if z == "öffentlich": return "frei"
    if z == "vermutlich öffentlich": return "vermutlich"
    if "Schulhof" in z: return "schulhof"
    if "Verein" in z: return "verein"
    return None

def slug(s):
    s = s.lower()
    for a, b in (("ä","ae"),("ö","oe"),("ü","ue"),("ß","ss"),("é","e"),("è","e"),("ó","o"),("ś","s"),("ł","l")):
        s = s.replace(a, b)
    s = re.sub(r"\(([^)]*)\)", r"\1", s)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")

def anzeige_name(stadt):
    return stadt.split(" - ")[0]  # "Cottbus - Chóśebuz" -> "Cottbus"

def e(s): return html.escape(str(s), quote=True)

def unterzeile(o, stadt):
    ort = (o["ort"] or "").strip()
    teile = [t.strip() for t in ort.split(",") if t.strip()]
    if teile and (teile[0] == stadt or teile[0].startswith(stadt + "-")):
        rest = teile[0][len(stadt):].lstrip("-")
        teile = ([rest] if rest else []) + teile[1:]
    return ", ".join(teile)

def km(a, b):
    return math.hypot((a[0]-b[0])*111, (a[1]-b[1])*111*math.cos(math.radians(a[0])))

ZAHL = lambda n: f"{n:,}".replace(",", ".")

# ---------- Daten ----------
orte = out
proStadt = collections.defaultdict(list)
for o in orte:
    if not o["stadt"] or not o["land"]: continue
    if re.search(r"Forst|Park$|gemeindefrei", o["stadt"]): continue
    proStadt[(o["stadt"], o["land"])].append(o)

staedte = []
for (stadt, land), liste in proStadt.items():
    gut = [o for o in liste if gruppe(o)]
    if len(gut) < MIN_PLAETZE: continue
    lat = sum(o["lat"] for o in liste)/len(liste); lon = sum(o["lon"] for o in liste)/len(liste)
    staedte.append(dict(stadt=stadt, name=anzeige_name(stadt), land=land, alle=liste, gut=gut, mitte=(lat, lon)))
staedte.sort(key=lambda s: -len(s["gut"]))
belegt = set()
for s in staedte:  # gleicher Name: die größere Stadt bekommt den kurzen Link
    sl = slug(s["name"])
    if sl in belegt: sl = sl + "-" + s["land"][3:].lower()
    belegt.add(sl); s["slug"] = sl

# ---------- Gemeinsame Teile ----------
CSS = """
:root{--gruen:#0B9463;--gruen-tief:#087B51;--gruen-zart:#E7F5EE;--grund:#F6F8F6;--flaeche:#fff;
--tinte:#0F1A15;--tinte-2:#3D4B44;--tinte-3:#5C6B63;--kante:rgba(15,26,21,.08);
--frei:#0E7C43;--frei-bg:#E4F6EA;--absprache:#A8620B;--absprache-bg:#FDF0DC;
--p-gruen:#34C759;--p-orange:#FF9F0A;--p-grau:#8E8E93;
--schrift:-apple-system,BlinkMacSystemFont,'SF Pro Display','SF Pro Text','Segoe UI Variable','Segoe UI',system-ui,Roboto,'Helvetica Neue',sans-serif}
@media (prefers-color-scheme:dark){:root{--gruen:#2FBF85;--gruen-tief:#4FD39C;--gruen-zart:#11281F;--grund:#0C1210;
--flaeche:#151D1A;--tinte:#EAF1ED;--tinte-2:#BCC8C1;--tinte-3:#93A39A;--kante:rgba(234,241,237,.10);
--frei:#5FD394;--frei-bg:#123222;--absprache:#F0B45E;--absprache-bg:#33250F}}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-padding-top:120px}
body{font-family:var(--schrift);background:var(--grund);color:var(--tinte);font-size:15px;line-height:1.55;-webkit-text-size-adjust:100%;-webkit-font-smoothing:antialiased}
a{color:var(--gruen-tief)}
.rahmen{max-width:960px;margin:0 auto;padding:0 16px}
header.kopf{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--grund) 85%,transparent);backdrop-filter:saturate(180%) blur(14px);-webkit-backdrop-filter:saturate(180%) blur(14px);border-bottom:1px solid var(--kante)}
header.kopf .rahmen{display:flex;align-items:center;justify-content:space-between;height:58px;gap:12px}
.marke{display:flex;align-items:center;gap:9px;text-decoration:none;color:var(--tinte);font-weight:800;font-size:18px;letter-spacing:-.02em}
.marke img{width:30px;height:30px;border-radius:8px}
.knopf{display:inline-flex;align-items:center;gap:6px;background:var(--gruen);color:#fff;text-decoration:none;font-weight:700;font-size:14px;padding:10px 18px;border-radius:999px;white-space:nowrap}
.knopf:hover{background:var(--gruen-tief)}
.knopf.hell{background:#fff;color:#08573A}
.knopf.hell:hover{background:#F0FAF5}
.held{margin:20px 0 8px;padding:28px 24px 26px;border-radius:24px;color:#fff;
  background:radial-gradient(120% 140% at 100% 0%,#18B47A 0%,#0B9463 45%,#06603F 100%);box-shadow:0 18px 40px -22px rgba(6,96,63,.7)}
.pfad{font-size:13px;opacity:.85;margin-bottom:10px}
.pfad a{color:#fff}
h1{font-size:clamp(27px,5vw,40px);line-height:1.1;letter-spacing:-.025em;margin-bottom:10px}
.lead{font-size:17px;opacity:.92;max-width:62ch}
.held .suche{width:100%;font:inherit;font-size:16px;padding:14px 16px;border-radius:14px;border:0;background:#fff;color:#0F1A15;margin-top:18px;box-shadow:0 6px 18px -8px rgba(0,0,0,.35)}
.held-zahlen{display:flex;flex-wrap:wrap;gap:8px 26px;margin-top:18px}
.held-zahlen div b{display:block;font-size:26px;letter-spacing:-.02em;line-height:1.1}
.held-zahlen div span{font-size:13px;opacity:.85}
.held .knopf{margin-top:18px}
.zahlen{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:18px 0}
.zahl{background:var(--flaeche);border-radius:18px;padding:16px 18px;box-shadow:0 1px 2px rgba(15,26,21,.04),0 6px 18px -10px rgba(15,26,21,.12)}
.zahl b{display:flex;align-items:center;gap:8px;font-size:28px;letter-spacing:-.02em;line-height:1.15}
.zahl b i{width:10px;height:10px;border-radius:50%;background:var(--p-gruen)}
.zahl.orange b i{background:var(--p-orange)}.zahl.hell b i{background:transparent;box-shadow:inset 0 0 0 2px var(--p-gruen)}
.zahl span{display:block;font-size:13px;line-height:1.35;color:var(--tinte-3)}
.filter{position:sticky;top:58px;z-index:4;display:flex;gap:8px;overflow-x:auto;padding:10px 16px;margin:0 -16px;scrollbar-width:none;
  background:color-mix(in srgb,var(--grund) 88%,transparent);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
.filter::-webkit-scrollbar{display:none}
.filter button{flex:none;font:inherit;font-size:14px;font-weight:600;border:0;background:var(--flaeche);color:var(--tinte-2);padding:8px 14px;border-radius:999px;cursor:pointer;box-shadow:0 0 0 1px var(--kante)}
.filter button[aria-pressed=true]{background:var(--tinte);color:var(--grund);box-shadow:none}
section.gruppe{margin:26px 0}
section.gruppe h2{font-size:20px;letter-spacing:-.015em;display:flex;align-items:center;gap:10px}
section.gruppe h2 small{color:var(--tinte-3);font-weight:600;font-size:15px}
.punkt{width:11px;height:11px;border-radius:50%;flex:none;background:var(--p-gruen)}
.g-vermutlich .punkt{background:transparent;box-shadow:inset 0 0 0 2.5px var(--p-gruen)}
.g-schulhof .punkt,.g-verein .punkt{background:var(--p-orange)}
.erkl{font-size:14px;color:var(--tinte-3);margin:2px 0 10px}
ul.plaetze{list-style:none;display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:0 18px;background:var(--flaeche);border-radius:18px;padding:4px 16px;
  box-shadow:0 1px 2px rgba(15,26,21,.04),0 6px 18px -10px rgba(15,26,21,.12)}
ul.plaetze li{border-bottom:1px solid var(--kante)}
ul.plaetze a{display:flex;align-items:center;gap:12px;padding:12px 0;text-decoration:none;color:inherit;height:100%}
ul.plaetze a:hover .pn{color:var(--gruen-tief)}
.pi{width:38px;height:38px;border-radius:11px;flex:none;display:grid;place-items:center;font-size:18px;background:var(--frei-bg)}
.g-schulhof .pi,.g-verein .pi{background:var(--absprache-bg)}
.pt{min-width:0;flex:1}
.pn{display:block;font-weight:650;font-size:15px;line-height:1.3;overflow-wrap:anywhere}
.pu{display:block;font-size:13px;color:var(--tinte-3);margin-top:1px;overflow-wrap:anywhere}
.tags{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;margin-top:3px;font-size:12.5px;color:var(--tinte-3)}
.tags .sp{font-size:14px;letter-spacing:1px}
.tag{font-size:12px;font-weight:600;padding:1px 8px;border-radius:999px;background:var(--grund);color:var(--tinte-3)}
.mehr{margin-top:10px}
.mehr summary{cursor:pointer;font-weight:650;color:var(--gruen-tief);font-size:14px;padding:8px 2px;list-style:none}
.mehr summary::-webkit-details-marker{display:none}
.hinweis{background:var(--flaeche);border-radius:18px;padding:18px 20px;margin:26px 0;color:var(--tinte-2);box-shadow:0 1px 2px rgba(15,26,21,.04),0 6px 18px -10px rgba(15,26,21,.12)}
.hinweis h2{font-size:18px;color:var(--tinte);margin-bottom:6px;letter-spacing:-.01em}
.nachbarn{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.nachbarn a{font-size:14px;font-weight:600;text-decoration:none;background:var(--grund);padding:8px 13px;border-radius:999px;color:var(--tinte-2)}
.nachbarn a:hover{color:var(--gruen-tief)}
.faq details{border-top:1px solid var(--kante);padding:12px 0}
.faq details:first-of-type{border-top:0}
.faq summary{cursor:pointer;font-weight:650;color:var(--tinte);font-size:15.5px;list-style:none;display:flex;justify-content:space-between;gap:12px}
.faq summary::after{content:"+";color:var(--tinte-3);font-weight:500}
.faq details[open] summary::after{content:"–"}
.faq p{color:var(--tinte-2);margin-top:6px}
footer{border-top:1px solid var(--kante);margin-top:40px;padding:22px 0 40px;font-size:13px;color:var(--tinte-3)}
footer a{color:var(--tinte-3)}
footer .rahmen{display:flex;flex-wrap:wrap;gap:8px 18px}
h2.abschnitt{font-size:20px;letter-spacing:-.015em;margin:30px 0 12px}
.gross{list-style:none;display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
.gross a{display:block;text-decoration:none;color:var(--tinte);background:var(--flaeche);border-radius:18px;padding:16px 16px 14px;
  box-shadow:0 1px 2px rgba(15,26,21,.04),0 6px 18px -10px rgba(15,26,21,.12);transition:transform .18s}
.gross a:hover{transform:translateY(-2px)}
.gross b{display:block;font-size:17px;letter-spacing:-.01em}
.gross span{display:block;font-size:13px;color:var(--tinte-3)}
.gross em{display:block;font-style:normal;font-size:26px;font-weight:750;letter-spacing:-.02em;color:var(--gruen-tief);margin-top:6px}
.laender{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 4px}
.laender a{font-size:13.5px;font-weight:600;text-decoration:none;background:var(--flaeche);color:var(--tinte-2);padding:7px 12px;border-radius:999px;box-shadow:0 0 0 1px var(--kante)}
.laender a:hover{color:var(--gruen-tief)}
.land h2{font-size:19px;margin:28px 0 8px;letter-spacing:-.01em}
.stadtliste{list-style:none;columns:220px auto;column-gap:22px;background:var(--flaeche);border-radius:18px;padding:8px 18px;
  box-shadow:0 1px 2px rgba(15,26,21,.04),0 6px 18px -10px rgba(15,26,21,.12)}
.stadtliste li{break-inside:avoid;border-bottom:1px solid var(--kante)}
.stadtliste a{display:flex;justify-content:space-between;gap:8px;padding:9px 0;text-decoration:none;color:var(--tinte);font-weight:600}
.stadtliste a:hover{color:var(--gruen-tief)}
.stadtliste a span{color:var(--tinte-3);font-weight:500;font-size:13px;font-variant-numeric:tabular-nums}
.leer-suche{padding:16px 2px;color:var(--tinte-3)}
[hidden]{display:none!important}
@media (max-width:600px){.held{padding:22px 18px;border-radius:20px;margin-top:14px}.lead{font-size:16px}}
"""

def kopf(titel, beschreibung, url, jsonld):
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{e(titel)}</title>
<meta name="description" content="{e(beschreibung)}">
<link rel="canonical" href="{url}">
<meta name="theme-color" content="#0B9463">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="96x96" href="/favicon-96.png">
<link rel="apple-touch-icon" href="/icon-180.png">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Platzfrei">
<meta property="og:locale" content="de_DE">
<meta property="og:url" content="{url}">
<meta property="og:title" content="{e(titel)}">
<meta property="og:description" content="{e(beschreibung)}">
<meta property="og:image" content="{BASIS}/og-bild.png">
<link rel="stylesheet" href="/bolzplaetze/stil.css">
<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>
</head>
<body>
<header class="kopf"><div class="rahmen">
<a class="marke" href="/"><img src="/icon-192.png" alt="" width="30" height="30">Platzfrei</a>
<a class="knopf" href="/">Karte öffnen →</a>
</div></header>
<main class="rahmen">
"""

FUSS = f"""</main>
<footer><div class="rahmen">
<span>Stand: {STAND}</span>
<a href="/bolzplaetze/">Alle Städte</a>
<a href="/#impressum">Impressum</a>
<a href="/#datenschutz">Datenschutz</a>
<span>Platzdaten teils © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap-Mitwirkende</a> (ODbL) und amtliche Listen der Städte</span>
</div></footer>
"""

FILTER_JS = """<script>
(function(){var f=document.querySelector('.filter');if(!f)return;f.hidden=false;
f.addEventListener('click',function(ev){var b=ev.target.closest('button');if(!b)return;
f.querySelectorAll('button').forEach(function(x){x.setAttribute('aria-pressed',x===b)});
var s=b.dataset.sport;document.querySelectorAll('ul.plaetze li').forEach(function(li){
li.hidden=!!s&&(' '+li.dataset.sport+' ').indexOf(' '+s+' ')<0});
document.querySelectorAll('details.mehr').forEach(function(d){if(s)d.open=true});
document.querySelectorAll('section.gruppe').forEach(function(g){
g.hidden=!g.querySelector('ul.plaetze li:not([hidden])')})});})();
</script>"""

def karte(o, stadt):
    sp = o["sportarten"] or []
    icons = "".join(SPORT_ICON.get(x, "") for x in sp)
    zusatz = []
    if o["beleuchtung"] == "ja": zusatz.append('<span class="tag">Flutlicht</span>')
    if o["zeiten"] and o["zeiten"] != "unbekannt": zusatz.append(f'<span class="tag">{e(o["zeiten"])}</span>')
    if o["untergrund"]: zusatz.append(f'<span>{e(o["untergrund"])}</span>')
    u = unterzeile(o, stadt)
    sportdaten = " ".join(slug(x) for x in sp)
    return (f'<li data-sport="{sportdaten}"><a href="/#platz={e(o["id"])}">'
            f'<span class="pi" aria-hidden="true">{SPORT_ICON.get(sp[0], "⚽") if sp else "⚽"}</span><span class="pt">'
            f'<span class="pn">{e(lesbar(o["name"]) or "Platz ohne Namen")}</span>'
            + (f'<span class="pu">{e(lesbar(u))}</span>' if u else "")
            + f'<span class="tags"><span class="sp" title="{e(", ".join(sp))}">{icons}</span>{"".join(zusatz)}</span></span></a></li>')

def sortkey(o, stadt):
    return (unterzeile(o, stadt).lower(), (o["name"] or "").lower())

# ---------- Stadtseiten ----------
shutil.rmtree(f"{AUS}/bolzplaetze", ignore_errors=True)
os.makedirs(f"{AUS}/bolzplaetze")
open(f"{AUS}/bolzplaetze/stil.css", "w").write(CSS.strip() + "\n")

for s in staedte:
    stadt, name = s["stadt"], s["name"]
    gruppen = collections.defaultdict(list)
    for o in s["gut"]: gruppen[gruppe(o)].append(o)
    unklar = len(s["alle"]) - len(s["gut"])
    sport = collections.Counter(x for o in s["gut"] for x in (o["sportarten"] or []))
    fb = sport.get("Fußball", 0)
    url = f"{BASIS}/bolzplaetze/{s['slug']}/"
    titel = f"Bolzplätze & Sportplätze in {name} – wo darf ich spielen? | Platzfrei"
    beschr = (f"{ZAHL(len(s['gut']))} öffentliche Sportplätze, Bolzplätze und Schulhöfe in {name}"
              f"{f', davon {ZAHL(fb)} zum Fußballspielen' if fb else ''}. Mit Untergrund, Flutlicht und ob du dort spielen darfst.")
    nachbarn = sorted((x for x in staedte if x is not s), key=lambda x: km(x["mitte"], s["mitte"]))[:8]
    land = LAENDER.get(s["land"], "")
    faq = [
      (f"Wo kann ich in {name} kostenlos Fußball spielen?",
       f"Platzfrei kennt in {name} {ZAHL(len(gruppen['frei']) + len(gruppen['vermutlich']))} Plätze, die öffentlich oder vermutlich öffentlich sind"
       + (f", dazu {ZAHL(len(gruppen['schulhof']))} Schulhöfe" if gruppen['schulhof'] else "")
       + ". Die bestätigten stehen oben in der Liste."),
      ("Darf ich auf einem Schulhof spielen?",
       "Viele Schulhöfe sind nach Schulschluss, am Wochenende und in den Ferien offen. Das regelt jede Gemeinde selbst, achte deshalb auf die Schilder am Eingang."),
      ("Wie aktuell sind die Angaben?",
       f"Die Plätze stammen aus OpenStreetMap, aus amtlichen Listen und aus Antworten der Gemeinden. Diese Seite wurde am {STAND} erstellt. Wenn etwas nicht stimmt, kannst du es in der App melden."),
    ]
    jsonld = {"@context":"https://schema.org","@graph":[
      {"@type":"BreadcrumbList","itemListElement":[
        {"@type":"ListItem","position":1,"name":"Platzfrei","item":BASIS + "/"},
        {"@type":"ListItem","position":2,"name":"Städte","item":BASIS + "/bolzplaetze/"},
        {"@type":"ListItem","position":3,"name":name,"item":url}]},
      {"@type":"FAQPage","mainEntity":[{"@type":"Question","name":f,"acceptedAnswer":{"@type":"Answer","text":a}} for f, a in faq]}]}
    t = [kopf(titel, beschr, url, jsonld)]
    t.append('<div class="held">')
    t.append(f'<nav class="pfad"><a href="/">Platzfrei</a> › <a href="/bolzplaetze/">Städte</a> › {e(name)}</nav>')
    t.append(f"<h1>Bolzplätze und Sportplätze in {e(name)}</h1>")
    t.append(f'<p class="lead">{ZAHL(len(s["gut"]))} Plätze in {e(name)}{" (" + e(land) + ")" if land and land != name else ""}, '
             f"auf denen du Fußball, Basketball, Tischtennis und mehr spielen kannst. Bei jedem Platz steht, ob du dort spielen darfst.</p>")
    t.append('<a class="knopf hell" href="/">Auf der Karte ansehen →</a>')
    t.append("</div>")
    t.append('<div class="zahlen">')
    vorher = len(gruppen["schulhof"]) + len(gruppen["verein"])
    for k, n, txt in (("", len(gruppen["frei"]), "frei zum Spielen, bestätigt"),
                      ("hell", len(gruppen["vermutlich"]), "frei, noch nicht bestätigt"),
                      ("orange", vorher, "vorher fragen (Schule, Verein)")):
        t.append(f'<div class="zahl {k}"><b><i></i>{ZAHL(n)}</b><span>{txt}</span></div>')
    if fb: t.append(f'<div class="zahl"><b>⚽ {ZAHL(fb)}</b><span>zum Fußballspielen</span></div>')
    t.append("</div>")
    if len(sport) > 1:
        t.append('<div class="filter" hidden><button aria-pressed="true" data-sport="">Alle</button>')
        for sp_, n in sport.most_common():
            t.append(f'<button aria-pressed="false" data-sport="{slug(sp_)}">{e(sp_)} <span style="opacity:.7">{n}</span></button>')
        t.append("</div>")
    for k, ueber, erkl in GRUPPEN:
        liste = sorted(gruppen[k], key=lambda o: sortkey(o, stadt))
        if not liste: continue
        t.append(f'<section class="gruppe g-{k}"><h2><span class="punkt"></span>{ueber} <small>{len(liste)}</small></h2>')
        t.append(f'<p class="erkl">{erkl}</p><ul class="plaetze">')
        t += [karte(o, stadt) for o in liste[:60]]
        t.append("</ul>")
        if len(liste) > 60:
            t.append(f'<details class="mehr"><summary>Alle {len(liste)} anzeigen ↓</summary><ul class="plaetze" style="margin-top:10px">')
            t += [karte(o, stadt) for o in liste[60:]]
            t.append("</ul></details>")
        t.append("</section>")
    if unklar:
        t.append(f'<div class="hinweis"><h2>Noch {ZAHL(unklar)} weitere Plätze</h2>'
                 f"Bei {ZAHL(unklar)} weiteren Plätzen in {e(name)} ist noch nicht klar, ob du dort spielen darfst. "
                 f'Du findest sie auf der <a href="/">Karte in Platzfrei</a>. Wenn du einen kennst, kannst du ihn dort bestätigen.</div>')
    t.append('<div class="hinweis faq"><h2>Häufige Fragen</h2>')
    for f, a in faq: t.append(f"<details><summary>{e(f)}</summary><p>{e(a)}</p></details>")
    t.append("</div>")
    t.append('<div class="hinweis"><h2>Städte in der Nähe</h2><div class="nachbarn">')
    t += [f'<a href="/bolzplaetze/{x["slug"]}/">{e(x["name"])}</a>' for x in nachbarn]
    t.append("</div></div>")
    t.append(FUSS)
    if len(sport) > 1: t.append(FILTER_JS)
    t.append("</body>\n</html>\n")
    os.makedirs(f"{AUS}/bolzplaetze/{s['slug']}")
    open(f"{AUS}/bolzplaetze/{s['slug']}/index.html", "w").write("\n".join(t))

# ---------- Übersicht ----------
url = f"{BASIS}/bolzplaetze/"
summe = sum(len(s["gut"]) for s in staedte)
t = [kopf("Bolzplätze & Sportplätze nach Stadt | Platzfrei",
          f"Öffentliche Bolzplätze, Sportplätze und Schulhöfe in {len(staedte)} deutschen Städten und Gemeinden – und ob du dort spielen darfst.",
          url, {"@context":"https://schema.org","@type":"CollectionPage","name":"Bolzplätze nach Stadt","url":url})]
laender_da = [(c, l) for c, l in sorted(LAENDER.items(), key=lambda x: x[1]) if any(s["land"] == c for s in staedte)]
t.append('<div class="held">')
t.append('<nav class="pfad"><a href="/">Platzfrei</a> › Städte</nav>')
t.append("<h1>Bolzplätze und Sportplätze nach Stadt</h1>")
t.append(f'<p class="lead">Finde Plätze in deiner Stadt, auf denen du spielen darfst, und sieh auf einen Blick, wo du vorher fragen solltest.</p>')
t.append(f'<div class="held-zahlen"><div><b>{ZAHL(summe)}</b><span>Plätze</span></div><div><b>{len(staedte)}</b><span>Städte und Gemeinden</span></div>'
         f'<div><b>{len(laender_da)}</b><span>Bundesländer</span></div></div>')
t.append('<input class="suche" type="search" placeholder="Stadt suchen, z. B. Köln" aria-label="Stadt suchen" hidden>')
t.append("</div>")
t.append('<div class="vorne"><h2 class="abschnitt">Die meisten Plätze</h2><ul class="gross">')
t += [f'<li><a href="/bolzplaetze/{s["slug"]}/"><b>{e(s["name"])}</b><span>{e(LAENDER.get(s["land"], ""))}</span><em>{ZAHL(len(s["gut"]))}</em></a></li>' for s in staedte[:8]]
t.append("</ul>")
t.append('<h2 class="abschnitt">Nach Bundesland</h2><nav class="laender">')
t += [f'<a href="#{slug(l)}">{e(l)}</a>' for c, l in laender_da]
t.append("</nav></div>")
t.append('<p class="leer-suche" hidden>Keine Stadt gefunden. Auf der <a href="/">Karte</a> findest du alle Plätze.</p>')
for code, land in sorted(LAENDER.items(), key=lambda x: x[1]):
    liste = sorted((s for s in staedte if s["land"] == code), key=lambda s: s["name"])
    if not liste: continue
    t.append(f'<section class="land" id="{slug(land)}"><h2>{e(land)}</h2><ul class="stadtliste">')
    t += [f'<li data-name="{e(s["name"].lower())}"><a href="/bolzplaetze/{s["slug"]}/">{e(s["name"])}<span>{len(s["gut"])}</span></a></li>' for s in liste]
    t.append("</ul></section>")
t.append(FUSS)
t.append("""<script>
(function(){var i=document.querySelector('.suche');i.hidden=false;i.addEventListener('input',function(){
var q=i.value.trim().toLowerCase();document.querySelectorAll('.stadtliste li').forEach(function(li){
li.hidden=q&&li.dataset.name.indexOf(q)<0});var n=0;document.querySelectorAll('section.land').forEach(function(s){
s.hidden=!s.querySelector('li:not([hidden])');if(!s.hidden)n++});document.querySelector('.vorne').hidden=!!q;
document.querySelector('.leer-suche').hidden=!q||n>0})})();
</script>
</body>
</html>
""")
open(f"{AUS}/bolzplaetze/index.html", "w").write("\n".join(t))

# ---------- Sitemap & robots ----------
heute = HEUTE.isoformat()
urls = [f"{BASIS}/", f"{BASIS}/bolzplaetze/"] + [f"{BASIS}/bolzplaetze/{s['slug']}/" for s in staedte]
open(f"{AUS}/sitemap.xml", "w").write('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    + "".join(f"<url><loc>{u}</loc><lastmod>{heute}</lastmod></url>\n" for u in urls) + "</urlset>\n")
open(f"{AUS}/robots.txt", "w").write(f"User-agent: *\nAllow: /\n\nSitemap: {BASIS}/sitemap.xml\n")
print(len(staedte), "Städte,", summe, "Plätze")
