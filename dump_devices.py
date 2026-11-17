"""Вивантажує все, що Tuya Cloud знає про прилади акаунта.

Запуск:  python dump_devices.py [днів_журналу]
Результат:
  out/devices_raw.json — сирі відповіді API по кожному приладу
  out/summary.md       — читабельний звіт: прилади, усі DP з поточними значеннями та одиницями, події online/offline
"""
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
from tuya_cloud import TuyaCloud, load_env  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

LOG_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
# Типи подій журналу: 1 online, 2 offline, 7 звіт даних (зміна DP)
LOG_TYPES = "1,2,7"

# Поправки до моделі приладу, коли прошивка звітує інакше, ніж заявляє модель.
# (категорія, код DP) → поля typeSpec, що перекривають модель.
# cz/cur_voltage: модель каже scale 0, а розетка шле В×10 (2380 = 238.0 В); max 2500 = стеля 250.0 В.
MODEL_OVERRIDES = {
    ("cz", "cur_voltage"): {"scale": 1},
}


def ok(r):
    return isinstance(r, dict) and r.get("success")


def err(r):
    return f"code={r.get('code', r.get('http_status'))} {r.get('msg')}"


def fmt_ts(ms):
    if not ms:
        return ""
    if ms > 10**12:
        ms /= 1000
    return datetime.fromtimestamp(ms).strftime("%Y-%m-%d %H:%M:%S")


def parse_values(v):
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}


def human_value(value, spec_values, dp_type):
    """Значення з урахуванням scale і одиниці, як його показує Smart Life."""
    if dp_type and dp_type.lower() in ("integer", "value") and isinstance(value, (int, float)):
        scale = spec_values.get("scale", 0) or 0
        v = value / (10 ** scale) if scale else value
        unit = spec_values.get("unit", "")
        return f"{v:g} {unit}".strip()
    return json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value


def spec_range(dp_type, sv):
    if not sv:
        return ""
    if "range" in sv:
        return "/".join(map(str, sv["range"]))
    if "min" in sv or "max" in sv:
        scale = sv.get("scale", 0) or 0
        k = 10 ** scale
        return f"{sv.get('min', 0)/k:g}..{sv.get('max', 0)/k:g} {sv.get('unit', '')}".strip()
    if "maxlen" in sv:
        return f"maxlen {sv['maxlen']}"
    return ""


def collect_device_ids(api, seed_id):
    ids, uid = {}, None
    r = api.get(f"/v1.0/devices/{seed_id}")
    if ok(r):
        uid = r["result"].get("uid")
    else:
        print(f"! Не вдалося прочитати seed-прилад {seed_id}: {err(r)}")

    if uid:
        r = api.get(f"/v1.0/users/{uid}/devices")
        if ok(r):
            for d in r["result"]:
                ids[d["id"]] = d.get("name")
        else:
            print(f"! /v1.0/users/{{uid}}/devices: {err(r)}")

    # Альтернативний список — прилади проєкту (на випадок, якщо частину не повернув список акаунта)
    last_id = ""
    while True:
        params = {"page_size": 20}
        if last_id:
            params["last_id"] = last_id
        r = api.get("/v2.0/cloud/thing/device", params)
        if not ok(r) or not r.get("result"):
            break
        for d in r["result"]:
            ids.setdefault(d["id"], d.get("name"))
        if len(r["result"]) < 20:
            break
        last_id = r["result"][-1]["id"]
    return uid, ids


def dump_device(api, dev_id, start_ms, end_ms):
    d = {"id": dev_id}
    d["info"] = api.get(f"/v1.0/devices/{dev_id}")
    d["specification"] = api.get(f"/v1.0/iot-03/devices/{dev_id}/specification")
    d["shadow"] = api.get(f"/v2.0/cloud/thing/{dev_id}/shadow/properties")
    d["model"] = api.get(f"/v2.0/cloud/thing/{dev_id}/model")
    d["sub_devices"] = api.get(f"/v1.0/devices/{dev_id}/sub-devices")
    d["logs"] = api.get(
        f"/v2.0/cloud/thing/{dev_id}/logs",
        {"start_time": start_ms, "end_time": end_ms, "type": LOG_TYPES, "query_type": 1, "size": 100},
    )
    # Окремо online/offline: у спільному журналі їх витісняють часті звіти DP (ліміт 100 подій)
    d["logs_conn"] = api.get(
        f"/v2.0/cloud/thing/{dev_id}/logs",
        {"start_time": start_ms, "end_time": end_ms, "type": "1,2", "query_type": 1, "size": 100},
    )
    if not ok(d["logs"]):
        d["logs_v1"] = api.get(
            f"/v1.0/devices/{dev_id}/logs",
            {"start_time": start_ms, "end_time": end_ms, "type": LOG_TYPES, "size": 100},
        )
    return d


def log_events(d, keys=("logs", "logs_v1")):
    for key in keys:
        r = d.get(key)
        if ok(r):
            res = r["result"]
            return res.get("logs") or res.get("list") or []
    return None


