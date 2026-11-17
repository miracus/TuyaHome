"""Команди Telegram-бота: відповіді з бази (без запитів до Tuya). Webhook приходить у web-Lambda (/tg)."""
import time
from datetime import datetime

import address
import air_alerts
import weather
import schedule
from devices import fmt_duration, fmt_time, local_tz
from rules import power_devices

# команда → (опис для меню, функція)
COMMANDS = {}


def command(name, description):
    def deco(fn):
        COMMANDS[name] = (description, fn)
        return fn
    return deco


def _ctx(store, cfg, u=None):
    now = int(time.time() * 1000)
    states = [s for s in store.get_states().values() if not s.get("missing")]
    alerts = {k: v for k, v in (store.get_meta("air_alerts") or {}).items() if not k.startswith("_")}
    if u is not None:  # лише регіони цього користувача, з позначкою 🔔/🔕
        alerts = {r["name"]: {**(alerts.get(r["name"]) or {}), "on": r.get("on", True)}
                  for r in air_alerts.user_regions(u, cfg)}
    return {"now": now, "states": states, "rules": store.get_meta("rules") or {}, "alerts": alerts,
            "last_poll": (store.get_meta("last_poll") or {}).get("ts"), "store": store, "cfg": cfg, "u": u}


def _dp(dev, pred):
    return next((d for d in dev.get("dps", []) if pred(d)), None)


def _hub_online(c):
    hubs = [s for s in c["states"] if s.get("gateway")]
    return any(h.get("online") for h in hubs) if hubs else True


def _ago(c, ts):
    return fmt_duration(c["now"] - ts) if ts else "—"


def light_text(c):
    cfg, r = c["cfg"], c["rules"]
    if r.get("outage"):
        s = r["outage"]["start"]
        line = f"🔌 Світла немає з {fmt_time(s, cfg)} ({_ago(c, s)})"
    elif not _hub_online(c):
        line = "❔ Світло: невідомо — немає зв'язку з домом (хаб офлайн)"
    else:
        since = (r.get("power") or {}).get("since") or (r.get("last_outage") or {}).get("end")
        line = f"💡 Світло є{f' з {fmt_time(since, cfg, with_date=True)} ({_ago(c, since)})' if since else ''}"
    sc = schedule.for_web(c["store"], cfg, c["now"])
    if sc and sc.get("next_off"):
        a, b = sc["next_off"]
        end = "24:00" if fmt_time(b, cfg) == "00:00" else fmt_time(b, cfg)
        line += (f"\n📅 За графіком (черга {sc['group']}): зараз відключення до {end}" if a <= c["now"]
                 else f"\n📅 Наступне за графіком (черга {sc['group']}): {fmt_time(a, cfg, with_date=True)}–{end}")
    return line


def internet_text(c):
    cfg, net = c["cfg"], c["rules"].get("internet") or {}
    if _hub_online(c):
        return f"🌐 Інтернет є{f' з {fmt_time(net['since'], cfg, with_date=True)} ({_ago(c, net["since"])})' if net.get('since') else ''}"
    since = net.get("since")
    return f"📡 Немає інтернету{f' з {fmt_time(since, cfg)} ({_ago(c, since)})' if since else ''} — хаб офлайн"


def voltage_text(c):
    cfg, lines = c["cfg"], []
    lo, hi = cfg.get("voltage_low", 200), cfg.get("voltage_high", 248)
    for dev in power_devices(c["states"], cfg):
        d = _dp(dev, lambda x: "voltage" in x["code"])
        if not d:
            continue
        if not dev.get("online"):
            lines.append(f"⚡ {dev['name']}: офлайн (останнє {d['value']} В)")
            continue
        ok = lo <= d["value"] <= hi
        hist = [v for _, v in c["store"].get_history(dev["id"], d["code"], c["now"] - 86_400_000) if isinstance(v, (int, float))]
        rng = f"\n   за 24 год: мін {min(hist)} · макс {max(hist)} В" if hist else ""
        lines.append(f"{'✅' if ok else '⚠️'} Напруга {d['value']} В (норма {lo}–{hi}) · {dev['name']}{rng}")
    lines.append(f"🕒 оновлено {fmt_time(c['last_poll'], cfg) if c['last_poll'] else '—'}")
    return "\n".join(lines)


def queue_unknown_text(zone):
    z = address.ZONES.get(zone, {})
    return (f"⚠️ Черга для вашої адреси невідома: {z.get('provider', 'ДТЕК')} не дає визначити її автоматично. "
            f"Оберіть чергу кнопкою «📍 Моя адреса» → «🔢 Обрати чергу»; дізнатися її можна на сайті: {z.get('lookup', '')}\n"
            f"Поки черга не обрана, графіка й сповіщень про його зміни не буде.")


NO_ZONE_TEXT = ("⚠️ Для вашої області графіків відключень у боті немає (є лише для м. Київ, Київської, "
                "Дніпропетровської та Одеської областей) — приходитимуть лише тривоги.")


def schedule_text(c):
    cfg, now, u = c["cfg"], c["now"], c.get("u") or {}
    addr = u.get("addr")
    if addr is not None and not addr.get("zone"):
        return NO_ZONE_TEXT
    if addr and not addr.get("group"):
        return queue_unknown_text(addr["zone"])
    if addr is None and u.get("access") == "guest":
        return "📍 Спершу вкажіть адресу кнопкою «📍 Моя адреса» — бот знайде графік і тривоги для неї."
    sc = schedule.for_web(c["store"], cfg, now, *((addr["zone"], addr["group"]) if addr else ()))
    if not sc or not sc.get("days"):
        return "📅 Графіка ДТЕК поки немає (завантажиться за кілька хвилин)"

    def span(a, b):
        end = "24:00" if fmt_time(b, cfg) == "00:00" else fmt_time(b, cfg)
        mark = " ◀ зараз" if a <= now < b else ""
        return f"{fmt_time(a, cfg)}–{end}{mark}"

    zname = address.ZONES.get(addr["zone"], {}).get("name") if addr else None
    lines = [f"📅 Графік ДТЕК · {zname + ' · ' if zname else ''}черга {sc['group']}"]
    for d in sc["days"]:
        lines.append(f"\n{d['label'][:1].upper()}{d['label'][1:]}:")
        if d["off"]:
            lines += [f"🔴 {span(a, b)}" for a, b in d["off"]]
        if d["maybe"]:
            lines += [f"🟡 можливо {span(a, b)}" for a, b in d["maybe"]]
        if not d["off"] and not d["maybe"]:
            lines.append("🟢 відключень немає")
    if len(sc["days"]) == 1:
        lines.append("\nЗавтра: ДТЕК ще не опублікував графік")
    if sc.get("update"):
        lines.append(f"\nОновлено ДТЕК: {sc['update']}")
    return "\n".join(lines)


