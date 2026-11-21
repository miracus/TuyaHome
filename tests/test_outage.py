import unittest

from helpers import CFG, MIN, T0, device

import rules


def mains(online):
    return [device("plug", "Розетка", [("cur_voltage", 220, "V")], online=online, mains=True, category="cz"),
            device("gas", "Газ", [], online=online, mains=True, category="rqbj"),
            device("hub", "Хаб", [], online=True, gateway=True, category="wg2")]


class OutageConfirmation(unittest.TestCase):
    def sequence(self, offline_min, expected_off):
        prev, meta, kinds = {}, {"sched_expected_off": expected_off}, []
        steps = [(0, True)] + [(m, False) for m in range(2, offline_min + 1, 2)] + [(offline_min + 2, True)]
        for m, online in steps:
            r = rules.evaluate(prev, mains(online), meta, CFG, T0 + m * MIN)
            prev, meta = {s["id"]: s for s in r.states}, {**r.meta, "sched_expected_off": expected_off}
            kinds += [e["kind"] for e in r.events]
        return kinds

    def test_glitch_outside_schedule_is_not_outage(self):
        kinds = self.sequence(4, expected_off=False)  # 4 хв — збій Zigbee
        self.assertNotIn("outage_start", kinds)
        self.assertIn("power_glitch", kinds)

    def test_long_outage_outside_schedule(self):
        kinds = self.sequence(12, expected_off=False)
        self.assertIn("outage_start", kinds)
        self.assertIn("outage_end", kinds)
        self.assertNotIn("power_glitch", kinds)

    def test_scheduled_outage_immediate(self):
        kinds = self.sequence(2, expected_off=True)
        self.assertIn("outage_start", kinds)


if __name__ == "__main__":
    unittest.main()
