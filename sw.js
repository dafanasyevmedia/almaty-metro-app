/*
 * Service Worker — офлайн-доступ (см. CLAUDE.md, "Офлайн-доступ").
 *
 * Написан в том же стиле, что и index.html (var/function, без стрелочных
 * функций и async/await) — сознательно, ради совместимости со старыми
 * WebView, где вообще может запуститься этот файл.
 *
 * Две разные стратегии кеша:
 *  - Сама страница и статичные ассеты (иконки/маскот) — stale-while-revalidate:
 *    мгновенно отдаём то, что есть в кеше, а свежую версию в фоне подтягиваем
 *    на будущее. Код меняется редко (руками, через git push), лишняя секунда
 *    устаревания не страшна ради мгновенной загрузки.
 *  - data/*.json (расписание/станции/праздники) — network-first с таймаутом:
 *    сначала всегда пробуем сеть (свежесть важнее), и только если сеть не
 *    ответила за NETWORK_TIMEOUT_MS (или явно недоступна) — берём кеш. Так
 *    честнее: если человек стоит у входа в метро с плохим сигналом, незачем
 *    ждать полминуты зависшего запроса ради вопроса "успею ли на поезд".
 *
 * Версию CACHE_NAME нужно поднимать при заметных изменениях набора
 * закешированных файлов (activate удаляет все кеши с другим именем).
 */

var CACHE_NAME = 'almaty-metro-v1';
var NETWORK_TIMEOUT_MS = 5000;

// Пути, которые SW считает "данными" — для них network-first вместо
// stale-while-revalidate. Матчим по concat, а не по точному equals — рабочий
// URL приходит абсолютным (со схемой и хостом).
var DATA_FILES = ['data/stations.json', 'data/schedule.json', 'data/holidays.json'];

// Версии favicon/apple-touch-icon (?v=N) должны совпадать с текущими ссылками
// в index.html <head> — при следующей смене иконок поднять и тут, и там.
var PRECACHE_URLS = [
  './',
  'index.html',
  'data/stations.json',
  'data/schedule.json',
  'data/holidays.json',
  'favicon-16x16.png?v=4',
  'favicon-32x32.png?v=4',
  'favicon.ico?v=4',
  'apple-touch-icon.png?v=4',
  'site.webmanifest',
  'android-chrome-192x192.png',
  'android-chrome-512x512.png',
  'mascot/logo.png',
  'mascot/idle.png',
  'mascot/searching.png',
  'mascot/far.png',
  'mascot/sleeping.png',
  'mascot/waving.png',
  'mascot/peek.png'
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      // Promise.allSettled, а не cache.addAll() — addAll() атомарный: одна
      // упавшая ссылка (опечатка в пути, временный сбой сети на установке)
      // обрушила бы весь precache. allSettled просто пропускает то, что не
      // получилось, остальное всё равно закешируется.
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

// Помечает ответ из офлайн-кеша заголовком X-From-SW-Cache, чтобы страница
// (см. fetchJSON в index.html) могла отличить "реально нет сети прямо
// сейчас" от "джоба обновления давно не запускалась, но сеть есть" — это
// два разных, независимых случая, и подменять одно другим нечестно.
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
  // cache:'no-store' — принципиально не даём браузеру тихо подсунуть свой
  // собственный HTTP-кеш вместо реальной попытки сети (Python-сервер в
  // scripts/update_schedule.py не шлёт Cache-Control, так что по умолчанию
  // браузер иногда решает, что файл ещё "не протух", и вовсе не пытается
  // достучаться до сети — а нам для расписания нужна гарантированно свежая
  // попытка, а не эвристика браузера).
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
  if (url.origin !== self.location.origin) return; // шрифты/GA/Telegram/FormSubmit — не наша забота

  if (isDataRequest(url)) {
    event.respondWith(networkFirst(request));
  } else {
    event.respondWith(staleWhileRevalidate(request));
  }
});
