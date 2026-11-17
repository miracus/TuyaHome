"""Адреса користувача бота → регіон тривог, зона графіків ДТЕК і (де можливо) черга.

- Геокодер — OpenStreetMap Nominatim (без ключа; правила: ≤ 1 запит/с, свій User-Agent).
- Зона графіків — за областю: є лише для м. Київ, Київської, Дніпропетровської й Одеської областей
  (дзеркало Baskerville42/outage-data-ua).
- Черга за адресою — лише через відкритий API YASNO (м. Київ і Дніпропетровська обл.). Для Київської й Одеської
  областей черга є тільки на сайтах ДТЕК, які закриті захистом від ботів (не обходимо) — людина обирає чергу кнопками.
"""
import json
import re
import urllib.parse
import urllib.request

UA = "TuyaHome-bot/0.1"
NOMINATIM = "https://nominatim.openstreetmap.org"
YASNO = "https://app.yasno.ua/api/blackout-service/public/shutdowns/addresses/v2"

# зона дзеркала → опис; yasno = (regionId, [dsoId…]) — де черга визначається автоматично
ZONES = {
    "kyiv": {"name": "м. Київ", "provider": "ДТЕК Київські електромережі",
             "lookup": "https://www.dtek-kem.com.ua/ua/shutdowns", "yasno": (25, [902])},
    "kyiv-region": {"name": "Київська область", "provider": "ДТЕК Київські регіональні електромережі",
                    "lookup": "https://www.dtek-krem.com.ua/ua/shutdowns"},
    "dnipro": {"name": "Дніпропетровська область", "provider": "ДТЕК Дніпровські електромережі",
               "lookup": "https://www.dtek-dnem.com.ua/ua/shutdowns", "yasno": (3, [301, 303])},
    "odesa": {"name": "Одеська область", "provider": "ДТЕК Одеські електромережі",
              "lookup": "https://www.dtek-oem.com.ua/ua/shutdowns"},
}
STATE_ZONE = {"Київська область": "kyiv-region", "Дніпропетровська область": "dnipro", "Одеська область": "odesa"}


def _get(url, params=None, timeout=8):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "uk"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def geocode(text=None, lat=None, lon=None):
    """Текст адреси або координати → поля адреси OSM (state, district, municipality, city, road, house_number)."""
    if text:
        res = _get(f"{NOMINATIM}/search", {"q": text, "format": "jsonv2", "addressdetails": 1, "limit": 1,
                                           "countrycodes": "ua", "accept-language": "uk"})
        if not res:
            return None
        hit = res[0]
    else:
        hit = _get(f"{NOMINATIM}/reverse", {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18,
                                            "addressdetails": 1, "accept-language": "uk"})
        if not hit or hit.get("error"):
            return None
    a = hit.get("address") or {}
    if a.get("country_code") != "ua":
        return None
    city = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or ""
    state = a.get("state") or ("Київ" if city == "Київ" else "")
    return {"state": state, "district": a.get("district", ""), "municipality": a.get("municipality", ""),
            "city": city, "road": a.get("road", ""), "house": a.get("house_number", ""),
            "lat": float(hit["lat"]), "lon": float(hit["lon"]), "label": short_label(a, city)}


def short_label(a, city):
    parts = [city, a.get("road", ""), a.get("house_number", "")]
    return ", ".join(p for p in parts if p) or a.get("state", "")


def zone_for(g):
    if g["state"] == "Київ" or g["city"] == "Київ" and "область" not in g["state"]:
        return "kyiv"
    return STATE_ZONE.get(g["state"])


# --- регіон тривог (довідник siren.pp.ua): громада → район → область
def _stem(word):
    return re.sub(r"(ська|цька|зька|ський|цький|ий|а|е|і)$", "", word.lower())


def alert_region(cat, g):
    """id найдрібнішого регіону довідника тривог для адреси (громада, інакше район, інакше область)."""
    if g["state"] == "Київ" or (g["city"] == "Київ" and "область" not in g["state"]):
        return next((r["id"] for r in cat.values() if r["name"] == "м. Київ"), None)
    state = next((r for r in cat.values() if r["parent"] is None and r["name"] == g["state"]), None)
    if not state:
        return None
    dist = next((cat[k] for k in state["children"] if cat[k]["name"] == g["district"]), None)
    if not dist:
        return state["id"]
    mun = (g["municipality"] or "").split()
    if mun:
        stem = _stem(mun[0])
        for k in dist["children"]:
            words = [_stem(w.strip("«»().,")) for w in cat[k]["name"].split()]
            if stem and stem in words:
                return k
    return dist["id"]


# --- черга через YASNO (лише м. Київ і Дніпропетровська обл.)
_TYPES = ("вулиця", "вул.", "проспект", "просп.", "провулок", "пров.", "бульвар", "бульв.", "площа", "пл.",
          "узвіз", "набережна", "шосе", "тупик", "проїзд", "алея", "майдан", "дорога")


_CANON = {"вулиця": "вул", "вул.": "вул", "проспект": "просп", "просп.": "просп", "провулок": "пров", "пров.": "пров",
          "бульвар": "бульв", "бульв.": "бульв", "площа": "пл", "пл.": "пл"}


def _street_type(road):
    return next((_CANON[w.lower()] for w in road.split() if w.lower() in _CANON), None)


def _clean_street(road):
    words = [w for w in road.split() if w.lower() not in _TYPES]
    return " ".join(words)


def _norm(s):
    return re.sub(r"\s+", "", s.lower().replace("’", "'"))


def yasno_group(zone, g):
    """Черга 'GPVx.y' або None. Будь-яка помилка/неоднозначність → None (людина обере сама)."""
    z = ZONES.get(zone) or {}
    if "yasno" not in z or not g.get("road") or not g.get("house"):
        return None
    region, dsos = z["yasno"]
    street_q = _clean_street(g["road"])
    if len(street_q) < 3:
        return None
    for dso in dsos:
        try:
            base = {"regionId": region, "dsoId": dso}
            if zone == "dnipro":
                cities = _get(f"{YASNO}/cities", {**base, "query": g["city"][:40]})
                city = next((c for c in cities if c["value"].split(". ", 1)[-1] == g["city"]), None)
                if not city:
                    continue
                base["cityId"] = city["id"]
            streets = _get(f"{YASNO}/streets", {**base, "query": street_q[:40]})
            same = [s for s in streets if _norm(_clean_street(s["value"])) == _norm(street_q)]
            kind = _street_type(g["road"])
            typed = [s for s in same if _street_type(s["value"]) == kind] if kind else []
            # одна назва буває в кількох типів (вул./просп./пров. Яворницького) — беремо лише однозначний збіг
            street = typed[0] if len(typed) == 1 else same[0] if len(same) == 1 else None
            if not street:
                continue
            num = re.match(r"\d+", g["house"])
            if not num:
                return None
            houses = _get(f"{YASNO}/houses", {**base, "streetId": street["id"], "query": num.group()})
            house = next((h for h in houses if _norm(h["value"]) == _norm(g["house"])), None)
            if not house:
                return None
            grp = _get(f"{YASNO}/group", {**base, "streetId": street["id"], "houseId": house["id"]})
            if grp.get("group"):
                return f"GPV{grp['group']}.{grp.get('subgroup', 1)}"
        except Exception as e:  # API змінився або недоступний — чергу обере людина
            print(f"yasno error: {type(e).__name__}: {e}")
            return None
    return None
