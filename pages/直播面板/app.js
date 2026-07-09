const state = {
  overview: null,
  memory: null,
  configSchema: {},
  configGroups: [],
  configValues: {},
  configDirty: false,
  configFallback: false,
  viewerFilter: "",
};

const els = {};

document.addEventListener("DOMContentLoaded", () => {
  [
    "subtitle", "stats", "refreshBtn", "startBtn", "stopBtn", "liveBadge",
    "liveFlow", "autoReplyPanel", "stagePanel", "topViewers", "memoryRefreshBtn",
    "memoryOverview", "memoryItems", "highlightItems", "topicItems", "threadItems",
    "summaryItems", "viewerFilter", "viewerRows", "eventCount", "eventRows",
    "configEditor", "saveConfigBtn", "resetConfigBtn", "configDirtyBadge",
    "configStatus", "obsRefreshBtn", "obsControlPanel", "toast",
  ].forEach((id) => {
    els[id] = document.getElementById(id);
  });

  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  });
  els.refreshBtn?.addEventListener("click", () => loadAll());
  els.memoryRefreshBtn?.addEventListener("click", () => loadMemory());
  els.startBtn?.addEventListener("click", () => startLive());
  els.stopBtn?.addEventListener("click", () => stopLive());
  els.obsRefreshBtn?.addEventListener("click", () => refreshObsControl());
  els.obsControlPanel?.addEventListener("click", handleObsControlClick);
  els.saveConfigBtn?.addEventListener("click", () => saveConfig());
  els.resetConfigBtn?.addEventListener("click", () => resetConfigForm());
  els.viewerFilter?.addEventListener("input", () => {
    state.viewerFilter = els.viewerFilter.value.trim().toLowerCase();
    renderViewers();
  });

  loadAll();
  window.setInterval(loadAll, 15000);
});

function activateTab(tab) {
  document.querySelectorAll(".tab").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.tab === tab);
  });
  document.querySelectorAll(".panel").forEach((item) => {
    item.classList.toggle("is-active", item.id === `panel-${tab}`);
  });
  if (tab === "memory" && !state.memory) {
    loadMemory();
  }
  if (tab === "config" && !Object.keys(state.configSchema).length) {
    loadConfig();
  }
}

async function loadAll() {
  try {
    const [overview] = await Promise.all([
      LivePageApi.get("/overview"),
      loadConfig({ silent: true }),
    ]);
    state.overview = overview;
    renderOverview();
    if (document.getElementById("panel-memory")?.classList.contains("is-active")) {
      await loadMemory();
    }
  } catch (error) {
    renderOfflineShell(error);
    showToast(error.message || String(error));
  }
}

async function loadMemory() {
  try {
    const data = await LivePageApi.get("/memory");
    state.memory = data;
    renderMemory();
    renderViewers();
  } catch (error) {
    showToast(error.message || String(error));
  }
}

async function loadConfig(options = {}) {
  if (state.configDirty && options.silent) return null;
  try {
    const data = await LivePageApi.get("/config/schema");
    applyConfigModel({
      schema: data.schema || {},
      groups: data.groups || [],
      values: data.values || {},
      fallback: false,
    });
    state.configDirty = false;
    return data;
  } catch (error) {
    useFallbackConfigModel();
    if (!options.silent) showToast(error.message || String(error));
    return null;
  }
}

function applyConfigModel({ schema, groups, values, fallback }) {
  state.configSchema = schema;
  state.configGroups = groups;
  state.configValues = { ...LiveConfigForm.defaultValues(schema), ...values };
  state.configFallback = Boolean(fallback);
  renderConfig();
  updateDirtyState();
}

function useFallbackConfigModel() {
  if (Object.keys(state.configSchema).length) return;
  applyConfigModel({
    schema: FALLBACK_CONFIG_SCHEMA,
    groups: FALLBACK_CONFIG_GROUPS,
    values: state.overview?.config || {},
    fallback: true,
  });
}

