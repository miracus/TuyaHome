import unittest

from helpers import CFG, DAY, HOUR, MIN, T0

import reports


def sched(off, maybe=()):
    return {"group": "1.1", "days": [{"day": T0, "off": [list(x) for x in off], "maybe": [list(x) for x in maybe]}]}


class LightToday(unittest.TestCase):
    def test_on_windows_from_now(self):
        sc = sched([(T0 + 8 * HOUR, T0 + 12 * HOUR), (T0 + 18 * HOUR, T0 + 22 * HOUR)])
        lines = reports.light_today(sc, T0, T0 + DAY, CFG, now=T0 + 7 * HOUR)
        text = "\n".join(lines)
        self.assertIn("🟢 зараз–08:00", text)
        self.assertIn("🟢 12:00–18:00", text)
        self.assertIn("🟢 22:00–24:00", text)
        self.assertIn("🔴 без світла: 08:00–12:00, 18:00–22:00", text)

    def test_no_outages(self):
        lines = reports.light_today(sched([]), T0, T0 + DAY, CFG, now=T0 + 7 * HOUR)
        self.assertIn("весь день", lines[0])

    def test_no_schedule(self):
        self.assertIn("ще немає", reports.light_today(None, T0, T0 + DAY, CFG)[0])


class Health(unittest.TestCase):
    def test_battery_rate_after_replacement(self):
        pts = [(T0, 30), (T0 + DAY, 100), (T0 + 2 * DAY, 98), (T0 + 4 * DAY, 94)]
        self.assertAlmostEqual(reports.battery_rate(pts), 2.0)
        self.assertIsNone(reports.battery_rate(pts[:2]))

    def test_offline_outside_outages_only(self):
        ev = [{"kind": "offline", "device_id": "a", "ts": T0 + HOUR},
              {"kind": "online", "device_id": "a", "ts": T0 + HOUR + 5 * MIN},
              {"kind": "offline", "device_id": "a", "ts": T0 + 10 * HOUR},          # під час відключення світла
              {"kind": "online", "device_id": "a", "ts": T0 + 14 * HOUR}]
        out = reports.offline_stats(ev, T0 + DAY, expected=[[T0 + 10 * HOUR, T0 + 14 * HOUR]])
        self.assertEqual(out, {"a": (1, 5 * MIN)})


if __name__ == "__main__":
    unittest.main()
