# UsageTray

A small Windows tray app that shows how much of your Claude plan you have used, as reported by Claude Code itself.

UsageTray is an independent, unofficial project. It is not made, endorsed or supported by Anthropic. "Claude" and "Claude Code" are names of Anthropic products and are used here only to say what the app works with.

## What it shows

**The icon** shows the weekly figure on top and, below it, how far you are from an even pace through the week. Each weekly limit is shared out evenly over its 7 days, so by the end of day 3 you would expect to have used 3/7 of it (about 42.9%). `+3` means 3 points ahead of that pace (shown in red), `−10` means 10 points behind it (shown in green). The weekly figure turns orange at 75% and red at 90%.

**The tooltip and right-click menu** show:

- every limit your plan reports (session, week, weekly limits for single models, extra usage), each with its reset time and its pace for the day;
- the tokens Claude Code used on this computer today and since the weekly reset, with what they would have cost at API prices;
- what the last completed 1% of your weekly limit cost, in tokens and API dollars;
- when the figures were last updated, or why a check failed.

**Notifications** appear the first time in a day that a weekly limit gets ahead of its pace, and when the API value of 1% shifts by 25% or more, which can mean the limit itself has changed.

**A monthly spreadsheet** (`Monthly records\Usage <year>-<month>.csv`) gets one row per day and, below the days, one row for every completed 1% of the weekly limit, with weekly and monthly averages. The menu item "Open this month's record" opens it.

## How it gets the figures

Every 5 minutes UsageTray starts the official `claude.exe`, asks it for usage the same way the VS Code extension's Account & usage dialog does, and closes it again.

- **No prompt is sent**, so a check does not count towards your plan.
- **Your sign-in stays inside `claude.exe`.** UsageTray never opens Claude's credentials file and never sees your sign-in token.
- Hooks, plugins and MCP servers are turned off for the check, and any `CLAUDE*` or `ANTHROPIC*` environment variables are left out, so an API key in your environment cannot replace your plan's figures.
- When the usage server answers "too many requests", UsageTray waits as long as the server asks and checks less often for the rest of the day.

The token counts come from the chat files Claude Code saves under `%USERPROFILE%\.claude\projects`. They cover Claude Code on this computer only; Claude in a browser, on a phone or on another computer counts towards your plan percentages but not towards these token totals.

**Please note:** the usage request is marked experimental in Claude Code, so a Claude Code update may change or remove it. If that happens the icon shows `?` and the menu gives the reason, rather than showing a wrong number.

## Requirements

- Windows 10 or 11
- Claude Code installed (standalone, on the `PATH`, or inside the VS Code extension) and signed in with a Claude plan
- To run from source: Python 3.11 or later, with `pystray` and `Pillow`

## Run it

From source:

```powershell
pip install -r requirements.txt
pythonw usage_tray.py
```

As a single `.exe` (needs PyInstaller):

```powershell
pip install pyinstaller
.\build.ps1
.\dist\UsageTray.exe
```

The menu item "Start with Windows" adds the built `.exe` to your own startup list (no administrator rights needed). It is available only in the built `.exe`.

## Files it writes

| File | What it holds |
|---|---|
| `%LOCALAPPDATA%\UsageTray\usage.json` | The latest figures, rewritten after every check (see below) |
| `%LOCALAPPDATA%\UsageTray\daily.json` | The data behind the monthly spreadsheets |
| `Monthly records\Usage <year>-<month>.csv` | The monthly spreadsheets, next to the app (next to `dist` when built) |
| `%TEMP%\UsageTray check.log` | Claude Code's log of one check, deleted straight after |
| `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` | A `UsageTray` entry, only if you tick "Start with Windows" |

## Reading the figures from other programs

`usage.json` lets scripts, dashboards or a Claude Code session read your usage without starting `claude.exe`. An example:

```json
{
  "plan": "Max",
  "updated": "2026-11-12T10:05:00+05:00",
  "last_check": "2026-11-12T10:05:00+05:00",
  "next_check": "2026-11-12T10:10:00+05:00",
  "error": null,
  "no_plan_limits": false,
  "limits": [
    {"name": "Session", "percent_used": 18, "resets": "2026-11-12T13:00:00+05:00",
     "day_of_week": null, "expected_percent": null, "difference": null},
    {"name": "Week", "percent_used": 52, "resets": "2026-11-16T00:00:00+05:00",
     "day_of_week": 4, "expected_percent": 57.1, "difference": -5.1}
  ],
  "tokens": {
    "since": "2026-11-09T00:00:00+05:00",
    "today": {"total": 61500000, "input": 41000, "cache_write": 1900000,
              "cache_read": 59300000, "output": 259000, "replies": 812, "cost": 31.42},
    "this_week": {"total": 230400000, "input": 152000, "cache_write": 7100000,
                  "cache_read": 222100000, "output": 1048000, "replies": 3044, "cost": 118.75}
  },
  "tokens_per_percent": {
    "last_point": {"tokens": 4400000, "api_usd": 2.31, "points": 1,
                   "from_percent": 51, "to_percent": 52, "at": "2026-11-12T09:47:30+05:00"},
    "today": {"tokens": 4600000, "api_usd": 2.38, "points": 3},
    "this_week": {"tokens": 4430000, "api_usd": 2.28, "points": 52}
  }
}
```

- `updated` is the last good check. After a failed check the last good figures stay, and `error` says why.
- In `limits`, `percent_used` is in whole percent. For weekly limits, `day_of_week` (1 to 7), `expected_percent` (the use expected by the end of that day) and `difference` (`percent_used` minus `expected_percent`) are filled in; a negative difference means you are behind the expected usage.
- Each part of `tokens_per_percent` stays `null` until there is a completed 1% to measure it from.

## Limits of the figures

- Plan usage comes in whole percent only. Finer figures would need a real model request, which would cost usage.
- API prices are a table kept by hand (`PRICES` in `usage_tray.py`). A model missing from it is priced as Claude Opus 5.5. Corrections are welcome.
- A change of plan makes the weekly figure jump. UsageTray notices this and starts its 1% measurements again from there.

## License

MIT. See [LICENSE](LICENSE).
