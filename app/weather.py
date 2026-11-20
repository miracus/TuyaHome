"""Погода: прогноз Open-Meteo (без ключа, квоту Tuya не витрачає) і порівняння з датчиком «Назовні».

meta "weather": {fetched, current, hourly: [[мс, темп, волог, код WMO, імовірн. опадів %, опади мм, вітер м/с], …]}
— учора, сьогодні, завтра (past_days=1, forecast_days=2). Минулі доби — в архів назавжди (ARCH#weather)."""
import json
import urllib.parse
import urllib.request
from datetime import datetime

from devices import fmt_time, local_tz

MIN = 60_000
URL = "https://api.open-meteo.com/v1/forecast"
HOURLY = ["temperature_2m", "relative_humidity_2m", "weather_code", "precipitation_probability", "precipitation",
          "wind_speed_10m"]

DAILY = ["weather_code", "temperature_2m_min", "temperature_2m_max", "precipitation_sum",
         "precipitation_probability_max", "wind_speed_10m_max", "relative_humidity_2m_mean"]
RAINY = set(range(51, 68)) | set(range(80, 83)) | {95, 96, 99}
SNOWY = set(range(71, 78)) | {85, 86}


def intensity(mmh):
    """Сила опадів за мм/год (шкала WMO/AMS): слабкий < 2.5, помірний < 7.6, сильний < 50, злива."""
    if mmh < 0.5:
        return "дуже слабкий"
    if mmh < 2.5:
        return "слабкий"
    if mmh < 7.6:
        return "помірний"
    return "сильний" if mmh < 50 else "злива"


def rain_now(cur, sensor):
    """Чи йде зараз дощ: інтенсивність — з прогнозу (опади за останні 15 хв ×4 = мм/год),
    датчик вологості — підтвердження: під дощем повітря біля насичення (≥ 93%). Сам датчик сили дощу не міряє.
    → {"rain": bool, "mmh", "level", "hum", "confirmed", "text"}"""
    mmh = round((cur.get("precip") or 0) * 4, 1)
    code = cur.get("code")
    hum = (sensor or {}).get("hum")
    wet = hum is not None and hum >= 93
    snow = code in SNOWY
    what = "сніг" if snow else "дощ"
    if mmh > 0 or code in RAINY or snow:
        lvl = intensity(mmh)
        conf = "" if hum is None else (f"датчик {hum}% — підтверджує" if wet else f"датчик {hum}% — сумнівно, повітря не насичене")
        text = f"{'🌨' if snow else '🌧'} Зараз {what}, {lvl}" + (f" (~{mmh:g} мм/год)" if mmh else "") + (f" · {conf}" if conf else "")
        return {"rain": True, "mmh": mmh, "level": lvl, "hum": hum, "confirmed": wet, "text": text}
    if hum is not None and hum >= 97:
        return {"rain": False, "mmh": 0, "hum": hum, "confirmed": False,
                "text": f"🌫 Опадів за прогнозом немає, але датчик {hum}% — можлива мряка або туман"}
    return {"rain": False, "mmh": 0, "hum": hum, "confirmed": False,
            "text": "☂️ Опадів зараз немає" + (f" (датчик {hum}%)" if hum is not None else "")}


# коди WMO → (значок, опис)
WMO = {0: ("☀️", "ясно"), 1: ("🌤", "переважно ясно"), 2: ("⛅", "мінлива хмарність"), 3: ("☁️", "хмарно"),
       45: ("🌫", "туман"), 48: ("🌫", "туман з памороззю"),
       51: ("🌦", "легка мряка"), 53: ("🌦", "мряка"), 55: ("🌧", "сильна мряка"),
       56: ("🌧", "крижана мряка"), 57: ("🌧", "сильна крижана мряка"),
       61: ("🌦", "невеликий дощ"), 63: ("🌧", "дощ"), 65: ("🌧", "сильний дощ"),
       66: ("🌧", "крижаний дощ"), 67: ("🌧", "сильний крижаний дощ"),
       71: ("🌨", "невеликий сніг"), 73: ("🌨", "сніг"), 75: ("❄️", "сильний сніг"), 77: ("🌨", "снігова крупа"),
       80: ("🌦", "короткочасний дощ"), 81: ("🌧", "зливи"), 82: ("⛈", "сильні зливи"),
       85: ("🌨", "снігопад"), 86: ("❄️", "сильний снігопад"),
       95: ("⛈", "гроза"), 96: ("⛈", "гроза з градом"), 99: ("⛈", "сильна гроза з градом")}


