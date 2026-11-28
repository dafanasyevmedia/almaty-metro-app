#!/usr/bin/env python3
"""
Скачивает актуальное расписание с официального API Метрополитена Алматы
и сохраняет его в data/schedule.json в компактном формате для приложения.

Источник: https://metroalmaty.kz/ru/schedule (использует api.metroalmaty.kz)
Запускается вручную (`python3 scripts/update_schedule.py`) или через GitHub Actions
(.github/workflows/update-schedule.yml): почасово по запросу внешнего планировщика
(workflow_dispatch) плюс запасной встроенный cron.
"""

from __future__ import annotations

import bisect
import copy
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
DRIFT_LOG_FILE = ROOT / "data" / "schedule_drift_log.jsonl"
ALMATY_TZ = timezone(timedelta(hours=5))
MAX_ATTEMPTS = 5

# Выше скольки процентов разницы в числе рейсов (для одной станции/дня недели/
# направления, относительно прошлого часового снимка) считаем скачок подозрительным
# и НЕ применяем его — см. reject_anomalies(). Порог подобран по данным: временные
# глюки источника давали скачки от −34% до +87%, а реальное изменение графика — около 3%.
ANOMALY_THRESHOLD = 0.15

# Сколько станций может не ответить за один прогон: для них остаётся прежнее расписание
# (или рассчитанное по соседней, см. fill_failed_station), остальные обновляются. Если не ответило больше — это сбой источника в целом: прогон
# завершается ошибкой и ничего не записывает.
MAX_FAILED_STATIONS = 2

