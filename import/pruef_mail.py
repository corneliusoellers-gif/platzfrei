"""Platzfrei: Mail schicken, wenn im Admin etwas Neues zu prüfen ist.

Fragt Supabase (RPC platzfrei_pruef_stand) und schickt nur dann eine Mail,
wenn seit der letzten Mail etwas dazugekommen ist. TEST=true schickt immer.
"""
import json, os, smtplib, ssl, sys, urllib.request, urllib.error
from email.message import EmailMessage
from email.utils import formataddr

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
ADRESSE = os.environ.get("MAIL_ADRESSE", "").strip()
PASSWORT = "".join(os.environ.get("MAIL_PASSWORT", "").split())  # Leerzeichen/Zeilenumbrüche raus
AN = os.environ.get("MAIL_AN") or ADRESSE
SERVER = os.environ.get("MAIL_SERVER") or "smtp.gmail.com"
PORT = int(os.environ.get("MAIL_PORT") or 587)
TEST = os.environ.get("TEST", "") in ("1", "true", "ja")


def rpc(name, daten=None):
    anfrage = urllib.request.Request(
        f"{URL}/rest/v1/rpc/{name}", data=json.dumps(daten or {}).encode(), method="POST",
        headers={"apikey": KEY, "Content-Type": "application/json",
                 **({} if KEY.startswith("sb_") else {"Authorization": "Bearer " + KEY})})
    try:
        with urllib.request.urlopen(anfrage, timeout=60) as a:
            text = a.read().decode()
            return json.loads(text) if text else None
    except urllib.error.HTTPError as f:
        sys.exit(f"Supabase-Fehler {f.code} bei {name}: {f.read().decode()}")


def esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    stand = rpc("platzfrei_pruef_stand")
    neu, liste = stand["neu"], stand["liste"]
    print(f"Neu: {neu}, offen: {sum(e['offen'] for e in liste)}")
    if neu == 0 and not TEST:
        return

    zeilen = [f"{e['was']}: {e['offen']} offen" + (f" ({e['neu']} neu)" if e["neu"] else "") for e in liste]
    text = "\n".join(zeilen or ["Gerade ist nichts offen."]) + "\n\nhttps://platzfrei.app/#admin\n"
    tabelle = "".join(
        f'<tr><td style="padding:6px 14px 6px 0">{esc(e["was"])}</td>'
        f'<td style="padding:6px 14px;text-align:right"><b>{e["offen"]}</b></td>'
        f'<td style="padding:6px 0;color:#1a7f37">{"+" + str(e["neu"]) + " neu" if e["neu"] else ""}</td></tr>'
        for e in liste) or "<tr><td>Gerade ist nichts offen.</td></tr>"
    html = ('<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:15px;color:#111">'
            f'<table style="border-collapse:collapse">{tabelle}</table>'
            '<p style="margin-top:20px"><a href="https://platzfrei.app/#admin" style="background:#1a7f37;'
            'color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none">Im Admin öffnen</a></p></div>')

    mail = EmailMessage()
    mail["Subject"] = f"Platzfrei: {neu} neu zu prüfen" if neu else "Platzfrei: Test-Mail"
    mail["From"] = formataddr(("Platzfrei", ADRESSE))
    mail["To"] = AN
    mail.set_content(text)
    mail.add_alternative(html, subtype="html")

    betreff = mail["Subject"]
    grund = mail_schicken(mail)
    if grund is None:
        print("Mail verschickt an", AN)
    else:
        print("Mail ging nicht:", grund)
        if not github_hinweis(betreff, text):
            sys.exit("Weder Mail noch GitHub-Hinweis hat geklappt.")
        print("Stattdessen GitHub-Hinweis angelegt (GitHub schickt dazu eine Mail).")
    rpc("platzfrei_pruef_gesendet", {"p_bis": stand["bis"], "p_mail": True})


def mail_schicken(mail):
    """Gibt None zurück, wenn es geklappt hat, sonst den Grund."""
    if "@" not in ADRESSE or not PASSWORT:
        return "Secret MAIL_ADRESSE oder MAIL_PASSWORT fehlt."
    print(f"Anmelden bei {SERVER} als {ADRESSE[:3]}…{ADRESSE[ADRESSE.index('@'):]}, Passwort {len(PASSWORT)} Zeichen")
    grund = None
    for port in (465, PORT):
        try:
            if port == 465:
                s = smtplib.SMTP_SSL(SERVER, 465, timeout=60, context=ssl.create_default_context())
            else:
                s = smtplib.SMTP(SERVER, port, timeout=60)
                s.starttls(context=ssl.create_default_context())
            with s:
                s.login(ADRESSE, PASSWORT)
                s.send_message(mail)
            return None
        except smtplib.SMTPAuthenticationError as f:
            return f"Gmail lehnt die Anmeldung ab ({f.smtp_code}): App-Passwort prüfen."
        except (smtplib.SMTPException, OSError) as f:
            grund = f"Port {port}: {f!r}"
            print(grund)
    return grund


def github_hinweis(betreff, text):
    """Ersatzweg: Kommentar in einem offenen Issue "Platzfrei: zu prüfen" (oder neues Issue).
    GitHub schickt dem Repo-Besitzer dafür eine Mail."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return False

    def gh(pfad, daten=None):
        a = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/{pfad}", method="POST" if daten else "GET",
            data=json.dumps(daten).encode() if daten else None,
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(a, timeout=30) as r:
            return json.loads(r.read().decode() or "null")

    try:
        titel = "Platzfrei: Neues zu prüfen"
        offen = [i for i in gh("issues?state=open&per_page=100")
                 if i["title"] == titel and "pull_request" not in i]
        inhalt = f"**{betreff}**\n\n" + text.replace("\n", "  \n")
        if offen:
            gh(f"issues/{offen[0]['number']}/comments", {"body": inhalt})
        else:
            gh("issues", {"title": titel, "body": inhalt + "\n\nSchließen, wenn erledigt. Beim nächsten Neuen kommt ein neues."})
        return True
    except Exception as f:
        print("GitHub-Hinweis ging nicht:", f)
        return False

if __name__ == "__main__":
    main()
