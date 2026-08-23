#!/usr/bin/env bash
# Bonus drain account helpers layer on the claude token rotator store.
# Sourced by scout.sh, and runnable as a CLI so the
# drain runner resolves CYCLE identically.
set -uo pipefail

_ACC_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# config.sh preserves every explicit override and supplies the stable source-tree CLI path.
# shellcheck source=skills/bonus-drain/config.sh
source "$_ACC_HERE/config.sh"

bonus_account_labels() {
  local store="${1:-${BONUS_ACCOUNTS_STORE:-}}"
  local path base
  [ -n "$store" ] && [ -d "$store" ] || return 0
  for path in "$store"/*.json; do
    [ -e "$path" ] || continue
    base="${path##*/}"
    [ "$base" = "mcp.json" ] && continue
    case "$base" in
      *.usage.json) continue ;;
    esac
    printf '%s\n' "${base%.json}"
  done | sort
}

bonus_account_count() {
  local store="${1:-${BONUS_ACCOUNTS_STORE:-}}"
  local count
  count="$(bonus_account_labels "$store" | wc -l)"
  printf '%s\n' "${count//[[:space:]]/}"
}

bonus_multi_active() {
  local store="${1:-${BONUS_ACCOUNTS_STORE:-}}"
  local count
  [ -n "$store" ] && [ -d "$store" ] || return 1
  count="$(bonus_account_count "$store")"
  [ "${count:-0}" -ge 2 ]
}

_bonus_fetch_usage() {
  local token="${1:-}"
  local mock resp
  [ -n "$token" ] || { echo ""; return 0; }
  if [ -n "${ROTATOR_USAGE_MOCK_DIR:-}" ]; then
    mock="$ROTATOR_USAGE_MOCK_DIR/$token.json"
    [ -f "$mock" ] && cat "$mock" || echo ""
    return 0
  fi
  resp="$(printf 'Authorization: Bearer %s\n' "$token" | curl -s --max-time 6 \
    "https://api.anthropic.com/api/oauth/usage" \
    -H @- \
    -H "anthropic-beta: oauth-2025-04-20" \
    -H "Content-Type: application/json" 2>/dev/null || true)"
  if printf '%s' "$resp" | jq -e '.five_hour' >/dev/null 2>&1; then
    printf '%s\n' "$resp"
  else
    echo ""
  fi
}

_bonus_usage_fields_from_json() {
  local json="${1:-}"
  local five weekly reset_iso reset_epoch
  if [ -z "$json" ]; then
    echo "  "
    return 0
  fi
  five="$(printf '%s' "$json" | jq -r '.five_hour.utilization // empty' 2>/dev/null || true)"
  weekly="$(printf '%s' "$json" | jq -r '.seven_day.utilization // empty' 2>/dev/null || true)"
  reset_iso="$(printf '%s' "$json" | jq -r '.seven_day.resets_at // empty' 2>/dev/null || true)"
  reset_epoch=""
  if [ -n "$reset_iso" ]; then
    reset_epoch="$(date -d "$reset_iso" +%s 2>/dev/null || true)"
  fi
  printf '%s %s %s\n' "$five" "$weekly" "$reset_epoch"
}

bonus_account_usage() {
  local label="${1:?label required}"
  local store="${2:-${BONUS_ACCOUNTS_STORE:-}}"
  local now max_age snapshot age mtime cred token json
  now="${BONUS_NOW_OVERRIDE:-$(date +%s)}"
  max_age="${BONUS_USAGE_MAX_AGE:-3600}"
  snapshot="$store/$label.usage.json"
  cred="$store/$label.json"

  if [ -f "$snapshot" ]; then
    mtime="$(stat -c %Y "$snapshot" 2>/dev/null || echo 0)"
    age=$(( now - mtime ))
    if [ "$age" -le "$max_age" ]; then
      _bonus_usage_fields_from_json "$(cat "$snapshot")"
      return 0
    fi
  fi

  token=""
  if [ -f "$cred" ]; then
    token="$(jq -r '.claudeAiOauth.accessToken // empty' "$cred" 2>/dev/null || true)"
  fi
  if [ -n "$token" ]; then
    json="$(_bonus_fetch_usage "$token")"
    if [ -n "$json" ]; then
      _bonus_usage_fields_from_json "$json"
      return 0
    fi
  fi

  if [ -f "$snapshot" ]; then
    _bonus_usage_fields_from_json "$(cat "$snapshot")"
  else
    echo "  "
  fi
}

