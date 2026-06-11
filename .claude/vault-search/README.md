# vault-search daemon

Persistent local MCP server that indexes the Obsidian vault and exposes 13 MCP tools (search, recall, topology, workflow, file-relationships) over Streamable HTTP, plus REST shims at `/api/session_brief` and `/api/topology_status` for hook consumers.

## What's in here

| File | Purpose |
|---|---|
| `server.py` | FastMCP entry point. Registers all 13 MCP tools and the `/health`, `/api/session_brief`, `/api/topology_status` HTTP routes. |
| `tools.py` | The 13 MCP tool handlers (RRF fusion, title/heading boosts, topology and workflow queries). |
| `store.py` | sqlite-vec + FTS5 schema, upsert, hybrid query primitives. |
| `indexer.py` | Markdown chunking, frontmatter parsing, watchdog observer, indexer thread. |
| `topology.py` | Cached PARA + wikilink-graph topology feeding the topology and project tools. |
| `workflow.py` | Workflow-cluster builder (Jaccard similarity over shared References). |
| `config.yaml` | Vault path, port, exclude paths, model config, cadence overrides. Ships with `<VAULT_ROOT>`/`<HOME>`/`<DATA_DIR>` placeholders; `install_daemon.py` substitutes real paths. |
| `requirements.txt` | Pinned Python deps (FastMCP 3.2, sqlite-vec 0.1.9, sentence-transformers 5+). |
| `install_daemon.py` | One-command installer (stdlib only): creates the venv, installs deps, writes config. Does NOT start the server. |
| `install-service.ps1` | NSSM service install for Windows (idempotent; needs admin). Run `install_daemon.py` first. |
| `install-autostart.ps1` | Logon-task install for Windows via Task Scheduler (idempotent; no admin). Run `install_daemon.py` first. |
| `test-restart.ps1` | Verifies NSSM auto-restart on kill. |
| `.venv/` | Virtualenv created by `install_daemon.py` (not committed). |
| `data/` | Runtime data — index db + logs — created by `install_daemon.py` (not committed). |