async function startLive() {
  try {
    const formValues = collectConfigValues();
    const roomId = formValues.bilibili_room_id
      || state.overview?.live?.room_id
      || state.overview?.config?.bilibili_room_id
      || 0;
    const data = await LivePageApi.post("/control/start", { room_id: roomId });
    showToast(data.message || "已请求启动监听。");
    await loadAll();
  } catch (error) {
    showToast(error.message || String(error));
  }
}

async function stopLive() {
  try {
    const data = await LivePageApi.post("/control/stop", {});
    showToast(data.message || "已请求停止监听。");
    await loadAll();
  } catch (error) {
    showToast(error.message || String(error));
  }
}

async function saveConfig() {
  try {
    const values = collectConfigValues();
    els.saveConfigBtn.disabled = true;
    const data = await LivePageApi.post("/config/save", { values });
    state.configValues = data.values || values;
    state.configDirty = false;
    renderConfig();
    updateDirtyState();
    showToast(data.message || "配置已保存。");
    await loadAll();
  } catch (error) {
    if (state.configFallback) {
      showToast("当前 AstrBot 进程还没加载拓展页保存接口，重启/重载插件后即可保存。");
    } else {
      showToast(error.message || String(error));
    }
  } finally {
    els.saveConfigBtn.disabled = false;
  }
}

function resetConfigForm() {
  state.configDirty = false;
  renderConfig();
  updateDirtyState();
}


function renderOverview() {
  const data = state.overview || {};
  const live = data.live || {};
  const memory = data.memory || {};
  const autoReply = data.auto_reply || {};
  const companion = data.companion || {};
  const integration = data.integration || {};
  const streamer = autoReply.streamer || {};
  if (state.configFallback && !state.configDirty && data.config) {
    state.configValues = { ...state.configValues, ...data.config };
    renderConfig();
  }
  els.subtitle.textContent = live.running
    ? `直播监听中 · 房间 ${live.room_id || "未配置"} · ${formatDuration(live.duration_seconds)}`
    : `离线配置可用 · 直播监听未运行 · 房间 ${live.room_id || data.config?.bilibili_room_id || "未配置"}`;
  els.liveBadge.textContent = live.running ? "直播中" : "未运行";
  els.liveBadge.className = `badge ${live.running ? "ok" : "idle"}`;

  renderStats([
    ["直播事件", live.session_count || 0, "本场累计"],
    ["缓存事件", live.cache_count || 0, "最近保留"],
    ["直播记忆", memory.memory_count || 0, "可承接条目"],
    ["观众画像", data.viewers?.count || 0, "累计观众"],
    ["本分钟回复", `${autoReply.used_this_minute || 0}/${autoReply.max_per_minute || 0}`, "普通弹幕限流"],
    ["直播小结", memory.summary_count || 0, "历史整理"],
    ["联动健康", `${integration.ok_count || 0}/${integration.total || 0}`, "监听/字幕/记忆"],
  ]);

  renderLiveFlow(data);
  renderMetricList(els.autoReplyPanel, [
    ["状态", boolText(autoReply.enabled)],
    ["模式", autoReply.mode || "native"],
    ["身份", autoReply.identity_label || autoReply.identity_mode || "主播模式"],
    ["主播称呼", streamer.name || "主播"],
    ["身份来源", streamer.source || "fallback"],
    ["待回应事件", autoReply.pending || 0],
    ["冷却", `${autoReply.cooldown_seconds || 0}s`],
    ["读空气降噪", boolText(autoReply.air_guard)],
    ["陪伴读空气", boolText(autoReply.air_guard_model)],
    ["回应阈值", autoReply.air_guard_threshold || 2.5],
    ["全量 TTS", boolText(autoReply.force_full_tts)],
    ["TTS/打字机同步", boolText(autoReply.sync_tts_subtitle)],
    ["TTS 本机播放", boolText(autoReply.local_playback)],
    ["每分钟上限", autoReply.max_per_minute === 0 ? "不限" : autoReply.max_per_minute],
    ["豁免事件", (autoReply.exempt_event_types || []).join("、") || "无"],
  ]);
  renderMetricList(els.stagePanel, [
    ["VTS", data.vts?.connected ? "已连接" : "未连接"],
    ["VTS 地址", data.vts?.url || "--"],
    ["字幕", data.subtitle?.enabled ? (data.subtitle.running ? "运行中" : "已启用") : "未启用"],
    ["字幕地址", data.subtitle?.url || "--"],
    ["嘴型", data.mouth_sync?.enabled ? "已启用" : "未启用"],
    ["嘴型参数", data.mouth_sync?.parameter || "--"],
    ["陪伴插件", companion.available ? "已连接" : "未找到"],
    ["LivingMemory", data.living_memory?.ready ? "已就绪" : (data.living_memory?.available ? "初始化中" : "未找到")],
  ]);
  renderObsControl(data.obs_control || {});
  renderTopViewers(data.live?.top_viewers || []);
  renderEvents();
}

