#!/usr/bin/env python3
"""Waybar custom/claude — Claude subscription usage, drawn in the bar's palette.

Runs as one long-lived process (modules/claude.jsonc has no "interval") and
prints a JSON line per render. Two clocks:
  - at every minute boundary it re-renders from what it already knows, so the
    reset countdown moves without touching the network;
  - every FETCH seconds it asks api.anthropic.com/api/oauth/usage — the endpoint
    behind Claude Code's own /usage — with the OAuth token Claude Code keeps in
    ~/.claude/.credentials.json.

It never refreshes that token. Refresh tokens rotate: spending one here would
leave the running Claude Code holding a dead one and log it out. When the access
token expires the pill dims ("auth") until Claude Code renews it.

Pace: the projection uses the burn rate over the last hour of samples, not the
window average, so a quiet morning does not hide a heavy last twenty minutes.

Colours are read from tokens/colors.css on every render, so the meter follows
matugen like the CSS does. Pango cannot see GTK colour tokens; this is the
workaround the hand-written modules do not have.

Control (wired in modules/claude.jsonc):
  claude-usage.py --toggle    compact <-> full view   (SIGUSR1 to the daemon)
  claude-usage.py --refresh   fetch now               (SIGUSR2 to the daemon)
"""

import argparse
import html
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKENS = HERE.parent / "tokens"
CREDENTIALS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / ".credentials.json"
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR")
RUN = (Path(RUNTIME_DIR) / "claude-usage" if RUNTIME_DIR
       else Path("/tmp") / f"claude-usage-{os.getuid()}")
PIDFILE = RUN / "pid"
CACHE = RUN / "usage.json"
STATE = RUN / "state.json"

URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_PAGE = "https://claude.ai/settings/usage"

FETCH = 180       # API period. The endpoint rate-limits; ai-usagebar used 300.
BACKOFF = 600     # after a 429 without Retry-After
MIN_MANUAL = 20   # a right-click storm still makes at most one call per 20 s
STALE = 900       # cached data older than this renders dimmed

SESSION = 5 * 3600
WEEK = 7 * 86400

WARN, CRIT = 75, 90
NOTIFY_AT = (80, 95)

BAR_CELLS = 8
TIP_CELLS = 26

# Same span as every module in modules/*.jsonc — keep the two in step.
ICON = "<span font='Material Symbols Rounded 21px' rise='-4pt'>{}</span>"
# Heavy box-drawing lines join into one continuous stroke in a monospace face,
# which is the closest Pango gets to an M3 linear progress indicator.
LINE = "<span font_family='JetBrains Mono' size='{size}'>{body}</span>"

FALLBACK = {
    "tertiary": "#c3c3eb", "error": "#ffb4ab", "on_surface": "#dee3e6",
    "on_error_container": "#ffdad6", "outline": "#8a9296", "state_warn": "#f9e2af",
}


# ── palette & drawing ──────────────────────────────────────────────────────

def palette():
    pal = dict(FALLBACK)
    for name in ("colors.css", "base.css"):
        try:
            text = (TOKENS / name).read_text()
        except OSError:
            continue
        pal.update(re.findall(r"@define-color\s+(\w+)\s+(#[0-9a-fA-F]{6})\b", text))
    return pal


def meter(pct, cells, colour, marker=None, marker_colour=None, size="12pt"):
    """Filled cells in `colour`, the track in the same colour at 28% — an M3
    track is a tint of its indicator, which also keeps it legible on whatever
    container the pill is sitting in. `marker` is a cell index drawn as a tick."""
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * cells)
    if pct > 0 and filled == 0:
        filled = 1
    runs = []
    for i in range(cells):
        if i == marker:
            key = ("╋", marker_colour, None)
        elif i < filled:
            key = ("━", colour, None)
        else:
            key = ("━", colour, "28%")
        if runs and runs[-1][0] == key:
            runs[-1][1] += 1
        else:
            runs.append([key, 1])
    body = ""
    for (ch, c, a), n in runs:
        alpha = f" fgalpha='{a}'" if a else ""
        body += f"<span foreground='{c}'{alpha}>{ch * n}</span>"
    return LINE.format(size=size, body=body)


def dim(text, alpha="65%"):
    return f"<span fgalpha='{alpha}'>{text}</span>"