def check_text(c):
    cfg = c["cfg"]
    out = ["🩺 Повний чекап", light_text(c).split("\n")[0], internet_text(c)]
    out.append(voltage_text(c).split("\n")[0])
    for name, a in c["alerts"].items():
        mute = " 🔕" if a.get("on") is False else ""
        out.append(f"🚨 {name}: тривога ({'жовтий' if a.get('level') == 'yellow' else 'червоний'} рівень, {_ago(c, a.get('since'))}){mute}"
                   if a.get("active") else f"🕊️ {name}: тихо{mute}")
    groups = [("💧 Протікання", r"water"), ("🔥 Газ", r"gas_sensor_st"), ("💨 Дим", r"smoke_sensor_st")]
    import re
    for title, pat in groups:
        devs = [(s, _dp(s, lambda x: re.search(pat, x["code"]))) for s in c["states"]]
        devs = [(s, d) for s, d in devs if d]
        if not devs:
            continue
        bad = [s["name"] for s, d in devs if str(d["value"]).lower() == "alarm"]
        off = [s["name"] for s, d in devs if not s.get("online")]
        state = (f"❌ ТРИВОГА: {', '.join(bad)}" if bad else "✅ норма") + (f" · офлайн: {', '.join(off)}" if off else "")
        out.append(f"{title} ({len(devs)}): {state}")
    door = [s for s in c["states"] if s.get("category") == "mcs"]
    for s in door:
        d = _dp(s, lambda x: x["code"] in ("prm_switch", "doorcontact_state", "switch"))
        if d is not None:
            out.append(f"🚪 {s['name']}: {'відчинено ⚠️' if d['value'] else 'зачинено'}")
    low = [f"{s['name']} {b['value']}%" for s in c["states"]
           for b in [_dp(s, lambda x: "battery" in x["code"].lower() and x.get("unit") == "%")]
           if b and isinstance(b["value"], (int, float)) and b["value"] < cfg.get("battery_low_pct", 15) + 5]
    out.append(f"🔋 Низький заряд: {', '.join(low)}" if low else "🔋 Батареї: в нормі")
    offline = [s["name"] for s in c["states"] if not s.get("online")]
    out.append(f"📴 Офлайн: {', '.join(offline)}" if offline else "📶 Усі прилади онлайн")
    return "\n".join(out)


@command("light", "💡 Чи є світло")
def cmd_light(c):
    return light_text(c)


@command("internet", "🌐 Чи є інтернет")
def cmd_internet(c):
    return internet_text(c)


@command("voltage", "⚡ Поточна напруга")
def cmd_voltage(c):
    return voltage_text(c)


@command("check", "🩺 Повний чекап: світло, протікання, газ, дим, напруга")
def cmd_check(c):
    return check_text(c)


@command("schedule", "📅 Графік відключень ДТЕК на сьогодні й завтра")
def cmd_schedule(c):
    return schedule_text(c)


@command("alerts", "🚨 Тривоги: стан і мої регіони")
def cmd_alerts(c):
    return None  # відповідь з кнопками регіонів — формується в handle_update


@command("address", "📍 Моя адреса: графік і тривоги для неї")
def cmd_address(c):
    return None  # діалог — у handle_update


@command("sound", "🔔 Звук сповіщень: тихий і нічний режим")
def cmd_sound(c):
    return None  # відповідь з кнопками — формується в handle_update


@command("dtek", "🆘 Заявка в ДТЕК: немає світла")
def cmd_dtek(c):
    return None  # питання з кнопками — у handle_update


@command("dashboard", "📊 Відкрити дашборд")
def cmd_dashboard(c):
    return None  # відповідь — кнопка, формується в handle_update


@command("weather", "🌤 Погода: прогноз і датчик «Назовні»")
def cmd_weather(c):
    return weather.weather_text(c["store"], c["cfg"], c["now"], c["states"])


@command("help", "Що вміє бот")
def cmd_help(c):
    return "Команди:\n" + "\n".join(f"/{n} — {d}" for n, (d, _) in COMMANDS.items() if n != "help")


# Кнопки під полем вводу в особистому чаті (Telegram не дозволяє прибрати поле вводу — кнопки замість набору тексту)
BUTTONS = [["💡 Світло", "🌐 Інтернет"], ["⚡ Напруга", "🩺 Чекап", "🌤 Погода"], ["🚨 Тривоги", "📅 Графік ДТЕК", "🆘 Заявка ДТЕК"],
           ["📊 Дашборд", "📍 Моя адреса", "🔔 Звук"]]
BUTTON_CMD = {"💡 Світло": "light", "🌐 Інтернет": "internet", "⚡ Напруга": "voltage", "🩺 Чекап": "check",
              "🚨 Тривоги": "alerts", "📅 Графік ДТЕК": "schedule", "📊 Дашборд": "dashboard",
              "🌤 Погода": "weather"}
KEYBOARD = {"keyboard": [[{"text": t} for t in row] for row in BUTTONS], "resize_keyboard": True,
            "is_persistent": True, "input_field_placeholder": "Оберіть кнопку нижче"}
OWNER_BUTTONS = BUTTONS + [["👥 Користувачі"]]
OWNER_KEYBOARD = {**KEYBOARD, "keyboard": [[{"text": t} for t in row] for row in OWNER_BUTTONS]}
BUTTON_CMD["👥 Користувачі"] = "users"
BUTTON_CMD["📍 Моя адреса"] = "address"
BUTTON_CMD["🔔 Звук"] = "sound"
BUTTON_CMD["🆘 Заявка ДТЕК"] = "dtek"
# Гість — лише тривоги й графік для своєї адреси (без даних дому)
GUEST_BUTTONS = [["🚨 Тривоги", "📅 Графік ДТЕК"], ["📍 Моя адреса", "🔔 Звук"]]
GUEST_KEYBOARD = {**KEYBOARD, "keyboard": [[{"text": t} for t in row] for row in GUEST_BUTTONS]}
GUEST_CMDS = {"start", "alerts", "schedule", "address", "sound"}
ADDR_KEYBOARD = {"keyboard": [[{"text": "📍 Надіслати геолокацію", "request_location": True}], [{"text": "‹ Скасувати"}]],
                 "resize_keyboard": True, "one_time_keyboard": True, "input_field_placeholder": "Місто, вулиця, будинок"}


