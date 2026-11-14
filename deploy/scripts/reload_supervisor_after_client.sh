#!/usr/bin/env bash
set -euo pipefail

client_pid=${1:-}
supervisor_pid=${2:-}
if [[ ! "${client_pid}" =~ ^[0-9]+$ || ! "${supervisor_pid}" =~ ^[0-9]+$ ]]; then
  printf 'usage: %s CLIENT_PID SUPERVISOR_PID\n' "$0" >&2
  exit 2
fi

while kill -0 "${client_pid}" 2>/dev/null \
  && [[ "$(awk '{print $3}' "/proc/${client_pid}/stat" 2>/dev/null || true)" != Z ]]; do
  sleep 5
done

if kill -0 "${supervisor_pid}" 2>/dev/null \
  && tr '\0' ' ' < "/proc/${supervisor_pid}/cmdline" \
    | grep -Fq 'run_pipeline_supervisor.sh'; then
  kill -KILL "${supervisor_pid}"
fi
