#!/usr/bin/env bash
set -euo pipefail

work_root=/home/leo/.local/share/abc-subtitles-recovery-6
state_root=${work_root}/state
workers_file=${state_root}/translation_workers
request_pid_file=${state_root}/translation_client.pid

workers=${1:-}
if [[ ! "${workers}" =~ ^[0-9]+$ ]] || ((workers < 1 || workers > 36)); then
  printf 'usage: %s WORKERS  # 1..36\n' "$0" >&2
  exit 2
fi

mkdir -p "${state_root}"
temporary=$(mktemp "${state_root}/.translation_workers.XXXXXX")
printf '%s\n' "${workers}" > "${temporary}"
mv -f "${temporary}" "${workers_file}"
printf 'translation workers set to %s\n' "${workers}"

if [[ -s "${request_pid_file}" ]]; then
  read -r request_pid < "${request_pid_file}"
  if [[ "${request_pid}" =~ ^[0-9]+$ ]] \
    && kill -0 "${request_pid}" 2>/dev/null \
    && tr '\0' ' ' < "/proc/${request_pid}/cmdline" \
      | grep -Fq 'subtitle_pipeline.corpus_translation_cli'; then
    kill -TERM "${request_pid}"
    printf 'restarting request client pid=%s; vLLM service remains loaded\n' \
      "${request_pid}"
  fi
fi
