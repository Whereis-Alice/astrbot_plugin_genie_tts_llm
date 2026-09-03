/* =====================================================================
   Genie TTS · 语音合成工作台 · 前端逻辑
   全部请求走 window.AstrBotPluginPage 桥（iframe 无 same-origin，不能直接 fetch）
   ===================================================================== */
(function () {
"use strict";

var SDK = window.AstrBotPluginPage;
var D = document;
var SEP = String.fromCharCode(1);

/* ------------------------------------------------------------ DOM 工具 */

function $(id) { return D.getElementById(id); }

function append(node, kids) {
  if (kids === null || kids === undefined || kids === false || kids === true) return node;
  if (Array.isArray(kids)) {
    for (var i = 0; i < kids.length; i++) append(node, kids[i]);
    return node;
  }
  if (typeof kids === "object" && kids.nodeType) { node.appendChild(kids); return node; }
  node.appendChild(D.createTextNode(String(kids)));
  return node;
}

function h(tag, attrs, kids) {
  var n = D.createElement(tag);
  if (attrs) {
    for (var k in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
      var v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") { n.className = v; }
      else if (k === "text") { n.textContent = String(v); }
      else if (k === "value") { n.value = v; }
      else if (k === "checked") { n.checked = !!v; }
      else if (k === "disabled") { n.disabled = !!v; }
      else if (k === "hidden") { n.hidden = !!v; }
      else if (k.slice(0, 2) === "on") { n.addEventListener(k.slice(2), v); }
      else { n.setAttribute(k, v === true ? "" : String(v)); }
    }
  }
  if (kids !== undefined) append(n, kids);
  return n;
}

function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); return node; }

/* 切分区后把滚动位置拉回顶部。
   插件页跑在 iframe 里，topbar / tabbar 都是 sticky，如果不复位，
   从长分区（比如感情库）切到别的分区时视口还停在页尾，看起来像「没切过去」。
   harness 的 window 没有 scrollTo，所以每一步都做 typeof 判断 + try/catch。 */
function scrollTopSafe() {
  try {
    if (typeof window !== "undefined" && typeof window.scrollTo === "function") window.scrollTo(0, 0);
  } catch (e1) {}
  try { if (D.documentElement) D.documentElement.scrollTop = 0; } catch (e2) {}
  try { if (D.body) D.body.scrollTop = 0; } catch (e3) {}
}

/* ------------------------------------------------------------ 格式化 */

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}

function fmtSec(n) {
  n = Number(n) || 0;
  if (n <= 0) return "0.00s";
  if (n < 60) return n.toFixed(2) + "s";
  var m = Math.floor(n / 60);
  return m + "m" + (n - m * 60).toFixed(1) + "s";
}

function fmtMs(n) { return (Number(n) || 0) + "ms"; }

function fmtTime(v) {
  if (!v) return "—";
  var d = new Date(v);
  if (isNaN(d.getTime())) return String(v);
  function p(x) { return (x < 10 ? "0" : "") + x; }
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
         p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function shorten(v, n) {
  v = v === null || v === undefined ? "" : String(v);
  return v.length > n ? v.slice(0, n - 1) + "…" : v;
}

function safeName(v, fallback) {
  v = String(v || "").trim();
  v = v.replace(new RegExp("[^0-9A-Za-z._\u4e00-\u9fff-]+", "g"), "_");
  v = v.replace(new RegExp("^[._]+"), "");
  if (!v) return fallback;
  if (v.toLowerCase().slice(-5) !== ".json") v = v + ".json";
  return v;
}

/* ------------------------------------------------------------ 小部件 */

function kicker(t) { return h("p", { class: "kicker", text: t }); }
function rule() { return h("div", { class: "rule" }, h("i")); }
function chip(text, cls) { return h("span", { class: "chip" + (cls ? " " + cls : ""), text: text }); }
function badge(text, tone) { return h("span", { class: "badge", "data-tone": tone || null, text: text }); }
function note(text, tone) { return h("div", { class: "note", "data-tone": tone || null }, h("span", {}, text)); }
function empty(title, desc) { return h("div", { class: "empty" }, [h("b", { text: title }), desc || ""]); }
function dim(text) { return h("span", { class: "dim", text: text }); }
function mono(text) { return h("span", { class: "mono", text: text }); }

function stat(num, cap, tone) {
  return h("div", { class: "stat", "data-tone": tone || null }, [
    h("span", { class: "stat-num", text: num }),
    h("span", { class: "stat-cap", text: cap })
  ]);
}

function kvRow(label, value, cls) {
  return h("div", { class: "kv-row" }, [
    h("span", { class: "kv-label", text: label }),
    h("span", { class: "kv-value" + (cls ? " " + cls : ""), text: value === null || value === undefined || value === "" ? "—" : String(value) })
  ]);
}

function kv(rows) {
  var box = h("div", { class: "kv" });
  for (var i = 0; i < rows.length; i++) {
    if (!rows[i]) continue;
    box.appendChild(kvRow(rows[i][0], rows[i][1], rows[i][2]));
  }
  return box;
}

function card(opts) {
  var c = h("section", { class: "card" + (opts.sub ? " card-sub" : "") + (opts.class ? " " + opts.class : "") });
  if (opts.kicker || opts.title || opts.desc || opts.tools) {
    var head = h("div", { class: "card-head" });
    var grow = h("div", { class: "grow" });
    if (opts.kicker) grow.appendChild(kicker(opts.kicker));
    if (opts.title) grow.appendChild(h("h2", { class: "card-title", text: opts.title }));
    if (opts.desc) grow.appendChild(h("p", { class: "card-desc", text: opts.desc }));
    head.appendChild(grow);
    if (opts.tools) head.appendChild(h("div", { class: "card-tools" }, opts.tools));
    c.appendChild(head);
    c.appendChild(rule());
  }
  c.appendChild(h("div", { class: "card-body" }, opts.body || []));
  return c;
}

function btn(text, opts) {
  opts = opts || {};
  return h("button", {
    type: "button",
    class: "btn" + (opts.kind ? " btn-" + opts.kind : "") + (opts.sm ? " btn-sm" : "") + (opts.class ? " " + opts.class : ""),
    title: opts.title || null,
    disabled: opts.disabled || false,
    onclick: opts.onclick || null
  }, text);
}

function field(opts) {
  var f = h("div", { class: "field" + (opts.class ? " " + opts.class : "") });
  if (opts.label || opts.key) {
    var lab = h("label", { class: "field-label" });
    if (opts.label) lab.appendChild(h("span", { text: opts.label }));
    if (opts.key) lab.appendChild(h("span", { class: "field-key", text: opts.key }));
    if (opts.tag) lab.appendChild(opts.tag);
    f.appendChild(lab);
  }
  if (opts.desc) f.appendChild(h("p", { class: "field-desc", text: opts.desc }));
  append(f, opts.control);
  if (opts.hint) f.appendChild(h("p", { class: "field-hint", text: opts.hint }));
  return f;
}

function select(options, value, onchange, cls) {
  var s = h("select", { class: "select" + (cls ? " " + cls : ""), onchange: onchange });
  for (var i = 0; i < options.length; i++) {
    var o = options[i];
    var val = typeof o === "object" ? o.value : o;
    var lab = typeof o === "object" ? o.label : o;
    s.appendChild(h("option", { value: val, text: lab, selected: String(val) === String(value) ? true : null }));
  }
  s.value = value === null || value === undefined ? "" : String(value);
  return s;
}

function input(value, onchange, opts) {
  opts = opts || {};
  return h("input", {
    class: "input" + (opts.mono ? " mono" : ""),
    type: opts.type || "text",
    value: value === null || value === undefined ? "" : String(value),
    placeholder: opts.placeholder || null,
    min: opts.min === undefined ? null : opts.min,
    max: opts.max === undefined ? null : opts.max,
    step: opts.step === undefined ? null : opts.step,
    oninput: opts.oninput || null,
    onchange: onchange || null
  });
}

function textarea(value, onchange, opts) {
  opts = opts || {};
  var t = h("textarea", {
    class: "textarea" + (opts.mono ? " mono" : ""),
    rows: opts.rows || null,
    placeholder: opts.placeholder || null,
    oninput: opts.oninput || null,
    onchange: onchange || null
  });
  t.value = value === null || value === undefined ? "" : String(value);
  return t;
}

function switchBox(checked, text, onchange) {
  var inp = h("input", { type: "checkbox", checked: !!checked, onchange: onchange });
  return h("label", { class: "switch" }, [inp, h("span", { class: "switch-track" }), h("span", { class: "switch-text", text: text })]);
}

function segment(options, value, onpick) {
  var box = h("div", { class: "seg" });
  options.forEach(function (o) {
    var val = typeof o === "object" ? o.value : o;
    var lab = typeof o === "object" ? o.label : o;
    box.appendChild(h("button", {
      type: "button",
      "aria-pressed": String(val) === String(value) ? "true" : "false",
      text: lab,
      onclick: function () { onpick(val); }
    }));
  });
  return box;
}

function table(head, cols) {
  var wrap = h("div", { class: "table-wrap" });
  var t = h("table", { class: "tbl" });
  if (cols) {
    var cg = h("colgroup");
    cols.forEach(function (c) { cg.appendChild(h("col", { class: c || null })); });
    t.appendChild(cg);
  }
  var tr = h("tr");
  head.forEach(function (x) { tr.appendChild(h("th", { text: typeof x === "object" ? x.label : x, class: typeof x === "object" ? x.class : null })); });
  t.appendChild(h("thead", {}, tr));
  var tb = h("tbody");
  t.appendChild(tb);
  wrap.appendChild(t);
  wrap.body = tb;
  return wrap;
}

/* ------------------------------------------------------------ toast / modal */

function toast(msg, tone, ms) {
  var host = $("toasts");
  if (!host) return;
  var box = h("div", { class: "toast", "data-tone": tone || "info" }, [
    h("i", { class: "toast-bar" }),
    h("span", { class: "grow", text: String(msg) })
  ]);
  var x = h("button", { type: "button", class: "toast-x", "aria-label": "关闭", text: "×" });
  box.appendChild(x);
  function kill() {
    if (box.dataset.out === "true") return;
    box.dataset.out = "true";
    setTimeout(function () { if (box.parentNode) box.parentNode.removeChild(box); }, 220);
  }
  x.addEventListener("click", kill);
  host.appendChild(box);
  while (host.children.length > 5) host.removeChild(host.firstChild);
  setTimeout(kill, ms || (tone === "danger" ? 8000 : 4200));
}

var modalState = { resolve: null, onOk: null };

function closeModal(result) {
  var m = $("modal");
  if (!m || m.hidden) return;
  m.hidden = true;
  var r = modalState.resolve;
  modalState.resolve = null;
  modalState.onOk = null;
  if (r) r(result);
}

function openModal(opts) {
  var m = $("modal");
  $("modal-kicker").textContent = opts.kicker || "CONFIRM";
  $("modal-title").textContent = opts.title || "";
  clear($("modal-body"));
  append($("modal-body"), opts.body === undefined ? "" : opts.body);
  var ok = $("modal-ok");
  var cancel = $("modal-cancel");
  ok.textContent = opts.okText || "确定";
  ok.className = "btn " + (opts.danger ? "btn-danger" : "btn-primary");
  cancel.textContent = opts.cancelText || "取消";
  cancel.hidden = !!opts.hideCancel;
  m.hidden = false;
  setTimeout(function () { try { ok.focus(); } catch (e) {} }, 30);
  return new Promise(function (resolve) { modalState.resolve = resolve; });
}

function confirmModal(title, body, opts) {
  opts = opts || {};
  return openModal({
    kicker: opts.kicker || "CONFIRM",
    title: title,
    body: body,
    okText: opts.okText || "确定",
    danger: opts.danger
  });
}

function copyText(text, label) {
  var ta = h("textarea", { "aria-hidden": "true" });
  ta.value = String(text === null || text === undefined ? "" : text);
  ta.style.position = "fixed";
  ta.style.top = "-1000px";
  ta.style.opacity = "0";
  D.body.appendChild(ta);
  ta.focus();
  ta.select();
  var ok = false;
  try { ok = D.execCommand("copy"); } catch (e) { ok = false; }
  D.body.removeChild(ta);
  toast(ok ? (label || "已复制到剪贴板") : "复制失败，请手动选中文本", ok ? "ok" : "warn");
  return ok;
}

/* ------------------------------------------------------------ API */

function apiGet(ep, params) { return SDK.apiGet(ep, params || {}); }
function apiPost(ep, body) { return SDK.apiPost(ep, body || {}); }

function fail(e, prefix) {
  var msg = e && e.message ? e.message : String(e || "未知错误");
  toast((prefix ? prefix + "：" : "") + msg, "danger");
  return null;
}

/* ------------------------------------------------------------ 状态 */

var TABS = [
  { id: "studio",   label: "工作台",  ico: "◈" },
  { id: "emotions", label: "感情库",  ico: "❀" },
  { id: "packs",    label: "感情包",  ico: "❐" },
  { id: "config",   label: "配置",    ico: "⚙" },
  { id: "servers",  label: "服务器",  ico: "☁" },
  { id: "sessions", label: "会话",    ico: "◉" },
  { id: "commands", label: "指令表",  ico: "⌘" },
  { id: "about",    label: "关于",    ico: "ⓘ" }
];

var state = {
  tab: "studio",
  prefs: { theme: "moonlit", density: "comfortable", tab: "studio", themes: [], densities: ["comfortable", "compact"] },
  overview: null,
  emotions: null,
  packs: null,
  servers: null,
  config: null,
  sessions: null,
  commands: null,
  loaded: {},
  busy: {},
  studio: {
    character: "", emotion: "", language: "",
    text: "", preview: null, result: null, history: [],
    previewing: false, synthing: false, refPath: "", refText: "", freeRef: false
  },
  emo: { q: "", ch: "", picked: {}, editing: null, form: null },
  pack: { importText: "", mode: "merge", report: null, note: "", filename: "", dry: true, fileName: "" },
  cfg: { dirty: {}, needsReload: false, saving: false }
};

function pickedKeys() {
  var out = [];
  for (var k in state.emo.picked) {
    if (state.emo.picked[k]) {
      var parts = k.split(SEP);
      out.push({ character: parts[0], emotion: parts[1] });
    }
  }
  return out;
}

/* ------------------------------------------------------------ 主题 / 密度 */

function themeList() {
  var t = (state.prefs.themes && state.prefs.themes.length) ? state.prefs.themes
        : (state.overview && state.overview.themes) ? state.overview.themes : [];
  return t.length ? t : [{ id: "moonlit", name: "月夜", hint: "", dark: true }];
}

function applyTheme(id, persist) {
  var list = themeList();
  var hit = null;
  for (var i = 0; i < list.length; i++) if (list[i].id === id) hit = list[i];
  if (!hit) hit = list[0];
  state.prefs.theme = hit.id;
  D.documentElement.setAttribute("data-theme", hit.id);
  var sel = $("theme-select");
  if (sel && sel.value !== hit.id) sel.value = hit.id;
  renderStatus();
  if (state.tab === "about") renderAbout();
  if (persist) savePrefs();
}

function applyDensity(d, persist) {
  state.prefs.density = d === "compact" ? "compact" : "comfortable";
  D.documentElement.setAttribute("data-density", state.prefs.density);
  var lab = $("density-label");
  if (lab) lab.textContent = state.prefs.density === "compact" ? "紧凑" : "宽松";
  renderStatus();
  if (persist) savePrefs();
}

var prefTimer = null;
function savePrefs() {
  if (prefTimer) clearTimeout(prefTimer);
  prefTimer = setTimeout(function () {
    prefTimer = null;
    apiPost("prefs/save", { theme: state.prefs.theme, density: state.prefs.density, tab: state.tab })
      .catch(function () { /* 偏好保存失败不打扰用户 */ });
  }, 420);
}

/* ------------------------------------------------------------ tab 栏 */

function tabBadge(id) {
  var o = state.overview;
  if (!o) return null;
  if (id === "emotions") {
    var w = Number(o.counts && o.counts.warnings) || 0;
    return { text: String((o.counts && o.counts.emotions) || 0), tone: w > 0 ? "warn" : null };
  }
  if (id === "packs") return { text: String(o.packs || 0), tone: null };
  if (id === "servers") return { text: String(o.servers || 0), tone: null };
  if (id === "commands") return { text: String((o.counts && o.counts.commands) || 0), tone: null };
  if (id === "sessions") {
    var s = o.session || {};
    var n = (s.active_sessions ? s.active_sessions.length : 0) + (s.w_active_sessions ? s.w_active_sessions.length : 0);
    return n ? { text: String(n), tone: null } : null;
  }
  return null;
}

function renderTabbar() {
  var bar = clear($("tabbar"));
  TABS.forEach(function (t) {
    var kids = [h("span", { class: "tab-ico", "aria-hidden": "true", text: t.ico }), h("span", { text: t.label })];
    var b = tabBadge(t.id);
    if (b) kids.push(h("span", { class: "tab-badge", "data-tone": b.tone, text: b.text }));
    bar.appendChild(h("button", {
      type: "button", class: "tab", id: "tab-" + t.id, role: "tab",
      "aria-selected": state.tab === t.id ? "true" : "false",
      "aria-controls": "view-" + t.id,
      onclick: function () { go(t.id); }
    }, kids));
  });
}

/* ------------------------------------------------------------ 状态条 */

function sep() { return h("span", { class: "sep", text: "·" }); }

function renderStatus() {
  var o = state.overview;
  var left = clear($("status-left"));
  var right = clear($("status-right"));
  if (!o) { left.appendChild(h("span", { text: "正在连接 AstrBot…" })); return; }
  var c = o.counts || {};
  var s = o.stats || {};
  append(left, [
    "角色 ", h("b", { text: String(c.characters || 0) }), sep(),
    "感情 ", h("b", { text: String(c.emotions || 0) }), sep(),
    "请求 ", h("b", { text: String(s.requests || 0) }), sep(),
    "成功 ", h("b", { text: String(s.succeeded || 0) }), sep(),
    "失败 ", h("b", { text: String(s.failed || 0) }), sep(),
    "队列 ", h("b", { text: String(s.queue_size || 0) })
  ]);
  var themeName = state.prefs.theme;
  themeList().forEach(function (t) { if (t.id === state.prefs.theme) themeName = t.name; });
  append(right, [
    h("span", { text: themeName }), sep(),
    h("span", { text: state.prefs.density === "compact" ? "紧凑" : "宽松" }), sep(),
    "服务器 ", h("b", { text: String(o.servers || 0) }), sep(),
    h("span", { text: (o.plugin && o.plugin.name) || "astrbot" }), sep(),
    h("b", { text: "v" + ((o.plugin && o.plugin.version) || "?") })
  ]);
}

/* ------------------------------------------------------------ 导航 / 加载 */

var VIEWS = {
  studio: renderStudio, emotions: renderEmotions, packs: renderPacks, config: renderConfig,
  servers: renderServers, sessions: renderSessions, commands: renderCommands, about: renderAbout
};

var LOADERS = {
  emotions: function () { return apiGet("emotions").then(function (d) { state.emotions = d; }); },
  packs: function () { return apiGet("packs").then(function (d) { state.packs = d; }); },
  config: function () { return apiGet("config").then(function (d) { state.config = d; state.cfg.dirty = {}; }); },
  servers: function () { return apiGet("servers").then(function (d) { state.servers = d; }); },
  sessions: function () { return apiGet("sessions").then(function (d) { state.sessions = d; }); },
  commands: function () { return apiGet("commands").then(function (d) { state.commands = d; }); }
};

function viewNode(id) { return $("view-" + id); }

function busyView(id, msg) {
  var v = clear(viewNode(id));
  v.appendChild(h("div", { class: "card" }, h("div", { class: "row-tight" }, [h("span", { class: "spinner" }), dim(msg || "正在加载…")])));
}

function go(tab) {
  if (!VIEWS[tab]) tab = "studio";
  var changed = state.tab !== tab;
  state.tab = tab;
  TABS.forEach(function (t) {
    var v = viewNode(t.id);
    if (v) v.hidden = t.id !== tab;
    var b = $("tab-" + t.id);
    if (b) b.setAttribute("aria-selected", t.id === tab ? "true" : "false");
  });
  if (changed) scrollTopSafe();
  savePrefs();
  if (LOADERS[tab] && !state.loaded[tab]) {
    busyView(tab, "正在读取数据…");
    state.loaded[tab] = true;
    LOADERS[tab]()
      .then(function () { VIEWS[tab](); })
      .catch(function (e) {
        state.loaded[tab] = false;
        var v = clear(viewNode(tab));
        v.appendChild(card({
          kicker: "ERROR", title: "读取失败",
          body: [note(e && e.message ? e.message : String(e), "danger"),
                 h("div", { class: "btnrow" }, btn("重试", { kind: "soft", onclick: function () { go(tab); } }))]
        }));
      });
    return;
  }
  VIEWS[tab]();
}

function reloadTab(tab, silent) {
  tab = tab || state.tab;
  var jobs = [apiGet("overview").then(function (d) { state.overview = d; })];
  if (LOADERS[tab]) jobs.push(LOADERS[tab]());
  return Promise.all(jobs).then(function () {
    renderTabbar();
    renderStatus();
    VIEWS[tab]();
    if (!silent) toast("已刷新", "ok", 1800);
  }).catch(function (e) { fail(e, "刷新失败"); });
}

function refreshOverview() {
  return apiGet("overview").then(function (d) {
    state.overview = d;
    renderTabbar();
    renderStatus();
  }).catch(function () {});
}

/* ------------------------------------------------------------ 启动 */

function bindChrome() {
  var sel = clear($("theme-select"));
  themeList().forEach(function (t) {
    sel.appendChild(h("option", { value: t.id, text: t.name + (t.dark ? " · 暗" : " · 亮") }));
  });
  sel.value = state.prefs.theme;
  sel.addEventListener("change", function () { applyTheme(sel.value, true); });

  $("density-toggle").addEventListener("click", function () {
    applyDensity(state.prefs.density === "compact" ? "comfortable" : "compact", true);
  });

  $("refresh-btn").addEventListener("click", function () {
    var b = $("refresh-btn");
    b.disabled = true;
    reloadTab(state.tab).then(function () { b.disabled = false; });
  });

  $("reset-btn").addEventListener("click", function () {
    confirmModal("清空本页草稿？", "会清掉工作台的文本草稿、试听历史和预览结果。感情库、配置和服务器上的数据都不会动。", { danger: true, okText: "清空" })
      .then(function (ok) {
        if (!ok) return;
        state.studio.text = "";
        state.studio.history = [];
        state.studio.preview = null;
        state.studio.result = null;
        state.pack.importText = "";
        state.pack.report = null;
        toast("本页草稿已清空", "ok");
        go(state.tab);
      });
  });

  $("modal-cancel").addEventListener("click", function () { closeModal(false); });
  $("modal-mask").addEventListener("click", function () { closeModal(false); });
  $("modal-ok").addEventListener("click", function () {
    if (modalState.onOk) {
      var r = modalState.onOk();
      if (r === false) return;
      closeModal(r === undefined ? true : r);
      return;
    }
    closeModal(true);
  });
  D.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !$("modal").hidden) closeModal(false);
  });
}

