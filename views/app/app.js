import {mountMemory} from "./memory.js";
import {openDiagnostics, disposeDiagnostics} from "./diagnostics.js";
import {openBackup, disposeBackup} from "./backup.js";

const bridge = window.AstrBotPluginView;
const main = document.getElementById("main");
const dialog = document.getElementById("dialog");
const content = document.getElementById("dialog-content");
const fileInput = document.getElementById("file-input");
new ResizeObserver(([entry]) => {
  document.documentElement.style.setProperty("--header-height", `${entry.target.getBoundingClientRect().height}px`);
}).observe(document.querySelector(".app-header"));
const toolsObserver = new ResizeObserver(() => {
  const toolbar = main.querySelector(".sticky-reading-toolbar, :scope > .page-tools > .toolbar, :scope > .page-tools > .subhead, :scope > .toolbar, :scope > .subhead");
  document.documentElement.style.setProperty("--page-tools-height", `${toolbar?.getBoundingClientRect().height || 0}px`);
});
dialog.addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  const controls = [
    ...dialog.querySelectorAll(
      'button, input, select, textarea, a[href], summary, [tabindex]',
    ),
  ].filter(
    (el) => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length,
  );
  if (!controls.length) return;
  const index = controls.indexOf(document.activeElement);
  if (event.shiftKey && index <= 0) {
    event.preventDefault();
    controls[controls.length - 1].focus();
  } else if (!event.shiftKey && (index < 0 || index === controls.length - 1)) {
    event.preventDefault();
    controls[0].focus();
  }
});
const state = {
  tab: "album",
  data: null,
  character: "",
  category: "all",
  query: "",
  offset: 0,
  assets: [],
  serial: 0,
};
const kindNames = {
  generated: "已生成",
  imported: "已导入",
  scene: "临时场景参考",
  reference: "角色参考",
};
const selectedPhotos = new Map();
const selectedPlans = new Map();
let planSortMenuEvents;
let albumPanelEvents;
let settingsSession;
let qqSettingsSession;
let assistantSettingsSession;
let settingsWriteBusy = false;
window.addEventListener("beforeunload", event => {
  if (settingsWriteBusy || [settingsSession, assistantSettingsSession, qqSettingsSession].some(session => session && (session.saving || session.dirty()))) { event.preventDefault(); event.returnValue = ""; }
});
const selectedHistory = new Map();
const historyState = {kind: "jobs", status: "", character: "", query: "", offset: 0, manage: false, page: null};
const historyNames = {jobs: "生图任务", deliveries: "照片投递", runs: "朝夕足迹", memory: "拾忆日志"};
const icons = {
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  hidden: '<path d="m3 3 18 18M10.6 5.1A12 12 0 0 1 12 5c6.5 0 10 7 10 7a19 19 0 0 1-3 4M6.5 6.5A22 22 0 0 0 2 12s3.5 7 10 7a12 12 0 0 0 5.5-1.5"/>',
  key: '<circle cx="8" cy="8" r="5"/><path d="m11.5 11.5 9 9M17 17l3-3M14 14l3-3"/>',
  edit: '<path d="m15 5 4 4M4 20l4-1L20 7a3 3 0 0 0-4-4L4 15v5Z"/>',
  remove: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 .5c0 1.5-2.5 2-2.5 3M12 17h.01"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  camera: '<path d="M3 7h4l2-3h6l2 3h4v13H3Z"/><circle cx="12" cy="13" r="4"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 11h18M8 15h2M14 15h2"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3"/>',
  moon: '<path d="M20.5 13A8.5 8.5 0 0 1 11 3.5 8.5 8.5 0 1 0 20.5 13Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${icons[name] || ""}</svg>`;
const photoIcon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${name === "heart" ? '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>' : name === "pencil" ? '<path d="m16 3 5 5L8 21H3v-5ZM14 5l5 5"/>' : '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>'}</svg>`;
const statusNames = {
  queued: "排队中",
  submitting: "正在提交",
  running: "生成中",
  ready: "已入相册",
  downloading: "已生成，下载中",
  download_failed: "已生成，下载未完成",
  failed: "失败",
  unknown: "待核对",
  cancelled: "已停止",
};
const recordStatus = {...statusNames, sent: "已发送", sending: "发送中", held: "已保留", skipped: "已跳过", expired: "过期未送达"};
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const date = (ts) =>
  new Date(ts * 1000).toLocaleString("zh-CN", {
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
const chars = () => state.data?.characters || [];
const charName = (id) => chars().find((c) => c.id === id)?.name || "未命名角色";
const options = (current, all = false) =>
  (all
    ? '<option value="">全部角色</option>'
    : '<option value="">选择角色</option>') +
  chars()
    .map(
      (c) =>
        `<option value="${escape(c.id)}" ${current === c.id ? "selected" : ""}>${escape(c.name)}</option>`,
    )
    .join("");
let toastTimer;
let hintSerial = 0;
function helpTip(label, text, iconName = "help") {
  const id = `hint-${++hintSerial}`;
  return `<span class="hint"><button type="button" class="hint-button" aria-label="${escape(label)}" aria-describedby="${id}">${icon(iconName)}</button><span class="hint-tip" role="tooltip" id="${id}">${escape(text)}</span></span>`;
}
function placeHint(hint) {
  const tip = hint.querySelector('.hint-tip'), rect = hint.getBoundingClientRect();
  tip.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - tip.offsetWidth - 12))}px`;
  tip.style.top = `${Math.max(12, Math.min(rect.bottom + 6, window.innerHeight - tip.offsetHeight - 12))}px`;
}
for (const eventName of ['pointerover', 'focusin']) document.addEventListener(eventName, e => {
  const hint = e.target.closest?.('.hint');
  if (hint && !hint.contains(e.relatedTarget)) { hint.removeAttribute('data-dismissed'); placeHint(hint); }
});
document.addEventListener('click', e => {
  const button = e.target.closest?.('.hint-button'), hint = button?.closest('.hint');
  document.querySelectorAll('.hint').forEach(el => { if (el !== hint) { el.removeAttribute('data-open'); el.setAttribute('data-dismissed', ''); } });
  if (hint) { const wasOpen = hint.hasAttribute('data-open'); hint.toggleAttribute('data-open', !wasOpen); hint.toggleAttribute('data-dismissed', wasOpen); placeHint(hint); }
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const hints = [...document.querySelectorAll('.hint')].filter(el => !el.hasAttribute('data-dismissed') && (el.hasAttribute('data-open') || el.matches(':hover, :focus-within')));
  if (!hints.length) return;
  e.preventDefault(); e.stopPropagation();
  hints.forEach(el => { el.removeAttribute('data-open'); el.setAttribute('data-dismissed', ''); });
}, true);
function toast(text, error = false) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.className = "visible" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = ""), 6000);
}
async function act(action, payload) {
  if (!["settings", "save_provider", "qq_settings"].includes(action)) return bridge.apiPost("action/" + action, payload);
  if (settingsWriteBusy) throw new Error("设置正在保存，请稍后重试");
  settingsWriteBusy = true;
  try { return await bridge.apiPost("action/" + action, payload); }
  finally { settingsWriteBusy = false; }
}
async function refreshData() {
  state.data = await bridge.apiGet("state");
  const size = main.querySelector("#album-storage-size");
  if (size) size.textContent = (state.data.storage_bytes / 1024 / 1024).toFixed(1);
}
let dialogPagePosition = null;
dialog.addEventListener("close", () => {
  if (dialog.open || !dialogPagePosition) return;
  const position = dialogPagePosition;
  dialogPagePosition = null;
  window.scrollTo({...position, behavior: "instant"});
});
function showDialog(title, body, foot = "") {
  disposeDiagnostics();
  if (!dialog.open) dialogPagePosition = {left: window.scrollX, top: window.scrollY};
  delete content.dataset.assetId;
  content.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">${escape(title)}</h2><button class="close" aria-label="关闭" data-close>×</button></div>${body}${foot}`;
  if (!dialog.open) {
    dialog.showModal();
    window.scrollTo({...dialogPagePosition, behavior: "instant"});
  }
  content
    .querySelectorAll("[data-close]")
    .forEach((btn) => (btn.onclick = () => dialog.close()));
}
function footer(buttons) {
  return `<div class="dialog-foot">${buttons}</div>`;
}
function exportDiagnostics(initial = {}) {
  return openDiagnostics({bridge, dialog, content, showDialog, footer, escape}, initial);
}
async function busy(button, fn) {
  const original = button.textContent;
  const showLabel = !button.querySelector("svg");
  button.disabled = true; button.setAttribute("aria-busy", "true");
  if (showLabel) button.textContent = "处理中…";
  try {
    await fn();
  } catch (e) {
    toast(e.message || "操作失败", true);
  } finally {
    button.disabled = false; button.removeAttribute("aria-busy");
    if (showLabel && button.textContent === "处理中…") button.textContent = original;
  }
}
async function thumbs(root) {
  const nodes = [...root.querySelectorAll("img[data-asset]")];
  let next = 0;
  const serial = state.serial;
  await Promise.all(
    [0, 1, 2].map(async () => {
      while (next < nodes.length) {
        const el = nodes[next++];
        if (!el.isConnected || serial !== state.serial) return;
        try {
          const r = await bridge.apiGet("preview", { id: el.dataset.asset });
          if (el.isConnected) el.src = r.data_url;
        } catch {
          el.alt = "图片暂时无法加载";
        }
      }
    }),
  );
}
async function render() {
  if (main.dataset.page !== state.tab && content.querySelector("#diagnostic-form")) {
    disposeDiagnostics();
    dialog.close();
  }
  settingsSession?.dispose(); settingsSession = null;
  qqSettingsSession = null;
  assistantSettingsSession?.dispose();
  disposeBackup();
  const serial = ++state.serial;
  planSortMenuEvents?.abort();
  albumPanelEvents?.abort();
  toolsObserver.disconnect();
  main.dataset.page = state.tab;
  document.getElementById("mobile-app-select").value = state.tab;
  main.setAttribute("aria-busy", "true");
  document.querySelectorAll("[data-tab]").forEach((b) => {
    if (b.dataset.tab === state.tab) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  try {
    if (state.tab === "album") await renderAlbum(serial);
    if (state.tab === "characters") renderCharacters();
    if (state.tab === "jobs") await renderJobs(serial);
    if (state.tab === "plans") await renderPlans();
    if (state.tab === "settings") renderSettings();
    if (state.tab === "memory") await mountMemory({main, bridge, showDialog, footer, busy, toast, escape, helpTip, serial, openAssistantSettings: openMemoryAssistantSettings, openDiagnostics: target => {historyState.kind="memory"; historyState.scope=target.scope; historyState.cid=target.cid; historyState.status="failed"; historyState.query=""; historyState.offset=0; state.tab="jobs"; return render();}, isCurrent: () => state.tab === "memory" && serial === state.serial});
  } catch (e) {
    if (serial === state.serial) {
      main.innerHTML = `<div class="empty"><h2>暂时无法打开</h2><p>${escape(e.message)}</p><button id="retry">重新加载</button></div>`;
      main.querySelector("#retry").onclick = () => reload();
    }
  } finally {
    const toolbar = main.querySelector(".sticky-reading-toolbar, :scope > .page-tools > .toolbar, :scope > .page-tools > .subhead, :scope > .toolbar, :scope > .subhead");
    document.documentElement.style.setProperty("--page-tools-height", `${toolbar?.getBoundingClientRect().height || 0}px`);
    toolsObserver.observe(main);
    main.setAttribute("aria-busy", "false");
  }
}
async function reload() {
  await refreshData();
  await render();
}
function nav(tab) {
  document.getElementById("mobile-app-select").value = state.tab;
  if (state.tab === tab) return Promise.resolve(true);
  if (settingsWriteBusy || settingsSession?.saving || assistantSettingsSession?.saving || (settingsSession?.dirty() || assistantSettingsSession?.dirty() || qqSettingsSession?.dirty()) && !window.confirm("有未保存的设置，离开后将放弃这些修改。继续离开？")) {
    return false;
  }
  selectedPhotos.clear(); state.albumManage = false;
  selectedPlans.clear(); state.planManage = false;
  selectedHistory.clear(); historyState.manage = false;
  state.tab = tab;
  const rendered = render();
  window.scrollTo(0, 0);
  return rendered.then(() => true);
}
document
  .querySelectorAll("[data-tab]")
  .forEach((b) => (b.onclick = () => nav(b.dataset.tab)));
document.getElementById("mobile-app-select").onchange = (event) => nav(event.target.value);

async function renderAlbum(serial, nextFilter = null) {
  if (state.tab !== "album") return;
  const filterState = {...state, ...nextFilter};
  const result = await bridge.apiGet("assets", {
    character_id: filterState.character,
    category: filterState.category,
    query: filterState.query,
    offset: filterState.offset,
    start_date: filterState.startDate || "", end_date: filterState.endDate || "",
  });
  if (serial !== state.serial || state.tab !== "album") return;
  if (!result.items.length && result.total && filterState.offset >= result.total) {
    return renderAlbum(serial, {...nextFilter, offset: Math.floor((result.total - 1) / 24) * 24});
  }
  if (nextFilter) { Object.assign(state, nextFilter); selectedPhotos.clear(); }
  albumPanelEvents?.abort();
  albumPanelEvents = new AbortController();
  state.assets = result.items;
  const filtered = Boolean(state.query || state.character || state.category !== "all" || state.startDate || state.endDate);
  let gallery = "";
  let day = "";
  for (const a of result.items) {
    const d = new Date(a.created * 1000).toLocaleDateString("zh-CN", {
      year: "numeric",
      month: "long",
      day: "numeric",
    });
    if (day !== d) {
      if (day) gallery += "</div>";
      gallery += `<h2 class="date-heading">${escape(d)}</h2><div class="photos">`;
      day = d;
    }
    gallery += `<article class="photo ${selectedPhotos.has(a.id) ? "selected" : ""}"><button class="photo-open" data-detail="${a.id}" aria-label="${state.albumManage ? "选择" : "查看"} ${escape(a.description || "未命名照片")}" ${state.albumManage ? `aria-pressed="${selectedPhotos.has(a.id)}"` : ""}><img data-asset="${a.id}" alt="${escape(a.description || "相册照片")}" loading="lazy"><div class="caption"><p>${escape(a.description || "未命名照片")}</p><small>${escape(charName(a.character_id))} · ${kindNames[a.kind]}</small></div></button>${state.albumManage ? `<label class="photo-select"><input type="checkbox" data-select-photo="${a.id}" aria-label="选择照片 ${escape(a.description || a.id)}" ${selectedPhotos.has(a.id) ? "checked" : ""}></label>` : `<div class="photo-actions">${["generated", "imported"].includes(a.kind) ? `<button class="icon-button favorite-button" data-favorite="${a.id}" aria-label="${a.favorite ? "取消收藏" : "收藏照片"}" aria-pressed="${a.favorite}" title="${a.favorite ? "取消收藏" : "收藏"}">${photoIcon("heart")}</button>` : ""}${a.trash ? `<button data-restore="${a.id}">恢复</button>` : `<button class="icon-button danger" data-trash-photo="${a.id}" aria-label="删除照片" title="移到回收站">${photoIcon("trash")}</button>`}</div>${a.favorite ? '<span class="favorite-mark" aria-label="已收藏">♥</span>' : ""}`}</article>`;
  }
  if (day) gallery += "</div>";
  const categories = Object.entries({all: "全部照片", favorite: "收藏", generated: "已生成", imported: "已导入", scene: "临时场景参考", trash: "回收站"}).map(([k,v]) => `<button data-category="${k}" class="${state.category === k ? "active" : ""}" aria-pressed="${state.category === k}">${v}</button>`).join("");
  const batch = `<div class="album-batch"><span id="selection-count" role="status"></span><div class="actions"><button id="select-page">全选本页</button><button id="clear-selection">取消选择</button><button id="batch-favorite" aria-label="收藏所选照片" title="收藏所选" class="icon-button favorite-button">${photoIcon("heart")}</button><button id="batch-unfavorite">取消收藏</button><button id="batch-trash" aria-label="删除所选照片" title="移到回收站" class="icon-button danger">${photoIcon("trash")}</button>${state.category === "trash" ? '<button id="batch-restore">恢复所选</button>' : ""}<button id="manage-album">完成管理</button></div></div>`;
  main.innerHTML = `<h2 class="sr-only">相册</h2><div class="page-tools album-tools"><div class="toolbar album-toolbar"><select id="album-character" aria-label="查看角色">${options(state.character, true)}</select>${state.albumManage ? batch : `<div class="actions"><button id="album-search-toggle" class="icon-button ${state.query ? 'has-filter' : ''}" aria-label="搜索照片" title="搜索照片" aria-expanded="false" aria-controls="album-filter-panel">${icon("search")}</button><button id="album-date-toggle" class="icon-button ${state.startDate || state.endDate ? 'has-filter' : ''}" aria-label="按日期筛选" title="按日期筛选" aria-expanded="false" aria-controls="album-filter-panel">${icon("calendar")}</button><button id="import"><span class="desktop-label">导入照片</span><span class="mobile-label">导入</span></button><button id="generate" class="primary"><span class="desktop-label">生成照片</span><span class="mobile-label">生成</span></button><button id="album-more-toggle" class="icon-button" aria-label="更多相册操作" title="更多操作" aria-expanded="false" aria-controls="album-filter-panel">···</button></div><div id="album-filter-panel" class="album-filter-panel" hidden></div>`}</div></div><div class="categories album-categories" role="group" aria-label="照片分类">${categories}</div><div class="album-active-filters" ${state.query || state.startDate || state.endDate ? '' : 'hidden'}>${state.query ? `<button data-clear-filter="search" class="filter-chip" aria-label="清除搜索：${escape(state.query)}">${icon("search")}${escape(state.query)}<span aria-hidden="true">×</span></button>` : ''}${state.startDate || state.endDate ? `<button data-clear-filter="date" class="filter-chip" aria-label="清除日期筛选">${icon("calendar")}${escape(state.startDate || "不限")} — ${escape(state.endDate || "不限")}<span aria-hidden="true">×</span></button>` : ''}<p class="album-filter-error" role="alert" hidden></p></div>${gallery || `<div class="empty"><div class="empty-icon" aria-hidden="true">▧</div><h2>${chars().length ? filtered ? "无匹配照片" : "暂无照片" : "先创建一个角色"}</h2>${!filtered && chars().length ? "<p>导入或生成照片</p>" : ""}<div class="actions"><button id="empty-action" class="primary">${!chars().length ? "设置角色参考" : filtered ? "重置筛选" : "导入照片"}</button></div></div>`}<div class="pagination"><button id="prev" ${state.offset === 0 ? "disabled" : ""}>上一页</button><span>${result.total} 张照片 · ${Math.floor(state.offset / 24) + 1} 页</span><button id="next" ${state.offset + 24 >= result.total ? "disabled" : ""}>下一页</button></div>`;
  main.querySelector("#album-character").onchange = (e) => {
    selectedPhotos.clear();
    state.character = e.target.value;
    state.offset = 0;
    render();
  };
  const manageAlbum = () => { state.albumManage = !state.albumManage; selectedPhotos.clear(); render(); };
  const panel = main.querySelector("#album-filter-panel");
  let panelTrigger = null;
  const closePanel = (restore = false) => {
    if (!panel) return;
    panel.hidden = true;
    panelTrigger?.setAttribute("aria-expanded", "false");
    if (restore) panelTrigger?.focus({preventScroll: true});
    panelTrigger = null;
  };
  const applyFilter = async (patch, button, error) => {
    if (state.tab !== "album") return;
    const requestSerial = ++state.serial;
    button.disabled = true; button.setAttribute("aria-busy", "true"); error.hidden = true;
    try { await renderAlbum(requestSerial, {...patch, offset: 0}); }
    catch (e) {
      if (requestSerial === state.serial && error.isConnected) { error.textContent = e.message || "筛选暂时不可用"; error.hidden = false; }
    } finally { if (button.isConnected) { button.disabled = false; button.removeAttribute("aria-busy"); } }
  };
  for (const kind of ["search", "date", "more"]) {
    const trigger = main.querySelector(`#album-${kind}-toggle`);
    if (!trigger) continue;
    trigger.onclick = () => {
      if (panelTrigger === trigger) return closePanel(true);
      closePanel(); panelTrigger = trigger; trigger.setAttribute("aria-expanded", "true"); panel.hidden = false;
      panel.innerHTML = kind === "more" ? '<button id="manage-album">管理相册</button>' : `<form class="album-filter-form">${kind === "search" ? `<label for="search">搜索照片</label><input id="search" type="search" value="${escape(state.query)}" placeholder="照片内容或关键词">` : `<div class="form-row"><div><label for="album-from">开始日期</label><input id="album-from" type="date" value="${state.startDate || ''}"></div><div><label for="album-to">结束日期</label><input id="album-to" type="date" value="${state.endDate || ''}"></div></div>`}<p class="album-panel-error" role="alert" hidden></p><div class="actions"><button type="button" data-panel-close>取消</button><button type="submit" class="primary">${kind === "search" ? "搜索" : "应用日期"}</button></div></form>`;
      if (kind === "more") { panel.querySelector("#manage-album").onclick = manageAlbum; return; }
      const form = panel.querySelector("form"), error = panel.querySelector('[role="alert"]');
      form.querySelector('[data-panel-close]').onclick = () => closePanel(true);
      form.onsubmit = async (event) => {
        event.preventDefault(); if (!form.reportValidity()) return;
        const from = form.querySelector("#album-from"), to = form.querySelector("#album-to");
        if (from?.value && to?.value && from.value > to.value) { error.textContent = "结束日期不能早于开始日期"; error.hidden = false; to.focus(); return; }
        await applyFilter(kind === "search" ? {query: form.querySelector("#search").value.trim()} : {startDate: from.value, endDate: to.value}, form.querySelector('[type="submit"]'), error);
      };
      form.querySelector("input").focus({preventScroll: true});
    };
  }
  document.addEventListener("click", (event) => {
    if (panelTrigger && !panel.contains(event.target) && !event.target.closest('[id^="album-"][id$="-toggle"]')) closePanel();
  }, {signal: albumPanelEvents.signal});
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && panelTrigger && !event.defaultPrevented) { event.preventDefault(); closePanel(true); }
  }, {signal: albumPanelEvents.signal});
  main.querySelectorAll("[data-clear-filter]").forEach(button => {
    button.onclick = () => applyFilter(button.dataset.clearFilter === "search" ? {query: ""} : {startDate: "", endDate: ""}, button, main.querySelector(".album-filter-error"));
  });
  main.querySelectorAll("[data-category]").forEach(
    (b) =>
      (b.onclick = () => {
        selectedPhotos.clear();
        state.category = b.dataset.category;
        state.offset = 0;
        render();
      }),
  );
  main
    .querySelectorAll("[data-detail]")
    .forEach(
      (b) =>
        (b.onclick = () => {
          const a = result.items.find((a) => a.id === b.dataset.detail);
          if (state.albumManage) toggleSelection(a); else openDetail(a);
        }),
    );
  const updateSelection = () => {
    main.querySelectorAll("[data-select-photo]").forEach((input) => { input.checked = selectedPhotos.has(input.dataset.selectPhoto); input.closest(".photo").classList.toggle("selected", input.checked); input.closest(".photo").querySelector(".photo-open").setAttribute("aria-pressed", String(input.checked)); });
    const counter = main.querySelector("#selection-count");
    if (counter) counter.textContent = `已选 ${selectedPhotos.size} 张`;
    const all = main.querySelector('#select-page');
    if (all) { const checked = result.items.length > 0 && result.items.every(a => selectedPhotos.has(a.id)); all.textContent = checked ? '取消全选本页' : '全选本页'; all.setAttribute('aria-pressed', String(checked)); all.disabled = !result.items.length; }
    main.querySelectorAll('[id^="batch-"]').forEach((button) => button.disabled = !selectedPhotos.size);
  };
  const toggleSelection = (a) => {
    if (selectedPhotos.has(a.id)) selectedPhotos.delete(a.id);
    else if (selectedPhotos.size < 100) selectedPhotos.set(a.id, a);
    else return toast("一次最多选择100张照片", true);
    updateSelection();
  };
  const applyPhotos = async (photos, patch) => {
    await act("assets", {patches: photos.map((a) => ({id: a.id, revision: a.revision, ...patch}))});
    selectedPhotos.clear(); await refreshData(); await render();
    toast(patch.trash ? "已移到回收站，可以恢复" : "照片已更新");
  };
  if (state.albumManage) main.querySelector("#manage-album").onclick = manageAlbum;
  main.querySelectorAll("[data-select-photo]").forEach((input) => input.onchange = () => toggleSelection(result.items.find((a) => a.id === input.dataset.selectPhoto)));
  for (const [attribute, patch] of [["favorite", null], ["trash-photo", {trash:true}], ["restore", {trash:false}]]) {
    main.querySelectorAll(`[data-${attribute}]`).forEach((button) => button.onclick = () => busy(button, () => {
      const a = result.items.find((a) => a.id === button.getAttribute(`data-${attribute}`));
      return applyPhotos([a], patch || {favorite:!a.favorite});
    }));
  }
  if (state.albumManage) {
    main.querySelector("#select-page").onclick = () => {
      const clear = result.items.every(a => selectedPhotos.has(a.id));
      result.items.forEach(a => { if (clear) selectedPhotos.delete(a.id); else if (selectedPhotos.size < 100) selectedPhotos.set(a.id, a); });
      updateSelection(); if (!clear && !result.items.every(a => selectedPhotos.has(a.id))) toast('最多选择100张，请先处理已选照片');
    };
    main.querySelector("#clear-selection").onclick = () => { selectedPhotos.clear(); updateSelection(); };
    for (const [id, patch] of [["favorite",{favorite:true}], ["unfavorite",{favorite:false}], ["trash",{trash:true}], ["restore",{trash:false}]]) {
      const button = main.querySelector("#batch-" + id);
      if (button) button.onclick = () => busy(button, () => applyPhotos([...selectedPhotos.values()], patch));
    }
    updateSelection();
  }
  main.querySelector("#prev").onclick = () => {
    state.offset = Math.max(0, state.offset - 24);
    render();
  };
  main.querySelector("#next").onclick = () => {
    state.offset += 24;
    render();
  };
  if (!state.albumManage) {
    main.querySelector("#import").onclick = openImport;
    main.querySelector("#generate").onclick = openGenerate;
  }
  const empty = main.querySelector("#empty-action");
  if (empty)
    empty.onclick = () => {
      if (!chars().length) return nav("characters");
      if (!filtered) return openImport();
      state.character = ""; state.category = "all"; state.query = "";
      state.startDate = ""; state.endDate = ""; state.offset = 0;
      selectedPhotos.clear(); render();
    };
  thumbs(main);
}