def kb_for(u):
    return OWNER_KEYBOARD if u.get("role") == "owner" else GUEST_KEYBOARD if u.get("access") == "guest" else KEYBOARD


# --- Хто користується ботом. Сповіщення й відповіді — лише підтвердженим, кожному в особистий чат.
# meta tg_users: {user_id: {"name", "role": "owner"|"member", "status": "approved"|"pending"|"denied", "since"}}
def get_users(store):
    return store.get_meta("tg_users") or {}


def save_users(store, users):
    store.put_meta("tg_users", users)


def approved_chat_ids(store):
    """Отримувачі подій ДОМУ (світло, газ, напруга…) — лише група «Дім». Тривоги й графіки — окремо, кожному свої."""
    return [uid for uid, u in get_users(store).items() if u.get("status") == "approved" and u.get("access") != "guest"]


def keyboards(store):
    """Кнопки кожного схваленого користувача — їх чіпляємо до сповіщень, щоб клавіатура не губилась
    (напр. після «Очистити історію» в Telegram)."""
    return {uid: kb_for(u) for uid, u in get_users(store).items() if u.get("status") == "approved"}


def owners(users):
    return [uid for uid, u in users.items() if u.get("role") == "owner" and u.get("status") == "approved"]


def _group_status(notifier, group_id, user_id):
    """Статус у старій групі «Дім» — для автоматичного перенесення її учасників."""
    if not group_id:
        return ""
    ok, r = notifier.call("getChatMember", {"chat_id": group_id, "user_id": user_id}, retries=0)
    return (r.get("result") or {}).get("status", "") if ok else ""


def _name(user):
    n = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    return n + (f" (@{user['username']})" if user.get("username") else "") if n else str(user.get("id"))


def dashboard_link(web, role):
    """Власник — посилання owner, решта — member (кожне вимикається окремо)."""
    keys, base = web.get("keys") or {}, (web.get("base") or "").rstrip("/") + "/"
    key = "owner" if role == "owner" and "owner" in keys else "member" if "member" in keys else next(iter(keys), None)
    return f"{base}?k={keys[key]}" if key else None


def dashboard_reply(notifier, chat_id, link):
    """Кнопка «відкрити в Telegram» (міні-застосунок) + запасна «у браузері»."""
    if not link:
        return _send(notifier, chat_id, "Посилання на дашборд не налаштоване.")
    return _send(notifier, chat_id, "📊 Дашборд дому", reply_markup={"inline_keyboard": [
        [{"text": "📊 Відкрити дашборд", "web_app": {"url": link}}],
        [{"text": "🌐 Відкрити в браузері", "url": link}]]})


def _send(notifier, chat_id, text, **extra):
    return notifier.call("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True, **extra},
                         retries=0)


def _welcome(notifier, uid, u, web):
    kb = kb_for(u)
    if u.get("access") == "guest":
        _send(notifier, uid, "Готово ✅ Сюди приходитимуть повітряні тривоги і зміни графіка відключень для вашої адреси. "
                             + ("" if u.get("addr") else "Спершу натисніть «📍 Моя адреса». ") + "Кнопки нижче 👇", reply_markup=kb)
    else:
        _send(notifier, uid, "Готово ✅ Сюди приходитимуть сповіщення дому (світло, інтернет, тривоги, протікання, газ, дим, "
                             "напруга, графік ДТЕК). Кнопки нижче — щоб перевірити стан будь-коли 👇", reply_markup=kb)
    # синя кнопка біля поля вводу — «Меню» зі списком команд (Telegram дозволяє лише одну кнопку:
    # або меню, або міні-застосунок; дашборд — кнопкою «📊 Дашборд» унизу і командою /dashboard)
    notifier.call("setChatMenuButton", {"chat_id": uid, "menu_button": {"type": "commands"}}, retries=0)
    _owner_commands(notifier, uid, u)


def menu_commands(owner=False):
    cmds = [{"command": "start", "description": "⌨️ Показати кнопки"}]
    cmds += [{"command": n, "description": d} for n, (d, _) in COMMANDS.items() if n != "help"]
    if owner:
        cmds.append({"command": "users", "description": "👥 Користувачі бота"})
    return cmds


def _owner_commands(notifier, uid, u):
    """Меню команд на людину: власник — усе + користувачі; гість — лише свої; решта — загальне меню."""
    if u.get("role") == "owner":
        cmds = menu_commands(owner=True)
    elif u.get("access") == "guest":
        cmds = [c for c in menu_commands() if c["command"] in GUEST_CMDS]
    else:
        notifier.call("deleteMyCommands", {"scope": {"type": "chat", "chat_id": int(uid)}}, retries=0)
        return
    notifier.call("setMyCommands", {"commands": cmds, "scope": {"type": "chat", "chat_id": int(uid)}}, retries=0)


def users_text(users):
    rows = []
    for uid, u in sorted(users.items(), key=lambda x: (x[1].get("status") != "approved", x[1].get("role") != "owner")):
        mark = {"approved": "✅", "pending": "⏳", "denied": "⛔"}.get(u.get("status"), "?")
        grp = "👤 гість" if u.get("access") == "guest" else "🏠 дім"
        a = u.get("addr") or {}
        where = f" · {a.get('label')}" + (f", черга {a['group'].replace('GPV', '')}" if a.get("group") else
                                          ", черга невідома" if a.get("zone") else "") if a else ""
        rows.append(f"{mark} {u.get('name', uid)} — {'власник' if u.get('role') == 'owner' else grp}{where}")
    return "👥 Користувачі бота:\n" + "\n".join(rows or ["—"]) + "\n\n🏠 дім — усе; 👤 гість — лише тривоги й графік"


