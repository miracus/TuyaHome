"""Дашборд і JSON API (Lambda Function URL). Доступ лише з секретним ключем ?k=… — інакше 404.
Читає тільки сховище, до Tuya не звертається (квоту не витрачає)."""
import hmac
import json
import os
import time

import accuracy
import schedule
import weather
from budget import plan
from devices import load_config, local_tz
from rules import power_devices

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_HOURS = 24 * 90


def _resp(status, body, ctype="application/json; charset=utf-8"):
    if not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False)
    return {"statusCode": status, "headers": {"Content-Type": ctype, "Cache-Control": "no-store",
                                              "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"},
            "body": body}


def user_for_key(keys, given):
    """keys: {user: key}. Кожен користувач має своє посилання — доступ вимикається окремо."""
    for user, key in keys.items():
        if key and hmac.compare_digest(given or "", key):
            return user
    return None


PAGES = {"/": "dashboard.html", "/index.html": "dashboard.html"}
# PWA (21.11): іконки й service worker — без ключа (нічого особистого); маніфест — з ключем (start_url = посилання людини)
STATIC = {"/sw.js": ("static/sw.js", "text/javascript; charset=utf-8"),
          "/static/icon-192.png": ("static/icon-192.png", "image/png"),
          "/static/icon-512.png": ("static/icon-512.png", "image/png"),
          "/static/icon-maskable.png": ("static/icon-maskable.png", "image/png"),
          "/static/apple-touch-icon.png": ("static/apple-touch-icon.png", "image/png")}


def static_file(path):
    name, ctype = STATIC[path]
    with open(os.path.join(HERE, name), "rb") as f:
        data = f.read()
    headers = {"Content-Type": ctype, "Cache-Control": "no-cache" if path == "/sw.js" else "public, max-age=604800",
               "X-Robots-Tag": "noindex"}
    if ctype.startswith("text/"):
        return {"statusCode": 200, "headers": headers, "body": data.decode("utf-8")}
    import base64
    return {"statusCode": 200, "headers": headers, "body": base64.b64encode(data).decode(), "isBase64Encoded": True}


def _vacuum_refresh(store, op, every_min):
    """Дашборд відкрили, а дані пилососа застарілі — попросити Lambda пилососа оновити (не частіше every_min)."""
    if not os.environ.get("VACUUM_FN"):
        return False
    now = int(time.time() * 1000)
    t = store.get_meta("vacuum_tick") or {}
    if now - t.get(f"web_{op}", 0) < every_min * 60_000:
        return False
    store.put_meta("vacuum_tick", {**t, f"web_{op}": now})
    try:
        import vacuum_bot
        vacuum_bot.invoke({"op": op})
        return True
    except Exception as e:
        print(f"[web] vacuum {op}: {type(e).__name__}: {e}")
        return False


def vacuum_state(store, cfg, live=False):
    """Пилосос на дашборд (дашборд — лише учасники «🏠 Дім»: гості посилання не мають)."""
    meta = store.get_meta("vacuum")
    if not meta:
        return {"connected": False}
    now = int(time.time() * 1000)
    if live and not meta.get("active") and now - (meta.get("updated") or 0) > 10 * 60_000:
        _vacuum_refresh(store, "status", 5)
    import vacuum_bot as vb
    m = store.get_meta("vacuum_map") or {}
    return {"connected": True, **{k: v for k, v in meta.items() if k not in ("fail", "lost_told")},
            "state_text": vb.state_text(meta.get("state")), "text": vb.status_text(meta, cfg, now),
            "mode_text": vb.mode_label(vb.MODE_NAME_TO_KEY.get(meta.get("mode") or "")),
            "suction_text": vb.SUCTION_UA.get(meta.get("suction") or "", meta.get("suction")),
            "map_ts": m.get("ts")}


def vacuum_map(store):
    m = store.get_meta("vacuum_map") or {}
    now = int(time.time() * 1000)
    if now - (m.get("ts") or 0) > 6 * 3600_000:
        _vacuum_refresh(store, "map", 10)
    if not m.get("png"):
        return _resp(404, "no map", "text/plain")
    return {"statusCode": 200, "headers": {"Content-Type": "image/png", "Cache-Control": "private, max-age=60",
                                           "X-Robots-Tag": "noindex"}, "body": m["png"], "isBase64Encoded": True}


