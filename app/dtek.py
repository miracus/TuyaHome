"""Заявка «немає світла» в чат-бот ДТЕК КРЕМ — від Telegram-акаунта власника (Telethon, MTProto).

Звичайний бот не може писати іншому боту, тому пишемо як людина: ключ входу (StringSession) у SSM SecureString,
створює його `tools/dtek_tg.py login` (код і пароль вводить сам власник). Код працює ЛИШЕ з чатом бота ДТЕК.

Обмеження (рішення 16.11): одна заявка — і наступну не подаємо, доки ДТЕК не відповів;
без відповіді — повтор не раніше ніж через repeat_after_h (12 год).

meta dtek_report: {"status": "submitting"|"open"|"answered"|"failed", "ts", "by", "by_uid", "last_id",
                   "answered_ts", "last_msg_ts", "error"}
  open      — подано, відповіді ще немає (блокує повтор на repeat_after_h);
  answered  — ДТЕК відповів після подання (повтор дозволено), ще стежимо за результатом розгляду;
  failed    — подати не вдалося (не блокує).
Відповідь = повідомлення бота ДТЕК, що прийшло ПІСЛЯ завершення сценарію подання (відповіді під час
сценарію — автоматичні підтвердження, вони не рахуються)."""
import asyncio
import json
import os
import re
import time

H = 3600 * 1000


def conf(cfg):
    return {"enabled": False, "bot": "DTEKKyivRegionElektromerezhiBot", "repeat_after_h": 12, "watch_days": 7,
            "quiet_after_answer_h": 72, "ssm_param": "/tuyahome/tg_user", "flow": [], **(cfg.get("dtek_report") or {})}


def state(store):
    return store.get_meta("dtek_report") or {}


def can_submit(st, now, cfg):
    """(можна?, коли можна наступну в мс або None)."""
    c = conf(cfg)
    if st.get("status") in ("open", "submitting"):
        # «submitting» завислий (Lambda впала посеред подання) — через 10 хв вважаємо невдалим
        if st["status"] == "submitting" and now - st.get("ts", 0) > 10 * 60 * 1000:
            return True, None
        nxt = st.get("ts", 0) + c["repeat_after_h"] * H
        return now >= nxt, nxt
    return True, None


def watching(st, now, cfg):
    """Чи читати чат ДТЕК у цьому запуску опитувача."""
    c = conf(cfg)
    if st.get("status") not in ("open", "answered"):
        return False
    if now - st.get("ts", 0) > c["watch_days"] * 24 * H:
        return False
    if st["status"] == "answered" and now - st.get("last_msg_ts", st.get("ts", 0)) > c["quiet_after_answer_h"] * H:
        return False
    return True


# --- Telethon (імпорт лише тут: бібліотека — у шарі Lambda опитувача)
def _secret(c):
    import boto3
    p = boto3.client("ssm").get_parameter(Name=c["ssm_param"], WithDecryption=True)
    return json.loads(p["Parameter"]["Value"])


def _client(sec):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(sec["session"]), int(sec["api_id"]), sec["api_hash"],
                          device_model="TuyaHome", app_version="1.0", receive_updates=False)


def _buttons(m):
    rm = getattr(m, "reply_markup", None)
    return [[getattr(b, "text", "") for b in row.buttons] for row in getattr(rm, "rows", None) or []]


def _fmt(m):
    t = (m.message or "").strip()
    if m.media and not t:
        t = "[вкладення]"
    return t


async def _incoming_after(cl, peer, after_id, limit=20):
    msgs = await cl.get_messages(peer, limit=limit, min_id=after_id)
    return sorted([m for m in msgs if not m.out], key=lambda m: m.id)


async def _wait_reply(cl, peer, after_id, wait_s=15):
    """Чекаємо відповідь бота (кілька повідомлень поспіль — збираємо всі)."""
    end, got = time.time() + wait_s, []
    while time.time() < end:
        got = await _incoming_after(cl, peer, after_id)
        if got:
            await asyncio.sleep(1.5)
            return await _incoming_after(cl, peer, after_id)
        await asyncio.sleep(1)
    return got


async def _run_flow(cl, peer, flow):
    """Сценарій з config.dtek_report.flow: кроки {"send": текст} | {"click": regex кнопки} | {"expect": regex}.
    click шукає кнопку в останніх відповідях бота: inline — натискаємо, звичайна клавіатура — надсилаємо її текст."""
    last = (await cl.get_messages(peer, limit=1) or [None])[0]
    last_id = last.id if last else 0
    replies = []
    for i, step in enumerate(flow, 1):
        if "send" in step:
            sent = await cl.send_message(peer, step["send"])
            got = await _wait_reply(cl, peer, sent.id, step.get("wait_s", 15))
        elif "click" in step:
            rx = re.compile(step["click"], re.I)
            target = next(((m, b) for m in reversed(replies or ([last] if last else [])) for row in _buttons(m)
                           for b in row if rx.search(b)), None)
            if not target:
                raise RuntimeError(f"крок {i}: немає кнопки «{step['click']}»")
            m, text = target
            from telethon.tl.types import ReplyInlineMarkup
            if isinstance(m.reply_markup, ReplyInlineMarkup):
                ref = (await cl.get_messages(peer, limit=1))[0].id
                before = (_fmt(m), _buttons(m))
                await m.click(text=text)
                got = await _wait_reply(cl, peer, ref, step.get("wait_s", 15))
                if not got:  # бот міг не писати нове, а відредагувати це ж повідомлення
                    fresh = await cl.get_messages(peer, ids=m.id)
                    if fresh and (_fmt(fresh), _buttons(fresh)) != before:
                        got = [fresh]
            else:
                sent = await cl.send_message(peer, text)
                got = await _wait_reply(cl, peer, sent.id, step.get("wait_s", 15))
        elif "expect" in step:
            if not any(re.search(step["expect"], _fmt(m), re.I) for m in replies):
                raise RuntimeError(f"крок {i}: бот відповів не те, що очікували ({step['expect']})")
            continue
        else:
            continue
        if not got and not step.get("optional"):
            raise RuntimeError(f"крок {i}: бот ДТЕК не відповів")
        replies = got or replies
        last_id = max([last_id] + [m.id for m in got])
    return replies, last_id


