"""Рейтинг міст за тривогами: одноразове наповнення лічильників за останні 365 днів.

Запуск (локально, один раз):
  python tools/alert_rank_build.py <official_data_uk.csv> <regions.json> [--upload]

Джерела:
  - відкритий набір даних github.com/Vadimkin/ukrainian-air-raid-sirens-dataset (official_data_uk.csv, MIT) —
    усі офіційні тривоги з 2022 р. (область / район / громада), час UTC;
  - siren.pp.ua /api/v3/alerts/regionHistory — останні 20 тривог регіону: закриває прогалину між кінцем набору
    даних і сьогоднішнім днем (для спокійних міст — повністю; де тривог більше — місто позначається «неповні дані»).
Зберігаємо НЕ історію, а лише лічильники по днях для кожного міста: {дата: [тривог, хвилин]} (ARCH#arank / <місто>).
Далі лічильники веде опитувач (app/arank.py) зі списку активних тривог кожні 2 хв.

Місто під тривогою, якщо тривога на рівні його громади, його району або його області (як у боті)."""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import arank  # noqa: E402

# райони, перейменовані після 2024 р.: довідник тривог — нова назва, набір даних — стара
RAION_OLD = {"Шептицький район": ["Червоноградський район"], "Самарівський район": ["Новомосковський район"],
             "Звягельський район": ["Новоград-Волинський район"]}


def local_day(ms, tz):
    return datetime.fromtimestamp(ms / 1000, tz).date().isoformat()


def split_days(a, b, tz):
    """[a, b) мс → {дата: мс у цій добі}."""
    out = {}
    while a < b:
        d = datetime.fromtimestamp(a / 1000, tz)
        nxt = int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp() * 1000) + 86_400_000
        # перехід часу: перерахувати північ наступної доби в місцевому часі
        nd = datetime.fromtimestamp(nxt / 1000, tz)
        nxt = int(datetime(nd.year, nd.month, nd.day, tzinfo=tz).timestamp() * 1000)
        if nxt <= a:
            nxt = a + 86_400_000
        e = min(b, nxt)
        out[d.date().isoformat()] = out.get(d.date().isoformat(), 0) + (e - a)
        a = e
    return out


def merge(iv):
    iv = sorted(iv)
    out = []
    for a, b in iv:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def norm(s):
    return (s or "").lower().replace("’", "'").replace("ʼ", "'").replace("`", "'").strip()


def ms(iso, default=None):
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000) if d.year >= 2000 else default   # 0001-01-01 — тривога ще триває
    except (ValueError, OSError, AttributeError):
        return default


