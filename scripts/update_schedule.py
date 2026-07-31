#!/usr/bin/env python3
"""
Скачивает актуальное расписание с официального API Метрополитена Алматы
и сохраняет его в data/schedule.json в компактном формате для приложения.

Источник: https://metroalmaty.kz/ru/schedule (использует api.metroalmaty.kz)
Запускается вручную (`python3 scripts/update_schedule.py`) или раз в неделю
через GitHub Actions (.github/workflows/update-schedule.yml).
"""

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
    result = {
        "generated_at": datetime.now(tz=ALMATY_TZ).isoformat(),
        "source": f"{API_BASE}/schedule/{{slug}}",
        "stations": {},
    }

    max_order = max(s["order"] for s in stations)

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
            print(f"    !! {slug}: на сервере нет расписания на выходные — используем будни как оценку", file=sys.stderr)

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

        entry = {
            "weekday": {
                "forward": times("non_weekend", fwd_key),
                "backward": times("non_weekend", bwd_key),
            },
            "weekend": {
                "forward": times("weekend", fwd_key) if not weekend_missing else times("non_weekend", fwd_key),
                "backward": times("weekend", bwd_key) if not weekend_missing else times("non_weekend", bwd_key),
            },
        }
        if weekend_missing:
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
