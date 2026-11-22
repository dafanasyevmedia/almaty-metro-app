/*
 * Service Worker — офлайн-доступ.
 *
 * Стиль тот же, что у index.html (var/function, без стрелочных функций и async/await) —
 * для совместимости со старыми WebView.
 *
 * Две стратегии кеша:
 *  - Страница и статичные ассеты (иконки, маскоты, шрифты) — stale-while-revalidate:
 *    сразу отдаётся кеш, свежая версия подтягивается в фоне. Код меняется редко, секунда
 *    устаревания не страшна ради мгновенной загрузки.
 *  - data/*.json (расписание, станции, праздники) — network-first с таймаутом:
 *    сначала сеть (важна свежесть), и только если она не ответила за NETWORK_TIMEOUT_MS
 *    или недоступна — кеш. Так у входа в метро при слабом сигнале не приходится ждать
 *    зависший запрос.
 *
 * CACHE_NAME поднимается при заметных изменениях набора закешированных файлов
 * (activate удаляет все кеши с другим именем).
 *
 * Шрифты (fonts/*.woff2) — same-origin файлы, отдельная логика для них не нужна:
 * их покрывают precache и stale-while-revalidate наравне с иконками.
 */

var CACHE_NAME = 'almaty-metro-v2';
var NETWORK_TIMEOUT_MS = 5000;

// Пути «данных» — для них network-first вместо stale-while-revalidate. Сравнение по
// вхождению в pathname, потому что URL запроса абсолютный.
var DATA_FILES = ['data/stations.json', 'data/schedule.json', 'data/holidays.json'];

// Версии favicon/apple-touch-icon (?v=N) должны совпадать со ссылками в <head> index.html.
var PRECACHE_URLS = [
  './',
  'index.html',
  'data/stations.json',
  'data/schedule.json',
  'data/holidays.json',
  'favicon-16x16.png?v=5',
  'favicon-32x32.png?v=5',
  'favicon.ico?v=5',
  'apple-touch-icon.png?v=5',
  'site.webmanifest',
  'android-chrome-192x192.png',
  'android-chrome-512x512.png',
  'mascot/logo.png',
  'mascot/idle.png',
  'mascot/searching.png',
  'mascot/far.png',
  'mascot/sleeping.png',
  'mascot/waving.png',
  'mascot/peek.png',
  'mascot/run1.png',
  'mascot/run2.png',
  'fonts/unbounded-latin.woff2',
  'fonts/unbounded-cyrillic.woff2',
  'fonts/inter-latin.woff2',
  'fonts/inter-cyrillic.woff2',
  'fonts/inter-cyrillic-ext.woff2'
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      // Promise.allSettled, а не cache.addAll(): addAll атомарный, и одна упавшая ссылка
      // сорвала бы весь precache; allSettled пропускает неудачные файлы, остальные кешируются.
      return Promise.allSettled(
        PRECACHE_URLS.map(function (url) { return cache.add(url); })
      );
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(
        names
          .filter(function (name) { return name !== CACHE_NAME; })
          .map(function (name) { return caches.delete(name); })
      );
    }).then(function () { return self.clients.claim(); })
  );
});

function isDataRequest(url) {
  return DATA_FILES.some(function (f) { return url.pathname.indexOf(f) !== -1; });
}

// Помечает ответ из офлайн-кеша заголовком X-From-SW-Cache, чтобы страница (fetchJSON в
// index.html) отличала «сети сейчас нет» от «расписание давно не обновлялось, но сеть есть».
function markAsFromCache(response) {
  return response.blob().then(function (body) {
    var headers = new Headers(response.headers);
    headers.set('X-From-SW-Cache', '1');
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers: headers
    });
  });
}

function networkFirst(request) {
  // cache:'no-store': JSON отдаётся без Cache-Control, и без этого браузер по эвристике
  // может не ходить в сеть вовсе, подставив свой HTTP-кеш; для расписания нужна реальная
  // попытка достучаться до сервера.
  return new Promise(function (resolve) {
    var settled = false;
    var timer = setTimeout(function () {
      if (settled) return;
      settled = true;
      caches.match(request).then(function (cached) {
        resolve(cached ? markAsFromCache(cached) : fetch(request.url, { cache: 'no-store' }));
      });
    }, NETWORK_TIMEOUT_MS);

    fetch(request.url, { cache: 'no-store' }).then(function (response) {
      if (settled) return; // таймаут уже сработал и увёл ответ в кеш — сеть опоздала
      settled = true;
      clearTimeout(timer);
      if (response && response.ok) {
        var copy = response.clone();
        caches.open(CACHE_NAME).then(function (cache) { cache.put(request, copy); });
      }
      resolve(response);
    }).catch(function () {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      caches.match(request).then(function (cached) {
        resolve(cached ? markAsFromCache(cached) : Response.error());
      });
    });
  });
}

function staleWhileRevalidate(request) {
  return caches.open(CACHE_NAME).then(function (cache) {
    return cache.match(request).then(function (cached) {
      var networkFetch = fetch(request).then(function (response) {
        if (response && response.ok) cache.put(request, response.clone());
        return response;
      }).catch(function () {
        return cached; // сети нет вообще — тихо остаёмся с тем, что уже отдали
      });
      return cached || networkFetch;
    });
  });
}

self.addEventListener('fetch', function (event) {
  var request = event.request;
  if (request.method !== 'GET') return; // POST формы обратной связи и т.п. не трогаем

  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return; // GA/Telegram/FormSubmit — не наша забота, свои шрифты уже same-origin

  if (isDataRequest(url)) {
    event.respondWith(networkFirst(request));
  } else {
    event.respondWith(staleWhileRevalidate(request));
  }
});
