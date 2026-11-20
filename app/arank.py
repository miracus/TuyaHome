"""🏆 Рейтинг міст України (населення > 50 тис.) за тривогами за останні 365 днів — без зберігання історії.

Зберігаємо лише лічильники по днях для кожного міста: ARCH#arank / <місто> = {days: {"YYYY-MM-DD": [тривог, хвилин]}}.
Наповнення за минулий рік — tools/alert_rank_build.py (відкритий набір даних + історія siren.pp.ua).
Далі — раз на добу (опитувач після півночі): історія тривог регіонів міст за вчора (siren.pp.ua regionHistory,
порціями), вчорашній день дописується в лічильники, перераховується підсумок META arank (його читає дашборд),
дні старші за 400 — відкидаються.

Місто під тривогою, якщо тривога на рівні його громади, його району або його області (як у боті).
Окуповані міста не враховуються. Населення — оцінка Держстату на 01.01.2022 (пізніших офіційних немає;
через війну й внутрішню міграцію реальні числа інші — тому населення лише фільтр «> 50 тис.», а не вага)."""
import time
from datetime import datetime, timedelta

from devices import local_tz

# (місто, область, населення 01.01.2022, тис.) — підконтрольні Україні; окуповані (Донецьк, Луганськ, Маріуполь,
# Мелітополь, Бердянськ, Енергодар, Горлівка, Макіївка, Сєвєродонецьк, Лисичанськ, Алчевськ, Кадіївка, Бахмут,
# міста Криму тощо) свідомо не включено. Покровськ — на лінії фронту, виключено.
CITIES = [
    ("Київ", "м. Київ", 2952), ("Харків", "Харківська область", 1421), ("Одеса", "Одеська область", 1010),
    ("Дніпро", "Дніпропетровська область", 969), ("Львів", "Львівська область", 717),
    ("Запоріжжя", "Запорізька область", 711), ("Кривий Ріг", "Дніпропетровська область", 603),
    ("Миколаїв", "Миколаївська область", 470), ("Вінниця", "Вінницька область", 370),
    ("Полтава", "Полтавська область", 283), ("Чернігів", "Чернігівська область", 282),
    ("Херсон", "Херсонська область", 279), ("Хмельницький", "Хмельницька область", 274),
    ("Черкаси", "Черкаська область", 272), ("Чернівці", "Чернівецька область", 264),
    ("Житомир", "Житомирська область", 261), ("Суми", "Сумська область", 256), ("Рівне", "Рівненська область", 244),
    ("Івано-Франківськ", "Івано-Франківська область", 238), ("Кам’янське", "Дніпропетровська область", 226),
    ("Тернопіль", "Тернопільська область", 225), ("Кропивницький", "Кіровоградська область", 222),
    ("Кременчук", "Полтавська область", 216), ("Луцьк", "Волинська область", 213),
    ("Біла Церква", "Київська область", 207), ("Краматорськ", "Донецька область", 147),
    ("Ужгород", "Закарпатська область", 115), ("Бровари", "Київська область", 109),
    ("Слов’янськ", "Донецька область", 106), ("Нікополь", "Дніпропетровська область", 105),
    ("Павлоград", "Дніпропетровська область", 101), ("Кам’янець-Подільський", "Хмельницька область", 98),
    ("Мукачево", "Закарпатська область", 85), ("Конотоп", "Сумська область", 84), ("Умань", "Черкаська область", 81),
    ("Олександрія", "Кіровоградська область", 78), ("Дрогобич", "Львівська область", 74),
    ("Бердичів", "Житомирська область", 73), ("Шостка", "Сумська область", 72), ("Ізмаїл", "Одеська область", 69),
    ("Ковель", "Волинська область", 68), ("Новомосковськ", "Дніпропетровська область", 68),
    ("Костянтинівка", "Донецька область", 67), ("Сміла", "Черкаська область", 66), ("Калуш", "Івано-Франківська область", 66),
    ("Ніжин", "Чернігівська область", 66), ("Червоноград", "Львівська область", 65), ("Ірпінь", "Київська область", 65),
    ("<ваше місто>", "Київська область", 63), ("Первомайськ", "Миколаївська область", 63),
    ("Коломия", "Івано-Франківська область", 61), ("Стрий", "Львівська область", 59),
    ("Чорноморськ", "Одеська область", 58), ("Звягель", "Житомирська область", 55), ("Лозова", "Харківська область", 54),
    ("Дружківка", "Донецька область", 54), ("Прилуки", "Чернігівська область", 53),
    ("Нововолинськ", "Волинська область", 51), ("Горішні Плавні", "Полтавська область", 50),
]
ALIASES = {"Звягель": ["Звягель", "Новоград-Волинський"], "Новомосковськ": ["Новомосковськ", "Самар"],
           "Червоноград": ["Червоноград", "Шептицький"], "Горішні Плавні": ["Горішні плавні", "Горішні Плавні"]}


