const VERSION = "2026-10-28-1";
const CACHE_PREFIX = "quarterdeck-shell-";
const CACHE = CACHE_PREFIX + VERSION;
const OFFLINE_URL = "/offline.html";
const SHELL = [
  "/",
  "/static/index.html",
  OFFLINE_URL,
  "/manifest.webmanifest",
  "/favicon.ico",
  "/favicon.svg",
  "/favicon-16.png",
  "/favicon-32.png",
  "/favicon-48.png",
  "/favicon-256.png",
  "/apple-touch-icon.png",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-maskable-192.png",
  "/icon-maskable-512.png"
];

function isLoginPage(text) {
  return text.includes("Continue with Google");
}

async function precache() {
  const cache = await caches.open(CACHE);
  await Promise.all(SHELL.map(async path => {
    try {
      const response = await fetch(new Request(path, {cache: "reload"}));
      if (!response || !response.ok) return;
      if (path === "/") {
        const body = await response.clone().text();
        if (isLoginPage(body)) return;
      }
      await cache.put(path, response.clone());
    } catch (error) {
      return;
    }
  }));
}

self.addEventListener("install", event => {
  event.waitUntil(precache().then(() => {
    if (!self.registration.active) return self.skipWaiting();
  }));
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys
          .filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE)
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("message", event => {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

async function offlineShell(request) {
  const cache = await caches.open(CACHE);
  return (await cache.match(request, {ignoreSearch: true}))
    || (await cache.match("/static/index.html"))
    || (await cache.match("/"))
    || (await cache.match(OFFLINE_URL))
    || new Response("Quarterdeck is offline.", {
      status: 503,
      headers: {"Content-Type": "text/plain; charset=utf-8"}
    });
}

async function navigate(event, request) {
  try {
    return await fetch(request);
  } catch (error) {
    return offlineShell(request);
  }
}

async function shellAsset(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request, {ignoreSearch: true});
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response && response.ok) await cache.put(request, response.clone());
    return response;
  } catch (error) {
    return Response.error();
  }
}

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/ws/")) return;

  if (request.mode === "navigate") {
    event.respondWith(navigate(event, request));
    return;
  }

  if (url.pathname.startsWith("/api/")) {
    event.respondWith(fetch(request));
    return;
  }

  if (SHELL.includes(url.pathname)) {
    event.respondWith(shellAsset(request));
  }
});
