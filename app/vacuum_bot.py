"""Пилосос у боті: екрани й кнопки (лише учасники «🏠 Дім»). Самі команди виконує Lambda пилососа (vacuum.py).

Кнопки — під одним повідомленням, яке редагується крок за кроком:
  головне → (усі / усі, крім… / вибрані) → кімнати → скільки разів → режим → підтвердження → ▶️
  «⏏️ На базу» — одразу, без підтвердження.
Чернетка вибору — META vacuum_draft {uid: {...}}; callback_data — «vc:…» (≤ 64 байти)."""
import json
import os
import time

from devices import fmt_time, local_tz

# режим → (значення Dreame CLEANING_MODE, підпис)
MODES = {
    "sweep": (0, "🧹 Пилосос"),
    "mop": (1, "🧽 Миття"),
    "both": (2, "🧹+🧽 Разом"),
    "after": (3, "🧹→🧽 Спочатку пилосос, потім миття"),
}
MODE_BY_VALUE = {v: k for k, (v, _) in MODES.items()}
MODE_NAME_TO_KEY = {"sweeping": "sweep", "mopping": "mop", "sweeping_and_mopping": "both",
                    "mopping_after_sweeping": "after"}
SUCTION_UA = {"quiet": "тихо", "standard": "стандарт", "strong": "сильно", "turbo": "турбо"}
KINDS = {"all": "🏠 Усі кімнати", "ex": "➖ Усі, крім…", "sel": "✅ Вибрані кімнати"}
MAX_TIMES = 3

# стани Dreame (DreameVacuumState, ім'я в нижньому регістрі) → текст
STATE_UA = {
    "sweeping": "🧹 Пилососить", "idle": "💤 Очікує", "paused": "⏸ Пауза", "error": "⚠️ Помилка",
    "returning": "↩️ Їде на базу", "charging": "🔌 Заряджається", "mopping": "🧽 Миє підлогу",
    "drying": "🌬 Сушить мопи", "washing": "🚿 Миє мопи на базі", "returning_to_wash": "↩️ Їде мити мопи",
    "building": "🗺 Будує карту", "sweeping_and_mopping": "🧹🧽 Пилососить і миє",
    "charging_completed": "🅿️ На базі, заряджено", "upgrading": "⬆️ Оновлює прошивку",
    "washing_paused": "⏸ Миття мопів на паузі", "auto_emptying": "🗑 Вивантажує пил",
    "smart_charging": "🔌 Розумна зарядка", "second_cleaning": "🔁 Повторне прибирання",
    "spot_cleaning": "🎯 Точкове прибирання", "returning_auto_empty": "↩️ Їде вивантажити пил",
    "waiting_for_task": "💤 Чекає завдання", "station_cleaning": "🚿 Чистить базу",
    "returning_to_drain": "↩️ Їде злити воду", "draining": "💧 Зливає воду",
    "auto_water_draining": "💧 Зливає воду", "emptying": "🗑 Вивантажує", "dust_bag_drying": "🌬 Сушить мішок",
    "heading_to_extra_cleaning": "🔁 Їде доприбрати", "clean_add_water": "💧 Додає воду",
}
# стани, коли пилосос на базі й нічого не робить
DOCKED_STATES = {"charging", "charging_completed", "idle", "smart_charging", "waiting_for_task", "drying",
                 "dust_bag_drying"}

# стандартні назви кімнат на карті Dreame → українською (власні назви з config vacuum.room_names мають пріоритет)
ROOM_UA = {
    "kitchen": "Кухня", "living room": "Вітальня", "primary bedroom": "Спальня", "master bedroom": "Спальня",
    "bedroom": "Спальня", "secondary bedroom": "Друга спальня", "guest bedroom": "Гостьова",
    "corridor": "Коридор", "hallway": "Передпокій", "bathroom": "Ванна", "toilet": "Туалет",
    "study": "Кабінет", "dining hall": "Їдальня", "dining room": "Їдальня", "balcony": "Балкон",
    "kids room": "Дитяча", "children room": "Дитяча", "laundry": "Пральня", "storage": "Комора",
    "cloakroom": "Гардероб", "entrance": "Вхід", "room": "Кімната", "gym": "Спортзал", "office": "Кабінет",
}


def room_name(raw, rid, cfg=None):
    """Назва кімнати: власна з config → переклад стандартної («Corridor 2» → «Коридор 2») → як на карті."""
    own = ((cfg or {}).get("vacuum") or {}).get("room_names") or {}
    if str(rid) in own:
        return own[str(rid)]
    raw = (raw or "").strip()
    if not raw:
        return f"Кімната {rid}"
    base, _, tail = raw.rpartition(" ")
    if base and tail.isdigit() and base.lower() in ROOM_UA:
        return f"{ROOM_UA[base.lower()]} {tail}"
    return ROOM_UA.get(raw.lower(), raw)


