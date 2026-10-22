"""Offline checks: no real credentials or network requests are used."""

import importlib.util
import io
import json
import os
import select
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

    def test_expired_locked_window_does_not_hold_auto_pause(self):
        now = 1700000000
        data = sample(100, now=now - 7200)
        data["five_hour"]["locked_reason"] = "limit reached"
        self.assertIsNone(usage.limit_reset(data, now))

    def test_auto_pause_uses_the_last_blocking_reset(self):
        now = 1700000000
        for session, weekly, expected in ((100, 40, now + 7200),
                                           (25, 100, now + 86400),
                                           (100, 100, now + 86400),
                                           (25, 40, None)):
            with self.subTest(session=session, weekly=weekly):
                self.assertEqual(usage.limit_reset(sample(session, weekly, now), now), expected)
        data = sample()
        data["five_hour"]["locked_reason"] = "limit reached"
        self.assertEqual(usage.limit_reset(data, now), now + 7200)

    def test_auto_pause_requires_a_reset_for_every_blocking_window(self):
        now = 1700000000
        for deadline in (None, "bad timestamp"):
            with self.subTest(deadline=deadline):
                data = sample(100, 100)
                data["five_hour"]["resets_at"] = deadline
                self.assertIsNone(usage.limit_reset(data, now))

    def test_auto_pause_shows_resume_time_and_local_countdown(self):
        fetched = 1700000000
        data = sample(100, 100, now=fetched)
        now = fetched + 600
        out = usage.render(data, "ok", fetched, {}, {}, now)
        self.assertEqual(out["class"], "limited")
        self.assertIn(usage.clock(fetched + 86400, now), out["text"])
        self.assertIn("Auto pause", out["tooltip"])
        self.assertIn("Resumes at", out["tooltip"])
        self.assertIn("in 23h 50m", out["tooltip"])
        self.assertIn("Right-click to check now", out["tooltip"])
        self.assertNotIn("At this pace", out["tooltip"])

    def test_tooltip_escapes_server_labels(self):
        now = 1700000000
        data = sample()
        data["seven_day_breakdown"] = {"rows": [{"display_name": "<Chats & Code>", "percent": 10}]}
        out = usage.render(data, "ok", now, {}, {}, now)
        self.assertIn("&lt;Chats &amp; Code&gt;", out["tooltip"])


    def test_pause_keeps_claude_identity_with_or_without_cached_data(self):
        now = 1700000000
        for data, status in ((None, "auth"), (sample(100), "ok")):
            with self.subTest(status=status):
                out = usage.render(data, status, now, {"paused": True}, {}, now)
                self.assertEqual(out["class"], "paused")
                self.assertEqual(out["alt"], "paused")
                self.assertIn("asterisk", out["text"])
                self.assertIn("Paused", out["text"])
                self.assertIn("Manual pause", out["tooltip"])
                self.assertIn("No new usage requests", out["tooltip"])
                self.assertIn("Middle  resume", out["tooltip"])
                self.assertNotIn("Resumes at", out["tooltip"])
                if data:
                    self.assertIn(usage.clock(now + 7200, now), out["text"])
                    self.assertIn("Polling stays paused after the reset", out["tooltip"])
                else:
                    self.assertIn("Reset time is unknown", out["tooltip"])

    def test_paused_snapshot_does_not_invent_a_reset_or_project_usage(self):
        fetched = 1700000000
        now = fetched + 3 * 3600
        data = sample(100, now=fetched)
        data["seven_day_opus"] = dict(data["five_hour"])
        out = usage.render(data, "ok", fetched, {"paused": True}, {}, now)
        self.assertEqual(out["percentage"], 100)
        self.assertIn("Values below are from the last fetch", out["tooltip"])
        self.assertIn("Opus · weekly</b>  100%", out["tooltip"])
        self.assertNotIn("No active window", out["tooltip"])
        self.assertNotIn("in 0m", out["tooltip"])
        self.assertNotIn("At this pace", out["tooltip"])
        self.assertNotIn("On pace", out["tooltip"])
        self.assertIn("Reset was scheduled for", out["tooltip"])


