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

        weekend_missing = not isinstance(payload.get("weekend"), dict)
        if weekend_missing:
            print(f"    !! {slug}: на сервере нет расписания на выходные — используем будни как оценку", file=sys.stderr)

        entry = {
            "weekday": {
                "forward": times("non_weekend", "direction1"),
                "backward": times("non_weekend", "direction2"),
            },
            "weekend": {
                "forward": times("weekend", "direction1") if not weekend_missing else times("non_weekend", "direction1"),
                "backward": times("weekend", "direction2") if not weekend_missing else times("non_weekend", "direction2"),
            },
        }
        if weekend_missing:
            entry["weekend_estimated"] = True
        result["stations"][slug] = entry

    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Готово: {OUTPUT_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
