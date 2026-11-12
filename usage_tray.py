"""
UsageTray: shows Claude plan usage in the Windows taskbar tray.

Where the figures come from
    The official Claude Code program reports them. Every few minutes this app
    starts claude.exe, asks it for usage the way the VS Code extension's
    Account & usage dialog does, and closes it again. No prompt is sent, so a
    check does not count towards the plan.

    The sign-in stays inside claude.exe. This app never opens Claude's sign-in
    files: Anthropic does not allow other programs to collect those tokens.

    The extension marks this request as experimental, so an update could change
    it. If that happens the icon shows "?" and the menu gives the reason, rather
    than a wrong number.

Every plan
    The answer is read whatever the plan sends: its list of limits when there is
    one (session, weekly, weekly for one model, or any other kind), otherwise
    the named windows (5-hour, 7-day, 7-day for Opus or Sonnet), and extra
    usage when it is turned on. The plan's name is the kind the usage answer
    gives, such as "max"; it does not say which Max tier. The sign-in's own
    name for the plan is not used: it can keep the old plan's name for a
    while after a plan change.
    A sign-in without plan limits (an API key, for instance) shows "--" and
    is checked every half hour, not every 5 minutes.

When the usage server says no
    claude.exe asks claude.ai's usage server, which can answer 429 (too many
    requests) and say how long to wait. Each check is a fresh claude.exe, which
    would ask again at once, so every check writes claude.exe's own log to a
    temporary file and the app reads from it why the request failed and how long
    the server asked it to wait. The next check waits that long, and for the
    next day checks come every 10 minutes instead of 5. Other failures wait 1, 2,
    4 minutes and so on, up to half an hour. The last good figures stay on show,
    greyed, and are reloaded from the figures file after a restart, which also
    keeps to a wait the usage server asked for.

A plan change
    A new plan keeps the week's reset time but measures against a different
    limit, so the weekly figure jumps (from 56% to 0%, say). The app takes a
    change of plan kind, a fall of the weekly figure, or
    a rise of 10 points or more between two checks as a plan change: the 1%
    measurements start again from there and are never mixed across it.

Why claude.exe is not kept open
    Measured with all CPU threads busy: a start costs about 5 s of CPU, which is
    1.7% of one core at one check every 5 minutes. Left open and idle it used
    2.9% of a core and 200 to 400 MB of RAM. Starting takes up to a minute under
    that load, but nearly all of it is waiting behind other programs.

Expected usage
    Each weekly limit is shared out evenly over the 7 days before it resets,
    so the expected use by the end of day 3 is 3/7 of 100%, about 42.9%. The
    share steps up at the reset's time of day. The icon shows the week's
    figure over the gap: "−10" means 10% behind the expected usage (used
    less), "+3" means 3% ahead of it (used more). The first time in a day that
    a weekly limit gets ahead, a Windows notification says so.

Tokens this week
    Claude Code saves every chat as a .jsonl file in the projects folder inside
    .claude, with the token counts of each reply. The app adds up the replies
    since the weekly reset, and after the first look it reads only the lines
    added since the last one. It covers Claude Code on this computer only:
    claude.ai in a browser or on a phone, and other computers, count towards
    the percentages but are not in this number. Most of it is cache reads,
    earlier parts of a chat sent again with each reply.

API value
    Each reply is also priced as the API would charge for it, per model. Most
    tokens are re-reads of the chat so far, which cost 1/20 of new input on
    Opus 5.5, so a long chat piles up tokens without using much more of the
    plan. The API value follows the work done instead, which makes it the
    steadier yardstick for the plan's meter.

What the last 1% cost, and the monthly record
    When the weekly figure goes up while the app is watching, the line was
    crossed between the previous check and this one, so the use at both
    checks is noted and the crossing taken as halfway. The use between two
    crossings is what 1% cost. The hover text shows the last completed 1%, in
    tokens and API value; it stays fixed until the next one is completed.
    Each day gets a row in "Usage <year-month>.csv" in the Monthly records
    folder: the weekly figure at the day's end, the day's share of it, its
    tokens and API value, and both per 1% from that day's crossings. Below
    the days, a second table gives every completed 1% of the month on its
    own row: when it was reached, from which percent to which, and its tokens
    and API value, with an average for each week and one for the month. A
    week's average covers the whole week, from 0% at its start (the tokens
    are counted from then, so the use before the app first saw a step is in
    the week's first row) to its last completed 1%. The 1% under way is in
    none of these until it is completed. If the API value
    per 1% over the latest 5 points moves 25% or more from the points before
    them, a notification says so: the limit may have changed. After a plan
    change, a week's table shows an average for each plan's part of it.

The figures file
    After every check the latest figures are saved to usage.json in a UsageTray
    folder under %LOCALAPPDATA%, so a Claude Code chat can read them when
    asked, without starting claude.exe. "updated" is the last good check;
    after a failed one the figures stay and "error" says why.

Whole numbers only
    claude.exe reports usage in whole percent. Finer figures exist only in the
    replies to real model requests, so showing them would cost tokens.
"""

import csv
import ctypes
import functools
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import winreg
from ctypes import wintypes
from datetime import date, datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem, Menu

REFRESH_INTERVAL = 300   # seconds between checks
RETRY_INTERVAL   = 60    # after a failed one: at logon the network may not be up yet...
RETRY_LONGEST    = 1800  # ...doubled after each further failure, up to this
QUIET_INTERVAL   = 600   # between checks for a day after the usage server said "too many"
QUIET_FOR        = 24 * 3600
BUSY_WAIT        = 600   # after a "too many" without a time to wait: doubled each time...
BUSY_LONGEST     = 3600  # ...up to this
HOLD_MARGIN      = 30    # seconds added to the wait the usage server asks for
HOLD_LONGEST     = 3 * 3600
NO_LIMITS_INTERVAL = 1800   # a sign-in without plan limits: its plan rarely changes
ASK_TIMEOUT      = 180   # seconds a check may take
WARN_PERCENT     = 75    # the number turns orange from here
DANGER_PERCENT   = 90    # and red from here
WEEK_DAYS        = 7     # a weekly limit's 100% is shared out evenly over these

FIGURES_FILE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                            "UsageTray", "usage.json")
CHATS_FOLDER = os.path.join(os.path.expanduser("~"), ".claude", "projects")
DAILY_FILE   = os.path.join(os.path.dirname(FIGURES_FILE), "daily.json")   # the record's data
# claude.exe's own log of one check, read for why the usage server said no,
# then deleted. In the temporary folder: it is written by claude.exe and read
# by this app, and the Store build of Python sees the real folder there.
CHECK_LOG    = os.path.join(tempfile.gettempdir(), "UsageTray check.log")

# The monthly spreadsheets go in the app's own folder (above dist when built),
# where they are easy to find; the menu also opens them.
_HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
RECORDS_FOLDER = os.path.join(os.path.dirname(_HERE) if os.path.basename(_HERE).lower() == "dist" else _HERE,
                              "Monthly records")

# Plugins, connectors and hooks play no part in a usage check, so they stay off.
PROGRAM_ARGS = [
    "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
    "--no-session-persistence", "--strict-mcp-config", "--settings", '{"disableAllHooks":true}',
]

BACKGROUND = (44, 28, 20)
TEXT       = (245, 240, 232)
WARN       = (255, 170, 60)
DANGER     = (255, 80, 80)
FADED      = (150, 140, 130)
GOOD       = (120, 220, 120)

state = {
    "limits":  [],      # read_limits() result
    "plan":    None,    # the plan's name as shown, such as "Max"
    "plan_key": None,   # its kind in the usage answer, such as "max": a change is a new plan
    "updated": None,    # epoch of the last good check
    "error":   None,    # why the last check failed
    "no_limits": False, # the sign-in has no plan limits: not an error
    "next_check": None, # epoch of the next check
    "failures": 0,      # failed checks in a row, for the growing wait
    "busy":    0,       # "too many" answers in a row without a time to wait
    "quiet_until": 0,   # epoch until which checks are spaced out after a "too many"
    "warned":  set(),   # (limit, reset, day) already notified as ahead
    "tokens":  None,    # TokenCounter.totals() for this week
}
icon_ref = [None]
_refresh = threading.Event()


