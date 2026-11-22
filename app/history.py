"""Вкладка «Історія» на дашборді: за обраний період — графік ДТЕК і фактичні відключення, прогноз погоди і датчик.

/api/range?from=YYYY-MM-DD&to=YYYY-MM-DD (обидві дати включно, місцеві доби).
Короткий період (≤ DETAIL_DAYS) — подробиці по днях: 48 півгодин графіка, годинний прогноз, ряди датчика.
Довший — підсумки по днях: години без світла (план/факт), мін/макс/середнє температури (прогноз/датчик), опади."""
import time
from datetime import date, datetime, timedelta

import schedule
import weather
from devices import local_tz

MAX_DAYS = 400
DETAIL_DAYS = 7
H = 3_600_000


def _day_ms(d, cfg):
    return int(datetime(d.year, d.month, d.day, tzinfo=local_tz(cfg)).timestamp() * 1000)


def _overlap(a, b, lo, hi):
    return max(0, min(b, hi) - max(a, lo))


def alert_segments(events, meta, now):
    """Тривоги по регіонах: {регіон: [[початок, кінець, "red"|"yellow"], …]} — зміна рівня ділить тривогу на відрізки."""
    segs, open_ = {}, {}
    for e in sorted(events, key=lambda e: e.get("ts", 0)):
        k, reg = e.get("kind"), e.get("region")
        if not reg or not str(k).startswith("air_alert"):
            continue
        if k == "air_alert_start":
            open_[reg] = [e.get("start") or e["ts"], e.get("level") or "red"]
        elif k == "air_alert_level":
            if reg in open_:
                a, lvl = open_[reg]
                segs.setdefault(reg, []).append([a, e["ts"], lvl])
            open_[reg] = [e["ts"], e.get("level") or "red"]
        elif k == "air_alert_end":
            a, lvl = open_.pop(reg, [e.get("start") or e["ts"], "red"])
            segs.setdefault(reg, []).append([a, e.get("end") or e["ts"], lvl])
    for reg, st in (meta or {}).items():          # тривога, що триває зараз
        if reg.startswith("_") or not isinstance(st, dict) or not st.get("active"):
            continue
        a, lvl = open_.pop(reg, [st.get("since") or now, st.get("level") or "red"])
        segs.setdefault(reg, []).append([a, now, lvl])
    return segs


