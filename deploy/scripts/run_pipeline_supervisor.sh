#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

source_root=/home/leo/nas/Other/abc
work_root=/home/leo/.local/share/abc-subtitles-recovery-6
pipeline_root=${work_root}/pipeline-src
python=/home/leo/podcast/.venv/bin/python
model_root=${work_root}/models
log_root=${work_root}/logs
state_root=${work_root}/state
expected_audio=579
server_port=8123
vllm_service=${pipeline_root}/deploy/scripts/vllm_service.sh
translation_workers_file=${state_root}/translation_workers
request_pid_file=${state_root}/translation_client.pid
request_pid=

mkdir -p "${log_root}" "${state_root}"
exec >> "${log_root}/pipeline_supervisor.log" 2>&1

log() {
  printf '%s %s\n' "$(date --iso-8601=seconds)" "$*"
}

is_running() {
  pgrep -f -- "$1" >/dev/null
}

audio_count() {
  local files=("${work_root}"/audio/*.flac)
  printf '%s\n' "${#files[@]}"
}

artifact_count() {
  local pattern=$1
  local files=(${pattern})
  printf '%s\n' "${#files[@]}"
}

asr_summary_complete() {
  jq -e --argjson expected "${expected_audio}" '
    .stage == "corpus_asr"
    and .failed == 0
    and .missing_audio == 0
    and (.completed + .existing + .target_exists) == $expected
  ' "${state_root}/corpus_asr_summary.json" >/dev/null 2>&1
}

alignment_summary_complete() {
  jq -e --argjson expected "${expected_audio}" '
    .stage == "corpus_alignment"
    and .failed == 0
    and .missing_audio == 0
    and .missing_asr == 0
    and (.completed + .existing + .target_exists) == $expected
  ' "${state_root}/corpus_alignment_summary.json" >/dev/null 2>&1
}

start_extraction() {
  log "starting resumable extraction"
  setsid -f env PYTHONPATH="${pipeline_root}/src" \
    "${python}" -u -m subtitle_pipeline.runtime_cli extract --workers 4 \
    >> "${log_root}/extract_resume.log" 2>&1
}

start_asr() {
  log "starting incremental FunASR"
  setsid -f env PYTHONPATH="${pipeline_root}/src" \
    "${python}" -u -m subtitle_pipeline.corpus_asr_cli \
    --batch-size 32 --wait-for-audio-seconds 7200 --poll-seconds 10 \
    >> "${log_root}/corpus_asr.log" 2>&1
}

run_with_retry() {
  local label=$1
  shift
  local attempt=1
  while true; do
    log "stage start ${label} attempt=${attempt}"
    if "$@"; then
      log "stage complete ${label} attempt=${attempt}"
      return
    fi
    log "stage retry ${label} after failed attempt=${attempt}"
    attempt=$((attempt + 1))
    sleep 30
  done
}

run_alignment_stage() {
  log "stage start corpus_alignment batch_size=64"
  if env PYTHONPATH="${work_root}/align/site-packages:${pipeline_root}/src" \
    "${python}" -u -m subtitle_pipeline.corpus_alignment_cli \
    --batch-size 64 \
    --model-dir "${model_root}/Qwen3-ForcedAligner-0.6B-hf"; then
    log "stage complete corpus_alignment batch_size=64"
    return
  fi
  log "stage retry corpus_alignment batch_size=32"
  sleep 30
  env PYTHONPATH="${work_root}/align/site-packages:${pipeline_root}/src" \
    "${python}" -u -m subtitle_pipeline.corpus_alignment_cli \
    --batch-size 32 \
    --model-dir "${model_root}/Qwen3-ForcedAligner-0.6B-hf"
  log "stage complete after retry corpus_alignment batch_size=32"
}

wait_for_models() {
  local required=(
    "${model_root}/Qwen3-ForcedAligner-0.6B-hf/.download-complete"
    "${model_root}/Qwen3.5-35B-A3B-FP8/.download-complete"
    "${model_root}/Qwen3-30B-A3B-Instruct-2507-FP8/.download-complete"
    "${model_root}/Qwen3.6-27B-FP8/.download-complete"
  )
  while true; do
    local missing=0
    local sentinel
    for sentinel in "${required[@]}"; do
      [[ -f "${sentinel}" ]] || missing=$((missing + 1))
    done
    ((missing == 0)) && return
    if ! is_running 'download_models.sh'; then
      log "restarting resumable model downloads; missing=${missing}"
      setsid -f bash "${pipeline_root}/deploy/scripts/download_models.sh" \
        >> "${log_root}/model_download.log" 2>&1
    fi
    log "waiting for models; missing=${missing}"
    sleep 60
  done
}

sha1_file() {
  local digest remainder
  read -r digest remainder < <(sha1sum "$1")
  [[ "${digest}" =~ ^[0-9a-f]{40}$ ]]
  printf '%s' "${digest}"
}

write_model_revisions() {
  local vad asr aligner primary fallback reviewer temporary
  vad=$(sha1_file "${model_root}/fsmn-vad/configuration.json")
  asr=$(sha1_file /home/leo/.cache/modelscope/FunAudioLLM/Fun-ASR-Nano-2512/configuration.json)
  aligner=$(sha1_file "${model_root}/Qwen3-ForcedAligner-0.6B-hf/config.json")
  primary=$(sha1_file "${model_root}/Qwen3.5-35B-A3B-FP8/config.json")
  fallback=$(sha1_file "${model_root}/Qwen3-30B-A3B-Instruct-2507-FP8/config.json")
  reviewer=$(sha1_file "${model_root}/Qwen3.6-27B-FP8/config.json")
  temporary=$(mktemp "${state_root}/.model_revisions.XXXXXX")
  printf '{"aligner":"%s","asr":"%s","fallback_translation":"%s","primary_translation":"%s","reviewer_translation":"%s","vad":"%s"}\n' \
    "${aligner}" "${asr}" "${fallback}" "${primary}" "${reviewer}" "${vad}" \
    > "${temporary}"
  mv -f "${temporary}" "${state_root}/model_revisions.json"
  log "stored local model revision evidence"
}

ensure_server() {
  local model_path=$1
  local served_name=$2
  local server_log=$3
  local gpu_memory_utilization=$4
  local max_model_len=$5
  local execution_mode=$6
  "${vllm_service}" ensure "${model_path}" "${served_name}" "${server_log}" \
    "${gpu_memory_utilization}" "${max_model_len}" "${execution_mode}"
}

primary_drafts_complete() {
  local count
  local targets
  count=$(artifact_count "${work_root}/translation/corpus/*/*.draft.translation.json")
  targets=$(jq -r '.target_exists // 0' \
    "${state_root}/corpus_translation_primary_summary.json" 2>/dev/null || printf '0')
  [[ $((count + targets)) -eq "${expected_audio}" ]] \
    && jq -e '.failed == 0 and .missing_alignment == 0 and .missing_draft == 0' \
      "${state_root}/corpus_translation_primary_summary.json" >/dev/null 2>&1
}

read_translation_workers() {
  local workers=16
  if [[ -s "${translation_workers_file}" ]]; then
    read -r workers < "${translation_workers_file}"
  fi
  [[ "${workers}" =~ ^[0-9]+$ ]] && ((workers >= 1 && workers <= 36))
  printf '%s' "${workers}"
}

stop_request_client() {
  [[ -n "${request_pid}" ]] || return 0
  if kill -0 "${request_pid}" 2>/dev/null; then
    log "stopping translation request client pid=${request_pid}; vLLM remains loaded"
    kill -TERM "${request_pid}" 2>/dev/null || true
    wait "${request_pid}" 2>/dev/null || true
  fi
  request_pid=
  rm -f -- "${request_pid_file}"
}

run_translation_with_retry() {
  local pass_name=$1
  local model=$2
  local revision=$3
  local attempt=1
  local workers
  local summary_path
  local run_id
  local expected_failures
  local final_count
  local review_targets
  local checkpoint_path
  local video_id
  local -a video_args=()
  local -a failed_video_ids=()
  local -a checkpoint_paths=()
  local -a checkpoint_video_ids=()
  local -a review_parallel_args=()
  summary_path="${state_root}/corpus_translation_${pass_name}_summary.json"
  if [[ "${pass_name}" == "review" ]]; then
    review_parallel_args=(--parallel-review-blocks)
  fi
  if jq -e '
    .failed > 0 and .missing_alignment == 0 and .missing_draft == 0
  ' "${summary_path}" >/dev/null 2>&1; then
    run_id=$(jq -er '.run_id' "${summary_path}")
    expected_failures=$(jq -er '.failed' "${summary_path}")
    mapfile -t failed_video_ids < <(
      jq -r --arg run_id "${run_id}" \
        'select(.run_id == $run_id and .event == "video_failed_exception")
         | .video_id' "${state_root}/runtime_progress.jsonl" | sort -u
    )
    if ((${#failed_video_ids[@]} == expected_failures)); then
      for video_id in "${failed_video_ids[@]}"; do
        video_args+=(--video-id "${video_id}")
      done
      log "resuming targeted corpus_translation_${pass_name} videos=${#failed_video_ids[@]}"
    fi
  fi
  if [[ "${pass_name}" == "review" ]] && ((${#video_args[@]} == 0)); then
    mapfile -t checkpoint_paths < <(
      find "${work_root}/translation/corpus" -mindepth 2 -maxdepth 2 \
        -type f -name '*.review.checkpoint.json' -print | sort
    )
    final_count=$(artifact_count "${work_root}/translation/corpus/*/*.zh.srt")
    for checkpoint_path in "${checkpoint_paths[@]}"; do
      video_id=${checkpoint_path%/*}
      video_id=${video_id##*/}
      if [[ ! "${video_id}" =~ ^[0-9a-f]{64}$ ]] \
        || [[ -e "${work_root}/translation/corpus/${video_id}/${video_id}.zh.srt" ]]; then
        checkpoint_video_ids=()
        break
      fi
      checkpoint_video_ids+=("${video_id}")
    done
    review_targets=$(jq -r '.target_exists // 0' "${summary_path}" 2>/dev/null || printf '0')
    if ((${#checkpoint_video_ids[@]} > 0)) \
      && ((final_count + ${#checkpoint_video_ids[@]} + review_targets == expected_audio)); then
      for video_id in "${checkpoint_video_ids[@]}"; do
        video_args+=(--video-id "${video_id}")
      done
      log "resuming review checkpoint tail videos=${#checkpoint_video_ids[@]} finals=${final_count}"
    fi
  fi
  while true; do
    workers=$(read_translation_workers)
    log "stage start corpus_translation_${pass_name} attempt=${attempt} workers=${workers}"
    env PYTHONPATH="${pipeline_root}/src" "${python}" -u \
      -m subtitle_pipeline.corpus_translation_cli --pass "${pass_name}" \
      --endpoint "http://127.0.0.1:${server_port}" \
      --model "${model}" --revision "${revision}" \
      --workers "${workers}" --commit-size 20 \
      --max-tokens 4096 --timeout 600 \
      "${review_parallel_args[@]}" "${video_args[@]}" &
    request_pid=$!
    printf '%s\n' "${request_pid}" > "${request_pid_file}"
    if wait "${request_pid}"; then
      request_pid=
      rm -f -- "${request_pid_file}"
      log "stage complete corpus_translation_${pass_name} attempt=${attempt} workers=${workers}"
      return
    fi
    request_pid=
    rm -f -- "${request_pid_file}"
    log "stage retry corpus_translation_${pass_name} after failed attempt=${attempt}; vLLM remains loaded"
    video_args=()
    failed_video_ids=()
    if jq -e '
      .failed > 0 and .missing_alignment == 0 and .missing_draft == 0
    ' "${summary_path}" >/dev/null 2>&1; then
      run_id=$(jq -er '.run_id' "${summary_path}")
      expected_failures=$(jq -er '.failed' "${summary_path}")
      mapfile -t failed_video_ids < <(
        jq -r --arg run_id "${run_id}" \
          'select(.run_id == $run_id and .event == "video_failed_exception")
           | .video_id' "${state_root}/runtime_progress.jsonl" | sort -u
      )
      if ((${#failed_video_ids[@]} == expected_failures)); then
        for video_id in "${failed_video_ids[@]}"; do
          video_args+=(--video-id "${video_id}")
        done
        log "targeted retry corpus_translation_${pass_name} videos=${#failed_video_ids[@]}"
      fi
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
}

verify_targets() {
  local relative
  local count=0
  while IFS= read -r -d '' relative; do
    if [[ ! -s "${source_root}/${relative}" ]]; then
      log "missing published subtitle ${relative}"
      return 1
    fi
    count=$((count + 1))
  done < <(
    jq -j '.records[] | select(.audio_stream_index != null) | .target_relative_path, "\u0000"' \
      "${state_root}/runtime_inventory.json"
  )
  [[ "${count}" -eq "${expected_audio}" ]]
  printf '%s' "${count}"
}

cleanup_owned_temporaries() {
  local name
  local target
  for name in audio segments asr align translate translation final scratch models; do
    target="${work_root}/${name}"
    [[ "${target}" == "${work_root}/"* ]]
    if [[ -e "${target}" ]]; then
      log "cleaning owned temporary ${target}"
      rm -rf --one-file-system -- "${target}"
    fi
  done
  if [[ -d /tmp/abc-subtitle-dev ]]; then
    rm -rf --one-file-system -- /tmp/abc-subtitle-dev
  fi
}

trap stop_request_client EXIT
trap 'exit 130' INT TERM

log "pipeline supervisor started"
while true; do
  completed=$(audio_count)
  ((completed >= expected_audio)) && break
  is_running 'subtitle_pipeline.runtime_cli extract' || start_extraction
  is_running 'subtitle_pipeline.corpus_asr_cli' || start_asr
  log "pipeline progress audio=${completed}/${expected_audio}"
  sleep 60
done

log "all audio artifacts are visible"
while is_running 'subtitle_pipeline.runtime_cli extract'; do
  sleep 30
done
while is_running 'subtitle_pipeline.corpus_asr_cli'; do
  sleep 30
done

asr_targets=$(jq -r '.target_exists // 0' \
  "${state_root}/corpus_asr_summary.json" 2>/dev/null || printf '0')
asr_count=$(artifact_count "${work_root}/asr/corpus/*/asr.json")
if [[ $((asr_count + asr_targets)) -eq "${expected_audio}" ]] && asr_summary_complete; then
  log "reusing verified ASR stage ${asr_count}+${asr_targets}/${expected_audio}"
else
  run_with_retry corpus_asr_final \
    env PYTHONPATH="${pipeline_root}/src" "${python}" -u \
    -m subtitle_pipeline.corpus_asr_cli --batch-size 32
  asr_count=$(artifact_count "${work_root}/asr/corpus/*/asr.json")
  asr_targets=$(jq -r '.target_exists // 0' \
    "${state_root}/corpus_asr_summary.json" 2>/dev/null || printf '0')
fi
[[ $((asr_count + asr_targets)) -eq "${expected_audio}" ]]
log "verified ASR artifacts ${asr_count}+${asr_targets}/${expected_audio}"

wait_for_models
write_model_revisions
alignment_targets=$(jq -r '.target_exists // 0' \
  "${state_root}/corpus_alignment_summary.json" 2>/dev/null || printf '0')
alignment_count=$(artifact_count "${work_root}/asr/corpus/*/alignment.json")
if [[ $((alignment_count + alignment_targets)) -eq "${expected_audio}" ]] \
  && alignment_summary_complete; then
  log "reusing verified alignment stage ${alignment_count}+${alignment_targets}/${expected_audio}"
else
  run_alignment_stage
  alignment_count=$(artifact_count "${work_root}/asr/corpus/*/alignment.json")
  alignment_targets=$(jq -r '.target_exists // 0' \
    "${state_root}/corpus_alignment_summary.json" 2>/dev/null || printf '0')
fi
[[ $((alignment_count + alignment_targets)) -eq "${expected_audio}" ]]
log "verified alignment artifacts ${alignment_count}+${alignment_targets}/${expected_audio}"

primary_model=Qwen/Qwen3.5-35B-A3B-FP8
primary_path="${model_root}/Qwen3.5-35B-A3B-FP8"
primary_revision=$(sha1_file "${primary_path}/config.json")
if primary_drafts_complete; then
  log "reusing verified primary drafts ${expected_audio}/${expected_audio}"
else
  ensure_server "${primary_path}" "${primary_model}" \
    "${log_root}/vllm_primary.log" 0.92 147456 graph
  run_translation_with_retry primary "${primary_model}" "${primary_revision}"
fi

review_model=Qwen/Qwen3.6-27B-FP8
review_path="${model_root}/Qwen3.6-27B-FP8"
review_revision=$(sha1_file "${review_path}/config.json")
ensure_server "${review_path}" "${review_model}" \
  "${log_root}/vllm_review.log" 0.90 32768 graph
run_translation_with_retry review "${review_model}" "${review_revision}"

# Translation is fully finished; publishing and auditing do not use the GPU model.
run_with_retry protected_target_snapshot \
  env PYTHONPATH="${pipeline_root}/src" "${python}" -u \
  -m subtitle_pipeline.corpus_audit_cli --mode snapshot-protected

run_with_retry corpus_publish \
  env PYTHONPATH="${pipeline_root}/src" "${python}" -u \
  -m subtitle_pipeline.corpus_publish_cli \
  --review-model "${review_model}" --review-revision "${review_revision}" \
  --workers 1

run_with_retry corpus_publish_audit \
  env PYTHONPATH="${pipeline_root}/src" "${python}" -u \
  -m subtitle_pipeline.corpus_audit_cli --mode pre-cleanup \
  --review-model "${review_model}" --review-revision "${review_revision}"
published_count=$(jq -er 'select(.status == "verified") | .target_count' \
  "${state_root}/corpus_publish_audit.json")
[[ "${published_count}" -eq "${expected_audio}" ]]
log "audited published subtitles ${published_count}/${expected_audio}"

cleanup_owned_temporaries
run_with_retry corpus_post_cleanup_audit \
  env PYTHONPATH="${pipeline_root}/src" "${python}" -u \
  -m subtitle_pipeline.corpus_audit_cli --mode post-cleanup
published_count=$(jq -er \
  'select(.status == "verified_after_cleanup") | .published_targets' \
  "${state_root}/final_verification.json")
[[ "${published_count}" -eq "${expected_audio}" ]]
"${vllm_service}" stop
printf 'verified %s subtitles after cleanup\n' "${published_count}" \
  > "${state_root}/pipeline_complete"
log "pipeline complete; published=${published_count}; cleanup verified"
