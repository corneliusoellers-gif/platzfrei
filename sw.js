/* Platzfrei – Service Worker
   Sorgt dafür, dass die App nach dem ersten Besuch auch ohne Netz startet.
   Gespeichert werden nur die App-Dateien (Seite, Karten- und Datenbank-
   Bibliothek). Die Platzliste liegt getrennt im Browser (IndexedDB).
   Anfragen an die Datenbank, Kartenbilder, Wetter und Adresssuche gehen
   immer direkt ins Netz und werden hier nicht gespeichert.
   Bei Änderungen an dieser Datei die Versionsnummer erhöhen. */
const VERSION = "platzfrei-v1";
const BIBLIOTHEKEN = [
  "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css",
  "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js",
  "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.117.0/dist/umd/supabase.js"
];
const EIGENE = ["./", "manifest.webmanifest", "icon-192.png", "icon-512.png", "favicon.ico"];

self.addEventListener("install", e => {
  e.waitUntil((async () => {
    const c = await caches.open(VERSION);
    await Promise.all([
      ...EIGENE.map(u => c.add(u).catch(() => {})),
      ...BIBLIOTHEKEN.map(u => c.add(new Request(u, { mode:"cors" })).catch(() => {}))
    ]);
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", e => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== VERSION) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.method !== "GET") return;
  const url = new URL(r.url);

  /* Die Seite selbst: erst Netz (immer die neueste Fassung), ohne Netz die Kopie. */
  if (r.mode === "navigate") {
    e.respondWith((async () => {
      try {
        const antwort = await fetch(r);
        if (antwort.ok) (await caches.open(VERSION)).put("./", antwort.clone());
        return antwort;
      } catch {
        return (await caches.match("./")) || Response.error();
      }
    })());
    return;
  }

  /* Feste Bibliotheken und eigene Bilder: erst Kopie, sonst Netz. */
  const eigen = url.origin === self.location.origin && /\.(png|ico|webmanifest)$/.test(url.pathname);
  if (BIBLIOTHEKEN.includes(r.url) || eigen) {
    e.respondWith((async () => {
      const kopie = await caches.match(r);
      if (kopie) return kopie;
      const antwort = await fetch(r);
      if (antwort.ok) (await caches.open(VERSION)).put(r, antwort.clone());
      return antwort;
    })());
  }
  /* Alles andere (Datenbank, Kartenbilder, Wetter, Nominatim): nicht anfassen. */
});
