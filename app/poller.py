"""Опитувач: запускається за розкладом (EventBridge) раз на snapshot_every_min.

1. Знімок усіх приладів одним запитом → стан, події (світло, тривоги, напруга, батареї), історія.
2. Детальна історія з журналів приладів за правилами source=log — кожен прилад × параметр зі своїм інтервалом.
3. Запобіжник квоти: лічильник реальних запитів за місяць; якщо йдемо швидше за план — детальні запити
   пропускаємо, а біля самого ліміту зупиняємось повністю.
"""
import os
import time
from datetime import datetime

import air_alerts
import arank
import schedule
import accuracy
import reports
import weather
from budget import device_rules, plan
from devices import fmt_time, is_number, load_config, local_tz, normalize, parse_model, parse_specification, scaled
from rules import MIN, _alert_due, evaluate, voltage_bad, voltage_in_outage, voltage_text


def _service_to(store):
    """Службові повідомлення (збої й ліміт Tuya) — лише власнику бота (рішення 16.11); власника ще немає — усім."""
    import bot
    return bot.owners(bot.get_users(store)) or None

SERVICE_EVENTS = {"power_source_down", "battery_drain"}  # події правил, що йдуть лише власнику
GRACE_MS = 30_000  # запуск за розкладом може прийти на кілька секунд раніше


def _month_bounds(now, cfg):
    dt = datetime.fromtimestamp(now / 1000, local_tz(cfg))
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return start.strftime("%Y-%m"), start.timestamp() * 1000, nxt.timestamp() * 1000


def _num(v):
    if is_number(v):
        return v
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v


def _models(store, api, raw_devices, cfg, now):
    out = {}
    refresh = cfg.get("model_refresh_days", 7) * 86_400_000
    for d in raw_devices:
        m = store.get_model(d["id"])
        if not m or now - m.get("fetched", 0) > refresh:
            # модель (власні коди прошивки) + специфікація (стандартні коди) — стан приходить у змішаних
            specs = {}
            r = api.get(f"/v1.0/iot-03/devices/{d['id']}/specification")
            if r.get("success"):
                specs.update(parse_specification(r.get("result") or {}))
            r = api.get(f"/v2.0/cloud/thing/{d['id']}/model")
            if r.get("success") and r.get("result", {}).get("model"):
                specs.update(parse_model(r["result"]["model"]))
            if specs:
                m = {"fetched": now, "specs": specs}
                store.put_model(d["id"], m)
        out[d["id"]] = (m or {}).get("specs", {})
    return out


def _tuya_rooms(store, api, raw_devices, cfg, now):
    """Кімнати Smart Life (дім → кімнати → прилади) раз на tuya_rooms_refresh_hours; для плану квартири."""
    meta = store.get_meta("tuya_rooms") or {}
    if now - meta.get("fetched", 0) < cfg.get("tuya_rooms_refresh_hours", 24) * 3_600_000:
        return
    homes = {d.get("owner_id") for d in raw_devices if d.get("owner_id")}
    by_device, rooms = {}, []
    for home in homes:
        r = api.get(f"/v1.0/homes/{home}/rooms")
        if not r.get("success"):
            return
        for room in (r.get("result") or {}).get("rooms") or []:
            rooms.append(room["name"])
            rd = api.get(f"/v1.0/homes/{home}/rooms/{room['room_id']}/devices")
            for d in rd.get("result") or [] if rd.get("success") else []:
                by_device[d["id"]] = room["name"]
    store.put_meta("tuya_rooms", {"fetched": now, "rooms": rooms, "by_device": by_device})


def _fetch_logs(api, dev_id, codes, start, end, max_pages=5):
    events, row_key = [], None
    for _ in range(max_pages):
        params = {"start_time": int(start), "end_time": int(end), "type": 7, "query_type": 1,
                  "size": 100, "codes": ",".join(sorted(codes))}
        if row_key:
            params["last_row_key"] = row_key
        r = api.get(f"/v2.0/cloud/thing/{dev_id}/logs", params)
        if not r.get("success"):
            return None, r
        res = r.get("result") or {}
        events += [e for e in res.get("logs", []) if e.get("code") in codes]
        row_key = res.get("last_row_key")
        if not res.get("has_next") or not row_key:
            break
    return events, None


