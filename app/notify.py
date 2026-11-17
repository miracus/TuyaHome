"""Сповіщення в Telegram з чергою: що не вдалося надіслати — досилається при наступних запусках.
Кожне сповіщення — кожному отримувачу (особисті чати підтверджених користувачів бота). Без токена — лише друк у лог."""
import json
import time
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    def __init__(self, bot_token="", chat_id="", max_outbox=100, enabled=True):
        self.bot_token = bot_token
        # отримувачі: один id або список (особисті чати користувачів бота)
        self.chat_ids = [str(c) for c in (chat_id if isinstance(chat_id, (list, tuple)) else [chat_id]) if c]
        self.chat_id = self.chat_ids[0] if self.chat_ids else ""
        self.enabled = enabled  # config.notifications_enabled
        self.sent = []
        self.last_error = None
        self.down = False      # Telegram не відповідає — до кінця запуску не чекаємо на нього
        self.outbox = []       # [{"ts", "text", "chat_id"}] — завантажує й зберігає poller
        self.max_outbox = max_outbox
        self.dropped = 0
        self.markup = {}       # {chat_id: reply_markup} — постійні кнопки бота, чіпляються до кожного сповіщення
        self.silent = None     # fn(chat_id, kind) -> True: прийде без звуку (налаштування «🔔 Звук» у боті)

    @property
    def configured(self):
        return bool(self.bot_token and self.chat_id)

    def call(self, method, payload=None, retries=1):
        """Виклик Bot API. Повертає (ok, відповідь або текст помилки); ok = None — «ймовірно доставлено».

        Таймаут ЧИТАННЯ відповіді не повторюємо: 14.11 Bot API приймав запити, але відповідав через ~20 хв,
        і кожен повтор став окремим повідомленням (дублі). Повтор — лише якщо запит точно не дійшов
        (не вдалося з'єднатися) або сервер відповів 5xx."""
        body = json.dumps(payload or {}).encode()
        for attempt in range(retries + 1):
            req = urllib.request.Request(API.format(token=self.bot_token, method=method), data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    return True, json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                detail = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
                if e.code < 500:
                    return False, detail
            except TimeoutError as e:  # з'єднання було, відповіді немає — запит, найімовірніше, прийнято
                return None, f"TimeoutError: {e}"
            except Exception as e:  # не вдалося з'єднатися (DNS, відмова) — запит не дійшов
                detail = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
        return False, detail

    def _deliver(self, text, chat_id=None, kind=None):
        """True — доставлено; False — тимчасовий збій (варто повторити); None — помилка запиту, повтор не допоможе."""
        cid = str(chat_id or self.chat_id)
        payload = {"chat_id": cid, "text": text, "disable_web_page_preview": True}
        if self.silent:
            try:
                if self.silent(cid, kind):
                    payload["disable_notification"] = True  # приходить, але без звуку
            except Exception as e:  # налаштування звуку не мають ламати доставку
                print(f"[notify] silent check failed: {e}")
        if self.markup.get(cid):
            payload["reply_markup"] = self.markup[cid]
        ok, detail = self.call("sendMessage", payload)
        if ok:
            self.sent.append(text)
            return True
        if ok is None:  # ймовірно доставлено — не повторюємо (щоб не було дублів), але далі в цьому запуску не чекаємо
            self.last_error = detail
            self.down = True
            print(f"[notify] Telegram не відповів, вважаємо доставленим: {detail}; text: {text}")
            return True
        self.last_error = detail
        permanent = detail.startswith("HTTP 4")
        self.down = self.down or not permanent
        print(f"[notify] Telegram error: {detail}; text: {text}")
        return None if permanent else False

    def _queue(self, ts, text, chat_id, kind=None):
        self.outbox.append({"ts": int(ts), "text": text, "chat_id": chat_id, "kind": kind})
        while len(self.outbox) > self.max_outbox:
            self.outbox.pop(0)
            self.dropped += 1

    def send(self, text, ts=None, to=None, kind=None):
        """to — лише цим отримувачам (напр. тим, у кого увімкнена тривога для регіону); None — усім.
        kind — тип сповіщення (alarm, schedule, air, outage_start…): від нього залежить, чи буде звук."""
        ts = ts or int(time.time() * 1000)
        if not self.enabled:
            print(f"[notify вимкнено] {text}")
            return True
        if not self.configured:
            print(f"[notify] {text}")
            return True
        delivered = False
        for cid in (self.chat_ids if to is None else [str(c) for c in to]):
            if self.down:
                print(f"[notify] (у черзі, Telegram недоступний) {cid}: {text}")
                self._queue(ts, text, cid, kind)
                continue
            result = self._deliver(text, cid, kind)
            if result is False:
                self._queue(ts, text, cid, kind)
            delivered = delivered or bool(result)
        return delivered

    def flush(self, fmt_ts):
        """Досилає чергу по порядку; зупиняється на першому збої. fmt_ts(ms) -> рядок часу події."""
        if not self.enabled or not self.configured or not self.outbox:
            return 0
        delivered = 0
        if self.dropped:
            for cid in self.chat_ids:
                self._deliver(f"⚠️ Через збій Telegram {self.dropped} старіших сповіщень не збереглося", cid)
        while self.outbox and not self.down:
            item = self.outbox[0]
            result = self._deliver(f"⏳ {fmt_ts(item['ts'])} · {item['text']}", item.get("chat_id"), item.get("kind"))
            if result is False:
                break
            self.outbox.pop(0)  # доставлено або безнадійно (4xx) — далі не тримаємо
            delivered += result is True
        return delivered
