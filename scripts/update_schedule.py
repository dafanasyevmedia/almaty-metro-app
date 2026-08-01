#!/usr/bin/env python3
"""
Скачивает актуальное расписание с официального API Метрополитена Алматы
и сохраняет его в data/schedule.json в компактном формате для приложения.

Источник: https://metroalmaty.kz/ru/schedule (использует api.metroalmaty.kz)
Запускается вручную (`python3 scripts/update_schedule.py`) или раз в неделю
через GitHub Actions (.github/workflows/update-schedule.yml).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

API_BASE = "https://api.metroalmaty.kz/api/v1"
ROOT = Path(__file__).resolve().parent.parent
STATIONS_FILE = ROOT / "data" / "stations.json"
OUTPUT_FILE = ROOT / "data" / "schedule.json"
ALMATY_TZ = timezone(timedelta(hours=5))
MAX_ATTEMPTS = 5

# API не отдаёт английские названия станций (только ru/kk через current_station_locales),
# поэтому держим их здесь вручную. Источник — англоязычная Википедия
# (https://en.wikipedia.org/wiki/Almaty_Metro), написания скопированы как есть.
EN_NAMES = {
    "RMBK": "Raiymbek batyr",
    "ZZ": "Zhibek Joly",
    "ALM": "Almaly",
    "ABA": "Abay",
    "BKNR": "Baikonur",
    "TEATR": "Auezov Theater",
    "ALA": "Alatau",
    "SRN": "Sayran",
    "MSK": "Moskva",
    "SA": "Saryarqa",
    "BM": "Bauyrjan Momyshuly",
}


def time_to_minutes(t: str) -> float:
    """Как timeStrToMinutes() в index.html: хвостовые поезда после полуночи
    (час < 3) считаются продолжением текущих суток, а не следующих."""
    h, m, s = (int(x) for x in t.split(":"))
    mins = h * 60 + m + s / 60
    if h < 3:
        mins += 24 * 60
    return mins


def minutes_to_time_str(mins: float) -> str:
    mins = mins % (24 * 60)
    total_seconds = round(mins * 60)
    h = (total_seconds // 3600) % 24
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def estimate_weekend_via_neighbor(own_weekday: list[str], neighbor_weekday: list[str], neighbor_weekend: list[str]) -> list[str] | None:
    """Оценивает расписание станции на выходные, когда сервер метро его не отдаёт.

    Вместо того чтобы просто копировать будние интервалы (они гуще настоящих
    выходных), считаем медианный сдвиг по времени в пути между этой станцией
    и соседней (по будним данным, где сверять есть с чем), и переносим этот
    сдвиг на реальное выходное расписание соседа. Физическое время в пути
    между двумя конкретными станциями от дня недели не зависит.
    """
    if not own_weekday or not neighbor_weekday or not neighbor_weekend:
        return None
    neighbor_minutes = sorted(time_to_minutes(t) for t in neighbor_weekday)
    offsets = []
    for t in own_weekday:
        om = time_to_minutes(t)
        nearest = min(neighbor_minutes, key=lambda nm: abs(nm - om))
        offsets.append(om - nearest)
    offsets.sort()
    median_offset = offsets[len(offsets) // 2]

    estimated = [minutes_to_time_str(time_to_minutes(t) + median_offset) for t in neighbor_weekend]
    estimated.sort(key=time_to_minutes)
    return estimated


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "almaty-metro-schedule-app/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_station_payload(slug: str) -> dict:
    """non_weekend иногда приходит null из-за временного сбоя API — перезапрашиваем.
    weekend у некоторых станций (напр. ABA) отсутствует на сервере стабильно —
    это не ретраится, а обрабатывается фолбэком в main()."""
    url = f"{API_BASE}/schedule/{slug}"
    last_payload = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        payload = fetch_json(url)["data"]
        last_payload = payload
        if isinstance(payload.get("non_weekend"), dict):
            return payload
        print(f"    ...{slug}: попытка {attempt}/{MAX_ATTEMPTS}, API вернул неполные будние данные, повтор", file=sys.stderr)
        time.sleep(1.5)
    raise RuntimeError(f"API не отдал будние расписание для {slug} за {MAX_ATTEMPTS} попыток: {last_payload}")


def main() -> int:
    stations = json.loads(STATIONS_FILE.read_text(encoding="utf-8"))
    stations_by_slug = {s["slug"]: s for s in stations}
    stations_by_order = {s["order"]: s for s in stations}
    result = {
        "generated_at": datetime.now(tz=ALMATY_TZ).isoformat(),
        "source": f"{API_BASE}/schedule/{{slug}}",
        "stations": {},
    }

    max_order = max(s["order"] for s in stations)

    # Проход 1: скачиваем и раскладываем сырые данные по станциям. Оценку
    # недостающих выходных откладываем на проход 2 — там она может понадобиться
    # станция, которая по порядку в списке идёт позже текущей (напр. для Абая
    # нужен Байконыр, а он в списке станций идёт после).
    raw = {}
    for st in sorted(stations, key=lambda s: s["order"]):
        slug = st["slug"]
        print(f"  {slug:6s} -> {API_BASE}/schedule/{slug}", file=sys.stderr)
        try:
            payload = fetch_station_payload(slug)
        except Exception as exc:  # noqa: BLE001
            print(f"ОШИБКА при загрузке {slug}: {exc}", file=sys.stderr)
            return 1

        def times(day_key: str, dir_key: str) -> list[str]:
            day = payload.get(day_key) or {}
            entries = day.get(dir_key) or []
            return [e["arrival_time"] for e in entries]

        # По умолчанию API отдаёт direction1 = в сторону Б.Момышулы (forward),
        # direction2 = в сторону Райымбек батыра (backward). Но у конечных станций
        # физически возможно только одно направление, и на практике API иногда
        # (подтверждено для Б.Момышулы) кладёт реальные рейсы не в тот индекс —
        # station1/station2 говорят одно, а данные лежат в другом direction.
        # Поэтому для конечных станций направление определяем не по номеру
        # direction, а по тому, где реально есть рейсы.
        fwd_key, bwd_key = "direction1", "direction2"
        if st["order"] in (1, max_order):
            d1_has_data = bool((payload.get("non_weekend") or {}).get("direction1"))
            only_dir_key = "direction1" if d1_has_data else "direction2"
            empty_dir_key = "direction2" if d1_has_data else "direction1"
            if st["order"] == 1:
                fwd_key, bwd_key = only_dir_key, empty_dir_key
            else:
                bwd_key, fwd_key = only_dir_key, empty_dir_key

        weekend_missing = not isinstance(payload.get("weekend"), dict)
        if weekend_missing:
            print(f"    !! {slug}: на сервере нет расписания на выходные, оценим по соседям", file=sys.stderr)

        # API отдаёт название станции на русском и казахском в current_station_locales —
        # используем это, чтобы data/stations.json содержал оба варианта для переключения
        # языка в приложении. st["name"] мутируется прямо в загруженном списке station,
        # который целиком перезаписывается в конце main().
        locales = payload.get("current_station_locales") or {}
        existing_name = st.get("name")
        existing_name_ru = existing_name.get("ru") if isinstance(existing_name, dict) else existing_name
        name_ru = locales.get("ru") or existing_name_ru or slug
        name_kk = locales.get("kk") or name_ru
        name_en = EN_NAMES.get(slug) or name_ru
        st["name"] = {"ru": name_ru, "kk": name_kk, "en": name_en}

        raw[slug] = {
            "order": st["order"],
            "weekday_forward": times("non_weekend", fwd_key),
            "weekday_backward": times("non_weekend", bwd_key),
            "weekend_forward": [] if weekend_missing else times("weekend", fwd_key),
            "weekend_backward": [] if weekend_missing else times("weekend", bwd_key),
            "weekend_missing": weekend_missing,
        }

    # Проход 2: заполняем недостающие выходные расписания оценкой по соседям
    # (см. estimate_weekend_via_neighbor), а если соседа с реальными выходными
    # данными нет — откатываемся на старый способ (будни как оценка).
    for slug, data in raw.items():
        if not data["weekend_missing"]:
            continue

        order = data["order"]
        fwd_neighbor = stations_by_order.get(order - 1)
        bwd_neighbor = stations_by_order.get(order + 1)

        estimated_forward = None
        if fwd_neighbor and not raw[fwd_neighbor["slug"]]["weekend_missing"]:
            estimated_forward = estimate_weekend_via_neighbor(
                data["weekday_forward"],
                raw[fwd_neighbor["slug"]]["weekday_forward"],
                raw[fwd_neighbor["slug"]]["weekend_forward"],
            )

        estimated_backward = None
        if bwd_neighbor and not raw[bwd_neighbor["slug"]]["weekend_missing"]:
            estimated_backward = estimate_weekend_via_neighbor(
                data["weekday_backward"],
                raw[bwd_neighbor["slug"]]["weekday_backward"],
                raw[bwd_neighbor["slug"]]["weekend_backward"],
            )

        if estimated_forward is not None and estimated_backward is not None:
            print(f"    -> {slug}: выходные оценены по соседям ({fwd_neighbor['slug']} / {bwd_neighbor['slug']})", file=sys.stderr)
            data["weekend_forward"] = estimated_forward
            data["weekend_backward"] = estimated_backward
        else:
            print(f"    -> {slug}: соседи тоже без выходных данных, используем будни как оценку", file=sys.stderr)
            data["weekend_forward"] = data["weekday_forward"]
            data["weekend_backward"] = data["weekday_backward"]

    for slug, data in raw.items():
        entry = {
            "weekday": {
                "forward": data["weekday_forward"],
                "backward": data["weekday_backward"],
            },
            "weekend": {
                "forward": data["weekend_forward"],
                "backward": data["weekend_backward"],
            },
        }
        if data["weekend_missing"]:
            entry["weekend_estimated"] = True
        result["stations"][slug] = entry

    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    STATIONS_FILE.write_text(
        json.dumps(stations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Готово: {OUTPUT_FILE}, {STATIONS_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
