import unittest

from helpers import CFG, DAY, HOUR, MIN, T0

import accuracy


class ScheduleAccuracy(unittest.TestCase):
    def test_compute(self):
        plan = [[T0 + 8 * HOUR, T0 + 12 * HOUR], [T0 + 18 * HOUR, T0 + 22 * HOUR], [T0 + DAY + 8 * HOUR, T0 + DAY + 12 * HOUR]]
        actual = [[T0 + 8 * HOUR + 5 * MIN, T0 + 12 * HOUR + 10 * MIN],      # +5 / +10 хв
                  [T0 + 18 * HOUR + 7 * MIN, T0 + 22 * HOUR + 14 * MIN],     # +7 / +14 хв
                  [T0 + 14 * HOUR, T0 + 15 * HOUR],                          # поза графіком
                  [T0 + 16 * HOUR, T0 + 16 * HOUR + 2 * MIN]]                # збій — не рахуємо
        r = accuracy.compute(actual, plan, unknown=[], now=T0 + 2 * DAY)
        self.assertEqual((r["matched"], r["emergency"], r["skipped"]), (2, 1, 1))
        self.assertEqual((r["start"], r["end"]), (6, 12))

    def test_unknown_period_not_counted_as_skipped(self):
        plan = [[T0 + 8 * HOUR, T0 + 12 * HOUR]]
        r = accuracy.compute([], plan, unknown=[[T0 + 7 * HOUR, T0 + 13 * HOUR]], now=T0 + DAY)
        self.assertEqual(r["skipped"], 0)

    def test_merge_across_midnight(self):
        docs = [{"off": [[T0 + 22 * HOUR, T0 + DAY]]}, {"off": [[T0 + DAY, T0 + DAY + 2 * HOUR]]}]
        self.assertEqual(accuracy.planned(docs), [[T0 + 22 * HOUR, T0 + DAY + 2 * HOUR]])

    def test_texts(self):
        acc = {"matched": 8, "emergency": 1, "skipped": 3, "start": 7, "end": 5, "days": 6}
        self.assertEqual(accuracy.summary_text(acc), "За 6 днів: зазвичай вимикають на 7 хв пізніше, вмикають на 5 хв "
                                                     "пізніше; 1 з 9 поза графіком, 3 рази за графіком не вимикали")
        self.assertEqual(accuracy.usual_end(acc, T0 + 18 * HOUR + 30 * MIN, CFG), "18:35")
        self.assertIsNone(accuracy.usual_end({**acc, "end": 2}, T0, CFG))
        self.assertIsNone(accuracy.summary_text({"matched": 1, "emergency": 0}))

    def test_plural(self):
        self.assertEqual([accuracy.plural(n, "раз", "рази", "разів") for n in (1, 2, 5, 11, 21, 22)],
                         ["1 раз", "2 рази", "5 разів", "11 разів", "21 раз", "22 рази"])


if __name__ == "__main__":
    unittest.main()
