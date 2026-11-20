"""Планувальник квоти Tuya API: окремий інтервал для кожного приладу × параметра, сума — в межах ліміту.

Запуск локально:  python app/budget.py   (бере список приладів з out/devices_raw.json або з локального стану)
"""
import math
import re

MONTH_MIN = 30 * 24 * 60


def matches(rule, dev):
    m = rule.get("match", {})
    return all(dev.get(k) == v for k, v in m.items())


def rule_codes(rule):
    codes = rule.get("codes") or [rule.get("code", "*")]
    return codes


def device_rules(cfg, dev):
    """{code: {"source", "every_min"}} для приладу; пізніше правило перекриває раннє, конкретний код — '*'."""
    out, star = {}, None
    for r in cfg["polling"].get("rules", []):
        if not matches(r, dev):
            continue
        spec = {"source": r.get("source", "snapshot"), "every_min": r["every_min"]}
        if r.get("code_re"):  # шаблон коду: усі параметри приладу, що підходять
            for d in dev.get("dps", []):
                if re.search(r["code_re"], d["code"], re.I) and not re.search(r"compens", d["code"], re.I):
                    out[d["code"]] = spec
            continue
        for c in rule_codes(r):
            spec = {"source": r.get("source", "snapshot"), "every_min": r["every_min"]}
            if c == "*":
                star = spec
            else:
                out[c] = spec
    if star:
        for d in dev.get("dps", []):
            out.setdefault(d["code"], star)
    return out


def log_schedule(cfg, devices):
    """{device_id: {code: every_min}} — параметри з окремим запитом: source=log (журнал) або shadow
    (тіньові властивості — те, чого прилад не віддає у звичайному стані, напр. вологість датчика руху)."""
    sched = {}
    for dev in devices:
        rules = {c: s["every_min"] for c, s in device_rules(cfg, dev).items() if s["source"] in ("log", "shadow")}
        if rules:
            sched[dev["id"]] = rules
    return sched


def _count_runs(snapshot_every, intervals):
    """Скільки запитів на місяць дає прилад з такими інтервалами параметрів, якщо Lambda запускається
    раз на snapshot_every хв, а параметри, що настали одночасно, йдуть одним запитом."""
    # 19.11: замість покрокової симуляції місяця (21 600 кроків × 30 ітерацій пошуку × прилади — 1,5 с процесора
    # на кожне оновлення дашборда) — те саме аналітично. Кожен параметр спрацьовує на старті, далі кожні
    # ceil(i / крок) кроків; запуски, що збіглися, — один запит. Отже запитів = розмір об'єднання цих прогресій.
    steps = math.ceil(MONTH_MIN / snapshot_every)
    fired = set()
    for i in intervals:
        fired.update(range(0, steps, max(1, math.ceil(i / snapshot_every))))
    return len(fired)


def plan(cfg, devices):
    """Повертає план з урахуванням ліміту: фактори розтягування, ефективні інтервали, кошторис."""
    p = cfg["polling"]
    budget = p["monthly_api_quota"] * p.get("quota_safety_pct", 90) / 100
    base = p["snapshot_every_min"]
    refresh_days = cfg.get("model_refresh_days", 7)
    # токен раз на ~2 год + модель і специфікація кожного приладу раз на refresh_days
    # + кімнати Smart Life (список кімнат і прилади кожної) раз на tuya_rooms_refresh_hours
    rooms_calls = 30 * 24 / cfg.get("tuya_rooms_refresh_hours", 24) * 8
    overhead = int(30 * 12 + 2 * len(devices) * math.ceil(30 / refresh_days) + rooms_calls)
    sched = log_schedule(cfg, devices)

    def cost(snap_factor, log_factor):
        """log_factor=None — детальна історія вимкнена."""
        snap_every = base * snap_factor
        snap = MONTH_MIN / snap_every
        rows = []
        if log_factor is not None:
            for dev_id, codes in sched.items():
                eff = {c: max(snap_every, m * log_factor) for c, m in codes.items()}
                rows.append((dev_id, eff, _count_runs(base, sorted(set(eff.values())))))
        return snap + sum(r[2] for r in rows) + overhead, snap, rows

    snap_factor, log_factor = 1.0, 1.0
    if cost(1, 1)[0] > budget:
        # Спершу розтягуємо детальні правила; знімок — лише якщо без детальних усе одно не влазить
        hi = 1000.0
        if cost(1, hi)[0] <= budget:
            lo = 1.0
            for _ in range(30):
                mid = (lo + hi) / 2
                lo, hi = (lo, mid) if cost(1, mid)[0] <= budget else (mid, hi)
            log_factor = hi
        else:
            log_factor = None
            snap_factor = max(1.0, (MONTH_MIN / base) / max(1.0, budget - overhead))
    total, snap, rows = cost(snap_factor, log_factor)

    return {
        "budget": int(budget),
        "quota": p["monthly_api_quota"],
        "total": int(total),
        "overhead": overhead,
        "snapshot_every_min": base * snap_factor,
        "snapshot_calls": int(snap),
        "log_factor": log_factor or 0,
        "log_disabled": log_factor is None,
        "devices": [{"id": d, "intervals": e, "calls": c} for d, e, c in rows],
    }


if __name__ == "__main__":
    import json
    import os
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from devices import load_config, normalize, parse_model

    cfg = load_config()
    raw = json.load(open(os.path.join(here, "..", "out", "devices_raw.json"), encoding="utf-8"))
    devs = []
    for d in raw["devices"]:
        info = d["info"]["result"]
        model = d["model"].get("result", {}).get("model") if d["model"].get("success") else None
        devs.append(normalize(info, parse_model(model) if model else {}, cfg))
    names = {d["id"]: d["name"] for d in devs}
    pl = plan(cfg, devs)
    print(f"Ліміт {pl['quota']}/міс, бюджет {pl['budget']} ({cfg['polling'].get('quota_safety_pct', 90)} %)")
    print(f"Знімок усіх приладів: раз на {pl['snapshot_every_min']:g} хв → {pl['snapshot_calls']} запитів")
    for row in pl["devices"]:
        iv = ", ".join(f"{c} {m:g} хв" for c, m in row["intervals"].items())
        print(f"Журнал {names.get(row['id'], row['id'])}: {iv} → {row['calls']} запитів")
    print(f"Службові (токен, моделі): {pl['overhead']}")
    print(f"РАЗОМ: {pl['total']} / {pl['budget']}"
          + ("" if pl["log_factor"] in (0, 1) else f"  — детальні інтервали розтягнуто ×{pl['log_factor']:.2f}")
          + ("  — детальна історія ВИМКНЕНА, знімок розтягнуто" if pl["log_disabled"] else ""))
