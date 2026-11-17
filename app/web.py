"""Дашборд і JSON API (Lambda Function URL). Доступ лише з секретним ключем ?k=… — інакше 404.
Читає тільки сховище, до Tuya не звертається (квоту не витрачає)."""
import hmac
import json
import os
import time

import schedule
import weather
from budget import plan
from devices import load_config
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
    user = user_for_key(keys, qs.get("k", ""))
    if not user:
        return _resp(404, "Not found", "text/plain")

    if path in PAGES:
        print(f"{path} opened by {user}")
        with open(os.path.join(HERE, PAGES[path]), encoding="utf-8") as f:
            return _resp(200, f.read(), "text/html; charset=utf-8")

    if path == "/api/state":
        states = [s for s in store.get_states().values() if not s.get("missing")]
        states.sort(key=lambda s: (not s.get("gateway"), not s.get("mains"), s.get("name", "")))
        rules = store.get_meta("rules") or {}
        p = plan(cfg, states) if states else None
        return _resp(200, {
            "now": int(time.time() * 1000),
            "devices": states,
            "outage": rules.get("outage"),
            "last_outage": rules.get("last_outage"),
            "internet": rules.get("internet"),
            "power": rules.get("power"),
            "last_internet_outage": rules.get("last_internet_outage"),
            "power_ids": [d["id"] for d in power_devices(states, cfg)],
            "schedule": schedule.for_web(store, cfg, int(time.time() * 1000)),
            "air_alerts": {k: v for k, v in (store.get_meta("air_alerts") or {}).items() if not k.startswith("_")},
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
        return _resp(200, {"events": store.get_events(limit=n)})

    return _resp(404, "Not found", "text/plain")


def lambda_handler(event, context):
    from store import DynamoStore

    cfg = load_config()
    store = DynamoStore(os.environ["TABLE_NAME"])
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "GET")
    body = event.get("body") or ""
    if event.get("isBase64Encoded") and body:
        import base64
        body = base64.b64decode(body).decode("utf-8", "replace")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return handle(store, event.get("rawPath", "/"), event.get("queryStringParameters") or {}, cfg,
                  json.loads(os.environ.get("WEB_KEYS", "{}")), method, body, headers)
