"""Гра «Пляшечки» на дашборді: рекорди гравців (ARCH#game / <гравець>, без строку зберігання).

Гравець визначається так:
  - у Telegram (дашборд відкрито кнопкою бота) — за підписаними даними міні-застосунку (initData, HMAC ключем бота):
    id = tg:<user_id>, ім'я — як у списку користувачів бота;
  - у звичайному браузері — id = web:<випадковий id браузера>, ім'я вводить сам гравець.
Сервер лише зберігає найкращий результат (рівень і очки беруться з більшого) — гра рахує сама, у браузері."""
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import parse_qsl


def tg_user(init_data, token, max_age_s=7 * 86400):
    """Перевіряє підпис initData міні-застосунку Telegram → {"id", "first_name", …} або None."""
    if not init_data or not token:
        return None
    try:
        params = dict(parse_qsl(init_data, keep_blank_values=True))
        given = params.pop("hash", "")
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, given):
            return None
        if max_age_s and time.time() - int(params.get("auth_date", "0")) > max_age_s:
            return None
        return json.loads(params.get("user") or "{}") or None
    except (ValueError, KeyError):
        return None


def _clean_name(name):
    name = re.sub(r"[\x00-\x1f<>]", "", str(name or "")).strip()
    return name[:24] or "Гравець"


def identify(store, body):
    """(id гравця, ім'я) з тіла запиту: initData з Telegram або browser id + ім'я."""
    u = tg_user(body.get("initData"), os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    if u and u.get("id"):
        known = (store.get_meta("tg_users") or {}).get(str(u["id"])) or {}
        name = known.get("name") or " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
        return f"tg:{u['id']}", _clean_name(re.sub(r"\s*\(@[^)]*\)", "", name)), True
    pid = re.sub(r"[^a-zA-Z0-9_-]", "", str(body.get("pid") or ""))[:40]
    if len(pid) < 8:
        return None, None, False
    return f"web:{pid}", _clean_name(body.get("name")), False


def save(store, body):
    pid, name, tg = identify(store, body)
    if not pid:
        return {"error": "немає гравця"}
    try:
        level = max(0, min(int(body.get("level") or 0), 10000))
        score = max(0, min(int(body.get("score") or 0), 10_000_000))
    except (TypeError, ValueError):
        return {"error": "погані дані"}
    doc = store.get_archive("game", pid) or {"id": pid, "since": int(time.time() * 1000)}
    # рівень — найкращий; очки — поточні (витрачаються на 🧪 «+1 пляшечка»)
    doc.update(name=name, tg=tg, level=max(level, doc.get("level", 0)), score=score, updated=int(time.time() * 1000))
    store.put_archive("game", pid, doc)
    return {"ok": True, "me": doc, "top": top(store), "checkers_top": checkers_top(store)}


def me(store, body):
    pid, name, tg = identify(store, body)
    if not pid:
        return {"me": None, "top": top(store), "checkers_top": checkers_top(store)}
    doc = store.get_archive("game", pid) or {"id": pid, "name": name, "level": 0, "score": 0, "tg": tg}
    return {"me": {**doc, "name": doc.get("name") if not tg else name}, "top": top(store), "checkers_top": checkers_top(store)}


LEVEL_PTS = {"easy": 1, "medium": 2, "hard": 3}


def checkers_result(store, body):
    """Підсумок партії в шашки: перемоги/поразки/нічиї за рівнем складності."""
    pid, name, tg = identify(store, body)
    if not pid:
        return {"error": "немає гравця"}
    res, lvl = body.get("result"), body.get("level")
    if res not in ("win", "loss", "draw") or lvl not in LEVEL_PTS:
        return {"error": "погані дані"}
    doc = store.get_archive("game", pid) or {"id": pid, "since": int(time.time() * 1000), "level": 0, "score": 0}
    ck = doc.setdefault("checkers", {})
    row = ck.setdefault(lvl, {"w": 0, "l": 0, "d": 0})
    row[{"win": "w", "loss": "l", "draw": "d"}[res]] += 1
    doc.update(name=name, tg=tg, updated=int(time.time() * 1000))
    store.put_archive("game", pid, doc)
    return {"ok": True, "me": doc, "top": top(store), "checkers_top": checkers_top(store)}


def checkers_top(store, n=15):
    rows = []
    for d in store.list_archive("game"):
        ck = d.get("checkers") or {}
        pts = sum(LEVEL_PTS[k] * (v or {}).get("w", 0) for k, v in ck.items() if k in LEVEL_PTS)
        wins = sum((v or {}).get("w", 0) for v in ck.values())
        if wins:
            rows.append({"name": d.get("name"), "pts": pts, "w": wins, "tg": d.get("tg", False), "id": d.get("id")})
    rows.sort(key=lambda r: (-r["pts"], -r["w"]))
    return rows[:n]


def top(store, n=15):
    rows = [d for d in store.list_archive("game") if d.get("level", 0) > 0]
    rows.sort(key=lambda d: (-d.get("level", 0), -d.get("score", 0), d.get("updated", 0)))
    return [{"name": d.get("name"), "level": d.get("level", 0), "score": d.get("score", 0), "tg": d.get("tg", False),
             "id": d.get("id")} for d in rows[:n]]
