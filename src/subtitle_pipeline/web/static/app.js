// No build step and no external assets, so the console works offline.
// Code identifiers stay English; operator-facing strings stay Chinese.

(function () {
  "use strict";


  var STAGES = [
    { name: "inventory", num: "01", cn: "清点" },
    { name: "extract", num: "02", cn: "抽取" },
    { name: "asr", num: "03", cn: "转写" },
    { name: "alignment", num: "04", cn: "对齐" },
    { name: "translation_primary", num: "05", cn: "主翻译" },
    { name: "translation_review", num: "06", cn: "复核" },
    { name: "publish", num: "07", cn: "发布" }
  ];

  var STAGE_ALIASES = {
    inventory: ["inventory", "runtime_inventory"],
    extract: ["extract", "runtime_extract"],
    asr: ["asr", "corpus_asr"],
    alignment: ["alignment", "corpus_alignment", "align"],
    translation_primary: ["translation_primary", "corpus_translation_primary", "primary"],
    translation_review: ["translation_review", "corpus_translation_review", "review"],
    publish: ["publish", "corpus_publish"]
  };

  var STATE_CN = {
    queued: "排队中",
    running: "运行中",
    succeeded: "成功",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断",
    dry_run: "试运行"
  };

  var STAGE_STATUS_CN = {
    pending: "待运行",
    running: "运行中",
    succeeded: "成功",
    failed: "失败",
    cancelled: "已取消",
    skipped: "跳过"
  };

  var SOURCE_CN = {
    env: "环境变量",
    file: "文件",
    derived: "派生",
    default: "默认"
  };

  var REASON_CN = {
    cancelled: "操作员取消",
    restart: "服务重启",
    server_shutdown: "服务停止",
    interrupted: "中断"
  };

  function reasonCn(reason) {
    if (!reason) return "";
    return REASON_CN[reason] || String(reason);
  }

  var SECTION_CN = { paths: "路径", runtime: "运行时", translation: "翻译" };
  var SECTION_DESC = {
    paths: "语料、工作目录与模型位置",
    runtime: "设备与 ASR 引擎",
    translation: "OpenAI 兼容服务与模型版本"
  };

  var EVENT_CN = {
    started: "开始",
    discovered: "发现视频",
    observed: "探测完成",
    completed: "完成",
    failed: "失败",
    batch_started: "批次开始",
    batch_completed: "批次完成",
    batch_failed: "批次失败",
    batch_deferred: "批次延后",
    batch_incomplete: "批次未完成",
    video_started: "视频开始",
    video_completed: "视频完成",
    video_existing: "视频已存在",
    video_failed_exception: "视频异常",
    video_target_collision: "目标冲突",
    skipped_verified: "校验通过跳过",
    skipped_existing: "已存在跳过",
    skipped_no_audio: "无音轨跳过",
    deferred_missing_audio: "延后 · 缺音频",
    deferred_missing_source: "延后 · 缺源文件",
    video_missing_audio: "缺失 · 音频",
    video_missing_asr: "缺失 · 转写",
    video_missing_alignment: "缺失 · 对齐",
    video_missing_draft: "缺失 · 草稿",
    video_missing_candidate: "缺失 · 候选",
    video_invalid_candidate: "无效 · 候选",
    model_download_started: "下载模型",
    model_download_completed: "模型就绪"
  };

  var COUNTER_CN = {
    count: "条目",
    discovered: "发现",
    observed: "探测",
    selected: "选中",
    completed: "完成",
    existing: "已存在",
    no_audio: "无音轨",
    target_exists: "已发布",
    failed: "失败",
    missing_audio: "缺音频",
    missing_asr: "缺转写",
    missing_alignment: "缺对齐",
    missing_draft: "缺草稿",
    missing_candidate: "缺候选",
    invalid_candidate: "无效候选",
    target_collision: "目标冲突",
    skipped_existing: "已跳过",
    skipped_no_audio: "无音轨跳过",
    skipped_verified: "校验跳过",
    deferred: "延后"
  };

  var COUNTER_ORDER = [
    "count", "completed", "failed", "selected", "observed", "discovered",
    "existing", "no_audio", "target_exists", "missing_audio", "missing_asr",
    "missing_alignment", "missing_draft", "missing_candidate",
    "invalid_candidate", "target_collision", "skipped_existing",
    "skipped_no_audio", "skipped_verified", "deferred"
  ];

  var ERROR_COPY = {
    job_running: {
      title: "已有作业在运行",
      hint: "同一工作目录一次只能运行一个作业。等待当前作业结束，或在作业详情中取消它。"
    },
    external_activity: {
      title: "检测到外部运行",
      hint: "进度日志在最近 120 秒内有写入，但没有 Web 作业在运行。可能有人在终端直接运行了 subgen；同时运行会让两个进程争用汇总文件与临时目录。"
    },
    settings_invalid: {
      title: "参数设置无效",
      hint: "subgen.toml 无法加载，服务拒绝了这次请求。请到<a href=\"#/settings\">参数设置</a>修复后重试。"
    },
    settings_changed: {
      title: "配置文件已被修改",
      hint: "保存期间 subgen.toml 被其他进程改动。已重新载入最新文件，请确认内容后再次保存。"
    },
    cannot_rewrite: {
      title: "无法安全重写配置文件",
      hint: "当前文件布局无法在保留注释的前提下重写。可以强制写入规范 TOML，原文件会先备份为 subgen.toml.bak。"
    },
    restart_required: {
      title: "需要重启服务",
      hint: "work_root 或 source_root 已改变。重启 <span class=\"mono\">subgen web</span> 后新配置才会生效。"
    },
    invalid_params: {
      title: "作业参数无效",
      hint: "检查限流数量、视频 ID 与批大小后重试。"
    },
    job_not_finished: {
      title: "作业尚未结束",
      hint: "只有已结束的作业可以删除。运行中的作业请先取消。"
    },
    not_found: {
      title: "找不到目标",
      hint: "该作业可能已被删除，或服务的工作目录已经改变。"
    },
    unknown_stage: {
      title: "未知阶段",
      hint: "阶段映射与当前流水线版本不匹配，请升级 subgen。"
    },
    settings_load_error: {
      title: "配置文件无法加载",
      hint: "普通保存已禁用。修复会用表单内容重写整个文件。"
    },
    settings_conflict: {
      title: "配置文件已被修改",
      hint: "请确认最新文件内容后再次保存。"
    },
    draft_invalid: {
      title: "参数设置校验未通过",
      hint: "服务端拒绝了这份草稿。修正标红的字段后重试；预览需要一次通过的校验。"
    },
    internal_error: {
      title: "服务内部错误",
      hint: "subgen web 返回了未预期的错误。诊断信息可用于排查。"
    },
    offline: {
      title: "无法连接本地服务",
      hint: "subgen web 可能已停止。页面会每 5 秒自动重试。"
    }
  };

  var FIELD_SPECS = [
    {
      section: "paths", key: "source_root", label: "视频语料目录", kind: "path",
      env: "SUBGEN_SOURCE_ROOT",
      help: "只读视频语料根目录。必须是绝对路径，且存在、不是符号链接。"
    },
    {
      section: "paths", key: "work_root", label: "工作目录", kind: "path",
      env: "SUBGEN_WORK_ROOT",
      help: "流水线状态、模型、日志都写在这里。必须是绝对路径且存在。"
    },
    {
      section: "paths", key: "model_root", label: "模型目录", kind: "path",
      env: "SUBGEN_MODEL_ROOT",
      help: "本地权重目录。留空时由工作目录派生。"
    },
    {
      section: "paths", key: "asr_model", label: "ASR 模型目录", kind: "path",
      env: "SUBGEN_ASR_MODEL",
      help: "ASR 权重目录。留空时由模型目录派生。"
    },
    {
      section: "runtime", key: "device", label: "运行设备", kind: "enum",
      env: "SUBGEN_DEVICE", choices: ["auto", "cpu", "cuda", "mps"],
      help: "auto 依次选择 CUDA、MPS、CPU。显式值覆盖自动选择。"
    },
    {
      section: "runtime", key: "asr_engine", label: "ASR 引擎", kind: "enum",
      env: "SUBGEN_ASR_ENGINE", choices: ["auto", "vllm", "funasr"],
      help: "auto 在 CUDA 上选 vLLM，其余用 FunASR。"
    },
    {
      section: "translation", key: "endpoint", label: "翻译服务地址", kind: "url",
      env: "SUBGEN_TRANSLATION_ENDPOINT",
      help: "OpenAI 兼容服务地址，必须以 http:// 或 https:// 开头。"
    },
    {
      section: "translation", key: "model", label: "主翻译模型", kind: "text",
      env: "SUBGEN_TRANSLATION_MODEL",
      help: "主翻译模型的 provider 模型 ID。"
    },
    {
      section: "translation", key: "revision", label: "主翻译版本", kind: "text",
      env: "SUBGEN_TRANSLATION_REVISION",
      help: "主翻译模型版本，来自 provider 配置。"
    },
    {
      section: "translation", key: "api_key_env", label: "API key 变量名", kind: "text",
      env: "SUBGEN_API_KEY_ENV",
      help: "存放 API key 的环境变量名。控制台只显示是否配置，不读取 key 本身。"
    },
    {
      section: "translation", key: "review_model", label: "复核模型", kind: "text",
      env: "SUBGEN_TRANSLATION_REVIEW_MODEL",
      help: "留空时沿用主翻译模型。"
    },
    {
      section: "translation", key: "review_revision", label: "复核版本", kind: "text",
      env: "SUBGEN_TRANSLATION_REVIEW_REVISION",
      help: "留空时沿用主翻译版本。"
    }
  ];

  var MAX_EVENTS = 60;
  var MAX_LOG = 240000;
  var META_INTERVAL = 5000;
  var DETAIL_INTERVAL = 1000;


  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  var ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/[&<>"']/g, function (ch) { return ESC_MAP[ch]; });
  }

  function pad2(n) { return String(n).padStart(2, "0"); }

  function toEpoch(value) {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (typeof value === "string") {
      var parsed = Date.parse(value);
      if (Number.isFinite(parsed)) return parsed / 1000;
      var numeric = Number(value);
      if (Number.isFinite(numeric)) return numeric;
    }
    return null;
  }

  function fmtTime(value) {
    var sec = toEpoch(value);
    if (sec === null) return "—";
    var d = new Date(sec * 1000);
    if (isNaN(d.getTime())) return "—";
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) +
      " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  function fmtClock(value) {
    var sec = toEpoch(value);
    if (sec === null) return "—";
    var d = new Date(sec * 1000);
    if (isNaN(d.getTime())) return "—";
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  function fmtDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    var total = Math.floor(seconds);
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    if (h > 0) return h + ":" + pad2(m) + ":" + pad2(s);
    return pad2(m) + ":" + pad2(s);
  }

  function fmtNum(n) {
    if (!Number.isFinite(n)) return "—";
    return n.toLocaleString("en-US");
  }

  function num(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function clampPercent(done, total) {
    if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return null;
    return Math.max(0, Math.min(100, (done / total) * 100));
  }

  function shortSha(value) {
    if (typeof value !== "string" || !value) return "—";
    return value.length > 12 ? value.slice(0, 12) + "…" : value;
  }

  function fmtValue(value) {
    if (value === null || value === undefined) return "未设置";
    var text = String(value);
    return text === "" ? "空值" : text;
  }

  function isActiveState(state) {
    return state === "queued" || state === "running";
  }

  function stageCn(name) {
    for (var i = 0; i < STAGES.length; i++) if (STAGES[i].name === name) return STAGES[i].cn;
    return name || "—";
  }

  function stageNum(name) {
    for (var i = 0; i < STAGES.length; i++) if (STAGES[i].name === name) return STAGES[i].num;
    return "--";
  }

  function resolveStageName(name) {
    if (!name || typeof name !== "string") return null;
    var raw = name;
    var keys = Object.keys(STAGE_ALIASES);
    for (var i = 0; i < keys.length; i++) {
      if (STAGE_ALIASES[keys[i]].indexOf(raw) !== -1) return keys[i];
    }
    return null;
  }

  function eventCn(event) {
    if (EVENT_CN[event]) return EVENT_CN[event];
    if (typeof event !== "string") return "事件";
    if (event.indexOf("deferred_") === 0) return "延后 · " + event.slice(9).replace(/_/g, " ");
    if (event.indexOf("video_missing_") === 0) return "缺失 · " + event.slice(14).replace(/_/g, " ");
    if (event.indexOf("video_invalid_") === 0) return "无效 · " + event.slice(14).replace(/_/g, " ");
    if (event.indexOf("skipped_") === 0) return "跳过 · " + event.slice(8).replace(/_/g, " ");
    if (event.indexOf("batch_") === 0) return "批次 · " + event.slice(6).replace(/_/g, " ");
    return event;
  }

  function isFailureEvent(event) {
    if (typeof event !== "string") return false;
    return event.indexOf("_failed") !== -1 ||
      event.indexOf("deferred_") === 0 ||
      event.indexOf("video_missing_") === 0 ||
      event.indexOf("video_invalid_") === 0 ||
      event === "failed" ||
      event === "video_target_collision";
  }

  function specFor(section, key) {
    for (var i = 0; i < FIELD_SPECS.length; i++) {
      if (FIELD_SPECS[i].section === section && FIELD_SPECS[i].key === key) return FIELD_SPECS[i];
    }
    return null;
  }

  function stageEntry(record, name) {
    var stages = record && Array.isArray(record.stages) ? record.stages : [];
    for (var i = 0; i < stages.length; i++) {
      var s = stages[i];
      if (!s || typeof s !== "object") continue;
      if ((s.name || s.stage) === name) return s;
    }
    return null;
  }

  function stageStatus(stage) {
    if (!stage) return "pending";
    return stage.status || stage.state || "pending";
  }

  function stageCounters(stage) {
    if (!stage) return null;
    if (stage.counters && typeof stage.counters === "object") return stage.counters;
    if (stage.progress && stage.progress.counts && typeof stage.progress.counts === "object") {
      return stage.progress.counts;
    }
    return null;
  }

  function stageEvents(stage) {
    if (!stage) return [];
    if (Array.isArray(stage.events)) return stage.events;
    if (stage.progress && Array.isArray(stage.progress.events)) return stage.progress.events;
    return [];
  }

  function isActive(record) {
    return !!record && isActiveState(record.state);
  }

  function currentStageName(record) {
    if (!record) return null;
    var stages = Array.isArray(record.stages) ? record.stages : [];
    var index = Number.isInteger(record.current) ? record.current : -1;
    if (index >= 0 && index < STAGES.length) {
      var named = STAGES[index].name;
      if (stageEntry(record, named)) return named;
    }
    for (var i = 0; i < stages.length; i++) {
      if (stageStatus(stages[i]) === "running") {
        var resolved = resolveStageName(stages[i].name || stages[i].stage);
        if (resolved) return resolved;
      }
    }
    return null;
  }

  function stageIndex(record) {
    var name = currentStageName(record);
    if (!name) return -1;
    for (var i = 0; i < STAGES.length; i++) if (STAGES[i].name === name) return i;
    return -1;
  }

  function recordParams(record) {
    if (!record) return {};
    return record.params || record.plan || record.job_params || {};
  }

  function paramChips(params) {
    var chips = [];
    if (params.limit !== null && params.limit !== undefined) chips.push("限流 " + params.limit);
    if (Array.isArray(params.video_ids) && params.video_ids.length) {
      chips.push("指定 " + params.video_ids.length + " 个视频");
    }
    if (params.skip_inventory) chips.push("跳过清点");
    if (params.skip_extract) chips.push("跳过抽取");
    chips.push("ASR 批 " + (params.asr_batch_size || 32));
    chips.push("对齐批 " + (params.alignment_batch_size || 16));
    return chips;
  }

  function plannedCommandCount(record) {
    var stages = record && Array.isArray(record.stages) ? record.stages : [];
    var planned = stages.filter(function (stage) {
      return stage && Array.isArray(stage.argv) && stage.argv.length;
    }).length;
    if (planned > 0) return planned;
    var params = recordParams(record);
    var count = 7;
    if (params.skip_inventory) count -= 1;
    if (params.skip_extract) count -= 1;
    return count;
  }

  function durationOf(record) {
    var start = toEpoch(record && record.created_at);
    if (start === null) return null;
    var end = toEpoch(record && record.ended_at);
    return (end === null ? Date.now() / 1000 : end) - start;
  }

  function eventKey(event, fallbackStage) {
    var stage = event && (event.stage || fallbackStage) ? String(event.stage || fallbackStage) : "";
    var at = event && event.timestamp !== undefined ? String(event.timestamp) : (event && event.at ? String(event.at) : "");
    var video = event && (event.video_id || event.relative_path) ? String(event.video_id || event.relative_path) : "";
    return stage + "|" + String(event && event.event) + "|" + at + "|" + video;
  }

  function isNearBottom(el, slack) {
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < (slack || 40);
  }


  function ApiError(options) {
    var opts = options || {};
    this.name = "ApiError";
    this.status = opts.status || 0;
    this.code = opts.code || "internal_error";
    this.message = opts.message || "";
    this.detail = opts.detail || "";
    this.fields = opts.fields || [];
    if (Error.captureStackTrace) Error.captureStackTrace(this, ApiError);
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function OfflineError(message) {
    this.name = "OfflineError";
    this.message = message || "无法连接本地服务";
    if (Error.captureStackTrace) Error.captureStackTrace(this, OfflineError);
  }
  OfflineError.prototype = Object.create(Error.prototype);
  OfflineError.prototype.constructor = OfflineError;

  function normalizeFields(fields) {
    var out = [];
    if (!fields) return out;
    if (Array.isArray(fields)) {
      fields.forEach(function (item) {
        if (typeof item === "string") {
          if (item) out.push({ field: "", message: item });
        } else if (item && typeof item === "object") {
          var message = item.message || item.error || item.msg || "";
          if (message) {
            out.push({
              field: String(item.field || item.name || item.key || ""),
              message: String(message)
            });
          }
        }
      });
      return out;
    }
    if (typeof fields === "object") {
      Object.keys(fields).forEach(function (key) {
        var value = fields[key];
        if (typeof value === "string") out.push({ field: key, message: value });
        else if (value) out.push({ field: key, message: JSON.stringify(value) });
      });
    }
    return out;
  }

  function requestJson(method, path, body) {
    var mutating = method !== "GET" && method !== "HEAD";
    var init = {
      method: method,
      cache: "no-store",
      headers: { Accept: "application/json" }
    };
    // The server's same-origin guard requires a JSON content type on every
    // mutating request, including POST and DELETE calls that carry no payload.
    if (mutating) init.headers["Content-Type"] = "application/json";
    if (body !== undefined) init.body = JSON.stringify(body);
    else if (mutating) init.body = "{}";
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, 15000);
    init.signal = controller.signal;

    return fetch(path, init).catch(function () {
      throw new OfflineError("请求失败：连接被拒绝或服务未响应");
    }).then(function (response) {
      if (response.status === 204 || response.status === 205) return null;
      return response.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (err) { data = null; }
        }
        if (!response.ok) {
          var envelope = data && typeof data === "object" ? data.error : null;
          throw new ApiError({
            status: response.status,
            code: envelope && envelope.code ? envelope.code : "http_" + response.status,
            message: (envelope && envelope.message) || "",
            detail: (envelope && envelope.detail) || "",
            fields: normalizeFields(envelope && envelope.fields)
          });
        }
        if (data === null) {
          throw new ApiError({ status: response.status, code: "internal_error", message: "响应不是 JSON" });
        }
        return data;
      });
    }).finally(function () {
      clearTimeout(timer);
    });
  }

  function requestText(path) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, 15000);
    return fetch(path, { cache: "no-store", signal: controller.signal }).catch(function () {
      throw new OfflineError("请求失败：连接被拒绝或服务未响应");
    }).then(function (response) {
      return response.text().then(function (text) {
        if (!response.ok) {
          var envelope = null;
          try { envelope = JSON.parse(text).error; } catch (err) { envelope = null; }
          throw new ApiError({
            status: response.status,
            code: (envelope && envelope.code) || "http_" + response.status,
            message: (envelope && envelope.message) || "",
            detail: (envelope && envelope.detail) || "",
            fields: normalizeFields(envelope && envelope.fields)
          });
        }
        return text;
      });
    }).finally(function () {
      clearTimeout(timer);
    });
  }

  function unwrapJob(data) {
    if (!data || typeof data !== "object") return null;
    if (data.job && typeof data.job === "object") return data.job;
    if (data.job_id) return data;
    if (data.record && typeof data.record === "object") return data.record;
    return null;
  }

  function mergeRecord(base, patch) {
    if (!base) return patch;
    var next = Object.assign({}, base, patch || {});
    var baseStages = Array.isArray(base.stages) ? base.stages : [];
    var patchStages = patch && Array.isArray(patch.stages) ? patch.stages : [];
    var merged = baseStages.map(function (stage) {
      var name = stage.name || stage.stage;
      var update = null;
      for (var i = 0; i < patchStages.length; i++) {
        if ((patchStages[i].name || patchStages[i].stage) === name) { update = patchStages[i]; break; }
      }
      return update ? Object.assign({}, stage, update) : stage;
    });
    patchStages.forEach(function (stage) {
      var name = stage.name || stage.stage;
      var known = merged.some(function (item) { return (item.name || item.stage) === name; });
      if (!known) merged.push(stage);
    });
    next.stages = merged;
    return next;
  }


  var app = {
    route: { name: "jobs", id: null },
    meta: null,
    metaError: null,
    metaOnline: true,
    metaPolledAt: 0,
    metaTimer: null,
    jobs: [],
    jobsLoaded: false,
    jobsLoading: false,
    jobListError: null,
    activeJobId: null,
    detail: null,
    detailTimer: null,
    settings: null,
    settingsLoading: false,
    settingsTimer: null,
    clockTimer: null,
    toastTimer: null,
    pendingCreateToken: null,
    createBusy: false
  };


  function toast(message, kind, code) {
    var el = $("#toast");
    if (!el) return;
    el.innerHTML = esc(message) + (code ? '<span class="toast-code">' + esc(code) + "</span>" : "");
    el.dataset.kind = kind || "info";
    el.dataset.visible = "true";
    clearTimeout(app.toastTimer);
    app.toastTimer = setTimeout(function () {
      el.dataset.visible = "false";
    }, kind === "error" ? 7000 : 4200);
  }

  function announce(message) {
    var el = $("#sr-status");
    if (!el) return;
    el.textContent = "";
    requestAnimationFrame(function () { el.textContent = message; });
  }

  function confirmAction(options) {
    var opts = options || {};
    return new Promise(function (resolve) {
      var dialog = $("#confirm-dialog");
      $("#confirm-title").textContent = opts.title || "确认";
      $("#confirm-body").innerHTML = opts.body || "";
      $("#confirm-code").textContent = opts.code || "";
      var okBtn = $("#confirm-ok");
      okBtn.textContent = opts.confirmLabel || "确认";
      okBtn.className = "btn " + (opts.danger === false ? "btn--primary" : "btn--danger");

      var settled = false;
      function finish(value) {
        if (settled) return;
        settled = true;
        okBtn.removeEventListener("click", onOk);
        $("#confirm-cancel").removeEventListener("click", onCancel);
        dialog.removeEventListener("close", onClose);
        if (dialog.open) dialog.close();
        resolve(value);
      }
      function onOk() { finish(true); }
      function onCancel() { finish(false); }
      function onClose() { finish(false); }

      okBtn.addEventListener("click", onOk);
      $("#confirm-cancel").addEventListener("click", onCancel);
      dialog.addEventListener("close", onClose);
      dialog.showModal();
    });
  }

  function askExternalAck(err) {
    return new Promise(function (resolve) {
      var dialog = $("#external-ack");
      var meta = app.meta || {};
      var activity = meta.external_activity;
      var detailBits = "";
      if (activity && typeof activity === "object") {
        var bits = [];
        if (activity.stage) bits.push("阶段 " + esc(stageCn(resolveStageName(activity.stage) || activity.stage)));
        if (activity.event) bits.push("事件 " + esc(eventCn(String(activity.event))));
        if (Number.isFinite(activity.age_seconds)) bits.push("最近写入 " + Math.round(activity.age_seconds) + " 秒前");
        if (bits.length) detailBits = "<p class=\"mono\">" + bits.join(" · ") + "</p>";
      }
      var diag = "";
      if (err && (err.message || err.detail)) {
        diag = '<details class="error-diag"><summary>诊断信息（英文原文）</summary><pre>' +
          esc([err.message, err.detail].filter(Boolean).join("\n\n")) + "</pre></details>";
      }
      $("#ack-body").innerHTML =
        "<p>进度日志在最近 <strong>120 秒</strong>内有写入，但当前没有 Web 作业在运行。</p>" +
        "<p>可能有人在终端直接运行了 <span class=\"mono\">subgen</span>。两个进程同时运行会争用阶段汇总文件、共享临时目录和发布临时文件。</p>" +
        detailBits +
        "<p>确认后仍要创建作业吗？</p>" + diag;

      var settled = false;
      function finish(value) {
        if (settled) return;
        settled = true;
        $("#ack-confirm").removeEventListener("click", onConfirm);
        $("#ack-cancel").removeEventListener("click", onCancel);
        dialog.removeEventListener("close", onClose);
        if (dialog.open) dialog.close();
        resolve(value);
      }
      function onConfirm() { finish(true); }
      function onCancel() { finish(false); }
      function onClose() { finish(false); }

      $("#ack-confirm").addEventListener("click", onConfirm);
      $("#ack-cancel").addEventListener("click", onCancel);
      dialog.addEventListener("close", onClose);
      dialog.showModal();
    });
  }


  function errorCopyFor(err) {
    if (err && err.code && ERROR_COPY[err.code]) return ERROR_COPY[err.code];
    if (err && err.name === "OfflineError") return ERROR_COPY.offline;
    if (err && err.status >= 500) return ERROR_COPY.internal_error;
    return { title: "请求失败", hint: "服务返回了未预期的错误。" };
  }

  function errorHtml(err, options) {
    var opts = options || {};
    var copy = errorCopyFor(err);
    var status = err && err.status ? (err.code + " · HTTP " + err.status) : (err && err.code ? err.code : "");
    var fields = (err && Array.isArray(err.fields)) ? err.fields : [];
    var diag = [];
    if (err && err.message) diag.push(err.message);
    if (err && err.detail) diag.push(typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail, null, 2));
    var actions = opts.actions || "";

    return '<div class="error-block" role="alert">' +
      '<div class="error-head">' +
      '<span class="error-mark" aria-hidden="true">!</span>' +
      '<span class="error-title">' + esc(copy.title) + "</span>" +
      '<span class="error-code mono">' + esc(status) + "</span>" +
      "</div>" +
      '<p class="error-hint">' + copy.hint + "</p>" +
      (fields.length ? '<div class="error-fields">' + fields.map(function (item) {
        return "<span>" + (item.field ? "<b>" + esc(item.field) + "</b> " : "") + esc(item.message) + "</span>";
      }).join("") + "</div>" : "") +
      (actions ? '<div class="error-actions">' + actions + "</div>" : "") +
      (diag.length ? '<details class="error-diag"><summary>诊断信息（英文原文）</summary><pre>' + esc(diag.join("\n\n")) + "</pre></details>" : "") +
      "</div>";
  }


  function activeJobId(meta) {
    if (!meta || typeof meta !== "object") return null;
    if (typeof meta.active_job_id === "string" && meta.active_job_id) return meta.active_job_id;
    if (meta.active_job && typeof meta.active_job === "object" && meta.active_job.job_id) return meta.active_job.job_id;
    if (typeof meta.active_job === "string" && meta.active_job) return meta.active_job;
    return null;
  }

  function metaSettingsOk(meta) {
    if (!meta) return true;
    if (meta.settings && typeof meta.settings === "object" && typeof meta.settings.settings_ok === "boolean") {
      return meta.settings.settings_ok;
    }
    if (typeof meta.settings_ok === "boolean") return meta.settings_ok;
    if (typeof meta.load_error === "string" && meta.load_error) return false;
    if (typeof meta.settings_load_error === "string" && meta.settings_load_error) return false;
    return true;
  }

  function metaInventoryText(meta) {
    if (!meta) return "—";
    var inventory = meta.inventory !== undefined ? meta.inventory : (meta.inventory_info || meta.inventory_summary);
    if (inventory === null || inventory === undefined) return "—";
    if (typeof inventory === "number") return fmtNum(inventory) + " 条";
    if (typeof inventory === "string") return inventory;
    if (typeof inventory === "object") {
      if (Number.isFinite(inventory.count)) return fmtNum(inventory.count) + " 条";
      if (Number.isFinite(inventory.total)) return fmtNum(inventory.total) + " 条";
      if (inventory.exists === false) return "未发现";
      if (inventory.ok === false || inventory.valid === false) return "不可用";
      if (inventory.exists === true) return "已发现";
    }
    return "—";
  }

  function metaWorkRoot(meta) {
    if (!meta) return "—";
    if (typeof meta.work_root === "string" && meta.work_root) return meta.work_root;
    if (meta.paths && meta.paths.work_root) return meta.paths.work_root;
    if (meta.config && meta.config.work_root) return meta.config.work_root;
    return "—";
  }

  function describeConnectionError(err) {
    if (!err) return "";
    if (err.name === "OfflineError") return err.message;
    var bits = [];
    if (err.code) bits.push(err.code);
    if (err.status) bits.push("HTTP " + err.status);
    if (err.message) bits.push(err.message);
    return bits.join(" · ");
  }

  function setConnection(online, err) {
    var wasOffline = !app.metaOnline;
    app.metaOnline = online;
    var conn = $("#conn");
    var banner = $("#offline-banner");
    if (online) {
      conn.dataset.conn = "online";
      $("#conn-text").textContent = "在线";
      banner.hidden = true;
      if (wasOffline) toast("已重新连接本地服务", "success");
    } else {
      conn.dataset.conn = "offline";
      $("#conn-text").textContent = "离线";
      banner.hidden = false;
      $("#offline-detail").textContent = describeConnectionError(err);
    }
    renderPollState();
  }

  function renderPollState() {
    var el = $("#meta-poll");
    if (!el) return;
    if (document.hidden) {
      el.textContent = "已暂停 · 标签页隐藏";
      return;
    }
    if (!app.metaOnline) {
      el.textContent = "重试中 · 5s";
      return;
    }
    var stamp = app.metaPolledAt ? fmtClock(app.metaPolledAt / 1000) : "—";
    el.textContent = stamp + " · 5s";
  }

  function renderMeta() {
    var meta = app.meta || {};
    var workRoot = metaWorkRoot(meta);
    var workEl = $("#meta-work-root");
    workEl.textContent = workRoot;
    workEl.title = workRoot;

    $("#meta-inventory").textContent = metaInventoryText(meta);

    var activeId = activeJobId(meta);
    var activeEl = $("#meta-active");
    if (activeId) {
      var activeState = meta.active_job && typeof meta.active_job === "object" ? meta.active_job.state : "";
      activeEl.innerHTML = '<a href="#/jobs/' + encodeURIComponent(activeId) + '">' + esc(activeId) + "</a>" +
        (activeState ? " · " + esc(STATE_CN[activeState] || activeState) : "");
    } else {
      activeEl.textContent = "无";
    }

    var settingsEl = $("#meta-settings");
    if (metaSettingsOk(meta)) {
      settingsEl.innerHTML = '<a href="#/settings">正常</a>';
      settingsEl.className = "stat-v";
    } else {
      settingsEl.innerHTML = '<a href="#/settings">无法加载</a>';
      settingsEl.className = "stat-v is-bad";
    }

    $("#conn").dataset.conn = app.metaOnline ? "online" : "offline";
    $("#conn-text").textContent = app.metaOnline ? "在线" : "离线";
    app.activeJobId = activeId;
    renderRunStrip(meta, activeId);
    renderPollState();
    syncCreateLock();
    renderJobListCount();
  }

  function renderRunStrip(meta, activeId) {
    var strip = $("#run-strip");
    if (!strip) return;
    if (!activeId) {
      strip.hidden = true;
      return;
    }
    strip.hidden = false;
    strip.dataset.live = "true";
    var progress = meta.active_job && typeof meta.active_job === "object" ? meta.active_job.progress : null;
    var pct = null;
    if (progress && Number.isFinite(progress.total) && progress.total > 0) {
      pct = clampPercent(num(progress.done) || 0, progress.total);
    }
    strip.querySelector("i").style.setProperty("--p", (pct === null ? 2 : Math.max(pct, 1.5)) + "%");
  }

  function refreshMeta() {
    return requestJson("GET", "/api/meta").then(function (data) {
      app.meta = data || {};
      app.metaError = null;
      app.metaPolledAt = Date.now();
      setConnection(true);
      renderMeta();
      if (app.route.name === "jobs" && app.jobsLoaded) refreshJobs({ quiet: true });
    }).catch(function (err) {
      app.metaError = err;
      app.metaPolledAt = Date.now();
      setConnection(false, err);
      renderPollState();
    });
  }

  function scheduleMeta(delay) {
    clearTimeout(app.metaTimer);
    app.metaTimer = setTimeout(function () {
      if (document.hidden) {
        scheduleMeta(2000);
        return;
      }
      refreshMeta().then(function () {
        scheduleMeta(META_INTERVAL);
      });
    }, delay === undefined ? META_INTERVAL : delay);
  }


  function normalizeJobList(data) {
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.jobs)) return data.jobs;
    return [];
  }

  function jobScopeText(job) {
    var params = job.params || job.plan || {};
    return paramChips(params).join(" · ");
  }

  function jobProgressHtml(job) {
    var state = job.state || "queued";
    if (state === "dry_run") {
      return '<span class="progress-num">试运行 · ' + plannedCommandCount(job) + " 条命令</span>";
    }
    var progress = job.progress && typeof job.progress === "object" ? job.progress : null;
    if (!progress || (!Number.isFinite(progress.done) && !Number.isFinite(progress.total))) {
      return '<span class="progress-num">—</span>';
    }
    var done = Number.isFinite(progress.done) ? progress.done : 0;
    var total = Number.isFinite(progress.total) ? progress.total : null;
    var pct = clampPercent(done, total);
    var approx = progress.exact === false ? "约 " : "";
    var bar = '<span class="progress" data-state="' + esc(state) + '" role="progressbar"' +
      (total !== null ? ' aria-valuemin="0" aria-valuemax="' + total + '" aria-valuenow="' + done + '"' : "") +
      '><i style="--p:' + (pct === null ? 0 : pct) + '%"></i></span>';
    var label = approx + "<b>" + fmtNum(done) + "</b>" + (total !== null ? " / " + fmtNum(total) : "");
    if (progress.failed > 0) {
      label += ' · <span class="fail">失败 ' + fmtNum(progress.failed) + "</span>";
    }
    return bar + '<span class="progress-num">' + label + "</span>";
  }

  function jobRowHtml(job) {
    var id = job.job_id || job.id || "";
    var state = job.state || "queued";
    var stage = job.current_stage || job.current || null;
    var stageName = resolveStageName(stage) || stage;
    var active = isActiveState(state);
    var actions = ['<a class="job-row-open" href="#/jobs/' + encodeURIComponent(id) + '">打开</a>'];
    if (active) {
      actions.push('<button class="row-action" type="button" data-action="cancel" data-job="' + esc(id) + '">取消</button>');
    } else {
      actions.push('<button class="row-action" type="button" data-action="rerun" data-job="' + esc(id) + '">重跑</button>');
      actions.push('<button class="row-action row-action--danger" type="button" data-action="delete" data-job="' + esc(id) + '">删除</button>');
    }

    return '<li class="job-row" data-testid="job-row" data-job-id="' + esc(id) + '" data-state="' + esc(state) + '">' +
      '<div class="job-row-main">' +
      '<div class="job-row-top">' +
      '<span class="chip" data-state="' + esc(state) + '">' + esc(STATE_CN[state] || state) + "</span>" +
      '<a class="job-row-id" href="#/jobs/' + encodeURIComponent(id) + '">' + esc(id) + "</a>" +
      '<span class="job-row-time">' + esc(fmtTime(job.created_at)) + "</span>" +
      "</div>" +
      '<div class="job-row-meta">' +
      '<span class="meta-stage">当前阶段 <b>' + esc(stageName ? stageCn(stageName) : "未开始") + "</b></span>" +
      '<span class="sep">/</span>' +
      "<span>" + esc(jobScopeText(job)) + "</span>" +
      "</div>" +
      '<div class="job-row-progress">' + jobProgressHtml(job) + "</div>" +
      "</div>" +
      '<div class="job-row-actions">' + actions.join("") + "</div>" +
      "</li>";
  }

  function renderJobListCount() {
    var el = $("#job-list-count");
    if (!el) return;
    if (!app.jobsLoaded) {
      el.textContent = app.jobListError ? "载入失败" : "载入中";
      return;
    }
    var n = app.jobs.length;
    el.textContent = n === 0 ? "0 个作业" : n + " 个作业";
  }

  function renderJobList() {
    var list = $("#job-list");
    var empty = $("#job-empty");
    if (!list || !empty) return;
    if (!app.jobs.length) {
      list.innerHTML = "";
      empty.hidden = false;
    } else {
      empty.hidden = true;
      list.innerHTML = app.jobs.map(jobRowHtml).join("");
    }
    renderJobListCount();
  }

  function refreshJobs(options) {
    var opts = options || {};
    if (app.jobsLoading) return Promise.resolve();
    app.jobsLoading = true;
    if (!opts.quiet && !app.jobsLoaded) $("#jobs-loading").hidden = false;

    return requestJson("GET", "/api/jobs").then(function (data) {
      app.jobs = normalizeJobList(data);
      app.jobsLoaded = true;
      if (data && typeof data === "object" && !Array.isArray(data) && data.active_job_id !== undefined) {
        app.activeJobId = data.active_job_id;
      }
      app.jobListError = null;
      $("#jobs-error").hidden = true;
      $("#jobs-error").innerHTML = "";
      renderJobList();
      syncCreateLock();
    }).catch(function (err) {
      app.jobListError = err;
      var box = $("#jobs-error");
      if (err instanceof OfflineError) {
        box.innerHTML = '<div class="notice notice--bad">' +
          '<span class="notice-mark" aria-hidden="true">!</span>' +
          "<div><strong>无法载入作业列表。</strong>本地服务未响应；连接恢复后会自动重试。</div></div>";
      } else {
        box.innerHTML = errorHtml(err);
      }
      box.hidden = false;
      if (!app.jobsLoaded) $("#job-empty").hidden = true;
      renderJobListCount();
      renderPollState();
    }).finally(function () {
      app.jobsLoading = false;
      $("#jobs-loading").hidden = true;
    });
  }

  function findJob(jobId) {
    for (var i = 0; i < app.jobs.length; i++) {
      if (app.jobs[i].job_id === jobId) return app.jobs[i];
    }
    return null;
  }

  async function cancelJob(jobId) {
    var job = findJob(jobId);
    var jobLabel = jobId + (job && job.state ? " · " + (STATE_CN[job.state] || job.state) : "");
    var ok = await confirmAction({
      title: "取消作业",
      code: "cancel · " + jobId,
      confirmLabel: "取消作业",
      body: "<p>将向 <span class=\"mono\">" + esc(jobLabel) + "</span> 的当前阶段发送 SIGINT，让阶段执行 <span class=\"mono\">KeyboardInterrupt</span> 清理。10 秒未退出会升级为 SIGTERM，再 3 秒后 SIGKILL。</p>" +
        "<p>取消后作业状态为「已取消」，后续阶段跳过。</p>"
    });
    if (!ok) return;

    try {
      var data = await requestJson("POST", "/api/jobs/" + encodeURIComponent(jobId) + "/cancel", {});
      var record = unwrapJob(data);
      if (app.detail && app.detail.id === jobId && record) {
        app.detail.record = mergeRecord(app.detail.record, record);
        updateDetailLive();
        scheduleDetail(600);
      }
      toast("已请求取消，等待阶段退出", "info");
      announce("已请求取消作业");
      refreshJobs({ quiet: true });
      refreshMeta();
    } catch (err) {
      showJobActionError(err);
    }
  }

  async function rerunJob(jobId, fromDetail) {
    try {
      var data = await requestJson("POST", "/api/jobs/" + encodeURIComponent(jobId) + "/rerun", {});
      var record = unwrapJob(data);
      toast("已创建重跑作业", "success");
      refreshJobs({ quiet: true });
      if (record && record.job_id) {
        location.hash = "#/jobs/" + encodeURIComponent(record.job_id);
      } else {
        refreshJobs({ quiet: true });
      }
      refreshMeta();
    } catch (err) {
      showJobActionError(err);
    }
  }

  async function deleteJob(jobId) {
    var ok = await confirmAction({
      title: "删除作业",
      code: "delete · " + jobId,
      confirmLabel: "删除作业",
      body: "<p>将删除作业 <span class=\"mono\">" + esc(jobId) + "</span> 的记录与日志。流水线产物不会被删除。</p>" +
        "<p>此操作不可撤销。</p>"
    });
    if (!ok) return;

    try {
      await requestJson("DELETE", "/api/jobs/" + encodeURIComponent(jobId), {});
      toast("已删除作业记录与日志", "success");
      announce("已删除作业");
      if (app.route.name === "detail" && app.route.id === jobId) {
        location.hash = "#/jobs";
      }
      refreshJobs({ quiet: true });
      refreshMeta();
    } catch (err) {
      showJobActionError(err);
    }
  }

  function showJobActionError(err) {
    if (app.detail && app.route.name === "detail") {
      app.detail.actionError = err;
      renderDetailError();
      return;
    }
    var box = $("#jobs-error");
    if (box) {
      box.innerHTML = errorHtml(err);
      box.hidden = false;
    }
    if (err instanceof OfflineError) toast("无法连接本地服务", "error");
  }


  function newClientToken() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.prototype.map.call(bytes, function (b) { return b.toString(16).padStart(2, "0"); }).join("");
  }

  function createFormEls() {
    return {
      form: $("#create-form"),
      limit: $("#create-limit"),
      videoIds: $("#create-video-ids"),
      asrBatch: $("#create-asr-batch"),
      alignBatch: $("#create-align-batch"),
      skipInventory: $("#create-skip-inventory"),
      skipExtract: $("#create-skip-extract"),
      dryRun: $("#create-dry-run"),
      submit: $("#create-submit"),
      error: $("#create-error")
    };
  }

  function readCreateForm() {
    var els = createFormEls();
    var errors = {};
    var params = {
      limit: null,
      video_ids: [],
      skip_inventory: !!els.skipInventory.checked,
      skip_extract: !!els.skipExtract.checked,
      dry_run: !!els.dryRun.checked,
      asr_batch_size: 32,
      alignment_batch_size: 16
    };

    var limitRaw = els.limit.value.trim();
    if (limitRaw !== "") {
      if (!/^\d+$/.test(limitRaw)) errors.limit = "限流数量必须是非负整数。";
      else params.limit = parseInt(limitRaw, 10);
    }

    var rawIds = els.videoIds.value.split(/[\s,]+/).filter(Boolean);
    for (var i = 0; i < rawIds.length; i++) {
      if (!/^[0-9a-f]{64}$/.test(rawIds[i])) {
        errors.video_ids = "第 " + (i + 1) + " 个 ID 不是 64 位小写十六进制。";
        break;
      }
    }
    params.video_ids = rawIds;

    var asrRaw = els.asrBatch.value.trim();
    if (!/^\d+$/.test(asrRaw) || parseInt(asrRaw, 10) < 1) errors.asr_batch_size = "ASR 批大小必须是至少 1 的整数。";
    else params.asr_batch_size = parseInt(asrRaw, 10);

    var alignRaw = els.alignBatch.value.trim();
    if (!/^\d+$/.test(alignRaw) || parseInt(alignRaw, 10) < 1) errors.alignment_batch_size = "对齐批大小必须是至少 1 的整数。";
    else params.alignment_batch_size = parseInt(alignRaw, 10);

    return { params: params, errors: Object.keys(errors).length ? errors : null };
  }

  function clearCreateFeedback() {
    var els = createFormEls();
    els.error.hidden = true;
    els.error.innerHTML = "";
    $$("[data-create-error]").forEach(function (el) {
      el.hidden = true;
      el.textContent = "";
    });
    $$("#create-form input, #create-form textarea").forEach(function (el) {
      el.removeAttribute("aria-invalid");
    });
  }

  function showCreateErrors(errors) {
    var els = createFormEls();
    var first = null;
    Object.keys(errors).forEach(function (field) {
      var holder = $('[data-create-error="' + field + '"]');
      if (holder) {
        holder.textContent = errors[field];
        holder.hidden = false;
      }
      var input = els.form.querySelector('[name="' + field + '"]');
      if (input) {
        input.setAttribute("aria-invalid", "true");
        if (!first) first = input;
      }
    });
    if (first) first.focus();
  }

  function setCreateError(html) {
    var els = createFormEls();
    els.error.innerHTML = html;
    els.error.hidden = false;
    els.error.scrollIntoView({ block: "nearest" });
  }

  function setCreateBusy(busy) {
    var els = createFormEls();
    app.createBusy = busy;
    els.submit.disabled = busy;
    els.submit.textContent = busy ? "创建中…" : "创建作业";
    els.form.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function resetCreateForm() {
    var els = createFormEls();
    els.limit.value = "";
    els.videoIds.value = "";
    ["skipInventory", "skipExtract", "dryRun"].forEach(function (key) {
      els[key].checked = false;
    });
    clearCreateFeedback();
  }

  function setCreateFormDisabled(disabled) {
    var els = createFormEls();
    $$("#create-form input, #create-form textarea").forEach(function (el) {
      el.disabled = disabled;
    });
    els.submit.disabled = disabled || app.createBusy;
  }

  function syncCreateLock() {
    var lock = $("#create-lock");
    var link = $("#create-lock-link");
    var notice = $("#create-settings-notice");
    if (!lock || !link || !notice) return;

    var active = activeJobId(app.meta);
    var settingsOk = metaSettingsOk(app.meta);
    var busy = app.createBusy;

    if (active) {
      lock.hidden = false;
      link.href = "#/jobs/" + encodeURIComponent(active);
      link.textContent = "打开当前作业 →";
    } else {
      lock.hidden = true;
    }

    notice.hidden = settingsOk;
    setCreateFormDisabled(!!active || !settingsOk);
    if (busy) setCreateBusy(true);
  }

  function submitCreate(options) {
    var opts = options || {};
    if (app.createBusy) return;
    clearCreateFeedback();

    var parsed = readCreateForm();
    if (parsed.errors) {
      showCreateErrors(parsed.errors);
      return;
    }
    if (!app.pendingCreateToken) app.pendingCreateToken = newClientToken();

    var payload = {
      params: parsed.params,
      client_token: app.pendingCreateToken
    };
    if (opts.acknowledgeExternal) payload.acknowledge_external = true;

    setCreateBusy(true);

    requestJson("POST", "/api/jobs", payload).then(function (data) {
      var record = unwrapJob(data);
      app.pendingCreateToken = null;
      setCreateBusy(false);
      resetCreateForm();
      var deduplicated = data && data.deduplicated === true;
      toast(deduplicated ? "复用了已创建的作业" : "已创建作业", "success");
      announce("已创建作业");
      refreshMeta();
      refreshJobs({ quiet: true });
      if (record && record.job_id) {
        location.hash = "#/jobs/" + encodeURIComponent(record.job_id);
      } else {
        refreshJobs({ quiet: true });
      }
    }).catch(function (err) {
      setCreateBusy(false);

      if (err instanceof OfflineError) {
        setConnection(false, err);
        setCreateError(errorHtml(err));
        return;
      }

      if (err.code === "external_activity" && !opts.acknowledgeExternal) {
        askExternalAck(err).then(function (accepted) {
          if (accepted) {
            submitCreate({ acknowledgeExternal: true });
          } else {
            app.pendingCreateToken = null;
            toast("已取消创建", "info");
          }
        });
        return;
      }

      if (err.code === "invalid_params" && err.fields.length) {
        var fieldErrors = {};
        err.fields.forEach(function (item) {
          var key = item.field.replace(/^.*\./, "");
          fieldErrors[key] = item.message;
        });
        showCreateErrors(fieldErrors);
      }

      if (err.code === "job_running" || err.code === "settings_invalid" || err.code === "restart_required") {
        refreshMeta();
        refreshJobs({ quiet: true });
      }

      // A rejected create never produced a job; mint a fresh token next time.
      app.pendingCreateToken = null;
      setCreateError(errorHtml(err));
    });
  }


  function detailSkeleton() {
    return '<div class="detail-skeleton" aria-hidden="true">' +
      '<div class="bar"></div><div class="bar bar--tall"></div><div class="bar"></div>' +
      "</div>";
  }

  function renderDetailError() {
    var box = $("#detail-error");
    var d = app.detail;
    if (!box || !d) return;
    var parts = [];
    var record = d.record;
    if (record && record.state === "failed") parts.push(failureHtml(record));
    else if (record && record.state === "interrupted") parts.push(interruptedHtml(record));
    else if (record && record.state === "cancelled") parts.push(cancelledHtml(record));
    if (d.actionError) parts.push(errorHtml(d.actionError));
    box.innerHTML = parts.join("");
    box.hidden = parts.length === 0;
    $$(".failure-tail pre", box).forEach(function (pre) {
      pre.scrollTop = pre.scrollHeight;
    });
  }

  function logTailLines(count) {
    var d = app.detail;
    if (!d || !d.log) return [];
    var lines = d.log.replace(/\r\n/g, "\n").split("\n");
    while (lines.length && lines[lines.length - 1] === "") lines.pop();
    return lines.slice(-count);
  }

  function failureHtml(record) {
    var stages = Array.isArray(record.stages) ? record.stages : [];
    var failedStage = null;
    for (var i = 0; i < stages.length; i++) {
      if (stageStatus(stages[i]) === "failed") { failedStage = stages[i]; break; }
    }
    var failedName = failedStage ? (resolveStageName(failedStage.name || failedStage.stage) || failedStage.name || failedStage.stage) : null;
    var exitCode = failedStage && failedStage.exit_code !== undefined && failedStage.exit_code !== null
      ? failedStage.exit_code
      : record.exit_code;
    var counters = stageCounters(failedStage);
    var stageError = failedStage && failedStage.error ? String(failedStage.error) : (record.error ? String(record.error) : "");
    var tail = logTailLines(10);
    var laterSkipped = false;
    if (failedStage) {
      var idx = STAGES.findIndex(function (s) { return s.name === failedName; });
      for (var j = idx + 1; j < stages.length; j++) {
        if (stageStatus(stages[j]) === "skipped") { laterSkipped = true; break; }
      }
    }

    return '<div class="failure" role="alert">' +
      '<div class="failure-head">' +
      '<span class="failure-mark" aria-hidden="true">!</span>' +
      "<h3>作业失败</h3>" +
      '<span class="chip" data-state="failed">失败</span>' +
      "</div>" +
      '<div class="failure-body">' +
      "<p>阶段 <strong>" + esc(failedName ? stageNum(failedName) + " " + stageCn(failedName) : "未知") + "</strong>" +
      " 以退出码 <span class=\"mono\">" + esc(exitCode === null || exitCode === undefined ? "未知" : exitCode) + "</span> 结束。" +
      (laterSkipped ? "后续阶段已跳过。" : "") + "</p>" +
      (!counters ? '<p class="counts-note">该阶段没有写入终止进度行，因此没有阶段计数；以下是退出码与日志尾部。</p>' : "") +
      (stageError ? "<p class=\"mono\">" + esc(stageError) + "</p>" : "") +
      '<div class="failure-tail">' +
      '<div class="mini-head">日志尾部<span class="mono">' + (tail.length ? "末 " + tail.length + " 行" : "尚未读取") + "</span></div>" +
      "<pre>" + (tail.length ? esc(tail.join("\n")) : "暂无日志内容。重新运行 subgen web 时作业日志仍保留在 state/web/jobs/ 下。") + "</pre>" +
      "</div></div></div>";
  }

  function interruptedHtml(record) {
    var reason = reasonCn(record.reason);
    return '<div class="notice notice--bad">' +
      '<span class="notice-mark" aria-hidden="true">!</span>' +
      "<div><strong>作业已中断。</strong>当前阶段被标记为中断，后续阶段跳过。服务崩溃或停止不会删除任何流水线产物。" +
      (reason ? "原因：<span class=\"mono\">" + esc(reason) + "</span>。" : "") +
      "可以重跑作业继续处理。" +
      "</div></div>";
  }

  function cancelledHtml(record) {
    var reason = reasonCn(record.reason);
    return '<div class="notice">' +
      '<span class="notice-mark" aria-hidden="true">·</span>' +
      "<div><strong>作业已取消。</strong>取消请求优先于阶段退出码，后续阶段没有启动。" +
      (reason ? "原因：<span class=\"mono\">" + esc(reason) + "</span>。" : "") +
      "</div></div>";
  }

  function dryPanelHtml(record) {
    var stages = Array.isArray(record.stages) ? record.stages : [];
    var rows = stages.filter(function (stage) {
      return Array.isArray(stage.argv) && stage.argv.length;
    }).map(function (stage) {
      var name = resolveStageName(stage.name || stage.stage) || stage.name || stage.stage;
      var argv;
      if (Array.isArray(stage.argv)) {
        argv = stage.argv.map(function (part) { return String(part); }).join(" ");
      } else {
        argv = String(stage.argv || "");
      }
      return '<div class="cmd-row">' +
        '<div class="cmd-stage">' + stageNum(name) + " " + esc(stageCn(name)) + '<span class="mono">' + esc(name) + "</span></div>" +
        '<pre class="cmd-argv">' + esc(argv) + "</pre>" +
        "</div>";
    });
    if (!rows.length) {
      return '<section class="panel dry-panel"><header class="panel-head"><h2>试运行命令</h2></header>' +
        '<div class="panel-body counts-note">作业记录中没有命令清单。</div></section>';
    }
    return '<section class="panel dry-panel" aria-labelledby="dry-title">' +
      '<header class="panel-head"><h2 id="dry-title">试运行命令</h2>' +
      '<span class="panel-hint">已记录 · 未执行</span></header>' +
      '<div class="cmd-list">' + rows.join("") + "</div></section>";
  }

  function railLinkState(record, index) {
    var stage = stageEntry(record, STAGES[index].name);
    var status = stageStatus(stage);
    if (index >= STAGES.length - 1) return "none";
    if (status === "succeeded") return "filled";
    if (status === "failed" || status === "cancelled") return "break";
    if (status === "running") return "live";
    return "idle";
  }

  function counterPairs(counters, limit) {
    if (!counters || typeof counters !== "object") return [];
    var pairs = [];
    COUNTER_ORDER.forEach(function (key) {
      if (Number.isFinite(counters[key]) && counters[key] !== undefined) {
        pairs.push([key, counters[key]]);
      }
    });
    if (pairs.length < (limit || 3)) {
      Object.keys(counters).forEach(function (key) {
        if (pairs.some(function (pair) { return pair[0] === key; })) return;
        if (Number.isFinite(counters[key])) pairs.push([key, counters[key]]);
      });
    }
    return pairs.slice(0, limit || 3);
  }

  function railCountsHtml(record, index) {
    var name = STAGES[index].name;
    var stage = stageEntry(record, name);
    var d = app.detail;
    var status = stageStatus(stage);
    if (!stage && record.state === "dry_run") status = "skipped";
    var current = currentStageName(record);

    if (status === "running" && current === name && d && d.live.stage === name) {
      var live = d.live;
      if (live.done > 0 || live.total !== null) {
        var total = live.total === null ? "" : '<span class="dim">/</span><b>' + fmtNum(live.total) + "</b>";
        var fail = live.failed > 0 ? ' · <b class="fail">失败 ' + fmtNum(live.failed) + "</b>" : "";
        return '约 <b>' + fmtNum(live.done) + "</b>" + total + fail;
      }
      return "等待进度行…";
    }

    var counters = stageCounters(stage);
    if (counters) {
      var pairs = counterPairs(counters, 3);
      if (pairs.length) {
        var out = [];
        pairs.forEach(function (pair, index) {
          var label = COUNTER_CN[pair[0]] || pair[0];
          var isFail = pair[0].indexOf("failed") !== -1 || pair[0] === "missing_candidate" || pair[0] === "invalid_candidate";
          var extra = index >= 2 ? ' data-extra="1"' : "";
          if (index > 0) out.push('<span class="dim"' + (index >= 2 ? ' data-extra="1"' : "") + "> · </span>");
          out.push('<span class="' + (isFail && pair[1] > 0 ? "fail" : "") + '"' + extra + ">" +
            esc(label) + " <b>" + fmtNum(pair[1]) + "</b></span>");
        });
        return out.join("");
      }
    }

    if (status === "succeeded" || status === "failed" || status === "cancelled") return "无计数";
    if (status === "skipped") return "未执行";
    return "";
  }

  function renderRail() {
    var d = app.detail;
    var record = d && d.record;
    var rail = $("#detail-rail");
    if (!record || !rail) return;

    rail.innerHTML = STAGES.map(function (stage, index) {
      var entry = stageEntry(record, stage.name);
      var status = stageStatus(entry);
      if (!entry && record.state === "dry_run") status = "skipped";
      var counts = railCountsHtml(record, index);
      var srCounts = counts ? counts.replace(/<[^>]+>/g, " ") : "";
      return '<div class="stage-cell" role="listitem" data-testid="stage-cell" data-stage="' + stage.name + '" data-status="' + status + '" data-link="' + railLinkState(record, index) + '">' +
        '<div class="cell-node" aria-hidden="true">' + stage.num + "</div>" +
        '<div class="cell-name">' + stage.cn + "</div>" +
        '<div class="cell-id">' + stage.name + "</div>" +
        '<div class="cell-status">' + esc(STAGE_STATUS_CN[status] || status) + "</div>" +
        '<div class="cell-counts">' + counts + "</div>" +
        '<span class="sr-only">第 ' + (index + 1) + " 阶段 " + stage.cn + "，" + (STAGE_STATUS_CN[status] || status) + "。" + srCounts + "</span>" +
        "</div>";
    }).join("");

    var foot = $("#detail-rail-foot");
    if (!foot) return;
    var current = currentStageName(record);
    var duration = durationOf(record);
    var currentState = STATE_CN[record.state] || record.state;
    var progressHtml = "";
    if (current && d.live.stage === current && d.live.total !== null) {
      var pct = clampPercent(d.live.done, d.live.total);
      progressHtml = '<span class="rail-prog"><span class="progress" data-state="' + esc(record.state) + '"><i style="--p:' +
        (pct === null ? 0 : pct) + '%"></i></span><span class="mono">' +
        fmtNum(d.live.done) + " / " + fmtNum(d.live.total) + "</span></span>";
    }
    var currentLabel = current ? stageNum(current) + " " + stageCn(current) : (record.state === "dry_run" ? "未启动" : (record.state === "queued" ? "等待调度" : "无"));
    foot.innerHTML =
      "<span>当前阶段 <strong>" + esc(currentLabel) + "</strong></span>" +
      "<span>" + esc(currentState) + "</span>" +
      progressHtml +
      '<span class="mono rail-foot-push">已用 ' + esc(fmtDuration(duration)) + "</span>";
  }

  function renderFacts() {
    var d = app.detail;
    var record = d && d.record;
    var facts = $("#detail-facts");
    if (!record || !facts) return;
    var exitCode = record.exit_code;
    var exitClass = exitCode === null || exitCode === undefined ? "" : (exitCode === 0 ? " is-good" : " is-bad");
    facts.innerHTML = [
      ["创建", fmtTime(record.created_at), ""],
      ["更新", fmtTime(record.updated_at), ""],
      ["结束", fmtTime(record.ended_at), ""],
      ["已用", fmtDuration(durationOf(record)), ""],
      ["退出码", exitCode === null || exitCode === undefined ? "—" : String(exitCode), exitClass],
      ["调度", record.state === "dry_run" ? "试运行" : (record.state === "queued" ? "排队中" : (currentStageName(record) ? "第 " + (stageIndex(record) + 1) + " 阶段" : "—")), ""]
    ].map(function (row) {
      return '<div class="fact"><dt class="fact-k">' + esc(row[0]) + '</dt><dd class="fact-v' + row[2] + '">' + esc(row[1]) + "</dd></div>";
    }).join("");
    var scope = $("#detail-scope");
    if (scope) {
      scope.innerHTML = '<span class="scope-k">运行范围</span><span class="scope-v">' +
        paramChips(recordParams(record)).map(function (chip) { return "<span>" + esc(chip) + "</span>"; }).join("") +
        "</span>";
    }
  }

  function terminalCountersHtml(record) {
    var stages = Array.isArray(record.stages) ? record.stages : [];
    var rows = [];
    stages.forEach(function (stage) {
      var name = resolveStageName(stage.name || stage.stage) || stage.name || stage.stage;
      var counters = stageCounters(stage);
      if (!counters) return;
      var pairs = counterPairs(counters, 4);
      if (!pairs.length) return;
      rows.push('<div class="count-row"><span class="count-row-k">' +
        esc(stageNum(name) + " " + stageCn(name)) +
        '<span class="mono">' + esc(name) + "</span></span>" +
        '<span class="count-row-v">' + pairs.map(function (pair) {
          var isFail = pair[0].indexOf("failed") !== -1;
          return '<span class="' + (isFail && pair[1] > 0 ? "fail" : "") + '">' +
            esc(COUNTER_CN[pair[0]] || pair[0]) + ' <span class="dim">' + fmtNum(pair[1]) + "</span></span>";
        }).join('<span class="dim"> · </span>') + "</span></div>");
    });
    return rows.join("");
  }

  function renderCounts() {
    var d = app.detail;
    var record = d && d.record;
    var box = $("#detail-counts");
    var hint = $("#counts-hint");
    if (!record || !box) return;

    var current = currentStageName(record);
    var live = d.live;
    var html = "";

    if (record.state === "running" && current) {
      var total = live.total;
      var done = live.done;
      var pct = clampPercent(done, total);
      html += '<div class="live">' +
        '<div class="live-k">当前阶段 · ' + esc(stageNum(current) + " " + stageCn(current)) + " · 约</div>" +
        '<div class="live-v">' + fmtNum(done) + (total !== null ? "<span>/" + fmtNum(total) + "</span>" : "") +
        '<em>近似</em></div>' +
        '<div class="meter"><i style="--p:' + (pct === null ? 0 : pct) + '%"></i></div>' +
        '<div class="live-meta">' +
        '<span>失败 <b class="' + (live.failed > 0 ? "is-bad" : "") + '">' + fmtNum(live.failed) + "</b></span>" +
        "<span>事件 <b>" + fmtNum(live.events) + "</b></span>" +
        "<span>阶段已用 <b>" + esc(stageElapsed(record, current)) + "</b></span>" +
        "</div></div>";
      if (hint) hint.textContent = "每秒 · 近似";
    } else if (record.state === "queued") {
      html += '<div class="live"><div class="live-k">等待调度</div>' +
        '<div class="live-v is-quiet">排队中</div>' +
        '<div class="live-meta"><span>工作进程尚未领取作业</span></div></div>';
      if (hint) hint.textContent = "排队";
    } else if (record.state === "dry_run") {
      html += '<div class="live"><div class="live-k">试运行</div>' +
        '<div class="live-v is-quiet">' + plannedCommandCount(record) + " 条命令已构建</div>" +
        '<div class="live-meta"><span>没有阶段执行，因此没有计数</span></div></div>';
      if (hint) hint.textContent = "无计数";
    } else {
      html += '<div class="live"><div class="live-k">终态</div>' +
        '<div class="live-v is-quiet">' + esc(STATE_CN[record.state] || record.state) + "</div>" +
        '<div class="live-meta"><span>总用时 <b>' + esc(fmtDuration(durationOf(record))) + "</b></span>" +
        "<span>退出码 <b>" + esc(record.exit_code === null || record.exit_code === undefined ? "—" : record.exit_code) + "</b></span></div></div>";
      if (hint) hint.textContent = "终止进度行";
    }

    var counters = terminalCountersHtml(record);
    if (counters) {
      html += '<div class="counts-table">' + counters + "</div>";
    } else if (record.state !== "running" && record.state !== "dry_run" && record.state !== "queued") {
      html += '<p class="counts-note">本作业没有可用的阶段计数。' +
        (record.state === "failed" ? "失败阶段没有写入终止进度行，页面用退出码和日志尾部代替计数。" : "阶段没有写入终止进度行，或作业在第一个阶段前就结束了。") +
        "</p>";
    }
    box.innerHTML = html;
  }

  function stageElapsed(record, name) {
    var stage = stageEntry(record, name);
    if (!stage) return "—";
    var start = toEpoch(stage.started_at);
    if (start === null) return "—";
    var end = toEpoch(stage.ended_at);
    return fmtDuration((end === null ? Date.now() / 1000 : end) - start);
  }

  function eventRowHtml(event, fallbackStage) {
    var stageName = resolveStageName(event.stage || fallbackStage) || event.stage || fallbackStage || "";
    var time = event.timestamp !== undefined ? fmtClock(event.timestamp) : (event.at ? String(event.at).slice(-8) : "—");
    var video = event.relative_path || event.video_id || "";
    var rawEvent = String(event.event || "");
    var failure = isFailureEvent(rawEvent);
    return '<li class="tail-row">' +
      '<span class="tail-time mono">' + esc(time) + "</span>" +
      '<span class="tail-stage mono">' + esc(stageName ? stageCn(stageName) : "—") + "</span>" +
      '<div class="tail-body">' +
      '<span class="tail-event' + (failure ? " ev-fail" : "") + '" title="' + esc(rawEvent) + '">' + esc(eventCn(rawEvent)) + "</span>" +
      '<span class="tail-video" title="' + esc(video || rawEvent) + '">' + esc(video || "—") + "</span>" +
      "</div></li>";
  }

  function renderEvents() {
    var d = app.detail;
    var list = $("#detail-events");
    if (!d || !list) return;
    if (!d.events.length) {
      list.innerHTML = '<li class="tail-empty">暂无逐视频事件。<br>作业进入阶段后会每秒追加进度行。</li>';
    } else {
      list.innerHTML = d.events.map(function (event) {
        return eventRowHtml(event, event.stage);
      }).join("");
    }
    var hint = $("#events-hint");
    if (hint) hint.textContent = d.events.length ? "最近 " + d.events.length + " 条" : "最近 " + MAX_EVENTS + " 条";
    list.scrollTop = list.scrollHeight;
  }

  function appendEvents(rows) {
    var d = app.detail;
    var list = $("#detail-events");
    if (!d || !list || !rows.length) return;
    var nearBottom = isNearBottom(list, 60);
    var added = 0;

    rows.forEach(function (event) {
      if (!event || typeof event !== "object") return;
      var key = eventKey(event, currentStageName(d.record));
      if (d.eventKeys.has(key)) return;
      d.eventKeys.add(key);
      d.events.push(event);
      if (added < MAX_EVENTS) {
        list.insertAdjacentHTML("beforeend", eventRowHtml(event, currentStageName(d.record)));
        added += 1;
      }
    });

    if (!d.events.length) return;
    var emptyEl = list.querySelector(".tail-empty");
    if (emptyEl) emptyEl.remove();

    while (d.events.length > MAX_EVENTS) d.events.shift();
    while (list.children.length > MAX_EVENTS) list.removeChild(list.firstElementChild);

    var hint = $("#events-hint");
    if (hint) hint.textContent = "最近 " + d.events.length + " 条";
    if (nearBottom) list.scrollTop = list.scrollHeight;
  }

  function renderLog() {
    var d = app.detail;
    var pre = $("#detail-log");
    if (!d || !pre) return;
    pre.textContent = d.log;
    pre.dataset.empty = d.log ? "false" : "true";
    if (d.following) pre.scrollTop = pre.scrollHeight;
    updateFollowButton();
    updateLogOffset();
  }

  function updateFollowButton() {
    var btn = $("#follow-btn");
    var d = app.detail;
    if (!btn || !d) return;
    btn.setAttribute("aria-pressed", d.following ? "true" : "false");
    btn.textContent = d.following ? "跟随中" : "已暂停";
  }

  function updateLogOffset() {
    var el = $("#log-offset");
    var d = app.detail;
    if (!el || !d) return;
    if (d.logSince === null) {
      el.textContent = d.log ? "偏移 —" : "偏移 0 B";
    } else {
      el.textContent = "偏移 " + fmtNum(d.logSince) + " B";
    }
  }

  function applyLogChunk(chunk, reset) {
    var d = app.detail;
    var pre = $("#detail-log");
    if (!d) return;
    if (reset) {
      d.log = "";
      if (pre) pre.textContent = "";
    }
    if (typeof chunk !== "string" || !chunk) {
      if (pre) pre.dataset.empty = d.log ? "false" : "true";
      return;
    }
    d.log += chunk;
    if (d.log.length > MAX_LOG) d.log = d.log.slice(-MAX_LOG);
    if (pre) {
      if (d.log.length >= MAX_LOG) {
        pre.textContent = d.log;
      } else {
        pre.appendChild(document.createTextNode(chunk));
      }
      pre.dataset.empty = "false";
      if (d.following) pre.scrollTop = pre.scrollHeight;
    }
    updateLogOffset();
  }

  function foldLive(events, record) {
    var d = app.detail;
    if (!d) return;
    var current = currentStageName(record);
    if (!current) return;
    if (d.live.stage !== current) {
      d.live = { stage: current, done: 0, total: null, failed: 0, events: 0 };
    }
    var live = d.live;
    events.forEach(function (event) {
      if (!event || typeof event !== "object") return;
      var resolved = resolveStageName(event.stage) || event.stage;
      if (resolved !== current) return;
      var name = String(event.event || "");
      live.events += 1;
      if (isFailureEvent(name)) live.failed += 1;

      if (current === "inventory") {
        if (name === "observed") live.done += 1;
        if (name === "discovered" && Number.isFinite(event.count)) live.total = event.count;
        return;
      }
      if (current === "extract") {
        if (name === "batch_started" && Number.isFinite(event.selected)) live.total = event.selected;
        if (name === "completed" || name === "skipped_existing" || name === "skipped_no_audio") live.done += 1;
        return;
      }
      if (name === "batch_started") {
        var total = num(event.processable);
        if (total === null) total = num(event.selected);
        if (total !== null) live.total = total;
        return;
      }
      if (current === "publish") {
        if (name === "video_completed") live.done += 1;
        return;
      }
      if (name === "video_completed" || name === "skipped_verified") live.done += 1;
      if ((current === "translation_primary" || current === "translation_review") && name === "video_existing") {
        live.done += 1;
      }
    });
  }

  function collectStoredEvents(record) {
    var rows = [];
    var stages = record && Array.isArray(record.stages) ? record.stages : [];
    stages.forEach(function (stage) {
      var name = resolveStageName(stage.name || stage.stage) || stage.name || stage.stage;
      stageEvents(stage).forEach(function (event) {
        if (event && typeof event === "object") {
          rows.push(Object.assign({}, event, { stage: event.stage || name }));
        }
      });
    });
    return rows;
  }

  function updateDetailLive() {
    var d = app.detail;
    var record = d && d.record;
    if (!record) return;

    var stateEl = $("#detail-state");
    if (stateEl) {
      stateEl.textContent = STATE_CN[record.state] || record.state;
      stateEl.dataset.state = record.state;
    }

    renderFacts();
    renderRail();
    renderCounts();
    renderDetailError();
    updateActionState();

    var toolbarState = $("#rail-state-note");
    if (toolbarState) toolbarState.textContent = railNote(record);
  }

  function updateActionState() {
    var d = app.detail;
    var record = d && d.record;
    if (!record) return;
    var active = isActive(record);
    var cancelBtn = $("#cancel-button");
    var rerunBtn = $("#rerun-button");
    var deleteBtn = $("#delete-button");
    if (cancelBtn) {
      cancelBtn.disabled = !active || !!d.actionBusy;
      cancelBtn.title = active ? "取消当前阶段（SIGINT → SIGTERM → SIGKILL）" : "作业已结束";
    }
    if (rerunBtn) {
      rerunBtn.disabled = active || !!d.actionBusy;
      rerunBtn.title = active ? "运行中的作业不能重跑" : "用相同参数和当前设置创建新作业";
    }
    if (deleteBtn) {
      deleteBtn.disabled = active || !!d.actionBusy;
      deleteBtn.title = active ? "作业尚未结束，不能删除" : "删除作业记录与日志";
    }
  }

  function bindDetailActions() {
    var cancelBtn = $("#cancel-button");
    var rerunBtn = $("#rerun-button");
    var deleteBtn = $("#delete-button");
    var followBtn = $("#follow-btn");
    var logPre = $("#detail-log");

    if (cancelBtn) cancelBtn.addEventListener("click", function () { cancelJob(app.detail.id); });
    if (rerunBtn) rerunBtn.addEventListener("click", function () { rerunJob(app.detail.id, true); });
    if (deleteBtn) deleteBtn.addEventListener("click", function () { deleteJob(app.detail.id); });

    if (followBtn) {
      followBtn.addEventListener("click", function () {
        app.detail.following = !app.detail.following;
        updateFollowButton();
        if (app.detail.following && logPre) logPre.scrollTop = logPre.scrollHeight;
      });
    }
    if (logPre) {
      logPre.addEventListener("scroll", function () {
        if (!app.detail) return;
        var atBottom = isNearBottom(logPre, 30);
        if (atBottom !== app.detail.following) {
          app.detail.following = atBottom;
          updateFollowButton();
        }
      });
    }
  }

  function railNote(record) {
    if (!record) return "—";
    if (record.state === "dry_run") return "试运行 · " + plannedCommandCount(record) + " 条命令";
    var index = stageIndex(record);
    if (index >= 0) return "第 " + (index + 1) + " / 7 阶段";
    return STATE_CN[record.state] || record.state;
  }

  function renderDetailShell() {
    var d = app.detail;
    var record = d && d.record;
    var view = $("#job-detail");
    if (!record || !view) return;

    var params = recordParams(record);
    var dry = record.state === "dry_run" || params.dry_run === true;
    var stateCn = STATE_CN[record.state] || record.state;
    var origin = record.origin_job_id
      ? '<a class="origin-link" href="#/jobs/' + encodeURIComponent(record.origin_job_id) + '">来源 ' + esc(record.origin_job_id) + "</a>"
      : "";

    view.innerHTML =
      '<nav class="crumbs" aria-label="面包屑"><a href="#/jobs">作业</a><span class="crumb-sep" aria-hidden="true">/</span><span class="mono">' + esc(record.job_id) + "</span></nav>" +
      '<div class="detail-head">' +
      '<div class="detail-ident">' +
      '<h1 class="detail-id">' + esc(record.job_id) + "</h1>" +
      '<div class="detail-badges">' +
      '<span class="chip" id="detail-state" data-testid="job-state" data-state="' + esc(record.state) + '">' + esc(stateCn) + "</span>" +
      (record.reason ? '<span class="chip chip--plain">原因 ' + esc(reasonCn(record.reason)) + "</span>" : "") +
      origin +
      "</div></div>" +
      '<div class="detail-actions">' +
      '<button class="btn btn--tiny" type="button" id="cancel-button" data-testid="cancel-button">取消</button>' +
      '<button class="btn btn--tiny" type="button" id="rerun-button" data-testid="rerun-button">重跑</button>' +
      '<button class="btn btn--tiny btn--danger" type="button" id="delete-button" data-testid="delete-button">删除</button>' +
      "</div></div>" +
      '<dl class="facts" id="detail-facts"></dl>' +
      '<p class="detail-scope" id="detail-scope"></p>' +
      '<div class="job-error" id="detail-error" data-testid="job-error" hidden></div>' +
      '<section class="panel rail-panel" aria-labelledby="rail-title">' +
      '<header class="panel-head"><h2 id="rail-title">七阶段进度</h2>' +
      '<span class="panel-hint" id="rail-state-note">' + esc(railNote(record)) + "</span></header>" +
      '<div class="rail" id="detail-rail" data-testid="stage-rail" role="list" aria-label="七阶段进度"></div>' +
      '<div class="rail-foot" id="detail-rail-foot"></div>' +
      "</section>" +
      '<div class="detail-grid">' +
      '<section class="panel counts-panel" aria-labelledby="counts-title">' +
      '<header class="panel-head"><h2 id="counts-title">进度计数</h2><span class="panel-hint" id="counts-hint">—</span></header>' +
      '<div id="detail-counts"></div>' +
      "</section>" +
      '<section class="panel" aria-labelledby="events-title">' +
      '<header class="panel-head"><h2 id="events-title">逐视频事件</h2><span class="panel-hint" id="events-hint">最近 ' + MAX_EVENTS + " 条</span></header>" +
      '<ol class="tail" id="detail-events" data-testid="event-tail"></ol>' +
      "</section>" +
      '<section class="panel log-panel" id="log-panel" data-testid="log-panel" aria-labelledby="log-title">' +
      '<header class="panel-head"><h2 id="log-title">阶段日志</h2>' +
      '<div class="log-tools"><span class="panel-hint" id="log-offset">偏移 0 B</span>' +
      '<button class="follow-btn" type="button" id="follow-btn" aria-pressed="true">跟随中</button></div></header>' +
      '<pre class="log-body" id="detail-log" tabindex="0" aria-label="作业日志"></pre>' +
      "</section>" +
      "</div>" +
      (dry ? dryPanelHtml(record) : "");

    bindDetailActions();
    renderFacts();
    renderRail();
    renderCounts();
    renderEvents();
    renderLog();
    renderDetailError();
    updateActionState();
  }

  function renderDetailLoadError(err) {
    var view = $("#job-detail");
    if (!view) return;
    var notFound = err && (err.code === "not_found" || err.status === 404);
    view.innerHTML =
      '<nav class="crumbs" aria-label="面包屑"><a href="#/jobs">作业</a><span class="crumb-sep">/</span><span class="mono">找不到作业</span></nav>' +
      (notFound
        ? '<div class="empty"><div class="empty-mark" aria-hidden="true">404</div><h3>找不到该作业</h3><p>它可能已被删除，或服务的工作目录已经改变。</p><p class="empty-action"><a href="#/jobs">返回作业列表 →</a></p></div>'
        : errorHtml(err) +
          '<p class="empty-action"><button class="btn" type="button" data-act="reload-detail">重新加载</button> ' +
          '<a href="#/jobs">返回作业列表 →</a></p>');
  }

  function openDetail(jobId) {
    stopDetailPoll();
    var detail = {
      id: jobId,
      record: null,
      events: [],
      eventKeys: new Set(),
      log: "",
      since: null,
      logSince: null,
      live: { stage: null, done: 0, total: null, failed: 0, events: 0 },
      following: true,
      loading: true,
      error: null,
      actionError: null,
      actionBusy: false,
      polling: false
    };
    app.detail = detail;

    var view = $("#job-detail");
    view.innerHTML = detailSkeleton();

    requestJson("GET", "/api/jobs/" + encodeURIComponent(jobId)).then(function (data) {
      if (app.detail !== detail) return;
      var record = unwrapJob(data);
      if (!record) throw new ApiError({ status: 200, code: "internal_error", message: "作业响应缺少 job_id" });

      detail.record = record;
      detail.loading = false;
      detail.lastRenderedState = record.state;

      collectStoredEvents(record).slice(-MAX_EVENTS).forEach(function (event) {
        var key = eventKey(event, currentStageName(record));
        if (!detail.eventKeys.has(key)) {
          detail.eventKeys.add(key);
          detail.events.push(event);
        }
      });

      renderDetailShell();

      if (isActive(record)) {
        scheduleDetail(700);
      } else {
        loadFinishedLog(detail);
      }
    }).catch(function (err) {
      if (app.detail !== detail) return;
      detail.loading = false;
      detail.error = err;
      if (err instanceof OfflineError) setConnection(false, err);
      renderDetailLoadError(err);
    });
  }

  function loadFinishedLog(detail) {
    requestText("/api/jobs/" + encodeURIComponent(detail.id) + "/log").then(function (text) {
      if (app.detail !== detail) return;
      detail.log = text.length > MAX_LOG ? text.slice(-MAX_LOG) : text;
      renderLog();
      renderDetailError();
    }).catch(function () {
      // A finished job without a log file is not an error worth a banner.
      if (app.detail !== detail) return;
      renderLog();
    });
  }

  function scheduleDetail(delay) {
    clearTimeout(app.detailTimer);
    if (document.hidden) return;
    app.detailTimer = setTimeout(detailTick, delay === undefined ? DETAIL_INTERVAL : delay);
  }

  function stopDetailPoll() {
    clearTimeout(app.detailTimer);
    app.detailTimer = null;
  }

  async function detailTick() {
    var detail = app.detail;
    if (!detail || !detail.record) return;
    if (document.hidden) { scheduleDetail(1500); return; }
    if (!isActive(detail.record)) return;
    if (detail.polling) { scheduleDetail(DETAIL_INTERVAL); return; }
    detail.polling = true;

    var nextDelay = DETAIL_INTERVAL;
    try {
      var query = [];
      if (detail.since !== null) query.push("since=" + encodeURIComponent(detail.since));
      if (detail.logSince !== null) query.push("log_since=" + encodeURIComponent(detail.logSince));
      var path = "/api/jobs/" + encodeURIComponent(detail.id) + "/progress" + (query.length ? "?" + query.join("&") : "");

      var data = await requestJson("GET", path);
      if (app.detail !== detail) return;

      var job = unwrapJob(data);
      if (job) {
        detail.record = mergeRecord(detail.record, job);
      }
      if (Number.isFinite(data.next_offset)) detail.since = data.next_offset;
      if (Number.isFinite(data.next_log_offset)) detail.logSince = data.next_log_offset;

      var events = Array.isArray(data.events) ? data.events : [];
      foldLive(events, detail.record);
      appendEvents(events.slice(-MAX_EVENTS));
      applyLogChunk(typeof data.log === "string" ? data.log : "", data.log_reset === true);

      var previousState = detail.lastRenderedState;
      updateDetailLive();
      if (previousState !== detail.record.state) {
        announce("作业状态：" + (STATE_CN[detail.record.state] || detail.record.state));
        detail.lastRenderedState = detail.record.state;
      }

      if (!isActive(detail.record)) {
        await finishDetail(detail);
        return;
      }
    } catch (err) {
      if (app.detail !== detail) return;
      if (err instanceof OfflineError) {
        setConnection(false, err);
        detail.actionError = err;
        renderDetailError();
        nextDelay = 4000;
      } else if (err.code === "not_found") {
        detail.actionError = err;
        renderDetailError();
        return;
      } else {
        detail.actionError = err;
        renderDetailError();
        nextDelay = 3000;
      }
    } finally {
      detail.polling = false;
    }

    if (app.detail === detail && isActive(detail.record)) scheduleDetail(nextDelay);
  }

  async function finishDetail(detail) {
    stopDetailPoll();
    try {
      var data = await requestJson("GET", "/api/jobs/" + encodeURIComponent(detail.id));
      if (app.detail !== detail) return;
      var record = unwrapJob(data);
      if (record) detail.record = record;

      collectStoredEvents(detail.record).forEach(function (event) {
        var key = eventKey(event, currentStageName(detail.record));
        if (!detail.eventKeys.has(key)) {
          detail.eventKeys.add(key);
          detail.events.push(event);
        }
      });
      while (detail.events.length > MAX_EVENTS) detail.events.shift();

      if (!detail.log) {
        try {
          var text = await requestText("/api/jobs/" + encodeURIComponent(detail.id) + "/log");
          detail.log = text.length > MAX_LOG ? text.slice(-MAX_LOG) : text;
        } catch (err) { /* the log is optional for the summary */ }
      }

      detail.actionError = null;
      renderDetailShell();
      var stateCn = STATE_CN[detail.record.state] || detail.record.state;
      toast("作业" + stateCn, detail.record.state === "succeeded" || detail.record.state === "dry_run" ? "success" : "info");
      announce("作业" + stateCn);
      refreshMeta();
      if (app.route.name === "jobs") refreshJobs({ quiet: true });
    } catch (err) {
      detail.actionError = err;
      renderDetailError();
      updateActionState();
    }
  }


  function fieldsFromSpecs() {
    return FIELD_SPECS.map(function (spec) {
      return {
        section: spec.section,
        key: spec.key,
        env: spec.env,
        kind: spec.kind,
        file_value: null,
        effective_value: null,
        source: "default",
        choices: spec.choices || [],
        error: null
      };
    });
  }

  function normalizeSettingsView(view) {
    var data = view && typeof view === "object" ? view : {};
    var rawFields = Array.isArray(data.fields) ? data.fields : [];
    var fields = rawFields.map(function (field) {
      if (!field || typeof field !== "object") return null;
      var section = String(field.section || "");
      var key = String(field.key || "");
      if (!section || !key) return null;
      var spec = specFor(section, key);
      return {
        section: section,
        key: key,
        env: field.env || (spec ? spec.env : ""),
        kind: field.kind || (spec ? spec.kind : "text"),
        file_value: field.file_value === undefined ? null : field.file_value,
        effective_value: field.effective_value === undefined ? null : field.effective_value,
        source: field.source || "default",
        choices: Array.isArray(field.choices) ? field.choices : (spec && spec.choices ? spec.choices : []),
        error: field.error || null
      };
    }).filter(Boolean);
    if (!fields.length) fields = fieldsFromSpecs();

    return {
      config_path: data.config_path || "",
      exists: data.exists !== false,
      settings_ok: data.settings_ok !== false,
      load_error: data.load_error || null,
      base_sha256: data.base_sha256 === undefined ? "" : data.base_sha256,
      api_key_configured: data.api_key_configured === true,
      fields: fields,
      warnings: normalizeWarnings(data.warnings)
    };
  }

  function normalizeWarnings(warnings) {
    if (!Array.isArray(warnings)) return [];
    return warnings.map(function (item) {
      if (typeof item === "string") return item;
      if (item && typeof item === "object") {
        return item.message || item.error || item.detail || JSON.stringify(item);
      }
      return String(item);
    }).filter(Boolean);
  }

  function draftFromFields(fields) {
    var draft = {};
    fields.forEach(function (field) {
      var key = field.section + "." + field.key;
      draft[key] = field.file_value === null || field.file_value === undefined ? "" : String(field.file_value);
    });
    return draft;
  }

  function makeSettingsState(view) {
    var normalized = normalizeSettingsView(view);
    var draft = draftFromFields(normalized.fields);
    return {
      view: normalized,
      draft: draft,
      baseline: Object.assign({}, draft),
      baseSha: normalized.base_sha256,
      fieldErrors: {},
      warnings: normalized.warnings.slice(),
      preview: "",
      valid: null,
      validating: false,
      pendingValidation: false,
      saving: false,
      error: null,
      conflict: false,
      loadError: normalized.load_error || (normalized.settings_ok === false ? "settings_ok=false" : null),
      validationDiagnostic: null,
      debounce: null,
      lastSavedAt: null
    };
  }

  function settingsFields() {
    return app.settings ? app.settings.view.fields : [];
  }

  function fieldByKey(key) {
    var fields = settingsFields();
    for (var i = 0; i < fields.length; i++) {
      if (fields[i].section + "." + fields[i].key === key) return fields[i];
    }
    return null;
  }

  function isSettingsDirty() {
    var s = app.settings;
    if (!s) return false;
    return Object.keys(s.draft).some(function (key) {
      var before = s.baseline[key] === undefined ? "" : s.baseline[key];
      return (s.draft[key] || "") !== before;
    });
  }

  function dirtyFieldCount() {
    var s = app.settings;
    if (!s) return 0;
    return Object.keys(s.draft).filter(function (key) {
      var before = s.baseline[key] === undefined ? "" : s.baseline[key];
      return (s.draft[key] || "") !== before;
    }).length;
  }

  function renderSettingsLoading() {
    $("#settings-body").innerHTML =
      '<div class="detail-skeleton" aria-hidden="true"><div class="bar"></div><div class="bar bar--tall"></div><div class="bar"></div></div>';
  }

  function settingsSummaryHtml() {
    var s = app.settings;
    var view = s.view;
    var existsClass = view.exists ? "is-good" : "is-warn";
    var okClass = view.settings_ok ? "is-good" : "is-bad";
    var apiClass = view.api_key_configured ? "is-good" : "";
    var baseClass = view.base_sha256 ? "" : "is-warn";
    return '<section class="panel summary-panel" aria-labelledby="summary-title">' +
      '<header class="panel-head"><h2 id="summary-title">配置文件</h2>' +
      '<span class="panel-hint">' + esc(view.exists ? "已读取" : "文件不存在") + "</span></header>" +
      '<div class="summary-path"><span class="k">路径</span>' + esc(view.config_path || "未定位到 subgen.toml") + "</div>" +
      '<dl class="summary-grid">' +
      '<div class="summary-item"><dt>文件</dt><dd class="' + existsClass + '">' + (view.exists ? "存在" : "缺失") + "</dd></div>" +
      '<div class="summary-item"><dt>设置</dt><dd class="' + okClass + '">' + (view.settings_ok ? "可加载" : "无法加载") + "</dd></div>" +
      '<div class="summary-item"><dt>API key</dt><dd class="' + apiClass + '">' + (view.api_key_configured ? "已配置" : "未配置") + "</dd></div>" +
      '<div class="summary-item"><dt>base sha</dt><dd class="mono ' + baseClass + '" title="' + esc(view.base_sha256 || "") + '">' + esc(shortSha(view.base_sha256)) + "</dd></div>" +
      "</dl></section>";
  }

  function fieldInputHtml(field, value, describedBy) {
    var key = field.section + "." + field.key;
    var inputId = "f-" + field.section + "-" + field.key;
    var described = describedBy ? ' aria-describedby="' + esc(describedBy) + '"' : "";
    var spec = specFor(field.section, field.key);
    var label = spec ? spec.label : field.key;
    var choices = field.choices && field.choices.length ? field.choices : (spec && spec.choices ? spec.choices : []);

    if (field.kind === "enum" && choices.length) {
      var options = '<option value=""' + (value ? "" : " selected") + ">未设置（按默认）</option>" +
        choices.map(function (choice) {
          return '<option value="' + esc(choice) + '"' + (String(choice) === String(value) ? " selected" : "") + ">" + esc(choice) + "</option>";
        }).join("");
      if (value && choices.indexOf(value) === -1) {
        options += '<option value="' + esc(value) + '" selected>' + esc(value) + "</option>";
      }
      return '<select class="input field-input" id="' + inputId + '" data-testid="field-input" data-field="' + esc(key) + '" aria-label="' + esc(label) + '"' + described + ">" + options + "</select>";
    }
    return '<input class="input field-input" id="' + inputId + '" data-testid="field-input" data-field="' + esc(key) + '"' +
      ' type="text" value="' + esc(value) + '" spellcheck="false" autocomplete="off" autocapitalize="off"' +
      ' aria-label="' + esc(label) + '"' + described + ">";
  }

  function fieldRowHtml(field) {
    var key = field.section + "." + field.key;
    var spec = specFor(field.section, field.key);
    var label = spec ? spec.label : field.key;
    var help = spec ? spec.help : "";
    var value = app.settings.draft[key] === undefined ? "" : app.settings.draft[key];
    var source = field.source || "default";
    var shadowed = source === "env";
    var envName = field.env || (spec ? spec.env : "");
    var fileValue = fmtValue(field.file_value);
    var effective = fmtValue(field.effective_value);
    var effectiveEmpty = field.effective_value === null || field.effective_value === undefined;
    var hintId = "hint-" + field.section + "-" + field.key;
    var errorId = "err-" + field.section + "-" + field.key;

    return '<div class="field-row" data-field="' + esc(key) + '" data-shadowed="' + shadowed + '">' +
      '<div class="field-top">' +
      '<label for="f-' + esc(field.section) + "-" + esc(field.key) + '">' + esc(label) + "</label>" +
      '<span class="field-key">' + esc(key) + "</span>" +
      '<span class="field-source" data-testid="field-source" data-field="' + esc(key) + '" data-source="' + esc(source) + '">' +
      esc(SOURCE_CN[source] || source) + "</span>" +
      "</div>" +
      fieldInputHtml(field, value, hintId + " " + errorId) +
      '<div class="field-meta">' +
      '<span class="m-file">文件 <b>' + esc(fileValue) + "</b></span>" +
      '<span class="m-eff">生效 <b class="' + (effectiveEmpty ? "is-empty" : "") + '">' + esc(effective) + "</b></span>" +
      "</div>" +
      (help ? '<p class="field-hint" id="' + hintId + '">' + esc(help) + "</p>" : "") +
      (shadowed && envName
        ? '<div class="field-env">环境变量 <span class="mono">' + esc(envName) + "</span> 覆盖文件值；此处保存只写入 subgen.toml，不改变本次生效值。</div>"
        : "") +
      '<p class="field-error" id="' + errorId + '" data-testid="field-error" data-field="' + esc(key) + '" hidden></p>' +
      "</div>";
  }

  function isWideField(field) {
    if (field.kind === "path" || field.kind === "url") return true;
    return field.section === "translation" && field.key === "api_key_env";
  }

  function settingsGroupFieldsHtml(groupFields) {
    var html = [];
    var pending = [];

    function flush() {
      if (!pending.length) return;
      html.push('<div class="field-pair">' + pending.join("") + "</div>");
      pending = [];
    }

    groupFields.forEach(function (field) {
      if (isWideField(field)) {
        flush();
        html.push(fieldRowHtml(field));
        return;
      }
      pending.push(fieldRowHtml(field));
      if (pending.length === 2) flush();
    });
    flush();
    return html.join("");
  }

  function settingsGroupsHtml() {
    var sections = ["paths", "runtime", "translation"];
    var fields = settingsFields();
    return sections.map(function (section) {
      var groupFields = fields.filter(function (field) { return field.section === section; });
      if (!groupFields.length) return "";
      return '<section class="panel settings-group" aria-labelledby="group-' + section + '">' +
        '<header class="panel-head"><h2 id="group-' + section + '">' + esc(SECTION_CN[section] || section) + "</h2>" +
        '<span class="group-desc">' + esc(SECTION_DESC[section] || "") + "</span>" +
        '<span class="panel-hint">' + esc(section) + "</span></header>" +
        settingsGroupFieldsHtml(groupFields) +
        "</section>";
    }).join("");
  }

  function settingsWarningsHtml() {
    var warnings = app.settings.warnings;
    if (!warnings.length) return "";
    return '<section class="panel warnings-panel" aria-labelledby="warnings-title">' +
      '<header class="panel-head"><h2 id="warnings-title">警告</h2><span class="panel-hint">不阻止保存</span></header>' +
      '<div class="warnings">' + warnings.map(function (warning) {
        return '<div class="warning-item">' + esc(warning) + "</div>";
      }).join("") + "</div></section>";
  }

  function renderSettings() {
    var s = app.settings;
    if (!s) return;
    var body = $("#settings-body");
    if (!body) return;

    body.innerHTML =
      '<div class="settings-error" id="settings-error" data-testid="settings-error" hidden></div>' +
      '<div class="settings-layout">' +
      '<form class="settings-main" id="settings-form" data-testid="settings-form" novalidate>' +
      settingsSummaryHtml() +
      settingsGroupsHtml() +
      settingsWarningsHtml() +
      "</form>" +
      '<aside class="settings-side">' +
      '<section class="panel preview-wrap" aria-labelledby="preview-title">' +
      '<header class="panel-head"><h2 id="preview-title">保存预览</h2><span class="panel-hint" id="preview-hint">校验通过后生成</span></header>' +
      '<pre class="preview" id="settings-preview" data-testid="settings-preview" aria-live="polite"></pre>' +
      "</section>" +
      '<section class="panel save-panel" aria-labelledby="save-title">' +
      '<header class="panel-head"><h2 id="save-title">保存</h2><span class="panel-hint" id="save-hint">未修改</span></header>' +
      '<div class="panel-body">' +
      '<div class="save-state" id="save-state" data-valid="pending"><span class="state-dot" aria-hidden="true"></span><div id="save-state-text">校验中…</div></div>' +
      '<div class="save-actions">' +
      '<button class="btn btn--primary" type="submit" form="settings-form" id="settings-save" data-testid="settings-save" disabled>保存</button>' +
      '<button class="btn btn--primary" type="button" data-act="repair" data-testid="repair-action" hidden>修复并写入</button>' +
      '<button class="btn btn--danger" type="button" data-act="force" hidden>强制写入规范 TOML</button>' +
      "</div>" +
      '<div class="save-meta"><span>base <b id="save-base">—</b></span><span id="save-dirty">未修改</span></div>' +
      '<details class="save-diag" id="save-diag" hidden><summary>校验诊断（英文原文）</summary><pre id="save-diag-pre"></pre></details>' +
      '<p class="save-note">保存会保留注释与顺序，并先把原文件备份为 subgen.toml.bak。环境变量覆盖的字段只写文件，不影响生效值。</p>' +
      "</div></section>" +
      "</aside></div>";

    bindSettingsForm();
    updateSettingsMeta();
    renderSettingsErrors();
    updateSaveState();
  }

  function bindSettingsForm() {
    var form = $("#settings-form");
    if (!form) return;
    form.addEventListener("input", onSettingsInput);
    form.addEventListener("change", onSettingsInput);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      saveSettings(false);
    });
  }

  function onSettingsInput(event) {
    var s = app.settings;
    if (!s) return;
    var target = event.target;
    if (!target || !target.dataset || !target.dataset.field) return;
    var key = target.dataset.field;
    if (!(key in s.draft)) return;
    if (s.draft[key] === target.value) return;
    s.draft[key] = target.value;
    // Any edit invalidates the last verdict until the next validate round trip.
    s.valid = null;
    var preview = $("#settings-preview");
    if (preview && s.preview) preview.classList.add("is-stale");
    updateSaveState();
    scheduleValidate(420);
  }

  function scheduleValidate(delay) {
    var s = app.settings;
    if (!s) return;
    clearTimeout(s.debounce);
    s.debounce = setTimeout(function () { validateSettings(); }, delay === undefined ? 400 : delay);
  }

  function fieldErrorMap(fields) {
    var map = {};
    fields.forEach(function (item) {
      var field = String(item.field || "");
      if (!field) return;
      var key = field.indexOf(".") !== -1 ? field : null;
      if (!key) {
        var match = settingsFields().filter(function (spec) {
          return spec.key === field;
        })[0];
        if (match) key = match.section + "." + match.key;
      }
      if (!key) key = field;
      map[key] = item.message;
    });
    return map;
  }

  function validateSettings() {
    var s = app.settings;
    if (!s) return;
    if (s.validating) { s.pendingValidation = true; return; }
    s.validating = true;
    s.validationSnapshot = JSON.stringify(s.draft);
    updateSaveState();

    var values = {};
    Object.keys(s.draft).forEach(function (key) { values[key] = s.draft[key]; });

    return requestJson("POST", "/api/settings/validate", { values: values }).then(function (data) {
      if (app.settings !== s) return;
      if (JSON.stringify(s.draft) !== s.validationSnapshot) {
        s.pendingValidation = true;
        return;
      }
      s.valid = data && data.valid === false ? false : true;
      s.validationDiagnostic = null;
      if (s.valid) {
        s.preview = typeof data.preview === "string" ? data.preview : "";
        s.warnings = normalizeWarnings(data.warnings);
        s.fieldErrors = {};
        settingsFields().forEach(function (field) { field.error = null; });
        if (Array.isArray(data.fields)) {
          data.fields.forEach(function (field) {
            if (field && field.error) {
              var key = field.section && field.key ? field.section + "." + field.key : (field.key || "");
              if (key) s.fieldErrors[key] = field.error;
            }
          });
        }
      } else {
        s.preview = "";
      }
      s.error = null;
    }).catch(function (err) {
      if (app.settings !== s) return;
      if (JSON.stringify(s.draft) !== s.validationSnapshot) {
        s.pendingValidation = true;
        return;
      }
      if (err instanceof OfflineError) {
        s.valid = null;
        s.error = err;
      } else if (err.fields && err.fields.length) {
        s.valid = false;
        settingsFields().forEach(function (field) { field.error = null; });
        s.validationDiagnostic = { code: err.code, status: err.status, message: err.message, detail: err.detail };
        s.fieldErrors = fieldErrorMap(err.fields);
        s.preview = "";
        s.error = null;
      } else {
        s.valid = false;
        s.preview = "";
        s.error = err;
      }
    }).finally(function () {
      if (app.settings !== s) return;
      s.validating = false;
      updateSettingsMeta();
      renderSettingsErrors();
      updateSaveState();
      if (s.pendingValidation) {
        s.pendingValidation = false;
        scheduleValidate(120);
      }
    });
  }

  function updateSettingsMeta() {
    var s = app.settings;
    if (!s) return;

    settingsFields().forEach(function (field) {
      var key = field.section + "." + field.key;
      var row = $('.field-row[data-field="' + key.replace(/"/g, '\\"') + '"]');
      if (row) {
        var sourceEl = $('[data-testid="field-source"]', row);
        if (sourceEl) {
          sourceEl.textContent = SOURCE_CN[field.source] || field.source || "默认";
          sourceEl.dataset.source = field.source || "default";
        }
        var fileB = $(".m-file b", row);
        if (fileB) fileB.textContent = fmtValue(field.file_value);
        var effB = $(".m-eff b", row);
        if (effB) {
          effB.textContent = fmtValue(field.effective_value);
          effB.classList.toggle("is-empty", field.effective_value === null || field.effective_value === undefined);
        }
        row.dataset.shadowed = field.source === "env" ? "true" : "false";
      }

      var errorEl = $('[data-testid="field-error"][data-field="' + key.replace(/"/g, '\\"') + '"]');
      var input = $('[data-testid="field-input"][data-field="' + key.replace(/"/g, '\\"') + '"]');
      var message = s.fieldErrors[key] || field.error || "";
      if (errorEl) {
        errorEl.textContent = message;
        errorEl.hidden = !message;
      }
      if (input) {
        if (message) input.setAttribute("aria-invalid", "true");
        else input.removeAttribute("aria-invalid");
      }
    });

    var summaryHost = $(".summary-panel");
    if (summaryHost) {
      var replacement = document.createElement("div");
      replacement.innerHTML = settingsSummaryHtml();
      summaryHost.replaceWith(replacement.firstElementChild);
    }

    var warningsHost = $(".warnings-panel");
    var warningsHtml = settingsWarningsHtml();
    if (warningsHost) {
      if (warningsHtml) {
        var holder = document.createElement("div");
        holder.innerHTML = warningsHtml;
        warningsHost.replaceWith(holder.firstElementChild);
      } else {
        warningsHost.remove();
      }
    } else if (warningsHtml) {
      var form = $("#settings-form");
      if (form) form.insertAdjacentHTML("beforeend", warningsHtml);
    }

    var preview = $("#settings-preview");
    if (preview) {
      if (s.preview) {
        preview.textContent = s.preview;
        preview.dataset.state = "ready";
        preview.classList.remove("is-stale");
      } else {
        preview.textContent = s.valid === false
          ? "存在校验错误，无法生成预览。修正后再保存。"
          : "校验通过后在这里显示将写入的 TOML（服务端精确输出）。";
        preview.dataset.state = "empty";
      }
    }
    var previewHint = $("#preview-hint");
    if (previewHint) {
      previewHint.textContent = s.preview ? "服务端精确输出" : "校验通过后生成";
    }

    updateSaveState();
  }

  function updateSaveState() {
    var s = app.settings;
    if (!s) return;
    var btn = $("#settings-save");
    var stateEl = $("#save-state");
    var textEl = $("#save-state-text");
    var hint = $("#save-hint");
    var dirtyEl = $("#save-dirty");
    var baseEl = $("#save-base");
    var repair = $('[data-testid="repair-action"]');
    var force = $('[data-act="force"]');

    var dirty = isSettingsDirty();
    var dirtyCount = dirtyFieldCount();
    var needsForce = !!(s.error && s.error.code === "cannot_rewrite");
    var loadError = !!s.loadError;

    if (btn) {
      btn.hidden = loadError;
      btn.disabled = s.saving || loadError || s.valid !== true;
      btn.textContent = s.saving ? "保存中…" : "保存";
    }
    if (repair) {
      repair.hidden = !loadError;
      repair.disabled = s.saving || s.valid !== true;
      repair.textContent = s.saving ? "写入中…" : "修复并写入";
    }
    if (force) {
      force.hidden = !needsForce;
      force.disabled = s.saving || s.valid !== true;
    }

    var label;
    var kind;
    if (s.loadError) {
      label = "配置文件无法加载 · 使用修复并写入";
      kind = "false";
    } else if (s.validating) {
      label = "校验中…";
      kind = "pending";
    } else if (s.valid === true) {
      label = dirty ? "校验通过 · 有未保存的修改" : "校验通过 · 与文件一致";
      kind = "true";
    } else if (s.valid === false) {
      label = "校验未通过 · 保存已禁用";
      kind = "false";
    } else {
      label = "尚未校验 · 保存已禁用";
      kind = "pending";
    }
    if (s.conflict && !s.loadError && s.valid === true) {
      label = "文件已被其他进程修改 · 请再次保存";
      kind = "pending";
    }
    if (stateEl) stateEl.dataset.valid = kind;
    if (textEl) textEl.textContent = label;
    if (hint) hint.textContent = dirty ? dirtyCount + " 项修改" : "未修改";
    if (dirtyEl) {
      dirtyEl.textContent = dirty ? dirtyCount + " 项修改" : "未修改";
      dirtyEl.className = dirty ? "dirty" : "";
    }
    if (baseEl) baseEl.textContent = shortSha(s.baseSha);

    var diag = $("#save-diag");
    if (diag) {
      var info = s.validationDiagnostic;
      diag.hidden = !info;
      if (info) {
        var head = [];
        if (info.code) head.push(info.code + (info.status ? " · HTTP " + info.status : ""));
        if (info.message) head.push(String(info.message));
        if (info.detail) head.push(String(info.detail));
        var pre = $("#save-diag-pre");
        if (pre) pre.textContent = head.join("\n\n");
      }
    }
  }

  function renderSettingsErrors() {
    var s = app.settings;
    var box = $("#settings-error");
    if (!s || !box) return;
    var parts = [];

    if (s.loadError) {
      parts.push('<div class="error-block" role="alert">' +
        '<div class="error-head"><span class="error-mark" aria-hidden="true">!</span>' +
        '<span class="error-title">配置文件无法加载</span>' +
        '<span class="error-code mono">settings_load_error</span></div>' +
        '<p class="error-hint">subgen.toml 当前无法被加载，普通保存已禁用。先填写 <span class="mono">source_root</span> 与 ' +
        '<span class="mono">work_root</span>，再在「保存」面板选择「修复并写入」：它会用表单中的值重写整个文件，' +
        '并先把原文件备份为 <span class="mono">subgen.toml.bak</span>；文件里的注释与顺序可能丢失。</p>' +
        '<details class="error-diag"><summary>诊断信息（英文原文）</summary><pre>' +
        esc(typeof s.loadError === "string" ? s.loadError : JSON.stringify(s.loadError, null, 2)) +
        "</pre></details></div>");
    }

    if (s.conflict) {
      parts.push('<div class="notice"><span class="notice-mark" aria-hidden="true">!</span>' +
        "<div><strong>配置文件已被修改。</strong>保存期间 subgen.toml 被其他进程改动。已重新载入最新文件，" +
        "表单里的修改仍然保留；确认后请再次保存。</div></div>");
    }

    if (s.error && !(s.loadError && s.error.code === "settings_load_error")) {
      parts.push(errorHtml(s.error));
    }

    box.innerHTML = parts.join("");
    box.hidden = parts.length === 0;
    $$(".failure-tail pre", box).forEach(function (pre) {
      pre.scrollTop = pre.scrollHeight;
    });
  }

  function reloadSettingsPreservingDraft() {
    var previous = app.settings;
    var draft = previous ? previous.draft : null;
    return requestJson("GET", "/api/settings").then(function (view) {
      var next = makeSettingsState(view);
      if (draft) next.draft = Object.assign({}, next.draft, draft);
      app.settings = next;
      renderSettings();
      return validateSettings();
    });
  }

  function loadSettings(options) {
    var opts = options || {};
    if (app.settingsLoading) return Promise.resolve();
    app.settingsLoading = true;
    if (!opts.background) renderSettingsLoading();

    return requestJson("GET", "/api/settings").then(function (view) {
      app.settings = makeSettingsState(view);
      renderSettings();
      refreshMeta();
      return validateSettings();
    }).catch(function (err) {
      app.settings = null;
      var body = $("#settings-body");
      if (body) {
        body.innerHTML = errorHtml(err) +
          '<p class="empty-action"><button class="btn" type="button" data-act="reload-settings">重新加载</button></p>';
      }
      if (err instanceof OfflineError) setConnection(false, err);
    }).finally(function () {
      app.settingsLoading = false;
    });
  }

  function saveSettings(force) {
    var s = app.settings;
    if (!s || s.saving) return;
    if (s.valid !== true) {
      if (force) toast("请先通过校验再强制写入", "error");
      return;
    }
    if (s.loadError && !force) {
      toast("配置文件无法加载，请使用「修复并写入」", "error");
      return;
    }

    s.saving = true;
    s.conflict = false;
    updateSaveState();

    var repairing = !!s.loadError;
    var payload = {
      values: s.draft,
      base_sha256: s.baseSha
    };
    if (force) payload.force = true;

    requestJson("PUT", "/api/settings", payload).then(function () {
      s.saving = false;
      var message = repairing
        ? "已修复并写入 subgen.toml（原文件备份为 subgen.toml.bak）"
        : force
          ? "已强制写入规范 TOML（原文件备份为 subgen.toml.bak）"
          : "已保存到 subgen.toml（原文件备份为 subgen.toml.bak）";
      s.validationDiagnostic = null;
      toast(message, "success");
      announce("参数设置已保存");
      return loadSettings({ background: true });
    }).catch(function (err) {
      s.saving = false;

      if (err instanceof OfflineError) {
        s.error = err;
        setConnection(false, err);
        renderSettingsErrors();
        updateSaveState();
        return;
      }

      if (err.code === "settings_changed") {
        s.conflict = true;
        toast("配置文件已被修改，已重新载入最新内容", "error", "settings_changed");
        reloadSettingsPreservingDraft().then(function () {
          var current = app.settings;
          if (current) {
            current.conflict = true;
            renderSettingsErrors();
            updateSaveState();
          }
        });
        return;
      }

      if (err.code === "cannot_rewrite") {
        s.error = err;
        renderSettingsErrors();
        updateSaveState();
        return;
      }

      if (err.code === "settings_invalid" && err.fields && err.fields.length) {
        s.valid = false;
        s.fieldErrors = fieldErrorMap(err.fields);
        s.error = new ApiError({
          status: err.status,
          code: "draft_invalid",
          message: err.message,
          detail: err.detail,
          fields: err.fields
        });
      } else {
        s.error = err;
      }
      renderSettingsErrors();
      updateSettingsMeta();
      updateSaveState();
    });
  }


  function parseRoute() {
    var hash = location.hash.replace(/^#/, "");
    if (!hash || hash === "/") return { name: "jobs", id: null };
    var detailMatch = /^\/jobs\/([^/]+)\/?$/.exec(hash);
    if (detailMatch) {
      var id;
      try { id = decodeURIComponent(detailMatch[1]); } catch (err) { id = detailMatch[1]; }
      return { name: "detail", id: id };
    }
    if (/^\/jobs\/?$/.test(hash)) return { name: "jobs", id: null };
    if (/^\/settings\/?$/.test(hash)) return { name: "settings", id: null };
    return null;
  }

  function updateNav(name) {
    $$("[data-nav]").forEach(function (link) {
      var isCurrent = (name === "settings" ? link.dataset.nav === "settings" : link.dataset.nav === "jobs");
      if (isCurrent) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  function handleRoute() {
    var route = parseRoute();
    if (!route) {
      if (location.hash !== "#/jobs") {
        location.replace("#/jobs");
        return;
      }
      route = { name: "jobs", id: null };
    }

    var previous = app.route;
    app.route = route;

    if (previous.name === "detail" && route.name !== "detail") {
      stopDetailPoll();
      app.detail = null;
    }

    $("#jobs-view").hidden = route.name !== "jobs";
    $("#job-detail").hidden = route.name !== "detail";
    $("#settings-view").hidden = route.name !== "settings";

    updateNav(route.name === "settings" ? "settings" : "jobs");
    window.scrollTo(0, 0);

    if (route.name === "jobs") {
      document.title = "作业 · subgen 控制台";
      if (!app.jobsLoaded) refreshJobs();
      else { renderJobList(); syncCreateLock(); }
    } else if (route.name === "detail") {
      document.title = "作业 " + route.id + " · subgen 控制台";
      openDetail(route.id);
    } else if (route.name === "settings") {
      document.title = "参数设置 · subgen 控制台";
      loadSettings();
    }
  }


  function updateClock() {
    var el = $("#conn-clock");
    if (!el) return;
    el.textContent = fmtClock(Date.now() / 1000);
  }

  function onVisibilityChange() {
    renderPollState();
    if (document.hidden) {
      clearTimeout(app.detailTimer);
      return;
    }
    refreshMeta();
    scheduleMeta(META_INTERVAL);
    if (app.route.name === "jobs" && app.jobsLoaded) refreshJobs({ quiet: true });
    if (app.route.name === "detail" && app.detail && isActive(app.detail.record)) {
      scheduleDetail(50);
    }
  }

  function bindStaticEvents() {
    $("#create-form").addEventListener("submit", function (event) {
      event.preventDefault();
      submitCreate();
    });

    $("#list-refresh").addEventListener("click", function () {
      refreshMeta();
      refreshJobs();
    });

    $("#job-list").addEventListener("click", function (event) {
      var button = event.target.closest("[data-action]");
      if (!button) return;
      var jobId = button.dataset.job;
      if (!jobId) return;
      var action = button.dataset.action;
      if (action === "cancel") cancelJob(jobId);
      else if (action === "rerun") rerunJob(jobId, false);
      else if (action === "delete") deleteJob(jobId);
    });

    $("#job-detail").addEventListener("click", function (event) {
      var actionEl = event.target.closest("[data-act]");
      if (!actionEl) return;
      if (actionEl.dataset.act === "reload-detail" && app.route.name === "detail") {
        openDetail(app.route.id);
      }
    });

    $("#settings-body").addEventListener("click", function (event) {
      var actionEl = event.target.closest("[data-act]");
      if (!actionEl) return;
      var action = actionEl.dataset.act;
      if (action === "repair") saveSettings(true);
      else if (action === "force") saveSettings(true);
      else if (action === "reload-settings") loadSettings();
    });

    window.addEventListener("hashchange", handleRoute);
    document.addEventListener("visibilitychange", onVisibilityChange);
  }

  function boot() {
    bindStaticEvents();
    updateClock();
    app.clockTimer = setInterval(updateClock, 1000);

    if (!location.hash) {
      try {
        history.replaceState(null, "", "#/jobs");
      } catch (err) {
        location.hash = "#/jobs";
      }
    }

    handleRoute();
    refreshMeta();
    scheduleMeta(META_INTERVAL);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();