async function openDetail(a) {
  const serial = state.serial;
  try { a = await bridge.apiGet("asset/" + a.id); }
  catch (error) { toast(error.message, true); return; }
  if (serial !== state.serial) return;
  const deliveries = state.data.deliveries.filter((d) => d.asset_id === a.id || d.asset_ids?.includes(a.id));
  const deliveryLabel = (d) => `${d.target === "qzone" ? "空间" : "聊天"} · ${{sent: "已发送", unknown: "待核对", failed: "发送失败", sending: "发送中"}[d.state] || escape(d.state)}`;
  const latest = deliveries.filter((d, i) => deliveries.findIndex((other) => other.target === d.target) === i);
  showDialog(
    "照片详情",
    `<img id="detail-image" class="detail-image" alt="${escape(a.description || "相册照片")}"><form id="asset-form" class="dialog-body"><div class="detail-meta">${escape(charName(a.character_id))} · ${date(a.created)} · ${a.width} × ${a.height}</div>${latest.length ? `<p class="help">最近投递：${latest.map(deliveryLabel).join("；")}</p>` : ""}<details><summary>照片信息与投递记录</summary><p class="help">照片编号：${escape(a.id)}${a.provenance?.reference_version ? `<br>参考版本：${escape(a.provenance.reference_version)}` : ""}</p>${deliveries.length ? `<ul>${deliveries.map((d) => `<li>${d.created ? date(d.created) + " · " : ""}${deliveryLabel(d)}</li>`).join("")}</ul>` : '<p class="help">暂无投递记录</p>'}</details><label for="description">画面描述</label><textarea id="description" name="description" maxlength="1000">${escape(a.description)}</textarea>${a.generation ? `<details class="generation-detail"><summary>生成详情与参考图</summary><p class="help">${escape(a.generation.site || "原生成站点未记录")} · ${escape(a.generation.model || "模型未记录")}</p><h3>实际生成提示词</h3><p class="preserve-text">${escape(a.generation.prompt)}</p><h3>本次使用的参考图</h3><div class="ref-grid">${a.generation.references.map((ref) => `<div class="generation-ref">${ref.expired ? '<div class="reference-unavailable">参考图已清理</div>' : `<img data-asset="${ref.id}" alt="${escape(ref.description || kindNames[ref.kind] || "生成参考")}">`}<p class="help">${escape(kindNames[ref.kind] || "参考图")} · ${escape(ref.description || "未填写说明")}</p></div>`).join("") || '<p class="help">此任务未保存参考图记录。</p>'}</div></details>` : a.kind === "generated" ? '<p class="help">生成详情不可用</p>' : ""}<p class="help">${["scene","reference"].includes(a.kind) ? "仅供参考，不直接发送" : "可用于聊天与空间"}${a.expired ? " · 文件已清理，无法恢复" : a.trash ? " · 清理后无法恢复" : ""}</p></form>`,
    footer(
      `${["generated","imported"].includes(a.kind) ? `<button id="favorite-detail" class="icon-button favorite-button" aria-label="${a.favorite ? "取消收藏" : "收藏照片"}" aria-pressed="${a.favorite}" title="${a.favorite ? "取消收藏" : "收藏"}">${photoIcon("heart")}</button>` : ""}<button id="download">下载原图</button><button id="trash" class="danger">${a.trash ? "恢复照片" : "移到回收站"}</button>${helpTip("回收规则", "移到回收站后，文件清理前可以恢复；清理后无法恢复。")}<button id="save-asset" class="primary">保存更改</button>`,
    ),
  );
  content.dataset.assetId = a.id;
  dialog.addEventListener("close", () => main.querySelector(`[data-detail="${CSS.escape(a.id)}"]`)?.focus(), {once:true});
  thumbs(content);
  const favorite = content.querySelector("#favorite-detail");
  if (favorite) favorite.onclick = () => busy(favorite, async () => {
    a = {...a, ...await act("asset", {id:a.id, revision:a.revision, favorite:!a.favorite})};
    favorite.setAttribute("aria-pressed", String(a.favorite));
    favorite.setAttribute("aria-label", a.favorite ? "取消收藏" : "收藏照片");
    favorite.title = a.favorite ? "取消收藏" : "收藏";
    await render();
  });
  bridge
    .apiGet("preview", { id: a.id, variant: "delivery" })
    .then((r) => {
      const img = content.querySelector("#detail-image");
      if (img && content.dataset.assetId === a.id) img.src = r.data_url;
    })
    .catch((e) => toast(e.message, true));
  content.querySelector("#download").onclick = (e) =>
    busy(e.target, () =>
      bridge.download("download/" + a.id, {}, a.id + ".png"),
    );
  content.querySelector("#trash").onclick = (e) =>
    busy(e.target, async () => {
      await act("asset", { id: a.id, revision: a.revision, trash: !a.trash });
      dialog.close();
      await render();
      toast(a.trash ? "照片已恢复" : "已移到回收站，可以恢复");
    });
  content.querySelector("#save-asset").onclick = (e) =>
    busy(e.target, async () => {
      const f = content.querySelector("#asset-form").elements;
      await act("asset", {
        id: a.id,
        revision: a.revision,
        description: f.description.value,
      });
      dialog.close();
      await render();
      toast("照片信息已保存");
    });
}

function pickFile(callback) {
  fileInput.value = "";
  fileInput.onchange = () => {
    if (fileInput.files[0]) callback(fileInput.files[0]);
  };
  fileInput.click();
}
function openImport() {
  if (!chars().length) {
    nav("characters");
    return;
  }
  showDialog(
    "导入照片",
    `<div class="dialog-body"><label for="import-character">收入哪个角色的相册</label><select id="import-character">${options(state.character || chars()[0].id)}</select><label for="import-kind">照片用途</label><select id="import-kind"><option value="imported">相册照片</option><option value="scene">临时场景参考</option></select><p class="help" id="import-use">可用于聊天与空间</p><p class="help">静态 PNG、JPEG、WebP · 最大 20 MB ${helpTip("参考保留规则", "临时场景参考保留24小时；活动任务使用中会延后清理。")}</p></div>`,
    footer('<button id="choose-file" class="primary">选择图片并导入</button>'),
  );
  content.querySelector("#import-kind").onchange = (e) => {
    content.querySelector("#import-use").textContent = e.target.value === "scene" ? "仅供参考，不直接发送 · 保留24小时" : "可用于聊天与空间";
  };
  content.querySelector("#choose-file").onclick = (e) => {
    const character = content.querySelector("#import-character").value;
    const kind = content.querySelector("#import-kind").value;
    if (!character) return toast("请选择角色", true);
    pickFile((file) =>
      busy(e.target, async () => {
        const a = await bridge.upload(`upload/${character}/${kind}`, file);
        dialog.close();
        state.character = character;
        state.category = kind;
        state.offset = 0;
        await render();
        openDetail(a);
        toast("照片已导入");
      }),
    );
  };
}

function renderCharacters() {
  main.innerHTML = `<div class="subhead"><div><h2>角色参考</h2></div><button id="new-character" class="primary">新建角色</button></div><div class="character-grid">${chars()
    .map((c) => {
      const active = c.versions.find((v) => v.id === c.active_version);
      return `<article class="character-card"><div class="character-cover">${active ? `<img data-asset="${active.asset_ids[0]}" alt="${escape(c.name)}的参考图">` : "尚未设置参考图"}</div><div class="character-info"><h3>${escape(c.name)}</h3>${c.appearance ? `<p class="muted">${escape(c.appearance.slice(0, 85))}</p>` : ""}<span class="badge">${c.sessions.length} 个绑定会话 · ${c.versions.length} 个参考版本</span><div class="actions" style="margin-top:16px"><button data-edit-char="${c.id}">编辑角色</button><button data-refs="${c.id}" class="primary">管理参考图</button></div></div></article>`;
    })
    .join(
      "",
    )}</div>${chars().length ? "" : '<div class="empty"><div class="empty-icon" aria-hidden="true">◇</div><h2>暂无角色</h2><p>新建角色后可设置参考图</p></div>'}`;
  main.querySelector("#new-character").onclick = () => editCharacter();
  main
    .querySelectorAll("[data-edit-char]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          editCharacter(chars().find((c) => c.id === b.dataset.editChar))),
    );
  main
    .querySelectorAll("[data-refs]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          openReferences(chars().find((c) => c.id === b.dataset.refs))),
    );
  thumbs(main);
}

function editCharacter(c) {
  showDialog(
    c ? "编辑角色" : "新建角色",
    `<form id="character-form" class="dialog-body"><label for="name">角色名称</label><input id="name" name="name" maxlength="80" required value="${escape(c?.name)}"><label for="appearance">外观描述</label><textarea id="appearance" name="appearance" maxlength="2000" placeholder="发色、瞳色、具有辨识度的特征…">${escape(c?.appearance)}</textarea><label for="style">画风偏好</label><textarea id="style" name="style" maxlength="2000" placeholder="例如：二次元角色，融入真实照片的环境与光线">${escape(c?.style)}</textarea><details><summary>更多选项</summary><label for="failure-style">拍照失败时的反应（可选）</label><input id="failure-style" name="failure_style" maxlength="500" placeholder="语气或小动作" value="${escape(c?.failure_style)}"></details><label for="sessions">绑定会话，一行一个</label><textarea id="sessions" name="sessions" placeholder="平台ID:FriendMessage:会话ID">${escape(c?.sessions.join("\n"))}</textarea><p class="help">仅绑定会话可使用相册工具；留空则仅供管理页使用。</p>${state.data.sessions.length ? `<label for="known-session">最近收到的会话</label><select id="known-session"><option value="">选择一项添加</option>${state.data.sessions.map((s) => `<option value="${escape(s.id)}">${escape(s.name)} · ${escape(s.id)}</option>`).join("")}</select>` : ""}${helpTip("绘图资料说明", "这些资料只用于绘图，不会覆盖 AstrBot 人格或更换 QQ 头像。")}</form>`,
    footer('<button id="save-character" class="primary">保存角色</button>'),
  );
  const known = content.querySelector("#known-session");
  if (known)
    known.onchange = () => {
      const field = content.querySelector("#sessions");
      if (known.value && !field.value.split("\n").includes(known.value))
        field.value += (field.value ? "\n" : "") + known.value;
    };
  content.querySelector("#save-character").onclick = (e) =>
    busy(e.target, async () => {
      const f = content.querySelector("#character-form").elements;
      if (!f.name.value.trim()) return toast("请填写角色名称", true);
      const saved = await act("character", {
        id: c?.id,
        revision: c?.revision,
        name: f.name.value,
        appearance: f.appearance.value,
        style: f.style.value,
        failure_style: f.failure_style.value,
        sessions: f.sessions.value
          .split("\n")
          .map((x) => x.trim())
          .filter(Boolean),
      });
      dialog.close();
      await reload();
      toast("角色已保存");
      if (!c) openReferences(saved);
    });
}

async function openReferences(c, offset = 0, selection = null) {
  try {
    const r = await bridge.apiGet("assets", {
      character_id: c.id,
      category: "reference",
      offset,
    });
    let selected = selection || [
      ...(c.versions.find((v) => v.id === c.active_version)?.asset_ids || []),
    ];
    showDialog(
      c.name + " · 角色参考",
      `<div class="dialog-body"><p class="help"><span id="reference-count" role="status">已选 ${selected.length}/4</span> · 仅影响之后的新任务 ${helpTip("参考数量说明", "角色与额外参考合计最多4张；需要场景参考时，请预留位置。")}</p><div class="ref-grid">${r.items.map((a) => `<button class="ref-choice ${selected.includes(a.id) ? "selected" : ""}" data-ref="${a.id}" aria-pressed="${selected.includes(a.id)}"><img data-asset="${a.id}" alt="角色参考 ${escape(date(a.created))}"><span>${escape(date(a.created))}</span></button>`).join("") || '<p class="muted">还没有参考图，先上传一张。</p>'}</div><div class="actions"><button id="ref-prev" ${offset === 0 ? "disabled" : ""}>上一页</button><span>${offset + 1}–${Math.min(offset + r.items.length, r.total)} / ${r.total}</span><button id="ref-next" ${offset + r.items.length >= r.total ? "disabled" : ""}>下一页</button></div><label for="old-version">历史版本</label><select id="old-version"><option value="">选择历史版本…</option>${c.versions.map((v, i) => `<option value="${v.id}">版本 ${i + 1} · ${date(v.created)}${v.id === c.active_version ? " · 当前使用" : ""}</option>`).join("")}</select></div>`,
      footer(
        '<button id="upload-ref">上传新参考</button><button id="restore-version">启用历史版本</button><button id="activate-ref" class="primary">启用所选参考</button>',
      ),
    );
    content.querySelectorAll("[data-ref]").forEach(
      (b) =>
        (b.onclick = () => {
          if (selected.includes(b.dataset.ref))
            selected = selected.filter((x) => x !== b.dataset.ref);
          else if (selected.length < 4) selected.push(b.dataset.ref);
          else return toast("一次最多选择四张参考图", true);
          content.querySelector("#reference-count").textContent = `已选 ${selected.length}/4`;
          b.classList.toggle("selected", selected.includes(b.dataset.ref));
          b.setAttribute(
            "aria-pressed",
            String(selected.includes(b.dataset.ref)),
          );
        }),
    );
    content.querySelector("#ref-prev").onclick = () => openReferences(c, Math.max(0, offset - 24), selected);
    content.querySelector("#ref-next").onclick = () => openReferences(c, offset + 24, selected);
    content.querySelector("#upload-ref").onclick = (e) =>
      pickFile((file) =>
        busy(e.target, async () => {
          await bridge.upload(`upload/${c.id}/reference`, file);
          await openReferences(c, 0, selected);
          toast("已上传，尚未启用");
        }),
      );
    content.querySelector("#activate-ref").onclick = (e) =>
      busy(e.target, async () => {
        await act("reference", {
          id: c.id,
          revision: c.revision,
          asset_ids: selected,
        });
        dialog.close();
        await reload();
        toast("新参考版本已启用");
      });
    content.querySelector("#restore-version").onclick = (e) =>
      busy(e.target, async () => {
        const version = content.querySelector("#old-version").value;
        if (!version) return toast("请选择历史版本", true);
        await act("reference", {
          id: c.id,
          revision: c.revision,
          version_id: version,
        });
        dialog.close();
        await reload();
        toast("历史参考版本已启用");
      });
    thumbs(content);
  } catch (e) {
    toast(e.message, true);
  }
}

function openGenerate() {
  if (!chars().length) {
    nav("characters");
    return;
  }
  const requestId = crypto.randomUUID();
  let sceneSelection = [];
  showDialog(
    "生成一张新照片",
    `<div class="dialog-body"><label for="gen-character">角色</label><select id="gen-character">${options(state.character || chars()[0].id)}</select><label for="scene">想留下怎样的画面</label><textarea id="scene" maxlength="3000" rows="5" placeholder="描述一个瞬间：在哪里、穿什么、正在做什么，镜头从哪里看…"></textarea><label>额外参考 ${helpTip("生成参考说明", "固定角色参考自动加入；额外参考最多3张，与角色参考合计最多4张。")}</label><div id="scene-picker"></div><p class="help">仅入册，不自动发送 QQ 或空间。</p></div>`,
    footer('<button id="submit-generation" class="primary">开始生成</button>'),
  );
  const drawScenes = async (offset = 0, category = "scene") => {
    const character = content.querySelector("#gen-character").value;
    const box = content.querySelector("#scene-picker");
    box.textContent = "正在读取参考…";
    try {
      const r = await bridge.apiGet("assets", {character_id: character, category, offset});
      if (!box.isConnected || content.querySelector("#gen-character").value !== character) return;
      box.innerHTML = `<div class="actions"><button type="button" data-scenes="scene">聊天临时参考</button><button type="button" data-scenes="all">相册照片</button><span>已选 ${sceneSelection.length}/3</span></div><div class="ref-grid">${r.items.map((a) => `<button type="button" data-scene="${a.id}" class="ref-choice ${sceneSelection.includes(a.id) ? "selected" : ""}" aria-pressed="${sceneSelection.includes(a.id)}"><img data-asset="${a.id}" alt="${escape(a.description || "场景参考")}"><span>${escape(a.description || date(a.created))}</span></button>`).join("") || '<p class="muted">暂无可用参考</p>'}</div><div class="actions"><button type="button" id="scene-prev" ${offset === 0 ? "disabled" : ""}>上一页</button><span>${r.total} 张</span><button type="button" id="scene-next" ${offset + r.items.length >= r.total ? "disabled" : ""}>下一页</button></div>`;
      box.querySelectorAll("[data-scenes]").forEach((b) => b.onclick = () => drawScenes(0, b.dataset.scenes));
      box.querySelectorAll("[data-scene]").forEach((b) => b.onclick = () => {
        if (sceneSelection.includes(b.dataset.scene)) sceneSelection = sceneSelection.filter((id) => id !== b.dataset.scene);
        else if (sceneSelection.length < 3) sceneSelection.push(b.dataset.scene);
        else return toast("最多选择三张额外参考", true);
        drawScenes(offset, category);
      });
      box.querySelector("#scene-prev").onclick = () => drawScenes(Math.max(0, offset - 24), category);
      box.querySelector("#scene-next").onclick = () => drawScenes(offset + 24, category);
      thumbs(box);
    } catch (e) { box.textContent = e.message; }
  };
  content.querySelector("#gen-character").onchange = () => { sceneSelection = []; drawScenes(); };
  drawScenes();
  content.querySelector("#submit-generation").onclick = (e) =>
    busy(e.target, async () => {
      const scene = content.querySelector("#scene").value;
      const character = content.querySelector("#gen-character").value;
      await act("generate", {
        character_id: character,
        scene,
        asset_ids: sceneSelection,
        request_id: requestId,
      });
      dialog.close();
      state.tab = "jobs";
      await reload();
      toast("任务已创建，完成后会自动入册");
    });
}

async function renderJobs(serial) {
  if (historyState.kind === "memory") return renderMemoryDiagnostics(serial);
  const r = await bridge.apiGet("history", {kind: historyState.kind, status: historyState.status, character: historyState.character, query: historyState.query, offset: historyState.offset});
  if (state.tab !== "jobs" || serial !== state.serial) return;
  historyState.page = r; historyState.offset = r.offset;
  const kind = historyState.kind;
  main.innerHTML = `<div class="page-tools"><div class="subhead"><div><h2>任务记录</h2></div><div class="actions"><button id="history-diagnostics">导出诊断包</button><button id="history-policy">保留设置</button><button id="manage-history">${historyState.manage ? "完成管理" : "管理记录"}</button><button id="refresh-jobs">刷新</button></div></div><div class="categories history-nav" role="group" aria-label="记录类型">${Object.entries(historyNames).map(([k,v])=>`<button data-history-kind="${k}" aria-pressed="${kind===k}" class="${kind===k?'active':''}">${v}</button>`).join("")}</div><details class="history-filters" ${window.matchMedia("(min-width: 601px)").matches ? "open" : ""}><summary>筛选条件${historyState.status||historyState.character||historyState.query ? " · 已应用" : ""}</summary><form id="history-filter" class="history-filter"><div><label for="history-status">状态</label><select id="history-status"><option value="">全部状态</option>${Object.entries(r.counts).map(([k,n])=>`<option value="${escape(k)}" ${historyState.status===k?'selected':''}>${escape(recordStatus[k]||k)} (${n})</option>`).join("")}</select></div>${kind==='jobs'?`<div><label for="history-character">角色</label><select id="history-character">${options(historyState.character,true)}</select></div>`:''}<div class="history-search"><label for="history-query">搜索</label><input id="history-query" maxlength="100" value="${escape(historyState.query)}" placeholder="画面、名称、错误或编号"></div><div class="actions"><button type="submit">筛选</button><button type="button" id="history-reset">重置</button></div></form></details><div class="history-status"><span id="history-update" role="status">共 ${r.total} 条 · 每页 ${r.limit} 条</span></div>${historyState.manage?'<div class="actions history-batch"><span id="history-selected" role="status"></span><button id="history-select-all">全选本页可清理记录</button><button id="history-clear">取消选择</button><button id="history-delete" class="danger">清理所选记录</button></div>':''}</div><div class="history-list">${r.items.map(j=>`<article class="history-card"><div class="history-card-head">${historyState.manage?`<input type="checkbox" data-history-select="${j.id}" aria-label="选择记录 ${escape(j.id)}" ${selectedHistory.has(j.id)?'checked':''} ${!j.cleanable?'disabled':''}>`:''}<strong>${escape(kind==='jobs'?j.character_name:kind==='runs'?j.name:(j.target==='qzone'?'QQ 空间':j.id.endsWith(':caption')?'照片接话':j.id.endsWith(':photo')?'图片投递':'聊天投递'))||historyNames[kind]}</strong><span class="status ${escape(j.state)}">${escape(recordStatus[j.state]||j.state)}</span><time>${date(j.created||j.at||0)}</time></div>${j.scene||j.error?`<p class="history-scene">${escape(j.scene||j.error)}</p>`:''}${j.scene&&j.error?`<p class="history-error">${escape(j.error)}</p>`:''}${kind==='jobs'?`<p class="help">聊天：${{none:'仅入相册',pending:j.notice==='sent'?'已通知状态':'等待完成',sent:'已发送',held:'留在相册',failed:'发送失败',unknown:'待核对'}[j.delivery]||'暂无'}${j.qzone_intent?` · 空间：${escape(recordStatus[j.qzone_delivery]||j.qzone_delivery||'待发布')}`:''}</p>`:''}<div class="history-card-foot">${historyState.manage&&!j.cleanable?helpTip("记录受保护",j.protection,"lock"):""}<button data-history-detail="${j.id}">查看详情</button></div></article>`).join('')||`<div class="empty"><h3>${historyState.status||historyState.character||historyState.query?"无匹配记录":"暂无记录"}</h3>${historyState.status||historyState.character||historyState.query?'<button id="history-empty-reset">重置筛选</button>':""}</div>`}</div><div class="history-pager actions"><button id="history-prev" ${r.offset===0?'disabled':''}>上一页</button><span>第 ${Math.floor(r.offset/r.limit)+1} / ${Math.max(1,Math.ceil(r.total/r.limit))} 页</span><button id="history-next" ${r.offset+r.limit>=r.total?'disabled':''}>下一页</button></div>`;
  const updateSelection = () => {
    const label = main.querySelector('#history-selected'); if (!label) return;
    label.textContent = `已选 ${selectedHistory.size} 条`;
    const eligible = r.items.filter(x=>x.cleanable);
    const all = eligible.length && eligible.every(x=>selectedHistory.has(x.id));
    main.querySelector('#history-select-all').textContent = all?'取消全选本页':'全选本页可清理记录';
    main.querySelector('#history-select-all').disabled = !eligible.length;
    main.querySelector('#history-delete').disabled = !selectedHistory.size;
    main.querySelectorAll('[data-history-select]').forEach(el=>el.checked=selectedHistory.has(el.dataset.historySelect));
  };
  main.querySelector('#manage-history').onclick=()=>{historyState.manage=!historyState.manage;selectedHistory.clear();render();};
  main.querySelector('#refresh-jobs').onclick=e=>busy(e.currentTarget,async()=>{selectedHistory.clear();await render();toast('记录已刷新');});
  main.querySelector('#history-policy').onclick=()=>openHistoryPolicy();
  main.querySelector('#history-diagnostics').onclick=()=>exportDiagnostics({categories:kind === 'runs' ? ['runs','images'] : ['images'],character:historyState.character,status:historyState.status,query:historyState.query});
  main.querySelectorAll('[data-history-kind]').forEach(b=>b.onclick=()=>{historyState.kind=b.dataset.historyKind;historyState.scope=historyState.cid="";historyState.status=historyState.character=historyState.query='';historyState.offset=0;selectedHistory.clear();render();});
  main.querySelector('#history-filter').onsubmit=e=>{e.preventDefault();historyState.status=main.querySelector('#history-status').value;historyState.character=main.querySelector('#history-character')?.value||'';historyState.query=main.querySelector('#history-query').value.trim();historyState.offset=0;selectedHistory.clear();render();};
  main.querySelector('#history-reset').onclick=()=>{historyState.status=historyState.character=historyState.query='';historyState.offset=0;selectedHistory.clear();render();};
  const emptyReset=main.querySelector('#history-empty-reset');
  if(emptyReset)emptyReset.onclick=main.querySelector('#history-reset').onclick;
  main.querySelector('#history-prev').onclick=()=>{historyState.offset=Math.max(0,r.offset-r.limit);render();window.scrollTo(0,0);};
  main.querySelector('#history-next').onclick=()=>{historyState.offset=r.offset+r.limit;render();window.scrollTo(0,0);};
  main.querySelectorAll('[data-history-select]').forEach(el=>el.onchange=()=>{const j=r.items.find(x=>x.id===el.dataset.historySelect);if(el.checked){if(selectedHistory.size>=200){el.checked=false;return toast('每次最多选择200条',true);}selectedHistory.set(j.id,{id:j.id,token:j.token});}else selectedHistory.delete(j.id);updateSelection();});
  if(historyState.manage){
    main.querySelector('#history-select-all').onclick=()=>{const eligible=r.items.filter(x=>x.cleanable);const all=eligible.every(x=>selectedHistory.has(x.id));if(!all&&new Set([...selectedHistory.keys(),...eligible.map(x=>x.id)]).size>200)return toast('每次最多选择200条',true);eligible.forEach(x=>all?selectedHistory.delete(x.id):selectedHistory.set(x.id,{id:x.id,token:x.token}));updateSelection();};
    main.querySelector('#history-clear').onclick=()=>{selectedHistory.clear();updateSelection();};
    main.querySelector('#history-delete').onclick=()=>{
      const patches=[...selectedHistory.values()];
      showDialog('清理所选记录',`<div class="dialog-body"><p>清理所选 ${patches.length} 条记录？详细日志无法恢复，照片会保留。</p></div>`,footer('<button data-cancel-clean>取消</button><button id="confirm-clean" class="danger">确认清理</button>'));
      content.querySelector('[data-cancel-clean]').onclick=()=>dialog.close();
      content.querySelector('#confirm-clean').onclick=e=>busy(e.currentTarget,async()=>{const result=await act('history_manage',{kind,patches});selectedHistory.clear();dialog.close();await render();toast(`已清理 ${result.cleaned} 条记录，照片已保留`);});
    };
    updateSelection();
  }
  main.querySelectorAll('[data-history-detail]').forEach(b=>b.onclick=()=>busy(b,async()=>{
    const j=r.items.find(x=>x.id===b.dataset.historyDetail);
    if(kind==='jobs'){const detail=await bridge.apiGet('job/'+j.id);openJobDetail(detail.job,detail.deliveries,detail.proactive || []);}
    else {
      showDialog(historyNames[kind]+'详情',`<div class="dialog-body"><dl><dt>状态</dt><dd>${escape(recordStatus[j.state]||j.state)}</dd><dt>时间</dt><dd>${date(j.created||j.at||0)}</dd>${j.error?`<dt>错误说明</dt><dd>${escape(j.error)}</dd>`:""}</dl>${j.protection?`<p class="help">${escape(j.protection)}</p>`:""}${renderActionStages(j.stages || [], j.photo_mode, kind === 'runs')}${j.job_id ? '<button id="history-photo-job">查看照片任务与投递</button>' : ""}<details class="help-details"><summary>记录详情</summary><dl><dt>记录编号</dt><dd class="mono">${escape(j.id)}</dd>${j.message_id?`<dt>消息编号</dt><dd>${escape(j.message_id)}</dd>`:""}${j.tid?`<dt>空间编号</dt><dd>${escape(j.tid)}</dd>`:""}</dl></details></div>`);
      const photoJob=content.querySelector('#history-photo-job');
      if(photoJob)photoJob.onclick=()=>busy(photoJob,async()=>{const result=await bridge.apiGet('job/'+j.job_id);if(photoJob.isConnected && dialog.open)openJobDetail(result.job,result.deliveries,result.proactive || []);});
    }
  }));
}

