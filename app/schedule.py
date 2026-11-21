"""Графік відключень ДТЕК для черги адреси: завантаження, інтервали, сповіщення про зміни й нагадування.

Формат джерела (як на сайті ДТЕК): fact.data[<початок доби, сек>][<черга>][<година 1..24>] =
  yes — світло є; no — немає; first/second — немає перші/другі 30 хв; maybe/mfirst/msecond — можливо.
"""
import json
import re
import urllib.request

from devices import fmt_time

MIN = 60_000
HALF = 30 * MIN

# година → (стан першої половини, стан другої половини)
SLOT = {"yes": ("on", "on"), "no": ("off", "off"), "first": ("off", "on"), "second": ("on", "off"),
        "maybe": ("maybe", "maybe"), "mfirst": ("maybe", "on"), "msecond": ("on", "maybe")}


def fetch(url, group, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "TuyaHome"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    fact = data.get("fact") or {}
    days = {}
    for day, groups in (fact.get("data") or {}).items():
        if group in groups:
            days[str(day)] = groups[group]
    return {"update": fact.get("update"), "today": fact.get("today"), "days": days,
            "source_updated": data.get("lastUpdated")}


def slots(hours):
    """48 півгодинних станів доби."""
    out = []
    for h in range(1, 25):
        out += list(SLOT.get(hours.get(str(h), "yes"), ("on", "on")))
    return out


def intervals(day_sec, hours, state="off"):
    """[(початок_мс, кінець_мс)] безперервних проміжків зі станом state."""
    base = int(day_sec) * 1000
    res, start = [], None
    for i, s in enumerate(slots(hours) + ["end"]):
        if s == state and start is None:
            start = i
        elif s != state and start is not None:
            res.append((base + start * HALF, base + i * HALF))
            start = None
    return res


def describe(ivs, cfg):
    return ", ".join(f"{fmt_time(a, cfg)}–{'24:00' if (b - a) and fmt_time(b, cfg) == '00:00' else fmt_time(b, cfg)}"
                     for a, b in ivs) or "без відключень"


def day_label(day_sec, today_sec, cfg):
    d = int(day_sec) - int(today_sec or day_sec)
    name = {0: "сьогодні", 86400: "завтра"}.get(d, "")
    return f"{name} {fmt_time(int(day_sec) * 1000, cfg, with_date=True)[:5]}".strip()


def archive(store, sc, meta, new, now):
    """Архів графіка назавжди: на кожен день — остаточний графік і всі його версії (коли ДТЕК змінював).
    Ключ дня — початок доби в секундах (як у джерелі). Пишемо лише коли день новий або змінився."""
    seen = meta.setdefault("archived", {})
    n = 0
    for day, hours in sorted(new["days"].items()):
        if seen.get(day) == hours:
            continue
        doc = store.get_archive("schedule", day) or {"day": int(day), "group": sc["group"],
                                                     "address": sc.get("address"), "versions": []}
        if not doc["versions"] or doc["versions"][-1]["hours"] != hours:
            doc["versions"].append({"at": now, "update": new.get("update"), "hours": hours})
            doc["hours"] = hours
            doc["off"] = [list(iv) for iv in intervals(day, hours)]
            doc["maybe"] = [list(iv) for iv in intervals(day, hours, "maybe")]
            store.put_archive("schedule", day, doc)
            n += 1
        seen[day] = hours
    # пам'ять «що вже в архіві» — лише за останні дні, щоб meta не росла
    keep = int(new.get("today") or 0) - 2 * 86400
    meta["archived"] = {d: h for d, h in seen.items() if int(d) >= keep}
    return n


MIRROR = "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/{zone}.json"


def home_zone(cfg):
    """Зона дзеркала, з якої береться графік дому (з source_url)."""
    url = (cfg.get("outage_schedule") or {}).get("source_url", "")
    return url.rsplit("/", 1)[-1].replace(".json", "") if url else None


def subscriptions(store, cfg):
    """{(зона, черга): [uid…]} — кому слати «графік змінено». Дім без власної адреси — графік дому.
    None замість списку — користувачів бота ще немає (шлемо всім, як раніше)."""
    sc = cfg.get("outage_schedule") or {}
    home = (home_zone(cfg), sc.get("group"))
    users = {uid: u for uid, u in (store.get_meta("tg_users") or {}).items() if u.get("status") == "approved"}
    if not users:
        return {home: None}
    subs = {}
    for uid, u in users.items():
        addr = u.get("addr") or {}
        if addr.get("zone"):
            if addr.get("group"):
                subs.setdefault((addr["zone"], addr["group"]), []).append(uid)
        elif u.get("access", "home") == "home":
            subs.setdefault(home, []).append(uid)
    return subs


def fetch_zone(zone, timeout=8):
    """Усі черги зони: {"update", "today", "days": {доба: {черга: години}}}."""
    req = urllib.request.Request(MIRROR.format(zone=zone), headers={"User-Agent": "TuyaHome"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    fact = data.get("fact") or {}
    return {"update": fact.get("update"), "today": fact.get("today"),
            "days": {str(d): g for d, g in (fact.get("data") or {}).items()}}


def zone_groups(store, zone):
    days = (store.get_meta(f"zone#{zone}") or {}).get("days") or {}
    groups = {g for d in days.values() for g in d}
    return sorted(groups, key=lambda g: [int(x) for x in re.findall(r"\d+", g)])


def run_zones(store, cfg, notifier, now, subs):
    """Графіки інших зон/черг (адреси користувачів бота): оновлення раз на fetch_every_min і «графік змінено»."""
    sc = cfg.get("outage_schedule") or {}
    home = (home_zone(cfg), sc.get("group"))
    zones = {z for z, g in subs if (z, g) != home}
    out = {}
    for zone in sorted(zones):
        meta = store.get_meta(f"zone#{zone}") or {}
        if now - meta.get("fetched", 0) < sc.get("fetch_every_min", 15) * MIN:
            continue
        try:
            new = fetch_zone(zone)
        except Exception as e:
            out[zone] = f"{type(e).__name__}: {e}"
            meta["fetched"] = now
            store.put_meta(f"zone#{zone}", meta)
            continue
        old = meta.get("days") or {}
        if sc.get("notify_changes", True) and old:
            for (z, group), to in subs.items():
                if z != zone or (z, group) == home or not to:
                    continue
                for day in sorted(new["days"]):
                    if int(day) < int(new.get("today") or day):
                        continue
                    hours, before = new["days"][day].get(group), (old.get(day) or {}).get(group)
                    if hours is not None and hours != before:
                        head = "📅 Новий графік" if before is None else "📅 Графік змінено"
                        notifier.send(f"{head} на {day_label(day, new.get('today'), cfg)} (черга {group.replace('GPV', '')}): "
                                      f"світла не буде {describe(intervals(day, hours), cfg)}", ts=now, to=to, kind="schedule")
        store.put_meta(f"zone#{zone}", {**new, "fetched": now})
        out[zone] = "ok"
    return out


def run(store, cfg, notifier, now):
    """Оновлює графік (раз на fetch_every_min), шле сповіщення про зміни та нагадування перед відключенням."""
    sc = cfg.get("outage_schedule")
    if not sc:
        return None
    meta = store.get_meta("schedule") or {}
    summary = {}
    subs = subscriptions(store, cfg)
    home_to = subs.get((home_zone(cfg), sc.get("group")), [])  # None — усім (ще немає користувачів)
    summary["zones"] = run_zones(store, cfg, notifier, now, subs)

    if now - meta.get("fetched", 0) >= sc.get("fetch_every_min", 15) * MIN:
        try:
            new = fetch(sc["source_url"], sc["group"])
        except Exception as e:  # дзеркало недоступне — лишаємо старий графік
            summary["error"] = f"{type(e).__name__}: {e}"
            meta["fetched"] = now  # не довбати джерело кожні 2 хв
            store.put_meta("schedule", meta)
            return summary
        old_days = meta.get("days") or {}
        if sc.get("notify_changes", True) and old_days:
            for day, hours in sorted(new["days"].items()):
                if int(day) < int(new.get("today") or day):
                    continue
                if old_days.get(day) != hours:
                    off = intervals(day, hours)
                    head = "📅 Новий графік" if day not in old_days else "📅 Графік змінено"
                    if home_to != []:
                        notifier.send(f"{head} на {day_label(day, new.get('today'), cfg)} (черга {sc['group'].replace('GPV', '')}): "
                                      f"світла не буде {describe(off, cfg)}", ts=now, to=home_to, kind="schedule")
        meta.update(new, fetched=now)
        summary["fetched"] = True
        summary["archived"] = archive(store, sc, meta, new, now)

    # нагадування за remind_before_min до відключення за графіком (один раз на кожен проміжок)
    before = sc.get("remind_before_min", 30) * MIN
    reminded = {k: v for k, v in (meta.get("reminded") or {}).items() if int(k) > now - 2 * 86_400_000}
    for day, hours in (meta.get("days") or {}).items() if before > 0 else ():
        for a, b in intervals(day, hours):
            if 0 < a - now <= before and str(a) not in reminded:
                notifier.send(f"⏰ За графіком о {fmt_time(a, cfg)} вимкнуть світло (до "
                              f"{'24:00' if fmt_time(b, cfg) == '00:00' else fmt_time(b, cfg)})", ts=now, kind="schedule")
                reminded[str(a)] = now
    meta["reminded"] = reminded
    store.put_meta("schedule", meta)
    return summary


def next_off_text(store, cfg, now):
    """Для «💡 Світло є»: наступне відключення за графіком дому. Графіка немає — None (нічого не пишемо)."""
    sc = cfg.get("outage_schedule")
    meta = (store.get_meta("schedule") or {}) if sc else {}
    days = {d: h for d, h in (meta.get("days") or {}).items() if int(d) * 1000 + 86_400_000 > now}
    if not days:
        return None
    ahead = sorted(iv for d, h in days.items() for iv in intervals(d, h) if iv[0] > now)
    if not ahead:
        last = day_label(max(days, key=int), meta.get("today"), cfg).split(" ")[0]
        return "📅 За графіком відключень " + ("до кінця завтрашнього дня" if last == "завтра" else "до кінця дня") + " немає"
    a, b = ahead[0]
    day = next(d for d in days if int(d) * 1000 <= a < int(d) * 1000 + 86_400_000)
    end = "24:00" if fmt_time(b, cfg) == "00:00" else fmt_time(b, cfg)
    return f"📅 Наступне відключення за графіком: {day_label(day, meta.get('today'), cfg)} {fmt_time(a, cfg)}–{end}"


EARLY = 15 * MIN   # світло часто вимикають на кілька хвилин раніше/пізніше за графік
LATE_AFTER = 30 * MIN  # стільки чекаємо після кінця відключення за графіком, перш ніж радити ДТЕК


def _home_ivs(store, cfg, now):
    """Інтервали графіка дому навколо now: [(a, b, "off"|"maybe")]; None — графіка немає."""
    sc = cfg.get("outage_schedule")
    days = ((store.get_meta("schedule") or {}).get("days") or {}) if sc else {}
    days = {d: h for d, h in days.items() if int(d) * 1000 + 2 * 86_400_000 > now}
    if not days:
        return None
    return sorted([(a, b, "off") for d, h in days.items() for a, b in intervals(d, h)] +
                  [(a, b, "maybe") for d, h in days.items() for a, b in intervals(d, h, "maybe")])


def _hh(ms, cfg):
    return "24:00" if fmt_time(ms, cfg) == "00:00" else fmt_time(ms, cfg)


def dtek_hint(cfg):
    bot = (cfg.get("dtek_report") or {}).get("bot")
    return f"повідомте ДТЕК: t.me/{bot}" if bot else "повідомте ДТЕК"


def expected_off(store, cfg, now):
    """Чи зараз відключення за графіком (з допуском EARLY на ранній початок): True / False / None (графіка немає)."""
    ivs = _home_ivs(store, cfg, now)
    if ivs is None:
        return None
    return any(a - EARLY <= now < b for a, b, _ in ivs)


def outage_context(store, cfg, start):
    """Для «🔌 Немає світла»: чи це за графіком і чи треба звертатись до ДТЕК. Графіка немає — None."""
    ivs = _home_ivs(store, cfg, start)
    if ivs is None:
        return None
    hit = next(((a, b, k) for a, b, k in ivs if a - EARLY <= start < b), None)
    if hit and hit[2] == "off":
        a, b, _ = hit
        # 19.11: про ДТЕК пишемо лише тоді, коли справді треба звертатись (аварійне / не повернули вчасно)
        # 21.11: з історії — коли насправді вмикають для цієї черги (медіана зсуву, accuracy.py)
        import accuracy
        usual = accuracy.usual_end(store.get_meta("sched_acc"), b, cfg)
        tail = f" (зазвичай вмикають о {usual})" if usual else ""
        return f"📅 За графіком: відключення {fmt_time(a, cfg)}–{_hh(b, cfg)}, світло мають повернути о {_hh(b, cfg)}{tail}."
    if hit:
        a, b, _ = hit
        return (f"🟡 За графіком {fmt_time(a, cfg)}–{_hh(b, cfg)} — можливе відключення, ймовірно, це воно. "
                f"Якщо світла не буде після {_hh(b, cfg)} — бот нагадає.")
    nxt = next(((a, b) for a, b, k in ivs if k == "off" and a > start), None)
    if nxt:
        same = fmt_time(nxt[0], cfg, with_date=True)[:5] == fmt_time(start, cfg, with_date=True)[:5]
        when = "сьогодні" if same else fmt_time(nxt[0], cfg, with_date=True)[:5]
        plan = f"наступне за графіком — {when} {fmt_time(nxt[0], cfg)}–{_hh(nxt[1], cfg)}"
    else:
        plan = "до кінця відомого графіка відключень немає"
    return (f"⚠️ За графіком світло зараз має бути ({plan}) — схоже на аварійне відключення.\n"
            f"Перевірте світло в під'їзді / у сусідів: якщо там теж немає — {dtek_hint(cfg)}")


def outage_late(store, cfg, start, now):
    """Світла досі немає через LATE_AFTER після кінця відключення за графіком → текст нагадування (або None).
    Повертає (ключ інтервалу, текст), щоб нагадати один раз."""
    ivs = _home_ivs(store, cfg, start)
    if not ivs:
        return None
    hit = next(((a, b, k) for a, b, k in ivs if a - EARLY <= start < b), None)
    if not hit:
        return None  # аварійне — про ДТЕК уже сказали на початку
    # кілька відключень поспіль (12:00–16:00, 16:00–20:00) — кінець = кінець останнього, що йде впритул
    end = hit[1]
    for a, b, k in ivs:
        if a <= end < b or a == end:
            end = max(end, b)
    if now < end + LATE_AFTER:
        return None
    return str(end), (f"⏰ За графіком світло мало бути з {_hh(end, cfg)}, а його досі немає "
                      f"(вимкнули о {fmt_time(start, cfg)}).\n"
                      f"Можливо, аварія — {dtek_hint(cfg)}")


def for_web(store, cfg, now, zone=None, group=None):
    """Графік на сьогодні й завтра: дому (за замовчуванням) або зони/черги з адреси користувача бота."""
    sc = cfg.get("outage_schedule")
    if not sc:
        return None
    if zone and (zone, group) != (home_zone(cfg), sc.get("group")):
        zm = store.get_meta(f"zone#{zone}") or {}
        meta = {**zm, "days": {d: g[group] for d, g in (zm.get("days") or {}).items() if group in g}}
        sc = {"group": group}
    else:
        meta = store.get_meta("schedule") or {}
    if not meta.get("days"):
        return None
    days = []
    for day, hours in sorted(meta["days"].items()):
        if int(day) * 1000 + 86_400_000 < now:
            continue
        days.append({"day": int(day) * 1000, "label": day_label(day, meta.get("today"), cfg), "slots": slots(hours),
                     "off": intervals(day, hours), "maybe": intervals(day, hours, "maybe")})
    upcoming = sorted(iv for d in days for iv in d["off"] if iv[1] > now)
    return {"address": sc.get("address"), "provider": sc.get("provider"), "group": sc["group"].replace("GPV", ""),
            "update": meta.get("update"), "fetched": meta.get("fetched"), "days": days,
            "next_off": upcoming[0] if upcoming else None}
