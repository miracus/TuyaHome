"""Lambda пилососа Dreame (хмара Dreamehome) — tuyahome-vacuum.

Керування — через бібліотеку Tasshack/dreame-vacuum (MIT), вона лежить у шарі Lambda (tools/build_vacuum_layer.ps1).
Секрет — лише токен входу Dreamehome у SSM SecureString (DREAME_PARAM); пароль ніде не зберігається.
Сервер видає новий токен при кожному вході — зберігаємо його одразу; паралельні входи не допускаємо (замок у DynamoDB),
бо старий токен після оновлення може стати недійсним.

Події (event):
  {"op": "status", "chat", "msg"}                — стан → META vacuum, оновити повідомлення бота
  {"op": "clean", "chat", "msg", "by", "rooms": [id…], "times": 1..3, "mode": sweep|mop|both|after}
  {"op": "home", "chat", "msg", "by"}            — на базу негайно
  {"op": "map", "chat"?}                          — карта PNG → META vacuum_map (+ фото в чат)
  {"op": "watch"}                                 — під час прибирання (poller кожні 2 хв): прогрес і «готово»
  {"op": "check"}                                 — раз на добу: стан, прошивка, токен живий
"""
import base64
import io
import json
import os
import time
import traceback
import urllib.request
import uuid

import vacuum_bot as vb

PARAM = os.environ.get("DREAME_PARAM", "/tuyahome/dreame")
LOCK_S = 120


class LoginLost(Exception):
    pass


def _now():
    return int(time.time() * 1000)


# --- секрет (SSM) і замок (DynamoDB)

def load_secret():
    import boto3
    return json.loads(boto3.client("ssm").get_parameter(Name=PARAM, WithDecryption=True)["Parameter"]["Value"])


def save_secret(doc):
    import boto3
    boto3.client("ssm").put_parameter(Name=PARAM, Value=json.dumps(doc), Type="SecureString", Overwrite=True)


def _table():
    import boto3
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def acquire_lock(wait_s=40):
    from botocore.exceptions import ClientError
    t, me, end = _table(), uuid.uuid4().hex, time.time() + wait_s
    while True:
        now = int(time.time())
        try:
            t.put_item(Item={"pk": "META", "sk": "vacuum_lock", "doc": json.dumps({"id": me}), "exp": now + LOCK_S},
                       ConditionExpression="attribute_not_exists(pk) OR #e < :n",
                       ExpressionAttributeNames={"#e": "exp"}, ExpressionAttributeValues={":n": now})
            return me
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException" or time.time() > end:
                return None
            time.sleep(2)


def release_lock(me):
    try:
        _table().delete_item(Key={"pk": "META", "sk": "vacuum_lock"}, ConditionExpression="contains(#d, :me)",
                             ExpressionAttributeNames={"#d": "doc"}, ExpressionAttributeValues={":me": me})
    except Exception:
        pass


# --- пилосос

def connect(sec):
    """Вхід лише за токеном. Бібліотека створює хмарне з'єднання тільки з логіном+паролем — даємо заглушки
    й одразу стираємо пароль, щоб при збої токена вона не пробувала пароль (і не палила спроби акаунта)."""
    from dreame.device import DreameVacuumDevice
    dev = DreameVacuumDevice(sec.get("name") or "Пилосос", sec.get("host"), " ", sec.get("mac"), "token", "-",
                             sec.get("country", "eu"), True, "dreame", sec.get("did"), sec["token"])
    cloud = dev._protocol.cloud
    cloud._password = None
    if not cloud.login():
        raise LoginLost("вхід за токеном не вдався")
    keep_token(sec, cloud)
    dev.connect_device()
    return dev


def keep_token(sec, cloud):
    new = cloud.auth_key
    if new and new != sec.get("token"):
        sec["token"] = new
        save_secret(sec)


def wait_ready(dev, timeout=20):
    """Властивості й карта (кімнати) приходять не одразу — оновлюємо, поки не з'являться кімнати."""
    end = time.time() + timeout
    while True:
        try:
            dev.update()
        except Exception as e:
            print(f"[vacuum] update: {type(e).__name__}: {e}")
        if dev.status.segments or time.time() > end:
            return
        time.sleep(2)