class PollingTests(unittest.TestCase):
    def start_monitor(self, directory, paused=False, retry=None, pause_during_fetch=False,
                      cached=None, responses=None):
        runtime = Path(directory) / "claude-usage"
        runtime.mkdir(exist_ok=True)
        if paused:
            (runtime / "state.json").write_text(json.dumps({"paused": True}))
        if cached is not None:
            (runtime / "usage.json").write_text(json.dumps(cached))
        if responses is not None:
            (Path(directory) / "responses").write_text(json.dumps(responses))
        # Replace the entire fetch function in an isolated child. Every
        # attempted poll is counted without touching credentials or the network.
        code = """
import importlib.util
import json
import os
import signal
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('usage', sys.argv[1])
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)
counter = Path(sys.argv[2]) / 'calls'
retry = None if sys.argv[3] == 'None' else int(sys.argv[3])
pause_during_fetch = sys.argv[4] == 'True'
responses_path = Path(sys.argv[2]) / 'responses'
responses = json.loads(responses_path.read_text()) if responses_path.exists() else []
def fetch():
    with counter.open('a') as out:
        out.write('fetch\\n')
    if pause_during_fetch:
        os.kill(os.getpid(), signal.SIGRTMIN)
    if retry is not None:
        return 'ratelimit', None, retry
    if responses:
        return responses.pop(0) if len(responses) > 1 else responses[0]
    return 'ok', {'five_hour': {'utilization': 25}}, None
usage.fetch = fetch
usage.notify = lambda *args, **kwargs: None
sys.argv = [str(usage.HERE / 'claude-usage.py')]
usage.main()
"""
        env = dict(os.environ, CLAUDE_CONFIG_DIR=directory, XDG_RUNTIME_DIR=directory)
        proc = subprocess.Popen([sys.executable, "-u", "-c", code, str(SCRIPT), directory,
                                 str(retry), str(pause_during_fetch)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(self.stop_monitor, proc)
        return proc, env, runtime

    def stop_monitor(self, proc):
        if proc.stdout.closed and proc.stderr.closed:
            return
        if proc.poll() is None:
            proc.terminate()
        _, stderr = proc.communicate(timeout=5)
        self.assertEqual(stderr, b"")

    def output(self, proc):
        self.assertTrue(select.select([proc.stdout], [], [], 5)[0], "no JSON output")
        line = proc.stdout.readline()
        self.assertTrue(line, "monitor exited before emitting JSON")
        return json.loads(line)

    def control(self, proc, env, option):
        subprocess.run([sys.executable, str(SCRIPT), option], env=env,
                       check=True, capture_output=True, timeout=5)
        return self.output(proc)

    def calls(self, directory):
        path = Path(directory) / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def test_pause_blocks_due_and_manual_fetches_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, env, runtime = self.start_monitor(directory, paused=True)
            self.assertEqual(self.output(proc)["class"], "paused")
            self.assertEqual(self.control(proc, env, "--refresh")["class"], "paused")
            self.control(proc, env, "--toggle")
            self.assertEqual(self.calls(directory), [])
            state = json.loads((runtime / "state.json").read_text())
            self.assertTrue(state["paused"])
            self.assertEqual(state["mode"], "full")

            self.stop_monitor(proc)
            proc, env, _ = self.start_monitor(directory)
            self.assertEqual(self.output(proc)["class"], "paused")
            self.assertEqual(self.calls(directory), [])
            self.assertEqual(self.control(proc, env, "--pause")["class"], "normal")
            self.assertEqual(len(self.calls(directory)), 1)
            self.assertFalse(json.loads((runtime / "state.json").read_text())["paused"])
            self.assertEqual(self.control(proc, env, "--pause")["class"], "paused")
            self.control(proc, env, "--refresh")
            self.assertEqual(len(self.calls(directory)), 1)

    def test_resume_and_manual_refresh_respect_rate_limit_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, env, _ = self.start_monitor(directory, retry=600)
            self.output(proc)
            self.assertEqual(len(self.calls(directory)), 1)
            self.assertEqual(self.control(proc, env, "--pause")["class"], "paused")
            self.control(proc, env, "--refresh")
            self.control(proc, env, "--pause")
            self.control(proc, env, "--refresh")
            self.assertEqual(len(self.calls(directory)), 1)

    def test_pause_signal_during_fetch_is_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, env, _ = self.start_monitor(directory, pause_during_fetch=True)
            self.output(proc)
            self.assertEqual(self.output(proc)["class"], "paused")
            self.control(proc, env, "--refresh")
            self.assertEqual(len(self.calls(directory)), 1)

    def test_cached_limit_skips_due_poll_and_restores_pause_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            now = time.time()
            data = sample(100, now=now)
            cached = {"data": data, "fetched": now - usage.FETCH - 10}
            proc, env, _ = self.start_monitor(directory, cached=cached)
            self.assertEqual(self.output(proc)["class"], "limited")
            self.control(proc, env, "--toggle")
            self.assertEqual(self.calls(directory), [])

            self.stop_monitor(proc)
            proc, env, _ = self.start_monitor(directory)
            self.assertEqual(self.output(proc)["class"], "limited")
            self.assertEqual(self.calls(directory), [])
            self.assertEqual(self.control(proc, env, "--pause")["class"], "paused")
            self.control(proc, env, "--refresh")
            self.assertEqual(self.calls(directory), [])
            self.assertEqual(self.control(proc, env, "--pause")["class"], "limited")
            self.assertEqual(self.calls(directory), [])
            self.assertEqual(self.control(proc, env, "--refresh")["class"], "normal")
            self.assertEqual(len(self.calls(directory)), 1)

    def test_fresh_limit_resumes_at_reset_before_normal_fetch_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            now = time.time()
            data = sample(100, now=now)
            data["five_hour"]["resets_at"] = datetime.fromtimestamp(now + 2, timezone.utc).isoformat()
            responses = [("ok", data, None), ("ok", sample(now=now + 2), None)]
            proc, _, _ = self.start_monitor(directory, responses=responses)
            self.assertEqual(self.output(proc)["class"], "limited")
            self.assertEqual(len(self.calls(directory)), 1)
            # A minute-boundary render may arrive before the reset fetch.
            out = self.output(proc)
            while out["class"] == "limited" and time.time() < now + 5:
                out = self.output(proc)
            self.assertEqual(out["class"], "normal")
            self.assertEqual(len(self.calls(directory)), 2)

    def test_manual_check_replaces_auto_pause_deadline_with_latest_response(self):
        with tempfile.TemporaryDirectory() as directory:
            now = time.time()
            cached = {"data": sample(100, now=now), "fetched": now - usage.FETCH - 10}
            fresh = sample(100, 100, now=now)
            proc, env, runtime = self.start_monitor(directory, cached=cached,
                                                     responses=[("ok", fresh, None)])
            self.output(proc)
            out = self.control(proc, env, "--refresh")
            self.assertIn(usage.clock(now + 86400, now), out["text"])
            self.control(proc, env, "--toggle")
            self.assertEqual(len(self.calls(directory)), 1)
            self.assertEqual(json.loads((runtime / "usage.json").read_text())["data"], fresh)

    def test_manual_pause_stays_paused_past_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            now = time.time()
            cached = {"data": sample(100, now=now - 7201), "fetched": now - 7201}
            proc, env, _ = self.start_monitor(directory, paused=True, cached=cached)
            self.assertEqual(self.output(proc)["class"], "paused")
            self.control(proc, env, "--refresh")
            self.assertEqual(self.calls(directory), [])
            self.assertEqual(self.control(proc, env, "--pause")["class"], "normal")
            self.assertEqual(len(self.calls(directory)), 1)


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