Entry point is `server.py` (`python server.py`, run with the venv's interpreter). `indexer.py` is a library module imported by the server, not a launch target.

Index database and logs default to `<vault>/.claude/vault-search/data/` (`index.db`, `daemon.log`; under NSSM also `stdout.log`/`stderr.log`). `install_daemon.py` resolves `<DATA_DIR>` to that path and creates it; pass `--data-dir` to relocate.

## What ships

**Indexing.** Hybrid BM25+vector search; PARA classification from frontmatter and path; title/section/path lexical boosts; Index penalty; watchdog with 2s debounce; content-hash skip on unchanged files; 24h workflow re-clustering + 6h periodic safety rescan.

**MCP tools** (13, all registered in `server.py`):

- `vault_search` — hybrid retrieval ranked by BM25 + vector + boosts.
- `vault_index_status` — file/chunk counts, last update, uptime.
- `vault_recall` — session-log retrieval ranked by similarity + recency (60-day decay).
- `vault_find_related` — semantic + wikilink relatives for a given vault file.
- `vault_get_references_for_path` — references cited from a file (forward graph walk).
- `vault_get_projects_using` — projects citing a Reference (backward graph walk).
- `vault_get_bridge_references` — references cited by both of two given projects.
- `list_projects` — every project with its references, sessions, and last touch.
- `list_reference_domains` — Reference and Snippet domains with their indexes and citing projects.
- `topology_status` — PARA topology summary plus quality gaps.
- `vault_find_similar_projects` — Jaccard similarity over shared References.
- `detect_workflow` — match a context string against persisted workflow clusters.
- `session_brief` — composite session-start brief.

**HTTP shims.** `/health`, `/api/session_brief?cwd=<path>`, `/api/topology_status` — for hooks and clients that can't speak MCP.

**Open params.** `para_position` and `boost_linked_to` are accepted by `vault_search` but currently unused.

## Auto-launch setup

The daemon does not auto-launch when your machine starts. First run `install_daemon.py`
(creates `.venv/`, installs deps, writes `config.yaml`), then configure an
OS-appropriate launcher. In every template below, `<venv-python>` is the
interpreter the installer created:

- Windows: `<vault>\.claude\vault-search\.venv\Scripts\python.exe`
- macOS / Linux: `<vault>/.claude/vault-search/.venv/bin/python`

and the launch target is always `server.py` in that same folder.

### Windows

Two installers ship; both auto-locate the daemon from their own folder. Pick one:

| | `install-autostart.ps1` (Task Scheduler) | `install-service.ps1` (NSSM) |
|---|---|---|
| Admin required | no | yes |
| External dependency | none | NSSM at `C:\Tools\nssm\nssm.exe` |
| Crash restart | no — relaunches at next logon | yes (5s `AppRestartDelay`) |
| Runs while logged out | no | yes |

**Task Scheduler (default — no admin):**

```powershell
# Install (one-time). Run install_daemon.py first.
.\install-autostart.ps1

# Verify
Get-ScheduledTask note-kit-vault-search
.\.venv\Scripts\python.exe daemonctl.py status

# Remove
.\install-autostart.ps1 -Uninstall
```

The logon task runs `daemonctl.py start` under `pythonw.exe` (no console
window); `daemonctl` exits once the server is detached and healthy, so the
task itself is short-lived. Between logons, manage the daemon with
`daemonctl.py start|stop|restart|status`.

**NSSM service (admin — adds crash-restart supervision):**

```powershell
# Install (admin required, one-time). Run install_daemon.py first.
.\install-service.ps1

# Verify
Get-Service vault-search

# Manual start/stop after install
C:\Tools\nssm\nssm.exe start vault-search
C:\Tools\nssm\nssm.exe stop vault-search
```

`install-service.ps1` points the service at `.venv\Scripts\python.exe server.py`.
Install only one of the two — a service and a logon task racing for port 8765
leaves the loser exiting with a bind error at every boot.

### macOS

Create `~/Library/LaunchAgents/com.note-kit.vault-search.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.note-kit.vault-search</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/vault/.claude/vault-search/.venv/bin/python</string>
    <string>/path/to/vault/.claude/vault-search/server.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

Load: `launchctl load ~/Library/LaunchAgents/com.note-kit.vault-search.plist`.

### Linux

Create `~/.config/systemd/user/vault-search.service`:

```ini
[Unit]
Description=note-kit vault-search daemon
After=network.target

[Service]
ExecStart=/path/to/vault/.claude/vault-search/.venv/bin/python /path/to/vault/.claude/vault-search/server.py
Restart=on-failure

[Install]
WantedBy=default.target
```

Enable: `systemctl --user enable --now vault-search`.

## Operating (Windows / NSSM)

```powershell
# Status
Get-Service vault-search

# Start / stop
C:\Tools\nssm\nssm.exe start vault-search
C:\Tools\nssm\nssm.exe stop vault-search

# Health
curl http://127.0.0.1:8765/health
# 200 = healthy and warmed; 503 = warming or warmup error

# Tail logs (data dir defaults to <vault>\.claude\vault-search\data)
Get-Content -Wait .\data\daemon.log

# Re-install (e.g. after editing service config)
.\install-service.ps1   # from elevated PowerShell
```

## Updating the daemon

```powershell
C:\Tools\nssm\nssm.exe stop vault-search

# (edit code or update deps)
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --upgrade

C:\Tools\nssm\nssm.exe start vault-search
```

If you change the SQLite schema, bump `SCHEMA_VERSION` in `store.py`. The daemon detects the mismatch on startup, drops, and rebuilds (~1 minute on a ~350-file vault).

## Force a full reindex

```powershell
C:\Tools\nssm\nssm.exe stop vault-search
Remove-Item .\data\index.db*    # data dir = <vault>\.claude\vault-search\data
C:\Tools\nssm\nssm.exe start vault-search
```

## Troubleshooting

**`/health` stays 503.** Check `daemon.log` (and, under NSSM, `stdout.log`/`stderr.log`) in the data dir — `<vault>\.claude\vault-search\data\` by default — for tracebacks during warmup. Most likely: model download failed (no network on first run) or db permissions.

**Service won't start.** `Get-EventLog -LogName Application -Source nssm -Newest 10`. NSSM logs service-supervisor errors to the Windows event log; daemon stdout/stderr go to the data dir's logs.

**Searches return only old/stale results.** The 6h safety rescan catches missed events. Force-trigger by stopping/starting the service. If still stale, delete the db and let it rebuild.

**Port 8765 conflict.** Edit `config.yaml`'s `port`, restart the service, then update `hooks/session-start-context.py` which currently hardcodes `http://127.0.0.1:8765`. A future config-aware loader in the hook removes this second step.

## Validation

- `/health` returns 200 once warmed; `chunk_count` should roughly equal vault file count × ~10.
- Watchdog picks up file edits within ~5s of the save.
- NSSM restarts the daemon on kill — see `test-restart.ps1`.

Warm restart latency (port → ready) is dominated by model load (~7s) plus NSSM `AppRestartDelay` (5s). End-to-end ~14s for `/health` to return 200 after a kill. Port itself reopens in ~6s.