def _g(fn):
    try:
        v = fn()
        return v.value if hasattr(v, "value") and not isinstance(v, (int, float, str)) else v
    except Exception:
        return None


def hist_record(h):
    """Запис історії прибирань Dreame (CleaningHistory) → dict для бази й дашборда."""
    def nm(v):
        return (getattr(v, "name", "") or "").lower() or None
    return {"ts": int(h.date.timestamp() * 1000) if getattr(h, "date", None) else None,
            "minutes": getattr(h, "cleaning_time", None), "area": getattr(h, "cleaned_area", None),
            "completed": getattr(h, "completed", None), "kind": nm(getattr(h, "status", None)),
            "suction": nm(getattr(h, "suction_level", None)), "interrupt": nm(getattr(h, "task_interrupt_reason", None)),
            "method": nm(getattr(h, "cleanup_method", None)), "mop_after": bool(getattr(h, "mop_after_sweep", None))}


def archive_history(store, records, since=0):
    """Історія в хмарі Dreame — лише останні записи, тож для графіків за місяці/рік кладемо кожен запис
    в архів (ARCH#vacuum, ключ — час початку; повторний запис того самого просто перезаписує)."""
    n = 0
    for r in records:
        if r.get("ts") and r["ts"] >= since:
            store.put_archive("vacuum", f"{int(r['ts']):013d}", r)
            n += 1
    return n


def backfill(dev, store, limit=500):
    """Разово: уся доступна історія з хмари (бібліотека сама бере лише 30)."""
    from dreame.types import DIID, CleaningHistory, DreameVacuumProperty as P
    start = _g(lambda: dev.get_property(P.FIRST_CLEANING_DATE)) or 0
    res = dev._protocol.cloud.get_device_event(DIID(P.STATUS, dev.property_mapping), limit, start) or []
    recs = []
    for data in res:
        try:
            recs.append(hist_record(CleaningHistory(json.loads(data["history"] if "history" in data else data["value"]),
                                                    dev.property_mapping)))
        except Exception as e:
            print(f"[vacuum] backfill record: {type(e).__name__}: {e}")
    return archive_history(store, recs), len(res)


def collect(dev, cfg, prev=None):
    """Стан пилососа → META vacuum (бот + дашборд)."""
    from dreame.types import DreameVacuumProperty as P
    st = dev.status
    prev = prev or {}
    rooms = [{"id": int(sid), "name": vb.room_name(getattr(s, "custom_name", None) or getattr(s, "name", None), sid, cfg)}
             for sid, s in sorted((_g(lambda: st.segments) or {}).items())] or prev.get("rooms") or []
    area = _g(lambda: dev.get_property(P.CLEANED_AREA))
    minutes = _g(lambda: dev.get_property(P.CLEANING_TIME))
    room_now = _g(lambda: st.current_room)
    cons = {k: _g(lambda k=k: getattr(st, f"{k}_life")) for k in
            ("main_brush", "side_brush", "filter", "sensor_dirty", "mop", "detergent", "squeegee", "tank_filter")}
    station = {k: _g(lambda k=k: getattr(st, f"{k}_status_name")) for k in
               ("dust_bag", "clean_water_tank", "dirty_water_tank")}
    history = []
    for h in (_g(lambda: st._cleaning_history) or [])[:30]:
        try:
            history.append(hist_record(h))
        except Exception as e:
            print(f"[vacuum] history: {type(e).__name__}: {e}")
    return {
        **prev,
        "history": history or prev.get("history") or [],
        "name": prev.get("name") or _g(lambda: dev.name),
        "model": _g(lambda: dev.info.model),
        "fw": _g(lambda: dev.info.firmware_version),
        "state": _g(lambda: st.state_name),
        "battery": _g(lambda: st.battery_level),
        "mode": _g(lambda: st.cleaning_mode_name),
        "suction": _g(lambda: st.suction_level_name),
        "error": (_g(lambda: st.error_name) if _g(lambda: st.has_error) else None),
        "running": bool(_g(lambda: st.running)),
        "docked": bool(_g(lambda: st.docked)),
        "cleangenius": _g(lambda: st.cleangenius_name),
        "customized": bool(_g(lambda: st.customized_cleaning)),
        "rooms": rooms,
        "room_now": vb.room_name(getattr(room_now, "custom_name", None) or getattr(room_now, "name", None),
                                 getattr(room_now, "id", 0), cfg) if room_now else None,
        "area": area if isinstance(area, (int, float)) else None,
        "minutes": minutes if isinstance(minutes, (int, float)) else None,
        "consumables": {k: v for k, v in cons.items() if isinstance(v, (int, float))},
        "station": {k: v for k, v in station.items() if v},
        "updated": _now(),
    }