def duration(sec):
    sec = max(0, int(sec))
    d, rest = divmod(sec, 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def clock(epoch, now):
    # Rounded: resets_at drifts by a second between fetches (18:29:59.9 one
    # time, 18:30:00.4 the next), which flipped the face between 21:29 and 21:30.
    t = datetime.fromtimestamp(round(epoch / 60) * 60)
    if epoch - now < 20 * 3600:
        return t.strftime("%H:%M")
    return t.strftime("%a %H:%M")


# ── data ───────────────────────────────────────────────────────────────────

def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class Window:
    def __init__(self, raw, length, now):
        raw = raw or {}
        self.util = float(raw.get("utilization") or 0)
        self.resets = parse_ts(raw.get("resets_at"))
        self.locked = raw.get("locked_reason")
        self.length = length
        if self.resets and self.resets <= now:
            # The window rolled over since the last fetch; the API would say 0.
            self.util, self.resets = 0.0, None
        self.left = (self.resets - now) if self.resets else None
        self.elapsed = (1 - self.left / length) if self.left is not None else 0.0
        self.elapsed = max(0.0, min(1.0, self.elapsed))

    @property
    def limited(self):
        return self.util >= 100 or bool(self.locked)


def burn_rate(samples, util, now, win):
    """%/s over the last hour of samples, or the window average if there is not
    ten minutes of history yet."""
    recent = [(t, u) for t, u in samples if now - t <= 3600]
    if recent and now - recent[0][0] >= 600:
        t0, u0 = recent[0]
        return max(0.0, (util - u0) / (now - t0))
    spent = win.elapsed * win.length
    return util / spent if spent >= 600 else 0.0


def plan_name(creds):
    oauth = creds.get("claudeAiOauth") or {}
    kind = (oauth.get("subscriptionType") or "").lower()
    tier = (oauth.get("rateLimitTier") or "").lower()
    if kind == "max":
        for mult in ("20x", "5x"):
            if mult in tier:
                return f"Claude Max {mult.replace('x', '×')}"
        return "Claude Max"
    return {"pro": "Claude Pro", "team": "Claude Team", "enterprise": "Claude Enterprise"}.get(
        kind, "Claude")


def read_creds():
    try:
        return json.loads(CREDENTIALS.read_text())
    except (OSError, ValueError):
        return {}


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(path, obj):
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(obj))
        tmp.replace(path)
    except OSError:
        pass