def _n(s):
    return (s or "").lower().replace("’", "'").replace("ʼ", "'").replace("`", "'")


def catalog_from(data):
    idx = {}

    def walk(n, parent):
        rid = str(n["regionId"])
        idx[rid] = {"id": rid, "name": n["regionName"], "type": n.get("regionType"), "parent": parent}
        for k in n.get("regionChildIds") or []:
            walk(k, rid)
    for st in data.get("states", []):
        walk(st, None)
    return idx


# громади без «м. …» у назві (довідник тривог)
HROMADA = {"Умань": ("Уманська територіальна громада", "Уманський район"),
           "Сміла": ("Смілянська територіальна громада", "Черкаський район"),
           "Костянтинівка": ("Костянтинівська територіальна громада", "Краматорський район"),
           "Дружківка": ("Дружківська територіальна громада", "Краматорський район")}


def resolve_cities(cat):
    """Місто → його регіони тривог. Звичайне місто — громада в районі в області: ids = [громада, район, область].
    Великі міста, що в довіднику окремі (Харків, Запоріжжя), — ids = [місто, область]."""
    out = []
    states = {_n(r["name"]): r for r in cat.values() if r["parent"] is None}
    for name, oblast, pop in CITIES:
        e = {"name": name, "oblast": oblast, "pop": pop, "ids": [], "raion": None, "hromada": None}
        if oblast == "м. Київ":
            st = states.get(_n("м. Київ"))
            if st:
                e["ids"] = [st["id"]]
            out.append(e)
            continue
        ob = states.get(_n(oblast))
        hit = None
        if name in HROMADA:
            hname, rname = HROMADA[name]
            hit = next(((r, cat[r["parent"]]) for r in cat.values() if r["type"] == "Community" and _n(r["name"]) == _n(hname)
                        and r["parent"] and _n(cat[r["parent"]]["name"]) == _n(rname)), None)
        for alias in ([] if hit else ALIASES.get(name, [name])):
            for r in cat.values():
                if not _n(r["name"]).startswith(_n(f"м. {alias} ")):
                    continue
                if r["type"] == "Community":
                    up = cat.get(r["parent"]); top = cat.get(up["parent"]) if up else None
                    if top and _n(top["name"]) == _n(oblast):
                        hit = (r, up)
                        break
                elif r["parent"] is None and ob:   # місто — окремий регіон верхнього рівня
                    hit = (r, None)
                    break
            if hit:
                break
        if hit:
            r, up = hit
            e["hromada"] = r["name"]
            if up:
                e.update(ids=[r["id"], up["id"], ob["id"] if ob else cat[up["parent"]]["id"]], raion=up["name"])
            else:
                e["ids"] = [r["id"]] + ([ob["id"]] if ob else [])
        out.append(e)
    return out


def summarize(docs, today):
    """Підсумок за 365 днів до today включно: місця за сумарною тривалістю (менше — безпечніше)."""
    t = datetime.fromisoformat(today).date()
    frm = (t - timedelta(days=364)).isoformat()
    rows = []
    for d in docs:
        days = {k: v for k, v in (d.get("days") or {}).items() if frm <= k <= today}
        n = sum(v[0] for v in days.values())
        minutes = sum(v[1] for v in days.values())
        est = d.get("est")  # оцінка прогалини в даних (див. tools/alert_rank_build.py) — частка, що ще у вікні
        if est and est.get("to", "") >= frm:
            g0 = datetime.fromisoformat(est["from"]).date(); g1 = datetime.fromisoformat(est["to"]).date()
            part = ((g1 - max(g0, t - timedelta(days=364))).days + 1) / ((g1 - g0).days + 1)
            n += round(est["n"] * part); minutes += round(est["min"] * part)
        else:
            est = None
        rows.append({"name": d["name"], "oblast": d.get("oblast"), "pop": d.get("pop"), "n": n,
                     "hours": round(minutes / 60, 1), "share": round(minutes / (365 * 1440) * 100, 2),
                     "avg_min": round(minutes / n) if n else 0, "longest_day_h": round(max((v[1] for v in days.values()), default=0) / 60, 1),
                     "quiet_days": 365 - sum(1 for v in days.values() if v[1] > 0),
                     "estimated": bool(est), "est_from": est and est["from"], "est_to": est and est["to"],
                     "partial_days": len([x for x in d.get("partial_days") or [] if frm <= x <= today])})
    rows.sort(key=lambda r: (r["hours"], r["n"]))
    return {"from": frm, "to": today, "updated": int(time.time() * 1000), "cities": rows}