def users_markup(users):
    btns = [[{"text": f"{'🏠 → дім' if u.get('access') == 'guest' else '👤 → гість'}: {u.get('name', uid)[:24]}",
              "callback_data": f"{'approve' if u.get('access') == 'guest' else 'guest'}:{uid}"},
             {"text": "❌ Відкликати", "callback_data": f"deny:{uid}"}]
            for uid, u in users.items() if u.get("status") == "approved" and u.get("role") != "owner"]
    btns += [[{"text": f"🏠 {u.get('name', uid)[:24]}", "callback_data": f"approve:{uid}"},
              {"text": "👤 Гість", "callback_data": f"guest:{uid}"}]
             for uid, u in users.items() if u.get("status") in ("pending", "denied")]
    return {"inline_keyboard": btns} if btns else None


# --- Тривоги: у кожного користувача свій список регіонів; 🔔/🔕 — сповіщення для регіону вкл/викл
MAX_REGIONS = 5


def alerts_view(store, cfg, uid):
    u = get_users(store).get(uid) or {}
    regs = air_alerts.user_regions(u, cfg)
    state = store.get_meta("air_alerts") or {}
    now = int(time.time() * 1000)
    lines = ["🚨 Тривоги — ваші регіони"]
    for r in regs:
        a = state.get(r["name"])
        if not a:
            st = "перевіряється (до 2 хв)"
        elif a.get("active"):
            ico, lvl = air_alerts.LEVELS.get(a.get("level"), air_alerts.LEVELS["red"])
            st = f"{ico} тривога, {lvl}, з {fmt_time(a['since'], cfg)} ({fmt_duration(now - a['since'])})"
        else:
            last = a.get("last") or {}
            st = "🕊️ тихо" + (f" · останній відбій {fmt_time(last['end'], cfg, with_date=True)}" if last.get("end") else "")
        lines.append(f"{'🔔' if r.get('on', True) else '🔕'} {r['name']}: {st}")
    if not regs:
        lines.append("Список порожній — додайте регіон кнопкою нижче.")
    lines.append("\n🔔 — сповіщення приходять, 🔕 — вимкнені. Натисніть на регіон, щоб перемкнути; 🗑 — прибрати.")
    rows = [[{"text": f"{'🔔' if r.get('on', True) else '🔕'} {r['name']}", "callback_data": f"ar:t:{i}"},
             {"text": "🗑", "callback_data": f"ar:d:{i}"}] for i, r in enumerate(regs)]
    if len(regs) < MAX_REGIONS:
        rows.append([{"text": f"➕ Додати регіон ({len(regs)}/{MAX_REGIONS})", "callback_data": "ar:a"}])
    else:
        lines.append(f"Максимум {MAX_REGIONS} регіонів — щоб додати новий, приберіть один 🗑.")
    return "\n".join(lines), {"inline_keyboard": rows}


_UA = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"


def _ua_key(name):
    """Сортування за українською абеткою (стандартне ставить І/Є/Ї перед А)."""
    return [(_UA.find(ch) if ch in _UA else 100 + ord(ch)) for ch in name.lower()]


def _catalog_view(cfg, node=None):
    """Вибір регіону: області → (уся область | район) → (увесь район | громада)."""
    cat = air_alerts.catalog(cfg)
    if node is None:
        states = sorted((r for r in cat.values() if r["parent"] is None), key=lambda r: (not r["name"].startswith("м. Київ"), _ua_key(r["name"])))
        btn = [{"text": r["name"], "callback_data": f"ar:{'s' if r['children'] else '+'}:{r['id']}"} for r in states]
        rows = [btn[i:i + 2] for i in range(0, len(btn), 2)]
        rows.append([{"text": "‹ Мої регіони", "callback_data": "ar:b"}])
        return "Оберіть область (або місто):", {"inline_keyboard": rows}
    r = cat[node]
    whole = "✅ Уся область" if r["type"] == "State" else "✅ Увесь район"
    rows = [[{"text": f"{whole}: {r['name']}", "callback_data": f"ar:+:{r['id']}"}]]
    for k in r["children"]:
        kid = cat[k]
        rows.append([{"text": kid["name"], "callback_data": f"ar:{'r' if kid['children'] else '+'}:{k}"}])
    back = ([{"text": f"‹ {cat[r['parent']]['name']}", "callback_data": f"ar:s:{r['parent']}"}] if r["parent"] else []) + \
           [{"text": "‹ Області", "callback_data": "ar:a"}]
    rows.append(back)
    hint = "район або громаду" if r["type"] == "State" else "громаду"
    return f"{r['name']}: додайте цілком або оберіть {hint}:", {"inline_keyboard": rows}


def handle_alerts_callback(cb, store, cfg, notifier):
    uid = str((cb.get("from") or {}).get("id"))
    users = get_users(store)
    u = users.get(uid)
    if not u or u.get("status") != "approved":
        notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Немає доступу"}, retries=0)
        return "denied"
    _, op, arg = (cb.get("data") or "").split(":", 2) + [""] * (3 - len((cb.get("data") or "").split(":", 2)))
    regs = [dict(r) for r in air_alerts.user_regions(u, cfg)]
    note, view = "", None
    try:
        if op == "t" and arg.isdigit() and int(arg) < len(regs):
            r = regs[int(arg)]
            r["on"] = not r.get("on", True)
            note = f"{'🔔 Увімкнено' if r['on'] else '🔕 Вимкнено'}: {r['name']}"
        elif op == "d" and arg.isdigit() and int(arg) < len(regs):
            note = f"🗑 Прибрано: {regs.pop(int(arg))['name']}"
        elif op == "+":
            e = air_alerts.region_entry(air_alerts.catalog(cfg), arg)
            if any(r["name"] == e["name"] for r in regs):
                note = f"Уже є: {e['name']}"
            elif len(regs) >= MAX_REGIONS:
                note = f"Уже є: {MAX_REGIONS} регіонів (максимум) — спершу приберіть один 🗑"
            else:
                regs.append(e)
                note = f"➕ Додано: {e['name']}"
        elif op == "a":
            view = _catalog_view(cfg)
        elif op in ("s", "r"):
            view = _catalog_view(cfg, arg)
    except Exception as e:  # довідник недоступний
        print(f"alerts catalog error: {type(e).__name__}: {e}")
        note = "Довідник регіонів зараз недоступний, спробуйте пізніше"
    if op in ("t", "d", "+") and note and not note.startswith(("Уже", "Довідник")):
        u["air_regions"] = regs
        save_users(store, users)
    if view is None:
        view = alerts_view(store, cfg, uid)
    notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"], **({"text": note} if note else {})}, retries=0)
    msg = cb.get("message") or {}
    if msg:
        notifier.call("editMessageText", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                          "text": view[0], "reply_markup": view[1]}, retries=0)
    return f"alerts:{op}"


