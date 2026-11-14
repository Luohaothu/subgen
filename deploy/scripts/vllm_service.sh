#!/usr/bin/env bash
set -euo pipefail

work_root=/home/leo/.local/share/abc-subtitles-recovery-6
vllm=/home/leo/podcast/.venv/bin/vllm
state_root=${work_root}/state
log_root=${work_root}/logs
server_port=8123
unit_name=abc-subtitle-vllm
unit=${unit_name}.service
lock_file=${state_root}/vllm_service.lock
state_file=${state_root}/vllm_service.json

mkdir -p "${state_root}" "${log_root}"
exec 9>"${lock_file}"
flock 9

log() {
  printf '%s %s\n' "$(date --iso-8601=seconds)" "$*"
}

curl_local() {
  env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    NO_PROXY=127.0.0.1,localhost curl "$@"
}

healthy() {
  curl_local -fsS "http://127.0.0.1:${server_port}/health" >/dev/null 2>&1
}

served_model() {
  curl_local -fsS "http://127.0.0.1:${server_port}/v1/models" 2>/dev/null \
    | jq -er '.data[0].id'
}

active_state() {
  systemctl --user show "${unit}" --property=ActiveState --value 2>/dev/null \
    || printf 'inactive\n'
}

listener_pid() {
  fuser -n tcp "${server_port}" 2>/dev/null | xargs 2>/dev/null || true
}

wait_for_gpu_free() {
  local used
  local attempt
  for attempt in $(seq 1 120); do
    used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits \
      2>/dev/null | tr -d ' ' || true)
    if [[ "${used}" =~ ^[0-9]+$ ]] && ((used < 2048)); then
      return
    fi
    sleep 5
  done
  log "GPU did not become free in time; used_mib=${used:-unknown}"
  return 1
}

stop_service() {
  if [[ "$(active_state)" != inactive ]]; then
    log "stopping independent vLLM service unit=${unit}"
    systemctl --user stop "${unit}"
  elif healthy; then
    local pid
    local pgid
    local attempt
    pid=$(listener_pid)
    if [[ "${pid}" =~ ^[0-9]+$ ]]; then
      pgid=$(ps -o pgid= -p "${pid}" | tr -d ' ')
      log "stopping adopted detached vLLM service pid=${pid} pgid=${pgid}"
      kill -TERM -- "-${pgid}" 2>/dev/null || true
      for attempt in $(seq 1 90); do
        healthy || break
        sleep 2
      done
      if healthy; then
        kill -KILL -- "-${pgid}" 2>/dev/null || true
      fi
    fi
  fi
  systemctl --user reset-failed "${unit}" >/dev/null 2>&1 || true
  rm -f -- "${state_file}"
}

write_state() {
  local model=$1
  local model_path=$2
  local server_log=$3
  local gpu_memory_utilization=$4
  local max_model_len=$5
  local execution_mode=$6
  local temporary
  local managed=false
  [[ "$(active_state)" == active ]] && managed=true
  temporary=$(mktemp "${state_root}/.vllm_service.XXXXXX")
  jq -n \
    --arg model "${model}" \
    --arg model_path "${model_path}" \
    --arg unit "${unit}" \
    --arg log "${server_log}" \
    --argjson gpu_memory_utilization "${gpu_memory_utilization}" \
    --argjson max_model_len "${max_model_len}" \
    --arg execution_mode "${execution_mode}" \
    --arg started_at "$(date --iso-8601=seconds)" \
    --argjson managed_by_systemd "${managed}" \
    '{model:$model,model_path:$model_path,unit:$unit,log:$log,
      gpu_memory_utilization:$gpu_memory_utilization,
      max_model_len:$max_model_len,execution_mode:$execution_mode,
      started_at:$started_at,managed_by_systemd:$managed_by_systemd}' \
    > "${temporary}"
  mv -f "${temporary}" "${state_file}"
}

