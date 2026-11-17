"""Локальний запуск без AWS: стан у out/local_store.json, реальний Tuya API.

  python run_local.py poll            один цикл опитувача (сповіщення друкуються, якщо в .env немає Telegram)
  python run_local.py serve [port]    дашборд на http://localhost:8765/?k=local
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "app"))
sys.stdout.reconfigure(encoding="utf-8")

from devices import load_config  # noqa: E402
from notify import Notifier  # noqa: E402
from store import LocalStore, TokenCache  # noqa: E402
from tuya_cloud import TuyaCloud, load_env  # noqa: E402

STORE = os.path.join(HERE, "out", "local_store.json")


def poll(now=None):
    import poller

    load_env(os.path.join(HERE, ".env"))
    cfg = load_config()
    store = LocalStore(STORE)
    api = TuyaCloud(os.environ["TUYA_ACCESS_ID"], os.environ["TUYA_ACCESS_SECRET"], os.environ["TUYA_ENDPOINT"],
                    token_cache=TokenCache(store))
    notifier = Notifier(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""))
    print(json.dumps(poller.run(store, api, cfg, notifier, os.environ["TUYA_UID"], now=now), ensure_ascii=False))


def serve(port=8765):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    import web

    cfg = load_config()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.do_GET("POST", self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())

        def do_GET(self, method="GET", body=""):
            u = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            resp = web.handle(LocalStore(STORE), u.path, qs, cfg, keys={"local": "local"}, method=method, body=body)
            body = resp["body"].encode()
            self.send_response(resp["statusCode"])
            for k, v in resp["headers"].items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

    print(f"http://localhost:{port}/?k=local")
    HTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "poll"
    if cmd == "poll":
        poll(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    elif cmd == "serve":
        serve(int(sys.argv[2]) if len(sys.argv) > 2 else 8765)
    else:
        print(__doc__)
        print(time.time())
