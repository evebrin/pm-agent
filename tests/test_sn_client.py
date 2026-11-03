"""Offline checks for sn_client error handling (no PDI needed): python -m unittest discover tests"""
import os
import unittest
from unittest import mock

import requests

import sn_client
from sn_client import AuthError, InstanceUnavailable, SNClient


def fake_response(status, ctype="application/json", body='{"result": []}'):
    r = requests.Response()
    r.status_code = status
    r.headers["Content-Type"] = ctype
    r._content = body.encode()
    return r


@mock.patch.dict(os.environ, {"SN_INSTANCE": "https://dev0.service-now.com", "SN_USER": "u", "SN_PASSWORD": "p"})
@mock.patch.object(sn_client.time, "sleep")  # skip real backoff waits
class TestErrors(unittest.TestCase):
    def client(self, responses):
        c = SNClient()
        c.session.request = mock.Mock(side_effect=responses)
        return c

    def test_retries_then_succeeds(self, sleep):
        c = self.client([fake_response(503), fake_response(429), fake_response(200)])
        self.assertEqual(c.get("incident"), [])
        self.assertEqual(c.session.request.call_count, 3)

    def test_gives_up_after_three_retries(self, sleep):
        c = self.client([fake_response(503)] * 4)
        with self.assertRaises(InstanceUnavailable):
            c.get("incident")
        self.assertEqual(c.session.request.call_count, 4)  # 1 try + 3 retries
        self.assertEqual([call.args[0] for call in sleep.call_args_list if call.args[0] >= 1], [1, 2, 4])

    def test_hibernation_html_page(self, sleep):
        c = self.client([fake_response(200, "text/html", "<html>Your instance is hibernating</html>")])
        with self.assertRaisesRegex(InstanceUnavailable, "hibernating"):
            c.get("incident")

    def test_redirect_to_wake_page(self, sleep):
        c = self.client([fake_response(302, "text/html", "")])
        with self.assertRaises(InstanceUnavailable):
            c.get("incident")

    def test_connection_error(self, sleep):
        c = self.client([requests.ConnectionError()] * 4)
        with self.assertRaisesRegex(InstanceUnavailable, "hibernating"):
            c.get("incident")

    def test_auth_error(self, sleep):
        c = self.client([fake_response(401)])
        with self.assertRaises(AuthError):
            c.get("incident")

    def test_dry_run_sends_nothing(self, sleep):
        c = self.client([])
        c.dry_run = True
        c.create("incident", {"short_description": "x"})
        c.session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
