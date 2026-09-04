"""Check a locally published CI container. Uses only the scripted, no-API demo."""
from __future__ import annotations

import base64
import http.client
import json
import re
import sys
import time
from urllib.parse import urlencode, urlsplit


def smoke(base_url: str) -> None:
    target = urlsplit(base_url)
    if target.scheme != "http" or target.hostname != "127.0.0.1":
        raise ValueError("This CI smoke check only targets a localhost container.")
    credential = "Basic " + base64.b64encode(b"guest:test-only-access-password").decode()

    def request(method, path, form=None, auth=True):
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
        headers = {"Authorization": credential} if auth else {}
        body = urlencode(form).encode() if form is not None else None
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    deadline = time.monotonic() + 60
    while True:
        try:
            if request("GET", "/healthz", auth=False)[0] == 200:
                break
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Container did not become healthy within 60 seconds")
        time.sleep(0.5)

    assert request("GET", "/", auth=False)[0] == 401, "Anonymous access was not blocked"
    status, _, body = request("GET", "/")
    assert status == 200
    token = re.search(r'name="form_token" value="([^"]+)"', body).group(1)
    assert request("POST", "/formalize", {"mode": "demo"})[0] == 403
    status, _, body = request("POST", "/formalize", {
        "form_token": token, "mode": "demo", "attempts": "3",
        "specification": "Input: two integers a and b. Return their maximum. The result must be >= a and >= b, and equal a or b.",
    })
    assert status == 200, body
    contract = re.search(r'name="contract_token" value="([^"]+)"', body).group(1)
    status, headers, body = request("POST", "/verify", {
        "form_token": token, "contract_token": contract, "confirmed": "yes",
    })
    assert status == 303, body
    location = headers["Location"]
    job = location.split("job=")[1]
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status, _, body = request("GET", "/verify/status?job=" + job)
        assert status == 200, body
        progress = json.loads(body)
        if progress["status"] != "running":
            break
        time.sleep(0.5)
    assert progress["status"] == "done", progress
    status, _, body = request("GET", location)
    assert status == 200 and "PASS: Lean accepted" in body, body
    assert "Attempts to success: 2" in body, body
    assert "API calls: 0" in body, body
    print("Container smoke passed: healthy, authentication, form protection, real Lean FAIL -> PASS, zero API calls.")


if __name__ == "__main__":
    smoke(sys.argv[1])