function bootFail(msg) {
  var boot = $("boot");
  if (!boot) return;
  clear(boot);
  boot.appendChild(h("div", { class: "card", style: "max-width:520px" }, [
    kicker("BRIDGE ERROR"),
    h("h2", { class: "card-title", text: "无法连接插件页桥" }),
    rule(),
    note(msg, "danger"),
    h("p", { class: "card-desc", text: "请确认这是从 AstrBot WebUI 的「插件 → 管理页面」打开的，而不是直接访问 HTML 文件。" })
  ]));
}

async function init() {
  if (!SDK || typeof SDK.ready !== "function") {
    bootFail("window.AstrBotPluginPage 不存在。bridge-sdk.js 没有加载成功。");
    return;
  }
  try { await SDK.ready(); } catch (e) {
    bootFail("SDK.ready() 失败：" + (e && e.message ? e.message : String(e)));
    return;
  }
  var prefs = null;
  var overview = null;
  try {
    var got = await Promise.all([
      apiGet("prefs").catch(function () { return null; }),
      apiGet("overview")
    ]);
    prefs = got[0];
    overview = got[1];
  } catch (e) {
    bootFail("读取插件数据失败：" + (e && e.message ? e.message : String(e)));
    return;
  }
  if (prefs) state.prefs = Object.assign(state.prefs, prefs);
  state.overview = overview;
  if (overview && overview.defaults) {
    state.studio.character = overview.defaults.character || "";
    state.studio.emotion = overview.defaults.emotion || "";
    state.studio.language = overview.defaults.language || "";
  }
  bindChrome();
  applyTheme(state.prefs.theme, false);
  applyDensity(state.prefs.density, false);
  renderTabbar();
  renderStatus();
  var startTab = VIEWS[state.prefs.tab] ? state.prefs.tab : "studio";
  $("boot").hidden = true;
  go(startTab);
}

/* =====================================================================
   1) 工作台
   ===================================================================== */

LOADERS.studio = function () { return apiGet("emotions").then(function (d) { state.emotions = d; }); };

function emoRows() { return (state.emotions && state.emotions.rows) ? state.emotions.rows : []; }
function emoChars() { return (state.emotions && state.emotions.characters) ? state.emotions.characters : []; }

function emotionsOf(character) {
  return emoRows().filter(function (r) { return r.character === character; });
}

function rowFor(character, emotion) {
  var rows = emoRows();
  for (var i = 0; i < rows.length; i++) if (rows[i].character === character && rows[i].emotion === emotion) return rows[i];
  return null;
}

function normalizeStudioPick() {
  var s = state.studio;
  var chars = emoChars();
  if (!chars.length) { s.character = ""; s.emotion = ""; return; }
  var names = chars.map(function (c) { return c.name; });
  if (names.indexOf(s.character) < 0) s.character = names[0];
  var list = emotionsOf(s.character).map(function (r) { return r.emotion; });
  if (list.indexOf(s.emotion) < 0) s.emotion = list.length ? list[0] : "";
}

function insertAtCursor(ta, text) {
  var start = ta.selectionStart === undefined ? ta.value.length : ta.selectionStart;
  var end = ta.selectionEnd === undefined ? ta.value.length : ta.selectionEnd;
  ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
  var pos = start + text.length;
  try { ta.setSelectionRange(pos, pos); } catch (e) {}
  ta.focus();
  var ev = D.createEvent ? D.createEvent("Event") : null;
  if (ev) { ev.initEvent("input", true, true); ta.dispatchEvent(ev); }
  else { ta.dispatchEvent(new Event("input")); }
}

function langOptions() {
  var langs = (state.emotions && state.emotions.languages) ? state.emotions.languages.slice() : ["jp", "zh", "en"];
  var out = [{ value: "", label: "跟随音色 / 默认" }];
  var names = { jp: "日语 jp", zh: "中文 zh", en: "英语 en" };
  langs.forEach(function (l) { out.push({ value: l, label: names[l] || l }); });
  return out;
}