# --- Адреса користувача: геолокація або текст → регіон тривог, зона ДТЕК, черга (де можна — автоматично)
def address_summary(u, note=""):
    a = u.get("addr")
    if not a:
        return "📍 Адресу ще не вказано."
    lines = [f"📍 Адреса: {a.get('label')}"]
    if note:
        lines.append(note)
    z = address.ZONES.get(a.get("zone"))
    if not z:
        lines.append(NO_ZONE_TEXT)
    elif a.get("group"):
        how = "визначено автоматично через YASNO" if a.get("group_src") == "yasno" else "обрано вручну"
        lines.append(f"📅 Графіки: {z['provider']}, черга {a['group'].replace('GPV', '')} ({how})")
    else:
        lines.append(f"📅 Графіки: {z['provider']}")
        lines.append(queue_unknown_text(a["zone"]))
    return "\n".join(lines)


def _addr_markup(u):
    a = u.get("addr") or {}
    return {"inline_keyboard": [[{"text": "🔢 Обрати чергу", "callback_data": "q:pick"}]]} if a.get("zone") else None


def address_start(store, notifier, users, uid):
    u = users[uid]
    u["awaiting"] = "address"
    save_users(store, users)
    if u.get("addr"):
        _send(notifier, uid, address_summary(u), **({"reply_markup": _addr_markup(u)} if _addr_markup(u) else {}))
    _send(notifier, uid, "Надішліть геолокацію кнопкою нижче або напишіть адресу: місто, вулиця, будинок "
                         "(напр. «Київ, Хрещатик 22»).", reply_markup=ADDR_KEYBOARD)
    return "address start"


def address_set(store, cfg, notifier, users, uid, msg):
    u = users[uid]
    loc = msg.get("location")
    _send(notifier, uid, "🔎 Шукаю адресу…")
    try:
        g = (address.geocode(lat=loc["latitude"], lon=loc["longitude"]) if loc
             else address.geocode(text=(msg.get("text") or "")[:200]))
    except Exception as e:
        print(f"geocode error: {type(e).__name__}: {e}")
        g = None
    if not g:
        _send(notifier, uid, "Не вдалося знайти адресу в Україні. Спробуйте ще раз (місто, вулиця, будинок) "
                             "або надішліть геолокацію.", reply_markup=ADDR_KEYBOARD)
        return "address not found"
    zone = address.zone_for(g)
    group = address.yasno_group(zone, g) if zone else None
    u.pop("awaiting", None)
    u["addr"] = {"label": g["label"], "state": g["state"], "district": g["district"], "municipality": g["municipality"],
                 "zone": zone, "group": group, "group_src": "yasno" if group else None}
    note = ""
    try:
        cat = air_alerts.catalog(cfg)
        rid = address.alert_region(cat, g)
        if rid:
            e = air_alerts.region_entry(cat, rid)
            regs = [dict(r) for r in air_alerts.user_regions(u, cfg)]
            if any(r["name"] == e["name"] for r in regs):
                note = f"🚨 Тривоги: «{e['name']}» уже у ваших регіонах"
            elif len(regs) >= MAX_REGIONS:
                note = f"🚨 Тривоги: «{e['name']}» не додано — уже {MAX_REGIONS} регіонів (приберіть один у 🚨 Тривоги)"
            else:
                regs.insert(0, e)
                u["air_regions"] = regs
                note = f"🚨 Тривоги: додано «{e['name']}»"
    except Exception as e:
        print(f"alert region error: {type(e).__name__}: {e}")
    save_users(store, users)
    _send(notifier, uid, address_summary(u, note), reply_markup=kb_for(u))
    if zone and not group:
        _queue_picker(store, notifier, uid, zone)
    return "address set"


def _queue_picker(store, notifier, uid, zone, message=None):
    groups = schedule.zone_groups(store, zone)
    if not groups:
        try:
            store.put_meta(f"zone#{zone}", {**schedule.fetch_zone(zone), "fetched": int(time.time() * 1000)})
            groups = schedule.zone_groups(store, zone)
        except Exception as e:
            print(f"zone fetch error: {type(e).__name__}: {e}")
    if not groups:
        return _send(notifier, uid, "Список черг зараз недоступний — спробуйте пізніше.")
    per = 6 if len(groups) > 12 else 4
    btn = [{"text": g.replace("GPV", ""), "callback_data": f"q:{g}"} for g in groups]
    rows = [btn[i:i + per] for i in range(0, len(btn), per)] + [[{"text": "🤷 Не знаю", "callback_data": "q:?"}]]
    z = address.ZONES[zone]
    text = f"🔢 Оберіть свою чергу ({z['provider']}). Дізнатися її можна на сайті ДТЕК за адресою: {z['lookup']}"
    if message:
        return notifier.call("editMessageText", {"chat_id": uid, "message_id": message["message_id"], "text": text,
                                                 "reply_markup": {"inline_keyboard": rows},
                                                 "disable_web_page_preview": True}, retries=0)
    return _send(notifier, uid, text, reply_markup={"inline_keyboard": rows})


def handle_queue_callback(cb, store, notifier):
    uid = str((cb.get("from") or {}).get("id"))
    users = get_users(store)
    u = users.get(uid)
    a = (u or {}).get("addr") or {}
    if not u or u.get("status") != "approved" or not a.get("zone"):
        notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Спершу вкажіть адресу"}, retries=0)
        return "queue denied"
    arg = (cb.get("data") or "")[2:]
    msg = cb.get("message") or {}
    if arg == "pick":
        notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"]}, retries=0)
        _queue_picker(store, notifier, uid, a["zone"])
        return "queue pick"
    if arg == "?":
        text = "Добре, черга лишається невідомою. " + queue_unknown_text(a["zone"])
    elif arg in schedule.zone_groups(store, a["zone"]):
        a.update(group=arg, group_src="manual")
        save_users(store, users)
        text = f"✅ Черга {arg.replace('GPV', '')} збережена. Графік — кнопкою «📅 Графік ДТЕК», про зміни графіка прийде сповіщення."
    else:
        text = "Невідома черга, оберіть ще раз."
    notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"]}, retries=0)
    if msg:
        notifier.call("editMessageText", {"chat_id": uid, "message_id": msg["message_id"], "text": text,
                                          "disable_web_page_preview": True}, retries=0)
    return f"queue {arg}"