def submit(cfg):
    """Подати заявку. Повертає {"ok", "replies": [текст], "last_id", "error"}."""
    c = conf(cfg)
    if not c["flow"]:
        return {"ok": False, "error": "сценарій подання ще не налаштований (config.dtek_report.flow)"}

    async def go():
        async with _client(_secret(c)) as cl:
            peer = await cl.get_entity(c["bot"])
            replies, last_id = await _run_flow(cl, peer, c["flow"])
            return {"ok": True, "replies": [_fmt(m) for m in replies if _fmt(m)], "last_id": last_id}
    try:
        return asyncio.run(go())
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}


def fetch_new(cfg, after_id):
    """Нові повідомлення бота ДТЕК після after_id: [(id, ts_ms, текст)]."""
    c = conf(cfg)

    async def go():
        async with _client(_secret(c)) as cl:
            peer = await cl.get_entity(c["bot"])
            return [(m.id, int(m.date.timestamp() * 1000), _fmt(m)) for m in await _incoming_after(cl, peer, after_id, 50)]
    return asyncio.run(go())


# --- Виклики з опитувача
def run_submit(store, cfg, notifier, now=None):
    """Подія {"dtek_submit": …} від кнопки бота (web-Lambda викликає опитувача асинхронно)."""
    now = now or int(time.time() * 1000)
    st = state(store)
    by = st.get("by", "")
    res = submit(cfg)
    if res["ok"]:
        store.put_meta("dtek_report", {**st, "status": "open", "ts": now, "last_id": res["last_id"], "last_msg_ts": now})
        ack = "\n".join(res["replies"])[-1500:]
        text = f"📨 Заявку «немає світла» подано в ДТЕК ({by}).\n" + (f"\nДТЕК відповів:\n{ack}" if ack else "")
        store.add_event(now, {"kind": "dtek", "text": f"Заявку в ДТЕК подано ({by})"})
    else:
        store.put_meta("dtek_report", {**st, "status": "failed", "error": res["error"]})
        # невдача — лише тому, хто натиснув, і власнику (решті «Дім» з цим нічого робити)
        text = f"⚠️ Не вдалося подати заявку в ДТЕК: {res['error']}\nПодайте вручну: бот ДТЕК або 1588."
        store.add_event(now, {"kind": "dtek", "text": f"Заявка в ДТЕК не подана: {res['error']}"})
        import bot
        to = sorted(({st.get("by_uid")} | set(bot.owners(bot.get_users(store)))) - {None})
        notifier.send(text, to=to or None, kind="dtek")
        return res
    notifier.send(text, kind="dtek")  # отримувачі опитувача = усі «Дім»
    return res


def ping(cfg):
    """Перевірка з Lambda без жодних повідомлень: шар Telethon, ключ у SSM, вхід, бот ДТЕК знайдено."""
    import telethon
    out = {"telethon": telethon.__version__}
    c = conf(cfg)
    try:
        sec = _secret(c)
    except Exception as e:
        return {**out, "ssm": type(e).__name__}

    async def go():
        async with _client(sec) as cl:
            me = await cl.get_me()
            peer = await cl.get_entity(c["bot"])
            return {"me": me.first_name, "bot": getattr(peer, "first_name", "?")}
    try:
        return {**out, **asyncio.run(go())}
    except Exception as e:
        return {**out, "error": f"{type(e).__name__}: {e}"[:200]}


def check_replies(store, cfg, notifier, now=None):
    """Кожен запуск опитувача, поки заявка «жива»: нові повідомлення ДТЕК → усім «Дім»."""
    now = now or int(time.time() * 1000)
    st = state(store)
    if not watching(st, now, cfg):
        return None
    try:
        msgs = fetch_new(cfg, st.get("last_id", 0))
    except Exception as e:
        print(f"[dtek] не вдалося прочитати чат ДТЕК: {type(e).__name__}: {e}")
        return "error"
    if not msgs:
        return 0
    for mid, ts, text in msgs:
        notifier.send(f"📩 ДТЕК відповів на заявку:\n{text[:3000]}", ts=ts, kind="dtek")
        store.add_event(now, {"kind": "dtek", "text": f"ДТЕК: {text[:300]}"})
    st.update(status="answered", last_id=msgs[-1][0], last_msg_ts=now)
    st.setdefault("answered_ts", now)
    store.put_meta("dtek_report", st)
    return len(msgs)
