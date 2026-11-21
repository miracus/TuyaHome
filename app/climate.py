"""Мікроклімат у кімнатах: довго висока вологість (ризик плісняви) і холод (узимку під час відключень).

Працює на кожному знімку з правил (rules.evaluate): короткий ряд зрізів зберігається в стані приладу
(state["clim"]), окремих запитів до бази немає. Датчик «Назовні» (weather.sensor) і climate.exclude не беремо."""
from devices import fmt_time, is_number

MIN = 60_000
HOUR = 60 * MIN

DEFAULTS = {"hum_high": 65, "hum_hours": 3, "hum_cooldown_h": 24, "cold_c": 16, "cold_cooldown_h": 6,
            "cold_rate_c_h": 0.3, "cold_ahead_h": 3, "notify_from": 9, "notify_to": 21}


def conf(cfg):
    c = dict(DEFAULTS)
    c.update(cfg.get("climate") or {})
    return c


def _due(meta, key, now, cooldown_ms):
    alerts = meta.setdefault("alerts", {})
    if now - alerts.get(key, 0) >= cooldown_ms:
        alerts[key] = now
        return True
    return False


def is_indoor(dev, cfg):
    skip = set(conf(cfg).get("exclude") or []) | {(cfg.get("weather") or {}).get("sensor")}
    return dev.get("name") not in skip


def reading(dev):
    """(температура, вологість) приладу або None, якщо їх немає."""
    t = h = None
    for d in dev.get("dps", []):
        if not is_number(d.get("value")):
            continue
        if t is None and (d["code"].startswith("temp_current") or d["code"] == "va_temperature"):
            t = d["value"]
        elif h is None and (d["code"].startswith("humidity_value") or d["code"] == "va_humidity"):
            h = d["value"]
    return (t, h) if t is not None or h is not None else None


def trend(track, now, hours=2):
    """Швидкість зміни температури, °C/год, за останні hours годин (лінійна регресія по зрізах)."""
    pts = [(ts, t) for ts, t, _ in track if ts >= now - hours * HOUR and t is not None]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < 45 * MIN:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    if not den:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den * HOUR


def _local_hour(now, cfg):
    return int(fmt_time(now, cfg)[:2])


def check(dev, state, meta, cfg, now, event):
    """event(kind, text, device_id, notify=True) — як Result.event. Повертає оновлений ряд зрізів."""
    c = conf(cfg)
    r = reading(dev)
    if not r or not is_indoor(dev, cfg) or not dev.get("online"):
        return
    t, h = r
    track = [list(p) for p in (state.get("clim") or {}).get("track", [])]
    if not track or now - track[-1][0] >= 10 * MIN or [t, h] != track[-1][1:]:
        track.append([now, t, h])
    track = [p for p in track if p[0] >= now - 6 * HOUR][-80:]
    clim = dict(state.get("clim") or {})
    clim["track"] = track
    name = dev["name"]
    awake = c["notify_from"] <= _local_hour(now, cfg) < c["notify_to"]

    # --- вологість: вище hum_high безперервно hum_hours годин
    if h is not None and h > c["hum_high"]:
        clim.setdefault("humid_since", now)
    else:
        clim.pop("humid_since", None)
    since = clim.get("humid_since")
    if since and now - since >= c["hum_hours"] * HOUR and awake and _due(
            meta, f"humid:{dev['id']}", now, c["hum_cooldown_h"] * HOUR):
        hrs = round((now - since) / HOUR)
        event("humidity_high", f"💧 {name}: вологість {h:g}% уже {hrs} год. Понад {c['hum_high']}% довго — "
              f"ризик плісняви: провітріть 10–15 хв (вікно навстіж, а не на провітрювання).", dev["id"], notify=True)

    # --- холод: нижче cold_c — одразу; падає швидше cold_rate_c_h — попередження наперед
    if t is not None:
        rate = trend(track, now)
        if t < c["cold_c"]:
            if _due(meta, f"cold:{dev['id']}", now, c["cold_cooldown_h"] * HOUR):
                tail = f", падає ~{abs(rate):.1f}°/год" if rate is not None and rate <= -c["cold_rate_c_h"] else ""
                event("cold", f"🥶 {name}: {t:g}°C — нижче {c['cold_c']}°{tail}. Одягніться тепліше, закрийте вікна "
                      f"й двері в холодні кімнати.", dev["id"], notify=True)
        elif rate is not None and rate <= -c["cold_rate_c_h"]:
            eta = now + (t - c["cold_c"]) / -rate * HOUR
            if eta - now <= c["cold_ahead_h"] * HOUR and _due(meta, f"cold:{dev['id']}", now, c["cold_cooldown_h"] * HOUR):
                event("cold", f"🌡 {name}: {t:g}°C і падає ~{abs(rate):.1f}°/год — за такого темпу "
                      f"{c['cold_c']}° буде близько {fmt_time(eta, cfg)}. Закрийте вікна, збережіть тепло.",
                      dev["id"], notify=True)
    state["clim"] = clim
