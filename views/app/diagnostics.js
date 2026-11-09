// A diagnostic preview belongs to one dialog and one exact selection.
const categories = {runs: "朝夕执行", images: "生图与投递", memory: "记忆整理与图片描述", recall: "召回与向量"};
const components = {features: "功能设置", models: "模型与调用设置", plans: "计划情境", characters: "绘图角色要求", persona: "当前聊天人设", summary_prompt: "拾忆自定义整理提示"};
const objectNames = {plan: "计划", character: "角色", chat: "聊天"};
const presets = {photo: ["runs", "images"], image: ["images"], memory: ["memory"], recall: ["recall"]};
const presetNames = {photo: "朝夕未发图", image: "图片生成失败", memory: "记忆整理失败", recall: "召回异常"};
const missingReasons = {LINKED_JOB_UNAVAILABLE_OR_TRUNCATED:"关联照片任务已不存在或超出本次上限",MEMORY_LOG_TABLE_UNAVAILABLE:"拾忆日志不可取得",SELECTED_PLAN_UNAVAILABLE:"所选计划已不存在",SELECTED_CHARACTER_UNAVAILABLE:"所选角色已不存在",BYTE_LIMIT:"文件体积已达上限",FIELD_LIMIT:"部分字段超出长度上限"};
let activeDispose = null;

// Defaults are ordinary manual selections. Shortcuts contribute a union;
// an explicit manual edit wins until that shortcut is enabled again.
export function diagnosticChoices(initial = []) {
  const manual = new Map(initial.map(key => [key, true])), enabled = new Set();
  const contribution = name => [...presets[name].map(key => `category:${key}`), "component:features", "component:models"];
  const has = key => manual.has(key) ? manual.get(key) : [...enabled].some(name => contribution(name).includes(key));
  return {has, enabled,
    set(key, checked) {manual.set(key, checked);},
    toggle(name) {
      if (enabled.has(name)) enabled.delete(name);
      else {enabled.add(name); contribution(name).forEach(key => {if (manual.get(key) === false) manual.delete(key);});}
    },
    state(name) {return !enabled.has(name) ? "false" : contribution(name).every(has) ? "true" : "mixed";},
    clear() {manual.clear(); enabled.clear();},
  };
}

