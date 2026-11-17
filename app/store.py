"""Сховище стану: DynamoDB у Lambda, JSON-файл локально. Однаковий інтерфейс.

Одна таблиця (pk, sk — рядки):
  META   / <name>                 службове: токен, стан відключення, кулдауни сповіщень
  STATE  / <device_id>            останній стан приладу
  MODEL  / <device_id>            модель приладу (масштаб, одиниці) — кеш
  H#<device_id>#<code> / <ts13>   історія числового DP
  EVENT  / <ts13>#<n>             журнал подій
Складні значення лежать JSON-рядком у полі doc — без мороки з Decimal.
"""
import json
import os
import time


def ts13(ms):
    return f"{int(ms):013d}"


class TokenCache:
    def __init__(self, store):
        self.store = store

    def load(self):
        m = self.store.get_meta("token")
        return (m["token"], m["expire"]) if m else None

    def save(self, token, expire):
        self.store.put_meta("token", {"token": token, "expire": expire})


class LocalStore:
    def __init__(self, path):
        self.path = path
        self.data = {"META": {}, "STATE": {}, "MODEL": {}, "H": {}, "EVENT": []}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))

    def flush(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)

    def get_meta(self, name):
        return self.data["META"].get(name)

    def put_meta(self, name, doc):
        self.data["META"][name] = doc

    def get_states(self):
        return dict(self.data["STATE"])

    def put_state(self, dev_id, doc):
        self.data["STATE"][dev_id] = doc

    def get_model(self, dev_id):
        return self.data["MODEL"].get(dev_id)

    def put_model(self, dev_id, doc):
        self.data["MODEL"][dev_id] = doc

    def add_history(self, dev_id, code, ms, value):
        self.data["H"].setdefault(f"{dev_id}#{code}", []).append([int(ms), value])

    def get_history(self, dev_id, code, since_ms):
        return [tuple(p) for p in self.data["H"].get(f"{dev_id}#{code}", []) if p[0] >= since_ms]

    def add_event(self, ms, doc):
        self.data["EVENT"].append({"ts": int(ms), **doc})

    def get_events(self, limit=100, since_ms=0):
        ev = [e for e in self.data["EVENT"] if e["ts"] >= since_ms]
        return sorted(ev, key=lambda e: e["ts"], reverse=True)[:limit]

    # архів без строку зберігання (графік ДТЕК по днях тощо)
    def get_archive(self, kind, key):
        return self.data.setdefault("ARCH", {}).get(f"{kind}#{key}")

    def put_archive(self, kind, key, doc):
        self.data.setdefault("ARCH", {})[f"{kind}#{key}"] = doc

    def list_archive(self, kind, key_from=""):
        arch = self.data.get("ARCH", {})
        return [arch[k] for k in sorted(arch) if k.startswith(f"{kind}#") and k[len(kind) + 1:] >= key_from]


class DynamoStore:
    def __init__(self, table_name, history_ttl_days=180, events_ttl_days=730):
        import boto3  # є в рантаймі Lambda; локально не потрібен

        self.table = boto3.resource("dynamodb").Table(table_name)
        self.history_ttl = history_ttl_days * 86400
        self.events_ttl = events_ttl_days * 86400
        self._event_n = 0

    def flush(self):
        pass

    def _get(self, pk, sk):
        item = self.table.get_item(Key={"pk": pk, "sk": sk}).get("Item")
        return json.loads(item["doc"]) if item else None

    def _put(self, pk, sk, doc, ttl=None):
        item = {"pk": pk, "sk": sk, "doc": json.dumps(doc, ensure_ascii=False)}
        if ttl:
            item["exp"] = int(time.time()) + ttl
        self.table.put_item(Item=item)

    def _query(self, pk, sk_from=None, newest_first=False, limit=None):
        from boto3.dynamodb.conditions import Key

        cond = Key("pk").eq(pk)
        if sk_from is not None:
            cond = cond & Key("sk").gte(sk_from)
        kwargs = {"KeyConditionExpression": cond, "ScanIndexForward": not newest_first}
        items = []
        while True:
            if limit:
                kwargs["Limit"] = limit - len(items)
            r = self.table.query(**kwargs)
            items += r["Items"]
            if "LastEvaluatedKey" not in r or (limit and len(items) >= limit):
                return items
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def get_meta(self, name):
        return self._get("META", name)

    def put_meta(self, name, doc):
        self._put("META", name, doc)

    def get_states(self):
        return {i["sk"]: json.loads(i["doc"]) for i in self._query("STATE")}

    def put_state(self, dev_id, doc):
        self._put("STATE", dev_id, doc)

    def get_model(self, dev_id):
        return self._get("MODEL", dev_id)

    def put_model(self, dev_id, doc):
        self._put("MODEL", dev_id, doc)

    def add_history(self, dev_id, code, ms, value):
        self._put(f"H#{dev_id}#{code}", ts13(ms), value, ttl=self.history_ttl)

    def get_history(self, dev_id, code, since_ms):
        return [(int(i["sk"]), json.loads(i["doc"])) for i in self._query(f"H#{dev_id}#{code}", ts13(since_ms))]

    def add_event(self, ms, doc):
        self._event_n += 1
        self._put("EVENT", f"{ts13(ms)}#{self._event_n}", {"ts": int(ms), **doc}, ttl=self.events_ttl)

    def get_events(self, limit=100, since_ms=0):
        return [json.loads(i["doc"]) for i in self._query("EVENT", ts13(since_ms), newest_first=True, limit=limit)]

    # архів без строку зберігання (без exp — TTL DynamoDB його не видаляє)
    def get_archive(self, kind, key):
        return self._get(f"ARCH#{kind}", str(key))

    def put_archive(self, kind, key, doc):
        self._put(f"ARCH#{kind}", str(key), doc)

    def list_archive(self, kind, key_from=""):
        return [json.loads(i["doc"]) for i in self._query(f"ARCH#{kind}", str(key_from) if key_from else None)]
