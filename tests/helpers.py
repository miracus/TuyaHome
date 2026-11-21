"""Спільне для тестів: шлях до app/ і детерміністичний конфіг (UTC)."""
import os
import sys

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
T0 = 1_795_000_000_000 - 1_795_000_000_000 % DAY  # північ UTC

CFG = {"timezone": "UTC", "utc_offset_fallback_h": 0, "polling": {"default_store_every_min": 30},
       "weather": {"sensor": "Назовні"}, "alarm_codes": [], "alarm_values": ["alarm"]}


def device(dev_id, name, dps, online=True, mains=False, gateway=False, category="wsdcg"):
    return {"id": dev_id, "name": name, "product": "test", "online": online, "category": category, "mains": mains,
            "gateway": gateway, "sub": not gateway,
            "dps": [{"code": c, "value": v, "unit": u, "type": "value"} for c, v, u in dps]}