const fieldNames = {object:"对象",collected_at:"采集时间",revision:"版本",models_revision:"模型设置版本",settings_source:"设置来源",features:"功能设置",drawing:"绘图角色要求",plan:"计划情境",models:"模型连接",host_model:"聊天模型",sites:"图片站点（执行顺序）",policy:"图片调用策略",call_policy:"整理调用策略",summary:"总结",assistant:"检查助手",embedding:"向量",rerank:"精排",summary_prompt:"自定义整理提示",name:"名称",instruction:"情境",appearance:"外观",style:"风格",failure_style:"失败表达",reference_count:"参考图片数量",reference_enabled:"参考图启用",enabled:"启用",model:"模型",type:"类型",address:"地址",key_configured:"已配置密钥",availability:"状态",mode:"连接方式",binding_mode:"保存的连接方式",inherited_from:"跟随连接",interface:"接口",timeout:"超时（秒）",task_timeout:"图片任务超时（秒）",max_retries:"最多重试次数",retry_delay:"重试间隔（秒）",poll_interval:"轮询间隔（秒）",parallel_backup:"并行备用",retry_unknown:"重试未知结果",request_max_retries:"连接重试次数",size:"图片尺寸",reference_field:"参考字段",max_context_tokens:"上下文容量",dimensions:"向量维度",photo_mode:"拍照模式",contact_mode:"联系模式",timezone:"时区",cron:"执行规则",window_before:"提前区间（分）",window_after:"延后区间（分）",run_once:"一次性计划",day_mode:"日期规则",summary_timeout:"总结超时（秒）",summary_max_retries:"总结最多重试次数",summary_trigger_turns:"总结触发轮数",keep_turns:"最近保留轮数",memory_tokens:"记忆容量（Token）",retention_days:"备份保留天数",auto_process:"自动整理",inject_enabled:"注入记忆",query_rewrite:"召回查询改写",recall_candidates:"召回候选数",recall_max:"最多召回条数",recall_rerank_min:"精排最低分",recall_vector_min:"向量最低分",image_keep_turns:"原图保留轮数",image_describe:"图片描述",vector_enabled:"向量召回",selection_source:"人设选择来源",text:"正文",component:"组件",reason:"原因"};
const valueNames = {CONFIGURED:"已选择保存连接（未测试请求）",AVAILABLE:"可取得",NOT_CONFIGURED:"未配置",CONNECTION_UNAVAILABLE:"连接不可取得",DISABLED:"关闭",NOT_RECORDED:"未记录",UNKNOWN:"未知",NO_USABLE_IMAGE_SITE:"没有可执行图片站点",HOST_MODEL_INTERFACE_UNAVAILABLE:"宿主模型接口不可取得",NO_HOST_MODEL:"未选择宿主模型",PERSONA_INTERFACE_UNAVAILABLE:"人设接口不可取得",CHAT_UNAVAILABLE:"宿主聊天不可取得",WEBCHAT_BUILTIN:"宿主内置人设",FIELD_LIMIT:"字段超过长度上限",NO_PERSONA:"没有人设",DEFAULTS_NO_SAVED_ROW:"默认值（尚未保存）",SAVED:"已保存",disabled:"关闭",host:"宿主连接",legacy:"旧版连接",current:"当前连接",adaptive:"自然联系",reminder:"明确提醒",cron:"固定规则",school_day:"上课 / 工作日",holiday_rest:"休息 / 假期",sleep_before_work:"明日早起催睡",night_owl_before_rest:"明日休息夜猫子",image_service:"图片服务",persona:"当前聊天人设",auto:"按内容决定",always:"要求拍照",never:"不拍照",preset:"独立连接",unavailable:"不可执行",summary:"总结连接",chat:"聊天",image:"图片",embedding:"向量",rerank:"精排",FORCED:"强制人设",CHAT:"聊天指定",DEFAULT:"默认",EXPLICIT_NONE:"明确不使用人设"};
const displayTime = value => {
  if (!value) return "未记录";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : "未记录";
};
const enumFields = new Set(["availability","mode","binding_mode","inherited_from","interface","type","photo_mode","contact_mode","day_mode","settings_source","selection_source","component","reason"]);
function configurationMarkup(value, esc, depth = 0, field = "") {
  if (value === null || value === undefined) return "未记录";
  if (typeof value !== "object") return esc(typeof value === "boolean" ? value ? "是" : "否" : enumFields.has(field) ? valueNames[value] || value : value);
  if (depth > 5) return "详见完整JSON";
  if (Array.isArray(value)) return value.length ? value.map((entry,index)=>`<section class="diagnostic-config-entry">${value.length > 1 ? `<strong>第 ${index+1} 项</strong>` : ""}${configurationMarkup(entry,esc,depth+1,field)}</section>`).join("") : "无";
  return `<dl class="diagnostic-config">${Object.entries(value).map(([key,entry])=>`<div><dt>${esc(fieldNames[key] || key)}</dt><dd>${key === "collected_at" ? esc(displayTime(entry)) : configurationMarkup(entry,esc,depth+1,key)}</dd></div>`).join("")}</dl>`;
}

export function disposeDiagnostics() {
  activeDispose?.();
  activeDispose = null;
}

export function diagnosticSelection(form, objects, stamp = Date.now()) {
  const checked = name => [...form.querySelectorAll(`[data-${name}]`)].filter(el => el.checked).map(el => el.dataset[name]);
  const range = form.querySelector("#diagnostic-range").value;
  const end = range === "custom" ? Date.parse(form.querySelector("#diagnostic-end").value) / 1000 : Math.floor(stamp / 1000);
  const start = range === "custom" ? Date.parse(form.querySelector("#diagnostic-start").value) / 1000 : end - Number(range) * 86400;
  if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end || end - start > 31 * 86400) throw new Error("请选择有效时间范围，最多31天");
  const selected = checked("object").map(index => objects[Number(index)]).filter(Boolean);
  if (selected.length > 100) throw new Error("最多选择100个具体对象，请先减少选择");
  const scopes = {all: form.querySelector("#diagnostic-all").checked, plans: [], characters: [], chats: []};
  selected.forEach(row => {
    if (row.kind === "plan") scopes.plans.push(row.id);
    if (row.kind === "character") scopes.characters.push(row.id);
    if (row.kind === "chat") scopes.chats.push({scope: row.scope, cid: row.cid});
  });
  const chosen = checked("category"), include = checked("component");
  if (!chosen.length) throw new Error("请至少选择一类日志");
  if (!scopes.all && !selected.length) throw new Error("请选择对象，或明确选择全部对象的日志");
  if (include.length && !selected.length) throw new Error("请选择附带信息对应的计划、角色或聊天");
  for (const [component, kind] of [["plans", "plans"], ["characters", "characters"], ["persona", "chats"], ["summary_prompt", "chats"]]) {
    if (include.includes(component) && !scopes[kind].length) throw new Error(`请为“${components[component]}”选择对应对象`);
  }
  return {categories: chosen, start: Math.floor(start), end: Math.floor(end), objects: scopes, include,
    filters: {status: form.querySelector("#diagnostic-status").value, query: form.querySelector("#diagnostic-query").value.trim()}};
}