function renderOfflineShell(error) {
  useFallbackConfigModel();
  els.subtitle.textContent = "拓展页 API 暂不可用，仍可查看本地页面结构。";
  els.liveBadge.textContent = "离线";
  els.liveBadge.className = "badge idle";
  renderStats([
    ["直播事件", 0, "等待 API"],
    ["缓存事件", 0, "等待 API"],
    ["直播记忆", 0, "等待 API"],
    ["观众画像", 0, "等待 API"],
  ]);
  renderMetricList(els.autoReplyPanel, [["状态", "未知"], ["原因", error?.message || "请求失败"]]);
  renderMetricList(els.stagePanel, [["字幕预览", "页面内可用"], ["真实 overlay", "等待 API"]]);
  els.liveFlow.innerHTML = emptyText("未连接直播也可以使用配置页；当前只是拓展页 API 没有响应。");
  renderObsControl({});
  renderEvents();
}

function renderStats(items) {
  els.stats.innerHTML = items.map(([label, value, hint]) => `
    <article class="stat">
      <b>${escapeHtml(value)}</b>
      <span>${escapeHtml(label)}</span>
      <small>${escapeHtml(hint)}</small>
    </article>
  `).join("");
}

function renderLiveFlow(data) {
  const live = data.live || {};
  const companion = data.companion || {};
  const memory = data.memory || {};
  const living = data.living_memory || {};
  const steps = [
    ["B站监听", live.running ? "ok" : "idle", live.running ? `${live.type}/${live.backend}` : "可在配置页准备"],
    ["事件缓存", live.cache_count ? "ok" : "idle", `${live.cache_count || 0} 条`],
    ["自动回应", data.auto_reply?.enabled ? "ok" : "idle", data.auto_reply?.mode || "native"],
    ["直播记忆", memory.enabled ? "ok" : "idle", `${memory.memory_count || 0} 条记忆`],
    ["陪伴联动", companion.available ? "ok" : "idle", companion.available ? "已连接" : "未找到"],
    ["长期记忆", living.ready ? "ok" : "idle", living.ready ? `召回 top_k ${living.top_k || 0}` : (living.available ? "初始化中" : "未找到")],
    ["VTS/字幕", data.vts?.connected || data.subtitle?.running ? "ok" : "idle", data.subtitle?.running ? "字幕运行" : "演出待命"],
  ];
  els.liveFlow.innerHTML = steps.map(([title, status, desc]) => `
    <div class="flow-step ${status}">
      <span></span>
      <b>${escapeHtml(title)}</b>
      <small>${escapeHtml(desc)}</small>
    </div>
  `).join("");
}