def build(store, cfg, d_from, d_to, regions=None):
    """regions — назви регіонів тривог, які показувати (увімкнені 🔔 у людей цієї ролі); None — усі."""
    now = int(time.time() * 1000)
    try:
        f = date.fromisoformat(d_from)
        t = date.fromisoformat(d_to)
    except ValueError:
        return {"error": "дати у форматі YYYY-MM-DD"}
    if t < f:
        f, t = t, f
    today = datetime.fromtimestamp(now / 1000, local_tz(cfg)).date()
    t = min(t, today + timedelta(days=1))
    if (t - f).days + 1 > MAX_DAYS:
        f = t - timedelta(days=MAX_DAYS - 1)
    days = [f + timedelta(days=i) for i in range((t - f).days + 1)]
    starts = [_day_ms(d, cfg) for d in days] + [_day_ms(t + timedelta(days=1), cfg)]
    lo, hi = starts[0], starts[-1]
    detail = len(days) <= DETAIL_DAYS

    # --- світло: графік з архіву (ключ — початок доби в секундах), факт — з подій
    sched = {int(doc["day"]) * 1000: doc for doc in store.list_archive_range("schedule", str(lo // 1000), str(hi // 1000))}
    events = store.get_events_range(lo, hi + 7 * 86_400_000)  # відключення, що почались у періоді, а скінчились пізніше
    off = [[e["start"], e["end"]] for e in events if e.get("kind") == "outage_end" and e["start"] < hi and e["end"] > lo]
    unknown = [[e["start"], e["end"]] for e in events if e.get("kind") == "internet_up" and e["start"] < hi and e["end"] > lo]
    rules = store.get_meta("rules") or {}
    if rules.get("outage") and rules["outage"]["start"] < hi:
        off.append([rules["outage"]["start"], now])
    net = rules.get("internet") or {}
    if net and not net.get("up") and net.get("since", now) < hi:
        unknown.append([net["since"], now])
    monitor_since = (store.get_meta("first_poll") or {}).get("ts")
    # тривоги: з подій (від тижня до початку періоду — щоб «з …» тривоги, що почалась раніше, теж було видно)
    a_events = [e for e in events if str(e.get("kind", "")).startswith("air_alert")]
    a_events += [e for e in store.get_events_range(lo - 7 * 86_400_000, lo) if str(e.get("kind", "")).startswith("air_alert")]
    alerts = alert_segments(a_events, store.get_meta("air_alerts"), now)
    if regions is not None:
        alerts = {r: v for r, v in alerts.items() if r in regions}
    alerts = {r: [x for x in v if x[0] < hi and x[1] > lo] for r, v in alerts.items()}
    region_names = sorted(set(alerts) | (set(regions) if regions is not None else set()))
    # початки тривог (зміна рівня ділить тривогу на відрізки, але це та сама тривога)
    a_starts = {r: [a for i, (a, b, l) in enumerate(v) if i == 0 or v[i - 1][1] != a] for r, v in alerts.items()}

    # --- погода: прогноз по годинах з архіву (завершені доби) + поточний прогноз (сьогодні/вчора/завтра)
    wx = {}
    for doc in store.list_archive_range("weather", str(lo), str(hi)):
        for r in doc.get("hourly") or []:
            wx[r[0]] = r
    for r in (store.get_meta("weather") or {}).get("hourly") or []:
        if lo <= r[0] < hi and r[0] not in wx:
            wx[r[0]] = r
    hourly = [wx[k] for k in sorted(wx)]
    sensor = weather.outdoor_sensor(store, cfg)
    s_temp, s_hum = [], []
    if sensor:
        if "temp" in sensor["codes"]:
            s_temp = sorted(store.get_history_range(sensor["id"], sensor["codes"]["temp"], lo, min(hi, now)))
        if "hum" in sensor["codes"]:
            s_hum = sorted(store.get_history_range(sensor["id"], sensor["codes"]["hum"], lo, min(hi, now)))

    out_days = []
    for i, d in enumerate(days):
        d0, d1 = starts[i], starts[i + 1]
        doc = sched.get(d0)
        plan_off = sum(_overlap(a, b, d0, d1) for a, b in (doc or {}).get("off") or [])
        plan_maybe = sum(_overlap(a, b, d0, d1) for a, b in (doc or {}).get("maybe") or [])
        seen_from = max(d0, monitor_since or d0)
        seen_to = min(d1, now)
        fact = sum(_overlap(a, b, d0, d1) for a, b in off)
        unk = sum(_overlap(a, b, d0, d1) for a, b in unknown)
        hrs = [r for r in hourly if d0 <= r[0] < d1]
        st = [v for t_, v in s_temp if d0 <= t_ < d1 and v is not None]
        sh = [v for t_, v in s_hum if d0 <= t_ < d1 and v is not None]
        row = {"day": d0, "end": d1, "date": d.isoformat(),
               "plan": doc is not None, "plan_off_h": round(plan_off / H, 2), "plan_maybe_h": round(plan_maybe / H, 2),
               "observed": seen_to > seen_from, "fact_off_h": round(fact / H, 2), "unknown_h": round(unk / H, 2),
               # графік лише за час, коли система вже стежила (і до «зараз») — для чесного «факт − графік»
               "plan_seen_h": round(sum(_overlap(a, b, seen_from, seen_to) for a, b in (doc or {}).get("off") or []) / H, 2)
               if seen_to > seen_from else 0,
               "outages": sum(1 for a, b in off if d0 <= a < d1)}
        if hrs:
            temps = [r[1] for r in hrs if r[1] is not None]
            row["fc"] = {"min": min(temps), "max": max(temps), "avg": round(sum(temps) / len(temps), 1),
                         "precip": round(sum(r[5] or 0 for r in hrs), 1), "pp": max((r[4] or 0) for r in hrs),
                         "code": max((r[3] or 0) for r in hrs)}
        if st:
            row["sensor"] = {"min": min(st), "max": max(st), "avg": round(sum(st) / len(st), 1),
                             "hum": round(sum(sh) / len(sh)) if sh else None}
        row["alerts"] = {r: {"red_h": round(sum(_overlap(a, b, d0, d1) for a, b, l in v if l == "red") / H, 2),
                             "yellow_h": round(sum(_overlap(a, b, d0, d1) for a, b, l in v if l != "red") / H, 2),
                             "n": sum(1 for a in a_starts[r] if d0 <= a < d1)}
                         for r, v in alerts.items()}
        if detail:
            row["slots"] = schedule.slots(doc["hours"]) if doc and doc.get("hours") else None
            row["hours"] = hrs
        out_days.append(row)

    res = {"now": now, "from": f.isoformat(), "to": t.isoformat(), "detail": detail, "days": out_days,
           "monitor_since": monitor_since, "weather_fields": ["ts"] + weather.HOURLY,
           "sensor_name": sensor and sensor["name"]}
    res["alert_regions"] = region_names
    # пилосос: архів прибирань (ARCH#vacuum, ключ — час початку), лише якщо пилосос підключено
    if store.get_meta("vacuum"):
        res["vacuum"] = [r for r in store.list_archive_range("vacuum", f"{lo:013d}", f"{hi:013d}") if r.get("ts")]
    if detail:
        res["off"], res["unknown"] = off, unknown
        res["alert_segments"] = alerts
        res["sensor_temp"], res["sensor_hum"] = s_temp, s_hum
    return res