# Потолок времени в пути между соседними станциями (минуты) при сопоставлении рейсов.
MAX_SEGMENT_MIN = 8

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
    # Калкаман — ещё не открыта (pending:true в data/stations.json); slug "KLM" — из
    # списка станций API метро. Романизация — с казахской Википедии.
    "KLM": "Kalkaman",
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

    Поезда сопоставляются по порядковому номеру в отсортированном списке
    (n-й поезд дня у одной станции = n-й поезд дня у соседней), а не по
    "ближайшему по времени" — сопоставление по времени ловит коллизии в час
    пик, когда интервал между поездами (~6 мин) сопоставим со временем в
    пути между станциями (~2-3 мин): алгоритм иногда путает "этот же поезд"
    со следующим/предыдущим и получает сдвиг с обратным знаком. Проверено на
    реальных данных (станция Абая, направление на Райымбек батыра) — именно
    так ловился баг с неверным временем.
    """
    if not own_weekday or not neighbor_weekday or not neighbor_weekend:
        return None
    own_sorted = sorted(own_weekday, key=time_to_minutes)
    neighbor_sorted = sorted(neighbor_weekday, key=time_to_minutes)
    n = min(len(own_sorted), len(neighbor_sorted))
    if n == 0:
        return None
    offsets = sorted(
        time_to_minutes(own_sorted[i]) - time_to_minutes(neighbor_sorted[i])
        for i in range(n)
    )
    median_offset = offsets[len(offsets) // 2]

    estimated = [minutes_to_time_str(time_to_minutes(t) + median_offset) for t in neighbor_weekend]
    estimated.sort(key=time_to_minutes)
    return estimated


def estimate_from_neighbor(own_old: list[str], neighbor_old: list[str], neighbor_new: list[str], own_departs_later: bool) -> list[str] | None:
    """Переносит новое расписание соседней станции на станцию, которая не ответила.

    Время в пути берётся из прежнего снимка: для каждого рейса соседа ищется ближайшее
    отправление этой станции после него (own_departs_later) или до него, и берётся медиана
    разницы. Рейсы сопоставляются по времени, а не по порядковому номеру: число рейсов у
    соседних станций может различаться на один-два."""
    own = sorted(time_to_minutes(t) for t in own_old)
    offsets = []
    for t in (time_to_minutes(x) for x in neighbor_old):
        i = bisect.bisect_right(own, t) if own_departs_later else bisect.bisect_left(own, t) - 1
        if 0 <= i < len(own) and abs(own[i] - t) <= MAX_SEGMENT_MIN:
            offsets.append(own[i] - t)
    if not offsets:
        return None
    offset = sorted(offsets)[len(offsets) // 2]
    estimated = [minutes_to_time_str(time_to_minutes(t) + offset) for t in neighbor_new]
    return sorted(estimated, key=time_to_minutes)


def fill_failed_station(station: dict, old_stations: dict, new_stations: dict, stations_by_order: dict) -> dict:
    """Расписание станции, не ответившей в этом прогоне.

    Пока соседняя станция не изменилась, остаётся прежнее расписание — оно точнее расчёта.
    Если у соседа оно изменилось, список этой станции пересчитывается по нему; такие типы
    дней перечисляются в "neighbor_estimated" (приложение показывает пометку). Сосед,
    с которого отправляется тот же поезд раньше, имеет приоритет. new_stations содержит
    только ответившие станции."""
    entry = copy.deepcopy(old_stations[station["slug"]])
    recalculated = set()
    for daytype in ("weekday", "weekend"):
        for direction in ("forward", "backward"):
            own_old = (entry.get(daytype) or {}).get(direction) or []
            if not own_old:
                continue
            upstream_step = -1 if direction == "forward" else 1
            for step in (upstream_step, -upstream_step):
                neighbor = stations_by_order.get(station["order"] + step)
                if not neighbor:
                    continue
                slug = neighbor["slug"]
                neighbor_old = ((old_stations.get(slug) or {}).get(daytype) or {}).get(direction) or []
                neighbor_new = ((new_stations.get(slug) or {}).get(daytype) or {}).get(direction) or []
                if not neighbor_old or not neighbor_new:
                    continue
                if neighbor_new != neighbor_old:
                    estimated = estimate_from_neighbor(own_old, neighbor_old, neighbor_new, step == upstream_step)
                    if estimated:
                        entry[daytype][direction] = estimated
                        recalculated.add(daytype)
                break  # решает первый сосед, по которому есть данные
    if recalculated:
        entry["neighbor_estimated"] = sorted(recalculated | set(entry.get("neighbor_estimated", [])))
    return entry


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


def reject_anomalies(new_stations: dict, old_stations: dict) -> list[dict]:
    """Ловит подозрительные скачки ЧИСЛА рейсов (станция/день недели/направление)
    относительно прошлого часового снимка и откатывает конкретно их на старое
    значение — ПРЯМО В new_stations (мутирует), прежде чем результат уйдёт в
    diff_schedules()/запись файла. Так аномалия не попадает на живой сайт сама
    по себе — только после того, как кто-то посмотрит на неё в
    schedule_drift_log.jsonl (поле "anomalies" у строки прогона, см. log_drift())
    и либо решит, что это реальное изменение (тогда следующий же час, когда
    источник снова отдаст то же самое, порог всё равно будет превышен и снимок
    опять откатится — "ручное подтверждение" в этом смысле означает не "нажать
    кнопку", а руками разобраться и при необходимости подправить данные/порог,
    не полагаться, что само рассосётся), либо (как было в реальном инциденте
    2026-09-24..2026-10-02, см. ANOMALY_THRESHOLD) — что это временный глюк
    источника, и ничего руками трогать не нужно, он сам откатится обратно.

    Намеренно НЕ пытается быть умнее (не копит историю между запусками, не
    требует повторного подтверждения на следующий час) — простой вариант,
    выбранный явно вместо автоподтверждения по двум похожим снимкам подряд."""
    anomalies = []
    for slug, new_entry in new_stations.items():
        old_entry = old_stations.get(slug)
        if not old_entry:
            continue  # новая станция — не с чем сравнивать, не её случай
        for daytype in ("weekday", "weekend"):
            for direction in ("forward", "backward"):
                old_list = ((old_entry.get(daytype) or {}).get(direction)) or []
                new_list = ((new_entry.get(daytype) or {}).get(direction)) or []
                old_count = len(old_list)
                new_count = len(new_list)
                if old_count == 0:
                    continue  # не с чем сравнивать в процентах — не её случай
                pct_change = abs(new_count - old_count) / old_count
                if pct_change <= ANOMALY_THRESHOLD:
                    continue
                anomalies.append({
                    "station": slug,
                    "daytype": daytype,
                    "direction": direction,
                    "old_count": old_count,
                    "new_count": new_count,
                    "pct_change": round(pct_change, 3),
                })
                new_entry[daytype][direction] = old_list  # откат — новое не применяем
    return anomalies


def diff_schedules(old_stations: dict, new_stations: dict) -> list[dict]:
    """Сравнивает предыдущий и только что скачанный schedule.json (по станциям/
    дню недели/направлению) — диагностика для отслеживания того, насколько часто
    и насколько сильно сам metroalmaty.kz меняет расписание на ещё не наступившие
    часы дня (metroalmaty.kz пересчитывает ещё не наступившую часть дня). Возвращает список найденных расхождений;
    пустой список — ничего не изменилось с прошлого запуска."""
    changes = []
    for slug, new_entry in new_stations.items():
        old_entry = old_stations.get(slug)
        if not old_entry:
            continue  # новая станция в данных — сравнивать не с чем
        for daytype in ("weekday", "weekend"):
            for direction in ("forward", "backward"):
                old_list = ((old_entry.get(daytype) or {}).get(direction)) or []
                new_list = ((new_entry.get(daytype) or {}).get(direction)) or []
                if old_list == new_list:
                    continue
                first_diff = next(
                    (i for i in range(min(len(old_list), len(new_list))) if old_list[i] != new_list[i]),
                    None,
                )
                changes.append({
                    "station": slug,
                    "daytype": daytype,
                    "direction": direction,
                    "old_count": len(old_list),
                    "new_count": len(new_list),
                    "first_diff_index": first_diff,
                    "old_at_first_diff": old_list[first_diff] if first_diff is not None else None,
                    "new_at_first_diff": new_list[first_diff] if first_diff is not None else None,
                })
    return changes


def log_drift(changes: list[dict], anomalies: list[dict] | None = None, failed: list[str] | None = None) -> None:
    """Дописывает одну строку JSON (JSONL) за запуск — есть расхождения или нет.
    Одна строка на весь прогон, а не на каждую станцию/направление (11 станций × 2 дня
    недели × 2 направления = 44 проверки). Запись есть и без расхождений — иначе пропуск
    в логе нельзя отличить от незапустившейся джобы.

    `anomalies` (см. reject_anomalies()) — отдельное поле, не часть `changes`: то, что
    было отклонено этим прогоном (откат делает новый снимок равным старому, поэтому в
    `changes` оно не попадает) и ждёт ручной проверки.

    `failed` — станции, не ответившие в этом прогоне: их расписание прежнее или рассчитано
    по соседней станции."""
    entry = {
        "checked_at": datetime.now(tz=ALMATY_TZ).isoformat(),
        "changed_count": len(changes),
        "changes": changes,
        "anomalies": anomalies or [],
        "failed": failed or [],
    }
    with DRIFT_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if anomalies:
        print(f"  !!! АНОМАЛИЯ: {len(anomalies)} скачков отклонено, оставлено прежнее значение — проверить руками (см. {DRIFT_LOG_FILE.name})", file=sys.stderr)
    if changes:
        print(f"  !! Расхождения с предыдущим снимком: {len(changes)} (см. {DRIFT_LOG_FILE.name})", file=sys.stderr)
    elif not anomalies:
        print("  Расхождений с предыдущим снимком не обнаружено", file=sys.stderr)


def main() -> int:
    stations = json.loads(STATIONS_FILE.read_text(encoding="utf-8"))
    stations_by_slug = {s["slug"]: s for s in stations}
    # pending:true — станция заведена в data/stations.json, но ещё не открыта и в API метро
    # её нет. Такие станции исключаются отовсюду (определение конечной по max_order, поиск
    # соседа для оценки выходных, скачивание): иначе запрос к несуществующему slug прервал бы
    # весь прогон для всех станций, а max_order сместился бы раньше времени и сбил определение
    # direction1/2 у текущей конечной (Б. Момышулы).
    active_stations = [s for s in stations if not s.get("pending")]
    stations_by_order = {s["order"]: s for s in active_stations}
    result = {
        "generated_at": datetime.now(tz=ALMATY_TZ).isoformat(),
        "source": f"{API_BASE}/schedule/{{slug}}",
        "stations": {},
    }

    max_order = max(s["order"] for s in active_stations)

    # Прошлый снимок: источник прежних данных для не ответивших станций и база для сравнения.
    old_stations: dict = {}
    if OUTPUT_FILE.exists():
        try:
            old_stations = json.loads(OUTPUT_FILE.read_text(encoding="utf-8")).get("stations", {})
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  (не удалось прочитать предыдущий снимок: {exc})", file=sys.stderr)

    for st in stations:
        if st.get("pending"):
            print(f"  {st['slug']:6s} -> пропускаем (pending: станция ещё не открыта)", file=sys.stderr)

    # Проход 1: скачиваем и раскладываем сырые данные по станциям. Оценку
    # недостающих выходных откладываем на проход 2 — там она может понадобиться
    # станция, которая по порядку в списке идёт позже текущей (напр. для Абая
    # нужен Байконыр, а он в списке станций идёт после).
    raw = {}
    failed: list[str] = []
    for st in sorted(active_stations, key=lambda s: s["order"]):
        slug = st["slug"]
        print(f"  {slug:6s} -> {API_BASE}/schedule/{slug}", file=sys.stderr)
        try:
            payload = fetch_station_payload(slug)
        except Exception as exc:  # noqa: BLE001
            print(f"ОШИБКА при загрузке {slug}: {exc}", file=sys.stderr)
            failed.append(slug)
            if slug not in old_stations:
                print(f"Для {slug} нет прежнего расписания — прогон прерван", file=sys.stderr)
                return 1
            if len(failed) > MAX_FAILED_STATIONS:
                print(f"Не ответили станции: {', '.join(failed)} — больше {MAX_FAILED_STATIONS}, прогон прерван", file=sys.stderr)
                return 1
            # ::warning:: — предупреждение в интерфейсе GitHub Actions без падения прогона.
            print(f"::warning::{slug}: станция не ответила, расписание прежнее или рассчитано по соседней")
            continue

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
    # данными нет — используем будни как оценку. Если сосед не ответил в этом прогоне,
    # оставляем прежнюю оценку выходных.
    for slug, data in raw.items():
        if not data["weekend_missing"]:
            continue

        order = data["order"]
        fwd_neighbor = stations_by_order.get(order - 1)
        bwd_neighbor = stations_by_order.get(order + 1)

        estimated_forward = None
        if fwd_neighbor and fwd_neighbor["slug"] in raw and not raw[fwd_neighbor["slug"]]["weekend_missing"]:
            estimated_forward = estimate_weekend_via_neighbor(
                data["weekday_forward"],
                raw[fwd_neighbor["slug"]]["weekday_forward"],
                raw[fwd_neighbor["slug"]]["weekend_forward"],
            )

        estimated_backward = None
        if bwd_neighbor and bwd_neighbor["slug"] in raw and not raw[bwd_neighbor["slug"]]["weekend_missing"]:
            estimated_backward = estimate_weekend_via_neighbor(
                data["weekday_backward"],
                raw[bwd_neighbor["slug"]]["weekday_backward"],
                raw[bwd_neighbor["slug"]]["weekend_backward"],
            )

        if estimated_forward is not None and estimated_backward is not None:
            print(f"    -> {slug}: выходные оценены по соседям ({fwd_neighbor['slug']} / {bwd_neighbor['slug']})", file=sys.stderr)
            data["weekend_forward"] = estimated_forward
            data["weekend_backward"] = estimated_backward
            continue

        old_weekend = (old_stations.get(slug) or {}).get("weekend")
        neighbor_failed = any(n and n["slug"] in failed for n in (fwd_neighbor, bwd_neighbor))
        if neighbor_failed and old_weekend:
            print(f"    -> {slug}: сосед не ответил, оставлена прежняя оценка выходных", file=sys.stderr)
            data["weekend_forward"] = old_weekend["forward"]
            data["weekend_backward"] = old_weekend["backward"]
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

    # Подозрительные скачки откатываются до расчёта не ответивших станций, чтобы расчёт
    # не опирался на них, и до diff_schedules(): неоткатанная аномалия попала бы в файл как
    # обычное изменение.
    anomalies = reject_anomalies(result["stations"], old_stations)

    failed_entries = {
        slug: fill_failed_station(stations_by_slug[slug], old_stations, result["stations"], stations_by_order)
        for slug in failed
    }
    result["stations"].update(failed_entries)
    # Станции пишутся в порядке линии независимо от того, кто из них ответил.
    result["stations"] = {
        s["slug"]: result["stations"][s["slug"]]
        for s in sorted(active_stations, key=lambda s: s["order"])
    }

    # Дрейф относительно того, что лежало в schedule.json до перезаписи, пишется в лог.
    if old_stations:
        try:
            log_drift(diff_schedules(old_stations, result["stations"]), anomalies, failed)
        except OSError as exc:
            print(f"  (не удалось записать лог расхождений: {exc})", file=sys.stderr)

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