def fetch():
    """-> (status, data, retry_after). status: ok | auth | ratelimit | offline | error"""
    oauth = read_creds().get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return "auth", None, None
    if (oauth.get("expiresAt") or 0) / 1000 < time.time():
        return "auth", None, None
    req = urllib.request.Request(URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "waybar-claude-usage",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return "ok", json.load(resp), None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "auth", None, None
        if e.code == 429:
            try:
                retry = int(e.headers.get("Retry-After") or BACKOFF)
            except ValueError:
                retry = BACKOFF
            return "ratelimit", None, max(60, retry)
        return "error", None, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return "offline", None, None


# ── notifications ──────────────────────────────────────────────────────────

def notify(summary, body, urgency="normal"):
    try:
        subprocess.Popen(
            ["notify-send", "-a", "Claude", "-u", urgency, "-i", "dialog-information",
             summary, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def check_thresholds(state, name, win, now):
    """One notification per threshold per window, remembered across bar
    restarts (the state file lives in XDG_RUNTIME_DIR, so per boot)."""
    if not win.resets:
        return
    key = f"{name}:{int(win.resets // 60)}"
    sent = state.setdefault("notified", {}).setdefault(key, [])
    for level in NOTIFY_AT:
        if win.util >= level and level not in sent:
            sent.append(level)
            label = "session" if name == "session" else "weekly limit"
            notify(f"Claude · {win.util:.0f}% of {label} used",
                   f"Resets at {clock(win.resets, now)}, in {duration(win.left)}",
                   "critical" if level >= 95 else "normal")
    # Drop keys of windows that are long gone.
    live = {k for k in state["notified"] if int(k.split(":")[1]) * 60 > now - WEEK}
    state["notified"] = {k: v for k, v in state["notified"].items() if k in live}


# ── render ─────────────────────────────────────────────────────────────────

def render(data, status, fetched, state, creds, now):
    pal = palette()
    full = state.get("mode") == "full"

    if data is None:
        text = {"auth": "sign in", "offline": "offline", "ratelimit": "…"}.get(status, "—")
        tip = {
            "auth": "No valid Claude Code login.\nRun <b>claude</b> once; the pill recovers by itself.",
            "offline": "api.anthropic.com is unreachable.",
            "ratelimit": "Rate-limited by the usage endpoint; retrying later.",
        }.get(status, "Usage endpoint returned an error.")
        return {"text": f"{ICON.format('asterisk')}  {text}", "tooltip": tip,
                "class": status if status in ("auth", "error") else "stale",
                "alt": status}

    s = Window(data.get("five_hour"), SESSION, now)
    w = Window(data.get("seven_day"), WEEK, now)

    rate = burn_rate(state.get("samples", []), s.util, now, s)
    hit_at = now + (100 - s.util) / rate if rate > 0 and s.util < 100 else None
    runs_out = bool(hit_at and s.resets and hit_at < s.resets)

    severities = {(lim or {}).get("severity") for lim in data.get("limits") or []}
    peak = max(s.util, w.util)
    if s.limited or w.limited:
        cls = "limited"
    elif peak >= CRIT or "critical" in severities:
        cls = "critical"
    elif peak >= WARN or "warning" in severities or (runs_out and s.util >= 50):
        cls = "warning"
    else:
        cls = "normal"

    accent = {"normal": pal["tertiary"], "warning": pal["state_warn"],
              "critical": pal["error"], "limited": pal["on_error_container"]}[cls]

    age = now - fetched
    if status == "auth" or (age > STALE and status != "ok"):
        cls_out = "auth" if status == "auth" else "stale"
        accent = pal["outline"]
    else:
        cls_out = cls

    # ── bar face
    icon = ICON.format("asterisk")
    if cls == "limited":
        blocking = s if s.limited else w
        back = clock(blocking.resets, now) if blocking.resets else "—"
        text = f"{icon}  Limit  {dim('·')}  {back}"
    else:
        sess = f"{s.util:.0f}%  {meter(s.util, BAR_CELLS, accent)}"
        if s.left is not None:
            sess += f"  {dim(duration(s.left))}"
        parts = [f"{icon}  {sess}"]
        if full:
            wk = f"{dim('7d')} {w.util:.0f}%  {meter(w.util, BAR_CELLS, accent)}"
            if w.left is not None:
                wk += f"  {dim(duration(w.left))}"
            parts.append(wk)
        elif w.util >= 50:
            # The weekly cap only earns space on the face once it is the one
            # that might actually bind.
            parts.append(f"{dim('7d')} {w.util:.0f}%")
        text = f"  {dim('·')}  ".join(parts)

    # ── tooltip
    marker_c = pal["on_surface"]
    gap = "<span size='7pt'> </span>"
    lines = [f"<span size='12.5pt' weight='bold'>{html.escape(plan_name(creds))}</span>"
             f"   {dim('subscription usage')}", gap]

    def section(title, sub, win, extra):
        out = [f"<b>{title}</b>  {dim(sub)}",
               f"<span size='15pt' weight='bold'>{win.util:.0f}%</span> {dim('used')}",
               meter(win.util, TIP_CELLS, accent,
                     marker=min(TIP_CELLS - 1, int(win.elapsed * TIP_CELLS)) if win.resets else None,
                     marker_colour=marker_c)]
        if win.resets:
            out.append(dim(f"Resets {clock(win.resets, now)}  ·  in {duration(win.left)}"
                           f"  ·  {win.elapsed * 100:.0f}% of window elapsed"))
        else:
            out.append(dim("No active window — starts with the next message"))
        out += extra
        return out

    pace = []
    if s.resets and s.util < 100:
        if runs_out:
            pace.append(f"<span foreground='{pal['state_warn']}'>At this pace the limit hits at "
                        f"{clock(hit_at, now)}  ·  in {duration(hit_at - now)}</span>")
        elif rate > 0:
            projected = min(100.0, s.util + rate * s.left)
            pace.append(dim(f"On pace for ~{projected:.0f}% by the reset"))
    lines += section("Session", "5-hour window", s, pace)
    lines.append(gap)

    breakdown = [r for r in (data.get("seven_day_breakdown") or {}).get("rows") or []
                 if (r.get("percent") or 0) > 0]
    extra = []
    if breakdown:
        extra.append(dim("  ·  ".join(
            f"{html.escape(r.get('display_name') or r.get('key') or '?')} {r['percent']:.0f}%"
            for r in breakdown)))
    lines += section("Weekly", "7-day window", w, extra)

    for key, title in (("seven_day_opus", "Opus · weekly"), ("seven_day_sonnet", "Sonnet · weekly")):
        if data.get(key):
            win = Window(data[key], WEEK, now)
            lines += [gap, f"<b>{title}</b>  {win.util:.0f}%  {meter(win.util, 12, accent)}"]

    ex = data.get("extra_usage") or {}
    if ex.get("is_enabled"):
        dp = ex.get("decimal_places") or 2
        cur = "$" if (ex.get("currency") or "USD") == "USD" else f"{ex.get('currency')} "
        used = (ex.get("used_credits") or 0) / (10 ** dp)
        cap = ex.get("monthly_limit")
        cap_s = f" of {cur}{cap / 10 ** dp:.2f}" if cap else ""
        lines += [gap, f"<b>Extra usage</b>  {cur}{used:.2f}{cap_s} this month"]

    lines.append(gap)
    when = datetime.fromtimestamp(fetched).strftime("%H:%M")
    note = {"auth": "  ·  login expired, run claude", "ratelimit": "  ·  rate-limited",
            "offline": "  ·  offline", "error": "  ·  fetch failed"}.get(status, "")
    lines.append(dim(f"Updated {when}{note}", "55%"))
    lines.append(dim("Click  view  ·  Right-click  refresh  ·  Middle  usage page", "55%"))

    return {"text": text, "tooltip": "\n".join(lines), "class": cls_out,
            "alt": cls_out, "percentage": int(round(s.util))}


# ── main loop ──────────────────────────────────────────────────────────────

def signal_daemon(sig):
    try:
        os.kill(int(PIDFILE.read_text().strip()), sig)
    except (OSError, ValueError):
        pass


def prepare_runtime():
    # The /tmp fallback must not share state with other users. Refuse links or
    # foreign directories before touching their permissions or contents.
    os.umask(0o077)
    RUN.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RUN.is_symlink() or RUN.stat().st_uid != os.getuid():
        raise PermissionError(f"Unsafe runtime directory: {RUN}")
    RUN.chmod(0o700)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--toggle", action="store_true", help="toggle compact/full view")
    controls.add_argument("--refresh", action="store_true", help="request a fetch from the running monitor")
    controls.add_argument("--open", action="store_true", help="open the Claude usage page")
    args = parser.parse_args()
    if args.toggle:
        signal_daemon(signal.SIGUSR1)
        return
    if args.refresh:
        signal_daemon(signal.SIGUSR2)
        return
    if args.open:
        subprocess.Popen(["xdg-open", USAGE_PAGE], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return

    prepare_runtime()
    PIDFILE.write_text(str(os.getpid()))

    # Signals only set a flag; set_wakeup_fd turns them into a readable byte so
    # the select() below wakes immediately instead of at the next minute.
    rfd, wfd = os.pipe()
    os.set_blocking(wfd, False)
    signal.set_wakeup_fd(wfd)
    flags = set()
    signal.signal(signal.SIGUSR1, lambda *_: flags.add("toggle"))
    signal.signal(signal.SIGUSR2, lambda *_: flags.add("refresh"))

    state = load(STATE, {})
    cache = load(CACHE, {})
    data, fetched = cache.get("data"), cache.get("fetched", 0)
    status = "ok" if data else "error"
    next_fetch = fetched + FETCH
    last_manual = 0.0

    while True:
        now = time.time()

        manual = ("refresh" in flags and now - last_manual >= MIN_MANUAL
                  and (status != "ratelimit" or now >= next_fetch))
        if manual:
            last_manual = now
        if manual or now >= next_fetch:
            status, fresh, retry = fetch()
            now = time.time()
            if fresh is not None:
                data, fetched = fresh, now
                save(CACHE, {"data": data, "fetched": fetched})
                next_fetch = now + FETCH
            else:
                next_fetch = now + (retry or FETCH)

            if fresh is not None:
                s = Window(fresh.get("five_hour"), SESSION, now)
                key = int(s.resets // 60) if s.resets else None
                samples = state.get("samples", []) if state.get("samples_key") == key else []
                if samples and s.util < samples[-1][1] - 1:
                    samples = []
                samples = (samples + [[now, s.util]])[-60:]
                state.update(samples_key=key, samples=samples)

                was_limited = state.get("limited", False)
                is_limited = s.limited
                if was_limited and not is_limited:
                    notify("Claude is available again", "The session limit has reset.")
                state["limited"] = is_limited
                check_thresholds(state, "session", s, now)
                check_thresholds(state, "weekly", Window(fresh.get("seven_day"), WEEK, now), now)
                save(STATE, state)

        if "toggle" in flags:
            state["mode"] = "compact" if state.get("mode") == "full" else "full"
            save(STATE, state)
        flags.clear()

        try:
            out = render(data, status, fetched, state, read_creds(), now)
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            return  # Waybar went away.

        # Wake at the next minute boundary (the countdown only shows minutes),
        # or at the next fetch, whichever is first.
        now = time.time()
        timeout = min(60 - now % 60 + 0.2, max(1.0, next_fetch - now))
        try:
            ready, _, _ = select.select([rfd], [], [], timeout)
        except InterruptedError:
            ready = [rfd]
        if ready:
            try:
                os.read(rfd, 64)
            except BlockingIOError:
                pass


if __name__ == "__main__":
    main()