def fetch_history(rid):
    req = urllib.request.Request(f"https://siren.pp.ua/api/v3/alerts/regionHistory?regionId={rid}",
                                 headers={"User-Agent": "TuyaHome", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode())
    return (d[0] if isinstance(d, list) and d else {}).get("alarms") or []


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    csv_path, regions_path = sys.argv[1], sys.argv[2]
    from devices import load_config, local_tz
    tz = local_tz(load_config())  # Europe/Kyiv (із запасним зсувом, якщо в системі немає бази часових поясів)
    now = int(time.time() * 1000)
    today = datetime.fromtimestamp(now / 1000, tz).date()
    win_from = (today - timedelta(days=364)).isoformat()
    cat = arank.catalog_from(json.load(open(regions_path, encoding="utf-8")))
    cities = arank.resolve_cities(cat)
    missing = [c["name"] for c in cities if not c.get("ids")]
    if missing:
        print("НЕ ЗНАЙДЕНО в довіднику:", missing)
    cities = [c for c in cities if c.get("ids")]

    # 1) набір даних: інтервали для кожного міста (область / район / громада)
    by_oblast, by_raion, by_hrom = {}, {}, {}
    for c in cities:
        by_oblast.setdefault(norm(c["oblast"]), []).append(c["name"])
        if c.get("raion"):
            by_raion.setdefault((norm(c["oblast"]), norm(c["raion"])), []).append(c["name"])
            for old in RAION_OLD.get(c["raion"], []):   # перейменовані райони — у наборі даних стара назва
                by_raion.setdefault((norm(c["oblast"]), norm(old)), []).append(c["name"])
        if c.get("hromada"):
            by_hrom.setdefault(norm(c["hromada"]), []).append(c["name"])
    iv = {c["name"]: [] for c in cities}
    until = 0
    start_cut = ms(win_from + "T00:00:00+02:00") - 86_400_000
    for r in csv.DictReader(open(csv_path, encoding="utf-8")):
        a, b = ms(r["started_at"]), ms(r["finished_at"])
        until = max(until, b)
        if b < start_cut:
            continue
        lvl, ob = r["level"], norm(r["oblast"])
        names = []
        if lvl == "oblast":
            names = by_oblast.get(ob, [])
        elif lvl == "raion":
            names = by_raion.get((ob, norm(r["raion"])), [])
        elif lvl == "hromada":
            names = by_hrom.get(norm(r["hromada"]), [])
        for n in names:
            iv[n].append([a, b])
    print(f"Набір даних — до {datetime.fromtimestamp(until / 1000, tz):%d.%m.%Y %H:%M}")

    # 2) прогалина: останні 20 тривог кожного регіону міста (громада, район, область)
    hist, cover = {}, {}
    all_ids = sorted({i for c in cities for i in c["ids"]})
    for k, rid in enumerate(all_ids):
        try:
            al = fetch_history(rid)
        except Exception as e:
            print("історія", rid, e)
            al = []
        hist[rid] = [[ms(x["startDate"]), ms(x.get("endDate") or "", now)] for x in al if ms(x.get("startDate") or "")]
        # покрито, якщо найстаріша з 20 почалась раніше за кінець набору даних (або тривог менше 20 — це всі)
        cover[rid] = len(al) < 20 or (hist[rid] and min(a for a, _ in hist[rid]) <= until)
        if k % 20 == 0:
            print(f"  історія {k + 1}/{len(all_ids)}")
        time.sleep(0.15)

    docs = []
    for c in cities:
        allv = iv[c["name"]] + [x for rid in c["ids"] for x in hist.get(rid, []) if x[1] > until]
        merged = merge(allv)  # однакова тривога з набору даних і з історії перекривається — злиття прибирає подвійний облік
        days = {}
        for a, b in merged:
            d0 = local_day(a, tz)
            if d0 >= win_from:
                days.setdefault(d0, [0, 0])[0] += 1
            for d, m in split_days(a, min(b, now), tz).items():
                if d >= win_from:
                    days.setdefault(d, [0, 0])[1] += round(m / 60000)
        incomplete = not all(cover.get(rid, False) for rid in c["ids"])
        est = None
        if incomplete:
            # непокрита частина прогалини: від кінця набору даних до найпізнішого «найстарішого з 20» серед регіонів міста;
            # оцінка = середній темп тривог цього ж міста за період набору даних × довжина відрізка (мінус уже пораховане)
            gap_to = max(min(a for a, _ in hist[rid]) for rid in c["ids"] if not cover.get(rid) and hist.get(rid))
            g0, g1 = local_day(until, tz), local_day(gap_to, tz)
            known = {d: v for d, v in days.items() if win_from <= d < g0}
            span_known = max(1, (datetime.fromisoformat(g0) - datetime.fromisoformat(win_from)).days)
            span_gap = max(1, (datetime.fromisoformat(g1) - datetime.fromisoformat(g0)).days + 1)
            in_gap = [v for d, v in days.items() if g0 <= d <= g1]
            n_est = sum(v[0] for v in known.values()) / span_known * span_gap - sum(v[0] for v in in_gap)
            m_est = sum(v[1] for v in known.values()) / span_known * span_gap - sum(v[1] for v in in_gap)
            est = {"from": g0, "to": g1, "n": max(0, round(n_est)), "min": max(0, round(m_est))}
        docs.append({**{k: c[k] for k in ("name", "oblast", "raion", "hromada", "pop", "ids")},
                     "days": dict(sorted(days.items())), "est": est,
                     "seeded": now, "dataset_until": until})
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out", "alert_rank_seed.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(docs, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    summ = arank.summarize(docs, today.isoformat())
    print(f"\nМіст: {len(docs)} · вікно {summ['from']} … {summ['to']}")
    for i, r in enumerate(summ["cities"][:25], 1):
        print(f"{i:2}. {r['name']:<22} {r['hours']:7.1f} год  {r['n']:4} трив.  {r['share']:5.1f}%{f"  (оцінка {r['est_from']}…{r['est_to']})" if r['estimated'] else ''}")
    if "--upload" in sys.argv:
        from store import DynamoStore
        st = DynamoStore("tuyahome")
        for d in docs:
            st.put_archive("arank", d["name"], d)
        st.put_meta("arank", summ)
        print("Записано в базу:", len(docs), "міст + підсумок")


if __name__ == "__main__":
    main()
