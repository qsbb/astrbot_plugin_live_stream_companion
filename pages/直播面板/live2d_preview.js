const Live2DPreview = (() => {
  const SCRIPT_URL = document.currentScript?.src
    || document.querySelector('script[src*="live2d_preview.js"]')?.src
    || window.location.href;
  const PIXI_URL = resolveBundledAsset("./vendor/live2d_preview/pixi.min.js");
  const LIVE2D_URL = resolveBundledAsset("./vendor/live2d_preview/pixi-live2d-display-cubism4.min.js");
  const CUBISM_CORE_URL = "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js";
  const MIN_ZOOM = 0.25;
  const MAX_ZOOM = 4;

  let app = null;
  let model = null;
  let host = null;
  let fallback = null;
  let resizeObserver = null;
  let latestFacs = {};
  let displayedFacs = {};
  let lastParameterUpdateAt = 0;
  let beforeModelUpdate = null;
  let statusListener = () => {};
  let zoomListener = () => {};
  let loadingPromise = null;
  let zoomFactor = 1;
  let activePointers = new Map();
  let pinchStart = null;

  function resolveBundledAsset(relativePath) {
    try {
      const base = new URL(SCRIPT_URL, window.location.href);
      const target = new URL(relativePath, base);
      base.searchParams.forEach((value, key) => {
        if (!target.searchParams.has(key)) target.searchParams.append(key, value);
      });
      return target.href;
    } catch (error) {
      return relativePath;
    }
  }

  function init(options = {}) {
    host = options.host || null;
    fallback = options.fallback || null;
    statusListener = typeof options.onStatus === "function" ? options.onStatus : () => {};
    zoomListener = typeof options.onZoomChange === "function" ? options.onZoomChange : () => {};
    bindZoomGestures();
    notifyZoom();
    setStatus("idle", "未导入模型，当前使用插件图像预览情绪状态。");
  }

  async function loadFiles(fileList) {
    const files = Array.from(fileList || []);
    const modelFile = files.find((file) => file.name.toLowerCase().endsWith(".model3.json"));
    if (!modelFile) {
      throw new Error("所选文件夹中没有 .model3.json 模型文件");
    }
    if (!files.some((file) => file.name.toLowerCase().endsWith(".moc3"))) {
      throw new Error("所选文件夹中没有 .moc3 模型数据");
    }
    if (!host) throw new Error("Live2D 预览容器尚未初始化");

    setStatus("loading", `正在加载 ${modelFile.name}...`);
    try {
      await ensureRuntime();
      host.hidden = false;
      ensureApplication();
      removeModel({ keepStatus: true });
      host.hidden = false;

      const Live2DModel = window.PIXI?.live2d?.Live2DModel;
      if (!Live2DModel) throw new Error("Live2D 渲染器未正确加载");
      // VTube Studio may add files such as items_pinned_to_model.json. The
      // upstream loader also treats those as model settings, so put the real
      // Cubism settings file first.
      const preparedFiles = await prepareModelFiles(files, modelFile);
      model = await Live2DModel.from(preparedFiles, {
        autoInteract: false,
        autoUpdate: true,
      });
      model.anchor?.set(0.5, 0.5);
      if (model.internalModel) {
        model.internalModel.eyeBlink = undefined;
        model.internalModel.breath = undefined;
      }
      beforeModelUpdate = () => applyParameters();
      model.internalModel?.on?.("beforeModelUpdate", beforeModelUpdate);
      app.stage.addChild(model);
      resizeCanvas();

      if (fallback) fallback.hidden = true;
      setStatus("ready", `${modelFile.name} 已加载，Soullink 参数正在驱动页面预览。`, modelFile.name);
      return modelFile.name;
    } catch (error) {
      removeModel({ keepStatus: true });
      setStatus("error", friendlyError(error));
      throw error;
    }
  }

  function setFacs(facs) {
    latestFacs = facs && typeof facs === "object" ? facs : {};
    if (!Object.keys(displayedFacs).length) displayedFacs = { ...latestFacs };
  }

  function setZoom(value) {
    const nextZoom = normalizedZoom(value);
    if (Math.abs(nextZoom - zoomFactor) < 0.0001) return zoomFactor;
    zoomFactor = nextZoom;
    fitModel();
    notifyZoom();
    return zoomFactor;
  }

  function zoomBy(delta) {
    return setZoom(zoomFactor + number(delta));
  }

  function resetZoom() {
    return setZoom(1);
  }

  function getZoom() {
    return zoomFactor;
  }

  function removeModel(options = {}) {
    if (model) {
      if (beforeModelUpdate) {
        model.internalModel?.off?.("beforeModelUpdate", beforeModelUpdate);
      }
      app?.stage?.removeChild(model);
      model.destroy({ children: true, texture: true, baseTexture: true });
      model = null;
      beforeModelUpdate = null;
    }
    activePointers.clear();
    pinchStart = null;
    zoomFactor = 1;
    notifyZoom();
    if (host) host.hidden = true;
    if (fallback) fallback.hidden = false;
    if (!options.keepStatus) {
      setStatus("idle", "未导入模型，当前使用插件图像预览情绪状态。");
    }
  }

  function hasModel() {
    return Boolean(model);
  }

  function ensureApplication() {
    if (app) return;
    app = new window.PIXI.Application({
      resizeTo: host,
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: true,
      resolution: Math.min(window.devicePixelRatio || 1, 2),
    });
    app.view.className = "live2d-preview-canvas";
    host.appendChild(app.view);
    resizeObserver = new ResizeObserver(resizeCanvas);
    resizeObserver.observe(host);
    resizeCanvas();
  }

  function bindZoomGestures() {
    if (!host || host.dataset.live2dZoomBound === "true") return;
    host.dataset.live2dZoomBound = "true";
    host.addEventListener("wheel", handleWheelZoom, { passive: false });
    host.addEventListener("pointerdown", handlePointerDown);
    host.addEventListener("pointermove", handlePointerMove);
    host.addEventListener("pointerup", handlePointerEnd);
    host.addEventListener("pointercancel", handlePointerEnd);
  }

  function handleWheelZoom(event) {
    if (!model) return;
    const unit = event.deltaMode === 1 ? 16 : (event.deltaMode === 2 ? host.clientHeight : 1);
    const nextZoom = normalizedZoom(zoomFactor * Math.exp(-event.deltaY * unit * 0.0015));
    if (Math.abs(nextZoom - zoomFactor) < 0.0001) return;
    event.preventDefault();
    setZoom(nextZoom);
  }

  function handlePointerDown(event) {
    if (!model || event.pointerType === "mouse") return;
    activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (activePointers.size === 2) {
      pinchStart = {
        distance: pointerDistance(),
        zoom: zoomFactor,
      };
    }
  }

  function handlePointerMove(event) {
    if (!activePointers.has(event.pointerId)) return;
    activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (activePointers.size !== 2 || !pinchStart?.distance) return;
    event.preventDefault();
    setZoom(pinchStart.zoom * pointerDistance() / pinchStart.distance);
  }

  function handlePointerEnd(event) {
    activePointers.delete(event.pointerId);
    if (activePointers.size < 2) pinchStart = null;
  }

  function pointerDistance() {
    const [first, second] = Array.from(activePointers.values());
    return first && second ? Math.hypot(second.x - first.x, second.y - first.y) : 0;
  }

  async function ensureRuntime() {
    if (window.PIXI?.live2d?.Live2DModel && window.Live2DCubismCore) return;
    if (loadingPromise) return loadingPromise;
    loadingPromise = (async () => {
      window.process ||= { env: { NODE_ENV: "production" } };
      window.process.env ||= { NODE_ENV: "production" };
      await loadScript(PIXI_URL, () => Boolean(window.PIXI));
      await loadScript(CUBISM_CORE_URL, () => Boolean(window.Live2DCubismCore));
      await loadScript(LIVE2D_URL, () => Boolean(window.PIXI?.live2d?.Live2DModel));
    })();
    try {
      await loadingPromise;
    } finally {
      loadingPromise = null;
    }
  }

  function loadScript(src, ready) {
    if (ready()) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const existing = document.querySelector(`script[data-live2d-src="${src}"]`);
      if (existing) {
        existing.addEventListener("load", () => ready() ? resolve() : reject(new Error(`运行库未初始化: ${src}`)), { once: true });
        existing.addEventListener("error", () => reject(new Error(`运行库加载失败: ${src}`)), { once: true });
        return;
      }
      const script = document.createElement("script");
      script.src = src;
      script.async = true;
      script.dataset.live2dSrc = src;
      script.onload = () => ready() ? resolve() : reject(new Error(`运行库未初始化: ${src}`));
      script.onerror = () => reject(new Error(`运行库加载失败: ${src}`));
      document.head.appendChild(script);
    });
  }

  function applyParameters() {
    const core = model?.internalModel?.coreModel;
    if (!core?.setParameterValueById) return;
    const now = performance.now();
    const deltaSeconds = lastParameterUpdateAt
      ? Math.min(0.1, Math.max(0.001, (now - lastParameterUpdateAt) / 1000))
      : 1 / 60;
    lastParameterUpdateAt = now;
    const factor = 1 - Math.exp(-8 * deltaSeconds);
    const keys = new Set([...Object.keys(displayedFacs), ...Object.keys(latestFacs)]);
    keys.forEach((key) => {
      const current = number(displayedFacs[key], number(latestFacs[key]));
      displayedFacs[key] = current + (number(latestFacs[key]) - current) * factor;
    });

    const facs = displayedFacs;
    const eyeOpen = number(facs.eyeOpen, 1);
    const eyeOpenLeft = Math.max(
      0,
      eyeOpen * (1 - number(facs.eyeBlinkL)) - number(facs.eyeSquint) * 0.3,
    );
    const eyeOpenRight = Math.max(
      0,
      eyeOpen * (1 - number(facs.eyeBlinkR)) - number(facs.eyeSquint) * 0.3,
    );
    const mouthForm = clamp(
      number(facs.mouthSmile) - 0.04 - number(facs.mouthFrown),
      -1,
      1,
    );
    const brow = clamp(
      number(facs.browInnerUp) * 0.55 + number(facs.browOuterUp) * 0.45 - number(facs.browDown),
      -1,
      1,
    );
    const values = {
      ParamAngleX: (number(facs.headX) + number(facs.bodyX) * 0.65) * 36,
      ParamAngleY: (number(facs.headY) + number(facs.bodyY) * 0.6) * 32,
      ParamAngleZ: (number(facs.headZ) + number(facs.bodyZ) * 0.65) * 34,
      ParamBodyAngleX: number(facs.bodyX ?? facs.headX) * 10,
      ParamEyeBallX: number(facs.gazeX),
      ParamEyeBallY: number(facs.gazeY),
      ParamEyeLOpen: eyeOpenLeft,
      ParamEyeROpen: eyeOpenRight,
      ParamEyeLSmile: Math.max(0, number(facs.eyeSmile)),
      ParamEyeRSmile: Math.max(0, number(facs.eyeSmile)),
      ParamMouthForm: mouthForm,
      ParamMouthOpenY: clamp(number(facs.mouthOpen), 0, 1),
      ParamBrowLY: brow,
      ParamBrowRY: brow,
      ParamBreath: clamp(number(facs.breath), 0, 1),
      ParamCheek: clamp(number(facs.blush), 0, 1),
    };
    Object.entries(values).forEach(([id, value]) => core.setParameterValueById(id, value, 1));
  }

  function fitModel() {
    if (!model || !host) return;
    const width = host.clientWidth || 1;
    const height = host.clientHeight || 1;
    const originalWidth = model.internalModel?.originalWidth || model.width || 1;
    const originalHeight = model.internalModel?.originalHeight || model.height || 1;
    const scale = Math.min(width / originalWidth, height / originalHeight) * 0.94 * zoomFactor;
    model.scale.set(scale);
    model.x = width * 0.5;
    model.y = height * 0.54;
  }

  function resizeCanvas() {
    if (!app || !host || host.hidden) return;
    const width = Math.max(1, host.clientWidth);
    const height = Math.max(1, host.clientHeight);
    if (app.renderer.width !== width || app.renderer.height !== height) {
      app.renderer.resize(width, height);
    }
    fitModel();
  }

  function setStatus(status, message, name = "") {
    statusListener({
      status,
      message,
      name,
      hasModel: Boolean(model),
      zoom: zoomFactor,
      minZoom: MIN_ZOOM,
      maxZoom: MAX_ZOOM,
    });
  }

  function notifyZoom() {
    zoomListener({
      zoom: zoomFactor,
      minZoom: MIN_ZOOM,
      maxZoom: MAX_ZOOM,
      hasModel: Boolean(model),
    });
  }

  function normalizedZoom(value) {
    return clamp(number(value, 1), MIN_ZOOM, MAX_ZOOM);
  }

  function friendlyError(error) {
    const message = error?.message || String(error);
    if (message.includes(CUBISM_CORE_URL) || message.includes("Cubism")) {
      return "Cubism Core 加载失败，请检查网络后重新导入模型。";
    }
    return `模型加载失败：${message}`;
  }

  async function prepareModelFiles(files, modelFile) {
    const orderedFiles = [modelFile, ...files.filter((file) => file !== modelFile)];
    const hasUnicodePath = orderedFiles.some((file) => /[^\x00-\x7f]/.test(file.webkitRelativePath || file.name));
    if (!hasUnicodePath) return orderedFiles;

    const modelData = JSON.parse(await modelFile.text());
    const modelPath = normalizePath(modelFile.webkitRelativePath || modelFile.name);
    const modelDirectory = modelPath.includes("/") ? modelPath.slice(0, modelPath.lastIndexOf("/")) : "";
    const pathMap = new Map();
    const virtualRows = orderedFiles.map((file, index) => {
      const sourcePath = normalizePath(file.webkitRelativePath || file.name);
      const suffix = file === modelFile ? ".model3.json" : safeFileSuffix(file.name);
      const virtualName = file === modelFile ? `model${suffix}` : `asset_${String(index).padStart(3, "0")}${suffix}`;
      const virtualPath = `live2d_preview/${virtualName}`;
      pathMap.set(sourcePath, virtualName);
      return { file, virtualName, virtualPath };
    });

    modelData.FileReferences = rewriteModelReferences(
      modelData.FileReferences,
      modelDirectory,
      pathMap,
    );

    return virtualRows.map(({ file, virtualName, virtualPath }) => {
      const content = file === modelFile ? JSON.stringify(modelData) : file;
      const clone = new File([content], virtualName, {
        type: file.type,
        lastModified: file.lastModified,
      });
      Object.defineProperty(clone, "webkitRelativePath", { value: virtualPath });
      return clone;
    });
  }

  function rewriteModelReferences(value, modelDirectory, pathMap) {
    if (typeof value === "string") {
      const sourcePath = normalizePath(modelDirectory ? `${modelDirectory}/${value}` : value);
      return pathMap.get(sourcePath) || value;
    }
    if (Array.isArray(value)) {
      return value.map((item) => rewriteModelReferences(item, modelDirectory, pathMap));
    }
    if (value && typeof value === "object") {
      return Object.fromEntries(
        Object.entries(value).map(([key, item]) => [
          key,
          rewriteModelReferences(item, modelDirectory, pathMap),
        ]),
      );
    }
    return value;
  }

  function safeFileSuffix(name) {
    const match = String(name || "").match(/(\.[a-z0-9]+(?:\.json)?)$/i);
    return match ? match[1].toLowerCase() : ".bin";
  }

  function normalizePath(path) {
    const output = [];
    String(path || "").replace(/\\/g, "/").split("/").forEach((part) => {
      if (!part || part === ".") return;
      if (part === "..") output.pop();
      else output.push(part);
    });
    return output.join("/");
  }

  function number(value, fallbackValue = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallbackValue;
  }

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  return {
    init,
    loadFiles,
    setFacs,
    removeModel,
    hasModel,
    setZoom,
    zoomBy,
    resetZoom,
    getZoom,
  };
})();

window.Live2DPreview = Live2DPreview;