# bonus_account_ceiling <label> - the weekly ceiling this account is held to, 0-100.
#
# Resolved from the ROTATOR's config.env (WEEKLY_CEIL_<label>, then WEEKLY_CEIL_DEFAULT), which
# is the single source of truth; see the note on BONUS_ROTATOR_CONFIG in config.sh. Falls back
# to DRAIN_UNTIL_PCT when there is no rotator config, which is the single-account case where
# ceilings do not exist and behavior must be unchanged.
#
# Parsed with sed rather than sourced: config.env is plain KEY=VALUE, and sourcing it here would
# execute an operator's file inside every drain tick and leak its other settings (ACCOUNTS,
# thresholds) into this process. The ^[[:space:]]*KEY= anchor also means a commented-out
# override cannot win.
bonus_account_ceiling() {
  local label="${1:?label required}"
  local cfg="${BONUS_ROTATOR_CONFIG:-}"
  local fallback="${DRAIN_UNTIL_PCT:-98}"
  local key val
  if [ -z "$cfg" ] || [ ! -f "$cfg" ]; then
    printf '%s\n' "$fallback"
    return 0
  fi
  key="WEEKLY_CEIL_$(printf '%s' "$label" | tr -c 'A-Za-z0-9_' '_')"
  val="$(sed -n "s/^[[:space:]]*${key}=[\"']\{0,1\}\([0-9.]\{1,\}\).*/\1/p" "$cfg" | tail -1)"
  [ -n "$val" ] || val="$(sed -n "s/^[[:space:]]*WEEKLY_CEIL_DEFAULT=[\"']\{0,1\}\([0-9.]\{1,\}\).*/\1/p" "$cfg" | tail -1)"
  [ -n "$val" ] || val="$fallback"
  printf '%s\n' "$val"
}

bonus_select_drain_account() {
  local store="${1:-${BONUS_ACCOUNTS_STORE:-}}"
  local now="${2:-${BONUS_NOW_OVERRIDE:-$(date +%s)}}"
  local lead_secs label five weekly reset secs open take acct_ceil
  local best_label="" best_reset="" best_five="" best_weekly="" best_secs=""
  lead_secs=$(( ${DRAIN_LEAD_MAX_HOURS:-30} * 3600 ))

  while IFS= read -r label; do
    [ -n "$label" ] || continue
    five=""
    weekly=""
    reset=""
    read -r five weekly reset < <(bonus_account_usage "$label" "$store") || true
    [ -n "${reset:-}" ] || continue
    case "$reset" in
      *[!0-9]*) continue ;;
    esac
    secs=$(( reset - now ))
    [ "$secs" -gt 0 ] || continue
    [ "$secs" -le "$lead_secs" ] || continue
    [ -n "${weekly:-}" ] || continue
    # Each account is judged against ITS OWN ceiling, not one global number. An account
    # reserved for interactive surfaces caps lower than one used only for background work,
    # so a raw weekly of 96 can be spent on one account and over the line on another.
    acct_ceil="$(bonus_account_ceiling "$label")"
    open="$(awk -v weekly="$weekly" -v ceil="$acct_ceil" 'BEGIN{print ((weekly+0) < (ceil+0)) ? 1 : 0}')"
    [ "$open" = 1 ] || continue
    take="$(awk -v secs="$secs" -v best="$best_secs" -v weekly="$weekly" -v bestw="$best_weekly" '
      BEGIN{
        if (best == "") print 1;
        else if ((secs+0) < (best+0)) print 1;
        else if ((secs+0) == (best+0) && (weekly+0) < (bestw+0)) print 1;
        else print 0;
      }')"
    if [ "$take" = 1 ]; then
      best_label="$label"
      best_reset="$reset"
      best_five="$five"
      best_weekly="$weekly"
      best_secs="$secs"
    fi
  done < <(bonus_account_labels "$store")

  if [ -n "$best_label" ]; then
    printf '%s %s %s %s\n' "$best_label" "$best_reset" "$best_five" "$best_weekly"
  fi
}