function renderStudio() {
  var v = clear(viewNode("studio"));
  var s = state.studio;
  var o = state.overview || {};
  var lim = o.limits || {};
  var tog = o.toggles || {};
  normalizeStudioPick();
  if (!s.adv) {
    s.adv = {
      enable_sentence_splitting: tog.sentence_splitting !== false,
      sentences_per_chunk: Number(lim.sentences_per_chunk) || 2,
      chunk_gap_ms: Number(lim.chunk_gap_ms) || 260,
      enable_custom_pause_marker: tog.custom_pause_marker !== false
    };
  }

  var ui = {};
  var synthLimit = Number(lim.synth_text_limit) || 1500;

  /* ---------- 音色选择 ---------- */
  var chars = emoChars();
  var charSel = select(chars.map(function (c) { return { value: c.name, label: c.name + "（" + c.count + "）" }; }), s.character, function () {
    s.character = charSel.value;
    var list = emotionsOf(s.character).map(function (r) { return r.emotion; });
    s.emotion = list.length ? list[0] : "";
    refillEmotion();
    syncVoice();
  });
  var emoSel = select([], s.emotion, function () { s.emotion = emoSel.value; syncVoice(); });
  function refillEmotion() {
    clear(emoSel);
    var list = emotionsOf(s.character);
    if (!list.length) emoSel.appendChild(h("option", { value: "", text: "（该角色没有感情）" }));
    list.forEach(function (r) {
      emoSel.appendChild(h("option", { value: r.emotion, text: r.emotion + (r.warning ? "  ⚠" : "") }));
    });
    emoSel.value = s.emotion || "";
  }
  refillEmotion();

  var langSel = select(langOptions(), s.language, function () { s.language = langSel.value; });

  var freeSwitch = switchBox(s.freeRef, "手动指定参考音频（试听未登记的候选）", function (e) {
    s.freeRef = e.target.checked;
    renderStudio();
  });

  ui.ref = h("div", { class: "ref-card" });
  ui.name = h("span", { class: "vn-name" });

  function syncVoice() {
    var row = rowFor(s.character, s.emotion);
    clear(ui.name);
    append(ui.name, [
      h("span", { text: s.character || "未选择角色" }),
      h("span", { class: "vn-name-sep", text: "·" }),
      h("span", { class: "vn-name-emo", text: s.emotion || "无感情" })
    ]);
    clear(ui.ref);
    if (s.freeRef) {
      var p = input(s.refPath, null, { mono: true, placeholder: "相对路径，例如 kisaki/happy.wav", oninput: function (e) { s.refPath = e.target.value; } });
      var t = input(s.refText, null, { placeholder: "参考音频里念的那句原文", oninput: function (e) { s.refText = e.target.value; } });
      append(ui.ref, [
        field({ label: "参考音频路径", control: p, desc: "相对 Space 工作目录，不能以 / 开头也不能含 .." }),
        h("div", { style: "height:8px" }),
        field({ label: "参考文本", control: t, desc: "留空则由服务端自行判断，识别质量会下降" })
      ]);
      return;
    }
    if (!row) {
      ui.ref.appendChild(note("当前角色下没有可用的感情，先去「感情库」登记一条。", "warn"));
      return;
    }
    ui.ref.appendChild(kv([
      ["参考音频", row.ref_audio_path],
      ["参考文本", shorten(row.ref_audio_text, 120)],
      ["语言", row.language || "（默认）"]
    ]));
    if (row.warning) ui.ref.appendChild(note(row.warning, "warn"));
  }

  /* ---------- VN 对话框 ---------- */
  var ta = h("textarea", { class: "vn-text", placeholder: "在这里写要合成的台词……" + nl() + "支持 [pause=600] 手动停顿，句末标点和「……」会自动补停顿。", spellcheck: "false" });
  ta.value = s.text || "";
  ui.count = h("span", { class: "vn-count" });
  function syncCount() {
    var n = ta.value.length;
    ui.count.textContent = n + " / " + synthLimit + " 字";
    ui.count.setAttribute("data-tone", n > synthLimit ? "danger" : (n > synthLimit * 0.8 ? "warn" : ""));
  }
  ta.addEventListener("input", function () { s.text = ta.value; syncCount(); });
  syncCount();

  var tools = h("div", { class: "vn-tools" }, [
    btn("短停 300", { sm: true, title: "插入 [pause=300]", onclick: function () { insertAtCursor(ta, "[pause=300]"); } }),
    btn("中停 600", { sm: true, title: "插入 [pause=600]", onclick: function () { insertAtCursor(ta, "[pause=600]"); } }),
    btn("长停 1200", { sm: true, title: "插入 [pause=1200]", onclick: function () { insertAtCursor(ta, "[pause=1200]"); } }),
    btn("……", { sm: true, title: "插入省略号，会自动补一段较长的停顿", onclick: function () { insertAtCursor(ta, "……"); } }),
    btn("。", { sm: true, title: "补句末标点，句末才会补满段间停顿", onclick: function () { insertAtCursor(ta, "。"); } }),
    btn("复制", { sm: true, kind: "ghost", onclick: function () { copyText(ta.value, "台词已复制"); } }),
    btn("清空", { sm: true, kind: "ghost", onclick: function () { ta.value = ""; s.text = ""; syncCount(); ta.focus(); } }),
    ui.count
  ]);

  var vn = h("div", { class: "vn-box" }, [ui.name, ta, h("span", { class: "vn-cursor", "aria-hidden": "true", text: "▼" }), tools]);

  /* ---------- 动作按钮 ---------- */
  ui.previewBtn = btn("分段预览", { kind: "soft", onclick: doPreview });
  ui.synthBtn = btn("合成并试听", { kind: "primary", onclick: doSynth });
  var actions = h("div", { class: "btnrow" }, [ui.synthBtn, ui.previewBtn, h("span", { class: "spacer" }), ui.status = h("span", { class: "dim tiny" })]);

  /* ---------- 预览试算参数 ---------- */
  var adv = h("div", { class: "grid-4" }, [
    field({ label: "分句", control: switchBox(s.adv.enable_sentence_splitting, "启用", function (e) { s.adv.enable_sentence_splitting = e.target.checked; }) }),
    field({ label: "每段句数", control: input(s.adv.sentences_per_chunk, null, { type: "number", min: 1, max: 20, mono: true, oninput: function (e) { s.adv.sentences_per_chunk = Number(e.target.value) || 1; } }) }),
    field({ label: "段间停顿 ms", control: input(s.adv.chunk_gap_ms, null, { type: "number", min: 0, max: Number(lim.max_chunk_gap_ms) || 2000, step: 20, mono: true, oninput: function (e) { s.adv.chunk_gap_ms = Number(e.target.value) || 0; } }) }),
    field({ label: "[pause] 标记", control: switchBox(s.adv.enable_custom_pause_marker, "启用", function (e) { s.adv.enable_custom_pause_marker = e.target.checked; }) })
  ]);

  var mainCard = card({
    kicker: "SYNTH DESK",
    title: "语音合成工作台",
    desc: "选音色 → 写台词 → 分段预览 → 直接在浏览器里试听。合成走的是和聊天里完全一样的那条链路。",
    tools: [badge("队列 " + ((o.stats && o.stats.queue_size) || 0), (o.stats && o.stats.queue_size) ? "warn" : null),
            badge("上限 " + synthLimit + " 字")],
    body: [
      h("div", { class: "voice-head" }, [
        field({ label: "角色", control: charSel }),
        field({ label: "感情", control: emoSel }),
        field({ label: "语言", control: langSel })
      ]),
      h("div", { class: "row-tight" }, freeSwitch),
      ui.ref,
      vn,
      actions,
      h("details", {}, [
        h("summary", { class: "field-label", style: "cursor:pointer", text: "预览试算参数（只影响下面的分段预览，不写入配置）" }),
        h("div", { style: "margin-top:10px" }, adv)
      ])
    ]
  });

  ui.previewCard = h("div");
  ui.resultCard = h("div");

  /* ---------- 侧栏 ---------- */
  /* RUNTIME 卡只重建这一格，不整体重渲分区：
     整体重渲会把试听区的 <audio> 换成新节点，正在播放的语音会被打断。 */
  ui.statGrid = h("div", { class: "stat-grid" });
  function syncStats() {
    var st = (state.overview || {}).stats || {};
    clear(ui.statGrid);
    append(ui.statGrid, [
      stat(st.requests || 0, "总请求", "accent"),
      stat(st.succeeded || 0, "成功", "ok"),
      stat(st.failed || 0, "失败", (st.failed ? "danger" : null)),
      stat(st.skipped_no_speech || 0, "无可读内容"),
      stat(st.leak_guard_hits || 0, "泄漏拦截", (st.leak_guard_hits ? "warn" : null)),
      stat(st.queue_size || 0, "排队中")
    ]);
  }
  var side = h("aside", { class: "side" }, [
    card({
      kicker: "RUNTIME", title: "运行统计", sub: false,
      tools: [btn("⟳", { sm: true, kind: "ghost", title: "只刷新统计", onclick: function () { refreshOverview().then(syncStats); } })],
      body: [ui.statGrid]
    }),
    card({
      kicker: "LIMITS", title: "限额与开关",
      body: [
        kv([
          ["文本上限", (lim.max_text_length || "—") + " 字"],
          ["试听上限", synthLimit + " 字"],
          ["超时", (lim.timeout_seconds || "—") + "s"],
          ["重试", lim.max_retries],
          ["段间停顿", fmtMs(lim.chunk_gap_ms)],
          ["每段句数", lim.sentences_per_chunk],
          ["采样率", (lim.sample_rate || 32000) + " Hz"]
        ]),
        h("div", { class: "chips" }, [
          chip((tog.sentence_splitting ? "分句 开" : "分句 关"), tog.sentence_splitting ? "chip-ok" : "chip-warn"),
          chip((tog.custom_pause_marker ? "[pause] 开" : "[pause] 关"), tog.custom_pause_marker ? "chip-ok" : "chip-warn"),
          chip((tog.text_cleaning ? "清洗 开" : "清洗 关"), tog.text_cleaning ? "chip-ok" : ""),
          chip((tog.translation ? "翻译 开" : "翻译 关"), tog.translation ? "chip-ok" : ""),
          chip((tog.leak_guard ? "泄漏防护 开" : "泄漏防护 关"), tog.leak_guard ? "chip-ok" : "chip-warn")
        ])
      ]
    }),
    ui.histCard = h("div")
  ]);

  v.appendChild(h("div", { class: "split" }, [
    h("div", { class: "grow", style: "display:flex;flex-direction:column;gap:var(--gap)" }, [mainCard, ui.previewCard, ui.resultCard]),
    side
  ]));

  syncStats();
  syncVoice();
  renderPreviewCard();
  renderResultCard();
  renderHistory();

  /* ---------- 预览 ---------- */
  function renderPreviewCard() {
    clear(ui.previewCard);
    var p = s.preview;
    if (!p) return;
    var body = [];
    if (p.blocked) body.push(note(p.blocked, "warn"));
    if (p.steps && p.steps.length) {
      var steps = h("ol", { class: "steps" });
      p.steps.forEach(function (x) {
        steps.appendChild(h("li", { class: "step" }, [
          h("div", { class: "step-name", text: x.name }),
          h("div", { class: "step-detail", text: x.detail })
        ]));
      });
      body.push(h("div", {}, [h("p", { class: "kicker", text: "PIPELINE" }), steps]));
    }
    var chunks = p.chunks || [];
    if (chunks.length) {
      var list = h("div", { class: "chunk-list" });
      chunks.forEach(function (ck, i) {
        if (i > 0) {
          var g = ck.gap_before_ms || 0;
          list.appendChild(h("div", { class: "gap-link" }, h("span", {
            class: "gap-tag", "data-tone": g > 0 ? null : "mute",
            text: g > 0 ? "+" + g + "ms 停顿" : "无停顿（切在句中）"
          })));
        }
        var metas = [
          badge(ck.chars + " 字"),
          badge("可读 " + ck.pronounceable, ck.pronounceable ? null : "warn")
        ];
        if (ck.custom_pause_ms) metas.push(badge("[pause] " + ck.custom_pause_ms + "ms", "accent"));
        if (ck.auto_pause_ms) metas.push(badge("自动停顿 " + ck.auto_pause_ms + "ms"));
        if (ck.tail) metas.push(badge("句末 " + ck.tail));
        if (!ck.voiceable) metas.push(badge("不会发声", "danger"));
        list.appendChild(h("div", { class: "chunk", "data-mute": ck.voiceable ? null : "true" }, [
          h("span", { class: "chunk-no", text: String(ck.index === undefined ? i + 1 : ck.index) }),
          h("div", { class: "chunk-main" }, [
            h("div", { class: "chunk-text", text: ck.text }),
            h("div", { class: "chunk-meta" }, metas)
          ])
        ]));
      });
      body.push(h("div", {}, [h("p", { class: "kicker", text: "CHUNKS" }), list]));
    } else if (!p.blocked) {
      body.push(empty("没有可发声的分段", "文本清洗后没剩下能读的内容。"));
    }
    var tt = p.totals;
    if (tt) {
      body.push(h("div", { class: "totals" }, [
        badge("分段 " + tt.chunks, "accent"),
        badge("总字数 " + tt.chars),
        badge("可读 " + tt.pronounceable),
        badge("段间停顿 " + fmtMs(tt.chunk_gap_ms)),
        badge("[pause] " + fmtMs(tt.custom_pause_ms)),
        badge("自动停顿 " + fmtMs(tt.auto_pause_ms)),
        badge("停顿合计 " + fmtMs(tt.pause_total_ms), tt.pause_total_ms ? "accent" : null),
        badge("预计时长 " + fmtSec(tt.expected_seconds))
      ]));
    }
    if (p.truncated) body.push(note("文本超过上限已被截断，实际合成也会被截断。", "warn"));
    ui.previewCard.appendChild(card({
      kicker: "PREVIEW", title: "分段与停顿预览",
      desc: "这是合成前的完整预处理链：剥标记 → 清洗 → 截断 → 补句末标点 → 可读性检查 → 分段。",
      tools: [btn("复制分段文本", { sm: true, kind: "ghost", onclick: function () {
        copyText(chunks.map(function (ck, i) { return "#" + (i + 1) + "  " + ck.text; }).join(nl()), "分段文本已复制");
      } })],
      body: body
    }));
  }

  function doPreview() {
    if (s.previewing) return;
    var text = ta.value.trim();
    if (!text) { toast("先写点台词再预览", "warn"); return; }
    s.previewing = true;
    ui.previewBtn.disabled = true;
    ui.status.textContent = "正在试算分段…";
    apiPost("preview", {
      text: text,
      language: s.language || "",
      enable_sentence_splitting: !!s.adv.enable_sentence_splitting,
      sentences_per_chunk: s.adv.sentences_per_chunk,
      chunk_gap_ms: s.adv.chunk_gap_ms,
      enable_custom_pause_marker: !!s.adv.enable_custom_pause_marker
    }).then(function (d) {
      s.preview = d || {};
      renderPreviewCard();
      ui.status.textContent = d && d.totals ? ("已试算：" + d.totals.chunks + " 段 / 预计 " + fmtSec(d.totals.expected_seconds)) : "已试算";
    }).catch(function (e) { fail(e, "预览失败"); ui.status.textContent = ""; })
      .then(function () { s.previewing = false; ui.previewBtn.disabled = false; });
  }

  /* ---------- 合成 ---------- */
  function renderResultCard() {
    clear(ui.resultCard);
    var r = s.result;
    if (!r) return;
    var kids = [];
    if (r.audio_base64) {
      var au = h("audio", { controls: true, preload: "metadata" });
      au.src = "data:" + (r.mime || "audio/wav") + ";base64," + r.audio_base64;
      kids.push(au);
    } else {
      kids.push(note("音频体积超过内联上限（12 MB），没法在浏览器里直接播。文件已生成在服务器上：" + (r.path || "?"), "warn"));
    }
    kids.push(h("div", { class: "player-meta" }, [
      badge(r.character + " · " + r.emotion, "accent"),
      badge("时长 " + fmtSec(r.duration_seconds)),
      badge("预计 " + fmtSec(r.expected_seconds)),
      badge("耗时 " + fmtSec(r.elapsed_seconds), r.elapsed_seconds > 20 ? "warn" : "ok"),
      badge(fmtBytes(r.bytes)),
      badge("队列 " + (r.queue_size || 0)),
      badge(r.language || "默认语言")
    ]));
    ui.resultCard.appendChild(card({
      kicker: "AUDITION", title: "试听",
      desc: "文件名 " + (r.filename || "—") + " · 生成于 " + fmtTime(r.created_at),
      tools: [btn("复制服务器路径", { sm: true, kind: "ghost", onclick: function () { copyText(r.path, "路径已复制"); } })],
      body: [h("div", { class: "player" }, [h("div", { class: "player-title" }, [h("span", { text: "🔊" }), h("span", { text: shorten(r.text, 60) })])].concat(kids))]
    }));
  }

  function renderHistory() {
    clear(ui.histCard);
    var list = h("div", { class: "hist" });
    if (!s.history.length) list.appendChild(empty("还没有试听记录", "合成过的音频会留在这里，可以随时重播。"));
    s.history.forEach(function (it, idx) {
      list.appendChild(h("div", { class: "hist-item" }, [
        h("div", { class: "hist-body" }, [
          h("div", { class: "hist-line", text: shorten(it.text, 42) }),
          h("div", { class: "hist-sub", text: it.character + " · " + it.emotion + " · " + fmtSec(it.duration) + " · " + fmtBytes(it.bytes) })
        ]),
        btn("▶", { sm: true, kind: "soft", title: "重新播放", onclick: function () {
          s.result = it.raw;
          renderResultCard();
          toast("已载入到试听区", "ok", 1600);
        } }),
        btn("↺", { sm: true, kind: "ghost", title: "把台词填回工作台", onclick: function () {
          ta.value = it.text; s.text = it.text; syncCount(); ta.focus();
        } })
      ]));
    });
    ui.histCard.appendChild(card({
      kicker: "HISTORY", title: "本页历史",
      desc: "只存在这个页面的内存里，刷新就没了。",
      tools: s.history.length ? [btn("清空", { sm: true, kind: "ghost", onclick: function () { s.history = []; renderHistory(); } })] : null,
      body: [list]
    }));
  }

  function doSynth() {
    if (s.synthing) return;
    var text = ta.value.trim();
    if (!text) { toast("先写点台词", "warn"); return; }
    if (text.length > synthLimit) { toast("试听最多 " + synthLimit + " 字，现在 " + text.length + " 字", "warn"); return; }
    var payload = { character: s.character, emotion: s.emotion, text: text, language: s.language || "" };
    if (s.freeRef) {
      if (!s.refPath) { toast("手动模式下必须填参考音频路径", "warn"); return; }
      payload.ref_audio_path = s.refPath;
      payload.ref_audio_text = s.refText;
      if (!payload.character) payload.character = (emoChars()[0] || {}).name || "";
    } else if (!s.character || !s.emotion) {
      toast("先选一个角色和感情", "warn");
      return;
    }
    s.synthing = true;
    ui.synthBtn.disabled = true;
    clear(ui.synthBtn);
    append(ui.synthBtn, [h("span", { class: "spinner" }), h("span", { text: "合成中…" })]);
    ui.status.textContent = "已排队，服务端正在生成…";
    var t0 = Date.now();
    apiPost("synthesize", payload).then(function (d) {
      s.result = d;
      renderResultCard();
      s.history.unshift({
        text: d.text || text, character: d.character, emotion: d.emotion,
        duration: d.duration_seconds, bytes: d.bytes, raw: d
      });
      while (s.history.length > 12) s.history.pop();
      renderHistory();
      ui.status.textContent = "完成，耗时 " + fmtSec(d.elapsed_seconds || (Date.now() - t0) / 1000);
      toast("合成成功：" + fmtSec(d.duration_seconds) + " / " + fmtBytes(d.bytes), "ok");
    }).catch(function (e) { fail(e, "合成失败"); ui.status.textContent = ""; })
      .then(function () {
        s.synthing = false;
        ui.synthBtn.disabled = false;
        clear(ui.synthBtn);
        append(ui.synthBtn, "合成并试听");
        /* 成功和失败都要刷：失败也会让「失败」计数 +1。
           refreshOverview() 内部已吞掉异常，这里不会再抛。 */
        return refreshOverview().then(syncStats);
      });
  }
}