async function renderMemoryDiagnostics(serial) {
  const r = await bridge.apiGet("memory/diagnostics", {scope:historyState.scope || "",cid:historyState.cid || "",status:historyState.status,query:historyState.query,offset:historyState.offset});
  if (state.tab !== "jobs" || serial !== state.serial) return;
  historyState.offset = r.offset;
  const names = {reorganization_saving:"保存候选结果中",reorganization_generating:"候选生成中",reorganization_retrying:"候选重试中",reorganization_repairing:"候选AI修复中",history_generating:"历史概览生成中",history_retrying:"历史整理重试中",history_repairing:"历史概览修复中",history_completed:"历史概览已保存",history_failed:"历史整理未完成",history_save_failed:"历史候选发布失败",history_resume_failed:"原任务续跑未完成",repairing:"AI修复中",repair_validated:"修复校验通过",summary_failed:"总结失败",check_failed:"检查助手失败",check_repairing:"检查AI修复中",index_failed:"向量生成失败",recall_failed:"召回失败",recall_fallback:"召回改用字面查找",recalled:"召回完成",rerank_failed:"精排失败",summarized:"总结完成",checked:"检查完成",indexed:"向量已更新",image_described:"图片已描述",image_failed:"图片描述失败",undone:"已撤销",reset:"已重置",restored:"已恢复",copied:"已复制",imported:"已导入",inject_failed:"本轮未加入记忆",retrying:"重试中",checking:"检查中",summarizing:"总结中",queued:"等待处理",stopped:"已停止"};
  main.innerHTML = `<div class="page-tools"><div class="subhead"><h2>任务记录</h2><div class="actions"><button id="memory-diagnostics-export">导出诊断包</button><button id="memory-log-refresh">刷新</button></div></div><div class="categories history-nav" role="group" aria-label="记录类型">${Object.entries(historyNames).map(([key,name]) => `<button data-history-kind="${key}" aria-pressed="${key === "memory"}" class="${key === "memory" ? "active" : ""}">${name}</button>`).join("")}</div><form id="memory-log-filter" class="history-filter"><label>状态<select id="memory-log-status"><option value="">全部日志</option><option value="failed" ${historyState.status === "failed" ? "selected" : ""}>仅失败</option></select></label><label>搜索<input id="memory-log-query" maxlength="200" value="${escape(historyState.query)}" placeholder="状态、错误或编号"></label><button type="submit">筛选</button>${historyState.scope || historyState.cid ? '<button id="memory-log-all" type="button">查看全部聊天</button>' : ""}</form>${historyState.scope ? `<p class="memory-binding-scope">${escape(historyState.scope.replace(':FriendMessage:', ' · 私聊 · ').replace(':GroupMessage:', ' · 群聊 · '))} · 文件 ${escape(historyState.cid?.slice(0,8) || "全部")}</p>` : ""}<div class="history-status">共 ${r.total} 条</div></div><section class="history-list" aria-label="拾忆日志">${r.items.map(item => `<article class="history-card"><div class="history-card-head"><strong>${escape(names[item.state] || item.state)}</strong><time>${date(item.at)}</time><span>#${item.id}</span></div><p class="${item.state.includes("failed") ? "memory-error" : ""}">${escape(item.detail.split(" [diag ")[0])}</p><details><summary>诊断详情</summary><dl><dt>会话对象</dt><dd>${escape(item.scope)}</dd><dt>聊天文件</dt><dd>${escape(item.cid)}</dd><dt>状态代码</dt><dd>${escape(item.state)}</dd>${item.detail.includes(" [diag ") ? `<dt>错误类别与位置</dt><dd class="memory-diagnostic-tech">${escape(item.detail.split(" [diag ")[1].replace(/\]$/, ""))}</dd>` : ""}</dl></details></article>`).join("") || '<p class="empty">暂无匹配日志</p>'}</section><div class="actions history-pager"><button id="memory-log-prev" ${r.offset === 0 ? "disabled" : ""}>上一页</button><span>第 ${Math.floor(r.offset/20)+1} / ${Math.max(1,Math.ceil(r.total/20))} 页</span><button id="memory-log-next" ${r.offset+20 >= r.total ? "disabled" : ""}>下一页</button></div>`;
  main.querySelectorAll("[data-history-kind]").forEach(button => {button.onclick = () => {historyState.kind=button.dataset.historyKind;historyState.scope=historyState.cid=historyState.status=historyState.query="";historyState.offset=0;return render();};});
  main.querySelector("#memory-log-refresh").onclick = () => render();
  main.querySelector("#memory-diagnostics-export").onclick = () => exportDiagnostics({categories:["memory","recall"],scope:historyState.scope,cid:historyState.cid,status:historyState.status,query:historyState.query});
  main.querySelector("#memory-log-filter").onsubmit = event => {event.preventDefault();historyState.status=main.querySelector("#memory-log-status").value;historyState.query=main.querySelector("#memory-log-query").value.trim();historyState.offset=0;return render();};
  const all = main.querySelector("#memory-log-all"); if (all) all.onclick = () => {historyState.scope=historyState.cid="";historyState.offset=0;return render();};
  for (const [name,offset] of [["prev",Math.max(0,r.offset-20)],["next",r.offset+20]]) main.querySelector(`#memory-log-${name}`).onclick = () => {historyState.offset=offset;return render();};
}

async function openHistoryPolicy() {
  try {
    await refreshData();
    const s=state.data.settings;
    const preview=await act('history_cleanup',{});
    showDialog('记录保留设置',`<form id="history-policy-form" class="dialog-body"><p>超期或超量时清理已结束的详情，清理后无法恢复，照片保留。受保护记录可能超出上限。</p><label for="record-days">保留天数</label><input id="record-days" type="number" min="1" max="3650" required value="${s.records_retention_days}"><label for="record-count">每类记录数量上限</label><input id="record-count" type="number" min="20" max="10000" required value="${s.records_max_count}"><details class="help-details"><summary>清理与保护规则</summary><p>每分钟检查，每类每次最多清理200条。活动任务、待恢复、结果未知及现有计划的最后一次执行受保护。详细日志清理后无法恢复；照片生成信息与防重信息保留，数据库空间供后续复用。</p><p>拾忆日志按同样的天数和数量每小时清理；其中的召回记录另外只保留7天、最多300条。</p></details><ul>${Object.entries(preview).map(([k,v])=>`<li>${historyNames[k]}：${v.retained} 条，${v.protected} 条受保护，按当前规则可清理 ${v.eligible} 条</li>`).join('')}</ul></form>`,footer('<button id="cancel-policy">取消</button><button class="primary" type="submit" form="history-policy-form" id="save-policy">保存保留设置</button>'));
    content.querySelector('#cancel-policy').onclick=()=>dialog.close();
    content.querySelector('#history-policy-form').onsubmit=e=>{e.preventDefault();busy(content.querySelector('#save-policy'),async()=>{await act('settings',{settings_revision:s.settings_revision,records_retention_days:Number(content.querySelector('#record-days').value),records_max_count:Number(content.querySelector('#record-count').value)});dialog.close();selectedHistory.clear();await reload();toast('保留设置已保存');});};
  }catch(e){toast(e.message,true);}
}

function renderProactiveMarkup(records = []) {
  if (!records.length) return "";
  const historyNames = {saved:"历史已保存", pending:"已发送，历史待保存", orphaned:"原聊天已删除或不可写"};
  const kinds = {text:"主动消息", caption:"照片接话", photo:"照片引用", photo_delivery:"照片引用", notice:"照片状态", started:"拍照接单"};
  return `<section class="proactive-records" aria-label="发送原文与聊天历史">${records.map(r=>`<details class="proactive-record"><summary>${escape(kinds[r.kind] || "主动消息")} · ${escape(r.state === "sent" ? historyNames[r.history_state] || "已发送，历史待保存" : recordStatus[r.state] || "尚未发送")}${r.sent_at ? ` · ${escape(date(r.sent_at))}` : ""}</summary>
    <p class="preserve-text">${r.body_compacted ? "正文已清理" : escape(r.text)}</p><dl><dt>目标聊天</dt><dd class="mono">${escape(r.cid || "未记录")}</dd><dt>发送对象</dt><dd class="mono">${escape(r.scope)}</dd></dl>
    ${r.history_error ? `<p class="history-error">${escape(r.history_error)}</p>` : ""}
    ${r.state === "sent" && r.history_state === "pending" && !r.body_compacted && !r.chat_deleted_at ? `<button type="button" data-history-retry="${escape(r.id)}">重试保存历史</button>` : ""}
    <p class="history-error" data-proactive-error="${escape(r.id)}" role="alert" tabindex="-1" hidden></p></details>`).join("")}</section>`;
}

function bindProactiveRetry(root, refresh) {
  root.querySelectorAll('[data-history-retry]').forEach(button=>button.onclick=async()=>{
    const original = button.textContent;
    const error = [...root.querySelectorAll('[data-proactive-error]')].find(el=>el.dataset.proactiveError === button.dataset.historyRetry);
    error.hidden = true; button.disabled = true; button.textContent = "正在保存…"; button.setAttribute("aria-busy", "true");
    try {
      await act("retry_proactive_history", {key:button.dataset.historyRetry});
      if (button.isConnected) await refresh();
    } catch (err) {
      if (button.isConnected) {error.textContent = err.message || "历史保存失败，请稍后重试"; error.hidden = false; error.focus();}
    } finally {
      if (button.isConnected) {button.disabled = false; button.textContent = original; button.removeAttribute("aria-busy");}
    }
  });
}

function renderActionStages(stages = [], photoMode = "", isLifeRun = false) {
  if (!stages.length && !isLifeRun) return "";
  const labels = {generation:"生成接话",validation:"发送前校验",sending:"消息投递",history:"历史补存",photo:"附带照片",follow_up:"后续约定",recovery:"生成恢复",assistant:"助手修正"};
  const states = {ok:"完成",failed:"失败",held:"已阻止",pending:"待处理",skipped:"未执行",unknown:"待核对",fallback:"使用最小退路",retrying:"等待重试",recovered:"已恢复",exhausted:"预算已用完",cancelled:"已停止"};
  const modes = {auto:"按内容自动决定",always:"每次附带拍照",never:"关闭拍照"};
  const photoReasons = {PHOTO_DISABLED:"关闭拍照",PHOTO_NO_SCENE:photoMode === "always" ? "要求拍照，但未返回有效画面" : "未返回有效画面",PHOTO_ENGINE_UNAVAILABLE:"图片引擎不可用",TIMER_ONLY:"提醒仅文字退路",PHOTO_SUBMIT_FAILED:"照片任务建立失败",PHOTO_TASK_CREATED:"照片任务已建立"};
  const failureReasons = {
    PROTOCOL_THOUGHT:"思考包装不完整",PROTOCOL_STRUCTURE:"控制结构或位置不正确",PROTOCOL_JSON:"控制字段JSON无效",PROTOCOL_FIELDS:"控制字段不正确（旧记录）",
    PROTOCOL_DECISION_TYPE:"决策须为对象",PROTOCOL_DECISION_FIELDS:"决策含不支持的字段",PROTOCOL_DECISION_CONTACT:"联系决策字段不正确",PROTOCOL_DECISION_PROGRESS:"生活进度字段不正确",
    PROTOCOL_SOURCE_TYPE:"来源编号须为字符串数组",PROTOCOL_SOURCE_COUNT:"来源数量超限",PROTOCOL_SOURCE_UNKNOWN:"来源编号无效",PROTOCOL_SOURCE_REQUIRED:"缺少必要来源编号",PROTOCOL_REMINDER_CONTACT:"明确提醒不能自主取消或改点",PROTOCOL_EFFECTIVE_TS:"延后时间字段不正确",
    PROTOCOL_REASON_TYPE:"决策原因须为文字",PROTOCOL_REASON_LENGTH:"决策原因过长",PROTOCOL_PHOTO_EMPTY:"图片描述为空",PROTOCOL_PHOTO_LENGTH:"图片描述过长",
    PROTOCOL_FOLLOW_TYPE:"心念参数须为对象",PROTOCOL_FOLLOW_FIELDS:"心念含不支持的字段",PROTOCOL_FOLLOW_DELAY_TYPE:"心念分钟数须为整数",PROTOCOL_FOLLOW_DELAY_RANGE:"心念分钟数超出范围",PROTOCOL_FOLLOW_WINDOW_TYPE:"心念浮动须为整数",PROTOCOL_FOLLOW_WINDOW_RANGE:"心念浮动超出范围",
    PROTOCOL_FOLLOW_NAME_TYPE:"心念名称须为文字",PROTOCOL_FOLLOW_NAME_LENGTH:"心念名称过长",PROTOCOL_FOLLOW_INSTRUCTION_TYPE:"心念内容须为文字",PROTOCOL_FOLLOW_INSTRUCTION_EMPTY:"心念内容为空",PROTOCOL_FOLLOW_INSTRUCTION_LENGTH:"心念内容过长",PROTOCOL_FOLLOW_PHOTO_TYPE:"心念拍照字段须为布尔值",
    GENERATION_BUDGET:"本次生成次数已用完",GENERATION_DEADLINE:"本次生成总时限已到",GENERATION_CANCELLED:"本次生成已停止",GENERATION_STALE:"任务或聊天已变化",CHAT_ACTIVE:"聊天已更新，本次不打扰",
    MODEL_AUTH:"模型密钥或权限异常",MODEL_CONFIG:"模型配置不正确",MODEL_RATE_LIMIT:"模型服务限流",MODEL_SERVER:"模型服务暂时失败",ASSISTANT_UNAVAILABLE:"助手连接不可用",ASSISTANT_CHANGED:"助手用途或连接已变化",ASSISTANT_RETRY:"助手建议重新生成",ASSISTANT_UNREPAIRABLE:"助手无法修复",ASSISTANT_RESPONSE:"助手返回格式不正确",ASSISTANT_UNSAFE_EDIT:"助手修改超出允许范围",RESPONSE_LIMIT:"模型返回超过限制"
  };
  const hasPhoto = stages.some(s=>s.stage === "photo");
  return `<details class="help-details"><summary>处理诊断</summary>${hasPhoto || isLifeRun ? `<p>执行时拍照模式：${escape(modes[photoMode] || "未记录")}</p>` : ""}${isLifeRun && !hasPhoto ? "<p>拍照原因未记录</p>" : ""}${stages.map(s=>{
    const photo = s.stage === "photo";
    const status = photo && s.status === "ok" ? "照片任务已建立" : states[s.status] || s.status;
    const reason = photo ? photoReasons[s.code] || "原因未记录" : failureReasons[s.code] || s.code;
    const count = (key, max) => Number.isSafeInteger(s[key]) && s[key] >= 0 && s[key] <= max;
    const counts = [count("attempt",6)?`第${s.attempt}次尝试`:"",count("calls",6)?`插件调用${s.calls}次`:"",count("elapsed_ms",1200000)?`耗时${(s.elapsed_ms/1000).toFixed(1)}秒`:""].filter(Boolean).join(" · ");
    return `<p>${s.at ? escape(date(s.at)) + " · " : ""}${escape(labels[s.stage] || s.stage)} · ${escape(photo && photoReasons[s.code] ? photoReasons[s.code] : status)}${reason && (!photo || !photoReasons[s.code]) ? ` · ${escape(reason)}` : ""}${counts ? ` · ${escape(counts)}` : ""}</p>`;
  }).join("")}</details>`;
}

