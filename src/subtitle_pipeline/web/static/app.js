/* No build step and no external assets: the console works offline. */
(function () {
  "use strict";


  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));

  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (character) => ESCAPES[character]);
  }

  function pad2(value) {
    return String(value).padStart(2, "0");
  }

  const numberFormatter = new Intl.NumberFormat("zh-CN");

  function fmtCount(value) {
    const number = Number(value);
    return Number.isFinite(number) ? numberFormatter.format(number) : "—";
  }

  function toSeconds(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function fmtDateTime(epoch) {
    const seconds = toSeconds(epoch);
    if (seconds == null) return "";
    const date = new Date(seconds * 1000);
    return `${date.getMonth() + 1}月${date.getDate()}日 ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  }

  function fmtClock(epoch) {
    const seconds = toSeconds(epoch);
    if (seconds == null) return "";
    const date = new Date(seconds * 1000);
    return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  }

  function toEpoch(value) {
    if (typeof value === "number") return value > 1e12 ? value / 1000 : value;
    if (typeof value === "string" && value) {
      const parsed = Date.parse(value);
      if (!Number.isNaN(parsed)) return parsed / 1000;
      const numeric = Number(value);
      if (Number.isFinite(numeric)) return numeric;
    }
    return null;
  }

  function fmtDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    if (total < 60) return `${total} 秒`;
    const minutes = Math.floor(total / 60);
    if (minutes < 60) {
      const rest = total % 60;
      return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
    }
    const hours = Math.floor(minutes / 60);
    const restMinutes = minutes % 60;
    return restMinutes ? `${hours} 小时 ${restMinutes} 分` : `${hours} 小时`;
  }

  function jobTitle(epoch) {
    const date = fmtDateTime(epoch);
    return date ? `${date} 的任务` : "任务";
  }

  function basename(path) {
    if (!path) return "";
    const parts = String(path).split("/");
    return parts[parts.length - 1] || "";
  }

  function clampPercent(done, total) {
    if (!total || total <= 0) return null;
    return Math.min(100, Math.max(0, Math.round((Number(done || 0) / Number(total)) * 100)));
  }

  function newToken() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return `job-${Date.now()}-${Math.random().toString(16).slice(2, 12)}`;
  }

  function escapeRegExp(text) {
    return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }


  class ApiError extends Error {
    constructor(options) {
      super(options.message || "request failed");
      this.status = options.status || 0;
      this.code = options.code || "error";
      this.fields = options.fields || {};
      this.detail = options.detail || "";
    }

    get offline() {
      return this.status === 0;
    }
  }

  async function api(method, path, body) {
    const init = {
      method,
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    };
    if (method !== "GET" && method !== "HEAD") {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body === undefined ? {} : body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      throw new ApiError({ status: 0, code: "offline", message: "无法连接本地服务" });
    }
    let text = "";
    try {
      text = await response.text();
    } catch (error) {
      throw new ApiError({ status: 0, code: "offline", message: "读取响应时连接断开" });
    }
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (error) {
        if (response.ok) {
          throw new ApiError({ status: response.status, code: "bad_json", message: "响应不是 JSON" });
        }
      }
    }
    if (!response.ok) {
      const envelope = data && data.error ? data.error : {};
      throw new ApiError({
        status: response.status,
        code: envelope.code || `http_${response.status}`,
        message: envelope.message || "请求没有成功",
        fields: envelope.fields || {},
        detail: envelope.detail || "",
      });
    }
    return data;
  }

  function diagnostic(error) {
    if (!error) return "";
    const lines = [`${error.code || "error"} (HTTP ${error.status || 0})`];
    if (error.message) lines.push(error.message);
    if (error.detail) lines.push(error.detail);
    const fieldKeys = Object.keys(error.fields || {});
    if (fieldKeys.length) {
      lines.push(fieldKeys.map((key) => `${key}: ${error.fields[key]}`).join("\n"));
    }
    return lines.join("\n");
  }

  function friendlyError(error) {
    if (!error) return "出了点问题，可以稍后重试。";
    if (error.offline) return "连不上本地服务。确认它还在运行，页面会自动重试。";
    switch (error.code) {
      case "job_not_found":
        return "这个任务不存在，可能已经被删除。";
      case "job_running":
        return "已经有一个任务在运行，等它结束再开始新的。";
      case "job_not_finished":
        return "任务还在运行，结束以后才能这样做。";
      case "settings_invalid":
        return "设置还没有准备好，先去设置里补全文件夹和翻译服务。";
      case "settings_load_error":
        return "配置文件现在读不懂，需要先修复。";
      case "cannot_rewrite":
        return "这份配置文件没法原地修改，需要用修复方式重写。";
      case "settings_changed":
        return "配置文件在别处被改动过。重新读取后再保存。";
      case "restart_required":
        return "文件夹改过了，需要重启一次本地服务才能生效。";
      case "external_activity":
        return "工作目录刚被别的程序用过，确认后可以继续。";
      case "invalid_params":
        return "有些参数不合适，检查后再试一次。";
      case "invalid_settings":
        return "有几项设置需要修改。";
      case "job_store_error":
        return "任务没有保存成功，可以稍后重试。";
      case "not_found":
        return "这个功能在当前版本里还不可用。";
      default:
        return error.status >= 500 ? "本地服务出错了，可以稍后重试。" : "没有成功，请检查后重试。";
    }
  }


  const STAGES = [
    { id: "inventory", label: "检查视频", phrase: "检查视频", phase: "prepare" },
    { id: "extract", label: "提取音频", phrase: "提取音频", phase: "prepare" },
    { id: "asr", label: "识别语音", phrase: "识别语音", phase: "recognize" },
    { id: "alignment", label: "对齐时间", phrase: "对齐时间", phase: "recognize" },
    { id: "translation_primary", label: "翻译成中文", phrase: "翻译成中文", phase: "translate" },
    { id: "translation_review", label: "校对译文", phrase: "校对译文", phase: "translate" },
    { id: "publish", label: "写入字幕", phrase: "写入字幕", phase: "done" },
  ];

  const PHASES = [
    { id: "prepare", name: "准备", detail: "检查视频、提取音频", stages: ["inventory", "extract"] },
    { id: "recognize", name: "识别", detail: "语音识别、对齐时间", stages: ["asr", "alignment"] },
    { id: "translate", name: "翻译", detail: "翻译成中文、校对", stages: ["translation_primary", "translation_review"] },
    { id: "done", name: "完成", detail: "写入字幕文件", stages: ["publish"] },
  ];

  const STAGE_BY_ID = Object.fromEntries(STAGES.map((stage) => [stage.id, stage]));

  const STAGE_ALIASES = {
    corpus_asr: "asr",
    corpus_alignment: "alignment",
    corpus_translation_primary: "translation_primary",
    corpus_translation_review: "translation_review",
    corpus_publish: "publish",
    primary: "translation_primary",
    review: "translation_review",
  };

  const STAGE_HEADLINE = {
    inventory: "正在检查视频",
    extract: "正在提取音频",
    asr: "正在识别语音",
    alignment: "正在对齐时间",
    translation_primary: "正在翻译成中文",
    translation_review: "正在校对译文",
    publish: "正在写入字幕",
  };

  const STAGE_STATUS_LABEL = {
    pending: "等待",
    running: "进行中",
    succeeded: "完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断",
    skipped: "已跳过",
  };

  const EVENT_LABEL = {
    observed: "发现视频",
    completed: "清单完成",
    skipped_existing: "已有字幕，跳过",
    skipped_no_audio: "没有音频，跳过",
    video_completed: "视频完成",
    batch_completed: "一批完成",
    batch_failed: "一批失败",
    batch_deferred: "推迟处理",
    batch_incomplete: "这批没有做完",
    skipped_verified: "已校验，跳过",
    video_existing: "已有字幕，跳过",
  };

  const JOB_STATES = {
    queued: { label: "准备开始", tone: "warm" },
    running: { label: "进行中", tone: "running" },
    succeeded: { label: "已完成", tone: "success" },
    failed: { label: "未完成", tone: "danger" },
    cancelled: { label: "已取消", tone: "muted" },
    interrupted: { label: "已中断", tone: "muted" },
    dry_run: { label: "试运行", tone: "dry" },
  };

  function normalizeStage(name) {
    return STAGE_ALIASES[name] || name || "";
  }

  function stageLabel(name) {
    const stage = STAGE_BY_ID[normalizeStage(name)];
    return stage ? stage.label : String(name || "");
  }

  function stagePhrase(name) {
    const stage = STAGE_BY_ID[normalizeStage(name)];
    return stage ? stage.phrase : String(name || "");
  }

  function phaseForStage(name) {
    const stage = STAGE_BY_ID[normalizeStage(name)];
    return stage ? PHASES.find((phase) => phase.id === stage.phase) : null;
  }

  function jobStateMeta(state) {
    return JOB_STATES[state] || { label: state || "未知", tone: "muted" };
  }

  function phaseStatus(job, phase) {
    if (job.state === "dry_run") return "pending";
    const stages = (job.stages || []).filter((stage) => phase.stages.includes(normalizeStage(stage.name)));
    const statuses = stages.map((stage) => stage.status);
    if (statuses.includes("failed")) return "failed";
    if (statuses.includes("running")) return "running";
    if (statuses.includes("cancelled")) return "cancelled";
    if (statuses.includes("interrupted")) return "interrupted";
    if (statuses.length && statuses.every((status) => status === "succeeded" || status === "skipped")) {
      return statuses.some((status) => status === "succeeded") ? "succeeded" : "skipped";
    }
    if (statuses.some((status) => status === "succeeded")) return "running";
    return "pending";
  }

  function phaseStateText(job, status) {
    if (job.state === "dry_run") return "仅预览";
    switch (status) {
      case "running":
        return "正在进行";
      case "succeeded":
        return "已完成";
      case "failed":
        return "在这里停下";
      case "cancelled":
        return "已取消";
      case "interrupted":
        return "已中断";
      case "skipped":
        return "没有进行到";
      default:
        return "还没有开始";
    }
  }

  function markSvg(status) {
    if (status === "succeeded") {
      return '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.4 8.4l3 3 6.2-6.6"/></svg>';
    }
    if (status === "failed") {
      return '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.6 4.6l6.8 6.8M11.4 4.6l-6.8 6.8"/></svg>';
    }
    if (status === "cancelled" || status === "interrupted") {
      return '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 8h8"/></svg>';
    }
    return "";
  }


  const state = {
    route: { name: "jobs" },
    meta: null,
    metaError: null,
    online: null,
    jobs: [],
    jobsLoaded: false,
    jobsError: null,
    jobsBusy: false,
    jobsSignature: "",
    knownJobIds: new Set(),
    activeJobId: null,
    detailJobId: null,
    detailBusy: false,
    detailError: null,
    detailTimer: null,
    metaTimer: null,
    settings: null,
    settingsLoaded: false,
    settingsError: null,
    settingsDraft: {},
    settingsDirty: false,
    settingsBusy: false,
    settingsBaseSha: "",
    settingsSavedAt: null,
    settingsReason: "",
    createBusy: false,
    createOpen: false,
    confirmResolve: null,
    ackResolve: null,
    browsePath: null,
    browseParent: null,
    browseLoaded: false,
    lastPlacesKey: "",
  };

  const detailUI = {
    job: null,
    stageSignature: "",
    railSignature: "",
    eventKeys: new Set(),
    logText: "",
    latestPath: "",
    progressOffset: 0,
    logOffset: 0,
    logPinned: true,
    mounted: false,
  };


  let toastTimer = null;

  function toast(text, tone) {
    const element = $("#toast");
    if (!element) return;
    element.textContent = text;
    element.dataset.tone = tone || "info";
    element.dataset.visible = "true";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      element.dataset.visible = "false";
    }, 3600);
  }

  function announce(text) {
    const live = $("#sr-status");
    if (live) live.textContent = text;
  }

  function renderService() {
    const element = $("#service-status");
    if (!element) return;
    const text = $(".service-text", element);
    if (state.online === null) {
      element.dataset.state = "pending";
      if (text) text.textContent = "正在连接本地服务";
      element.removeAttribute("title");
      return;
    }
    if (state.online) {
      element.dataset.state = "online";
      if (text) text.textContent = "本地服务正常";
      element.title = state.meta && state.meta.server ? `subgen ${state.meta.server.version}` : "";
      return;
    }
    element.dataset.state = "offline";
    if (text) text.textContent = "无法连接本地服务";
    element.removeAttribute("title");
  }

  function renderOffline(error) {
    const banner = $("#offline-banner");
    if (!banner) return;
    const offline = state.online === false;
    banner.hidden = !offline;
    banner.title = offline && error ? diagnostic(error) : "";
    const detail = $("#offline-detail");
    if (detail) detail.textContent = "";
  }

  function setNav(section) {
    $$("[data-nav]").forEach((link) => {
      if (link.dataset.nav === section) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  function closeMenus(except) {
    $$(".menu[open]").forEach((menu) => {
      if (menu !== except) menu.removeAttribute("open");
    });
  }


  async function refreshMeta() {
    try {
      const meta = await api("GET", "/api/meta");
      const recovered = state.online === false;
      state.meta = meta;
      state.metaError = null;
      state.online = true;
      renderService();
      renderOffline(null);
      if (recovered) {
        toast("本地服务已恢复");
        announce("本地服务已恢复");
      }
      if (state.route.name === "jobs") {
        renderSetup();
        renderJobsNotice();
      }
      syncPlaces();
    } catch (error) {
      state.online = false;
      state.metaError = error;
      renderService();
      renderOffline(error);
      if (state.route.name === "jobs") renderSetup();
    }
  }

  function scheduleMeta() {
    clearTimeout(state.metaTimer);
    state.metaTimer = setTimeout(async () => {
      if (!document.hidden) {
        await refreshMeta();
        if (state.route.name === "jobs" && state.online) await refreshJobs(true);
      }
      scheduleMeta();
    }, 5000);
  }

  function setupInfo() {
    return state.meta && state.meta.setup ? state.meta.setup : null;
  }

  function setupReady(setup) {
    return Boolean(setup && setup.source_exists && setup.translation_configured);
  }

  function setupCompleteCount(setup) {
    if (!setup) return 0;
    let count = 0;
    if (setup.source_exists) count += 1;
    if (setup.translation_configured) count += 1;
    if (setup.asr_model_exists) count += 1;
    return count;
  }

  function setupMissingLabels(setup) {
    const missing = [];
    if (!setup.source_exists) missing.push("视频文件夹");
    if (!setup.translation_configured) missing.push("翻译服务");
    if (!setup.asr_model_exists) missing.push("模型文件");
    return missing;
  }

  function setSetupRow(prefix, stateText, tone, value) {
    const stateElement = $(`#setup-${prefix}-state`);
    const valueElement = $(`#setup-${prefix}-note` ) || $(`#setup-${prefix}-path`);
    if (stateElement) {
      stateElement.textContent = stateText;
      stateElement.dataset.tone = tone;
    }
    if (valueElement) {
      valueElement.textContent = value;
      if (valueElement.classList.contains("mono")) valueElement.title = value;
    }
    const item = $(`[data-testid="setup-item"][data-item="${prefix === "source" ? "source" : prefix === "translation" ? "translation" : "models"}"]`);
    if (item) item.dataset.state = tone;
  }

  function renderSetup() {
    const count = $("#setup-count");
    const setup = setupInfo();
    const offline = state.online === false;

    if (!setup) {
      const stateText = offline ? "读取失败" : "正在读取";
      const tone = offline ? "bad" : "muted";
      const value = offline ? "本地服务没有响应" : "正在读取设置…";
      setSetupRow("source", stateText, tone, value);
      setSetupRow("translation", stateText, tone, value);
      setSetupRow("models", stateText, tone, value);
      if (count) {
        count.textContent = offline ? "等待本地服务" : "正在读取设置…";
        count.dataset.tone = offline ? "bad" : "";
      }
      return;
    }

    if (!setup.source_root) {
      setSetupRow("source", "还没选择", "warn", "还没有选择文件夹");
    } else if (!setup.source_exists) {
      setSetupRow("source", "文件夹不存在", "bad", setup.source_root);
    } else {
      setSetupRow("source", "已选择", "ok", setup.source_root);
    }

    if (!setup.translation_configured) {
      setSetupRow("translation", "还没填写", "warn", "还没有填写服务地址和模型");
    } else if (!setup.api_key_configured) {
      setSetupRow("translation", "已填写，但没有检测到 API key", "warn", "服务地址已经填写，密钥需要单独设置");
    } else {
      setSetupRow("translation", "已填写", "ok", "服务地址和模型已填写，API key 已检测到");
    }

    if (setup.asr_model_exists) {
      setSetupRow("models", "已找到", "ok", setup.asr_model || setup.model_root || "模型文件已准备好");
    } else {
      setSetupRow("models", "没有找到", "warn", "试运行不需要模型文件，真实运行前需要准备");
    }

    if (count) {
      const done = setupCompleteCount(setup);
      count.textContent = `已完成 ${done} / 3`;
      count.dataset.tone = done === 3 ? "ok" : "warn";
    }
  }

  function renderJobsNotice() {
    const notice = $("#jobs-setup-notice");
    if (!notice) return;
    const setup = setupInfo();
    if (!setup || !state.jobs.length) {
      notice.hidden = true;
      return;
    }
    const missing = setupMissingLabels(setup);
    if (!missing.length) {
      notice.hidden = true;
      return;
    }
    notice.hidden = false;
    const text = $("#jobs-setup-notice-text");
    if (text) text.textContent = `还有 ${missing.length} 项没有准备好：${missing.join("、")}。真实运行前先补上，试运行不受影响。`;
  }


  function parseRoute() {
    const raw = location.hash.replace(/^#/, "").replace(/^\/+/, "");
    if (!raw || raw === "jobs") return { name: "jobs" };
    if (raw === "settings" || raw.startsWith("settings")) return { name: "settings" };
    const match = raw.match(/^jobs\/(.+)$/);
    if (match) {
      let id = match[1];
      try {
        id = decodeURIComponent(id);
      } catch (error) {
        id = match[1];
      }
      return { name: "detail", id };
    }
    return { name: "jobs" };
  }

  function handleRoute() {
    const next = parseRoute();
    const previous = state.route;
    const changed = next.name !== previous.name || next.id !== previous.id;
    state.route = next;

    $("#jobs-view").hidden = next.name !== "jobs";
    $("#job-detail").hidden = next.name !== "detail";
    $("#settings-view").hidden = next.name !== "settings";

    if (next.name === "jobs") {
      setNav("jobs");
      document.title = "任务 · subgen 字幕工坊";
      stopDetailPolling();
      renderJobsView();
      refreshJobs();
    } else if (next.name === "settings") {
      setNav("settings");
      document.title = "设置 · subgen 字幕工坊";
      stopDetailPolling();
      showSettingsView();
      if (!state.settingsLoaded) loadSettings();
    } else {
      setNav("jobs");
      document.title = "任务详情 · subgen 字幕工坊";
      renderJobsView();
      showDetailView(next.id);
    }

    if (changed) {
      window.scrollTo({ top: 0, behavior: "auto" });
      closeMenus();
    }
  }


  function mountCreateButton(mode) {
    const button = $("#create-open");
    if (!button) return;
    if (mode === "hero") {
      const actions = $("#hero-actions");
      if (actions && button.parentElement !== actions) actions.prepend(button);
      button.textContent = "开始生成字幕";
    } else {
      const action = $("#list-action");
      if (action && button.parentElement !== action) action.appendChild(button);
      button.textContent = "新建任务";
    }
  }

  function renderJobsView() {
    const boot = $("#jobs-boot");
    const empty = $("#jobs-empty");
    const live = $("#jobs-live");
    const errorCard = $("#jobs-error");
    if (!empty || !live) return;

    const hasJobs = state.jobs.length > 0;
    const loading = !state.jobsLoaded && !state.jobsError;

    if (boot) boot.hidden = !loading;
    empty.hidden = loading || hasJobs;
    live.hidden = loading || !hasJobs;

    if (errorCard) {
      const showError = Boolean(state.jobsError) && !state.jobsError.offline && state.online !== false;
      errorCard.hidden = !showError;
      if (showError) {
        const copy = $("#jobs-error-copy");
        if (copy) copy.textContent = friendlyError(state.jobsError);
        const tech = $("#jobs-error-tech");
        if (tech) tech.textContent = diagnostic(state.jobsError);
      }
    }

    if (!hasJobs) {
      mountCreateButton("hero");
      renderSetup();
      return;
    }

    mountCreateButton("list");
    renderJobList();
    renderJobsNotice();
  }

  function renderJobList() {
    const list = $("#job-list");
    const count = $("#jobs-count");
    if (!list) return;
    const jobs = state.jobs;
    if (count) {
      const active = jobs.filter((job) => job.state === "running" || job.state === "queued").length;
      count.textContent = active ? `共 ${jobs.length} 个任务 · ${active} 个进行中` : `共 ${jobs.length} 个任务`;
    }

    const signature = jobs
      .map((job) => {
        const progress = job.progress || {};
        return [job.job_id, job.state, job.current_stage, progress.done, progress.total, job.updated_at].join(":");
      })
      .join("|");
    if (signature === state.jobsSignature) {
      renderJobsNotice();
      return;
    }
    state.jobsSignature = signature;

    const freshIds = [];
    const html = jobs.map((job, index) => {
      const fresh = !state.knownJobIds.has(job.job_id);
      if (fresh) freshIds.push(job.job_id);
      return jobRowHtml(job, index, fresh);
    });
    list.innerHTML = html.join("");
    jobs.forEach((job) => state.knownJobIds.add(job.job_id));
    if (freshIds.length > 6) state.jobsSignature = signature;
    renderJobsNotice();
  }

  function jobRowHtml(job, index, fresh) {
    const stateInfo = jobStateMeta(job.state);
    const active = job.state === "running" || job.state === "queued";
    const progress = job.progress || {};
    const percent = clampPercent(progress.done, progress.total);
    const bar =
      active
        ? `<div class="job-bar" ${percent == null ? 'data-indeterminate="1"' : ""} role="progressbar" aria-label="任务进度" ${
            percent == null ? "" : `aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100"`
          }><span style="width:${percent == null ? 34 : percent}%"></span></div>`
        : "";

    const menuItems = active
      ? '<button class="menu-item" type="button" data-action="cancel">取消任务</button>'
      : `<button class="menu-item" type="button" data-action="rerun">重跑</button>
         <button class="menu-item menu-item--danger" type="button" data-action="delete">删除任务</button>`;

    return `<li class="job-card" data-testid="job-row" data-job-id="${esc(job.job_id)}" data-state="${esc(job.state)}" ${
      fresh ? `data-fresh="1" style="--i:${Math.min(index, 6)}"` : ""
    }>
      <div class="job-card-main">
        <div class="job-card-top">
          <h3 class="job-card-title"><a href="#/jobs/${encodeURIComponent(job.job_id)}">${esc(jobTitle(job.created_at))}</a></h3>
          <span class="pill" data-tone="${esc(stateInfo.tone)}" data-testid="job-row-state">${esc(stateInfo.label)}</span>
        </div>
        <p class="job-line">${esc(jobLine(job))}</p>
        ${bar}
      </div>
      <div class="job-card-actions">
        <a class="btn btn--quiet btn--small" href="#/jobs/${encodeURIComponent(job.job_id)}">查看</a>
        <details class="menu">
          <summary>更多</summary>
          <div class="menu-panel">${menuItems}</div>
        </details>
      </div>
    </li>`;
  }

  function jobLine(job) {
    const progress = job.progress || {};
    if (job.state === "dry_run") return `试运行，已生成 ${STAGES.length} 条命令`;
    if (job.state === "queued") return "正在准备开始";
    if (job.state === "running") {
      const phase = phaseForStage(job.current_stage);
      const step = phase ? PHASES.indexOf(phase) + 1 : 1;
      const doing = stagePhrase(job.current_stage) || (phase ? phase.name : "处理中");
      if (progress.done != null && progress.total) {
        return `正在进行第 ${step} 步，${doing} · 已处理 ${fmtCount(progress.done)} / ${fmtCount(progress.total)} 个视频`;
      }
      return `正在进行第 ${step} 步，${doing}`;
    }
    if (job.state === "succeeded") return `已生成 ${fmtCount(progress.done || 0)} 个字幕`;
    if (job.state === "failed") return "这次没有完成，打开详情可以看原因";
    if (job.state === "cancelled") return "已取消，已经生成的字幕保留";
    if (job.state === "interrupted") return "运行被中断，可以重跑";
    return "—";
  }

  async function refreshJobs(quiet) {
    if (state.route.name !== "jobs" || state.jobsBusy) return;
    state.jobsBusy = true;
    try {
      const data = await api("GET", "/api/jobs");
      if (state.route.name !== "jobs") return;
      state.jobs = Array.isArray(data.jobs) ? data.jobs : [];
      state.activeJobId = data.active_job_id || null;
      state.jobsLoaded = true;
      state.jobsError = null;
      renderJobsView();
    } catch (error) {
      if (state.route.name !== "jobs") return;
      if (error.offline) {
        state.online = false;
        renderService();
        renderOffline(error);
      }
      state.jobsError = error;
      if (!state.jobsLoaded) {
        state.jobsLoaded = true;
        state.jobs = [];
      }
      renderJobsView();
    } finally {
      state.jobsBusy = false;
    }
  }

  async function refreshJobsNow() {
    await refreshJobs(false);
  }


  async function rerunJob(jobId) {
    try {
      const job = await api("POST", `/api/jobs/${encodeURIComponent(jobId)}/rerun`, {
        client_token: newToken(),
      });
      toast("已经开始重跑");
      announce("已经开始重跑");
      await refreshJobsNow();
      location.hash = `#/jobs/${encodeURIComponent(job.job_id)}`;
    } catch (error) {
      if (error.code === "external_activity") {
        const acknowledged = await askExternal(error.detail);
        if (acknowledged) {
          try {
            const job = await api("POST", `/api/jobs/${encodeURIComponent(jobId)}/rerun`, {
              client_token: newToken(),
              acknowledge_external: true,
            });
            toast("已经开始重跑");
            await refreshJobsNow();
            location.hash = `#/jobs/${encodeURIComponent(job.job_id)}`;
          } catch (retryError) {
            toast(friendlyError(retryError), "error");
          }
        }
        return;
      }
      toast(friendlyError(error), "error");
    }
  }

  async function deleteJob(jobId) {
    const confirmed = await confirmAction({
      kicker: "删除任务",
      title: "删除这个任务？",
      body: "只会删除任务记录，视频和已经生成的字幕不会被删掉。",
      confirmLabel: "删除",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await api("DELETE", `/api/jobs/${encodeURIComponent(jobId)}`);
      toast("任务已删除");
      announce("任务已删除");
      if (state.detailJobId === jobId) {
        state.detailJobId = null;
        location.hash = "#/jobs";
      } else {
        await refreshJobsNow();
      }
    } catch (error) {
      toast(friendlyError(error), "error");
    }
  }

  async function cancelJob(jobId) {
    const confirmed = await confirmAction({
      kicker: "取消任务",
      title: "取消这次任务？",
      body: "已经生成的字幕会保留，任务会停在这里。",
      confirmLabel: "取消任务",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await api("POST", `/api/jobs/${encodeURIComponent(jobId)}/cancel`, {});
      toast("任务已取消");
      announce("任务已取消");
      await refreshJobsNow();
      if (state.route.name === "detail" && state.detailJobId === jobId) {
        await pollDetail();
      }
    } catch (error) {
      toast(friendlyError(error), "error");
    }
  }


  function showDetailView(jobId) {
    const view = $("#job-detail");
    if (!view) return;
    view.hidden = false;
    if (state.detailJobId === jobId && detailUI.mounted) {
      startDetailPolling();
      return;
    }
    stopDetailPolling();
    detailUI.locked = false;
    state.detailJobId = jobId;
    state.detailError = null;
    resetDetail();
    openDetail(jobId);
  }

  function resetDetail() {
    detailUI.job = null;
    detailUI.stageSignature = "";
    detailUI.railSignature = "";
    detailUI.eventKeys = new Set();
    detailUI.logText = "";
    detailUI.latestPath = "";
    detailUI.progressOffset = 0;
    detailUI.logOffset = 0;
    detailUI.logPinned = true;
    detailUI.mounted = false;

    $("#detail-loading").hidden = false;
    $("#detail-content").hidden = true;
    $("#detail-error").hidden = true;
    const eventTail = $("#event-tail");
    if (eventTail) eventTail.innerHTML = "";
    const stageBody = $("#stage-body");
    if (stageBody) stageBody.innerHTML = "";
    const phaseRail = $("#phase-rail");
    if (phaseRail) phaseRail.innerHTML = "";
    const log = $("#log-panel");
    if (log) log.textContent = "";
  }

  async function openDetail(jobId) {
    const requested = jobId;
    detailUI.locked = true;
    try {
      const data = await api(
        "GET",
        `/api/jobs/${encodeURIComponent(jobId)}/progress?since=0&log_since=0`
      );
      if (state.detailJobId !== requested) return;
      state.detailError = null;
      detailUI.progressOffset = Number(data.next_offset) || 0;
      detailUI.logOffset = Number(data.next_log_offset) || 0;
      detailUI.mounted = true;
      $("#detail-loading").hidden = true;
      $("#detail-content").hidden = false;
      $("#detail-error").hidden = true;
      const seeded = storedEvents(data.job).concat(Array.isArray(data.events) ? data.events : []);
      ingestEvents(seeded, true, data.job);
      applyDetail(data.job, true);
      applyLog(data.log || "", true);
      startDetailPolling();
    } catch (error) {
      if (state.detailJobId !== requested) return;
      showDetailError(error);
    } finally {
      if (state.detailJobId === requested) detailUI.locked = false;
    }
  }

  function showDetailError(error) {
    stopDetailPolling();
    state.detailError = error;
    if (error.offline) {
      state.online = false;
      renderService();
      renderOffline(error);
    }
    $("#detail-loading").hidden = true;
    $("#detail-content").hidden = true;
    const card = $("#detail-error");
    card.hidden = false;
    const title = $("#detail-error-title");
    const copy = $("#detail-error-copy");
    if (error.code === "job_not_found") {
      if (title) title.textContent = "这个任务不存在";
      if (copy) copy.textContent = "它可能已经被删除。回到任务列表看看最新的记录。";
    } else {
      if (title) title.textContent = "这个任务没有读出来";
      if (copy) copy.textContent = friendlyError(error);
    }
    const tech = $("#detail-error-tech");
    if (tech) tech.textContent = diagnostic(error);
  }

  async function pollDetail() {
    if (!state.detailJobId || detailUI.locked || state.route.name !== "detail") return;
    const requested = state.detailJobId;
    detailUI.locked = true;
    try {
      const data = await api(
        "GET",
        `/api/jobs/${encodeURIComponent(requested)}/progress?since=${detailUI.progressOffset}&log_since=${detailUI.logOffset}`
      );
      if (state.detailJobId !== requested) return;
      detailUI.progressOffset = Number(data.next_offset) || detailUI.progressOffset;
      detailUI.logOffset = Number(data.next_log_offset) || detailUI.logOffset;
      ingestEvents(Array.isArray(data.events) ? data.events : [], false, data.job);
      applyDetail(data.job, false);
      applyLog(data.log || "", Boolean(data.log_reset));
    } catch (error) {
      if (state.detailJobId !== requested) return;
      if (error.code === "job_not_found") {
        showDetailError(error);
        return;
      }
      if (error.offline) {
        state.online = false;
        renderService();
        renderOffline(error);
      }
    } finally {
      detailUI.locked = false;
    }
  }

  async function detailTick() {
    state.detailTimer = null;
    if (document.hidden || !state.detailJobId || state.route.name !== "detail") return;
    await pollDetail();
    if (document.hidden || !state.detailJobId || state.route.name !== "detail") return;
    state.detailTimer = setTimeout(detailTick, 1000);
  }

  function startDetailPolling(delay) {
    stopDetailPolling();
    if (document.hidden || !state.detailJobId) return;
    state.detailTimer = setTimeout(detailTick, typeof delay === "number" ? delay : 1000);
  }

  function stopDetailPolling() {
    clearTimeout(state.detailTimer);
    state.detailTimer = null;
  }

  function progressOf(job) {
    const summary = state.jobs.find((item) => item.job_id === job.job_id);
    if (summary && summary.progress && summary.progress.exact) {
      return { ...summary.progress, exact: true };
    }
    const stages = job.stages || [];
    const current = stages[job.current];
    if (current && current.counters) {
      const counters = current.counters;
      const done = counters.completed != null ? counters.completed : counters.count;
      const total = counters.selected != null ? counters.selected : counters.count;
      return { done, total, failed: counters.failed || 0, exact: true };
    }
    if (job.state === "running" && current) {
      const rows = liveRowsFor(job, current.name);
      const done = rows.filter((row) => isCompletionEvent(row.event)).length;
      let total = null;
      for (let index = rows.length - 1; index >= 0; index -= 1) {
        const row = rows[index];
        if (row.selected != null) {
          total = Number(row.selected);
          break;
        }
        if (row.processable != null) {
          total = Number(row.processable);
          break;
        }
      }
      return { done, total, failed: rows.filter((row) => isFailureEvent(row.event)).length, exact: false };
    }
    return { done: null, total: null, failed: 0, exact: false };
  }

  function liveRowsFor(job, stageName) {
    const target = normalizeStage(stageName);
    const rows = [];
    for (const stage of job.stages || []) {
      if (normalizeStage(stage.name) !== target) continue;
      for (const row of stage.events || []) rows.push(row);
    }
    return rows;
  }

  function isCompletionEvent(name) {
    return new Set([
      "completed",
      "video_completed",
      "batch_completed",
      "skipped_existing",
      "skipped_no_audio",
      "skipped_verified",
      "video_existing",
    ]).has(name);
  }

  function isFailureEvent(name) {
    return String(name || "").includes("failed");
  }

  function applyDetail(job, initial) {
    if (!job) return;
    const previousState = detailUI.job ? detailUI.job.state : null;
    detailUI.job = job;
    detailUI.latestPath = lastPath(job) || detailUI.latestPath;

    if (initial) {
      detailUI.mounted = true;
    }

    const title = $("#detail-title");
    if (title) title.textContent = jobTitle(job.created_at);
    document.title = `${jobTitle(job.created_at)} · subgen 字幕工坊`;

    const stateElement = $("#detail-state");
    const stateInfo = jobStateMeta(job.state);
    if (stateElement) {
      stateElement.textContent = stateInfo.label;
      stateElement.dataset.tone = stateInfo.tone;
    }

    const idText = $("#detail-id-text");
    if (idText) idText.textContent = job.job_id;
    const copyButton = $("#detail-copy");
    if (copyButton) copyButton.dataset.jobId = job.job_id;

    renderProgress(job);
    renderRail(job);
    renderStages(job);
    renderFailure(job);
    renderActions(job);

    if (previousState && previousState !== job.state) {
      announce(`任务${stateInfo.label}`);
      refreshJobsNow();
    }
  }

  function renderProgress(job) {
    const card = $(".progress-card");
    if (card) card.dataset.state = job.state;
    const progress = progressOf(job);
    const kicker = $("#progress-kicker");
    const title = $("#progress-title");
    const sub = $("#progress-sub");
    const meta = $("#progress-meta");
    const track = $("#progress-track");
    const fill = $("#progress-fill");
    const latest = $("#progress-latest");
    const cancel = $("#cancel-button");

    const elapsed = Math.max(0, (job.ended_at || Date.now() / 1000) - (job.created_at || Date.now() / 1000));
    const currentStage = (job.stages || [])[job.current];
    const phase = currentStage ? phaseForStage(currentStage.name) : null;

    let kickerText = "任务状态";
    let titleText = "任务";
    let subText = "";
    let metaText = "";
    let showTrack = false;
    let indeterminate = false;
    let percent = null;

    if (job.state === "dry_run") {
      kickerText = "试运行";
      titleText = "试运行完成";
      subText = "没有处理任何视频";
      metaText = "这次只检查了将要执行的命令，视频文件没有被改动。";
    } else if (job.state === "running" || job.state === "queued") {
      kickerText = "正在进行";
      titleText = job.state === "queued" ? "正在准备任务" : STAGE_HEADLINE[normalizeStage(currentStage && currentStage.name)] || "正在处理";
      if (progress.done != null && progress.total) {
        subText = `已处理 ${fmtCount(progress.done)} / ${fmtCount(progress.total)} 个视频`;
      } else if (phase) {
        subText = `正在进行第 ${PHASES.indexOf(phase) + 1} 步，${phase.name}`;
      } else {
        subText = "正在开始";
      }
      if (progress.failed) subText += ` · ${fmtCount(progress.failed)} 个失败`;
      metaText = `已用 ${fmtDuration(elapsed)}`;
      showTrack = true;
      percent = clampPercent(progress.done, progress.total);
      indeterminate = percent == null;
    } else if (job.state === "succeeded") {
      kickerText = "已完成";
      titleText = "字幕已生成";
      subText = `已生成 ${fmtCount(progress.done != null ? progress.done : 0)} 个字幕`;
      metaText = `用时 ${fmtDuration(elapsed)} · 字幕就在每个视频旁边，文件名是视频名加 .zh.srt。`;
      showTrack = true;
      percent = 100;
    } else if (job.state === "failed") {
      kickerText = "任务停止";
      titleText = "这次没有完成";
      const failedStage = (job.stages || []).find((stage) => stage.status === "failed") || currentStage;
      const failedPhase = failedStage ? phaseForStage(failedStage.name) : null;
      subText = failedStage
        ? `在第 ${failedPhase ? PHASES.indexOf(failedPhase) + 1 : 1} 步「${failedPhase ? failedPhase.name : "处理"}」时失败了。`
        : "任务停在了中间。";
      metaText = `用时 ${fmtDuration(elapsed)}`;
      percent = clampPercent(progress.done, progress.total);
      showTrack = Boolean(progress.total) && (percent == null || percent > 0);
    } else if (job.state === "cancelled") {
      kickerText = "已取消";
      titleText = "任务已取消";
      subText = "已经停下，已经生成的字幕保留。";
      metaText = `已用 ${fmtDuration(elapsed)}`;
      showTrack = Boolean(progress.total);
      percent = clampPercent(progress.done, progress.total);
    } else if (job.state === "interrupted") {
      kickerText = "已中断";
      titleText = "任务已中断";
      subText = "运行没有正常结束，可以重跑一次。";
      metaText = `已用 ${fmtDuration(elapsed)}`;
    }

    if (kicker) kicker.textContent = kickerText;
    if (title) title.textContent = titleText;
    if (sub) sub.textContent = subText;
    if (meta) meta.textContent = metaText;

    if (track && fill) {
      track.hidden = !showTrack;
      track.dataset.indeterminate = indeterminate ? "1" : "0";
      fill.style.width = indeterminate ? "34%" : `${percent == null ? 0 : percent}%`;
    }

    if (latest) {
      const latestPath = detailUI.latestPath;
      const showLatest = (job.state === "running" || job.state === "queued") && latestPath;
      latest.hidden = !showLatest;
      if (showLatest) latest.textContent = `正在处理 ${basename(latestPath)}`;
    }

    if (cancel) cancel.hidden = !(job.state === "running" || job.state === "queued");
  }

  function renderRail(job) {
    const rail = $("#phase-rail");
    if (!rail) return;
    const cells = PHASES.map((phase, index) => {
      const status = phaseStatus(job, phase);
      return { phase, index, status, text: phaseStateText(job, status) };
    });
    const signature = cells.map((cell) => `${cell.phase.id}:${cell.status}`).join("|");
    if (signature === detailUI.railSignature && rail.children.length) return;
    detailUI.railSignature = signature;
    rail.innerHTML = cells
      .map(
        (cell) => `<li class="phase-cell" data-testid="phase-cell" data-phase="${esc(cell.phase.id)}" data-status="${esc(cell.status)}">
          <span class="phase-mark" aria-hidden="true">${markSvg(cell.status)}</span>
          <p class="phase-kicker">第 ${cell.index + 1} 步</p>
          <h3 class="phase-name">${esc(cell.phase.name)}</h3>
          <p class="phase-detail">${esc(cell.phase.detail)}</p>
          <p class="phase-state">${esc(cell.text)}</p>
        </li>`
      )
      .join("");
  }

  function renderStages(job) {
    const body = $("#stage-body");
    if (!body) return;
    const stages = job.stages || [];
    const signature = stages
      .map((stage) => `${stage.name}:${stage.status}:${stage.exit_code}:${JSON.stringify(stage.counters)}`)
      .join("|");
    if (signature === detailUI.stageSignature && body.children.length) return;
    detailUI.stageSignature = signature;
    body.innerHTML = stages
      .map((stage) => {
        const argv = (stage.argv || []).join(" ");
        return `<tr data-testid="stage-cell" data-stage="${esc(stage.name)}" data-status="${esc(stage.status)}">
          <td><span class="stage-name">${esc(stageLabel(stage.name))}</span><span class="stage-tech">${esc(stage.name)}</span></td>
          <td><span class="stage-state" data-status="${esc(stage.status)}">${esc(STAGE_STATUS_LABEL[stage.status] || stage.status)}</span></td>
          <td><span class="stage-exit">${stage.exit_code == null ? "—" : esc(stage.exit_code)}</span></td>
          <td><span class="stage-count">${esc(counterText(stage.counters))}</span></td>
          <td><div class="stage-argv" title="${esc(argv)}">${esc(argv || "—")}</div></td>
        </tr>`;
      })
      .join("");
  }

  function counterText(counters) {
    if (!counters) return "—";
    const parts = [];
    const done = counters.completed != null ? counters.completed : counters.count;
    const total = counters.selected != null ? counters.selected : counters.count;
    if (done != null && total != null) parts.push(`完成 ${fmtCount(done)} / ${fmtCount(total)}`);
    else if (done != null) parts.push(`完成 ${fmtCount(done)}`);
    if (counters.failed) parts.push(`失败 ${fmtCount(counters.failed)}`);
    return parts.length ? parts.join(" · ") : "—";
  }

  function renderFailure(job) {
    const card = $("#job-failure");
    if (!card) return;
    if (job.state !== "failed") {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const failedStage = (job.stages || []).find((stage) => stage.status === "failed") || (job.stages || [])[job.current];
    const cause = failureCause(job, failedStage);
    const title = $("#failure-title");
    if (title) title.textContent = cause.title;
    const hint = $("#job-error-hint");
    if (hint) hint.innerHTML = cause.hintHtml;
    const line = $("#job-failure-line");
    if (line) line.textContent = "修好以后可以重跑，已经生成的字幕会保留。";
    updateFailureLog(job, failedStage);
  }

  function failureCause(job, failedStage) {
    const parts = [
      job.error || "",
      failedStage && failedStage.error ? failedStage.error : "",
      failedStage && failedStage.events
        ? failedStage.events.map((event) => event.reason || "").join(" ")
        : "",
    ];
    const text = (parts.join(" ") + " " + lastLines(detailUI.logText || "", 80)).toLowerCase();
    if (/model|checkpoint|weights/.test(text) && /not a directory|not found|missing|unavailable|no such/.test(text)) {
      return {
        title: "没有找到语音模型",
        hintHtml:
          '在 <a href="#/settings" data-settings-focus="models">设置 → 模型与运行</a> 里放好模型文件，或者先试运行确认命令。',
      };
    }
    if (/source root|source_root|folder/.test(text) && /does not exist|not found|no such|missing/.test(text)) {
      return {
        title: "视频文件夹不见了",
        hintHtml:
          '它可能被移动或删除了。在 <a href="#/settings" data-settings-focus="source">设置 → 视频文件夹</a> 里重新选一个存在的文件夹。',
      };
    }
    if (/endpoint|translation|api key|unauthorized|connection refused|connecterror|timeout|11434|11436/.test(text)) {
      return {
        title: "翻译服务没有响应",
        hintHtml:
          '确认服务已经启动，并检查 <a href="#/settings" data-settings-focus="translation">设置 → 翻译服务</a> 里的地址和 API key 环境变量。',
      };
    }
    if (/ffmpeg|audio extraction|extract failed|failed to extract|extraction failed/.test(text)) {
      return {
        title: "音频提取失败",
        hintHtml: "确认这台机器上装了 ffmpeg，并且它能读取这个视频文件。",
      };
    }
    return {
      title: "这次没有完成",
      hintHtml: "可以打开「处理细节」查看技术原因，或者修好设置后重跑一次。",
    };
  }

  function updateFailureLog(job, failedStage) {
    const panel = $("#failure-log");
    if (!panel) return;
    const lines = detailUI.logText ? lastLines(detailUI.logText, 80) : "";
    const raw = [job.error || "", failedStage && failedStage.error ? failedStage.error : ""]
      .filter(Boolean)
      .join("\n");
    panel.textContent = lines || raw || "这次没有留下日志。";
  }

  function lastLines(text, count) {
    const lines = String(text).replace(/\s+$/, "").split("\n");
    return lines.slice(Math.max(0, lines.length - count)).join("\n");
  }

  function renderActions(job) {
    const rerun = $("#rerun-button");
    const remove = $("#delete-button");
    const actions = $("#detail-actions");
    const failure = $("#job-failure");
    const rail = $(".rail-card");
    const terminal = ["succeeded", "failed", "cancelled", "interrupted", "dry_run"].includes(job.state);
    if (rerun) rerun.hidden = !terminal;
    if (remove) remove.hidden = !terminal;
    if (!actions) return;
    actions.hidden = !terminal;
    if (job.state === "failed" && failure) {
      const logBlock = $("details.tech", failure);
      if (actions.parentElement !== failure) failure.insertBefore(actions, logBlock);
    } else if (rail && actions.previousElementSibling !== rail) {
      rail.after(actions);
    }
  }

  function lastPath(job) {
    const rows = storedEvents(job);
    for (let index = rows.length - 1; index >= 0; index -= 1) {
      if (rows[index].path) return rows[index].path;
    }
    return "";
  }

  function storedEvents(job) {
    const rows = [];
    for (const stage of job.stages || []) {
      for (const event of stage.events || []) {
        rows.push({ ...event, stage: event.stage || stage.name });
      }
    }
    return rows;
  }

  function eventKey(row) {
    return [row.timestamp, row.stage, row.event, row.path, row.video_id, row.reason].join("|");
  }

  function eventText(row) {
    const label = EVENT_LABEL[row.event] || row.event || "";
    const bits = [label];
    const file = row.path ? basename(row.path) : "";
    if (file) bits.push(file);
    else if (row.count != null) bits.push(`${fmtCount(row.count)} 个视频`);
    else if (row.selected != null) bits.push(`${fmtCount(row.selected)} 个视频`);
    if (row.reason) bits.push(String(row.reason).slice(0, 72));
    return bits.join(" · ");
  }

  function ingestEvents(rows, initial, job) {
    const tail = $("#event-tail");
    if (!tail) return;
    if (job) detailUI.job = job;
    if (initial) {
      detailUI.eventKeys = new Set();
      tail.innerHTML = "";
    }
    const sorted = initial
      ? rows.slice().sort((left, right) => (toEpoch(left.timestamp) || 0) - (toEpoch(right.timestamp) || 0))
      : rows;

    const nearBottom = tail.scrollHeight - tail.scrollTop - tail.clientHeight < 40;
    let added = 0;
    const fragment = document.createDocumentFragment();
    for (const row of sorted) {
      const key = eventKey(row);
      if (detailUI.eventKeys.has(key)) continue;
      detailUI.eventKeys.add(key);
      added += 1;
      if (row.path) detailUI.latestPath = row.path;
      const item = document.createElement("li");
      item.className = "event-row";
      item.innerHTML = `<span class="event-time">${esc(fmtClock(toEpoch(row.timestamp)))}</span>
        <span class="event-stage">${esc(stageLabel(row.stage))}</span>
        <span class="event-text" title="${esc(eventText(row))}">${esc(eventText(row))}</span>`;
      fragment.appendChild(item);
    }
    if (!added) return;
    tail.appendChild(fragment);
    while (tail.children.length > 240) tail.removeChild(tail.firstElementChild);
    const empty = $("#event-empty");
    if (empty) empty.hidden = tail.children.length > 0;
    if (initial || nearBottom) tail.scrollTop = tail.scrollHeight;
    if (detailUI.job) renderProgress(detailUI.job);
  }

  function applyLog(chunk, reset) {
    const panel = $("#log-panel");
    if (!panel) return;
    if (reset) detailUI.logText = "";
    if (chunk) detailUI.logText += chunk;
    if (detailUI.logText.length > 700000) {
      detailUI.logText = detailUI.logText.slice(-500000);
    }
    const empty = $("#log-empty");
    if (empty) empty.hidden = Boolean(detailUI.logText.trim());
    if (!detailUI.logText) {
      panel.textContent = "";
      return;
    }
    const text = lastLines(detailUI.logText, 400);
    if (panel.textContent === text) return;
    panel.textContent = text;
    if (detailUI.logPinned) panel.scrollTop = panel.scrollHeight;
    const job = detailUI.job;
    if (job && job.state === "failed") {
      renderFailure(job);
    }
  }


  function openCreate() {
    const setup = setupInfo();
    if (!state.meta) {
      toast(state.online === false ? "连不上本地服务，先确认服务还在运行" : "正在读取设置，请稍等");
      return;
    }
    if (!setupReady(setup)) {
      const missingSource = !setup || !setup.source_exists;
      toast("先把视频文件夹和翻译服务准备好", "warn");
      goToSettings(
        missingSource ? "source" : "translation",
        missingSource
          ? "先选好视频文件夹，再填写翻译服务，就可以开始生成字幕了。"
          : "先填写翻译服务，就可以开始生成字幕了。"
      );
      return;
    }

    resetCreateForm();
    renderCreateNotices();

    const modal = $("#create-modal");
    if (!modal) return;
    if (typeof modal.showModal === "function") modal.showModal();
    else modal.setAttribute("open", "");
    state.createOpen = true;
    const checked = $('input[name="scope"]:checked');
    if (checked) checked.focus();
  }

  function resetCreateForm() {
    const all = $("#create-scope-all");
    if (all) all.checked = true;
    const limit = $("#create-limit");
    if (limit) limit.value = "5";
    $("#create-dry-run").checked = false;
    $("#create-skip-inventory").checked = false;
    $("#create-skip-extract").checked = false;
    $("#create-asr-batch").value = "32";
    $("#create-align-batch").value = "16";
    $("#create-video-ids").value = "";
    const advanced = $("#create-advanced");
    if (advanced) advanced.open = false;
    hideCreateError();
    setCreateBusy(false);
    syncCreateChoice();
  }

  function syncCreateChoice() {
    $$("[data-choice]").forEach((choice) => {
      const input = $('input[name="scope"]', choice);
      choice.dataset.checked = input && input.checked ? "true" : "false";
    });
  }

  function renderCreateNotices() {
    const setup = setupInfo();
    const notice = $("#create-notice");
    const text = $("#create-notice-text");
    const notes = [];
    if (setup) {
      if (!setup.asr_model_exists) {
        notes.push("还没有找到模型文件。可以先试运行，真实运行前准备好模型。");
      }
      if (!setup.api_key_configured) {
        notes.push("没有检测到 API key。服务需要密钥的话，先在环境变量里设置。");
      }
      if (!setup.source_exists) notes.push("视频文件夹不可用，先去设置里重新选择。");
    } else {
      notes.push("设置没有读出来，创建任务可能会失败。");
    }
    if (notice && text) {
      notice.hidden = notes.length === 0;
      text.innerHTML = notes.map((note) => `<p>${esc(note)}</p>`).join("");
    }

    const active = (state.meta && state.meta.active_job_id) || state.activeJobId;
    const busy = $("#create-busy-notice");
    if (busy) busy.hidden = !active;
    const submit = $("#create-submit");
    if (submit) submit.disabled = Boolean(active) || state.createBusy;
    if (active) {
      const link = $("#create-busy-link");
      if (link) link.dataset.jobId = active;
    }
  }

  function closeCreate() {
    const modal = $("#create-modal");
    if (!modal) return;
    if (modal.open) modal.close();
    else modal.removeAttribute("open");
    state.createOpen = false;
  }

  function setCreateBusy(busy) {
    state.createBusy = busy;
    const submit = $("#create-submit");
    if (!submit) return;
    submit.disabled = busy || Boolean((state.meta && state.meta.active_job_id) || state.activeJobId);
    submit.textContent = busy ? "正在开始…" : "开始";
  }

  function showCreateError(text, error) {
    const box = $("#create-error");
    if (!box) return;
    const detail = error ? `<details class="tech"><summary>技术细节</summary><pre class="tech-pre">${esc(diagnostic(error))}</pre></details>` : "";
    box.innerHTML = `<p>${esc(text)}</p>${detail}`;
    box.hidden = false;
  }

  function hideCreateError() {
    const box = $("#create-error");
    if (!box) return;
    box.hidden = true;
    box.innerHTML = "";
  }

  function createErrorCopy(error) {
    const base = friendlyError(error);
    const fieldKeys = Object.keys(error.fields || {});
    if (fieldKeys.length && error.code === "invalid_params") {
      const labels = { limit: "处理数量", video_ids: "指定视频", asr_batch_size: "识别批量", alignment_batch_size: "对齐批量" };
      return `${base} 需要修改：${fieldKeys.map((key) => labels[key] || key).join("、")}。`;
    }
    return base;
  }

  async function submitCreate(acknowledge) {
    if (state.createBusy) return;
    hideCreateError();
    const scope = $('input[name="scope"]:checked');
    const scopeValue = scope ? scope.value : "all";
    let limit = null;
    if (scopeValue === "limit") {
      const value = Number.parseInt($("#create-limit").value, 10);
      if (!Number.isFinite(value) || value < 1) {
        showCreateError("先填写要处理的视频数量，至少 1 个。");
        $("#create-limit").focus();
        return;
      }
      limit = value;
    }
    const rawIds = $("#create-video-ids").value
      .split(/[\s,，]+/)
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean);
    const badId = rawIds.find((item) => !/^[0-9a-f]{64}$/.test(item));
    if (badId) {
      showCreateError("视频编号需要是 64 位十六进制字符，请检查后再试。");
      $("#create-video-ids").focus();
      return;
    }
    const params = {
      limit,
      video_ids: rawIds,
      skip_inventory: $("#create-skip-inventory").checked,
      skip_extract: $("#create-skip-extract").checked,
      asr_batch_size: positiveInt($("#create-asr-batch").value, 32),
      alignment_batch_size: positiveInt($("#create-align-batch").value, 16),
      dry_run: $("#create-dry-run").checked,
    };

    setCreateBusy(true);
    try {
      const job = await api("POST", "/api/jobs", {
        params,
        client_token: newToken(),
        acknowledge_external: Boolean(acknowledge),
      });
      setCreateBusy(false);
      closeCreate();
      toast(params.dry_run ? "试运行已开始" : "任务已开始");
      announce(params.dry_run ? "试运行已开始" : "任务已开始");
      await refreshJobsNow();
      location.hash = `#/jobs/${encodeURIComponent(job.job_id)}`;
    } catch (error) {
      if (error.code === "external_activity" && !acknowledge) {
        setCreateBusy(false);
        const acknowledged = await askExternal(error.detail);
        if (acknowledged) await submitCreate(true);
        return;
      }
      setCreateBusy(false);
      showCreateError(createErrorCopy(error), error);
      renderCreateNotices();
    }
  }

  function positiveInt(value, fallback) {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  }


  const FIELD_SPECS = {
    "paths.source_root": {
      label: "文件夹路径",
      kind: "path",
      placeholder: "例如 /Users/你的用户名/Movies",
      help: "subgen 会在这个文件夹及其子文件夹里寻找视频。字幕会写在同一位置，文件名是视频名加 .zh.srt。",
    },
    "paths.work_root": {
      label: "工作目录",
      kind: "path",
      help: "清单、音频和日志存放的位置。不确定就保持默认。",
    },
    "paths.model_root": {
      label: "模型文件夹",
      kind: "path",
      help: "模型文件所在的文件夹。只在真实运行时读取。",
    },
    "paths.asr_model": {
      label: "语音模型文件夹",
      kind: "path",
      help: "语音识别模型的具体文件夹。真实运行前准备好即可。",
    },
    "runtime.device": { label: "运行设备", kind: "enum" },
    "runtime.asr_engine": { label: "语音识别引擎", kind: "enum" },
    "translation.endpoint": {
      label: "服务地址",
      kind: "path",
      placeholder: "http://127.0.0.1:11434/v1",
      help: "兼容 OpenAI 接口的服务地址，结尾通常带 /v1。",
    },
    "translation.model": { label: "模型名", kind: "text", placeholder: "qwen2.5-32b-instruct" },
    "translation.revision": { label: "版本", kind: "text", placeholder: "2026-09" },
    "translation.api_key_env": {
      label: "API key 环境变量",
      kind: "text",
      placeholder: "OPENAI_API_KEY",
      help: "这里写环境变量的名字，不是密钥本身。",
    },
    "translation.review_model": { label: "校对模型", kind: "text", placeholder: "留空就沿用翻译阶段的模型" },
    "translation.review_revision": { label: "校对版本", kind: "text", placeholder: "留空就沿用翻译阶段的版本" },
  };

  const ENUM_LABELS = {
    auto: "自动选择",
    cpu: "CPU",
    cuda: "NVIDIA 显卡（CUDA）",
    mps: "Apple 芯片（MPS）",
    vllm: "vLLM",
    funasr: "FunASR",
  };

  const PLACE_LABELS = {
    home: "个人文件夹",
    movies: "影片",
    downloads: "下载",
    desktop: "桌面",
    documents: "文稿",
  };

  function showSettingsView() {
    const loading = $("#settings-loading");
    const form = $("#settings-form");
    const savebar = $("#savebar");
    const errorCard = $("#settings-error");
    if (state.settingsLoaded) {
      if (loading) loading.hidden = true;
      if (form) form.hidden = false;
      if (savebar) savebar.hidden = false;
      if (errorCard) errorCard.hidden = true;
    } else if (state.settingsError) {
      if (loading) loading.hidden = true;
      if (form) form.hidden = true;
      if (savebar) savebar.hidden = true;
      if (errorCard) errorCard.hidden = false;
    } else {
      if (loading) loading.hidden = false;
      if (form) form.hidden = true;
      if (savebar) savebar.hidden = true;
      if (errorCard) errorCard.hidden = true;
    }
  }

  async function loadSettings() {
    state.settingsError = null;
    state.settingsLoaded = false;
    showSettingsView();
    try {
      const data = await api("GET", "/api/settings");
      state.settings = data;
      state.settingsDraft = draftFromSettings(data);
      state.settingsBaseSha = data.base_sha256 || "";
      state.settingsLoaded = true;
      state.settingsDirty = false;
      state.settingsSavedAt = null;
      renderSettings();
      showSettingsView();
    } catch (error) {
      state.settingsError = error;
      state.settingsLoaded = false;
      showSettingsView();
      const copy = $("#settings-error-copy");
      if (copy) copy.textContent = friendlyError(error);
      const tech = $("#settings-error-tech");
      if (tech) tech.textContent = diagnostic(error);
      if (error.offline) {
        state.online = false;
        renderService();
        renderOffline(error);
      }
    }
  }

  function draftFromSettings(data) {
    const draft = {};
    for (const field of data.fields || []) {
      const key = `${field.section}.${field.key}`;
      draft[key] = typeof field.file_value === "string" ? field.file_value : "";
    }
    return draft;
  }

  function fieldRow(key) {
    if (!state.settings) return null;
    return (state.settings.fields || []).find((field) => `${field.section}.${field.key}` === key) || null;
  }

  function fieldValue(key) {
    return state.settingsDraft[key] != null ? state.settingsDraft[key] : "";
  }

  function fieldInputHtml(key) {
    const spec = FIELD_SPECS[key] || { label: key, kind: "text" };
    const row = fieldRow(key);
    const id = `field-${key.replace(/\./g, "-")}`;
    const value = fieldValue(key);
    let control = "";
    if (row && row.kind === "enum" && (row.choices || []).length) {
      const choices = row.choices
        .map((choice) => `<option value="${esc(choice)}" ${choice === value ? "selected" : ""}>${esc(ENUM_LABELS[choice] || choice)}</option>`)
        .join("");
      control = `<select class="input field-input" id="${id}" data-testid="field-input" data-field="${esc(key)}">
        <option value="" ${value === "" ? "selected" : ""}>未设置</option>${choices}</select>`;
    } else {
      const placeholder = state.settingsDraft[key] === "" && row && row.source === "env" ? "由环境变量控制" : spec.placeholder || "";
      const mono = spec.kind === "path" ? " mono" : "";
      control = `<input class="input field-input${mono}" id="${id}" data-testid="field-input" data-field="${esc(key)}" type="text" value="${esc(value)}" placeholder="${esc(placeholder)}" spellcheck="false" autocomplete="off">`;
    }
    const browse =
      key === "paths.source_root"
        ? '<button class="btn btn--quiet" type="button" id="browse-open" data-testid="browse-open">浏览…</button>'
        : "";
    const help = spec.help ? `<p class="field-help" id="${id}-help">${esc(spec.help)}</p>` : "";
    const shadowed = row && row.source === "env";
    const shadow = `<p class="field-shadow" id="${id}-shadow" ${shadowed ? "" : "hidden"}>当前由环境变量 ${esc(row ? row.env : "")} 控制，这里保存的是文件中的值。</p>`;
    const describedBy = [spec.help ? `${id}-help` : "", `${id}-error`, shadowed ? `${id}-shadow` : ""]
      .filter(Boolean)
      .join(" ");
    const errorText = row && row.error ? fieldErrorCopy(row.error, key) : "";
    const described = `aria-describedby="${describedBy}"`;
    control = control.replace(/^<(input|select)/, `<$1 ${described}`);
    return `<div class="field" data-field="${esc(key)}">
      <label class="field-label" for="${id}">${esc(spec.label)}</label>
      <div class="field-input-row">${control}${browse}</div>
      ${help}
      ${shadow}
      <p class="field-error" id="${id}-error" data-testid="field-error" data-field="${esc(key)}" ${errorText ? "" : "hidden"} ${errorText ? `title="${esc(row.error)}"` : ""}>${esc(errorText)}</p>
    </div>`;
  }

  function placeChipsHtml() {
    const places = state.meta && Array.isArray(state.meta.places) ? state.meta.places : [];
    if (!places.length) return "";
    return places
      .map(
        (place) =>
          `<button class="place" type="button" data-place="${esc(place.path)}">${esc(PLACE_LABELS[place.key] || place.name || basename(place.path))}</button>`
      )
      .join("");
  }

  function placesHtml() {
    const chips = placeChipsHtml();
    return `<div class="places" id="places" role="group" aria-label="快速选择文件夹" ${chips ? "" : "hidden"}>${chips}</div>`;
  }

  function renderSettings() {
    const data = state.settings;
    if (!data) return;
    const sections = $("#settings-sections");
    if (!sections) return;

    const cardSource = `<section class="card" id="card-source">
      <header class="card-head">
        <p class="card-kicker">第一步</p>
        <h2 class="card-title">视频文件夹</h2>
        <p class="card-lede">先选一个存有日语视频的文件夹。</p>
      </header>
      ${fieldInputHtml("paths.source_root")}
      ${placesHtml()}
      <div class="field-check-row">
        <button class="btn btn--quiet btn--small" type="button" id="check-source">检查文件夹</button>
        <span class="check-result" id="check-result" role="status"></span>
      </div>
    </section>`;

    const keyHint = data.api_key_configured
      ? '<p class="key-hint" data-tone="ok">已检测到 API key</p>'
      : '<p class="key-hint" data-tone="warn">没有检测到 API key</p>';

    const cardTranslation = `<section class="card" id="card-translation">
      <header class="card-head">
        <p class="card-kicker">第二步</p>
        <h2 class="card-title">翻译服务</h2>
        <p class="card-lede">任何兼容 OpenAI 接口的服务都可以。运行 <code class="mono">subgen serve</code> 也可以启动本地服务。</p>
      </header>
      ${fieldInputHtml("translation.endpoint")}
      <div class="field-row">
        ${fieldInputHtml("translation.model")}
        ${fieldInputHtml("translation.revision")}
      </div>
      ${fieldInputHtml("translation.api_key_env")}
      ${keyHint}
      <details class="fold-sub">
        <summary>校对小模型（可选）</summary>
        <div class="field-row">
          ${fieldInputHtml("translation.review_model")}
          ${fieldInputHtml("translation.review_revision")}
        </div>
      </details>
    </section>`;

    const cardRuntime = `<details class="card card--fold" id="card-runtime">
      <summary>
        <span class="fold-summary-copy">
          <span class="fold-summary-title">模型与运行</span>
          <span class="fold-summary-note">真实运行才需要，试运行可以留空</span>
        </span>
        <span class="fold-mark" aria-hidden="true"></span>
      </summary>
      <div class="card-body">
        <p class="card-lede">模型文件只在真实运行时读取。第一次可以先试运行，确认命令没问题。</p>
        ${fieldInputHtml("paths.model_root")}
        ${fieldInputHtml("paths.asr_model")}
        <div class="field-row">
          ${fieldInputHtml("runtime.device")}
          ${fieldInputHtml("runtime.asr_engine")}
        </div>
        ${fieldInputHtml("paths.work_root")}
      </div>
    </details>`;

    sections.innerHTML = cardSource + cardTranslation + cardRuntime;

    const repair = $("#settings-repair");
    if (repair) {
      const needsRepair = data.settings_ok === false;
      repair.hidden = !needsRepair;
      if (needsRepair) {
        const error = $("#repair-error");
        if (error) error.textContent = data.load_error || "";
        const repairPath = $("#repair-path");
        if (repairPath) repairPath.textContent = basename(data.config_path) || "subgen.toml";
      }
    }

    const notesElement = $("#settings-warning");
    if (notesElement) {
      const notes = Array.from(new Set((data.warnings || []).map((warning) => warningNote(warning))));
      notesElement.hidden = notes.length === 0;
      notesElement.innerHTML = notes.map((note) => `<p class="note">${esc(note)}</p>`).join("");
    }

    const configPath = $("#settings-config-path");
    if (configPath) configPath.textContent = data.config_path || "—";
    const sha = $("#settings-base-sha");
    if (sha) sha.textContent = data.base_sha256 ? data.base_sha256.slice(0, 12) : "—";
    const rawWarnings = $("#settings-raw-warnings");
    if (rawWarnings) {
      rawWarnings.innerHTML = (data.warnings || []).length
        ? `<pre class="tech-pre">${esc((data.warnings || []).join("\n"))}</pre>`
        : "";
    }

    setSettingsStatus(statusText(), statusTone());
    bindSettingsForm();
    setCheckResult("还没有检查", "muted");
    closePreview();

    if (state.settingsReason) {
      showSettingsNotice(state.settingsReason);
      state.settingsReason = "";
    }

    const pendingFocus = sessionStorage.getItem("subgen.settings.focus");
    if (pendingFocus) {
      sessionStorage.removeItem("subgen.settings.focus");
      setTimeout(() => focusSettingsTarget(pendingFocus), 0);
    }
  }

  function statusText() {
    if (state.settingsDirty) return "有未保存的修改";
    if (state.settings && state.settings.settings_ok === false) return "配置文件需要修复";
    if (state.settingsSavedAt) return `上次保存 ${fmtClock(state.settingsSavedAt)}`;
    return "设置已读取";
  }

  function statusTone() {
    if (state.settingsDirty) return "warn";
    if (state.settings && state.settings.settings_ok === false) return "warn";
    if (state.settingsSavedAt) return "ok";
    return "";
  }

  function setSettingsStatus(text, tone) {
    const element = $("#settings-status");
    if (!element) return;
    element.textContent = text;
    if (tone) element.dataset.tone = tone;
    else delete element.dataset.tone;
  }

  function bindSettingsForm() {
    const form = $("#settings-form");
    if (!form || form.dataset.bound === "1") return;
    form.dataset.bound = "1";
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      saveSettings(false);
    });
    form.addEventListener("input", onSettingsInput);
    form.addEventListener("change", onSettingsInput);
    form.addEventListener("click", (event) => {
      const check = event.target.closest("#check-source");
      if (check) {
        checkFolder();
        return;
      }
      const place = event.target.closest("[data-place]");
      if (place) {
        setSourceValue(place.dataset.place);
        return;
      }
      const browse = event.target.closest('[data-testid="browse-open"]');
      if (browse) {
        openBrowse();
      }
    });
  }

  function onSettingsInput(event) {
    const input = event.target.closest("[data-field]");
    if (!input) return;
    state.settingsDraft[input.dataset.field] = input.value;
    state.settingsDirty = true;
    setSettingsStatus(statusText(), statusTone());
    clearFieldError(input.dataset.field);
    hideSettingsNotice();
  }

  function setSourceValue(path) {
    const input = $('[data-testid="field-input"][data-field="paths.source_root"]');
    if (!input) return;
    input.value = path;
    state.settingsDraft["paths.source_root"] = path;
    state.settingsDirty = true;
    setSettingsStatus(statusText(), statusTone());
    clearFieldError("paths.source_root");
    input.focus();
    checkFolder();
  }

  function collectValues() {
    $$("[data-testid='field-input']").forEach((input) => {
      state.settingsDraft[input.dataset.field] = input.value;
    });
    return { ...state.settingsDraft };
  }

  function fieldErrorElement(key) {
    return $$('[data-testid="field-error"]').find((element) => element.dataset.field === key) || null;
  }

  function clearFieldError(key) {
    const element = fieldErrorElement(key);
    if (element) {
      element.hidden = true;
      element.textContent = "";
    }
    const input = $$('[data-testid="field-input"]').find((item) => item.dataset.field === key);
    if (input) input.removeAttribute("aria-invalid");
  }

  function clearAllFieldErrors() {
    $$('[data-testid="field-error"]').forEach((element) => {
      element.hidden = true;
      element.textContent = "";
    });
    $$('[data-testid="field-input"]').forEach((element) => element.removeAttribute("aria-invalid"));
  }

  function showFieldErrors(fields) {
    clearAllFieldErrors();
    let first = null;
    for (const key of Object.keys(fields || {})) {
      if (key === "_") continue;
      const element = fieldErrorElement(key);
      if (!element) continue;
      element.textContent = fieldErrorCopy(fields[key], key);
      element.title = fields[key];
      element.hidden = false;
      const input = $$('[data-testid="field-input"]').find((item) => item.dataset.field === key);
      if (input) input.setAttribute("aria-invalid", "true");
      if (!first) first = input || element;
    }
    if (first) {
      const fold = first.closest("details");
      if (fold) fold.open = true;
      first.scrollIntoView({ block: "center", behavior: "smooth" });
      if (typeof first.focus === "function") first.focus({ preventScroll: true });
    }
    const general = fields && (fields._ || fields.values);
    if (general) showSettingsNotice(fieldErrorCopy(general, "_"));
  }

  function fieldErrorCopy(raw, key) {
    const text = String(raw || "").toLowerCase();
    if (text.includes("root does not exist")) return "这个文件夹不存在，请重新选择。";
    if (text.includes("canonical absolute path")) return "请选择一个真实路径，不要用快捷方式。";
    if (text.includes("endpoint, model, and revision")) return "服务地址、模型名和版本需要一起填写。";
    if (text.includes("unknown settings field")) return "这一项已经不再使用。";
    if (text.includes("must be a string")) return "这一项需要填写文本。";
    if (text.includes("api key")) return "这个环境变量名需要是有效的名称。";
    if (text.includes("does not exist")) return "这个路径不存在，请检查后再保存。";
    return "这一项没有通过检查，请确认后重试。";
  }

  function warningNote(raw) {
    const text = String(raw || "").toLowerCase();
    if (text.startsWith("model root is not a directory") || text.startsWith("asr model root is not a directory")) {
      return "还没有找到模型文件。真实运行前需要在「模型与运行」里准备好，试运行可以跳过。";
    }
    if (text.includes("does not load")) return "现有配置文件读不懂，保存时需要选择修复。";
    if (text.includes("not utf-8")) return "配置文件不是 UTF-8 编码，保存时需要重写。";
    if (text.includes("cannot rewrite in place")) return "配置文件里有些格式没法原地修改，保存时会重写整个文件。";
    if (text.includes("cannot read the existing file")) return "现有配置文件读不到，保存时可能需要重写。";
    return "有一条设置提示，具体内容在技术细节里。";
  }

  async function saveSettings(force) {
    if (state.settingsBusy) return;
    const values = collectValues();
    setSettingsBusy(true);
    hideSettingsNotice();
    try {
      const data = await api("PUT", "/api/settings", {
        values,
        base_sha256: state.settingsBaseSha,
        force: Boolean(force),
      });
      state.settings = data;
      state.settingsDraft = draftFromSettings(data);
      state.settingsBaseSha = data.base_sha256 || "";
      state.settingsDirty = false;
      state.settingsSavedAt = Date.now() / 1000;
      state.settingsLoaded = true;
      renderSettings();
      setSettingsStatus(`上次保存 ${fmtClock(state.settingsSavedAt)}`, "ok");
      toast(force ? "配置文件已修复并保存" : "设置已保存");
      announce("设置已保存");
      renderSetup();
      if (state.meta) refreshMeta();
    } catch (error) {
      if (error.code === "invalid_settings" || error.code === "invalid_params") {
        showFieldErrors(error.fields || {});
        const count = Object.keys(error.fields || {}).filter((key) => key !== "_").length;
        setSettingsStatus(count ? `有 ${count} 项需要修改` : "有项目需要修改", "bad");
      } else if (error.code === "settings_load_error" || error.code === "cannot_rewrite") {
        showRepair(error);
      } else if (error.code === "settings_changed") {
        showSettingsNotice("配置文件在别处被改动过。重新读取设置后再保存。");
      } else {
        showSettingsNotice(friendlyError(error));
      }
      if (error.offline) {
        state.online = false;
        renderService();
        renderOffline(error);
      }
    } finally {
      setSettingsBusy(false);
    }
  }

  function showRepair(error) {
    const repair = $("#settings-repair");
    if (repair) repair.hidden = false;
    const tech = $("#repair-error");
    if (tech) tech.textContent = diagnostic(error);
    showSettingsNotice("现有配置文件没法直接保存，需要用「修复并保存」重写。");
  }

  function setSettingsBusy(busy) {
    state.settingsBusy = busy;
    const save = $("#settings-save");
    if (save) {
      save.disabled = busy;
      save.textContent = busy ? "正在保存…" : "保存设置";
    }
    const preview = $("#settings-preview");
    if (preview) preview.disabled = busy;
    const repair = $("#repair-action");
    if (repair) repair.disabled = busy;
  }

  function showSettingsNotice(text) {
    const notice = $("#settings-notice");
    if (!notice) return;
    notice.textContent = text;
    notice.hidden = !text;
    notice.className = "notice notice--warm";
  }

  function hideSettingsNotice() {
    const notice = $("#settings-notice");
    if (!notice) return;
    notice.hidden = true;
    notice.textContent = "";
  }

  function setCheckResult(text, tone) {
    const element = $("#check-result");
    if (!element) return;
    element.textContent = text;
    if (tone) element.dataset.tone = tone;
    else delete element.dataset.tone;
  }

  async function checkFolder() {
    const input = $('[data-testid="field-input"][data-field="paths.source_root"]');
    if (input && !input.value.trim()) {
      setCheckResult("先选一个文件夹", "bad");
      input.focus();
      return;
    }
    setCheckResult("正在检查…", "");
    try {
      await api("POST", "/api/settings/validate", { values: collectValues() });
      setCheckResult("可以使用", "ok");
      clearAllFieldErrors();
    } catch (error) {
      if (error.status === 422) {
        showFieldErrors(error.fields || {});
        if (error.fields && error.fields["paths.source_root"]) {
          setCheckResult("还不能使用", "bad");
        } else {
          setCheckResult("文件夹没问题，但还有其它设置需要修改", "warn");
        }
      } else {
        setCheckResult(friendlyError(error), "bad");
      }
    }
  }

  async function togglePreview() {
    const panel = $("#preview-panel");
    if (panel && !panel.hidden) {
      closePreview();
      return;
    }
    const button = $("#settings-preview");
    if (button) button.disabled = true;
    try {
      const data = await api("POST", "/api/settings/validate", { values: collectValues() });
      const panelElement = $("#preview-panel");
      const pre = $("#preview-pre");
      if (pre) pre.textContent = data.preview || "";
      const meta = $("#preview-meta");
      if (meta) {
        const sha = (state.settingsBaseSha || "").slice(0, 12);
        meta.textContent = `${state.settings && state.settings.config_path ? state.settings.config_path : ""}${sha ? ` · 配置版本 ${sha}` : ""}`;
      }
      if (panelElement) {
        panelElement.hidden = false;
        panelElement.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      if (button) button.textContent = "收起预览";
      clearAllFieldErrors();
      hideSettingsNotice();
      announce("已经显示将要写入的内容");
    } catch (error) {
      if (error.status === 422) {
        showFieldErrors(error.fields || {});
        showSettingsNotice("还有几项需要修改，修改后才能预览。");
      } else {
        showSettingsNotice(friendlyError(error));
      }
    } finally {
      if (button) button.disabled = false;
    }
  }

  function closePreview() {
    const panel = $("#preview-panel");
    if (panel) panel.hidden = true;
    const button = $("#settings-preview");
    if (button) button.textContent = "查看将写入的内容";
  }

  function goToSettings(focusKey, reason) {
    const target = focusKey || "";
    if (reason) state.settingsReason = reason;
    if (target) sessionStorage.setItem("subgen.settings.focus", target);
    if (location.hash === "#/settings") {
      if (state.settingsLoaded) {
        renderSettings();
        focusSettingsTarget(target);
      } else {
        loadSettings();
      }
    } else {
      location.hash = "#/settings";
    }
  }

  function focusSettingsTarget(target) {
    if (!target) return;
    const map = {
      source: 'paths.source_root',
      translation: 'translation.endpoint',
      models: 'paths.model_root',
    };
    const key = map[target] || target;
    const input = $$('[data-testid="field-input"]').find((element) => element.dataset.field === key);
    if (!input) return;
    const details = input.closest("details");
    if (details) details.open = true;
    input.scrollIntoView({ block: "center", behavior: "smooth" });
    input.focus({ preventScroll: true });
  }

  function syncPlaces() {
    const key = JSON.stringify((state.meta && state.meta.places) || []);
    if (key === state.lastPlacesKey) return;
    state.lastPlacesKey = key;
    const existing = $("#places");
    if (!existing) return;
    const chips = placeChipsHtml();
    existing.innerHTML = chips;
    existing.hidden = !chips;
  }


  const FOLDER_ICON =
    '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M2.8 6.2c0-1 .8-1.8 1.8-1.8h3l1.6 2h5.6c1 0 1.8.8 1.8 1.8v6c0 1-.8 1.8-1.8 1.8H4.6c-1 0-1.8-.8-1.8-1.8V6.2z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>';

  function homePath() {
    const places = state.meta && Array.isArray(state.meta.places) ? state.meta.places : [];
    const home = places.find((place) => place.key === "home");
    return home ? home.path : places.length ? places[0].path : "/Users";
  }

  async function openBrowse() {
    const modal = $("#browse-modal");
    if (!modal) return;
    if (typeof modal.showModal === "function") modal.showModal();
    else modal.setAttribute("open", "");
    closePreview();

    const current = fieldValue("paths.source_root").trim();
    const candidates = [];
    if (current.startsWith("/")) candidates.push(current);
    const home = homePath();
    if (home && !candidates.includes(home)) candidates.push(home);

    $("#browse-list").innerHTML = "";
    $("#browse-error").hidden = true;
    $("#browse-empty").hidden = true;
    $("#browse-truncated").hidden = true;
    $("#browse-select").disabled = true;
    $("#browse-up").disabled = true;

    let lastError = null;
    for (const candidate of candidates) {
      const ok = await loadBrowse(candidate);
      if (ok) return;
      lastError = state.browseLastError;
    }
    if (lastError) {
      $("#browse-loading").hidden = true;
      const error = $("#browse-error");
      error.hidden = false;
      error.textContent = browseErrorCopy(lastError);
      const path = $("#browse-path");
      if (path) path.textContent = current || home || "—";
    }
  }

  async function loadBrowse(path) {
    const loading = $("#browse-loading");
    const error = $("#browse-error");
    const list = $("#browse-list");
    if (loading) loading.hidden = false;
    if (error) error.hidden = true;
    if (list) list.innerHTML = "";
    $("#browse-empty").hidden = true;
    $("#browse-truncated").hidden = true;
    $("#browse-select").disabled = true;
    const pathLabel = $("#browse-path");
    if (pathLabel) pathLabel.textContent = path || "—";
    try {
      const data = await api("GET", `/api/browse?path=${encodeURIComponent(path)}`);
      state.browsePath = data.path;
      state.browseParent = data.parent;
      state.browseLoaded = true;
      state.browseLastError = null;
      if (pathLabel) pathLabel.textContent = data.path;
      const up = $("#browse-up");
      if (up) up.disabled = !data.parent;
      const entries = Array.isArray(data.entries) ? data.entries : [];
      if (list) {
        list.innerHTML = entries
          .map(
            (entry) =>
              `<li><button class="browse-entry" type="button" data-testid="browse-entry" data-path="${esc(entry.path)}" title="${esc(entry.path)}">${FOLDER_ICON}<span>${esc(entry.name)}</span></button></li>`
          )
          .join("");
      }
      $("#browse-empty").hidden = entries.length > 0;
      $("#browse-truncated").hidden = !data.truncated;
      $("#browse-loading").hidden = true;
      $("#browse-select").disabled = false;
      return true;
    } catch (err) {
      state.browseLoaded = false;
      state.browseLastError = err;
      $("#browse-loading").hidden = true;
      if (error) {
        error.hidden = false;
        error.textContent = browseErrorCopy(err);
      }
      const up = $("#browse-up");
      if (up) up.disabled = true;
      $("#browse-select").disabled = true;
      if (err.code === "not_found") {
        if (pathLabel) pathLabel.textContent = path;
      }
      return false;
    }
  }

  function browseErrorCopy(error) {
    if (!error) return "这个文件夹打不开。";
    if (error.offline) return "连不上本地服务，等它恢复后再试。";
    switch (error.code) {
      case "invalid_path":
        return "这个路径不是绝对路径，没法打开。";
      case "not_a_directory":
        return "这个文件夹不存在，或者它其实是一个文件。可以返回上一级再找。";
      case "forbidden_path":
        return "没有权限打开这个文件夹。换一个文件夹试试。";
      case "not_found":
        return "这个版本的服务还不能浏览文件夹。可以直接在设置里填写路径。";
      default:
        return "这个文件夹打不开，可以返回上一级，或者直接填写路径。";
    }
  }

  function closeBrowse() {
    const modal = $("#browse-modal");
    if (!modal) return;
    if (modal.open) modal.close();
    else modal.removeAttribute("open");
  }

  async function selectBrowse() {
    if (!state.browseLoaded || !state.browsePath) return;
    const selected = state.browsePath;
    closeBrowse();
    setSourceValue(selected);
  }


  function confirmAction(options) {
    return new Promise((resolve) => {
      const dialog = $("#confirm-dialog");
      if (!dialog) {
        resolve(false);
        return;
      }
      state.confirmResolve = resolve;
      $("#confirm-kicker").textContent = options.kicker || "确认";
      $("#confirm-title").textContent = options.title || "确认";
      $("#confirm-body").textContent = options.body || "确定要继续吗？";
      const ok = $("#confirm-ok");
      ok.textContent = options.confirmLabel || "确认";
      ok.className = options.danger ? "btn btn--primary btn--danger" : "btn btn--primary";
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    });
  }

  function settleConfirm(value) {
    const resolve = state.confirmResolve;
    state.confirmResolve = null;
    if (resolve) resolve(value);
  }

  function askExternal(detail) {
    return new Promise((resolve) => {
      const dialog = $("#external-ack");
      if (!dialog) {
        resolve(false);
        return;
      }
      state.ackResolve = resolve;
      const element = $("#ack-detail");
      if (element) element.textContent = detail || "进度文件里出现了不是这个页面写入的记录。";
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    });
  }

  function settleAck(value) {
    const resolve = state.ackResolve;
    state.ackResolve = null;
    if (resolve) resolve(value);
  }

  function openHelp() {
    const dialog = $("#help-modal");
    if (!dialog) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeHelp() {
    const dialog = $("#help-modal");
    if (!dialog) return;
    if (dialog.open) dialog.close();
    else dialog.removeAttribute("open");
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (dialog.open) dialog.close();
    else dialog.removeAttribute("open");
  }


  function bindDialogClose(selector, settle) {
    const dialog = $(selector);
    if (!dialog) return;
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) closeDialog(dialog);
    });
    dialog.addEventListener("close", () => settle(false));
  }

  function bindEvents() {
    $("#create-open").addEventListener("click", openCreate);
    $$(".help-trigger").forEach((button) => button.addEventListener("click", openHelp));
    $("#help-close").addEventListener("click", closeHelp);
    $("#help-settings-link").addEventListener("click", closeHelp);

    $("#offline-retry").addEventListener("click", async () => {
      await refreshMeta();
      if (state.route.name === "jobs") await refreshJobs(false);
      if (state.route.name === "settings" && !state.settingsLoaded) loadSettings();
      if (state.route.name === "detail" && state.detailJobId) openDetail(state.detailJobId);
    });

    $("#jobs-retry").addEventListener("click", () => refreshJobs(false));

    $("#job-list").addEventListener("click", (event) => {
      const action = event.target.closest("[data-action]");
      if (!action) return;
      const row = event.target.closest("[data-job-id]");
      if (!row) return;
      closeMenus();
      const jobId = row.dataset.jobId;
      if (action.dataset.action === "rerun") rerunJob(jobId);
      else if (action.dataset.action === "delete") deleteJob(jobId);
      else if (action.dataset.action === "cancel") cancelJob(jobId);
    });

    document.addEventListener("click", (event) => {
      const settingsLink = event.target.closest("[data-settings-focus]");
      if (settingsLink) {
        event.preventDefault();
        goToSettings(settingsLink.dataset.settingsFocus);
        return;
      }
      const setupLink = event.target.closest("[data-goto]");
      if (setupLink) {
        event.preventDefault();
        goToSettings(setupLink.dataset.goto);
        return;
      }
      const menu = event.target.closest(".menu");
      closeMenus(menu);
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeMenus();
    });

    $("#detail-retry").addEventListener("click", () => {
      if (!state.detailJobId) return;
      openDetail(state.detailJobId);
    });

    $("#detail-copy").addEventListener("click", async () => {
      const jobId = $("#detail-copy").dataset.jobId || (detailUI.job && detailUI.job.job_id);
      if (!jobId) return;
      try {
        await navigator.clipboard.writeText(jobId);
        toast("任务编号已复制");
      } catch (error) {
        toast("复制失败，可以手动选中编号", "error");
      }
    });

    $("#cancel-button").addEventListener("click", () => {
      if (detailUI.job) cancelJob(detailUI.job.job_id);
    });
    $("#rerun-button").addEventListener("click", () => {
      if (detailUI.job) rerunJob(detailUI.job.job_id);
    });
    $("#delete-button").addEventListener("click", () => {
      if (detailUI.job) deleteJob(detailUI.job.job_id);
    });

    const logPanel = $("#log-panel");
    if (logPanel) {
      logPanel.addEventListener("scroll", () => {
        detailUI.logPinned = logPanel.scrollHeight - logPanel.scrollTop - logPanel.clientHeight < 40;
      });
    }

    $("#create-form").addEventListener("submit", (event) => {
      event.preventDefault();
      submitCreate(false);
    });
    $("#create-cancel").addEventListener("click", closeCreate);
    $$("[data-choice]").forEach((choice) => {
      const input = $('input[name="scope"]', choice);
      if (input) input.addEventListener("change", syncCreateChoice);
    });
    $("#create-limit").addEventListener("focus", () => {
      const limitScope = $("#create-scope-limit");
      if (limitScope) {
        limitScope.checked = true;
        syncCreateChoice();
      }
    });
    $("#create-limit").addEventListener("click", (event) => event.stopPropagation());
    $("#create-notice-link").addEventListener("click", closeCreate);
    $("#create-busy-link").addEventListener("click", (event) => {
      const jobId = event.currentTarget.dataset.jobId;
      closeCreate();
      if (jobId) location.hash = `#/jobs/${encodeURIComponent(jobId)}`;
    });
    $("#settings-retry").addEventListener("click", loadSettings);
    $("#settings-preview").addEventListener("click", togglePreview);
    $("#preview-close").addEventListener("click", closePreview);
    $("#repair-action").addEventListener("click", () => saveSettings(true));

    $("#browse-up").addEventListener("click", () => {
      if (state.browseParent) loadBrowse(state.browseParent);
    });
    $("#browse-list").addEventListener("click", (event) => {
      const entry = event.target.closest("[data-path]");
      if (entry) loadBrowse(entry.dataset.path);
    });
    $("#browse-select").addEventListener("click", selectBrowse);
    $("#browse-cancel").addEventListener("click", () => {
      closeBrowse();
      const input = $('[data-testid="field-input"][data-field="paths.source_root"]');
      if (input) input.focus();
    });

    $("#confirm-ok").addEventListener("click", () => {
      const resolve = state.confirmResolve;
      state.confirmResolve = null;
      closeDialog($("#confirm-dialog"));
      if (resolve) resolve(true);
    });
    $("#confirm-cancel").addEventListener("click", () => {
      const resolve = state.confirmResolve;
      state.confirmResolve = null;
      closeDialog($("#confirm-dialog"));
      if (resolve) resolve(false);
    });

    $("#ack-confirm").addEventListener("click", () => {
      const resolve = state.ackResolve;
      state.ackResolve = null;
      closeDialog($("#external-ack"));
      if (resolve) resolve(true);
    });
    $("#ack-cancel").addEventListener("click", () => {
      const resolve = state.ackResolve;
      state.ackResolve = null;
      closeDialog($("#external-ack"));
      if (resolve) resolve(false);
    });

    bindDialogClose("#confirm-dialog", settleConfirm);
    bindDialogClose("#external-ack", settleAck);

    window.addEventListener("hashchange", handleRoute);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        stopDetailPolling();
        return;
      }
      refreshMeta();
      if (state.route.name === "jobs") refreshJobs(true);
      if (state.route.name === "detail" && state.detailJobId) startDetailPolling(0);
    });

    window.addEventListener("storage", (event) => {
      if (event.key === "subgen.settings.focus" && state.route.name === "settings" && state.settingsLoaded) {
        const target = sessionStorage.getItem("subgen.settings.focus");
        sessionStorage.removeItem("subgen.settings.focus");
        if (target) focusSettingsTarget(target);
      }
    });
  }


  function init() {
    if (!location.hash) {
      history.replaceState(null, "", "#/jobs");
    }
    bindEvents();
    renderService();
    renderOffline(null);
    handleRoute();

    refreshMeta().finally(scheduleMeta);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();