def _dtek_replies(store, cfg, notifier, now):
    import dtek
    if not dtek.conf(cfg)["enabled"] or not dtek.watching(dtek.state(store), now, cfg):
        return None
    return dtek.check_replies(store, cfg, notifier, now)


def run(store, api, cfg, notifier, uid, now=None):
    now = now or int(time.time() * 1000)
    pcfg = cfg["polling"]
    summary = {"snapshot": False, "log_fetches": 0, "events": 0}

    # --- лічильник запитів за місяць
    month, m_start, m_end = _month_bounds(now, cfg)
    usage = store.get_meta("usage") or {}
    if usage.get("month") != month:
        usage = {"month": month, "calls": 0}
    quota = pcfg["monthly_api_quota"]
    budget = quota * pcfg.get("quota_safety_pct", 90) / 100
    on_pace = budget * (now - m_start) / (m_end - m_start) + budget * 0.02
    meta = store.get_meta("rules") or {}

    # --- черга сповіщень: спершу досилаємо те, що не дійшло раніше (порядок подій зберігається)
    outbox = store.get_meta("outbox") or {}
    notifier.outbox = list(outbox.get("items", []))
    notifier.dropped = outbox.get("dropped", 0)
    summary["outbox_sent"] = notifier.flush(lambda ts: fmt_time(ts, cfg, with_date=True))

    # --- графік відключень ДТЕК (з дзеркала на GitHub, квоту Tuya не витрачає)
    summary["schedule"] = schedule.run(store, cfg, notifier, now)
    # --- повітряні тривоги (siren.pp.ua, квоту Tuya не витрачає)
    summary["air_alerts"] = air_alerts.run(store, cfg, notifier, now)
    # --- рейтинг міст за тривогами: раз на добу дописує вчорашній день (без зберігання історії)
    try:
        summary["arank"] = arank.run(store, cfg, now)
    except Exception as e:
        summary["arank"] = f"{type(e).__name__}: {e}"
    # --- прогноз погоди (Open-Meteo, квоту Tuya не витрачає)
    try:
        summary["weather"] = weather.run(store, cfg, now)
    except Exception as e:  # погода не повинна ламати опитування
        summary["weather"] = f"{type(e).__name__}: {e}"
    # --- точність графіка ДТЕК (раз на добу) і регулярні повідомлення (ранковий підсумок, стан приладів за тиждень)
    for key, fn in (("sched_acc", lambda: accuracy.run(store, cfg, now)),
                    ("reports", lambda: reports.run(store, cfg, notifier, now))):
        try:
            summary[key] = fn()
        except Exception as e:  # не повинні ламати опитування
            summary[key] = f"{type(e).__name__}: {e}"
            print(f"[poller] {key}: {type(e).__name__}: {e}")
    # --- відповіді ДТЕК на заявку «немає світла» (лише поки заявка «жива», див. dtek.py)
    summary["dtek"] = _dtek_replies(store, cfg, notifier, now)

    def finish():
        usage["calls"] += api.calls
        store.put_meta("usage", usage)
        store.put_meta("rules", meta)
        store.put_meta("outbox", {"items": notifier.outbox, "dropped": 0 if not notifier.outbox else notifier.dropped})
        store.flush()
        summary.update(calls=api.calls, month_calls=usage["calls"], outbox=len(notifier.outbox))
        return summary

    if usage["calls"] >= quota * 0.98:
        if _alert_due(meta, "quota_stop", now, 24 * 60 * MIN):
            notifier.send(f"Ліміт Tuya API майже вичерпано ({usage['calls']}/{quota}) — опитування зупинено до кінця місяця",
                          to=_service_to(store), kind="service")
        summary["stopped"] = "quota"
        return finish()

    if not store.get_meta("first_poll"):  # від цього моменту є «факт» світла для графіка
        store.put_meta("first_poll", {"ts": now})

    prev = store.get_states()
    pl = plan(cfg, list(prev.values())) if prev else None
    snap_every = (pl["snapshot_every_min"] if pl else pcfg["snapshot_every_min"]) * MIN
    last_poll = store.get_meta("last_poll") or {}

    # --- 1. знімок
    if now - last_poll.get("ts", 0) >= snap_every - GRACE_MS:
        r = api.get(f"/v1.0/users/{uid}/devices")
        if not r.get("success") and r.get("code") not in (1010, 1011):  # поодинокий збій хмари (16.11 — 501 вночі) — ще раз
            time.sleep(3)
            r = api.get(f"/v1.0/users/{uid}/devices")
        fail = meta.get("api_fail") or {}
        if not r.get("success"):
            # сповіщення — лише якщо Tuya не відповідає ≥ 10 хв поспіль; поодинокі збої — тільки в журнал
            fail = {"since": fail.get("since") or now, "n": fail.get("n", 0) + 1, "notified": fail.get("notified", False)}
            if not fail["notified"] and now - fail["since"] >= 10 * MIN:
                notifier.send(f"⚠️ Tuya не відповідає з {fmt_time(fail['since'], cfg)}: code={r.get('code')} {r.get('msg')}. "
                              "Стан приладів зараз не оновлюється", to=_service_to(store), kind="service")
                fail["notified"] = True
            meta["api_fail"] = fail
            store.add_event(now, {"kind": "api_error", "text": f"Tuya API: {r.get('code')} {r.get('msg')}"})
            return finish()
        if fail:
            if fail.get("notified"):
                notifier.send(f"✅ Tuya знову відповідає (збій з {fmt_time(fail['since'], cfg)})", to=_service_to(store), kind="service")
            meta.pop("api_fail", None)
        raw = r["result"] or []
        # 16.11 05:57 Tuya один раз віддала порожній список — усі 14 приладів «зникли з акаунта».
        # Якщо разом «зникло» більше половини відомих приладів — це збій відповіді, знімок пропускаємо
        # (наступна спроба — за 2 хв). Справжнє масове видалення приймаємо після 5 таких знімків поспіль.
        ids = {d["id"] for d in raw}
        known = [i for i, st in prev.items() if not st.get("missing")]
        lost = [i for i in known if i not in ids]
        suspect = store.get_meta("snapshot_suspect") or {"n": 0}
        if known and len(lost) * 2 > len(known) and suspect["n"] < 5:
            store.put_meta("snapshot_suspect", {"n": suspect["n"] + 1, "ts": now, "got": len(raw), "known": len(known)})
            summary["snapshot_suspect"] = f"{len(raw)}/{len(known)}"
            print(f"[poller] підозрілий знімок: {len(raw)} приладів замість {len(known)} — пропускаю")
            return finish()
        if suspect["n"]:
            store.put_meta("snapshot_suspect", {"n": 0})
        # параметри, яких немає у звичайному стані (вологість/освітленість датчика руху), — з кешу тіньових властивостей
        shadow = store.get_meta("shadow") or {}
        for d in raw:
            have = {s["code"] for s in d.get("status", [])}
            d.setdefault("status", []).extend({"code": c, "value": v["value"]}
                                              for c, v in (shadow.get(d["id"]) or {}).items() if c not in have)
        specs = _models(store, api, raw, cfg, now)
        _tuya_rooms(store, api, raw, cfg, now)
        devices = [normalize(d, specs[d["id"]], cfg) for d in raw]

        try:  # чи зараз відключення за графіком — від цього залежить, як швидко оголошувати «немає світла»
            meta["sched_expected_off"] = schedule.expected_off(store, cfg, now)
        except Exception as e:
            meta["sched_expected_off"] = None
            print(f"[poller] expected_off: {type(e).__name__}: {e}")
        res = evaluate(prev, devices, meta, cfg, now)
        meta = res.meta
        for dev_id, code, value in res.history:
            store.add_history(dev_id, code, now, value)
        for st in res.states:
            store.put_state(st["id"], st)
        # «зник з акаунта» — лише якщо приладу немає 3 знімки поспіль (~6 хв), а не через один збій
        absent = store.get_meta("absent") or {}
        absent = {i: absent.get(i, 0) + 1 for i in set(prev) - ids if not prev[i].get("missing")}
        for gone, n in list(absent.items()):
            if n >= 3:
                store.put_state(gone, {**prev[gone], "missing": True})
                res.event("device_removed", f"Прилад зник з акаунта: {prev[gone].get('name')}", gone)
                absent.pop(gone)
        store.put_meta("absent", absent)
        for ev in res.events:
            if ev.get("kind") == "outage_end":  # + коли наступне відключення за графіком (якщо графік відомий)
                try:
                    nxt = schedule.next_off_text(store, cfg, now)
                except Exception as e:
                    nxt = None
                    print(f"[poller] next_off_text: {type(e).__name__}: {e}")
                if nxt:
                    ev["text"] += "\n" + nxt
            if ev.get("kind") == "outage_start":  # за графіком чи аварійне, і чи звертатись до ДТЕК
                try:
                    ctx = schedule.outage_context(store, cfg, ev.get("start") or now)
                except Exception as e:
                    ctx = None
                    print(f"[poller] outage_context: {type(e).__name__}: {e}")
                if ctx:
                    ev["text"] += "\n" + ctx
            store.add_event(now, ev)
            if ev["notify"]:
                # «розетка офлайн, але світло є» — службове, лише власнику (рішення 16.11)
                to = _service_to(store) if ev.get("kind") in SERVICE_EVENTS else None
                notifier.send(ev["text"], to=to, kind=ev.get("kind"))
        # світла досі немає, хоча за графіком мало вже бути — одне нагадування з порадою звернутись до ДТЕК
        out = meta.get("outage")
        if out:
            try:
                late = schedule.outage_late(store, cfg, out["start"], now)
                key = late and f"{out['start']}:{late[0]}"
                if late and (store.get_meta("outage_late") or {}).get("key") != key:
                    store.put_meta("outage_late", {"key": key, "ts": now})
                    store.add_event(now, {"kind": "outage_late", "text": late[1], "notify": True})
                    notifier.send(late[1], kind="outage_late")
            except Exception as e:
                print(f"[poller] outage_late: {type(e).__name__}: {e}")
        store.put_meta("last_poll", {"ts": now, "devices": len(devices)})
        summary.update(snapshot=True, events=len(res.events))
        prev = {s["id"]: s for s in res.states}
        pl = plan(cfg, list(prev.values()))

    # --- 2. детальна історія з журналів
    if pl and not pl["log_disabled"] and usage["calls"] + api.calls <= on_pace:
        log_meta = store.get_meta("logs") or {}
        for row in pl["devices"]:
            dev = prev.get(row["id"])
            if not dev or dev.get("missing"):
                continue
            last = log_meta.setdefault(row["id"], {})
            due = [c for c, every in row["intervals"].items() if now - last.get(c, 0) >= every * MIN - GRACE_MS]
            if not due:
                continue
            sources = {c: s["source"] for c, s in device_rules(cfg, dev).items()}
            shadow_due = [c for c in due if sources.get(c) == "shadow"]
            if shadow_due:  # тіньові властивості: один запит, значення — в кеш, далі їх підхопить знімок
                rs = api.get(f"/v2.0/cloud/thing/{row['id']}/shadow/properties", {"codes": ",".join(sorted(shadow_due))})
                summary["shadow_fetches"] = summary.get("shadow_fetches", 0) + 1
                if rs.get("success"):
                    cache = store.get_meta("shadow") or {}
                    for prop in (rs.get("result") or {}).get("properties") or []:
                        if prop.get("code") in shadow_due:
                            cache.setdefault(row["id"], {})[prop["code"]] = {"value": prop.get("value"), "ts": prop.get("time") or now}
                    store.put_meta("shadow", cache)
                for c in shadow_due:
                    last[c] = now
                due = [c for c in due if c not in shadow_due]
                if not due:
                    continue
            start = min(last.get(c) or now - row["intervals"][c] * MIN for c in due)
            events, err = _fetch_logs(api, row["id"], set(due), start, now)
            summary["log_fetches"] += 1
            if events is None:
                store.add_event(now, {"kind": "api_error", "text": f"Журнал {dev['name']}: {err.get('code')} {err.get('msg')}"})
                continue
            model = (store.get_model(row["id"]) or {}).get("specs", {})
            for e in events:
                # той самий масштаб і поправки, що й у знімку
                spec = dict(model.get(e["code"], {}))
                spec.update(cfg.get("model_overrides", {}).get(f"{dev['category']}/{e['code']}", {}))
                value = scaled(_num(e.get("value")), spec)
                store.add_history(row["id"], e["code"], e["event_time"], value)
                if "voltage" in e["code"] and is_number(value) and voltage_bad(value, cfg)                         and not voltage_in_outage(e["event_time"], meta, cfg):
                    if _alert_due(meta, f"volt:{row['id']}", now, cfg.get("voltage_alert_cooldown_min", 30) * MIN):
                        text = voltage_text(value, dev["name"], cfg)
                        notifier.send(f"{text} (о {fmt_time(e['event_time'], cfg)})", ts=e["event_time"], kind="voltage")
                        store.add_event(now, {"kind": "voltage", "text": text, "device_id": row["id"], "value": value})
            for c in due:
                last[c] = now
        store.put_meta("logs", log_meta)
    elif pl and pl["devices"]:
        summary["log_skipped"] = "pace"

    return finish()


