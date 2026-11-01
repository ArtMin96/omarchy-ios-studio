# MCP server evaluation

`evaluation.xml` holds fourteen questions in the format of the [mcp-builder](https://github.com/anthropics/skills/tree/main/skills/mcp-builder) evaluation guide. Each question is read-only and stands on its own, needs more than one fact, and has a single answer checked by string comparison. They run against demo mode (`IOSSTUDIO_DEMO=1`), whose made-up account, iPhone, projects, crash reports, camera roll, app files and metrics session always give the same answers. Question 13 starts a measurement (`ios_metrics_start`), which changes nothing on the phone; every other question only reads.

## Run it with an agent

The mcp-builder skill's `scripts/evaluation.py` runs Claude against the server and reports accuracy, tool calls and the agent's feedback on the tools. It needs `ANTHROPIC_API_KEY`. Keep it in a separate venv, because it uses the `mcp` 1.x client API:

```bash
git clone --depth 1 https://github.com/anthropics/skills /tmp/skills
cd /tmp/skills/skills/mcp-builder/scripts
python -m venv .venv && .venv/bin/pip install anthropic "mcp<2"
.venv/bin/python evaluation.py -t stdio -c /path/to/omarchy-ios-studio/bin/iosstudio-mcp \
  -e IOSSTUDIO_DEMO=1 -o report.md /path/to/omarchy-ios-studio/mcp/evaluation.xml
```

## Results (2026-11-01)

This run had no API key. Each question was solved by hand in an MCP client over stdio, using only the server's tools in demo mode.

| # | Topic | Tools used | Calls | Result |
|---|---|---|---|---|
| 1 | Failed build: file:line of the protocol error | `ios_project_list`, `ios_build_diagnostics` | 2 | pass |
| 2 | Branch of the failing project | `ios_project_list` | 1 | pass |
| 3 | Days left on the auto-renewed app's profile | `ios_signing_status` | 1 | pass |
| 4 | Exception type of the latest crash | `ios_crash_list`, `ios_crash_get` | 2 | pass |
| 5 | Topmost own-code frame in the HabitTracker crash | `ios_crash_list(bundle_id)`, `ios_crash_get` | 2 | pass |
| 6 | Registered (XTL-) bundle id | `ios_account_status` | 1 | pass |
| 7 | Connection used by the last renewal | `ios_build_history` | 1 | pass |
| 8 | Running apps that aren't your own builds | `ios_app_list(scope='all')` | 1 | pass |
| 9 | Free storage, in percent | `ios_device_info` | 1 | pass |
| 10 | Template with the most Swift files | `ios_project_templates` | 1 | pass |
| 11 | Higher latency of the two conditions blocked over Wi-Fi | `ios_network_conditions` | 1 | pass |
| 12 | Length of the newest camera roll video | `ios_media_list(kind='video')` | 1 | pass |
| 13 | Longest hang in the weather app's metrics report | `ios_metrics_start`, `ios_metrics_summary` | 2 | pass |
| 14 | Name of the weather app's exported CSV | `ios_files_list(depth=4)` | 1 | pass |

All 14 passed, with 1.3 calls per question on average. Questions 11 to 14 were added with the Network, Files and Performance tools and checked the same way, through the MCP Python SDK against `IOSSTUDIO_DEMO=1`.

The pass found these problems, which are now fixed:

- Crash report names changed between calls. The demo timestamps moved with the clock, so a name from `ios_crash_list` could fail in `ios_crash_get`, and a bare file name didn't resolve. Names now stay stable.
- Unhelpful "not found" error. `ios_crash_get` now lists the newest report names when a name doesn't match. `ios_crash_list` reports `has_more` and says when results were cut off.
- Inconsistent demo data. pids in `ios_app_list` and `ios_process_list` disagreed, and two apps shared a pid. `ios_status` also gave a different iOS version from `ios_device_info`.
- Missing parameter descriptions. Several `offset`/`limit` parameters had none, and "Filter" was too vague.
- A dead end in the demo workflow. In demo mode, `ios_project_create`/`add`/`convert` returned no next step. They now point to the demo projects.
