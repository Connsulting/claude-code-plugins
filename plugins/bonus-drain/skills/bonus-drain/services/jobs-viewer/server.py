# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Web viewer for background jobs: bonus-drain queue + scheduled timers.

One page, two tabs:
  - Bonus-drain (default): what's remaining this week in drain order, a live
    weekly/5h usage header, and a recent-run log. Sourced from the bonus-drain
    SQLite ledger + bonusdb.sh/usage.sh in skills/bonus-drain.
  - Scheduled jobs: every `systemd --user` timer with its next/last fire, schedule
    expression, and the command/prompt it runs. Spent one-shots (already run, no
    future fire) are tucked into a collapsed "Archive" section, hidden by default.

This replaces the two former in-skill viewers (bonus-drain :8766, bg-schedule
:8767). It now serves both on a single port (:8766).

Localhost-agnostic: binds the configured loopback address and knows nothing about
Tailscale. HTTPS over the tailnet is fronted separately by `tailscale serve`.

Usage:
    python3 server.py [--port 8766] [--host 127.0.0.1]
"""
from __future__ import annotations

import argparse
import calendar as _cal
import functools
import html
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path

# services/jobs-viewer/ -> services/ -> installed Bonus Drain skill root.
# BONUS_SKILL_DIR remains overrideable so the same literal server can run from a source tree
# during validation without depending on the claude-settings checkout at runtime.
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from bonus_drain import config as graph_config  # noqa: E402
from bonus_drain import dispatcher as graph_dispatcher  # noqa: E402
from bonus_drain import usage as graph_usage  # noqa: E402
from bonus_drain.db import QueueDB  # noqa: E402
from bonus_drain.kick import kick_task  # noqa: E402

BONUS_SKILL_DIR = Path(os.environ.get("BONUS_SKILL_DIR", str(REPO)))
BONUSDB_SH = BONUS_SKILL_DIR / "bonusdb.sh"
ACCOUNTS_SH = BONUS_SKILL_DIR / "accounts.sh"
DB_PATH = Path(os.environ.get(
    "BONUS_DB", str(Path.home() / ".local" / "state" / "bonus-drain" / "queue.db")
))
DRAIN_LEAD_MAX_HOURS = int(os.environ.get("DRAIN_LEAD_MAX_HOURS", "30"))
CLAUDE_ACCOUNTS_STORE = Path(os.environ.get(
    "BONUS_ACCOUNTS_STORE", str(Path.home() / ".config" / "bonus-drain" / "accounts" / "claude")
))

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")
ALLOWED_ORIGINS: tuple[str, ...] = ()
MUTATIONS_ENABLED = False
VIEWER_BIND = "127.0.0.1"


def configure_request_boundary() -> None:
    """Load the secretless Tailscale boundary used by the HTTP handler."""

    global ALLOWED_HOSTS, ALLOWED_ORIGINS, DB_PATH, MUTATIONS_ENABLED, VIEWER_BIND
    cfg = graph_config.load_config(os.environ.get("BONUS_DRAIN_CONFIG"))
    viewer = cfg.viewer
    bind = str(viewer.get("bind", "127.0.0.1"))
    try:
        loopback = bind.lower() == "localhost" or ip_address(bind).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("viewer bind must be loopback")
    remote = viewer.get("remote") or {}
    if remote and remote.get("trusted_loopback_proxy") is not True:
        raise ValueError("remote viewer must be behind the trusted loopback proxy")
    hosts = remote.get("allowed_hosts") or ()
    origins = remote.get("allowed_origins") or ()
    if remote and (not hosts or not origins):
        raise ValueError("remote viewer requires exact hosts and origins")
    ALLOWED_HOSTS = tuple(hosts) if remote else ("127.0.0.1", "localhost")
    ALLOWED_ORIGINS = tuple(origins)
    DB_PATH = cfg.database
    MUTATIONS_ENABLED = viewer.get("mutations_enabled") is True
    VIEWER_BIND = bind


# ===========================================================================
# Bonus-drain data access
# ===========================================================================

def _num(s: str):
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def _grok_number(value) -> float | None:
    """Finite Grok numeric field, excluding bool (a Python int subclass)."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _normalize_usage(raw) -> dict | None:
    """Validate the four-field Claude/Codex usage contract before it reaches the page."""
    if not isinstance(raw, dict):
        return None
    values = {key: _grok_number(raw.get(key)) for key in ("u5", "r5", "u7", "r7")}
    if any(values[key] is None or not 0 <= values[key] <= 100 for key in ("u5", "u7")):
        return None
    for key in ("r5", "r7"):
        if values[key] is None or values[key] < 0 or not values[key].is_integer():
            return None
        values[key] = int(values[key])
    if values["r7"] <= 0:
        return None
    return values


def _provider_usage(provider_id: str) -> dict | None:
    """Translate normalized graph cache into the established viewer's window shape."""

    try:
        cfg = graph_config.load_config(os.environ.get("BONUS_DRAIN_CONFIG"))
        snapshots = graph_usage.read_all(cfg)
    except (graph_config.ConfigError, OSError, ValueError):
        return None
    candidates: list[tuple[int, int, float, int]] = []
    for account in cfg.accounts_for_provider(provider_id):
        snapshot = snapshots.get((provider_id, account.id))
        if snapshot is None or not snapshot.fresh:
            continue
        for limit in cfg.limits_for_plan(account.plan_id):
            reading = snapshot.limits.get(limit.id)
            if not reading:
                continue
            used, reset = reading.get("used_percent"), reading.get("resets_at")
            if isinstance(used, (int, float)) and isinstance(reset, (int, float)):
                candidates.append((snapshot.captured_at, limit.window_seconds, float(used), int(reset)))
    if not candidates:
        return None
    newest = max(item[0] for item in candidates)
    windows = [item for item in candidates if item[0] == newest]
    shortest = min(windows, key=lambda item: item[1])
    longest = max(windows, key=lambda item: item[1])
    return _normalize_usage({
        "u5": shortest[2], "r5": shortest[3],
        "u7": longest[2], "r7": longest[3],
    })


def get_usage() -> dict | None:
    return _provider_usage("claude")


def get_codex_usage() -> dict | None:
    return _provider_usage("codex")


def _normalize_grok_usage(raw) -> dict | None:
    """Validate the provider-shaped response before it can become durable viewer data."""
    if not isinstance(raw, dict):
        return None
    raw_percent = raw.get("weekly_percent", raw.get("usage_pct"))
    weekly_percent = _grok_number(raw_percent)
    if weekly_percent is not None and not 0 <= weekly_percent <= 100:
        weekly_percent = None

    raw_reset = raw.get("weekly_reset", raw.get("reset_epoch"))
    weekly_reset = _grok_number(raw_reset)
    if weekly_reset is not None and weekly_reset > 0 and weekly_reset.is_integer():
        weekly_reset = int(weekly_reset)
    else:
        weekly_reset = None

    tier = raw.get("tier")
    if not isinstance(tier, str) or not tier.strip():
        tier = None
    source_ts = raw.get("source_ts")
    if not isinstance(source_ts, str) or not source_ts.strip():
        source_ts = None

    # A partially-shaped response is not a safe replacement: it would turn a known last-good
    # budget into an unknown card. Grok's weekly percentage and future integer reset are one
    # coherent signal, so require both before persisting it.
    if weekly_percent is None or weekly_reset is None or weekly_reset <= time.time():
        return None
    return {
        "tier": tier,
        "weekly_percent": weekly_percent,
        "weekly_reset": weekly_reset,
        "source_ts": source_ts,
    }


def get_grok_usage() -> dict | None:
    usage = _provider_usage("grok")
    if usage is None:
        return None
    return _normalize_grok_usage({
        "weekly_percent": usage["u7"], "weekly_reset": usage["r7"],
    })


# The read-only half of one scout tick, expressed purely as calls into the skill's own
# functions: config.sh for every knob, accounts.sh for the per-account ceilings/usage and the
# drain selection, bonusdb.sh `plan` for the batch sizes and coordinator. The viewer must not
# grow its own copy of any of that - a second implementation of the gates is a second thing to
# drift. Emits `key=value` lines; `acct=` repeats once per bootstrapped account.
_GATES_SH = r"""
source "$BD/config.sh"; source "$BD/accounts.sh"
# `bonus_account_usage` normally refreshes a stale per-account snapshot over OAuth. The viewer's
# planner is read-only: it must show the last disk reading instead of ever doing provider I/O in
# a request thread. Rebind it before either the display loop or selection helper uses it.
viewer_account_usage() {
  local label="${1:?label required}" store="${2:-${BONUS_ACCOUNTS_STORE:-}}" snapshot
  snapshot="$store/$label.usage.json"
  if [ -r "$snapshot" ]; then
    _bonus_usage_fields_from_json "$(cat "$snapshot")"
  else
    echo "  "
  fi
}
bonus_account_usage() { viewer_account_usage "$@"; }
now="${6:-$(date +%s)}"
emit() { printf '%s=%s\n' "$1" "$2"; }
emit lead_hours "${DRAIN_LEAD_MAX_HOURS}";          emit window_hours "${WINDOW_HOURS}"
emit ceiling "${DRAIN_UNTIL_PCT}";                  emit five_hour_max "${FIVE_HOUR_START_MAX}"
emit pct_per_window "${PCT_PER_WINDOW}";            emit est_pct_per_job "${EST_PCT_PER_JOB}"
emit batch_n "${BATCH_N}"
emit codex_lead_hours "${CODEX_DRAIN_LEAD_MAX_HOURS}"
emit codex_ceiling "${CODEX_WEEKLY_CEIL}"
emit codex_pct_per_window "${CODEX_PCT_PER_WINDOW}";emit codex_est_pct_per_job "${CODEX_EST_PCT_PER_JOB}"
emit grok_lead_hours "${GROK_DRAIN_LEAD_MAX_HOURS:-${DRAIN_LEAD_MAX_HOURS:-30}}"
emit grok_ceiling "${GROK_WEEKLY_CEIL:-98}"
emit grok_pct_per_window "${GROK_PCT_PER_WINDOW:-2.5}"
emit grok_est_pct_per_job "${GROK_EST_PCT_PER_JOB:-0.75}"
if bonus_multi_active; then
  emit multi 1
  emit active "$(cat "$BONUS_ACCOUNTS_STORE/active" 2>/dev/null)"
  while IFS= read -r label; do
    [ -n "$label" ] || continue
    read -r five weekly reset < <(bonus_account_usage "$label" "$BONUS_ACCOUNTS_STORE") || true
    emit acct "$label|$(bonus_account_ceiling "$label")|${five:-}|${weekly:-}|${reset:-}"
  done < <(bonus_account_labels "$BONUS_ACCOUNTS_STORE")
else
  emit multi 0
fi
# The Codex rotator's accounts, for display only. Ceilings come from the SAME
# bonus_account_ceiling resolver as the Claude side, because the rotator enforces
# WEEKLY_CEIL_<label> per label regardless of provider - a Codex-only second number
# would drift from the line the rotator actually stops at.
if bonus_codex_multi_active; then
  emit codex_multi 1
  emit codex_active "$(cat "$BONUS_CODEX_ACCOUNTS_STORE/active" 2>/dev/null)"
  while IFS= read -r label; do
    [ -n "$label" ] || continue
    read -r weekly reset < <(bonus_codex_account_usage "$label") || true
    emit codex_acct "$label|$(bonus_account_ceiling "$label")|${weekly:-}|${reset:-}"
  done < <(bonus_codex_account_labels)
  # Mirror the scout's own Codex selection: the account whose window is open now, if any.
  read -r XSL XSR XS7 < <(bonus_codex_select_drain_account "$BONUS_CODEX_ACCOUNTS_STORE" "$now")
  [ -n "${XSL:-}" ] && emit codex_selected "$XSL"
else
  emit codex_multi 0
fi
# Mirror the scout's own selection: the account whose window is open now, if any. Its ceiling
# rebinds DRAIN_UNTIL_PCT for `plan`, exactly as scout.sh does, so the batch we show is the
# batch that tick would size.
read -r SL SR S5 S7 < <(bonus_select_drain_account "$BONUS_ACCOUNTS_STORE" "$now")
U5c=0; U7c=100; WLC=0
if [ -n "${SL:-}" ]; then
  emit selected "$SL"
  U5c="$S5"; U7c="$S7"
  export DRAIN_UNTIL_PCT="$(bonus_account_ceiling "$SL")"
  WLC=$(( ( (SR - now) + WINDOW_HOURS*3600 - 1 ) / (WINDOW_HOURS*3600) ))
fi
# The leading 5h field is consumed and discarded: Codex has no 5h window, so nothing gates on it.
# Its value comes from the viewer's background snapshot, never a provider/rollout reader invoked
# by a page request.
CODEX_JSON="${5:-null}"
U7x="$(jq -r 'if ((.u7 | type) == "number") and .u7 >= 0 and .u7 <= 100 then .u7 else 100 end' <<<"$CODEX_JSON" 2>/dev/null)" || U7x=100
R7x="$(jq -r 'if ((.r7 | type) == "number") and .r7 > 0 and (.r7 | floor) == .r7 then (.r7 | floor | tostring) else empty end' <<<"$CODEX_JSON" 2>/dev/null)" || R7x=""
WLX=0
case "${R7x:-}" in ''|*[!0-9]*) U7x=100 ;; *)
  if [ "$R7x" -gt "$now" ] && [ $(( R7x - now )) -le $(( CODEX_DRAIN_LEAD_MAX_HOURS*3600 )) ]; then
    WLX=$(( ( (R7x - now) + WINDOW_HOURS*3600 - 1 ) / (WINDOW_HOURS*3600) ))
  fi ;;
esac
GROK_JSON="${4:-null}"
U7g="$(jq -r 'if ((.weekly_percent | type) == "number") and .weekly_percent >= 0 and .weekly_percent <= 100 then .weekly_percent else "unknown" end' <<<"$GROK_JSON" 2>/dev/null)" || U7g=unknown
R7g="$(jq -r 'if ((.weekly_reset | type) == "number") and .weekly_reset > 0 and (.weekly_reset | floor) == .weekly_reset then (.weekly_reset | floor | tostring) else empty end' <<<"$GROK_JSON" 2>/dev/null)" || R7g=""
WLG=0
case "$R7g" in ''|*[!0-9]*) ;; *)
  GROK_LEAD="${GROK_DRAIN_LEAD_MAX_HOURS:-${DRAIN_LEAD_MAX_HOURS:-30}}"
  if [ "$R7g" -gt "$now" ] && [ $(( R7g - now )) -le $(( GROK_LEAD*3600 )) ]; then
    WLG=$(( ( (R7g - now) + WINDOW_HOURS*3600 - 1 ) / (WINDOW_HOURS*3600) ))
  fi ;;
esac
emit windows_left_claude "$WLC"; emit windows_left_codex "$WLX"; emit windows_left_grok "$WLG"
# The positional planner protocol was retired with the graph-backed CLI. Keep the shell
# half above for account-display snapshots, but ask the owning CLI for its gates.
"$BONUSDB" gates --json
"""

_gates_cache: dict = {"t": 0.0, "key": None, "val": None}