class UsageError(Exception):
    """A check failed; the message is shown in the menu."""


# ── Asking Claude Code ─────────────────────────────────────────────────────────

def _version(path):
    match = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", path)
    return tuple(int(part) for part in match.groups()) if match else ()


def find_claude():
    """The official program: on the PATH, installed on its own, or inside VS Code."""
    home = os.path.expanduser("~")
    installed = [shutil.which("claude.exe"), os.path.join(home, ".local", "bin", "claude.exe")]
    bundled = glob.glob(os.path.join(home, ".vscode*", "extensions", "anthropic.claude-code-*",
                                     "resources", "native-binary", "claude.exe"))
    for path in [*installed, *sorted(bundled, key=_version, reverse=True)]:
        if path and os.path.isfile(path):
            return path
    raise UsageError("Claude Code was not found")


def _send(program, request_id, request):
    message = {"type": "control_request", "request_id": request_id, "request": request}
    program.stdin.write(json.dumps(message) + "\n")
    program.stdin.flush()


def _message(line):
    """A line of output as a dict, or None for plain text."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _close(program):
    try:
        program.stdin.close()
        program.wait(10)
    except (OSError, subprocess.TimeoutExpired):
        program.kill()


def _ask(extra_args):
    """Start claude.exe, ask for usage, close it. Returns the answer."""
    # An API key in the environment would report that key, not the plan.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("CLAUDE", "ANTHROPIC"))}
    program = subprocess.Popen(
        [find_claude(), *PROGRAM_ARGS, *extra_args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        cwd=os.path.expanduser("~"), env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    watchdog = threading.Timer(ASK_TIMEOUT, program.kill)
    watchdog.start()
    said = ""   # the program's last plain text: its own reason, if it stops
    try:
        _send(program, "start", {"subtype": "initialize"})
        for line in program.stdout:
            message = _message(line)
            if message is None:
                said = line.strip() or said
                continue
            reply = message.get("response")
            if message.get("type") != "control_response" or not isinstance(reply, dict):
                continue
            if reply.get("subtype") == "error":
                raise UsageError(str(reply.get("error") or "Claude Code refused the request"))
            if reply.get("request_id") == "start":
                _send(program, "usage", {"subtype": "get_usage", "skip_behaviors": True})
            elif reply.get("request_id") == "usage":
                answer = reply.get("response")
                return answer if isinstance(answer, dict) else {}
    except OSError:
        pass   # it stopped while being written to: read on as if it had not answered
    finally:
        watchdog.cancel()
        _close(program)
    raise UsageError(f"Claude Code did not answer: {said[:100]}" if said
                     else "Claude Code did not answer")


_log_option = [True]   # False once a Claude Code update has dropped --debug-file


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def ask_usage():
    """Start claude.exe, ask for usage, close it. Returns {"answer", "fetch"}:
    the get_usage answer, and what claude.exe's own log says about its request
    to the usage server (None without the log)."""
    if not _log_option[0]:
        return {"answer": _ask([]), "fetch": None}
    started = time.time()
    _remove(CHECK_LOG)
    try:
        answer = _ask(["--debug-file", CHECK_LOG])
    except UsageError as exc:
        if "debug-file" not in str(exc):
            raise
        _log_option[0] = False   # this version does not know the option: ask without it
        return {"answer": _ask([]), "fetch": None}
    fetch = read_fetch(CHECK_LOG, started)
    _remove(CHECK_LOG)
    return {"answer": answer, "fetch": fetch}


# Lines claude.exe writes to its log about the usage server, such as
#   ... [DEBUG] fetchUtilization: 429 remembered for this bearer; not asking again for 3311s
#   ... [ERROR] Failed to load usage data: Request failed with status code 429
_HOLD_SAID   = re.compile(r"not ask(?:ing|ed) again for (\d+)\s*s")
_FAILED_SAID = re.compile(r"Failed to load usage data:\s*(.*)")
_STATUS_SAID = re.compile(r"status code (\d{3})")
_EMPTY_SAID  = "fieldless or non-object body"


def read_fetch(path, started):
    """{"failed", "status", "hold"} from claude.exe's log of this check: why
    the usage server's answer was not used (None when it was), its HTTP status,
    and the seconds it asked to be left alone. Lines from before this check
    (a log that could not be deleted) are skipped."""
    found = {"failed": None, "status": None, "hold": None}
    try:
        with open(path, encoding="utf-8", errors="replace") as log:
            for line in log:
                when = _stamp(line.split(" ", 1)[0])
                if when is not None and when < started - 2:
                    continue
                hold = _HOLD_SAID.search(line)
                if hold and "fetchUtilization" in line:
                    found["hold"] = int(hold.group(1))
                status = _STATUS_SAID.search(line)
                failed = _FAILED_SAID.search(line)
                if failed:
                    found["failed"] = failed.group(1).strip() or "no reason given"
                    if status:
                        found["status"] = int(status.group(1))
                elif _EMPTY_SAID in line:
                    found["failed"] = "it sent no figures"
                elif "fetchUtilization" in line and "replayed" in line:
                    found["failed"] = found["failed"] or "it said no a short while ago"
                    if status:
                        found["status"] = int(status.group(1))
    except OSError:
        pass
    if found["hold"] and not found["failed"]:
        found["failed"] = "it asked to be left alone"
    return found


def fetch_problem(fetch, hold_until):
    """The usage server's refusal in a few words, for the menu."""
    status = fetch.get("status")
    if status == 429:
        text = "the usage server says too many requests (429)"
    elif status == 401:
        text = "the usage server did not accept the sign-in (401); sign in again in Claude Code"
    elif status == 403:
        text = "the usage server refused the request (403)"
    elif status:
        text = f"the usage server answered {status}"
    else:
        text = f"no figures from the usage server: {fetch.get('failed') or 'no reason given'}"[:110]
    if hold_until:
        text += f"; it asked to wait until {datetime.fromtimestamp(hold_until):%H:%M}"
    return text


# ── Reading the answer ─────────────────────────────────────────────────────────