# --- Звук сповіщень (кожному свій). Сповіщення приходять завжди; «без звуку» = Telegram disable_notification.
# Без категорій (рішення користувача — так простіше): або все зі звуком, або все без звуку.
# u["sound"] = {"mute": вимкнути звук до «Увімкнути звук», "night": {"on", "from", "to"} — без звуку у свої години}
SOUND_DEFAULT = {"mute": False, "night": {"on": False, "from": 0, "to": 6}}


def sound_prefs(u):
    s = {**SOUND_DEFAULT, **((u or {}).get("sound") or {})}
    s["night"] = {**SOUND_DEFAULT["night"], **(s.get("night") or {})}
    return s


def _in_night(s, hour):
    n = s["night"]
    if not n["on"] or n["from"] == n["to"]:
        return False
    return n["from"] <= hour < n["to"] if n["from"] < n["to"] else hour >= n["from"] or hour < n["to"]


def is_silent(u, kind, hour):
    """True — повідомлення прийде без звуку."""
    s = sound_prefs(u)
    return bool(s["mute"]) or _in_night(s, hour)  # kind не враховуємо: на все однаково


def silencer(store, cfg):
    """Для Notifier.silent: година — місцева (config.timezone), на момент відправки."""
    users = get_users(store)

    def fn(cid, kind):
        hour = datetime.now(local_tz(cfg)).hour
        return is_silent(users.get(str(cid)), kind, hour)
    return fn


def _hh(h):
    return f"{int(h) % 24:02d}:00"


def sound_view(u, cfg):
    s = sound_prefs(u)
    n = s["night"]
    hour = datetime.now(local_tz(cfg)).hour
    now_quiet = s["mute"] or _in_night(s, hour)
    lines = ["🔔 Звук сповіщень (приходять завжди, тут — лише звук)",
             "Зараз: " + ("🔕 без звуку" + (" — до «Увімкнути звук»" if s["mute"] else f" (нічний режим до {_hh(n['to'])})")
                          if now_quiet else "🔔 зі звуком")]
    if n["on"]:
        lines.append(f"🌙 Нічний режим: щодня {_hh(n['from'])}–{_hh(n['to'])} без звуку.")
    # «Увімкнути звук» перебиває все (і нічний режим); «Вимкнути звук» — тиша до «Увімкнути звук»
    loud_all = not (s["mute"] or n["on"])
    rows = [[{"text": f"{'✅ ' if loud_all else ''}🔔 Увімкнути звук", "callback_data": "snd:on"},
             {"text": f"{'✅ ' if s['mute'] else ''}🔕 Вимкнути звук", "callback_data": "snd:off"}],
            [{"text": f"{'✅ ' if n['on'] else ''}🌙 Нічний {_hh(n['from'])}–{_hh(n['to'])}", "callback_data": "snd:non"},
             {"text": "☀️ Вимкнути нічний", "callback_data": "snd:noff"}],
            [{"text": "🕐 Години нічного", "callback_data": "snd:h"}]]
    return "\n".join(lines), {"inline_keyboard": rows}


def _hours_markup(step, start=None):
    """step f — з котрої години, t — до котрої."""
    hs = list(range(24))
    rows = [[{"text": _hh(h), "callback_data": f"snd:{step}:{h}" if step == "f" else f"snd:t:{start}:{h}"}
             for h in hs[i:i + 6]] for i in range(0, 24, 6)]
    return {"inline_keyboard": rows + [[{"text": "‹ Назад", "callback_data": "snd:b"}]]}


def handle_sound_callback(cb, store, cfg, notifier):
    uid = str((cb.get("from") or {}).get("id"))
    users = get_users(store)
    u = users.get(uid)
    if not u or u.get("status") != "approved":
        notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Немає доступу"}, retries=0)
        return "denied"
    parts = (cb.get("data") or "").split(":")[1:]
    op = parts[0] if parts else ""
    s = sound_prefs(u)
    note, text, markup = "", None, None
    for k in ("mute_schedule", "alarm_loud", "air_loud"):  # прибрані категорії (16.11) — не зберігаємо
        s.pop(k, None)
    if op == "on":  # перебиває все, зокрема нічний режим
        s["mute"] = False
        s["night"]["on"] = False
        note = "🔔 Звук увімкнено (нічний режим вимкнено)"
    elif op == "off":
        s["mute"] = True
        note = "🔕 Без звуку — до «Увімкнути звук»"
    elif op == "non":
        s["night"]["on"] = True
        note = f"🌙 Нічний режим: {_hh(s['night']['from'])}–{_hh(s['night']['to'])}"
    elif op == "noff":
        s["night"]["on"] = False
        note = "☀️ Нічний режим вимкнено"
    elif op == "n":
        s["night"]["on"] = not s["night"]["on"]
    elif op == "h":
        text, markup = "🌙 З котрої години без звуку?", _hours_markup("f")
    elif op == "f" and len(parts) > 1 and parts[1].isdigit():
        text, markup = f"🌙 Без звуку з {_hh(parts[1])} до котрої години?", _hours_markup("t", int(parts[1]))
    elif op == "t" and len(parts) > 2 and parts[1].isdigit() and parts[2].isdigit():
        a, b = int(parts[1]) % 24, int(parts[2]) % 24
        if a == b:
            note = "Початок і кінець однакові — оберіть інший кінець"
            text, markup = f"🌙 Без звуку з {_hh(a)} до котрої години?", _hours_markup("t", a)
        else:
            s["night"].update(on=True, **{"from": a, "to": b})
            note = f"🌙 Без звуку з {_hh(a)} до {_hh(b)}"
    if text is None:
        u["sound"] = s
        save_users(store, users)
        text, markup = sound_view(u, cfg)
    notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"], **({"text": note} if note else {})}, retries=0)
    msg = cb.get("message") or {}
    if msg:
        notifier.call("editMessageText", {"chat_id": uid, "message_id": msg["message_id"], "text": text,
                                          "reply_markup": markup}, retries=0)
    return f"sound {op}"


