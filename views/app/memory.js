// 拾忆回响：版式参照小白X剧情总结。只显示记忆内容，不显示原文。
const view = {target: null, tab: "story", query: "", role: "", descending: true, planFilter: "open", settingsTab: "rules"};
const ROLE_ALL = "", ROLE_NONE = "未标注";
let mountNumber = 0;
let pollTimer;
let followTimer;
let closeMaintenance = null;

export function parseMemoryImport(text) {
  if (!text.trim()) throw new Error("请选择记忆文件");
  let value;
  try { value = JSON.parse(text); } catch { throw new Error("JSON格式不正确"); }
  if (!value || value.format !== "lumen-memory" || typeof value.memory !== "object") throw new Error("请选择拾忆回响导出的记忆文件");
  return value;
}

const turnLabel = (e) => e.turns ? `(#${e.turns[0]}${e.turns[1] !== e.turns[0] ? "-" + e.turns[1] : ""})` : "";

export function eventMarkup(event, esc) {
  const people = (event.participants || []).map(p => `<button class="lj-text" data-person="${esc(p)}">${esc(p)}</button>`).join("");
  return `<article class="lj-event" id="lj-event-${esc(event.id)}"><div class="lj-event-time">${esc(event.date || "")}</div><div class="lj-event-main"><button class="lj-event-title" data-event="${esc(event.id)}">${esc(event.title)}</button><p>${esc(event.summary)} <span class="lj-turns">${turnLabel(event)}</span></p><div class="lj-event-meta">${people}<span>${esc(event.role || ROLE_NONE)}</span>${event.causes?.length ? `<button class="lj-text" data-event="${esc(event.id)}">前因 ${event.causes.length}</button>` : ""}</div></div></article>`;
}

function dateText(seconds) {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"});
}