function renderObsControl(control) {
  if (!els.obsControlPanel) return;
  const obs = control.obs || {};
  const l2d = control.l2dstudio || {};
  const settings = control.settings || {};
  const enabled = Boolean(control.enabled);
  const connected = Boolean(obs.websocket?.connected);
  const streamAllowed = Boolean(control.safety?.stream_start_allowed);
  const actionDisabled = !enabled;
  const obsActionDisabled = actionDisabled || !connected;
  const streamDisabled = obsActionDisabled || !streamAllowed;
  const cards = [
    ["控制开关", enabled ? "已启用" : "未启用", enabled ? "可执行开播控制" : "到配置页启用 OBS 开播控制", enabled ? "ok" : "idle"],
    ["OBS", obs.running ? "运行中" : "未运行", obs.configured ? (obs.process?.exists ? obs.process?.name || "路径可用" : "路径不存在") : "未配置路径", obs.running ? "ok" : "idle"],
    ["L2DStudio", l2d.running ? "运行中" : "未运行", l2d.configured ? (l2d.process?.exists ? l2d.process?.name || "路径可用" : "路径不存在") : "未配置路径", l2d.running ? "ok" : "idle"],
    ["OBS WebSocket", connected ? "已连接" : "未连接", connected ? `${obs.websocket?.obs_version || "OBS"} / ${obs.websocket?.websocket_version || "WebSocket"}` : (obs.websocket?.error || "等待 OBS 启动并开启 WebSocket"), connected ? "ok" : "idle"],
    ["当前场景", obs.current_scene || settings.obs_live_scene_name || "未读取", settings.obs_live_scene_name ? `默认：${settings.obs_live_scene_name}` : "未配置默认场景", obs.current_scene ? "ok" : "idle"],
    ["推流状态", obs.streaming ? "直播中" : "未推流", streamAllowed ? "允许二次确认开播" : "配置禁止插件开播", obs.streaming ? "danger" : "idle"],
  ];
  els.obsControlPanel.innerHTML = `
    <div class="obs-status-grid">
      ${cards.map(([label, value, note, tone]) => `
        <section class="obs-status-card ${escapeHtml(tone)}">
          <span>${escapeHtml(label)}</span>
          <b>${escapeHtml(value)}</b>
          <small>${escapeHtml(note)}</small>
        </section>
      `).join("")}
    </div>
    <div class="obs-action-grid">
      <button type="button" data-obs-action="open_obs" ${actionDisabled || !obs.configured ? "disabled" : ""}>打开 OBS</button>
      <button type="button" data-obs-action="open_l2dstudio" ${actionDisabled || !l2d.configured ? "disabled" : ""}>打开 L2DStudio</button>
      <button type="button" data-obs-action="start_apps" ${actionDisabled ? "disabled" : ""}>打开两端</button>
      <button type="button" data-obs-action="check" ${actionDisabled ? "disabled" : ""}>检查连接</button>
      <button type="button" data-obs-action="debug" ${actionDisabled ? "disabled" : ""}>直播调试</button>
      <button type="button" data-obs-action="switch_scene" ${obsActionDisabled || !settings.obs_live_scene_name ? "disabled" : ""}>切换场景</button>
      <button type="button" data-obs-action="${obs.virtual_camera ? "stop_virtual_camera" : "start_virtual_camera"}" ${obsActionDisabled ? "disabled" : ""}>${obs.virtual_camera ? "关闭虚拟摄像机" : "开启虚拟摄像机"}</button>
      <button type="button" data-obs-action="${obs.recording ? "stop_record" : "start_record"}" ${obsActionDisabled ? "disabled" : ""}>${obs.recording ? "停止录制" : "开始录制"}</button>
      <button type="button" class="danger" data-obs-action="start_stream" ${streamDisabled || obs.streaming ? "disabled" : ""}>开始直播</button>
      <button type="button" class="danger-outline" data-obs-action="stop_stream" ${obsActionDisabled || !obs.streaming ? "disabled" : ""}>停止直播</button>
    </div>
    <p class="muted obs-hint">开始直播会调用 OBS StartStream，要求配置页开启“允许插件开始推流”，并且需要二次点击确认。B 站推流侧建议先安装 obs-bilibili-stream。</p>
  `;
}

async function refreshObsControl() {
  try {
    const data = await LivePageApi.get("/control/obs/status");
    state.overview = { ...(state.overview || {}), obs_control: data };
    renderObsControl(data);
    showToast("OBS 状态已刷新。");
  } catch (error) {
    showToast(error.message || String(error));
  }
}