def state_text(state):
    return STATE_UA.get(state or "", (state or "невідомо").replace("_", " "))


def mode_label(key):
    return MODES.get(key, (None, key or "—"))[1]


def plan_ids(draft, rooms):
    """Які кімнати прибрати (у порядку карти): усі / усі, крім позначених / лише позначені."""
    ids = [r["id"] for r in rooms]
    marked = set(draft.get("rooms") or [])
    if draft.get("kind") == "all":
        return ids
    if draft.get("kind") == "ex":
        return [i for i in ids if i not in marked]
    return [i for i in ids if i in marked]


def names(ids, rooms):
    by_id = {r["id"]: r["name"] for r in rooms}
    return ", ".join(by_id.get(i, str(i)) for i in ids)


def status_text(meta, cfg, now=None):
    """Короткий стан для бота (з кешу META vacuum, який пише Lambda пилососа)."""
    if not meta:
        return "🤖 Пилосос ще не підключено."
    now = now or int(time.time() * 1000)
    lines = [f"🤖 {meta.get('name', 'Пилосос')} — {state_text(meta.get('state'))}"
             + (f" · 🔋 {meta['battery']} %" if meta.get("battery") is not None else "")]
    if meta.get("error"):
        lines.append(f"⚠️ {meta['error']}")
    mode = MODE_NAME_TO_KEY.get(meta.get("mode") or "")
    suction = SUCTION_UA.get(meta.get("suction") or "", meta.get("suction"))
    if mode or suction:
        lines.append(f"Режим: {mode_label(mode) if mode else '—'}" + (f" · потужність: {suction}" if suction else ""))
    act = meta.get("active")
    if act:
        lines.append(f"▶️ Зараз: {act.get('rooms_text', '')} · {act.get('times', 1)}× · {mode_label(act.get('mode'))}"
                     + (f" (запустив {act['by']})" if act.get("by") else ""))
        if meta.get("room_now"):
            lines.append(f"📍 Кімната: {meta['room_now']}")
        if meta.get("area") is not None:
            lines.append(f"📐 Прибрано {meta['area']} м² за {meta.get('minutes', 0)} хв")
    if meta.get("updated"):
        lines.append(f"Оновлено {fmt_time(meta['updated'], cfg)}")
    return "\n".join(lines)


def _btn(text, data):
    return {"text": text, "callback_data": f"vc:{data}"}


MODE_SHORT = {"sweep": "🧹 Пилосос", "mop": "🧽 Миття", "both": "🧹+🧽 Разом", "after": "🧹→🧽 Потім миття"}
NAV = "‹ Назад", "✖️ Скасувати"


def busy(meta):
    """Пилосос зараз чимось зайнятий (прибирає, їде) — тоді головне — «На базу»."""
    meta = meta or {}
    return bool(meta.get("active")) or (bool(meta.get("state")) and meta["state"] not in DOCKED_STATES)


def main_markup(meta=None):
    """Головне меню під станом. Прибирає — зверху велика «⏏️ На базу»; стоїть на базі — запуск прибирання.
    Найчастіша дія — на всю ширину, «На базу» (коли не на базі) — окремим рядком унизу, під великим пальцем."""
    info = [_btn("🗺 Карта", "map"), _btn("🔄 Оновити", "st")]
    if busy(meta):
        return {"inline_keyboard": [[_btn("⏏️ На базу — негайно", "home")], info,
                                    [_btn(KINDS["all"], "k:all")]]}
    rows = [[_btn(KINDS["all"], "k:all")],
            [_btn("✅ Вибрані", "k:sel"), _btn("➖ Усі, крім…", "k:ex")],
            info]
    if meta is None or not meta.get("docked", True):
        rows.append([_btn("⏏️ На базу", "home")])
    return {"inline_keyboard": rows}


def _nav():
    return [_btn(NAV[0], "b"), _btn(NAV[1], "x")]


