"""Правила: що змінилося між попереднім і поточним опитуванням → історія, події, сповіщення."""
from budget import device_rules
from devices import dp, fmt_duration, fmt_time, is_number

MIN = 60_000
HOUR = 60 * MIN


class Result:
    def __init__(self):
        self.states = []
        self.history = []  # (device_id, code, value)
        self.events = []   # {"kind", "text", "device_id", "notify"}
        self.meta = {}

    def event(self, kind, text, device_id=None, notify=False, **extra):
        self.events.append({"kind": kind, "text": text, "device_id": device_id, "notify": notify, **extra})


def _alert_due(meta, key, now, cooldown_ms):
    alerts = meta.setdefault("alerts", {})
    if now - alerts.get(key, 0) >= cooldown_ms:
        alerts[key] = now
        return True
    return False


def _is_alarm(value, cfg):
    return str(value).lower() in [v.lower() for v in cfg.get("alarm_values", [])]


# Текст тривоги за типом датчика (за кодом DP): (тривога, відбій)
ALARM_TEXT = (
    ("water", "💧 ЗАТОПЛЕННЯ! Спрацював датчик «{name}»", "✅ Затоплення: норма — датчик «{name}»"),
    ("gas", "🔥 ГАЗ! Спрацював датчик «{name}»", "✅ Газ: норма — датчик «{name}»"),
    ("smoke", "💨 ДИМ! Спрацював датчик «{name}»", "✅ Дим: норма — датчик «{name}»"),
    ("master", "🚨 Тривога хаба «{name}»", "✅ Хаб «{name}»: тривогу знято"),
)


def alarm_text(code, name, on):
    for key, alarm, clear in ALARM_TEXT:
        if key in code:
            return (alarm if on else clear).format(name=name)
    return f"🚨 Тривога: «{name}» ({code})" if on else f"✅ «{name}»: тривогу знято"


def voltage_text(v, name, cfg):
    hi, lo = cfg.get("voltage_high", 248), cfg.get("voltage_low", 200)
    return f"⚡ Напруга поза нормою: {v} В (норма {lo}–{hi}) · {name}"


def voltage_bad(v, cfg):
    return v > cfg.get("voltage_high", 248) or v < cfg.get("voltage_low", 200)