def describe(code):
    return WMO.get(int(code) if code is not None else -1, ("🌡", "—"))


def fetch(wc, cfg, timeout=8):
    q = {"latitude": wc["lat"], "longitude": wc["lon"], "timezone": cfg.get("timezone", "Europe/Kyiv"),
         "timeformat": "unixtime", "wind_speed_unit": "ms", "past_days": 1, "forecast_days": 7,
         "daily": ",".join(DAILY),
         "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
         "hourly": ",".join(HOURLY)}
    req = urllib.request.Request(f"{URL}?{urllib.parse.urlencode(q)}", headers={"User-Agent": "TuyaHome"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    h = data["hourly"]
    # по годинах — лише вчора, сьогодні, завтра (тиждень — денними підсумками)
    rows = [[t * 1000] + [h[k][i] for k in HOURLY] for i, t in enumerate(h["time"])][:72]
    dd = data.get("daily") or {}
    daily = [[t * 1000] + [dd[k][i] for k in DAILY] for i, t in enumerate(dd.get("time") or [])]
    c = data.get("current") or {}
    current = {"ts": (c.get("time") or 0) * 1000, "temp": c.get("temperature_2m"), "hum": c.get("relative_humidity_2m"),
               "feels": c.get("apparent_temperature"), "code": c.get("weather_code"), "wind": c.get("wind_speed_10m"),
               "precip": c.get("precipitation")}
    return {"current": current, "hourly": rows, "daily": daily}


def _day_start(ms, cfg):
    dt = datetime.fromtimestamp(ms / 1000, local_tz(cfg))
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def run(store, cfg, now):
    """Оновлює прогноз раз на fetch_every_min; завершені доби — в архів (прогноз по годинах + датчик)."""
    wc = cfg.get("weather")
    if not wc:
        return None
    meta = store.get_meta("weather") or {}
    if now - meta.get("fetched", 0) < wc.get("fetch_every_min", 30) * MIN:
        return None
    try:
        new = fetch(wc, cfg)
    except Exception as e:  # джерело недоступне — лишаємо старий прогноз, повтор через fetch_every_min
        meta["fetched"] = now
        meta["error"] = f"{type(e).__name__}: {e}"
        store.put_meta("weather", meta)
        return {"error": meta["error"]}
    today = _day_start(now, cfg)
    yesterday = _day_start(today - 12 * 3_600_000, cfg)
    archived = meta.get("archived")
    if archived != yesterday:
        rows = [r for r in new["hourly"] if yesterday <= r[0] < today]
        if rows:
            sensor = outdoor_sensor(store, cfg)
            doc = {"day": yesterday, "place": wc.get("place"), "lat": wc["lat"], "lon": wc["lon"],
                   "hourly_fields": ["ts"] + HOURLY, "hourly": rows}
            if sensor:
                doc["sensor"] = {"name": sensor["name"],
                                 **{k: [p for p in store.get_history(sensor["id"], code, yesterday) if p[0] < today]
                                    for k, code in sensor["codes"].items()}}
            store.put_archive("weather", str(yesterday), doc)
            archived = yesterday
    store.put_meta("weather", {**new, "fetched": now, "archived": archived})
    return {"fetched": True, "archived": archived == yesterday and meta.get("archived") != yesterday}


def outdoor_sensor(store, cfg, states=None):
    """Датчик «Назовні» з config.weather.sensor: {id, name, online, temp, hum, codes, ts}."""
    name = (cfg.get("weather") or {}).get("sensor")
    if not name:
        return None
    states = states if states is not None else list(store.get_states().values())
    dev = next((s for s in states if s.get("name") == name and not s.get("missing")), None)
    if not dev:
        return None
    codes, vals = {}, {}
    for d in dev.get("dps", []):
        key = "temp" if "temp" in d["code"] else "hum" if "humid" in d["code"] else None
        if key and key not in codes:
            codes[key] = d["code"]
            vals[key] = d.get("value")
    ts = max((dev.get("hist_ts") or {}).values(), default=None) or dev.get("seen")
    return {"id": dev["id"], "name": dev["name"], "online": dev.get("online"), "codes": codes, "ts": ts, **vals}


def for_web(store, cfg, now):
    """Прогноз на сьогодні й завтра по годинах + ряди датчика за ці доби (для смуг «Прогноз / Датчик»)."""
    wc = cfg.get("weather")
    meta = store.get_meta("weather") or {}
    if not wc or not meta.get("hourly"):
        return None
    today = _day_start(now, cfg)
    days = []
    for i, label in enumerate(("сьогодні", "завтра")):
        d0 = _day_start(today + i * 86_400_000 + 12 * 3_600_000, cfg)  # перехід на літній/зимовий час
        d1 = _day_start(d0 + 36 * 3_600_000, cfg)
        rows = [r for r in meta["hourly"] if d0 <= r[0] < d1]
        if rows:
            days.append({"day": d0, "end": d1, "label": f"{label} {fmt_time(d0, cfg, with_date=True)[:5]}",
                         "hours": rows})
    sensor = outdoor_sensor(store, cfg)
    s = None
    if sensor:
        s = {k: sensor.get(k) for k in ("name", "online", "temp", "hum", "ts")}
        for k, code in sensor["codes"].items():
            s[f"{k}_series"] = sorted(store.get_history(sensor["id"], code, today))
    cur = meta.get("current") or {}
    return {"place": wc.get("place"), "fetched": meta.get("fetched"), "current": cur,
            "rain": rain_now(cur, sensor), "fields": ["ts"] + HOURLY, "days": days, "sensor": s,
            "daily_fields": ["ts"] + DAILY, "week": [r for r in meta.get("daily") or [] if r[0] >= today][:7]}


def _km(lat1, lon1, lat2, lon2):
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def user_location(u, cfg):
    """Прогноз за адресою користувача: (lat, lon, назва), якщо адреса має координати і далі home_radius_km від дому
    (або це гість). Ближче до дому чи адреса без координат (задана до 17.11) — None: погода дому з датчиком."""
    wc = cfg.get("weather") or {}
    a = u.get("addr") or {}
    if a.get("lat") is None or a.get("lon") is None or not wc:
        return None
    far = _km(a["lat"], a["lon"], wc["lat"], wc["lon"]) > wc.get("home_radius_km", 5)
    if far or u.get("access") == "guest":
        return a["lat"], a["lon"], a.get("label") or ""
    return None


def point(store, cfg, lat, lon, now):
    """Прогноз для довільної точки (адреса користувача): кеш meta weather@lat,lon, оновлення раз на 30 хв."""
    wc = cfg.get("weather") or {}
    key = f"weather@{lat:.2f},{lon:.2f}"
    meta = store.get_meta(key) or {}
    if now - meta.get("fetched", 0) >= 30 * MIN:
        try:
            meta = {**fetch({**wc, "lat": round(lat, 3), "lon": round(lon, 3)}, cfg), "fetched": now}
            store.put_meta(key, meta)
        except Exception as e:  # джерело недоступне — віддаємо старий прогноз, якщо є
            meta = {**meta, "error": f"{type(e).__name__}: {e}"}
    return meta


def forecast_lines(meta, cfg, now):
    """Лише прогноз (без поточного стану й датчика): решта сьогодні по 3 год, завтра, тиждень."""
    out = []
    rows = meta.get("hourly") or []
    today = _day_start(now, cfg)
    tomorrow = _day_start(today + 36 * 3_600_000, cfg)
    nxt = [r for r in rows if now <= r[0] < tomorrow][::3][:6]
    if nxt:
        out.append("Сьогодні:")
        for r in nxt:
            ico, _ = describe(r[3])
            out.append(f"{fmt_time(r[0], cfg)}  {ico} {r[1]:.0f}°, {r[2]}%" + (f", опади {r[4]}%" if r[4] else ""))
    week = [r for r in meta.get("daily") or [] if r[0] >= tomorrow][:6]
    if week:
        out.append("")
        out.append("Далі:")
        for r in week:
            ico, desc = describe(r[1])
            day = datetime.fromtimestamp(r[0] / 1000, local_tz(cfg))
            wd = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"][day.weekday()]
            rain = f", {r[4]:.1f} мм ({r[5]}%)" if r[4] else (f", опади {r[5]}%" if r[5] else "")
            out.append(f"{wd} {day:%d.%m}  {ico} {r[2]:.0f}…{r[3]:.0f}°, {desc}{rain}")
    return out


def forecast_text(store, cfg, now, lat, lon, label):
    meta = point(store, cfg, lat, lon, now)
    out = [f"🌤 Прогноз погоди · {label}".rstrip(" ·")]
    lines = forecast_lines(meta, cfg, now)
    if not lines:
        return out[0] + "\nПрогноз зараз недоступний, спробуйте пізніше"
    out += [""] + lines
    if meta.get("fetched"):
        out.append(f"\nOpen-Meteo, оновлено {fmt_time(meta['fetched'], cfg)}")
    return "\n".join(out)


def weather_text(store, cfg, now, states=None):
    """Відповідь бота «🌤 Погода»: датчик зараз, прогноз зараз, різниця, найближчі години і завтра."""
    wc = cfg.get("weather") or {}
    meta = store.get_meta("weather") or {}
    out = [f"🌤 Погода · {wc.get('place', '')}".rstrip(" ·")]
    sensor = outdoor_sensor(store, cfg, states)
    cur = meta.get("current") or {}
    if sensor and sensor.get("temp") is not None:
        ago = f", {fmt_time(sensor['ts'], cfg)}" if sensor.get("ts") else ""
        off = "" if sensor.get("online") else " (датчик офлайн — останнє значення)"
        out.append(f"🌡 Датчик «{sensor['name']}»: {sensor['temp']:g}°C, вологість {sensor.get('hum', '—')}%{ago}{off}")
    if cur.get("temp") is not None:
        ico, desc = describe(cur.get("code"))
        out.append(f"{ico} Прогноз зараз: {cur['temp']:.0f}°C (відчувається {cur.get('feels', 0):.0f}°), {desc}, "
                   f"вологість {cur.get('hum', '—')}%, вітер {cur.get('wind', 0):.0f} м/с")
        out.append(rain_now(cur, sensor)["text"])
        if sensor and sensor.get("temp") is not None:
            dt = sensor["temp"] - cur["temp"]
            dh = (sensor.get("hum") or 0) - (cur.get("hum") or 0)
            out.append(f"↔️ Датчик − прогноз: {dt:+.1f}° · вологість {dh:+.0f}%")
    rows = meta.get("hourly") or []
    if not rows:
        out.append("Прогноз ще не завантажено")
        return "\n".join(out)
    today = _day_start(now, cfg)
    tomorrow = _day_start(today + 36 * 3_600_000, cfg)
    after = _day_start(tomorrow + 36 * 3_600_000, cfg)
    nxt = [r for r in rows if now <= r[0] < tomorrow][::3][:6]
    if nxt:
        out.append("")
        out.append("Сьогодні:")
        for r in nxt:
            ico, desc = describe(r[3])
            rain = f", опади {r[4]}%" if r[4] else ""
            out.append(f"{fmt_time(r[0], cfg)}  {ico} {r[1]:.0f}°, {r[2]}%{rain}")
    tm = [r for r in rows if tomorrow <= r[0] < after]
    if tm:
        lo, hi = min(r[1] for r in tm), max(r[1] for r in tm)
        codes = [r[3] for r in tm if 8 <= datetime.fromtimestamp(r[0] / 1000, local_tz(cfg)).hour <= 20] or [r[3] for r in tm]
        ico, desc = describe(max(codes))  # найгірше за день
        pp = max((r[4] or 0) for r in tm)
        mm = sum((r[5] or 0) for r in tm)
        out.append("")
        out.append(f"Завтра: {ico} {lo:.0f}…{hi:.0f}°, {desc}" + (f", опади до {pp}% ({mm:.1f} мм)" if pp else ""))
    if meta.get("fetched"):
        out.append(f"\nПрогноз Open-Meteo, оновлено {fmt_time(meta['fetched'], cfg)}")
    return "\n".join(out)