/* =====================================================================
   2) 感情库
   ===================================================================== */

function formModal(opts) {
  var p = openModal(opts);
  modalState.onOk = opts.onOk || null;
  return p;
}

function applyEmotionRows(d) {
  if (!state.emotions) state.emotions = {};
  state.emotions.rows = d.rows || [];
  state.emotions.warnings = d.warnings || 0;
  var map = {};
  var order = [];
  state.emotions.rows.forEach(function (r) {
    if (map[r.character] === undefined) { map[r.character] = 0; order.push(r.character); }
    map[r.character] += 1;
  });
  order.sort();
  state.emotions.characters = order.map(function (n) { return { name: n, count: map[n] }; });
  state.loaded.packs = false;
  refreshOverview();
}

function charOptions(includeBlank) {
  var out = includeBlank ? [{ value: "", label: "（全部角色）" }] : [];
  emoChars().forEach(function (c) { out.push({ value: c.name, label: c.name + "（" + c.count + "）" }); });
  return out;
}

function emotionFormBody(row, ctl) {
  ctl.character = input(row ? row.character : (state.emo.ch || state.studio.character || ""), null, { placeholder: "角色名，例如 kisaki" });
  ctl.emotion = input(row ? row.emotion : "", null, { placeholder: "感情名，例如 悲伤" });
  ctl.path = input(row ? row.ref_audio_path : "", null, { mono: true, placeholder: "kisaki/sad_01.wav" });
  ctl.text = textarea(row ? row.ref_audio_text : "", null, { rows: 3, placeholder: "参考音频里念的那句原文，越准越像" });
  ctl.lang = select(langOptions(), row ? (row.language || "") : "", null);
  return [
    field({ label: "角色", control: ctl.character, desc: "同名会归到同一个角色下" }),
    field({ label: "感情", control: ctl.emotion }),
    field({ label: "参考音频路径", control: ctl.path, desc: "相对 Space 工作目录，必须是相对路径，不能含 .." }),
    field({ label: "参考文本", control: ctl.text }),
    field({ label: "语言", control: ctl.lang, desc: "留空表示跟随全局默认语言" })
  ];
}

function openEmotionForm(row) {
  var ctl = {};
  formModal({
    kicker: row ? "EDIT" : "NEW",
    title: row ? "编辑感情 · " + row.character + " / " + row.emotion : "登记一条新感情",
    body: emotionFormBody(row, ctl),
    okText: "保存",
    onOk: function () {
      var c = ctl.character.value.trim();
      var e = ctl.emotion.value.trim();
      var p = ctl.path.value.trim();
      if (!c || !e) { toast("角色名和感情名都不能空", "warn"); return false; }
      if (!p) { toast("参考音频路径不能空", "warn"); return false; }
      if (p.charAt(0) === "/" || p.indexOf("..") >= 0 || p.indexOf(":") >= 0) { toast("必须是相对路径且不能含 ..", "warn"); return false; }
      return {
        character: c, emotion: e, ref_audio_path: p,
        ref_audio_text: ctl.text.value.trim(), language: ctl.lang.value,
        original_character: row ? row.character : "", original_emotion: row ? row.emotion : "",
        overwrite: true
      };
    }
  }).then(function (payload) {
    if (!payload) return;
    apiPost("emotions/upsert", payload).then(function (d) {
      applyEmotionRows(d);
      toast((d.renamed ? "已改名并保存：" : "已保存：") + d.saved.character + " / " + d.saved.emotion, "ok");
      renderEmotions();
    }).catch(function (e) { fail(e, "保存失败"); });
  });
}

function deleteEmotions(items, label) {
  confirmModal("删除 " + items.length + " 条感情？", (label || items.map(function (i) { return i.character + " / " + i.emotion; }).join("、")) + nl() + "只会从 emotions.json 里移除登记，参考音频文件本身不会被删。", { danger: true, okText: "删除" })
    .then(function (ok) {
      if (!ok) return;
      apiPost("emotions/delete", { items: items }).then(function (d) {
        applyEmotionRows(d);
        state.emo.picked = {};
        toast("已删除 " + d.removed.length + " 条" + (d.missing && d.missing.length ? "，" + d.missing.length + " 条本来就不存在" : ""), "ok");
        renderEmotions();
      }).catch(function (e) { fail(e, "删除失败"); });
    });
}

function openCopyForm(items) {
  var ctl = {};
  ctl.target = input("", null, { placeholder: "目标角色名（可以是新角色）" });
  ctl.pick = select(charOptions(true), "", function () { if (ctl.pick.value) ctl.target.value = ctl.pick.value; });
  var moveOn = false;
  var owOn = false;
  formModal({
    kicker: "COPY / MOVE",
    title: "把 " + items.length + " 条感情复制或移动到别的角色",
    body: [
      field({ label: "从已有角色里选", control: ctl.pick }),
      field({ label: "目标角色", control: ctl.target, desc: "填一个不存在的名字就会新建这个角色" }),
      h("div", { class: "row-tight", style: "margin-top:12px;gap:16px" }, [
        switchBox(false, "移动（源角色下会删掉）", function (e) { moveOn = e.target.checked; }),
        switchBox(false, "覆盖同名感情", function (e) { owOn = e.target.checked; })
      ])
    ],
    okText: "执行",
    onOk: function () {
      var t = ctl.target.value.trim();
      if (!t) { toast("目标角色不能空", "warn"); return false; }
      return { items: items, target_character: t, move: moveOn, overwrite: owOn };
    }
  }).then(function (payload) {
    if (!payload) return;
    apiPost("emotions/copy", payload).then(function (d) {
      applyEmotionRows(d);
      state.emo.picked = {};
      var msg = (d.moved ? "已移动 " : "已复制 ") + d.copied.length + " 条";
      if (d.skipped && d.skipped.length) msg += "，跳过 " + d.skipped.length + " 条（" + d.skipped[0].reason + "）";
      toast(msg, "ok");
      renderEmotions();
    }).catch(function (e) { fail(e, "操作失败"); });
  });
}

function openRenameChar(name) {
  var ctl = {};
  ctl.name = input(name, null, { placeholder: "新的角色名" });
  var mergeOn = false;
  formModal({
    kicker: "RENAME",
    title: "重命名角色 · " + name,
    body: [
      field({ label: "新角色名", control: ctl.name }),
      h("div", { style: "margin-top:12px" }, switchBox(false, "目标角色已存在时合并进去", function (e) { mergeOn = e.target.checked; })),
      note("改名后，配置里的「默认角色」如果还写着旧名字，需要自己去配置页改一下。", "warn")
    ],
    okText: "重命名",
    onOk: function () {
      var n = ctl.name.value.trim();
      if (!n) { toast("新名字不能空", "warn"); return false; }
      if (n === name) { toast("名字没变", "warn"); return false; }
      return { character: name, new_name: n, merge: mergeOn };
    }
  }).then(function (payload) {
    if (!payload) return;
    apiPost("emotions/rename-character", payload).then(function (d) {
      applyEmotionRows(d);
      if (state.emo.ch === payload.character) state.emo.ch = payload.new_name;
      if (state.studio.character === payload.character) state.studio.character = payload.new_name;
      toast("已重命名：" + d.renamed.from + " → " + d.renamed.to, "ok");
      renderEmotions();
    }).catch(function (e) { fail(e, "重命名失败"); });
  });
}

function deleteCharacter(name, count) {
  confirmModal("删掉整个角色 " + name + "？", "会移除它名下全部 " + count + " 条感情登记。参考音频文件本身不动。", { danger: true, okText: "全部删除" })
    .then(function (ok) {
      if (!ok) return;
      apiPost("emotions/delete", { character: name }).then(function (d) {
        applyEmotionRows(d);
        if (state.emo.ch === name) state.emo.ch = "";
        toast("已删除角色 " + name + "（" + d.removed.length + " 条）", "ok");
        renderEmotions();
      }).catch(function (e) { fail(e, "删除失败"); });
    });
}

function renderEmotions() {
  var v = clear(viewNode("emotions"));
  var e = state.emotions || { rows: [], characters: [] };
  var st = state.emo;

  var q = st.q.trim().toLowerCase();
  var rows = (e.rows || []).filter(function (r) {
    if (st.ch && r.character !== st.ch) return false;
    if (!q) return true;
    return (r.character + " " + r.emotion + " " + r.ref_audio_path + " " + (r.ref_audio_text || "")).toLowerCase().indexOf(q) >= 0;
  });

  var picked = pickedKeys();

  /* ---------- 搜索 / 筛选 ---------- */
  var qInput = input(st.q, null, { placeholder: "搜角色 / 感情 / 路径 / 参考文本…", oninput: function (ev) {
    st.q = ev.target.value;
    renderEmotions();
    var n = $("emo-q");
    if (n) { n.focus(); try { n.setSelectionRange(n.value.length, n.value.length); } catch (x) {} }
  } });
  qInput.id = "emo-q";

  var chipsBox = h("div", { class: "chips" });
  chipsBox.appendChild(h("button", {
    type: "button", class: "chip chip-btn", "aria-pressed": st.ch ? "false" : "true",
    text: "全部 " + (e.rows || []).length, onclick: function () { st.ch = ""; renderEmotions(); }
  }));
  (e.characters || []).forEach(function (c) {
    chipsBox.appendChild(h("button", {
      type: "button", class: "chip chip-btn", "aria-pressed": st.ch === c.name ? "true" : "false",
      text: c.name + " " + c.count, onclick: function () { st.ch = st.ch === c.name ? "" : c.name; renderEmotions(); }
    }));
  });

  /* ---------- 批量条 ---------- */
  var batch = h("div", { class: "row-tight", hidden: picked.length === 0 }, [
    badge("已选 " + picked.length + " 条", "accent"),
    btn("导出所选", { sm: true, kind: "soft", onclick: function () { exportEmotions({ items: picked }); } }),
    btn("复制 / 移动", { sm: true, onclick: function () { openCopyForm(picked); } }),
    btn("存为快照", { sm: true, onclick: function () { savePackFrom({ items: picked }); } }),
    btn("删除所选", { sm: true, kind: "danger", onclick: function () { deleteEmotions(picked); } }),
    btn("取消选择", { sm: true, kind: "ghost", onclick: function () { st.picked = {}; renderEmotions(); } })
  ]);

  /* ---------- 表格 ---------- */
  var allOn = rows.length > 0 && rows.every(function (r) { return st.picked[r.character + SEP + r.emotion]; });
  var headPick = h("input", { type: "checkbox", checked: allOn, "aria-label": "全选", onchange: function (ev) {
    rows.forEach(function (r) { st.picked[r.character + SEP + r.emotion] = ev.target.checked; });
    renderEmotions();
  } });
  var t = table([{ label: "", class: "col-pick" }, "角色", "感情", "参考音频", "参考文本", "语言", { label: "操作", class: "acts" }], ["col-pick"]);
  t.querySelector("thead th").appendChild(headPick);

  if (!rows.length) {
    v.appendChild(card({
      kicker: "EMOTIONS", title: "感情库",
      desc: "emotions.json：" + (e.file || "—"),
      tools: [btn("＋ 新增感情", { sm: true, kind: "primary", onclick: function () { openEmotionForm(null); } })],
      body: [
        h("div", { class: "row-tight" }, [h("span", { class: "grow" }, qInput)]),
        chipsBox,
        empty((e.rows || []).length ? "没有匹配的结果" : "感情库还是空的", (e.rows || []).length ? "换个关键词，或者点上面的「全部」。" : "点右上角「新增感情」登记第一条参考音频。")
      ]
    }));
  } else {
    rows.forEach(function (r) {
      var key = r.character + SEP + r.emotion;
      var tr = h("tr", { "data-selected": st.picked[key] ? "true" : null, "data-warn": r.warning ? "true" : null });
      tr.appendChild(h("td", {}, h("input", {
        type: "checkbox", checked: !!st.picked[key], "aria-label": "选择 " + r.character + " " + r.emotion,
        onchange: function (ev) { st.picked[key] = ev.target.checked; tr.setAttribute("data-selected", ev.target.checked ? "true" : ""); renderEmotions(); }
      })));
      tr.appendChild(h("td", {}, h("b", { text: r.character })));
      tr.appendChild(h("td", {}, [h("span", { text: r.emotion }), r.warning ? h("span", { class: "badge", "data-tone": "warn", text: "⚠", title: r.warning }) : null]));
      tr.appendChild(h("td", { class: "cell-path", title: r.ref_audio_path }, r.ref_audio_path));
      tr.appendChild(h("td", { class: "cell-text", title: r.ref_audio_text || "" }, r.ref_audio_text || "—"));
      tr.appendChild(h("td", {}, badge(r.language || "默认")));
      tr.appendChild(h("td", { class: "acts" }, [
        btn("试听", { sm: true, kind: "soft", title: "带着这个音色跳到工作台", onclick: function () {
          state.studio.character = r.character;
          state.studio.emotion = r.emotion;
          state.studio.freeRef = false;
          go("studio");
          toast("已切到工作台：" + r.character + " / " + r.emotion, "ok", 2000);
        } }),
        btn("编辑", { sm: true, onclick: function () { openEmotionForm(r); } }),
        btn("复制", { sm: true, kind: "ghost", onclick: function () { openCopyForm([{ character: r.character, emotion: r.emotion }]); } }),
        btn("删除", { sm: true, kind: "danger", onclick: function () { deleteEmotions([{ character: r.character, emotion: r.emotion }], r.character + " / " + r.emotion); } })
      ]));
      t.body.appendChild(tr);
    });

    var body = [];
    if (e.warnings) body.push(note("有 " + e.warnings + " 条登记存在问题（路径为空、绝对路径或含 ..）。表格里带 ⚠ 的就是，建议改掉，否则合成时会失败。", "warn"));
    body.push(h("div", { class: "row-tight" }, [h("span", { class: "grow" }, qInput), badge(rows.length + " / " + (e.rows || []).length + " 条")]));
    body.push(chipsBox);
    body.push(batch);
    body.push(t);
    v.appendChild(card({
      kicker: "EMOTIONS", title: "感情库",
      desc: "emotions.json：" + (e.file || "—"),
      tools: [
        btn("＋ 新增感情", { sm: true, kind: "primary", onclick: function () { openEmotionForm(null); } }),
        btn("导出全部", { sm: true, kind: "soft", onclick: function () { exportEmotions({}); } }),
        btn("导入", { sm: true, onclick: function () { go("packs"); } })
      ],
      body: body
    }));
  }

  /* ---------- 角色管理 ---------- */
  var chBody = [];
  if (!(e.characters || []).length) {
    chBody.push(empty("还没有角色", "登记一条感情就会自动建出角色。"));
  } else {
    var ct = table(["角色", "感情数", { label: "操作", class: "acts" }]);
    (e.characters || []).forEach(function (c) {
      ct.body.appendChild(h("tr", {}, [
        h("td", {}, h("b", { text: c.name })),
        h("td", {}, badge(c.count + " 条", "accent")),
        h("td", { class: "acts" }, [
          btn("只看它", { sm: true, kind: "ghost", onclick: function () { state.emo.ch = c.name; renderEmotions(); } }),
          btn("导出", { sm: true, kind: "soft", onclick: function () { exportEmotions({ characters: [c.name] }); } }),
          btn("重命名", { sm: true, onclick: function () { openRenameChar(c.name); } }),
          btn("删除", { sm: true, kind: "danger", onclick: function () { deleteCharacter(c.name, c.count); } })
        ])
      ]));
    });
    chBody.push(ct);
  }
  v.appendChild(card({
    kicker: "CHARACTERS", title: "角色",
    desc: "重命名会把这个角色名下所有感情一起搬过去。",
    body: chBody
  }));
}