def evaluate(prev_states, devices, meta, cfg, now):
    res = Result()
    meta = dict(meta or {})
    first_run = not prev_states
    default_every = cfg["polling"].get("default_store_every_min", 30) * MIN

    for dev in devices:
        prev = prev_states.get(dev["id"])
        state = dict(dev)
        name = dev["name"]

        # --- поява / перейменування
        if prev is None:
            # для офлайн-приладу Tuya update_time — момент останнього звіту, тобто «офлайн з»
            state["online_since" if dev["online"] else "offline_since"] = (
                now if dev["online"] else (dev.get("update_time") or now))
            if not first_run:
                res.event("device_new", f"Новий прилад: {name} ({dev['product']})", dev["id"], notify=True)
        elif prev.get("name") != name:
            res.event("device_renamed", f"Перейменовано: {prev.get('name')} → {name}", dev["id"])

        # --- online / offline
        if prev is not None:
            state["online_since"] = prev.get("online_since")
            state["offline_since"] = prev.get("offline_since")
            if prev.get("online") != dev["online"]:
                if dev["online"]:
                    state["online_since"] = now
                    off = prev.get("offline_since")
                    took = f" (не було {fmt_duration(now - off)})" if off else ""
                    # про хаб сповіщає подія «інтернет», тут лише журнал
                    res.event("online", f"{name} знову онлайн{took}", dev["id"])
                    meta.setdefault("alerts", {}).pop(f"offline:{dev['id']}", None)
                else:
                    state["offline_since"] = now
                    res.event("offline", f"{name} офлайн", dev["id"])
        if not dev["online"] and state.get("offline_since"):
            long_ms = cfg.get("device_offline_alert_hours", 6) * HOUR
            if now - state["offline_since"] >= long_ms and _alert_due(meta, f"offline:{dev['id']}", now, 10**15):
                res.event("offline_long",
                          f"{name} офлайн уже {fmt_duration(now - state['offline_since'])}", dev["id"], notify=True)

        # --- тривоги (протікання, газ, дим, хаб)
        alarms = dict(prev.get("alarm", {})) if prev else {}
        for code in cfg.get("alarm_codes", []):
            d = dp(dev, code)
            if not d:
                continue
            now_alarm = _is_alarm(d["value"], cfg)
            if now_alarm != alarms.get(code, False) and not (first_run and not now_alarm):
                text = alarm_text(code, name, now_alarm)
                res.event("alarm" if now_alarm else "alarm_clear", text, dev["id"], notify=True, code=code)
            alarms[code] = now_alarm
        state["alarm"] = alarms

        if dev["online"]:
            # --- напруга
            for d in dev["dps"]:
                if "voltage" in d["code"] and d["unit"] == "V" and is_number(d["value"]):
                    v = d["value"]
                    if voltage_bad(v, cfg) and _alert_due(
                            meta, f"volt:{dev['id']}", now, cfg.get("voltage_alert_cooldown_min", 30) * MIN):
                        res.event("voltage", voltage_text(v, name, cfg), dev["id"], notify=True, value=v)

            # --- батарея
            for d in dev["dps"]:
                if "battery" in d["code"].lower() and d["unit"] == "%" and is_number(d["value"]):
                    if d["value"] < cfg.get("battery_low_pct", 15) and _alert_due(
                            meta, f"batt:{dev['id']}", now, 24 * HOUR):
                        res.event("battery", f"Сідає батарея: {name} — {d['value']} %", dev["id"], notify=True)

            # --- історія зі знімка. DP з source=log пише журнал приладу (poller), тут їх пропускаємо.
            hist_ts = dict(prev.get("hist_ts", {})) if prev else {}
            prev_vals = {d["code"]: d["value"] for d in prev.get("dps", [])} if prev else {}
            rules = device_rules(cfg, dev)
            for d in dev["dps"]:
                rule = rules.get(d["code"])
                if rule and rule["source"] == "log":
                    continue
                changed = prev_vals.get(d["code"]) != d["value"]
                since = now - hist_ts.get(d["code"], 0)
                if rule:  # явне правило: числове — рівно раз на every_min
                    due = since >= rule["every_min"] * MIN if is_number(d["value"]) else changed
                else:     # за замовчуванням: при зміні, і не рідше ніж раз на default_store_every_min
                    due = changed or (is_number(d["value"]) and since >= default_every)
                if due:
                    res.history.append((dev["id"], d["code"], d["value"]))
                    hist_ts[d["code"]] = now
            state["hist_ts"] = hist_ts
        elif prev:
            state["hist_ts"] = prev.get("hist_ts", {})

        state["seen"] = now
        res.states.append(state)

    _power_and_internet(res, devices, meta, cfg, now)
    res.meta = meta
    return res


def power_devices(devices, cfg):
    """Прилади, за якими визначаємо світло (config.power_sources); якщо не задано — усі «від мережі»."""
    sources = cfg.get("power_sources") or []
    picked = [d for d in devices if any(all(d.get(k) == v for k, v in s.items()) for s in sources)]
    return picked or [d for d in devices if d["mains"]]