def lambda_handler(event, context):
    from notify import Notifier
    from store import DynamoStore, TokenCache
    from tuya_cloud import TuyaCloud

    cfg = load_config()
    store = DynamoStore(os.environ["TABLE_NAME"], cfg.get("history_ttl_days", 180), cfg.get("events_ttl_days", 730))
    api = TuyaCloud(os.environ["TUYA_ACCESS_ID"], os.environ["TUYA_ACCESS_SECRET"],
                    os.environ["TUYA_ENDPOINT"], token_cache=TokenCache(store))
    import bot
    # отримувачі — особисті чати підтверджених користувачів бота; поки їх немає — стара група
    recipients = bot.approved_chat_ids(store) or [os.environ.get("TELEGRAM_CHAT_ID", "")]
    notifier = Notifier(os.environ.get("TELEGRAM_BOT_TOKEN", ""), recipients,
                        enabled=cfg.get("notifications_enabled", True))
    notifier.markup = bot.keyboards(store)
    notifier.silent = bot.silencer(store, cfg)
    if isinstance(event, dict) and event.get("tg_setup"):  # реєстрація webhook і меню команд бота
        return bot.setup(notifier, os.environ["WEB_URL"].rstrip("/") + "/tg", os.environ["TG_SECRET"],
                         store, os.environ.get("TELEGRAM_CHAT_ID", ""))
    if isinstance(event, dict) and event.get("dtek_ping"):  # перевірка Telethon/входу без жодних повідомлень
        import dtek
        return dtek.ping(cfg)
    if isinstance(event, dict) and event.get("dtek_submit"):  # кнопка «🆘 Заявка ДТЕК» (web-Lambda, асинхронно)
        import dtek
        outbox = store.get_meta("outbox") or {}
        notifier.outbox = list(outbox.get("items", []))
        res = dtek.run_submit(store, cfg, notifier)
        store.put_meta("outbox", {"items": notifier.outbox, "dropped": outbox.get("dropped", 0)})
        store.flush()
        print("[dtek] submit:", {k: v for k, v in res.items() if k != "replies"})
        return {k: v for k, v in res.items() if k != "replies"}
    if isinstance(event, dict) and event.get("test_message"):  # ручна перевірка зв'язку з Telegram
        me_ok, me = notifier.call("getMe")
        ok = notifier.send(str(event["test_message"]))
        return {"getMe": me if me_ok else f"error {me}", "sent": ok, "error": notifier.last_error}
    summary = run(store, api, cfg, notifier, os.environ["TUYA_UID"])
    print(summary)
    return summary
