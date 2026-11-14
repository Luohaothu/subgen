#!/usr/bin/env bash
set -euo pipefail

modelscope=/home/leo/podcast/.venv/bin/modelscope
model_root=/home/leo/.local/share/abc-subtitles-recovery-6/models

download_model() {
  local repo_id=$1
  local target=$2
  local sentinel="${target}/.download-complete"

  if [[ -f "${sentinel}" ]]; then
    printf 'MODEL already complete %s\n' "${repo_id}"
    return
  fi

  printf 'MODEL download start %s -> %s\n' "${repo_id}" "${target}"
  "${modelscope}" download "${repo_id}" --local-dir "${target}" --max-workers 4
  [[ -f "${target}/config.json" || -f "${target}/configuration.json" ]]
  touch "${sentinel}"
  printf 'MODEL download complete %s\n' "${repo_id}"
}

download_model Qwen/Qwen3-ForcedAligner-0.6B \
  "${model_root}/Qwen3-ForcedAligner-0.6B-hf"
download_model Qwen/Qwen3.5-35B-A3B-FP8 \
  "${model_root}/Qwen3.5-35B-A3B-FP8"
download_model Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 \
  "${model_root}/Qwen3-30B-A3B-Instruct-2507-FP8"
download_model Qwen/Qwen3.6-27B-FP8 \
  "${model_root}/Qwen3.6-27B-FP8"