def lock_view(store, cfg, user, now=None):
    """Стан для екрана блокування телефону (Automate; 22.11). Samsung показує там заголовок і 2 рядки
    по ~30 символів. Лише головне (рішення власника 22.11): світло зараз, коли зміниться за графіком, тривоги.
    Інтернет — лише коли його немає. Квоту Tuya не витрачає."""
    import air_alerts
    import bot
    from devices import fmt_time
    now = now or int(time.time() * 1000)
    c = bot._ctx(store, cfg)
    r = c["rules"]
    hub = bot._hub_online(c)
    off = bool(r.get("outage"))
    upd = c.get("last_poll") or now
    stale = now - upd > 15 * 60_000

    def hm(ms):
        return "24:00" if fmt_time(ms, cfg) == "00:00" else fmt_time(ms, cfg)

    # 22.11 (власник): «Світло є» / «Немає (23:00)» — у дужках коли мають дати за графіком; без «з якого часу»
    sc = schedule.for_web(store, cfg, now)
    nxt = sc.get("next_off") if sc else None
    if off:
        title = "🔌 Немає" + (f" ({hm(nxt[1])})" if nxt and nxt[0] <= now else "")
    elif not hub:
        title = "❔ Світло невідомо"
    else:  # у дужках — коли вимкнуть за графіком (сьогодні/завтра); далі — без дужок
        title = "💡 Світло є"
        if nxt and nxt[0] > now and nxt[0] - now < 44 * 3600_000:
            title += f" (до {'завтра ' if nxt[0] - now >= 20 * 3600_000 else ''}{fmt_time(nxt[0], cfg)})"
    if not hub:
        title += " · 📡 без інтернету"
    if stale:
        title = "⚠️ " + title
    if (cfg.get("lockscreen") or {}).get("debug_time"):  # тимчасово для перевірки: час відповіді сервера з секундами
        from datetime import datetime
        title = datetime.fromtimestamp(now / 1000, local_tz(cfg)).strftime("%H:%M:%S") + " · " + title

    active = [(k, v) for k, v in visible_alerts(store, user).items() if v.get("active")]
    if len(active) == 1:
        k, v = active[0]
        alert = f"{air_alerts.LEVELS.get(v.get('level'), ('🚨', ''))[0]} Тривога з {fmt_time(v['since'], cfg)}"
    elif active:
        alert = f"🚨 Тривоги: {len(active)}"
    else:
        alert = "🕊 Тривоги немає"
    # погода: значок неба (прогноз; дощ/сніг — за rain_now), температура надворі (датчик, інакше прогноз) і вдома
    import climate
    from datetime import datetime
    wmeta = store.get_meta("weather") or {}
    cur = wmeta.get("current") or {}
    sensor = weather.outdoor_sensor(store, cfg, c["states"])
    ico = weather.describe(cur.get("code"))[0] if cur.get("code") is not None else "🌡"
    rn = weather.rain_now(cur, sensor) if cur else {}
    if rn.get("rain"):
        ico = "🌨" if "сніг" in rn.get("text", "") else "🌧"
    hour = datetime.fromtimestamp(now / 1000, local_tz(cfg)).hour
    if ico in ("☀️", "🌤") and (hour >= 19 or hour < 6):
        ico = "🌙"
    t_out = (sensor or {}).get("temp") if sensor and sensor.get("online") else None
    if t_out is None:
        t_out = cur.get("temp")
    outside = (cfg.get("weather") or {}).get("sensor")
    t_in = next((rd[0] for st in sorted(c["states"], key=lambda s: s.get("name", ""))
                 if st.get("online") and st.get("name") != outside
                 for rd in [climate.reading(st)] if rd and rd[0] is not None), None)
    temp = " · ".join(x for x in (f"{ico} {round(t_out)}° надворі" if t_out is not None else "",
                                  f"🏠 {round(t_in)}° вдома" if t_in is not None else "") if x)
    # три рядки, кожен про своє (власник 22.11): світло — заголовок; тривоги; погода
    return {"title": title, "text": chr(10).join(x for x in (alert, temp) if x), "updated": fmt_time(upd, cfg),
            "stale": stale}