def _power_and_internet(res, devices, meta, cfg, now):
    """Інтернет = хаб онлайн (хаб на зарядній станції). Світло = розетка онлайн, але лише поки хаб на зв'язку:
    розетка — Zigbee через хаб, тож без хаба її стан невідомий.
    Типовий порядок при відключенні: зникає розетка (немає світла) → згодом зникає хаб (немає інтернету)."""
    power = power_devices(devices, cfg)
    gateways = [d for d in devices if d["gateway"]]
    states = {s["id"]: s for s in res.states}
    outage = meta.get("outage")
    hub_online = any(g["online"] for g in gateways) if gateways else True

    # --- інтернет (за хабом)
    if gateways:
        net = meta.get("internet") or {"up": True}
        if net.get("up") and not hub_online:
            start = max(states[g["id"]].get("offline_since") or now for g in gateways)
            light = (f"світла немає з {fmt_time(outage['start'], cfg)}" if outage
                     else "що зі світлом — невідомо (розетка працює через хаб)")
            meta["internet"] = {"up": False, "since": start, "outage_at_start": bool(outage)}
            res.event("internet_down", f"📡 Немає інтернету з {fmt_time(start, cfg)} — зв'язок дому з системою втрачено (хаб офлайн); {light}",
                      notify=True, start=start)
        elif not net.get("up") and hub_online:
            start = net["since"]
            meta["internet"] = {"up": True, "since": now}
            meta["last_internet_outage"] = {"start": start, "end": now}
            res.event("internet_up", f"🌐 Інтернет є з {fmt_time(now, cfg)}. Не було {fmt_duration(now - start)}",
                      notify=True, start=start, end=now)
            net_was_down_since = start
        else:
            net_was_down_since = None
            if not meta.get("internet"):
                meta["internet"] = {"up": hub_online, "since": now}
    else:
        net_was_down_since = None

    if not power or not hub_online:
        return  # без хаба стан розетки невідомий — світло лишаємо як було

    # --- світло: розетка + «свідки» — інші прилади від мережі (напр. датчик газу). Розетка може зникнути сама
    # (вийняли, збій Zigbee) — 15.11 13:17 так було хибне «немає світла», хоча датчик газу був на зв'язку.
    # Немає світла = офлайн УСІ прилади від мережі; офлайн лише розетка — окреме попередження.
    ids = {d["id"] for d in power}
    witnesses = [d for d in devices if d["mains"] and not d["gateway"] and d["id"] not in ids]
    watch = power + witnesses
    alive = [d for d in witnesses if d["online"]]
    if not any(d["online"] for d in power) and alive:
        gone = min(states[d["id"]].get("offline_since") or now for d in power)
        if not meta.get("power_source_down") and now - gone >= 10 * 60_000:  # короткі збої Zigbee (1–2 хв) не шлемо
            meta["power_source_down"] = True
            res.event("power_source_down", f"⚠️ Розетка «{', '.join(d['name'] for d in power)}» офлайн, але "
                      f"«{', '.join(d['name'] for d in alive)}» (від мережі) на зв'язку — світло, ймовірно, є. "
                      f"Перевірте розетку.", notify=True)
    elif any(d["online"] for d in power):
        meta["power_source_down"] = False

    if outage and not any(d["online"] for d in power) and any(
            (states[d["id"]].get("online_since") or now) < outage["start"] for d in alive):
        # «відключення» почалось, коли свідок уже був онлайн — це зникла лише розетка, а не світло
        meta["outage"] = None
        res.event("outage_cancel", f"✅ Світло є: «немає світла з {fmt_time(outage['start'], cfg)}» — хибне, "
                  f"офлайн лише розетка, а «{', '.join(d['name'] for d in alive)}» весь час на зв'язку", notify=True)
        outage = None
    elif not outage and all(not d["online"] for d in watch):
        start = max(states[d["id"]].get("offline_since") or now for d in watch)
        meta["outage"] = {"start": start}
        res.event("outage_start", f"🔌 Немає світла з {fmt_time(start, cfg)}", notify=True, start=start)
    elif outage and any(d["online"] for d in watch):
        start = outage["start"]
        meta["outage"] = None
        meta["last_outage"] = {"start": start, "end": now}
        # світло повернулось, поки не було інтернету — точний час невідомий
        approx = (f" (точний час невідомий: не було інтернету з {fmt_time(net_was_down_since, cfg)})"
                  if net_was_down_since else "")
        res.event("outage_end", f"💡 Світло є з {fmt_time(now, cfg)}. Не було {fmt_duration(now - start)} "
                                f"(з {fmt_time(start, cfg, with_date=True)}){approx}",
                  notify=True, start=start, end=now)

    # «світло є з …» для дашборда: момент останньої зміни; уперше — з коли розетка онлайн
    if meta.get("outage"):
        meta["power"] = {"on": False, "since": meta["outage"]["start"]}
    elif not (meta.get("power") or {}).get("on"):
        last = meta.get("last_outage")
        since = last["end"] if last else min((states[d["id"]].get("online_since") or now for d in watch if d["online"]),
                                             default=now)
        meta["power"] = {"on": True, "since": since}
