"""Нормалізація приладу: значення DP з урахуванням моделі (scale, одиниці) і поправок з config.json."""
import json
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def local_tz(cfg):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(cfg.get("timezone", "Europe/Kyiv"))
    except Exception:  # Windows без tzdata
        return timezone(timedelta(hours=cfg.get("utc_offset_fallback_h", 2)))


def fmt_time(ms, cfg, with_date=False):
    dt = datetime.fromtimestamp(ms / 1000, local_tz(cfg))
    return dt.strftime("%d.%m %H:%M" if with_date else "%H:%M")


def fmt_duration(ms):
    minutes = max(0, int(ms // 60000))
    h, m = divmod(minutes, 60)
    d, h = divmod(h, 24)
    parts = ([f"{d} д"] if d else []) + ([f"{h} год"] if h else []) + [f"{m} хв"]
    return " ".join(parts)


def parse_model(model_str):
    """Модель приладу → {code: spec}. Модель має ті самі коди DP, що й стан приладу."""
    model = json.loads(model_str) if isinstance(model_str, str) else model_str
    specs = {}
    for svc in model.get("services", []):
        for p in svc.get("properties", []):
            ts = p.get("typeSpec", {})
            specs[p["code"]] = {
                "dp_id": p.get("abilityId"),
                "name": p.get("name", ""),
                "access": p.get("accessMode", ""),
                **{k: ts[k] for k in ("type", "scale", "unit", "min", "max", "range") if k in ts},
            }
    return specs


SPEC_TYPES = {"Integer": "value", "Enum": "enum", "Boolean": "bool", "String": "string", "Json": "string"}


def parse_specification(result):
    """Специфікація приладу (стандартні коди Tuya: va_temperature, switch, gas_sensor_status…) → {code: spec}.
    Спільний запит стану повертає частину DP саме в цих кодах, а модель — у власних кодах прошивки."""
    specs = {}
    for group, access in (("status", "ro"), ("functions", "rw")):
        for s in result.get(group, []):
            try:
                vals = json.loads(s.get("values") or "{}")
            except ValueError:
                vals = {}
            e = specs.setdefault(s["code"], {"name": s.get("name", ""), "type": SPEC_TYPES.get(s.get("type"), ""),
                                             **{k: vals[k] for k in ("scale", "unit", "min", "max", "range") if k in vals}})
            e["access"] = "rw" if access == "rw" or e.get("access") == "rw" else "ro"
    return specs


def scaled(raw, spec):
    if spec.get("type") == "value" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        scale = spec.get("scale") or 0
        return round(raw / (10 ** scale), scale) if scale else raw
    return raw


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# Усе, що Tuya віддає про прилад (для вікна приладу). local_key — секрет для локального керування, не зберігаємо.
INFO_FIELDS = ("product_id", "model", "uuid", "node_id", "ip", "time_zone", "active_time", "create_time",
               "update_time", "biz_type", "icon", "owner_id", "lat", "lon")


def normalize(dev, specs, cfg):
    cat = dev.get("category", "")
    dps = []
    for s in dev.get("status", []):
        code = s["code"]
        spec = dict(specs.get(code, {}))
        spec.update(cfg.get("model_overrides", {}).get(f"{cat}/{code}", {}))
        dps.append({
            "code": code,
            "dp_id": spec.get("dp_id"),
            "name": spec.get("name", ""),
            "type": spec.get("type", ""),
            "access": spec.get("access", ""),
            "unit": spec.get("unit", ""),
            "raw": s.get("value"),
            "value": scaled(s.get("value"), spec),
            **{k: spec[k] for k in ("range", "min", "max", "scale") if k in spec},
        })
    has_battery = any("battery" in d["code"].lower() for d in dps)
    gateway = cat in cfg.get("gateway_categories", [])
    update_time = dev.get("update_time") or 0
    return {
        "id": dev["id"],
        "name": dev.get("name", dev["id"]),
        "category": cat,
        "product": dev.get("product_name", ""),
        "online": bool(dev.get("online")),
        "sub": bool(dev.get("sub")),
        "gateway": gateway,
        # Від мережі живиться все, що не шлюз і не має батареї (розетки, датчик газу, ІЧ-пульт)
        "mains": not gateway and not has_battery,
        "update_time": update_time * 1000 if update_time < 10**12 else update_time,
        "dps": dps,
        "info": {k: dev[k] for k in INFO_FIELDS if dev.get(k) not in (None, "")},
    }


def dp(device, code):
    return next((d for d in device["dps"] if d["code"] == code), None)