# --- Заявка «немає світла» в ДТЕК (подає опитувач від акаунта власника, див. dtek.py)
def dtek_view(store, cfg):
    """Текст і кнопки: або питання про коридор, або чому зараз подати не можна."""
    import dtek
    now = int(time.time() * 1000)
    c = _ctx(store, cfg)
    head = light_text(c).split("\n")[0]
    if not dtek.conf(cfg)["enabled"]:
        return "🆘 Заявки в ДТЕК ще не налаштовані. Поки що — бот ДТЕК @DTEKKyivRegionElektromerezhiBot або 1588.", None
    st = dtek.state(store)
    ok, nxt = dtek.can_submit(st, now, cfg)
    if not ok:
        return (f"📨 Заявку вже подано {fmt_time(st['ts'], cfg, with_date=True)} ({st.get('by', '')}), "
                f"ДТЕК ще не відповів.\nПовторно — не раніше {fmt_time(nxt, cfg, with_date=True)} "
                f"або одразу після відповіді ДТЕК.\n\n{head}"), None
    prev = ""
    if st.get("status") == "answered":
        prev = f"\n(попередня заявка {fmt_time(st['ts'], cfg, with_date=True)} — ДТЕК відповів)"
    return (f"{head}{prev}\n\n💡 Чи є світло в будинку — у загальному коридорі?",
            {"inline_keyboard": [[{"text": "💡 Є", "callback_data": "dt:yes"},
                                  {"text": "🌑 Немає — подати заявку", "callback_data": "dt:no"}],
                                 [{"text": "‹ Скасувати", "callback_data": "dt:x"}]]})


def _invoke_poller(payload):
    import json
    import os
    import boto3
    boto3.client("lambda").invoke(FunctionName=os.environ["POLLER_FN"], InvocationType="Event",
                                  Payload=json.dumps(payload).encode())


def handle_dtek_callback(cb, store, cfg, notifier):
    import dtek
    uid = str((cb.get("from") or {}).get("id"))
    users = get_users(store)
    u = users.get(uid)
    if not u or u.get("status") != "approved" or u.get("access") == "guest":
        notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Лише для учасників дому"}, retries=0)
        return "denied"
    op = (cb.get("data") or "").split(":", 1)[1]
    now = int(time.time() * 1000)
    note = ""
    if op == "yes":
        text = ("💡 Якщо в коридорі світло є — відключення не з боку ДТЕК, а в квартирі: "
                "перевірте автомати в щитку й лічильник. Заявку не подаю.")
    elif op == "no":
        st = dtek.state(store)
        ok, nxt = dtek.can_submit(st, now, cfg)
        if not dtek.conf(cfg)["enabled"]:
            text = "🆘 Заявки в ДТЕК ще не налаштовані."
        elif not ok:
            text = (f"📨 Заявку вже подано {fmt_time(st['ts'], cfg, with_date=True)} ({st.get('by', '')}). "
                    f"Повторно — не раніше {fmt_time(nxt, cfg, with_date=True)} або після відповіді ДТЕК.")
        else:
            store.put_meta("dtek_report", {**st, "status": "submitting", "ts": now, "by": u.get("name", uid),
                                           "by_uid": uid, "error": None})
            try:
                _invoke_poller({"dtek_submit": True})
                text, note = "⏳ Подаю заявку в ДТЕК… Результат прийде сюди всім учасникам дому.", "Подаю заявку"
            except Exception as e:
                store.put_meta("dtek_report", {**st, "status": "failed", "error": str(e)[:200]})
                text = f"⚠️ Не вдалося запустити подання: {type(e).__name__}. Подайте вручну: бот ДТЕК або 1588."
    else:
        text = "Скасовано."
    notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"], **({"text": note} if note else {})}, retries=0)
    msg = cb.get("message") or {}
    if msg:
        notifier.call("editMessageText", {"chat_id": uid, "message_id": msg["message_id"], "text": text}, retries=0)
    return f"dtek {op}"


def handle_callback(cb, store, notifier, web, cfg=None):
    """Кнопки під повідомленнями: тривоги (кожен користувач — свої), доступ (лише власник)."""
    if (cb.get("data") or "").startswith("ar:"):
        return handle_alerts_callback(cb, store, cfg or {}, notifier)
    if (cb.get("data") or "").startswith("q:"):
        return handle_queue_callback(cb, store, notifier)
    if (cb.get("data") or "").startswith("snd:"):
        return handle_sound_callback(cb, store, cfg or {}, notifier)
    if (cb.get("data") or "").startswith("dt:"):
        return handle_dtek_callback(cb, store, cfg or {}, notifier)
    users = get_users(store)
    who = str((cb.get("from") or {}).get("id"))
    action, _, target = (cb.get("data") or "").partition(":")
    if who not in owners(users) or target not in users:
        notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Немає прав"}, retries=0)
        return "denied"
    u = users[target]
    if action in ("approve", "guest"):
        u.update(status="approved", role=u.get("role") or "member", access="guest" if action == "guest" else "home")
        _welcome(notifier, target, u, web)
        text = f"✅ {u.get('name')}: {'👤 гість (тривоги й графік)' if action == 'guest' else '🏠 дім (усе)'}"
    else:
        u["status"] = "denied"
        _send(notifier, target, "Доступ до бота відкликано.", reply_markup={"remove_keyboard": True})
        notifier.call("setChatMenuButton", {"chat_id": target, "menu_button": {"type": "default"}}, retries=0)
        text = f"⛔ Доступ закрито: {u.get('name')}"
    save_users(store, users)
    notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": text}, retries=0)
    msg = cb.get("message") or {}
    if msg:
        notifier.call("editMessageText", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                          "text": users_text(users), "reply_markup": users_markup(users) or {"inline_keyboard": []}},
                      retries=0)
    return action


