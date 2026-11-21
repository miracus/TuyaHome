import json
import unittest

import helpers  # noqa: F401  (шлях до app/)

import watchdog
import web


class Watchdog(unittest.TestCase):
    def test_alarm_and_recovery(self):
        alarm = {"AlarmName": "x-poller_silent", "AlarmDescription": "опитувач не запускався 10 хв",
                 "NewStateValue": "ALARM", "OldStateValue": "OK", "NewStateReason": "Threshold Crossed"}
        self.assertTrue(watchdog.format_message(json.dumps(alarm)).startswith("🚨 TuyaHome: опитувач не запускався"))
        ok = {**alarm, "NewStateValue": "OK", "OldStateValue": "ALARM"}
        self.assertIn("відновлено", watchdog.format_message(json.dumps(ok)))
        first = {**alarm, "NewStateValue": "OK", "OldStateValue": "INSUFFICIENT_DATA"}
        self.assertIsNone(watchdog.format_message(json.dumps(first)))

    def test_budget_plain_text(self):
        self.assertIn("бюджет AWS", watchdog.format_message("AWS Budget Notification ...", "Budget exceeded"))


class Pwa(unittest.TestCase):
    def test_static_without_key(self):
        r = web.handle(None, "/static/icon-192.png", {}, {}, {"me": "secret"})
        self.assertEqual(r["statusCode"], 200)
        self.assertTrue(r["isBase64Encoded"])
        self.assertEqual(web.handle(None, "/sw.js", {}, {}, {})["headers"]["Content-Type"].split(";")[0], "text/javascript")

    def test_manifest_needs_key(self):
        self.assertEqual(web.handle(None, "/manifest.webmanifest", {}, {}, {"me": "secret"})["statusCode"], 404)
        r = web.handle(None, "/manifest.webmanifest", {"k": "secret"}, {}, {"me": "secret"})
        m = json.loads(r["body"])
        self.assertEqual(m["start_url"], "/?k=secret")
        self.assertEqual(m["display"], "standalone")


if __name__ == "__main__":
    unittest.main()
