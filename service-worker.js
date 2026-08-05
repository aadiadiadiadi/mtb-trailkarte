const APP_VERSION = "2.3.0";
const STATIC_CACHE = `mtb-trailkarte-static-${APP_VERSION}`;
const DATA_CACHE = `mtb-trailkarte-data-${APP_VERSION}`;

const STATIC_FILES = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-192.svg",
  "./icon-512.svg",
  "./data-meta.json"
];

self.addEventListener("install", event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(STATIC_CACHE).then(cache => cache.addAll(STATIC_FILES))
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys
          .filter(key => key.startsWith("mtb-trailkarte-") && ![STATIC_CACHE, DATA_CACHE].includes(key))
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  if(event.request.method !== "GET") return;

  const url = new URL(event.request.url);
  if(url.origin !== self.location.origin) return;

  const isNavigation = event.request.mode === "navigate";
  const isGeoJson = url.pathname.endsWith(".geojson");
  const isElevationProfile = url.pathname.endsWith("/poc-output/elevation-profiles.json");
  const isMetadata = url.pathname.endsWith("data-meta.json");

  if(isNavigation || isMetadata || isGeoJson){
    event.respondWith(networkFirst(event.request, isGeoJson ? DATA_CACHE : STATIC_CACHE));
    return;
  }

  if(isElevationProfile){
    event.respondWith(cacheFirstWithRefresh(event.request, DATA_CACHE));
    return;
  }

  event.respondWith(cacheFirstWithRefresh(event.request, STATIC_CACHE));
});

async function networkFirst(request, cacheName){
  try{
    const response = await fetch(request, {cache:"no-store"});
    if(response && response.ok){
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  }catch{
    const cached = await caches.match(request);
    return cached || caches.match("./index.html");
  }
}

async function cacheFirstWithRefresh(request, cacheName){
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);

  const refresh = fetch(request).then(response => {
    if(response && response.ok) cache.put(request, response.clone());
    return response;
  }).catch(() => null);

  return cached || refresh || new Response("Offline nicht verfügbar", {status:503});
}
