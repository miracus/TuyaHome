"""Повітряні тривоги з siren.pp.ua (обгортка Ukraine Alarm): стан і події початок/відбій.

Регіони — у кожного користувача бота свої (tg_users[uid].air_regions: [{name, ids, on}]), редагуються в боті.
Перевіряється об'єднання регіонів усіх користувачів; сповіщення — лише тим, у кого регіон увімкнений (🔔).
Поки користувачів немає (або в когось список ще не задавався) — регіони з config.air_alerts.regions."""
import json
import urllib.request
from datetime import datetime

from devices import fmt_duration, fmt_time

TYPES = {"AIR": "повітряна тривога", "ARTILLERY": "загроза артобстрілу", "URBAN_FIGHTS": "вуличні бої",
         "CHEMICAL": "хімічна загроза", "NUCLEAR": "радіаційна загроза", "INFO": "інформація"}


def _ms(iso):
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, AttributeError):
        return None


def fetch(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "TuyaHome"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


LEVELS = {"red": ("🔴", "червоний рівень"), "yellow": ("🟡", "жовтий рівень")}

REGIONS_URL = "https://siren.pp.ua/api/v3/regions"
_catalog = None  # кеш довідника на час життя Lambda


def catalog(cfg):
    """Довідник регіонів: {id: {id, name, type, parent, children}}; область → район → громада."""
    global _catalog
    if _catalog is None:
        data = fetch((cfg.get("air_alerts") or {}).get("regions_url", REGIONS_URL), timeout=10)
        idx = {}

        def walk(n, parent):
            rid = str(n["regionId"])
            kids = n.get("regionChildIds") or []
            idx[rid] = {"id": rid, "name": n["regionName"], "type": n.get("regionType"), "parent": parent,
                        "children": [str(k["regionId"]) for k in kids]}
            for k in kids:
                walk(k, rid)

        for st in data.get("states", []):
            if "Тестовий" not in st.get("regionName", ""):
                walk(st, None)
        _catalog = idx
    return _catalog


def region_entry(cat, rid):
    """Регіон для списку користувача: тривога діє, якщо активна в ньому самому або в будь-якому «батьку»
    (громада ← район ← область)."""
    ids, cur = [], rid
    while cur:
        ids.append(cur)
        cur = cat[cur]["parent"]
    return {"name": cat[rid]["name"], "ids": ids, "on": True}


def default_regions(cfg):
    return [{"name": r["name"], "ids": list(r["ids"]), "on": True} for r in (cfg.get("air_alerts") or {}).get("regions", [])]


def user_regions(u, cfg):
    regs = u.get("air_regions")
    return default_regions(cfg) if regs is None else regs


def watched(store, cfg):
    """{назва: {"ids", "to"}}: що перевіряти і кому слати. to = None — усім (користувачів бота ще немає)."""
    users = {uid: u for uid, u in (store.get_meta("tg_users") or {}).items() if u.get("status") == "approved"}
    if not users:
        return {r["name"]: {"ids": r["ids"], "to": None} for r in default_regions(cfg)}
    out = {}
    for uid, u in users.items():
        for r in user_regions(u, cfg):
            w = out.setdefault(r["name"], {"ids": r["ids"], "to": []})
            if r.get("on", True):
                w["to"].append(uid)
    return out


def status(alerts, ids):
    """Активні тривоги для набору regionId: активна?, з якого моменту, типи, рівень (red вищий за yellow)."""
    found = [a for a in alerts if str(a.get("regionId")) in ids and a.get("activeAlerts")]
    if not found:
        return {"active": False}
    types = sorted({x.get("type") for a in found for x in a["activeAlerts"]})
    levels = [lv for a in found for x in a["activeAlerts"] for lv in x.get("activeAlertLevels") or []]
    names = {str(lv.get("alertLevel", "")).lower() for lv in levels}
    level = "red" if "red" in names or not names else "yellow"
    starts = [_ms(lv.get("createdAt")) for lv in levels]
    starts = [s for s in starts if s] or [_ms(a.get("lastUpdate")) for a in found]
    return {"active": True, "since": min(s for s in starts if s), "types": types, "level": level,
            "where": sorted({a.get("regionName", "") for a in found})}


def run(store, cfg, notifier, now):
    ac = cfg.get("air_alerts")
    if not ac:
        return None
    try:
        alerts = fetch(ac["source_url"])
    except Exception as e:  # джерело недоступне — стан лишаємо як був
        return {"error": f"{type(e).__name__}: {e}"}
    meta = store.get_meta("air_alerts") or {}
    first = not meta
    out = {}
    for name, reg in watched(store, cfg).items():
        prev = meta.get(name) or {"active": False}
        cur = status(alerts, set(reg["ids"]))
        if cur["active"] and prev.get("active") and prev.get("since"):
            # тривога триває: початок — перший побачений. siren.pp.ua посеред тривоги може перевидати запис
            # з новим createdAt (16.11: тривога з 18:30, а відбій о 19:01 показав «2 хв (з 18:58)»)
            cur["since"] = min(prev["since"], cur["since"])

        def notify(text, ts, to=reg["to"]):
            if ac.get("notify", True) and not first and to != []:
                notifier.send(text, ts=ts, to=to, kind="air")
        if cur["active"] and not prev.get("active"):
            kinds = ", ".join(TYPES.get(t, t) for t in cur["types"])
            ico, lvl = LEVELS[cur["level"]]
            text = f"🚨 {kinds.capitalize()} ({ico} {lvl}): {name} (з {fmt_time(cur['since'], cfg)})"
            store.add_event(now, {"kind": "air_alert_start", "text": text, "region": name, "start": cur["since"], "level": cur["level"]})
            notify(text, cur["since"])
        elif cur["active"] and prev.get("active") and prev.get("level") and prev["level"] != cur["level"]:
            # рівень змінився посеред тривоги (жовтий ↔ червоний)
            ico, lvl = LEVELS[cur["level"]]
            head = "Тривога посилилась до червоного рівня" if cur["level"] == "red" else "Тривогу знижено до жовтого рівня"
            text = f"{ico} {head}: {name} (триває з {fmt_time(prev.get('since') or now, cfg)})"
            store.add_event(now, {"kind": "air_alert_level", "text": text, "region": name, "level": cur["level"]})
            notify(text, now)
        elif not cur["active"] and prev.get("active"):
            start = prev.get("since") or now
            text = f"✅ Відбій тривоги: {name}. Тривала {fmt_duration(now - start)} (з {fmt_time(start, cfg)})"
            store.add_event(now, {"kind": "air_alert_end", "text": text, "region": name, "start": start, "end": now})
            notify(text, now)
            cur["last"] = {"start": start, "end": now}
        elif not cur["active"]:
            cur["last"] = prev.get("last")
        out[name] = cur
    out["_checked"] = now
    store.put_meta("air_alerts", out)
    return {k: v["active"] for k, v in out.items() if not k.startswith("_")}