function occurrenceLocalValue(timestamp, timezone) {
  const parts = new Intl.DateTimeFormat("en-CA", {timeZone:timezone || "Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hourCycle:"h23"}).formatToParts(new Date(timestamp * 1000));
  const p = Object.fromEntries(parts.map(item=>[item.type,item.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

function occurrenceTimestamp(value, timezone) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) throw new Error("请选择完整的日期和时间");
  const naive = Date.parse(value + "Z") / 1000;
  if (!Number.isFinite(naive)) throw new Error("日期或时间无效");
  const offsets = new Set([-36,0,36].map(h=>{
    const sample = naive + h * 3600;
    return Date.parse(occurrenceLocalValue(sample,timezone) + "Z") / 1000 - sample;
  }));
  const matches = [...offsets].map(offset=>naive-offset).filter(ts=>occurrenceLocalValue(ts,timezone) === value);
  if (!matches.length) throw new Error("该日期或当地时间不存在，请重新选择");
  if (matches.length > 1) throw new Error("该当地时间因夏令时重复，请选择其他时间");
  return matches[0];
}

function occurrenceTarget(scope, data) {
  const [bot, kind, target] = String(scope || "").split(":");
  if (data.bots?.some(b=>b.id === bot) && ["FriendMessage","GroupMessage"].includes(kind) && target) {
    return `QQ ${kind === "GroupMessage" ? "群聊" : "私聊"} ${target}${data.bots.length > 1 ? ` · ${data.bots.find(b=>b.id === bot).name || bot}` : ""}`;
  }
  return scope || "目标未记录";
}

function renderTodayMarkup(data) {
  const scope = data.calendar_scope || "";
  const items = data.today || [];
  const states = {pending:"待到点", sent:"已发送", running:"执行中", skipped:"本次未打扰", held:"已保留", failed:"生成或发送失败", unknown:"投递待核对", delayed:"临时延后", expired:"时间已过"};
  const progress = {ongoing:"聊天中正在进行", completed:"聊天中已说完成", cancelled:"聊天中已取消"};
  const decisions = {continue:"承接已有进展", skip:"本次不打扰", delay:"临时改时间", replace:"替换本次情境"};
  return `<section class="today-workspace"><div class="today-scope"><label for="today-scope">聊天对象</label><select id="today-scope"><option value="">所有当前聊天</option>${chars().flatMap(c=>c.sessions.map(s=>`<option value="${escape(s)}" ${s === scope ? "selected" : ""}>${escape(c.name)} · ${escape(s)}</option>`)).join("")}</select>${data.conversation_id ? `<details><summary>当前聊天</summary><p class="mono">${escape(data.conversation_id)}</p></details>` : ""}</div>
    ${items.map(r=>{
      const changed = r.effective_ts !== r.default_ts;
      const when = occurrenceLocalValue(r.effective_ts,r.timezone).replace("T"," ");
      return `<article class="panel today-card"><div class="today-card-head"><h3>${escape(r.name)}</h3><span class="status ${["failed","unknown"].includes(r.state)?r.state:""}">${escape(states[r.state] || r.state || "待到点")}</span></div>
        <p class="today-time">${changed ? `<del>${escape(occurrenceLocalValue(r.default_ts,r.timezone).replace("T"," "))}</del><span aria-label="改为"> → </span>` : ""}<time>${escape(when)}</time>${r.timezone && r.timezone !== "Asia/Shanghai" ? ` · ${escape(r.timezone)}` : ""}${changed || r.decision && r.decision !== "keep" ? '<span class="badge">仅本次</span>' : ""}</p>
        ${progress[r.progress] || decisions[r.decision] || r.mode === "reminder" || !scope ? `<p class="help">${[progress[r.progress], decisions[r.decision], r.mode === "reminder" ? "按约提醒" : "", !scope ? `${chars().find(c=>c.sessions.includes(r.scope))?.name || "角色"} · ${occurrenceTarget(r.scope,data)}` : ""].filter(Boolean).map(escape).join(" · ")}</p>` : ""}
        ${r.reason ? `<p>${escape(r.reason)}</p>` : ""}
        <details><summary>安排与依据</summary><p class="preserve-text">${escape(r.instruction)}</p>${(r.sources || []).map(s=>`<blockquote class="preserve-text">${escape(s.role === "user" ? "用户" : "角色")}：${escape(s.text)}${s.at?`<footer>${escape(date(s.at))}</footer>`:""}</blockquote>`).join("")}${r.source_ids?.length ? `<p class="help mono">来源：${r.source_ids.map(escape).join("、")}</p>` : ""}</details>
        ${r.editable ? `<div class="actions today-actions"><button type="button" data-occurrence="${escape(r.id)}" data-decision="delay">改时间</button><button type="button" data-occurrence="${escape(r.id)}" data-decision="skip">本次不打扰</button>${changed || r.decision && r.decision !== "keep" ? `<button type="button" data-occurrence="${escape(r.id)}" data-decision="restore">恢复本次默认</button>` : ""}</div>` : ""}</article>`;
    }).join("") || `<div class="empty"><h3>${"今天没有可显示的安排"}</h3></div>`}</section>`;
}

function editOccurrence(row, decision, data) {
  if (!row.editable) return;
  const title = {delay:"修改本次时间",skip:"本次不打扰",restore:"恢复本次默认"}[decision];
  const scope = row.scope || data.calendar_scope;
  const cid = row.cid || row.conversation_id || data.conversation_id;
  const source = document.activeElement;
  showDialog(title, `<form id="occurrence-form" class="dialog-body occurrence-editor"><p><strong>${escape(row.name)}</strong> · 仅本次</p><p class="help">${escape(occurrenceTarget(scope,data))}</p><details><summary>目标聊天</summary><p class="mono">${escape(cid)}</p></details>
    ${decision === "delay" ? `<label for="occurrence-time">执行时间 · ${escape(row.timezone || "Asia/Shanghai")}</label><input id="occurrence-time" type="datetime-local" step="60" required value="${escape(occurrenceLocalValue(row.effective_ts,row.timezone))}">` : `<p>${decision === "restore" ? "恢复本次原定时间；已经错过的时间不会补发。" : row.mode === "reminder" ? "这次约定提醒将不再发送。" : "这次安排不主动联系；下次仍按默认作息。"}</p>`}
    <label for="occurrence-reason">备注（可选）</label><input id="occurrence-reason" maxlength="300" value="">
    <p class="history-error" id="occurrence-error" role="alert" tabindex="-1" hidden></p></form>`, footer('<button type="button" data-close>取消</button><button type="submit" form="occurrence-form" id="save-occurrence" class="primary">确认本次调整</button>'));
  const form = content.querySelector('#occurrence-form'), save = content.querySelector('#save-occurrence'), error = content.querySelector('#occurrence-error');
  dialog.addEventListener("close", ()=>{if(source?.isConnected)source.focus();}, {once:true});
  form.onsubmit = async e=>{
    e.preventDefault(); if(save.disabled || !form.reportValidity())return;
    error.hidden = true; save.disabled = true; save.textContent = "正在保存…"; save.setAttribute("aria-busy","true");
    try {
      const payload = {scope,cid,occurrence_id:row.id,revision:row.revision,decision,reason:content.querySelector('#occurrence-reason').value.trim(),source_ids:[]};
      if(decision === "delay") payload.effective_ts = occurrenceTimestamp(content.querySelector('#occurrence-time').value,row.timezone);
      await act("adjust_occurrence",payload);
      if(form.isConnected && dialog.open) {dialog.close(); await render(); toast("本次安排已更新");}
    } catch(err) {
      if(form.isConnected && dialog.open) {error.textContent = err.message || "调整失败，请重试"; error.hidden = false; error.focus();}
    } finally {
      if(form.isConnected) {save.disabled = false; save.textContent = "确认本次调整"; save.removeAttribute("aria-busy");}
    }
  };
}

function bindTodayView(data) {
  const selector = main.querySelector('#today-scope');
  selector.onchange = ()=>{state.calendarScope = selector.value; render();};
  main.querySelectorAll('[data-occurrence]').forEach(button=>button.onclick=()=>{
    const row = (data.today || []).find(r=>r.id === button.dataset.occurrence);
    if(row) editOccurrence(row,button.dataset.decision,data);
  });
}

function openJobDetail(j, receipts = null, proactive = []) {
  const stageNames = {provider: "请求 / 查询上游", download: "下载成图", import: "校验并收入相册"};
  const resumable = j.result_available || j.provider_task_id || j.attempts?.some(a=>a.task_id && ["running","unknown"].includes(a.state));
  const waiting = ["queued","submitting","running","unknown","downloading","download_failed"].includes(j.state);
  const controls = `${j.asset_id?'<button id="detail-photo">查看照片</button>':''}${waiting&&resumable?`<button id="detail-resume">${j.result_available?'恢复下载原图':'继续查询'}</button>`:''}${waiting?'<button id="detail-cancel" class="danger">停止本地等待</button>':''}`;
  const deliveries = receipts || state.data.deliveries.filter((d) => d.job_id === j.id);
  const recovery = j.result_available ? "可恢复下载原图，不会重新生图。" : resumable ? "可继续查询原上游任务，不会重新生图。" : j.state === "unknown" ? "请先到原站核对结果，避免重复提交。" : j.state === "failed" && !j.attempts?.length ? "原结果未保留，可从图片站下载后导入。" : "";
  const deliveryLabel = d => d.target === "qzone" ? "QQ 空间" : d.id.endsWith(":caption") ? "照片接话" : d.id.endsWith(":photo") ? "图片投递" : "聊天投递";
  const latest = [...new Map([...deliveries].sort((a,b)=>(a.created||0)-(b.created||0)).map(d=>[deliveryLabel(d),d])).entries()];
  showDialog("任务详情", `<div class="dialog-body">
    ${j.scene?`<p>${escape(j.scene)}</p>`:""}<dl><dt>当前状态</dt><dd>${escape(statusNames[j.state] || j.state)}</dd>
    ${j.upstream_completed||j.asset_id?"<dt>上游成图</dt><dd>已返回图片结果</dd>":j.state==='unknown'?"<dt>上游成图</dt><dd>结果待核对</dd>":""}
    ${j.error?`<dt>错误说明</dt><dd>${escape(j.error)}</dd>`:""}${recovery?`<dt>恢复方式</dt><dd>${escape(recovery)}</dd>`:""}</dl>
    ${latest.length?`<h3>投递结果</h3>${latest.map(([label,d])=>`<p>${label}：${escape(recordStatus[d.state]||d.state)}${d.error?` · ${escape(d.error)}`:""}</p>`).join("")}`:""}
    ${renderProactiveMarkup(proactive)}
    ${renderActionStages(j.notification_stages || [])}
    ${waiting?'<p class="help">停止本地等待不会取消上游任务，上游仍可能完成或计费。</p>':""}
    <details class="help-details"><summary>诊断与完整回执</summary><p class="mono">任务编号：${escape(j.id)}</p>
    ${!j.upstream_completed&&!j.asset_id?'<p>未记录成图结果，不能仅凭失败状态判断上游是否完成。</p>':""}
    ${(j.diagnostics||[]).length?`<h3>处理记录</h3>${j.diagnostics.map(d=>`<p>${escape(date(d.at))} · ${escape(stageNames[d.stage]||d.stage)}<br>${escape(d.code||"异常类别未记录")}${d.http_status?` · HTTP ${d.http_status}`:""}</p>`).join("")}`:""}
    ${j.attempts?.length?`<h3>站点尝试</h3><p>每站追加 ${j.retry_policy?.max_retries??0} 次 · 并行备用 ${j.retry_policy?.parallel_backup?"开启":"关闭"}</p>${j.attempts.map(a=>`<details><summary>第 ${escape(a.id)} 次 · ${escape(a.provider_name)} · ${{completed:"图片可用",failed:"失败",unknown:"待核对",running:"查询中",submitting:"请求中",download_failed:"下载失败",downloading:"取图中",abandoned:"已停止等待",unused:"未采用",preparing:"准备中"}[a.state]||escape(a.state)}${j.winner===a.id?" · 已采用":""}</summary><p>${escape(date(a.at))} · ${escape(a.stage||"生成")}<br>${escape(a.error||"")}${a.code?` · ${escape(a.code)}`:""}${a.http_status?` · HTTP ${a.http_status}`:""}${a.download_attempts?` · 下载尝试 ${a.download_attempts} 次`:""}</p></details>`).join("")}`:""}
    <h3>投递回执</h3>${deliveries.map(d=>`<p class="mono">${escape(d.id)}<br>${escape(recordStatus[d.state]||d.state)}${d.message_id?` · 消息 ${escape(d.message_id)}`:""}${d.tid?` · 空间 ${escape(d.tid)}`:""}</p>`).join("")||'<p>尚无投递</p>'}</details>
  </div>`, footer(controls));

  for(const action of ['resume','cancel']) {
    const button=content.querySelector('#detail-'+action);
    if(button)button.onclick=()=>busy(button,async()=>{await act(action,{id:j.id});const detail=await bridge.apiGet('job/'+j.id);openJobDetail(detail.job,detail.deliveries,detail.proactive || []);toast(action==='cancel'?'已停止本地等待，记录保留；上游可能仍会完成':'已恢复处理原任务，不会重新生图');});
  }
  const photo=content.querySelector('#detail-photo');
  if(photo)photo.onclick=()=>busy(photo,async()=>openDetail(await bridge.apiGet('asset/'+j.asset_id)));
  const retryAnchor = content.querySelector('.proactive-records');
  bindProactiveRetry(content,async()=>{const detail=await bridge.apiGet('job/'+j.id);if(retryAnchor?.isConnected && dialog.open)openJobDetail(detail.job,detail.deliveries,detail.proactive || []);});
}

async function renderPlans() {
  const serial = state.serial;
  const data = await bridge.apiGet("plans", {scope: state.calendarScope || ""});
  if (state.tab !== "plans" || serial !== state.serial) return;
  const names = {delayed: "临时延后", sent: "已发送", skipped: "本次未打扰", held: "已保留", running: "执行中", unknown: "待核对", failed: "发送失败", expired: "过期未送达"};
  const photoLabels = {auto: "按内容自动拍照", always: "每次附带拍照", never: "仅发文字"};
  const dayModeLabels = {cron: "固定规则", school_day: "上课日", holiday_rest: "休息日", sleep_before_work: "明日早起", night_owl_before_rest: "明日休息"};
  const deltaText = (d) => typeof d === "number" && d !== 0 ? (d < 0 ? `早 ${Math.abs(d)} 分钟` : `晚 ${d} 分钟`) : "准点";
  const windowText = (p) => (p.window_before || p.window_after) ? `浮动 -${p.window_before || 0} ~ +${p.window_after || 0} 分钟` : "准点触发";
  const view = state.planView || "list";
  const plans = data.plans.filter((p) => !!p.deleted === (view === "trash"));
  const sortField = state.planSortField || "next";
  const sortDirection = state.planSortDirection || "asc";
  const sortFields = {next: "下次执行时间", last: "上次执行时间", name: "任务名称", original: "原有顺序"};
  const sortHint = `排序：${sortFields[sortField]}${sortField === "original" ? "" : ` · ${sortDirection === "asc" ? "升序" : "降序"}`}`;
  if (view === "list" && sortField !== "original") {
    const collator = new Intl.Collator("zh-CN", {numeric: true, sensitivity: "base"});
    const nextTime = (p) => {
      const value = sortField === "last" ? p.last_run?.at : p.next_times?.[0];
      return value ? (typeof value === "number" ? value * 1000 : Date.parse(value)) : NaN;
    };
    plans.sort((a, b) => {
      if (sortField === "name") return collator.compare(a.name, b.name) * (sortDirection === "desc" ? -1 : 1);
      const left = nextTime(a), right = nextTime(b);
      if (!Number.isFinite(left)) return Number.isFinite(right) ? 1 : 0;
      if (!Number.isFinite(right)) return -1;
      return (left - right) * (sortDirection === "desc" ? -1 : 1);
    });
  }
  const sortControl = view === "list" ? `<div class="plan-sort">
    <button id="plan-sort" class="icon-button" type="button" title="${sortHint}（点击更改）" aria-label="${sortHint}，更改排序" aria-haspopup="menu" aria-expanded="false" aria-controls="plan-sort-menu"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path class="sort-arrow${sortField !== "original" && sortDirection === "asc" ? " active" : ""}" d="M7 20V4m-4 4 4-4 4 4"/><path class="sort-arrow${sortField !== "original" && sortDirection === "desc" ? " active" : ""}" d="M17 4v16m-4-4 4 4 4-4"/></svg></button>
    <div id="plan-sort-menu" class="plan-sort-menu" role="menu" aria-label="排序方式" hidden>
      <div role="group" aria-label="排序依据">${Object.entries(sortFields).map(([value,label])=>`<button type="button" role="menuitemradio" tabindex="-1" aria-checked="${value === sortField}" data-sort-field="${value}">${icon("check")}<span>${label}</span></button>`).join("")}</div>
      <div class="sort-separator" role="separator"></div>
      <div role="group" aria-label="排序方向">${Object.entries({asc:"升序",desc:"降序"}).map(([value,label])=>`<button type="button" role="menuitemradio" tabindex="-1" aria-checked="${value === sortDirection && sortField !== "original"}" data-sort-direction="${value}" ${sortField === "original" ? "disabled" : ""}>${icon("check")}<span>${label}</span></button>`).join("")}</div>
    </div></div>` : "";
  const wallTime = (value, zone) => value ? new Intl.DateTimeFormat("zh-CN", {timeZone:zone || "Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false}).format(new Date(typeof value === "number" ? value * 1000 : value)) : "暂无";
  for (const key of selectedPlans.keys()) if (!plans.some((p) => p.id === key)) selectedPlans.delete(key);
  const recipient = (scope) => {
    const [bot, kind, number] = String(scope || "").split(":");
    return data.bots?.some(b => b.id === bot) && ["FriendMessage", "GroupMessage"].includes(kind) && number ? `QQ ${kind === "GroupMessage" ? "群聊" : "私聊"} ${number}${data.bots.length > 1 ? ` · ${data.bots.find(b => b.id === bot).name || bot}` : ""}` : scope;
  };
  const scheduleText = (p) => {
    if (p.run_once) return "一次性约定";
    const value = readPlanSchedule(p.cron || "");
    const days = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
    if (p.day_mode && p.day_mode !== "cron" && value.mode !== "custom") return `基准 ${value.time}`;
    return value.mode === "custom" ? "自定义规则" : `${value.mode === "daily" ? "每天" : value.mode === "monthly" ? `每月 ${value.day} 日` : value.days.map(d => days[d]).join("、")} ${value.time}`;
  };
  const cards = plans.map((p) => `<article class="panel plan-card ${selectedPlans.has(p.id) ? "selected" : ""}"><div class="plan-head"><div class="plan-title">${state.planManage ? `<input type="checkbox" data-select-plan="${p.id}" aria-label="选择计划 ${escape(p.name)}" ${selectedPlans.has(p.id) ? "checked" : ""}>` : ""}<h3>${escape(p.name)}</h3>${p.run_once ? '<span class="badge">一次性</span>' : ""}${p.run_once && p.override_day_wake ? `<span class="badge">单日起床${p.override_date ? ` (${escape(p.override_date)})` : ""}</span>` : (!p.run_once && p.day_mode && p.day_mode !== "cron" ? `<span class="badge">${escape(dayModeLabels[p.day_mode] || p.day_mode)}</span>` : "")}${p.window_before || p.window_after ? helpTip("执行浮动", windowText(p), "clock") : ""}${helpTip("拍照方式", photoLabels[p.photo_mode || "auto"] || "按内容自动拍照", "camera")}</div><div class="actions">${p.deleted ? `<button data-restore-plan="${p.id}">恢复计划</button>` : `<label class="plan-switch"><input type="checkbox" role="switch" data-toggle-plan="${p.id}" aria-label="启用计划 ${escape(p.name)}" ${p.enabled ? "checked" : ""}><span aria-hidden="true"></span><span>${p.enabled ? "已启用" : "已停用"}</span></label>${p.run_once ? "" : `<button class="icon-button" data-edit-plan="${p.id}" aria-label="编辑计划 ${escape(p.name)}" title="编辑计划">${photoIcon("pencil")}</button>`}<button data-delete-plan="${p.id}" class="icon-button danger" aria-label="删除计划 ${escape(p.name)}" title="移到回收站">${photoIcon("trash")}</button>`}</div></div><p>${escape(scheduleText(p))}${p.timezone && p.timezone !== "Asia/Shanghai" ? ` · ${escape(p.timezone)}` : ""}</p><p class="help">${escape(charName(p.character_id))} · ${escape(recipient(p.scope))}</p><div class="plan-times"><p><span>上次执行时间</span>${p.last_run ? `${escape(wallTime(p.last_run.at, p.timezone))}${p.last_run.delta_minutes ? `（${escape(deltaText(p.last_run.delta_minutes))}）` : ""} · ${escape(names[p.last_run.state] || p.last_run.state)}` : "尚未执行"}</p><p><span>下次预计触发</span>${p.deleted ? "已删除，不会执行" : `${escape(wallTime(p.next_times[0], p.timezone))}${p.next_details?.[0] && p.next_details[0].delta_minutes ? `（较基准${escape(deltaText(p.next_details[0].delta_minutes))}）` : ""}${p.next_details?.[0]?.day_label ? ` · ${escape(p.next_details[0].day_label)}` : ""}${p.enabled ? "" : "（停用中，仅预览）"}`}</p></div><details><summary>计划详情</summary><p class="preserve-text">${escape(p.instruction)}</p><p class="help mono">${p.run_once && p.trigger_at ? `基准 ${escape(wallTime(p.trigger_at, p.timezone))} · 触发后自动移除` : `Cron：${escape(p.cron)}`} · ${escape(p.timezone)}<br>${escape(p.scope)}</p></details></article>`).join("");
  const runs = data.runs.map((r) => `<details class="panel plan-run"><summary>${escape(wallTime(r.at, r.timezone))} · ${escape(r.name)}${r.run_once ? "（一次性）" : ""}${r.delta_minutes ? ` · ${escape(deltaText(r.delta_minutes))}` : ""} · ${escape(names[r.state] || r.state)}</summary><p class="help">${escape(r.timezone)}${r.anchor_at ? ` · 基准时间 ${escape(wallTime(r.anchor_at, r.timezone))}` : ""}</p>${(data.proactive || []).some(p=>p.run_id === r.id) ? renderProactiveMarkup(data.proactive.filter(p=>p.run_id === r.id)) : r.summary ? `<p class="preserve-text">${escape(r.summary)}</p>` : ""}${r.error || r.code || r.message_id || r.job_id || r.follow_up_name ? `<p>${escape(r.error || "")}${r.code ? ` · ${escape(r.code)}` : ""}${r.message_id ? ` · 消息 ${escape(r.message_id)}` : ""}${r.job_id ? ` · 伴随拍照任务 ${escape(r.job_id)}` : ""}${r.follow_up_name ? ` · 延续约定「${escape(r.follow_up_name)}」` : ""}</p>` : ""}${renderActionStages(r.stages || [], r.photo_mode, true)}${r.job_id ? `<button data-run-job="${escape(r.job_id)}">查看照片任务与投递</button>` : ""}</details>`).join("");
  const todaySummary = data.today_profile ? `${escape(data.today_profile.date)} ${escape(data.today_profile.weekday_cn)} · ${escape(data.today_profile.label)}${data.today_profile.active_trip ? ` · 行程「${escape(data.today_profile.active_trip.title)}」` : ""}` : "";
  main.innerHTML = `<div class="page-tools"><div class="subhead"><div><h2>朝夕拾记</h2></div><div class="actions">${view !== "runs" && view !== "calendar" && view !== "today" ? `<button id="manage-plans">${state.planManage ? "完成管理" : "管理计划"}</button>` : view === "runs" ? '<button id="run-diagnostics-export">导出诊断包</button><button id="all-run-history">查看全部执行记录</button>' : ''}<details class="toolbar-more"><summary>更多</summary><div class="actions"><button id="import-plans">导入计划</button><button id="export-plans">导出 JSON</button></div></details><button id="new-plan" class="primary">新建计划</button></div></div><div class="plan-view-tools"><div class="categories plan-nav" role="group" aria-label="计划导航">${Object.entries({list:"朝夕列表",today:"今日",calendar:"时令日历",runs:"朝夕足迹",trash:"回收站"}).map(([key,label])=>`<button data-plan-view="${key}" class="${key === view ? "active" : ""}" aria-pressed="${key === view}">${label}</button>`).join("")}</div>${sortControl}</div>${todaySummary && view !== "calendar" ? `<p class="help plan-rule">${todaySummary}</p>` : ""}${state.planManage && view !== "runs" && view !== "calendar" && view !== "today" ? `<div class="plan-batch"><span id="plan-selection-count" role="status"></span><div class="actions"><button id="select-all-plans">全选</button><button id="clear-plans">取消选择</button>${view === "trash" ? '<button data-plan-batch="restore">恢复所选</button>' : '<button data-plan-batch="enable" aria-label="启用所选">启用</button><button data-plan-batch="disable" aria-label="关闭所选">关闭</button><button data-plan-batch="delete" class="danger" aria-label="删除所选">删除</button>'}</div></div>` : ""}</div>${data.scheduler_error || !data.scheduler_running ? `<p class="help" role="status">${escape(data.scheduler_error || "朝夕拾记检查未运行")}</p>` : ""}<div class="plan-list">${view === "today" ? renderTodayMarkup(data) : view === "calendar" ? renderCalendarMarkup(data) : view === "runs" ? runs || '<div class="empty"><h3>暂无朝夕足迹</h3></div>' : cards || `<div class="empty"><h3>${view === "trash" ? "回收站是空的" : "暂无计划"}</h3><p>${view === "trash" ? "可恢复为停用状态。" : "导入或新建计划。"}</p></div>`}</div>`;
  const sortButton = main.querySelector("#plan-sort");
  if (sortButton) {
    const menu = main.querySelector("#plan-sort-menu");
    const closeMenu = (restoreFocus = false) => {
      menu.hidden = true; sortButton.setAttribute("aria-expanded", "false");
      if (restoreFocus) sortButton.focus();
    };
    const openMenu = () => {
      menu.hidden = false; sortButton.setAttribute("aria-expanded", "true");
      menu.querySelector('[aria-checked="true"]')?.focus();
    };
    sortButton.onclick = () => menu.hidden ? openMenu() : closeMenu(true);
    sortButton.onkeydown = (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); openMenu(); }
    };
    menu.querySelectorAll("button").forEach((button) => button.onclick = async () => {
      if (button.dataset.sortField) state.planSortField = button.dataset.sortField;
      if (button.dataset.sortDirection) state.planSortDirection = button.dataset.sortDirection;
      closeMenu(true);
      const renderSerial = state.serial + 1;
      await render();
      if (state.tab === "plans" && state.serial === renderSerial) main.querySelector("#plan-sort")?.focus();
    });
    menu.onkeydown = (e) => {
      const items = [...menu.querySelectorAll("button:not(:disabled)")];
      const index = items.indexOf(document.activeElement);
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
        e.preventDefault();
        items[e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 : (index + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
      } else if (e.key === "Escape") { e.preventDefault(); closeMenu(true); }
      else if (e.key === "Tab") closeMenu(true);
    };
    planSortMenuEvents = new AbortController();
    const signal = planSortMenuEvents.signal;
    document.addEventListener("pointerdown", (e) => { if (!sortButton.parentElement.contains(e.target)) closeMenu(); }, {signal});
    document.addEventListener("focusin", (e) => { if (!sortButton.parentElement.contains(e.target)) closeMenu(); }, {signal});
  }
  const applyPlans = async (items, operation) => {
    await act("plans_manage", {operation, patches:items.map((p)=>({id:p.id,revision:p.revision}))});
    selectedPlans.clear(); await render();
    toast({enable:"计划已启用，不补发错过的时间",disable:"计划已关闭",delete:"已移到回收站并停用，可以恢复",restore:"计划已恢复，保持停用"}[operation]);
  };
  const updateSelection = () => {
    main.querySelectorAll('[data-select-plan]').forEach((el)=>{el.checked=selectedPlans.has(el.dataset.selectPlan);el.closest('.plan-card').classList.toggle('selected',el.checked);});
    const counter=main.querySelector('#plan-selection-count');
    if (counter) counter.textContent=`已选 ${selectedPlans.size} 条`;
    const all = main.querySelector('#select-all-plans');
    if (all) { const checked = plans.length > 0 && plans.slice(0,200).every(p => selectedPlans.has(p.id)); all.textContent = checked ? '取消全选' : '全选'; all.setAttribute('aria-pressed', String(checked)); all.disabled = !plans.length; }
    main.querySelectorAll('[data-plan-batch]').forEach((el)=>el.disabled=!selectedPlans.size);
  };
  main.querySelectorAll('[data-plan-view]').forEach((b)=>b.onclick=()=>{state.planView=b.dataset.planView;state.planManage=false;selectedPlans.clear();render();window.scrollTo(0,0);});
  const runExport=main.querySelector('#run-diagnostics-export');
  if(runExport)runExport.onclick=()=>exportDiagnostics({categories:['runs','images'],scope:data.calendar_scope,cid:data.conversation_id});
  main.querySelectorAll('[data-run-job]').forEach(button=>button.onclick=()=>busy(button,async()=>{const result=await bridge.apiGet('job/'+button.dataset.runJob);if(button.isConnected)openJobDetail(result.job,result.deliveries,result.proactive || []);}));
  const allRuns=main.querySelector('#all-run-history');
  if(allRuns)allRuns.onclick=()=>{historyState.kind='runs';historyState.offset=0;historyState.status=historyState.character=historyState.query='';nav('jobs');};
  const manage=main.querySelector('#manage-plans');
  if (manage) manage.onclick=()=>{state.planManage=!state.planManage;selectedPlans.clear();render();};
  main.querySelectorAll('[data-select-plan]').forEach((el)=>el.onchange=()=>{
    const p=plans.find((p)=>p.id===el.dataset.selectPlan);
    if (!el.checked) selectedPlans.delete(p.id);
    else if (selectedPlans.size<200) selectedPlans.set(p.id,p);
    else {el.checked=false;toast('一次最多选择200条计划',true);}
    updateSelection();
  });
  if (state.planManage && view !== "runs" && view !== "calendar" && view !== "today") {
    main.querySelector('#select-all-plans').onclick=()=>{const clear=plans.slice(0,200).every(p=>selectedPlans.has(p.id)); if(clear)selectedPlans.clear();else plans.slice(0,200).forEach(p=>selectedPlans.set(p.id,p));updateSelection();if(!clear&&plans.length>200)toast('已选择前200条，可分批处理');};
    main.querySelector('#clear-plans').onclick=()=>{selectedPlans.clear();updateSelection();};
    main.querySelectorAll('[data-plan-batch]').forEach((b)=>b.onclick=()=>busy(b,()=>applyPlans([...selectedPlans.values()],b.dataset.planBatch)));
    updateSelection();
  }
  main.querySelectorAll('[data-toggle-plan]').forEach((el)=>el.onchange=()=>busy(el,async()=>{
    const p=plans.find((p)=>p.id===el.dataset.togglePlan);
    try {await applyPlans([p],el.checked?'enable':'disable');}
    catch(error){el.checked=p.enabled;throw error;}
  }));
  for (const [attribute,operation] of [['delete','delete'],['restore','restore']]) {
    main.querySelectorAll(`[data-${attribute}-plan]`).forEach((b)=>b.onclick=()=>busy(b,()=>applyPlans([plans.find((p)=>p.id===b.getAttribute(`data-${attribute}-plan`))],operation)));
  }
  main.querySelector("#new-plan").onclick = () => editPlan({}, data.bots);
  main.querySelectorAll("[data-edit-plan]").forEach((b) => b.onclick = () => editPlan(data.plans.find((p) => p.id === b.dataset.editPlan), data.bots));
  main.querySelector("#import-plans").onclick = () => {
    showDialog("导入朝夕拾记", `<div class="dialog-body"><label for="plan-file">选择 JSON 文件</label><input id="plan-file" type="file" accept=".json,application/json"><label for="import-scope">迁入会话</label><select id="import-scope"><option value="">使用文件原会话</option>${chars().flatMap((c) => c.sessions.map((s) => `<option value="${escape(s)}">${escape(c.name)} · ${escape(s)}</option>`)).join("")}</select><label class="check"><input id="import-overwrite" type="checkbox" aria-describedby="import-overwrite-help">同名覆盖（同一会话）</label><p id="import-overwrite-help" class="help">勾选后替换同名计划的内容和时间设置，并停用；未勾选则跳过。回收站同名计划会重新导入为停用状态。</p><p class="help">导入的计划先停用。会话须已绑定角色；旧文件缺少时区时使用 Asia/Shanghai。</p><p id="import-result" role="status" hidden></p><p id="import-error" role="alert" tabindex="-1" hidden></p></div>`, footer('<button id="confirm-import" class="primary">导入并停用</button>'));
    const fileInput = content.querySelector("#plan-file"), scopeInput = content.querySelector("#import-scope");
    const overwriteInput = content.querySelector("#import-overwrite"), button = content.querySelector("#confirm-import");
    const error = content.querySelector("#import-error"), status = content.querySelector("#import-result");
    let importing = false, closed = false;
    dialog.addEventListener("close", () => { closed = true; }, {once:true});
    const current = () => !closed && dialog.open && button.isConnected;
    button.onclick = async () => {
      if (importing || !current()) return;
      const file = fileInput.files[0], scope = scopeInput.value, overwrite = overwriteInput.checked;
      importing = true; error.hidden = true; status.hidden = true;
      [fileInput, scopeInput, overwriteInput, button].forEach(el => { el.disabled = true; });
      button.textContent = "正在导入…"; button.setAttribute("aria-busy", "true");
      try {
        if (!file || file.size > 2 * 1024 * 1024) throw new Error("请选择不超过 2 MB 的 JSON 文件");
        let document;
        try { document = JSON.parse(await file.text()); } catch { throw new Error("文件不是有效 JSON"); }
        if (!current()) return;
        const result = await act("plans_import", {document, scope, overwrite});
        if (!current()) return;
        const created = result.created || 0, updated = result.updated || 0, restored = result.restored || 0, skipped = result.skipped || 0;
        const summary = `新增 ${created} 条，覆盖 ${updated} 条，从回收站重新导入 ${restored} 条，跳过 ${skipped} 条`;
        if (!created && !updated && !restored) {
          status.textContent = `${summary}；已有计划未修改。需要替换时可勾选“同名覆盖”后重试。`;
          status.hidden = false;
        } else {
          dialog.close(); await render(); toast(`${summary}；本次导入的计划均已停用`);
        }
      } catch (err) {
        if (current()) { error.textContent = err.message || "导入失败，请重试"; error.hidden = false; error.focus(); }
      } finally {
        importing = false;
        if (current()) {
          [fileInput, scopeInput, overwriteInput, button].forEach(el => { el.disabled = false; });
          button.textContent = "导入并停用"; button.removeAttribute("aria-busy");
        }
      }
    };
  };
  if (view === "calendar") bindCalendarView(data);
  if (view === "today") bindTodayView(data);
  if (view === "runs") bindProactiveRetry(main,()=>render());
  main.querySelector("#export-plans").onclick = (e) => busy(e.target, async () => {
    const data = await bridge.apiGet("plans/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = "拾光手账-朝夕拾记.json";
    a.click(); setTimeout(() => URL.revokeObjectURL(url), 5000);
  });
}

function renderCalendarMarkup(data) {
  const cal = data.calendar || {};
  const scope = data.calendar_scope || "";
  const terms = cal.school_terms || {};
  const customDays = Array.isArray(cal.custom_days) ? cal.custom_days : [];
  const trips = Array.isArray(cal.trips) ? cal.trips : [];
  const holidaysText = Array.isArray(cal.holiday_ranges) && cal.holiday_ranges.length ? cal.holiday_ranges.join("\n") : Object.entries(cal.holidays || {}).map(([d, name]) => `${d}=${name}`).join("\n");
  const workdaysText = Array.isArray(cal.workday_ranges) && cal.workday_ranges.length ? cal.workday_ranges.join("\n") : Object.entries(cal.workdays || {}).map(([d, name]) => `${d}=${name}`).join("\n");
  const examRangesText = Array.isArray(terms.exam_ranges) ? terms.exam_ranges.map((r) => `${r.start}~${r.end}`).join("\n") : String(terms.exam_ranges ?? "01-04~01-17,06-22~07-07").split(",").filter(Boolean).join("\n");
  const customDaysText = customDays.map((c) => `${String(c.date || "").replace(/^lunar:/i, "农历")}|${c.name || ""}|${c.is_holiday ? "放假" : "仅情绪"}|${c.note || ""}`).join("\n");
  const tp = data.today_profile || {};
  const lunarText = tp.lunar_cn || (tp.lunar && !String(tp.lunar).startsWith("lunar:") ? tp.lunar : "");
  const activeScheme = data.active_scheme || (cal.search_schemes || []).find((s) => s.id === cal.active_search_scheme) || {name: "复用 AstrBot 全局配置", configured: !!data.tavily_configured};
  const tavilyBadge = `<button type="button" class="badge" id="btn-badge-scheme" style="cursor:pointer;background:transparent;border:1px solid var(--line);font-size:12px;display:inline-flex;align-items:center;gap:4px;vertical-align:middle" aria-label="配置联网搜索连接" title="配置联网搜索连接">${activeScheme.configured ? `已连接：${escape(activeScheme.name || "复用 AstrBot 全局配置")}` : "未配置联网搜索"}</button>`;
  const tripRows = trips.length ? trips.map((t) => `<div class="panel" style="margin-bottom:8px;padding:10px 12px;display:flex;justify-content:space-between;align-items:center;gap:12px"><div><strong>${escape(t.title)}</strong> <span class="badge">${escape(t.start_date)} ~ ${escape(t.end_date)}</span> ${t.wake_time ? `<span class="badge">起床 ${escape(t.wake_time)}</span>` : ""}${t.note || t.scope ? `<p class="help" style="margin:4px 0 0">${escape(t.note || "")}${t.scope ? ` · ${escape(t.scope)}` : ""}</p>` : ""}</div><button type="button" class="danger" data-delete-trip="${escape(t.id)}">结束/删除</button></div>`).join("") : '<p class="help">暂无行程</p>';
  return `<div class="calendar-workspace" style="display:grid;gap:16px">
    <section class="panel"><label for="calendar-scope">编辑范围</label><select id="calendar-scope"><option value="">公共默认日历</option>${chars().flatMap(c => c.sessions.map(s => `<option value="${escape(s)}" ${s === scope ? "selected" : ""}>${escape(c.name)} · ${escape(s)}</option>`)).join("")}</select><p class="help">${scope ? "仅修改本会话；法定节假日与搜索连接使用公共配置。" : "供未单独配置的会话使用；个人日历请先选择会话。"}</p></section>
    <section class="panel">
      <div class="subhead"><div><h3>今日时令 ${scope ? "" : tavilyBadge}</h3><p class="help">${escape(tp.date || "")} ${escape(tp.weekday_cn || "")}${lunarText ? `（${escape(lunarText)}）` : ""} · <strong>${escape(tp.label || "")}</strong></p></div>
      ${scope ? "" : `<div class="actions" style="display:flex;align-items:center;gap:8px">
        <button type="button" id="btn-search-scheme" class="icon-button" aria-label="管理联网搜索方案与密钥" title="管理联网搜索方案与密钥">${icon("key")}</button>
        <input id="cal-sync-year" type="number" min="2024" max="2035" value="${new Date().getFullYear()}" style="width:84px" aria-label="同步年份">
        <button type="button" id="btn-sync-year">同步全年节假日</button>
      </div>`}</div>
      ${tp.mood_note ? `<p class="help">${escape(tp.mood_note)}</p>` : ""}
      ${tp.custom_notes?.length ? `<p class="help">今日纪念：${escape(tp.custom_notes.join("；"))}</p>` : ""}
    </section>
    <section class="panel">
      <h3>特殊行程</h3>
      <div id="trip-list">${tripRows}</div>
      <details style="margin-top:10px"><summary>新增行程</summary>
        <div class="form-row" style="margin-top:8px">
          <div><label for="trip-title">行程名称</label><input id="trip-title" placeholder="例如：桂林阳朔三日游"></div>
          <div><label for="trip-wake">起床时间（留空照常）</label><input id="trip-wake" type="time" step="60"></div>
        </div>
        <div class="form-row">
          <div><label for="trip-start">开始日期</label><input id="trip-start" type="date" value="${escape(tp.date || "")}"></div>
          <div><label for="trip-end">结束日期</label><input id="trip-end" type="date" value="${escape(tp.date || "")}"></div>
        </div>
        <label for="trip-note">情境备注</label><input id="trip-note" placeholder="例如：和哥哥一起去阳朔玩，早上坐竹筏看漓江">
        <div class="actions" style="margin-top:8px"><button type="button" id="btn-add-trip" class="primary">保存特殊行程</button></div>
      </details>
    </section>
    <section class="panel">
      <h3>时令日历</h3>
      <details><summary>填写格式与示例</summary><div class="help">
        <p>寒暑假：MM-DD 表示每年重复，YYYY-MM-DD 仅用于该年；考试周每行一个 MM-DD~MM-DD 区间，也可用逗号分隔。</p>
        <p>纪念日每行：<code>日期|名称|放假 或 仅情绪|备注</code>。日期支持 MM-DD、YYYY-MM-DD、农历MM-DD，例如 <code>10-01|纪念日|仅情绪|一起庆祝</code>。</p>
        <p>放假与补班每行：<code>YYYY-MM-DD=名称</code> 或 <code>YYYY-MM-DD~YYYY-MM-DD=名称</code>。</p>
        <p>也可在聊天中登记个人日历或行程。行程结束次日恢复原作息，结束满 3 天清理；往年过期的节假日与调休会自动清理。</p>
      </div></details>
      <div class="form-row">
        <div><label for="term-winter-start">寒假开始</label><input id="term-winter-start" value="${escape(terms.winter_start ?? "01-18")}"></div>
        <div><label for="term-winter-end">寒假结束</label><input id="term-winter-end" value="${escape(terms.winter_end ?? "02-25")}"></div>
      </div>
      <div class="form-row">
        <div><label for="term-summer-start">暑假开始</label><input id="term-summer-start" value="${escape(terms.summer_start ?? "07-08")}"></div>
        <div><label for="term-summer-end">暑假结束</label><input id="term-summer-end" value="${escape(terms.summer_end ?? "08-31")}"></div>
      </div>
      <label for="term-exams">考试周</label>
      <textarea id="term-exams" rows="2" class="mono">${escape(examRangesText)}</textarea>
      <label for="cal-custom-days">节日与纪念日</label>
      <textarea id="cal-custom-days" rows="6" class="mono">${escape(customDaysText)}</textarea>
      <div class="form-row">
        <div><label for="cal-holidays">法定放假</label><textarea id="cal-holidays" rows="6" class="mono" ${scope ? "readonly" : ""}>${escape(holidaysText)}</textarea></div>
        <div><label for="cal-workdays">调休补班</label><textarea id="cal-workdays" rows="6" class="mono" ${scope ? "readonly" : ""}>${escape(workdaysText)}</textarea></div>
      </div>
      <div class="actions" style="margin-top:12px"><button type="button" id="btn-save-calendar" class="primary">保存时令日历</button></div>
    </section>
  </div>`;
}

function openSearchSchemesDialog(data) {
  let cal = data.calendar || {};
  let schemes = structuredClone(cal.search_schemes || []);
  if (!schemes.some((s) => s.id === "default_astrbot")) {
    schemes.unshift({
      id: "default_astrbot",
      name: "复用 AstrBot 全局配置",
      api_url: "https://api.tavily.com/search",
      api_key: "",
      is_builtin: true,
      configured: !!data.tavily_configured,
    });
  }
  let activeId = cal.active_search_scheme || "default_astrbot";
  let selectedId = activeId;
  let isCreating = false;
  let viewSerial = 0;
  dialog.addEventListener("close", () => {
    viewSerial++;
    const input = content.querySelector("#scheme-input-key");
    if (input) input.value = "";
  }, {once: true});
  let newSchemeDraft = {
    name: "",
    api_url: "https://api.tavily.com/search",
    api_key: "",
  };

  function renderSchemeDialog() {
    viewSerial++;
    const previousKey = content.querySelector("#scheme-input-key");
    if (previousKey) previousKey.value = "";
    let cur = isCreating
      ? newSchemeDraft
      : schemes.find((s) => s.id === selectedId) || schemes[0];
    if (!isCreating && cur) selectedId = cur.id;

    const optionsHtml =
      schemes
        .map(
          (s) =>
            `<option value="${escape(s.id)}" ${!isCreating && s.id === selectedId ? "selected" : ""}>${escape(s.name)}${s.id === activeId ? " (当前启用)" : ""}${s.is_builtin ? " · 内置" : ""}</option>`,
        )
        .join("") +
      `<option value="__new__" ${isCreating ? "selected" : ""}>+ 新增连接…</option>`;

    const isBuiltin = !isCreating && cur.is_builtin;
    const isCurActive = !isCreating && cur.id === activeId;
    const keyVal = isCreating ? newSchemeDraft.api_key : "";
    const placeholder = isBuiltin
      ? cur.configured
        ? "已配置，点击眼睛查看 AstrBot 全局密钥"
        : "未在 AstrBot 全局配置中检测到密钥"
      : cur.configured ? "已保存，留空保留；点击眼睛核对" : "tvly-key1, tvly-key2（支持多个 Key，逗号分隔）";

    const bodyHtml = `
      <div class="dialog-body" style="padding:16px 20px">
        <p class="help" style="margin:0 0 16px">切换连接或关闭会舍弃未保存的修改。保存连接后可单独启用。</p>
        <div style="display:flex;align-items:flex-end;gap:10px;margin-bottom:16px">
          <div style="flex:1">
            <label for="scheme-select" style="font-weight:600">选择连接</label>
            <select id="scheme-select" style="width:100%">${optionsHtml}</select>
          </div>
          <button type="button" id="btn-quick-add-scheme" ${isCreating ? "disabled" : ""}>+ 新增连接</button>
        </div>

        <div class="panel" style="padding:16px;background:var(--bg);border:1px solid var(--line);border-radius:10px;margin-bottom:0">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px">
            <h4 style="margin:0">${isCreating ? "新增连接" : escape(cur.name || "连接配置详情")}</h4>
            ${!isCreating ? (isCurActive ? '<span class="badge" style="color:var(--accent);border-color:var(--accent);font-weight:600">● 当前启用</span>' : '<span class="badge">备用连接</span>') : '<span class="badge">新连接</span>'}
          </div>

          <div class="form-row">
            <div>
              <label for="scheme-input-name">连接名称</label>
              <input id="scheme-input-name" ${isBuiltin ? "readonly disabled" : ""} value="${escape(isCreating ? newSchemeDraft.name : cur.name || "")}" placeholder="例如：Tavily 官方、自建代理">
            </div>
            <div>
              <label for="scheme-input-url">API 搜索地址</label>
              <input id="scheme-input-url" ${isBuiltin ? "readonly disabled" : ""} value="${escape(isCreating ? newSchemeDraft.api_url : cur.api_url || "https://api.tavily.com/search")}" placeholder="https://api.tavily.com/search">
            </div>
          </div>

          <label for="scheme-input-key" style="margin-top:10px">API Key ${isBuiltin ? "（跟随 AstrBot 全局配置）" : "（支持多 Key，逗号分隔）"}</label>
          <div class="key-summary">
            <input id="scheme-input-key" type="password" ${isBuiltin ? "readonly" : ""} value="${escape(isCreating ? newSchemeDraft.api_key : keyVal)}" placeholder="${escape(placeholder)}">
            <button type="button" class="icon-button" id="btn-toggle-scheme-key" aria-label="显示/隐藏 Key" title="显示/隐藏 Key" ${keyVal || cur.configured ? "" : "disabled"}>${icon("eye")}</button>
          </div>
          <p class="help" style="margin:6px 0 0">
            ${
              isBuiltin
                ? "此连接跟随 AstrBot 全局 Tavily 配置。"
                : "改地址需重填密钥；地址不变时，留空保留。"
            }
          </p>

          <div style="margin-top:16px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
            <div>
              ${
                !isCreating && !isBuiltin
                  ? '<button type="button" id="btn-delete-scheme" class="danger">删除此连接</button>'
                  : isCreating
                    ? '<button type="button" id="btn-cancel-new-scheme">取消新增</button>'
                    : ""
              }
            </div>
            <div>
              ${
                !isBuiltin
                  ? `<button type="button" id="btn-save-connection" class="button-secondary">${isCreating ? "保存新连接" : "保存连接"}</button>`
                  : ""
              }
            </div>
          </div>
        </div>
      </div>
    `;

    showDialog(
      "联网搜索连接",
      bodyHtml,
      footer('<button type="button" data-close>取消</button><button type="button" id="btn-save-search-config" class="primary">启用此连接</button>'),
    );

    const nameInput = content.querySelector("#scheme-input-name");
    const urlInput = content.querySelector("#scheme-input-url");
    const keyInput = content.querySelector("#scheme-input-key");
    const toggleKeyBtn = content.querySelector("#btn-toggle-scheme-key");
    let keyEdited = false, keySerial = 0;
    const currentForm = () => ({
      id: selectedId, name: nameInput.value.trim(), api_url: urlInput.value.trim(),
      api_key: keyEdited ? keyInput.value.trim() : "",
    });
    const hasEdits = () => !isBuiltin && (isCreating || nameInput.value.trim() !== cur.name || urlInput.value.trim() !== cur.api_url || (keyEdited && !!keyInput.value.trim()));
    keyInput.addEventListener("input", () => {
      keyEdited = true; keySerial++;
      toggleKeyBtn.disabled = !keyInput.value && (!cur.configured || urlInput.value.trim() !== cur.api_url);
    });
    urlInput.addEventListener("input", () => {
      keySerial++; keyEdited = false; keyInput.value = ""; keyInput.type = "password";
      toggleKeyBtn.innerHTML = icon("eye");
      toggleKeyBtn.disabled = !cur.configured || urlInput.value.trim() !== cur.api_url;
    });
    toggleKeyBtn.onclick = () => busy(toggleKeyBtn, async () => {
      if (keyInput.type === "text") {
        keyInput.type = "password";
        if (!keyEdited) keyInput.value = "";
        toggleKeyBtn.innerHTML = icon("eye"); return;
      }
      if (!keyEdited) {
        if (!cur.configured || urlInput.value.trim() !== cur.api_url) return;
        const requestSerial = ++keySerial, requestView = viewSerial;
        const result = await act("calendar_scheme_reveal", {id: cur.id, api_url: cur.api_url, revision: cal.revision});
        if (requestSerial !== keySerial || requestView !== viewSerial || !dialog.open || !keyInput.isConnected || keyEdited || urlInput.value.trim() !== cur.api_url) return;
        keyInput.value = result.value || "";
      }
      keyInput.type = "text"; toggleKeyBtn.innerHTML = icon("hidden");
    }).finally(() => {
      if (toggleKeyBtn.isConnected) toggleKeyBtn.disabled = !keyInput.value && (!cur.configured || urlInput.value.trim() !== cur.api_url);
    });

    const selectEl = content.querySelector("#scheme-select");
    selectEl.onchange = () => {
      isCreating = selectEl.value === "__new__";
      if (isCreating) newSchemeDraft = {name: "", api_url: "https://api.tavily.com/search", api_key: ""};
      else selectedId = selectEl.value;
      renderSchemeDialog();
    };
    content.querySelector("#btn-quick-add-scheme").onclick = () => {
      isCreating = true;
      newSchemeDraft = {name: "", api_url: "https://api.tavily.com/search", api_key: ""};
      renderSchemeDialog();
    };
    const cancelNewBtn = content.querySelector("#btn-cancel-new-scheme");
    if (cancelNewBtn) cancelNewBtn.onclick = () => {isCreating = false; selectedId = activeId; renderSchemeDialog();};

    const saveConnBtn = content.querySelector("#btn-save-connection");
    if (saveConnBtn) saveConnBtn.onclick = () => busy(saveConnBtn, async () => {
      const scheme = currentForm();
      if (!scheme.name || !scheme.api_url) throw new Error("请填写连接名称和 API 地址");
      if (!scheme.api_key && (isCreating || !cur.configured || scheme.api_url !== cur.api_url)) throw new Error("新增连接或修改 API 地址后，请重新填写 API Key");
      if (isCreating) scheme.id = "conn_" + Math.random().toString(36).slice(2, 10);
      const requestView = viewSerial;
      const result = await act("calendar_schemes_save", {scheme, revision: cal.revision});
      cal = result.calendar; schemes = structuredClone(cal.search_schemes); activeId = cal.active_search_scheme;
      if (dialog.open && requestView === viewSerial && saveConnBtn.isConnected) {
        selectedId = scheme.id; isCreating = false; renderSchemeDialog();
      }
      await render(); toast(`连接「${scheme.name}」已保存`);
    });

    const deleteBtn = content.querySelector("#btn-delete-scheme");
    if (deleteBtn) deleteBtn.onclick = () => busy(deleteBtn, async () => {
      const requestView = viewSerial;
      const result = await act("calendar_schemes_save", {delete_scheme_id: selectedId, revision: cal.revision});
      cal = result.calendar; schemes = structuredClone(cal.search_schemes); activeId = cal.active_search_scheme;
      if (dialog.open && requestView === viewSerial && deleteBtn.isConnected) {selectedId = activeId; renderSchemeDialog();}
      await render(); toast("连接已删除");
    });

    const saveActiveBtn = content.querySelector("#btn-save-search-config");
    saveActiveBtn.onclick = () => busy(saveActiveBtn, async () => {
      if (hasEdits()) throw new Error("请先点击「保存连接」，或切换连接舍弃尚未保存的修改");
      const targetId = selectedId, requestView = viewSerial;
      const result = await act("calendar_schemes_save", {active_search_scheme: targetId, revision: cal.revision});
      cal = result.calendar; activeId = cal.active_search_scheme;
      if (dialog.open && requestView === viewSerial && saveActiveBtn.isConnected) dialog.close();
      await render(); toast(`已启用连接方案「${schemes.find(s => s.id === targetId)?.name || "连接方案"}」`);
    });
  }

  renderSchemeDialog();
}

function bindCalendarView(data) {
  const scope = data.calendar_scope || "";
  const scopeSelect = main.querySelector("#calendar-scope");
  scopeSelect.onchange = () => {state.calendarScope = scopeSelect.value; render();};
  const schemeBtn = main.querySelector("#btn-search-scheme");
  if (schemeBtn) schemeBtn.onclick = () => openSearchSchemesDialog(data);
  const badgeBtn = main.querySelector("#btn-badge-scheme");
  if (badgeBtn) badgeBtn.onclick = () => openSearchSchemesDialog(data);

  const syncBtn = main.querySelector("#btn-sync-year");
  if (syncBtn) syncBtn.onclick = () => busy(syncBtn, async () => {
    const year = Number(main.querySelector("#cal-sync-year").value || new Date().getFullYear());
    const res = await act("calendar_sync_year", {year, use_llm: true, revision: data.calendar.revision});
    await render();
    toast(res?.tavily_searched ? `已通过联网搜索同步 ${year} 年节假日与调休安排` : `已同步 ${year} 年节假日与调休安排`);
  });
  const addTripBtn = main.querySelector("#btn-add-trip");
  if (addTripBtn) addTripBtn.onclick = () => busy(addTripBtn, async () => {
    const title = main.querySelector("#trip-title").value.trim();
    const start_date = main.querySelector("#trip-start").value.trim();
    const end_date = main.querySelector("#trip-end").value.trim();
    const wake_time = main.querySelector("#trip-wake").value.trim();
    const note = main.querySelector("#trip-note").value.trim();
    await act("trip_save", {title, start_date, end_date, wake_time, note, scope});
    await render();
    toast("特殊行程已保存");
  });
  main.querySelectorAll("[data-delete-trip]").forEach((btn) => btn.onclick = () => busy(btn, async () => {
    await act("trip_delete", {id: btn.dataset.deleteTrip, scope});
    await render();
    toast("行程已删除，日常作息已恢复");
  }));
  const saveCalBtn = main.querySelector("#btn-save-calendar");
  if (saveCalBtn) saveCalBtn.onclick = () => busy(saveCalBtn, async () => {
    const parseKV = (text) => {
      const out = {};
      for (const line of text.split(/\r?\n/)) {
        const s = line.trim();
        if (!s || !s.includes("=")) continue;
        const idx = s.indexOf("=");
        const k = s.slice(0, idx).trim(), v = s.slice(idx + 1).trim();
        if (k) out[k] = v || "节假日";
      }
      return out;
    };
    const exam_ranges = main.querySelector("#term-exams").value
      .split(/[\r\n,，]+/)
      .map((s) => s.split("=")[0].trim())
      .filter((s) => s.includes("~"))
      .join(",");
    const custom_days = [];
    for (const line of main.querySelector("#cal-custom-days").value.split(/\r?\n/)) {
      const s = line.trim();
      if (!s) continue;
      const [date, name, flag, note] = s.split("|").map((x) => (x || "").trim());
      if (date && name) custom_days.push({date, name, is_holiday: flag === "放假" || flag === "true" || flag === "1", note: note || ""});
    }
    const calendar = {
      scope, revision: data.calendar.revision,
      ...(!scope ? {holidays: parseKV(main.querySelector("#cal-holidays").value), workdays: parseKV(main.querySelector("#cal-workdays").value)} : {}),
      custom_days,
      school_terms: {
        winter_start: main.querySelector("#term-winter-start").value.trim(),
        winter_end: main.querySelector("#term-winter-end").value.trim(),
        summer_start: main.querySelector("#term-summer-start").value.trim(),
        summer_end: main.querySelector("#term-summer-end").value.trim(),
        exam_ranges,
      },
    };
    await act("calendar_save", calendar);
    await render();
    toast("时令日历已保存并生效");
  });
}

function readPlanSchedule(cron) {
  const value = {mode:"custom", time:"12:00", days:[1,2,3,4,5], day:1};
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return value;
  const [minute, hour, day, month, week] = parts;
  if (!/^\d+$/.test(minute) || !/^\d+$/.test(hour) || +minute > 59 || +hour > 23 || month !== "*") return value;
  value.time = `${hour.padStart(2,"0")}:${minute.padStart(2,"0")}`;
  if (day === "*" && week === "*") value.mode = "daily";
  else if (/^\d+$/.test(day) && +day >= 1 && +day <= 31 && week === "*") { value.mode = "monthly"; value.day = +day; }
  else if (day === "*" && /^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$/.test(week)) {
    const days = new Set();
    for (const part of week.split(',')) {
      const [from, to = from] = part.split('-').map(Number);
      if (from > to || to > 7) return value;
      for (let i = from; i <= to; i++) days.add(i % 7);
    }
    value.mode = "weekly"; value.days = [...days].sort((a,b)=>a-b);
  }
  return value;
}

function buildPlanCron(schedule) {
  if (schedule.mode === "custom") return schedule.cron.trim();
  const match = /^(\d{2}):(\d{2})$/.exec(schedule.time);
  if (!match || +match[1] > 23 || +match[2] > 59) throw new Error("请选择有效的执行时间");
  const prefix = `${+match[2]} ${+match[1]}`;
  if (schedule.mode === "daily") return `${prefix} * * *`;
  if (schedule.mode === "monthly") {
    if (!Number.isInteger(+schedule.day) || +schedule.day < 1 || +schedule.day > 31) throw new Error("请选择每月的日期");
    return `${prefix} ${+schedule.day} * *`;
  }
  if (schedule.mode !== "weekly" || !schedule.days.length || schedule.days.some(d=>!Number.isInteger(d)||d<0||d>6)) throw new Error("请至少选择一个星期");
  return `${prefix} * * ${[...new Set(schedule.days)].sort((a,b)=>a-b).join(',')}`;
}

function editPlan(p = {}, bots = []) {
  const initialCron = p.cron || "0 12 * * 1-5";
  const initial = readPlanSchedule(initialCron);
  const sessions = chars().flatMap(c=>c.sessions.map(scope=>({scope, character:c})));
  const chosen = sessions.find(s=>s.scope === p.scope) || sessions[0];
  const parts = (p.scope || chosen?.scope || '').split(':');
  const canVisual = bots.some(b=>b.id===parts[0]) && ['FriendMessage','GroupMessage'].includes(parts[1]) && /^[1-9][0-9]{4,11}$/.test(parts[2] || '');
  const recipientMode = canVisual || !chosen ? 'qq' : 'existing';
  const weekdays = [[1,'周一'],[2,'周二'],[3,'周三'],[4,'周四'],[5,'周五'],[6,'周六'],[0,'周日']];
  showDialog(p.id ? "编辑朝夕拾记" : "新建朝夕拾记", `<form id="plan-form" class="dialog-body plan-editor">
    <label for="plan-name">计划名称</label><input id="plan-name" name="name" maxlength="120" required value="${escape(p.name)}">
    <fieldset><legend>发送给谁</legend><label for="recipient-mode">接收方式</label><select id="recipient-mode"><option value="qq" ${recipientMode==='qq'?'selected':''}>填写 QQ 接收对象</option><option value="existing" ${recipientMode==='existing'?'selected':''}>选择已绑定会话</option></select>
      <div id="qq-recipient"><div class="form-row"><div><label for="plan-character">相册角色</label><select id="plan-character" required>${options(p.character_id || chosen?.character.id || chars()[0]?.id)}</select></div><div><label for="plan-bot">发送机器人</label><select id="plan-bot" required>${bots.length ? bots.map(b=>`<option value="${escape(b.id)}" ${b.id===parts[0]?'selected':''}>${escape(b.name)}</option>`).join('') : '<option value="">暂无可用 QQ 机器人</option>'}</select></div></div>
      <div class="form-row"><div><label for="plan-kind">聊天类型</label><select id="plan-kind"><option value="FriendMessage">QQ 私聊</option><option value="GroupMessage" ${parts[1]==='GroupMessage'?'selected':''}>QQ 群聊</option></select></div><div><label for="plan-number" id="plan-number-label">QQ 号</label><input id="plan-number" inputmode="numeric" pattern="[1-9][0-9]{4,11}" maxlength="12" required value="${canVisual ? escape(parts[2]) : ''}" placeholder="输入接收消息的号码"></div></div>
      <p class="help" id="recipient-summary" aria-live="polite"></p></div>
      <div id="existing-recipient"><label for="plan-scope">已绑定的角色会话</label><select id="plan-scope" required>${p.scope && !sessions.some(s=>s.scope===p.scope) ? `<option selected value="${escape(p.scope)}">原会话已解绑，请重新填写 QQ 接收对象</option>` : ""}${sessions.length ? sessions.map(s=>`<option value="${escape(s.scope)}" ${s.scope===p.scope?'selected':''}>${escape(s.character.name)} · ${escape(s.scope)}</option>`).join('') : '<option value="">暂无绑定，请填写 QQ 接收对象</option>'}</select></div>
      <p class="help">保存会绑定新接收对象并开放相册能力，原绑定保留。机器人须能向该好友或群发消息。</p>
    </fieldset>
    <fieldset id="schedule-editor"><legend>执行时间</legend><div class="form-row"><div><label for="plan-mode">重复方式</label><select id="plan-mode">${Object.entries({daily:'每天',weekly:'每周',monthly:'每月',custom:'自定义时间表达式'}).map(([v,l])=>`<option value="${v}" ${initial.mode===v?'selected':''}>${l}</option>`).join('')}</select></div><div><label for="plan-zone">时区</label><input id="plan-zone" required value="${escape(p.timezone || 'Asia/Shanghai')}" list="plan-zones"><datalist id="plan-zones"><option value="Asia/Shanghai">北京时间</option><option value="Asia/Tokyo"></option><option value="UTC"></option><option value="America/New_York"></option><option value="Europe/London"></option></datalist></div></div>
      <div id="weekly-fields"><span class="field-label" id="weekday-label">选择星期</span><div class="weekday-picker" role="group" aria-labelledby="weekday-label">${weekdays.map(([d,label])=>`<label><input type="checkbox" value="${d}" ${initial.days.includes(d)?'checked':''}><span>${label}</span></label>`).join('')}</div><div class="actions weekday-presets"><button type="button" data-days="1,2,3,4,5">周一至周五</button><button type="button" data-days="6,0">周末</button><button type="button" data-days="0,1,2,3,4,5,6">每天</button></div></div>
      <div class="form-row"><div id="monthly-fields"><label for="plan-monthday">每月日期</label><select id="plan-monthday">${Array.from({length:31},(_,i)=>`<option value="${i+1}" ${initial.day===i+1?'selected':''}>${i+1} 日</option>`).join('')}</select></div><div id="time-fields"><label for="plan-time">基准时间</label><input id="plan-time" type="time" step="60" required value="${initial.time}"></div></div>
      <div id="custom-fields"><label for="plan-cron">时间表达式</label><input id="plan-cron" value="${escape(initialCron)}" spellcheck="false" required><details><summary>表达式格式</summary><p class="help">顺序：分钟 小时 日期 月份 星期。星期 1=周一，0/7=周日；例如 <code>30 9 * * 1-5</code>。日期和星期都有限制时，任一匹配即执行。</p></details></div>
      <div class="form-row"><div><label for="plan-window-before">最多提前（分钟，0–120）</label><input id="plan-window-before" type="number" min="0" max="120" required value="${p.window_before ?? 0}"></div><div><label for="plan-window-after">最多延后（分钟，0–120）</label><input id="plan-window-after" type="number" min="0" max="120" required value="${p.window_after ?? 0}"></div></div>
      <div class="actions weekday-presets"><button type="button" data-window="0,0">准点 (0)</button><button type="button" data-window="5,5">±5 分钟</button><button type="button" data-window="10,10">±10 分钟</button><button type="button" data-window="15,15">±15 分钟</button><button type="button" data-window="10,20">-10 ~ +20 分钟</button></div>
      <div><label for="plan-day-mode">作息模式</label><select id="plan-day-mode">${Object.entries({cron:'固定规则',school_day:'上课 / 工作日',holiday_rest:'休息 / 假期',sleep_before_work:'明日早起催睡',night_owl_before_rest:'明日休息夜猫子'}).map(([v,l])=>`<option value="${v}" ${(p.day_mode||'cron')===v?'selected':''}>${l}</option>`).join('')}</select></div>
      <div><label for="plan-contact-mode">联系规则</label><select id="plan-contact-mode"><option value="adaptive" ${(p.contact_mode||"adaptive")==="adaptive"?"selected":""}>随聊天调整</option><option value="reminder" ${p.contact_mode==="reminder"?"selected":""}>按约提醒</option></select></div>
      <div><label for="plan-photo-mode">拍照方式</label><select id="plan-photo-mode">${Object.entries({auto:'按内容自动决定',always:'每次拍照',never:'仅发文字'}).map(([v,l])=>`<option value="${v}" ${(p.photo_mode||'auto')===v?'selected':''}>${l}</option>`).join('')}</select></div>
      <details><summary>作息与拍照说明</summary><p class="help">固定规则不随调休切换。上课/工作日避开寒暑假、法定假日与广西壮族三月三，调休补班日自动补位；休息/假期与其相反。催睡与夜猫子按次日是否早起安排。特殊行程可临时调整作息。随聊天调整只影响当天这一次；按约提醒保留明确约定，不因正在聊天自动取消。拍照先发文字，再异步生成照片。</p></details><p class="help" id="schedule-note"></p><div class="schedule-preview" aria-live="polite"><strong>接下来 5 次</strong><p id="schedule-summary"></p><ol id="plan-preview"></ol><p class="help">按所选时区与浮动区间预览。保存并启用才执行，不补发错过的时间。</p></div>
    </fieldset>
    <label for="plan-instruction">生活情境</label><textarea id="plan-instruction" maxlength="8000" rows="6" required>${escape(p.instruction)}</textarea>
    <label class="check"><input type="checkbox" id="plan-enabled" ${p.enabled?'checked':''}>启用此计划并按时主动聊天</label>
  </form>`, footer('<button id="cancel-plan" type="button">取消</button><button id="save-plan" class="primary" disabled>保存计划</button>'));
  const form = content.querySelector('#plan-form'), $ = id=>form.querySelector('#'+id);
  const save = content.querySelector('#save-plan');
  let previewSerial=0, timer, saving=false;
  const scheduleValue = () => ({mode:$('plan-mode').value,time:$('plan-time').value,day:+$('plan-monthday').value,days:[...form.querySelectorAll('.weekday-picker input:checked')].map(el=>+el.value).sort((a,b)=>a-b),cron:$('plan-cron').value});
  const initialValue = JSON.stringify(scheduleValue());
  const rule = () => {
    const wb = Number($('plan-window-before').value || 0), wa = Number($('plan-window-after').value || 0);
    if (!Number.isInteger(wb) || !Number.isInteger(wa) || wb < 0 || wb > 120 || wa < 0 || wa > 120) throw new Error('浮动时间区间须为 0 至 120 分钟的整数');
    const sc = $('recipient-mode').value === 'qq' ? `${$('plan-bot').value}:${$('plan-kind').value}:${$('plan-number').value}` : $('plan-scope').value;
    return {id:p.id||'',name:$('plan-name').value.trim(),cron:JSON.stringify(scheduleValue())===initialValue ? initialCron : buildPlanCron(scheduleValue()),timezone:$('plan-zone').value.trim(),window_before:wb,window_after:wa,photo_mode:$('plan-photo-mode').value,contact_mode:$('plan-contact-mode').value,day_mode:$('plan-day-mode').value,scope:sc||p.scope||''};
  };
  const targetVisibility = () => {
    const qq=$('recipient-mode').value==='qq';
    for(const [id,visible] of [['qq-recipient',qq],['existing-recipient',!qq]]) { const box=$(id); box.hidden=!visible; box.querySelectorAll('input,select').forEach(el=>el.disabled=!visible); }
    const group=$('plan-kind').value==='GroupMessage';
    $('plan-number-label').textContent=group?'QQ 群号':'QQ 号';
    $('recipient-summary').textContent=`${$('plan-bot').value || '请选择机器人'} → ${group?'群聊':'私聊'} ${$('plan-number').value || '待填写号码'}`;
  };
  const scheduleVisibility = () => {
    const mode=$('plan-mode').value;
    for(const [id,visible] of [['weekly-fields',mode==='weekly'],['monthly-fields',mode==='monthly'],['time-fields',mode!=='custom'],['custom-fields',mode==='custom']]) { const box=$(id);box.hidden=!visible;box.querySelectorAll('input,select,button').forEach(el=>el.disabled=!visible); }
    $('schedule-note').textContent=$('plan-day-mode').value!=='cron'?'按时令日历与特殊行程调整，具体时间见下方预览。':mode==='monthly'?'选 29、30、31 日时，没有该日期的月份不会执行。':mode==='weekly'?'固定规则仅按星期执行，不随节假日调休。':'';
  };
  const preview = async () => {
    const serial=++previewSerial; clearTimeout(timer); save.disabled=true;
    const list=$('plan-preview'), summary=$('schedule-summary');
    let current;
    try { current=rule(); if(!current.cron || !current.timezone) throw new Error('请填写完整的定时规则和时区'); }
    catch(error) { list.innerHTML='';summary.textContent=error.message;return; }
    list.innerHTML='';summary.textContent='正在计算生效时间…';
    timer=setTimeout(async()=>{
      try {
        const result=await act('plan_preview',current);
        if(!form.isConnected || !dialog.open || serial!==previewSerial)return;
        const formatter=new Intl.DateTimeFormat('zh-CN',{timeZone:current.timezone,year:'numeric',month:'2-digit',day:'2-digit',weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
        const details=result.preview_details || result.next_times.map(t=>({actual:t,delta_minutes:0}));
        list.innerHTML=details.map(d=>{const tag=d.delta_minutes?`（较基准${d.delta_minutes<0?`早 ${Math.abs(d.delta_minutes)}`:`晚 ${d.delta_minutes}`} 分钟）`:(current.window_before||current.window_after?'（准点）':'');const label=d.day_label?` · ${d.day_label}`:'';return `<li>${escape(formatter.format(new Date(d.actual)))}${escape(tag)}${escape(label)}</li>`;}).join('');
        const winSummary=(current.window_before||current.window_after)?` · 浮动 -${current.window_before}~+${current.window_after}分`:'';
        summary.textContent=result.next_times.length ? `${current.timezone} · ${current.cron}${winSummary}` : '此规则没有后续执行时间，请调整日期。';
        save.disabled=saving || !result.next_times.length;
      } catch(error) { if(form.isConnected && serial===previewSerial){summary.textContent=error.message || '时间预览失败，请稍后重试';list.innerHTML='';} }
    },250);
  };
  $('recipient-mode').onchange=()=>{targetVisibility();preview();};
  for(const id of ['plan-kind','plan-bot','plan-number','plan-scope'])$(id).addEventListener('input',()=>{targetVisibility();preview();});
  $('schedule-editor').addEventListener('input',()=>{scheduleVisibility();preview();});
  form.querySelectorAll('[data-days]').forEach(b=>b.onclick=()=>{const days=b.dataset.days.split(',');form.querySelectorAll('.weekday-picker input').forEach(el=>el.checked=days.includes(el.value));preview();});
  form.querySelectorAll('[data-window]').forEach(b=>b.onclick=()=>{const [wb,wa]=b.dataset.window.split(',');$('plan-window-before').value=wb;$('plan-window-after').value=wa;preview();});
  $('plan-mode').addEventListener('change',()=>{
    if($('plan-mode').value==='custom' && previousMode!=='custom') {
      try { $('plan-cron').value=buildPlanCron({...scheduleValue(),mode:previousMode}); } catch { $('plan-cron').value=''; }
    } else if(previousMode==='custom') {
      const parsed=readPlanSchedule($('plan-cron').value);
      if(parsed.mode!=='custom') {
        $('plan-time').value=parsed.time;$('plan-monthday').value=parsed.day;
        form.querySelectorAll('.weekday-picker input').forEach(el=>el.checked=parsed.days.includes(+el.value));
      }
    }
    previousMode=$('plan-mode').value;scheduleVisibility();preview();
  });
  let previousMode=initial.mode;
  content.querySelector('#cancel-plan').onclick=()=>dialog.close();
  form.onsubmit=e=>{e.preventDefault();if(!save.disabled)save.click();};
  save.onclick=async()=>{
    if(!form.reportValidity())return;
    saving=true;save.disabled=true;
    try {
      const data={id:p.id,revision:p.revision,name:$('plan-name').value,instruction:$('plan-instruction').value,enabled:$('plan-enabled').checked,...rule()};
      if($('recipient-mode').value==='qq') {
        const char=chars().find(c=>c.id===$('plan-character').value);
        data.recipient={character_id:char?.id,character_revision:char?.revision,bot_id:$('plan-bot').value,message_type:$('plan-kind').value,number:$('plan-number').value};
      } else data.scope=$('plan-scope').value;
      await act('plan',data);dialog.close();await refreshData();await render();toast('计划已保存');
    } catch(error) {toast(error.message || '保存失败',true);}
    finally {saving=false;if(form.isConnected && dialog.open)preview();}
  };
  targetVisibility();scheduleVisibility();preview();
}

function providerFields(s, recovery, maintenance) {
  const panel = main.querySelector("#site-panel"), form = main.querySelector("#settings-form");
  const fields = ["name", "base_url", "api_keys", "active_key_id", "model", "size", "reference_field", "enabled", "id"];
  const globalFields = [...form.querySelectorAll("[name]")];
  const globals = () => ({...Object.fromEntries(globalFields.map(el => [el.name, el.type === "checkbox" ? el.checked : Number(el.value)])), ...(recovery ? {plan_recovery: recovery.value()} : {}), ...(maintenance ? {memory_maintenance: maintenance.value()} : {})});
  const title = (p) => {
    if (p.name?.trim()) return p.name.trim();
    try { return new URL(p.base_url).hostname; } catch { return "未命名站点"; }
  };
  const sitesFrom = (settings) => {
    const primary = {id: "primary", name: settings.primary_name, enabled: settings.primary_enabled,
      ...Object.fromEntries(["base_url", "model", "size", "reference_field", "api_keys", "active_key_id"].map(key => [key, settings[key]]))};
    const sites = [...(settings.base_url || settings.api_key_set || settings.model ? [primary] : []), ...settings.providers];
    const order = settings.provider_order || [];
    return sites.map(p => ({...p, api_keys: (p.api_keys || []).map(k => ({...k, value: "", saved: true}))}))
      .sort((a, b) => (order.includes(a.id) ? order.indexOf(a.id) : 99) - (order.includes(b.id) ? order.indexOf(b.id) : 99));
  };
  const address = (site, value = site.base_url) => site.id === "primary" ? value.trim() : value.trim().replace(/\/+$/, "");
  const payload = (site) => Object.fromEntries(fields.map(key => [key, key === "name" ? title(site) : key === "base_url" ? address(site) : key === "api_keys"
    ? site.api_keys.map(k => ({id: k.id, name: k.name.trim(), value: k.value || ""}))
    : typeof site[key] === "string" ? site[key].trim() : site[key]]));
  const signature = (site) => JSON.stringify(payload(site));
  const connection = (site) => JSON.stringify([address(site), site.active_key_id, site.api_keys.find(k => k.id === site.active_key_id)?.value || ""]);
  let baseline = sitesFrom(s), drafts = structuredClone(baseline), saved = structuredClone(s);
  let selected = drafts.some(p => p.id === state.settingsSite) ? state.settingsSite : drafts[0]?.id;
  let preferred = s.preferred_provider, editing, saving = false, disposed = false, viewVersion = 0;
  let hideKeys = () => {}, closeKeys = () => {};
  const events = new AbortController();
  const read = () => {
    if (!editing || saving) return;
    panel.querySelectorAll("[data-field]").forEach(el => editing[el.dataset.field] = el.type === "checkbox" ? el.checked : el.value);
  };
  const dirty = (site = editing) => !!site && signature(site) !== (baseline.find(p => p.id === site.id) ? signature(baseline.find(p => p.id === site.id)) : "");
  const anyDirty = () => {
    return drafts.some(p => dirty(p)) || JSON.stringify(drafts.map(p => p.id)) !== JSON.stringify(baseline.map(p => p.id))
      || preferred !== saved.preferred_provider || globalFields.some(el => globals()[el.name] !== saved[el.name]) || !!recovery?.dirty() || !!maintenance?.dirty();
  };
  const valid = () => !disposed && panel.isConnected;
  const updateSelectors = () => {
    panel.querySelector("#site-select").innerHTML = drafts.map(p => `<option value="${escape(p.id)}">${escape(title(p))}${p.enabled ? "" : " · 停用"}</option>`).join("") || '<option value="">尚未添加站点</option>';
    panel.querySelector("#site-select").value = selected || "";
    const enabled = drafts.filter(p => p.enabled);
    const select = main.querySelector("#preferred-provider");
    select.innerHTML = '<option value="" disabled>请选择首选站点</option>' + enabled.map(p => `<option value="${escape(p.id)}">${escape(title(p))}</option>`).join("");
    select.value = enabled.some(p => p.id === preferred) ? preferred : "";
    panel.querySelector("#add-provider").disabled = saving || drafts.length >= 5;
    const eligible = drafts.filter(p => p.enabled && p.base_url.trim() && p.model.trim() && p.api_keys.some(k => k.id === p.active_key_id && (k.saved || k.value)));
    const effectivePreferred = enabled.some(p => p.id === preferred) ? preferred : "primary";
    const ordered = [...eligible.filter(p => p.id === effectivePreferred), ...eligible.filter(p => p.id !== effectivePreferred)];
    main.querySelector("#site-order").textContent = (anyDirty() ? "保存后预览：" : "") + (ordered.map(title).join(" → ") || "暂无可用站点");
  };
  const updateStatus = (error = "") => {
    const changed = dirty();
    main.querySelector("#save-settings").hidden = !anyDirty();
    for (const id of ["save-site", "cancel-site"]) {
      const button = panel.querySelector("#" + id);
      button.hidden = !changed; button.disabled = saving || !changed;
    }
    const siteStatus = panel.querySelector("#site-status"), settingsStatus = main.querySelector("#settings-status");
    siteStatus.textContent = changed ? "本站有未保存修改" : "";
    settingsStatus.textContent = error || (saving ? "正在保存…" : anyDirty() ? "有未保存的更改" : "");
    siteStatus.hidden = !siteStatus.textContent; settingsStatus.hidden = !settingsStatus.textContent;
    settingsStatus.classList.toggle("error", !!error);
    updateSelectors();
  };
  const invalidateModels = (site, message = "连接已修改，请重新获取模型。") => {
    site.modelRequest = (site.modelRequest || 0) + 1; site.models = []; site.modelStatus = message;
    if (editing === site) { const button = panel.querySelector("#load-models"); if (button) button.disabled = saving; }
  };
  const drawModels = (site) => {
    const select = panel.querySelector("#site-model-list");
    const models = [...new Set([...(site.models || []), ...(site.model ? [site.model] : [])])];
    select.innerHTML = '<option value="">请选择模型</option>' + models.map((model) => `<option value="${escape(model)}">${escape(model)}</option>`).join("") + '<option value="__manual__">手动输入模型名…</option>';
    select.value = site.model || "";
    const status = panel.querySelector("#model-status");
    status.textContent = site.modelStatus || "";
    status.hidden = !status.textContent;
  };
  const drawKeys = (site) => {
    hideKeys();
    const area = panel.querySelector("#site-keys"), keyEntries = site.api_keys;
    area.innerHTML = `<label id="key-label">当前密钥 · ${keyEntries.length} 把</label><div class="key-summary"><div class="key-picker"><button type="button" id="current-key" aria-labelledby="key-label" aria-haspopup="listbox" aria-expanded="false" aria-controls="site-key-options"></button><div id="site-key-options" role="listbox" aria-label="本站密钥" hidden></div></div><button type="button" id="reveal-current-key" class="icon-button" aria-label="显示本站密钥" aria-pressed="false" title="显示本站密钥" ${keyEntries.length ? "" : "disabled"}>${icon("eye")}</button><button type="button" id="manage-keys" class="icon-button" aria-label="管理站点密钥" title="管理站点密钥">${icon("key")}</button></div>`;
    const display = area.querySelector("#current-key"), list = area.querySelector("#site-key-options"), eye = area.querySelector("#reveal-current-key");
    let revealVersion = 0, revealed = false, values = new Map();
    const active = () => valid() && display.isConnected && editing === site;
    list.innerHTML = keyEntries.map(entry => `<button type="button" role="option" aria-selected="${entry.id === site.active_key_id}" data-key-option="${escape(entry.id)}"></button>`).join("");
    const drawValues = () => {
      const current = keyEntries.find(k => k.id === site.active_key_id);
      const label = entry => entry.name + (revealed ? ` · ${values.get(entry.id) ?? "读取中…"}` : "");
      display.textContent = current ? label(current) : "尚未选择密钥";
      display.disabled = saving || !keyEntries.length;
      list.querySelectorAll("[data-key-option]").forEach(button => {
        const entry = keyEntries.find(k => k.id === button.dataset.keyOption);
        button.textContent = label(entry) + (entry.id === site.active_key_id ? " ✓" : "");
        button.setAttribute("aria-selected", String(entry.id === site.active_key_id));
        button.onclick = () => {
          if (saving) return;
          const before = connection(site); site.active_key_id = button.dataset.keyOption;
          if (connection(site) !== before) { invalidateModels(site); drawModels(site); }
          closeList(); updateStatus(); display.focus();
        };
      });
      eye.setAttribute("aria-pressed", String(revealed));
      eye.setAttribute("aria-label", revealed ? "隐藏本站密钥" : "显示本站密钥");
      eye.title = revealed ? "隐藏本站密钥" : "显示本站密钥";
      eye.innerHTML = icon(revealed ? "hidden" : "eye");
    };
    hideKeys = () => {
      revealVersion++; revealed = false; values.clear();
      if (area.isConnected) drawValues();
    };
    const closeList = () => { list.hidden = true; display.setAttribute("aria-expanded", "false"); hideKeys(); };
    closeKeys = closeList;
    const openList = (last = false) => {
      if (saving || !keyEntries.length) return;
      list.hidden = false; display.setAttribute("aria-expanded", "true");
      const options = [...list.querySelectorAll("[data-key-option]")];
      (last ? options.at(-1) : options.find(el => el.dataset.keyOption === site.active_key_id) || options[0])?.focus();
    };
    display.onclick = () => list.hidden ? openList() : closeList();
    display.onkeydown = event => {
      if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); openList(event.key === "ArrowUp"); }
      if (event.key === "Escape") { event.preventDefault(); closeList(); }
    };
    list.onkeydown = event => {
      const options = [...list.querySelectorAll("[data-key-option]")], index = options.indexOf(document.activeElement);
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
        options[next]?.focus();
      }
      if (event.key === "Escape") { event.preventDefault(); closeList(); display.focus(); }
      if (event.key === "Tab") closeList();
    };
    eye.onclick = async () => {
      if (saving) return;
      if (revealed) { hideKeys(); return; }
      revealed = true; const version = ++revealVersion; drawValues();
      await Promise.all(keyEntries.map(async entry => {
        try {
          const value = entry.value || (await act("reveal_key", {id: site.id, base_url: address(site), key_id: entry.id, settings_revision: saved.settings_revision})).value;
          if (!active() || !revealed || version !== revealVersion) return;
          values.set(entry.id, value); drawValues();
        } catch {
          if (!active() || !revealed || version !== revealVersion) return;
          values.set(entry.id, "读取失败"); drawValues(); toast("密钥读取失败，请重新显示后重试", true);
        }
      }));
    };
    drawValues();
    area.querySelector("#manage-keys").onclick = () => {
      if (saving) return;
      closeList();
      let entries = structuredClone(site.api_keys), active = site.active_key_id;
      let editingEntry = null, viewedKeyValue = null, originalValueRead = Promise.resolve(), committing = null;
      showDialog("管理站点密钥", `<div class="dialog-body key-manager"><div class="key-manager-head"><p>${escape(title(site))}</p><button type="button" id="add-key">添加密钥</button></div><div id="key-list"></div><div id="key-editor"></div><p class="help">完成后仅应用到站点草稿；取消会舍弃本次修改。</p></div>`, footer('<button type="button" id="cancel-keys">取消</button><button type="button" id="apply-keys" class="primary">完成</button>'));
      const manager = content.querySelector(".key-manager");
      const drawList = () => {
        content.querySelector("#add-key").disabled = entries.length >= 20 || !!editingEntry;
        content.querySelector("#key-list").innerHTML = entries.map((entry, i) => `<div class="key-item ${entry.id === active ? "selected" : ""}"><div class="key-item-name"><strong>${escape(entry.name || `密钥 ${i + 1}`)}</strong><p class="help">${entry.id === active ? "当前选用" : "备用"} · ${entry.saved ? "已保存" : "待保存"}</p></div><div class="actions"><button type="button" class="icon-button" data-key-select="${escape(entry.id)}" aria-label="选用密钥 ${i + 1}" title="选用" aria-pressed="${entry.id === active}" ${editingEntry ? "disabled" : ""}>${icon("check")}</button><button type="button" class="icon-button" data-key-edit="${escape(entry.id)}" aria-label="编辑密钥 ${i + 1}" title="编辑" ${editingEntry ? "disabled" : ""}>${icon("edit")}</button><button type="button" class="icon-button danger" data-key-remove="${escape(entry.id)}" aria-label="删除密钥 ${i + 1}" title="删除" ${editingEntry ? "disabled" : ""}>${icon("remove")}</button></div><div class="key-summary key-content"><input type="password" readonly autocomplete="off" aria-label="密钥 ${i + 1} 内容" placeholder="点击眼睛核对密钥内容"><button type="button" class="icon-button" data-key-reveal="${escape(entry.id)}" aria-label="显示密钥 ${i + 1} 内容" title="显示密钥内容" aria-pressed="false" ${editingEntry ? "disabled" : ""}>${icon("eye")}</button></div></div>`).join("") || '<p class="key-empty">还没有密钥，添加后即可选用。</p>';
        content.querySelectorAll("[data-key-reveal]").forEach((button, i) => {
          const entry = entries.find((k) => k.id === button.dataset.keyReveal);
          const input = button.previousElementSibling;
          button.onclick = () => busy(button, async () => {
            const visible = input.type === "text";
            const value = visible ? "" : entry.value || (await act("reveal_key", {id: site.id, base_url: address(site), key_id: entry.id, settings_revision: saved.settings_revision})).value;
            if (!valid() || !button.isConnected || !dialog.open || editing !== site || !entries.includes(entry)) return;
            input.value = value; input.type = visible ? "password" : "text";
            button.setAttribute("aria-pressed", String(!visible));
            button.setAttribute("aria-label", `${visible ? "显示" : "隐藏"}密钥 ${i + 1} 内容`);
            button.innerHTML = icon(visible ? "eye" : "hidden");
          });
        });
        content.querySelectorAll("[data-key-select]").forEach((button) => button.onclick = () => { active = button.dataset.keySelect; drawList(); content.querySelector(`[data-key-select="${CSS.escape(active)}"]`).focus(); });
        content.querySelectorAll("[data-key-edit]").forEach((button) => button.onclick = () => openEditor(entries.find((k) => k.id === button.dataset.keyEdit)));
        content.querySelectorAll("[data-key-remove]").forEach((button) => button.onclick = () => {
          entries = entries.filter((k) => k.id !== button.dataset.keyRemove);
          if (active === button.dataset.keyRemove) active = entries[0]?.id || "";
          drawList(); content.querySelector("#add-key").focus();
        });
      };
      const commitEntry = async () => {
        if (!editingEntry) return true;
        if (committing === editingEntry) return false;
        const entry = editingEntry, form = content.querySelector("#key-edit-form"), button = content.querySelector("#apply-keys");
        committing = entry; button.disabled = true; button.setAttribute("aria-busy", "true");
        try {
          // A saved key edited in an earlier dialog still needs its original value
          // as private comparison evidence; a slow read cannot commit a closed form.
          if (entry.saved && form.dataset.keyEdited === "true") await originalValueRead;
          if (!valid() || !form.isConnected || !dialog.open || editingEntry !== entry || editing !== site) return false;
          form.elements.key_value.setCustomValidity(!entry.saved && !form.elements.key_value.value.trim() ? "请填写本站密钥" : "");
          if (!form.reportValidity()) return false;
          entry.name = form.elements.key_name.value.trim() || `密钥 ${entries.length + 1}`;
          if (form.dataset.keyEdited === "true") {
            const entered = form.elements.key_value.value.trim();
            entry.value = entry.saved && entered === viewedKeyValue ? "" : entered;
          }
          const index = entries.findIndex(k => k.id === entry.id);
          if (index < 0) entries.push(entry); else entries[index] = entry;
          active ||= entry.id;
          editingEntry = null; viewedKeyValue = null;
          content.querySelector("#key-editor").innerHTML = ""; drawList();
          return true;
        } finally {
          if (committing === entry) {
            committing = null;
            if (button.isConnected) { button.disabled = false; button.removeAttribute("aria-busy"); }
          }
        }
      };
      const openEditor = (entry) => {
        editingEntry = entry ? {...entry} : {id: "key_" + crypto.randomUUID(), name: "", value: "", saved: false};
        viewedKeyValue = null; originalValueRead = Promise.resolve();
        content.querySelector("#key-editor").innerHTML = `<form id="key-edit-form" class="key-edit-form"><h3>${entry ? "编辑密钥" : "添加密钥"}</h3><label for="key-edit-name">密钥名称</label><input id="key-edit-name" name="key_name" maxlength="80" placeholder="给这把密钥起个名字"><label for="key-edit-value">密钥内容</label><input id="key-edit-value" name="key_value" type="text" autocomplete="new-password" maxlength="2000" ${editingEntry.saved ? "" : "required"} placeholder="${editingEntry.saved ? "留空保留已保存密钥" : "填写本站密钥"}">${editingEntry.saved ? '<p class="help">留空保留原密钥；替换内容后，恢复旧任务需核对原密钥。</p>' : ""}<div class="actions"><button type="button" id="cancel-key-edit">取消编辑</button><button type="submit">应用编辑</button></div></form>`;
        const form = content.querySelector("#key-edit-form");
        form.elements.key_name.value = editingEntry.name; form.elements.key_value.value = editingEntry.value || "";
        let valueVersion = 0;
        form.elements.key_value.oninput = () => { valueVersion++; form.dataset.keyEdited = "true"; form.elements.key_value.setCustomValidity(""); };
        if (editingEntry.saved) {
          const entry = editingEntry, version = valueVersion;
          if (!entry.value) form.elements.key_value.placeholder = "正在读取已保存密钥…";
          originalValueRead = act("reveal_key", {id: site.id, base_url: address(site), key_id: entry.id, settings_revision: saved.settings_revision}).then((result) => {
            if (valid() && form.isConnected && dialog.open && editing === site && editingEntry === entry) {
              viewedKeyValue = result.value;
              if (!entry.value && version === valueVersion) form.elements.key_value.value = result.value;
            }
          }).catch(() => { if (valid() && form.isConnected) toast("密钥读取失败，可取消后重试；留空仍保留原密钥。", true); })
            .finally(() => { if (valid() && form.isConnected) form.elements.key_value.placeholder = "留空保留已保存密钥"; });
        }
        form.onsubmit = async event => { event.preventDefault(); if (await commitEntry()) content.querySelector("#add-key").focus(); };
        content.querySelector("#cancel-key-edit").onclick = () => {
          editingEntry = null; viewedKeyValue = null; committing = null;
          const button = content.querySelector("#apply-keys"); button.disabled = false; button.removeAttribute("aria-busy");
          content.querySelector("#key-editor").innerHTML = ""; drawList(); content.querySelector("#add-key").focus();
        };
        drawList(); form.elements.key_name.focus();
      };
      content.querySelector("#add-key").onclick = () => openEditor();
      content.querySelector("#cancel-keys").onclick = () => dialog.close();
      content.querySelector("#apply-keys").onclick = async () => {
        if (!await commitEntry() || !valid() || editing !== site || !area.isConnected || !dialog.open) return;
        const before = connection(site);
        site.api_keys = entries; site.active_key_id = active;
        if (connection(site) !== before) { invalidateModels(site); drawModels(site); }
        dialog.close(); drawKeys(site); updateStatus(); panel.querySelector("#manage-keys").focus();
      };
      dialog.addEventListener("close", () => {
        if (manager.isConnected) { manager.replaceChildren(); if (area.isConnected) panel.querySelector("#manage-keys").focus(); }
        entries = []; editingEntry = null; viewedKeyValue = null;
      }, {once: true});
      drawList();
    };
  };
  const draw = () => {
    hideKeys(); viewVersion++;
    state.settingsSite = selected;
    editing = drafts.find(p => p.id === selected);
    const site = editing;
    if (site?.modelStatus === "正在获取模型…") site.modelStatus = "";
    const editor = panel.querySelector("#site-editor");
    updateStatus();
    if (!site) { panel.querySelector("#site-controls").innerHTML = ""; editor.innerHTML = '<p class="help">添加一个图片站点开始配置。</p>'; return; }
    const index = drafts.indexOf(site);
    editor.innerHTML = `<div class="form-row"><div><label for="site-name">站点名称</label><input id="site-name" data-field="name" maxlength="120" placeholder="留空时使用域名"></div><div><label for="site-url">API 基础地址</label><input id="site-url" data-field="base_url" type="url" required placeholder="https://example.com/v1"></div></div><div id="site-keys"></div><div class="site-model-head"><div class="actions"><label for="site-model-list">图片模型</label>${helpTip("模型选择说明", "填写地址和密钥后获取模型，选择支持图片生成的模型；也可手动填写模型名称。")}</div><button type="button" id="load-models">获取模型</button></div><select id="site-model-list"></select><p class="help" id="model-status" role="status" aria-live="polite"></p><label for="site-model">模型名称</label><input id="site-model" data-field="model" placeholder="选择或手动填写"><label class="check"><input data-field="enabled" type="checkbox">启用此站点</label><details class="site-advanced"><summary>更多连接选项</summary><div class="form-row"><div><label for="site-size">图片尺寸</label><input id="site-size" data-field="size"></div><div><label for="site-field">多参考图字段</label><select id="site-field" data-field="reference_field"><option>image[]</option><option>image</option></select></div></div><p class="help">使用 Images generations/edits 接口，基础地址通常以 /v1 结尾。</p></details>`;
    panel.querySelector("#site-controls").innerHTML = `<button type="button" id="site-up" ${index === 0 ? "disabled" : ""}>顺序提前</button><button type="button" id="site-down" ${index === drafts.length - 1 ? "disabled" : ""}>顺序延后</button><button type="button" id="remove-provider" class="danger">移除站点</button>`;
    drawKeys(site);
    editor.querySelectorAll("[data-field]").forEach((el) => {
      if (el.type === "checkbox") el.checked = !!site[el.dataset.field];
      else el.value = site[el.dataset.field] || "";
      el.addEventListener("input", () => {
        if (saving) return;
        const before = address(site);
        read();
        if (el.dataset.field === "base_url" && before !== address(site)) {
          const original = baseline.find(p => p.id === site.id && address(p) === address(site));
          site.api_keys = original ? structuredClone(original.api_keys) : []; site.active_key_id = original?.active_key_id || "";
          invalidateModels(site, original ? "" : "地址已修改，请添加此站点的密钥。");
          drawKeys(site); drawModels(site);
        }
        if (el.dataset.field === "model") drawModels(site);
        updateStatus();
      });
    });
    drawModels(site);
    panel.querySelector("#site-model-list").onchange = (event) => {
      if (saving) return;
      if (event.target.value === "__manual__") { panel.querySelector("#site-model").focus(); return; }
      site.model = event.target.value; panel.querySelector("#site-model").value = site.model;
      updateStatus();
    };
    panel.querySelector("#load-models").onclick = async () => {
      if (saving) return;
      read();
      const query = {id: site.id, base_url: site.base_url.trim(), key_id: site.active_key_id, api_key: site.api_keys.find(k => k.id === site.active_key_id)?.value || "", clear_api_key: !site.active_key_id};
      const before = connection(site), version = viewVersion, requestNumber = site.modelRequest = (site.modelRequest || 0) + 1;
      const button = panel.querySelector("#load-models"); button.disabled = true;
      site.modelStatus = "正在获取模型…"; drawModels(site);
      const unchanged = () => valid() && editing === site && viewVersion === version && site.modelRequest === requestNumber && before === connection(site);
      try {
        const result = await act("models", query);
        if (!unchanged()) return;
        site.models = result.models;
        site.modelStatus = result.models.length ? `已获取 ${result.models.length} 个模型` : "站点未返回模型，可手动填写。";
      } catch (error) {
        if (!unchanged()) return;
        site.modelStatus = error.message || "获取模型失败，请检查地址和密钥。";
      } finally { if (unchanged()) button.disabled = saving; }
      if (unchanged()) drawModels(site);
    };
    panel.querySelector("#remove-provider").onclick = () => {
      if (saving) return;
      drafts = drafts.filter(p => p.id !== site.id);
      if (preferred === site.id) preferred = "primary";
      selected = drafts[Math.min(index, drafts.length - 1)]?.id;
      draw(); panel.querySelector("#site-select").focus();
    };
    for (const [id, delta] of [["site-up", -1], ["site-down", 1]]) {
      panel.querySelector("#" + id).onclick = () => {
        if (saving) return;
        read(); [drafts[index], drafts[index + delta]] = [drafts[index + delta], drafts[index]];
        draw(); panel.querySelector("#site-select").focus();
      };
    }
  };
  panel.innerHTML = `<div class="subhead"><div class="actions"><h3>图片站点</h3>${helpTip("站点保存方式", "保存站点仅保存本站；保存设置会提交全部修改。切换查看会保留草稿。")}</div><button type="button" id="add-provider">添加站点</button></div><p id="site-status" class="help" role="status" aria-live="polite"></p><label for="site-select">选择站点</label><select id="site-select"></select><div id="site-editor"></div><div class="actions site-actions"><div id="site-controls" class="actions"></div><div class="actions"><button type="button" id="cancel-site" title="放弃本站未保存修改" hidden>取消保存</button><button type="button" id="save-site" hidden>保存站点</button></div></div>`;
  const validateSite = (site) => {
    let selector = "", message = "";
    try { const url = new URL(site.base_url); if (url.protocol !== "https:" || url.username || url.password || url.hash || url.search) throw new Error(); }
    catch { selector = "#site-url"; message = "请填写公开 HTTPS 基础地址，不含凭据、查询参数或片段"; }
    if (!message && site.enabled && !site.model.trim()) { selector = "#site-model"; message = "启用站点前请填写模型名称"; }
    if (!message && (site.api_keys.some(k => !k.value && !k.saved) || site.enabled && !site.api_keys.some(k => k.id === site.active_key_id && (k.saved || k.value)))) {
      selector = "#manage-keys"; message = "启用站点前请填写并选用本站密钥";
    }
    if (message) {
      if (selected !== site.id) { selected = site.id; draw(); }
      panel.querySelector(selector).focus(); throw new Error(`${title(site)}：${message}`);
    }
  };
  const fullPayload = () => {
    const primary = drafts.find(p => p.id === "primary");
    const first = primary ? payload(primary) : {base_url: "", api_keys: [], active_key_id: "", model: ""};
    return {...Object.fromEntries(["base_url", "api_keys", "active_key_id", "model", "size", "reference_field"].filter(k => k in first).map(k => [k, first[k]])),
      primary_name: primary ? title(primary) : "", primary_enabled: !!primary?.enabled,
      providers: drafts.filter(p => p.id !== "primary").map(payload), provider_order: drafts.map(p => p.id), preferred_provider: preferred, ...globals()};
  };
  const save = async (siteOnly = false) => {
    if (saving || disposed || dialog.open || settingsWriteBusy || assistantSettingsSession?.saving || recovery?.saving || maintenance?.saving) return;
    read();
    const target = siteOnly ? editing : null;
    try {
      for (const site of target ? [target] : drafts) validateSite(site);
      if (!siteOnly) { for (const el of globalFields) if (!el.reportValidity()) { el.focus(); return; } recovery?.validate(); maintenance?.validate(); }
    } catch (error) { updateStatus(error.message); toast(error.message, true); return; }
    const data = siteOnly ? {site: payload(target)} : fullPayload();
    const oldPreferred = saved.preferred_provider;
    saving = true; hideKeys(); recovery?.hide(); maintenance?.hide(); viewVersion++;
    const controls = [...form.querySelectorAll("input, select, button"), main.querySelector("#save-settings")];
    const disabled = controls.map(el => [el, el.disabled]); controls.forEach(el => el.disabled = true);
    const writeButton = main.querySelector(siteOnly ? "#save-site" : "#save-settings"); writeButton.setAttribute("aria-busy", "true");
    updateStatus();
    try {
      const result = await act(siteOnly ? "save_provider" : "settings", {...data, settings_revision: saved.settings_revision});
      if (!valid()) return;
      const normalized = sitesFrom(result);
      if (siteOnly) {
        const replacement = normalized.find(p => p.id === target.id);
        if (!replacement) throw new Error("保存回执缺少本站，请刷新后核对");
        Object.assign(target, replacement);
        if (preferred === oldPreferred) preferred = result.preferred_provider;
      } else {
        for (const site of drafts) Object.assign(site, normalized.find(p => p.id === site.id));
        preferred = result.preferred_provider;
        for (const el of globalFields) { if (el.type === "checkbox") el.checked = result[el.name]; else el.value = result[el.name]; }
        recovery?.accept(result.plan_recovery); maintenance?.accept(result.memory_maintenance);
      }
      baseline = normalized; saved = structuredClone(result); state.data.settings = structuredClone(result);
      // Keep this editor, scroll position, details and focus. Only normalized fields change.
      if (editing && (!siteOnly || editing === target)) {
        panel.querySelectorAll("[data-field]").forEach(el => { if (el.type === "checkbox") el.checked = editing[el.dataset.field]; else el.value = editing[el.dataset.field] || ""; });
      }
      toast(siteOnly ? "本站已保存" : "设置已保存");
    } catch (error) {
      if (valid()) { updateStatus(error.message || "保存失败，修改已保留"); toast(error.message || "保存失败，修改已保留", true); }
      return;
    } finally {
      saving = false;
      for (const [el, wasDisabled] of disabled) if (el.isConnected) el.disabled = wasDisabled;
      writeButton.removeAttribute("aria-busy");
      if (valid()) {
        // A late models read is invalidated by the write; its button can be used again.
        const modelsButton = panel.querySelector("#load-models"); if (modelsButton) modelsButton.disabled = false;
        if (editing) {
          if (editing.modelStatus === "正在获取模型…") editing.modelStatus = "";
          drawKeys(editing); drawModels(editing);
        }
        for (const id of ["save-site", "cancel-site"]) { const button = panel.querySelector("#" + id); button.hidden = !dirty(); button.disabled = !dirty(); }
        updateSelectors();
      }
    }
    updateStatus();
  };
  panel.querySelector("#save-site").onclick = () => save(true);
  panel.querySelector("#cancel-site").onclick = () => {
    if (saving || !editing) return;
    hideKeys();
    const original = baseline.find(p => p.id === selected), index = drafts.indexOf(editing);
    if (original) drafts[index] = structuredClone(original);
    else {
      drafts.splice(index, 1);
      if (preferred === selected) preferred = saved.preferred_provider;
      selected = drafts[Math.min(index, drafts.length - 1)]?.id;
    }
    draw(); panel.querySelector("#site-select").focus();
  };
  panel.querySelector("#site-select").onchange = event => {
    if (saving) return;
    read(); selected = event.target.value; draw();
  };
  panel.querySelector("#add-provider").onclick = () => {
    if (saving || drafts.length >= 5) return;
    read(); selected = drafts.some(p => p.id === "primary") ? "site_" + crypto.randomUUID() : "primary";
    drafts.push({id: selected, name: "", base_url: "", api_keys: [], active_key_id: "", model: "", size: "1024x1024", reference_field: "image[]", enabled: false});
    draw(); panel.querySelector("#site-name").focus();
  };
  main.querySelector("#preferred-provider").onchange = event => { if (!saving) { preferred = event.target.value; updateStatus(); } };
  form.addEventListener("input", event => { if (event.target.name && !saving) updateStatus(); });
  document.addEventListener("pointerdown", event => { if (!panel.querySelector("#site-keys")?.contains(event.target)) closeKeys(); }, {signal: events.signal});
  const controller = {
    get saving() { return saving || !!recovery?.saving || !!maintenance?.saving; }, dirty: anyDirty, saveAll: () => save(),
    acceptAssistantSave(result, expectedRevision) {
      if (!valid() || saving || !Number.isSafeInteger(expectedRevision) || saved.settings_revision !== expectedRevision || result.settings_revision !== expectedRevision + 1) return;
      saved.settings_revision = result.settings_revision;
      saved.memory_maintenance = structuredClone(result.memory_maintenance);
    },
    dispose() { disposed = true; hideKeys(); events.abort(); recovery?.dispose(); maintenance?.dispose(); drafts = []; },
  };
  draw();
  if (recovery) recovery.changed = () => updateStatus();
  if (maintenance) maintenance.changed = () => updateStatus();
  return controller;
}

function planRecoverySettings(initial, maintenance = false, directory = {}, root = main.querySelector(maintenance ? "#memory-maintenance-panel" : "#plan-recovery-panel")) {
  const prefix = maintenance ? "maintenance-" : "plan-";
  const normalize = v => maintenance ? {enabled:v?.enabled ?? false,auto_after_summary:v?.auto_after_summary ?? false,binding:structuredClone(v?.binding || {mode:"summary"}),max_calls:v?.max_calls ?? 3,unit_timeout:v?.unit_timeout ?? 300,manual_timeout:v?.manual_timeout ?? 1800} : ({extra_attempts: v?.extra_attempts ?? 2, total_timeout: v?.total_timeout ?? 300,
    assistant_enabled: v?.assistant_enabled ?? false, assistant_binding: structuredClone(v?.assistant_binding || {mode: "summary"})});
  let saved = normalize(initial), disposed = false, generation = 0, readNumber = 0, saving = false, editing = null;
  let catalogue = [], revision = null, editorRevision = null, baseline = "";
  const q = id => root.querySelector("#" + prefix + id);
  const scoped = html => maintenance ? html.replaceAll("plan-", "maintenance-") : html;
  const valid = () => !disposed && root.isConnected;
  root.innerHTML = scoped(`<h3>朝夕生成恢复</h3><div class="form-row"><div><label for="plan-extra-attempts">额外生成次数</label><input id="plan-extra-attempts" type="number" min="0" max="5" step="1" value="${saved.extra_attempts}"></div><div><label for="plan-total-timeout">本次生成总时限（秒）</label><input id="plan-total-timeout" type="number" min="30" max="1200" step="1" value="${saved.total_timeout}"></div></div>
  <label class="check"><input id="plan-assistant-enabled" type="checkbox" ${saved.assistant_enabled ? "checked" : ""}>启用助手修正朝夕失败输出</label>
  <label for="plan-assistant-connection">助手连接</label><select id="plan-assistant-connection"></select><p id="plan-connection-status" class="help" role="status"></p>
  <div class="actions"><button type="button" id="plan-new-connection">新建助手连接</button><button type="button" id="plan-edit-connection">查看/编辑连接</button><button type="button" id="plan-refresh-connections">刷新连接</button></div><div id="plan-connection-editor"></div>
  <p class="help">失败恢复可能增加模型费用；助手调用也占用额外次数。</p><details id="plan-recovery-help"><summary>调用与时效说明</summary><p class="help">首次生成、纠正、助手和聊天变化重评共用预算。设为0仅调用首次生成，不重发已发送或结果未知的消息。总时限包含请求与退避。</p><p class="help">跟随总结使用任务所属聊天的连接；缺少连接时只使用原模型的基础恢复。这里限制插件调用次数，宿主内部HTTP尝试及服务端计费次数可能不同。</p><p class="help">文本连接与拾忆回响共享；保存连接立即更新共享目录，保存设置才应用本页用途选择。</p></details>`);
  if (maintenance) {
    root.innerHTML = `<h3>记忆维护助手</h3><label class="check"><input id="maintenance-assistant-enabled" type="checkbox" ${saved.enabled ? "checked" : ""}>启用记忆维护助手</label><label class="check"><input id="maintenance-auto-after-summary" type="checkbox" ${saved.auto_after_summary ? "checked" : ""}>总结完成后自动检查实际变化</label>
    <label for="maintenance-assistant-connection">助手连接</label><select id="maintenance-assistant-connection"></select><p id="maintenance-connection-status" class="help" role="status"></p><div class="actions"><button type="button" id="maintenance-new-connection">新建助手连接</button><button type="button" id="maintenance-edit-connection">查看/编辑连接</button><button type="button" id="maintenance-refresh-connections">刷新连接</button></div><div id="maintenance-connection-editor"></div><p class="help">检查会增加模型费用。开启不会扫描旧记忆；手动范围在拾忆回响中选择。</p><details id="maintenance-recovery-help"><summary>调用与保护</summary><div class="form-row"><div><label for="maintenance-max-calls">每单元最多模型调用</label><input id="maintenance-max-calls" type="number" min="1" max="6" step="1" value="${saved.max_calls}"></div><div><label for="maintenance-unit-timeout">单元总时限（秒）</label><input id="maintenance-unit-timeout" type="number" min="30" max="1200" step="1" value="${saved.unit_timeout}"></div></div><label for="maintenance-manual-timeout">手动任务总时限（秒）</label><input id="maintenance-manual-timeout" type="number" min="30" max="7200" step="1" value="${saved.manual_timeout}"><p class="help">每单元最多6项；有完整有效原文支持时才允许最小修正。人工保护仅限制助手自动修改，不改变普通总结策略。跟随总结按任务所属聊天解析；缺少连接不会改用其他模型。保存连接立即更新共享目录，用途选择仍需保存设置。</p></details>`;
  }
  const select = q("assistant-connection");
  const notice = (text, error = false) => { q("connection-status").textContent = text; q("connection-status").classList.toggle("error", error); };
  const binding = () => select.value === "__summary__" ? {mode:"summary"} : {mode:"preset", preset_id:select.value};
  const value = () => maintenance ? {enabled:q("assistant-enabled").checked,auto_after_summary:q("auto-after-summary").checked,binding:binding(),max_calls:Number(q("max-calls").value),unit_timeout:Number(q("unit-timeout").value),manual_timeout:Number(q("manual-timeout").value)} : ({extra_attempts:Number(q("extra-attempts").value),total_timeout:Number(q("total-timeout").value),assistant_enabled:q("assistant-enabled").checked,assistant_binding:binding()});
  const editorValue = () => JSON.stringify([...q("connection-editor").querySelectorAll("input")].map(el => el === q("connection-key") ? el.dataset.revealed || !el.value ? "" : "changed" : el.value));
  const editorDirty = () => editing && editorValue() !== baseline;
  const updateEditorActions = () => { for(const id of ["connection-save","connection-cancel"]){const button=q(id);if(button)button.hidden=!editorDirty();} };
  const clearSecret = () => { const field=q("connection-key");if(field){field.value="";field.type="password";delete field.dataset.revealed;}const button=q("connection-key-toggle");if(button){button.textContent="显示";button.setAttribute("aria-pressed","false");} };
  const closeEditor = () => { generation++;clearSecret();editing=null;q("connection-editor").innerHTML="";baseline=""; };
  const dismissEditor = () => {
    if(saving||settingsWriteBusy||!editing)return;
    if(editorDirty()&&!window.confirm("连接有未保存的修改，关闭后将放弃。继续关闭？"))return;
    const trigger=q(editing.id?"edit-connection":"new-connection");closeEditor();refreshSummary();controller.changed();trigger.focus();
  };
  const refreshSummary = () => {
    q("new-connection").disabled=saving || revision===null;
    q("edit-connection").disabled=saving || !catalogue.some(p=>p.id===select.value);
    if(select.value==="__summary__")notice("按本次任务所属聊天解析总结连接。");
    else {const item=catalogue.find(p=>p.id===select.value);notice(item ? `${item.name} · ${item.model} · ${item.key_configured ? "Key 已保存" : "Key 未配置"}` : revision===null ? "正在读取共享连接…" : "所选连接已不存在，请重新选择。",revision!==null&&!item);}
  };
  const drawCatalogue = (chosen = select.value) => {
    const missing=chosen&&chosen!=="__summary__"&&!catalogue.some(p=>p.id===chosen);
    select.innerHTML='<option value="__summary__">跟随任务所属聊天的总结连接</option>' + catalogue.map(p=>`<option value="${escape(p.id)}">${escape(p.name)} · ${escape(p.model)}</option>`).join("") + (missing?`<option value="${escape(chosen)}">所选连接不可用</option>`:"");
    select.value=chosen||"__summary__";refreshSummary();
  };
  drawCatalogue((maintenance?saved.binding:saved.assistant_binding).mode==="preset"?(maintenance?saved.binding:saved.assistant_binding).preset_id:"__summary__");
  const load = async () => {
    if(saving||typeof bridge==="undefined")return;
    const request=++readNumber, epoch=directory.epoch||0;
    try {const result=await (directory.pending ||= bridge.apiGet("memory/model_catalog").finally(()=>{directory.pending=null;}));if(!valid()||request!==readNumber||epoch!==(directory.epoch||0))return;
      catalogue=result.presets.filter(p=>p.type==="chat");revision=result.models_revision;drawCatalogue();}
    catch {if(valid()&&request===readNumber)notice("共享连接读取失败，请点击刷新连接重试。",true);}
  };
  const openEditor = item => {
    if(saving||settingsWriteBusy)return;
    if(editorDirty()){notice("请先保存或取消当前连接编辑。",true);return;}
    closeEditor();editing=item||{};editorRevision=revision;
    const used=item ? `此连接被 ${Number.isSafeInteger(item.usage_count)?item.usage_count:0} 份聊天使用${item.global_roles?.includes("plan_recovery")?"，用于朝夕失败修正":item.global_roles?.includes("memory_maintenance")?"，用于记忆维护":""}。` : "保存到共享文本连接目录。";
    q("connection-editor").innerHTML=scoped(`<div class="plan-connection-editor"><div class="subhead"><h4>${item?"查看/编辑连接":"新建助手连接"}</h4><button type="button" class="close" id="plan-connection-close" aria-label="关闭连接详情">×</button></div><p class="help">${escape(used)}</p><label for="plan-connection-name">名称</label><input id="plan-connection-name" maxlength="60" value="${escape(item?.name||"")}"><label for="plan-connection-url">接口地址</label><input id="plan-connection-url" type="url" value="${escape(item?.base_url||"")}" placeholder="https://example.com/v1"><label for="plan-connection-key">API Key</label><div class="key-summary"><input id="plan-connection-key" type="password" autocomplete="off" spellcheck="false" placeholder="${item?.key_configured?"已保存，留空保留":""}"><button type="button" id="plan-connection-key-toggle" aria-pressed="false">显示</button></div><label for="plan-connection-model">模型名称</label><input id="plan-connection-model" value="${escape(item?.model||"")}"><label for="plan-connection-window">模型窗口（token）</label><input id="plan-connection-window" type="number" min="1024" max="2000000" step="1" value="${item?.max_context_tokens||32000}"><p class="help">按服务商公布的模型窗口填写。修改地址后须填写新 Key。</p><div class="actions"><button type="button" id="plan-connection-save" hidden>保存连接</button><button type="button" id="plan-connection-cancel" hidden>取消</button></div></div>`);
    baseline=editorValue();q("connection-name").focus();
    q("connection-url").oninput=()=>{generation++;clearSecret();};
    q("connection-close").onclick=dismissEditor;
    q("connection-cancel").onclick=()=>{
      if(saving||settingsWriteBusy)return;
      generation++;const values=JSON.parse(baseline);[...q("connection-editor").querySelectorAll("input")].forEach((input,i)=>input.value=values[i]);
      clearSecret();updateEditorActions();refreshSummary();controller.changed();q("connection-name").focus();
    };
    q("connection-key-toggle").onclick=async()=>{
      if(saving)return;
      const input=q("connection-key"),button=q("connection-key-toggle"),token=++generation;
      if(input.type==="text"){if(input.dataset.revealed){input.value="";delete input.dataset.revealed;}input.type="password";button.textContent="显示";button.setAttribute("aria-pressed","false");return;}
      const before=editorValue();
      if(!input.value&&editing.id&&editing.key_configured){
        if(q("connection-url").value.trim().replace(/\/+$/,"")!==editing.base_url.replace(/\/+$/,"")){notice("接口地址已改动，请填写新 Key。",true);return;}
        button.disabled=true;
        try{const result=await bridge.apiPost("memory/model_key",{id:editing.id,base_url:editing.base_url,revision:editorRevision});
          if(!valid()||token!==generation||!input.isConnected||before!==editorValue())return;
          input.value=result.value||"";
          // A read-only reveal must never become a key update or dirty draft.
          input.dataset.revealed="true";
        }catch{if(valid()&&token===generation)notice("密钥读取失败，请重试。",true);return;}
        finally{if(button.isConnected)button.disabled=false;}
      }
      if(valid()&&token===generation&&input.isConnected){input.type="text";button.textContent="隐藏";button.setAttribute("aria-pressed","true");}
    };
    q("connection-key").oninput=()=>{delete q("connection-key").dataset.revealed;};
    q("connection-save").onclick=async()=>{
      if(saving||settingsWriteBusy||!editorDirty())return;
      const input=q("connection-key"),config={type:"chat",name:q("connection-name").value.trim(),base_url:q("connection-url").value.trim(),model:q("connection-model").value.trim(),max_context_tokens:Number(q("connection-window").value)};
      if(!config.name||!config.model||!/^\d+$/.test(q("connection-window").value)||!Number.isSafeInteger(config.max_context_tokens)||config.max_context_tokens<1024||config.max_context_tokens>2000000){notice("请填写名称、模型和有效的整数窗口。",true);return;}
      try{const u=new URL(config.base_url);if(u.protocol!=="https:"||u.username||u.password||u.search||u.hash)throw Error();}catch{notice("请填写不含凭据或查询参数的公开 HTTPS 地址。",true);return;}
      if(input.value&&!input.dataset.revealed)config.api_key=input.value;
      if(!config.api_key&&(!editing.key_configured||config.base_url.replace(/\/+$/,"")!==(editing.base_url||"").replace(/\/+$/,""))){notice("请填写此地址的 API Key。",true);return;}
      const token=++generation,ids=new Set(catalogue.map(p=>p.id)),identifier=editing.id;
      // A read started before this write cannot replace its catalogue or error.
      readNumber++;directory.epoch=(directory.epoch||0)+1;directory.pending=null;
      const controls=[...new Set([...main.querySelectorAll("input, select, button"),...root.querySelectorAll("input, select, button")])],disabled=controls.map(el=>[el,el.disabled]);
      saving=true;settingsWriteBusy=true;controls.forEach(el=>el.disabled=true);q("connection-save").setAttribute("aria-busy","true");
      controller.changed();
      try{const result=await bridge.apiPost("memory/model_preset",{operation:"save",id:identifier,revision:editorRevision,config});
        if(!valid()||token!==generation)return;
        catalogue=result.presets.filter(p=>p.type==="chat");revision=result.models_revision;
        for(const receive of directory.receivers||[])receive(result);
        const added=catalogue.find(p=>!ids.has(p.id));closeEditor();drawCatalogue(added?.id||select.value);notice("连接已保存；保存设置后应用助手选择。");controller.changed();directory.onSaved?.();
      }catch{if(valid()&&token===generation)notice("连接保存失败，请核对字段或刷新连接版本后重试；输入已保留。",true);}
      finally{saving=false;settingsWriteBusy=false;for(const [el,old]of disabled)if(el.isConnected)el.disabled=old;if(valid()){q("connection-save")?.removeAttribute("aria-busy");q("edit-connection").disabled=!catalogue.some(p=>p.id===select.value);controller.changed();}}
    };
  };
  const controller={changed:()=>{},get saving(){return saving;},value,
    dirty:()=>JSON.stringify(value())!==JSON.stringify(saved)||!!editorDirty(),
    reset(){closeEditor();controller.accept(saved);controller.changed();},
    hide(){generation++;const input=q("connection-key");if(input?.dataset.revealed)clearSecret();else if(input){input.type="password";q("connection-key-toggle").textContent="显示";q("connection-key-toggle").setAttribute("aria-pressed","false");}},
    validate(){
      for(const [id,min,max]of(maintenance?[["max-calls",1,6],["unit-timeout",30,1200],["manual-timeout",30,7200]]:[["extra-attempts",0,5],["total-timeout",30,1200]])){const input=q(id),n=Number(input.value);if(!/^\d+$/.test(input.value)||!Number.isSafeInteger(n)||n<min||n>max){input.focus();throw Error(maintenance?`请填写${min}—${max}范围内的整数`:id==="extra-attempts"?"额外生成次数须为0—5的整数":"本次生成总时限须为30—1200的整数秒");}}
      if(editorDirty()){q("connection-name").focus();throw Error("请先保存或取消连接编辑");}
      if((maintenance?value().enabled:value().assistant_enabled)&&binding().mode==="preset"&&!catalogue.some(p=>p.id===select.value)){select.focus();throw Error("所选助手连接不可用，请刷新或重新选择");}
    },
    accept(v){saved=normalize(v);if(maintenance){for(const key of ["max_calls","unit_timeout","manual_timeout"])q(key.replaceAll("_","-")).value=saved[key];q("auto-after-summary").checked=saved.auto_after_summary;q("assistant-enabled").checked=saved.enabled;}else{q("extra-attempts").value=saved.extra_attempts;q("total-timeout").value=saved.total_timeout;q("assistant-enabled").checked=saved.assistant_enabled;}drawCatalogue((maintenance?saved.binding:saved.assistant_binding).mode==="preset"?(maintenance?saved.binding:saved.assistant_binding).preset_id:"__summary__");},
    dispose(){disposed=true;readNumber++;closeEditor();catalogue=[];directory.receivers=(directory.receivers||[]).filter(fn=>fn!==receive);},
  };
  root.addEventListener("input",()=>{if(!saving){generation++;updateEditorActions();controller.changed();}});
  q("connection-editor").addEventListener("keydown",event=>{if(event.key==="Escape"&&editing){event.preventDefault();event.stopPropagation();dismissEditor();}});
  select.onchange=()=>{if(!saving){generation++;clearSecret();updateEditorActions();refreshSummary();controller.changed();}};
  q("new-connection").onclick=()=>openEditor(null);
  q("edit-connection").onclick=()=>openEditor(catalogue.find(p=>p.id===select.value));
  q("refresh-connections").onclick=load;
  const receive=result=>{if(valid()){readNumber++;catalogue=result.presets.filter(p=>p.type==="chat");revision=result.models_revision;drawCatalogue();}};
  (directory.receivers ||= []).push(receive);
  load();return controller;
}

async function openMemoryAssistantSettings(options = {}) {
  if (settingsWriteBusy || settingsSession?.saving) return;
  if (assistantSettingsSession) { assistantSettingsSession.focus(); return; }
  const inline = !!options.container;
  if (inline && !options.container.isConnected) return;
  const trigger = document.activeElement, serial = state.serial;
  const parentScroll = trigger?.closest?.(".memory-dialog");
  const position = parentScroll && {top: parentScroll.scrollTop, left: parentScroll.scrollLeft};
  const panel = document.createElement(inline ? "section" : "dialog"), events = new AbortController();
  panel.id = inline ? "memory-assistant-settings-inline" : "memory-assistant-settings-dialog";
  panel.className = inline ? "assistant-settings-inline" : "assistant-settings-dialog";
  panel.setAttribute("aria-labelledby", "assistant-settings-title");
  panel.innerHTML = `<div class="assistant-settings-shell"><div class="dialog-head">${inline ? '<span id="assistant-settings-title" class="help">适用于全部聊天</span>' : '<h2 id="assistant-settings-title">助手设置</h2>'}<div class="actions"><button type="button" id="assistant-settings-save" class="primary" hidden>保存设置</button><button type="button" id="assistant-settings-cancel" hidden>取消修改</button>${inline ? "" : '<button type="button" id="assistant-settings-close" class="close" aria-label="关闭助手设置">×</button>'}</div></div><div class="dialog-body">${inline ? "" : '<p class="help">适用于全部聊天</p>'}<p id="assistant-settings-error" role="alert" hidden></p><p id="assistant-settings-loading" role="status">正在读取设置…</p><section id="memory-maintenance-panel"></section></div></div>`;
  (inline ? options.container : document.body).appendChild(panel);
  const q = id => panel.querySelector("#assistant-settings-" + id);
  let controller, baseline, revision, saving = false, disposed = false;
  const valid = () => !disposed && panel.isConnected && state.serial === serial && (options.isCurrent?.() ?? true);
  const message = text => { q("error").textContent = text; q("error").hidden = !text; };
  const update = () => {
    for (const id of ["save", "cancel"]) { q(id).hidden = !controller?.dirty(); q(id).disabled = saving || !!controller?.saving; }
    if (q("close")) q("close").disabled = saving || !!controller?.saving;
  };
  const dispose = () => {
    if (disposed) return;
    disposed = true; events.abort(); controller?.dispose();
    if (assistantSettingsSession === session) assistantSettingsSession = null;
    if (!inline && panel.open) panel.close();
    panel.remove();
    if (trigger?.isConnected) trigger.focus({preventScroll: true});
    else main.querySelector("#lj-maintenance, #memory-assistant-link, button")?.focus({preventScroll: true});
    if (parentScroll?.isConnected) { parentScroll.scrollTop = position.top; parentScroll.scrollLeft = position.left; }
  };
  const canClose = () => !(saving || controller?.saving || settingsWriteBusy) && (!controller?.dirty() || window.confirm("助手设置有未保存的修改，关闭后将放弃。继续关闭？"));
  const close = () => { if (canClose()) dispose(); };
  const focusPanel = () => {
    if (inline) {
      const group = options.container.closest("details"); if (group) group.open = true;
      panel.querySelector("input, select")?.focus({preventScroll: true});
    } else q("close").focus();
  };
  const session = {dirty: () => !!controller?.dirty(), get saving() { return saving || !!controller?.saving; }, dispose, focus: focusPanel};
  assistantSettingsSession = session;
  if (!inline) {
    panel.addEventListener("cancel", event => { event.preventDefault(); event.stopPropagation(); close(); }, {signal: events.signal});
    panel.addEventListener("close", dispose, {signal: events.signal});
  } else {
    const guardClose = event => {
      if (event.type === "cancel" ? event.target !== dialog : !event.target.closest?.("[data-close]")) return;
      if (!canClose()) { event.preventDefault(); event.stopImmediatePropagation(); }
    };
    dialog.addEventListener("cancel", guardClose, {capture: true, signal: events.signal});
    dialog.addEventListener("click", guardClose, {capture: true, signal: events.signal});
  }
  if (dialog.open) dialog.addEventListener("close", dispose, {signal: events.signal});
  panel.addEventListener("keydown", event => {
    if (inline || event.key !== "Tab") return;
    const nodes = [...panel.querySelectorAll('button, input, select, summary, [tabindex]')].filter(el => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length);
    if (!nodes.length) return;
    const index = nodes.indexOf(document.activeElement);
    if (event.shiftKey && index <= 0) { event.preventDefault(); nodes.at(-1).focus(); }
    else if (!event.shiftKey && (index < 0 || index === nodes.length - 1)) { event.preventDefault(); nodes[0].focus(); }
  }, {signal: events.signal});
  if (q("close")) q("close").onclick = close;
  q("cancel").onclick = () => {
    if (!valid() || saving || controller?.saving || settingsWriteBusy) return;
    controller.reset(); message(""); update();
  };
  q("save").onclick = async () => {
    if (!valid() || !controller?.dirty() || saving || controller.saving || settingsSession?.saving || settingsWriteBusy) return;
    try { controller.validate(); } catch (error) { message(error.message); return; }
    const payload = {settings_revision: revision, memory_maintenance: controller.value()};
    saving = true; controller.hide(); update(); message("");
    const controls = [...panel.querySelectorAll("input, select, button")], disabled = controls.map(el => [el, el.disabled]);
    const focus = document.activeElement;
    controls.forEach(el => el.disabled = true);
    try {
      const result = await act("settings", payload);
      if (!valid()) return;
      settingsSession?.acceptAssistantSave?.(result, payload.settings_revision);
      revision = result.settings_revision; baseline = structuredClone(result.memory_maintenance);
      controller.accept(baseline);
      // Only this policy belongs to this panel. Other settings drafts stay with their owner.
      if (state.data?.settings) { state.data.settings.memory_maintenance = structuredClone(baseline); state.data.settings.settings_revision = revision; }
      options.onChange?.(); toast("助手设置已保存");
    } catch (error) { if (valid()) message(error.message || "保存失败，修改已保留"); }
    finally {
      saving = false;
      for (const [el, previous] of disabled) if (el.isConnected) el.disabled = previous;
      if (valid()) { update(); if (focus?.isConnected && !focus.hidden) focus.focus(); else focusPanel(); }
    }
  };
  if (!inline) panel.showModal(); focusPanel();
  try {
    const result = await bridge.apiGet("state");
    if (!valid()) { dispose(); return; }
    baseline = structuredClone(result.settings.memory_maintenance); revision = result.settings.settings_revision;
    controller = planRecoverySettings(baseline, true, {onSaved: () => { if (valid()) options.onChange?.(); }}, panel.querySelector("#memory-maintenance-panel"));
    controller.changed = update; q("loading").hidden = true; update();
  } catch { if (valid()) { q("loading").hidden = true; message(inline ? "设置读取失败，请关闭记忆助手后重试。" : "设置读取失败，请关闭后重试。"); } }
  return session;
}

function mountQQSettings(form, data, characters, qqScopes) {
  const find = id => form.querySelector("#qq-" + id);
  let saved = structuredClone(data), sessions = [...data.sessions], saving = false;
  const enabled = find("enabled"), target = find("target"), account = find("account");
  const picker = find("account-choice"), accountLabel = find("account-label"), add = find("add");
  let lookup = 0, loadingAccounts = false;
  const list = find("grants"), status = find("status"), submit = find("save"), cancel = find("cancel");
  const value = () => ({enabled: enabled.checked, sessions});
  const dirty = () => JSON.stringify(value()) !== JSON.stringify(saved);
  const options = characters.flatMap(character => (character.sessions || []).flatMap(scope => {
    if (!qqScopes.includes(scope)) return [];
    const match = /^([^:]+):(FriendMessage|GroupMessage):([1-9][0-9]{0,15}(?:_[1-9][0-9]{0,15})?)$/.exec(scope);
    if (!match) return [];
    const [, platform, kind, id] = match;
    if (kind === "FriendMessage" && id.includes("_")) return [];
    const label = `${character.name} · ${platform} · ${kind === "FriendMessage" ? "私聊" : "群聊"} ${id}`;
    return [`<option value="${escape(scope)}">${escape(label)}</option>`];
  }));
  target.innerHTML = `<option value="">${options.length ? "选择已绑定会话" : "请先在角色页绑定 QQ 会话"}</option>` + options.join("");
  add.disabled = true;
  const update = () => {
    submit.hidden = cancel.hidden = !dirty() && !saving;
    submit.disabled = cancel.disabled = saving;
  };
  target.onchange = async () => {
    const scope = target.value, version = ++lookup;
    loadingAccounts = !!scope;
    account.value = ""; account.readOnly = false;
    account.hidden = false; account.disabled = true;
    picker.hidden = true; picker.disabled = true; picker.value = "";
    accountLabel.setAttribute("for", "qq-account");
    add.disabled = true; status.textContent = "";
    if (!scope) return;
    try {
      const result = await bridge.apiGet("qq/accounts", {scope});
      if (version !== lookup || !form.isConnected) return;
      const accounts = result.accounts;
      if (accounts.length === 1) {
        account.value = accounts[0];
        account.readOnly = true;
      } else if (accounts.length > 1) {
        account.hidden = true;
        picker.innerHTML = '<option value="">选择机器人 QQ 号</option>' +
          accounts.map(id => `<option value="${escape(id)}">${escape(id)}</option>`).join("");
        picker.value = ""; picker.hidden = false; picker.disabled = false;
        accountLabel.setAttribute("for", "qq-account-choice");
      } else {
        status.textContent = "此会话还没有识别到机器人。可以手填，或发一条消息后重新选择。";
      }
    } catch {
      if (version !== lookup || !form.isConnected) return;
      status.textContent = "读取机器人失败，请重新选择会话重试。";
      return;
    } finally {
      if (version === lookup) loadingAccounts = false;
    }
    account.disabled = account.hidden;
    add.disabled = false;
  };
  const draw = () => {
    list.innerHTML = sessions.map((grant, index) => {
      const [platform, bot, kind, id] = grant.split(":");
      const label = id ? `${platform} · 机器人 ${bot} · ${kind === "FriendMessage" ? "私聊" : "群聊"} ${id}` : grant;
      return `<li>${escape(label)} <button type="button" data-qq-remove="${index}">移除</button></li>`;
    }).join("");
    for (const button of list.querySelectorAll("[data-qq-remove]")) {
      button.onclick = () => {
        if (saving) return;
        sessions.splice(Number(button.dataset.qqRemove), 1);
        status.textContent = ""; draw(); target.focus();
      };
    }
    update();
  };
  enabled.onchange = () => { status.textContent = ""; update(); };
  add.onclick = () => {
    if (saving || loadingAccounts || add.disabled) return;
    const field = picker.hidden ? account : picker;
    const robot = field.value.trim();
    if (!target.value || !/^[1-9][0-9]{0,15}$/.test(robot)) {
      status.textContent = "请选择会话并填写机器人 QQ 号。";
      (target.value ? field : target).focus();
      return;
    }
    const separator = target.value.indexOf(":");
    const grant = target.value.slice(0, separator) + ":" + robot + target.value.slice(separator);
    if (sessions.includes(grant)) { status.textContent = "这条会话已授权给该机器人。"; return; }
    sessions.push(grant);
    status.textContent = ""; draw();
  };
  cancel.onclick = () => {
    enabled.checked = saved.enabled; sessions = [...saved.sessions];
    status.textContent = ""; draw(); enabled.focus();
  };
  form.onsubmit = async event => {
    event.preventDefault();
    if (!dirty() || saving || loadingAccounts || settingsWriteBusy || settingsSession?.saving) return;
    saving = true; status.textContent = ""; update();
    const controls = [...form.querySelectorAll("input, select, button")];
    const disabled = controls.map(el => [el, el.disabled]);
    controls.forEach(el => el.disabled = true);
    try {
      saved = await act("qq_settings", {...value(), expected: saved});
      state.data.qq_actions = structuredClone(saved);
      enabled.checked = saved.enabled; sessions = [...saved.sessions];
      status.textContent = "已保存";
    } catch (error) { status.textContent = error.message || "保存失败，修改已保留"; }
    finally {
      saving = false;
      for (const [el, old] of disabled) el.disabled = old;
      draw();
    }
  };
  draw();
  return {dirty};
}

function renderSettings() {
  const s = state.data.settings;
  main.innerHTML = `<div class="page-tools"><div class="subhead"><h2>设置</h2><div class="savebar"><p class="help" id="settings-status" role="status" aria-live="polite"></p><button id="save-settings" class="primary" type="submit" form="settings-form" hidden>保存设置</button></div></div></div>
  ${state.data.qq_actions ? `<form id="qq-settings-form" class="panel" novalidate>
  <h3>QQ 资料与互动</h3>
  <label class="check"><input type="checkbox" id="qq-enabled" ${state.data.qq_actions.enabled ? "checked" : ""}>启用 QQ 资料与互动</label>
  <p class="help">授权会话可戳当前发言者。改昵称、签名、状态和头像仅限管理员 QQ 私聊。</p>
  <ul id="qq-grants" aria-label="已授权会话"></ul>
  <div class="form-row"><div><label for="qq-target">要授权的会话</label><select id="qq-target"></select></div>
  <div><label id="qq-account-label" for="qq-account">机器人 QQ 号</label><input id="qq-account" inputmode="numeric" maxlength="16" disabled><select id="qq-account-choice" hidden disabled></select></div></div>
  <div class="actions"><button type="button" id="qq-add">添加授权</button><button type="button" id="qq-cancel" hidden>取消修改</button><button class="primary" id="qq-save" type="submit" hidden>保存 QQ 设置</button></div>
  <p id="qq-status" class="help" role="status" aria-live="polite"></p></form>` : ""}
  <form id="settings-form" novalidate><div class="settings-grid"><section class="panel" id="site-panel"></section>
  <section class="panel"><h3>生成与重试</h3>
  <label for="preferred-provider">首选生成站点</label><select id="preferred-provider"></select>
  <div class="form-row"><div><label for="max-retries">每站追加重试次数</label><input id="max-retries" name="max_retries" type="number" min="0" max="10" value="${s.max_retries}"></div><div><label for="retry-delay">首次重试等待（秒）</label><input id="retry-delay" name="retry_delay" type="number" min="1" max="300" value="${s.retry_delay}"></div></div>
  <label class="check"><input type="checkbox" name="parallel_backup" ${s.parallel_backup ? "checked" : ""}>重试时同时请求另一个站点</label>
  <p class="help">会额外消耗额度；停止本地等待不保证上游停止计费。</p>
  <label class="check"><input type="checkbox" name="retry_unknown" ${s.retry_unknown ? "checked" : ""}>结果未知时允许重试（可能重复计费）</label>
  <details><summary>重试与站点顺序</summary><p class="help">从首选站点开始，失败后按以下顺序切换。选择查看站点不会改变首选。</p><p class="help" id="site-order"></p>
  <p class="help">重试等待逐步增加，限流时遵守服务端等待时间；已成图只重取原图。并行请求仅采用先取得可读图片的结果，之后优先使用该站点。</p>
  <p class="help">HTTP 524、断线等可能已经提交成功；有任务编号时继续查询，重启和历史任务不自动重投。</p></details></section>
  <section class="panel" id="plan-recovery-panel"></section>
  <section class="panel"><h3>记忆助手</h3><button type="button" id="memory-assistant-link">记忆助手设置</button></section>
  <section class="panel"><h3>QQ 空间与相册保留</h3>
  <label class="check"><input name="qzone_enabled" type="checkbox" ${s.qzone_enabled ? "checked" : ""}>允许所属角色发布相册照片</label><p class="help">开启不会立即发布。</p>
  <label for="visibility">默认可见范围</label><select id="visibility" name="qzone_visibility"><option value="4" ${s.qzone_visibility === 4 ? "selected" : ""}>好友可见</option><option value="64" ${s.qzone_visibility === 64 ? "selected" : ""}>仅自己可见</option><option value="1" ${s.qzone_visibility === 1 ? "selected" : ""}>所有人可见</option></select>
  <div class="form-row"><div><label for="retention-days">成图保留天数</label><input id="retention-days" name="generated_retention_days" type="number" min="1" max="365" value="${s.generated_retention_days}"></div><div><label for="max-photos">成图数量上限</label><input id="max-photos" name="generated_max_count" type="number" min="1" max="1000" value="${s.generated_max_count}"></div></div>
  <p class="help">超期或超量会清理旧成图，清理后无法恢复。收藏和使用中的图片保留，可能暂时超限。</p>
  <div class="form-row"><div><label for="task-timeout">等待任务时限（秒）</label><input id="task-timeout" name="task_timeout" type="number" min="60" max="3600" value="${s.task_timeout}"></div><div><label for="poll">查询间隔（秒）</label><input id="poll" name="poll_interval" type="number" min="3" max="60" value="${s.poll_interval}"></div></div>
  <details><summary>存储与超时说明</summary><p class="help">相册原图占用 <span id="album-storage-size">${(state.data.storage_bytes / 1024 / 1024).toFixed(1)}</span> MB</p>
  <p class="help">每分钟检查保留规则；没有可删图片时暂停新生图。回收站可在自动清理前恢复，清理后只保留任务元数据，用户源文件不受影响。</p>
  <p class="help">场景参考保留24小时，角色参考不自动清理，两者仅用于生图。</p>
  <p class="help">超时有任务编号时保留查询；无编号按“结果未知时允许重试”设置处理。</p></details></section></div></form><section class="panel backup-entry"><h3>备份与恢复</h3><button type="button" id="open-backup">备份与恢复</button></section>`;
  const directory = {};
  settingsSession = providerFields(s, planRecoverySettings(s.plan_recovery, false, directory));
  const qqForm = main.querySelector("#qq-settings-form");
  if (qqForm) qqSettingsSession = mountQQSettings(qqForm, state.data.qq_actions, state.data.characters || [], state.data.qq_scopes || []);
  main.querySelector("#memory-assistant-link").onclick = () => openMemoryAssistantSettings();
  main.querySelector("#open-backup").onclick = event => {
    const session = settingsSession;
    if (!session || session.saving || settingsWriteBusy) return;
    openBackup({bridge, escape, settings: session, trigger: event.currentTarget,
      isCurrent: () => state.tab === "settings" && settingsSession === session});
  };
  main.querySelector("#settings-form").onsubmit = event => { event.preventDefault(); return settingsSession.saveAll(); };
}

try {
  if (!bridge) throw new Error("请从 AstrBot 的“扩展功能”打开此页面");
  await bridge.ready();
  const themeToggle = document.getElementById("theme-toggle");
  const updateTheme = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    themeToggle.innerHTML = icon(dark ? "sun" : "moon");
    themeToggle.title = dark ? "切换亮色主题" : "切换暗色主题";
    themeToggle.setAttribute("aria-label", themeToggle.title);
  };
  themeToggle.onclick = () => {
    document.documentElement.dataset.theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  };
  new MutationObserver(updateTheme).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
  updateTheme();
  await reload();
} catch (e) {
  main.innerHTML = `<div class="empty"><h2>相册尚未连接</h2><p>${escape(e.message)}</p></div>`;
  main.setAttribute("aria-busy", "false");
}
let historyPoll = false;
setInterval(async () => {
  if (!document.hidden && state.tab === "jobs" && !dialog.open && !historyState.manage && !historyPoll && historyState.page) {
    historyPoll = true;
    const serial = state.serial;
    try {
      const r = await bridge.apiGet("history", {kind:historyState.kind,offset:historyState.offset,status:historyState.status,character:historyState.character,query:historyState.query});
      if(serial===state.serial && r.signature!==historyState.page.signature){
        const label=main.querySelector('#history-update');if(label)label.textContent='有新记录或状态变化，点击刷新查看';
      }
    } catch { /* Keep the current page and retry the read-only check later. */ }
    finally { historyPoll=false; }
  }
}, 8000);
