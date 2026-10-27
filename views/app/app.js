const bridge = window.AstrBotPluginView;
const main = document.getElementById("main");
const dialog = document.getElementById("dialog");
const content = document.getElementById("dialog-content");
const fileInput = document.getElementById("file-input");
new ResizeObserver(([entry]) => {
  document.documentElement.style.setProperty("--header-height", `${entry.target.getBoundingClientRect().height}px`);
}).observe(document.querySelector(".app-header"));
const toolsObserver = new ResizeObserver(([entry]) => {
  document.documentElement.style.setProperty("--page-tools-height", `${entry.target.getBoundingClientRect().height}px`);
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
const selectedHistory = new Map();
const historyState = {kind: "jobs", status: "", character: "", query: "", offset: 0, manage: false, page: null};
const historyNames = {jobs: "生图任务", deliveries: "照片投递", runs: "朝夕足迹"};
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
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3"/>',
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
const recordStatus = {...statusNames, sent: "已发送", sending: "发送中", held: "已保留", skipped: "已跳过"};
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
  return bridge.apiPost("action/" + action, payload);
}
async function refreshData() {
  state.data = await bridge.apiGet("state");
}
function showDialog(title, body, foot = "") {
  delete content.dataset.assetId;
  content.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">${escape(title)}</h2><button class="close" aria-label="关闭" data-close>×</button></div>${body}${foot}`;
  if (!dialog.open) dialog.showModal();
  content
    .querySelectorAll("[data-close]")
    .forEach((btn) => (btn.onclick = () => dialog.close()));
}
function footer(buttons) {
  return `<div class="dialog-foot">${buttons}</div>`;
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
  const serial = ++state.serial;
  planSortMenuEvents?.abort();
  toolsObserver.disconnect();
  main.dataset.page = state.tab;
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
  } catch (e) {
    if (serial === state.serial) {
      main.innerHTML = `<div class="empty"><h2>暂时无法打开</h2><p>${escape(e.message)}</p><button id="retry">重新加载</button></div>`;
      main.querySelector("#retry").onclick = () => reload();
    }
  } finally {
    const toolbar = main.querySelector(":scope > .page-tools, :scope > .toolbar, :scope > .subhead");
    document.documentElement.style.setProperty("--page-tools-height", `${toolbar?.getBoundingClientRect().height || 0}px`);
    if (toolbar) toolsObserver.observe(toolbar);
    main.setAttribute("aria-busy", "false");
  }
}
async function reload() {
  await refreshData();
  await render();
}
function nav(tab) {
  if (state.tab === tab) return;
  selectedPhotos.clear(); state.albumManage = false;
  selectedPlans.clear(); state.planManage = false;
  selectedHistory.clear(); historyState.manage = false;
  state.tab = tab;
  render();
  window.scrollTo(0, 0);
}
document
  .querySelectorAll("[data-tab]")
  .forEach((b) => (b.onclick = () => nav(b.dataset.tab)));