def handle_update(update, store, cfg, notifier, group_id, web=None):
    """Лише особистий чат з ботом: доступ — підтвердженим користувачам; нові — через запит власнику.
    Учасників старої групи «Дім» бот впізнає й підтверджує сам (власник групи → власник бота)."""
    web = web or {}
    if update.get("callback_query"):
        return handle_callback(update["callback_query"], store, notifier, web, cfg)
    msg = update.get("message") or {}
    chat, user = msg.get("chat") or {}, msg.get("from") or {}
    if chat.get("type") != "private" or not user.get("id") or user.get("is_bot"):
        return None  # у групах бот більше не відповідає
    text = (msg.get("text") or "").strip()
    uid, now = str(user["id"]), int(time.time() * 1000)
    users = get_users(store)
    u = users.get(uid)

    if not u or u.get("status") == "pending":
        gs = _group_status(notifier, group_id, user["id"])
        if gs in ("creator", "administrator", "member", "restricted"):  # учасник старої групи — без запиту
            u = users[uid] = {"name": _name(user), "role": "owner" if gs == "creator" or not owners(users) and gs == "administrator" else "member",
                              "status": "approved", "since": now}
            save_users(store, users)
            _welcome(notifier, uid, u, web)
            return "approved (group)"
        if not u:
            u = users[uid] = {"name": _name(user), "role": "member", "status": "pending", "since": now}
            save_users(store, users)
            for oid in owners(users):
                _send(notifier, oid, f"🔔 {u['name']} хоче користуватись ботом.\n"
                                     f"🏠 Дім — усе (світло, датчики, дашборд); 👤 Гість — лише тривоги й графік для своєї адреси.",
                      reply_markup={"inline_keyboard": [[
                          {"text": "🏠 Дім", "callback_data": f"approve:{uid}"},
                          {"text": "👤 Гість", "callback_data": f"guest:{uid}"},
                          {"text": "❌", "callback_data": f"deny:{uid}"}]]})
        _send(notifier, uid, "Запит на доступ надіслано власнику. Щойно він підтвердить — тут з'являться кнопки.")
        return "pending"
    if u.get("status") != "approved":
        _send(notifier, uid, "Доступ до бота закрито.")
        return "denied"

    kb = kb_for(u)
    cmd = BUTTON_CMD.get(text)
    if not cmd and text.startswith("/"):
        cmd = text.split()[0][1:].split("@")[0].lower()
    if msg.get("location"):  # геолокацію можна надіслати будь-коли — це завжди «моя адреса»
        return address_set(store, cfg, notifier, users, uid, msg)
    if u.get("awaiting") == "address":  # чекаємо адресу: текст або геолокація
        if text == "‹ Скасувати":
            u.pop("awaiting", None)
            save_users(store, users)
            _send(notifier, uid, "Скасовано.", reply_markup=kb)
            return "address cancel"
        if msg.get("location") or (text and not cmd):
            return address_set(store, cfg, notifier, users, uid, msg)
        u.pop("awaiting", None)
        save_users(store, users)
    if u.get("access") == "guest" and cmd and cmd not in GUEST_CMDS:
        _send(notifier, uid, "Ця команда — лише для учасників дому. Вам доступні 🚨 Тривоги, 📅 Графік ДТЕК, 📍 Моя адреса і 🔔 Звук.",
              reply_markup=kb)
        return "guest denied"
    if cmd == "address":
        return address_start(store, notifier, users, uid)
    if cmd == "start":
        _welcome(notifier, uid, u, web)
        return cmd
    if cmd == "users" and u.get("role") == "owner":
        _send(notifier, uid, users_text(users), **({"reply_markup": users_markup(users)} if users_markup(users) else {}))
        return cmd
    if cmd == "dashboard":
        dashboard_reply(notifier, uid, dashboard_link(web, u.get("role")))
        return cmd
    if cmd == "sound":
        text, markup = sound_view(u, cfg)
        _send(notifier, uid, text, reply_markup=markup)
        return cmd
    if cmd == "dtek":
        text, markup = dtek_view(store, cfg)
        _send(notifier, uid, text, **({"reply_markup": markup} if markup else {}))
        return cmd
    if cmd == "alerts":
        text, markup = alerts_view(store, cfg, uid)
        _send(notifier, uid, text, reply_markup=markup)
        return cmd
    if cmd in COMMANDS and cmd != "help":
        _send(notifier, uid, COMMANDS[cmd][1](_ctx(store, cfg, u)), reply_markup=kb)
        return cmd
    # довільний текст, стікер, фото тощо — мовчки стираємо (Telegram дозволяє боту видаляти вхідні в приваті),
    # щоб у чаті лишались тільки кнопки й відповіді
    if msg.get("message_id"):
        notifier.call("deleteMessage", {"chat_id": uid, "message_id": msg["message_id"]}, retries=0)
    # людина пише текст — найчастіше тому, що кнопок не видно (очищено історію). Повертаємо клавіатуру
    # службовим повідомленням і одразу стираємо його: клавіатура лишається, у чаті — нічого зайвого
    ok, r = _send(notifier, uid, "⌨️", reply_markup=kb)
    if ok and (r.get("result") or {}).get("message_id"):
        notifier.call("deleteMessage", {"chat_id": uid, "message_id": r["result"]["message_id"]}, retries=0)
    return "text (deleted, keyboard restored)"


def setup(notifier, webhook_url, secret, store=None, group_id=""):
    """Реєструє webhook (повідомлення + кнопки) і меню команд для особистих чатів.
    Перенесення: власника старої групи «Дім» одразу вносить у список як власника бота."""
    res = {}
    if store is not None and group_id:
        ok, r = notifier.call("getChatAdministrators", {"chat_id": group_id}, retries=0)
        users = get_users(store)
        for m in (r.get("result") or []) if ok else []:
            u = m.get("user") or {}
            if m.get("status") == "creator" and not u.get("is_bot") and str(u.get("id")) not in users:
                users[str(u["id"])] = {"name": _name(u), "role": "owner", "status": "approved",
                                       "since": int(time.time() * 1000)}
        save_users(store, users)
        res["owners"] = (True, ", ".join(users[o]["name"] for o in owners(users)))
    res["webhook"] = notifier.call("setWebhook", {"url": webhook_url, "secret_token": secret,
                                                   "allowed_updates": ["message", "callback_query"],
                                                   "drop_pending_updates": True})
    cmds = menu_commands()
    for uid, u in get_users(store).items() if store is not None else ():
        if u.get("status") == "approved":  # прибрати старі особисті кнопки «Дашборд», поставити «Меню»
            notifier.call("setChatMenuButton", {"chat_id": uid, "menu_button": {"type": "commands"}}, retries=0)
            _owner_commands(notifier, uid, u)
    res["commands_private"] = notifier.call("setMyCommands", {"commands": cmds, "scope": {"type": "all_private_chats"}})
    res["commands_default"] = notifier.call("setMyCommands", {"commands": cmds})
    res["menu_button"] = notifier.call("setChatMenuButton", {"menu_button": {"type": "commands"}})
    return {k: (v[0], str(v[1])[:200]) for k, v in res.items()}
