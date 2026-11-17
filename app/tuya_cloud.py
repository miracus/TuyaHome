"""Мінімальний клієнт Tuya Cloud OpenAPI (лише стандартна бібліотека).

Підпис: https://developer.tuya.com/en/docs/iot/new-singnature
"""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()


def load_env(path=".env"):
    if not os.path.exists(path):
        raise SystemExit(f"Немає файлу {path}. Скопіюйте .env.example у .env і впишіть Access Secret.")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


class TuyaError(RuntimeError):
    pass


# Коди Tuya «токен недійсний / прострочений» — після них токен беремо заново
TOKEN_INVALID_CODES = {1010, 1011}


class TuyaCloud:
    def __init__(self, access_id, access_secret, endpoint, token_cache=None):
        """token_cache — об'єкт з load() -> (token, expire) | None і save(token, expire).
        Кеш економить квоту: токен живе ~2 год, а не береться на кожен запуск."""
        self.access_id = access_id
        self.secret = access_secret.encode()
        self.endpoint = endpoint.rstrip("/")
        self.token = None
        self.token_expire = 0
        self.token_cache = token_cache
        self.calls = 0

    def _sign(self, method, path_with_query, body, with_token):
        t = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        body_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_BODY_SHA256
        string_to_sign = "\n".join([method, body_hash, "", path_with_query])
        payload = self.access_id + (self.token if with_token else "") + t + nonce + string_to_sign
        sign = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest().upper()
        headers = {
            "client_id": self.access_id,
            "sign": sign,
            "t": t,
            "nonce": nonce,
            "sign_method": "HMAC-SHA256",
            "Content-Type": "application/json",
        }
        if with_token:
            headers["access_token"] = self.token
        return headers

    def _request(self, method, path, params=None, body=None, with_token=True):
        if params:
            # Tuya вимагає відсортовані параметри в рядку для підпису
            query = "&".join(f"{k}={params[k]}" for k in sorted(params))
            path_with_query = f"{path}?{query}"
        else:
            path_with_query = path
        raw = json.dumps(body).encode() if body is not None else b""
        headers = self._sign(method, path_with_query, raw, with_token)
        url = self.endpoint + urllib.parse.quote(path_with_query, safe="/?&=,:")
        req = urllib.request.Request(url, data=raw or None, headers=headers, method=method)
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"success": False, "http_status": e.code, "msg": e.read().decode(errors="replace")}

    def ensure_token(self, force=False):
        if not force and self.token and time.time() < self.token_expire - 60:
            return
        if not force and self.token_cache:
            cached = self.token_cache.load()
            if cached and time.time() < cached[1] - 60:
                self.token, self.token_expire = cached
                return
        r = self._request("GET", "/v1.0/token", {"grant_type": 1}, with_token=False)
        if not r.get("success"):
            raise TuyaError(f"Не вдалося отримати токен: code={r.get('code')} msg={r.get('msg')}")
        self.token = r["result"]["access_token"]
        self.token_expire = time.time() + r["result"]["expire_time"]
        if self.token_cache:
            self.token_cache.save(self.token, self.token_expire)

    def _call(self, method, path, params=None, body=None):
        self.ensure_token()
        r = self._request(method, path, params, body)
        if not r.get("success") and r.get("code") in TOKEN_INVALID_CODES:
            self.ensure_token(force=True)
            r = self._request(method, path, params, body)
        return r

    def get(self, path, params=None):
        return self._call("GET", path, params)

    def post(self, path, body=None):
        return self._call("POST", path, body=body or {})
