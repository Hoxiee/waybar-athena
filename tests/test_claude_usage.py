"""Offline checks: no real credentials or network requests are used."""

import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "claude-usage.py"
spec = importlib.util.spec_from_file_location("claude_usage", SCRIPT)
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)


def sample(session=25, weekly=40, now=1700000000):
    def window(percent, seconds):
        return {"utilization": percent,
                "resets_at": datetime.fromtimestamp(now + seconds, timezone.utc).isoformat()}
    return {"five_hour": window(session, 7200), "seven_day": window(weekly, 86400)}


class FetchTests(unittest.TestCase):
    def credentials(self, expires=None):
        return {"claudeAiOauth": {"accessToken": "test-access-token",
                                 "refreshToken": "test-refresh-token",
                                 "expiresAt": expires or (time.time() + 3600) * 1000}}

    def test_missing_or_expired_login_never_calls_network(self):
        with patch.object(usage.urllib.request, "urlopen") as request:
            for creds in ({}, self.credentials(1)):
                with patch.object(usage, "read_creds", return_value=creds):
                    self.assertEqual(usage.fetch(), ("auth", None, None))
            request.assert_not_called()

    def test_fetch_sends_access_token_only_and_returns_usage(self):
        payload = sample()
        with patch.object(usage, "read_creds", return_value=self.credentials()), \
             patch.object(usage.urllib.request, "urlopen", return_value=io.StringIO(json.dumps(payload))) as request:
            status, data, retry = usage.fetch()
        self.assertEqual((status, data, retry), ("ok", payload, None))
        req = request.call_args.args[0]
        self.assertEqual(req.full_url, usage.URL)
        self.assertEqual(req.get_header("Authorization"), "Bearer test-access-token")
        self.assertIsNone(req.data)
        self.assertNotIn("test-refresh-token", str(req.headers))
        self.assertNotIn("test-access-token", json.dumps(data))

    def test_http_errors_and_retry_after(self):
        cases = [(401, {}, ("auth", None, None)),
                 (403, {}, ("auth", None, None)),
                 (429, {"Retry-After": "120"}, ("ratelimit", None, 120)),
                 (429, {"Retry-After": "invalid"}, ("ratelimit", None, usage.BACKOFF)),
                 (429, {"Retry-After": "1"}, ("ratelimit", None, 60)),
                 (500, {}, ("error", None, None))]
        for code, headers, expected in cases:
            with self.subTest(code=code, headers=headers), \
                 patch.object(usage, "read_creds", return_value=self.credentials()), \
                 patch.object(usage.urllib.request, "urlopen", side_effect=
                              urllib.error.HTTPError(usage.URL, code, "test", headers, None)):
                self.assertEqual(usage.fetch(), expected)

    def test_bad_json_is_recoverable(self):
        with patch.object(usage, "read_creds", return_value=self.credentials()), \
             patch.object(usage.urllib.request, "urlopen", return_value=io.StringIO("not json")):
            self.assertEqual(usage.fetch(), ("offline", None, None))


class RenderTests(unittest.TestCase):
    def test_thresholds_and_full_view(self):
        now = 1700000000
        for percent, expected in ((25, "normal"), (75, "warning"), (90, "critical"), (100, "limited")):
            with self.subTest(percent=percent):
                out = usage.render(sample(percent), "ok", now, {}, {}, now)
                self.assertEqual(out["class"], expected)
                self.assertEqual(out["percentage"], percent)
                json.dumps(out)
        out = usage.render(sample(), "ok", now, {"mode": "full"}, {}, now)
        self.assertIn("7d", out["text"])

    def test_cached_auth_failure_is_dimmed(self):
        now = 1700000000
        out = usage.render(sample(90), "auth", now - 100, {}, {}, now)
        self.assertEqual(out["class"], "auth")
        self.assertIn("login expired", out["tooltip"])

    def test_elapsed_window_is_no_longer_limited(self):
        now = 1700000000
        window = usage.Window({"utilization": 100, "resets_at":
                               datetime.fromtimestamp(now - 1, timezone.utc).isoformat()}, usage.SESSION, now)
        self.assertFalse(window.limited)
        self.assertEqual(window.util, 0)

    def test_tooltip_escapes_server_labels(self):
        now = 1700000000
        data = sample()
        data["seven_day_breakdown"] = {"rows": [{"display_name": "<Chats & Code>", "percent": 10}]}
        out = usage.render(data, "ok", now, {}, {}, now)
        self.assertIn("&lt;Chats &amp; Code&gt;", out["tooltip"])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.old_umask = os.umask(0o022)
        self.addCleanup(os.umask, self.old_umask)

    def test_runtime_directory_and_files_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "claude-usage"
            with patch.object(usage, "RUN", runtime):
                usage.prepare_runtime()
                usage.save(runtime / "usage.json", {"data": sample()})
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
            self.assertEqual((runtime / "usage.json").stat().st_mode & 0o777, 0o600)
            self.assertFalse((runtime / "usage.tmp").exists())

    def test_runtime_symlink_is_rejected_without_changing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir(mode=0o755)
            runtime = Path(directory) / "claude-usage"
            runtime.symlink_to(target, target_is_directory=True)
            with patch.object(usage, "RUN", runtime), self.assertRaises(PermissionError):
                usage.prepare_runtime()
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_daemon_streams_and_accepts_toggle_without_login(self):
        with tempfile.TemporaryDirectory() as directory:
            # Isolate the real user's login and runtime; no credentials means
            # fetch() returns auth before attempting any network request.
            env = dict(os.environ, CLAUDE_CONFIG_DIR=directory, XDG_RUNTIME_DIR=directory)
            proc = subprocess.Popen([sys.executable, "-u", str(SCRIPT)], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                import select
                self.assertTrue(select.select([proc.stdout], [], [], 5)[0], "no initial JSON output")
                first = json.loads(proc.stdout.readline())
                self.assertEqual(first["class"], "auth")
                proc.send_signal(signal.SIGUSR1)
                self.assertTrue(select.select([proc.stdout], [], [], 5)[0], "toggle did not wake the daemon")
                second = json.loads(proc.stdout.readline())
                self.assertEqual(second["class"], "auth")
                state = json.loads((Path(directory) / "claude-usage" / "state.json").read_text())
                self.assertEqual(state["mode"], "full")
            finally:
                proc.terminate()
                _, stderr = proc.communicate(timeout=5)
            self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