/* =====================================================================
   3) 感情包 · 导入 / 导出 / 快照
   ===================================================================== */

LOADERS.packs = function () {
  var jobs = [apiGet("packs").then(function (d) { state.packs = d; })];
  if (!state.emotions) {
    jobs.push(apiGet("emotions").then(function (d) { state.emotions = d; state.loaded.emotions = true; }));
  }
  return Promise.all(jobs);
};

function packState() {
  var p = state.pack;
  if (!p.scope) p.scope = "all";
  if (!p.chars) p.chars = {};
  if (!p.mode) p.mode = (state.emotions && state.emotions.default_import_mode) || "merge";
  if (p.dry === undefined || p.dry === null) p.dry = true;
  return p;
}

function importModeOptions() {
  var raw = (state.emotions && state.emotions.import_modes) || ["merge", "overwrite", "replace"];
  var labels = { merge: "合并 · 只补新", overwrite: "覆盖 · 冲突用新值", replace: "替换 · 先清空" };
  return raw.map(function (m) { return { value: m, label: labels[m] || m }; });
}

function modeHint(mode) {
  if (mode === "replace") return note("替换：先清空现有 emotions.json 再整份写入，包里没有的条目会被丢弃。建议先试运行，或先存一份快照。", "danger");
  if (mode === "overwrite") return note("覆盖：同名条目用包里的值替换；包里没有的条目保持不动。", "warn");
  return note("合并：只补新条目，同名冲突保留现有值（记进「跳过」）。最安全，推荐默认用它。", "info");
}

/* ---------- 范围 ---------- */

function scopeChars() {
  var p = packState();
  var out = [];
  emoChars().forEach(function (c) { if (p.chars[c.name]) out.push(c.name); });
  return out;
}

function currentScope() {
  var p = packState();
  if (p.scope === "picked") return { items: pickedKeys() };
  if (p.scope === "chars") return { characters: scopeChars() };
  return {};
}

function scopeLabel(sc) {
  sc = sc || {};
  if (sc.items) return "感情库勾选的 " + sc.items.length + " 条";
  if (sc.characters) return sc.characters.length ? "角色 " + sc.characters.join("、") : "未选角色";
  return "全部感情";
}

function scopeCount(sc) {
  sc = sc || {};
  if (sc.items) return sc.items.length;
  if (sc.characters) {
    var n = 0;
    emoChars().forEach(function (c) { if (sc.characters.indexOf(c.name) >= 0) n += c.count; });
    return n;
  }
  return emoRows().length;
}

function scopeReady(sc) {
  sc = sc || {};
  if (sc.items) return sc.items.length > 0;
  if (sc.characters) return sc.characters.length > 0;
  return emoRows().length > 0;
}

function normScope(opts) {
  opts = opts || {};
  var sc = {};
  if (opts.items) sc.items = opts.items;
  if (opts.characters) sc.characters = opts.characters;
  return sc;
}

function exportQuery(sc, memo, filename) {
  var q = {};
  if (sc.characters && sc.characters.length) q.characters = sc.characters.join(",");
  if (sc.items && sc.items.length) q.items = JSON.stringify(sc.items);
  if (memo) q.note = memo;
  if (filename) q.filename = filename;
  return q;
}

function exportBody(sc, memo, filename) {
  return {
    characters: sc.characters || [],
    items: sc.items || [],
    note: memo || "",
    filename: filename || ""
  };
}

/* ---------- 导出 ---------- */

function exportEmotions(opts) {
  opts = opts || {};
  var sc = normScope(opts);
  if (!scopeReady(sc)) { toast("这个范围里没有可导出的条目", "warn"); return Promise.resolve(null); }
  return apiPost("emotions/export-preview", exportBody(sc, opts.note, opts.filename)).then(function (d) {
    var okMsg = "已导出 " + d.filename + " · " + d.summary.characters + " 角色 / " + d.summary.emotions + " 条 · " + fmtBytes(d.bytes);
    var task = null;
    try { task = SDK.download("emotions/export", exportQuery(sc, opts.note, d.filename), d.filename); }
    catch (e) { fail(e, "下载失败"); return d; }
    if (task && typeof task.then === "function") task.then(function () { toast(okMsg, "ok", 4600); }, function (e) { fail(e, "下载失败"); });
    else toast(okMsg, "ok", 4600);
    return d;
  }).catch(function (e) { return fail(e, "导出失败"); });
}

function previewExportText(opts) {
  opts = opts || {};
  var sc = normScope(opts);
  if (!scopeReady(sc)) { toast("这个范围里没有可导出的条目", "warn"); return; }
  apiPost("emotions/export-preview", exportBody(sc, opts.note, opts.filename)).then(function (d) {
    var box = textarea(d.text, null, { mono: true, rows: 15 });
    openModal({
      kicker: "EXPORT TEXT",
      title: d.filename,
      body: [
        kv([["范围", scopeLabel(sc)], ["角色 / 感情", d.summary.characters + " / " + d.summary.emotions], ["体积", fmtBytes(d.bytes)]]),
        box,
        note("浏览器不给剪贴板权限时，直接在文本框里全选复制也一样。", "info")
      ],
      okText: "复制全文",
      cancelText: "关闭"
    }).then(function (ok) { if (ok) copyText(d.text, "导出文本已复制"); });
    setTimeout(function () { try { box.focus(); box.setSelectionRange(0, 0); } catch (e) {} }, 60);
  }).catch(function (e) { fail(e, "生成导出文本失败"); });
}

function savePackFrom(opts) {
  opts = opts || {};
  var sc = normScope(opts);
  if (!scopeReady(sc)) { toast("这个范围里没有可保存的条目", "warn"); return; }
  var ctl = {};
  var over = { on: false };
  ctl.name = input(opts.filename || "", null, { mono: true, placeholder: "留空自动命名，例如 emotions-20260903.json" });
  ctl.memo = input(opts.note || "", null, { placeholder: "备注，例如：上线前基线" });
  formModal({
    kicker: "SNAPSHOT",
    title: "存为快照 · " + scopeLabel(sc),
    body: [
      note("快照写在插件数据目录（不进仓库），随时可以恢复或下载。", "info"),
      field({ label: "文件名", control: ctl.name, hint: "非法字符会被替换，后缀自动补 .json" }),
      field({ label: "备注", control: ctl.memo }),
      field({ label: "同名处理", control: switchBox(false, "允许覆盖同名快照", function (e) { over.on = e.target.checked; }) })
    ],
    okText: "保存",
    onOk: function () {
      return {
        characters: sc.characters || [],
        items: sc.items || [],
        filename: ctl.name.value.trim(),
        note: ctl.memo.value.trim(),
        overwrite: over.on
      };
    }
  }).then(function (payload) {
    if (!payload) return;
    apiPost("packs/save", payload).then(function (d) {
      state.packs = Object.assign({}, state.packs || {}, { packs: d.packs || [] });
      toast("已保存快照 " + d.filename + "（" + d.summary.characters + " 角色 / " + d.summary.emotions + " 条）", "ok", 4600);
      refreshOverview();
      if (state.tab === "packs") renderPacks();
    }).catch(function (e) { fail(e, "保存快照失败"); });
  });
}

/* ---------- 变更报告 ---------- */

function diffCol(kind, label, items, tone) {
  items = items || [];
  var col = h("div", { class: "diff-col", "data-kind": kind });
  col.appendChild(h("div", { class: "diff-head" }, [
    h("span", { text: label }),
    h("b", { text: String(items.length) })
  ]));
  var list = h("ul", { class: "diff-list" });
  if (!items.length) {
    list.appendChild(h("li", {}, dim("—")));
  } else {
    items.slice(0, 60).forEach(function (it) {
      var li = h("li", {}, [
        h("span", { text: (it.character || "?") + " · " + (it.emotion || "整个角色") })
      ]);
      if (it.reason) li.appendChild(h("span", { class: "diff-why", text: "  " + it.reason }));
      else if (it.before && it.after) {
        var b = String(it.before.ref_audio_path || "");
        var a = String(it.after.ref_audio_path || "");
        li.appendChild(h("span", { class: "diff-why", text: b === a ? "  文本/语言有变" : "  " + shorten(b, 18) + " → " + shorten(a, 18) }));
      }
      list.appendChild(li);
    });
    if (items.length > 60) list.appendChild(h("li", {}, dim("…还有 " + (items.length - 60) + " 条")));
  }
  col.appendChild(list);
  if (tone) col.dataset.tone = tone;
  return col;
}

function reportCard(report, opts) {
  opts = opts || {};
  var c = report.counts || {};
  var modeText = { merge: "合并", overwrite: "覆盖", replace: "替换" }[report.mode] || String(report.mode || "?");
  var head = [];
  if (report.dry_run) head.push(note("这是试运行，emotions.json 没有被写入。确认没问题后关掉「试运行」再执行一次。", "warn"));
  else if (!report.changed) head.push(note("没有任何实际变更，文件未改动。", "info"));
  else head.push(note("已写入 emotions.json，现在共 " + ((report.result || {}).characters || 0) + " 角色 / " + ((report.result || {}).emotions || 0) + " 条感情。", "ok"));

  var meta = report.meta || {};
  var metaRows = [
    ["模式", modeText + (report.dry_run ? " · 试运行" : "")],
    report.filename ? ["来源快照", report.filename] : null,
    meta.format ? ["包格式", String(meta.format) + (meta.version ? " v" + meta.version : "")] : null,
    meta.exported_at ? ["导出时间", String(meta.exported_at)] : null,
    meta.plugin_version ? ["导出自插件", "v" + String(meta.plugin_version)] : null,
    meta.source ? ["来源", String(meta.source)] : null,
    meta.note ? ["备注", String(meta.note)] : null
  ];

  return card({
    sub: true,
    kicker: opts.kicker || "REPORT",
    title: opts.title || "变更报告",
    tools: report.summary_text ? [btn("复制摘要", { sm: true, kind: "ghost", onclick: function () { copyText(report.summary_text, "摘要已复制"); } })] : null,
    body: [
      h("div", { class: "stat-grid" }, [
        stat(c.added || 0, "新增", c.added ? "ok" : null),
        stat(c.updated || 0, "更新", c.updated ? "accent" : null),
        stat(c.skipped || 0, "跳过", c.skipped ? "warn" : null),
        stat(c.unchanged || 0, "无变化", null),
        stat(c.removed || 0, "移除", c.removed ? "danger" : null),
        stat(c.invalid || 0, "无效", c.invalid ? "danger" : null)
      ]),
      head,
      kv(metaRows),
      h("div", { class: "diff" }, [
        diffCol("added", "新增", report.added),
        diffCol("updated", "更新", report.updated),
        diffCol("skipped", "跳过", report.skipped),
        diffCol("removed", "移除", report.removed),
        diffCol("invalid", "无效", report.invalid)
      ])
    ]
  });
}

/* ---------- 导入 ---------- */

function applyImportResult(d, label) {
  var report = d.report || {};
  state.pack.report = report;
  if (!report.dry_run && report.changed) applyEmotionRows(d);
  else { refreshOverview(); }
  var c = report.counts || {};
  var tone = report.dry_run ? "warn" : (report.changed ? "ok" : "info");
  toast((label || "导入") + (report.dry_run ? " 试运行完成" : " 完成") + " · 新增 " + (c.added || 0) + " / 更新 " + (c.updated || 0) + " / 跳过 " + (c.skipped || 0) + " / 移除 " + (c.removed || 0), tone, 5200);
}

function runImport(dryRun) {
  var p = packState();
  var text = String(p.importText || "").trim();
  if (!text) { toast("先粘贴或拖入一份感情包", "warn"); return; }
  var go2 = function () {
    p.busy = true;
    renderPacks();
    apiPost("emotions/import", { text: text, mode: p.mode, dry_run: !!dryRun })
      .then(function (d) { p.busy = false; applyImportResult(d, "导入"); renderPacks(); })
      .catch(function (e) { p.busy = false; renderPacks(); fail(e, "导入失败"); });
  };
  if (dryRun || p.mode !== "replace") { go2(); return; }
  confirmModal("确认用替换模式导入？", "替换模式会先清空当前 emotions.json，包里没有的条目全部丢弃。这一步不可撤销，建议先存快照。", { danger: true, okText: "我确认，替换" })
    .then(function (ok) { if (ok) go2(); });
}

function restorePack(item, mode, dryRun) {
  var run = function () {
    apiPost("packs/restore", { filename: item.filename, mode: mode, dry_run: !!dryRun })
      .then(function (d) { applyImportResult(d, "恢复 " + item.filename); renderPacks(); })
      .catch(function (e) { fail(e, "恢复失败"); });
  };
  if (dryRun || mode !== "replace") { run(); return; }
  confirmModal("用替换模式恢复 " + item.filename + "？", "会先清空当前 emotions.json，再整份写入这个快照的内容。不可撤销。", { danger: true, okText: "替换恢复" })
    .then(function (ok) { if (ok) run(); });
}

function openRestoreForm(item) {
  var pick = { mode: packState().mode, dry: true };
  var host = h("div", {});
  var draw = function () {
    clear(host);
    append(host, [
      segment(importModeOptions(), pick.mode, function (v) { pick.mode = v; draw(); }),
      h("div", { style: "margin-top:10px" }, modeHint(pick.mode))
    ]);
  };
  draw();
  formModal({
    kicker: "RESTORE",
    title: "恢复快照 · " + item.filename,
    body: [
      kv([
        ["角色 / 感情", (item.characters || 0) + " / " + (item.emotions || 0)],
        ["体积", fmtBytes(item.bytes || 0)],
        ["修改时间", item.modified || "—"],
        item.note ? ["备注", item.note] : null
      ]),
      field({ label: "合并模式", control: host }),
      field({ label: "试运行", control: switchBox(true, "只看变更，不写文件", function (e) { pick.dry = e.target.checked; }) })
    ],
    okText: "执行",
    onOk: function () { return { mode: pick.mode, dry: pick.dry }; }
  }).then(function (r) {
    if (!r) return;
    restorePack(item, r.mode, r.dry);
  });
}

function deletePack(item) {
  confirmModal("删除快照 " + item.filename + "？", "文件会从插件数据目录里删掉，无法找回。当前 emotions.json 不受影响。", { danger: true, okText: "删除" })
    .then(function (ok) {
      if (!ok) return;
      apiPost("packs/delete", { filename: item.filename }).then(function (d) {
        state.packs = Object.assign({}, state.packs || {}, { packs: d.packs || [] });
        toast("已删除 " + d.deleted, "ok");
        refreshOverview();
        renderPacks();
      }).catch(function (e) { fail(e, "删除失败"); });
    });
}

function downloadPack(item) {
  try {
    var task = SDK.download("packs/download", { filename: item.filename }, item.filename);
    if (task && typeof task.then === "function") task.then(function () { toast("已下载 " + item.filename, "ok"); }, function (e) { fail(e, "下载失败"); });
    else toast("已下载 " + item.filename, "ok");
  } catch (e) { fail(e, "下载失败"); }
}

/* ---------- 拖放区 ---------- */