async function renderAlbum(serial) {
  const result = await bridge.apiGet("assets", {
    character_id: state.character,
    category: state.category,
    query: state.query,
    offset: state.offset,
    start_date: state.startDate || "", end_date: state.endDate || "",
  });
  if (serial !== state.serial) return;
  if (!result.items.length && result.total && state.offset >= result.total) {
    state.offset = Math.floor((result.total - 1) / 24) * 24;
    return renderAlbum(serial);
  }
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
  main.innerHTML = `<div class="page-tools"><div class="toolbar"><div class="filters"><select id="album-character" aria-label="查看角色">${options(state.character, true)}</select><input id="search" aria-label="搜索照片" placeholder="搜索照片" value="${escape(state.query)}"></div><div class="actions"><button id="manage-album">${state.albumManage ? "完成管理" : "管理相册"}</button><button id="import">导入照片</button><button id="generate" class="primary">生成照片</button></div></div><div class="categories" role="group" aria-label="照片分类">${Object.entries(
    {
      all: "全部照片",
      favorite: "收藏",
      generated: "已生成",
      imported: "已导入",
      scene: "临时场景参考",
      trash: "回收站",
    },
  )
    .map(
      ([k, v]) =>
        `<button data-category="${k}" class="${state.category === k ? "active" : ""}" aria-pressed="${state.category === k}">${v}</button>`,
    )
    .join(
      "",
    )}</div><div class="album-range"><div class="actions"><input type="date" id="album-from" aria-label="开始日期" value="${state.startDate || ""}"><span>至</span><input type="date" id="album-to" aria-label="结束日期" value="${state.endDate || ""}"><button id="filter-dates">查看</button></div></div>${state.albumManage ? `<div class="album-batch"><span id="selection-count" role="status"></span><div class="actions"><button id="select-page">全选本页</button><button id="clear-selection">取消选择</button><button id="batch-favorite" aria-label="收藏所选照片" title="收藏所选" class="icon-button favorite-button">${photoIcon("heart")}</button><button id="batch-unfavorite">取消收藏</button><button id="batch-trash" aria-label="删除所选照片" title="移到回收站" class="icon-button danger">${photoIcon("trash")}</button>${state.category === "trash" ? `<button id="batch-restore">恢复所选</button>` : ""}</div></div>` : ""}</div>${gallery || `<div class="empty"><div class="empty-icon" aria-hidden="true">▧</div><h2>${chars().length ? filtered ? "无匹配照片" : "暂无照片" : "先创建一个角色"}</h2>${!filtered && chars().length ? "<p>导入或生成照片</p>" : ""}<div class="actions"><button id="empty-action" class="primary">${!chars().length ? "设置角色参考" : filtered ? "重置筛选" : "导入照片"}</button></div></div>`}<div class="pagination"><button id="prev" ${state.offset === 0 ? "disabled" : ""}>上一页</button><span>${result.total} 张照片 · ${Math.floor(state.offset / 24) + 1} 页</span><button id="next" ${state.offset + 24 >= result.total ? "disabled" : ""}>下一页</button></div>`;
  main.querySelector("#album-character").onchange = (e) => {
    selectedPhotos.clear();
    state.character = e.target.value;
    state.offset = 0;
    render();
  };
  main.querySelector("#search").onkeydown = (e) => {
    if (e.key === "Enter") {
      selectedPhotos.clear();
      state.query = e.target.value.trim();
      state.offset = 0;
      render();
    }
  };
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
  main.querySelector("#manage-album").onclick = () => { state.albumManage = !state.albumManage; selectedPhotos.clear(); render(); };
  main.querySelector("#filter-dates").onclick = () => {
    const from = main.querySelector("#album-from"), to = main.querySelector("#album-to");
    if (!from.reportValidity() || !to.reportValidity()) return;
    if (from.value && to.value && from.value > to.value) { toast("结束日期不能早于开始日期", true); to.focus(); return; }
    state.startDate = from.value; state.endDate = to.value; state.offset = 0; selectedPhotos.clear(); render();
  };
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
  main.querySelector("#import").onclick = openImport;
  main.querySelector("#generate").onclick = openGenerate;
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
  const r = await bridge.apiGet("history", {kind: historyState.kind, status: historyState.status, character: historyState.character, query: historyState.query, offset: historyState.offset});
  if (state.tab !== "jobs" || serial !== state.serial) return;
  historyState.page = r; historyState.offset = r.offset;
  const kind = historyState.kind;
  main.innerHTML = `<div class="page-tools"><div class="subhead"><div><h2>任务记录</h2></div><div class="actions"><button id="history-policy">保留设置</button><button id="manage-history">${historyState.manage ? "完成管理" : "管理记录"}</button><button id="refresh-jobs">刷新</button></div></div><div class="categories history-nav" role="group" aria-label="记录类型">${Object.entries(historyNames).map(([k,v])=>`<button data-history-kind="${k}" aria-pressed="${kind===k}" class="${kind===k?'active':''}">${v}</button>`).join("")}</div><details class="history-filters" ${window.matchMedia("(min-width: 601px)").matches ? "open" : ""}><summary>筛选条件${historyState.status||historyState.character||historyState.query ? " · 已应用" : ""}</summary><form id="history-filter" class="history-filter"><div><label for="history-status">状态</label><select id="history-status"><option value="">全部状态</option>${Object.entries(r.counts).map(([k,n])=>`<option value="${escape(k)}" ${historyState.status===k?'selected':''}>${escape(recordStatus[k]||k)} (${n})</option>`).join("")}</select></div>${kind==='jobs'?`<div><label for="history-character">角色</label><select id="history-character">${options(historyState.character,true)}</select></div>`:''}<div class="history-search"><label for="history-query">搜索</label><input id="history-query" maxlength="100" value="${escape(historyState.query)}" placeholder="画面、名称、错误或编号"></div><div class="actions"><button type="submit">筛选</button><button type="button" id="history-reset">重置</button></div></form></details><div class="history-status"><span id="history-update" role="status">共 ${r.total} 条 · 每页 ${r.limit} 条</span></div>${historyState.manage?'<div class="actions history-batch"><span id="history-selected" role="status"></span><button id="history-select-all">全选本页可清理记录</button><button id="history-clear">取消选择</button><button id="history-delete" class="danger">清理所选记录</button></div>':''}</div><div class="history-list">${r.items.map(j=>`<article class="history-card"><div class="history-card-head">${historyState.manage?`<input type="checkbox" data-history-select="${j.id}" aria-label="选择记录 ${escape(j.id)}" ${selectedHistory.has(j.id)?'checked':''} ${!j.cleanable?'disabled':''}>`:''}<strong>${escape(kind==='jobs'?j.character_name:kind==='runs'?j.name:(j.target==='qzone'?'QQ 空间':j.id.endsWith(':caption')?'照片接话':j.id.endsWith(':photo')?'图片投递':'聊天投递'))||historyNames[kind]}</strong><span class="status ${escape(j.state)}">${escape(recordStatus[j.state]||j.state)}</span><time>${date(j.created||j.at||0)}</time></div>${j.scene||j.error?`<p class="history-scene">${escape(j.scene||j.error)}</p>`:''}${j.scene&&j.error?`<p class="history-error">${escape(j.error)}</p>`:''}${kind==='jobs'?`<p class="help">聊天：${{none:'仅入相册',pending:j.notice==='sent'?'已通知状态':'等待完成',sent:'已发送',held:'留在相册',failed:'发送失败',unknown:'待核对'}[j.delivery]||'暂无'}${j.qzone_intent?` · 空间：${escape(recordStatus[j.qzone_delivery]||j.qzone_delivery||'待发布')}`:''}</p>`:''}<div class="history-card-foot">${historyState.manage&&!j.cleanable?helpTip("记录受保护",j.protection,"lock"):""}<button data-history-detail="${j.id}">查看详情</button></div></article>`).join('')||`<div class="empty"><h3>${historyState.status||historyState.character||historyState.query?"无匹配记录":"暂无记录"}</h3>${historyState.status||historyState.character||historyState.query?'<button id="history-empty-reset">重置筛选</button>':""}</div>`}</div><div class="history-pager actions"><button id="history-prev" ${r.offset===0?'disabled':''}>上一页</button><span>第 ${Math.floor(r.offset/r.limit)+1} / ${Math.max(1,Math.ceil(r.total/r.limit))} 页</span><button id="history-next" ${r.offset+r.limit>=r.total?'disabled':''}>下一页</button></div>`;
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
  main.querySelectorAll('[data-history-kind]').forEach(b=>b.onclick=()=>{historyState.kind=b.dataset.historyKind;historyState.status=historyState.character=historyState.query='';historyState.offset=0;selectedHistory.clear();render();});
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
    if(kind==='jobs'){const detail=await bridge.apiGet('job/'+j.id);openJobDetail(detail.job,detail.deliveries);}
    else showDialog(historyNames[kind]+'详情',`<div class="dialog-body"><dl><dt>状态</dt><dd>${escape(recordStatus[j.state]||j.state)}</dd><dt>时间</dt><dd>${date(j.created||j.at||0)}</dd>${j.error?`<dt>错误说明</dt><dd>${escape(j.error)}</dd>`:""}</dl>${j.protection?`<p class="help">${escape(j.protection)}</p>`:""}<details class="help-details"><summary>记录详情</summary><dl><dt>记录编号</dt><dd class="mono">${escape(j.id)}</dd>${j.message_id?`<dt>消息编号</dt><dd>${escape(j.message_id)}</dd>`:""}${j.tid?`<dt>空间编号</dt><dd>${escape(j.tid)}</dd>`:""}</dl></details></div>`);
  }));
}