export async function openDiagnostics(env, initial = {}) {
  const {bridge, dialog, content, showDialog, footer, escape: esc} = env;
  showDialog("导出诊断包", '<form id="diagnostic-form" class="dialog-body diagnostic-form"><p id="diagnostic-loading" role="status">正在读取可选对象…</p><div id="diagnostic-fields"></div><p id="diagnostic-error" class="history-error" role="alert" tabindex="-1" hidden></p><section id="diagnostic-preview" aria-live="polite"></section></form>', footer('<button data-close>关闭</button><button id="diagnostic-clear" type="button" hidden>取消选择</button><button id="diagnostic-generate" type="submit" form="diagnostic-form" disabled>生成预览</button><button id="diagnostic-download" class="primary" disabled>下载诊断包</button>'));
  const form = content.querySelector("#diagnostic-form");
  const fields = form.querySelector("#diagnostic-fields");
  const error = form.querySelector("#diagnostic-error");
  const preview = form.querySelector("#diagnostic-preview");
  const generate = content.querySelector("#diagnostic-generate");
  const download = content.querySelector("#diagnostic-download");
  const clear = content.querySelector("#diagnostic-clear");
  let disposed = false, generation = 0, pending = false, saved = null, objects = [];
  const urls = new Map();
  let refresh = () => {}, choices, chatDialog = null, chatBody = null, chatButton = null, chatIndex = null, chatSerial = 0;
  const alive = () => !disposed && dialog.open && content.querySelector("#diagnostic-form") === form;
  const releaseChat = (returnFocus = true) => {
    chatSerial++;
    const button = chatButton, hadSession = chatIndex !== null;
    chatButton = null; chatIndex = null;
    chatBody?.replaceChildren();
    if (chatDialog?.open) chatDialog.close();
    if (returnFocus && hadSession && alive()) {
      const target = [button,form.querySelector("#diagnostic-object-search"),content.querySelector("[data-close]")].find(el=>el?.isConnected && !el.disabled && el.getClientRects().length);
      target?.focus({preventScroll:true});
    }
  };
  const chatClosed = () => {if (!chatDialog?.open && chatIndex !== null) releaseChat();};
  const chatCancelled = event => {event.preventDefault();event.stopPropagation();releaseChat();};
  const chatKeydown = event => {
    if (event.key !== "Tab" || !chatDialog?.open) return;
    const controls = [...chatDialog.querySelectorAll('button, input, select, textarea, a[href], summary, [tabindex]')].filter(el=>!el.disabled && el.tabIndex>=0 && el.getClientRects().length);
    if (!controls.length) return;
    const index = controls.indexOf(document.activeElement);
    if (event.shiftKey && index<=0) {event.preventDefault();controls[controls.length-1].focus();}
    else if (!event.shiftKey && (index<0 || index===controls.length-1)) {event.preventDefault();controls[0].focus();}
  };
  const ensureChatDialog = () => {
    if (chatDialog) return;
    chatDialog = document.createElement("dialog");
    chatDialog.setAttribute("id", "diagnostic-chat-dialog");
    chatDialog.setAttribute("class", "diagnostic-chat-dialog");
    chatDialog.setAttribute("aria-labelledby", "diagnostic-chat-title");
    chatDialog.innerHTML = '<div class="diagnostic-chat-shell"><div class="dialog-head"><h2 id="diagnostic-chat-title">聊天预览</h2><button type="button" class="close" data-chat-close aria-label="关闭聊天预览" autofocus>×</button></div><div id="diagnostic-chat-body" class="dialog-body diagnostic-chat-preview"></div></div>';
    document.body.append(chatDialog);
    chatBody = chatDialog.querySelector("#diagnostic-chat-body");
    chatDialog.querySelector("[data-chat-close]").onclick = () => releaseChat();
    chatDialog.addEventListener("cancel", chatCancelled);
    chatDialog.addEventListener("close", chatClosed);
    chatDialog.addEventListener("keydown", chatKeydown);
  };
  const dispose = () => {
    if (disposed) return;
    disposed = true; generation++; saved = null;
    urls.forEach((timer, url) => {clearTimeout(timer); URL.revokeObjectURL(url);}); urls.clear();
    preview.replaceChildren();
    releaseChat(false);
    if (chatDialog) {
      chatDialog.removeEventListener("cancel", chatCancelled); chatDialog.removeEventListener("close", chatClosed);
      chatDialog.removeEventListener("keydown", chatKeydown);
      chatDialog.querySelector("[data-chat-close]").onclick = null;
      chatDialog.remove(); chatDialog = null; chatBody = null;
    }
    objects = [];
    dialog.removeEventListener("close", dispose);
    if (activeDispose === dispose) activeDispose = null;
  };
  activeDispose = dispose;
  dialog.addEventListener("close", dispose);
  const fail = message => {error.textContent = message; error.hidden = false; error.focus();};
  const invalidate = () => {
    generation++; saved = null; download.disabled = true; preview.replaceChildren(); error.hidden = true;
  };
  form.addEventListener("input", event => {if (event.target.type !== "checkbox" && event.target.id !== "diagnostic-object-search") {invalidate(); refresh();}});
  form.addEventListener("change", event => {if (!event.target.dataset.group && !["diagnostic-history","diagnostic-object-search"].includes(event.target.id)) {invalidate(); refresh();}});
  let available;
  try { available = await bridge.apiGet("diagnostics/options", initial.scope && initial.cid ? {scope: initial.scope, cid: initial.cid} : undefined); }
  catch { if (alive()) {form.querySelector("#diagnostic-loading").hidden = true; fail("可选对象读取失败，请关闭后重试");} return; }
  if (!alive()) {dispose(); return;}
  objects = available.objects || [];
  const historical = row => row.kind === "chat" && row.availability === "CHAT_UNAVAILABLE";
  const selected = row => row.kind === "character" && row.id === initial.character || row.kind === "plan" && ((initial.plans || []).includes(row.id) || initial.scope && !initial.cid && row.scope === initial.scope) || row.kind === "chat" && row.scope === initial.scope && (!initial.cid || row.cid === initial.cid);
  if (initial.scope && initial.cid && !objects.some(row => row.kind === "chat" && row.scope === initial.scope && row.cid === initial.cid)) objects.push({kind: "chat", id: "current-filter", scope: initial.scope, cid: initial.cid, label: "当前筛选聊天（元数据不可取得）", availability:"METADATA_UNAVAILABLE"});
  const initialHistory = initial.scope && initial.cid ? objects.findIndex(row => historical(row) && row.scope===initial.scope && row.cid===initial.cid) : -1;
  const hasInitial = Boolean(initial.scope || initial.character || initial.plans?.length);
  const chosenCategories = initial.categories || ["runs", "images", "memory", "recall"];
  const statusOptions = {"":"全部状态",failed:"失败",sent:"已发送",ready:"已生成",queued:"排队",running:"执行中",held:"已保留",skipped:"未执行",unknown:"结果未知",cancelled:"已取消"};
  if (initial.status && !(initial.status in statusOptions)) statusOptions[initial.status] = `当前筛选：${initial.status}`;
  const localDate = stamp => {
    const value = new Date(stamp); value.setMinutes(value.getMinutes() - value.getTimezoneOffset()); return value.toISOString().slice(0, 16);
  };
  const groupTitle = (key,label) => `<label class="diagnostic-group-choice"><input type="checkbox" data-group="${key}" aria-label="选择当前列表中的${label}"><span>${label}</span><span data-count="${key}"></span></label>`;
  const rowMeta = row => [selected(row) ? row.kind === "chat" ? historical(row) ? "当前历史筛选" : "当前聊天" : "当前对象" : "", row.platform, row.updated_at ? `最近活动 ${displayTime(row.updated_at)}` : "", row.kind === "chat" ? row.short_id || row.cid?.slice(-8) : "", historical(row) ? "聊天已删除，仅保留历史记录" : row.availability === "METADATA_UNAVAILABLE" ? "聊天元数据不可取得" : ""].filter(Boolean).join(" · ");
  fields.innerHTML = `<fieldset class="diagnostic-presets"><legend>快捷选择</legend><div class="actions">${Object.entries(presetNames).map(([key,label])=>`<button type="button" data-preset="${key}" aria-pressed="false">${label}</button>`).join("")}</div></fieldset>
    <fieldset><legend>${groupTitle("category","日志类别")}</legend><div class="diagnostic-checks">${Object.entries(categories).map(([key, label]) => `<label><input type="checkbox" data-category="${key}" ${chosenCategories.includes(key) ? "checked" : ""}>${label}</label>`).join("")}</div></fieldset>
    <div class="diagnostic-range"><label>时间<select id="diagnostic-range"><option value="1">近24小时</option><option value="7">近7天</option><option value="custom">自定义（最多31天）</option></select></label><div id="diagnostic-custom" hidden><label>开始<input id="diagnostic-start" type="datetime-local" value="${localDate(Date.now() - 86400000)}"></label><label>结束<input id="diagnostic-end" type="datetime-local" value="${localDate(Date.now())}"></label></div></div>
    <details ${initial.status || initial.query ? "open" : ""}><summary>状态与日志搜索</summary><label>状态<select id="diagnostic-status">${Object.entries(statusOptions).map(([key,label])=>`<option value="${esc(key)}" ${initial.status===key?"selected":""}>${esc(label)}</option>`).join("")}</select></label><label>日志搜索<input id="diagnostic-query" maxlength="100" value="${esc(initial.query || "")}"></label></details>
    <fieldset><legend>对象</legend><label class="diagnostic-check"><input type="checkbox" id="diagnostic-all" ${!hasInitial ? "checked" : ""}>全部对象的日志</label><p class="help">附带信息仅取明确勾选的对象；使用已保存配置。</p><label class="diagnostic-check"><input type="checkbox" id="diagnostic-history">包含历史聊天</label><p id="diagnostic-history-hint" class="help" ${initialHistory < 0 ? "hidden" : ""}>该聊天已删除，可开启包含历史聊天查看</p><label>对象名称搜索<input id="diagnostic-object-search" type="search" maxlength="100"></label><p id="diagnostic-object-limit" class="history-error" role="status" hidden></p>${Object.entries(objectNames).map(([kind, label]) => `<div class="diagnostic-object-group"><input type="checkbox" data-group="${kind}" aria-label="选择当前列表中的${label}"><details class="diagnostic-objects" data-object-details="${kind}" ${objects.some(row => row.kind === kind && selected(row)) ? "open" : ""}><summary>${label} <span data-count="${kind}"></span></summary><p class="help" data-group-scope="${kind}"></p><div class="diagnostic-object-list">${objects.map((row, index) => row.kind === kind ? `<div data-object-row="${index}"><div class="diagnostic-object-line"><label><input type="checkbox" data-object="${index}" ${selected(row) && !historical(row) ? "checked" : ""}><span>${esc(row.label || label)}<small>${esc(rowMeta(row))}</small></span></label>${kind === "chat" ? `<button type="button" data-chat-preview="${index}" aria-haspopup="dialog">${historical(row) ? "详情" : "预览"}</button>` : ""}</div></div>` : "").join("") || '<span class="help">暂无可选对象</span>'}</div></details><p class="history-error" data-group-error="${kind}" role="status" hidden></p></div>`).join("")}${available.truncated ? '<p class="history-error">对象列表仅显示部分记录；组选择只作用当前列表。可从对应聊天或计划进入。</p>' : ""}</fieldset>
    <div class="diagnostic-object-group"><input type="checkbox" data-group="component" aria-label="选择全部附带排查信息"><details id="diagnostic-include"><summary>附带排查信息 <span data-count="component"></span></summary><div class="diagnostic-checks">${Object.entries(components).map(([key, label]) => `<label><input type="checkbox" data-component="${key}">${label}</label>`).join("")}</div></details><p id="diagnostic-private-note" class="help"><span id="diagnostic-selected-text"></span>文字可能包含私人信息。会遮蔽已知密钥，但不能识别全部隐私，请检查预览后再分享。</p></div><p id="diagnostic-selection-status" class="help" role="status" hidden></p>`;
  form.querySelector("#diagnostic-loading").hidden = true;
  const selectionInputs = () => [...form.querySelectorAll("[data-category],[data-component],[data-object],#diagnostic-all")];
  const inputKey = el => el.dataset.category ? `category:${el.dataset.category}` : el.dataset.component ? `component:${el.dataset.component}` : el.dataset.object !== undefined ? `object:${el.dataset.object}` : "all";
  choices = diagnosticChoices(selectionInputs().filter(el=>el.checked).map(inputKey));
  const historyToggle = form.querySelector("#diagnostic-history");
  const objectMatches = row => (!historical(row) || historyToggle.checked) && (row.label || "").toLocaleLowerCase().includes(form.querySelector("#diagnostic-object-search").value.trim().toLocaleLowerCase());
  const groupInputs = key => key === "category" || key === "component" ? [...form.querySelectorAll(`[data-${key}]`)] : [...form.querySelectorAll("[data-object]")].filter(el=>objects[Number(el.dataset.object)].kind === key && objectMatches(objects[Number(el.dataset.object)]));
  const objectLimit = form.querySelector("#diagnostic-object-limit");
  const setManual = (inputs, checked) => {
    const selectedCount = selectionInputs().filter(el=>el.dataset.object !== undefined && choices.has(inputKey(el))).length;
    const additions = inputs.filter(el=>el.dataset.object !== undefined && !choices.has(inputKey(el))).length;
    if (checked && selectedCount + additions > Math.min(100, available.limits?.max_objects || 100)) {
      const group = inputs.find(el=>el.dataset.object !== undefined);
      const message = group ? form.querySelector(`[data-group-error="${objects[Number(group.dataset.object)].kind}"]`) : objectLimit;
      message.textContent = "最多选择100个具体对象，请减少其它选择或缩小搜索范围。"; message.hidden = false; refresh(); return false;
    }
    inputs.forEach(el=>choices.set(inputKey(el),checked)); objectLimit.hidden = true; form.querySelectorAll("[data-group-error]").forEach(el=>{el.hidden=true;}); invalidate(); refresh(); return true;
  };
  refresh = () => {
    if (!choices || !alive()) return;
    selectionInputs().forEach(el=>{el.checked=choices.has(inputKey(el));});
    form.querySelectorAll("[data-object-row]").forEach(el=>{
      el.hidden=!objectMatches(objects[Number(el.dataset.objectRow)]);
      if(el.hidden && chatIndex === el.dataset.objectRow) releaseChat(false);
    });
    form.querySelectorAll("[data-group]").forEach(el=>{
      const items=groupInputs(el.dataset.group), count=items.filter(input=>input.checked).length;
      el.checked=items.length>0 && count===items.length; el.indeterminate=count>0 && count<items.length; el.disabled=!items.length;
      el.setAttribute("aria-checked",el.indeterminate?"mixed":String(el.checked));
      form.querySelector(`[data-count="${el.dataset.group}"]`).textContent=`${count}/${items.length}`;
      const scope=form.querySelector(`[data-group-scope="${el.dataset.group}"]`);
      if(scope)scope.textContent=`选择${form.querySelector("#diagnostic-object-search").value.trim()?"搜索结果":"当前列表"}（${items.length}个）`;
    });
    form.querySelectorAll("[data-preset]").forEach(button=>{
      const status=choices.state(button.dataset.preset); button.setAttribute("aria-pressed",status);
      button.textContent=(status==="true"?"✓ ":status==="mixed"?"− ":"")+presetNames[button.dataset.preset]+(status==="mixed"?" · 已调整":"");
    });
    const selectedText=["models","plans","characters","persona","summary_prompt"].filter(key=>choices.has(`component:${key}`));
    form.querySelector("#diagnostic-selected-text").textContent=selectedText.length?`已选文字项：${selectedText.map(key=>components[key]).join("、")}。`:"";
    clear.hidden=!selectionInputs().some(el=>el.checked) && !choices.enabled.size;
    const selectionStatus = form.querySelector("#diagnostic-selection-status");
    try {diagnosticSelection(form,objects); generate.disabled=pending; selectionStatus.hidden=true;}
    catch (err) {generate.disabled=true; selectionStatus.textContent=err.message; selectionStatus.hidden=clear.hidden;}
  };
  selectionInputs().forEach(el=>el.addEventListener("change",()=>setManual([el],el.checked)));
  form.querySelectorAll("[data-group]").forEach(el=>el.onchange=()=>{
    const items=groupInputs(el.dataset.group);setManual(items,!items.length||!items.every(input=>choices.has(inputKey(input))));
  });
  form.querySelector("#diagnostic-object-search").oninput=()=>{objectLimit.hidden=true;form.querySelectorAll("[data-group-error]").forEach(el=>{el.hidden=true;});refresh();};
  form.querySelector("#diagnostic-range").onchange=()=>{form.querySelector("#diagnostic-custom").hidden=form.querySelector("#diagnostic-range").value!=="custom";refresh();};
  form.querySelectorAll("[data-preset]").forEach(button=>button.onclick=()=>{choices.toggle(button.dataset.preset);form.querySelector("#diagnostic-include").open=true;invalidate();refresh();});
  clear.onclick=()=>{choices.clear();invalidate();objectLimit.hidden=true;form.querySelectorAll("[data-group-error]").forEach(el=>{el.hidden=true;});releaseChat();refresh();};
  historyToggle.onchange = () => {
    if (!historyToggle.checked) {
      selectionInputs().filter(el=>el.dataset.object !== undefined && historical(objects[Number(el.dataset.object)])).forEach(el=>choices.set(inputKey(el),false));
      if (chatIndex !== null && historical(objects[Number(chatIndex)])) releaseChat(false);
      invalidate();
    }
    form.querySelector("#diagnostic-history-hint").hidden=initialHistory<0 || historyToggle.checked;
    refresh();
    if (historyToggle.checked && initialHistory >= 0) {
      form.querySelector('[data-object-details="chat"]').open=true;
      form.querySelector(`[data-object-row="${initialHistory}"]`).scrollIntoView({block:"nearest"});
    }
  };
  form.querySelectorAll("[data-object-details]").forEach(group=>group.addEventListener("toggle",()=>{
    if(!group.open && group.dataset.objectDetails === "chat") releaseChat(false);
  }));
  const chatMetadata = row => `<strong>${esc(row.label || "未命名聊天")}</strong><dl class="diagnostic-config"><div><dt>创建时间</dt><dd>${esc(displayTime(row.created_at))}</dd></div><div><dt>最近活动</dt><dd>${esc(displayTime(row.updated_at))}</dd></div></dl><details><summary>聊天身份</summary><p>${esc(row.scope)} · ${esc(row.cid)}</p></details>`;
  form.querySelectorAll("[data-chat-preview]").forEach(button=>button.onclick=async()=>{
    const index=button.dataset.chatPreview,row=objects[Number(index)];
    if(!alive() || !objectMatches(row) || chatIndex===index && chatDialog?.open)return;
    ensureChatDialog();
    const serial=++chatSerial;
    chatIndex=index;chatButton=button;
    chatBody.innerHTML=chatMetadata(row)+(historical(row)?'<p>聊天已删除，仅保留历史记录；没有宿主正文可预览。</p>':'<p role="status">正在读取辨认片段…</p>');
    if(!chatDialog.open)chatDialog.showModal();
    if(historical(row))return;
    const current=()=>alive() && chatSerial===serial && chatDialog?.open && chatIndex===index;
    try{
      const result=await bridge.apiPost("diagnostics/chat-preview",{scope:row.scope,cid:row.cid});
      if(!current())return;
      const metadata=result.metadata||row;
      const messages=Array.isArray(result.messages)?result.messages.slice(0,6):[];
      const availability={EMPTY:"暂无可预览文字",CONTENT_TOO_LARGE:"正文过大，仅显示聊天信息",CONTENT_UNAVAILABLE:"正文不可取得，仅显示聊天信息",CHAT_UNAVAILABLE:"聊天已删除，仅保留历史记录；没有宿主正文可预览。",METADATA_UNAVAILABLE:"聊天信息不可取得"};
      chatBody.innerHTML=chatMetadata(metadata)+(messages.length?messages.map(message=>`<p><strong>${message.role==="user"?"用户":"助手"}</strong> ${esc(String(message.text||"").slice(0,200))}</p>`).join(""):`<p>${esc(availability[result.availability]||"暂无可预览文字")}</p>`)+(result.truncated?'<p class="help">仅展示部分文字，用于确认聊天；不会加入诊断包。</p>':'<p class="help">仅用于确认聊天，不会加入诊断包。</p>');
    }catch{if(current())chatBody.innerHTML=chatMetadata(row)+'<p class="history-error" role="alert">预览读取失败，请关闭后重试。</p>';}
  });
  refresh();
  form.onsubmit = async event => {
    event.preventDefault();
    if (!alive() || pending) return;
    invalidate();
    let selection;
    try {selection = diagnosticSelection(form, objects);} catch (err) {fail(err.message); return;}
    const own = generation;
    pending = true; generate.disabled = true; generate.textContent = "正在生成预览…"; generate.setAttribute("aria-busy", "true");
    try {
      const result = await bridge.apiPost("diagnostics/preview", selection);
      if (!alive() || own !== generation) return;
      if (typeof result.json !== "string" || new Blob([result.json]).size > (available.limits?.max_bytes || 5242880)) throw new Error("诊断包超过大小上限，请缩小范围");
      const document = JSON.parse(result.json);
      saved = {json: result.json, filename: /^lumen-diagnostics-[\w-]+\.json$/.test(result.filename) ? result.filename : "lumen-diagnostics.json"};
      const summary = result.summary || {};
      const included = summary.included_components || [];
      const configuration = document.current_configuration || [];
      const hasConfiguration = Object.keys(configuration).length > 0;
      const missing = summary.missing || [];
      const textItems = summary.free_text_components || [];
      const objectCounts = summary.object_counts || {};
      const objectSummary = Object.entries({plans:"计划",characters:"角色",chats:"聊天"}).filter(([key])=>Number.isInteger(objectCounts[key])).map(([key,label])=>`${label} ${objectCounts[key]} 个`).join(" · ");
      preview.innerHTML = `<h3>预览</h3>${objectSummary ? `<p>本包涉及：${esc(objectSummary)}</p>` : ""}<p>${Object.entries(summary.counts || {}).map(([key, count]) => `${esc(categories[key] || key)} ${Number(count) || 0} 条`).join(" · ")} · ${(new Blob([saved.json]).size / 1024).toFixed(1)} KiB</p>${summary.truncated ? '<p class="history-error">部分导出：已达资源上限，请缩小范围以取得更多记录。</p>' : ""}<p>附带：${esc(included.map(key => components[key] || key).join("、") || (hasConfiguration ? "见下方配置" : "无"))}</p>${textItems.length ? `<p class="history-error">包含文字项：${esc(textItems.map(key => components[key] || key).join("、"))}。分享前请展开检查。</p>` : ""}${missing.length ? `<details><summary>缺失与未收录说明</summary><p>${esc(missing.map(code => missingReasons[code] || "部分资料不可取得，详见JSON").join("；"))}</p></details>` : ""}${hasConfiguration ? '<details><summary>检查附带的当前配置</summary><div id="diagnostic-configuration"></div></details>' : ""}<details><summary>查看完整JSON</summary><pre id="diagnostic-json"></pre></details>`;
      const config = preview.querySelector("#diagnostic-configuration");
      if (config) config.innerHTML = configurationMarkup(configuration, esc);
      preview.querySelector("#diagnostic-json").textContent = saved.json;
      download.disabled = false;
    } catch (err) {if (alive() && own === generation) fail(err.message || "诊断预览失败，请重试");}
    finally {
      pending = false;
      if (alive()) {generate.textContent = "生成预览"; generate.removeAttribute("aria-busy"); refresh();}
      else dispose();
    }
  };
  download.onclick = () => {
    if (!alive() || pending || !saved || download.disabled) return;
    download.disabled = true;
    try {
      const url = URL.createObjectURL(new Blob([saved.json], {type: "application/json;charset=utf-8"}));
      const link = document.createElement("a"); link.href = url; link.download = saved.filename;
      link.click(); link.remove();
      urls.set(url, setTimeout(() => {URL.revokeObjectURL(url); urls.delete(url); if (alive() && saved) download.disabled = false;}, 1000));
    } catch {download.disabled = false; fail("下载未能开始，请重试");}
  };
}
