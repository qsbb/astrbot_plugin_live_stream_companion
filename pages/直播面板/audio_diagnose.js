/* 拓展页「语音诊断 / 生成试听」卡片。
 * 参数与后端 /tts/diagnose 对齐：只跑「文本转换 → 合成 → 读取音频参数」，不发送消息。 */
const LiveAudioDiagnose = (() => {
  const CARD_ID = "config-group-audio-diagnose";
  const ANCHOR_ID = "config-group-audio";
  const DEFAULT_TEXT = "你好，这是一段语音合成诊断文本，用来检查语速、音量和停顿是否正常。";

  const FILLS = {
    short: "测试一下，一二三。",
    numbers: "订单号 20260918，重量 3.5 公斤，温度 26 摄氏度。",
    long: "这是一段稍长的诊断文本，用来观察长句的断句、停顿和整体语速是否自然，顺便确认合成耗时是否在接受范围内。",
  };

  const WAVE_BARS = [
    8, 16, 26, 38, 52, 64, 48, 34, 28, 44, 66, 76, 56, 40, 30, 22, 36, 54, 70, 82, 68, 50,
    38, 26, 20, 32, 48, 62, 76, 60, 44, 32, 24, 18, 28, 42, 56, 68, 54, 40, 28, 20, 14, 22,
    34, 46, 38, 26,
  ];

  const state = {
    text: DEFAULT_TEXT,
    running: false,
    result: null,
    error: "",
    lastRunAt: "",
    pushOverlay: false,
    localPlay: false,
    switchesReady: false,
    audioEl: null,
    playing: false,
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function formatSeconds(value) {
    const total = Number(value) || 0;
    const minutes = Math.floor(total / 60);
    const seconds = total - minutes * 60;
    return `${String(minutes).padStart(2, "0")}:${seconds.toFixed(1).padStart(4, "0")}`;
  }

  function formatSize(bytes) {
    const size = Number(bytes) || 0;
    if (!size) return "--";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / 1024 / 1024).toFixed(2)} MB`;
  }

  function channelText(channels) {
    const value = Number(channels) || 0;
    if (value === 1) return "单声道";
    if (value === 2) return "立体声";
    return value ? `${value} 声道` : "声道未知";
  }

  function rateText(rate) {
    const value = Number(rate) || 0;
    if (!value) return "采样率未知";
    return value % 1000 === 0 ? `${value / 1000} kHz` : `${(value / 1000).toFixed(1)} kHz`;
  }

  function cardHtml() {
    return `
      <article class="config-card diag-card" id="${CARD_ID}" data-group="audio-diagnose">
        <div class="config-card-head">
          <div>
            <h2>语音诊断与试听</h2>
            <p>不用开播也能跑一次真实直播 TTS 链路：文本转换 → 服务定位 → 合成 → 试听，并给出实际命中的后端、耗时和音频参数。</p>
          </div>
          <div class="config-actions">
            <span class="badge idle" id="diagBackendBadge">正在读取配置…</span>
            <button type="button" data-diag="reload">重读配置</button>
          </div>
        </div>

        <div class="field-grid">
          <div class="field field-wide" data-config-key="__diag_text">
            <span>
              <b>诊断文本</b>
              <small>与直播自动回应走同一套清洗：剥离 &lt;tts&gt; 块、口播化转换、字幕切分</small>
            </span>
            <textarea id="diagText" rows="3" spellcheck="false"></textarea>
            <div class="diag-chipline">
              <span class="label">快速填入：</span>
              <button type="button" class="diag-chip" data-diag-fill="short">短句</button>
              <button type="button" class="diag-chip" data-diag-fill="numbers">数字与符号</button>
              <button type="button" class="diag-chip" data-diag-fill="long">长句</button>
            </div>
          </div>
        </div>

        <div class="diag-switchline">
          <label><input type="checkbox" id="diagPushOverlay"> 同时推送到 OBS 字幕 overlay 播放</label>
          <label><input type="checkbox" id="diagLocalPlay"> 生成后在本机播放一次</label>
          <small class="muted">两个开关只对本次诊断生效，不写回配置。</small>
        </div>

        <div class="diag-actions">
          <button type="button" class="primary" id="diagRunBtn" data-diag="run">生成试听</button>
          <button type="button" id="diagRunPlayBtn" data-diag="run-play">生成并本机播放</button>
          <span class="muted" id="diagStamp"></span>
        </div>

        <div id="diagOutput"></div>
      </article>
    `;
  }

  function renderIdle() {
    const out = document.getElementById("diagOutput");
    if (!out) return;
    out.innerHTML = `
      <div class="diag-tip">尚未运行诊断。会用当前配置合成一次，不会发送消息、不写直播记忆、不占用自动回应限流。</div>
    `;
  }

  function renderProgress() {
    const out = document.getElementById("diagOutput");
    if (!out) return;
    out.innerHTML = `
      <div class="diag-progress"><span class="spinner"></span><span>正在合成…</span></div>
    `;
  }

  function renderError(message) {
    const out = document.getElementById("diagOutput");
    if (!out) return;
    out.innerHTML = `
      <div class="diag-result is-error">
        <div class="diag-result-head">
          <b>诊断未通过</b>
          <span class="badge idle">需要处理</span>
        </div>
        <div class="diag-item tone-warn"><small>失败原因</small><b>${escapeHtml(message || "未知错误")}</b></div>
      </div>
    `;
  }

  function renderResult(data) {
    const out = document.getElementById("diagOutput");
    if (!out) return;
    const audio = data.audio || {};
    const timing = data.timing || {};
    const external = data.external || {};
    const checks = data.checks || [];
    const passed = checks.filter((item) => item.ok).length;
    const valueText = [
      audio.format || "未知格式",
      rateText(audio.sample_rate),
      channelText(audio.channels),
      audio.duration_known ? `${Number(audio.duration_seconds).toFixed(2)}s` : "时长未知",
      formatSize(audio.size_bytes),
    ].join(" · ");
    const backendText = external.resolved
      ? [external.plugin, external.tool, external.method].filter(Boolean).join(" / ")
      : (data.backend_label || data.backend || "--");
    const checksHtml = checks.map((item) => `
      <div class="diag-check ${item.ok ? "ok" : "warn"}">
        <span class="mark">${item.ok ? "✓" : "!"}</span>
        <span>${escapeHtml(item.label)}${item.note ? ` <small>（${escapeHtml(item.note)}）</small>` : ""}</span>
      </div>
    `).join("");
    out.innerHTML = `
      <div class="diag-result">
        <div class="diag-result-head">
          <b>${data.ok ? "诊断完成" : "诊断未通过"} · ${escapeHtml(data.backend_label || "--")}</b>
          <span class="badge ${data.ok ? "ok" : "idle"}">${passed}/${checks.length} 项通过</span>
        </div>
        <div class="diag-summary">
          <span>实际后端 <b>${escapeHtml(backendText)}</b></span>
          <span>文本转换 <b>${Number(timing.convert_seconds || 0).toFixed(2)}s</b></span>
          <span>合成 <b>${Number(timing.synthesize_seconds || 0).toFixed(2)}s</b></span>
          <span>总计 <b>${Number(timing.total_seconds || 0).toFixed(2)}s</b></span>
        </div>

        <div class="diag-player">
          <button type="button" class="diag-play" id="diagPlayBtn" data-diag="play" title="试听" ${data.audio_data_url ? "" : "disabled"}>▶</button>
          <div class="diag-wave" id="diagWave">
            ${WAVE_BARS.map((height) => `<i style="height:${height}%"></i>`).join("")}
          </div>
          <span class="diag-timer" id="diagTimer">00:00.0 / ${audio.duration_known ? formatSeconds(audio.duration_seconds) : "--:--"}</span>
        </div>

        <div class="diag-grid2">
          <div class="diag-item${audio.size_bytes ? " tone-ok" : " tone-warn"}">
            <small>音频文件</small>
            <b>${escapeHtml(valueText)}</b>
          </div>
          <div class="diag-item">
            <small>输出路径</small>
            <b><code>${escapeHtml(audio.path || "--")}</code></b>
          </div>
          <div class="diag-item${audio.duration_known ? " tone-ok" : " tone-warn"}">
            <small>嘴型 / 字幕联动</small>
            <b>${audio.duration_known ? "可同步：能读到音频时长，嘴型与字幕按它对齐" : "不可同步：读不到时长，嘴型联动会跳过这条音频"}</b>
          </div>
          <div class="diag-item">
            <small>回退策略</small>
            <b>${data.fallback_used ? "外部服务不可用，已回退会话 TTS Provider" : (external.configured ? "本次未触发回退" : "当前后端不使用外部服务")}</b>
          </div>
        </div>

        <div class="diag-checks">${checksHtml}</div>
        <div class="diag-tip">诊断只合成一次音频：不会发送消息、不写直播记忆、不占自动回应限流；重复点击可以对比不同参数下的听感与耗时。</div>
      </div>
    `;
  }

  function renderOutput() {
    if (state.running) return renderProgress();
    if (state.error) return renderError(state.error);
    if (state.result) return renderResult(state.result);
    return renderIdle();
  }

  function updatePlayIcon() {
    const button = document.getElementById("diagPlayBtn");
    if (button) button.textContent = state.playing ? "⏸" : "▶";
  }

  function updateWaveProgress(ratio) {
    const wave = document.getElementById("diagWave");
    if (!wave) return;
    const bars = wave.querySelectorAll("i");
    const played = Math.round(bars.length * Math.max(0, Math.min(1, ratio || 0)));
    bars.forEach((bar, index) => bar.classList.toggle("played", index < played));
  }

  function updateTimer(audio) {
    const timer = document.getElementById("diagTimer");
    if (!timer) return;
    const current = Number(audio?.currentTime) || 0;
    const duration = Number(audio?.duration);
    const total = Number.isFinite(duration) && duration > 0 ? formatSeconds(duration) : "--:--";
    timer.textContent = `${formatSeconds(current)} / ${total}`;
  }

  function releaseAudio() {
    if (state.audioEl) {
      try {
        state.audioEl.pause();
      } catch (error) {
        /* 忽略：暂停失败不影响诊断流程 */
      }
    }
    state.audioEl = null;
    state.playing = false;
  }

  function attachAudio(url) {
    releaseAudio();
    if (!url) return;
    const audio = new Audio(url);
    audio.preload = "metadata";
    audio.addEventListener("timeupdate", () => {
      updateTimer(audio);
      const duration = Number(audio.duration);
      if (Number.isFinite(duration) && duration > 0) {
        updateWaveProgress(audio.currentTime / duration);
      }
    });
    audio.addEventListener("ended", () => {
      state.playing = false;
      updatePlayIcon();
      updateWaveProgress(0);
    });
    state.audioEl = audio;
  }

  async function run(withLocalPlay = false) {
    if (state.running) return;
    const text = document.getElementById("diagText")?.value?.trim() || state.text;
    state.text = text;
    if (!text) {
      state.error = "请先填写要合成的诊断文本";
      state.result = null;
      renderOutput();
      return;
    }
    state.running = true;
    state.error = "";
    state.result = null;
    state.pushOverlay = Boolean(document.getElementById("diagPushOverlay")?.checked);
    state.localPlay = withLocalPlay || Boolean(document.getElementById("diagLocalPlay")?.checked);
    const runBtn = document.getElementById("diagRunBtn");
    const runPlayBtn = document.getElementById("diagRunPlayBtn");
    if (runBtn) runBtn.disabled = true;
    if (runPlayBtn) runPlayBtn.disabled = true;
    renderOutput();
    try {
      const data = await LivePageApi.post("/tts/diagnose", {
        text,
        push_overlay: state.pushOverlay,
        local_play: state.localPlay,
      });
      state.result = data || null;
      state.lastRunAt = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    } catch (error) {
      state.error = error?.message || String(error);
    } finally {
      state.running = false;
      if (runBtn) runBtn.disabled = false;
      if (runPlayBtn) runPlayBtn.disabled = false;
      const stamp = document.getElementById("diagStamp");
      if (stamp && state.lastRunAt) {
        stamp.textContent = `最近一次诊断：${state.lastRunAt}${state.localPlay ? "（含本机播放）" : ""}`;
      }
      renderOutput();
      attachAudio(state.result?.audio_data_url || "");
    }
  }

  function updateBackendBadge() {
    const badge = document.getElementById("diagBackendBadge");
    if (!badge) return;
    const result = state.result;
    if (result) {
      badge.textContent = result.ok
        ? `上次后端：${result.backend_label || result.backend || "--"}`
        : "上次诊断未通过";
      badge.className = `badge ${result.ok ? "ok" : "idle"}`;
      return;
    }
    badge.textContent = "运行后显示实际后端";
    badge.className = "badge idle";
  }

  function bindCard(card) {
    if (card.dataset.bound === "1") return;
    card.dataset.bound = "1";
    card.addEventListener("input", (event) => {
      if (event.target instanceof HTMLTextAreaElement) state.text = event.target.value;
    });
    card.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("[data-diag],[data-diag-fill]") : null;
      if (!button) return;
      const fill = button.dataset.diagFill;
      if (fill) {
        state.text = FILLS[fill] || state.text;
        const node = document.getElementById("diagText");
        if (node) node.value = state.text;
        return;
      }
      const action = button.dataset.diag;
      if (action === "run") run(false);
      else if (action === "run-play") run(true);
      else if (action === "play") {
        const audio = state.audioEl;
        if (!audio) return;
        if (state.playing) {
          audio.pause();
          state.playing = false;
          updatePlayIcon();
        } else {
          audio.play().then(() => {
            state.playing = true;
            updatePlayIcon();
          }).catch(() => {
            state.playing = false;
            updatePlayIcon();
          });
        }
      } else if (action === "reload") {
        releaseAudio();
        state.result = null;
        state.error = "";
        state.lastRunAt = "";
        const stamp = document.getElementById("diagStamp");
        if (stamp) stamp.textContent = "";
        renderOutput();
        updateBackendBadge();
      }
    });
  }

  function mount(container, options = {}) {
    if (!container) return;
    const anchor = document.getElementById(ANCHOR_ID);
    let card = document.getElementById(CARD_ID);
    if (!card) {
      const holder = document.createElement("div");
      holder.innerHTML = cardHtml();
      card = holder.firstElementChild;
      if (anchor) anchor.after(card);
      else container.appendChild(card);
    }
    bindCard(card);
    if (!state.switchesReady) {
      state.pushOverlay = Boolean(options.webPlayback);
      state.localPlay = Boolean(options.localPlayback);
      state.switchesReady = true;
    }
    const textNode = document.getElementById("diagText");
    if (textNode && textNode.value !== state.text) textNode.value = state.text;
    const overlayBox = document.getElementById("diagPushOverlay");
    if (overlayBox) overlayBox.checked = state.pushOverlay;
    const localBox = document.getElementById("diagLocalPlay");
    if (localBox) localBox.checked = state.localPlay;
    renderOutput();
    updateBackendBadge();
    if (state.result?.audio_data_url && !state.audioEl) attachAudio(state.result.audio_data_url);
  }

  return { mount, cardId: CARD_ID };
})();

// 顶层 const 不会挂到 window，这里显式导出给 app.js 使用
window.LiveAudioDiagnose = LiveAudioDiagnose;