def get_gates(n_elig: int, n_codex: int, n_grok: int,
              grok_usage: dict | None, codex_usage: dict | None = None) -> dict:
    """Read-only planner view, keyed by the cached Codex and Grok provider snapshots."""
    now = time.time()
    raw_percent = (grok_usage or {}).get("weekly_percent")
    if (isinstance(raw_percent, (int, float)) and not isinstance(raw_percent, bool)
            and math.isfinite(float(raw_percent)) and 0 <= float(raw_percent) <= 100):
        weekly_percent = raw_percent
    else:
        weekly_percent = None
    raw_reset = (grok_usage or {}).get("weekly_reset")
    if (isinstance(raw_reset, (int, float)) and not isinstance(raw_reset, bool)
            and math.isfinite(float(raw_reset)) and float(raw_reset).is_integer()
            and int(raw_reset) > 0):
        weekly_reset = int(raw_reset)
    else:
        weekly_reset = None
    grok_json = json.dumps(
        {"weekly_percent": weekly_percent, "weekly_reset": weekly_reset},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    normalized_codex = _normalize_usage(codex_usage)
    codex_json = json.dumps(
        normalized_codex or {}, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    key = (n_elig, n_codex, n_grok, grok_json, codex_json)
    if _gates_cache["val"] is not None and _gates_cache["key"] == key and now - _gates_cache["t"] < 60:
        return _gates_cache["val"]
    out: dict = {"acct": [], "codex_acct": []}
    try:
        result = subprocess.run(
            ["bash", "-c", _GATES_SH, "gates", str(n_elig), str(n_codex),
             str(n_grok), grok_json, codex_json, str(int(now))],
            capture_output=True, text=True, timeout=25,
            env={**os.environ, "BD": str(BONUS_SKILL_DIR)},
        )
        raw = result.stdout
        gate_payload: dict | None = None
        for line in raw.splitlines():
            if line.startswith("{"):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    gate_payload = parsed
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k == "acct":
                f = v.split("|")
                if len(f) == 5:
                    out["acct"].append({
                        "label": f[0], "ceiling": _num(f[1]),
                        "u5": _num(f[2]), "u7": _num(f[3]), "r7": _num(f[4]),
                    })
            elif k == "codex_acct":
                f = v.split("|")
                if len(f) == 4:
                    out["codex_acct"].append({
                        "label": f[0], "ceiling": _num(f[1]),
                        "u7": _num(f[2]), "r7": _num(f[3]),
                    })
            else:
                out[k] = v
        # `gates --json` is the current planning boundary. These fields preserve the viewer's
        # display metadata, while shimmer remains driven by durable in-flight runs below.
        gates = gate_payload.get("gates", []) if gate_payload else []
        candidates = []
        for gate in gates:
            if not isinstance(gate, dict) or not gate.get("open"):
                continue
            provider = gate.get("provider_id")
            if provider not in RUN_ENGINES:
                continue
            batch = int(_f(gate.get("batch_size")))
            out[f"{provider}_batch"] = max(int(_f(out.get(f"{provider}_batch"))), batch)
            if batch > 0:
                candidates.append(gate)
        if candidates:
            next_gate = min(candidates, key=lambda gate: _f(gate.get("resets_at")) or float("inf"))
            out["coordinator"] = next_gate["provider_id"]
    except Exception:
        pass
    _gates_cache.update(t=now, key=key, val=out)
    return out


def windows_until_reset(reset, lead_hours, window_hours) -> int:
    """5h windows between now and `reset`, or 0 when the reset is past/absent or still further
    out than the drain lead. Mirrors scout.sh's `windows_until_reset` (the shell function is
    defined inside the scout, so it cannot be sourced without running a whole tick)."""
    try:
        reset, lead_hours, window_hours = int(reset), int(lead_hours), int(window_hours)
    except (TypeError, ValueError):
        return 0
    secs = reset - time.time()
    if secs <= 0 or secs > lead_hours * 3600:
        return 0
    win = window_hours * 3600
    return int((secs + win - 1) // win)


def _claude_multi_active() -> bool:
    """The accounts.sh multi-account predicate, evaluated from disk without sourcing it."""
    try:
        labels = [
            path for path in CLAUDE_ACCOUNTS_STORE.glob("*.json")
            if path.name != "mcp.json" and not path.name.endswith(".usage.json")
        ]
        return len(labels) >= 2
    except OSError:
        return False


def _canonical_cycle(now: float) -> int:
    """Mirror accounts.sh's default Saturday 22:05 UTC anchor without invoking usage.sh."""
    anchor_dow = int(os.environ.get("BONUS_WEEKLY_ANCHOR_DOW", "6"))
    anchor_hhmm = os.environ.get("BONUS_WEEKLY_ANCHOR_HHMM", "2205")
    try:
        anchor_hour, anchor_minute = int(anchor_hhmm[:2]), int(anchor_hhmm[2:])
        if not 0 <= anchor_dow <= 6 or not 0 <= anchor_hour <= 23 or not 0 <= anchor_minute <= 59:
            raise ValueError
    except (TypeError, ValueError):
        anchor_dow, anchor_hour, anchor_minute = 6, 22, 5
    utc = time.gmtime(now)
    # Python's Monday=0 maps to accounts.sh's Sunday=0 date output with (weekday + 1) % 7.
    today_dow = (utc.tm_wday + 1) % 7
    midnight = _cal.timegm((utc.tm_year, utc.tm_mon, utc.tm_mday, 0, 0, 0))
    candidate = midnight + ((anchor_dow - today_dow) % 7) * 86_400
    candidate += anchor_hour * 3600 + anchor_minute * 60
    if candidate <= now:
        candidate += 7 * 86_400
    return int((candidate + 1800) // 3600 * 3600)


def current_cycle(usage: dict | None) -> int:
    """Compute the scout-compatible cycle from cached usage and the on-disk account layout."""
    now = time.time()
    if _claude_multi_active():
        return _canonical_cycle(now)
    reset = (usage or {}).get("r7")
    reset_number = _grok_number(reset)
    if reset_number is not None and reset_number > 0 and reset_number.is_integer():
        return int((reset_number + 1800) // 3600 * 3600)
    try:
        with _db() as cx:
            row = cx.execute("SELECT MAX(cycle) FROM runs").fetchone()
            if row and row[0]:
                return int(row[0])
    except Exception:
        pass
    return 0


def _db() -> sqlite3.Connection:
    # read-only; never mutate the drain DB from the viewer
    cx = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    cx.row_factory = sqlite3.Row
    return cx


def _pick(cycle: int, codex_only: bool) -> list[dict]:
    args = ["bash", str(BONUSDB_SH), "pick", "9999", str(cycle)]
    if codex_only:
        args.append("--codex")
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=15).stdout.strip()
        return json.loads(out) if out else []
    except Exception:
        return []


def _remaining_snapshot(cycle: int) -> list[dict] | None:
    """Read graph-backed remaining work plus the providers the scout can use."""
    try:
        out = subprocess.run(
            ["bash", str(BONUSDB_SH), "queue", "--json", str(cycle)],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        payload = json.loads(out) if out else {}
        if not isinstance(payload, dict):
            return None
        values = payload.get("eligible_provider_ids")
        task_ids = payload.get("eligible_task_ids")
        tasks = payload.get("tasks")
        if not isinstance(values, dict) or not isinstance(task_ids, list) or not isinstance(tasks, list):
            return None
        tasks_by_id = {task.get("id"): task for task in tasks if isinstance(task, dict)}
        remaining = []
        for task_id in task_ids:
            task = tasks_by_id.get(task_id)
            providers = values.get(task_id)
            if task is None or not isinstance(providers, list):
                continue
            task = dict(task)
            task["eligible_providers"] = [str(provider) for provider in providers]
            remaining.append(task)
        return remaining
    except Exception:
        return None


def get_remaining(cycle: int) -> list[dict]:
    """Remaining work in drain order, annotated with scout-authoritative providers."""
    picks = _remaining_snapshot(cycle)
    if picks is None:
        # Compatibility for an older installed CLI during a package upgrade. This branch keeps
        # the historical display only until the paired graph-backed CLI is installed.
        picks = _pick(cycle, codex_only=False)
        for p in picks:
            p["eligible_providers"] = _legacy_providers(p)
    last = _last_runs()
    for p in picks:
        lr = last.get(p["id"])
        p["last_ts"] = lr["ts"] if lr else None
        p["last_status"] = lr["status"] if lr else None
        p["last_engine"] = lr["engine"] if lr else None
    return picks


def _last_runs() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        with _db() as cx:
            rows = cx.execute(
                """SELECT task, ts, status, engine FROM runs
                   WHERE rowid_pk IN (SELECT MAX(rowid_pk) FROM runs GROUP BY task)"""
            ).fetchall()
            for r in rows:
                out[r["task"]] = {"ts": r["ts"], "status": r["status"], "engine": r["engine"]}
    except Exception:
        pass
    return out


def get_recent_runs(limit: int = 80) -> list[dict]:
    try:
        with _db() as cx:
            rows = cx.execute(
                """SELECT r.ts, r.task, COALESCE(t.title, r.task) AS title, r.kind,
                          r.status, r.engine, r.cycle, r.summary, r.branch
                   FROM runs r LEFT JOIN tasks t ON t.id = r.task
                   ORDER BY r.ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_inflight() -> list[dict]:
    """Dispatched-and-not-yet-terminal runs: the jobs actually burning tokens right now.

    Same predicate as `bonusdb.sh inflight` (a dispatch with no later done/skipped/failed for
    the same task), joined out to the title and cwd the run log does not carry. `branch` is in
    the schema but is written empty by every dispatcher today, so the row shows the working
    directory instead - it is the field that actually identifies where the job is working."""
    try:
        with _db() as cx:
            rows = cx.execute(
                """SELECT r.ts, r.task, COALESCE(t.title, r.task) AS title, r.engine, t.cwd
                   FROM runs r LEFT JOIN tasks t ON t.id = r.task
                   WHERE r.status='dispatched'
                     AND NOT EXISTS (SELECT 1 FROM runs r2 WHERE r2.task=r.task
                           AND r2.status IN ('done','skipped','failed')
                           AND r2.rowid_pk > r.rowid_pk)
                   ORDER BY r.ts DESC"""
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_dispatch_times(cycle: int) -> list[float]:
    """Epochs of this cycle's dispatch events, for the per-window pacing strip."""
    out = []
    try:
        with _db() as cx:
            rows = cx.execute(
                "SELECT ts FROM runs WHERE status='dispatched' AND ((cycle+1800)/3600*3600)=? ",
                (cycle,),
            ).fetchall()
    except Exception:
        return out
    for r in rows:
        e = _iso_epoch(r["ts"])
        if e:
            out.append(e)
    return out


def _iso_epoch(iso) -> float | None:
    """The run log stores UTC ISO8601; time.strptime has no tz, so subtract the local offset."""
    if not iso:
        return None
    try:
        t = time.strptime(str(iso).replace("Z", "").split(".")[0], "%Y-%m-%dT%H:%M:%S")
        return time.mktime(t) - time.timezone
    except Exception:
        return None


def get_disabled() -> list[dict]:
    """Return disabled tasks so the UI can re-enable them without a shell."""
    try:
        with _db() as cx:
            rows = cx.execute(
                """SELECT id, title, kind, priority, cadence, cwd, goal
                   FROM tasks WHERE active=0 ORDER BY priority, title"""
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def set_task_active(task_id: str, active: bool) -> tuple[bool, str]:
    """Mutate through bonusdb.sh, keeping it as the sole queue access layer."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", task_id):
        return False, "invalid task id"
    try:
        with _db() as cx:
            if cx.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
                return False, "task not found"
    except Exception:
        return False, "could not read task"
    command = "activate" if active else "deactivate"
    try:
        result = subprocess.run(
            ["bash", str(BONUSDB_SH), command, task_id],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return False, "could not update task"
    if result.returncode != 0:
        return False, result.stderr.strip() or "could not update task"
    return True, "enabled" if active else "disabled"


def _last_line(text: str, fallback: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else fallback


RUN_ENGINES = ("claude", "codex", "grok", "auto")


def run_task_now(task_id: str, engine: str) -> tuple[bool, str]:
    """Force-dispatch through the shared router-only kickoff service."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", task_id):
        return False, "invalid task id"
    if engine not in RUN_ENGINES:
        return False, "invalid engine"
    try:
        cfg = graph_config.load_config(os.environ.get("BONUS_DRAIN_CONFIG"))
        result = kick_task(cfg, QueueDB(cfg.database), task_id, engine)
    except graph_dispatcher.AlreadyClaimed:
        return False, "this job is already claimed or no longer eligible"
    except graph_dispatcher.AmbiguousDispatch:
        return False, "launch state is ambiguous; reconcile it before retrying"
    except graph_dispatcher.DispatchError as exc:
        return False, str(exc)[:500] or "could not launch this job"
    except (graph_config.ConfigError, OSError, ValueError):
        return False, "could not launch this job"
    return True, f"launched on {result.provider_id} as {result.job_id}"


# ===========================================================================
# Scheduled-timers data access (read-only)
# ===========================================================================

def _systemctl(*args: str) -> str:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True, timeout=12
        ).stdout
    except Exception:
        return ""


def list_timers() -> list[dict]:
    raw = _systemctl("list-timers", "--all", "--output=json")
    try:
        rows = json.loads(raw) if raw.strip() else []
    except Exception:
        rows = []
    out = []
    for r in rows:
        unit = r.get("unit", "")
        out.append({
            "unit": unit,
            "activates": r.get("activates", ""),
            "next": _usec(r.get("next")),
            "last": _usec(r.get("last")),
        })
    return out


def _usec(v) -> float | None:
    # list-timers emits microsecond epochs; 0/absent means "no occurrence"
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v / 1_000_000 if v > 0 else None


def _read_unit_file(unit: str) -> str:
    p = UNIT_DIR / unit
    try:
        return p.read_text()
    except Exception:
        return ""


def _show(unit: str, prop: str) -> str:
    out = _systemctl("show", unit, "-p", prop)
    if "=" in out:
        return out.split("=", 1)[1].strip()
    return ""


def enrich(t: dict) -> dict:
    """Add schedule (OnCalendar), description, and the command/prompt for a timer."""
    unit = t["unit"]
    svc = t["activates"] or unit.replace(".timer", ".service")
    timer_txt = _read_unit_file(unit)
    svc_txt = _read_unit_file(svc)

    oncal = _grep(timer_txt, r"^\s*OnCalendar\s*=\s*(.+)$")
    if not oncal:
        # interval timers (rotator, some system units) use On*Sec instead of OnCalendar
        interval = _grep(timer_txt, r"^\s*OnUnitActiveSec\s*=\s*(.+)$")
        if interval:
            oncal = f"every {interval}"
    persistent = bool(_grep(timer_txt, r"^\s*Persistent\s*=\s*(true)$"))
    desc = _grep(svc_txt, r"^\s*Description\s*=\s*(.+)$") or _grep(timer_txt, r"^\s*Description\s*=\s*(.+)$")
    execstart = _grep(svc_txt, r"^\s*ExecStart\s*=\s*(.+)$")

    # transient one-shot units have no file on disk; fall back to systemctl show
    if not oncal and not svc_txt:
        desc = desc or _show(svc, "Description")
        execstart = execstart or _show(svc, "ExecStart")

    prompt = _resolve_prompt(execstart, svc)
    schedule = oncal or ("(transient one-shot)" if not persistent else "")
    t.update({
        "service": svc,
        "schedule": schedule,
        "frequency": frequency_for(schedule),
        "description": desc,
        "command": execstart,
        "prompt": prompt,
        "family": _family(unit),
    })
    return t


# --- how regularly does it run (cadence) --------------------------------------
# systemd is the source of truth: ask `systemd-analyze calendar` for the next few
# fire times and read the recurrence off the interval between them. DST/TZ-correct
# because systemd does the calendar math, not us.

_WEEKDAYS = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
             "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}
_ELAPSE_RE = re.compile(r":\s+([A-Z][a-z]{2} \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC")


@functools.lru_cache(maxsize=256)
def frequency_for(expr: str) -> str:
    if not expr:
        return ""
    if expr.startswith("every "):        # OnUnitActiveSec, already human ("every 15min")
        return expr
    if expr.startswith("("):             # "(transient one-shot)"
        return "one-shot"
    times = _calendar_iterations(expr, 4)
    if len(times) == 0:      # a fixed date with no future occurrence (spent one-shot)
        return "one-shot"
    if len(times) == 1:      # a single future fire, no recurrence
        return "once"
    base = min(times[i + 1] - times[i] for i in range(len(times) - 1))
    return _humanize_period(base) + _weekday_note(expr, base)


def _calendar_iterations(expr: str, n: int) -> list[float]:
    try:
        out = subprocess.run(
            ["systemd-analyze", "calendar", f"--iterations={n}", expr],
            capture_output=True, text=True, timeout=6,
        ).stdout
    except Exception:
        return []
    epochs = []
    for m in _ELAPSE_RE.finditer(out):
        try:
            epochs.append(_cal.timegm(time.strptime(m.group(1), "%a %Y-%m-%d %H:%M:%S")))
        except Exception:
            pass
    return epochs


def _humanize_period(secs: float) -> str:
    hour, day, week, month = 3600, 86400, 604800, 2592000
    if abs(secs - hour) <= 0.1 * hour:
        return "hourly"
    if abs(secs - day) <= 0.1 * day:
        return "daily"
    if abs(secs - week) <= 0.08 * week:
        return "weekly"
    if 27 * day <= secs <= 31 * day:
        return "monthly"
    if secs < hour:
        return f"every {int(round(secs / 60))}min"
    if secs < day:
        return f"every {int(round(secs / hour))}h"
    return f"every {int(round(secs / day))}d"


def _weekday_note(expr: str, base: float) -> str:
    if "Mon..Fri" in expr or "Mon-Fri" in expr:
        return " (weekdays)"
    if base >= 6 * 86400:  # weekly-ish: name the day if the expression pins one
        m = re.match(r"^([A-Z][a-z]{2})\b", expr)
        if m and m.group(1) in _WEEKDAYS:
            return f" ({_WEEKDAYS[m.group(1)]}s)"
    return ""


def _grep(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _resolve_prompt(execstart: str, svc: str) -> str:
    """bg-schedule jobs often read the prompt from a sibling .prompt file via
    $(cat <path>). Surface that file's contents as the actual prompt."""
    if not execstart:
        return ""
    m = re.search(r"cat\s+'?([^')]+\.prompt)'?", execstart)
    if m:
        try:
            return Path(m.group(1)).read_text().strip()
        except Exception:
            pass
    sibling = UNIT_DIR / svc.replace(".service", ".prompt")
    if sibling.exists():
        try:
            return sibling.read_text().strip()
        except Exception:
            pass
    return ""


def _result(svc: str) -> str:
    """systemd's last-run Result for a service: 'success' when clean, else a
    failure token ('exit-code', 'signal', 'timeout', ...). Used to badge archive
    rows so a spent one-shot that died is visible."""
    return _show(svc, "Result")


def _family(unit: str) -> str:
    if unit.startswith("cc-bonus-scout") or unit.startswith("cc-weekly-anchor"):
        return "bonus-drain"
    if unit.startswith("cc-token-rotator"):
        return "rotator"
    if unit.startswith("cc-bg-"):
        return "bg-schedule"
    return "other"


FAMILY_META = {
    "bg-schedule": ("Background jobs", "Ad-hoc + recurring bg-schedule jobs (automatic routing, explicit Claude, or Codex app server threads)", "var(--acc)"),
    "bonus-drain": ("Bonus-drain infrastructure", "Hourly scout + weekly usage-window anchor", "var(--ok)"),
    "rotator": ("Token rotator", "Multi-account credential rotation", "var(--warn)"),
    "other": ("Other timers", "Unrelated system timers", "var(--dim2)"),
}
FAMILY_ORDER = ["bg-schedule", "bonus-drain", "rotator", "other"]


# ===========================================================================
# Shared render helpers
# ===========================================================================

def esc(s) -> str:
    return html.escape("" if s is None else str(s))


def _chip(label, value, color):
    return f'<span class="stat"><span class="k">{esc(label)}</span><span class="v" style="--c:{color}">{esc(value)}</span></span>'


def fmt_abs(epoch: float | None) -> str:
    if not epoch:
        return "—"
    return time.strftime("%a %d %b %H:%M", time.localtime(epoch))


def rel(epoch: float | None) -> str:
    if not epoch:
        return ""
    secs = epoch - time.time()
    past = secs < 0
    secs = abs(secs)
    if secs < 90:
        v = f"{int(secs)}s"
    elif secs < 5400:
        v = f"{int(secs/60)}m"
    elif secs < 172800:
        v = f"{int(secs/3600)}h"
    else:
        v = f"{int(secs/86400)}d"
    return f"{v} ago" if past else f"in {v}"


def rel_time(iso_or_none) -> str:
    if not iso_or_none:
        return "—"
    epoch = _iso_epoch(iso_or_none)
    if epoch is None:
        return esc(iso_or_none)
    return _humanize(time.time() - epoch, past=True)


def dur(secs: float) -> str:
    """Elapsed as the console's `4h 12m` / `18m 04s`, for in-flight jobs and window offsets.

    Deliberately two-unit all the way up: this page is read to answer "how long until the drain
    opens" and "how long has this job been running", and `rel()`'s single coarse unit ("4d")
    loses the hours that decide whether to wait or force-dispatch."""
    secs = max(0, int(secs))
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    if secs < 172800:
        return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600:02d}h"


def _humanize(secs: float, past: bool) -> str:
    secs = abs(secs)
    if secs < 90:
        v = f"{int(secs)}s"
    elif secs < 5400:
        v = f"{int(secs / 60)}m"
    elif secs < 172800:
        v = f"{int(secs / 3600)}h"
    else:
        v = f"{int(secs / 86400)}d"
    return f"{v} ago" if past else f"in {v}"


# ===========================================================================
# Bonus-drain tab
# ===========================================================================

STATUS_COLORS = {
    "done": "var(--ok)", "dispatched": "var(--acc)", "failed": "var(--warn)",
    "skipped": "var(--dim)",
}
SIZE_LEVEL = {"tiny": 1, "small": 2, "medium": 3, "large": 4, "huge": 5}
PRI_TINT = {0: "var(--warn)", 1: "var(--acc)", 2: "var(--fg)", 3: "var(--dim)", 4: "var(--dim)"}


# Engine marks, taken from the agent-viewer repo's own logo set rather than redrawn, so the two
# tools label an engine identically; that set carries the real Claude and OpenAI Codex marks plus
# the official Grok mark and an agent router "auto" glyph. Emitted once as a <symbol> sprite and
# referenced by <use>, because
# the Claude path alone is ~2KB and this page draws an engine mark upward of forty times.
#
# Deliberately monochrome on currentColor rather than brand-coloured. Colour on this page means
# STATE (amber active, warn spent, green clear, slate unknown), and the two brand hexes collide
# with that vocabulary head-on: Claude's #d97757 sits directly on top of --warn, so a
# brand-coloured mark would read as "this row is in trouble". The silhouettes carry the
# distinction on their own.
ICON_SPRITE = '<svg class="sprite" aria-hidden="true" focusable="false"><symbol id="i-claude" viewBox="0 0 24 24"><g transform="translate(1.92 1.92) scale(0.84)" fill="currentColor"><path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"/></g></symbol><symbol id="i-codex" viewBox="0 0 20 20"><g transform="translate(1.6 1.6) scale(0.84)" fill="currentColor"><path d="M11.248 18.25q-.825 0-1.568-.314a4.3 4.3 0 0 1-1.32-.874 4 4 0 0 1-1.304.214 4 4 0 0 1-2.046-.544 4.27 4.27 0 0 1-1.518-1.485 4 4 0 0 1-.56-2.095q0-.48.131-1.04A4.4 4.4 0 0 1 2.04 10.71a4.07 4.07 0 0 1 .017-3.4 4.2 4.2 0 0 1 1.056-1.418 3.8 3.8 0 0 1 1.6-.842 3.9 3.9 0 0 1 .76-1.683q.593-.759 1.451-1.188a4.04 4.04 0 0 1 1.832-.429q.825 0 1.567.313.742.314 1.32.875a4 4 0 0 1 1.304-.215q1.106 0 2.046.545a4.14 4.14 0 0 1 1.501 1.485q.578.941.578 2.095 0 .48-.132 1.04.66.61 1.023 1.419.363.792.363 1.666 0 .892-.38 1.717a4.3 4.3 0 0 1-1.072 1.435 3.8 3.8 0 0 1-1.584.825 3.8 3.8 0 0 1-.775 1.683 4.06 4.06 0 0 1-1.436 1.188 4.04 4.04 0 0 1-1.832.429m-4.076-2.062q.825 0 1.435-.347l3.103-1.782a.36.36 0 0 0 .164-.313v-1.42L7.881 14.62a.67.67 0 0 1-.726 0l-3.118-1.798a.5.5 0 0 1-.017.115v.198q0 .841.396 1.551.413.693 1.139 1.089a3.2 3.2 0 0 0 1.617.412m.165-2.69a.4.4 0 0 0 .181.05q.083 0 .165-.05l1.238-.71-3.977-2.31a.7.7 0 0 1-.363-.643v-3.58q-.825.362-1.32 1.122a2.9 2.9 0 0 0-.495 1.65q0 .809.413 1.55.412.743 1.072 1.123zm3.91 3.663q.875 0 1.585-.396a2.96 2.96 0 0 0 1.534-2.64v-3.564a.32.32 0 0 0-.165-.297l-1.254-.726v4.604a.7.7 0 0 1-.363.643l-3.119 1.799a3 3 0 0 0 1.783.577m.627-6.039V8.878L10.01 7.822 8.129 8.878v2.244l1.881 1.056zM7.057 5.859a.7.7 0 0 1 .363-.644l3.119-1.798a3 3 0 0 0-1.782-.578q-.874 0-1.584.396A2.96 2.96 0 0 0 6.05 4.324a3.07 3.07 0 0 0-.396 1.551v3.547q0 .199.165.314l1.237.726zm8.383 7.887q.825-.364 1.303-1.123.495-.758.495-1.65a3.15 3.15 0 0 0-.412-1.55q-.413-.743-1.073-1.123l-3.086-1.782q-.099-.065-.181-.049a.3.3 0 0 0-.165.05l-1.238.692 3.993 2.327a.6.6 0 0 1 .264.264.64.64 0 0 1 .1.363zm-3.317-8.382a.63.63 0 0 1 .726 0l3.135 1.831v-.297q0-.792-.396-1.501a2.86 2.86 0 0 0-1.105-1.155q-.71-.43-1.65-.43-.825 0-1.436.347L8.294 5.941a.36.36 0 0 0-.165.314v1.418z"/></g></symbol><symbol id="i-auto" viewBox="0 0 24 24"><g transform="translate(1.92 1.92) scale(0.84)" fill="currentColor"><path d="M10.5 2.2a1.5 1.5 0 1 0 3 0 1.5 1.5 0 1 0-3 0Z"/><path d="M11.25 2.2h1.5V6h-1.5Z"/><path fill-rule="evenodd" d="M7 5.5h10a4 4 0 0 1 4 4V16a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V9.5a4 4 0 0 1 4-4ZM7 11.6a1.7 1.7 0 1 0 3.4 0 1.7 1.7 0 1 0-3.4 0ZM13.6 11.6a1.7 1.7 0 1 0 3.4 0 1.7 1.7 0 1 0-3.4 0ZM9.35 15.2h5.3a.95.95 0 0 1 0 1.9h-5.3a.95.95 0 0 1 0-1.9Z"/></g></symbol><symbol id="i-spin" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.4" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-dasharray="19 34"></circle></symbol><symbol id="i-ban" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="8.4"></circle><path d="M6.1 6.1 17.9 17.9"></path></g></symbol><symbol id="i-check" viewBox="0 0 24 24"><path d="M5.6 12.4 10.2 17 18.4 7.4" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"></path></symbol></svg>'
_GROK_SYMBOL = (
    '<symbol id="i-grok" viewBox="0 0 24 24" fill="currentColor">'
    '<path d="M9.27 15.29l7.978-5.897c.391-.29.95-.177 1.137.272.98 2.369.542 5.215-1.41 7.169-1.951 1.954-4.667 2.382-7.149 1.406l-2.711 1.257c3.889 2.661 8.611 2.003 11.562-.953 2.341-2.344 3.066-5.539 2.388-8.42l.006.007c-.983-4.232.242-5.924 2.75-9.383.06-.082.12-.164.179-.248l-3.301 3.305v-.01L9.267 15.292M7.623 16.723c-2.792-2.67-2.31-6.801.071-9.184 1.761-1.763 4.647-2.483 7.166-1.425l2.705-1.25a7.808 7.808 0 00-1.829-1A8.975 8.975 0 005.984 5.83c-2.533 2.536-3.33 6.436-1.962 9.764 1.022 2.487-.653 4.246-2.34 6.022-.599.63-1.199 1.259-1.682 1.925l7.62-6.815"></path>'
    '</symbol>'
)
_CALENDAR_SYMBOL = (
    '<symbol id="i-calendar" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="4" y="5.5" width="16" height="14" rx="2"></rect>'
    '<path d="M8 3.5v4M16 3.5v4M4 10h16"></path></g></symbol>'
)
_CYCLE_SYMBOL = (
    '<symbol id="i-cycle" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M19.4 12a7.4 7.4 0 1 1-2.17-5.23"></path>'
    '<path d="M17.6 3.1v3.9h-3.9"></path></g></symbol>'
)
_ARROW_SYMBOL = (
    '<symbol id="i-arrow" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M4.4 12h14.4M13.2 6.4 18.8 12l-5.6 5.6"></path></g></symbol>'
)
ICON_SPRITE = ICON_SPRITE.replace(
    '<symbol id="i-auto"',
    _GROK_SYMBOL + _CALENDAR_SYMBOL + _CYCLE_SYMBOL + _ARROW_SYMBOL + '<symbol id="i-auto"', 1,
)


def ico(name: str, cls: str = "") -> str:
    """One engine mark, sized by the CSS class rather than by the symbol."""
    return (f'<svg class="ico{" " + cls if cls else ""}" aria-hidden="true" focusable="false">'
            f'<use href="#i-{esc(name)}"></use></svg>')


def busy_button(inner: str) -> str:
    """A button's glyph plus the spinner that replaces it while its request is in flight.

    Both live in the DOM the whole time and CSS decides which shows. The handlers used to swap
    button.textContent for the word "launching", which broke the moment the buttons became
    icon-only: textContent on an icon button reads as empty, so restoring it after a FAILED
    launch replaced the mark with nothing and left a blank square."""
    return inner + ico("spin", "spin")


def size_badge(size: str | None) -> str:
    """Five-block capacity mark, with a visible fallback for legacy NULL rows."""
    label = size if size in SIZE_LEVEL else "unknown"
    level = SIZE_LEVEL.get(label, 0)
    unknown = '<span class="qmeta-label">unknown</span>' if level == 0 else ""
    return (f'<span class="qmeta-icon qsize" role="img" '
            f'aria-label="{esc(label)} size estimate" title="{esc(label)} size estimate">'
            f'{_size_blocks(label)}{unknown}</span>')


def _size_blocks(label: str) -> str:
    """The five-block capacity mark on its own, so the row badge and the size filter chip
    cannot drift apart."""
    level = SIZE_LEVEL.get(label, 0)
    blocks = "".join(f'<i class="{"on" if n <= level else ""}"></i>' for n in range(1, 6))
    return f'<span class="sizeblocks" aria-hidden="true">{blocks}</span>'


def schedule_badge(kind: str, last_ts: str | None) -> str:
    """Recurring jobs show their last-run age; one-offs need no recurrence mark."""
    if kind != "recurring":
        return ""
    if last_ts:
        age = rel_time(last_ts)
        short = age.removesuffix(" ago")
        label = f"Recurring · last ran {age}"
    else:
        short = "N/A"
        label = "Recurring · never run"
    return (f'<span class="qmeta-icon" aria-label="{esc(label)}" title="{esc(label)}">'
            f'{ico("calendar")}<span class="qmeta-label">{esc(short)}</span></span>')


def _run_buttons(t: dict) -> str:
    """The force-dispatch buttons that replaced the old single "Run now".

    An unavailable provider is rendered disabled rather than hidden: a missing button reads as
    a rendering bug, while the reason makes the graph-backed routing constraint visible.
    run-now.sh refuses the same case server-side, so this is a hint, not the guard.

    Auto asks agent-router which engine fits, then bonus-drain launches on it."""
    tid = esc(t["id"])
    providers = set(t.get("eligible_providers") or _legacy_providers(t))

    def attrs(provider: str, label: str) -> tuple[str, str, str]:
        if provider in providers:
            text = f"Force-dispatch on {label} now"
            return "", f' title="{text}"', text
        text = f"This task is not eligible for {label}"
        return " unavailable", f' disabled title="{text}"', text

    claude_cls, claude_attrs, claude_aria = attrs("claude", "Claude")
    codex_cls, codex_attrs, codex_aria = attrs("codex", "Codex")
    grok_cls, grok_attrs, grok_aria = attrs("grok", "Grok")
    return f"""
              <span class="runset">
                <span class="runlbl">force</span>
                <button class="task-run ionly{claude_cls}" data-task-id="{tid}" data-engine="claude"
                        aria-label="{claude_aria}" {claude_attrs}>{busy_button(ico("claude"))}</button>
                <button class="task-run ionly{codex_cls}" data-task-id="{tid}" data-engine="codex"
                        aria-label="{codex_aria}" {codex_attrs}>{busy_button(ico("codex"))}</button>
                <button class="task-run ionly{grok_cls}" data-task-id="{tid}" data-engine="grok"
                        aria-label="{grok_aria}" {grok_attrs}>{busy_button(ico("grok"))}</button>
                <button class="task-run ionly" data-task-id="{tid}" data-engine="auto"
                        aria-label="Let agent-router pick the engine, then dispatch"
                        title="Let agent-router pick the engine, then dispatch">{busy_button(ico("auto"))}</button>
              </span>"""


def _legacy_providers(task: dict) -> list[str]:
    """Keep direct renderer fixtures compatible with the pre-provider field shape."""
    return ["claude", "codex", "grok"] if task.get("engine_class") in ("codex-ok", "grok-ok") else ["claude"]


def _bar(pct, mark=None, cls="", time_mark=None) -> str:
    """A usage bar with an optional threshold tick (the ceiling, or the 5h throttle line).
    The tick is the whole point of this shape over a plain progress bar: it shows how much of
    the distance to the gate has been spent, not just how much has been used."""
    pct = max(0.0, min(100.0, _f(pct)))
    tick = ""
    if mark is not None:
        tick = f'<i class="mark" style="left:{max(0.0, min(100.0, _f(mark))):.1f}%"></i>'
    progress = ""
    if time_mark is not None:
        progress = f'<i class="wk" style="left:{max(0.0, min(100.0, _f(time_mark))):.1f}%"></i>'
    return f'<div class="bar {cls}"><i class="fill" style="width:{pct:.1f}%"></i>{tick}{progress}</div>'


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _card_name(engine: str, label: str) -> str:
    """Both rotators use the same account labels, so with four cards up a bare "Personal" names
    two different budgets. Prefix the engine, except on the single-budget fallback cards whose
    label already IS the engine."""
    return label if label == engine else f"{engine} · {label}"


def _card_state(c: dict) -> tuple[str, str]:
    """The one-line verdict in a card's top-right, in the same precedence the gates apply:
    drain window timing -> ceiling -> 5h throttle -> weekly pacing. Each line names the rule
    that is actually holding, so a stalled drain explains itself without opening the scout log."""
    if c["batch"] > 0:
        return f'dispatching · batch {c["batch"]}/{c["batch_n"]}', "acc"
    # Unknown is not zero. A missing reading must never render as a full budget.
    if c["u7"] is None:
        return "unknown · no usage reading", "dim"
    # Exhaustion outranks window timing: an account with nothing left is dead whether or not its
    # drain window happens to be open, and "closed · drain window" would hide that entirely.
    if _f(c["u7"]) >= 100:
        return "spent · weekly exhausted", "warn"
    if c["windows"] <= 0:
        return ("closed · drain window" if c["opens_in"] is not None
                else "closed · no reset signal"), "dim"
    if c["u7"] >= c["ceiling"]:
        return "capped · ceiling reached", "warn"
    # hot is None for an engine with no 5h window at all (Codex), so there is no throttle to trip.
    if c["hot"] is not None and _f(c["u5"]) >= _f(c["hot"]):
        return "throttled · 5h window hot", "warn"
    if c["eligible"] <= 0:
        return "open · nothing eligible", "dim"
    # Only one Claude account drains per tick (the open one whose reset is nearest), so an open
    # account that was not selected is queued behind another rather than held by a gate.
    if c.get("behind"):
        return f'queued · behind {c["behind"]}', "dim"
    return "holding · ahead of pace", "dim"


def _week(c: dict) -> dict:
    """How much of this account's week has gone, and how much budget is left.

    Both are measured, not forecast. An earlier version projected a burn rate forward to say when
    the account would run dry; that assumes spend is uniform across the week, and this box's is
    not - it clusters Monday to Thursday. A linear projection is therefore wrong in a systematic
    direction rather than a noisy one, claiming surplus every weekend that the actual pattern was
    never going to spend. It is gone. The panel states what is true and leaves the judgement to
    the reader, who is the only one who knows the shape of the week.

    An unknown reading yields nothing here, for the same reason it yields no usage bar.
    """
    out = {"known": False, "elapsed": None, "headroom": None}
    if c["u7"] is None:
        return out
    now = time.time()
    reset = _f(c.get("r7"))
    if not reset > now:
        return out
    week = 7 * 86400.0
    left = max(0.0, min(week, reset - now))
    out.update(known=True, elapsed=100.0 * (1.0 - left / week),
               headroom=max(0.0, _f(c["ceiling"]) - _f(c["u7"])))
    return out


def _drain_order(cards: list[dict]) -> list[dict]:
    """Sorted by what happens next, which is the ordering the rotator itself uses and the one
    thing a grid of equal cards cannot express: only one account per engine drains per tick, so
    these are a queue with a head, not four peers.

    Dispatching first, then accounts whose window is open (nearest reset first, since that is how
    the rotator picks between them), then closed accounts by how soon they open, then anything
    with no reading at all - an account we cannot gate is not a candidate for anything.
    """
    def key(c):
        if c["batch"] > 0:
            return (0, 0.0)
        if c["u7"] is None:
            return (3, 0.0)
        if c["windows"] > 0:
            return (1, _f(c.get("r7")))
        return (2, c["opens_in"] if c["opens_in"] is not None else float("inf"))
    return sorted(cards, key=key)


def _rotation(cards: list[dict]) -> str:
    """The next 7 days on one shared clock, one lane per account.

    This is the element four independent cards structurally cannot be: each card can state its own
    reset, but only a shared axis shows the STAGGER between them, and the stagger is the entire
    point of running multiple accounts. It is also the only place the panel can warn that two
    accounts have drifted onto the same cadence - drawn here as the span where their windows
    overlap, during which one of them can only ever be queued behind the other.

    It shows timing and nothing else. Budget lived here briefly as a projected runway and was
    removed: putting spend on a wall-clock axis requires assuming a uniform burn rate, and this
    box's spend clusters Monday to Thursday, so the projection was wrong in a systematic direction.
    Budget is stated as measured numbers in the rows below instead.
    """
    now, week = time.time(), 7 * 86400.0

    def pos(t):
        return max(0.0, min(100.0, 100.0 * (t - now) / week))

    # Window spans, so a lane can be told which parts of its own window it shares with a sibling.
    # Only same-engine accounts contend: a Claude window overlapping a Codex one costs nothing,
    # because they drain on separate coordinators.
    spans = {}
    for c in cards:
        reset = _f(c.get("r7"))
        # Per-engine lead: Claude and Codex have separate drain-lead settings, and the lane
        # must draw the window the rotator will actually honour for THAT engine.
        lead = int(_f(c.get("lead_h"), DRAIN_LEAD_MAX_HOURS))
        spans[id(c)] = (max(now, reset - lead * 3600), reset) if reset > now else None

    lanes = []
    for c in cards:
        span, bits = spans[id(c)], []
        for x in (14.3, 28.6, 42.9, 57.1, 71.4, 85.7):
            bits.append(f'<i class="gl" style="left:{x}%"></i>')
        if span:
            w0, w1 = pos(span[0]), pos(span[1])
            ov = []
            for other in cards:
                o = spans[id(other)]
                if other is c or o is None or other["engine"] != c["engine"]:
                    continue
                lo, hi = max(span[0], o[0]), min(span[1], o[1])
                if hi > lo:
                    ov.append(f'<i class="overlap" style="left:{pos(lo):.1f}%;'
                              f'width:{pos(hi) - pos(lo):.1f}%"></i>')
            cls = "win" + (" live" if c["batch"] > 0 else "")
            if c["u7"] is not None and _f(c["u7"]) >= 100:
                cls += " spent"
            elif c["u7"] is None:
                cls += " unk"
            if c["windows"] > 0:
                label = "draining" if c["batch"] > 0 else "open"
            else:
                label = f'opens {dur(c["opens_in"])}' if c["opens_in"] is not None else ""
            bits.append(f'<i class="{cls}" style="left:{w0:.1f}%;width:{max(0.0, w1 - w0):.1f}%">'
                        + (f"<b>{esc(label)}</b>" if label else "") + "</i>")
            bits.extend(ov)

            spent = c["u7"] is not None and _f(c["u7"]) >= _f(c["ceiling"])
            rcls = "reset" + (" warn" if spent else "")
            note = f'reset {dur(_f(c["r7"]) - now)}' if c["windows"] > 0 else "reset"
            bits.append(f'<i class="{rcls}" style="left:{w1:.1f}%"><em>{esc(note)}</em></i>')
        bits.append('<i class="nowline"></i>')

        lanes.append(f"""
            <div class="lane">
              <span class="lname{" active" if c.get("active") else ""}">{ico(c["engine"])}{esc(c["name"])}</span>
              <span class="track">{"".join(bits)}</span>
            </div>""")

    days = "".join(f'<span style="left:{i * 100 / 7:.1f}%">'
                   f'{"now" if i == 0 else f"+{i}d"}</span>' for i in range(7))
    # Name the lead only when every engine agrees on it; with Claude and Codex on different
    # leads there is no single number that describes the lanes.
    seen = {int(_f(c.get("lead_h"), DRAIN_LEAD_MAX_HOURS)) for c in cards}
    leads = f"{seen.pop()}h " if len(seen) == 1 else ""
    return f"""
    <div class="tl">
      <div class="tlhead"><span>rotation &middot; next 7 days</span>
        <span class="rt">{leads}drain window before each reset</span></div>
      <div class="tlgrid">
        <div class="tldays">{days}</div>
        {"".join(lanes)}
      </div>
      <div class="tlfoot">
        <span><i class="sw w"></i>drain window</span>
        <span><i class="sw r"></i>weekly reset</span>
        <span><i class="sw u"></i>unknown reading</span>
        <span class="wn"><i class="sw ov"></i>windows overlap, one waits</span>
      </div>
    </div>"""


def _account_row(c: dict) -> str:
    """One account as a row rather than a card. The state line moves from a 10px caption in the
    corner to the headline, because it names the rule that is actually binding and is therefore
    the answer to "why is nothing happening". The budget, the pace and the countdown sit beside it
    on one scan line; the pacing arithmetic, the 5h throttle and the window counts fold away.

    Everything that is identical across accounts - the eligible-task counts - has moved to the
    section header. Printed per card it read as a per-account number, which it never was.
    """
    state, tone = _card_state(c)
    p = _week(c)
    known = c["u7"] is not None
    u7, ceiling = _f(c["u7"]), _f(c["ceiling"])
    # The stripe answers a distinct, higher-level question from the status copy: which
    # subscription is currently selected for this provider. Keep that identity visible even
    # when its usage cache is unknown or its drain window is closed.
    stripe = "on" if c.get("active") else {"acc": "on", "warn": "warn", "dim": ""}.get(tone, "")
    if not known and not c.get("active"):
        stripe, tone = "unk", "unk"

    # --- budget cell ---------------------------------------------------------------------
    if known:
        fig = f'<span class="fig"><b>{u7:g}%</b> of {ceiling:g} ceiling</span>'
        # The stripe identifies the active subscription. Movement means this account owns the
        # current drain window; a child job may have already finished between scout ticks.
        bar = _bar(u7, ceiling, "lg" + (" draining" if c.get("draining") else " idle"), p["elapsed"])
    else:
        fig = '<span class="fig unk"><b>?</b> of ' + f'{ceiling:g} ceiling</span>'
        bar = '<div class="bar lg unknown"></div>'

    if not p["known"]:
        pace = ('<span class="paceline"><span class="unk">no spend reading</span>'
                '<b class="nil">nothing to compare</b></span>')
    else:
        pace = (f'<span class="paceline"><span>{u7:g}% spent &middot; '
                f'{p["elapsed"]:.0f}% of week gone</span>'
                f'<b>{p["headroom"]:.0f} pts left</b></span>')

    # --- when cell -----------------------------------------------------------------------
    if not known:
        when, wlabel, wtone = "n/a", "not eligible", "dimv"
    elif c["windows"] > 0:
        when, wlabel, wtone = dur(_f(c["r7"]) - time.time()), "to reset", ""
    elif c["opens_in"] is not None:
        when, wlabel, wtone = dur(c["opens_in"]), "until it opens", "acc"
    else:
        when, wlabel, wtone = "n/a", "no reset signal", "dimv"

    # --- expanded detail -----------------------------------------------------------------
    wk = [("used", f"{u7:g}%" if known else "no reading", "" if known else "unk"),
          ("ceiling", f"{ceiling:g}%", ""),
          ("headroom", f"{max(0.0, ceiling - u7):.1f}%" if known else "n/a", "")]
    if p["known"]:
        wk.append(("week elapsed", f'{p["elapsed"]:.1f}%', ""))
    if c["r7"]:
        wk.append(("reset in", dur(_f(c["r7"]) - time.time()), ""))

    win = []
    if c["windows"] > 0:
        win.append(("state", f'open, {c["windows"]} windows left', ""))
        # The pacing arithmetic earns its space only where it decides something. On a closed
        # window it used to render "window closed", restating the state line one row above.
        headroom = max(0.0, ceiling - u7) if known else 0.0
        per_window = headroom / c["windows"] if known else 0.0
        if not known:
            win.append(("pacing", "no usage reading", "dim"))
        elif c["windows"] <= 1:
            win.append(("pacing", "final window, drains all", "acc"))
        elif per_window >= c["ppw"]:
            win.append(("pacing", f"{per_window:.1f}% / window, clear", "ok"))
        else:
            win.append(("pacing", f'{per_window:.1f}% / window, hold (< {_f(c["ppw"]):g}%)', "dim"))
    elif c["opens_in"] is not None:
        win.append(("opens in", dur(c["opens_in"]), "acc"))
        win.append(("pacing", "evaluated when it opens", "dim"))
    else:
        win.append(("state", "closed, no reset signal", "dim"))
    if c.get("behind"):
        win.append(("queued behind", c["behind"], "dim"))

    def dls(rows):
        return "".join(
            f'<div class="dl"><span>{esc(k)}</span>'
            f'<b{" class=" + chr(34) + t + chr(34) if t else ""}>{esc(v)}</b></div>'
            for k, v, t in rows)

    if c["hot"] is None:
        five = '<div class="dnil">no 5h window on this plan</div>'
    else:
        five = (dls([("used", f'{_f(c["u5"]):g}%', ""),
                     ("throttles at", f'{_f(c["hot"]):g}%', "")])
                + _bar(c["u5"], c["hot"], "sm"))

    return f"""
          <details class="arow">
            <summary class="asum">
              <i class="stripe{" " + stripe if stripe else ""}"></i>
              <span class="aname">
                <span class="n1">{ico(c["engine"])}{esc(c["name"])}{c["tag"]}</span>
                <span class="n2 {tone}">{esc(state)}</span>
              </span>
              <span class="abudget">{fig}{bar}{pace}</span>
              <span class="awhen"><span class="t{" " + wtone if wtone else ""}">{esc(when)}</span>
                <span class="l">{esc(wlabel)}</span></span>
              <i class="caret"></i>
            </summary>
            <div class="adet">
              <div class="dgroup"><div class="dh">weekly</div>{dls(wk)}</div>
              <div class="dgroup"><div class="dh">drain window</div>{dls(win)}</div>
              <div class="dgroup"><div class="dh">5h throttle</div>{five}</div>
            </div>
          </details>"""


def _verdict(cards: list[dict], coord: str, lead_secs: int) -> tuple[str, str, str, str]:
    """The panel's answer sentence, as (tone, label, text, sub).

    The reader's first question is "is anything draining, and if not what is holding it", and
    until now that answer existed only as an inference across every card. The header pill and this
    line are both built from this one function so they can never disagree - the pill used to read
    "holding - window open, weekly pacing" while every visible card said "closed - drain window".
    """
    if not cards:
        return "idle", "no usage signal", "No account is reporting usage.", ""
    drain = next((c for c in cards if c["batch"] > 0), None)
    if drain is not None:
        into = (dur(lead_secs - (_f(drain["r7"]) - time.time()))
                if drain.get("r7") else "")
        sub = f'batch {drain["batch"]}/{drain["batch_n"]}'
        if into:
            sub += f" &middot; {into} into its window"
        if drain["u7"] is not None:
            sub += f' &middot; {_f(drain["u7"]):g}% of a {_f(drain["ceiling"]):g} ceiling'
        return ("live", "draining",
                f'{ico(drain["engine"])}{esc(drain["name"])} is dispatching.', sub)

    live = [c for c in cards if c["windows"] > 0 and c["u7"] is not None]
    # Prefer an open account that still has budget: if one exists it is the account the next tick
    # could actually dispatch on, so its gate is the interesting one. Falling back to a spent or
    # capped account keeps the line honest when every open window is dead.
    spendable = [c for c in live if _f(c["u7"]) < _f(c["ceiling"])]
    if live:
        near = min(spendable or live, key=lambda c: _f(c.get("r7")) or float("inf"))
        state, _ = _card_state(near)
        return ("hold", "holding",
                f'{ico(near["engine"])}{esc(near["name"])} has an open window; no active batch.',
                f'{esc(state)} &middot; resets in {dur(_f(near["r7"]) - time.time())}')

    opening = [c for c in cards if c["opens_in"] is not None]
    if opening:
        nxt = min(opening, key=lambda c: c["opens_in"])
        return ("idle", "nothing draining",
                "Every account is outside its drain window or spent.",
                f'next opens in {dur(nxt["opens_in"])} &middot; '
                f'{ico(nxt["engine"])}{esc(nxt["name"])}')
    return ("idle", "nothing draining", "No account has a usable drain window.", "")


def _claude_cards(gates: dict, usage: dict | None, n_elig: int, coord: str, batch: int) -> list[dict]:
    """One card per Claude account when the rotator holds 2+ (each has its own reset, its own
    ceiling, and therefore its own end-of-week drain window - collapsing them into a single
    "claude" number would show a weekly percentage that belongs to neither). One card from
    usage.sh otherwise."""
    lead = int(_f(gates.get("lead_hours"), DRAIN_LEAD_MAX_HOURS))
    win_h = int(_f(gates.get("window_hours"), 5))
    hot = _f(gates.get("five_hour_max"), 75)
    ppw = _f(gates.get("pct_per_window"), 2.5)
    selected = gates.get("selected") or ""
    active = gates.get("active") or ""
    accounts = gates.get("acct") or []

    if not accounts:
        if not usage:
            return []
        accounts = [{"label": "claude", "ceiling": _f(gates.get("ceiling"), 98),
                     "u5": usage.get("u5"), "u7": usage.get("u7"), "r7": usage.get("r7")}]
        active = "claude"
        # With one account there is nowhere else the drain could be landing, so when the
        # coordinator names this engine the single card IS the selected account. The rotator
        # store has no `selected` label to match against in this shape, and without this the
        # card never carries the batch: it would render "did not dispatch" mid-dispatch.
        if coord == "claude":
            selected = "claude"

    cards = []
    for a in accounts:
        windows = windows_until_reset(a.get("r7"), lead, win_h)
        opens = None
        if windows <= 0 and a.get("r7"):
            ahead = _f(a["r7"]) - time.time() - lead * 3600
            opens = ahead if ahead > 0 else None
        is_sel = a["label"] == selected
        is_active = a["label"] == active
        # At most ONE tag: "draining" already implies the rotator is pinned here, so stacking
        # "active" beside it only wraps the header row and knocks the cards out of alignment.
        tag = ""
        if is_sel:
            tag = '<span class="tag acc">draining</span>'
        elif is_active and len(accounts) > 1:
            tag = '<span class="tag">active</span>'
        cards.append({
            "name": _card_name("claude", a["label"]), "tag": tag, "engine": "claude",
            "u5": a.get("u5"), "u7": a.get("u7"), "ceiling": _f(a.get("ceiling"), 98),
            "hot": hot, "r7": a.get("r7"), "windows": windows, "opens_in": opens,
            "ppw": ppw, "batch": batch if (is_sel and coord == "claude") else 0,
            "batch_n": int(_f(gates.get("batch_n"), 6)),
            "eligible": n_elig, "elig_label": "eligible", "lead_h": lead,
            "active": is_active,
            "live": is_sel or a["label"] == active,
            "draining": is_sel and coord == "claude",
            "behind": selected if (selected and not is_sel and windows > 0) else "",
        })
    return cards


def _codex_cards(gates: dict, cx: dict | None, n_codex: int, coord: str, batch: int) -> list[dict]:
    """One card per Codex account when the rotator holds 2+ - each ChatGPT subscription has its
    own weekly allowance, its own reset, and its own ceiling, so a single collapsed "codex"
    number would belong to neither. Falls back to the one card from codex-usage.sh (rollout-file
    scraping) when the rotator store is absent or holds a single account.

    Only the ACTIVE account can carry a batch: bonus-drain dispatches Codex through whatever
    the provider credential store holds, which is the account the configured activator selected.
    Drawing the batch on both cards would claim work is landing somewhere it cannot."""
    lead = int(_f(gates.get("codex_lead_hours"), DRAIN_LEAD_MAX_HOURS))
    win_h = int(_f(gates.get("window_hours"), 5))
    accounts = gates.get("codex_acct") or []
    active = gates.get("codex_active") or ""
    selected = gates.get("codex_selected") or ""

    if not accounts:
        if not cx:
            return []
        accounts = [{"label": "codex", "ceiling": _f(gates.get("codex_ceiling"), 98),
                     "u7": cx.get("u7"), "r7": cx.get("r7")}]
        active = "codex"
        # Same single-account reasoning as _claude_cards: no `selected` label exists in this
        # shape, so without this the one card never carries the batch it is actually running.
        if coord == "codex":
            selected = "codex"

    cards = []
    for a in accounts:
        windows = windows_until_reset(a.get("r7"), lead, win_h)
        opens = None
        if windows <= 0 and a.get("r7"):
            ahead = _f(a["r7"]) - time.time() - lead * 3600
            opens = ahead if ahead > 0 else None
        is_active = a["label"] == active
        is_sel = a["label"] == selected
        # At most ONE tag, same rule as the Claude cards: "draining" already implies the
        # rotator is pinned here, so stacking "active" beside it wraps the header row.
        if is_sel:
            tag = '<span class="tag acc">draining</span>'
        elif is_active and len(accounts) > 1:
            tag = '<span class="tag">active</span>'
        else:
            tag = ""
        cards.append({
            "name": _card_name("codex", a["label"]), "tag": tag, "engine": "codex",
            # hot=None, deliberately, even where config carries a 5h knob: the ChatGPT plan has
            # no 5h window, so there is no second budget and nothing to draw as one.
            "u5": None, "hot": None,
            "u7": a.get("u7"), "ceiling": _f(a.get("ceiling"), 98),
            "r7": a.get("r7"), "windows": windows, "opens_in": opens,
            "ppw": _f(gates.get("codex_pct_per_window"), 2.5),
            "batch": batch if (coord == "codex" and is_sel) else 0,
            "batch_n": int(_f(gates.get("batch_n"), 6)),
            "eligible": n_codex, "elig_label": "jobs", "lead_h": lead,
            "active": is_active,
            # The usage bar is gold for the active subscription. A live batch is separately
            # represented by `batch`/the draining tag, so a closed drain window must not make
            # the active Codex account look inactive.
            "live": is_sel or is_active,
            "draining": is_sel and coord == "codex",
            "behind": selected if (selected and not is_sel and windows > 0) else "",
        })
    return cards


def _grok_cards(gates: dict, grok: dict | None, n_grok: int,
                coord: str, batch: int) -> list[dict]:
    """Adapt Grok's single weekly subscription signal to the shared account card."""
    # Grok is a configured bonus-drain engine, not a conditional visual. Keep its lane in the
    # top rotation while the durable snapshot is absent or a refresh is still in flight; the
    # normal unknown-reading state explains the missing telemetry without hiding the engine.
    grok = grok or {}
    lead = int(_f(gates.get("grok_lead_hours", gates.get("lead_hours")),
                  DRAIN_LEAD_MAX_HOURS))
    reset = grok.get("weekly_reset")
    windows = windows_until_reset(reset, lead, int(_f(gates.get("window_hours"), 5)))
    opens = None
    if windows <= 0 and reset:
        ahead = _f(reset) - time.time() - lead * 3600
        opens = ahead if ahead > 0 else None
    return [{
        "name": "Grok", "tag": "", "engine": "grok",
        "u5": None, "hot": None, "u7": grok.get("weekly_percent"),
        "ceiling": _f(gates.get("grok_ceiling"), 98), "r7": reset,
        "windows": windows, "opens_in": opens,
        "ppw": _f(gates.get("grok_pct_per_window"), 2.5),
        "batch": batch if coord == "grok" else 0,
        "batch_n": int(_f(gates.get("batch_n"), 6)),
        "eligible": n_grok, "elig_label": "jobs", "lead_h": lead,
        # Grok has one configured subscription, so it is always the active account. Whether
        # a batch is currently landing on it is separately represented by `live`/`batch`.
        "active": True, "live": True, "draining": coord == "grok", "behind": "",
    }]


def _pacing_strip(anchor, gates: dict, dispatches: list[float], batch: int) -> str:
    """The drain lead period cut into its 5h windows, each bar sized by how many jobs actually
    launched in it. Weekly pacing's whole job is spreading headroom across these windows, so the shape
    of the strip IS the pacing: front-loaded bars mean the taper failed, an empty run of them
    means it held. The last window is dashed - it always drains, whatever the math says, because
    unspent weekly tokens expire at the reset."""
    lead = int(_f(gates.get("lead_hours"), DRAIN_LEAD_MAX_HOURS))
    win_h = int(_f(gates.get("window_hours"), 5))
    if not anchor:
        return ""
    anchor, win, now = _f(anchor), win_h * 3600, time.time()
    n = max(1, -(-lead * 3600 // win))  # ceil
    start = anchor - n * win
    counts = [0] * n
    for e in dispatches:
        i = int((e - start) // win)
        if 0 <= i < n:
            counts[i] += 1
    peak = max(max(counts), batch, 1)
    cur = int((now - start) // win)

    cols = []
    for i, cnt in enumerate(counts):
        final = i == n - 1
        if i < cur:
            cls, label = ("ran", f"ran {cnt}") if cnt else ("held", "held")
        elif i == cur:
            cls, label = "now", f"now {cnt}"
            if batch:
                label = f"now {cnt} · +{batch}"
        else:
            cls, label = "next", f"w{n - i}"
        if final:
            cls += " final"
            label = "final · drains all" if i != cur else label
        h = 10 + int(46 * (cnt / peak)) if cnt else (10 if i < cur else 22)
        if i == cur and batch:
            h = max(h, 10 + int(46 * (batch / peak)))
        sweep = '<i class="sweep"></i>' if i == cur and cnt else ""
        cols.append(f'<div class="pcol{" wide" if final else ""}">'
                    f'<div class="pbar {cls}" style="height:{h}px">{sweep}</div>'
                    f'<div class="plbl">{esc(label)}</div></div>')
    opens_in = anchor - lead * 3600 - now
    note = "drain window open" if opens_in <= 0 else f"opens in {dur(opens_in)}"
    return f"""
    <div class="card pace">
      <div class="crow tiny"><span>pacing · {win_h}h windows to the {ico("claude")}claude reset</span>
        <span>{esc(note)} · batch tapers with headroom</span></div>
      <div class="pgrid">{"".join(cols)}</div>
    </div>"""


QUEUE_FILTER_GROUPS = ("kind", "provider", "priority", "size")
PROVIDER_LABELS = {"claude": "Claude", "codex": "Codex", "grok": "Grok"}


def _facet_kind(t: dict) -> str:
    return "recurring" if t.get("kind") == "recurring" else "oneoff"


def _facet_size(t: dict) -> str:
    size = t.get("size")
    return size if size in SIZE_LEVEL else "unknown"


def _facet_providers(t: dict) -> list[str]:
    have = set(t.get("eligible_providers") or _legacy_providers(t))
    return [p for p in PROVIDER_LABELS if p in have]


def _queue_filters(remaining: list[dict]) -> str:
    """Facet chips above the queue, applied client-side.

    Only values actually present in this cycle's queue get a chip: a zero-count chip filters to
    an empty list, which reads as a broken control rather than an empty facet. Every group is
    multi-select, OR within a group and AND across them, and an empty group means no constraint
    - which is what the leading `all` chip sets, so "all" stays a visible state rather than
    only the absence of a selection. The filter is client-side because the page reloads itself
    every 60s: a server-side filter would have to carry its state on the URL to survive that,
    where localStorage survives it for free."""
    if not remaining:
        return ""

    counts: dict[str, dict[str, int]] = {g: {} for g in QUEUE_FILTER_GROUPS}

    def bump(group: str, value: str) -> None:
        counts[group][value] = counts[group].get(value, 0) + 1

    for t in remaining:
        bump("kind", _facet_kind(t))
        bump("size", _facet_size(t))
        bump("priority", str(t["priority"]))
        for provider in _facet_providers(t):
            bump("provider", provider)

    size_order = list(SIZE_LEVEL) + ["unknown"]
    # (value, glyph, spoken name). The glyph carries the whole chip: with four facets on one
    # row there is no width for words, and every mark here is already the row vocabulary - the
    # engine marks, the five-block size gauge - so the chip reads as "filter by this thing you
    # can see in the rows". The spoken name is the title and the aria-label, never visible.
    groups: list[tuple[str, str, list[tuple[str, str, str]]]] = [
        ("kind", "type", [
            (v, ico("arrow" if v == "oneoff" else "cycle"),
             "one-off" if v == "oneoff" else "recurring")
            for v in ("oneoff", "recurring") if v in counts["kind"]
        ]),
        ("provider", "supported by", [
            (v, ico(v), PROVIDER_LABELS[v])
            for v in PROVIDER_LABELS if v in counts["provider"]
        ]),
        ("priority", "priority", [
            (v, "P" + v, "priority " + v) for v in sorted(counts["priority"], key=int)
        ]),
        ("size", "size", [
            (v, _size_blocks(v), v + " size") for v in size_order if v in counts["size"]
        ]),
    ]

    blocks = []
    for group, label, options in groups:
        # A single-value facet cannot filter anything, so it is a control that does nothing.
        if len(options) < 2:
            continue
        chips = ['<button type="button" class="fchip fall on" data-group="' + group
                 + '" data-value="" aria-pressed="true">all</button>']
        for value, glyph, name in options:
            chips.append(
                '<button type="button" class="fchip" data-group="' + group
                + '" data-value="' + esc(value) + '" aria-pressed="false"'
                + ' aria-label="' + esc(name) + '" title="' + esc(name) + '">'
                + glyph + "<i>" + str(counts[group][value]) + "</i></button>"
            )
        # The group name is spoken, not printed. Printing four labels cost ~200px, which is
        # exactly what the fourth group needed to stay on one row, and the marks are the same
        # ones the rows below carry - the label was naming what you can already see.
        blocks.append(
            '<div class="fgrp" role="group" aria-label="filter by ' + esc(label) + '">'
            + "".join(chips) + "</div>"
        )

    if not blocks:
        return ""
    return (
        '\n      <div class="qfilters" id="qfilters" role="group" aria-label="Filter remaining jobs">'
        + "".join(blocks)
        + '<button type="button" class="freset" id="qfreset" hidden'
        + ' title="Clear every filter" aria-label="Clear every filter">clear</button>'
        + "</div>"
    )


def render_bonus_body() -> str:
    usage = get_usage()
    codex = get_codex_usage()
    grok = get_grok_usage()
    cycle = current_cycle(usage)
    remaining = get_remaining(cycle)
    runs = get_recent_runs()
    disabled = get_disabled()
    inflight = get_inflight()

    n_claude = sum("claude" in set(r.get("eligible_providers") or _legacy_providers(r)) for r in remaining)
    n_codex = sum("codex" in set(r.get("eligible_providers") or _legacy_providers(r)) for r in remaining)
    n_grok = sum("grok" in set(r.get("eligible_providers") or _legacy_providers(r)) for r in remaining)
    n_weekly = sum(
        1 for r in remaining
        if r.get("kind") == "recurring" and r.get("cadence") == "weekly"
    )
    n_oneoff = sum(1 for r in remaining if r.get("kind") == "oneoff")
    gates = get_gates(len(remaining), n_codex, n_grok, grok, codex)
    # A plan identifies the provider currently owning an open drain window; the ledger says how
    # many child jobs remain in flight. The window owns shimmer, while only the ledger can claim
    # that a batch is dispatching in the status copy.
    live_batches = {
        engine: sum(1 for job in inflight if job.get("engine") == engine)
        for engine in RUN_ENGINES
    }
    live_engines = [engine for engine, count in live_batches.items() if count]
    coord = live_engines[0] if live_engines else gates.get("coordinator", "none")
    c_batch = live_batches.get("claude", 0)
    x_batch = live_batches.get("codex", 0)
    g_batch = live_batches.get("grok", 0)

    # --- header + live status pill -------------------------------------------------
    cards = _claude_cards(gates, usage, len(remaining), coord, c_batch)
    cards.extend(_codex_cards(gates, codex, n_codex, coord, x_batch))
    cards.extend(_grok_cards(gates, grok, n_grok, coord, g_batch))

    lead_secs = int(_f(gates.get("lead_hours"), DRAIN_LEAD_MAX_HOURS)) * 3600
    cards = _drain_order(cards)
    v_tone, v_label, v_text, v_sub = _verdict(cards, coord, lead_secs)
    pill = (v_label, v_tone)

    subline = " · ".join([
        "leftover-token backlog", f"{n_weekly} weekly", f"{n_oneoff} one-offs",
    ])

    # --- in flight ------------------------------------------------------------------
    if inflight:
        frows = []
        for j in inflight:
            started = _iso_epoch(j["ts"])
            frows.append(f"""
          <div class="fl">
            <span class="pdot"></span>
            <span class="fltitle">{esc(j["title"])}</span>
            <span class="dimtxt">{esc(Path(j.get("cwd") or "").name or "—")}</span>
            <span class="dimtxt ebadge">{ico(j.get("engine") if j.get("engine") in RUN_ENGINES else "claude")}{esc(j.get("engine") or "claude")}</span>
            <span class="elapsed">{dur(time.time() - started) if started else "—"}</span>
          </div>""")
        flight = f"""
      <div class="sec">
        <div class="sech"><span>in flight · {len(inflight)}</span>
          <span class="note">detached sessions · reconciled each scout tick</span></div>
        <div class="card flat">{"".join(frows)}</div>
      </div>"""
    else:
        flight = ""

    # --- remaining queue ------------------------------------------------------------
    if remaining:
        band_counts: dict = {}
        for t in remaining:
            band_counts[t["priority"]] = band_counts.get(t["priority"], 0) + 1
        rows, seen = [], set()
        for i, t in enumerate(remaining, 1):
            pri = t["priority"]
            if pri not in seen:
                seen.add(pri)
                n = band_counts[pri]
                rows.append(f"""
          <div class="band" data-band="{esc(pri)}"><span class="bpri" style="--c:{PRI_TINT.get(pri, "var(--dim)")}">P{esc(pri)}</span>
            <span class="brule"></span><span class="bcount">{n} task{"" if n == 1 else "s"}</span></div>""")
            cwd = esc(Path(t.get("cwd", "")).name or t.get("cwd", ""))
            goal = t.get("goal", "") or ""
            goal_short = goal if len(goal) <= 170 else goal[:170] + "…"
            rows.append(f"""
          <div class="qrow" data-kind="{_facet_kind(t)}" data-priority="{esc(pri)}"
               data-size="{esc(_facet_size(t))}" data-providers="{" ".join(_facet_providers(t))}">
            <span class="qn">{i}</span>
            <div class="qmain">
              <div class="qtitle">{esc(t["title"])}</div>
              <div class="qmeta">{size_badge(t.get("size"))}
                {schedule_badge(t["kind"], t.get("last_ts"))}
                <span class="qrepo" title="{esc(t.get("cwd", ""))}">{cwd}</span></div>
              <div class="qsub" title="{esc(goal)}">{esc(goal_short)}</div>
            </div>
            <div class="qact">{_run_buttons(t)}
              <button class="task-toggle ionly" data-task-id="{esc(t["id"])}" data-active="0"
                      aria-label="Disable this job" title="Disable this job"
                      >{busy_button(ico("ban"))}</button>
            </div>
          </div>""")
        rows.append('<p class="empty" id="qnone" hidden>no remaining jobs match these filters.</p>')
        queue = "".join(rows)
    else:
        queue = '<p class="empty">nothing remaining this cycle — queue drained (or none eligible yet).</p>'

    # --- run log --------------------------------------------------------------------
    this_cycle = sum(1 for r in runs if r.get("cycle") == cycle)
    if runs:
        lrows = []
        for r in runs:
            sc = STATUS_COLORS.get(r["status"], "var(--dim)")
            lrows.append(f"""
          <div class="lg">
            <span class="ltime" title="{esc(r["ts"])}">{rel_time(r["ts"])}</span>
            <span class="lstat" style="--c:{sc}">{esc(r["status"])}</span>
            <span class="ltitle">{esc(r["title"])}</span>
            <span class="dimtxt">{esc(r.get("engine") or "—")}</span>
            <span class="lnote">{esc(r.get("summary") or "")}</span>
          </div>""")
        runlog = "".join(lrows)
    else:
        runlog = '<p class="empty">no runs recorded yet.</p>'

    # --- disabled -------------------------------------------------------------------
    if disabled:
        drows = []
        for t in disabled:
            kind = "recurring" if t["kind"] == "recurring" else "one-off"
            cad = f' · {esc(t["cadence"])}' if t.get("cadence") else ""
            cwd = esc(Path(t.get("cwd", "")).name or t.get("cwd", ""))
            drows.append(f"""
          <div class="qrow off">
            <span class="qn" style="color:{PRI_TINT.get(t["priority"], "var(--dim)")}">P{esc(t["priority"])}</span>
            <div class="qmain">
              <div class="qtitle">{esc(t["title"])}</div>
              <div class="qmeta"><span>{kind}{cad}</span><span>{cwd}</span></div>
            </div>
            <div class="qact">
              <button class="task-toggle ionly" data-task-id="{esc(t["id"])}" data-active="1"
                      aria-label="Enable this job" title="Enable this job"
                      >{busy_button(ico("check"))}</button>
            </div>
          </div>""")
        # Collapsed by default, like the schedule tab's Archive. This list is the largest block
        # on the page - currently 91 rows against a 37-row queue - and it is reference material,
        # not something you act on: a disabled job is excluded from every pick, so it cannot be
        # what the drain does next. Expanded it buried the run log under a screen-height of
        # scroll. The count stays in the summary so it is still visible without opening it.
        disabled_sec = f"""
      <details class="sec fold">
        <summary class="sech"><span><i class="caret"></i>disabled · {len(disabled)}</span>
          <span class="note">excluded from every drain pick</span></summary>
        <div class="card flat">{"".join(drows)}</div>
      </details>"""
    else:
        disabled_sec = ""

    anchor = min((c["r7"] for c in cards
                  if c["engine"] == "claude" and c.get("r7") and _f(c["r7"]) > time.time()),
                 default=None)

    return f"""
    <div class="hd">
      <div>
        <h1>bonus-drain</h1>
        <div class="sub">{esc(subline)}</div>
      </div>
      <div class="pill {pill[1]}"><span class="pdot"></span><span>{esc(pill[0])}</span></div>
    </div>

    <div class="vbar {v_tone}"><span class="vdot"></span>
      <div class="vmain"><b>{esc(v_label)}</b>
        <div class="vtext">{v_text}</div>
        <div class="vsub">{v_sub}</div></div>
    </div>
    {_rotation(cards)}
    <div class="rows">
      <div class="rowhd"><span>drain order</span>
        <span>{len(remaining)} jobs &middot;
          {ico("claude")}{n_claude} Claude &middot; {ico("codex")}{n_codex} Codex &middot;
          {ico("grok")}{n_grok} Grok eligible</span></div>
      {"".join(_account_row(c) for c in cards)}
    </div>
    {_pacing_strip(anchor, gates, get_dispatch_times(cycle), c_batch)}
    {flight}

    <div class="sec">
      <div class="sech"><span>remaining this week · drain order</span>
        <span class="note"><span id="qcount">{len(remaining)} job{"" if len(remaining) == 1 else "s"}</span>
          · one-offs before recurring inside each band</span></div>
      {_queue_filters(remaining)}
      <div id="qlist">{queue}</div>
    </div>

    <div class="sec">
      <div class="sech"><span>run log</span>
        <span class="note">{this_cycle} ran this cycle · {len(remaining)} still eligible
          ({ico("claude")}{n_claude} Claude · {ico("codex")}{n_codex} Codex ·
          {ico("grok")}{n_grok} Grok)</span></div>
      <div class="card flat">{runlog}</div>
    </div>
    {disabled_sec}
    <footer>refreshes every 60s · cycle {cycle}</footer>
    """


# ===========================================================================
# Scheduled-jobs tab
# ===========================================================================

def render_schedule_body() -> str:
    timers = [enrich(t) for t in list_timers()]
    # spent one-shots (already run, no future fire) go to the collapsed archive;
    # everything with a scheduled next fire stays in the live family sections.
    active = [t for t in timers if t["next"] is not None]
    archived = [t for t in timers if t["next"] is None]

    by_family: dict[str, list[dict]] = {f: [] for f in FAMILY_ORDER}
    for t in active:
        by_family[t["family"]].append(t)
    for lst in by_family.values():
        lst.sort(key=lambda t: (t["next"] is None, t["next"] or 0))

    total = len(timers)
    n_bg = len(by_family["bg-schedule"])
    summary = "".join([
        _chip("timers", str(total), "var(--fg)"),
        _chip("scheduled", str(len(active)), "var(--acc2)"),
        _chip("archived", str(len(archived)), "var(--dim)"),
    ])

    sections = []
    for fam in FAMILY_ORDER:
        lst = by_family[fam]
        if not lst:
            continue
        label, blurb, color = FAMILY_META[fam]
        rows = "".join(_row(t) for t in lst)
        collapsed = " open" if fam != "other" else ""
        sections.append(f"""
        <details class="fam"{collapsed}>
          <summary><span class="fdot" style="--c:{color}"></span>{esc(label)}
            <span class="count">{len(lst)}</span>
            <span class="blurb">{esc(blurb)}</span></summary>
          <table class="grid">
            <thead><tr><th>Unit</th><th>Frequency</th><th>Next</th><th>Last</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </details>""")

    # --- archive: already-run jobs with no future fire, hidden by default ---
    if archived:
        archived.sort(key=lambda t: (t["last"] is None, -(t["last"] or 0)))
        for t in archived:
            t["result"] = _result(t["service"])
        arows = "".join(_arow(t) for t in archived)
        sections.append(f"""
        <details class="fam archive">
          <summary><span class="fdot" style="--c:var(--dim2)"></span>Archive
            <span class="count">{len(archived)}</span>
            <span class="blurb">already run · no future fire (spent one-shots)</span></summary>
          <table class="grid">
            <thead><tr><th>Unit</th><th>Frequency</th><th>Last run</th><th>Result</th></tr></thead>
            <tbody>{arows}</tbody>
          </table>
        </details>""")

    return f"""
    <div class="hd">
      <div>
        <h1>scheduled jobs</h1>
        <div class="sub">systemd --user timers · {n_bg} background job{"" if n_bg == 1 else "s"}</div>
      </div>
      <div class="chips">{summary}</div>
    </div>
    {"".join(sections) or '<p class="empty">no timers found.</p>'}
    <footer>read-only · refreshes every 60s · next/last from systemctl list-timers</footer>
    """


def _detail_cell(t: dict) -> str:
    if not (t.get("command") or t.get("prompt")):
        return ""
    parts = []
    if t.get("description"):
        parts.append(f'<div class="d-desc">{esc(t["description"])}</div>')
    if t.get("prompt"):
        parts.append(f'<div class="d-lbl">prompt</div><pre>{esc(t["prompt"])}</pre>')
    if t.get("command"):
        parts.append(f'<div class="d-lbl">command</div><pre class="cmd">{esc(t["command"])}</pre>')
    return f'<tr class="detailrow"><td colspan="4"><div class="detail">{"".join(parts)}</div></td></tr>'


def _row(t: dict) -> str:
    name = t["unit"].replace(".timer", "")
    detail = _detail_cell(t)
    nxt = fmt_abs(t["next"])
    nrel = rel(t["next"])
    lst = fmt_abs(t["last"])
    lrel = rel(t["last"])
    return f"""
      <tr class="trow" onclick="this.nextElementSibling&&this.nextElementSibling.classList.contains('detailrow')&&this.nextElementSibling.classList.toggle('show')">
        <td class="unit">{esc(name)}</td>
        <td class="freq"><span class="fq">{esc(t.get("frequency")) or "—"}</span>
            <span class="sched">{esc(t["schedule"])}</span></td>
        <td class="nowrap">{esc(nxt)}<span class="muted"> {esc(nrel)}</span></td>
        <td class="nowrap muted" title="{esc(lst)}">{esc(lrel) or "—"}</td>
      </tr>{detail}"""


def _arow(t: dict) -> str:
    name = t["unit"].replace(".timer", "")
    detail = _detail_cell(t)
    lst = fmt_abs(t["last"])
    lrel = rel(t["last"])
    result = t.get("result") or ""
    if result == "success":
        rbadge = '<span class="rbadge" style="--c:var(--ok)">success</span>'
    elif result:
        rbadge = f'<span class="rbadge" style="--c:var(--warn)">{esc(result)}</span>'
    else:
        rbadge = '<span class="muted">—</span>'
    return f"""
      <tr class="trow" onclick="this.nextElementSibling&&this.nextElementSibling.classList.contains('detailrow')&&this.nextElementSibling.classList.toggle('show')">
        <td class="unit">{esc(name)}</td>
        <td class="freq"><span class="fq">{esc(t.get("frequency")) or "—"}</span>
            <span class="sched">{esc(t["schedule"])}</span></td>
        <td class="nowrap" title="{esc(lst)}">{esc(lst)}<span class="muted"> {esc(lrel)}</span></td>
        <td>{rbadge}</td>
      </tr>{detail}"""


# ===========================================================================
# Page shell
# ===========================================================================

# The "console" skin: a dark instrument panel, monospace throughout, square corners, and a
# single amber accent reserved for LIVE things (what is draining now, what the next tick will
# spend). Everything static is greyscale, so a glance at the page finds the moving parts. Colors
# are declared once as tokens here and referenced by name everywhere else, including from the
# Python (STATUS_COLORS / PRI_TINT emit var(--...)).
CSS = """
:root{
  color-scheme:dark;
  --bg:#0a0a0b; --panel:#101012; --line:rgba(255,255,255,.09); --line2:rgba(255,255,255,.06);
  /* Two tiers of secondary text, and both were far too faint against #0a0a0b: at the .42/.28
     the mockup used they measured 3.5:1 and 2.2:1 against the page, and the tertiary tier was
     genuinely hard to read. Now 7.2:1 and 5.1:1, both clear of WCAG AA, with the hierarchy
     still intact because fg stays opaque. Nothing on this page renders text below --dim2, so
     these two numbers are the floor - keep any new secondary text on a token, not a literal. */
  --fg:#e9e7e2; --dim:rgba(233,231,226,.66); --dim2:rgba(233,231,226,.54);
  --acc:oklch(0.80 0.14 78); --acc2:oklch(0.86 0.11 78);
  --ok:oklch(0.78 0.13 155); --warn:oklch(0.72 0.17 40);
  --mono:'Geist Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg)}
body{color:var(--fg);font:13px/1.5 var(--mono);-webkit-text-size-adjust:100%}
@keyframes bdpulse{0%,100%{opacity:1}50%{opacity:.22}}
@keyframes bdspin{to{transform:rotate(360deg)}}
@keyframes bdsweep{0%{transform:translateX(-120%)}100%{transform:translateX(420%)}}
.wrap{max-width:1400px;margin:0 auto;padding:0 clamp(12px,3vw,28px) 80px}
a{color:var(--acc2);text-decoration:none}

/* top bar + tabs ---------------------------------------------------------------------- */
.topbar{position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:space-between;
  gap:16px;flex-wrap:wrap;min-height:54px;background:var(--bg);border-bottom:1px solid var(--line);
  margin:0 calc(-1 * clamp(12px,3vw,28px));padding:0 clamp(12px,3vw,28px)}
.brand{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim)}
.tabs{display:flex;gap:4px;padding:7px 0}
.tab{position:relative;min-height:40px;padding:0 14px;font:inherit;font-size:11.5px;letter-spacing:.04em;
  color:rgba(233,231,226,.62);background:none;border:0;border-radius:7px;cursor:pointer}
.tab:hover{background:rgba(255,255,255,.06);color:#fff}
.tab.active{color:#fff}
.tab.active::after{content:"";position:absolute;left:14px;right:14px;bottom:5px;height:2px;background:var(--acc)}
.pane{display:none}.pane.active{display:block}

/* page header + live pill ------------------------------------------------------------- */
.hd{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin:26px 0 22px}
.hd h1{margin:0;font-size:clamp(21px,3vw,27px);letter-spacing:-.02em;font-weight:500}
.hd .sub{font-size:11.5px;color:var(--dim);margin-top:5px;letter-spacing:.04em}
.pill{display:flex;align-items:center;gap:9px;padding:8px 12px;border:1px solid var(--line);border-radius:3px;
  font-size:11px;letter-spacing:.10em;text-transform:uppercase;color:var(--dim)}
.pill .pdot{width:6px;height:6px;border-radius:50%;background:currentColor;flex:none}
.pill.live{border-color:oklch(0.80 0.14 78 / .45);color:var(--acc2)}
.pill.live .pdot{background:var(--acc);animation:bdpulse 1.6s ease-in-out infinite}
.pill.hold{color:var(--ok);border-color:oklch(0.78 0.13 155 / .35)}

/* cards ------------------------------------------------------------------------------- */
.card{border:1px solid var(--line);background:var(--panel);padding:17px 18px 15px}
.card.flat{padding:0}
.crow{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.crow.tiny{font-size:10px;color:var(--dim2);margin-top:6px}
.crow .k{color:var(--dim)}
.cname{font-size:12.5px;letter-spacing:.08em}
.tag{margin-left:7px;font-size:9px;letter-spacing:.10em;text-transform:uppercase;color:var(--dim2);
  border:1px solid var(--line);padding:1px 5px;vertical-align:1px}
.tag.acc{color:var(--acc2);border-color:oklch(0.80 0.14 78 / .45)}
.bar{position:relative;height:8px;background:rgba(255,255,255,.07);margin-top:12px}
.bar .fill{position:absolute;top:0;bottom:0;left:0;background:var(--acc)}
.bar .mark{position:absolute;top:-3px;bottom:-3px;width:1px;background:rgba(233,231,226,.6)}
/* Week-elapsed position. It used to be .time-mark: 1px at .25 alpha, on a bar where the taller
   brighter ceiling tick takes the eye, so nobody ever read it. Same data, given a cap so it is a
   different SHAPE from the ceiling tick rather than a fainter version of one. */
.bar .wk{position:absolute;top:-4px;bottom:0;width:1px;background:rgba(233,231,226,.55);z-index:3}
.bar .wk::before{content:"";position:absolute;left:-3.5px;top:-4px;width:0;height:0;
  border-left:3.5px solid transparent;border-right:3.5px solid transparent;
  border-top:4px solid rgba(233,231,226,.8)}
.bar.unknown{background:repeating-linear-gradient(135deg,
  oklch(0.66 0.045 250 / .38) 0 5px, rgba(255,255,255,.05) 5px 10px)}
.bar.idle .fill{background:rgba(233,231,226,.32)}
.bar.draining .fill{background:linear-gradient(110deg,var(--acc) 0%,var(--acc2) 38%,#fff2b8 50%,
  var(--acc2) 62%,var(--acc) 100%);background-size:220% 100%;animation:drainshimmer 1.6s linear infinite}
@keyframes drainshimmer{to{background-position:-220% 0}}
@media (prefers-reduced-motion:reduce){.bar.draining .fill{animation:none}}
.bar.sm{height:4px;margin-top:5px}
.bar.sm .fill{background:rgba(233,231,226,.5)}
.bar.sm .mark{background:rgba(233,231,226,.45)}
.dimtxt{color:var(--dim)}

/* verdict line ------------------------------------------------------------------------
   The panel's answer sentence. Built from the same _verdict() as the header pill, so the two
   can never contradict each other the way the old pill contradicted the cards. */
.vbar{display:flex;align-items:flex-start;gap:13px;border:1px solid var(--line);
  background:var(--panel);padding:14px 16px}
.vbar .vdot{width:9px;height:9px;border-radius:50%;flex:none;margin-top:5px;background:var(--dim2)}
.vbar.hold .vdot{background:var(--ok)}
.vbar.live .vdot{background:var(--acc);animation:bdpulse 1.6s ease-in-out infinite}
.vbar .vmain{min-width:0}
.vbar .vmain>b{font-weight:400;font-size:11px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--dim)}
.vbar.hold .vmain>b{color:var(--ok)}
.vbar.live .vmain>b{color:var(--acc2)}
.vtext{font-size:14.5px;margin-top:5px}
.vsub{font-size:11px;color:var(--dim);letter-spacing:.04em;margin-top:4px}

/* rotation timeline -------------------------------------------------------------------
   The one element four independent cards cannot be: each can state its own reset, but only a
   shared axis shows the stagger between them, and two accounts drifting onto the same cadence. */
.tl{border:1px solid var(--line);background:var(--panel);margin-top:12px;padding:16px 18px 14px}
.tlhead{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;
  font-size:11px;letter-spacing:.06em;color:var(--dim)}
.tlhead .rt{color:var(--dim2);font-size:10.5px;text-align:right}
.tlgrid{position:relative;margin-top:16px}
.tldays{position:relative;height:14px;margin:0 0 0 176px;border-bottom:1px solid var(--line2)}
.tldays span{position:absolute;top:0;font-size:9.5px;letter-spacing:.08em;color:var(--dim2);
  transform:translateX(-50%);white-space:nowrap}
.lane{display:grid;grid-template-columns:176px 1fr;align-items:center;margin-top:11px}
.lane .lname{font-size:11px;letter-spacing:.06em;color:var(--dim);padding-right:12px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lane .lname.active{font-weight:700;color:oklch(0.72 0.17 40)}
.track{position:relative;height:22px;background:rgba(255,255,255,.04);
  border-top:1px solid var(--line2);border-bottom:1px solid var(--line2)}
.track .gl{position:absolute;top:0;bottom:0;width:1px;background:rgba(255,255,255,.05)}
.win{position:absolute;top:0;bottom:0;background:oklch(0.80 0.14 78 / .16);
  border-left:1px solid oklch(0.80 0.14 78 / .5)}
.win.live{background:oklch(0.80 0.14 78 / .34)}
.win.spent{background:oklch(0.72 0.17 40 / .18);border-left-color:oklch(0.72 0.17 40 / .6)}
.win.unk{background:repeating-linear-gradient(135deg,
  oklch(0.66 0.045 250 / .34) 0 5px, transparent 5px 10px);
  border-left-color:oklch(0.66 0.045 250 / .7)}
.win b{position:absolute;left:6px;top:50%;transform:translateY(-50%);font-size:9.5px;
  letter-spacing:.08em;color:var(--fg);white-space:nowrap}
.overlap{position:absolute;top:0;bottom:0;pointer-events:none;
  background:repeating-linear-gradient(135deg,
    oklch(0.72 0.17 40 / .30) 0 2px, transparent 2px 7px);
  border-left:1px dashed oklch(0.72 0.17 40 / .75);
  border-right:1px dashed oklch(0.72 0.17 40 / .75)}
.reset{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--fg)}
.reset.warn{background:var(--warn)}
.reset em{position:absolute;left:6px;top:50%;transform:translateY(-50%);font-style:normal;
  font-size:9.5px;letter-spacing:.06em;color:var(--dim);white-space:nowrap}
.nowline{position:absolute;left:0;top:-6px;bottom:-6px;width:1px;background:var(--acc2)}
.tlfoot{display:flex;gap:18px;flex-wrap:wrap;margin-top:14px;padding-top:11px;
  border-top:1px solid var(--line2);font-size:10px;letter-spacing:.06em;color:var(--dim2)}
.tlfoot span{display:flex;align-items:center;gap:6px}
.tlfoot .wn{color:var(--warn)}
.sw{width:16px;height:8px;flex:none}
.sw.w{background:oklch(0.80 0.14 78 / .16);border-left:1px solid oklch(0.80 0.14 78 / .5)}
.sw.r{background:var(--fg);width:2px;height:12px}
.sw.ov{background:repeating-linear-gradient(135deg,oklch(0.72 0.17 40 / .30) 0 2px,transparent 2px 7px);
  border-left:1px dashed oklch(0.72 0.17 40 / .75);border-right:1px dashed oklch(0.72 0.17 40 / .75)}

/* drain order -------------------------------------------------------------------------
   The accounts are a queue, not four peers: one per engine drains per tick, picked by nearest
   reset. A row states that ordering instead of asking you to rebuild it from four corners. */
.rows{border:1px solid var(--line);background:var(--panel);margin-top:12px}
.rowhd{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:11px 16px;
  border-bottom:1px solid var(--line);font-size:10.5px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--dim2)}
.arow{border-top:1px solid var(--line2)}
.arow:first-of-type{border-top:0}
.arow>summary{list-style:none;cursor:pointer}
.arow>summary::-webkit-details-marker{display:none}
.asum{display:grid;grid-template-columns:3px 1.55fr 1.2fr 1fr 16px;align-items:center;
  gap:16px;padding:13px 16px 13px 0}
.arow>summary:hover{background:rgba(255,255,255,.03)}
.stripe{align-self:stretch;background:var(--dim2)}
.stripe.on{background:var(--acc)}
.stripe.warn{background:var(--warn)}
.stripe.unk{background:oklch(0.66 0.045 250)}
.aname{min-width:0;padding-left:13px}
.aname .n1{font-size:12.5px;letter-spacing:.08em;display:block}
.aname .n2{display:block;margin-top:5px;font-size:11px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim)}
.aname .n2.acc{color:var(--acc2)}
.aname .n2.warn{color:var(--warn)}
.aname .n2.unk{color:oklch(0.66 0.045 250)}
.abudget{min-width:0}
.abudget .fig{font-size:12px;color:var(--dim);letter-spacing:.04em;font-variant-numeric:tabular-nums}
.abudget .fig b{font-weight:400;color:var(--fg);font-size:15px}
.abudget .fig.unk,.abudget .fig.unk b{color:oklch(0.66 0.045 250)}
.abudget .bar{margin-top:7px}
.paceline{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-top:8px;
  font-size:10.5px;letter-spacing:.04em;color:var(--dim2);font-variant-numeric:tabular-nums}
.paceline b{font-weight:400;white-space:nowrap;color:var(--dim)}
.paceline .unk{color:oklch(0.66 0.045 250)}
.paceline b.nil{color:oklch(0.66 0.045 250)}
.awhen{text-align:right;font-variant-numeric:tabular-nums;min-width:0}
.awhen .t{font-size:20px;letter-spacing:-.01em;display:block;line-height:1.1}
.awhen .t.acc{color:var(--acc2)}
.awhen .t.dimv{color:var(--dim)}
.awhen .l{display:block;margin-top:5px;font-size:9.5px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--dim2)}
.asum .caret{justify-self:center;margin:0}
.adet{padding:0 16px 16px 32px;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px 26px}
.dgroup .dh{font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim2);
  margin-bottom:7px}
.dgroup .dl{display:flex;justify-content:space-between;gap:10px;font-size:11px;color:var(--dim);
  padding:3px 0;font-variant-numeric:tabular-nums}
.dgroup .dl b{font-weight:400;color:var(--fg)}
.dgroup .dl b.ok{color:var(--ok)}
.dgroup .dl b.dim{color:var(--dim)}
.dgroup .dl b.warn{color:var(--warn)}
.dgroup .dl b.acc{color:var(--acc2)}
.dgroup .dl b.unk{color:oklch(0.66 0.045 250)}
.dnil{font-size:11px;color:var(--dim2)}

/* pacing strip ------------------------------------------------------------------------ */
.card.pace{margin-top:12px}
.pgrid{display:flex;gap:5px;margin-top:14px;align-items:flex-end}
.pcol{flex:1;text-align:center;min-width:0}
.pcol.wide{flex:1.5}
.pbar{background:rgba(255,255,255,.08);position:relative;overflow:hidden}
.pbar.held{background:rgba(255,255,255,.04);border:1px solid var(--line)}
.pbar.now{background:var(--acc)}
.pbar.next{background:oklch(0.80 0.14 78 / .16)}
.pbar.final{background:oklch(0.80 0.14 78 / .16);border:1px dashed oklch(0.80 0.14 78 / .5)}
.pbar.now.final{background:var(--acc)}
.pbar .sweep{position:absolute;top:0;bottom:0;left:0;width:28%;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.55),transparent);
  animation:bdsweep 2.6s linear infinite}
.plbl{font-size:9.5px;color:var(--dim2);margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pcol .pbar.now+.plbl{color:var(--acc2)}

/* sections ---------------------------------------------------------------------------- */
.sec{margin-top:26px}
.sech{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;
  font-size:12.5px;letter-spacing:.06em;margin-bottom:10px}
.sech .note{font-size:10.5px;color:var(--dim2);letter-spacing:0}
/* A collapsible section whose summary IS the section header, so an open fold is
   indistinguishable from a plain section and only the caret marks it as foldable. */
details.fold>summary{cursor:pointer;list-style:none}
details.fold>summary::-webkit-details-marker{display:none}
details.fold>summary:hover{color:var(--acc2)}
.caret{display:inline-block;width:0;height:0;margin-right:9px;vertical-align:2px;
  border-left:4px solid currentColor;border-top:3px solid transparent;border-bottom:3px solid transparent;
  transition:transform .12s ease}
details[open]>summary .caret{transform:rotate(90deg)}
footer{margin:34px 0 0;font-size:10.5px;color:var(--dim2);letter-spacing:.04em}
.empty{color:var(--dim);padding:14px 16px;background:var(--panel);border:1px solid var(--line)}

/* in-flight rows ---------------------------------------------------------------------- */
.fl{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 16px;border-top:1px solid var(--line2)}
.fl:first-child{border-top:0}
.fl .pdot{width:6px;height:6px;border-radius:50%;background:var(--acc);flex:none;animation:bdpulse 1.6s ease-in-out infinite}
.fltitle{flex:1 1 240px;font-size:12.5px;min-width:0}
.elapsed{font-variant-numeric:tabular-nums;font-size:12px}

/* queue filters ------------------------------------------------------------------------
   Chips, not selects: the whole facet is visible at rest with its count, and a native <select>
   multiple is unusable on the phone this page is mostly read on. `all` is a chip of its own so
   the cleared state is something you press, not something you deduce. */
[hidden]{display:none!important}
.qfilters{display:flex;flex-wrap:wrap;align-items:center;gap:9px 16px;
  padding:9px 12px;background:var(--panel);border:1px solid var(--line)}
.fgrp{display:flex;align-items:center;gap:4px;flex-wrap:wrap;min-width:0}
/* Groups are separated by a rule rather than a heading: the marks name themselves, and the
   only thing a reader needs is where one facet ends and the next begins. */
.fgrp+.fgrp{border-left:1px solid var(--line);padding-left:14px}
.fchip{display:inline-flex;align-items:center;gap:5px;min-height:30px;padding:0 8px;
  font:inherit;font-size:11px;cursor:pointer;border-radius:0;background:none;
  border:1px solid rgba(233,231,226,.18);color:rgba(233,231,226,.78)}
.fchip:hover{background:rgba(255,255,255,.07)}
.fchip .ico{width:15px;height:15px;margin-right:0;opacity:.82}
.fchip .sizeblocks{height:12px}
.fchip .sizeblocks i{width:4px;height:9px;opacity:.28}
.fchip .sizeblocks i.on{opacity:.92}
.fchip i{font-style:normal;font-size:10px;color:var(--dim2);font-variant-numeric:tabular-nums}
/* Selected is a lift in weight, not the accent: amber on this page means LIVE, and a selected
   filter is neither live nor an account state. */
.fchip.on{border-color:rgba(233,231,226,.52);background:rgba(255,255,255,.10);color:var(--fg)}
.fchip.on i{color:var(--dim)}
.freset{margin-left:auto;min-height:30px;padding:0 8px;font:inherit;font-size:10.5px;
  cursor:pointer;background:none;border:1px solid transparent;color:var(--dim2);border-radius:0}
.freset:hover{color:var(--acc2)}
#qlist{margin-top:2px}
@media(max-width:640px){
  .qfilters{gap:8px 12px}
  .fgrp+.fgrp{border-left:0;padding-left:0}
  .freset{margin-left:0}
}

/* queue ------------------------------------------------------------------------------- */
.band{display:flex;align-items:center;gap:10px;padding:18px 0 6px}
.bpri{font-size:10.5px;letter-spacing:.14em;color:var(--c)}
.brule{flex:1;height:1px;background:var(--line)}
.bcount{font-size:10px;color:var(--dim2)}
.qrow{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start;padding:12px 0;border-top:1px solid var(--line2)}
.qrow.off{opacity:.62;padding:12px 16px}
.qn{width:20px;font-size:11.5px;color:var(--dim2);padding-top:1px;flex:none}
.qmain{flex:1 1 300px;min-width:0}
.qtitle{font-size:13.5px;line-height:1.35}
.qsub{font-size:11.5px;color:var(--dim2);margin-top:8px;line-height:1.5;text-wrap:pretty}
.qmeta{display:flex;gap:13px;align-items:center;flex-wrap:wrap;margin-top:5px;min-height:20px;
  font-size:11px;color:var(--dim2)}
.qmeta-icon{display:inline-flex;align-items:center;gap:5px;line-height:1;color:var(--dim)}
.qmeta-icon .ico{width:18px;height:18px;margin-right:0;color:var(--fg);opacity:.82}
.qmeta-label{font-variant-numeric:tabular-nums}
.qrepo{color:var(--dim);font-size:11px}
.sizeblocks{display:inline-flex;align-items:center;gap:2px;height:14px}
.sizeblocks i{display:block;width:5px;height:10px;background:currentColor;opacity:.22}
.sizeblocks i.on{opacity:.9}
.qact{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.runset{display:inline-flex;gap:5px;align-items:center;white-space:nowrap}
.runlbl{font-size:9.5px;letter-spacing:.10em;text-transform:uppercase;color:var(--dim2)}
/* The three engines are peers, so they are one button style. Tinting `claude` as the accent
   reads as a recommendation when it is only the default, and the accent is spoken for anyway:
   on this page amber means LIVE, not preferred. Disabled-ness is the only thing that may
   change a control's appearance. */
.task-run,.task-toggle{display:inline-flex;align-items:center;justify-content:center;
  min-height:38px;padding:0 12px;font:inherit;font-size:11px;cursor:pointer;
  background:none;border:1px solid rgba(233,231,226,.18);color:rgba(233,231,226,.78);border-radius:0}
.task-run:hover:enabled,.task-toggle:hover:enabled{background:rgba(255,255,255,.07)}
.task-toggle{color:var(--dim)}
.task-toggle:hover:enabled{color:var(--warn);border-color:oklch(0.72 0.17 40 / .55)}
/* An in-flight launch is `wait`; a Codex button on a Claude-only task is permanently
   unavailable, so it reads `not-allowed` and dims further - the two must not look alike. */
.task-run:disabled,.task-toggle:disabled{cursor:wait;opacity:.6}
.task-run.unavailable{cursor:not-allowed;opacity:.3}

/* run log ----------------------------------------------------------------------------- */
.lg{display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;padding:9px 16px}
.ltime{font-size:11px;color:var(--dim2);font-variant-numeric:tabular-nums;min-width:56px}
.lstat{font-size:10.5px;letter-spacing:.10em;text-transform:uppercase;min-width:74px;color:var(--c)}
.ltitle{flex:1 1 220px;font-size:12px;min-width:0}
.lnote{flex:1 1 190px;font-size:11px;color:var(--dim2);min-width:0}

/* scheduled-jobs tab ------------------------------------------------------------------- */
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 0}
.stat{display:inline-flex;align-items:center;gap:6px;background:var(--panel);border:1px solid var(--line);padding:5px 10px;font-size:11px}
.stat .k{color:var(--dim)}
.stat .v{color:var(--c)}
.muted{color:var(--dim)}

/* engine marks -------------------------------------------------------------------------
   The sprite must stay in the layout with zero size rather than display:none, which stops
   <use> resolving in some engines. Marks inherit currentColor, so an icon always matches the
   state colour of whatever line it sits on. */
.sprite{position:absolute;width:0;height:0;overflow:hidden}
.ico{width:1.25em;height:1.25em;flex:none;vertical-align:-.22em;color:inherit;margin-right:.42em}
.ebadge{display:inline-flex;align-items:center;gap:5px}
.ebadge .ico{margin-right:0}
/* In a name the mark reads as part of the label, so it takes a little more size and shrugs off
   the dimming the surrounding text carries. */
.lname .ico,.n1 .ico{width:1.45em;height:1.45em;margin-right:.45em;color:var(--fg);opacity:.9}
.lname .ico{vertical-align:-.28em}
.n1 .ico{vertical-align:-.3em}
.vtext .ico{width:1.3em;height:1.3em;margin-right:.42em;vertical-align:-.24em}
.vsub .ico{margin-right:.34em}
/* Icon-only force buttons: square, so the mark is the whole target rather than an ornament in
   front of a word. Sized in px, not em, because the button no longer carries text to scale from. */
.task-run.ionly,.task-toggle.ionly{padding:0;min-width:44px}
.runset .ico,.task-toggle.ionly .ico{width:20px;height:20px;margin-right:0}
/* The spinner swaps in for a button's own glyph while its request is in flight. Both are in the
   DOM the whole time, so nothing has to rebuild the button's contents to show progress. */
.ionly .ico.spin{display:none}
.ionly.busy .ico{display:none}
.ionly.busy .ico.spin{display:block;animation:bdspin .8s linear infinite;transform-origin:50% 50%}
/* Slowed rather than stopped under reduced motion: a frozen spinner reads as a hung button. */
@media(prefers-reduced-motion:reduce){.ionly.busy .ico.spin{animation-duration:2.4s}}
.sech .ico,.rowhd .ico{margin-right:.34em;opacity:.8}
details.fam{background:var(--panel);border:1px solid var(--line);margin-bottom:12px;overflow:hidden}
details.fam>summary{cursor:pointer;padding:12px 14px;list-style:none;display:flex;align-items:center;gap:8px;
  font-size:12.5px;letter-spacing:.06em;background:rgba(255,255,255,.02)}
details.fam>summary::-webkit-details-marker{display:none}
.fdot{width:8px;height:8px;border-radius:50%;background:var(--c);flex:0 0 auto}
.count{border:1px solid var(--line);color:var(--dim);padding:0 7px;font-size:10.5px}
.blurb{color:var(--dim2);font-size:10.5px;letter-spacing:0}
table.grid{width:100%;border-collapse:collapse;font-size:12px}
.grid th{text-align:left;color:var(--dim2);font-weight:400;font-size:10px;letter-spacing:.10em;
  text-transform:uppercase;border-bottom:1px solid var(--line);padding:8px 14px}
.grid td{border-top:1px solid var(--line2);padding:9px 14px;vertical-align:top}
.trow{cursor:pointer}.trow:hover{background:rgba(255,255,255,.03)}
.unit{word-break:break-word}
.freq .sched{display:block;font-size:10.5px;color:var(--dim2);margin-top:3px}
.rbadge{border:1px solid var(--c);color:var(--c);padding:1px 7px;font-size:10.5px;white-space:nowrap}
.nowrap{white-space:nowrap}
.detailrow{display:none}.detailrow.show{display:table-row}
.detail{padding:2px 0 8px}
.d-lbl{color:var(--dim2);font-size:9.5px;text-transform:uppercase;letter-spacing:.10em;margin:8px 0 4px}
.d-desc{color:var(--dim);margin-bottom:4px}
.detail pre{margin:0;padding:11px;background:rgba(255,255,255,.03);border:1px solid var(--line);
  white-space:pre-wrap;word-break:break-word;font-size:11px;max-height:340px;overflow:auto}
.detail pre.cmd{color:var(--dim)}

/* On a phone the queue row cannot fit its metadata AND its four controls, and the controls are
   the only part you can't get anywhere else - an engine button hanging off the right edge is a
   button that does not exist. Drop the goal line and the FORCE label, let the buttons take the
   full row width under the title, and hide the schedule tab's second-line cron expression. */
@media(max-width:640px){
  .qsub{display:none}.runlbl{display:none}.freq .sched{display:none}
  .qact{width:100%;justify-content:flex-end}
  .lnote{flex-basis:100%}
  .tldays{margin:0}
  .lane{grid-template-columns:1fr;gap:5px}
  .lane .lname{padding-right:0}
  .asum{grid-template-columns:3px 1fr 14px;gap:10px;row-gap:12px}
  .abudget,.awhen{grid-column:2}
  .awhen{text-align:left}
}
"""

SCRIPT = """
<script>
(function(){
  var KEY='jobsViewerTab';
  function show(t){
    var tabs=document.querySelectorAll('.tab');
    var panes=document.querySelectorAll('.pane');
    var ok=false;
    panes.forEach(function(p){if(p.dataset.pane===t)ok=true});
    if(!ok)t='bonus';
    tabs.forEach(function(b){b.classList.toggle('active',b.dataset.tab===t)});
    panes.forEach(function(p){p.classList.toggle('active',p.dataset.pane===t)});
    try{localStorage.setItem(KEY,t)}catch(e){}
  }
  document.querySelectorAll('.tab').forEach(function(b){
    b.addEventListener('click',function(){show(b.dataset.tab)});
  });
  document.querySelectorAll('.task-toggle').forEach(function(b){
    b.addEventListener('click',async function(){
      b.disabled=true;b.classList.add('busy');
      try{
        var response=await fetch('/api/bonus/task',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:b.dataset.taskId,active:b.dataset.active==='1'})});
        if(!response.ok)throw new Error('update failed');
        location.reload();
      }catch(e){b.disabled=false;b.classList.remove('busy');alert('Could not update this job.');}
    });
  });
  document.querySelectorAll('.task-run').forEach(function(b){
    b.addEventListener('click',async function(){
      // Disable the whole row's run set, not just the clicked button: two engines fired at the
      // same task would race, and only one of them can win run-now's eligibility re-check.
      var set=b.closest('.runset');
      var peers=set?Array.prototype.slice.call(set.querySelectorAll('.task-run')):[b];
      var wasDisabled=peers.map(function(p){return p.disabled});
      peers.forEach(function(p){p.disabled=true});
      b.classList.add('busy');
      try{
        var response=await fetch('/api/bonus/task/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:b.dataset.taskId,engine:b.dataset.engine})});
        var data=await response.json().catch(function(){return {}});
        if(!response.ok)throw new Error(data.message||'launch failed');
        location.reload();
      }catch(e){
        peers.forEach(function(p,i){p.disabled=wasDisabled[i]});
        b.classList.remove('busy');
        alert('Could not launch this job on '+b.dataset.engine+': '+e.message);
      }
    });
  });
  // Queue facet filters. Client-side and localStorage-backed, so a selection survives the
  // page's own 60s meta refresh; the server keeps rendering the whole queue.
  (function(){
    var bar=document.getElementById('qfilters'),list=document.getElementById('qlist');
    if(!bar||!list)return;
    var GROUPS=['kind','provider','priority','size'],FKEY='bonusQueueFilters';
    var chips=Array.prototype.slice.call(bar.querySelectorAll('.fchip'));
    var rows=Array.prototype.slice.call(list.querySelectorAll('.qrow'));
    var bands=Array.prototype.slice.call(list.querySelectorAll('.band'));
    var none=document.getElementById('qnone'),count=document.getElementById('qcount');
    var reset=document.getElementById('qfreset');
    var offered={};
    GROUPS.forEach(function(g){offered[g]=[]});
    chips.forEach(function(c){if(c.dataset.value)offered[c.dataset.group].push(c.dataset.value)});
    var state={};
    GROUPS.forEach(function(g){state[g]=[]});
    try{
      var raw=JSON.parse(localStorage.getItem(FKEY)||'{}');
      // Drop values this cycle no longer offers, or a stale selection would silently hide
      // every row and read as an empty queue.
      GROUPS.forEach(function(g){
        if(Array.isArray(raw[g]))state[g]=raw[g].filter(function(v){return offered[g].indexOf(v)>=0});
      });
    }catch(e){}
    function keep(row){
      return GROUPS.every(function(g){
        var sel=state[g];
        if(!sel.length)return true;
        if(g==='provider'){
          var have=(row.dataset.providers||'').split(' ');
          return sel.some(function(v){return have.indexOf(v)>=0});
        }
        return sel.indexOf(row.dataset[g==='kind'?'kind':g])>=0;
      });
    }
    function apply(){
      var shown=0,per={};
      rows.forEach(function(r){
        var ok=keep(r);
        r.hidden=!ok;
        if(!ok)return;
        shown++;
        var n=r.querySelector('.qn');
        if(n)n.textContent=shown;
        per[r.dataset.priority]=(per[r.dataset.priority]||0)+1;
      });
      bands.forEach(function(b){
        var n=per[b.dataset.band]||0;
        b.hidden=!n;
        var c=b.querySelector('.bcount');
        if(c)c.textContent=n+' task'+(n===1?'':'s');
      });
      if(none)none.hidden=shown>0;
      var filtered=GROUPS.some(function(g){return state[g].length});
      if(count)count.textContent=(filtered?shown+' of '+rows.length:rows.length)+' job'+(rows.length===1&&!filtered?'':'s');
      if(reset)reset.hidden=!filtered;
      chips.forEach(function(c){
        var v=c.dataset.value,on=v?state[c.dataset.group].indexOf(v)>=0:!state[c.dataset.group].length;
        c.classList.toggle('on',on);
        c.setAttribute('aria-pressed',on?'true':'false');
      });
      try{localStorage.setItem(FKEY,JSON.stringify(state))}catch(e){}
    }
    chips.forEach(function(c){
      c.addEventListener('click',function(){
        var g=c.dataset.group,v=c.dataset.value;
        if(!v){state[g]=[]}
        else{var i=state[g].indexOf(v);if(i>=0)state[g].splice(i,1);else state[g].push(v)}
        apply();
      });
    });
    if(reset)reset.addEventListener('click',function(){
      GROUPS.forEach(function(g){state[g]=[]});apply();
    });
    apply();
  })();
  var saved='bonus';
  try{saved=localStorage.getItem(KEY)||'bonus'}catch(e){}
  show(saved);
})();
</script>
"""

# Geist Mono is the console skin's face. It is fetched from Google Fonts rather than vendored,
# and the fallback chain behind it is a full local monospace stack: on a box (or a phone) with
# no route to fonts.googleapis.com the page renders in the system mono and loses nothing but
# the typeface. No other asset is remote.
PAGE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{css}</style>
</head><body>""" + ICON_SPRITE + """<div class="wrap">
<div class="topbar">
  <span class="brand">background jobs</span>
  <div class="tabs">
    <button class="tab active" data-tab="bonus">01 bonus-drain</button>
    <button class="tab" data-tab="schedule">02 scheduled</button>
  </div>
</div>
<div class="pane active" data-pane="bonus">{bonus}</div>
<div class="pane" data-pane="schedule">{schedule}</div>
</div>{script}</body></html>"""


def render_page() -> bytes:
    try:
        bonus = render_bonus_body()
    except Exception as e:
        bonus = f'<h1>bonus-drain</h1><p class="empty">error: {esc(e)}</p>'
    try:
        schedule = render_schedule_body()
    except Exception as e:
        schedule = f'<h1>scheduled jobs</h1><p class="empty">error: {esc(e)}</p>'
    return PAGE.format(title="background jobs", css=CSS, script=SCRIPT,
                       bonus=bonus, schedule=schedule).encode("utf-8")


# ===========================================================================
# HTTP
# ===========================================================================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if not self._request_allowed(mutation=False):
            return
        if self.path in ("/", "/index.html"):
            try:
                body = render_page()
            except Exception as e:  # never 500 the whole page on a data hiccup
                body = PAGE.format(title="background jobs", css=CSS, script="",
                                   bonus=f'<p class="empty">error: {esc(e)}</p>',
                                   schedule="").encode()
            self._send(HTTPStatus.OK, body)
        elif self.path == "/api/bonus":
            usage = get_usage()
            cycle = current_cycle(usage)
            payload = {"usage": usage, "cycle": cycle,
                       "remaining": get_remaining(cycle), "disabled": get_disabled(),
                       "recent": get_recent_runs()}
            self._send(HTTPStatus.OK, json.dumps(payload).encode(), "application/json")
        elif self.path == "/api/schedule":
            data = [enrich(t) for t in list_timers()]
            self._send(HTTPStatus.OK, json.dumps(data).encode(), "application/json")
        elif self.path == "/health":
            self._send(HTTPStatus.OK, b"ok", "text/plain")
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def do_POST(self):
        if not MUTATIONS_ENABLED:
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b'{"error":"mutations disabled"}', "application/json")
            return
        if not self._request_allowed(mutation=True):
            return
        if self.path == "/api/bonus/task":
            payload = self._read_json()
            if payload is None:
                return
            task_id, active = payload.get("id"), payload.get("active")
            if not isinstance(task_id, str) or not isinstance(active, bool):
                self._bad_request()
                return
            ok, message = set_task_active(task_id, active)
        elif self.path == "/api/bonus/task/run":
            payload = self._read_json()
            if payload is None:
                return
            task_id, engine = payload.get("id"), payload.get("engine")
            if not isinstance(task_id, str) or engine not in RUN_ENGINES:
                self._bad_request()
                return
            ok, message = run_task_now(task_id, engine)
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        status = HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST
        self._send(status, json.dumps({"ok": ok, "message": message}).encode(), "application/json")

    def _read_json(self) -> dict | None:
        """Body as a dict, or None having already sent the 400."""
        try:
            if self._one_header("Content-Type") != "application/json":
                raise ValueError
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, TypeError, json.JSONDecodeError):
            self._bad_request()
            return None

    def _bad_request(self):
        self._send(HTTPStatus.BAD_REQUEST, b'{"error":"invalid request"}', "application/json")

    def _one_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        return values[0] if len(values) == 1 else None

    def _request_allowed(self, *, mutation: bool) -> bool:
        host = self._one_header("Host")
        if host not in ALLOWED_HOSTS:
            self._send(HTTPStatus.FORBIDDEN, b'{"error":"host rejected"}', "application/json")
            return False
        if not mutation:
            return True
        origin = self._one_header("Origin")
        if origin not in ALLOWED_ORIGINS:
            self._send(HTTPStatus.FORBIDDEN, b'{"error":"request rejected"}', "application/json")
            return False
        return True

    def _send(self, status, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8766")))
    ap.add_argument("--host", default=os.environ.get("HOST"))
    args = ap.parse_args()
    configure_request_boundary()
    host = args.host or VIEWER_BIND
    if host != VIEWER_BIND:
        raise SystemExit(f"viewer host must match configured loopback bind {VIEWER_BIND}")
    if not DB_PATH.exists():
        print(f"warning: {DB_PATH} not found; the bonus tab will render empty until bonus-drain is initialized")
    if shutil.which("bash") is None:
        print("warning: bash not found; remaining-table pick will be empty")
    srv = ThreadingHTTPServer((host, args.port), Handler)
    print(f"jobs viewer on http://{host}:{args.port}  (db={DB_PATH})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
