"""Наскільки точний графік ДТЕК для черги дому: реальні відключення (події outage_end) проти архіву графіка.

Раз на добу (після півночі) рахуємо за останні window_days днів і кладемо в meta «sched_acc»:
  matched — відключень, що збіглися з вікном графіка; start/end — медіана зсуву початку й кінця, хв
  (плюс — пізніше за графік); emergency — відключень поза графіком; skipped — вікон графіка, коли світло було.
Використовують: дашборд (рядок під графіком) і бот («світло мають повернути о 18:30, зазвичай о 18:42»)."""
import statistics
import time

from devices import fmt_time

MIN = 60_000
DAY = 86_400_000
MATCH_SLACK = 60 * MIN   # відключення належить вікну графіка, якщо перетинається з ним (±60 хв)
MIN_OUTAGE = 10 * MIN    # коротші — збої, не відключення


def _merge(ivs):
    out = []
    for a, b in sorted(ivs):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def planned(docs):
    """Вікна «без світла» з архіву графіка (остаточна версія дня; суміжні через північ — злиті)."""
    return _merge([list(iv) for doc in docs for iv in (doc.get("off") or [])])


def compute(actual, plan, unknown, now, monitor_since=None):
    """actual — [[start, end]] реальних відключень; plan — [[a, b]] вікон графіка; unknown — проміжки без інтернету."""
    actual = [iv for iv in actual if iv[1] - iv[0] >= MIN_OUTAGE]
    starts, ends, used = [], [], set()
    emergency = 0
    for s, e in actual:
        best = None
        for i, (a, b) in enumerate(plan):
            if s < b + MATCH_SLACK and e > a - MATCH_SLACK:
                ov = min(e, b) - max(s, a)
                if best is None or ov > best[0]:
                    best = (ov, i)
        if best is None:
            emergency += 1
            continue
        a, b = plan[best[1]]
        used.add(best[1])
        starts.append((s - a) / MIN)
        ends.append((e - b) / MIN)
    skipped = 0
    for i, (a, b) in enumerate(plan):
        if i in used or b > now or (monitor_since and a < monitor_since):
            continue
        if any(u[0] < b and u[1] > a for u in unknown):  # не було інтернету — факт невідомий
            continue
        if any(s < b + MATCH_SLACK and e > a - MATCH_SLACK for s, e in actual):
            continue
        skipped += 1
    return {"matched": len(starts), "emergency": emergency, "skipped": skipped,
            "start": round(statistics.median(starts)) if starts else None,
            "end": round(statistics.median(ends)) if ends else None}


def run(store, cfg, now, force=False):
    """Раз на добу перераховує meta sched_acc за останні window_days днів."""
    ac = cfg.get("schedule_accuracy") or {}
    days = ac.get("window_days", 30)
    old = store.get_meta("sched_acc") or {}
    if not force and now - old.get("ts", 0) < 20 * 3_600_000:
        return None
    lo = now - days * DAY
    docs = store.list_archive_range("schedule", str(lo // 1000 - 86400), str(now // 1000))
    events = store.get_events_range(lo, now)
    actual = [[e["start"], e["end"]] for e in events if e.get("kind") == "outage_end" and e.get("start")]
    unknown = [[e["start"], e["end"]] for e in events if e.get("kind") == "internet_up" and e.get("start")]
    plan = planned(docs)
    res = compute(actual, plan, unknown, now, (store.get_meta("first_poll") or {}).get("ts"))
    since = max(lo, (store.get_meta("first_poll") or {}).get("ts") or lo)
    res.update(ts=now, days=max(1, round((now - since) / DAY)), since=since)
    store.put_meta("sched_acc", res)
    return res


def _shift(m, verb):
    if m is None:
        return None
    if abs(m) < 3:
        return f"{verb} вчасно"
    return f"{verb} на {abs(m)} хв {'пізніше' if m > 0 else 'раніше'}"


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def summary_text(acc):
    """Один рядок для дашборда/бота. None — даних замало."""
    if not acc or acc.get("matched", 0) + acc.get("emergency", 0) < 3:
        return None
    parts = [p for p in (_shift(acc.get("start"), "вимикають"), _shift(acc.get("end"), "вмикають")) if p]
    total = acc["matched"] + acc["emergency"]
    tail = [f"{acc['emergency']} з {total} поза графіком"] if acc["emergency"] else [f"усі {total} — за графіком"]
    if acc.get("skipped"):
        tail.append(plural(acc["skipped"], "раз", "рази", "разів") + " за графіком не вимикали")
    return f"За {plural(acc.get('days', 30), 'день', 'дні', 'днів')}: зазвичай " + ", ".join(parts) + "; " + ", ".join(tail)


def short_text(acc):
    """«зазвичай вимикають на 7 хв пізніше, вмикають на 5 хв пізніше» — для ранкового підсумку."""
    if not acc or acc.get("matched", 0) < 3:
        return None
    parts = [p for p in (_shift(acc.get("start"), "вимикають"), _shift(acc.get("end"), "вмикають")) if p]
    return "ℹ️ Зазвичай " + ", ".join(parts) + " за графік." if parts else None


def usual_end(acc, b, cfg):
    """«зазвичай о 18:42» для кінця відключення за графіком b, якщо зсув помітний і даних досить."""
    if not acc or acc.get("matched", 0) < (cfg.get("schedule_accuracy") or {}).get("min_matched", 5):
        return None
    m = acc.get("end")
    if m is None or abs(m) < 5:
        return None
    return fmt_time(b + m * MIN, cfg)


if __name__ == "__main__":  # ручний перерахунок: python accuracy.py (потрібні TABLE_NAME і AWS)
    import json
    import os
    from devices import load_config
    from store import DynamoStore
    cfg = load_config()
    r = run(DynamoStore(os.environ["TABLE_NAME"]), cfg, int(time.time() * 1000), force=True)
    print(json.dumps(r, ensure_ascii=False))
    print(summary_text(r))