function dropZone(onText) {
  var picker = h("input", { type: "file", accept: ".json,application/json" });
  var zone = h("div", { class: "drop", tabindex: "0", role: "button" }, [
    h("b", { text: "把 .json 感情包拖进来" }),
    h("span", { text: "或点这里选文件 · 上限 4MB · 完整感情包和裸 emotions.json 都吃" }),
    picker
  ]);
  function read(file) {
    if (!file) return;
    if (file.size > 4 * 1024 * 1024) { toast("文件超过 4MB，已拒绝", "danger"); return; }
    var fr = new FileReader();
    fr.onload = function () { onText(String(fr.result || ""), file.name); };
    fr.onerror = function () { toast("读取文件失败", "danger"); };
    try { fr.readAsText(file, "utf-8"); } catch (e) { fail(e, "读取文件失败"); }
  }
  zone.addEventListener("click", function (e) { if (e.target !== picker) picker.click(); });
  zone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); picker.click(); }
  });
  picker.addEventListener("change", function () {
    read(picker.files && picker.files[0]);
    picker.value = "";
  });
  ["dragenter", "dragover"].forEach(function (n) {
    zone.addEventListener(n, function (e) { e.preventDefault(); e.stopPropagation(); zone.dataset.over = "true"; });
  });
  ["dragleave", "dragend"].forEach(function (n) {
    zone.addEventListener(n, function (e) { e.preventDefault(); zone.dataset.over = "false"; });
  });
  zone.addEventListener("drop", function (e) {
    e.preventDefault();
    e.stopPropagation();
    zone.dataset.over = "false";
    var dt = e.dataTransfer;
    if (!dt) return;
    if (dt.files && dt.files.length) { read(dt.files[0]); return; }
    var txt = "";
    try { txt = dt.getData("text/plain") || ""; } catch (err) { txt = ""; }
    if (txt) onText(txt, "");
  });
  return zone;
}

/* ---------- 页面 ---------- */

function renderPacks() {
  var v = clear(viewNode("packs"));
  var p = packState();
  var d = state.packs || { packs: [], directory: "" };
  var chars = emoChars();
  var rows = emoRows();
  var sc = currentScope();
  var ready = scopeReady(sc);

  /* ===== 导出 ===== */
  var scopeBox = h("div", {}, segment([
    { value: "all", label: "全部 · " + rows.length },
    { value: "picked", label: "感情库勾选 · " + pickedKeys().length },
    { value: "chars", label: "按角色挑" }
  ], p.scope, function (val) { p.scope = val; renderPacks(); }));

  var exBody = [
    field({ label: "导出范围", control: scopeBox, desc: "「感情库勾选」用的是感情库页面表格里打勾的那些条目。" })
  ];

  if (p.scope === "chars") {
    var picker = h("div", { class: "chips" });
    if (!chars.length) picker.appendChild(dim("还没有角色，先去感情库登记一条。"));
    chars.forEach(function (c) {
      var b = h("button", {
        type: "button",
        class: "chip chip-btn",
        "aria-pressed": p.chars[c.name] ? "true" : "false",
        text: c.name + " · " + c.count
      });
      b.addEventListener("click", function () { p.chars[c.name] = !p.chars[c.name]; renderPacks(); });
      picker.appendChild(b);
    });
    var picks = [picker];
    if (chars.length > 1) {
      picks.push(h("div", { class: "btnrow" }, [
        btn("全选", { sm: true, kind: "ghost", onclick: function () { chars.forEach(function (c) { p.chars[c.name] = true; }); renderPacks(); } }),
        btn("清空", { sm: true, kind: "ghost", onclick: function () { p.chars = {}; renderPacks(); } })
      ]));
    }
    exBody.push(field({ label: "挑角色", control: picks }));
  }

  if (p.scope === "picked" && !pickedKeys().length) {
    exBody.push(note("感情库里还没有勾选任何条目。切到「感情库」页面，用行首的复选框选中要导出的感情。", "warn"));
  }

  exBody.push(h("div", { class: "grid-2" }, [
    field({ label: "备注", control: input(p.note, null, { placeholder: "会写进包头 note，方便日后认出这是什么", oninput: function (e) { p.note = e.target.value; } }) }),
    field({ label: "文件名", control: input(p.filename, null, { mono: true, placeholder: "留空自动命名", oninput: function (e) { p.filename = e.target.value; } }) })
  ]));

  exBody.push(kv([
    ["当前范围", scopeLabel(sc)],
    ["条目数", String(scopeCount(sc)) + " 条感情"]
  ]));

  exBody.push(h("div", { class: "btnrow" }, [
    btn("下载 .json", { kind: "primary", disabled: !ready, onclick: function () { exportEmotions({ characters: sc.characters, items: sc.items, note: p.note, filename: p.filename }); } }),
    btn("预览 / 复制文本", { kind: "soft", disabled: !ready, onclick: function () { previewExportText({ characters: sc.characters, items: sc.items, note: p.note, filename: p.filename }); } }),
    btn("存为服务端快照", { disabled: !ready, onclick: function () { savePackFrom({ characters: sc.characters, items: sc.items, note: p.note, filename: p.filename }); } })
  ]));

  v.appendChild(card({
    kicker: "EXPORT",
    title: "导出感情包",
    desc: "导出的是一份带头部信息的 JSON，可以直接发给别人，或者当作模板备份。",
    tools: [btn("导出全部", { sm: true, kind: "ghost", onclick: function () { exportEmotions({}); } })],
    body: exBody
  }));

  /* ===== 导入 ===== */
  var ta = textarea(p.importText, null, {
    mono: true, rows: 9,
    placeholder: "把感情包 JSON 粘在这里，也可以直接粘裸 emotions.json（{角色:{感情:{...}}}）",
    oninput: function (e) { p.importText = e.target.value; if (p.fileName) { p.fileName = ""; } }
  });

  var zone = dropZone(function (text, name) {
    p.importText = text;
    p.fileName = name || "";
    p.report = null;
    renderPacks();
    toast(name ? "已读入 " + name + "（" + fmtBytes(text.length) + "）" : "已读入拖放的文本", "ok");
  });

  var imBody = [
    h("div", { class: "io-panel" }, [
      h("div", {}, [
        field({ label: "感情包内容", tag: p.fileName ? chip(p.fileName, "chip-mono chip-accent") : null, control: ta, hint: String(p.importText || "").length ? fmtBytes(String(p.importText).length) + " · " + String(p.importText).length + " 字符" : "还是空的" })
      ]),
      h("div", {}, [
        field({ label: "从文件读入", control: zone }),
        h("div", { class: "btnrow" }, [
          btn("清空内容", { sm: true, kind: "ghost", disabled: !String(p.importText || "").length, onclick: function () { p.importText = ""; p.fileName = ""; p.report = null; renderPacks(); } }),
          btn("格式化 JSON", { sm: true, kind: "ghost", disabled: !String(p.importText || "").length, onclick: function () {
            try {
              p.importText = JSON.stringify(JSON.parse(String(p.importText)), null, 2);
              renderPacks();
              toast("已格式化", "ok", 1800);
            } catch (e) { toast("这不是合法 JSON：" + (e && e.message ? e.message : String(e)), "danger"); }
          } })
        ])
      ])
    ]),
    field({ label: "合并模式", control: segment(importModeOptions(), p.mode, function (val) { p.mode = val; renderPacks(); }) }),
    modeHint(p.mode),
    field({ label: "试运行", control: switchBox(p.dry, "只算差异，不写 emotions.json", function (e) { p.dry = e.target.checked; renderPacks(); }) }),
    h("div", { class: "btnrow" }, [
      btn(p.busy ? "处理中…" : (p.dry ? "试运行" : "执行导入"), {
        kind: p.dry ? "soft" : "primary",
        disabled: !!p.busy || !String(p.importText || "").trim(),
        onclick: function () { runImport(p.dry); }
      }),
      p.dry ? btn("直接写入", { kind: "primary", disabled: !!p.busy || !String(p.importText || "").trim(), onclick: function () { runImport(false); } }) : null,
      btn("先存一份当前快照", { kind: "ghost", onclick: function () { savePackFrom({ note: "导入前自动备份" }); } })
    ].filter(Boolean))
  ];

  v.appendChild(card({
    kicker: "IMPORT",
    title: "导入感情包",
    desc: "先试运行看差异，确认无误再写入。写入前建议存一份快照当保险。",
    body: imBody
  }));

  if (p.report) v.appendChild(reportCard(p.report, { title: p.report.filename ? "恢复报告 · " + p.report.filename : "导入报告" }));

  /* ===== 快照 ===== */
  var list = h("div", { class: "pack-list" });
  var packs = d.packs || [];
  if (!packs.length) {
    list.appendChild(empty("还没有快照", "点右上角「保存当前」，会把现在的 emotions.json 存成一份带时间戳的快照。"));
  }
  packs.forEach(function (item) {
    var box = h("div", { class: "pack", "data-error": item.error ? "true" : null });
    var main = h("div", { class: "pack-main" }, [
      h("div", { class: "pack-name", text: item.filename, title: item.filename }),
      h("div", { class: "pack-sub", text: (item.modified || "—") + " · " + fmtBytes(item.bytes || 0) + (item.note ? " · " + shorten(item.note, 40) : "") })
    ]);
    box.appendChild(main);
    var chipbox = h("div", { class: "pack-chips" });
    if (item.error) chipbox.appendChild(chip("解析失败", "chip-danger"));
    else {
      chipbox.appendChild(chip((item.characters || 0) + " 角色", "chip-accent"));
      chipbox.appendChild(chip((item.emotions || 0) + " 条", "chip-mono"));
      if (item.exported_at) chipbox.appendChild(chip(shorten(String(item.exported_at), 19), "chip-mono"));
    }
    box.appendChild(chipbox);
    box.appendChild(h("div", { class: "btnrow" }, [
      btn("恢复", { sm: true, kind: "soft", disabled: !!item.error, onclick: function () { openRestoreForm(item); } }),
      btn("下载", { sm: true, onclick: function () { downloadPack(item); } }),
      btn("删除", { sm: true, kind: "danger", onclick: function () { deletePack(item); } })
    ]));
    if (item.error) box.appendChild(note(String(item.error), "danger"));
    list.appendChild(box);
  });

  v.appendChild(card({
    kicker: "SNAPSHOTS",
    title: "服务端快照",
    desc: "存在插件数据目录里，重装插件不会丢；上限 300 份，按修改时间倒序。",
    tools: [
      btn("保存当前", { sm: true, kind: "primary", onclick: function () { savePackFrom({ note: "手动快照" }); } }),
      btn("刷新", { sm: true, kind: "ghost", onclick: function () { reloadTab("packs"); } })
    ],
    body: [
      kv([["目录", d.directory || "—"], ["数量", packs.length + " / 300"]]),
      list
    ]
  }));
}


/* ============================================================ 配置 */

function nl() { return String.fromCharCode(10); }

function cfgKey(parent, key) { return parent ? (parent + SEP + String(key)) : String(key); }

function cfgIsDirty(parent, key) {
  return Object.prototype.hasOwnProperty.call(state.cfg.dirty, cfgKey(parent, key));
}

function cfgGet(parent, f) {
  var k = cfgKey(parent, f.key);
  if (Object.prototype.hasOwnProperty.call(state.cfg.dirty, k)) return state.cfg.dirty[k];
  return f.value;
}

function cfgApply(parent, f, value) {
  var k = cfgKey(parent, f.key);
  var same;
  try { same = JSON.stringify(value) === JSON.stringify(f.value); }
  catch (e) { same = value === f.value; }
  if (same) delete state.cfg.dirty[k];
  else state.cfg.dirty[k] = value;
}

function cfgDirtyCount() {
  var n = 0;
  for (var k in state.cfg.dirty) if (Object.prototype.hasOwnProperty.call(state.cfg.dirty, k)) n++;
  return n;
}

/* dirty 表拍平成后端要的 values：顶层键直接给值，object 子键聚合成只含改动的 dict */
function cfgPayload() {
  var top = {};
  for (var k in state.cfg.dirty) {
    if (!Object.prototype.hasOwnProperty.call(state.cfg.dirty, k)) continue;
    var v = state.cfg.dirty[k];
    var i = k.indexOf(SEP);
    if (i < 0) { top[k] = v; continue; }
    var parent = k.slice(0, i);
    var child = k.slice(i + 1);
    if (!top[parent] || typeof top[parent] !== "object") top[parent] = {};
    top[parent][child] = v;
  }
  return top;
}

function cfgUpdateBar() {
  var bar = state.cfg.barNode;
  if (!bar || !bar.parentNode) return;
  var n = cfgDirtyCount();
  bar.hidden = n === 0;
  var txt = bar.textNode;
  if (txt) {
    clear(txt);
    txt.appendChild(D.createTextNode("已修改 "));
    txt.appendChild(h("b", { text: String(n) }));
    txt.appendChild(D.createTextNode(" 项，尚未保存"));
  }
}

function cfgRestartTag(key) {
  var list = (state.config && state.config.restart_required_keys) || [];
  for (var i = 0; i < list.length; i++) if (String(list[i]) === String(key)) return true;
  return false;
}

function cfgControl(parent, f, holder) {
  var type = String(f.type || "string");
  function mark() {
    holder.dataset.dirty = cfgIsDirty(parent, f.key) ? "true" : "false";
    cfgUpdateBar();
  }
  function apply(v) { cfgApply(parent, f, v); mark(); }
  var cur = cfgGet(parent, f);
  var text0 = (cur === null || cur === undefined) ? "" : String(cur);

  if (type === "bool") {
    var box = switchBox(!!cur, cur ? "已开启" : "已关闭", function (ev) {
      var on = !!ev.target.checked;
      apply(on);
      var lab = box.querySelector(".switch-text");
      if (lab) lab.textContent = on ? "已开启" : "已关闭";
    });
    return box;
  }

  if (type === "int" || type === "float") {
    return input(text0, function (ev) {
      var raw = String(ev.target.value || "").trim();
      if (!raw) { apply(f.value); ev.target.value = String(f.value); return; }
      var num = type === "int" ? parseInt(raw, 10) : parseFloat(raw);
      if (isNaN(num)) {
        toast((f.title || f.key) + "：只能填数字", "warn");
        apply(f.value);
        ev.target.value = String(f.value);
        return;
      }
      apply(num);
      ev.target.value = String(num);
    }, { type: "number", step: type === "int" ? "1" : "0.1", mono: true });
  }

  if (type === "list") {
    var arr0 = (cur && cur.length) ? cur : [];
    return textarea(arr0.join(nl()), function (ev) {
      var arr = String(ev.target.value || "").split(nl()).map(function (s) { return s.trim(); })
        .filter(function (s) { return s.length > 0; });
      apply(arr);
      ev.target.value = arr.join(nl());
    }, { rows: Math.min(9, Math.max(3, arr0.length + 1)), mono: true, placeholder: "每行一条" });
  }

  if (f.secret) {
    var masked = (state.config && state.config.masked) || "__astrbot_masked__";
    var raw0 = (f.value === null || f.value === undefined) ? "" : String(f.value);
    var isSet = raw0 === masked;
    var shown = (cur === f.value) ? "" : text0;
    var inp = input(shown, function (ev) {
      var t = String(ev.target.value || "");
      apply(t ? t : f.value);
    }, { type: "password", mono: true, placeholder: isSet ? "已配置 · 留空表示不改动" : "未配置" });
    if (!isSet) return inp;
    inp.classList.add("grow");
    return h("div", { class: "row-tight" }, [
      inp,
      btn("清空", { sm: true, kind: "ghost", title: "把这个密钥保存成空值", onclick: function () {
        apply("");
        inp.value = "";
        inp.placeholder = "保存后将被清空";
        toast("已标记清空，记得点保存", "warn");
      } })
    ]);
  }

  var options = f.options || [];
  if (options.length) {
    return select(options, text0, function (ev) { apply(String(ev.target.value)); });
  }

  var multiline = type === "text" || text0.indexOf(nl()) >= 0 || text0.length > 110;
  if (multiline) {
    return textarea(text0, function (ev) { apply(String(ev.target.value)); },
      { rows: text0.length > 400 ? 9 : 6, mono: false, placeholder: "留空则用默认值" });
  }

  var monoish = new RegExp("(url|path|regex|model|_id|key|token)", "i").test(String(f.key));
  return input(text0, function (ev) { apply(String(ev.target.value)); },
    { mono: monoish, placeholder: (f.default === null || f.default === undefined || f.default === "") ? null : String(f.default) });
}