def rooms_view(draft, rooms):
    marked = set(draft.get("rooms") or [])
    head = ("Позначте кімнати, які ПРОПУСТИТИ — решту буде прибрано." if draft["kind"] == "ex"
            else "Позначте кімнати, які прибрати.")
    rows, row = [], []
    for r in rooms:
        row.append(_btn(("✅ " if r["id"] in marked else "☐ ") + r["name"], f"r:{r['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    n = len(plan_ids(draft, rooms))
    rows.append([_btn(f"Далі ➡️ ({n} {_rooms_word(n)})" if n else "Далі ➡️", "rn")])
    rows.append(_nav())
    return f"🤖 {KINDS[draft['kind']]}\n{head}", {"inline_keyboard": rows}


def _rooms_word(n):
    return "кімната" if n % 10 == 1 and n % 100 != 11 else "кімнати" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "кімнат"


def times_view(draft, rooms):
    ids = plan_ids(draft, rooms)
    return (f"🤖 Кімнати: {names(ids, rooms)}\nСкільки разів пройти кожну?",
            {"inline_keyboard": [[_btn(f"{n}×", f"t:{n}") for n in range(1, MAX_TIMES + 1)], _nav()]})


def mode_view(draft, rooms):
    ids = plan_ids(draft, rooms)
    k = list(MODES)
    return (f"🤖 Кімнати: {names(ids, rooms)} · {draft['times']}×\nРежим?",
            {"inline_keyboard": [[_btn(MODE_SHORT[k[0]], f"m:{k[0]}"), _btn(MODE_SHORT[k[1]], f"m:{k[1]}")],
                                 [_btn(MODE_SHORT[k[2]], f"m:{k[2]}"), _btn(MODE_SHORT[k[3]], f"m:{k[3]}")],
                                 _nav()]})


def confirm_view(draft, rooms):
    ids = plan_ids(draft, rooms)
    return (f"🤖 Прибрати: {names(ids, rooms)}\n🔁 {draft['times']}× · {mode_label(draft['mode'])}\n\nПочати?",
            {"inline_keyboard": [[_btn("▶️ Почати", "go")], _nav()]})


def step(draft, data, rooms):
    """Один крок майстра: (нова чернетка, екран, підказка). Екран None — лишитись (або, з підказкою «main», —
    повернутись у головне меню). Без побічних ефектів."""
    op, _, arg = data.partition(":")
    draft = dict(draft or {})
    if op == "k" and arg in KINDS:
        draft = {"kind": arg, "rooms": []}
        if arg == "all":
            draft["stage"] = "times"
            return draft, times_view(draft, rooms), ""
        draft["stage"] = "rooms"
        return draft, rooms_view(draft, rooms), ""
    if not draft.get("kind"):
        return {}, None, "Почніть спочатку"
    if op == "b":  # назад на крок
        stage = draft.get("stage")
        if stage == "confirm":
            draft.pop("mode", None)
            draft["stage"] = "mode"
            return draft, mode_view(draft, rooms), ""
        if stage == "mode":
            draft.pop("times", None)
            draft["stage"] = "times"
            return draft, times_view(draft, rooms), ""
        if stage == "times" and draft["kind"] != "all":
            draft["stage"] = "rooms"
            return draft, rooms_view(draft, rooms), ""
        return {}, None, "main"
    if op == "r":
        rid = int(arg)
        marked = list(draft.get("rooms") or [])
        marked.remove(rid) if rid in marked else marked.append(rid)
        draft["rooms"] = marked
        return draft, rooms_view(draft, rooms), ""
    if op == "rn":
        if not plan_ids(draft, rooms):
            return draft, None, ("Позначте хоча б одну кімнату" if draft["kind"] == "sel"
                                 else "Не можна пропустити всі кімнати")
        draft["stage"] = "times"
        return draft, times_view(draft, rooms), ""
    if op == "t":
        draft["times"] = max(1, min(MAX_TIMES, int(arg)))
        draft["stage"] = "mode"
        return draft, mode_view(draft, rooms), ""
    if op == "m" and arg in MODES and draft.get("times"):
        draft["mode"] = arg
        draft["stage"] = "confirm"
        return draft, confirm_view(draft, rooms), ""
    return draft, None, "Почніть спочатку"


# --- зв'язок з ботом і Lambda пилососа

def invoke(payload):
    """Асинхронно запускає Lambda пилососа (вона сама відповідає в Telegram)."""
    import boto3
    boto3.client("lambda").invoke(FunctionName=os.environ["VACUUM_FN"], InvocationType="Event",
                                  Payload=json.dumps(payload).encode())


def allowed(u):
    return bool(u) and u.get("status") == "approved" and u.get("access") != "guest"


def open_menu(store, cfg, notifier, uid):
    """Кнопка «🤖 Пилосос»: стан з кешу + кнопки; свіжий стан Lambda допише в те саме повідомлення."""
    meta = store.get_meta("vacuum")
    if not os.environ.get("VACUUM_FN") or not meta:
        notifier.call("sendMessage", {"chat_id": uid, "text": NOT_READY}, retries=0)
        if os.environ.get("VACUUM_FN"):  # Lambda є, але ще жодного разу не входила — спробувати зараз
            invoke({"op": "status", "chat": uid})
        return "vacuum not ready"
    ok, resp = notifier.call("sendMessage", {"chat_id": uid, "text": status_text(meta, cfg) + "\n⏳ Оновлюю стан…",
                                             "reply_markup": main_markup(meta)}, retries=0)
    msg_id = ((resp or {}).get("result") or {}).get("message_id") if ok else None
    try:
        invoke({"op": "status", "chat": uid, "msg": msg_id})
    except Exception as e:
        print(f"[vacuum] invoke status: {type(e).__name__}: {e}")
    return "vacuum"


NOT_READY = ("🤖 Пилосос ще не підключено. Власнику: зібрати шар (tools/build_vacuum_layer.ps1), terraform apply "
             "і один раз увійти (tools/dreame_login.py).")


def handle_callback(cb, store, cfg, notifier, users):
    uid = str((cb.get("from") or {}).get("id"))
    u = users.get(uid)
    msg = cb.get("message") or {}
    msg_id = msg.get("message_id")

    def answer(text=""):
        notifier.call("answerCallbackQuery", {"callback_query_id": cb["id"], **({"text": text} if text else {})},
                      retries=0)

    def edit(text, markup=None):
        if msg_id:
            notifier.call("editMessageText", {"chat_id": uid, "message_id": msg_id, "text": text,
                                              **({"reply_markup": markup} if markup else {})}, retries=0)

    if not allowed(u):
        answer("Лише для учасників дому")
        return "vacuum denied"
    if not os.environ.get("VACUUM_FN"):
        answer("Пилосос ще не підключено")
        return "vacuum not ready"
    data = (cb.get("data") or "")[3:]
    meta = store.get_meta("vacuum") or {}
    rooms = meta.get("rooms") or []
    drafts = store.get_meta("vacuum_draft") or {}
    by = u.get("name", uid)

    if data == "st":
        answer("Оновлюю…")
        edit(status_text(meta, cfg) + "\n⏳ Оновлюю стан…", main_markup(meta))
        invoke({"op": "status", "chat": uid, "msg": msg_id})
        return "vacuum status"
    if data == "home":
        answer("Відправляю на базу")
        edit("⏏️ Відправляю пилосос на базу…")
        invoke({"op": "home", "chat": uid, "msg": msg_id, "by": by})
        return "vacuum home"
    if data == "map":
        answer("Малюю карту…")
        invoke({"op": "map", "chat": uid})
        return "vacuum map"
    if data == "x":
        drafts.pop(uid, None)
        store.put_meta("vacuum_draft", drafts)
        answer()
        edit(status_text(meta, cfg), main_markup(meta))
        return "vacuum cancel"
    if data == "go":
        d = drafts.pop(uid, None) or {}
        store.put_meta("vacuum_draft", drafts)
        ids = plan_ids(d, rooms)
        if not ids or not d.get("times") or d.get("mode") not in MODES:
            answer("Почніть спочатку")
            edit(status_text(meta, cfg), main_markup(meta))
            return "vacuum go invalid"
        answer("Запускаю")
        edit(f"⏳ Запускаю прибирання: {names(ids, rooms)} · {d['times']}× · {mode_label(d['mode'])}…")
        invoke({"op": "clean", "chat": uid, "msg": msg_id, "by": by, "rooms": ids, "times": d["times"],
                "mode": d["mode"]})
        return "vacuum go"
    if not rooms:
        answer("Кімнати ще не завантажено — натисніть 🔄 Оновити")
        return "vacuum no rooms"
    draft, view, note = step(drafts.get(uid), data, rooms)
    if note == "main":  # «Назад» з першого кроку — у головне меню
        drafts.pop(uid, None)
        store.put_meta("vacuum_draft", drafts)
        answer()
        edit(status_text(meta, cfg), main_markup(meta))
        return "vacuum back"
    drafts[uid] = draft
    store.put_meta("vacuum_draft", drafts)
    answer(note)
    if view:
        edit(*view)
    return f"vacuum {data}"


def tick(store, cfg, now):
    """З опитувача (кожні 2 хв): під час прибирання — стеження; раз на добу (о check_hour) — перевірка,
    щоб токен Dreame не застоювався і зміна прошивки не пройшла непомітно."""
    vcfg = cfg.get("vacuum") or {}
    if not os.environ.get("VACUUM_FN") or not vcfg.get("enabled", True):
        return None
    meta = store.get_meta("vacuum") or {}
    if meta.get("active"):
        invoke({"op": "watch"})
        return "watch"
    from datetime import datetime
    t = store.get_meta("vacuum_tick") or {}
    hour = datetime.fromtimestamp(now / 1000, local_tz(cfg)).hour
    if now - t.get("check", 0) > 20 * 3600_000 and hour == vcfg.get("check_hour", 4):
        store.put_meta("vacuum_tick", {**t, "check": now})
        invoke({"op": "check"})
        return "check"
    return None

