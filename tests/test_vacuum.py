import os
import unittest

from helpers import CFG, T0

import bot
import vacuum_bot as vb

ROOMS = [{"id": 1, "name": "Кухня"}, {"id": 2, "name": "Коридор 2"}, {"id": 3, "name": "Спальня"},
         {"id": 4, "name": "Коридор"}, {"id": 5, "name": "Вітальня"}]


class FakeStore:
    def __init__(self, users, vacuum=None):
        self.meta = {"tg_users": users, "vacuum": vacuum or {"name": "Кузя", "rooms": ROOMS, "state": "charging_completed",
                                                             "battery": 100, "updated": T0}}

    def get_meta(self, k):
        return self.meta.get(k)

    def put_meta(self, k, v):
        self.meta[k] = v


class FakeNotifier:
    def __init__(self):
        self.calls = []

    def call(self, method, payload=None, retries=1):
        self.calls.append((method, payload))
        return True, {"result": {"message_id": 77}}


class Names(unittest.TestCase):
    def test_room_names(self):
        self.assertEqual(vb.room_name("Kitchen", 1), "Кухня")
        self.assertEqual(vb.room_name("Corridor 2", 2), "Коридор 2")
        self.assertEqual(vb.room_name("Primary Bedroom", 3), "Спальня")
        self.assertEqual(vb.room_name("Kitchen", 1, {"vacuum": {"room_names": {"1": "Кухня-їдальня"}}}), "Кухня-їдальня")
        self.assertEqual(vb.room_name("Sauna", 9), "Sauna")
        self.assertEqual(vb.room_name(None, 9), "Кімната 9")

    def test_status_text(self):
        t = vb.status_text({"name": "Кузя", "state": "charging_completed", "battery": 100, "mode": "mopping_after_sweeping",
                            "suction": "turbo", "updated": T0}, CFG, T0)
        self.assertIn("🅿️ На базі, заряджено · 🔋 100 %", t)
        self.assertIn("Спочатку пилосос", t)
        self.assertIn("турбо", t)


class Wizard(unittest.TestCase):
    def test_plan(self):
        self.assertEqual(vb.plan_ids({"kind": "all"}, ROOMS), [1, 2, 3, 4, 5])
        self.assertEqual(vb.plan_ids({"kind": "ex", "rooms": [3]}, ROOMS), [1, 2, 4, 5])
        self.assertEqual(vb.plan_ids({"kind": "sel", "rooms": [5, 1]}, ROOMS), [1, 5])  # порядок — як на карті

    def test_steps_except(self):
        d, view, _ = vb.step(None, "k:ex", ROOMS)
        self.assertIn("ПРОПУСТИТИ", view[0])
        d, view, _ = vb.step(d, "r:3", ROOMS)
        self.assertIn("✅ Спальня", str(view[1]))
        d, view, _ = vb.step(d, "rn", ROOMS)
        self.assertIn("Кухня, Коридор 2, Коридор, Вітальня", view[0])
        d, view, _ = vb.step(d, "t:2", ROOMS)
        d, view, _ = vb.step(d, "m:after", ROOMS)
        self.assertIn("2×", view[0])
        self.assertIn("▶️ Почати", str(view[1]))

    def test_cannot_skip_all_or_select_none(self):
        d, _, _ = vb.step(None, "k:sel", ROOMS)
        _, view, note = vb.step(d, "rn", ROOMS)
        self.assertIsNone(view)
        self.assertIn("хоча б одну", note)
        d, _, _ = vb.step(None, "k:ex", ROOMS)
        for r in ROOMS:
            d, _, _ = vb.step(d, f"r:{r['id']}", ROOMS)
        _, view, note = vb.step(d, "rn", ROOMS)
        self.assertIsNone(view)

    def test_back_steps(self):
        d, _, _ = vb.step(None, "k:sel", ROOMS)
        for x in ("r:1", "rn", "t:2", "m:mop"):
            d, _, _ = vb.step(d, x, ROOMS)
        d, view, _ = vb.step(d, "b", ROOMS)          # підтвердження → режим
        self.assertIn("Режим?", view[0])
        d, view, _ = vb.step(d, "b", ROOMS)          # режим → скільки разів
        self.assertIn("Скільки разів", view[0])
        d, view, _ = vb.step(d, "b", ROOMS)          # → кімнати (позначки збережено)
        self.assertIn("✅ Кухня", str(view[1]))
        d, view, note = vb.step(d, "b", ROOMS)       # → головне меню
        self.assertEqual((view, note), (None, "main"))

    def test_main_menu_depends_on_state(self):
        docked = vb.main_markup({"state": "charging_completed", "docked": True})
        self.assertEqual(docked["inline_keyboard"][0][0]["text"], "🏠 Усі кімнати")
        self.assertNotIn("На базу", str(docked))
        cleaning = vb.main_markup({"state": "sweeping", "docked": False, "active": {"rooms": [1]}})
        self.assertIn("На базу", cleaning["inline_keyboard"][0][0]["text"])

    def test_times_clamped(self):
        d, _, _ = vb.step(None, "k:all", ROOMS)
        d, _, _ = vb.step(d, "t:9", ROOMS)
        self.assertEqual(d["times"], 3)


class Callbacks(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.orig = vb.invoke
        vb.invoke = lambda p: self.sent.append(p)
        os.environ["VACUUM_FN"] = "test-vacuum"

    def tearDown(self):
        vb.invoke = self.orig
        os.environ.pop("VACUUM_FN", None)

    def press(self, store, data, uid="1"):
        cb = {"id": "c", "data": f"vc:{data}", "from": {"id": int(uid)}, "message": {"message_id": 5}}
        return bot.handle_callback(cb, store, FakeNotifier(), {}, CFG)

    def test_full_flow_invokes_clean(self):
        store = FakeStore({"1": {"name": "Сергій", "status": "approved", "access": "home"}})
        for d in ("k:sel", "r:1", "r:3", "rn", "t:2", "m:both", "go"):
            self.press(store, d)
        self.assertEqual(self.sent, [{"op": "clean", "chat": "1", "msg": 5, "by": "Сергій", "rooms": [1, 3],
                                      "times": 2, "mode": "both"}])
        self.assertNotIn("1", store.get_meta("vacuum_draft"))

    def test_home_is_immediate(self):
        store = FakeStore({"1": {"name": "Сергій", "status": "approved", "access": "home"}})
        self.press(store, "home")
        self.assertEqual(self.sent[0]["op"], "home")

    def test_guest_denied(self):
        store = FakeStore({"2": {"name": "Гість", "status": "approved", "access": "guest"}})
        self.assertEqual(self.press(store, "home", uid="2"), "vacuum denied")
        self.assertEqual(self.sent, [])

    def test_button_only_for_home(self):
        self.assertIn("🤖 Пилосос", str(bot.KEYBOARD))
        self.assertNotIn("🤖 Пилосос", str(bot.GUEST_KEYBOARD))
        self.assertNotIn("vacuum", bot.GUEST_CMDS)


if __name__ == "__main__":
    unittest.main()
