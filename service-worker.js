const CACHE='mtb-trails-v7';
const APP_FILES=[
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-192.svg",
  "./icon-512.svg",
  "./data-meta.json",
  "./trails-01.geojson",
  "./trails-02.geojson",
  "./trails-03.geojson",
  "./trails-04.geojson",
  "./trails-05.geojson",
  "./trails-06.geojson",
  "./trails-07.geojson",
  "./trails-08.geojson",
  "./trails-09.geojson",
  "./trails-10.geojson",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
  "https://unpkg.com/leaflet-rotate@0.2.7/dist/leaflet-rotate-src.js"
];

self.addEventListener('install',event=>{
  event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(APP_FILES)).catch(()=>{}));
  self.skipWaiting();
});

self.addEventListener('activate',event=>{
  event.waitUntil(caches.keys().then(keys=>Promise.all(
    keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))
  )));
  self.clients.claim();
});

self.addEventListener('fetch',event=>{
  if(event.request.method!=='GET') return;
  event.respondWith(
    caches.match(event.request).then(cached=>{
      const network=fetch(event.request).then(resp=>{
        if(resp && resp.status===200 && resp.type!=='opaque'){
          const copy=resp.clone();
          caches.open(CACHE).then(c=>c.put(event.request,copy));
        }
        return resp;
      }).catch(()=>cached);
      return cached || network;
    })
  );
});
