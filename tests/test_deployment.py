from __future__ import annotations

import base64
import http.client
import os
from pathlib import Path
import re
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import app
from contracts import MAXIMUM_CONTRACT
from verifier import find_lean


TEST_PASSWORD = "test-only-access-password"


class DeploymentConfigTests(unittest.TestCase):
    def test_local_default_does_not_need_a_password(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(app.server_address(), ("127.0.0.1", 8765))

    def test_public_bind_requires_a_long_password(self):
        with patch.dict(os.environ, {"HOST": "0.0.0.0", "PORT": "10000"}, clear=True):
            with self.assertRaisesRegex(ValueError, "APP_ACCESS_PASSWORD"):
                app.server_address()
            os.environ["APP_ACCESS_PASSWORD"] = "short"
            with self.assertRaises(ValueError):
                app.server_address()
            os.environ["APP_ACCESS_PASSWORD"] = TEST_PASSWORD
            self.assertEqual(app.server_address(), ("0.0.0.0", 10000))

    def test_required_auth_also_protects_local_bind(self):
        with patch.dict(os.environ, {"APP_REQUIRE_AUTH": "1"}, clear=True):
            with self.assertRaises(ValueError):
                app.server_address()

    def test_lean_does_not_inherit_api_credentials(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake", "APP_ACCESS_PASSWORD": TEST_PASSWORD,
                                     "SOME_OTHER_SECRET": "fake", "PATH": "/usr/bin", "LEAN_CMD": "/test/lean"}):
            command, env = find_lean()
        self.assertEqual(command, "/test/lean")
        self.assertEqual(env["PATH"], "/usr/bin")
        for key in ("DEEPSEEK_API_KEY", "APP_ACCESS_PASSWORD", "SOME_OTHER_SECRET"):
            self.assertNotIn(key, env)


class DeploymentHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.thread = threading.Thread(target=lambda: cls.server.serve_forever(poll_interval=0.05), daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.env = patch.dict(os.environ, {"APP_ACCESS_PASSWORD": TEST_PASSWORD, "APP_REQUIRE_AUTH": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = patch("app.DeepSeekAgent", side_effect=AssertionError("Deployment tests must never call the real API"))
        self.api.start()
        self.addCleanup(self.api.stop)
        with app.PENDING_LOCK:
            app.BENCHMARK_JOBS.clear()
            app.VERIFY_JOBS.clear()
            app.PENDING_CONTRACTS.clear()
            app.FORMALIZING = False

    def request(self, method, path, form=None, *, auth=True, extra_headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        headers = {}
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(("guest:" + TEST_PASSWORD).encode()).decode()
        if extra_headers:
            headers.update(extra_headers)
        body = urlencode(form).encode() if form is not None else None
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def test_health_is_public_but_app_and_status_are_protected(self):
        self.assertEqual(self.request("GET", "/healthz", auth=False)[0], 200)
        for path in ("/", "/benchmark", "/comparison", "/verify/status?job=x", "/benchmark/status?job=x"):
            with self.subTest(path=path):
                status, headers, _ = self.request("GET", path, auth=False)
                self.assertEqual(status, 401)
                self.assertIn("WWW-Authenticate", headers)

    def test_authenticated_page_has_no_secrets_and_is_not_cached(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(TEST_PASSWORD, body)
        self.assertIn('name="form_token"', body)
        self.assertIn('formaction="/benchmark"', body)

    def test_invalid_auth_and_fail_closed_configuration(self):
        for credential in ("Basic invalid-base64!!", "Bearer fake", "Basic Z3Vlc3Q6d3Jvbmc="):
            self.assertEqual(self.request("GET", "/", extra_headers={"Authorization": credential})[0], 401)
        with patch.dict(os.environ, {"APP_ACCESS_PASSWORD": ""}):
            self.assertEqual(self.request("GET", "/", auth=False)[0], 503)

    def test_get_benchmark_never_starts_work(self):
        with patch("app.start_benchmark_job") as start, patch("app.run_all_cases") as run:
            self.assertEqual(self.request("GET", "/benchmark?mode=deepseek&attempts=until_success")[0], 200)
        start.assert_not_called()
        run.assert_not_called()

    def test_post_requires_authentication_and_form_token(self):
        self.assertEqual(self.request("POST", "/benchmark", {"mode": "deepseek"}, auth=False)[0], 401)
        for token in ("", "forged"):
            self.assertEqual(self.request("POST", "/benchmark", {"mode": "deepseek", "form_token": token})[0], 403)

    def test_benchmark_post_preserves_options_and_redirects_to_job(self):
        with patch("app.start_benchmark_job", return_value="job123") as start:
            status, headers, _ = self.request("POST", "/benchmark", {
                "form_token": app.FORM_TOKEN, "mode": "deepseek", "attempts": "5", "strategy": "staged",
            })
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/benchmark?job=job123")
        start.assert_called_once_with(5, "staged")

    def test_busy_verification_does_not_consume_confirmation(self):
        token = app.store_contract("spec", "demo", MAXIMUM_CONTRACT, 3)
        app.BENCHMARK_JOBS["other"] = {"status": "running"}
        status, _, body = self.request("POST", "/verify", {
            "form_token": app.FORM_TOKEN, "contract_token": token, "confirmed": "yes",
        })
        self.assertEqual(status, 429)
        self.assertIn("Another task is running", body)
        self.assertIn(token, app.PENDING_CONTRACTS)

    def test_thread_start_failure_does_not_leave_a_running_job(self):
        with patch("app.threading.Thread.start", side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError):
                app.start_benchmark_job(3, "raw")
        self.assertFalse(app.BENCHMARK_JOBS)

    def test_retained_state_is_bounded(self):
        for i in range(105):
            app.store_contract(str(i), "demo", MAXIMUM_CONTRACT, 3)
        self.assertEqual(len(app.PENDING_CONTRACTS), 100)
        app.VERIFY_JOBS.update({str(i): {"status": "done"} for i in range(12)})
        with app.PENDING_LOCK:
            app.check_web_capacity()
        self.assertLess(len(app.VERIFY_JOBS), 10)

    def test_real_demo_confirmation_and_verification(self):
        status, _, body = self.request("POST", "/formalize", {
            "form_token": app.FORM_TOKEN, "specification": app.DEFAULT_SPEC, "mode": "demo", "attempts": "3",
        })
        self.assertEqual(status, 200)
        token = re.search(r'name="contract_token" value="([^"]+)"', body).group(1)
        with patch("app.save_trace", return_value=Path("/test-trace-not-written")):
            status, headers, _ = self.request("POST", "/verify", {
                "form_token": app.FORM_TOKEN, "contract_token": token, "confirmed": "yes",
            })
            self.assertEqual(status, 303)
            job_id = headers["Location"].split("job=")[1]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                with app.PENDING_LOCK:
                    job = app.VERIFY_JOBS[job_id]
                    done = job["status"] != "running"
                if done:
                    break
                threading.Event().wait(0.02)
            self.assertEqual(job["status"], "done", job)
        result = job["result"]
        self.assertTrue(result.passed)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.api_calls, 0)
        status, _, body = self.request("GET", headers["Location"])
        self.assertEqual(status, 200)
        self.assertIn("PASS: Lean accepted", body)


if __name__ == "__main__":
    unittest.main()
