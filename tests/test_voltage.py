import unittest

from helpers import CFG, T0, device

import rules


class Voltage(unittest.TestCase):
    def run_eval(self, meta):
        dev = device("k", "Кухня", [("cur_voltage", 154.0, "V")], category="cz")
        return [e for e in rules.evaluate({}, [dev], meta, CFG, T0).events if e["kind"] == "voltage"]

    def test_low_voltage_alert(self):
        self.assertEqual(len(self.run_eval({})), 1)

    def test_no_alert_during_outage(self):
        self.assertEqual(self.run_eval({"outage": {"start": T0 - 20 * 60_000}}), [])

    def test_journal_reading_before_outage(self):
        # 21.11: світло зникло о 11:39:05, розетка записала 154 В о 11:38:21, журнал прийшов о 12:01
        start = T0 + 11 * 3600_000 + 39 * 60_000 + 5_000
        reading = start - 44_000
        self.assertTrue(rules.voltage_in_outage(reading, {"outage": {"start": start}}, CFG))
        self.assertTrue(rules.voltage_in_outage(reading, {"last_outage": {"start": start, "end": start + 4 * 3600_000}}, CFG))
        self.assertFalse(rules.voltage_in_outage(start - 20 * 60_000, {"outage": {"start": start}}, CFG))
        self.assertFalse(rules.voltage_in_outage(reading, {}, CFG))


if __name__ == "__main__":
    unittest.main()
