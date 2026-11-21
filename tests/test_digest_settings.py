import unittest

from helpers import CFG, HOUR, T0

import bot
import reports


class FakeStore:
    def __init__(self, users):
        self.meta = {"tg_users": users}

    def get_meta(self, k):
        return self.meta.get(k)

    def put_meta(self, k, v):
        self.meta[k] = v

    def get_states(self):
        return {}


class FakeNotifier:
    def __init__(self):
        self.sent, self.calls = [], []

    def send(self, text, ts=None, to=None, kind=None):
        self.sent.append((tuple(to or ()), kind, text))
        return True

    def call(self, method, payload=None, retries=1):
        self.calls.append((method, payload))
        return True, {"result": {"message_id": len(self.calls)}}


class DigestMenu(unittest.TestCase):
    def setUp(self):
        self.users = {"1": {"name": "Тест", "status": "approved", "role": "owner", "menu": "settings"}}
        self.store, self.n = FakeStore(self.users), FakeNotifier()

    def press(self, text):
        users = self.store.get_meta("tg_users")
        return bot.handle_menu(self.store, CFG, self.n, users, "1", text, None, None)

    def test_pick_hour_and_toggle(self):
        self.assertEqual(bot.digest_button(self.users["1"], CFG), "☀️ Ранковий підсумок: 07:00")
        self.assertEqual(self.press("☀️ Ранковий підсумок: 07:00"), "menu:digest")
        self.press("08:00")
        u = self.store.get_meta("tg_users")["1"]
        self.assertEqual((u["digest"], u["digest_hour"]), (True, 8))
        self.press("🚫 Вимкнути")
        self.assertFalse(self.store.get_meta("tg_users")["1"]["digest"])
        self.assertEqual(bot.digest_button(self.store.get_meta("tg_users")["1"], CFG), "☀️ Ранковий підсумок: вимк.")

    def test_digest_sound_toggle(self):
        u = self.users["1"]
        self.assertTrue(bot.is_silent(u, "digest", 7))          # за замовчуванням — без звуку
        u["menu"] = "sound"
        self.press("☀️ Ранковий підсумок: 🔕 без звуку")
        u = self.store.get_meta("tg_users")["1"]
        self.assertFalse(bot.is_silent(u, "digest", 7))         # зі звуком
        self.assertFalse(bot.is_silent({**u, "sound": {**u["sound"], "night": {"on": True, "from": 0, "to": 8}}}, "digest", 7))
        self.assertTrue(bot.is_silent({**u, "sound": {**u["sound"], "mute": True}}, "digest", 7))
        self.assertTrue(bot.is_silent({"sound": {"night": {"on": True, "from": 0, "to": 8}}}, "alarm", 7))


class DigestTime(unittest.TestCase):
    def test_each_user_own_hour(self):
        users = {"a": {"status": "approved"}, "b": {"status": "approved", "digest_hour": 9},
                 "c": {"status": "approved", "digest": False}}
        orig = reports.digest_text
        reports.digest_text = lambda store, cfg, u, now, states=None: "hi"
        try:
            store, n, sent = FakeStore(users), FakeNotifier(), {}
            for h in (6, 7, 8, 9, 10, 11, 12):
                reports.run_digest(store, CFG, n, T0 + h * HOUR + 60_000, users, sent)
            got = [(to[0], ) for to, kind, _ in n.sent]
            self.assertEqual(got, [("a",), ("b",)])        # кожен — один раз, у свою годину; c вимкнув
            self.assertEqual([k for _, k, _ in n.sent], ["digest", "digest"])
        finally:
            reports.digest_text = orig


if __name__ == "__main__":
    unittest.main()