def manifest(key):
    icons = [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
             {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
             {"src": "/static/icon-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]
    return {"name": "Дім · TuyaHome", "short_name": "Дім", "lang": "uk", "start_url": f"/?k={key}", "scope": "/",
            "display": "standalone", "background_color": "#f4f5f7", "theme_color": "#1e3a8a", "icons": icons}


def visible_alerts(store, role):
    """Тривоги для дашборда ролі (owner/member): лише регіони, які хтось із людей цієї ролі має увімкненими (🔔).
    Перекреслений дзвіночок (🔕) у боті — регіон на дашборді не показується."""
    import air_alerts

    alerts = {k: v for k, v in (store.get_meta("air_alerts") or {}).items() if not k.startswith("_")}
    on = visible_regions(store, role)
    return alerts if on is None else {k: v for k, v in alerts.items() if k in on}


def visible_regions(store, role):
    """Регіони тривог, увімкнені (🔔) хоч в одного з людей цієї ролі; None — людей ще немає (показувати все)."""
    import air_alerts

    users = [u for u in (store.get_meta("tg_users") or {}).values()
             if u.get("status") == "approved" and u.get("role", "member") == role and u.get("access") != "guest"]
    if not users:
        return None
    cfg = load_config()
    return {r["name"] for u in users for r in air_alerts.user_regions(u, cfg) if r.get("on", True)}


def handle_telegram(store, cfg, headers, body):
    """Webhook Telegram-бота: приймаємо лише з правильним секретом (заголовок задає Telegram при setWebhook)."""
    import bot
    from notify import Notifier

    secret = os.environ.get("TG_SECRET", "")
    given = (headers or {}).get("x-telegram-bot-api-secret-token", "")
    if not secret or not hmac.compare_digest(given, secret):
        return _resp(404, "Not found", "text/plain")
    try:
        update = json.loads(body or "{}")
        notifier = Notifier(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""))
        web = {"base": f"https://{(headers or {}).get('host', '')}/", "keys": json.loads(os.environ.get("WEB_KEYS", "{}"))}
        print("tg command:", bot.handle_update(update, store, cfg, notifier, os.environ.get("TELEGRAM_CHAT_ID", ""), web))
    except Exception as e:  # Telegram повторюватиме запит, якщо не 200 — тож завжди 200
        print(f"tg error: {type(e).__name__}: {e}")
    return _resp(200, {"ok": True})


def handle(store, path, qs, cfg, keys, method="GET", body="", headers=None):
    if path == "/tg" and method == "POST":
        return handle_telegram(store, cfg, headers, body)
    if path in STATIC and method == "GET":
        return static_file(path)
    user = user_for_key(keys, qs.get("k", ""))
    if not user:
        return _resp(404, "Not found", "text/plain")

    if path == "/manifest.webmanifest":
        return _resp(200, manifest(qs.get("k", "")), "application/manifest+json; charset=utf-8")

    if path in PAGES:
        print(f"{path} opened by {user}")
        with open(os.path.join(HERE, PAGES[path]), encoding="utf-8") as f:
            return _resp(200, f.read(), "text/html; charset=utf-8")

    if path == "/api/lock":  # екран блокування телефону: {"title", "text", "updated", "stale"}
        print(f"lock screen by {user}")
        return _resp(200, lock_view(store, cfg, user))
    if path == "/api/vacuum":
        return _resp(200, vacuum_state(store, cfg, live=qs.get("live") == "1"))
    if path == "/api/vacuum/map":
        return vacuum_map(store)
    if path == "/api/vacuum/map/refresh" and method == "POST":  # натиснули на карту — свіжа (не частіше ніж раз на 1 хв)
        print(f"vacuum map refresh by {user}")
        return _resp(200, {"started": _vacuum_refresh(store, "map", 1)})

    if path == "/api/state":
        states = [s for s in store.get_states().values() if not s.get("missing")]
        states.sort(key=lambda s: (not s.get("gateway"), not s.get("mains"), s.get("name", "")))
        rules = store.get_meta("rules") or {}
        p = plan(cfg, states) if states else None
        sched = schedule.for_web(store, cfg, int(time.time() * 1000))
        alerts = visible_alerts(store, user)
        return _resp(200, {
            "now": int(time.time() * 1000),
            "devices": states,
            "outage": rules.get("outage"),
            "last_outage": rules.get("last_outage"),
            "internet": rules.get("internet"),
            "power": rules.get("power"),
            "last_internet_outage": rules.get("last_internet_outage"),
            "power_ids": [d["id"] for d in power_devices(states, cfg)],
            "schedule": sched,
            "sched_acc": accuracy.summary_text(store.get_meta("sched_acc")),
            "air_alerts": alerts,
            "tuya_rooms": (store.get_meta("tuya_rooms") or {}).get("by_device", {}),
            "tuya_room_order": (store.get_meta("tuya_rooms") or {}).get("rooms", []),
            "last_poll": store.get_meta("last_poll"),
            "usage": store.get_meta("usage"),
            "plan": p and {k: p[k] for k in ("quota", "budget", "total", "snapshot_every_min", "log_factor", "log_disabled")},
            "thresholds": {"voltage_high": cfg.get("voltage_high"), "voltage_low": cfg.get("voltage_low")},
        })

    if path == "/api/device":
        # усе про один прилад: стан, параметри з моделлю, кімната Smart Life, останні події
        dev_id = qs.get("id", "")
        state = store.get_states().get(dev_id)
        if not state:
            return _resp(404, {"error": "no device"})
        events = [e for e in store.get_events(limit=500) if e.get("device_id") == dev_id][:30]
        return _resp(200, {"now": int(time.time() * 1000), "device": state,
                           "model": (store.get_model(dev_id) or {}).get("specs", {}),
                           "tuya_room": (store.get_meta("tuya_rooms") or {}).get("by_device", {}).get(dev_id),
                           "events": events})

    if path == "/api/actual":
        # фактичні відключення за розеткою (і проміжки без інтернету — «невідомо») для смуги «Факт» під графіком ДТЕК
        now = int(time.time() * 1000)
        since = now - 3 * 86_400_000
        events = store.get_events(limit=500, since_ms=since)
        rules = store.get_meta("rules") or {}
        off = [[e["start"], e["end"]] for e in events if e.get("kind") == "outage_end"]
        unknown = [[e["start"], e["end"]] for e in events if e.get("kind") == "internet_up"]
        if rules.get("outage"):
            off.append([rules["outage"]["start"], now])
        net = rules.get("internet") or {}
        if net and not net.get("up"):
            unknown.append([net["since"], now])
        first = (store.get_meta("first_poll") or {}).get("ts")
        return _resp(200, {"now": now, "monitor_since": first, "off": off, "unknown": unknown})

    if path == "/api/weather":
        # прогноз по годинах на сьогодні й завтра + ряди датчика «Назовні» (смуги «Прогноз / Датчик»)
        now = int(time.time() * 1000)
        return _resp(200, {"now": now, "weather": weather.for_web(store, cfg, now)})

    if path in ("/api/game/me", "/api/game/score", "/api/game/checkers") and method == "POST":
        import game
        try:
            data = json.loads(body or "{}")
        except ValueError:
            return _resp(400, {"error": "bad json"})
        fn = {"score": game.save, "checkers": game.checkers_result}.get(path.rsplit("/", 1)[-1], game.me)
        return _resp(200, fn(store, data))

    if path == "/api/arank":
        # рейтинг міст за тривогами за 365 днів (перераховується раз на добу)
        return _resp(200, {"now": int(time.time() * 1000), "rank": store.get_meta("arank")})

    if path == "/api/range":
        import history
        on = visible_regions(store, user)
        return _resp(200, history.build(store, cfg, qs.get("from", ""), qs.get("to", ""), sorted(on) if on is not None else None))

    if path == "/api/history":
        try:
            hours = min(float(qs.get("h", 24)), MAX_HOURS)
        except ValueError:
            hours = 24
        since = int(time.time() * 1000 - hours * 3_600_000)
        pts = sorted(store.get_history(qs.get("d", ""), qs.get("c", ""), since))
        return _resp(200, {"points": pts})

    if path == "/api/series":
        # кілька рядів історії одним запитом: s=device:code,device:code
        try:
            hours = min(float(qs.get("h", 24)), MAX_HOURS)
        except ValueError:
            hours = 24
        since = int(time.time() * 1000 - hours * 3_600_000)
        out = {}
        for key in (qs.get("s") or "").split(",")[:40]:
            if ":" in key:
                dev, code = key.split(":", 1)
                out[key] = sorted(store.get_history(dev, code, since))
        return _resp(200, {"series": out})

    if path == "/api/events":
        try:
            n = min(int(qs.get("n", 100)), 500)
        except ValueError:
            n = 100
        events = store.get_events(limit=n)
        # тривоги з регіонів, де в боті 🔕, у журналі дашборда не показуємо (як і в шапці)
        on = visible_regions(store, user)
        if on is not None:
            events = [e for e in events if not str(e.get("kind", "")).startswith("air_alert") or e.get("region") in on]
        return _resp(200, {"events": events})

    return _resp(404, "Not found", "text/plain")


_CACHE = {}


def lambda_handler(event, context):
    from store import DynamoStore

    # 19.11: підключення до DynamoDB і конфіг — один раз на «теплу» Lambda, а не на кожен запит
    # (boto3.resource() коштував ~1 с процесора: дашборд відповідав 2 с, займаючи слоти ліміту 10 одночасних)
    if "store" not in _CACHE:
        _CACHE["store"] = DynamoStore(os.environ["TABLE_NAME"])
        _CACHE["cfg"] = load_config()
    cfg, store = _CACHE["cfg"], _CACHE["store"]
    if event.get("warm"):  # EventBridge раз на 5 хв: Lambda тепла, екран блокування відповідає за десяті секунди
        import air_alerts, bot  # noqa: F401,E401  (імпорти lock_view — заздалегідь)
        return {"warm": True}
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "GET")
    body = event.get("body") or ""
    if event.get("isBase64Encoded") and body:
        import base64
        body = base64.b64decode(body).decode("utf-8", "replace")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return handle(store, event.get("rawPath", "/"), event.get("queryStringParameters") or {}, cfg,
                  json.loads(os.environ.get("WEB_KEYS", "{}")), method, body, headers)