// The maintenance view owns only bounded memory metadata/results, never source bodies.
export function openMemoryMaintenance(env) {
  closeMaintenance?.();
  const {bridge, showDialog, footer, escape:esc, target, isCurrent, onChange=()=>{}, openAssistantSettings}=env;
  const settingsMarkup='<details id="mm-settings-section" class="mm-settings-section"><summary id="mm-settings">助手设置</summary><div id="mm-settings-content"></div></details>';
  let assistantPanel=null,assistantOpening=false;
  if (!target?.scope || !target?.cid) {
    showDialog("记忆助手", `<div id="maintenance-empty" class="memory-dialog lj-dialog-body">${settingsMarkup}<p class="lj-muted">选择聊天后可检查记忆或查看结果。</p></div>`, footer('<button type="button" data-close>关闭</button>'));
    const dialog=document.getElementById("dialog"),root=document.getElementById("maintenance-empty");
    let disposed=false;
    const valid=()=>!disposed&&isCurrent()&&dialog.open&&root.isConnected;
    root.querySelector("#mm-settings").onclick=async event=>{
      const section=root.querySelector("#mm-settings-section");if(section.open)return;
      event?.preventDefault?.();section.open=true;
      if(assistantOpening||assistantPanel)return;
      assistantOpening=true;
      try{const session=await openAssistantSettings?.({container:root.querySelector("#mm-settings-content"),isCurrent:valid});if(valid())assistantPanel=session;else session?.dispose();}
      finally{assistantOpening=false;}
    };
    const dispose=()=>{if(disposed)return;disposed=true;assistantPanel?.dispose();dialog.removeEventListener("close",dispose);if(closeMaintenance===dispose)closeMaintenance=null;};
    closeMaintenance=dispose;dialog.addEventListener("close",dispose,{once:true});
    return {dispose,refresh:()=>{}};
  }
  const frozen={scope:target.scope,cid:target.cid};
  const labels={events:"剧情",people:"人物",profiles:"生活档案",plans:"记忆约定",overview:"概况",keywords:"关键词"};
  const states={queued:"等待检查",reading:"核对来源",requesting:"正在检查",validating:"校验建议",committing:"保存修正",completed:"已完成",findings_only:"只报告疑点",partial:"部分完成",cancelled:"已停止",stale:"资料已变化",failed:"检查失败",interrupted:"执行已中断",undone:"已撤销",unknown:"结果待核对"};
  const reasons={BATCH_LIMIT:"最多选择120个批次，请减少批次选择",ITEM_MISSING:"此条记忆已删除，请关闭后重新选择",FACT:"事实核对",OWNER:"归属核对",STATUS:"状态核对",REFERENCE:"引用核对",DUPLICATE:"可能重复",UNCERTAIN:"依据不足",SCOPE:"超出修正范围",SOURCE_MISSING:"缺少完整原文",PROTECTED:"助手不自动修改",DISABLED:"助手或聊天记忆未启用",CONNECTION_MISSING:"当前聊天的助手连接不可用，请在设置中核对",CHAT_DELETED:"聊天已删除，未提交修正",HOST_METADATA_UNAVAILABLE:"聊天元数据暂不可核验，检查已停止",STALE:"资料或设置已变化，请重新预览",SOURCE_CHANGED:"来源已变化",CALL_LIMIT:"已到调用上限",TIME_LIMIT:"已到总时限",QUEUE_FULL:"等待队列已满",QUEUE_EXPIRED:"等待超时",CAPACITY:"所选范围或资料超出上限",PROTOCOL:"返回格式不符合要求",JSON_FORMAT:"返回JSON格式不正确",CONTENT_SAFETY:"内容未通过安全校验",CANCELLED:"已停止",REQUEST_FAILED:"模型请求失败",REQUEST_TIMEOUT:"模型请求超时",MODEL_AUTH:"模型鉴权失败，请检查连接",MODEL_CONFIG:"模型配置不正确",COMMIT_UNKNOWN:"提交结果待核对，请刷新",STORAGE:"结果保存失败",INTERRUPTED:"执行已中断",UNDO_UNAVAILABLE:"此单元已无法撤销，原结果已清理或没有可撤销修正",UNDO_CONFLICT:"后续修改与本单元冲突，未撤销任何记忆",MAINTENANCE_DEPENDENCY:"请先逆序撤销依赖本次总结的助手修正",MAINTENANCE_EXPIRED:"依赖的助手修正详情已清理，无法安全撤销本次总结"};
  const reason=code=>reasons[code]||"操作未完成，请刷新核对结果";
  const number=v=>Number.isSafeInteger(v)&&v>=0?v:0;
  const sourceText=list=>(Array.isArray(list)?list:[]).map(source=>`轮次 ${number(source.ordinal)}${source.batch?` · 批次 ${number(source.batch)}`:""}`).join("；");
  showDialog("记忆助手",`<div id="maintenance-dialog" class="memory-dialog lj-dialog-body lj-maintenance"><p class="lj-muted">${esc(target.title||"当前聊天")}</p><p id="mm-error" class="memory-error" role="alert" hidden></p>${settingsMarkup}<div class="lj-actions"><label class="check"><input id="mm-pause" type="checkbox">暂停此聊天的总结后自动检查</label><button type="button" class="lj-btn" id="mm-refresh">刷新结果</button></div><p id="mm-status" role="status" aria-live="polite"></p><details id="mm-scope" open><summary>选择要检查的记忆</summary><label for="mm-search">查找记忆</label><input id="mm-search" type="search" placeholder="按记忆名称查找"><div class="lj-actions"><button type="button" class="lj-btn" id="mm-select">选择可见项目</button><button type="button" class="lj-btn" id="mm-clear">清空选择</button><span id="mm-count" class="lj-muted"></span></div><p id="mm-capacity" class="memory-error" role="alert" hidden>已超过120项，请减少选择后再预览；当前选择未被截断。</p><div class="lj-actions"><button type="button" class="lj-btn" id="mm-latest" disabled>最近一次总结涉及的记忆</button><span id="mm-latest-info" class="lj-muted"></span></div><div id="mm-items" class="mm-items"></div><details id="mm-batches"><summary>或按总结批次选择实际变化</summary><div id="mm-batch-items"></div></details><label class="check"><input id="mm-apply" type="checkbox">有明确依据时应用修正</label><label class="check"><input id="mm-protected" type="checkbox" disabled>允许本次修正选中的受保护条目</label><p class="lj-muted">默认只报告疑点。最多120项，每6项为一个单元；记忆约定修正不会操作真实计划。</p><button type="button" class="lj-btn" id="mm-preview">预览范围与费用</button><div id="mm-preview-result" role="status" hidden></div><button type="button" class="lj-btn lj-btn-primary" id="mm-start" hidden>确认开始检查</button></details><div class="lj-section-head"><h3>检查结果</h3><button type="button" class="lj-btn" id="mm-stop" hidden>停止检查</button></div><div id="mm-results"></div><details id="mm-limits"><summary>保留期与处理上限</summary><p class="lj-muted">私有修正及疑点详情默认保留30天；每聊天100个单元、全局1000个单元或128MiB容量用满时，较旧的已结束详情可提前清理，撤销资料清理后不可撤销。人工保护不随详情清理。</p><p class="lj-muted">手动一次最多120项，每单元6个主项目；最多24个只读依赖、18次本地读取操作、200个完整文字轮次。请求受所选连接窗口限制，单请求16MiB、响应2MiB；超限停止或标记未检查，不截断为完整证据。费用预览只限制助手调用，相关向量更新可能另计。</p></details></div>`,footer('<button type="button" data-close>关闭</button>'));
  const dialog=document.getElementById("dialog"),root=document.getElementById("maintenance-dialog");
  const q=id=>root.querySelector("#mm-"+id);
  let disposed=false,writing=false,reading=false,previewing=false,readVersion=0,serial=0,timer=null,current=null,preview=null,requestId=null,pendingStart=null,resultSignature="",itemSignature="";
  const selected=new Set(),selectedBatches=new Set(),batchSelections=new Map();
  const valid=()=>!disposed&&isCurrent()&&dialog.open&&root.isConnected;
  const identity=item=>JSON.stringify([item.collection,item.id]);
  const showError=code=>{if(valid()){q("error").textContent=reason(code);q("error").hidden=false;}};
  const invalidate=()=>{serial++;preview=null;requestId=null;q("preview-result").textContent="";q("preview-result").hidden=true;q("start").hidden=!pendingStart;};
  const selectedIds=()=>new Set([...selected,...[...batchSelections.values()].flat()]);
  const count=()=>{const size=selectedIds().size;q("count").textContent=`已选 ${size} 项${selectedBatches.size?` · ${selectedBatches.size} 个批次`:""}`;q("capacity").hidden=size<=120;};
  const updateControls=()=>{
    q("preview").disabled=writing||previewing||!!pendingStart||!current?.enabled||!current?.configured||!!current?.running||!selectedIds().size||selectedIds().size>120;
    q("start").disabled=writing||(!preview&&!pendingStart)||!!current?.running;
    q("stop").hidden=!current?.running;q("stop").disabled=writing;
    q("refresh").disabled=reading||writing;
    for(const input of root.querySelectorAll("[data-mm-item], [data-mm-batch], [data-mm-protect]"))input.disabled=writing||!!pendingStart;
    for(const id of ["apply","select","clear","pause","latest"])q(id).disabled=writing||!!pendingStart;
    q("protected").disabled=writing||!!pendingStart||!q("apply").checked;
    q("latest").disabled=writing||!!pendingStart||!current?.latest_batch;
  };
  const filter=()=>{
    const query=q("search").value.trim().toLowerCase();
    root.querySelectorAll("[data-mm-row]").forEach(row=>{row.hidden=!(row.dataset.mmSearch||"").includes(query);});
    root.querySelectorAll("[data-mm-group]").forEach(group=>{group.hidden=!!query&&![...group.querySelectorAll("[data-mm-row]")].some(row=>!row.hidden);});
  };
  const selectedKeys=()=>[...selectedIds()].map(value=>{const [collection,id]=JSON.parse(value);return {collection,id};});
  const batchKeys=batch=>(batch?.keys||[]).map(identity);
  const refreshIncluded=()=>{
    const included=new Set([...batchSelections.values()].flat());
    for(const node of root.querySelectorAll("[data-mm-included]"))node.textContent=included.has(node.dataset.mmIncluded)?"所选批次包含":"";
    for(const input of root.querySelectorAll("[data-mm-batch]"))input.checked=selectedBatches.has(Number(input.dataset.mmBatch));
  };
  const chooseBatch=(batch,checked)=>{
    if(!batch||writing||pendingStart)return;
    if(checked&&(batch.state&&batch.state!=="applied"||!selectedBatches.has(batch.id)&&selectedBatches.size>=120)){refreshIncluded();showError(batch.state&&batch.state!=="applied"?"STALE":"BATCH_LIMIT");return;}
    if(checked){selectedBatches.add(batch.id);batchSelections.set(batch.id,batchKeys(batch));}
    else {selectedBatches.delete(batch.id);batchSelections.delete(batch.id);}
    invalidate();refreshIncluded();count();updateControls();
  };
  let detailDialog=null,detailBody=null,detailItem=null,detailButton=null,detailSerial=0,detailPending=false,detailQueued=null,detailScroll=null;
  const detailAlive=token=>valid()&&detailDialog?.open&&token===detailSerial;
  const releaseDetail=(returnFocus=true)=>{
    detailSerial++;detailQueued=null;const button=detailButton,scroll=detailScroll;detailButton=null;detailItem=null;detailScroll=null;
    detailBody?.replaceChildren();
    if(detailDialog?.open)detailDialog.close();
    if(returnFocus&&valid()){
      const focus=[button,q("search"),q("settings")].find(node=>node?.isConnected&&!node.disabled&&node.getClientRects().length);
      focus?.focus({preventScroll:true});
      if(scroll){for(const [node,top,left]of scroll.nodes)if(node.isConnected){node.scrollTop=top;node.scrollLeft=left;}window.scrollTo?.(scroll.x,scroll.y);}
    }
  };
  const detailCancel=event=>{event.preventDefault();event.stopPropagation();releaseDetail();};
  const detailClosed=()=>{if(!detailDialog?.open&&detailItem)releaseDetail();};
  const detailKeyboard=event=>{
    if(event.key==="Escape"){event.preventDefault();event.stopPropagation();releaseDetail();return;}
    if(event.key!=="Tab")return;
    const nodes=[...detailDialog.querySelectorAll('button, summary, [tabindex]')].filter(node=>!node.disabled&&node.tabIndex>=0&&node.getClientRects().length);
    const index=nodes.indexOf(document.activeElement);
    if(event.shiftKey&&index<=0){event.preventDefault();nodes.at(-1)?.focus();}
    else if(!event.shiftKey&&(index<0||index===nodes.length-1)){event.preventDefault();nodes[0]?.focus();}
  };
  const ensureDetail=()=>{
    if(detailDialog)return;
    detailDialog=document.createElement("dialog");detailDialog.className="mm-detail-dialog";detailDialog.id="mm-detail-dialog";
    detailDialog.setAttribute("aria-labelledby","mm-detail-title");
    detailDialog.innerHTML='<div class="mm-detail-shell"><div class="dialog-head"><h2 id="mm-detail-title">记忆详情</h2><button type="button" class="close" id="mm-detail-close" aria-label="关闭记忆详情" autofocus>×</button></div><div id="mm-detail-body" class="dialog-body" tabindex="0"></div></div>';
    document.body.append(detailDialog);detailBody=detailDialog.querySelector("#mm-detail-body");
    detailDialog.querySelector("#mm-detail-close").onclick=()=>releaseDetail();
    detailDialog.addEventListener("cancel",detailCancel);detailDialog.addEventListener("close",detailClosed);detailDialog.addEventListener("keydown",detailKeyboard);
  };
  const renderDetail=(result,item)=>{
    const value=result.value,relations=result.relations||[],fields=[];
    const ref=(collection,id)=>{const row=relations.find(row=>row.collection===collection&&row.id===id);return row?`${row.label||id}${row.missing?"（已不存在）":""}`:String(id??"");};
    const list=(values,collection)=>(Array.isArray(values)?values:[]).map(value=>collection?ref(collection,value):String(value)).join("、");
    const add=(label,value)=>{if(value!==undefined&&value!==null&&value!=="")fields.push([label,String(value)]);};
    if(typeof value==="string")add(labels[item.collection],value);
    else if(item.collection==="keywords"&&Array.isArray(value))for(const word of value)add(word&&typeof word==="object"?word.weight||"关键词":"关键词",word&&typeof word==="object"?word.text:word);
    else if(value&&typeof value==="object"){
      add("标题",value.title);add("姓名",value.name);add("其他称呼",list(value.aliases));add("介绍",value.description);
      add(item.collection==="plans"?"约定":"经过",value.summary);add("内容",value.text);
      if(value.person)add("所属人物",ref("people",value.person));
      if(value.kind)add("档案类别",({preference:"偏好",habit:"习惯",recent:"近况",background:"背景"})[value.kind]||value.kind);
      add("时间",value.time);add("日期",value.date);add("持续到",value.until);add("类别",value.role);
      if(value.status)add("状态",({proposed:"提议中",agreed:"已约定",ongoing:"进行中",paused:"暂停",done:"已完成",cancelled:"已取消",failed:"未能完成"})[value.status]||value.status);
      add("原因",value.reason);add("参与人物",list(value.participants,"people"));add("前因",list(value.causes,"events"));add("相关剧情",list(value.events,"events"));
      for(const relation of Array.isArray(value.relationships)?value.relationships:[]){if(!relation)continue;add(`与${ref("people",relation.to)}的关系`,relation.text);for(const history of Array.isArray(relation.history)?relation.history:[])add("此前关系",history?.text);}
    }
    detailBody.innerHTML=`<dl id="mm-detail-value" class="mm-detail-fields">${fields.map(([label],i)=>`<dt>${esc(label)}</dt><dd data-mm-detail-text="${i}"></dd>`).join("")}</dl><p id="mm-detail-relations" class="lj-muted"></p><details id="mm-detail-technical"><summary>完整字段与编号</summary><p id="mm-detail-identity" class="lj-muted"></p><pre id="mm-detail-raw"></pre></details>`;
    for(const node of detailBody.querySelectorAll("[data-mm-detail-text]"))node.textContent=fields[Number(node.dataset.mmDetailText)][1];
    detailBody.querySelector("#mm-detail-relations").textContent=relations.map(row=>`${labels[row.collection]||"记忆"}：${row.label||row.id}${row.missing?"（已不存在）":""}`).join("；");
    detailBody.querySelector("#mm-detail-identity").textContent=`条目编号 ${item.id} · 版本 ${result.version}`;
    detailBody.querySelector("#mm-detail-raw").textContent=pretty(value);
  };
  const readDetail=async(item,expected,token)=>{
    detailPending=true;
    try {
      const result=await bridge.apiGet("memory/maintenance/detail",expected);
      if(!detailAlive(token))return;
      if(result.scope!==frozen.scope||result.cid!==frozen.cid||result.epoch!==expected.epoch||result.collection!==expected.collection||result.id!==expected.id||result.version!==expected.version||result.hash!==expected.hash){detailBody.textContent=reason("STALE");return;}
      detailDialog.querySelector("#mm-detail-title").textContent=`${labels[item.collection]} · ${["overview","keywords"].includes(item.collection)?"只报告疑点":item.label||item.id}`;
      renderDetail(result,item);
    } catch(error) {if(detailAlive(token))detailBody.textContent=reason(error?.code||error?.message);}
    finally {detailPending=false;const queued=detailQueued;detailQueued=null;if(queued&&detailAlive(queued.token))readDetail(queued.item,queued.expected,queued.token);}
  };
  const openDetail=(item,button)=>{
    if(!valid())return;
    ensureDetail();const previousScroll=detailScroll;releaseDetail(false);
    const ancestors=[];for(let node=root;node;node=node.parentElement)ancestors.push(node);
    detailScroll=previousScroll||{nodes:[...new Set([...ancestors,q("items"),dialog])].map(node=>[node,node.scrollTop,node.scrollLeft]),x:window.scrollX||0,y:window.scrollY||0};
    detailItem={...item,epoch:current.epoch};detailButton=button;
    const token=detailSerial,expected={...frozen,collection:item.collection,id:item.id,epoch:current.epoch,version:item.version,hash:item.hash};
    detailBody.textContent="正在读取记忆…";detailDialog.showModal();
    if(detailPending){detailQueued={item,expected,token};return;}
    return readDetail(item,expected,token);
  };
  const renderItems=(items,batches)=>{
    const signature=JSON.stringify([items,batches]);if(signature===itemSignature)return;itemSignature=signature;
    const ordered=Object.keys(labels).flatMap(collection=>items.filter(item=>item.collection===collection));
    const previousIds=[...root.querySelectorAll("[data-mm-item]")].map(el=>el.dataset.mmItem);
    if(JSON.stringify(previousIds)!==JSON.stringify(ordered.map(identity))){
      const focus=document.activeElement,focused=["mmItem","mmDetail","mmProtect"].map(key=>({key,value:focus?.dataset?.[key]})).find(row=>row.value);
      const groups=new Map([...root.querySelectorAll("[data-mm-group]")].map(el=>[el.dataset.mmGroup,el.open])),scroll=q("items").scrollTop;
      q("items").innerHTML=Object.entries(labels).map(([collection,label])=>{
        const rows=ordered.filter(item=>item.collection===collection),report=["overview","keywords"].includes(collection);
        return `<details class="lj-group mm-group" data-mm-group="${collection}" ${groups.get(collection)===false?"":"open"}><summary>${label} <span>${rows.length}</span>${report?" · 只报告疑点":""}</summary>${rows.map(item=>`<div class="mm-item" data-mm-row data-mm-search="${esc(`${labels[item.collection]} ${item.label||item.id}`.toLowerCase())}"><div class="mm-item-head"><label class="check"><input type="checkbox" data-mm-item="${esc(identity(item))}"><span data-mm-label="${esc(identity(item))}"></span></label><button type="button" class="lj-text" data-mm-detail="${esc(identity(item))}" aria-haspopup="dialog">查看</button></div><span class="lj-muted" data-mm-source="${esc(identity(item))}"></span><span class="lj-muted mm-included" data-mm-included="${esc(identity(item))}"></span>${report?"":`<label class="check mm-protection"><input type="checkbox" data-mm-protect="${esc(identity(item))}">助手不自动修改</label>`}</div>`).join("")}</details>`;
      }).join("");
      for(const input of root.querySelectorAll("[data-mm-item]")){
        input.checked=selected.has(input.dataset.mmItem);
        input.onchange=()=>{if(writing||pendingStart)return;if(input.checked)selected.add(input.dataset.mmItem);else selected.delete(input.dataset.mmItem);invalidate();count();updateControls();};
      }
      q("items").scrollTop=scroll;
      if(focused)[...root.querySelectorAll("[data-mm-item], [data-mm-detail], [data-mm-protect]")].find(el=>el.dataset[focused.key]===focused.value)?.focus({preventScroll:true});
    }
    const known=new Set(items.map(identity));for(const id of selected)if(!known.has(id)){selected.delete(id);invalidate();}
    for(const [id,keys]of batchSelections){const remaining=keys.filter(key=>known.has(key));if(remaining.length!==keys.length){batchSelections.set(id,remaining);invalidate();}}
    for(const item of items){
      const id=identity(item),input=[...root.querySelectorAll("[data-mm-protect]")].find(el=>el.dataset.mmProtect===id);
      if(input){input.checked=item.protected;input.onchange=()=>{if(writing)return;const value=input.checked;input.checked=item.protected;invalidate();write({op:"protect",collection:item.collection,id:item.id,version:item.version,epoch:current?.epoch,protected:value});};}
      const label=[...root.querySelectorAll("[data-mm-label]")].find(el=>el.dataset.mmLabel===id);
      label.textContent=["overview","keywords"].includes(item.collection)?labels[item.collection]:item.label||item.id;
      label.closest("[data-mm-row]").dataset.mmSearch=`${labels[item.collection]} ${item.label||item.id}`.toLowerCase();
      const button=[...root.querySelectorAll("[data-mm-detail]")].find(el=>el.dataset.mmDetail===id);
      button.onclick=()=>openDetail(item,button);
      const source=[...root.querySelectorAll("[data-mm-source]")].find(el=>el.dataset.mmSource===id);
      source.textContent=[item.legacy_unverified?"旧来源未核实":"",!item.source_complete?"缺少完整原文":""].filter(Boolean).join(" · ");
    }
    const oldBatches=[...root.querySelectorAll("[data-mm-batch]")].map(el=>Number(el.dataset.mmBatch));
    if(JSON.stringify(oldBatches)!==JSON.stringify(batches.map(b=>b.id))){
      q("batch-items").innerHTML=batches.map(b=>`<label class="check"><input type="checkbox" data-mm-batch="${number(b.id)}"><span data-mm-batch-label="${number(b.id)}"></span></label>`).join("")||'<p class="lj-muted">没有总结批次</p>';
    }
    for(const input of root.querySelectorAll("[data-mm-batch]")){
      const batch=batches.find(row=>row.id===Number(input.dataset.mmBatch));
      input.onchange=()=>chooseBatch(batch,input.checked);
      const label=[...root.querySelectorAll("[data-mm-batch-label]")].find(row=>Number(row.dataset.mmBatchLabel)===batch.id);
      label.textContent=`第${number(batch.first)}—${number(batch.last)}轮 · 批次${number(batch.id)} · ${batchKeys(batch).length}项`;
    }
    refreshIncluded();count();filter();
  };
  const pretty=value=>typeof value==="string"?value:JSON.stringify(value,null,2);
  const renderResults=runs=>{
    const signature=JSON.stringify(runs);if(signature===resultSignature)return;resultSignature=signature;
    const privateText=[],slot=value=>{privateText.push(value);return `<span data-mm-text="${privateText.length-1}"></span>`;};
    const active=document.activeElement, focusKey=active?.dataset?.mmUndo?{attr:"data-mm-undo",value:active.dataset.mmUndo}:active?.dataset?.mmResolve?{attr:"data-mm-resolve",value:active.dataset.mmResolve}:null;
    const opened=new Set([...root.querySelectorAll("[data-mm-run]")].filter(el=>el.open).map(el=>el.dataset.mmRun));
    q("results").innerHTML=runs.map(run=>`<details class="lj-group" data-mm-run="${esc(run.id)}" ${opened.has(run.id)?"open":""}><summary>${run.id===run.task_id?"任务合计":"检查单元"} · ${esc(states[run.state]||"待核对")} · ${esc(dateText(run.created))} · 已修正 ${number(run.counts?.modified)} 项</summary>${run.code?`<p class="memory-error">${esc(reason(run.code))}</p>`:""}<p class="lj-muted">${run.trigger==="auto"?"总结后自动检查":"手动检查"} · 已检查 ${number(run.counts?.checked)} 项 · 疑点 ${number(run.counts?.findings)} 项 · 未检查 ${number(run.counts?.not_checked)} 项 · 调用 ${number(run.counts?.calls)} 次${run.expired?" · 私有详情已清理":""}${run.expires?` · 私有结果保留至 ${esc(dateText(run.expires))}`:""}</p>${(run.changes||[]).map(change=>`<article class="mm-change"><h4>${esc(labels[change.collection]||"记忆")} · ${esc(change.id)}</h4><div class="mm-comparison"><div><strong>修正前</strong><pre>${slot(pretty(change.before))}</pre></div><div><strong>修正后</strong><pre>${slot(pretty(change.after))}</pre></div></div>${change.reason?`<p>${slot(change.reason)}</p>`:""}<p class="lj-muted">${esc(sourceText(change.sources))}</p></article>`).join("")}${(run.findings||[]).map(finding=>`<article class="mm-finding"><strong>${esc(labels[finding.collection]||"记忆")} · ${esc(finding.id)} · ${esc(reason(finding.category))}${run.resolved||finding.resolved?" · 已处理":""}</strong>${finding.reason?`<p>${slot(finding.reason)}</p>`:""}${finding.suggestion?`<details><summary>仅建议，未应用</summary><pre>${slot(pretty(finding.suggestion))}</pre></details>`:""}<p class="lj-muted">${esc(sourceText(finding.sources))}</p></article>`).join("")}<p class="lj-muted">${esc(sourceText(run.sources))}</p><div class="lj-actions">${run.can_undo?`<button type="button" class="lj-btn" data-mm-undo="${esc(run.id)}">撤销本单元修正</button>`:""}${!run.resolved&&(run.findings||[]).some(f=>!f.resolved)?`<button type="button" class="lj-btn" data-mm-resolve="${esc(run.id)}">疑点标记已处理</button>`:""}</div></details>`).join("")||'<p class="lj-muted">暂无检查结果</p>';
    for(const node of root.querySelectorAll("[data-mm-text]"))node.textContent=privateText[Number(node.dataset.mmText)];
    for(const button of root.querySelectorAll("[data-mm-undo]"))button.onclick=()=>{if(!writing&&window.confirm("只撤销本单元修正；后续修改有冲突时将整单元拒绝。继续？")){invalidate();write({op:"undo",id:button.dataset.mmUndo});}};
    for(const button of root.querySelectorAll("[data-mm-resolve]"))button.onclick=()=>write({op:"resolve",id:button.dataset.mmResolve});
    if(focusKey)[...root.querySelectorAll(`[${focusKey.attr}]`)].find(el=>el.getAttribute(focusKey.attr)===focusKey.value)?.focus();
  };
  const accept=result=>{
    for(const row of result.unavailable_batches||[])if(selectedBatches.has(row.id)){selectedBatches.delete(row.id);batchSelections.delete(row.id);invalidate();}
    if(current&&(preview||previewing)&&(current.revision!==result.revision||current.policy_signature!==result.policy_signature||JSON.stringify(current.items)!==JSON.stringify(result.items)))invalidate();
    if(detailItem){const item=(result.items||[]).find(row=>identity(row)===identity(detailItem));if(!item||result.epoch!==detailItem.epoch||item.version!==detailItem.version||item.hash!==detailItem.hash){detailSerial++;detailBody.textContent=reason(item?"STALE":"ITEM_MISSING");}}
    if(current&&current.epoch!==result.epoch){selected.clear();selectedBatches.clear();batchSelections.clear();itemSignature="";invalidate();root.querySelectorAll("[data-mm-item]").forEach(el=>el.checked=false);root.querySelectorAll("[data-mm-batch]").forEach(el=>el.checked=false);}
    current=result;q("pause").checked=!!result.paused;
    const owner=result.task||{}, activeRun=(result.runs||[]).find(run=>run.task_id===owner.id&&["reading","requesting","validating","committing"].includes(run.state));
    const task=activeRun?{state:activeRun.state,calls:activeRun.counts?.calls}:owner;
    q("status").textContent=result.running?`${states[task.state]||"正在检查"}${Number.isSafeInteger(task.calls)?` · 已调用 ${task.calls} 次`:""}`:!result.enabled?"助手未启用；可打开助手设置，也可查看和撤销已有结果。":!result.configured?reason(result.error_code):"";
    q("status").hidden=!q("status").textContent;
    if(result.items){const batches=[...(result.batches||[])];if(result.latest_batch&&!batches.some(row=>row.id===result.latest_batch.id))batches.unshift(result.latest_batch);renderItems(result.items,batches);}
    const latest=result.latest_batch;q("latest-info").textContent=latest?`批次${number(latest.id)} · 第${number(latest.first)}—${number(latest.last)}轮${latest.keys?.length?"":" · 无可选变化"}`:"没有有效总结批次";
    if(result.runs)renderResults(result.runs);
    updateControls();
  };
  const schedule=()=>{clearTimeout(timer);if(valid())timer=setTimeout(()=>refresh(),3000);};
  const refresh=async()=>{
    if(!valid()){dispose();return;}if(reading||writing)return;reading=true;const version=readVersion,batchVersion=JSON.stringify([...selectedBatches]);updateControls();
    try{const result=await bridge.apiGet("memory/maintenance",{...frozen,...(selectedBatches.size?{selected_batches:batchVersion}:{})});if(valid()&&!writing&&version===readVersion&&batchVersion===JSON.stringify([...selectedBatches]))accept(result);}catch{showError("REQUEST_FAILED");}
    finally{reading=false;if(valid()){updateControls();schedule();}}
  };
  const write=async(payload)=>{
    if(!valid()||writing)return;writing=true;readVersion++;
    const assistantControls=new Set(q("settings-content").querySelectorAll("input,button"));
    const controls=[...root.querySelectorAll("input,button")].filter(el=>!assistantControls.has(el)),disabled=controls.map(el=>[el,el.disabled]);controls.forEach(el=>el.disabled=true);
    q("error").hidden=true;clearTimeout(timer);
    try{const result=await bridge.apiPost("memory/maintenance",{...frozen,...payload,...(selectedBatches.size?{selected_batches:[...selectedBatches]}:{})});if(!valid())return;if(payload.op==="start"){pendingStart=null;q("start").hidden=true;q("start").textContent="确认开始检查";}accept(result);onChange();}
    catch(error){if(!valid())return;const code=error?.code||error?.message;if(payload.op==="start"&&reasons[code]){pendingStart=null;q("start").hidden=true;showError(code);}else if(payload.op==="start"&&valid()){q("error").textContent="启动回执未收到，请刷新核对；继续本次启动会复用同一请求编号。";q("error").hidden=false;q("start").hidden=false;q("start").textContent="继续核对本次启动";}else showError(error?.code||error?.message);}
    finally{writing=false;if(valid()){for(const [el,value]of disabled)if(el.isConnected)el.disabled=value;updateControls();schedule();}}
  };
  q("search").oninput=filter;
  q("select").onclick=()=>{if(writing||pendingStart)return;for(const row of root.querySelectorAll("[data-mm-row]")){if(row.hidden||!row.closest("[data-mm-group]")?.open)continue;const input=row.querySelector("[data-mm-item]");input.checked=true;selected.add(input.dataset.mmItem);}invalidate();count();updateControls();};
  q("clear").onclick=()=>{if(writing)return;selected.clear();selectedBatches.clear();batchSelections.clear();refreshIncluded();root.querySelectorAll("[data-mm-item]").forEach(el=>el.checked=false);root.querySelectorAll("[data-mm-batch]").forEach(el=>el.checked=false);invalidate();count();updateControls();};
  q("apply").onchange=()=>{if(!q("apply").checked)q("protected").checked=false;q("protected").disabled=!q("apply").checked;invalidate();updateControls();};
  q("protected").onchange=()=>{invalidate();updateControls();};
  q("pause").onchange=()=>{const paused=q("pause").checked;q("pause").checked=!!current?.paused;invalidate();write({op:"pause",paused,revision:current?.revision});};
  q("latest").onclick=()=>{const batch=current?.latest_batch;if(batch&&!selectedBatches.has(batch.id))chooseBatch(batch,true);};
  q("settings").onclick=async event=>{
    const section=q("settings-section");if(section.open)return;
    event?.preventDefault?.();section.open=true;
    if(assistantOpening||assistantPanel)return;
    assistantOpening=true;
    try{const session=await openAssistantSettings?.({container:q("settings-content"),isCurrent:valid,onChange:()=>{if(valid()){invalidate();refresh();}}});if(valid())assistantPanel=session;else session?.dispose();}
    finally{assistantOpening=false;}
  };
  q("refresh").onclick=refresh;
  q("stop").onclick=()=>write({op:"stop",task_id:current?.task?.id||current?.task?.task_id});
  q("preview").onclick=async()=>{
    if(writing||q("preview").disabled)return;invalidate();const token=serial;previewing=true;q("error").hidden=true;updateControls();
    try{const result=await bridge.apiPost("memory/maintenance",{...frozen,op:"preview",keys:selectedKeys(),batches:[...selectedBatches],apply:q("apply").checked,allow_protected:q("protected").checked});
      if(!valid()||token!==serial)return;preview=result;requestId=crypto.randomUUID();q("preview-result").innerHTML=`<p>检查 ${number(result.selected)} 项 · ${number(result.units)} 个单元 · 最多 ${number(result.max_calls)} 次模型调用</p><p>总时限 ${number(result.total_timeout)} 秒 · 受保护 ${number(result.protected)} 项 · 缺少完整原文 ${number(result.source_missing)} 项</p><p class="lj-muted">将产生模型费用${result.vector_enabled?"；有修正时还会更新相关向量":""}。${q("apply").checked?"仅在有效依据支持时应用白名单修正。":"本次只报告，不自动改写。"}</p>`;q("preview-result").innerHTML+='<p class="lj-muted">概况和关键词仅报告疑点。</p>';q("preview-result").hidden=false;q("start").hidden=false;
    }catch(error){if(token===serial)showError(error?.code||error?.message);}finally{previewing=false;if(valid())updateControls();}
  };
  q("start").onclick=async()=>{if(writing||(!preview&&!pendingStart)||q("start").disabled)return;pendingStart ||= {token:preview.token,request_id:requestId};const payload={...pendingStart};invalidate();await write({op:"start",...payload});};
  const dispose=()=>{if(disposed)return;disposed=true;assistantPanel?.dispose();serial++;clearTimeout(timer);dialog.removeEventListener("close",dispose);releaseDetail(false);if(detailDialog){detailDialog.removeEventListener("cancel",detailCancel);detailDialog.removeEventListener("close",detailClosed);detailDialog.removeEventListener("keydown",detailKeyboard);detailDialog.remove();detailDialog=null;detailBody=null;}preview=null;pendingStart=null;current=null;selected.clear();selectedBatches.clear();batchSelections.clear();root.innerHTML="";if(closeMaintenance===dispose)closeMaintenance=null;};
  closeMaintenance=dispose;
  dialog.addEventListener("close",dispose,{once:true});
  updateControls();refresh();
  return {dispose,refresh};
}

