"""Регулярні повідомлення: ранковий підсумок (кожному, о 7:00) і щотижневий стан приладів (власнику, пн 10:00).

Обидва викликає опитувач на кожному запуску (poller.run → reports.run); відправлене позначається в meta
«reports», тож кожне повідомлення йде один раз на день/тиждень навіть при кількох запусках у вікні."""
from datetime import datetime

import accuracy
import schedule
import weather
from accuracy import plural
from devices import battery_dps, fmt_duration, fmt_time, is_number, local_tz

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


def _local(now, cfg):
    return datetime.fromtimestamp(now / 1000, local_tz(cfg))


def _day_start(now, cfg):
    return int(_local(now, cfg).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _hh(ms, cfg):
    return "24:00" if fmt_time(ms, cfg) == "00:00" else fmt_time(ms, cfg)


# --- ранковий підсумок ---------------------------------------------------------------------------------------

def light_today(sc, day0, day1, cfg, now=None):
    """Рядки «коли буде світло сьогодні» з графіка (for_web): світло = доба мінус відключення; лише від now."""
    if not sc or not sc.get("days"):
        return ["💡 Графіка відключень на сьогодні ще немає."]
    d = next((x for x in sc["days"] if day0 <= x["day"] < day1), None)
    if not d:
        return ["💡 Графіка відключень на сьогодні ще немає."]
    off = sorted([list(iv) for iv in d["off"]])
    if not off and not d["maybe"]:
        return [f"💡 Світло сьогодні буде весь день — відключень за графіком немає (черга {sc['group']})."]
    on, t = [], day0
    for a, b in off:
        if a > t:
            on.append((t, a))
        t = max(t, b)
    if t < day1:
        on.append((t, day1))
    now = now or day0
    cut = lambda ivs: [(max(a, now), b) for a, b in ivs if b > now]
    fmt = lambda a, b: f"{'зараз' if a == now and a != day0 else fmt_time(a, cfg)}–{_hh(b, cfg)}"
    on, off, maybe = cut(on), cut(off), cut(d["maybe"])
    if not off and not maybe:
        return [f"💡 Світло сьогодні буде до кінця дня — більше відключень за графіком немає (черга {sc['group']})."]
    lines = [f"💡 Світло сьогодні за графіком (черга {sc['group']}):"]
    lines += [f"   🟢 {fmt(a, b)}" for a, b in on if b - a >= 15 * MIN]
    if off:
        lines.append("   🔴 без світла: " + ", ".join(fmt(a, b) for a, b in off))
    if maybe:
        lines.append("   🟡 можливо без світла: " + ", ".join(fmt(a, b) for a, b in maybe))
    return lines


def weather_today(wmeta, sensor, now, cfg):
    """Зараз (датчик і прогноз) + підсумок на сьогодні з денного прогнозу."""
    out = []
    cur = wmeta.get("current") or {}
    if sensor and sensor.get("temp") is not None:
        hum = f", вологість {sensor['hum']:g}%" if is_number(sensor.get("hum")) else ""
        out.append(f"🌡 Зараз надворі: {sensor['temp']:g}°C{hum}")
    if cur.get("temp") is not None:
        ico, desc = weather.describe(cur.get("code"))
        lead = "   за прогнозом" if out else "🌡 Зараз"
        out.append(f"{lead}: {ico} {cur['temp']:.0f}°C (відчувається {cur.get('feels', cur['temp']):.0f}°), {desc}, "
                   f"вітер {cur.get('wind', 0):.0f} м/с")
    day0 = _day_start(now, cfg)
    row = next((r for r in wmeta.get("daily") or [] if day0 <= r[0] < day0 + DAY), None)
    if row:
        ico, desc = weather.describe(row[1])
        mm, pp, wind = row[4] or 0, row[5] or 0, row[6] or 0
        rain = f", опади {mm:.1f} мм ({pp}%)" if mm else f", ймовірність опадів {pp}%" if pp else ""
        out.append(f"🌤 Сьогодні: {ico} {row[2]:.0f}…{row[3]:.0f}°, {desc}{rain}, вітер до {wind:.0f} м/с")
    hours = [r for r in wmeta.get("hourly") or [] if now <= r[0] < day0 + DAY]
    wet = [r for r in hours if (r[4] or 0) >= 50]
    if wet:
        out.append(f"☔ Дощ найімовірніше з {fmt_time(wet[0][0], cfg)} — візьміть парасолю.")
    return out


def digest_text(store, cfg, u, now, states=None):
    name = (u.get("name") or "").split(" ")[0]
    lines = [f"☀️ Доброго ранку{', ' + name if name else ''}! Як спалось?", ""]
    loc = weather.user_location(u, cfg)
    if loc:
        lines += weather_today(weather.point(store, cfg, loc[0], loc[1], now), None, now, cfg)
    elif u.get("access") != "guest":
        lines += weather_today(store.get_meta("weather") or {}, weather.outdoor_sensor(store, cfg, states), now, cfg)
    a = u.get("addr") or {}
    sc = None
    if a.get("zone") and a.get("group"):
        sc = schedule.for_web(store, cfg, now, a["zone"], a["group"])
    elif not a and u.get("access") != "guest":
        sc = schedule.for_web(store, cfg, now)
    if sc is not None or a.get("zone"):
        day0 = _day_start(now, cfg)
        lines += [""] + light_today(sc, day0, _day_start(day0 + 36 * HOUR, cfg), cfg, now)
        home = (schedule.home_zone(cfg), (cfg.get("outage_schedule") or {}).get("group"))
        acc = accuracy.short_text(store.get_meta("sched_acc")) if not a or (a.get("zone"), a.get("group")) == home else None
        if acc and sc and any(x["off"] for x in sc["days"][:1]):
            lines.append("   " + acc)
    lines += ["", "Гарного дня! 🌿"]
    return "\n".join(lines)


def run_digest(store, cfg, notifier, now, users, sent):
    rc = cfg.get("morning_digest") or {}
    if not rc.get("enabled", True):
        return None
    loc = _local(now, cfg)
    if not rc.get("hour", 7) <= loc.hour < rc.get("until_hour", 10):
        return None
    today = loc.strftime("%Y-%m-%d")
    done = sent.setdefault("digest", {})
    n = 0
    states = list(store.get_states().values())
    for uid, u in users.items():
        if u.get("status") != "approved" or not u.get("digest", True) or done.get(uid) == today:
            continue
        try:
            text = digest_text(store, cfg, u, now, states)
        except Exception as e:
            print(f"[digest] {uid}: {type(e).__name__}: {e}")
            continue
        notifier.send(text, to=[uid], kind="digest")
        done[uid] = today
        n += 1
    return n


# --- щотижневий стан приладів --------------------------------------------------------------------------------

def battery_rate(points):
    """%/добу за ряд [(ts, %)] — від останньої заміни батареї (зростання заряду), щонайменше за 2 доби."""
    seg = []
    for p in sorted(points):
        if seg and p[1] > seg[-1][1]:
            seg = []
        seg.append(p)
    if len(seg) < 2 or seg[-1][0] - seg[0][0] < 2 * DAY:
        return None
    return (seg[0][1] - seg[-1][1]) / ((seg[-1][0] - seg[0][0]) / DAY)


def offline_stats(events, now, expected=()):
    """{device_id: (разів, мс офлайн)} з подій offline/online. expected — [[a, b]] проміжки, коли офлайн
    очікуваний (немає світла чи інтернету): випадіння, що почались у них, не рахуємо."""
    out, since = {}, {}
    for e in sorted(events, key=lambda e: e.get("ts", 0)):
        dev = e.get("device_id")
        if e.get("kind") == "offline" and dev and any(a - 10 * MIN <= e["ts"] <= b for a, b in expected):
            continue
        if e.get("kind") == "offline" and dev:
            since[dev] = e["ts"]
            n, ms = out.get(dev, (0, 0))
            out[dev] = (n + 1, ms)
        elif e.get("kind") == "online" and dev and dev in since:
            n, ms = out[dev]
            out[dev] = (n, ms + e["ts"] - since.pop(dev))
    for dev, t in since.items():
        n, ms = out[dev]
        out[dev] = (n, ms + now - t)
    return out


def health_text(store, cfg, now, days=7):
    lo = now - days * DAY
    states = [s for s in store.get_states().values() if not s.get("missing")]
    events = store.get_events_range(lo, now)
    f = lambda ms: fmt_time(ms, cfg, with_date=True)[:5]
    lines = [f"🩺 Стан приладів за тиждень ({f(lo)}–{f(now)})", ""]

    # батареї
    batt = []
    for s in states:
        d = next(iter(battery_dps(s)), None)
        if not d:
            continue
        rate = battery_rate(store.get_history(s["id"], d["code"], lo))
        batt.append((d["value"], s["name"], rate))
    if batt:
        lines.append("🔋 Батареї:")
        for v, name, rate in sorted(batt):
            warn = v < 20 or (rate is not None and rate >= 3)
            r = f", ~{rate:.0f}%/добу" if rate is not None and rate >= 1 else ""
            left = f", вистачить ~{v / rate:.0f} дн." if rate and rate >= 1 and v > 0 else ""
            lines.append(f"   {'⚠️' if warn else '✅'} {name}: {v}%{r}{left}")
        lines.append("")

    # зв'язок
    by_id = {s["id"]: s for s in states}
    expected = [[e["start"], e["end"]] for e in events if e.get("kind") in ("outage_end", "internet_up") and e.get("start")]
    rules = store.get_meta("rules") or {}
    if rules.get("outage"):
        expected.append([rules["outage"]["start"], now])
    if (rules.get("internet") or {}).get("up") is False:
        expected.append([rules["internet"]["since"], now])
    off = offline_stats(events, now, expected)
    bad = sorted(((n, ms, by_id[i]["name"]) for i, (n, ms) in off.items() if i in by_id), reverse=True)
    if bad:
        lines.append("📡 Випадали зі зв'язку (крім відключень світла й інтернету):")
        lines += [f"   • {name}: {plural(n, 'раз', 'рази', 'разів')}, разом {fmt_duration(ms)}" for n, ms, name in bad[:8]]
    else:
        lines.append("📡 Усі прилади весь тиждень були на зв'язку.")
    now_off = [s["name"] for s in states if not s.get("online")]
    if now_off:
        lines.append("   зараз офлайн: " + ", ".join(now_off))
    net = [e for e in events if e.get("kind") == "internet_up"]
    if net:
        lines.append(f"   🌐 інтернет пропадав {plural(len(net), 'раз', 'рази', 'разів')}, разом "
                     f"{fmt_duration(sum(e['end'] - e['start'] for e in net if e.get('start')))}")
    glitch = [e for e in events if e.get("kind") == "power_glitch"]
    cancel = [e for e in events if e.get("kind") == "outage_cancel"]
    if glitch or cancel:
        lines.append(f"   ⚡ збоїв Zigbee, відсіяних як «не відключення»: {len(glitch)}"
                     + (f"; хибних «немає світла»: {len(cancel)}" if cancel else ""))
    lines.append("")

    # світло й графік
    outs = [e for e in events if e.get("kind") == "outage_end"]
    if outs:
        lines.append(f"🔌 Відключень світла: {len(outs)}, разом {fmt_duration(sum(e['end'] - e['start'] for e in outs))}")
    acc = accuracy.summary_text(store.get_meta("sched_acc"))
    if acc:
        lines.append(f"📅 Точність графіка ДТЕК — {acc[0].lower()}{acc[1:]}")
    clim = [e for e in events if e.get("kind") in ("humidity_high", "cold")]
    if clim:
        lines.append(f"🏠 Нагадувань про мікроклімат: {len(clim)}")
    return "\n".join(lines).rstrip()


def run_health(store, cfg, notifier, now, owners, sent):
    rc = cfg.get("weekly_health") or {}
    if not rc.get("enabled", True) or not owners:
        return None
    loc = _local(now, cfg)
    if loc.weekday() != rc.get("weekday", 0) or not rc.get("hour", 10) <= loc.hour < rc.get("hour", 10) + 3:
        return None
    week = loc.strftime("%G-W%V")
    if sent.get("health") == week:
        return None
    notifier.send(health_text(store, cfg, now), to=owners, kind="service")
    sent["health"] = week
    return week


def run(store, cfg, notifier, now):
    import bot
    users = bot.get_users(store)
    sent = store.get_meta("reports") or {}
    out = {"digest": run_digest(store, cfg, notifier, now, users, sent),
           "health": run_health(store, cfg, notifier, now, bot.owners(users), sent)}
    if out["digest"] or out["health"]:
        store.put_meta("reports", sent)
    return out
