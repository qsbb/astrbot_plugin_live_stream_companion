const LivePageApi = (() => {
  const HTTP_API = "/astrbot_plugin_live_stream_companion/page";
  const PAGE_ENDPOINT_PREFIX = "page";

  async function get(path) {
    return request(path, { method: "GET" });
  }

  async function post(path, body) {
    return request(path, { method: "POST", body });
  }

  async function request(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    let payload;
    const bridge = await waitForBridge();
    if (bridge && typeof bridge.apiGet === "function" && typeof bridge.apiPost === "function") {
      payload = await bridgeRequest(bridge, path, method, options.body);
    } else if (new URLSearchParams(window.location.search).get("debug_http") === "1") {
      payload = await httpRequest(path, method, options.body);
    } else {
      throw new Error("未检测到 AstrBot 官方插件 Page 桥接，请从 AstrBot 后台的插件拓展页打开");
    }
    return unwrapPayload(payload);
  }

  async function waitForBridge() {
    for (let i = 0; i < 24; i += 1) {
      const bridge = getBridge();
      if (bridge && typeof bridge.apiGet === "function" && typeof bridge.apiPost === "function") {
        return bridge;
      }
      await sleep(80);
    }
    return null;
  }

  function getBridge() {
    if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
    try {
      if (window.parent && window.parent !== window && window.parent.AstrBotPluginPage) {
        return window.parent.AstrBotPluginPage;
      }
    } catch (error) {
      return null;
    }
    return null;
  }

  async function bridgeRequest(bridge, path, method, body) {
    const url = new URL(path, "https://astrbot-plugin-page.local/");
    const endpoint = `${PAGE_ENDPOINT_PREFIX}/${url.pathname.replace(/^\/+/, "")}`.replace(/\/+/g, "/");
    if (method === "GET") {
      const params = Object.fromEntries(url.searchParams.entries());
      return bridge.apiGet(endpoint, Object.keys(params).length ? params : undefined);
    }
    return bridge.apiPost(endpoint, body || {});
  }

  async function httpRequest(path, method, body) {
    const response = await fetch(`${HTTP_API}${path}`, {
      method,
      cache: "no-store",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    return response.json();
  }

  function unwrapPayload(payload) {
    if (typeof payload === "string") {
      try {
        payload = JSON.parse(payload);
      } catch (error) {
        throw new Error(payload);
      }
    }
    if (!payload || payload.success === false) {
      throw new Error(payload?.error || "请求失败");
    }
    return payload.data ?? payload;
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  return { get, post };
})();