async function openHistoryPolicy() {
  try {
    await refreshData();
    const s=state.data.settings;
    const preview=await act('history_cleanup',{});
    showDialog('记录保留设置',`<form id="history-policy-form" class="dialog-body"><p>超期或超量时清理已结束的详情，清理后无法恢复，照片保留。受保护记录可能超出上限。</p><label for="record-days">保留天数</label><input id="record-days" type="number" min="1" max="3650" required value="${s.records_retention_days}"><label for="record-count">每类记录数量上限</label><input id="record-count" type="number" min="20" max="10000" required value="${s.records_max_count}"><details class="help-details"><summary>清理与保护规则</summary><p>每分钟检查，每类每次最多清理200条。活动任务、待恢复、结果未知及现有计划的最后一次执行受保护。详细日志清理后无法恢复；照片生成信息与防重信息保留，数据库空间供后续复用。</p></details><ul>${Object.entries(preview).map(([k,v])=>`<li>${historyNames[k]}：${v.retained} 条，${v.protected} 条受保护，按当前规则可清理 ${v.eligible} 条</li>`).join('')}</ul></form>`,footer('<button id="cancel-policy">取消</button><button class="primary" type="submit" form="history-policy-form" id="save-policy">保存保留设置</button>'));
    content.querySelector('#cancel-policy').onclick=()=>dialog.close();
    content.querySelector('#history-policy-form').onsubmit=e=>{e.preventDefault();busy(content.querySelector('#save-policy'),async()=>{await act('settings',{settings_revision:s.settings_revision,records_retention_days:Number(content.querySelector('#record-days').value),records_max_count:Number(content.querySelector('#record-count').value)});dialog.close();selectedHistory.clear();await reload();toast('保留设置已保存');});};
  }catch(e){toast(e.message,true);}
}

