"""Phone notifications (Web Push): VAPID ES256 signature in pure Python, who gets told and when."""
import base64
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gui"))
sys.path.insert(0, str(ROOT / "tests"))
import server as sw  # noqa: E402
import test_gui_server as base  # noqa: E402


class P256(unittest.TestCase):
    def test_public_key_of_the_rfc6979_test_key(self):
        d = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
        pub = sw.p256_public(d)
        self.assertEqual(pub[1:33].hex().upper(), "60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6")
        self.assertEqual(pub[33:].hex().upper(), "7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299")

    def test_sign_and_verify(self):
        d = 12345678901234567890
        pub = sw.p256_public(d)
        sig = sw.es256_sign(d, b"hello")
        self.assertTrue(sw.es256_verify(pub, b"hello", sig))
        self.assertFalse(sw.es256_verify(pub, b"hellO", sig))

    def test_openssl_accepts_the_signature(self):
        if not sw.which("openssl"):
            self.skipTest("no openssl")
        d = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
        pub, msg = sw.p256_public(d), b"vapid header.claims"
        sig = sw.es256_sign(d, msg)
        # SubjectPublicKeyInfo for a P-256 key + DER signature
        spki = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200") + pub

        def der_int(b):
            b = b.lstrip(b"\0") or b"\0"
            if b[0] & 0x80:
                b = b"\0" + b
            return b"\x02" + bytes([len(b)]) + b
        body = der_int(sig[:32]) + der_int(sig[32:])
        der = b"\x30" + bytes([len(body)]) + body
        with tempfile.TemporaryDirectory() as t:
            Path(t, "k.pem").write_text("-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(spki).decode() + "\n-----END PUBLIC KEY-----\n")
            Path(t, "m").write_bytes(msg)
            Path(t, "s").write_bytes(der)
            r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", "k.pem", "-signature", "s", "m"], cwd=t, capture_output=True, text=True)
        self.assertIn("Verified OK", r.stdout, r.stderr)

    def test_vapid_header(self):
        with tempfile.TemporaryDirectory() as t:
            p = sw.Pusher(Path(t) / "vapid.json")
            k = p.public()
            self.assertEqual(sw.Pusher(Path(t) / "vapid.json").public(), k, "the key is kept")
            a = p.auth("https://fcm.googleapis.com/fcm/send/abc")
            tok = a.split("t=")[1].split(",")[0]
            head, claims, sig = tok.split(".")
            c = json.loads(sw.b64u_dec(claims))
            self.assertEqual(c["aud"], "https://fcm.googleapis.com")
            self.assertGreater(c["exp"], time.time())
            self.assertTrue(sw.es256_verify(sw.b64u_dec(k), (head + "." + claims).encode(), sw.b64u_dec(sig)))
            self.assertTrue(a.endswith("k=" + k))

    def test_only_real_push_services(self):
        self.assertTrue(sw.push_endpoint_ok("https://fcm.googleapis.com/fcm/send/x"))
        self.assertTrue(sw.push_endpoint_ok("https://updates.push.services.mozilla.com/wpush/v2/x"))
        self.assertTrue(sw.push_endpoint_ok("https://web.push.apple.com/x"))
        for bad in ("http://fcm.googleapis.com/x", "https://127.0.0.1/x", "https://evil.com/fcm.googleapis.com", "https://xfcm.googleapis.com/", None):
            self.assertFalse(sw.push_endpoint_ok(bad), bad)


class Notifications(base.TmpHome):
    EP = "https://fcm.googleapis.com/fcm/send/dev1"

    def setUp(self):
        super().setUp()
        self.app = sw.App(self.store, sw.ServerConf("esi", "h"), web=True)
        self.app.pusher = sw.Pusher(Path(self.tmp.name) / "vapid.json")
        self.sent = []
        self.app.pusher.send = lambda ep: self.sent.append(ep) or 201

    def chat(self):
        c = sw.ChatSession("aaaaaaaaaaaa", {"make_argv": lambda *a: ["true"], "cwd": "/tmp", "title": "Fix login page",
                                            "spec": {"kind": "chat", "project": "shop", "where": "server"}})
        c.on_notify = self.app.push_notify
        return c

    def wait_sent(self, n):
        end = time.time() + 3
        while len(self.sent) < n and time.time() < end:
            time.sleep(0.02)

    def test_done_and_needs_you_reach_the_phone_when_nobody_looks(self):
        self.assertEqual(self.app.push_info()["devices"], 0)
        with self.assertRaises(sw.ApiError):
            self.app.push_subscribe({"subscription": {"endpoint": "https://evil.example/x"}})
        self.app.push_subscribe({"subscription": {"endpoint": self.EP, "keys": {}}, "device": "Android phone"})
        self.assertEqual(self.app.push_info()["devices"], 1)
        c = self.chat()
        c.busy = True
        c._handle_event({"type": "result", "subtype": "success", "result": "ok"})
        self.wait_sent(1)
        self.assertEqual(self.sent, [self.EP])
        c._handle_event({"type": "control_request", "request_id": "r1", "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}}})
        c._handle_event({"type": "control_request", "request_id": "r1", "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}}})
        self.wait_sent(2)
        self.assertEqual(len(self.sent), 2, "the same question once")
        msgs = self.app.push_pending({"endpoint": self.EP})["messages"]
        self.assertEqual([(m["title"], m["body"], m["tab"]) for m in msgs],
                         [("Claude finished", "Fix login page · shop", "aaaaaaaaaaaa"), ("Claude needs you", "Fix login page · shop — Bash", "aaaaaaaaaaaa")])
        self.assertEqual(self.app.push_pending({"endpoint": self.EP})["messages"], [], "each shown once")

    def test_quiet_while_a_page_is_on_screen_or_right_after_a_reattach(self):
        self.app.push_subscribe({"subscription": {"endpoint": self.EP}})
        c = self.chat()
        self.app.last_visible = time.time()
        c.busy = True
        c._handle_event({"type": "result"})
        self.app.last_visible = 0
        c._linked_at = time.time()                   # replayed right after attaching: not new
        c.busy = True
        c._handle_event({"type": "result"})
        c._linked_at = 0
        c.busy = False                               # a result without a turn we saw running
        c._handle_event({"type": "result"})
        time.sleep(0.2)
        self.assertEqual(self.sent, [])

    def test_a_gone_device_is_forgotten(self):
        self.app.push_subscribe({"subscription": {"endpoint": self.EP}})
        self.app.pusher.send = lambda ep: 410
        self.app._push_send(self.app._push_subs())
        self.assertEqual(self.app.push_info()["devices"], 0)
        self.app.push_subscribe({"subscription": {"endpoint": self.EP}})
        self.app.push_unsubscribe({"endpoint": self.EP})
        self.assertEqual(self.app.push_info()["devices"], 0)

    def test_installed_app_has_no_push(self):
        self.assertEqual(sw.App(self.store, sw.ServerConf("esi", "h")).push_info(), {"available": False})


if __name__ == "__main__":
    unittest.main()