HIST_URL = "https://siren.pp.ua/api/v3/alerts/regionHistory?regionId={}"
PER_RUN = 8   # регіонів за запуск опитувача (кожен — окремий запит); 130 регіонів ≈ 16 запусків ≈ 30 хв після півночі
PAUSE_S = 1.5  # пауза між запитами: siren.pp.ua відповідає 429, якщо частіше


def _ms(iso, default=None):
    try:
        d = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return int(d.timestamp() * 1000) if d.year >= 2000 else default   # 0001-01-01 — тривога ще триває
    except (ValueError, OSError):
        return default


def _history(rid):
    import json
    import urllib.request
    req = urllib.request.Request(HIST_URL.format(rid), headers={"User-Agent": "TuyaHome"})
    with urllib.request.urlopen(req, timeout=8) as r:
        d = json.loads(r.read().decode())
    return (d[0] if isinstance(d, list) and d else {}).get("alarms") or []


def run(store, cfg, now, alerts=None):
    """Раз на добу (19.11: не щодві хвилини): після півночі збираємо останні 20 тривог кожного регіону міст
    (порціями по PER_RUN за запуск опитувача), рахуємо вчорашній день для кожного міста, дописуємо в лічильники
    й перераховуємо рейтинг. Протягом дня — нічого не робимо."""
    if not cfg.get("alert_rank", {}).get("enabled", True):
        return None
    tz = local_tz(cfg)
    today = datetime.fromtimestamp(now / 1000, tz).date()
    yday = (today - timedelta(days=1)).isoformat()
    st = store.get_meta("arank_daily") or {}
    if st.get("done") == yday:
        return None
    if st.get("day") != yday:  # нова доба — нова черга регіонів
        docs = store.list_archive("arank")
        if not docs:
            return "not seeded"
        cities = {d["name"]: d["ids"] for d in docs}
        st = {"day": yday, "cities": cities, "queue": sorted({i for v in cities.values() for i in v}), "hist": {}}
    fetched = 0
    while st["queue"] and fetched < PER_RUN:
        rid = st["queue"][0]
        try:
            al = _history(rid)
        except Exception as e:  # не відповів — спробуємо в наступний запуск
            print(f"[arank] history {rid}: {type(e).__name__}: {e}")
            break
        st["hist"][rid] = [[_ms(x.get("startDate")), _ms(x.get("endDate"), now)] for x in al if _ms(x.get("startDate"))]
        st["queue"].pop(0)
        fetched += 1
        if st["queue"] and fetched < PER_RUN:
            time.sleep(PAUSE_S)
    if st["queue"]:
        store.put_meta("arank_daily", st)
        return {"day": yday, "left": len(st["queue"])}
    # усі регіони зібрано — рахуємо вчора для кожного міста
    d0 = int(datetime(today.year, today.month, today.day, tzinfo=tz).timestamp() * 1000) - 86_400_000
    y = datetime.fromtimestamp(d0 / 1000 + 43200, tz).date()   # північ учора (з урахуванням переходу часу)
    d0 = int(datetime(y.year, y.month, y.day, tzinfo=tz).timestamp() * 1000)
    d1 = int(datetime(today.year, today.month, today.day, tzinfo=tz).timestamp() * 1000)
    counts, partial = {}, []
    for name, ids in st["cities"].items():
        iv = sorted([a, b] for rid in ids for a, b in st["hist"].get(rid, []) if a < d1 and b > d0)
        merged = []
        for a, b in iv:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        n = sum(1 for a, _ in merged if a >= d0)
        minutes = round(sum(min(b, d1) - max(a, d0) for a, b in merged) / 60000)
        counts[name] = [n, minutes]
        # 20 останніх тривог не дотягнулись до початку вчорашнього дня — день порахований неповністю
        if any(len(st["hist"].get(rid, [])) >= 20 and min(a for a, _ in st["hist"][rid]) > d0 for rid in ids):
            partial.append(name)
    _flush(store, yday, counts, today.isoformat(), partial)
    store.put_meta("arank_daily", {"done": yday, "partial": partial})
    return {"day": yday, "done": True, "partial": len(partial)}


def _flush(store, day, counts, today, partial=()):
    docs = store.list_archive("arank")
    keep = (datetime.fromisoformat(today).date() - timedelta(days=400)).isoformat()
    for d in docs:
        days = {k: v for k, v in (d.get("days") or {}).items() if k >= keep}
        if d["name"] in counts:
            days[day] = counts[d["name"]]          # перезаписуємо: наповнення могло порахувати день частково
        d["days"] = days
        pd = [x for x in (d.get("partial_days") or []) if x >= keep and x != day]
        d["partial_days"] = pd + ([day] if d["name"] in partial else [])
        store.put_archive("arank", d["name"], d)
    store.put_meta("arank", summarize(docs, day))
    return day