def summarize(d):
    info = d["info"]["result"] if ok(d["info"]) else {}
    name = info.get("name", d["id"])
    lines = [f"## {name}", ""]
    lines.append(f"- ID: `{d['id']}`")
    lines.append(f"- Продукт: {info.get('product_name', '')} (категорія `{info.get('category', '')}`)")
    lines.append(f"- Online: **{info.get('online')}**  ·  IP: {info.get('ip', '')}")
    lines.append(f"- Остання оновлення: {fmt_ts(info.get('update_time'))}  ·  Активовано: {fmt_ts(info.get('active_time'))}")
    if info.get("sub"):
        lines.append(f"- Підприлад шлюзу (gateway_id `{info.get('gateway_id') or ''}`)")
    if ok(d["sub_devices"]) and d["sub_devices"].get("result"):
        subs = ", ".join(s.get("name", s.get("id")) for s in d["sub_devices"]["result"])
        lines.append(f"- **Шлюз**, підприлади: {subs}")
    lines.append("")

    # Модель приладу — з тими самими кодами DP, що й shadow (специфікація часто дає стандартні коди va_*)
    spec = {}
    if ok(d["model"]) and d["model"]["result"].get("model"):
        model = json.loads(d["model"]["result"]["model"])
        for svc in model.get("services", []):
            for p in svc.get("properties", []):
                ts = dict(p.get("typeSpec", {}))
                ts.update(MODEL_OVERRIDES.get((info.get("category"), p["code"]), {}))
                mode = p.get("accessMode", "")
                spec[p["code"]] = {"type": ts.get("type"), "values": ts, "name": p.get("name", ""),
                                   "rw": set(("r" if "r" in mode else "") + ("w" if "w" in mode else ""))}

    props = []
    if ok(d["shadow"]):
        props = d["shadow"]["result"].get("properties", [])
    elif info.get("status"):
        props = [{"code": s["code"], "value": s["value"]} for s in info["status"]]

    if props:
        lines.append("| DP | Код | Назва | Тип | Значення | Сире | Діапазон | Доступ | Час |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for p in sorted(props, key=lambda x: x.get("dp_id") or 0):
            sp = spec.get(p["code"], {})
            dp_type = p.get("type") or sp.get("type") or ""
            sv = sp.get("values", {})
            lines.append(
                f"| {p.get('dp_id', '')} | `{p['code']}` | {p.get('custom_name') or sp.get('name', '')} | {dp_type} "
                f"| **{human_value(p.get('value'), sv, dp_type)}** | {json.dumps(p.get('value'), ensure_ascii=False)} "
                f"| {spec_range(dp_type, sv)} | {''.join(sorted(sp.get('rw', [])))} | {fmt_ts(p.get('time'))} |"
            )
    else:
        lines.append(f"_DP не прочитано: shadow {err(d['shadow'])}_")
    lines.append("")

    events = log_events(d)
    conn = log_events(d, ("logs_conn",))
    if events is None:
        lines.append(f"_Журнал недоступний: {err(d['logs'])}_")
    else:
        lines.append(f"Журнал: останні {len(events)} подій (ліміт 100)")
    if conn is not None:
        lines.append(f"Online/offline за {LOG_DAYS} дн.: {len(conn)}")
        for e in conn[:20]:
            kind = "online" if str(e.get("event_id")) == "1" else "offline"
            lines.append(f"- {fmt_ts(e.get('event_time'))} — {kind}")
    lines.append("")
    return lines


def main():
    load_env()
    api = TuyaCloud(os.environ["TUYA_ACCESS_ID"], os.environ["TUYA_ACCESS_SECRET"], os.environ["TUYA_ENDPOINT"])
    api.ensure_token()
    print("Токен отримано.")

    uid, ids = collect_device_ids(api, os.environ["TUYA_SEED_DEVICE_ID"])
    print(f"UID акаунта: {uid}; приладів знайдено: {len(ids)}")

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - LOG_DAYS * 86400 * 1000
    devices = []
    for i, (dev_id, name) in enumerate(ids.items(), 1):
        print(f"[{i}/{len(ids)}] {name} ({dev_id})")
        devices.append(dump_device(api, dev_id, start_ms, end_ms))

    os.makedirs("out", exist_ok=True)
    with open("out/devices_raw.json", "w", encoding="utf-8") as f:
        json.dump({"uid": uid, "generated": datetime.now().isoformat(), "devices": devices}, f, ensure_ascii=False, indent=2)

    md = [f"# Прилади Tuya — {datetime.now():%Y-%m-%d %H:%M}", "", f"Приладів: {len(devices)}", ""]
    for d in devices:
        md += summarize(d)
    with open("out/summary.md", "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    # Які API-виклики не пройшли — щоб було видно, якого сервісу бракує
    failures = {}
    for d in devices:
        for k in ("info", "specification", "shadow", "model", "logs"):
            if not ok(d.get(k)):
                failures.setdefault(k, err(d[k]))
    print("\nГотово: out/summary.md, out/devices_raw.json")
    for k, v in failures.items():
        print(f"  ! {k}: {v}")


if __name__ == "__main__":
    main()
