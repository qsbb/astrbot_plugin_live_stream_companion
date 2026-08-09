const LiveConfigForm = (() => {
  function renderGroups(container, groups, schema, values, options = {}) {
    const includeGroup = options.includeGroup || (() => true);
    const includeKey = options.includeKey || (() => true);
    container.innerHTML = groups
      .filter(includeGroup)
      .map((group) => {
        const keys = (group.keys || []).filter((key) => includeKey(key, group));
        if (!keys.length) return "";
        return `
          <article class="config-card" data-group="${escapeHtml(group.id)}">
            <div class="config-card-head">
              <div>
                <h2>${escapeHtml(group.title)}</h2>
                <p>${escapeHtml(group.description || "")}</p>
              </div>
            </div>
            <div class="field-grid">
              ${keys.map((key) => renderField(key, schema[key], values[key])).join("")}
            </div>
          </article>
        `;
      })
      .join("");
  }

  function renderField(key, meta = {}, value) {
    const type = meta.type || "string";
    const label = meta.description || key;
    const hint = meta.hint || "";
    const id = `cfg-${key}`;
    const slider = meta.slider || {};
    const options = meta.options || defaultOptionsForKey(key);
    const current = value ?? meta.default ?? "";
    if (key === "soullink_vts_mapping") {
      return `
        <div class="field field-wide mapping-config-link">
          <span><b>${escapeHtml(label)}</b>${hint ? `<small>${escapeHtml(hint)}</small>` : ""}</span>
          <button type="button" data-open-soullink-mapping>打开高级参数校准</button>
        </div>
      `;
    }
    if (type === "bool") {
      return `
        <label class="field field-toggle" for="${escapeHtml(id)}">
          <span>
            <b>${escapeHtml(label)}</b>
            ${hint ? `<small>${escapeHtml(hint)}</small>` : ""}
          </span>
          <input id="${escapeHtml(id)}" class="config-control" name="${escapeHtml(key)}" type="checkbox" ${current ? "checked" : ""}>
        </label>
      `;
    }
    if (type === "text") {
      return `
        <label class="field field-wide" for="${escapeHtml(id)}">
          <span><b>${escapeHtml(label)}</b>${hint ? `<small>${escapeHtml(hint)}</small>` : ""}</span>
          <textarea id="${escapeHtml(id)}" class="config-control" name="${escapeHtml(key)}" rows="4">${escapeHtml(current)}</textarea>
        </label>
      `;
    }
    if (options.length) {
      return `
        <label class="field" for="${escapeHtml(id)}">
          <span><b>${escapeHtml(label)}</b>${hint ? `<small>${escapeHtml(hint)}</small>` : ""}</span>
          <select id="${escapeHtml(id)}" class="config-control" name="${escapeHtml(key)}">
            ${options.map((item) => `<option value="${escapeHtml(item)}" ${String(current) === String(item) ? "selected" : ""}>${escapeHtml(optionLabelForKey(key, item))}</option>`).join("")}
          </select>
        </label>
      `;
    }
    if (isColorKey(key)) {
      const safeColor = /^#[0-9a-f]{6}$/i.test(String(current)) ? current : "#ffffff";
      return `
        <label class="field" for="${escapeHtml(id)}">
          <span><b>${escapeHtml(label)}</b>${hint ? `<small>${escapeHtml(hint)}</small>` : ""}</span>
          <input id="${escapeHtml(id)}" class="config-control color-control" name="${escapeHtml(key)}" type="color" value="${escapeHtml(safeColor)}">
        </label>
      `;
    }
    if (type === "int" || type === "float") {
      const step = slider.step ?? (type === "float" ? 0.1 : 1);
      return `
        <label class="field" for="${escapeHtml(id)}">
          <span><b>${escapeHtml(label)}</b>${hint ? `<small>${escapeHtml(hint)}</small>` : ""}</span>
          <input id="${escapeHtml(id)}" class="config-control" name="${escapeHtml(key)}" type="number"
            value="${escapeHtml(current)}" step="${escapeHtml(step)}"
            ${slider.min !== undefined ? `min="${escapeHtml(slider.min)}"` : ""}
            ${slider.max !== undefined ? `max="${escapeHtml(slider.max)}"` : ""}>
        </label>
      `;
    }
    return `
      <label class="field" for="${escapeHtml(id)}">
        <span><b>${escapeHtml(label)}</b>${hint ? `<small>${escapeHtml(hint)}</small>` : ""}</span>
        <input id="${escapeHtml(id)}" class="config-control" name="${escapeHtml(key)}" type="text" value="${escapeHtml(current)}">
      </label>
    `;
  }

  function collectValues(container, schema, fallbackValues = {}) {
    const values = { ...fallbackValues };
    container?.querySelectorAll(".config-control").forEach((control) => {
      const meta = schema[control.name] || {};
      if (meta.type === "bool") {
        values[control.name] = control.checked;
      } else if (meta.type === "int") {
        values[control.name] = Number.parseInt(control.value || "0", 10);
      } else if (meta.type === "float") {
        values[control.name] = Number.parseFloat(control.value || "0");
      } else {
        values[control.name] = control.value;
      }
    });
    return values;
  }

  function defaultValues(schema) {
    return Object.fromEntries(Object.entries(schema).map(([key, meta]) => [key, meta.default]));
  }

  function defaultOptionsForKey(key) {
    const options = {
      bilibili_type: ["web", "laplace", "open_live"],
      bili_live_auto_reply_mode: ["native", "direct"],
      subtitle_scope: ["bili_live", "twitch_live", "live", "all"],
      subtitle_position: ["bottom", "center", "top"],
      mouth_sync_mode: ["set", "add"],
      soullink_mode: ["emotion", "full"],
      soullink_motion_style: ["natural", "lively", "calm", "shy"],
    };
    return options[key] || [];
  }

  function optionLabelForKey(key, value) {
    const labels = {
      bilibili_type: { web: "Web 直播间", laplace: "Laplace 桥接", open_live: "B站开放平台" },
      bili_live_auto_reply_mode: { native: "AstrBot 原生流程", direct: "直接调用模型" },
      subtitle_scope: { bili_live: "仅 B站直播", twitch_live: "仅 Twitch 直播", live: "全部直播来源", all: "所有 Bot 回复" },
      subtitle_position: { bottom: "底部", center: "中部", top: "顶部" },
      mouth_sync_mode: { set: "覆盖参数", add: "叠加参数" },
      soullink_mode: { emotion: "情绪表演", full: "完整表演" },
      soullink_motion_style: { natural: "自然", lively: "活泼", calm: "平静", shy: "害羞" },
    };
    return labels[key]?.[value] || value;
  }

  function isColorKey(key) {
    return key.endsWith("_color");
  }

  return {
    collectValues,
    defaultValues,
    renderGroups,
  };
})();
