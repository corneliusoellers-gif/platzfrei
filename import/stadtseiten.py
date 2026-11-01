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

GRUPPEN = [  # (Schlüssel, Überschrift, Erklärung)
  ("frei", "Öffentlich – bestätigt", "Die Gemeinde oder ein Schild vor Ort sagt: hier darf jeder spielen."),
  ("vermutlich", "Vermutlich öffentlich", "Liegt in einem Park, auf einem Spielplatz oder gehört der Stadt. Noch nicht bestätigt."),
  ("schulhof", "Schulhöfe", "Oft nach Schulschluss, am Wochenende und in den Ferien offen. Schilder vor Ort beachten."),
  ("verein", "Vereinsgelände", "Gehört einem Verein. Nur nach Absprache oder zu bestimmten Zeiten."),
]

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
--tinte:#0F1A15;--tinte-2:#3D4B44;--tinte-3:#5C6B63;--kante:rgba(15,26,21,.09);
--frei:#0E7C43;--frei-bg:#E4F6EA;--absprache:#A8620B;--absprache-bg:#FDF0DC;
--verein:#6248B8;--verein-bg:#EFEBFB;--blau:#1F6FB2;--blau-bg:#E5F0FA;
--schrift:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI Variable','Segoe UI',system-ui,Roboto,'Helvetica Neue',sans-serif}
@media (prefers-color-scheme:dark){:root{--gruen:#2FBF85;--gruen-tief:#4FD39C;--gruen-zart:#11281F;--grund:#0C1210;
--flaeche:#151D1A;--tinte:#EAF1ED;--tinte-2:#BCC8C1;--tinte-3:#93A39A;--kante:rgba(234,241,237,.10);
--frei:#5FD394;--frei-bg:#123222;--absprache:#F0B45E;--absprache-bg:#33250F;--verein:#AE9BF2;--verein-bg:#241D3D;
--blau:#7DB8EA;--blau-bg:#14273A}}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--schrift);background:var(--grund);color:var(--tinte);font-size:15px;line-height:1.55;-webkit-text-size-adjust:100%}
a{color:var(--gruen-tief)}
.rahmen{max-width:880px;margin:0 auto;padding:0 16px}
header.kopf{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--grund) 88%,transparent);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--kante)}
header.kopf .rahmen{display:flex;align-items:center;justify-content:space-between;height:56px;gap:12px}
.marke{display:flex;align-items:center;gap:9px;text-decoration:none;color:var(--tinte);font-weight:800;font-size:18px;letter-spacing:-.01em}
.marke img{width:30px;height:30px;border-radius:8px}
.knopf{display:inline-block;background:var(--gruen);color:#fff;text-decoration:none;font-weight:700;font-size:14px;padding:9px 16px;border-radius:999px;white-space:nowrap}
.knopf:hover{background:var(--gruen-tief)}
.pfad{font-size:13px;color:var(--tinte-3);margin:22px 0 6px}
.pfad a{color:var(--tinte-3)}
h1{font-size:clamp(26px,5vw,36px);line-height:1.15;letter-spacing:-.02em;margin-bottom:10px}
.lead{font-size:17px;color:var(--tinte-2);max-width:62ch}
.zahlen{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:22px 0}
.zahl{background:var(--flaeche);border:1px solid var(--kante);border-radius:16px;padding:14px 16px}
.zahl b{display:block;font-size:26px;letter-spacing:-.02em}
.zahl span{font-size:13px;color:var(--tinte-3)}
.filter{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 6px}
.filter button{font:inherit;font-size:14px;font-weight:600;border:1px solid var(--kante);background:var(--flaeche);color:var(--tinte-2);padding:7px 13px;border-radius:999px;cursor:pointer}
.filter button[aria-pressed=true]{background:var(--gruen);border-color:var(--gruen);color:#fff}
section.gruppe{margin:30px 0}
section.gruppe h2{font-size:21px;letter-spacing:-.01em;display:flex;align-items:center;gap:10px}
.punkt{width:11px;height:11px;border-radius:50%;flex:none}
.g-frei .punkt{background:var(--frei)}.g-vermutlich .punkt{background:var(--blau)}
.g-schulhof .punkt{background:var(--absprache)}.g-verein .punkt{background:var(--verein)}
.erkl{font-size:14px;color:var(--tinte-3);margin:4px 0 12px}
ul.plaetze{list-style:none;display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
ul.plaetze li{background:var(--flaeche);border:1px solid var(--kante);border-radius:14px}
ul.plaetze a{display:block;padding:12px 14px;text-decoration:none;color:inherit;height:100%}
ul.plaetze a:hover{border-color:var(--gruen)}
ul.plaetze li:hover{border-color:var(--gruen)}
.pn{font-weight:700;font-size:15px;line-height:1.3;overflow-wrap:anywhere}
.pu{font-size:13px;color:var(--tinte-3);margin-top:2px;overflow-wrap:anywhere}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.tag{font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px;background:var(--gruen-zart);color:var(--gruen-tief)}
.tag.grau{background:var(--grund);color:var(--tinte-3);border:1px solid var(--kante)}
.mehr{margin-top:10px}
.mehr summary{cursor:pointer;font-weight:600;color:var(--gruen-tief);font-size:14px;padding:6px 0}
.hinweis{background:var(--flaeche);border:1px solid var(--kante);border-radius:16px;padding:16px 18px;margin:30px 0;color:var(--tinte-2)}
.hinweis h2{font-size:17px;color:var(--tinte);margin-bottom:6px}
.nachbarn{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.nachbarn a{font-size:14px;font-weight:600;text-decoration:none;background:var(--flaeche);border:1px solid var(--kante);padding:7px 12px;border-radius:999px;color:var(--tinte-2)}
.faq h3{font-size:16px;margin:16px 0 4px}
.faq p{color:var(--tinte-2)}
footer{border-top:1px solid var(--kante);margin-top:40px;padding:22px 0 40px;font-size:13px;color:var(--tinte-3)}
footer a{color:var(--tinte-3)}
footer .rahmen{display:flex;flex-wrap:wrap;gap:8px 18px}
.suche{width:100%;font:inherit;font-size:16px;padding:12px 14px;border-radius:12px;border:1px solid var(--kante);background:var(--flaeche);color:var(--tinte);margin:18px 0 6px}
.land h2{font-size:19px;margin:26px 0 10px}
.stadtliste{list-style:none;display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}
.stadtliste a{display:flex;justify-content:space-between;gap:8px;background:var(--flaeche);border:1px solid var(--kante);border-radius:12px;padding:10px 12px;text-decoration:none;color:var(--tinte);font-weight:600}
.stadtliste a span{color:var(--tinte-3);font-weight:500;font-size:13px}
[hidden]{display:none!important}
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
<a class="knopf" href="/">Karte öffnen</a>
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
    tags = "".join(f'<span class="tag">{e(s)}</span>' for s in sp)
    if o["untergrund"]: tags += f'<span class="tag grau">{e(o["untergrund"])}</span>'
    if o["beleuchtung"] == "ja": tags += '<span class="tag grau">Flutlicht</span>'
    if o["zeiten"] and o["zeiten"] != "unbekannt": tags += f'<span class="tag grau">{e(o["zeiten"])}</span>'
    u = unterzeile(o, stadt)
    sportdaten = " ".join(slug(s) for s in sp)
    return (f'<li data-sport="{sportdaten}"><a href="/#platz={e(o["id"])}">'
            f'<div class="pn">{e(o["name"] or "Platz ohne Namen")}</div>'
            + (f'<div class="pu">{e(u)}</div>' if u else "")
            + f'<div class="tags">{tags}</div></a></li>')

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
    t.append(f'<nav class="pfad"><a href="/">Platzfrei</a> › <a href="/bolzplaetze/">Städte</a> › {e(name)}</nav>')
    t.append(f"<h1>Bolzplätze und Sportplätze in {e(name)}</h1>")
    t.append(f'<p class="lead">Hier findest du {ZAHL(len(s["gut"]))} Plätze in {e(name)}{" (" + e(land) + ")" if land and land != name else ""}, '
             f"auf denen du Fußball, Basketball, Tischtennis und mehr spielen kannst. Platzfrei zeigt dir bei jedem Platz, ob du dort spielen darfst.</p>")
    t.append('<div class="zahlen">')
    for k, txt in (("frei", "bestätigt öffentlich"), ("vermutlich", "vermutlich öffentlich"), ("schulhof", "Schulhöfe")):
        t.append(f'<div class="zahl"><b>{ZAHL(len(gruppen[k]))}</b><span>{txt}</span></div>')
    if fb: t.append(f'<div class="zahl"><b>{ZAHL(fb)}</b><span>zum Fußballspielen</span></div>')
    t.append("</div>")
    if len(sport) > 1:
        t.append('<div class="filter" hidden><button aria-pressed="true" data-sport="">Alle</button>')
        for sp_, n in sport.most_common():
            t.append(f'<button aria-pressed="false" data-sport="{slug(sp_)}">{e(sp_)} <span style="opacity:.7">{n}</span></button>')
        t.append("</div>")
    for k, ueber, erkl in GRUPPEN:
        liste = sorted(gruppen[k], key=lambda o: sortkey(o, stadt))
        if not liste: continue
        t.append(f'<section class="gruppe g-{k}"><h2><span class="punkt"></span>{ueber} <span style="color:var(--tinte-3);font-weight:600">{len(liste)}</span></h2>')
        t.append(f'<p class="erkl">{erkl}</p><ul class="plaetze">')
        t += [karte(o, stadt) for o in liste[:60]]
        t.append("</ul>")
        if len(liste) > 60:
            t.append(f'<details class="mehr"><summary>Alle {len(liste)} anzeigen</summary><ul class="plaetze" style="margin-top:10px">')
            t += [karte(o, stadt) for o in liste[60:]]
            t.append("</ul></details>")
        t.append("</section>")
    if unklar:
        t.append(f'<div class="hinweis"><h2>Noch {ZAHL(unklar)} weitere Plätze</h2>'
                 f"Bei {ZAHL(unklar)} weiteren Plätzen in {e(name)} ist noch nicht klar, ob du dort spielen darfst. "
                 f'Du findest sie auf der <a href="/">Karte in Platzfrei</a>. Wenn du einen kennst, kannst du ihn dort bestätigen.</div>')
    t.append('<div class="hinweis faq"><h2>Häufige Fragen</h2>')
    for f, a in faq: t.append(f"<h3>{e(f)}</h3><p>{e(a)}</p>")
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
t.append('<nav class="pfad"><a href="/">Platzfrei</a> › Städte</nav>')
t.append("<h1>Bolzplätze und Sportplätze nach Stadt</h1>")
t.append(f'<p class="lead">{ZAHL(summe)} Plätze in {len(staedte)} Städten und Gemeinden, bei denen Platzfrei weiß oder vermutet, dass du dort spielen darfst.</p>')
t.append('<input class="suche" type="search" placeholder="Stadt suchen …" aria-label="Stadt suchen" hidden>')
for code, land in sorted(LAENDER.items(), key=lambda x: x[1]):
    liste = sorted((s for s in staedte if s["land"] == code), key=lambda s: s["name"])
    if not liste: continue
    t.append(f'<section class="land"><h2>{e(land)}</h2><ul class="stadtliste">')
    t += [f'<li data-name="{e(s["name"].lower())}"><a href="/bolzplaetze/{s["slug"]}/">{e(s["name"])}<span>{len(s["gut"])}</span></a></li>' for s in liste]
    t.append("</ul></section>")
t.append(FUSS)
t.append("""<script>
(function(){var i=document.querySelector('.suche');i.hidden=false;i.addEventListener('input',function(){
var q=i.value.trim().toLowerCase();document.querySelectorAll('.stadtliste li').forEach(function(li){
li.hidden=q&&li.dataset.name.indexOf(q)<0});document.querySelectorAll('section.land').forEach(function(s){
s.hidden=!s.querySelector('li:not([hidden])')})})})();
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
