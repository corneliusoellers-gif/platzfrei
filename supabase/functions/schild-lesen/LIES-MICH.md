# Schild-Foto lesen einrichten

Ohne diese Schritte läuft die App wie bisher: Das Schildfoto wird hochgeladen, aber nichts ausgelesen.

1. **API-Key holen:** https://console.anthropic.com → API Keys → „Create Key“. Unter Billing etwas Guthaben aufladen (5 € reichen lange) und am besten ein Monatslimit setzen.
2. **Key in Supabase hinterlegen:** Supabase → Edge Functions → Secrets → `ANTHROPIC_API_KEY` = der Key.
3. **SQL ausführen:** Supabase → SQL Editor → Inhalt von `supabase/schild-lesen.sql` einfügen → Run. Am Ende steht eine Zeile `schild_lesen_bremse`.
4. **Funktion anlegen:** Supabase → Edge Functions → „Deploy a new function“ → „Via Editor“. Name genau `schild-lesen`, Inhalt von `supabase/functions/schild-lesen/index.ts` einfügen, Deploy. Danach in den Einstellungen der Funktion **„Verify JWT“ ausschalten** (die App nutzt den Publishable Key, der ist kein JWT; die Bremse in der Funktion schützt stattdessen).
5. **Testen:** platzfrei.app im Handy öffnen, Platz antippen, „Schild fotografieren“. Nach ein paar Sekunden öffnet sich „Angaben ergänzen“ mit Zeiten und Zugang.

## Kosten und Grenzen
- Modell `claude-opus-5-5` mit niedrigem Aufwand, ungefähr 1–2 Cent pro Foto. Anderes Modell über das Secret `SCHILD_MODELL`.
- Bremse: 6 pro Stunde und 20 pro Tag je Anschluss, 300 pro Tag insgesamt (in `schild-lesen.sql` änderbar).
- Erlaubte Seiten: platzfrei.app (Secret `ERLAUBTE_URSPRUENGE` zum Ändern).

## Datenschutz
- Das Foto wird von der Funktion nicht gespeichert, nur an Anthropic weitergereicht.
- Auftragsverarbeitung: Anthropics DPA gilt für API-Kunden automatisch über die Commercial Terms. Bitte einmal prüfen, ob dir das reicht, und die Datenschutzerklärung (Abschnitt Fotos und Empfänger) gegenlesen.