bonus_canonical_cycle() {
  local now="${1:-${BONUS_NOW_OVERRIDE:-$(date +%s)}}"
  local dow hour minute second sod midnight anchor_dow hhmm anchor_h anchor_m anchor_sod days_ahead c
  dow="$(date -u -d "@$now" +%w)"
  hour="$(date -u -d "@$now" +%H)"
  minute="$(date -u -d "@$now" +%M)"
  second="$(date -u -d "@$now" +%S)"
  sod=$(( 10#$hour * 3600 + 10#$minute * 60 + 10#$second ))
  midnight=$(( now - sod ))
  anchor_dow="${BONUS_WEEKLY_ANCHOR_DOW:-6}"
  hhmm="${BONUS_WEEKLY_ANCHOR_HHMM:-2205}"
  anchor_h=$((10#${hhmm:0:2}))
  anchor_m=$((10#${hhmm:2:2}))
  anchor_sod=$(( anchor_h * 3600 + anchor_m * 60 ))
  days_ahead=$(( (anchor_dow - dow + 7) % 7 ))
  if [ "$days_ahead" -eq 0 ] && [ "$sod" -ge "$anchor_sod" ]; then
    days_ahead=7
  fi
  c=$(( midnight + days_ahead * 86400 + anchor_sod ))
  echo $(( (c + 1800) / 3600 * 3600 ))
}

bonus_resolve_cycle() {
  local _u5 _r5 _u7 r7
  if bonus_multi_active; then
    bonus_canonical_cycle
    return 0
  fi
  read -r _u5 _r5 _u7 r7 < <("${BONUS_USAGE_CMD:-$_ACC_HERE/usage.sh}") || true
  case "${r7:-}" in
    ""|*[!0-9]*) echo ""; return 0 ;;
  esac
  [ "$r7" -gt 0 ] && echo $(( (r7 + 1800) / 3600 * 3600 )) || echo ""
}

bonus_write_pin() {
  local label="${1:?label required}"
  local pin="${BONUS_PIN_FILE:-}"
  local store="${BONUS_ACCOUNTS_STORE:-}"
  local dir tmp
  [ -n "$pin" ] || return 0
  [ -z "$store" ] || [ -d "$store" ] || return 0
  dir="$(dirname "$pin")"
  [ -d "$dir" ] || return 0
  tmp="$(mktemp "$dir/.PIN.tmp.XXXXXX")" || return 1
  printf '%s' "$label" > "$tmp" || { rm -f "$tmp"; return 1; }
  chmod 600 "$tmp" || { rm -f "$tmp"; return 1; }
  mv -f "$tmp" "$pin"
}

bonus_clear_pin() {
  local pin="${BONUS_PIN_FILE:-}"
  [ -n "$pin" ] || return 0
  rm -f "$pin" 2>/dev/null || true
  return 0
}

bonus_read_pin() {
  local pin="${BONUS_PIN_FILE:-}"
  [ -n "$pin" ] && [ -f "$pin" ] && cat "$pin" || echo ""
}

# --- Codex rotator store (read-only) -------------------------------------------------
# Same store layout as the Claude side, with two differences that matter: credentials are
# `<label>.tokens` (not `<label>.json`), so bonus_account_labels finds nothing here; and there
# is no live re-poll fallback, because only the Codex rotator holds the per-account tokens.
# A snapshot older than BONUS_USAGE_MAX_AGE is UNKNOWN, never zero - a zero would read as a
# full weekly budget and make a spent account look like the best place to drain.

bonus_codex_account_labels() {
  local store="${1:-${BONUS_CODEX_ACCOUNTS_STORE:-}}"
  local path base
  [ -n "$store" ] && [ -d "$store" ] || return 0
  for path in "$store"/*.tokens; do
    [ -e "$path" ] || continue
    base="${path##*/}"
    printf '%s\n' "${base%.tokens}"
  done | sort
}

bonus_codex_account_count() {
  local store="${1:-${BONUS_CODEX_ACCOUNTS_STORE:-}}"
  local count
  count="$(bonus_codex_account_labels "$store" | wc -l)"
  printf '%s\n' "${count//[[:space:]]/}"
}

bonus_codex_multi_active() {
  local store="${1:-${BONUS_CODEX_ACCOUNTS_STORE:-}}"
  local count
  [ -n "$store" ] && [ -d "$store" ] || return 1
  count="$(bonus_codex_account_count "$store")"
  [ "${count:-0}" -ge 2 ]
}

# Prints "<weekly_pct> <weekly_reset_epoch>", or nothing when the reading is unknown.
bonus_codex_account_usage() {
  local label="${1:?label required}"
  local store="${2:-${BONUS_CODEX_ACCOUNTS_STORE:-}}"
  local now max_age snapshot mtime age
  now="${BONUS_NOW_OVERRIDE:-$(date +%s)}"
  max_age="${BONUS_USAGE_MAX_AGE:-3600}"
  snapshot="$store/$label.usage.json"
  [ -f "$snapshot" ] || return 0
  mtime="$(stat -c %Y "$snapshot" 2>/dev/null || echo 0)"
  age=$(( now - mtime ))
  [ "$age" -le "$max_age" ] || return 0
  jq -r '
    (.seven_day.utilization) as $u
    | (.seven_day.resets_at) as $r
    | if ($u | type) == "number" and ($r | type) == "number"
      then "\($u) \($r)" else empty end
  ' "$snapshot" 2>/dev/null || true
}

# Per-account Codex ceiling. Deliberately the SAME resolver as the Claude side: the Codex
# rotator enforces WEEKLY_CEIL_<label> per label regardless of provider, and the drain must
# stop pinning at the exact line the rotator refuses to leave the pointer on. A second
# Codex-only number here would drift from enforcement, and the drift stays invisible until an
# account is drained straight through its reserve. Falls back to the flat CODEX_WEEKLY_CEIL
# only when there is no rotator config to read (single-account, no rotator).
bonus_codex_account_ceiling() {
  local label="${1:?label required}"
  local cfg="${BONUS_ROTATOR_CONFIG:-}"
  if [ -n "$cfg" ] && [ -f "$cfg" ]; then
    bonus_account_ceiling "$label"
  else
    printf '%s\n' "${CODEX_WEEKLY_CEIL:-${DRAIN_UNTIL_PCT:-98}}"
  fi
}

# The Codex twin of bonus_select_drain_account: the account whose weekly reset lands soonest
# inside the Codex drain lead and which is still under ITS OWN ceiling. Emits
# "<label> <reset> <weekly>" - three fields, not four, because Codex has no 5h window.
bonus_codex_select_drain_account() {
  local store="${1:-${BONUS_CODEX_ACCOUNTS_STORE:-}}"
  local now="${2:-${BONUS_NOW_OVERRIDE:-$(date +%s)}}"
  local lead_secs label weekly reset secs open take acct_ceil
  local best_label="" best_reset="" best_weekly="" best_secs=""
  lead_secs=$(( ${CODEX_DRAIN_LEAD_MAX_HOURS:-${DRAIN_LEAD_MAX_HOURS:-30}} * 3600 ))

  while IFS= read -r label; do
    [ -n "$label" ] || continue
    weekly=""
    reset=""
    read -r weekly reset < <(bonus_codex_account_usage "$label" "$store") || true
    [ -n "${reset:-}" ] || continue
    case "$reset" in
      *[!0-9]*) continue ;;
    esac
    secs=$(( reset - now ))
    [ "$secs" -gt 0 ] || continue
    [ "$secs" -le "$lead_secs" ] || continue
    [ -n "${weekly:-}" ] || continue
    acct_ceil="$(bonus_codex_account_ceiling "$label")"
    open="$(awk -v weekly="$weekly" -v ceil="$acct_ceil" 'BEGIN{print ((weekly+0) < (ceil+0)) ? 1 : 0}')"
    [ "$open" = 1 ] || continue
    take="$(awk -v secs="$secs" -v best="$best_secs" -v weekly="$weekly" -v bestw="$best_weekly" '
      BEGIN{
        if (best == "") print 1;
        else if ((secs+0) < (best+0)) print 1;
        else if ((secs+0) == (best+0) && (weekly+0) < (bestw+0)) print 1;
        else print 0;
      }')"
    if [ "$take" = 1 ]; then
      best_label="$label"; best_reset="$reset"; best_weekly="$weekly"; best_secs="$secs"
    fi
  done < <(bonus_codex_account_labels "$store")

  if [ -n "$best_label" ]; then
    printf '%s %s %s\n' "$best_label" "$best_reset" "$best_weekly"
  fi
}

bonus_codex_write_pin() {
  local label="${1:?label required}"
  local pin="${BONUS_CODEX_PIN_FILE:-}"
  local store="${BONUS_CODEX_ACCOUNTS_STORE:-}"
  local dir tmp
  [ -n "$pin" ] || return 0
  [ -z "$store" ] || [ -d "$store" ] || return 0
  dir="$(dirname "$pin")"
  [ -d "$dir" ] || return 0
  tmp="$(mktemp "$dir/.PIN.tmp.XXXXXX")" || return 1
  printf '%s' "$label" > "$tmp" || { rm -f "$tmp"; return 1; }
  chmod 600 "$tmp" || { rm -f "$tmp"; return 1; }
  mv -f "$tmp" "$pin"
}

bonus_codex_clear_pin() {
  local pin="${BONUS_CODEX_PIN_FILE:-}"
  [ -n "$pin" ] || return 0
  rm -f "$pin" 2>/dev/null || true
  return 0
}

bonus_codex_read_pin() {
  local pin="${BONUS_CODEX_PIN_FILE:-}"
  [ -n "$pin" ] && [ -f "$pin" ] && cat "$pin" || echo ""
}

_bonus_accounts_usage() {
  echo "usage: accounts.sh ceiling|cycle|canonical-cycle|labels|count|select|multi|usage|codex-labels|codex-count|codex-multi|codex-usage|codex-ceiling|codex-select|write-pin|clear-pin|read-pin" >&2
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  sub="${1:-}"
  shift || true
  if [ -r "${BONUS_DRAIN_CONFIG:-}" ]; then
    case "$sub" in
      cycle|canonical-cycle)
        # argparse's compatibility shape is: action, optional account, optional epoch.
        # These actions have no account, so reserve that positional explicitly.
        exec "$BONUS_DRAIN_BIN" accounts --config "$BONUS_DRAIN_CONFIG" "$sub" _ "$@"
        ;;
      labels|count|multi|usage|select)
        exec "$BONUS_DRAIN_BIN" accounts --config "$BONUS_DRAIN_CONFIG" "$sub" "$@"
        ;;
    esac
  fi
  case "$sub" in
    cycle) bonus_resolve_cycle "$@" ;;
    canonical-cycle) bonus_canonical_cycle "$@" ;;
    labels) bonus_account_labels "${1:-${BONUS_ACCOUNTS_STORE:-}}" ;;
    count) bonus_account_count "${1:-${BONUS_ACCOUNTS_STORE:-}}" ;;
    select) bonus_select_drain_account "${1:-${BONUS_ACCOUNTS_STORE:-}}" "${2:-${BONUS_NOW_OVERRIDE:-$(date +%s)}}" ;;
    multi) bonus_multi_active "${1:-${BONUS_ACCOUNTS_STORE:-}}" ;;
    usage) bonus_account_usage "$@" ;;
    codex-labels) bonus_codex_account_labels "${1:-${BONUS_CODEX_ACCOUNTS_STORE:-}}" ;;
    codex-count) bonus_codex_account_count "${1:-${BONUS_CODEX_ACCOUNTS_STORE:-}}" ;;
    codex-multi) bonus_codex_multi_active "${1:-${BONUS_CODEX_ACCOUNTS_STORE:-}}" ;;
    codex-usage) bonus_codex_account_usage "$@" ;;
    codex-ceiling) bonus_codex_account_ceiling "$@" ;;
    codex-select) bonus_codex_select_drain_account "${1:-${BONUS_CODEX_ACCOUNTS_STORE:-}}" "${2:-${BONUS_NOW_OVERRIDE:-$(date +%s)}}" ;;
    codex-write-pin) bonus_codex_write_pin "$@" ;;
    codex-clear-pin) bonus_codex_clear_pin ;;
    codex-read-pin) bonus_codex_read_pin ;;
    ceiling) bonus_account_ceiling "$@" ;;
    write-pin) bonus_write_pin "$@" ;;
    clear-pin) bonus_clear_pin ;;
    read-pin) bonus_read_pin ;;
    *) _bonus_accounts_usage; exit 2 ;;
  esac
fi
