(() => {
  "use strict";

  const bridge = window.AstrBotPluginPage;
  const $ = (id) => document.getElementById(id);
  const LIST_FIELDS = new Set([
    "main_command_aliases", "image2_command_aliases", "grok_command_aliases",
    "grok2_command_aliases", "video_command_aliases", "main_gif_aliases",
    "main_gif2_aliases", "image2_gif_aliases", "image2_gif2_aliases",
    "backup_api_keys", "image2_backup_api_keys", "grok_backup_api_keys",
    "video_backup_api_keys", "reaction_emoji_list", "group_blacklist",
    "sensitive_categories",
  ]);
  const SECRET_FIELDS = new Set([
    "api_key", "backup_api_keys", "image2_api_key", "image2_backup_api_keys",
    "grok_api_key", "grok_backup_api_keys", "video_api_key", "video_backup_api_keys",
  ]);
  const FORM_IDS = [
    "main_command_aliases", "image2_command_aliases", "grok_command_aliases",
    "grok2_command_aliases", "video_command_aliases", "main_gif_aliases",
    "main_gif2_aliases", "image2_gif_aliases", "image2_gif2_aliases",
    "api_base", "api_key", "backup_api_keys", "model", "image2_api_base",
    "image2_api_key", "image2_backup_api_keys", "image2_model", "image2_size",
    "image2_api_mode", "grok_api_base", "grok_api_key", "grok_backup_api_keys",
    "video_api_base", "video_api_key", "video_backup_api_keys", "video_model",
    "video_duration", "video_aspect_ratio", "video_resolution", "video_poll_interval",
    "video_timeout", "video_max_concurrent", "video_max_concurrent_per_user",
    "video_cooldown_seconds", "video_cleanup_delay", "video_prompt_enhance",
    "video_style_prompt", "temperature", "timeout", "max_retry", "retry_delay",
    "animated_reference_frame", "enable_reply_image", "enable_at_avatar",
    "label_images", "prefer_chinese_text", "prefer_cn_locale", "style_prompt",
    "reply_with_quote", "reaction_emoji_enabled", "reaction_emoji_strategy",
    "reaction_emoji_list", "cooldown_seconds", "daily_quota", "daily_quota_main",
    "daily_quota_image2", "daily_quota_video", "cost_main_usd", "cost_image2_usd",
    "cost_video_usd", "group_blacklist", "cleanup_delay", "gif_frame_size", "gif_fps",
    "gif_max_bytes", "video_gif_max_duration", "video_gif_max_dimension", "video_gif_fps",
    "video_gif_max_bytes", "sensitive_filter_enabled", "sensitive_lexicon_path",
    "sensitive_categories",
  ];

  function unwrap(payload) {
    return payload && typeof payload === "object" && "data" in payload ? payload.data : payload;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function readList(value) {
    return String(value ?? "")
      .replaceAll("，", ",")
      .split(/[\n,;|]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function setValue(id, value) {
    const element = $(id);
    if (!element || value === undefined || value === null) return;
    if (element.type === "checkbox") {
      element.checked = Boolean(value);
    } else if (LIST_FIELDS.has(id)) {
      element.value = Array.isArray(value) ? value.join("\n") : String(value);
    } else {
      element.value = value;
    }
  }

  function showToast(message, kind = "ok") {
    const toast = $("toast");
    toast.textContent = message;
    toast.dataset.kind = kind;
    toast.classList.add("visible");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 3800);
  }

  function setSaveState(text, kind = "") {
    const state = $("save-state");
    state.textContent = text;
    state.dataset.kind = kind;
  }

  function formatQuota(item) {
    if (!item) return "--";
    return item.limit > 0 ? `${item.daily} / ${item.limit}` : `${item.daily} / ∞`;
  }

  function formatCost(item) {
    if (!item) return "--";
    return `$${Number(item.cost || 0).toFixed(4)} / 次 · 累计 ${item.total} 次`;
  }

  function renderCommandCatalog(commands = []) {
    const catalog = $("command-catalog");
    const hero = $("hero-commands");
    const primaryKeys = new Set(["main", "image2", "grok", "video"]);
    const primary = commands.filter((item) => primaryKeys.has(item.key));
    hero.innerHTML = primary.flatMap((item) => (item.names || []).slice(0, 4))
      .map((name) => `<span>/${escapeHtml(name)}</span>`).join("");
    catalog.innerHTML = commands.map((item) => {
      const names = item.names || [];
      const aliasCount = Math.max(0, names.length - 1);
      return `<article class="command-item" data-key="${escapeHtml(item.key)}">
        <div class="command-item-head"><strong>/${escapeHtml(item.primary || "")}</strong><small>${aliasCount} 个别名</small></div>
        <p>${escapeHtml(item.description || "")}</p>
        <div class="command-names">${names.map((name) => `<code>/${escapeHtml(name)}</code>`).join("")}</div>
      </article>`;
    }).join("");
  }

  function renderTasks(tasks = []) {
    const list = $("task-list");
    $("task-summary").textContent = tasks.length ? `最近 ${tasks.length} 条任务` : "暂无任务记录";
    if (!tasks.length) {
      list.innerHTML = '<tr><td colspan="7" class="task-empty">还没有生成任务记录</td></tr>';
      return;
    }
    list.innerHTML = tasks.map((task) => {
      const status = String(task.status || "unknown");
      const statusText = { running: "进行中", success: "成功", failed: "失败", interrupted: "中断" }[status] || status;
      const progress = Number(task.progress || 0);
      const code = task.request_id || task.code || "--";
      const duration = task.duration_seconds ? ` · ${task.duration_seconds}s` : "";
      return `<tr>
        <td>${escapeHtml(task.started_at || "--")}${escapeHtml(duration)}</td>
        <td><strong>${escapeHtml(task.channel || "--")}</strong><br /><span>${escapeHtml(task.command || "--")}</span></td>
        <td>${escapeHtml(task.model || "--")}</td>
        <td title="${escapeHtml(task.prompt || "")}">${escapeHtml(task.prompt || "（无文字提示）")}</td>
        <td><span class="task-status ${escapeHtml(status)}">${escapeHtml(statusText)}</span></td>
        <td class="task-progress">${progress}%</td>
        <td><span class="task-code" title="${escapeHtml(code)}">${escapeHtml(code)}</span></td>
      </tr>`;
    }).join("");
  }

  function fillDashboard(data) {
    const channels = data.channels || {};
    $("quota-main").textContent = formatQuota(channels.main);
    $("quota-image2").textContent = formatQuota(channels.image2);
    $("quota-video").textContent = formatQuota(channels.video);
    $("cost-main").textContent = formatCost(channels.main);
    $("cost-image2").textContent = formatCost(channels.image2);
    $("cost-video").textContent = formatCost(channels.video);

    const video = data.video || {};
    const limit = Number(video.limit || 0);
    const inflight = Number(video.inflight || 0);
    $("video-inflight").innerHTML = `${inflight}<span>/${limit}</span>`;
    $("video-bar").style.width = limit ? `${Math.min(100, inflight / limit * 100)}%` : "0%";
    $("runtime-note").textContent = limit
      ? `每用户上限 ${video.per_user_limit || 1} 个 · ${data.date || ""}`
      : "视频并发配置未读取";
    $("updated-at").textContent = data.date || "";

    const config = data.config || {};
    Object.keys(config).forEach((key) => {
      if (!key.endsWith("_configured") && !key.endsWith("_count") && !key.endsWith("_effective")) {
        setValue(key, config[key]);
      }
    });
    $("main-key-dot").classList.toggle("active", Boolean(config.api_key_configured));
    $("image2-key-dot").classList.toggle("active", Boolean(config.image2_api_key_configured));
    $("grok-key-dot").classList.toggle("active", Boolean(config.grok_api_key_effective_configured));
    $("video-key-dot").classList.toggle("active", Boolean(config.video_api_key_effective_configured));
    $("video-fallback-note").textContent = config.video_reuses_grok
      ? "当前视频地址和 Key 均留空，运行时复用 Grok 生图通道。"
      : "视频通道已配置独立覆盖项；未填写的字段仍会逐项复用 Grok 生图通道。";
    if (data.version) {
      $("footer-version").textContent = data.version;
      $("footer-version-bottom").textContent = data.version;
    }
    renderCommandCatalog(data.commands || []);
    renderTasks(data.tasks || []);
  }

  function collectUpdates() {
    const updates = {};
    FORM_IDS.forEach((id) => {
      const element = $(id);
      if (!element) return;
      if (SECRET_FIELDS.has(id) && !String(element.value || "").trim()) return;
      if (element.type === "checkbox") {
        updates[id] = element.checked;
      } else if (LIST_FIELDS.has(id)) {
        const values = readList(element.value);
        if (SECRET_FIELDS.has(id) && !values.length) return;
        updates[id] = values;
      } else if (element.type === "number") {
        if (element.value !== "") updates[id] = Number(element.value);
      } else {
        updates[id] = element.value;
      }
    });
    return updates;
  }

  async function loadDashboard() {
    setSaveState("读取中…");
    try {
      const response = unwrap(await bridge.apiGet("dashboard"));
      fillDashboard(response);
      setSaveState("已同步", "ok");
    } catch (error) {
      setSaveState("读取失败", "bad");
      showToast(`读取控制台数据失败：${error.message || error}`, "bad");
    }
  }

  async function saveConfig() {
    setSaveState("保存中…");
    try {
      const response = unwrap(await bridge.apiPost("config", { updates: collectUpdates() }));
      if (!response.ok) throw new Error(response.error || "保存失败");
      setSaveState("已保存 · 待重载", "ok");
      showToast("全部设置已保存，请在插件管理中重载康康图。", "ok");
    } catch (error) {
      setSaveState("保存失败", "bad");
      showToast(`保存失败：${error.message || error}`, "bad");
    }
  }

  async function testConnection(channel) {
    const result = $(`${channel}-test-result`);
    result.textContent = "测试中…";
    result.dataset.kind = "pending";
    try {
      const response = unwrap(await bridge.apiPost("test-connection", { channel }));
      if (!response.ok) throw new Error(response.error || `HTTP ${response.models_status || "?"}`);
      result.textContent = `正常 · ${(response.models || []).length} 个模型 · ${response.latency_ms}ms`;
      result.dataset.kind = "ok";
      showToast(`${channel} 通道连接正常，未创建生成任务。`, "ok");
    } catch (error) {
      result.textContent = "连接失败";
      result.dataset.kind = "bad";
      showToast(`${channel} 通道测试失败：${error.message || error}`, "bad");
    }
  }

  function bindNavigation() {
    const links = [...document.querySelectorAll(".nav-link")];
    const sections = links.map((link) => $(link.getAttribute("href").slice(1))).filter(Boolean);
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;
      links.forEach((link) => link.classList.toggle("active", link.getAttribute("href") === `#${visible.target.id}`));
    }, { rootMargin: "-15% 0px -70% 0px", threshold: [0, .2, .5] });
    sections.forEach((section) => observer.observe(section));
  }

  async function init() {
    if (!bridge || typeof bridge.ready !== "function") {
      setSaveState("桥接 SDK 未加载", "bad");
      showToast("页面桥接 SDK 未加载，请从 AstrBot 插件页面重新打开。", "bad");
      return;
    }
    try {
      await bridge.ready();
    } catch (error) {
      showToast(`页面桥接初始化失败：${error.message || error}`, "bad");
      return;
    }
    $("refresh-btn").addEventListener("click", loadDashboard);
    $("task-refresh-btn").addEventListener("click", loadDashboard);
    $("save-btn").addEventListener("click", saveConfig);
    document.querySelectorAll(".test-btn").forEach((button) => {
      button.addEventListener("click", () => testConnection(button.dataset.channel));
    });
    bindNavigation();
    await loadDashboard();
  }

  init();
})();
