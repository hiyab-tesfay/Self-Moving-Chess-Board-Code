import json
import unittest
import urllib.error
import urllib.request

from touch_companion import CompanionBridge, CompanionServer


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = CompanionBridge()
        self.bridge.publish({"phase": "human", "busy": False, "can_confirm": True, "choices": []})

    def test_duplicate_and_stale_confirmations_do_not_queue_another_move(self):
        revision = self.bridge.snapshot()["revision"]
        self.assertFalse(self.bridge.submit("confirm", revision - 1))
        self.assertTrue(self.bridge.submit("confirm", revision))
        self.assertFalse(self.bridge.submit("confirm", revision))
        self.assertEqual(self.bridge.next_action(), ("confirm", revision, None))
        self.assertIsNone(self.bridge.next_action())

    def test_busy_disabled_and_unknown_actions_are_refused(self):
        for state in ({"busy": True, "can_confirm": True}, {"busy": False, "can_confirm": False}):
            self.bridge.publish(state)
            self.assertFalse(self.bridge.submit("confirm", self.bridge.snapshot()["revision"]))
        self.assertFalse(self.bridge.submit("move", self.bridge.snapshot()["revision"]))

    def test_only_the_offered_promotion_can_be_selected(self):
        self.bridge.publish({"busy": False, "choices": [{"uci": "a7a8q", "label": "Queen"}]})
        revision = self.bridge.snapshot()["revision"]
        self.assertFalse(self.bridge.submit("choice", revision, "e2e4"))
        self.assertTrue(self.bridge.submit("choice", revision, "a7a8q"))

    def test_stop_bypasses_busy_and_pending_confirmation(self):
        self.bridge.submit("confirm", self.bridge.snapshot()["revision"])
        self.assertTrue(self.bridge.submit("stop", -1))
        self.assertTrue(self.bridge.stop.is_set())


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = CompanionServer(0)
        self.addCleanup(self.server.close)
        self.base = f"http://127.0.0.1:{self.server.http.server_port}"
        self.server.bridge.publish({"busy": False, "can_confirm": True})

    def request(self, path, body, origin=None, kind="application/json"):
        headers = {"Content-Type": kind}
        if origin:
            headers["Origin"] = origin
        return urllib.request.urlopen(urllib.request.Request(self.base + path,
                                      json.dumps(body).encode(), headers), timeout=3)

    def test_page_state_and_one_confirm_request(self):
        with urllib.request.urlopen(self.base, timeout=3) as page:
            self.assertIn(b"Confirm move", page.read())
        revision = self.server.bridge.snapshot()["revision"]
        with self.request("/api/confirm", {"revision": revision}, self.base) as response:
            self.assertEqual(response.status, 202)
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.request("/api/confirm", {"revision": revision}, self.base)
        self.assertEqual(rejected.exception.code, 409)
        self.assertEqual(self.server.http.server_address[0], "127.0.0.1")

    def test_other_websites_and_plain_form_posts_cannot_trigger_motion(self):
        for origin, kind in (("https://untrusted.example", "application/json"),
                             (None, "text/plain")):
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request("/api/confirm", {"revision": 1}, origin, kind)
            self.assertEqual(rejected.exception.code, 403)
        self.assertIsNone(self.server.bridge.next_action())

    def test_invalid_json_shape_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.request("/api/confirm", ["not", "an", "object"])
        self.assertEqual(rejected.exception.code, 400)

    def test_unrecognized_host_is_rejected_even_with_matching_origin(self):
        request = urllib.request.Request(self.base + "/api/stop", b'{"revision":1}',
                                         {"Content-Type": "application/json", "Host": "rebound.example",
                                          "Origin": "http://rebound.example"})
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(rejected.exception.code, 403)
        self.assertFalse(self.server.bridge.stop.is_set())


if __name__ == "__main__":
    unittest.main()