def render_map(dev, cfg):
    """Карта PNG (рендер бібліотеки) з українськими назвами кімнат; ≤ ~250 КБ, щоб влізти в запис DynamoDB."""
    from dreame.map import DreameVacuumMapRenderer
    st = dev.status
    # рендер бере назви за ТИПОМ кімнати (0 — шаблон «… %index%»), а номер («Коридор 2») додає сам
    names = {0: "Кімната %index%"}
    for sid, s in (st.segments or {}).items():
        t = getattr(s, "type", 0) or 0
        raw = (getattr(s, "name", None) or "").strip()
        base, _, tail = raw.rpartition(" ")
        if t and raw:
            names[t] = vb.room_name(base if base and tail.isdigit() else raw, sid)
    map_data = st.current_map or st.selected_map
    if map_data is None:
        return None
    renderer = DreameVacuumMapRenderer(robot_type=_g(lambda: dev.capability.robot_type) or 0, segment_names=names)
    png = renderer.render_map(dev.get_map_for_render(map_data), _g(lambda: st.robot_status) or 0,
                              _g(lambda: st.station_status) or 0)
    if png and len(png) > 250_000:
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        img.thumbnail((900, 900))
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        png = buf.getvalue()
    return png


# --- Telegram

def _tg(method, payload):
    req = urllib.request.Request(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[vacuum] tg {method}: {type(e).__name__}: {e}")
        return None


def send_photo(chat, png, caption):
    b = uuid.uuid4().hex
    parts = [f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n".encode(),
             f"--{b}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode(),
             f"--{b}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"map.png\"\r\n"
             f"Content-Type: image/png\r\n\r\n".encode(), png, f"\r\n--{b}--\r\n".encode()]
    req = urllib.request.Request(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendPhoto",
                                 data=b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"[vacuum] sendPhoto: {type(e).__name__}: {e}")


def edit(chat, msg, text, markup=None):
    if chat and msg:
        _tg("editMessageText", {"chat_id": chat, "message_id": msg, "text": text,
                                **({"reply_markup": markup} if markup else {})})
    elif chat:
        _tg("sendMessage", {"chat_id": chat, "text": text, **({"reply_markup": markup} if markup else {})})


def home_chats(store):
    import bot
    return bot.approved_chat_ids(store)


def owner_chats(store):
    import bot
    users = bot.get_users(store)
    return [uid for uid in bot.owners(users)]


def tell(chats, text, silent=True):
    for c in chats:
        _tg("sendMessage", {"chat_id": c, "text": text, "disable_notification": silent})


# --- операції

def firmware_watch(store, meta, old_fw):
    """Оновилась прошивка — команди могли змінитись: попередити власника перевірити функції."""
    new_fw = meta.get("fw")
    if old_fw and new_fw and new_fw != old_fw:
        tell(owner_chats(store), f"⚠️ Пилосос: оновилась прошивка {old_fw} → {new_fw}. Протокол Dreame неофіційний — "
                                 f"перевірте в боті прибирання кімнати, «На базу» і карту.", silent=False)


def run(event, store, cfg):
    op = event.get("op")
    chat, msg = event.get("chat"), event.get("msg")
    prev = store.get_meta("vacuum") or {}
    if op == "watch" and not prev.get("active"):
        return {"op": op, "skip": "not active"}
    lock = acquire_lock()
    if not lock:
        edit(chat, msg, "⏳ Пилосос зайнятий іншою командою — спробуйте за хвилину.", vb.main_markup(prev))
        return {"op": op, "error": "locked"}
    dev = None
    try:
        sec = load_secret()
        dev = connect(sec)
        wait_ready(dev, timeout=20 if op in ("map", "check", "status") or not prev.get("rooms") else 8)
        meta = collect(dev, cfg, prev)
        firmware_watch(store, meta, prev.get("fw"))
        result = {"op": op}
        new_hist = [h for h in meta.get("history") or [] if (h.get("ts") or 0) >= (prev.get("hist_arch") or 0) - 86_400_000]
        if new_hist:  # останні записи (+ доба назад: свіжий запис міг дописатись після прибирання)
            result["archived"] = archive_history(store, new_hist)
            meta["hist_arch"] = max(h["ts"] for h in new_hist if h.get("ts"))
        if op == "backfill":
            result["archived"], result["cloud_records"] = backfill(dev, store, int(event.get("limit") or 500))

        if op == "clean":
            ids = [int(i) for i in event.get("rooms") or []]
            known = {r["id"] for r in meta["rooms"]}
            ids = [i for i in ids if i in known]
            times = max(1, min(vb.MAX_TIMES, int(event.get("times") or 1)))
            mode = event.get("mode")
            if not ids or mode not in vb.MODES:
                raise ValueError("порожній список кімнат або невідомий режим")
            want = vb.MODES[mode][0]
            cur = vb.MODE_NAME_TO_KEY.get(meta.get("mode") or "")
            if cur != mode:
                dev.set_cleaning_mode(want)
                time.sleep(1)
            if meta.get("customized") or (meta.get("cleangenius") not in (None, "off")):
                note = "\n⚠️ У застосунку ввімкнено CleanGenius або власні налаштування кімнат — пилосос може змінити проходи й режим."
            else:
                note = ""
            r = dev.clean_segment(ids, cleaning_times=times)
            if isinstance(r, dict) and r.get("code") not in (0, None):
                raise RuntimeError(f"відповідь пилососа: code {r.get('code')}")
            rooms_text = vb.names(ids, meta["rooms"])
            meta["active"] = {"rooms": ids, "rooms_text": rooms_text, "times": times, "mode": mode,
                              "by": event.get("by"), "started": _now(), "seen_running": False}
            meta["state"] = "sweeping" if mode != "mop" else "mopping"
            edit(chat, msg, f"▶️ {meta.get('name', 'Пилосос')} почав прибирання: {rooms_text} · {times}× · "
                            f"{vb.mode_label(mode)}{note}\nСповіщу, коли закінчить.", vb.main_markup(meta))
            others = [c for c in home_chats(store) if str(c) != str(chat)]
            tell(others, f"🤖 {event.get('by') or 'Хтось'} запустив прибирання: {rooms_text} · {times}× · "
                         f"{vb.mode_label(mode)}")
            result["started"] = ids
        elif op == "home":
            r = dev.return_to_base()
            if isinstance(r, dict) and r.get("code") not in (0, None):
                raise RuntimeError(f"відповідь пилососа: code {r.get('code')}")
            meta["state"] = "returning"
            if meta.get("active"):
                meta["active"]["stopped_by"] = event.get("by")
            edit(chat, msg, f"⏏️ {meta.get('name', 'Пилосос')} їде на базу.", vb.main_markup(meta))
        elif op == "map":
            png = render_map(dev, cfg)
            if png:
                store.put_meta("vacuum_map", {"png": base64.b64encode(png).decode(), "ts": _now()})
                if chat:
                    send_photo(chat, png, f"🗺 {meta.get('name', 'Пилосос')} · {vb.state_text(meta.get('state'))}")
            elif chat:
                edit(chat, None, "🗺 Карту не отримано — пилосос ще не передав її. Спробуйте за хвилину.")
            result["map_bytes"] = len(png or b"")
        elif op == "watch":
            act = meta["active"]
            act["seen_running"] = act.get("seen_running") or meta["running"] or meta["state"] not in vb.DOCKED_STATES
            age_min = (_now() - act.get("started", _now())) / 60000
            finished = (act["seen_running"] and meta["state"] in vb.DOCKED_STATES and not meta["running"]) or age_min > 240
            if finished:
                h0 = (meta.get("history") or [{}])[0]
                if h0.get("ts") and h0["ts"] >= act.get("started", 0) - 120_000:  # запис саме цього прибирання
                    meta["area"], meta["minutes"] = h0.get("area") or meta.get("area"), h0.get("minutes") or meta.get("minutes")
                done = (f"✅ {meta.get('name', 'Пилосос')} закінчив: {act.get('rooms_text', '')}"
                        + (f" · {meta['area']} м²" if meta.get("area") else "")
                        + (f" за {meta['minutes']} хв" if meta.get("minutes") else "")
                        + (" (зупинено вручну)" if act.get("stopped_by") else ""))
                tell(home_chats(store), done)
                meta["last"] = {**act, "finished": _now(), "area": meta.get("area"), "minutes": meta.get("minutes")}
                meta["active"] = None
                try:  # свіжа карта з пройденим маршрутом — на дашборд
                    png = render_map(dev, cfg)
                    if png:
                        store.put_meta("vacuum_map", {"png": base64.b64encode(png).decode(), "ts": _now()})
                except Exception as e:
                    print(f"[vacuum] map after clean: {type(e).__name__}: {e}")
                result["finished"] = True
        elif op == "check":
            try:
                png = render_map(dev, cfg)
                if png:
                    store.put_meta("vacuum_map", {"png": base64.b64encode(png).decode(), "ts": _now()})
            except Exception as e:
                print(f"[vacuum] map: {type(e).__name__}: {e}")
        if op == "status":
            edit(chat, msg, vb.status_text(meta, cfg), vb.main_markup(meta))
        meta.pop("lost", None)
        meta.pop("fail", None)
        keep_token(sec, dev._protocol.cloud)
        store.put_meta("vacuum", meta)
        return result
    except LoginLost:
        prev["lost"] = _now()
        store.put_meta("vacuum", prev)
        edit(chat, msg, "🔑 Вхід у Dreame втрачено — власнику треба один раз увійти заново.")
        if not prev.get("lost_told"):
            tell(owner_chats(store), "🔑 Пилосос: вхід у Dreame за токеном не вдався. Запустіть на ПК "
                                     "tools/dreame_login.py (пароль Dreamehome вводите ви).", silent=False)
            prev["lost_told"] = _now()
            store.put_meta("vacuum", prev)
        return {"op": op, "error": "login lost"}
    except Exception as e:
        traceback.print_exc()
        what = {"clean": "прибирання", "home": "«На базу»", "map": "карта", "status": "стан",
                "watch": "стеження за прибиранням", "check": "щоденна перевірка"}.get(op, op)
        edit(chat, msg, f"⚠️ Не вдалося ({what}): {type(e).__name__}: {str(e)[:150]}", vb.main_markup(prev))
        # власнику — раз на 6 год: протокол неофіційний, збій команди = привід перевірити функції
        if _now() - (prev.get("fail") or {}).get("told", 0) > 6 * 3600_000:
            tell(owner_chats(store), f"⚠️ Пилосос: {what} — збій {type(e).__name__}: {str(e)[:150]}. "
                                     f"Якщо повторюється — Dreame могла змінити протокол чи прошивку, перевірте функції.",
                 silent=False)
            prev["fail"] = {"told": _now(), "op": op, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            store.put_meta("vacuum", prev)
        return {"op": op, "error": f"{type(e).__name__}: {e}"}
    finally:
        if dev is not None:
            try:
                dev.disconnect()
            except Exception:
                pass
        release_lock(lock)


def lambda_handler(event, context):
    from devices import load_config
    from store import DynamoStore
    cfg = load_config()
    store = DynamoStore(os.environ["TABLE_NAME"], cfg.get("history_ttl_days", 180), cfg.get("events_ttl_days", 730))
    res = run(event or {}, store, cfg)
    store.flush()
    print("[vacuum]", res)
    return res