function cfgFieldNode(parent, f) {
  var type = String(f.type || "string");
  var cur = type === "object" ? null : cfgGet(parent, f);
  var text0 = (cur === null || cur === undefined) ? "" : String(cur);
  var full = type === "object" || type === "list" || type === "text"
    || text0.indexOf(nl()) >= 0 || text0.length > 110;

  var holder = h("div", {
    class: "cfg-field",
    "data-dirty": (type !== "object" && cfgIsDirty(parent, f.key)) ? "true" : "false"
  });
  if (full) holder.setAttribute("data-span", "full");

  var lab = h("label", { class: "field-label" }, [h("span", { text: f.title || f.key })]);
  lab.appendChild(h("span", { class: "field-key", text: f.key }));
  if (f.secret) lab.appendChild(badge("密钥", "accent"));
  if (cfgRestartTag(f.key)) lab.appendChild(h("span", { class: "cfg-restart" }, badge("需重载插件", "warn")));
  holder.appendChild(lab);
  if (f.description) holder.appendChild(h("p", { class: "field-desc", text: f.description }));

  if (type === "object") {
    var kids = h("div", { class: "cfg-children" });
    var children = f.children || [];
    if (!children.length) kids.appendChild(dim("这一组没有可见子项"));
    children.forEach(function (child) { kids.appendChild(cfgFieldNode(f.key, child)); });
    holder.appendChild(kids);
    return holder;
  }

  holder.appendChild(cfgControl(parent, f, holder));

  var hints = [];
  if (f.hint) hints.push(String(f.hint));
  var dflt = f.default;
  if (type === "bool") hints.push("默认 " + (dflt ? "开" : "关"));
  else if (type === "list") hints.push("默认 " + (((dflt || []).length) ? (dflt || []).join(" / ") : "空"));
  else if (!f.secret && dflt !== null && dflt !== undefined && String(dflt) !== "") hints.push("默认 " + shorten(String(dflt), 52));
  if (options0(f).length) hints.push("可选 " + options0(f).join(" / "));
  if (hints.length) holder.appendChild(h("p", { class: "field-hint", text: hints.join("  ·  ") }));
  return holder;
}

function options0(f) { return f.options || []; }

/* 搜索命中：object 只要有一个子键命中就整块显示 */
function cfgMatch(f, q) {
  if (!q) return true;
  var hay = String(f.key || "") + " " + String(f.title || "") + " " + String(f.description || "") + " " + String(f.hint || "");
  if (hay.toLowerCase().indexOf(q) >= 0) return true;
  var children = f.children || [];
  for (var i = 0; i < children.length; i++) if (cfgMatch(children[i], q)) return true;
  return false;
}

function saveConfig() {
  if (state.cfg.saving) return;
  var n = cfgDirtyCount();
  if (!n) { toast("没有需要保存的修改", "warn"); return; }
  var values = cfgPayload();
  state.cfg.saving = true;
  apiPost("config/save", { values: values }).then(function (d) {
    d = d || {};
    var saved = d.saved || [];
    var rejected = d.rejected || [];
    state.cfg.needsReload = !!d.needs_reload;
    state.cfg.savedAt = d.saved_at || "";
    state.cfg.rejected = rejected;
    state.cfg.dirty = {};
    if (rejected.length) toast("已保存 " + saved.length + " 项，" + rejected.length + " 项被拒绝", "warn", 9000);
    else toast("已保存 " + saved.length + " 项配置", "ok");
    return LOADERS.config();
  }).then(function () {
    state.cfg.saving = false;
    state.loaded.config = true;
    renderConfig();
    refreshOverview();
  }).catch(function (e) {
    state.cfg.saving = false;
    fail(e, "保存失败");
    renderConfig();
  });
}