function openJobDetail(j, receipts = null) {
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
    ${waiting?'<p class="help">停止本地等待不会取消上游任务，上游仍可能完成或计费。</p>':""}
    <details class="help-details"><summary>诊断与完整回执</summary><p class="mono">任务编号：${escape(j.id)}</p>
    ${!j.upstream_completed&&!j.asset_id?'<p>未记录成图结果，不能仅凭失败状态判断上游是否完成。</p>':""}
    ${(j.diagnostics||[]).length?`<h3>处理记录</h3>${j.diagnostics.map(d=>`<p>${escape(date(d.at))} · ${escape(stageNames[d.stage]||d.stage)}<br>${escape(d.code||"异常类别未记录")}${d.http_status?` · HTTP ${d.http_status}`:""}</p>`).join("")}`:""}
    ${j.attempts?.length?`<h3>站点尝试</h3><p>每站追加 ${j.retry_policy?.max_retries??0} 次 · 并行备用 ${j.retry_policy?.parallel_backup?"开启":"关闭"}</p>${j.attempts.map(a=>`<details><summary>第 ${escape(a.id)} 次 · ${escape(a.provider_name)} · ${{completed:"图片可用",failed:"失败",unknown:"待核对",running:"查询中",submitting:"请求中",download_failed:"下载失败",downloading:"取图中",abandoned:"已停止等待",unused:"未采用",preparing:"准备中"}[a.state]||escape(a.state)}${j.winner===a.id?" · 已采用":""}</summary><p>${escape(date(a.at))} · ${escape(a.stage||"生成")}<br>${escape(a.error||"")}${a.code?` · ${escape(a.code)}`:""}${a.http_status?` · HTTP ${a.http_status}`:""}${a.download_attempts?` · 下载尝试 ${a.download_attempts} 次`:""}</p></details>`).join("")}`:""}
    <h3>投递回执</h3>${deliveries.map(d=>`<p class="mono">${escape(d.id)}<br>${escape(recordStatus[d.state]||d.state)}${d.message_id?` · 消息 ${escape(d.message_id)}`:""}${d.tid?` · 空间 ${escape(d.tid)}`:""}</p>`).join("")||'<p>尚无投递</p>'}</details>
  </div>`, footer(controls));

  for(const action of ['resume','cancel']) {
    const button=content.querySelector('#detail-'+action);
    if(button)button.onclick=()=>busy(button,async()=>{await act(action,{id:j.id});const detail=await bridge.apiGet('job/'+j.id);openJobDetail(detail.job,detail.deliveries);toast(action==='cancel'?'已停止本地等待，记录保留；上游可能仍会完成':'已恢复处理原任务，不会重新生图');});
  }
  const photo=content.querySelector('#detail-photo');
  if(photo)photo.onclick=()=>busy(photo,async()=>openDetail(await bridge.apiGet('asset/'+j.asset_id)));
}

async function renderPlans() {
  const serial = state.serial;
  const data = await bridge.apiGet("plans", {scope: state.calendarScope || ""});
  if (state.tab !== "plans" || serial !== state.serial) return;
  const names = {sent: "已发送", skipped: "已跳过", held: "已保留", running: "执行中", unknown: "待核对", failed: "发送失败"};
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
  const runs = data.runs.map((r) => `<details class="panel plan-run"><summary>${escape(wallTime(r.at, r.timezone))} · ${escape(r.name)}${r.run_once ? "（一次性）" : ""}${r.delta_minutes ? ` · ${escape(deltaText(r.delta_minutes))}` : ""} · ${escape(names[r.state] || r.state)}</summary><p class="help">${escape(r.timezone)}${r.anchor_at ? ` · 基准时间 ${escape(wallTime(r.anchor_at, r.timezone))}` : ""}</p>${r.summary ? `<p class="preserve-text">${escape(r.summary)}</p>` : ""}${r.error || r.code || r.message_id || r.job_id || r.follow_up_name ? `<p>${escape(r.error || "")}${r.code ? ` · ${escape(r.code)}` : ""}${r.message_id ? ` · 消息 ${escape(r.message_id)}` : ""}${r.job_id ? ` · 伴随拍照任务 ${escape(r.job_id)}` : ""}${r.follow_up_name ? ` · 延续约定「${escape(r.follow_up_name)}」` : ""}</p>` : ""}</details>`).join("");
  const todaySummary = data.today_profile ? `${escape(data.today_profile.date)} ${escape(data.today_profile.weekday_cn)} · ${escape(data.today_profile.label)}${data.today_profile.active_trip ? ` · 行程「${escape(data.today_profile.active_trip.title)}」` : ""}` : "";
  main.innerHTML = `<div class="page-tools"><div class="subhead"><div><h2>朝夕拾记</h2></div><div class="actions">${view !== "runs" && view !== "calendar" ? `<button id="manage-plans">${state.planManage ? "完成管理" : "管理计划"}</button>` : view === "runs" ? '<button id="all-run-history">查看全部执行记录</button>' : ''}<button id="import-plans">导入</button><button id="export-plans">导出 JSON</button><button id="new-plan" class="primary">新建计划</button></div></div><div class="plan-view-tools"><div class="categories plan-nav" role="group" aria-label="计划导航">${Object.entries({list:"朝夕列表",calendar:"时令日历",runs:"朝夕足迹",trash:"回收站"}).map(([key,label])=>`<button data-plan-view="${key}" class="${key === view ? "active" : ""}" aria-pressed="${key === view}">${label}</button>`).join("")}</div>${sortControl}</div>${todaySummary && view !== "calendar" ? `<p class="help plan-rule">${todaySummary}</p>` : ""}${state.planManage && view !== "runs" && view !== "calendar" ? `<div class="plan-batch"><span id="plan-selection-count" role="status"></span><div class="actions"><button id="select-all-plans">全选</button><button id="clear-plans">取消选择</button>${view === "trash" ? '<button data-plan-batch="restore">恢复所选</button>' : '<button data-plan-batch="enable" aria-label="启用所选">启用</button><button data-plan-batch="disable" aria-label="关闭所选">关闭</button><button data-plan-batch="delete" class="danger" aria-label="删除所选">删除</button>'}</div></div>` : ""}</div>${data.scheduler_error || !data.scheduler_running ? `<p class="help" role="status">${escape(data.scheduler_error || "朝夕拾记检查未运行")}</p>` : ""}<div class="plan-list">${view === "calendar" ? renderCalendarMarkup(data) : view === "runs" ? runs || '<div class="empty"><h3>暂无朝夕足迹</h3></div>' : cards || `<div class="empty"><h3>${view === "trash" ? "回收站是空的" : "暂无计划"}</h3><p>${view === "trash" ? "可恢复为停用状态。" : "导入或新建计划。"}</p></div>`}</div>`;
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
  if (state.planManage && view !== "runs" && view !== "calendar") {
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
    showDialog("导入朝夕拾记", `<div class="dialog-body"><p>支持拾光手账 JSON 和已有 AstrBot 计划导出。相同名称与会话不会重复导入；全部先停用。</p><label for="plan-file">选择 JSON 文件</label><input id="plan-file" type="file" accept=".json,application/json"><label for="import-scope">迁入会话（留空使用文件原值）</label><select id="import-scope"><option value="">使用文件原会话</option>${chars().flatMap((c) => c.sessions.map((s) => `<option value="${escape(s)}">${escape(c.name)} · ${escape(s)}</option>`)).join("")}</select><p class="help">旧文件没有时区时，明确采用 Asia/Shanghai。所有会话须先绑定角色。</p></div>`, footer('<button id="confirm-import" class="primary">导入并停用</button>'));
    content.querySelector("#confirm-import").onclick = (e) => busy(e.target, async () => {
      const file = content.querySelector("#plan-file").files[0];
      if (!file || file.size > 2 * 1024 * 1024) throw new Error("请选择不超过 2 MB 的 JSON 文件");
      let document;
      try { document = JSON.parse(await file.text()); } catch { throw new Error("文件不是有效 JSON"); }
      const result = await act("plans_import", {document, scope: content.querySelector("#import-scope").value});
      dialog.close(); await render(); toast(`导入 ${result.created} 条，跳过重复 ${result.skipped} 条；全部停用`);
    });
  };
  if (view === "calendar") bindCalendarView(data);
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
      <div><label for="plan-photo-mode">拍照方式</label><select id="plan-photo-mode">${Object.entries({auto:'按内容自动决定',always:'每次拍照',never:'仅发文字'}).map(([v,l])=>`<option value="${v}" ${(p.photo_mode||'auto')===v?'selected':''}>${l}</option>`).join('')}</select></div>
      <details><summary>作息与拍照说明</summary><p class="help">固定规则不随调休切换。上课/工作日避开寒暑假、法定假日与广西壮族三月三，调休补班日自动补位；休息/假期与其相反。催睡与夜猫子按次日是否早起安排。特殊行程可临时调整作息。拍照先发文字，再异步生成照片。</p></details><p class="help" id="schedule-note"></p><div class="schedule-preview" aria-live="polite"><strong>接下来 5 次</strong><p id="schedule-summary"></p><ol id="plan-preview"></ol><p class="help">按所选时区与浮动区间预览。保存并启用才执行，不补发错过的时间。</p></div>
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
    return {id:p.id||'',name:$('plan-name').value.trim(),cron:JSON.stringify(scheduleValue())===initialValue ? initialCron : buildPlanCron(scheduleValue()),timezone:$('plan-zone').value.trim(),window_before:wb,window_after:wa,photo_mode:$('plan-photo-mode').value,day_mode:$('plan-day-mode').value,scope:sc||p.scope||''};
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

function providerFields(s) {
  const panel = main.querySelector("#site-panel");
  const fields = ["name", "base_url", "api_keys", "active_key_id", "model", "size", "reference_field", "enabled", "id"];
  const primary = {id: "primary", name: s.primary_name, enabled: s.primary_enabled, ...Object.fromEntries(["base_url", "model", "size", "reference_field", "api_key_set", "api_keys", "active_key_id"].map((key) => [key, s[key]]))};
  let drafts = [...(s.base_url || s.api_key_set || s.model ? [primary] : []), ...s.providers.map((p) => ({...p}))];
  drafts = drafts.map((p) => ({name: "", ...p, api_keys: (p.api_keys || []).map((k) => ({...k, value: "", saved: true}))}));
  const order = s.provider_order || [];
  drafts.sort((a, b) => (order.includes(a.id) ? order.indexOf(a.id) : 99) - (order.includes(b.id) ? order.indexOf(b.id) : 99));
  let selected = drafts.some((p) => p.id === state.settingsSite) ? state.settingsSite : drafts[0]?.id;
  let preferred = s.preferred_provider;
  let editing;
  let settingsChanged = false;
  const staged = new Set();
  const savedIds = new Set(drafts.map((p) => p.id));
  const dirty = () => {
    const saved = drafts.find((p) => p.id === selected);
    return editing && fields.some((key) => JSON.stringify(editing[key]) !== JSON.stringify(saved?.[key]));
  };
  const updateStatus = () => {
    const changed = dirty();
    panel.querySelector("#save-site").disabled = !editing || !changed;
    const siteStatus = panel.querySelector("#site-status"), settingsStatus = main.querySelector("#settings-status");
    siteStatus.textContent = changed ? "未暂存；切换站点会舍弃修改。" : editing && !savedIds.has(selected) && !staged.has(selected) ? "新站点，请填写后暂存。" : "";
    settingsStatus.textContent = changed ? "请先暂存站点。" : staged.size || settingsChanged ? "有未保存的更改。" : "";
    siteStatus.hidden = !siteStatus.textContent; settingsStatus.hidden = !settingsStatus.textContent;
  };
  const title = (p) => {
    if (p.name?.trim()) return p.name.trim();
    try { return new URL(p.base_url).hostname; } catch { return "未命名站点"; }
  };
  const read = () => {
    const site = editing;
    if (!site) return;
    panel.querySelectorAll("[data-field]").forEach((el) => site[el.dataset.field] = el.type === "checkbox" ? el.checked : el.value);
  };
  const updateSelectors = () => {
    panel.querySelector("#site-select").innerHTML = drafts.map((p) => `<option value="${escape(p.id)}">${escape(title(p))}${p.enabled ? "" : " · 停用"}</option>`).join("") || '<option value="">尚未添加站点</option>';
    panel.querySelector("#site-select").value = selected || "";
    const enabled = drafts.filter((p) => p.enabled);
    const select = main.querySelector("#preferred-provider");
    select.innerHTML = enabled.map((p) => `<option value="${escape(p.id)}">${escape(title(p))}</option>`).join("") || '<option value="primary">暂无启用站点</option>';
    if (!enabled.some((p) => p.id === preferred)) preferred = enabled[0]?.id || "primary";
    select.value = preferred;
    main.querySelector("#site-order").textContent = drafts.map(title).join(" → ") || "添加站点后可设置顺序";
    panel.querySelector("#add-provider").disabled = drafts.length >= 5;
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
    const area = panel.querySelector("#site-keys");
    const current = site.api_keys.find((k) => k.id === site.active_key_id);
    area.innerHTML = `<label for="current-key">当前密钥 · ${site.api_keys.length} 把</label><div class="key-summary"><input id="current-key" readonly placeholder="尚未选择密钥"><button type="button" id="reveal-current-key" class="icon-button" aria-label="显示当前密钥" aria-pressed="false" title="显示当前密钥" ${current ? "" : "disabled"}>${icon("eye")}</button><button type="button" id="manage-keys" class="icon-button" aria-label="管理站点密钥" title="管理站点密钥">${icon("key")}</button></div>`;
    const display = area.querySelector("#current-key");
    let revealVersion = 0;
    display.value = current?.name || "";
    area.querySelector("#reveal-current-key").onclick = (event) => {
      const button = event.currentTarget;
      const requestVersion = ++revealVersion;
      busy(button, async () => {
        const visible = button.getAttribute("aria-pressed") === "true";
        if (visible) display.value = current.name;
        else {
          const result = current.value || (await act("reveal_key", {id: site.id, base_url: site.base_url, key_id: current.id, settings_revision: s.settings_revision})).value;
          if (!display.isConnected || editing !== site || site.active_key_id !== current.id || requestVersion !== revealVersion) return;
          display.value = result;
        }
        button.setAttribute("aria-pressed", String(!visible));
        button.setAttribute("aria-label", visible ? "显示当前密钥" : "隐藏当前密钥");
        button.title = visible ? "显示当前密钥" : "隐藏当前密钥";
        button.innerHTML = icon(visible ? "eye" : "hidden");
      });
    };
    area.querySelector("#manage-keys").onclick = () => {
      revealVersion++;
      display.value = current?.name || "";
      const eye = area.querySelector("#reveal-current-key");
      eye.setAttribute("aria-pressed", "false"); eye.setAttribute("aria-label", "显示当前密钥"); eye.title = "显示当前密钥"; eye.innerHTML = icon("eye");
      let entries = structuredClone(site.api_keys), active = site.active_key_id;
      let editingEntry = null;
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
            const value = visible ? "" : entry.value || (await act("reveal_key", {id: site.id, base_url: site.base_url, key_id: entry.id, settings_revision: s.settings_revision})).value;
            if (!button.isConnected || !dialog.open || editing !== site || !entries.includes(entry)) return;
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
      const commitEntry = () => {
        if (!editingEntry) return true;
        const form = content.querySelector("#key-edit-form");
        form.elements.key_value.setCustomValidity(!editingEntry.saved && !form.elements.key_value.value.trim() ? "请填写本站密钥" : "");
        if (!form.reportValidity()) return false;
        editingEntry.name = form.elements.key_name.value.trim() || `密钥 ${entries.length + 1}`;
        if (form.dataset.keyEdited === "true") editingEntry.value = form.elements.key_value.value.trim();
        const index = entries.findIndex((k) => k.id === editingEntry.id);
        if (index < 0) entries.push(editingEntry); else entries[index] = editingEntry;
        active ||= editingEntry.id;
        editingEntry = null; content.querySelector("#key-editor").innerHTML = ""; drawList();
        return true;
      };
      const openEditor = (entry) => {
        editingEntry = entry ? {...entry} : {id: "key_" + crypto.randomUUID(), name: "", value: "", saved: false};
        content.querySelector("#key-editor").innerHTML = `<form id="key-edit-form" class="key-edit-form"><h3>${entry ? "编辑密钥" : "添加密钥"}</h3><label for="key-edit-name">密钥名称</label><input id="key-edit-name" name="key_name" maxlength="80" placeholder="给这把密钥起个名字"><label for="key-edit-value">密钥内容</label><input id="key-edit-value" name="key_value" type="text" autocomplete="new-password" maxlength="2000" ${editingEntry.saved ? "" : "required"} placeholder="${editingEntry.saved ? "留空保留已保存密钥" : "填写本站密钥"}">${editingEntry.saved ? '<p class="help">留空保留原密钥；替换内容后，恢复旧任务需核对原密钥。</p>' : ""}<div class="actions"><button type="button" id="cancel-key-edit">取消编辑</button><button type="submit">应用编辑</button></div></form>`;
        const form = content.querySelector("#key-edit-form");
        form.elements.key_name.value = editingEntry.name; form.elements.key_value.value = editingEntry.value || "";
        let valueVersion = 0;
        form.elements.key_value.oninput = () => { valueVersion++; form.dataset.keyEdited = "true"; form.elements.key_value.setCustomValidity(""); };
        if (editingEntry.saved && !editingEntry.value) {
          const entry = editingEntry, version = valueVersion;
          form.elements.key_value.placeholder = "正在读取已保存密钥…";
          act("reveal_key", {id: site.id, base_url: site.base_url, key_id: entry.id, settings_revision: s.settings_revision}).then((result) => {
            if (form.isConnected && dialog.open && editing === site && editingEntry === entry && version === valueVersion) form.elements.key_value.value = result.value;
          }).catch(() => { if (form.isConnected) toast("密钥读取失败，可取消后重试；留空仍保留原密钥。", true); })
            .finally(() => { if (form.isConnected) form.elements.key_value.placeholder = "留空保留已保存密钥"; });
        }
        form.onsubmit = (event) => { event.preventDefault(); if (commitEntry()) content.querySelector("#add-key").focus(); };
        content.querySelector("#cancel-key-edit").onclick = () => { editingEntry = null; content.querySelector("#key-editor").innerHTML = ""; drawList(); content.querySelector("#add-key").focus(); };
        drawList(); form.elements.key_name.focus();
      };
      content.querySelector("#add-key").onclick = () => openEditor();
      content.querySelector("#cancel-keys").onclick = () => dialog.close();
      content.querySelector("#apply-keys").onclick = () => {
        if (!commitEntry() || editing !== site || !area.isConnected) return;
        if (JSON.stringify(entries) !== JSON.stringify(site.api_keys) || active !== site.active_key_id) {
          site.api_keys = entries; site.active_key_id = active;
          site.models = []; site.modelStatus = "密钥配置已修改，请重新获取模型。"; drawModels(site);
        }
        dialog.close(); drawKeys(site); updateStatus(); panel.querySelector("#manage-keys").focus();
      };
      dialog.addEventListener("close", () => {
        if (manager.isConnected) { manager.replaceChildren(); if (area.isConnected) panel.querySelector("#manage-keys").focus(); }
        entries = []; editingEntry = null;
      }, {once: true});
      drawList();
    };
  };
  const draw = (reset = true) => {
    updateSelectors();
    state.settingsSite = selected;
    const saved = drafts.find((p) => p.id === selected);
    if (reset) editing = saved ? {...structuredClone(saved), modelStatus: saved.modelStatus === "正在获取模型…" ? "" : saved.modelStatus} : null;
    const site = editing;
    const editor = panel.querySelector("#site-editor");
    updateStatus();
    if (!site) { panel.querySelector("#site-controls").innerHTML = ""; editor.innerHTML = '<p class="help">添加一个图片站点开始配置。</p>'; return; }
    const index = drafts.indexOf(saved);
    editor.innerHTML = `<div class="form-row"><div><label for="site-name">站点名称</label><input id="site-name" data-field="name" maxlength="120" placeholder="留空时使用域名"></div><div><label for="site-url">API 基础地址</label><input id="site-url" data-field="base_url" type="url" required placeholder="https://example.com/v1"></div></div><div id="site-keys"></div><div class="site-model-head"><div class="actions"><label for="site-model-list">图片模型</label>${helpTip("模型选择说明", "填写地址和密钥后获取模型，选择支持图片生成的模型；也可手动填写模型名称。")}</div><button type="button" id="load-models">获取模型</button></div><select id="site-model-list"></select><p class="help" id="model-status" role="status" aria-live="polite"></p><label for="site-model">模型名称</label><input id="site-model" data-field="model" placeholder="选择或手动填写"><label class="check"><input data-field="enabled" type="checkbox">启用此站点</label><details class="site-advanced"><summary>更多连接选项</summary><div class="form-row"><div><label for="site-size">图片尺寸</label><input id="site-size" data-field="size"></div><div><label for="site-field">多参考图字段</label><select id="site-field" data-field="reference_field"><option>image[]</option><option>image</option></select></div></div><p class="help">使用 Images generations/edits 接口，基础地址通常以 /v1 结尾。</p></details>`;
    panel.querySelector("#site-controls").innerHTML = `<button type="button" id="site-up" ${index === 0 ? "disabled" : ""}>顺序提前</button><button type="button" id="site-down" ${index === drafts.length - 1 ? "disabled" : ""}>顺序延后</button><button type="button" id="remove-provider" class="danger">移除站点</button>`;
    drawKeys(site);
    editor.querySelectorAll("[data-field]").forEach((el) => {
      if (el.type === "checkbox") el.checked = !!site[el.dataset.field];
      else el.value = site[el.dataset.field] || "";
      el.addEventListener("input", () => {
        read();
        if (el.dataset.field === "base_url") {
          site.api_keys = []; site.active_key_id = "";
          site.models = []; site.modelStatus = "地址已修改，请添加此站点的密钥。";
          drawKeys(site); drawModels(site);
        }
        if (el.dataset.field === "model") drawModels(site);
        updateStatus();
      });
    });
    drawModels(site);
    panel.querySelector("#site-model-list").onchange = (event) => {
      if (event.target.value === "__manual__") { panel.querySelector("#site-model").focus(); return; }
      site.model = event.target.value; panel.querySelector("#site-model").value = site.model;
      updateStatus();
    };
    panel.querySelector("#load-models").onclick = (event) => busy(event.target, async () => {
      read();
      const query = {id: site.id, base_url: site.base_url.trim(), key_id: site.active_key_id, api_key: site.api_keys.find((k) => k.id === site.active_key_id)?.value || "", clear_api_key: !site.active_key_id};
      const signature = JSON.stringify(query);
      const requestNumber = site.modelRequest = (site.modelRequest || 0) + 1;
      site.modelStatus = "正在获取模型…"; drawModels(site);
      const unchanged = () => {
        read();
        return panel.isConnected && editing === site && site.modelRequest === requestNumber && signature === JSON.stringify({id: site.id, base_url: site.base_url.trim(), key_id: site.active_key_id, api_key: site.api_keys.find((k) => k.id === site.active_key_id)?.value || "", clear_api_key: !site.active_key_id});
      };
      try {
        const result = await act("models", query);
        if (!unchanged()) return;
        site.models = result.models;
        site.modelStatus = result.models.length ? `已获取 ${result.models.length} 个模型` : "站点未返回模型，可手动填写。";
      } catch (error) {
        if (!unchanged()) return;
        site.modelStatus = error.message || "获取模型失败，请检查地址和密钥。";
      }
      if (selected === site.id) drawModels(site);
    });
    panel.querySelector("#remove-provider").onclick = () => {
      drafts = drafts.filter((p) => p.id !== site.id); staged.delete(site.id); settingsChanged = true;
      selected = drafts[Math.min(index, drafts.length - 1)]?.id;
      draw(); panel.querySelector("#site-select").focus();
    };
    for (const [id, delta] of [["site-up", -1], ["site-down", 1]]) {
      panel.querySelector("#" + id).onclick = () => {
        read(); [drafts[index], drafts[index + delta]] = [drafts[index + delta], drafts[index]]; settingsChanged = true;
        draw(false); panel.querySelector("#site-select").focus();
      };
    }
  };
  panel.innerHTML = `<div class="subhead"><div class="actions"><h3>图片站点</h3>${helpTip("站点保存方式", "编辑后先暂存站点，可继续编辑其他站点；最后点击“保存设置”统一生效。")}</div><div class="actions"><button type="button" id="add-provider">添加站点</button></div></div><p id="site-status" class="help" role="status" aria-live="polite"></p><label for="site-select">选择站点</label><select id="site-select"></select><div id="site-editor"></div><div class="actions site-actions"><div id="site-controls" class="actions"></div><button type="button" id="save-site">暂存站点</button></div>`;
  panel.querySelector("#save-site").onclick = () => {
    read();
    for (const el of panel.querySelectorAll("[data-field]")) if (!el.reportValidity()) return;
    if (editing.api_keys.some((k) => !k.value && !k.saved)) { toast("请填写新增密钥", true); return; }
    if (!editing.base_url.trim() || (editing.enabled && !editing.model.trim())) {
      toast("请补全站点地址及启用站点的模型", true); return;
    }
    drafts[drafts.findIndex((p) => p.id === selected)] = structuredClone(editing);
    staged.add(selected); updateSelectors(); updateStatus();
    toast("本站已暂存，保存设置后生效");
  };
  panel.querySelector("#site-select").onchange = (event) => {
    const discarded = dirty(); selected = event.target.value; draw();
    if (discarded) toast("已舍弃未暂存的站点修改");
  };
  panel.querySelector("#add-provider").onclick = () => {
    const discarded = dirty(); selected = drafts.some((p) => p.id === "primary") ? "site_" + crypto.randomUUID() : "primary";
    drafts.push({id: selected, name: "", base_url: "", api_keys: [], active_key_id: "", model: "", size: "1024x1024", reference_field: "image[]", enabled: false});
    settingsChanged = true;
    draw(); panel.querySelector("#site-name").focus();
    if (discarded) toast("已舍弃未暂存的站点修改");
  };
  main.querySelector("#preferred-provider").onchange = (event) => { preferred = event.target.value; settingsChanged = true; updateStatus(); };
  main.querySelector("#settings-form").addEventListener("input", (event) => {
    if (event.target.name) { settingsChanged = true; updateStatus(); }
  });
  draw();
  return () => {
    read();
    if (dirty()) {
      panel.querySelector("#save-site").focus();
      throw new Error("请先暂存站点；切换站点会舍弃当前修改。");
    }
    const invalid = drafts.find((p) => !p.base_url.trim() || (p.enabled && !p.model.trim()));
    if (invalid) { selected = invalid.id; draw(); throw new Error("请补全站点地址及启用站点的模型"); }
    const first = drafts.find((p) => p.id === "primary");
    return {
      ...(first ? Object.fromEntries(["base_url", "api_keys", "active_key_id", "model", "size", "reference_field"].map((key) => [key, first[key]])) : {base_url: "", api_keys: [], active_key_id: "", model: ""}),
      primary_name: first ? title(first) : "", primary_enabled: !!first?.enabled,
      providers: drafts.filter((p) => p !== first).map((p) => Object.fromEntries(fields.map((key) => [key, key === "name" ? title(p) : p[key]]))),
      provider_order: drafts.map((p) => p.id), preferred_provider: preferred,
    };
  };
}

function renderSettings() {
  const s = state.data.settings;
  main.innerHTML = `<div class="page-tools"><div class="subhead"><h2>设置</h2><div class="savebar"><p class="help" id="settings-status" role="status" aria-live="polite"></p><button id="save-settings" class="primary" type="submit" form="settings-form">保存设置</button></div></div></div>
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
  <section class="panel"><h3>QQ 空间与相册保留</h3>
  <label class="check"><input name="qzone_enabled" type="checkbox" ${s.qzone_enabled ? "checked" : ""}>允许所属角色发布相册照片</label><p class="help">开启不会立即发布。</p>
  <label for="visibility">默认可见范围</label><select id="visibility" name="qzone_visibility"><option value="4" ${s.qzone_visibility === 4 ? "selected" : ""}>好友可见</option><option value="64" ${s.qzone_visibility === 64 ? "selected" : ""}>仅自己可见</option><option value="1" ${s.qzone_visibility === 1 ? "selected" : ""}>所有人可见</option></select>
  <div class="form-row"><div><label for="retention-days">成图保留天数</label><input id="retention-days" name="generated_retention_days" type="number" min="1" max="365" value="${s.generated_retention_days}"></div><div><label for="max-photos">成图数量上限</label><input id="max-photos" name="generated_max_count" type="number" min="1" max="1000" value="${s.generated_max_count}"></div></div>
  <p class="help">超期或超量会清理旧成图，清理后无法恢复。收藏和使用中的图片保留，可能暂时超限。</p>
  <div class="form-row"><div><label for="task-timeout">等待任务时限（秒）</label><input id="task-timeout" name="task_timeout" type="number" min="60" max="3600" value="${s.task_timeout}"></div><div><label for="poll">查询间隔（秒）</label><input id="poll" name="poll_interval" type="number" min="3" max="60" value="${s.poll_interval}"></div></div>
  <details><summary>存储与超时说明</summary><p class="help">原图占用 ${(state.data.storage_bytes / 1024 / 1024).toFixed(1)} MB</p>
  <p class="help">每分钟检查保留规则；没有可删图片时暂停新生图。回收站可在自动清理前恢复，清理后只保留任务元数据，用户源文件不受影响。</p>
  <p class="help">场景参考保留24小时，角色参考不自动清理，两者仅用于生图。</p>
  <p class="help">超时有任务编号时保留查询；无编号按“结果未知时允许重试”设置处理。</p></details></section></div></form>`;
  const readProviders = providerFields(s);
  main.querySelector("#settings-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.target;
    busy(main.querySelector("#save-settings"), async () => {
      const f = form.elements;
      const sites = readProviders();
      for (const el of form.querySelectorAll("[name]")) if (!el.reportValidity()) return;
      const viewed = state.settingsSite;
      await act("settings", {
        ...sites,
        max_retries: Number(f.max_retries.value),
        retry_delay: Number(f.retry_delay.value),
        parallel_backup: f.parallel_backup.checked,
        retry_unknown: f.retry_unknown.checked,
        settings_revision: s.settings_revision,
        qzone_enabled: f.qzone_enabled.checked,
        qzone_visibility: Number(f.qzone_visibility.value),
        generated_retention_days: Number(f.generated_retention_days.value),
        generated_max_count: Number(f.generated_max_count.value),
        task_timeout: Number(f.task_timeout.value),
        poll_interval: Number(f.poll_interval.value),
      });
      state.settingsSite = viewed;
      await reload();
      toast("设置已保存");
    });
  };
}

try {
  if (!bridge) throw new Error("请从 AstrBot 的“扩展功能”打开此页面");
  await bridge.ready();
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
