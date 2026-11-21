import unittest

from helpers import CFG, HOUR, MIN, T0, device

import climate


def run(dev, steps):
    """steps — [(ts, temp, hum)]; повертає [(kind, text)]."""
    state, meta, out = {}, {}, []
    for ts, t, h in steps:
        dev["dps"] = [{"code": "temp_current", "value": t, "unit": "℃"}, {"code": "humidity_value", "value": h, "unit": "%"}]
        st = {**dev, **{k: v for k, v in state.items() if k == "clim"}}
        climate.check(dev, st, meta, CFG, ts, lambda kind, text, *a, **k: out.append((kind, text)))
        state = st
    return out


class Humidity(unittest.TestCase):
    def test_long_high_humidity_daytime(self):
        dev = device("p", "Спальня", [], category="pir")
        steps = [(T0 + 9 * HOUR + i * 10 * MIN, 21, 70) for i in range(4 * 6)]  # 09:00–13:00, 70%
        kinds = [k for k, _ in run(dev, steps)]
        self.assertEqual(kinds.count("humidity_high"), 1)  # один раз на добу

    def test_short_spike_and_night_silent(self):
        dev = device("p", "Спальня", [], category="pir")
        night = [(T0 + i * 10 * MIN, 21, 72) for i in range(6 * 6)]  # 00:00–06:00 — вночі не будимо
        self.assertEqual(run(dev, night), [])
        spike = [(T0 + 10 * HOUR + i * 10 * MIN, 21, 70 if i < 6 else 60) for i in range(12)]  # 1 год — і спало
        self.assertEqual(run(dev, spike), [])

    def test_outdoor_sensor_excluded(self):
        dev = device("o", "Назовні", [])
        steps = [(T0 + 9 * HOUR + i * 10 * MIN, 5, 95) for i in range(30)]
        self.assertEqual(run(dev, steps), [])


class Cold(unittest.TestCase):
    def test_falling_temperature_warns_ahead(self):
        dev = device("k", "Кухня", [])
        steps = [(T0 + 12 * HOUR + i * 10 * MIN, 18 - i * 0.1, 50) for i in range(10)]  # −0.6°/год
        out = run(dev, steps)
        self.assertTrue(any(k == "cold" and "за такого темпу" in t for k, t in out))

    def test_below_threshold(self):
        dev = device("k", "Кухня", [])
        out = run(dev, [(T0 + 12 * HOUR, 15.5, 50)])
        self.assertEqual([k for k, _ in out], ["cold"])

    def test_trend(self):
        track = [[T0 + i * 10 * MIN, 20 - i * 0.1, 50] for i in range(7)]
        self.assertAlmostEqual(climate.trend(track, T0 + HOUR), -0.6, places=3)


if __name__ == "__main__":
    unittest.main()
