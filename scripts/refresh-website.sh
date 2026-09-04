#!/usr/bin/env bash
# Daily incremental refresh of the website fallback index.
#
# Installed as a cron job (see README). Safe to run by hand at any time.
# A day with no site changes costs one sitemap fetch and a few seconds;
# only pages whose sitemap lastmod moved are re-fetched.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

LOG="$PROJECT_DIR/logs/website-refresh.log"
log() { echo "[$(date -Is)] $*" >>"$LOG"; }

log "refresh starting"
output=$(docker compose exec -T ppg-bot python -m app.web_ingest 2>&1)
status=$?
echo "$output" | grep -vE "onnxruntime|Failed to send telemetry" >>"$LOG"

if [ $status -ne 0 ]; then
    log "refresh FAILED (exit $status) — index left as it was"
    exit $status
fi

# Restart only when the index actually changed. The server holds Chroma
# open from process start, so a restart is what guarantees it serves the
# new content and cites the new date. It also clears in-process
# conversation memory, so it isn't done on the many days nothing changes.
if echo "$output" | grep -q "Already up to date."; then
    log "no changes; bot left running"
else
    docker compose restart ppg-bot >>"$LOG" 2>&1 \
        && log "index updated; bot restarted" \
        || log "index updated but restart FAILED"
fi