function renderConfig() {
  var v = clear(viewNode("config"));
  var d = state.config;
  if (!d) { v.appendChild(empty("配置还没加载", "点右上角「刷新」重试。")); return; }

  var groups = d.groups || [];
  var q = String(state.cfg.q || "").trim().toLowerCase();

  /* ---------- 头部：说明 + 搜索 ---------- */
  var qInput = input(state.cfg.q || "", null, {
    placeholder: "搜配置名 / 键名 / 说明…",
    oninput: function (ev) {
      state.cfg.q = ev.target.value;
      renderConfig();
      var node = $("cfg-q");
      if (node) { node.focus(); try { node.setSelectionRange(node.value.length, node.value.length); } catch (x) {} }
    }
  });
  qInput.id = "cfg-q";
  qInput.classList.add("grow");

  var jump = h("div", { class: "chips" });

  v.appendChild(card({
    kicker: "CONFIG",
    title: "插件配置",
    desc: "改完点底部「保存配置」。这里写的值和 AstrBot 插件管理页里的配置是同一份，保存即生效（少数项需要重载插件）。",
    tools: [btn("重新读取", { sm: true, kind: "ghost", onclick: function () { reloadTab("config"); } })],
    body: [
      h("div", { class: "row-tight" }, [qInput, badge(d.total + " 项", "mute")]),
      jump
    ]
  }));

  if (state.cfg.needsReload) {
    v.appendChild(note("刚保存的项目里包含「需重载插件」的配置（保活相关），去 AstrBot 插件管理页禁用再启用本插件才会生效。", "warn"));
  }
  if (state.cfg.rejected && state.cfg.rejected.length) {
    var rej = h("ul", { class: "diff-list" });
    state.cfg.rejected.forEach(function (item) {
      rej.appendChild(h("li", {}, [mono(String(item.key)), h("span", { class: "diff-why", text: String(item.reason || "") })]));
    });
    v.appendChild(card({
      kicker: "REJECTED",
      title: "上次保存被拒绝的项",
      desc: "这些键没有写进配置，检查一下取值范围。",
      tools: [btn("知道了", { sm: true, kind: "ghost", onclick: function () { state.cfg.rejected = []; renderConfig(); } })],
      body: rej
    }));
  }

  /* ---------- 分组 ---------- */
  var shown = 0;
  groups.forEach(function (g) {
    var fields = (g.fields || []).filter(function (f) { return cfgMatch(f, q); });
    if (!fields.length) return;
    shown += fields.length;

    var grid = h("div", { class: "cfg-fields" });
    fields.forEach(function (f) { grid.appendChild(cfgFieldNode("", f)); });

    var box = card({
      kicker: String(g.id || "").toUpperCase(),
      title: g.title || g.id,
      desc: g.description || "",
      body: grid,
      class: "cfg-group"
    });
    v.appendChild(box);

    jump.appendChild(h("button", {
      type: "button", class: "chip chip-btn", text: (g.title || g.id) + " " + fields.length,
      onclick: function () { try { box.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (x) { box.scrollIntoView(); } }
    }));
  });

  if (!shown) v.appendChild(empty("没有匹配的配置项", "换个关键词，或清空搜索框。"));

  /* ---------- 保存条 ---------- */
  var txt = h("span", { class: "savebar-text" });
  var bar = h("div", { class: "savebar", hidden: true }, [
    txt,
    h("span", { class: "grow" }),
    btn("放弃修改", { kind: "ghost", onclick: function () {
      if (!cfgDirtyCount()) return;
      state.cfg.dirty = {};
      renderConfig();
      toast("已放弃未保存的修改", "ok");
    } }),
    btn(state.cfg.saving ? "保存中…" : "保存配置", { kind: "primary", disabled: !!state.cfg.saving, onclick: saveConfig })
  ]);
  bar.textNode = txt;
  state.cfg.barNode = bar;
  v.appendChild(bar);
  cfgUpdateBar();

  if (state.cfg.savedAt) {
    v.appendChild(h("p", { class: "field-hint", text: "上次保存：" + state.cfg.savedAt }));
  }
}


/* ============================================================ 服务器 */

function srvItem(item) {
  var stateName = item.ok ? (item.busy ? "busy" : "ok") : "down";
  var sub = h("div", { class: "srv-sub" });
  sub.appendChild(badge(item.ok ? (item.busy ? "忙" : "在线") : "离线", item.ok ? (item.busy ? "warn" : "ok") : "danger"));
  if (item.latency_ms !== null && item.latency_ms !== undefined) sub.appendChild(chip(fmtMs(item.latency_ms), "chip-mono"));
  sub.appendChild(chip("角色 " + (item.character_count || 0)));
  var names = item.characters || [];
  names.slice(0, 6).forEach(function (n) { sub.appendChild(chip(n)); });
  if (names.length > 6) sub.appendChild(dim("+" + (names.length - 6)));

  var main = h("div", { class: "srv-main" }, [
    h("div", { class: "srv-url", title: item.url, text: item.url || "—" }),
    sub
  ]);
  if (item.error) main.appendChild(h("div", { class: "srv-err", text: shorten(String(item.error), 220) }));

  return h("div", { class: "srv" }, [
    h("i", { class: "srv-dot", "data-state": stateName }),
    main,
    btn("复制", { sm: true, kind: "ghost", onclick: function () { copyText(item.url, "服务器地址"); } })
  ]);
}

function renderServers() {
  var v = clear(viewNode("servers"));
  var d = state.servers;
  if (!d) { v.appendChild(empty("还没探测服务器", "点右上角「刷新」开始探测。")); return; }

  var servers = d.servers || [];
  var ka = d.keepalive || {};
  var down = servers.length - (d.online || 0);

  v.appendChild(card({
    kicker: "SERVERS",
    title: "TTS 服务器",
    desc: "逐个访问服务器判断可用性，顺便读回上面有哪些角色模型。单个探测超时 8 秒。",
    tools: [btn("重新探测", { sm: true, kind: "primary", onclick: function () { reloadTab("servers"); } })],
    body: [
      h("div", { class: "stat-grid" }, [
        stat(String(d.online || 0) + " / " + (d.total || 0), "在线 / 总数", down > 0 ? "warn" : "ok"),
        stat(String(d.queue_size || 0), "合成队列"),
        stat(String(ka.urls ? ka.urls.length : 0), "保活地址"),
        stat(ka.running ? "运行中" : "未运行", "保活任务", ka.enabled ? (ka.running ? "ok" : "warn") : null)
      ]),
      kv([["探测时间", d.checked_at || "—"]])
    ]
  }));

  var list = h("div", { class: "srv-list" });
  if (!servers.length) list.appendChild(empty("还没配置服务器", "去「配置 → 服务器」里填 tts_server_urls。"));
  servers.forEach(function (item) { list.appendChild(srvItem(item)); });
  v.appendChild(card({ kicker: "ENDPOINTS", title: "地址列表", body: list, sub: true }));

  /* ---------- 角色名对不上 ---------- */
  var mis = d.mismatch || {};
  var missing = mis.missing_on_server || [];
  var extra = mis.not_registered_locally || [];
  if (missing.length || extra.length) {
    var body = [];
    if (missing.length) {
      body.push(note("这些角色在 emotions.json 里登记了，但服务器上没有同名模型，合成时会直接失败。", "danger"));
      var c1 = h("div", { class: "chips" });
      missing.forEach(function (n) { c1.appendChild(chip(n, "chip-danger")); });
      body.push(c1);
    }
    if (extra.length) {
      body.push(note("服务器上还有这些角色没登记到本地感情库，可以在「感情库」里补上参考音频。", "warn"));
      var c2 = h("div", { class: "chips" });
      extra.forEach(function (n) { c2.appendChild(chip(n)); });
      body.push(c2);
    }
    v.appendChild(card({ kicker: "MISMATCH", title: "角色名对不上", body: body }));
  } else if (servers.length && d.online) {
    v.appendChild(note("本地感情库里的角色都能在服务器上找到，没有对不上的。", "ok"));
  }

  /* ---------- 保活 ---------- */
  var kaUrls = h("div", { class: "chips" });
  (ka.urls || []).forEach(function (u) { kaUrls.appendChild(chip(shorten(u, 58), "chip-mono")); });
  if (!(ka.urls || []).length) kaUrls.appendChild(dim("没有保活地址"));
  v.appendChild(card({
    kicker: "KEEPALIVE",
    title: "HuggingFace Space 保活",
    desc: "免费 Space 闲置会睡眠，醒来那一下要等几十秒。保活任务按间隔空跑一次把它叫醒。",
    body: [
      kv([
        ["开关", ka.enabled ? "已开启" : "已关闭"],
        ["任务状态", ka.running ? "运行中" : "未运行"],
        ["间隔", (ka.interval_minutes || 0) + " 分钟"]
      ]),
      kaUrls,
      note("保活这三个键改完需要重载插件才生效（在插件管理页禁用再启用即可）。", "warn")
    ],
    sub: true
  }));
}

/* ============================================================ 会话 */

function toggleSession(target, scope, kind, enabled) {
  if (state.busy.sessions) return;
  state.busy.sessions = true;
  renderSessions();
  apiPost("sessions/toggle", { target: target, scope: scope, kind: kind, enabled: enabled }).then(function (d) {
    state.sessions = d;
    state.busy.sessions = false;
    renderSessions();
    refreshOverview();
    toast((scope === "group" ? "群 " : "") + shorten(String(target), 28) + (enabled ? " 已开启" : " 已关闭"), "ok");
  }).catch(function (e) {
    state.busy.sessions = false;
    renderSessions();
    fail(e, "切换失败");
  });
}

function openSessionForm(scope) {
  var isGroup = scope === "group";
  var st = { kind: "tts", enabled: true };
  var target = input("", null, { mono: true, placeholder: isGroup ? "123456789" : "platform:MessageType:id" });
  var rows = [
    field({
      label: isGroup ? "群号" : "会话 ID",
      control: target,
      desc: isGroup ? "只填数字群号。" : "完整会话 ID，形如 aiocqhttp:GroupMessage:123456789，可从下面列表复制。"
    })
  ];
  if (!isGroup) {
    rows.push(field({
      label: "开关类型",
      desc: "自动配音 = 普通 TTS；W 模式 = 旁白 / 心声风格的第二套音色。",
      control: segment([{ value: "tts", label: "自动配音" }, { value: "w", label: "W 模式" }], st.kind, function (v) {
        st.kind = v;
        toast(v === "w" ? "将切换 W 模式" : "将切换自动配音", null, 1400);
      })
    }));
  }
  rows.push(field({
    label: "目标状态",
    control: segment([{ value: "on", label: "开启" }, { value: "off", label: "关闭" }], "on", function (v) { st.enabled = v === "on"; })
  }));

  formModal({
    kicker: isGroup ? "GROUP" : "SESSION",
    title: isGroup ? "手动开关某个群" : "手动开关某个会话",
    okText: "应用",
    body: rows,
    onOk: function () {
      var value = String(target.value || "").trim();
      if (!value) { toast("先填目标", "warn"); return false; }
      toggleSession(value, isGroup ? "group" : "session", st.kind, st.enabled);
      return true;
    }
  });
}

function renderSessions() {
  var v = clear(viewNode("sessions"));
  var d = state.sessions;
  if (!d) { v.appendChild(empty("会话状态还没加载", "点右上角「刷新」重试。")); return; }

  var rows = d.sessions || [];
  var groups = d.groups || { active: [], inactive: [] };
  var busy = !!state.busy.sessions;
  var ttsOn = rows.filter(function (r) { return r.tts_active; }).length;
  var wOn = rows.filter(function (r) { return r.w_active; }).length;

  v.appendChild(card({
    kicker: "SESSIONS",
    title: "会话与开关",
    desc: "这里的开关等价于在对应会话里发 /tts-llm、/tts-q、/tts-w 这些指令，改完立刻生效。",
    tools: [
      btn("指定会话", { sm: true, kind: "soft", onclick: function () { openSessionForm("session"); } }),
      btn("指定群", { sm: true, kind: "soft", onclick: function () { openSessionForm("group"); } }),
      btn("刷新", { sm: true, kind: "ghost", onclick: function () { reloadTab("sessions"); } })
    ],
    body: [
      h("div", { class: "stat-grid" }, [
        stat(String(ttsOn), "自动配音开启", ttsOn ? "ok" : null),
        stat(String(wOn), "W 模式开启", wOn ? "accent" : null),
        stat(String((groups.active || []).length), "白名单群"),
        stat(String((groups.inactive || []).length), "黑名单群")
      ]),
      kv([
        ["状态持久化", d.persistence ? "已开启（重启后保留）" : "已关闭（重启即清空）"],
        ["群聊默认", d.group_default ? "默认开启，黑名单里的群除外" : "默认关闭，只有白名单群会配音"]
      ])
    ]
  }));

  /* ---------- 会话表 ---------- */
  var t = table(["会话 ID", "自动配音", "W 模式", "音色", "W 音色", { label: "操作", class: "acts" }]);
  if (!rows.length) {
    t.body.appendChild(h("tr", {}, h("td", { colspan: 6 }, empty("暂时没有活跃会话", "在任意聊天里发一次 /tts-llm 就会出现在这里。"))));
  }
  rows.forEach(function (r) {
    var voice = r.character ? (r.character + " · " + (r.emotion || "—")) : "";
    var wVoice = r.w_character ? (r.w_character + " · " + (r.w_emotion || "—")) : "";
    var acts = h("td", { class: "acts" });
    if (r.has_last_audio) acts.appendChild(badge("有上条语音", "mute"));
    acts.appendChild(btn("复制 ID", { sm: true, kind: "ghost", onclick: function () { copyText(r.session_id, "会话 ID"); } }));
    t.body.appendChild(h("tr", {}, [
      h("td", {}, h("span", { class: "cell-path", title: r.session_id, text: shorten(r.session_id, 46) })),
      h("td", {}, switchBox(r.tts_active, r.tts_active ? "开" : "关", function (ev) {
        if (busy) { ev.target.checked = r.tts_active; return; }
        toggleSession(r.session_id, "session", "tts", !!ev.target.checked);
      })),
      h("td", {}, switchBox(r.w_active, r.w_active ? "开" : "关", function (ev) {
        if (busy) { ev.target.checked = r.w_active; return; }
        toggleSession(r.session_id, "session", "w", !!ev.target.checked);
      })),
      h("td", {}, voice ? h("span", { text: voice }) : dim("跟随默认")),
      h("td", {}, wVoice ? h("span", { text: wVoice }) : dim("跟随默认")),
      acts
    ]));
  });
  v.appendChild(card({ kicker: "LIVE", title: "活跃会话 " + rows.length, body: t, sub: true }));

  /* ---------- 群名单 ---------- */
  function groupChips(names, active) {
    var box = h("div", { class: "chips" });
    if (!names.length) { box.appendChild(dim("空")); return box; }
    names.forEach(function (n) {
      box.appendChild(h("button", {
        type: "button", class: "chip chip-btn", "aria-pressed": active ? "true" : "false",
        title: active ? "点一下移出白名单" : "点一下移出黑名单",
        text: n,
        onclick: function () { toggleSession(n, "group", "tts", !active); }
      }));
    });
    return box;
  }
  v.appendChild(card({
    kicker: "GROUPS",
    title: "群聊名单",
    desc: "白名单 = 明确开启配音的群；黑名单 = 明确关闭的群。点一下标签就翻到另一边。",
    body: [
      field({ label: "白名单（/ttg）", control: groupChips(groups.active || [], true) }),
      field({ label: "黑名单（/ttg-q）", control: groupChips(groups.inactive || [], false) })
    ],
    sub: true
  }));

  if (!d.persistence) {
    v.appendChild(note("状态持久化已关闭，AstrBot 重启后这里的开关会全部清空。想保留就去「配置 → 防护」打开 enable_state_persistence。", "warn"));
  }
}

/* ============================================================ 指令表 */

function renderCommands() {
  var v = clear(viewNode("commands"));
  var d = state.commands;
  if (!d) { v.appendChild(empty("指令表还没加载", "点右上角「刷新」重试。")); return; }

  if (!state.cmd) state.cmd = { q: "" };
  var q = String(state.cmd.q || "").trim().toLowerCase();

  var qInput = input(state.cmd.q || "", null, {
    placeholder: "搜指令 / 说明…",
    oninput: function (ev) {
      state.cmd.q = ev.target.value;
      renderCommands();
      var node = $("cmd-q");
      if (node) { node.focus(); try { node.setSelectionRange(node.value.length, node.value.length); } catch (x) {} }
    }
  });
  qInput.id = "cmd-q";
  qInput.classList.add("grow");

  v.appendChild(card({
    kicker: "COMMANDS",
    title: "指令速查",
    desc: "全部指令在聊天里直接发。方括号里的参数可以省略，省略时用配置里的默认值。",
    body: h("div", { class: "row-tight" }, [qInput, badge(d.total + " 条", "mute")])
  }));

  var shown = 0;
  (d.groups || []).forEach(function (g) {
    var items = (g.items || []).filter(function (it) {
      if (!q) return true;
      return (String(it.usage) + " " + String(it.desc) + " " + String(it.alias || "")).toLowerCase().indexOf(q) >= 0;
    });
    if (!items.length) return;
    shown += items.length;
    var t = table(["指令", "说明", { label: "", class: "acts" }]);
    items.forEach(function (it) {
      var head = String(it.usage).split(" ")[0];
      t.body.appendChild(h("tr", {}, [
        h("td", {}, h("span", { class: "cell-path", text: it.usage })),
        h("td", {}, h("span", { text: it.desc || "" })),
        h("td", { class: "acts" }, btn("复制", { sm: true, kind: "ghost", title: "复制 " + head, onclick: function () { copyText(head, "指令"); } }))
      ]));
    });
    v.appendChild(card({ kicker: String(g.group), title: g.group + " · " + items.length + " 条", body: t, sub: true }));
  });

  if (!shown) v.appendChild(empty("没有匹配的指令", "换个关键词试试。"));
}


/* ============================================================ 关于 */

var SWATCH = {
  moonlit:  ["#0a0e1a", "#2b3f80", "#8fb8ff", "#c6b0ff"],
  sakura:   ["#fff8fa", "#ffd5e2", "#e0709a", "#c184cf"],
  twilight: ["#150f1f", "#5c326e", "#f0a55e", "#e8708f"],
  aoi:      ["#0b1417", "#12595a", "#5fd3c4", "#6fa8ff"],
  usuyuki:  ["#f7f9fd", "#d5e6f8", "#4a86c8", "#74a8cd"],
  hiyo:     ["#0d0a0c", "#6d1a32", "#e8556f", "#f0955f"]
};

var ENDPOINTS = [
  ["总览", "GET", "overview", "统计、限额、主题清单"],
  ["感情", "GET", "emotions", "角色与感情全表"],
  ["感情", "POST", "emotions/upsert", "新增或改写一条感情"],
  ["感情", "POST", "emotions/delete", "批量删除感情"],
  ["感情", "POST", "emotions/copy", "复制 / 移动到别的角色"],
  ["感情", "POST", "emotions/rename-character", "重命名角色"],
  ["感情包", "GET", "emotions/export", "下载感情包 JSON"],
  ["感情包", "POST", "emotions/export-preview", "导出前先看文本"],
  ["感情包", "POST", "emotions/import", "导入感情包（支持试运行）"],
  ["感情包", "GET", "packs", "服务端快照列表"],
  ["感情包", "POST", "packs/save", "保存一份快照"],
  ["感情包", "POST", "packs/delete", "删除快照"],
  ["感情包", "POST", "packs/restore", "从快照恢复"],
  ["感情包", "GET", "packs/download", "下载快照文件"],
  ["合成", "POST", "synthesize", "工作台试听合成"],
  ["合成", "POST", "preview", "分段与停顿预览"],
  ["服务器", "GET", "servers", "探测所有 TTS 服务器"],
  ["配置", "GET", "config", "分组后的配置表"],
  ["配置", "POST", "config/save", "写回配置并落盘"],
  ["会话", "GET", "sessions", "活跃会话与群名单"],
  ["会话", "POST", "sessions/toggle", "切换会话 / 群开关"],
  ["其它", "GET", "commands", "指令速查表"],
  ["其它", "GET", "prefs", "读取界面偏好"],
  ["其它", "POST", "prefs/save", "保存界面偏好"]
];

function themeGallery() {
  var box = h("div", { class: "gallery" });
  themeList().forEach(function (t) {
    var sw = h("div", { class: "gal-swatch" });
    var colors = SWATCH[t.id] || SWATCH.moonlit;
    colors.forEach(function (c) {
      var i = h("i");
      i.style.background = c;
      sw.appendChild(i);
    });
    box.appendChild(h("button", {
      type: "button",
      class: "gal",
      "aria-pressed": state.prefs.theme === t.id ? "true" : "false",
      onclick: function () { applyTheme(t.id, true); toast("主题已切到「" + t.name + "」", "ok", 1800); }
    }, [
      sw,
      h("div", { class: "gal-name", text: t.name + (t.dark ? " · 暗" : " · 亮") }),
      h("div", { class: "gal-hint", text: t.hint || "" })
    ]));
  });
  return box;
}

function renderAbout() {
  var v = clear(viewNode("about"));
  var o = state.overview;
  if (!o) { v.appendChild(empty("还在连 AstrBot", "稍等一下，或点右上角刷新。")); return; }

  var p = o.plugin || {};
  var c = o.counts || {};
  var lim = o.limits || {};
  var tg = o.toggles || {};
  var df = o.defaults || {};
  var ss = o.session || {};

  /* ---------- Hero ---------- */
  var chips = h("div", { class: "hero-chips" }, [
    chip("v" + (p.version || "?"), "chip-accent"),
    chip(c.endpoints + " 个接口", "chip-mono"),
    chip(c.themes + " 套主题"),
    chip(c.commands + " 条指令")
  ]);
  var hero = h("div", { class: "hero" }, [
    h("div", { class: "hero-copy" }, [
      h("span", { class: "hero-slug", text: p.name || "astrbot_plugin_genie_tts_llm" }),
      h("h1", { class: "hero-title" }, [
        h("span", { text: p.display_name || "Genie TTS LLM" }),
        h("small", { text: "  语音合成工作台" })
      ]),
      h("p", { class: "hero-line", text: "把 Genie TTS 接到 AstrBot：登记参考音频、让 LLM 自己挑感情、按句切块补停顿，再把成品语音发回聊天。这个页面是它的可视化控制台。" })
    ]),
    chips
  ]);
  var repo = String(p.repo || "");
  var repoRow = h("div", { class: "row-tight" }, [
    h("a", { class: "chip chip-link", href: repo, target: "_blank", rel: "noreferrer noopener", text: "GitHub 仓库" }),
    btn("复制仓库地址", { sm: true, kind: "ghost", onclick: function () { copyText(repo, "仓库地址"); } }),
    dim(repo)
  ]);
  v.appendChild(card({ body: [hero, rule(), repoRow] }));

  /* ---------- 主题画廊 ---------- */
  v.appendChild(card({
    kicker: "THEMES",
    title: "主题",
    desc: "六套 galgame 风格配色，选中会记在服务器上，下次打开还是它。密度开关在右上角。",
    tools: [segment(
      [{ value: "comfortable", label: "宽松" }, { value: "compact", label: "紧凑" }],
      state.prefs.density,
      function (val) { applyDensity(val, true); renderAbout(); }
    )],
    body: themeGallery()
  }));

  /* ---------- 三列信息 ---------- */
  function onoff(flag) { return flag ? "开" : "关"; }
  var limitsCard = card({
    kicker: "LIMITS",
    title: "限额与默认值",
    body: kv([
      ["自动配音字数上限", lim.max_text_length + " 字"],
      ["工作台单次上限", lim.synth_text_limit + " 字"],
      ["单次超时", lim.timeout_seconds + " 秒"],
      ["失败重试", lim.max_retries + " 次"],
      ["块间停顿", lim.chunk_gap_ms + " ms（最大 " + lim.max_chunk_gap_ms + "）"],
      ["自定义停顿上限", lim.max_custom_pause_ms + " ms"],
      ["每块句数", String(lim.sentences_per_chunk)],
      ["采样率", lim.sample_rate + " Hz"]
    ]),
    sub: true
  });
  var defaultsCard = card({
    kicker: "DEFAULTS",
    title: "默认音色与开关",
    body: kv([
      ["默认角色", df.character || "未设置"],
      ["默认感情", df.emotion || "未设置"],
      ["默认语言", df.language || "jp"],
      ["触发方式", df.trigger_mode || "always"],
      ["自动配音输出", df.auto_output_mode || "—"],
      ["工具调用输出", df.tool_output_mode || "—"],
      ["分句切块 / 自定义停顿", onoff(tg.sentence_splitting) + " / " + onoff(tg.custom_pause_marker)],
      ["翻译 / 文本清洗", onoff(tg.translation) + " / " + onoff(tg.text_cleaning)],
      ["泄漏拦截 / 失败提示", onoff(tg.leak_guard) + " / " + onoff(tg.failure_notice)]
    ]),
    sub: true
  });
  var sessionCard = card({
    kicker: "SESSION",
    title: "当前会话规模",
    body: kv([
      ["自动配音会话", String(ss.active_sessions || 0)],
      ["W 模式会话", String(ss.w_active_sessions || 0)],
      ["白名单群", String(ss.active_groups || 0)],
      ["黑名单群", String(ss.inactive_groups || 0)],
      ["记住音色的会话", String(ss.session_emotions || 0)],
      ["状态持久化", onoff(tg.state_persistence)],
      ["群聊默认开启", onoff(tg.group_default)],
      ["Space 保活", onoff(tg.keepalive)]
    ]),
    sub: true
  });
  v.appendChild(h("div", { class: "grid-3" }, [limitsCard, defaultsCard, sessionCard]));

  /* ---------- 接口清单 ---------- */
  var t = table(["分类", "方法", "接口", "用途"]);
  ENDPOINTS.forEach(function (row) {
    t.body.appendChild(h("tr", {}, [
      h("td", {}, chip(row[0])),
      h("td", {}, h("span", { class: "cell-path", text: row[1] })),
      h("td", {}, h("span", { class: "cell-path", text: row[2] })),
      h("td", {}, h("span", { text: row[3] }))
    ]));
  });
  v.appendChild(card({
    kicker: "API",
    title: "接口清单 · " + ENDPOINTS.length + " 条",
    desc: "全部挂在 /api/plug/astrbot_plugin_genie_tts_llm/ 下，沿用 AstrBot 自己的鉴权，未登录访问不到。",
    body: t,
    sub: true
  }));

  /* ---------- 安全须知 ---------- */
  v.appendChild(card({
    kicker: "SECURITY",
    title: "安全须知",
    body: [
      note("这个页面跑在 sandbox iframe 里，没有 same-origin 权限：所有请求都经父窗口的桥转发，读不到 Dashboard 的 cookie，也没法直连外部地址。", "ok"),
      note("配置里的密钥（api_key / token 之类）读出来永远是掩码，不会明文回传到浏览器。想改就直接填新值，留空表示不动。", "ok"),
      note("参考音频路径只允许相对路径且不能含 ..，避免读到 Space 工作目录外的文件。导入感情包时同样会逐条校验，不合法的会被记成 invalid。", "warn"),
      note("「感情包 → 导入」的 replace 模式会先清空整个感情库再写入，务必先勾「试运行」看清报告。旁边的「保存当前」随时能存一份快照兜底。", "danger")
    ],
    sub: true
  }));
}

/* ============================================================ 启动 */

if (D.readyState === "loading") D.addEventListener("DOMContentLoaded", init);
else init();

})();