def _time(value):
    """To the nearest minute: one reset can arrive as 23:59:59.8 and as 00:00:00.
    ISO text, or epoch seconds (or milliseconds) as some answers carry."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            when = datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
        else:
            when = datetime.fromisoformat(value)
        rounded = when + timedelta(seconds=30)
        return rounded.replace(second=0, microsecond=0).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _row(label, percent, resets_at):
    if not isinstance(percent, (int, float)) or isinstance(percent, bool):
        return None
    return {"label": label, "percent": round(percent), "resets": _time(resets_at)}


# The limits list names each row by its group; the label is what the app shows.
GROUP_LABELS = {"session": "Session", "five_hour": "Session", "weekly": "Week",
                "seven_day": "Week", "monthly": "Month", "daily": "Day"}
# Without the list: the named windows, in this order.
WINDOW_LABELS = (("five_hour", "Session"), ("seven_day", "Week"), ("seven_day_opus", "Week, Opus"),
                 ("seven_day_sonnet", "Week, Sonnet"), ("seven_day_cowork", "Week, Cowork"))


def _scope_name(scope):
    """The model or surface a row is limited to, such as "Fable", or None."""
    if not isinstance(scope, dict):
        return None
    for key in ("model", "surface"):
        part = scope.get(key)
        if isinstance(part, dict):
            part = part.get("display_name") or part.get("name")
        if isinstance(part, str) and part.strip():
            return part.strip()
    return None


def _list_label(entry):
    """Such as "Session", "Week" or "Week, Fable". A kind the app does not know
    gets its own words, so that it is shown rather than dropped."""
    kind = str(entry.get("kind") or "")
    group = str(entry.get("group") or "") or ("five_hour" if kind.startswith("five_hour") else kind.split("_", 1)[0])
    base = GROUP_LABELS.get(group) or (group or kind).replace("_", " ").strip().capitalize() or "Limit"
    name = _scope_name(entry.get("scope"))
    if not name and "_" in kind:
        rest = kind.split("_", 1)[1]
        if rest not in ("all", "scoped", "hour", "day"):
            name = rest.replace("_", " ")
    return f"{base}, {name}" if name else base


def _extra_row(limits):
    """Extra usage (paid use past the plan's limits), when it is turned on."""
    extra = limits.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        return _row("Extra usage", extra.get("utilization"), None)
    return None


def read_limits(answer):
    """[{"label", "percent", "resets"}, ...], the session limit first and the
    whole week next, for any plan: the limits list when the answer has one,
    then the named windows, the weekly rows for single models and extra usage,
    each label once. Empty when the answer holds no figures."""
    limits = answer.get("rate_limits")
    if not isinstance(limits, dict):
        return []
    rows = [_row(_list_label(entry), entry.get("percent", entry.get("utilization")), entry.get("resets_at"))
            for entry in limits.get("limits") or [] if isinstance(entry, dict)]
    for key, label in WINDOW_LABELS:
        window = limits.get(key)
        if isinstance(window, dict):
            rows.append(_row(label, window.get("utilization"), window.get("resets_at")))
    for entry in limits.get("model_scoped") or []:
        if isinstance(entry, dict) and isinstance(entry.get("display_name"), str):
            rows.append(_row(f"Week, {entry['display_name']}", entry.get("utilization"), entry.get("resets_at")))
    rows.append(_extra_row(limits))
    found = {}
    for row in rows:
        if row and row["label"] not in found:
            found[row["label"]] = row
    first = [found.pop(label) for label in ("Session", "Week") if label in found]
    return first + list(found.values())


# ── Expected usage ─────────────────────────────────────────────────────────────

def _now():
    return datetime.now().astimezone()


def pace(row, now=None):
    """For a weekly limit with a reset time: which day of its week it is, the
    use expected by the end of that day, and how far the real use is from it
    (above 0 is ahead, below 0 behind). None when there is nothing to compare."""
    if not row["label"].startswith("Week") or not row["resets"]:
        return None
    now = now or _now()
    start = row["resets"] - timedelta(days=WEEK_DAYS)
    if not start <= now < row["resets"]:
        return None   # a reset time that has passed: the figure is from last week
    day = int((now - start) / timedelta(days=1)) + 1
    expected = 100 * day / WEEK_DAYS
    return {"day": day, "expected": expected, "difference": row["percent"] - expected,
            "day_ends": start + timedelta(days=day)}


def gap_words(difference):
    if abs(difference) < 0.05:
        return "on track"
    return f"{abs(difference):.1f}% {'ahead' if difference > 0 else 'behind'}"


def _by_when(day_ends, now=None):
    now = now or _now()
    if day_ends.hour == day_ends.minute == 0 and (day_ends - timedelta(minutes=1)).date() == now.date():
        return "end of today"
    return day_ends.strftime("%a %H:%M")


def pace_text(row):
    """Such as "Day 3 of 7: 42.9% expected by end of today, 12.9% behind"."""
    now = _now()
    p = pace(row, now)
    if not p:
        return None
    return (f"Day {p['day']} of {WEEK_DAYS}: {p['expected']:.1f}% expected by "
            f"{_by_when(p['day_ends'], now)}, {gap_words(p['difference'])}")


def warn_if_ahead():
    """A notification the first time in a day that a weekly limit is ahead of
    its expected use. Each new day brings a larger share, so it can come again."""
    for row in state["limits"]:
        p = pace(row)
        if not p or p["difference"] <= 0:
            continue
        key = (row["label"], _iso(row["resets"]), p["day"])
        if key in state["warned"]:
            continue
        state["warned"].add(key)
        title = f"{row['label']}: ahead of the expected usage"
        message = f"{row['percent']}% used. {pace_text(row)}."
        _notify(title, message)


# ── Tokens this week and today ─────────────────────────────────────────────────

TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
TOKEN_NAMES  = ("input", "cache_write", "cache_read", "output")


# API prices in dollars per million tokens: new input, output, 5-minute cache
# writes, 1-hour cache writes, cache reads (re-reading the chat so far). Most
# of the tokens are cache reads, which cost 1/20 of new input on Opus 5.5, so
# priced use follows the work done rather than the length of the chat. Taken
# from Anthropic's price list in November 2026.
PRICES = {
    "claude-opus-5-5":   (4.00, 20.00, 5.00, 8.00, 0.20),
    "claude-opus-5":     (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 4.00, 0.10),
    "claude-sonnet-5":   (2.00, 10.00, 2.50, 4.00, 0.20),
    "claude-fable-5-2":  (8.00, 40.00, 10.00, 16.00, 0.20),
    "claude-fable-5-1":  (10.00, 50.00, 12.50, 20.00, 0.25),
    "claude-fable-5":    (10.00, 50.00, 12.50, 20.00, 1.00),
    "claude-haiku-5-5":  (0.10, 0.50, 0.125, 0.20, 0.01),
}
DEFAULT_PRICES = PRICES["claude-opus-5-5"]   # a model newer than this list


def prices_for(model):
    """The longest name in PRICES that the model's id starts with, so a dated
    id finds its model and claude-opus-5-5 is not taken for claude-opus-5."""
    for name in sorted(PRICES, key=len, reverse=True):
        if (model or "").startswith(name):
            return PRICES[name]
    return DEFAULT_PRICES


def reply_cost(model, fresh, writes, writes_1h, reads, output):
    """Dollars at API prices for one reply; writes includes writes_1h."""
    p_in, p_out, p_5m, p_1h, p_read = prices_for(model)
    writes_1h = min(writes_1h, writes)
    return (fresh * p_in + output * p_out + (writes - writes_1h) * p_5m
            + writes_1h * p_1h + reads * p_read) / 1e6


def _stamp(text):
    """A chat file's UTC time as epoch seconds, or None."""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def _chat_files(folder):
    """(path, last changed) of every .jsonl file under folder. The dates come
    with the folder listing, so the many files that did not change this week
    cost no extra look each. tool-results folders hold saved tool output, not
    chats, and are most of the files, so they are skipped: 78 ms instead of
    400 ms for a full walk here, where glob took 970 ms."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                if entry.name != "tool-results":
                    yield from _chat_files(entry.path)
            elif entry.name.endswith(".jsonl"):
                yield entry.path, entry.stat().st_mtime
        except OSError:
            continue


class TokenCounter:
    """Adds up the token counts of the replies saved in Claude Code's chat files
    since a given moment (the weekly reset). Chat files only grow, so after the
    first look only the lines added since the last one are read. One reply is
    saved as several lines with the same message id, and a resumed chat repeats
    earlier ones, so each id counts once, with the largest figures seen for it."""

    def __init__(self, folder):
        self.folder = folder
        self.since = None
        self.offsets = {}   # file -> bytes already read
        self.replies = {}   # message id -> [epoch, input, cache writes, cache reads, output, 1-hour writes, model]

    def scan(self, since):
        """Read what is new. since: the weekly reset, as an aware datetime."""
        since = since.timestamp()
        if since != self.since:   # a new week starts from nothing
            self.since, self.offsets, self.replies = since, {}, {}
        for path, changed in _chat_files(self.folder):
            if changed < since:
                continue   # not touched this week, so nothing from this week is in it
            try:
                info = os.stat(path)
                done = self.offsets.get(path, 0)
                if info.st_size < done:
                    done = 0   # rewritten: read again; ids already counted stay counted once
                if info.st_size == done:
                    continue
                with open(path, "rb") as chat:
                    chat.seek(done)
                    for line in chat:
                        if not line.endswith(b"\n"):
                            break   # still being written: read it next time
                        done += len(line)
                        self._take(line)
                self.offsets[path] = done
            except OSError:
                continue   # moved or locked: try again next time

    def _take(self, line):
        if b'"usage"' not in line or b'"assistant"' not in line:
            return   # quick skip for the many lines without a reply's counts
        try:
            entry = json.loads(line)
        except ValueError:
            return
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            return   # such as a tool result that quotes a chat file
        message = entry.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            return
        if message.get("model") == "<synthetic>":
            return   # a placeholder written by Claude Code itself, not a model reply
        when = _stamp(entry.get("timestamp"))
        if when is None or when < self.since:
            return
        key = message.get("id") or entry.get("requestId") or entry.get("uuid")
        counts = self.replies.setdefault(key, [when, 0, 0, 0, 0, 0, str(message.get("model") or "")])
        counts[0] = min(counts[0], when)
        usage = message["usage"]
        split = usage.get("cache_creation")
        values = [usage.get(field) for field in TOKEN_FIELDS]
        values.append(split.get("ephemeral_1h_input_tokens") if isinstance(split, dict) else 0)
        for i, value in enumerate(values, start=1):
            if isinstance(value, int) and not isinstance(value, bool) and value > counts[i]:
                counts[i] = value

    def totals(self, since=None, until=None):
        """{"total", "input", "cache_write", "cache_read", "output", "replies",
        "cost"} for the replies from since up to until (epoch seconds), or the
        whole week. cost is dollars at API prices."""
        rows = [r for r in self.replies.values()
                if (since is None or r[0] >= since) and (until is None or r[0] < until)]
        sums = {name: sum(r[i] for r in rows) for i, name in enumerate(TOKEN_NAMES, start=1)}
        cost = sum(reply_cost(r[6], r[1], r[2], r[5], r[3], r[4]) for r in rows)
        return {"total": sum(sums.values()), **sums, "replies": len(rows), "cost": cost}


tokens = TokenCounter(CHATS_FOLDER)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetCurrentThread.restype = wintypes.HANDLE
_kernel32.SetThreadPriority.argtypes = (wintypes.HANDLE, ctypes.c_int)
_kernel32.SetThreadPriority.restype = wintypes.BOOL
_BACKGROUND_BEGIN = 0x00010000   # lowest CPU and disk priority for this thread
_BACKGROUND_END   = 0x00020000


def _background(on):
    return bool(_kernel32.SetThreadPriority(_kernel32.GetCurrentThread(),
                                            _BACKGROUND_BEGIN if on else _BACKGROUND_END))


def _week_start():
    """The start of the weekly limit's current week. A reset time that has
    passed (figures kept from before a long wait) moves on by whole weeks; a
    plan without a weekly limit counts from Monday midnight."""
    now = _now()
    week = _week_row()
    known = week["resets"] if week else None
    if known is None and record["last"]:   # no figures yet: the reset the record last saw
        try:
            known = datetime.fromisoformat(record["last"]["resets"])
        except (KeyError, TypeError, ValueError):
            pass
    if known is None:
        return _midnight(now) - timedelta(days=now.weekday())
    resets = known
    while resets <= now:
        resets += timedelta(days=WEEK_DAYS)
    return resets - timedelta(days=WEEK_DAYS)


def count_tokens():
    """Bring this week's and today's token totals up to date, in the background:
    the first look after a start reads all of this week's chats."""
    start = _week_start()
    if start is None:
        return
    _background(True)
    try:
        tokens.scan(start)
        today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
        state["tokens"] = {"since": start, "week": tokens.totals(),
                           "today": tokens.totals(max(today, start).timestamp())}
    except Exception:
        pass   # the last totals stay
    finally:
        _background(False)


def token_text(count):
    """Such as 950, 45.3K, 266.6M or 1.2B."""
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if count >= size * 0.99995:   # 999,999 reads as 1.0M, not 1000.0K
            return f"{count / size:.1f}{unit}"
    return str(count)


def _token_parts(sums):
    parts = [("read from cache", sums["cache_read"]), ("written to cache", sums["cache_write"]),
             ("output", sums["output"]), ("new input", sums["input"])]
    return ", ".join(f"{token_text(n)} {name}" for name, n in parts if n)


# ── Daily record and tokens per 1% ─────────────────────────────────────────────
# Each good check adds to a day-by-day record kept in daily.json beside the
# figures file, and the month's spreadsheet is rewritten from it.
#
# What 1% costs is measured, not estimated from daily totals: when a check
# sees the weekly figure go up (say 7% to 8%) while the checks before it ran
# without a break, the 8% line was crossed between the previous check and
# this one, so the week's tokens and API-priced value at both checks are
# noted, and the crossing is taken as halfway between them. The use between
# two crossings over the points between them is what 1% cost. The last
# completed 1% is what the app shows: it stays fixed until the next one.

CONTINUOUS     = 12 * 60   # seconds: checks closer than this watched without a break
RECENT_POINTS  = 5         # the latest points crossed this week are compared with...
BASE_POINTS    = 10        # ...at least this many points crossed before them
RATIO_CHANGE   = 0.25      # a move this large in API value per 1% is notified
COMPARE_WEEKS  = 5         # this week and the weeks before it used in that comparison
KEEP_WEEKS     = 9         # weeks of crossings kept: more than a month and a week, so a
                           # month's table of each 1% can always be rebuilt
PLAN_JUMP      = 10        # points: a rise this large between two unbroken checks is a plan
                           # change (a smaller limit), not use: 10% of a week in minutes

RECORD_COLUMNS = ("Date", "Day", "Week used by end of day (%)", "Used this day (%)", "Tokens this day",
                  "API value this day ($)", "Tokens per 1%", "API value per 1% ($)", "New input",
                  "Written to cache", "Read from cache", "Output", "Note")
POINTS_TITLE   = "Each 1% of the weekly limit"
POINT_COLUMNS  = ("Date", "Time reached", "Day", "From (%)", "To (%)", "Tokens per 1%",
                  "API value per 1% ($)", "Give or take (tokens)", "Note")

record = {
    "last":      None,   # the latest check: {"percent", "resets", "at", "tokens", "cost", "plan"}
    "days":      {},     # "2026-11-08" -> {"used", "week_end", "partial", "note", "tokens", "span"}
    "crossings": {},     # week's reset -> [{"percent", "at", "tokens": [before, after], "cost": [...], "plan"}],
                         # with {..., "plan_change": True} where a new plan began
    "warned":    [],     # changes in API value per 1% already notified
}
_unwritten = set()       # months whose spreadsheet could not be written, such as while open in Excel
_CROSSING_KEYS = {"percent", "at", "tokens", "cost"}


def load_record():
    try:
        with open(DAILY_FILE, encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return
    if isinstance(loaded, dict):
        for key, kind in (("last", dict), ("days", dict), ("crossings", dict), ("warned", list)):
            if isinstance(loaded.get(key), kind):
                record[key] = loaded[key]
    record["crossings"] = {week: [c for c in crossings if isinstance(c, dict) and _CROSSING_KEYS <= c.keys()]
                           for week, crossings in record["crossings"].items() if isinstance(crossings, list)}


def _midnight(when):
    return when.replace(hour=0, minute=0, second=0, microsecond=0)


def _new_day(watched):
    return {"used": 0 if watched else None, "week_end": None, "partial": not watched,
            "note": "" if watched else "not watched", "tokens": None, "span": None}


def _at_crossing(crossing):
    """Tokens and API value when the line was crossed: halfway between the
    check before and the check that saw it, so timing is off by half a check's use at most."""
    return sum(crossing["tokens"]) / 2, sum(crossing["cost"]) / 2


def measure(first, last):
    """What each point cost from one crossing to a later one: {"points",
    "tokens", "cost", "spread", "from", "to", "at"}, per 1%; spread is the most
    the timing can move the tokens figure. None if no points lie between."""
    points = last["percent"] - first["percent"]
    if points <= 0:
        return None
    (tokens0, cost0), (tokens1, cost1) = _at_crossing(first), _at_crossing(last)
    spread = (first["tokens"][1] - first["tokens"][0] + last["tokens"][1] - last["tokens"][0]) / 2
    return {"points": points, "tokens": (tokens1 - tokens0) / points, "cost": (cost1 - cost0) / points,
            "spread": spread / points, "from": first["percent"], "to": last["percent"], "at": last["at"]}


def _week_row():
    return next((row for row in state["limits"] if row["label"] == "Week" and row["resets"]), None)


def runs(week):
    """The week's crossings in runs under one plan, oldest first: [{"changed":
    when the plan change that began it was seen, or None for the run from the
    week's start, "crossings"}]. A run ends at a plan-change mark, or where the
    figure did not go up (records kept before the marks were written). The
    last run may be empty: a plan change with no step seen since."""
    found = [{"changed": None, "crossings": []}]
    for c in record["crossings"].get(week) or []:
        current = found[-1]["crossings"]
        if c.get("plan_change"):
            found.append({"changed": c["at"], "crossings": []})
        elif current and c["percent"] <= current[-1]["percent"]:
            found.append({"changed": c["at"], "crossings": [c]})
        else:
            current.append(c)
    return found


def with_start(week, run):
    """A run's crossings, with the week's start in front as a crossing at 0%
    for the run that began with the week, so that the use before the app first
    saw a step is counted too: the tokens are counted from the week's start,
    and at the start none had been used. A run after a plan change has no such
    start: what the new plan counted before its first step is unknown."""
    crossings = run["crossings"]
    if run["changed"] or not crossings:
        return list(crossings)
    start = datetime.fromisoformat(week) - timedelta(days=WEEK_DAYS)
    return [{"percent": 0, "at": _iso(start), "tokens": [0, 0], "cost": [0, 0]}] + crossings


def _current_run():
    """(week's reset, the run under way), or (None, None) without a weekly figure."""
    week = _week_row()
    if not week:
        return None, None
    key = _iso(week["resets"])
    return key, runs(key)[-1]


def last_point():
    """What the last completed 1% of this week cost; fixed until the next one."""
    _, run = _current_run()
    crossings = run["crossings"] if run else []
    return measure(crossings[-2], crossings[-1]) if len(crossings) >= 2 else None


def week_ratio():
    """Per 1% over the whole week from its start (or from the first step seen
    after a plan change) to the last completed step, so the 1% under way is
    left out; None until there is a step."""
    week, run = _current_run()
    crossings = with_start(week, run) if run else []
    return measure(crossings[0], crossings[-1]) if len(crossings) >= 2 else None


def day_ratio(key):
    """Per 1% over that day's crossings, or None."""
    span = (record["days"].get(key) or {}).get("span")
    if not span:
        return None
    return {"points": span["points"], "tokens": span["tokens"] / span["points"],
            "cost": span["cost"] / span["points"]}


def _same_plan_before(resets):
    """Spans of the weeks before this one (newest first, up to COMPARE_WEEKS
    weeks in all) back to the latest plan change: other plans are not compared."""
    spans = []
    for key in sorted((key for key in record["crossings"] if key < resets), reverse=True)[:COMPARE_WEEKS - 1]:
        for run in reversed(runs(key)):
            crossings = run["crossings"]
            if len(crossings) >= 2:
                spans.append(measure(crossings[0], crossings[-1]))
            if run["changed"]:
                return spans
    return spans


def ratio_change(resets):
    """(recent, before, change) in API value per 1%: the latest RECENT_POINTS
    crossed this week against this week's earlier crossings and the weeks
    before, all under the same plan. Five points rather than the last one,
    because one point can be off by a check's use at each end. None until
    there is enough to compare."""
    run = runs(resets)[-1]
    week = run["crossings"]
    if not week:
        return None
    last = week[-1]
    earlier = [c for c in week if c["percent"] <= last["percent"] - RECENT_POINTS]
    if not earlier:
        return None
    recent = measure(earlier[-1], last)
    upto = [c for c in week if c["percent"] <= earlier[-1]["percent"]]
    spans = [measure(upto[0], upto[-1])]
    if not run["changed"]:   # this run began with the week, so the plan may go back further
        spans += _same_plan_before(resets)
    spans = [s for s in spans if s]
    points = sum(s["points"] for s in spans)
    if points < BASE_POINTS:
        return None
    before = sum(s["cost"] * s["points"] for s in spans) / points
    if before <= 0:
        return None
    return recent["cost"], before, recent["cost"] / before - 1


def _notify(title, message):
    icon = icon_ref[0]
    try:
        if icon is not None and icon.HAS_NOTIFICATION:
            icon.notify(message[:255], title[:63])
    except Exception:
        pass   # the menu and the record still show it


def update_record():
    """After a good check: add the day's share of the weekly figure, note a
    crossing, refresh this week's daily tokens, then save and rewrite the
    spreadsheets of the months touched."""
    week, counted = _week_row(), state["tokens"]
    if week is None or not counted:
        return
    now = _now()
    midnight = _midnight(now)
    start = week["resets"] - timedelta(days=WEEK_DAYS)
    resets, percent = _iso(week["resets"]), week["percent"]
    days = record["days"]
    today = now.date().isoformat()
    row = days.setdefault(today, _new_day(True))
    if row["used"] is None:
        row["used"] = 0
    plan = state["plan_key"]
    week_total, week_cost = counted["week"]["total"], counted["week"]["cost"]
    last = record["last"]
    if last is None:
        if start >= midnight:   # the week began today, so all of it is today's
            row["used"] += percent
        else:
            row["partial"] = True
            row["note"] = f"recording started {now:%H:%M}; use before it is unknown"
    else:
        seen = datetime.fromisoformat(last["at"])
        same_week = last["resets"] == resets
        unbroken = (now - seen).total_seconds() <= CONTINUOUS
        rise = percent - last["percent"]
        renamed = bool(plan and last.get("plan") and plan != last["plan"])
        changed = renamed or (same_week and (rise < 0 or (unbroken and rise >= PLAN_JUMP)))
        if not same_week:
            row["used"] += percent
        elif not changed:
            row["used"] += max(0, rise)
        if not unbroken and seen < midnight:
            row["partial"] = True
            row["note"] = f"includes use since {seen:%a %d %b %H:%M}"
            before = days.get(seen.date().isoformat())
            if before is not None and not before["partial"]:
                before["partial"] = True
                before["note"] = f"not watched after {seen:%H:%M}"
        if changed:
            # Marked, not measured: the step from the old figure to the new one is not use.
            record["crossings"].setdefault(resets, []).append(
                {"percent": percent, "at": _iso(now), "tokens": [week_total, week_total],
                 "cost": [week_cost, week_cost], "plan": plan, "plan_change": True})
            when = f"at {now:%H:%M}" if unbroken else f"between {seen:%a %H:%M} and {now:%H:%M}"
            row["note"] = "; ".join(filter(None, [row.get("note"), f"plan changed {when}: the "
                                    f"weekly figure went from {last['percent']}% to {percent}%"]))
            names = f" from {last['plan']} to {plan}" if renamed else ""
            _notify("Plan changed", f"The plan changed{names}: the weekly figure went from "
                    f"{last['percent']}% to {percent}%. The cost of 1% is measured again from here.")
        elif same_week and unbroken and rise > 0 and "tokens" in last:
            record["crossings"].setdefault(resets, []).append(
                {"percent": percent, "at": _iso(now), "tokens": [last["tokens"], week_total],
                 "cost": [last["cost"], week_cost], "plan": plan})
    row["week_end"] = percent
    record["last"] = {"percent": percent, "resets": resets, "at": _iso(now), "tokens": week_total,
                      "cost": week_cost, "plan": plan or (last or {}).get("plan")}
    spans = []   # today's steps, run by run: steps under two plans are never joined
    for run in runs(resets):
        crossings_today = [c for c in with_start(resets, run) if c["at"][:10] == today]
        if len(crossings_today) >= 2:
            spans.append(measure(crossings_today[0], crossings_today[-1]))
    spans = [s for s in spans if s]
    row["span"] = ({"points": sum(s["points"] for s in spans),
                    "tokens": sum(s["tokens"] * s["points"] for s in spans),
                    "cost": sum(s["cost"] * s["points"] for s in spans)} if spans else None)

    touched = {today[:7]}
    if last is not None and last["resets"] != resets:   # a new week: last week's sheets lose "so far"
        touched |= {c["at"][:7] for c in record["crossings"].get(last["resets"]) or []}
    day = start.date()
    while day <= now.date():   # each day of this week so far
        begin = datetime.combine(day, datetime.min.time()).astimezone()
        sums = tokens.totals(max(begin, start).timestamp(), (begin + timedelta(days=1)).timestamp())
        key = day.isoformat()
        if sums["replies"] or key in days:
            days.setdefault(key, _new_day(False))["tokens"] = sums
            touched.add(key[:7])
        day += timedelta(days=1)

    for old in sorted(record["crossings"])[:-KEEP_WEEKS]:
        del record["crossings"][old]
    found = ratio_change(resets)
    if found and abs(found[2]) >= RATIO_CHANGE:
        recent, before, change = found
        key = f"{resets} {'up' if change > 0 else 'down'}"
        if key not in record["warned"]:
            record["warned"] = record["warned"][-20:] + [key]
            _notify("Use per 1% has changed",
                    f"The latest {RECENT_POINTS} points of the weekly limit took ${recent:.2f} of "
                    f"API-priced use per 1%, against ${before:.2f} before ({abs(change):.0%} "
                    f"{'more' if change > 0 else 'less'}). The limit may have changed, or use from "
                    f"claude.ai or another device is mixed in.")
    try:
        _write_file(DAILY_FILE, lambda f: json.dump(record, f, indent=1), encoding="utf-8")
    except OSError:
        pass
    for month in sorted(touched | _unwritten):
        try:
            write_month(month)
            _unwritten.discard(month)
        except OSError:
            _unwritten.add(month)   # open in Excel, perhaps: tried again at the next check


def _cell(value):
    return "" if value is None else value


def record_path(month):
    return os.path.join(RECORDS_FOLDER, f"Usage {month}.csv")


def points_in(month):
    """Each completed step of the weekly figure seen in that month, oldest
    first, as measure() gives it plus "week", the week's reset, and "run", its
    run under one plan. A step is one point unless the figure rose more than
    one between two crossings, and then its figures are per 1%. The 1% under
    way has no step until it is completed, so it is in no average. A week's
    first step starts at 0% when the week began; no step spans a plan change."""
    found = []
    for week in record["crossings"]:
        for number, run in enumerate(runs(week)):
            crossings = with_start(week, run)
            for first, last in zip(crossings, crossings[1:]):
                m = measure(first, last) if last["at"].startswith(month) else None
                if m:
                    found.append(dict(m, week=week, run=number))
    return sorted(found, key=lambda m: m["at"])


def pooled(steps):
    """(points, tokens per 1%, API value per 1%) over several steps, each weighted by its points."""
    points = sum(m["points"] for m in steps)
    return (points, sum(m["tokens"] * m["points"] for m in steps) / points,
            sum(m["cost"] * m["points"] for m in steps) / points)


def week_name(week):
    """'Week 8 to 14 Nov' for a week that resets at midnight, otherwise with the times."""
    resets = datetime.fromisoformat(week)
    start = resets - timedelta(days=WEEK_DAYS)
    if (resets.hour, resets.minute) == (0, 0):
        last = resets - timedelta(days=1)
        first = str(start.day) if start.month == last.month else f"{start.day} {start:%b}"
        return f"Week {first} to {last.day} {last:%b}"
    return f"Week {start.day} {start:%b} {start:%H:%M} to {resets.day} {resets:%b} {resets:%H:%M}"


def _plural(points):
    return f"{points} point{'s' if points != 1 else ''}"


def week_average(week, number, month):
    """The average of a week's run under one plan, from 0% at the week's start
    (or its first step after a plan change) to its last completed step, so the
    1% under way is left out: (name, measure, note). The note says how many
    points it covers and names any other month's sheet holding some of its
    steps, because a week that runs into another month is averaged whole in both."""
    every = runs(week)
    run = every[number]
    crossings = with_start(week, run)
    whole = measure(crossings[0], crossings[-1])
    later = any(other["changed"] for other in every[number + 1:])
    under_way = _now() < datetime.fromisoformat(week) and not later
    if run["changed"]:
        since = datetime.fromisoformat(run["changed"])
        name = f"{week_name(week)}, plan from {since:%a %d %b %H:%M}, average"
        reach = "from the first 1% after the plan change"
    else:
        name = f"{week_name(week)}, plan until it changed, average" if later else f"{week_name(week)} average"
        reach = "the week until the plan changed" if later else "the whole week"
    if under_way:
        note = f"{_plural(whole['points'])}: {reach} so far; the 1% under way is not counted"
    else:
        note = f"{_plural(whole['points'])}: {reach}, up to its last completed 1%"
    others = sorted({last["at"][:7] for last in crossings[1:]} - {month})
    if others:
        note += "; includes its steps in " + " and ".join(f"Usage {other}" for other in others)
    return name, whole, note


def write_month(month):
    """The month's spreadsheet, as a .csv file that Excel opens: a row a day
    and a total, then a row for each completed 1%, with an average for each
    week and for the month."""
    rows = sorted((key, row) for key, row in record["days"].items() if key.startswith(month))

    def money(value):
        return "" if value is None else round(value, 2)

    def write_points(out):
        out.writerow([])
        out.writerow([POINTS_TITLE])
        out.writerow(POINT_COLUMNS)
        steps = points_in(month)
        if not steps:
            out.writerow(["None yet: a 1% is measured once the weekly figure goes up twice while the app watches"])
            return
        groups = dict.fromkeys((m["week"], m["run"]) for m in steps)   # in the order of their steps
        for week, number in groups:
            its = [m for m in steps if (m["week"], m["run"]) == (week, number)]
            for m in its:
                seen = datetime.fromisoformat(m["at"])
                note = ["from the week's start, before the app saw a step"] if m["from"] == 0 and number == 0 else []
                note += [f"average over {m['points']} points"] if m["points"] > 1 else []
                out.writerow([f"{seen:%Y-%m-%d}", f"{seen:%H:%M}", f"{seen:%a}", m["from"], m["to"],
                              round(m["tokens"]), money(m["cost"]), round(m["spread"]), "; ".join(note)])
            name, whole, note = week_average(week, number, month)
            out.writerow([name, "", "", whole["from"], whole["to"],
                          round(whole["tokens"]), money(whole["cost"]), "", note])
            out.writerow([])
        points, per_token, per_cost = pooled(steps)
        mixed = any(any(run["changed"] for run in runs(week)) for week, _ in groups)
        out.writerow(["Month average", "", "", "", "", round(per_token), money(per_cost), "",
                      _plural(points) + ("; mixes the limits of more than one plan" if mixed else "")])

    def write(f):
        out = csv.writer(f)
        out.writerow(RECORD_COLUMNS)
        used = points = span_tokens = span_cost = 0
        sums = dict.fromkeys(("total", "cost") + TOKEN_NAMES, 0)
        for key, row in rows:
            day_sums = row.get("tokens") or {}
            ratio = day_ratio(key)
            out.writerow([key, date.fromisoformat(key).strftime("%a"), _cell(row.get("week_end")),
                          _cell(row.get("used")), _cell(day_sums.get("total")), money(day_sums.get("cost")),
                          round(ratio["tokens"]) if ratio else "", money(ratio["cost"]) if ratio else "",
                          *(_cell(day_sums.get(name)) for name in TOKEN_NAMES), row.get("note") or ""])
            used += row.get("used") or 0
            for name in sums:
                sums[name] += day_sums.get(name) or 0
            if row.get("span"):
                points += row["span"]["points"]
                span_tokens += row["span"]["tokens"]
                span_cost += row["span"]["cost"]
        out.writerow(["Total", "", "", used, sums["total"], money(sums["cost"]),
                      round(span_tokens / points) if points else "", money(span_cost / points) if points else "",
                      *(sums[name] for name in TOKEN_NAMES), ""])
        write_points(out)

    _write_file(record_path(month), write, encoding="utf-8-sig", newline="")


def _write_file(path, write, **options):
    """Write through a temporary file and swap it in, so a reader never sees half."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", **options) as f:
            write(f)
        os.replace(temporary, path)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _iso(when):
    return when.isoformat(timespec="seconds") if when else None


def _pace_fields(row, now):
    p = pace(row, now)
    return {"day_of_week": p["day"] if p else None,
            "expected_percent": round(p["expected"], 1) if p else None,
            "difference": round(p["difference"], 1) if p else None}


def _ratio_figures(now):
    def plain(m):
        return {"tokens": round(m["tokens"]), "api_usd": round(m["cost"], 2), "points": m["points"]} if m else None
    last = last_point()
    figures = {"last_point": plain(last), "today": plain(day_ratio(now.date().isoformat())),
               "this_week": plain(week_ratio())}
    if last:
        figures["last_point"].update({"from_percent": last["from"], "to_percent": last["to"], "at": last["at"]})
    return figures


def _token_figures():
    counted = state["tokens"]
    if not counted:
        return None
    return {"since": _iso(counted["since"]), "today": counted["today"], "this_week": counted["week"]}


def save_figures():
    """Write the latest figures, replacing the file in one step so a reader never sees half."""
    updated = datetime.fromtimestamp(state["updated"]).astimezone() if state["updated"] else None
    now = _now()
    next_check = datetime.fromtimestamp(state["next_check"]).astimezone() if state["next_check"] else None
    figures = {
        "plan": state["plan"],
        "updated": _iso(updated),
        "last_check": _iso(now),
        "next_check": _iso(next_check),
        "error": state["error"],
        "no_plan_limits": state["no_limits"],
        "limits": [{"name": row["label"], "percent_used": row["percent"], "resets": _iso(row["resets"]),
                    **_pace_fields(row, now)}
                   for row in state["limits"]],
        "tokens": _token_figures(),
        "tokens_per_percent": _ratio_figures(now),
    }
    try:
        _write_file(FIGURES_FILE, lambda f: json.dump(figures, f, indent=2), encoding="utf-8")
    except OSError:
        pass   # the tray icon still works without the file


def _saved_time(text):
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def load_figures():
    """The last good figures from the figures file, so that after a restart
    (during a long wait for the usage server, say) they are on show at once,
    greyed until the first good check. Returns the seconds still to wait
    when the usage server had asked to be left alone until later, else 0."""
    try:
        with open(FIGURES_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return 0
    if not isinstance(saved, dict):
        return 0
    rows = [_row(entry.get("name"), entry.get("percent_used"), entry.get("resets"))
            for entry in saved.get("limits") or [] if isinstance(entry, dict) and isinstance(entry.get("name"), str)]
    state["limits"] = [row for row in rows if row]
    state["plan"] = saved.get("plan") if isinstance(saved.get("plan"), str) else None
    state["updated"] = _saved_time(saved.get("updated"))
    state["no_limits"] = saved.get("no_plan_limits") is True
    state["error"] = "Checking..."
    error, next_check = saved.get("error"), _saved_time(saved.get("next_check"))
    if isinstance(error, str) and "asked to wait" in error and next_check and next_check > time.time():
        state["error"], state["next_check"] = error, next_check
        return next_check - time.time()
    return 0


def _plan_kind(answer):
    """The plan's kind as the usage answer gives it, such as "max" or "pro"."""
    kind = answer.get("subscription_type")
    return kind.strip().lower() if isinstance(kind, str) and kind.strip() else None


class ServerSaidNo(UsageError):
    """The usage server refused or could not be reached; hold is how long it asked to be left alone."""

    def __init__(self, message, hold=None, busy=False):
        super().__init__(message)
        self.hold, self.busy = hold, busy


def check():
    """One check. Returns the seconds to wait before the next one."""
    reply = ask_usage()
    answer, fetch = reply["answer"], reply["fetch"] or {}
    rows = read_limits(answer)
    kind = _plan_kind(answer)
    if kind:
        state["plan"] = kind.title()
    if fetch.get("failed"):
        # The usage server said no. claude.exe may still have sent its own saved
        # copy, up to an hour old: shown only when the app has nothing better.
        if rows and not state["limits"]:
            state["limits"] = rows
        hold = min(fetch["hold"], HOLD_LONGEST) + HOLD_MARGIN if fetch.get("hold") else None
        busy = fetch.get("status") == 429 or bool(fetch.get("hold"))
        raise ServerSaidNo(fetch_problem(fetch, time.time() + hold if hold else None), hold, busy)
    if answer.get("rate_limits_available") is False:
        state.update(limits=[], plan_key=kind, updated=time.time(), error=None, no_limits=True)
        return NO_LIMITS_INTERVAL
    if not rows:
        raise UsageError("Claude Code sent no usage figures; it may have changed, or the usage server did not answer")
    state.update(limits=rows, plan_key=kind, updated=time.time(), error=None, no_limits=False)
    warn_if_ahead()
    return QUIET_INTERVAL if time.time() < state["quiet_until"] else REFRESH_INTERVAL


def _wait_after(exc):
    """Seconds before the next check after a failed one: what the usage server
    asked for, else a wait that doubles with each failure in a row."""
    state["failures"] += 1
    if isinstance(exc, ServerSaidNo) and exc.busy:
        state["quiet_until"] = time.time() + QUIET_FOR
        state["busy"] += 1
        if exc.hold:
            return max(exc.hold, REFRESH_INTERVAL)
        return min(BUSY_WAIT * 2 ** (state["busy"] - 1), BUSY_LONGEST)
    return min(RETRY_INTERVAL * 2 ** (state["failures"] - 1), RETRY_LONGEST)


def refresh_loop(first_wait=0):
    """first_wait: seconds the usage server still wants to be left alone after a restart."""
    if first_wait > 0:
        count_tokens()
        save_figures()
        refresh_icon()
        _refresh.wait(first_wait)
        _refresh.clear()
    while True:
        try:
            wait = check()
            state["failures"] = state["busy"] = 0
        except Exception as exc:
            state["error"] = str(exc) or type(exc).__name__
            wait = _wait_after(exc)
        state["next_check"] = time.time() + wait
        count_tokens()
        if state["error"] is None and not state["no_limits"]:
            try:
                update_record()
            except Exception:
                pass   # the icon and the figures file carry on without it
        save_figures()
        refresh_icon()
        _refresh.wait(wait)
        _refresh.clear()


# ── Start with Windows ─────────────────────────────────────────────────────────
# An entry in the user's own Run list, so no administrator rights are needed.
# Only the built exe can add itself: from source, sys.executable is Python.

RUN_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "UsageTray"
BUILT    = getattr(sys, "frozen", False)


def autostart_enabled():
    """True only if the entry starts this very exe, so a moved exe shows unticked."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command, _ = winreg.QueryValueEx(key, RUN_NAME)
    except OSError:
        return False
    return os.path.normcase(str(command).strip().strip('"')) == os.path.normcase(sys.executable)


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


# ── Tray icon ──────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=64)
def _font(size):
    try:
        return ImageFont.truetype("arialbd.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def _draw_fitted_text(draw, box, text, fill):
    """Draw text centred in box at the largest size that fits."""
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    for size in range(height, 5, -1):
        font = _font(size)
        left, top, right, bottom = font.getbbox(text)
        if right - left <= width - 5 and bottom - top <= height - 4:
            break
    x = x0 + (width - (right - left)) / 2 - left
    y = y0 + (height - (bottom - top)) / 2 - top
    draw.text((x, y), text, fill=fill, font=font)


def _color(percent):
    if state["error"]:
        return FADED   # an old figure
    if percent >= DANGER_PERCENT:
        return DANGER
    return WARN if percent >= WARN_PERCENT else TEXT


def gap_icon_text(difference):
    """Whole numbers with a sign: "+3" ahead, "−10" behind."""
    rounded = abs(round(difference))
    return f"+{rounded}" if difference > 0 else f"−{rounded}"


def make_icon_image():
    """The weekly figure over its gap to the expected usage. Without a reset
    time to compare against: the session limit over the weekly one, or one alone."""
    image = Image.new("RGBA", (64, 64), BACKGROUND + (255,))
    draw = ImageDraw.Draw(image)
    week = next((row for row in state["limits"] if row["label"] == "Week"), None)
    week_pace = pace(week) if week else None
    if week_pace:
        gap = week_pace["difference"]
        _draw_fitted_text(draw, (0, 0, 64, 32), str(week["percent"]), _color(week["percent"]))
        _draw_fitted_text(draw, (0, 32, 64, 64), gap_icon_text(gap),
                          FADED if state["error"] else DANGER if gap > 0 else GOOD)
        return image
    shown = [row for row in state["limits"] if row["label"] in ("Session", "Week")] or state["limits"][:1]
    if not shown:
        failed = state["error"] is not None and not state["no_limits"]
        _draw_fitted_text(draw, (0, 0, 64, 64), "?" if failed else "--", DANGER if failed else FADED)
    elif len(shown) == 1:
        percent = shown[0]["percent"]
        _draw_fitted_text(draw, (0, 0, 64, 64), f"{percent}%", _color(percent))
    else:
        for row, box in zip(shown, ((0, 0, 64, 32), (0, 32, 64, 64))):
            _draw_fitted_text(draw, box, str(row["percent"]), _color(row["percent"]))
    return image


def _title():
    plan = state["plan"]
    if not plan:
        return "Claude usage"
    return f"Claude {plan.title() if plan.islower() else plan} usage"


def _clock(epoch):
    """"14:05" today, "Tue 14:05" on another day."""
    when = time.localtime(epoch)
    same_day = time.strftime("%Y%m%d", when) == time.strftime("%Y%m%d")
    return time.strftime("%H:%M" if same_day else "%a %H:%M", when)


def status_text():
    updated = _clock(state["updated"]) if state["updated"] else None
    if state["error"]:
        text = f"Not updated since {updated}: {state['error']}" if updated else state["error"]
        if state["next_check"] and state["next_check"] - time.time() > 90 and "wait until" not in text:
            text += f"; next try {_clock(state['next_check'])}"
        return text
    if state["no_limits"]:
        return f"No plan limits for this sign-in (checked {updated})"
    return f"Updated {updated}" if updated else "Checking..."


def _tooltip_row(row):
    p = pace(row)
    text = f"{row['label']}: {row['percent']}%"
    return f"{text}, {gap_words(p['difference'])}" if p else text


def _per_point(m):
    return f"{token_text(round(m['tokens']))} tokens, ${m['cost']:.2f}"


def _last_name(m):
    return "Last 1%" if m["points"] == 1 else f"Last {m['points']}%, per 1%"


def ratio_line():
    """Such as "Last 1%: 45.6M tokens, $20.31"; fixed until the next 1% is completed."""
    if not state["tokens"]:
        return None
    m = last_point()
    return f"{_last_name(m)}: {_per_point(m)}" if m else "Last 1%: measuring"


def make_tooltip():
    """Windows tray tooltips hold 127 characters. Lines that do not fit are left
    out whole, the title first, then the weekly limits for single models."""
    counted = state["tokens"]
    lines = [(2, _title())]
    for row in state["limits"]:
        text = _tooltip_row(row)
        if row["label"] in ("Session", "Week"):
            lines.append((9, text))
        else:
            lines.append((1, text[len("Week, "):] if row["label"].startswith("Week, ") else text))
    if counted:
        lines.append((9, f"Tokens: {token_text(counted['today']['total'])} today, "
                         f"{token_text(counted['week']['total'])} week"))
    if ratio_line():
        lines.append((9, ratio_line()))
    lines.append((9, status_text()))
    for least in (2, 1):
        while len("\n".join(text for _, text in lines)) > 127 and any(p == least for p, _ in lines):
            lines.remove(next(line for line in lines if line[0] == least))
    return "\n".join(text for _, text in lines)[:127]


def refresh_icon():
    icon = icon_ref[0]
    if icon is not None:
        icon.icon = make_icon_image()
        icon.title = make_tooltip()


# ── Menu ───────────────────────────────────────────────────────────────────────

def _limit_text(row):
    text = f"{row['label']}: {row['percent']}%"
    if row["resets"]:
        text += f"   (resets {row['resets'].strftime('%a %d %b %H:%M')})"
    return text


def on_refresh(icon, item):
    _refresh.set()


def on_toggle_autostart(icon, item):
    try:
        set_autostart(not autostart_enabled())
    except OSError:
        pass   # the checkbox keeps showing what the registry really holds


def on_exit(icon, item):
    icon.stop()


def _limit_lines(row):
    """The figure, and under it how it compares with the expected usage."""
    compared = pace_text(row)
    return [_limit_text(row), "      " + compared] if compared else [_limit_text(row)]


def _token_lines():
    """Today's and this week's tokens, each with its parts under it."""
    counted = state["tokens"]
    if not counted:
        return []
    lines = []
    for name, key in (("today", "today"), ("this week", "week")):
        sums = counted[key]
        lines.append(f"Tokens {name}: {token_text(sums['total'])}   (${sums['cost']:,.2f} at API prices)")
        if sums["total"]:
            lines.append("      " + _token_parts(sums))
    return lines


def _ratio_lines():
    """The last completed 1%, then today's and this week's figures per 1%."""
    if not state["tokens"]:
        return []
    m = last_point()
    if not m:
        return ["Last 1%: measuring, once the weekly figure goes up twice while the app watches"]
    done = datetime.fromisoformat(m["at"]).strftime("%H:%M")
    lines = [f"{_last_name(m)} ({m['from']}% to {m['to']}%, reached {done}): {_per_point(m)} at API prices",
             f"      give or take {token_text(round(m['spread']))} tokens for the minutes between checks"]
    also = []
    today, week = day_ratio(_now().date().isoformat()), week_ratio()
    if today:
        also.append(f"today {_per_point(today)}")
    if week and week["points"] > m["points"]:
        also.append(f"this week {_per_point(week)} over {week['points']} points")
    if also:
        lines.append("      Per 1%: " + "; ".join(also))
    return lines


def on_open_record(icon, item):
    """This month's spreadsheet, or the folder until there is one."""
    path = record_path(f"{_now():%Y-%m}")
    try:
        os.makedirs(RECORDS_FOLDER, exist_ok=True)
        os.startfile(path if os.path.exists(path) else RECORDS_FOLDER)
    except OSError:
        pass


def menu_items():
    """Menu contents, rebuilt each time the menu opens."""
    return (
        MenuItem(_title(), None, enabled=False),
        *(MenuItem(line, None, enabled=False) for row in state["limits"] for line in _limit_lines(row)),
        *(MenuItem(line, None, enabled=False) for line in _token_lines()),
        *(MenuItem(line, None, enabled=False) for line in _ratio_lines()),
        MenuItem(status_text(), None, enabled=False),
        Menu.SEPARATOR,
        MenuItem("Open this month's record", on_open_record),
        MenuItem("Refresh now", on_refresh),
        MenuItem("Start with Windows", on_toggle_autostart, enabled=BUILT,
                 checked=lambda item: autostart_enabled()),
        MenuItem("Exit", on_exit),
    )


_WM_RBUTTONUP = 0x0205


class TrayIcon(pystray.Icon):
    """Tray icon whose menu is rebuilt on each right-click, in the icon's own
    thread, so its figures are current and an open menu is never replaced."""

    def _on_notify(self, wparam, lparam):
        if lparam == _WM_RBUTTONUP:
            self.update_menu()
        super()._on_notify(wparam, lparam)


def main():
    load_record()
    first_wait = load_figures()
    icon = TrayIcon("UsageTray", make_icon_image(), make_tooltip(), menu=Menu(menu_items))
    icon_ref[0] = icon
    threading.Thread(target=refresh_loop, args=(first_wait,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
