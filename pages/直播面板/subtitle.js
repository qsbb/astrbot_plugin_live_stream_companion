const state = {
  configSchema: {},
  configGroups: [],
  configValues: {},
  configDirty: false,
  configFallback: false,
  previewToken: 0,
};

const els = {};

document.addEventListener("DOMContentLoaded", () => {
  [
    "subtitle", "refreshBtn", "configEditor", "saveConfigBtn", "resetConfigBtn",
    "configDirtyBadge", "configStatus", "previewText", "previewBtn",
    "replayPreviewBtn", "subtitlePreviewText", "subtitlePreviewStage", "toast",
  ].forEach((id) => {
    els[id] = document.getElementById(id);
  });

  els.refreshBtn?.addEventListener("click", () => loadConfig());
  els.saveConfigBtn?.addEventListener("click", () => saveConfig());
  els.resetConfigBtn?.addEventListener("click", () => resetConfigForm());
  els.previewBtn?.addEventListener("click", () => previewSubtitle(true));
  els.replayPreviewBtn?.addEventListener("click", () => previewSubtitle(false));
  els.previewText?.addEventListener("input", () => previewSubtitle(false));

  loadConfig();
});

async function loadConfig() {
  if (state.configDirty) return null;
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
    showToast(error.message || String(error));
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
    values: {},
    fallback: true,
  });
}

async function saveConfig() {
  try {
    const values = collectConfigValues();
    els.saveConfigBtn.disabled = true;
    const data = await LivePageApi.post("/config/save", { values });
    state.configValues = { ...state.configValues, ...(data.values || values) };
    state.configDirty = false;
    renderConfig();
    updateDirtyState();
    showToast(data.message || "配置已保存。");
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
  previewSubtitle(false);
}

async function previewSubtitle(sendToOverlay) {
  const text = els.previewText?.value.trim() || "谢谢喜欢，今天也一起把直播间热起来吧。";
  const style = subtitleStyleFromValues(collectConfigValues());
  playLocalSubtitle(text, style);
  if (!sendToOverlay) return;
  try {
    const data = await LivePageApi.post("/subtitle/preview", { text });
    showToast(data.message || "已播放字幕预览。");
  } catch (error) {
    showToast(error.message || String(error));
  }
}

function renderConfig() {
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
    includeGroup: (group) => group.id === "subtitle",
  });

  els.configEditor.querySelectorAll(".config-control").forEach((control) => {
    const onChange = () => {
      state.configDirty = true;
      updateDirtyState();
      previewSubtitle(false);
    };
    control.addEventListener("input", onChange);
    control.addEventListener("change", onChange);
  });
  playLocalSubtitle(els.previewText?.value || "", subtitleStyleFromValues(values), { instant: true });
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

function subtitleStyleFromValues(values) {
  return {
    typing_speed_ms: Math.max(1, Number(values.subtitle_typing_speed_ms || 45)),
    hold_seconds: Math.max(0, Number(values.subtitle_hold_seconds || 4)),
    font_size: Math.max(12, Number(values.subtitle_font_size || 42)),
    font_weight: Number(values.subtitle_font_weight || 700),
    text_color: values.subtitle_text_color || "#ffffff",
    stroke_color: values.subtitle_stroke_color || "#111111",
    stroke_size: Math.max(0, Number(values.subtitle_stroke_size || 4)),
    cursor_color: values.subtitle_cursor_color || values.subtitle_text_color || "#ffffff",
    show_cursor: values.subtitle_show_cursor !== false,
    fade_out: values.subtitle_fade_out !== false,
    position: values.subtitle_position || "bottom",
    padding: Math.max(0, Number(values.subtitle_padding || 48)),
    max_width: Math.max(200, Number(values.subtitle_max_width || 1100)),
  };
}

async function playLocalSubtitle(text, style, options = {}) {
  if (!els.subtitlePreviewText || !els.subtitlePreviewStage) return;
  const token = ++state.previewToken;
  const stage = els.subtitlePreviewStage;
  stage.style.setProperty("--preview-align", style.position === "top" ? "flex-start" : (style.position === "center" ? "center" : "flex-end"));
  stage.style.setProperty("--preview-padding", `${Math.min(style.padding, 120)}px`);
  stage.style.setProperty("--preview-max-width", `${Math.min(style.max_width, 980)}px`);
  stage.style.setProperty("--preview-text", style.text_color);
  stage.style.setProperty("--preview-stroke", style.stroke_color);
  stage.style.setProperty("--preview-stroke-size", `${style.stroke_size}px`);
  stage.style.setProperty("--preview-font-size", `${Math.min(style.font_size, 72)}px`);
  stage.style.setProperty("--preview-font-weight", String(style.font_weight));
  stage.style.setProperty("--preview-cursor", style.cursor_color);

  const target = els.subtitlePreviewText;
  const content = text || "谢谢喜欢，今天也一起把直播间热起来吧。";
  target.className = "is-visible";
  if (options.instant) {
    target.textContent = content;
    return;
  }
  target.textContent = "";
  const cursor = style.show_cursor === false ? "" : "▋";
  let current = "";
  for (const char of Array.from(content)) {
    if (token !== state.previewToken) return;
    current += char;
    target.textContent = current + cursor;
    await sleep(Math.min(Math.max(8, style.typing_speed_ms), 120));
  }
  if (token !== state.previewToken) return;
  target.textContent = current;
  await sleep(Math.min(style.hold_seconds * 1000, 1800));
  if (token !== state.previewToken) return;
  if (style.fade_out !== false) target.className = "is-visible is-fading";
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

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