export async function mountMemory(env) {
  closeMaintenance?.();
  const own = ++mountNumber;
  const {main, bridge, showDialog, footer, busy, toast, escape: esc} = env;
  const alive = () => own === mountNumber && env.isCurrent();
  const dialog = document.getElementById("dialog");
  const content = document.getElementById("dialog-content");
  let data = null;
  let following = true;
  let settingsSession = 0;
  clearTimeout(pollTimer);
  clearTimeout(followTimer);

  const params = (target = view.target) => ({scope: target.scope, cid: target.cid});
  const post = (route, body) => bridge.apiPost("memory/" + route, {...params(), ...body});
  const act = (name, extra = {}) => post("action", {action: name, revision: data?.state.revision, ...extra});

  function open(title, body, buttons = "", kind = "") {
    showDialog(title, `<div class="memory-dialog lj-dialog-body">${body}<p id="memory-dialog-error" class="memory-error" role="alert" hidden></p></div>`, buttons ? footer(buttons) : "");
    dialog.setAttribute("data-memory-dialog", kind);
    dialog.addEventListener("close", () => dialog.removeAttribute("data-memory-dialog"), {once: true});
    return {
      run: (button, fn) => busy(button, async () => {
        const box = content.querySelector("#memory-dialog-error");
        if (box) box.hidden = true;
        try { await fn(); } catch (error) {
          if (box) { box.textContent = ({MAINTENANCE_DEPENDENCY:"请先在记忆维护中逆序撤销依赖本次总结的修正",MAINTENANCE_EXPIRED:"依赖的助手修正详情已清理，无法安全撤销本次总结"})[error.message] || error.message || "操作失败"; box.hidden = false; }
        }
      }),
    };
  }

  // ----------------------------------------------------------- target ----
  async function resolveTarget() {
    if (view.target && !following) return;
    const bound = await bridge.apiGet("memory/follow");
    if (bound.scope && bound.cid && (!view.target || view.target.cid !== bound.cid)) view.target = {scope: bound.scope, cid: bound.cid, title: ""};
  }

  async function load() { data = view.target ? await bridge.apiGet("memory/state", params()) : null; }

  async function chooseConversation() {
    const editor = open("选择聊天", '<form id="lj-chat-search" class="lj-search lj-search-box"><input id="lj-chat-query" type="search" aria-label="搜索聊天" placeholder="搜索聊天标题"><button type="submit">搜索</button></form><div id="lj-chat-list"></div>', '<button data-close>关闭</button>');
    const fill = async (query = "") => {
      const result = await bridge.apiGet("memory/conversations", {page: 1, query});
      const list = content.querySelector("#lj-chat-list");
      list.innerHTML = result.items.map((item, i) => `<button class="lj-chat-row" data-chat="${i}" aria-pressed="${view.target?.cid === item.cid}"><strong>${esc(item.title)}</strong><small>${item.current ? "当前聊天" : "历史聊天"}${item.enabled ? ` · 记忆已启用 · ${item.events}段剧情` : ""}</small></button>`).join("") || '<p class="lj-empty">无匹配聊天</p>';
      list.querySelectorAll("[data-chat]").forEach(button => button.onclick = () => busy(button, async () => {
        const item = result.items[Number(button.dataset.chat)];
        view.target = {scope: item.scope, cid: item.cid, title: item.title};
        following = item.current;
        dialog.close();
        await refresh();
      }));
    };
    content.querySelector("#lj-chat-search").onsubmit = e => { e.preventDefault(); editor.run(e.submitter, () => fill(content.querySelector("#lj-chat-query").value.trim())); };
    await editor.run(content.querySelector("[data-close]"), () => fill());
  }

  // ------------------------------------------------------------ helpers ----
  const memory = () => data.memory;
  const eventsById = () => Object.fromEntries(memory().events.map(e => [e.id, e]));
  const matches = (...values) => !view.query || values.join(" ").toLowerCase().includes(view.query.toLowerCase());
  const openPlans = () => memory().plans.filter(p => ["proposed", "agreed", "ongoing", "paused"].includes(p.status));

  function heroMarkup() {
    const c = data.state.counts, p = data.progress;
    const meta = [`<span>已整理 <b>${c.through || 0}</b> 轮</span>`, `<span>待整理 <b>${p.pending}</b> 轮</span>`];
    if (p.running) {
      const s = p.stage || {};
      meta.push(`<span class="lj-accent">整理中${s.first ? ` · 第${s.first}—${s.last}轮` : ""}${s.attempt > 1 ? ` · 第${s.attempt}/${s.attempts}次` : ""}${s.stage === "saving" ? " · 保存中" : ""}</span>`);
    } else if (p.blocked) meta.push(`<span class="memory-error">暂无法整理：${esc(p.blocked)}</span>`);
    else if (p.failed) meta.push(`<span class="memory-error">上次整理未完成：${esc(p.failed)}</span>`);
    if (!p.window_configured) meta.push('<span class="memory-error">连接未设置模型窗口，按32000计算</span>');
    const action = p.running
      ? '<button class="lj-btn" id="lj-stop">停止整理</button>'
      : `<button class="lj-btn" id="lj-resum-hero" ${c.turns - c.pruned ? "" : "disabled"} title="清空当前记忆，用备份的原文从头整理">重新总结</button><button class="lj-btn lj-btn-primary" id="lj-run" ${p.pending ? "" : "disabled"} title="${p.pending ? `整理待整理的${p.pending}轮` : "没有待整理的对话"}">总结${p.pending ? ` ${p.pending} 轮` : ""}</button>`;
    return `<div class="lj-meta">${meta.join("")}</div><div class="lj-generate">${action}</div>`;
  }

  // ------------------------------------------------------------- views ----
  function storyMarkup() {
    const m = memory();
    const roles = [...data.roles, ROLE_NONE].filter(r => m.events.some(e => (e.role || ROLE_NONE) === r));
    if (view.role && !roles.includes(view.role)) view.role = ROLE_ALL;
    const index = [ROLE_ALL, ...roles].map(r => `<button class="lj-index-button" data-role="${esc(r)}" aria-pressed="${r === view.role}">${esc(r || "全部剧情")}<span>${r ? m.events.filter(e => (e.role || ROLE_NONE) === r).length : m.events.length}</span></button>`).join("");
    const words = m.keywords.length ? `<details class="lj-keywords" open><summary>核心关键词</summary><div class="lj-keyword-list">${m.keywords.map(k => `<button class="lj-tag ${k.weight === "核心" ? "lj-tag-core" : ""}" data-keyword="${esc(k.text)}">${esc(k.text)}</button>`).join("")}</div><button class="lj-text" id="lj-edit-keywords">编辑关键词</button></details>` : "";
    let events = m.events.filter(e => (!view.role || (e.role || ROLE_NONE) === view.role) && matches(e.title, e.summary, e.time, ...(e.participants || [])));
    if (view.descending) events = [...events].reverse();
    const overview = m.overview ? `<section class="lj-overview"><div class="lj-section-head"><h3>当前概况</h3><button class="lj-text" id="lj-edit-overview">编辑</button></div><p>${esc(m.overview)}</p></section>` : "";
    const backup = data.state.counts.turns - data.state.counts.pruned;
    const legacy = m.events.length && m.events.every(e => !e.role) ? `<div class="lj-notice"><span>${backup ? "这些剧情是旧版写作要求生成的，没有分类和前因。点“重新总结”会按新要求整理（原结果存为恢复点）。" : "这些剧情是旧版写作要求生成的，没有分类和前因；这份聊天没有原文备份，可以在“编辑剧情”里手动补充。"}</span>${backup ? '<button class="lj-btn" id="lj-resum-notice">重新总结</button>' : ""}</div>` : "";
    const body = legacy + (events.length ? events.map(e => eventMarkup(e, esc)).join("") : `<div class="lj-empty"><h2>${m.events.length ? "没有找到匹配的内容" : "还没有剧情记录"}</h2>${m.events.length ? "" : "<p>整理当前聊天后，经历过的事会在这里按时间展开。</p>"}</div>`);
    return `<div class="lj-story"><aside class="lj-index"><div class="lj-label">剧情索引</div><nav>${index}</nav>${words}</aside><section class="lj-reading">${overview}<div class="lj-section-head"><h2>${esc(view.role || "全部剧情")}</h2><div class="lj-actions"><button class="lj-text" id="lj-order">${view.descending ? "最新在前" : "最早在前"}</button></div></div>${body}</section></div>`;
  }

  function peopleMarkup() {
    const m = memory(), names = m.names;
    const people = m.people.filter(p => matches(p.name, ...p.aliases, p.description));
    if (!people.length) return `<div class="lj-empty"><h2>${m.people.length ? "没有找到匹配的内容" : "暂无人物记录"}</h2></div>`;
    return `<div class="lj-people">${people.map(p => {
      const related = m.events.filter(e => (e.participants || []).some(n => n === p.name || p.aliases.includes(n)));
      return `<article class="lj-person-card"><div class="lj-person-head"><span class="lj-avatar" aria-hidden="true">${esc(Array.from(p.name)[0] || "?")}</span><div><h3>${esc(p.name)}</h3>${p.aliases.length ? `<small>也叫：${esc(p.aliases.join("、"))}</small>` : ""}</div><button class="lj-text" data-edit-person="${esc(p.id)}">编辑</button></div>${p.description ? `<p>${esc(p.description)}</p>` : ""}${p.relationships.length ? `<dl class="lj-relations">${p.relationships.map(r => `<dt>${esc(names[r.to] || r.to)}</dt><dd>${esc(r.text)}${r.history?.length ? `<small class="lj-rel-history">之前：${esc(r.history.map(h => h.text).join(" → "))}</small>` : ""}</dd>`).join("")}</dl>` : ""}${profileCount(p.id) ? `<button class="lj-text" data-profiles-of="${esc(p.id)}">生活小档案 ${profileCount(p.id)}</button>` : ""}${related.length ? `<div class="lj-person-events"><span class="lj-label">相关剧情 ${related.length}</span>${related.slice(-4).reverse().map(e => `<button class="lj-cause" data-event="${esc(e.id)}">${esc(e.title)}</button>`).join("")}</div>` : ""}</article>`;
    }).join("")}</div>`;
  }

  const profiles = () => memory().profiles || [];
  const profileCount = id => profiles().filter(p => p.person === id).length;

  function profilesMarkup() {
    const m = memory(), names = m.names, kinds = data.profile_kinds;
    const order = Object.keys(kinds);
    const rows = profiles().filter(p => matches(names[p.person] || "", p.text, kinds[p.kind] || ""));
    const head = `<div class="lj-section-head"><h2>生活小档案</h2><div class="lj-actions">${m.people.length ? '<button class="lj-btn" id="lj-profile-add">添加</button>' : ""}</div></div>`;
    if (!rows.length) return `<section class="lj-reading lj-wide">${head}<div class="lj-empty"><h2>${profiles().length ? "没有找到匹配的内容" : "暂无生活小档案"}</h2></div></section>`;
    // One section per kind (偏好/习惯/近况/背景); inside it, entries grouped by owner.
    const sections = order.filter(kind => rows.some(p => p.kind === kind)).map(kind => {
      const items = rows.filter(p => p.kind === kind);
      const owners = [...new Set(items.map(p => p.person))];
      const groups = owners.map(owner => `<div class="lj-profile-owner"><span class="lj-profile-name">${esc(names[owner] || owner)}</span><ul class="lj-profile-list">${items.filter(p => p.person === owner).map(p => `<li class="lj-profile"><span class="lj-profile-text">${esc(p.text)}${p.until ? `<small>到${esc(p.until)}</small>` : ""}</span><button class="lj-text" data-edit-profile="${esc(p.id)}">编辑</button></li>`).join("")}</ul></div>`).join("");
      return `<section class="lj-profile-kind" data-kind="${esc(kind)}"><h3><span class="lj-pill" data-kind="${esc(kind)}">${esc(kinds[kind])}</span><small>${items.length}条</small></h3>${groups}</section>`;
    }).join("");
    return `<section class="lj-reading lj-wide">${head}<div class="lj-profile-kinds">${sections}</div></section>`;
  }

  function plansMarkup() {
    const m = memory();
    const plans = (view.planFilter === "all" ? m.plans : openPlans()).filter(p => matches(p.summary, p.time, p.reason));
    const filter = `<div class="lj-section-head"><h2>${view.planFilter === "all" ? "全部约定" : "未结束的约定"}</h2><div class="lj-actions"><button class="lj-text" data-plan-filter="open" aria-pressed="${view.planFilter === "open"}">未结束</button><button class="lj-text" data-plan-filter="all" aria-pressed="${view.planFilter === "all"}" title="已完成、取消的约定只留在这里，聊天时不会加入">全部</button></div></div>`;
    const events = eventsById();
    const sources = p => (p.events || []).filter(x => events[x]).map(x => `<button class="lj-text" data-event="${esc(x)}">${esc(events[x].date ? events[x].date + "｜" : "")}${esc(events[x].title)}</button>`).join("");
    const rows = plans.map(p => `<article class="lj-plan" data-status="${esc(p.status)}"><div class="lj-plan-status">${esc(data.status_names[p.status] || p.status)}</div><div><h3>${esc(p.summary)}</h3><div class="lj-event-meta">${p.time ? `<span>${esc(p.time)}</span>` : ""}${p.reason ? `<span>原因：${esc(p.reason)}</span>` : ""}<button class="lj-text" data-edit-plan="${esc(p.id)}">编辑</button></div>${sources(p) ? `<div class="lj-plan-events"><span class="lj-muted">相关剧情</span>${sources(p)}</div>` : ""}</div></article>`).join("");
    return `<section class="lj-reading lj-wide">${filter}${rows || '<div class="lj-empty"><h2>没有约定</h2></div>'}</section>`;
  }

  async function recordsMarkup() {
    const [batches, snaps] = await Promise.all([bridge.apiGet("memory/batches", params()), bridge.apiGet("memory/snapshots", params())]);
    const latest = batches.items.find(b => b.state === "applied");
    const rows = batches.items.map(b => `<article class="lj-record"><time>${dateText(b.created)}</time><div><strong>第${b.first}—${b.last}轮</strong><span>新增${b.stats.events || 0}段剧情${b.stats.plans ? ` · 约定${b.stats.plans}项` : ""}${b.stats.relations ? ` · 关系变化${b.stats.relations}处` : ""}${b.stats.profiles ? ` · 生活小档案${b.stats.profiles}条` : ""}${b.stats.skipped ? ` · 跳过${b.stats.skipped}项` : ""}</span>${b.state === "undone" ? '<em>已撤销</em>' : ""}${b.pruned ? '<em>原文备份已清理</em>' : ""}</div></article>`).join("");
    const snapRows = snaps.items.map(s => `<article class="lj-record"><time>${dateText(s.created)}</time><div><strong>${esc(s.reason)}</strong><span>${s.events}段剧情 · ${s.people}位人物${s.profiles ? ` · ${s.profiles}条档案` : ""} · ${s.plans}项约定 · 保留至${dateText(s.expires).slice(0, 10)}</span></div><button class="lj-btn" data-restore="${esc(s.id)}">恢复到此</button></article>`).join("");
    return `<section class="lj-reading lj-wide"><div class="lj-section-head"><h2>整理记录</h2><div class="lj-actions">${latest ? `<button class="lj-btn" id="lj-undo">撤销最近一次（第${latest.first}—${latest.last}轮）</button>` : ""}<button class="lj-btn" id="lj-resummarize">用备份重新整理</button></div></div>${rows || '<div class="lj-empty"><h2>还没有整理记录</h2></div>'}<h2 class="lj-subhead">恢复点</h2>${snapRows || '<p class="lj-muted">没有恢复点。清空、重新整理、导入、被覆盖前会自动保存一份，保留30天。</p>'}</section>`;
  }

  // ------------------------------------------------------------ render ----
  async function render() {
    if (!alive()) return;
    main.dataset.page = "memory";
    if (!view.target) {
      main.innerHTML = `<div class="lj"><div class="lj-topbar"><span class="lj-brand">拾忆回响</span><button class="lj-btn" id="lj-maintenance">记忆助手</button></div><div class="lj-empty"><h2>选择一份聊天</h2><p>也可以绑定QQ对象，自动跟随它当前使用的聊天。</p><div class="lj-actions lj-center"><button class="lj-btn lj-btn-primary" id="lj-choose">选择聊天</button><button class="lj-btn" id="lj-bind">会话绑定</button></div></div></div>`;
      main.querySelector("#lj-choose").onclick = chooseConversation;
      main.querySelector("#lj-bind").onclick = bindingDialog;
      main.querySelector("#lj-maintenance").onclick=()=>openMemoryMaintenance({bridge,showDialog,footer,escape:esc,target:null,isCurrent:alive,openAssistantSettings:env.openAssistantSettings});
      return;
    }
    const m = data.memory, enabled = data.state.enabled;
    const tabs = [["story", "剧情", m.events.length], ["people", "人物", m.people.length], ["profiles", "生活小档案", profiles().length], ["plans", "约定", openPlans().length], ["records", "整理记录", ""]];
    const topbar = `<div class="lj-topbar"><button class="lj-chat" id="lj-choose" title="切换聊天"><span class="lj-brand">${esc(view.target.title || "当前聊天")}</span><small>${following ? "跟随当前聊天" : "手动选择"} · 切换 ▾</small></button><div class="lj-actions"><button class="lj-btn" id="lj-maintenance">记忆助手</button><button class="lj-icon" id="lj-settings" title="总结设置" aria-label="总结设置"><svg viewBox="0 0 24 24"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg></button><details class="lj-more"><summary class="lj-icon" aria-label="更多">⋯</summary><div><button id="lj-logs">拾忆日志</button><button id="lj-bind">会话绑定</button><button id="lj-copy">复制记忆到另一份聊天</button><button id="lj-export">导出记忆</button><button id="lj-import">导入记忆</button><button id="lj-clear" class="lj-danger">清空记忆</button></div></details></div></div>`;
    if (!enabled) {
      main.innerHTML = `<div class="lj">${topbar}<div class="lj-empty"><h2>此聊天未启用记忆</h2><p>启用后从下一条消息开始备份对话，满设定轮数自动整理。</p><button class="lj-btn lj-btn-primary" id="lj-enable">启用记忆</button></div></div>`;
      bind();
      return;
    }
    const nav = `<div class="lj-nav"><div class="lj-tabs" role="tablist">${tabs.map(([k, n, c]) => `<button class="lj-tab" role="tab" data-tab="${k}" aria-selected="${view.tab === k}">${n}${c !== "" ? `<span>${c}</span>` : ""}</button>`).join("")}</div>${view.tab !== "records" ? `<label class="lj-search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/></svg><input id="lj-query" type="search" placeholder="${{story: "搜索剧情", people: "搜索人物", profiles: "搜索档案或人物", plans: "搜索约定"}[view.tab]}" value="${esc(view.query)}"></label>` : ""}</div>`;
    let body;
    if (view.tab === "story") body = storyMarkup();
    else if (view.tab === "people") body = peopleMarkup();
    else if (view.tab === "profiles") body = profilesMarkup();
    else if (view.tab === "plans") body = plansMarkup();
    else body = await recordsMarkup();
    if (!alive()) return;
    main.innerHTML = `<div class="lj">${topbar}<div class="lj-hero" id="lj-hero">${heroMarkup()}</div>${nav}<div class="lj-content">${body}</div></div>`;
    bind();
    schedulePoll();
  }

  // ------------------------------------------------------------ events ----
  function bind() {
    const $ = s => main.querySelector(s);
    $("#lj-choose").onclick = chooseConversation;
    const more = $("details.lj-more");
    more?.querySelectorAll("div button").forEach(b => b.addEventListener("click", () => { more.open = false; }));
    $("#lj-settings")?.addEventListener("click", settingsPage);
    $("#lj-maintenance")?.addEventListener("click", () => {const target={...view.target};openMemoryMaintenance({bridge,showDialog,footer,escape:esc,target,openAssistantSettings:env.openAssistantSettings,isCurrent:()=>alive()&&view.target.scope===target.scope&&view.target.cid===target.cid,onChange:()=>{load().catch(()=>{});}});});
    $("#lj-logs")?.addEventListener("click", () => env.openDiagnostics({...params()}));
    $("#lj-bind")?.addEventListener("click", bindingDialog);
    $("#lj-enable")?.addEventListener("click", e => busy(e.target, async () => { data = await act("enable", {enabled: true}); await render(); }));
    bindHero();
    main.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => { view.tab = b.dataset.tab; view.query = ""; render(); });
    const query = $("#lj-query");
    if (query) query.oninput = () => { view.query = query.value.trim(); const pos = query.selectionStart; render().then(() => { const q = main.querySelector("#lj-query"); q?.focus(); q?.setSelectionRange(pos, pos); }); };
    $("#lj-order")?.addEventListener("click", () => { view.descending = !view.descending; render(); });
    main.querySelectorAll("[data-role]").forEach(b => b.onclick = () => { view.role = b.dataset.role; render(); });
    main.querySelectorAll("[data-keyword]").forEach(b => b.onclick = () => { view.query = b.dataset.keyword; render(); });
    main.querySelectorAll("[data-event]").forEach(b => b.onclick = () => eventDetail(b.dataset.event));
    main.querySelectorAll("[data-person]").forEach(b => b.onclick = () => { view.tab = "people"; view.query = b.dataset.person; render(); });
    main.querySelectorAll("[data-edit-person]").forEach(b => b.onclick = () => personEditor(b.dataset.editPerson));
    main.querySelectorAll("[data-edit-plan]").forEach(b => b.onclick = () => planEditor(b.dataset.editPlan));
    main.querySelectorAll("[data-plan-filter]").forEach(b => b.onclick = () => { view.planFilter = b.dataset.planFilter; render(); });
    main.querySelectorAll("[data-edit-profile]").forEach(b => b.onclick = () => profileEditor(b.dataset.editProfile));
    main.querySelectorAll("[data-profiles-of]").forEach(b => b.onclick = () => { view.tab = "profiles"; view.query = memory().names[b.dataset.profilesOf] || ""; render(); });
    $("#lj-profile-add")?.addEventListener("click", () => profileEditor(null));
    $("#lj-edit-overview")?.addEventListener("click", overviewEditor);
    $("#lj-edit-keywords")?.addEventListener("click", keywordsEditor);
    $("#lj-undo")?.addEventListener("click", undoDialog);
    $("#lj-resum-notice")?.addEventListener("click", resummarizeDialog);
    $("#lj-resummarize")?.addEventListener("click", resummarizeDialog);
    main.querySelectorAll("[data-restore]").forEach(b => b.onclick = () => restoreDialog(b.dataset.restore));
    $("#lj-copy")?.addEventListener("click", copyDialog);
    $("#lj-export")?.addEventListener("click", () => bridge.download("memory/export", params(), "lumen-memory.json"));
    $("#lj-import")?.addEventListener("click", importDialog);
    $("#lj-clear")?.addEventListener("click", clearDialog);
  }

  function bindHero() {
    main.querySelector("#lj-resum-hero")?.addEventListener("click", resummarizeDialog);
    main.querySelector("#lj-run")?.addEventListener("click", e => busy(e.target, async () => { data = await act("summarize"); toast("已开始整理"); await render(); }));
    main.querySelector("#lj-stop")?.addEventListener("click", e => busy(e.target, async () => { data = await act("stop"); await render(); }));
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!data?.state.enabled) return;
    pollTimer = setTimeout(async () => {
      if (!alive() || dialog.open) return schedulePoll();
      try {
        const p = await bridge.apiGet("memory/progress", params());
        const changed = p.revision !== data.state.revision || p.progress.running !== data.progress.running;
        data.progress = p.progress;
        data.state.counts = p.counts;
        if (changed) { await load(); await render(); return; }
        const hero = main.querySelector("#lj-hero");
        if (hero) { hero.innerHTML = heroMarkup(); bindHero(); }
      } catch { /* next poll retries */ }
      schedulePoll();
    }, data.progress.running ? 3000 : 10000);
  }

  // ------------------------------------------------------------ drawer ----
  function planLinks(e) {
    const linked = memory().plans.filter(p => (e.plans || []).includes(p.id));
    if (!linked.length) return "";
    return `<h4>相关约定 · ${linked.length}</h4>${linked.map(p => `<button class="lj-cause" data-goto-plan="${esc(p.id)}"><span>${esc(data.status_names[p.status] || p.status)}${p.time ? " · " + esc(p.time) : ""}</span>${esc(p.summary)}</button>`).join("")}`;
  }

  function eventDetail(id, trail = []) {
    const events = eventsById(), e = events[id];
    if (!e) return;
    const link = x => events[x] ? `<button class="lj-cause" data-goto="${esc(x)}"><span>${esc(events[x].date || "")}</span>${esc(events[x].title)}</button>` : "";
    const body = `<div class="lj-detail-tags"><span>${esc(e.date || "未标注时间")}</span>${e.role ? `<span class="lj-pill">${esc(e.role)}</span>` : ""}</div><h3 class="lj-detail-heading">${esc(e.title)}</h3><p class="lj-detail-text">${esc(e.summary)} <span class="lj-turns">${turnLabel(e)}</span></p>${e.participants?.length ? `<h4>参与人物</h4><div class="lj-participants">${e.participants.map(p => `<button class="lj-chip" data-goto-person="${esc(p)}">${esc(p)}</button>`).join("")}</div>` : ""}${e.causes?.length ? `<h4>前因 · ${e.causes.length}</h4>${e.causes.map(link).join("")}` : ""}${e.followups?.length ? `<h4>由此引出 · ${e.followups.length}</h4>${e.followups.map(link).join("")}` : ""}${planLinks(e)}<div class="lj-detail-actions">${trail.length ? '<button class="lj-text" id="lj-back">← 返回刚才那段</button>' : ""}<button class="lj-text" id="lj-edit-event">编辑剧情</button></div>`;
    open("剧情详情", body, "", "drawer");
    content.querySelectorAll("[data-goto]").forEach(b => b.onclick = () => eventDetail(b.dataset.goto, [...trail, id]));
    content.querySelectorAll("[data-goto-person]").forEach(b => b.onclick = () => { dialog.close(); view.tab = "people"; view.query = b.dataset.gotoPerson; render(); });
    content.querySelectorAll("[data-goto-plan]").forEach(b => b.onclick = () => {
      const plan = memory().plans.find(p => p.id === b.dataset.gotoPlan);
      dialog.close(); view.tab = "plans"; view.query = "";
      if (plan && !openPlans().includes(plan)) view.planFilter = "all";
      render();
    });
    content.querySelector("#lj-back")?.addEventListener("click", () => eventDetail(trail[trail.length - 1], trail.slice(0, -1)));
    content.querySelector("#lj-edit-event").onclick = () => eventEditor(id);
  }

  // ----------------------------------------------------------- editors ----
  async function saveItem(kind, item, del = false) {
    data = await post("memory", {kind, item, delete: del, revision: data.state.revision});
    dialog.close();
    await render();
  }

  const listValue = value => value.split(/[、,，]/).map(x => x.trim()).filter(Boolean);

  function eventEditor(id) {
    const e = memory().events.find(x => x.id === id);
    const others = memory().events.filter(x => x.id !== id);
    const editor = open("编辑剧情", `<form id="lj-event-form" class="lj-form"><label>标题<input name="title" value="${esc(e.title)}" maxlength="160" required></label><div class="lj-form-row"><label>时间<input name="time" value="${esc(e.time || "")}" maxlength="80"></label><label>分类<select name="role"><option value="">未标注</option>${data.roles.map(r => `<option ${e.role === r ? "selected" : ""}>${esc(r)}</option>`).join("")}</select></label></div><label>正文<textarea name="summary" rows="7" required>${esc(e.summary)}</textarea></label><label>参与人物（用顿号分隔）<input name="participants" value="${esc((e.participants || []).join("、"))}"></label><fieldset class="lj-cause-picker"><legend>前因（最多3段）</legend>${others.map(o => `<label class="lj-check"><input type="checkbox" name="causes" value="${esc(o.id)}" ${e.causes.includes(o.id) ? "checked" : ""}><span>${esc(o.date ? o.date + "｜" : "")}${esc(o.title)}</span></label>`).join("") || '<p class="lj-muted">没有其他剧情</p>'}</fieldset></form>`, '<button id="lj-event-delete" class="lj-btn lj-danger">删除</button><button data-close class="lj-btn">取消</button><button id="lj-event-save" class="lj-btn lj-btn-primary">保存</button>', "form");
    const form = content.querySelector("#lj-event-form");
    content.querySelector("#lj-event-save").onclick = ev => editor.run(ev.target, () => saveItem("event", {id, title: form.title.value, time: form.time.value, role: form.role.value, summary: form.summary.value, participants: listValue(form.participants.value), causes: [...form.querySelectorAll('[name="causes"]:checked')].map(o => o.value).slice(0, 3)}));
    content.querySelector("#lj-event-delete").onclick = ev => editor.run(ev.target, () => saveItem("event", {id}, true));
  }

  function personEditor(id) {
    const p = memory().people.find(x => x.id === id);
    const others = memory().people.filter(x => x.id !== id);
    const relation = o => p.relationships.find(r => r.to === o.id)?.text || "";
    const relations = others.length ? `<fieldset class="lj-cause-picker"><legend>与其他人的关系</legend>${others.map(o => `<label class="lj-rel-row"><span>${esc(o.name)}</span><input data-rel="${esc(o.id)}" value="${esc(relation(o))}" maxlength="200" placeholder="没有就留空"></label>`).join("")}</fieldset>` : "";
    const owned = profiles().filter(x => x.person === id).length;
    const editor = open("编辑人物", `<form id="lj-person-form" class="lj-form"><label>名称<input name="name" value="${esc(p.name)}" required></label><label>其他称呼（用顿号分隔）<input name="aliases" value="${esc(p.aliases.join("、"))}"></label><label>介绍<textarea name="description" rows="4">${esc(p.description)}</textarea></label>${relations}</form>`, `<button id="lj-person-delete" class="lj-btn lj-danger" title="${owned ? `会同时删除属于此人的${owned}条生活小档案` : ""}">删除</button><button data-close class="lj-btn">取消</button><button id="lj-person-save" class="lj-btn lj-btn-primary">保存</button>`, "form");
    const form = content.querySelector("#lj-person-form");
    content.querySelector("#lj-person-save").onclick = ev => editor.run(ev.target, () => saveItem("person", {id, name: form.name.value, description: form.description.value, aliases: listValue(form.aliases.value), relationships: [...form.querySelectorAll("[data-rel]")].map(i => ({to: i.dataset.rel, text: i.value.trim()})).filter(r => r.text)}));
    content.querySelector("#lj-person-delete").onclick = ev => owned
      ? confirmDialog("删除人物", `<p>“${esc(p.name)}”和属于此人的 ${owned} 条生活小档案会一起删除。</p>`, "删除", () => saveItem("person", {id}, true), true)
      : editor.run(ev.target, () => saveItem("person", {id}, true));
  }

  function profileEditor(id) {
    const p = id ? profiles().find(x => x.id === id) : {person: memory().people[0]?.id, kind: "preference", text: "", until: ""};
    const kinds = data.profile_kinds;
    const body = `<form id="lj-profile-form" class="lj-form"><div class="lj-form-row"><label>属于<select name="person">${memory().people.map(o => `<option value="${esc(o.id)}" ${o.id === p.person ? "selected" : ""}>${esc(o.name)}</option>`).join("")}</select></label><label>类型<select name="kind">${Object.entries(kinds).map(([k, n]) => `<option value="${k}" ${p.kind === k ? "selected" : ""}>${n}</option>`).join("")}</select></label></div><label>内容<textarea name="text" rows="3" maxlength="600" required>${esc(p.text)}</textarea></label><div class="lj-form-row"><label>持续到<input name="until" value="${esc(p.until || "")}" maxlength="100" placeholder="长期有效就留空"></label></div></form>`;
    const editor = open(id ? "编辑生活小档案" : "添加生活小档案", body, `${id ? '<button id="lj-profile-delete" class="lj-btn lj-danger">删除</button>' : ""}<button data-close class="lj-btn">取消</button><button id="lj-profile-save" class="lj-btn lj-btn-primary">保存</button>`, "form");
    const form = content.querySelector("#lj-profile-form");
    content.querySelector("#lj-profile-save").onclick = ev => editor.run(ev.target, () => saveItem("profile", {...(id ? {id} : {}), person: form.person.value, kind: form.kind.value, text: form.text.value, until: form.until.value}));
    content.querySelector("#lj-profile-delete")?.addEventListener("click", ev => editor.run(ev.target, () => saveItem("profile", {id}, true)));
  }

  function planEditor(id) {
    const p = memory().plans.find(x => x.id === id);
    const linked = p.events || [];
    const picker = memory().events.length ? `<fieldset class="lj-cause-picker"><legend>相关剧情</legend>${memory().events.map(o => `<label class="lj-check"><input type="checkbox" name="events" value="${esc(o.id)}" ${linked.includes(o.id) ? "checked" : ""}><span>${esc(o.date ? o.date + "｜" : "")}${esc(o.title)}</span></label>`).join("")}</fieldset>` : "";
    const editor = open("编辑约定", `<form id="lj-plan-form" class="lj-form"><label>内容<textarea name="summary" rows="3" required>${esc(p.summary)}</textarea></label><div class="lj-form-row"><label>时间<input name="time" value="${esc(p.time || "")}"></label><label>状态<select name="status">${Object.entries(data.status_names).map(([k, n]) => `<option value="${k}" ${p.status === k ? "selected" : ""}>${n}</option>`).join("")}</select></label></div><label>原因<input name="reason" value="${esc(p.reason || "")}"></label>${picker}</form>`, '<button id="lj-plan-delete" class="lj-btn lj-danger">删除</button><button data-close class="lj-btn">取消</button><button id="lj-plan-save" class="lj-btn lj-btn-primary">保存</button>', "form");
    const form = content.querySelector("#lj-plan-form");
    content.querySelector("#lj-plan-save").onclick = ev => editor.run(ev.target, () => saveItem("plan", {id, summary: form.summary.value, time: form.time.value, status: form.status.value, reason: form.reason.value, events: [...form.querySelectorAll('[name="events"]:checked')].map(o => o.value)}));
    content.querySelector("#lj-plan-delete").onclick = ev => editor.run(ev.target, () => saveItem("plan", {id}, true));
  }

  function overviewEditor() {
    const editor = open("编辑当前概况", `<form class="lj-form"><textarea id="lj-overview-text" rows="8">${esc(memory().overview)}</textarea></form><p class="lj-muted">下次整理时，模型会在此基础上重写概况。</p>`, '<button data-close class="lj-btn">取消</button><button id="lj-overview-save" class="lj-btn lj-btn-primary">保存</button>', "form");
    content.querySelector("#lj-overview-save").onclick = ev => editor.run(ev.target, () => saveItem("overview", {text: content.querySelector("#lj-overview-text").value}));
  }

  function keywordsEditor() {
    const editor = open("编辑关键词", `<form class="lj-form"><label>关键词（用顿号分隔，最多12个）<input id="lj-keywords-text" value="${esc(memory().keywords.map(k => k.text).join("、"))}"></label></form>`, '<button data-close class="lj-btn">取消</button><button id="lj-keywords-save" class="lj-btn lj-btn-primary">保存</button>', "form");
    content.querySelector("#lj-keywords-save").onclick = ev => editor.run(ev.target, () => saveItem("keywords", {words: listValue(content.querySelector("#lj-keywords-text").value)}));
  }

  // ----------------------------------------------------------- actions ----
  // When opened from settings, 取消 goes back to the settings page instead of closing everything.
  let returnTo = null;
  function confirmDialog(title, text, label, fn, danger = false) {
    const back = returnTo;
    returnTo = null;
    const editor = open(title, text, `<button id="lj-cancel" class="lj-btn">${back ? "返回设置" : "取消"}</button><button id="lj-confirm" class="lj-btn ${danger ? "lj-danger-fill" : "lj-btn-primary"}">${label}</button>`, "form");
    content.querySelector("#lj-cancel").onclick = () => back ? back() : dialog.close();
    content.querySelector("#lj-confirm").onclick = ev => editor.run(ev.target, fn);
  }

  function undoDialog() {
    if (!data.state.counts.summarized) {
      const back = returnTo;
      return confirmDialog("没有可以撤销的整理", "<p>这份聊天还没有成功的整理记录。</p>", "知道了", async () => (back ? back() : dialog.close()));
    }
    confirmDialog("撤销最近一次整理", "<p>这次整理新增的剧情，以及人物、关系、生活小档案和约定的变化会被撤回，对应的对话回到待整理状态，下次整理时重新处理。</p>", "撤销", async () => {
      try { data = await act("undo"); }
      catch (error) {
        if (!/修改过/.test(error.message)) throw error;
        return confirmDialog("撤销会覆盖手动修改", `<p>${esc(error.message)}</p>`, "仍然撤销", async () => { data = await act("undo", {force: true}); dialog.close(); await render(); }, true);
      }
      dialog.close(); toast("已撤销"); await render();
    });
  }

  function resummarizeDialog() {
    const c = data.state.counts;
    if (!(c.turns - c.pruned)) {
      const back = returnTo;
      return confirmDialog("无法重新总结", "<p>这份聊天没有可用的原文备份（可能是旧版本导入，或备份已过保留期），所以不能从头重新总结。可以在“编辑剧情”里手动补充分类和前因。</p>", "知道了", async () => (back ? back() : dialog.close()));
    }
    confirmDialog("用备份重新整理", `<p>当前记忆会存为恢复点（保留30天）后清空，然后用备份的 ${c.turns - c.pruned} 轮原文从头整理${c.pruned ? `；有 ${c.pruned} 轮已过备份保留期，无法参与` : ""}。</p><p class="lj-muted">会按所选总结连接分批调用，产生对应费用。</p>`, "开始重新整理", async () => { data = await act("resummarize", {confirmed: true}); dialog.close(); await render(); }, true);
  }

  function restoreDialog(id) {
    confirmDialog("恢复记忆", "<p>当前记忆会先存为恢复点，再换成所选版本。之后新增的对话保留为待整理。</p>", "恢复", async () => { data = await act("restore", {id}); dialog.close(); await render(); });
  }

  function clearDialog() {
    confirmDialog("清空记忆", "<p>剧情、人物、生活小档案、约定和概况会被清空，清空前存为恢复点（保留30天）。原文备份不受影响。</p>", "清空", async () => { data = await act("clear", {confirmed: true}); dialog.close(); await render(); }, true);
  }

  async function copyDialog() {
    const editor = open("复制记忆到另一份聊天", '<p class="lj-muted">所选聊天的现有记忆会被覆盖（覆盖前存为恢复点）。</p><div id="lj-copy-list"></div>', '<button data-close class="lj-btn">关闭</button>', "form");
    await editor.run(content.querySelector("[data-close]"), async () => {
      const result = await bridge.apiGet("memory/conversations", {page: 1, query: ""});
      const items = result.items.filter(i => i.cid !== view.target.cid);
      const list = content.querySelector("#lj-copy-list");
      list.innerHTML = items.map((item, i) => `<button class="lj-chat-row" data-copy="${i}"><strong>${esc(item.title)}</strong><small>${item.current ? "当前聊天" : "历史聊天"}</small></button>`).join("") || '<p class="lj-empty">没有其他聊天</p>';
      list.querySelectorAll("[data-copy]").forEach(b => b.onclick = () => editor.run(b, async () => {
        const item = items[Number(b.dataset.copy)];
        const target = await bridge.apiGet("memory/state", {scope: item.scope, cid: item.cid});
        await act("copy_to", {target: {scope: item.scope, cid: item.cid, revision: target.state.revision}});
        dialog.close(); toast("已复制到“" + item.title + "”");
      }));
    });
  }

  function importDialog() {
    const editor = open("导入记忆", '<p class="lj-muted">导入会覆盖当前记忆，覆盖前存为恢复点。</p><input id="lj-import-file" type="file" accept=".json,application/json">', '<button data-close class="lj-btn">取消</button><button id="lj-import-confirm" class="lj-btn lj-btn-primary">导入</button>', "form");
    content.querySelector("#lj-import-confirm").onclick = ev => editor.run(ev.target, async () => {
      const file = content.querySelector("#lj-import-file").files[0];
      if (!file) throw new Error("请选择文件");
      data = await post("import", {payload: parseMemoryImport(await file.text()), revision: data.state.revision});
      dialog.close(); await render();
    });
  }

  const KIND = {FriendMessage: "私聊", GroupMessage: "群聊"};
  const scopeParts = scope => {
    const [platform, kind, ...rest] = String(scope || "").split(":");
    return {platform, kind: KIND[kind] || kind, number: rest.join(":")};
  };

  async function bindingDialog() {
    const choices = await bridge.apiGet("memory/binding_options");
    let picked = choices.current;
    const optionMarkup = o => `<button type="button" class="lj-session" data-scope="${esc(o.scope)}" aria-pressed="${o.scope === picked}"><span class="lj-session-main"><strong>${esc(o.name)}</strong><small>${esc(o.scope)}</small></span><span class="lj-session-tags">${o.character ? `<em>${esc(o.character)}</em>` : ""}<span class="lj-pill">${esc(o.platform)}</span></span></button>`;
    const current = choices.options.find(o => o.scope === choices.current);
    const bots = choices.bots;
    const body = `<p class="lj-muted">会话就是“机器人 ↔ 某个QQ用户或群”。绑定后，拾忆回响会自动显示这个会话当前正在用的聊天文件；对方 /new 开了新对话，这里也会跟着切过去。</p>${choices.current ? `<div class="lj-connection"><strong>当前绑定：${esc(current?.name || scopeParts(choices.current).number)}</strong><span>${esc(choices.current)}</span><button type="button" class="lj-text" id="lj-bind-clear">解除绑定</button></div>` : ""}<details class="lj-group lj-session-pick" id="lj-session-pick"><summary>选择会话<span class="lj-muted" id="lj-picked-label">${choices.options.length ? `（${choices.options.length}个）` : ""}</span></summary><input id="lj-session-filter" class="lj-session-filter" type="search" placeholder="搜索名称或QQ号"><div class="lj-options" id="lj-session-list">${choices.options.map(optionMarkup).join("") || '<p class="lj-muted">还没有会话。机器人收到过消息的用户或群会出现在这里。</p>'}</div></details><details class="lj-group"><summary>列表里没有？手动填写QQ号</summary><form id="lj-binding-form" class="lj-form"><div class="lj-form-row"><label>类型<select name="kind"><option value="FriendMessage">私聊（QQ号）</option><option value="GroupMessage">群聊（群号）</option></select></label><label>号码<input name="number" inputmode="numeric" pattern="[1-9][0-9]{4,11}" placeholder="例如 123456789"></label></div>${bots.length > 1 ? `<label>机器人（AstrBot 里的QQ平台）<select name="bot">${bots.map(b => `<option value="${esc(b.id)}">${esc(b.name)}</option>`).join("")}</select></label>` : ""}${bots.length ? "" : '<p class="lj-muted">AstrBot 里没有已启用的QQ平台，暂时只能从上面的列表里选。</p>'}</form></details>`;
    const ed = open("会话绑定", body, '<button data-close class="lj-btn">取消</button><button id="lj-bind-save" class="lj-btn lj-btn-primary">保存绑定</button>', "form");
    const form = content.querySelector("#lj-binding-form");
    content.querySelector("#lj-session-filter").oninput = ev => {
      const q = ev.target.value.trim().toLowerCase();
      content.querySelectorAll("[data-scope]").forEach(b => { b.hidden = q && !b.textContent.toLowerCase().includes(q); });
    };
    content.querySelectorAll("[data-scope]").forEach(b => b.onclick = () => {
      picked = b.dataset.scope; form.number.value = "";
      content.querySelectorAll("[data-scope]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
      const name = choices.options.find(o => o.scope === picked)?.name || "";
      content.querySelector("#lj-picked-label").textContent = name ? `：已选 ${name}` : "";
    });
    form.number.oninput = () => { if (form.number.value) { picked = ""; content.querySelectorAll("[data-scope]").forEach(x => x.setAttribute("aria-pressed", "false")); } };
    content.querySelector("#lj-bind-save").onclick = ev => ed.run(ev.target, async () => {
      const number = form.number.value.trim();
      if (!number && !picked) throw new Error("请选择一个会话，或填写QQ号");
      await bridge.apiPost("memory/bind", number ? {recipient: {bot: form.bot?.value || "", kind: form.kind.value, number}} : {scope: picked});
      following = true; view.target = null; dialog.close(); toast("已绑定"); await refresh();
    });
    content.querySelector("#lj-bind-clear")?.addEventListener("click", ev => ed.run(ev.target, async () => { await bridge.apiPost("memory/bind", {scope: ""}); dialog.close(); toast("已解除绑定"); }));
  }

  // ---------------------------------------------------------- settings ----
  async function settingsPage() {
    const session = ++settingsSession;
    const target = params();
    let settingsActive = true;
    let vecTimer = null, imageTimer = null, changed = false;
    // Strict reads own one foreground intent; progress reads own a displayed
    // form; configuration writes own a server revision, independent of drafts.
    const owner = {display: 0, intent: 0, images: 0, vectors: 0, write: null};
    const sameTarget = () => session === settingsSession && alive() && view.target?.scope === target.scope && view.target?.cid === target.cid;
    const currentSession = () => settingsActive && sameTarget();
    const fields = () => JSON.stringify([...content.querySelectorAll("input, select, textarea")].map(el => [el.value, el.checked]));
    const token = (strict = true) => {
      if (strict) ++owner.intent;
      return {strict, intent: owner.intent, display: owner.display, root: content.firstElementChild, form: content.querySelector("form"), open: dialog.open, kind: dialog.getAttribute("data-memory-dialog"), tab: view.settingsTab, fields: fields()};
    };
    const visible = t => currentSession() && t.display === owner.display && t.root === content.firstElementChild && t.form === content.querySelector("form") && t.open === dialog.open && t.kind === dialog.getAttribute("data-memory-dialog") && t.tab === view.settingsTab;
    const current = t => visible(t) && (!t.strict || (t.intent === owner.intent && t.fields === fields()));
    const clearConnectionKeys = (root = content) => {
      root.querySelectorAll("[data-connection-key]").forEach(input => { input.value = ""; input.type = "password"; });
      root.querySelectorAll("[data-preset-key]").forEach(button => { button.textContent = "显示"; button.setAttribute("aria-pressed", "false"); });
    };
    const invalidate = () => { ++owner.display; ++owner.intent; clearConnectionKeys(); clearTimeout(vecTimer); clearTimeout(imageTimer); };
    const guarded = async (t, fn) => { try { return await fn(); } catch (error) { if (current(t)) throw error; } };
    const busyButtons = new WeakMap();
    const run = async (ed, button, strict, fn) => {
      const t = token(strict), ticket = {pending: true};
      const state = busyButtons.get(button) || {text: button.textContent, disabled: button.disabled, count: 0};
      state.latest = ticket; ++state.count; busyButtons.set(button, state);
      try { return await ed.run(button, async () => {
        try { return await guarded(t, () => fn(t)); }
        finally {
          // Capture a valid callback's new label before generic busy cleanup.
          if (state.latest === ticket && button.textContent !== "处理中…") state.text = button.textContent;
        }
      }); }
      finally {
        ticket.pending = false; --state.count;
        // The generic busy helper also has a finally. Restore the current
        // request's ownership when an older call finishes on this same button.
        button.disabled = state.latest.pending || state.disabled;
        if (state.latest.pending) { button.setAttribute("aria-busy", "true"); if (!button.querySelector("svg")) button.textContent = "处理中…"; }
        else { button.removeAttribute("aria-busy"); if (!button.querySelector("svg")) button.textContent = state.text; }
        if (!state.count) busyButtons.delete(button);
      }
    };
    const configWrite = async (t, request, commit) => {
      if (owner.write) throw new Error("设置正在保存，请稍后再试");
      const write = {}; owner.write = write;
      try {
        const result = await request();
        if (currentSession() && owner.write === write) { commit(result, current(t)); changed = true; }
      } finally { if (owner.write === write) owner.write = null; }
    };
    dialog.addEventListener("close", () => { settingsActive = false; invalidate(); if (changed && sameTarget()) refresh(); }, {once: true});
    const initial = token();
    let s = await guarded(initial, () => bridge.apiGet("memory/settings", target));
    if (!current(initial)) return;
    const snapshot = (value = s) => JSON.stringify(value.settings) + [value.summary_preset, value.embedding_preset, value.rerank_preset].map(x => x || "").join("|");
    let saved = snapshot();
    s.revision ??= data.state.revision;
    let editing = null;
    const tabs = {rules: "总结规则", models: "模型连接", vectors: "向量召回", prompts: "提示词", data: "数据与回退"};
    let vec = null;
    let images = null;
    let imageExpanded = false;
    let bindVectorButtons = () => {};
    let bindImageButtons = () => {};
    let collectSettings = () => {};
    const loadVectors = () => bridge.apiGet("memory/vectors", target);
    const loadImages = (poll = false) => bridge.apiGet("memory/images", {...target, ...(poll ? {poll: "1"} : {})});
    if (initial.tab === "vectors") { const result = await guarded(initial, loadVectors); if (!current(initial)) return; vec = result; }
    if (initial.tab === "rules") { const result = await guarded(initial, loadImages); if (!current(initial)) return; images = result; }
    const toggle = (name, label, hint, checked) => `<label class="lj-setting"><span>${label}${hint ? `<small>${hint}</small>` : ""}</span><span class="lj-switch"><input type="checkbox" name="${name}" ${checked ? "checked" : ""}><i></i></span></label>`;
    const number = (name, label, hint, min, max, value, unit = "", step = "1") => `<label class="lj-setting"><span>${label}${hint ? `<small>${hint}</small>` : ""}</span><span class="lj-number"><input type="number" name="${name}" min="${min}" max="${max}" step="${step}" value="${value}">${unit ? `<em>${unit}</em>` : ""}</span></label>`;
    // One section per connection kind. Vector defaults follow 小白X (SiliconFlow free models).
    const SILICONFLOW = "https://api.siliconflow.cn/v1";
    const KINDS = {
      chat: {field: "summary_preset", title: "总结 API", hint: "整理记忆和识别图片", empty: "还没有选择总结用的连接。", defaults: {name: "", model: "", base_url: ""}},
      embedding: {field: "embedding_preset", title: "向量模型", hint: "按意思查找相关记忆；记忆搜索工具仍可按字面查找", empty: "未启用向量检索。", defaults: {name: "硅基流动 bge-m3", model: "BAAI/bge-m3", base_url: SILICONFLOW, dimensions: 1024}},
      rerank: {field: "rerank_preset", title: "精排模型", hint: "对找到的记忆重新排序，可不选", empty: "未启用精排。", defaults: {name: "硅基流动 bge-reranker", model: "BAAI/bge-reranker-v2-m3", base_url: SILICONFLOW}},
    };
    // Provider presets only fill the address; any OpenAI-compatible service works.
    const CHANNELS = {siliconflow: {name: "硅基流动", url: SILICONFLOW}, openai: {name: "OpenAI", url: "https://api.openai.com/v1"}, custom: {name: "自定义（OpenAI 兼容）", url: ""}};
    const channelOf = url => Object.entries(CHANNELS).find(([k, c]) => k !== "custom" && c.url === (url || "").replace(/\/+$/, ""))?.[0] || (url ? "custom" : "siliconflow");
    const presetEditor = (p, kind) => {
      const extra = kind === "chat"
        ? `<label>模型窗口（token）<input data-p="max_context_tokens" type="number" min="1024" max="2000000" value="${p.max_context_tokens || 32000}"></label>`
        : kind === "embedding" ? `<label>向量维度<input data-p="dimensions" type="number" min="1" max="4096" value="${p.dimensions || 1024}"></label>` : "";
      const note = kind === "chat" ? '<p class="lj-muted">模型窗口按服务商公布的上下文长度填写，整理时按它决定每批能放多少轮。</p>' : kind === "embedding" ? '<p class="lj-muted">换模型或维度后，已有向量会重新生成。测试连接会读出模型的实际维度。</p>' : "";
      const channel = kind === "chat" ? "" : `<label>渠道<select data-channel>${Object.entries(CHANNELS).map(([k, c]) => `<option value="${k}" ${channelOf(p.base_url) === k ? "selected" : ""}>${c.name}</option>`).join("")}</select></label>`;
      const key = `<label>API Key<span class="lj-key"><input data-p="api_key" type="password" autocomplete="off" spellcheck="false" placeholder="${p.key_configured ? "已保存，留空不修改" : ""}"><button type="button" class="lj-btn" data-key-toggle aria-pressed="false">显示</button></span></label>`;
      const model = `<label>模型<span class="lj-key"><input data-p="model" value="${esc(p.model || "")}" list="lj-model-list" autocomplete="off" spellcheck="false"><button type="button" class="lj-btn" id="lj-preset-list" title="从接口读取可用模型">拉取</button></span><datalist id="lj-model-list"></datalist></label>`;
      return `<div class="lj-preset-editor" data-kind="${kind}"><h4>${p.id ? "编辑连接" : "新建连接"}</h4><label>名称<input data-p="name" value="${esc(p.name || "")}" maxlength="60" placeholder="${kind === "chat" ? "例如：总结用 Gemini" : ""}"></label>${channel}<label>接口地址<input data-p="base_url" value="${esc(p.base_url || "")}" placeholder="https://…/v1"></label>${key}${model}<div class="lj-form-row">${extra}</div>${note}<p class="lj-muted" id="lj-preset-status" role="status"></p><div class="lj-actions"><button type="button" class="lj-btn" id="lj-preset-test">测试连接</button><button type="button" class="lj-btn lj-btn-primary" id="lj-preset-save">保存连接</button>${p.id ? '<button type="button" class="lj-btn lj-danger" id="lj-preset-delete">删除</button>' : ""}<button type="button" class="lj-text" id="lj-preset-cancel">取消</button></div></div>`;
    };
    const presetSummary = (p, kind) => kind === "chat"
      ? `${esc(p.model)} · 窗口 ${p.max_context_tokens || "未设置"} token`
      : kind === "embedding" ? `${esc(p.model)} · ${p.dimensions || "?"} 维` : esc(p.model);
    const connectionCard = kind => {
      const meta = KINDS[kind], active = s.presets.find(p => p.type === kind && p.id === s[meta.field]);
      if (!active) return `<p class="lj-muted">${meta.empty}</p>`;
      const key = `<div class="lj-key"><input data-connection-key="${kind}" aria-label="${meta.title} API Key" readonly type="password" autocomplete="off" spellcheck="false" placeholder="${active.key_configured ? "已保存" : "未配置"}">${active.key_configured ? `<button type="button" class="lj-btn" data-preset-key="${kind}" aria-label="${meta.title}密钥显示" aria-pressed="false">显示</button>` : ""}</div>`;
      return `<div class="lj-connection"><strong>${esc(active.name)}</strong><span>${presetSummary(active, kind)}</span><button type="button" class="lj-text" data-preset-edit="${kind}">编辑</button><dl class="lj-connection-details"><div><dt>接口地址</dt><dd data-connection-url>${esc(active.base_url)}</dd></div><div><dt>API Key</dt><dd>${key}</dd></div></dl></div>`;
    };
    const kindSection = kind => {
      const meta = KINDS[kind], options = s.presets.filter(p => p.type === kind), chosen = s[meta.field];
      const card = `<div data-preset-summary="${kind}">${connectionCard(kind)}</div>`;
      const editor = editing && editing.kind === kind ? presetEditor(editing.item, kind) : `<button type="button" class="lj-btn" data-preset-new="${kind}">新建连接</button>`;
      return `<section class="lj-settings-section"><h3>${meta.title}<small>${meta.hint}</small></h3><label class="lj-setting"><span>使用的连接</span><select name="${meta.field}"><option value="">${kind === "chat" ? "未选择" : "不使用"}</option>${options.map(p => `<option value="${esc(p.id)}" ${p.id === chosen ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>${card}${editor}</section>`;
    };
    const presetKey = () => Object.values(KINDS).map(k => s[k.field] || "").join("|");

    const imageProgress = () => {
      const i = images || {};
      const unavailable = !i.configured ? '<p class="memory-error">请先在模型连接里选择总结 API</p>' : "";
      const running = i.running ? '<p class="lj-accent" role="status">正在描述图片…</p>' : "";
      const pending = (i.pending || 0) + (i.unregistered || 0);
      const result = !i.running && i.result ? `<p class="lj-muted">本次描述 ${i.result.described} · 失败 ${i.result.failed} · 跳过 ${i.result.skipped}${i.result.remaining ? ` · 未处理 ${i.result.remaining}` : ""}</p>` : "";
      return `<div data-image-progress><p role="status">描述 ${i.ready || 0} · 待描述 ${pending} · 失败 ${i.failed || 0}</p>${running}${result}${unavailable}<div class="lj-actions"><button type="button" class="lj-btn" id="lj-image-fill" ${i.enabled && i.configured && s.settings.image_describe && !i.running && (i.available || i.current_pending) ? "" : "disabled"}>补描述</button><button type="button" class="lj-text" id="lj-image-refresh">刷新</button></div>${i.current_pending > 100 ? '<p class="lj-muted">每次最多处理 100 张。</p>' : ""}</div>`;
    };
    const imageGroup = st => `<details class="lj-settings-section lj-group" data-image-group><summary>图片</summary>${toggle("image_describe", "识别图片内容", "新图随总结一起识别，使用总结 API", st.image_describe)}${number("image_keep_turns", "保留原图的最近轮数", "更早且已有描述的图片换成文字；未识别的仍保留原图", 0, 50, st.image_keep_turns, "轮")}${imageProgress()}<details class="lj-group"><summary>可处理范围</summary><p class="lj-muted">仅处理当前聊天文件中仍保留的图片；已移除的原图无法补描述。手动补描述失败不自动重试；随总结识图沿用整理重试设置。</p></details></details>`;

    const pane = st => {
      if (view.settingsTab === "rules") return `<section class="lj-settings-section"><h3>此聊天<small>即时生效</small></h3>${toggle("enabled", "启用记忆", "关闭后不再备份和注入，已有记忆保留", data.state.enabled)}${toggle("inject_enabled", "聊天时加入记忆", "临时加入，不写进聊天文件", st.inject_enabled)}</section><section class="lj-settings-section"><h3>自动整理</h3>${toggle("auto_process", "启用自动整理", "", st.auto_process)}<div class="lj-sub">${number("summary_trigger_turns", "每积累多少轮整理一次", "", 5, 300, st.summary_trigger_turns, "轮")}</div></section><details class="lj-settings-section lj-group" open><summary>原文处理</summary>${number("keep_turns", "整理后保留最近原话", "已整理的轮次从聊天里移除，只保留最近这些轮", 0, 100, st.keep_turns, "轮")}${number("retention_days", "已整理原文的备份保留", "保留期内可撤销或重新整理；0为不清理", 0, 3650, st.retention_days, "天")}</details><details class="lj-settings-section lj-group"><summary>记忆注入</summary>${number("memory_tokens", "每次聊天加入的记忆上限", "按约1字1个估算，与模型无关；记忆和聊天加起来最好不超过10万", 500, 100000, st.memory_tokens, "token")}</details><details class="lj-settings-section lj-group"><summary>请求与重试</summary>${number("summary_max_retries", "失败后最多重试", "重试会重发本批文字和图片，按所选连接计费", 0, 10, st.summary_max_retries, "次")}${number("summary_timeout", "单次整理等待时间", "", 30, 1200, st.summary_timeout, "秒")}</details>${imageGroup(st)}`;
      if (view.settingsTab === "models") return Object.keys(KINDS).map(kindSection).join("");
      if (view.settingsTab === "vectors") {
        const v = vec || {counts: {}, total: 0, done: 0, missing: 0};
        const ready = v.configured;
        const kinds = {event: "剧情", profile: "生活小档案", plan: "未结束的约定"};
        const rows = Object.entries(kinds).map(([k, n]) => { const c = v.counts[k] || {total: 0, done: 0}; return `<div class="lj-vec-count"><span>${n}</span><b ${c.done < c.total ? 'data-missing="true"' : ""}>${c.done} / ${c.total}</b></div>`; }).join("");
        const state = !ready ? '<p class="lj-muted">还没有选择向量模型，请先到“模型连接”配置。</p>'
          : v.running ? '<p class="lj-accent" role="status">正在生成向量…</p>'
          : v.missing ? `<p class="memory-error" role="status">${v.missing} 条记忆还没有向量，暂不参与向量召回。</p>` : "";
        return `<section class="lj-settings-section"><h3>启用与参数<small>此聊天</small></h3>${toggle("vector_enabled", "启用向量检索", "关闭时聊天不做自动召回；记忆搜索工具仍可按字面查找", st.vector_enabled)}${toggle("query_rewrite", "召回前改写查询", "每次召回多调用一次总结 API，按所选连接计费", st.query_rewrite)}${ready ? `<p class="lj-muted">向量模型：${esc(v.model)} · ${v.dimensions} 维</p>` : ""}<details class="lj-settings-section lj-group"><summary>召回参数</summary>${number("recall_candidates", "送入精排的候选数", "", 5, 50, st.recall_candidates, "条")}${number("recall_max", "最多采用的记忆数", "", 1, 10, st.recall_max, "条")}${number("recall_rerank_min", "精排采用分数", "达到此分数的候选会被采用", 0, 1, st.recall_rerank_min, "", "0.01")}${number("recall_vector_min", "相似度采用分数", "没有达到精排分数时，达到此相似度也会采用", 0, 1, st.recall_vector_min, "", "0.01")}</details></section><section class="lj-settings-section" data-vector-progress><h3>处理进度<small>此聊天</small></h3><div class="lj-vec-counts">${rows}</div>${state}<div class="lj-actions"><button type="button" class="lj-btn lj-btn-primary" data-vec="fill" ${ready && st.vector_enabled && !v.running && v.missing ? "" : "disabled"}>补齐缺漏</button><button type="button" class="lj-btn" data-vec="rebuild" ${ready && st.vector_enabled && !v.running && v.total ? "" : "disabled"} title="清空后按当前模型全部重新生成">完整重建</button><button type="button" class="lj-btn lj-danger" data-vec="clear" ${!v.running && (v.done || v.other_model) ? "" : "disabled"}>清空</button><button type="button" class="lj-text" id="lj-vec-refresh">刷新</button></div><p class="lj-muted">整理、编辑、撤销后会自动更新变化的条目；失败不会自动重试，需要时点“补齐缺漏”。</p></section>`;
      }
      if (view.settingsTab === "prompts") {
        const custom = st.summary_prompt.trim() && st.summary_prompt.trim() !== s.default_prompt.trim();
        return `<section class="lj-settings-section"><div class="lj-prompt-head"><h3>写作要求<small>${custom ? "已自定义" : "使用内置默认"}</small></h3><button type="button" class="lj-btn" id="lj-prompt-reset" ${custom ? "" : "disabled"}>恢复默认</button></div><textarea class="lj-editor lj-prompt" name="summary_prompt" rows="22" maxlength="20000" spellcheck="false">${esc(custom ? st.summary_prompt : s.default_prompt)}</textarea><p class="lj-muted">这是发给总结模型的写作要求，决定怎么分剧情、写摘要、记人物和约定。保持默认时会随插件更新；改过后保留你的版本。修改只影响之后的整理，不会改动已有记忆。</p></section><section class="lj-settings-section"><h3>称呼</h3><label class="lj-setting"><span>总结里对用户的称呼<small>留空写作“用户”</small></span><input name="user_name" maxlength="40" value="${esc(st.user_name)}" placeholder="用户"></label></section><details class="lj-settings-section lj-group"><summary>输出格式（固定，不可修改）</summary><p class="lj-muted">程序按这个格式读取结果，所以不随写作要求改变。</p><pre class="lj-pre">${esc(s.output_format)}</pre></details>`;
      }
      const c = data.state.counts;
      return `<section class="lj-settings-section"><h3>回退<small>此聊天</small></h3><div class="lj-setting"><span>撤销最近一次整理<small>撤回最近一批新增的剧情和约定变化，对应对话回到待整理</small></span><button type="button" class="lj-btn" id="lj-s-undo">撤销</button></div><div class="lj-setting"><span>重新总结全部<small>当前记忆存为恢复点后清空，用备份的 ${c.turns - c.pruned} 轮原文从头整理；会产生模型费用</small></span><button type="button" class="lj-btn" id="lj-s-resum">重新总结</button></div><div class="lj-setting"><span>恢复点与整理记录<small>清空、重新总结、导入前自动保存，保留30天</small></span><button type="button" class="lj-btn" id="lj-s-records">查看</button></div></section><section class="lj-settings-section"><h3>导入与导出</h3><div class="lj-setting"><span>导出记忆<small>下载为 JSON 文件</small></span><button type="button" class="lj-btn" id="lj-s-export">导出</button></div><div class="lj-setting"><span>导入记忆<small>覆盖当前记忆，覆盖前存为恢复点</small></span><button type="button" class="lj-btn" id="lj-s-import">导入</button></div><div class="lj-setting"><span>复制到另一份聊天<small>开新档后，把当前记忆带过去</small></span><button type="button" class="lj-btn" id="lj-s-copy">选择聊天</button></div></section>`;
    };

    const confirmOwned = (title, body, label, submit, danger = false) => {
      invalidate(); let confirmation, pending = false;
      returnTo = () => { if (current(confirmation)) draw(); };
      confirmDialog(title, body, label, async () => {
        if (!current(confirmation) || pending) return;
        pending = true;
        try { await guarded(confirmation, () => submit(confirmation)); }
        finally { pending = false; }
      }, danger);
      confirmation = token();
    };
    const updateProgress = resource => {
      collectSettings();
      const next = document.createElement("div");
      next.innerHTML = resource === "images" ? imageProgress() : pane(s.settings);
      const selector = resource === "images" ? "[data-image-progress]" : "[data-vector-progress]";
      content.querySelector(selector)?.replaceWith(next.querySelector(selector));
      (resource === "images" ? bindImageButtons : bindVectorButtons)();
    };
    const progress = async (resource, t, poll = false) => {
      const sequence = ++owner[resource];
      let result;
      try { result = await (resource === "images" ? loadImages(poll) : loadVectors()); }
      catch (error) { if (current(t) && sequence === owner[resource]) throw error; return false; }
      if (!current(t) || sequence !== owner[resource]) return false;
      if (resource === "images") images = result; else vec = result;
      updateProgress(resource);
      return true;
    };
    const draw = () => {
      if (!currentSession()) return;
      invalidate();
      const st = s.settings;
      const body = `<div class="lj-settings"><nav class="lj-settings-nav">${Object.entries(tabs).map(([k, n]) => `<button type="button" data-stab="${k}" aria-current="${view.settingsTab === k ? "page" : "false"}">${n}</button>`).join("")}</nav><form id="lj-settings-form" class="lj-settings-body">${pane(st)}</form></div>`;
      const ed = open("总结设置", body, `<span id="lj-unsaved" class="lj-muted" hidden>有未保存修改</span><button id="lj-settings-reset" class="lj-btn">还原修改</button><button id="lj-settings-save" class="lj-btn lj-btn-primary">保存设置</button>`, "settings");
      const form = content.querySelector("#lj-settings-form");
      const imageDetails = form.querySelector("[data-image-group]");
      if (imageDetails) {
        imageDetails.open = imageExpanded;
        imageDetails.ontoggle = () => { imageExpanded = imageDetails.open; };
      }
      const collect = () => {
        if (!currentSession() || content.querySelector("#lj-settings-form") !== form) return;
        const v = name => form.querySelector(`[name="${name}"]`);
        const next = {...s.settings};
        for (const k of ["auto_process", "inject_enabled", "vector_enabled", "query_rewrite", "image_describe"]) if (v(k)) next[k] = v(k).checked;
        for (const k of ["summary_trigger_turns", "keep_turns", "retention_days", "memory_tokens", "summary_max_retries", "summary_timeout", "recall_candidates", "recall_max", "recall_rerank_min", "recall_vector_min", "image_keep_turns"]) if (v(k)) next[k] = Number(v(k).value);
        if (v("user_name")) next.user_name = v("user_name").value;
        // Saving the unchanged default keeps following future plugin updates.
        if (v("summary_prompt")) next.summary_prompt = v("summary_prompt").value.trim() === s.default_prompt.trim() ? "" : v("summary_prompt").value;
        for (const meta of Object.values(KINDS)) if (v(meta.field)) s[meta.field] = v(meta.field).value;
        s.settings = next;
        content.querySelector("#lj-unsaved").hidden = snapshot() === saved;
      };
      collectSettings = collect;
      form.oninput = form.onchange = event => {
        ++owner.intent; collect();
        const kind = Object.keys(KINDS).find(k => KINDS[k].field === event.target.getAttribute("name"));
        if (kind) {
          const card = form.querySelector(`[data-preset-summary="${kind}"]`);
          clearConnectionKeys(card); card.innerHTML = connectionCard(kind);
        }
      };
      content.querySelectorAll("[data-stab]").forEach(b => b.onclick = () => run(ed, b, true, async t => {
        collect();
        const tab = b.dataset.stab;
        // Keep the displayed tab and its polling alive until navigation commits.
        if (tab === view.settingsTab) return;
        const result = tab === "vectors" ? await loadVectors() : tab === "rules" ? await loadImages() : null;
        if (!current(t)) return;
        if (tab === "vectors") vec = result;
        if (tab === "rules") images = result;
        view.settingsTab = tab; editing = null; draw();
      }));
      bindVectorButtons = () => {
        content.querySelector("#lj-vec-refresh")?.addEventListener("click", ev => run(ed, ev.target, false, async t => { if (await progress("vectors", t)) watchVectors(); }));
        content.querySelectorAll("[data-vec]").forEach(b => b.onclick = () => {
          const name = b.dataset.vec;
          const submit = async t => {
            const result = await post("vectors", {...target, action: name});
            if (!current(t)) return;
            vec = result; changed = true; toast({fill: "已开始补齐", rebuild: "已开始重建", clear: "已清空向量"}[name]); draw();
          };
          if (name === "fill") return run(ed, b, true, submit);
          const text = name === "rebuild" ? `<p>清空此聊天的 ${vec.done} 条向量后，按当前模型重新生成全部 ${vec.total} 条。</p>` : "<p>清空此聊天的全部向量。聊天召回会暂时停用；记忆搜索工具仍可按字面查找。</p>";
          collect();
          confirmOwned(name === "rebuild" ? "完整重建向量" : "清空向量", text, name === "rebuild" ? "开始重建" : "清空", submit, name === "clear");
        });
        // The pane redraw keeps the form; keep its switch feeding the unsaved check.
        const sw = content.querySelector('[name="vector_enabled"]');
        if (sw) sw.onchange = () => { ++owner.intent; collect(); };
      };
      bindVectorButtons();
      bindImageButtons = () => {
        content.querySelector("#lj-image-refresh")?.addEventListener("click", ev => run(ed, ev.target, false, async t => {
          if (await progress("images", t)) watchImages();
        }));
        content.querySelector("#lj-image-fill")?.addEventListener("click", ev => run(ed, ev.target, true, async t => {
          collectSettings();
          if (snapshot() !== saved) { t.fields = fields(); throw new Error("请先保存设置，再补描述"); }
          const preview = await loadImages();
          if (!visible(t) || t.intent !== owner.intent) return;
          collectSettings();
          if (snapshot() !== saved) { t.fields = fields(); throw new Error("请先保存设置，再补描述"); }
          if (!current(t)) return;
          // Preview freezes only its confirmation scope. Progress has its own
          // ordering and may already have received a newer resource snapshot.
          const refs = [...(preview.refs || [])];
          if (preview.running) throw new Error("图片正在处理中");
          if (!refs.length) throw new Error("当前聊天没有可补描述的图片");
          const nonnegative = value => Number.isInteger(value) && value >= 0;
          const count = nonnegative(preview.available) ? preview.available : refs.length;
          const requests = [preview.batch_count, preview.max_requests].find(nonnegative);
          const batchText = requests === undefined ? "" : `，预计最多 ${requests} 次批量请求`;
          const blocked = nonnegative(preview.blocked_images) && preview.blocked_images ? `<p class="memory-error">其中 ${preview.blocked_images} 张超出当前处理上限，将保留原图。</p>` : "";
          imageExpanded = content.querySelector("[data-image-group]")?.open || false;
          confirmOwned("补描述", `<p>为当前聊天的 ${count} 张图片补描述，使用总结 API${batchText}。每批失败后不自动重试。</p>${blocked}`, "开始描述", async confirmation => {
            const result = await bridge.apiPost("memory/images", {...target, action: "fill", confirmed: true, refs});
            if (!current(confirmation)) return;
            images = result; changed = true;
            toast("已开始补描述"); view.settingsTab = "rules"; draw();
          });
        }));
      };
      bindImageButtons();
      const enabled = form.querySelector('[name="enabled"]');
      if (enabled) enabled.onchange = event => {
        event.stopPropagation();
        return run(ed, enabled, true, async t => {
        const value = enabled.checked;
        await configWrite(t, () => post("action", {...target, action: "enable", enabled: value, revision: s.revision}), (result, show) => {
          data = result; s.revision = result.state.revision;
          if (show) toast(value ? "已启用记忆" : "已关闭记忆");
        });
        });
      };
      content.querySelectorAll("[data-preset-new]").forEach(b => b.onclick = () => { collect(); const kind = b.dataset.presetNew; editing = {kind, item: {...KINDS[kind].defaults}}; draw(); });
      content.querySelectorAll("[data-preset-summary]").forEach(card => card.onclick = event => {
        const button = event.target.closest("button");
        if (button?.dataset.presetKey && !button.disabled) return run(ed, button, true, async t => {
          const kind = button.dataset.presetKey, input = card.querySelector("[data-connection-key]");
          if (input.type === "text") { clearConnectionKeys(card); return; }
          const preset = s.presets.find(p => p.type === kind && p.id === s[KINDS[kind].field]);
          if (!preset?.key_configured) return;
          const result = await bridge.apiPost("memory/model_key", {id: preset.id, base_url: preset.base_url, revision: s.models_revision});
          if (!current(t)) return;
          input.value = result.value; input.type = "text";
          button.textContent = "隐藏"; button.setAttribute("aria-pressed", "true");
        });
        const kind = button?.dataset.presetEdit;
        if (!kind) return;
        collect(); editing = {kind, item: s.presets.find(p => p.id === s[KINDS[kind].field]) || {}}; draw();
      });
      content.querySelector("#lj-preset-cancel")?.addEventListener("click", () => { editing = null; draw(); });
      const presetData = () => {
        const v = n => content.querySelector(`[data-p="${n}"]`)?.value.trim();
        const kind = editing.kind;
        const config = {name: v("name"), type: kind, base_url: v("base_url"), model: v("model"), ...(v("api_key") ? {api_key: v("api_key")} : {})};
        if (kind === "chat") config.max_context_tokens = Number(v("max_context_tokens"));
        if (kind === "embedding") config.dimensions = Number(v("dimensions"));
        return {id: editing.item.id, config, revision: s.models_revision};
      };
      const status = (text, ok) => { const el = content.querySelector("#lj-preset-status"); if (el) { el.textContent = text; el.dataset.ok = ok ? "true" : "false"; } };
      content.querySelector("[data-channel]")?.addEventListener("change", ev => {
        ++owner.intent;
        const url = CHANNELS[ev.target.value].url, input = content.querySelector('[data-p="base_url"]');
        if (url || ev.target.value === "custom") input.value = url;
        if (ev.target.value === "custom") input.focus();
      });
      content.querySelector('[data-p="base_url"]')?.addEventListener("input", ev => { const sel = content.querySelector("[data-channel]"); if (sel) sel.value = channelOf(ev.target.value.trim()); });
      content.querySelector("[data-key-toggle]")?.addEventListener("click", ev => run(ed, ev.target, true, async t => {
        const button = ev.target.closest("button"), input = content.querySelector('[data-p="api_key"]');
        if (input.type === "text") { input.type = "password"; button.textContent = "显示"; button.setAttribute("aria-pressed", "false"); return; }
        // A saved key is only revealed for the same address it was saved with.
        if (!input.value && editing.item.id && editing.item.key_configured) {
          if (content.querySelector('[data-p="base_url"]').value.trim().replace(/\/+$/, "") !== (editing.item.base_url || "").replace(/\/+$/, "")) throw new Error("接口地址已改动，旧 Key 不再显示，请重新填写");
          const result = await bridge.apiPost("memory/model_key", {id: editing.item.id, base_url: editing.item.base_url, revision: s.models_revision});
          if (!current(t)) return;
          input.value = result.value || ""; ++owner.intent;
        }
        input.type = "text"; button.textContent = "隐藏"; button.setAttribute("aria-pressed", "true");
      }));
      content.querySelector("#lj-preset-list")?.addEventListener("click", ev => run(ed, ev.target, true, async t => {
        const r = await bridge.apiPost("memory/model_preset", {operation: "list", ...presetData()});
        if (!current(t)) return;
        const want = {embedding: {test: m => !/rerank/i.test(m) && /embed|bge|m3|e5|gte/i.test(m)}, rerank: /rerank/i}[editing.kind];
        const items = want ? [...r.items.filter(m => want.test(m)), ...r.items.filter(m => !want.test(m))] : r.items;
        content.querySelector("#lj-model-list").innerHTML = items.map(m => `<option value="${esc(m)}"></option>`).join("");
        status(`读到 ${r.items.length} 个模型，在“模型”里输入或选择`, true);
        content.querySelector('[data-p="model"]').focus();
      }));
      content.querySelector("#lj-preset-test")?.addEventListener("click", ev => run(ed, ev.target, true, async t => {
        status("", true);
        try {
          const r = await bridge.apiPost("memory/model_preset", {operation: "test", ...presetData()});
          if (!current(t)) return;
          const field = content.querySelector('[data-p="dimensions"]');
          if (r.dimensions && field && Number(field.value) !== r.dimensions) {
            field.value = r.dimensions;
            ++owner.intent;
            status(`连接成功，模型是${r.dimensions}维，已帮你改好，保存连接即可`, true);
          } else status(r.dimensions ? `连接成功（${r.dimensions}维）` : "连接成功", true);
        } catch (error) {
          if (current(t)) status(error.message || "连接失败", false);
        }
      }));
      content.querySelector("#lj-preset-save")?.addEventListener("click", ev => run(ed, ev.target, true, async t => {
        const field = KINDS[editing.kind].field;
        const payload = presetData();
        await configWrite(t, () => bridge.apiPost("memory/model_preset", {operation: "save", ...payload}), (r, show) => {
          const added = r.presets.find(p => !s.presets.some(o => o.id === p.id));
          s = {...s, presets: r.presets, models_revision: r.models_revision};
          if (!show) return;
          if (added && !s[field]) s[field] = added.id;
          editing = null; toast("连接已保存，记得保存设置"); draw();
        });
      }));
      content.querySelector("#lj-preset-delete")?.addEventListener("click", ev => run(ed, ev.target, true, async t => {
        const payload = {operation: "delete", id: editing.item.id, revision: s.models_revision};
        await configWrite(t, () => bridge.apiPost("memory/model_preset", payload), (r, show) => {
          s = {...s, presets: r.presets, models_revision: r.models_revision};
          if (show) { editing = null; draw(); }
        });
      }));
      content.querySelector("#lj-s-records")?.addEventListener("click", () => { dialog.close(); view.tab = "records"; render(); });
      const dataDialog = show => {
        invalidate(); let back;
        returnTo = () => { if (current(back)) { view.settingsTab = "data"; draw(); } };
        show(); back = token();
      };
      content.querySelector("#lj-s-undo")?.addEventListener("click", () => dataDialog(undoDialog));
      content.querySelector("#lj-s-resum")?.addEventListener("click", () => dataDialog(resummarizeDialog));
      content.querySelector("#lj-prompt-reset")?.addEventListener("click", () => { form.querySelector('[name="summary_prompt"]').value = s.default_prompt; collect(); draw(); });
      content.querySelector("#lj-s-export")?.addEventListener("click", () => bridge.download("memory/export", target, "lumen-memory.json"));
      content.querySelector("#lj-s-import")?.addEventListener("click", () => { invalidate(); importDialog(); });
      content.querySelector("#lj-s-copy")?.addEventListener("click", () => { invalidate(); copyDialog(); });
      content.querySelector("#lj-settings-reset").onclick = () => { dialog.close(); settingsPage(); };
      content.querySelector("#lj-settings-save").onclick = ev => run(ed, ev.target, true, async t => {
        collect();
        const payload = {settings: {...s.settings}, summary_preset: s.summary_preset, embedding_preset: s.embedding_preset, rerank_preset: s.rerank_preset, revision: s.revision};
        await configWrite(t, () => post("settings", {...target, ...payload}), (result, show) => {
          // The acknowledgement belongs to submitted A even if B is now being
          // edited. Keep B's DOM/draft and advance only the saved baseline.
          saved = snapshot(result); s.revision = result.revision;
          data.state.revision = result.revision;
          ++owner.images; ++owner.vectors;
          collectSettings();
          if (show && snapshot() === saved) toast("设置已保存");
        });
        if (visible(t) && ["rules", "vectors"].includes(view.settingsTab)) {
          const resource = view.settingsTab === "rules" ? "images" : "vectors", progressOwner = token(false);
          try { if (await progress(resource, progressOwner, true)) watch(resource); }
          catch { /* the save succeeded; a progress refresh must not undo its acknowledgement */ }
        }
      });
      collect();
      watchVectors();
      watchImages();
    };

    // Polls are attached to the displayed form, not foreground navigation or
    // input intents. A cancelled navigation therefore cannot stop progress.
    const watch = resource => {
      const isImage = resource === "images", tab = isImage ? "rules" : "vectors";
      clearTimeout(isImage ? imageTimer : vecTimer);
      if (!currentSession() || !dialog.open || dialog.getAttribute("data-memory-dialog") !== "settings" || view.settingsTab !== tab || !(isImage ? images : vec)?.running) return;
      const t = token(false);
      const timer = setTimeout(async () => {
        if (!current(t)) return;
        try { if (await progress(resource, t, true)) watch(resource); }
        catch { if (current(t)) watch(resource); }
      }, 2000);
      if (isImage) imageTimer = timer; else vecTimer = timer;
    };
    const watchVectors = () => watch("vectors"), watchImages = () => watch("images");
    draw();
  }

  async function refresh() {
    await resolveTarget();
    await load();
    if (data && view.target && !view.target.title) {
      const list = await bridge.apiGet("memory/conversations", {page: 1, query: ""});
      view.target.title = list.items.find(i => i.cid === view.target.cid)?.title || "";
    }
    await render();
  }

  function watchFollow() {
    clearTimeout(followTimer);
    followTimer = setTimeout(async () => {
      if (!alive()) return;
      if (following && !dialog.open) {
        try {
          const bound = await bridge.apiGet("memory/follow");
          if (bound.cid && bound.cid !== view.target?.cid) { view.target = null; await refresh(); }
        } catch { /* retry later */ }
      }
      watchFollow();
    }, 5000);
  }

  try { await refresh(); } catch (error) { main.innerHTML = `<p class="memory-error">${esc(error.message || "拾忆回响暂时无法打开")}</p>`; }
  watchFollow();
}
