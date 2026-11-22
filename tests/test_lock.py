import os
import tempfile
import unittest

from helpers import CFG, HOUR, T0, device

import web
from store import LocalStore


class LockScreen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = LocalStore(os.path.join(self.tmp, "s.json"))
        for d in (device("hub", "Хаб", [], gateway=True),
                  device("out", "Назовні", [("temp_current", 3.2, "°C"), ("humidity_value", 90, "%")]),
                  device("k", "Кухня", [("temp_current", 21.4, "°C"), ("humidity_value", 48, "%")])):
            self.store.put_state(d["id"], d)
        self.store.put_meta("last_poll", {"ts": T0})
        self.store.put_meta("vacuum", {"name": "Кузя", "state": "charging_completed", "battery": 100})

    def view(self, **meta):
        for k, v in meta.items():
            self.store.put_meta(k, v)
        return web.lock_view(self.store, {**CFG, "weather": {"sensor": "Назовні"}}, "owner", now=T0 + 60_000)

    def test_normal(self):
        v = self.view()
        self.assertEqual(v["title"], "💡 Світло є")
        self.assertEqual(v["text"].split(chr(10)), ["🕊 Тривоги немає", "🌡 3° надворі · 🏠 21° вдома"])
        self.assertFalse(v["stale"])
        for line in [v["title"]] + v["text"].split(chr(10)):
            self.assertLessEqual(len(line), 32, line)   # Samsung: ~30 символів у рядку

    def test_outage_and_alert(self):
        v = self.view(rules={"outage": {"start": T0 - HOUR}},
                      air_alerts={"Київська область": {"active": True, "level": "red", "since": T0 - 600_000}})
        self.assertEqual(v["title"], "🔌 Немає")
        self.assertEqual(v["text"].split(chr(10)), ["🔴 Тривога з 23:50", "🌡 3° надворі · 🏠 21° вдома"])

    def test_weather_icon(self):
        v = self.view(weather={"current": {"temp": 2.6, "code": 61, "precip": 0.3}})
        self.assertIn("🌧 3° надворі · 🏠 21° вдома", v["text"])           # дощ; надворі — датчик (3.2°), не прогноз

    def test_bot_gives_personal_url(self):
        import bot
        t = bot.lockscreen_text("https://h.example/?k=SECRET")
        self.assertIn("https://h.example/api/lock?k=SECRET", t)
        self.assertNotIn("lockscreen", bot.GUEST_CMDS)


if __name__ == "__main__":
    unittest.main()