async function handleObsControlClick(event) {
  const button = event.target instanceof Element ? event.target.closest("[data-obs-action]") : null;
  if (!button) return;
  const action = button.dataset.obsAction || "";
  const body = { action };
  if (action === "start_stream") {
    if (!requireSecondClick(button, "start_stream", "再次点击会真正开始 OBS 推流", "确认开播")) return;
    body.confirm = true;
  }
  try {
    button.disabled = true;
    const data = await LivePageApi.post("/control/obs/action", body);
    if (data.obs_control) {
      state.overview = { ...(state.overview || {}), obs_control: data.obs_control };
      renderObsControl(data.obs_control);
    } else {
      await loadAll();
    }
    showToast(data.message || "OBS 控制动作已完成。");
  } catch (error) {
    showToast(error.message || String(error));
  } finally {
    button.disabled = false;
  }
}

function renderTopViewers(items) {
  if (!items.length) {
    els.topViewers.innerHTML = emptyText("本场还没有观众活跃数据；配置页和字幕预览无需开播也能使用。");
    return;
  }
  els.topViewers.innerHTML = items.map((item) => `
    <div class="viewer-chip">
      <b>${escapeHtml(item.name)}</b>
      <span>${escapeHtml(item.count)} 次</span>
    </div>
  `).join("");
}

function renderMemory() {
  const payload = state.memory?.memory || state.overview?.memory || {};
  els.memoryOverview.innerHTML = state.memory?.overview
    ? `<pre>${escapeHtml(state.memory.overview)}</pre>`
    : emptyText(state.memory?.message || "暂时还没有直播专用记忆。");
  renderCards(els.memoryItems, payload.recent_items || payload.all_recent_items || [], itemText);
  renderCards(els.highlightItems, payload.highlights || [], itemText);
  renderTopics(payload.topics || []);
  renderCards(els.threadItems, payload.open_threads || [], itemText);
  renderCards(els.summaryItems, payload.summaries || [], (item) => item.summary || item.body || "");
}

function renderViewers() {
  const source = state.memory?.viewers || state.overview?.viewers || {};
  const items = (source.items || []).filter((item) => {
    if (!state.viewerFilter) return true;
    return `${item.display_name || ""} ${item.live_username || ""}`.toLowerCase().includes(state.viewerFilter);
  });
  if (!items.length) {
    els.viewerRows.innerHTML = emptyText("暂时没有符合条件的观众画像。");
    return;
  }
  els.viewerRows.innerHTML = items.map((item) => {
    const counts = item.event_counts || {};
    const danmaku = (item.recent_danmaku || []).slice(0, 3).map((row) => row.content).filter(Boolean).join(" / ");
    return `
      <article class="viewer-card">
        <div>
          <h2>${escapeHtml(item.display_name || item.live_username || item.key)}</h2>
          <span>${escapeHtml(item.live_username || "已匹配关系节点")}</span>
        </div>
        <b>${escapeHtml(item.total_events || 0)}</b>
        <p>${escapeHtml(eventCountText(counts))}</p>
        <small>${escapeHtml(danmaku || "暂无最近弹幕样本")}</small>
      </article>
    `;
  }).join("");
}

function renderEvents() {
  const events = state.overview?.recent_events || [];
  els.eventCount.textContent = `${events.length} 条`;
  if (!events.length) {
    els.eventRows.innerHTML = emptyText("暂时没有直播事件。");
    return;
  }
  els.eventRows.innerHTML = events.map((item) => `
    <article class="event-row">
      <span>${escapeHtml(item.type)}</span>
      <div>
        <b>${escapeHtml(item.username || "系统")}</b>
        <p>${escapeHtml(item.content || item.display || "--")}</p>
      </div>
      <time>${escapeHtml(formatTime(item.ts))}</time>
    </article>
  `).join("");
}

