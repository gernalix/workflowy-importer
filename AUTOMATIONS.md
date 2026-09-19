# Automation map

`workflowy-importer` is also a small local automation bridge. The goal is one trusted layer around the Workflowy API instead of many scripts that each handle credentials, node IDs, retries and deduplication differently.

## Deployment decision

Use one canonical runtime: the ThinkPad/Fedora workstation.

- Workflowy API key, SQLite cache, bridge, routing and timers live on the ThinkPad.
- Do not duplicate this stack on the Oracle VM and do not add an SSH tunnel merely to keep it online 24/7.
- Browser capture, ActivityWatch, PersonalHub data, local Markdown and `~/projects` are already workstation-local, so colocating the automation layer removes unnecessary network/state synchronization.
- The systemd timers are persistent: missed scheduled runs can be recovered after the ThinkPad starts again.
- Reconsider the Oracle VM only if a future Workflowy workflow demonstrably needs to execute while the ThinkPad is off.

## Safety defaults

- API key: `~/.config/codex/secrets/workflowy-api-key`, regular file, owned by the current user, mode `0600`.
- Environment fallback is accepted only when the canonical secret file is absent.
- The localhost capture server binds only to loopback and accepts browser POSTs only from extension origins.
- Destructive/bulk actions are report-only unless `--apply` is explicitly supplied.
- Ambiguous PersonalHub/name matches are skipped.
- Routing falls back to Inbox instead of guessing.
- Workflowy binary attachment upload is deliberately not implemented because the public API has no upload endpoint.

## The 30 ideas and where they live

| # | Idea | Implementation |
|---:|---|---|
| 1 | Markdown import/sync | Existing `workflowy-import-md`; tracked import plus explicit replacement guard. |
| 2 | Backup/replica | `wf backup` JSON/Markdown + daily systemd template. |
| 3 | Local machine index | `wf sync` → SQLite/FTS cache; used by automations, not intended to replace Workflowy search for humans. |
| 4 | Daily mirror automation | Routing and `wf resurface` can create mirrors under Today. |
| 5 | Calendar targets | `wf today`; all create/move primitives accept Workflowy calendar targets. |
| 6 | Quick Inbox | `wf inbox`, `wf add`, localhost `/capture`. |
| 7 | Inbox routing | `wf route` + ordered deterministic rules + optional high-confidence external classifier. |
| 8 | Hub-and-spoke without manual linking | One canonical node plus automatic mirrors, never copied text. |
| 9 | Resurfacing | `wf resurface --older-than-days ...`. |
| 10 | Project scaffolding/index | `wf projects --root ~/projects`; records local path as text and Git remote when available. |
| 11 | Codex/roadmap events | `wf roadmap-sync` mirrors the complete canonical roadmap with tags/backlinks and turns exact `running`/`PASS`/`FAIL` child nodes into single-writer mutations. Generic events remain available through `wf ingest`. |
| 12 | ChatGPT → Workflowy | `wf chatgpt` extracts one conversation from `conversations.json`; `browser-extension/` exports the current open chat privately through localhost. |
| 13 | GitHub → Workflowy | `wf ingest github event.json`, idempotent through event IDs/hashes. |
| 14 | ActivityWatch → Workflowy | `wf ingest activitywatch event.json`. |
| 15 | PersonalHub → Workflowy | `wf ingest personalhub ...`; `wf personalhub-links` enriches a chosen DB column with uniquely matched Workflowy URLs. |
| 16 | Calendar → Workflowy | `wf ingest calendar ...`; use Workflowy's native Calendar integration when its built-in behavior is sufficient. |
| 17 | Gmail → Workflowy | `wf ingest gmail ...`; upstream mail selection remains outside this repo. |
| 18 | Web clipping | Official clipper remains preferred; custom current-page/chat capture can use localhost `/capture`. |
| 19 | Deduplication | `wf dedupe` reports case/whitespace-equivalent titles; no automatic merge. |
| 20 | Normalization | `wf normalize`; explicit regex rules, dry-run unless `--apply`. |
| 21 | Completed-task cleanup | `wf archive-completed`; report-only unless `--apply`. |
| 22 | Weekly review | `wf weekly-review` + optional systemd timer. |
| 23 | Dashboards | Mirrors are exposed as a primitive (`wf mirror`); dashboard policy stays configuration, not hard-coded structure. |
| 24 | Workflowy as automation panel | `wf roadmap-sync` is the dedicated roadmap control surface; `wf control --parent ...` remains for explicitly allowlisted generic `RUN: action` commands, without a shell. |
| 25 | Human-in-the-loop | Actions execute only after an explicit `RUN:` node exists; unknown/non-allowlisted requests remain untouched. |
| 26 | CLI | `wf` is the common interface for humans, scripts and agents. |
| 27 | One local API layer | `wf serve` exposes a loopback-only capture endpoint; authenticated Workflowy access stays server-side. |
| 28 | AI access | The CLI/cache can be used by agents; Workflowy's official Desktop MCP remains preferable when direct AI browsing/editing is wanted. |
| 29 | Filesystem/project index | `wf projects`; local filesystem paths are stored as text because Workflowy local file links are not portable/reliable. |
| 30 | Filtered life log | All upstream systems can feed `wf ingest`; routing decides what deserves a Workflowy node and optionally a Today mirror. |

## Routing example

Copy `rules.example.json` to `~/.config/workflowy-bridge/rules.json` and replace destination shortcut keys with shortcuts that exist in your Workflowy.

Rules are evaluated in order. A rule only fires when its conditions clearly match. If no rule matches, the destination is `inbox`. An optional external classifier may be configured, but its result is accepted only above `classifier_min_confidence`.

## PersonalHub deep links

1. `wf sync`
2. `wf personalhub-links personalhub.db --table TABLE --name-column NAME --url-column workflowy_url`
3. Inspect `unique_matches`.
4. Re-run with `--apply` (and `--create-column` only when intentionally adding that column).

Only exact case-insensitive name matches that map to exactly one Workflowy node are written. Ambiguous names are ignored.

## Single ChatGPT conversation

Stable route from an account export:

```bash
wf chatgpt conversations.json --conversation-id CONVERSATION_ID --parent inbox
```

Immediate route for the currently open web chat:

```bash
wf serve
```

Then load `browser-extension/` as an unpacked Chromium extension and click it on the current ChatGPT conversation. The extension never receives the Workflowy key.

## Local services

`deploy/systemd/` contains optional user-service templates for:

- localhost capture bridge;
- periodic cache refresh;
- daily JSON backup;
- weekly review.

They are templates only until installed/enabled on the target Fedora account.