ensure_service() {
  local model_path=$1
  local model=$2
  local server_log=$3
  local gpu_memory_utilization=$4
  local max_model_len=$5
  local execution_mode=$6
  local -a execution_args=()
  if [[ "${execution_mode}" == eager ]]; then
    execution_args+=(--enforce-eager)
  else
    # Keep a fixed graph-captured service ceiling at the measured safe boundary
    # so lower request-side tuning does not reload this dense 27B model.
    # Capturing the vLLM default of 512 sequences wastes several GiB and can OOM.
    # vLLM 0.24 auto-generates small capture sizes in steps of eight and would
    # round a max of 36 down to 32.  Include 36 explicitly so requests 33..36
    # remain graph-captured instead of falling back to non-graph execution.
    execution_args+=(
      --max-num-seqs 36
      --cudagraph-capture-sizes 1 2 4 8 16 24 32 36
    )
  fi
  local current=
  current=$(served_model 2>/dev/null || true)
  if healthy && [[ "${current}" == "${model}" ]]; then
    if [[ "$(active_state)" == active ]]; then
      log "reusing systemd-managed vLLM service model=${model} unit=${unit}"
    else
      log "adopting detached vLLM service model=${model}; no model reload"
    fi
    write_state "${model}" "${model_path}" "${server_log}" \
      "${gpu_memory_utilization}" "${max_model_len}" "${execution_mode}"
    return
  fi

  if [[ -n "${current}" && "${current}" != "${model}" ]]; then
    log "switching independent vLLM service from=${current} to=${model}"
  fi
  stop_service
  wait_for_gpu_free
  : >> "${server_log}"
  log "starting independent vLLM service model=${model} unit=${unit}"
  systemd-run --user --unit="${unit_name}" --service-type=exec \
    --property=Restart=on-failure \
    --property=RestartSec=5 \
    --property=StartLimitIntervalSec=300 \
    --property=StartLimitBurst=2 \
    --property=TimeoutStopSec=180 \
    --property=KillMode=control-group \
    --property="StandardOutput=append:${server_log}" \
    --property="StandardError=append:${server_log}" \
    "${vllm}" serve "${model_path}" \
    --served-model-name "${model}" \
    --host 127.0.0.1 --port "${server_port}" \
    --language-model-only --max-model-len "${max_model_len}" \
    --gpu-memory-utilization "${gpu_memory_utilization}" \
    "${execution_args[@]}" >/dev/null

  local attempt
  for attempt in $(seq 1 180); do
    current=$(served_model 2>/dev/null || true)
    if healthy && [[ "${current}" == "${model}" ]]; then
      write_state "${model}" "${model_path}" "${server_log}" \
        "${gpu_memory_utilization}" "${max_model_len}" "${execution_mode}"
      log "independent vLLM service ready model=${model} unit=${unit}"
      return
    fi
    if [[ "$(active_state)" == failed ]]; then
      log "independent vLLM service failed before readiness model=${model}"
      journalctl --user-unit="${unit}" -n 100 --no-pager || true
      return 1
    fi
    sleep 10
  done
  log "independent vLLM service readiness timeout model=${model}"
  return 1
}

case "${1:-}" in
  ensure)
    [[ $# -ge 4 && $# -le 7 ]] || {
      printf 'usage: %s ensure MODEL_PATH MODEL_NAME LOG_PATH [GPU_UTIL [MAX_LEN [MODE]]]\n' "$0" >&2
      exit 2
    }
    gpu_memory_utilization=${5:-0.92}
    max_model_len=${6:-147456}
    execution_mode=${7:-graph}
    [[ "${gpu_memory_utilization}" =~ ^0\.[0-9]+$ ]]
    [[ "${max_model_len}" =~ ^[0-9]+$ ]] && ((max_model_len >= 4096))
    [[ "${execution_mode}" == graph || "${execution_mode}" == eager ]]
    ensure_service "$2" "$3" "$4" "${gpu_memory_utilization}" \
      "${max_model_len}" "${execution_mode}"
    ;;
  status)
    model=$(served_model 2>/dev/null || true)
    printf 'unit=%s active=%s healthy=%s model=%s\n' \
      "${unit}" "$(active_state)" "$(healthy && printf yes || printf no)" \
      "${model:-none}"
    ;;
  stop)
    stop_service
    ;;
  *)
    printf 'usage: %s {ensure MODEL_PATH MODEL_NAME LOG_PATH [GPU_UTIL [MAX_LEN [MODE]]]|status|stop}\n' "$0" >&2
    exit 2
    ;;
esac
