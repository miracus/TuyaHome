import unittest

from helpers import CFG, DAY, HOUR, T0, device

import rules


class BatteryDrain(unittest.TestCase):
    def replay(self, series):
        """series — [(ts, %)] як приходили знімки; повертає моменти сповіщень."""
        meta, track, fired = {}, None, []
        for ts, v in series:
            track = rules.battery_track(track, v, ts, CFG)
            d = rules.battery_drain(track, ts, CFG)
            if d and rules._alert_due(meta, "x", ts, 72 * HOUR):
                fired.append((ts, d))
        return fired

    def test_fast_drain_detected(self):
        # 100% → 0% за 5 діб (датчик руху «Спальня», 16–21.11): ~20%/добу
        series = [(T0 + i * HOUR, max(0, 100 - 10 * (i // 12))) for i in range(5 * 24)]
        fired = self.replay(series)
        self.assertTrue(fired)
        ts, d = fired[0]
        self.assertLess(ts - T0, 2 * DAY)  # помітили за менше ніж 2 доби
        self.assertGreaterEqual(d["per_day"], 5)

    def test_normal_device_silent(self):
        # 1%/добу кроками по 10% — жодного сповіщення за 30 днів (знімок раз на 2 хв)
        series = [(T0 + i * 2 * 60_000, 100 - 10 * (i // (10 * 24 * 30))) for i in range(30 * 24 * 30)]
        self.assertEqual(self.replay(series), [])

    def test_battery_replacement_resets(self):
        track = rules.battery_track(None, 20, T0, CFG)
        track = rules.battery_track(track, 100, T0 + HOUR, CFG)
        self.assertEqual([p[1] for p in track], [100])

    def test_short_jumps_ignored(self):
        # стрибки при заміні батарейок (за 3 год) — ще не сповіщення
        track = None
        for i, v in enumerate([60, 50, 40]):
            track = rules.battery_track(track, v, T0 + i * HOUR, CFG)
        self.assertIsNone(rules.battery_drain(track, T0 + 2 * HOUR, CFG))

    def test_evaluate_emits_owner_event(self):
        dev = device("d1", "Спальня", [("battery_percentage", 100, "%")], category="pir")
        prev, meta, kinds = {}, {}, []
        for i, v in enumerate([100, 100, 90, 80, 70]):
            dev["dps"][0]["value"] = v
            r = rules.evaluate(prev, [dev], meta, CFG, T0 + i * 4 * HOUR)
            prev, meta = {s["id"]: s for s in r.states}, r.meta
            kinds += [e["kind"] for e in r.events]
        self.assertIn("battery_drain", kinds)

    def test_door_battery_without_unit(self):
        dev = device("d2", "Двері", [("battery", 52, ""), ("battery_state", "low", "")], category="mcs")
        self.assertEqual([d["code"] for d in rules.battery_dps(dev)], ["battery"])


if __name__ == "__main__":
    unittest.main()