function renderConfig() {
  if (!els.configEditor) return;
  if (els.configStatus) {
    els.configStatus.textContent = state.configFallback
      ? "正在使用页面内置配置结构；若保存失败，请重载/重启插件让后端新接口生效。"
      : "已连接拓展页配置接口，保存后会写入插件配置。";
  }
  if (!Object.keys(state.configSchema).length) {
    els.configEditor.innerHTML = emptyText("配置结构加载中。");
    return;
  }
  const values = state.configDirty
    ? { ...state.configValues, ...collectConfigValues(false) }
    : { ...state.configValues };
  LiveConfigForm.renderGroups(els.configEditor, state.configGroups, state.configSchema, values, {
    includeGroup: (group) => group.id !== "subtitle",
  });

  els.configEditor.querySelectorAll(".config-control").forEach((control) => {
    control.addEventListener("input", () => {
      state.configDirty = true;
      updateDirtyState();
    });
    control.addEventListener("change", () => {
      state.configDirty = true;
      updateDirtyState();
    });
  });
}

function collectConfigValues(includeFallback = true) {
  return LiveConfigForm.collectValues(
    els.configEditor,
    state.configSchema,
    includeFallback ? state.configValues : {},
  );
}

function updateDirtyState() {
  if (els.configDirtyBadge) els.configDirtyBadge.hidden = !state.configDirty;
  if (els.saveConfigBtn) els.saveConfigBtn.disabled = !state.configDirty;
}

function renderMetricList(target, items) {
  target.innerHTML = items.map(([label, value]) => `
    <div class="metric-row">
      <span>${escapeHtml(label)}</span>
      <b>${escapeHtml(value)}</b>
    </div>
  `).join("");
}

function renderCards(target, items, getText) {
  if (!items.length) {
    target.innerHTML = emptyText("暂无数据。");
    return;
  }
  target.innerHTML = items.map((item) => `
    <div class="mini-card">
      <span>${escapeHtml(item.type || item.username || item.date || "直播")}</span>
      <p>${escapeHtml(getText(item) || "--")}</p>
    </div>
  `).join("");
}

function renderTopics(items) {
  if (!items.length) {
    els.topicItems.innerHTML = emptyText("暂无话题。");
    return;
  }
  els.topicItems.innerHTML = items.map((item) => `
    <span class="topic">
      <b>${escapeHtml(item.topic)}</b>
      <small>${escapeHtml(item.count)} 次</small>
    </span>
  `).join("");
}

function itemText(item) {
  return item.text || item.content || item.summary || item.body || "";
}

function eventCountText(counts) {
  const labels = {
    danmaku: "弹幕",
    gift: "礼物",
    super_chat: "SC",
    buy_guard: "上舰",
    enter_room: "进房",
    follow: "关注",
    like: "点赞",
  };
  return Object.entries(counts)
    .filter(([, value]) => Number(value) > 0)
    .map(([key, value]) => `${labels[key] || key} ${value}`)
    .join("、") || "暂无分类";
}

function boolText(value) {
  return value ? "开启" : "关闭";
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function formatTime(ts) {
  if (!ts) return "--";
  return new Date(Number(ts) * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function emptyText(text) {
  return `<div class="empty">${escapeHtml(text)}</div>`;
}

function showToast(text) {
  if (!els.toast) return;
  els.toast.textContent = text;
  els.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    els.toast.hidden = true;
  }, 2600);
}

function requireSecondClick(button, key, message, nextText = "再次确认", timeoutMs = 6000) {
  const now = Date.now();
  const armed = button.dataset.confirmKey === key && now - Number(button.dataset.confirmAt || 0) < timeoutMs;
  if (armed) {
    delete button.dataset.confirmKey;
    delete button.dataset.confirmAt;
    return true;
  }
  button.dataset.confirmKey = key;
  button.dataset.confirmAt = String(now);
  button.dataset.originalText = button.dataset.originalText || button.textContent || "";
  button.textContent = nextText;
  showToast(message);
  window.clearTimeout(button._confirmTimer);
  button._confirmTimer = window.setTimeout(() => {
    if (button.dataset.confirmKey === key) {
      delete button.dataset.confirmKey;
      delete button.dataset.confirmAt;
      button.textContent = button.dataset.originalText || "";
      delete button.dataset.originalText;
    }
  }, timeoutMs);
  return false;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}


