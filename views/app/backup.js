// Backup credentials live only in this dialog. The host bridge owns downloads.
let currentBackup = null;
const moduleNames = {reference: "参考", life: "朝夕拾记", memory: "拾忆回响", settings: "设置"};
const countNames = {characters: "角色", reference_assets: "参考图片", life_plans: "计划", life_adjustments: "临时调整", life_sources: "约定来源", life_evidence_metadata: "调整证据版本", life_characters: "角色与会话依赖", calendar: "日历", calendar_connections: "日历连接", mem_chats: "聊天", mem_turns: "轮次", mem_turn_aliases: "旧身份关联", mem_images: "图片说明", mem_turn_images: "轮次图片关联", mem_batches: "批次", mem_snapshots: "恢复点", memory_model_settings: "拾忆模型连接", files: "文件", settings: "设置"};
const fixedMessages = {
  BUSY: "已有备份任务正在处理，请稍后再试。", BACKUP_BUSY: "已有备份任务正在处理，请稍后再试。",
  CANCELLED: "操作已取消。", BACKUP_CANCELLED: "操作已取消。",
  INVALID_PASSWORD: "密码无效，请核对后重新校验。", AUTHENTICATION_FAILED: "密码错误或文件已损坏。",
  AUTH_FAILED: "密码错误或文件已损坏。", DECRYPT_FAILED: "密码错误或文件已损坏。",
  INVALID_FORMAT: "备份格式不受支持或文件已损坏。", UNKNOWN_FORMAT: "备份格式不受支持。",
  LIMIT_EXCEEDED: "备份超出处理上限，未生成不完整文件。", SIZE_LIMIT: "文件超出处理上限。",
  DISK_SPACE: "临时磁盘空间不足，请释放空间后重试。", DISK_FULL: "临时磁盘空间不足，请释放空间后重试。",
  EXPIRED: "这份临时备份已过期，请重新生成。", TASK_NOT_FOUND: "这份临时备份已过期，请重新生成。",
  BACKUP_NOT_FOUND: "这份临时备份已过期，请重新生成。", PASSWORD_TOO_SHORT: "密码至少需要8个字符。",
  INVALID_MODULES: "请选择有效的备份模块。", INVALID_OPTIONS: "备份选项无效，请重新选择。",
  KEYS_REQUIRE_ENCRYPTION: "包含 API Key 时必须开启密码加密。", UPLOAD_LIMIT: "文件超出上传上限。",
  REQUEST_LIMIT: "请求超出处理上限。", UPLOAD_CONFLICT: "上传进度冲突，请重新选择文件校验。",
  MISSING_UPLOAD: "未收到备份文件，请重新选择文件。", UPLOAD_INCOMPLETE: "文件尚未完整上传，请重新校验。",
  BACKUP_NOT_READY: "备份尚未就绪，请等待处理结束。", BACKUP_FAILED: "备份处理失败，请重试。",
  CLEANUP_FAILED: "临时文件尚未清理完成，请稍后重试。",
};
for (const code of ["ARCHIVE_DUPLICATE", "ARCHIVE_INVALID", "ARCHIVE_LINK", "ARCHIVE_PATH", "ARCHIVE_TYPE", "FILE_HASH", "IMAGE_HASH", "IMAGE_INVALID", "INVENTORY_MISMATCH", "JSON_DUPLICATE_KEY", "JSON_INVALID", "JSONL_INVALID", "MANIFEST_INVALID", "MANIFEST_MISSING", "PRIVATE_PATH"]) fixedMessages[code] = "文件结构或完整性校验未通过，未写入业务数据。";
for (const code of ["COMPRESSION_LIMIT", "FILE_COUNT_LIMIT", "FILE_LIMIT", "IMAGE_LIMIT", "INPUT_LIMIT", "JSON_DEPTH", "RECORD_LIMIT", "TOTAL_LIMIT"]) fixedMessages[code] = "备份超过安全处理上限，未生成或接受不完整文件。";
for (const code of ["ENVELOPE_TRUNCATED", "ENVELOPE_UNSUPPORTED", "FORMAT_UNSUPPORTED", "PLUGIN_VERSION"]) fixedMessages[code] = "文件不完整或格式不受支持。";
Object.assign(fixedMessages, {PASSWORD_REQUIRED: "请输入备份密码。", ENCRYPTION_REQUIRED: "包含凭据的备份必须加密。", DISK_IO: "临时文件读写失败，请检查可用空间后重试。", SOURCE_CHANGED: "来源在处理期间发生变化，请重新生成备份。"});
const noteNames = {
  VECTOR_CACHE_EXCLUDED: "未包含可重建的向量缓存", HOST_IMAGES_EXCLUDED: "不包含宿主聊天原图",
  CREDENTIALS_OMITTED: "连接凭据待补齐", SOURCE_REFERENCE_MISSING: "部分旧来源缺失，保留已有内容",
  SOURCE_UNPROVEN: "部分旧来源无法核实", PRUNED_TURNS: "部分原文已清理",
  CREDENTIALS_REQUIRE_COMPLETION: "连接凭据待补齐", SOURCE_REFERENCES_UNPROVEN: "原来源引用无法证明，已有内容保持",
  SOURCE_BINDING_REQUIRES_REVIEW: "原聊天及旧来源仍需在恢复时核对",
  ORIGINAL_TEXT_ALREADY_PRUNED: "部分原文已清理", ORIGINAL_IMAGES_NOT_INCLUDED: "未描述的宿主原图不在包内",
  HOST_CHAT_AND_IMAGES: "不包含宿主聊天文件及原图", GENERATED_ALBUM_IMAGES: "不包含相册成图", DERIVED_MEDIA: "不包含可重建的图片派生文件",
  EXECUTION_DELIVERY_RECOVERY_QUEUES: "不包含可重放的执行、投递和补存队列", RAW_MODEL_RESPONSE_CACHE: "不包含模型原响应缓存",
  VECTOR_CACHE: "不包含可重建的向量缓存", OPTIONAL_FUTURE_AGREEMENTS_AND_ADJUSTMENTS: "未选择未来一次约定与有效临时调整",
};
const dependencyNames = {CHARACTER_SESSION_BINDINGS: "角色与接收会话绑定", MEMORY_MODEL_CONNECTIONS: "拾忆模型连接"};
const bytes = value => {
  const n = Number(value) || 0;
  return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / 1048576).toFixed(1)} MiB`;
};
const safeCode = value => /^[A-Z][A-Z0-9_]{0,63}$/.test(String(value || "")) ? String(value) : "";
const errorText = value => fixedMessages[safeCode(value)] || "操作未完成，请重试或检查管理连接。";

export function disposeBackup() {
  currentBackup?.dispose();
}

export function openBackup({bridge, escape, settings, trigger, isCurrent = () => true}) {
  disposeBackup();
  const modal = document.createElement("dialog");
  modal.id = "backup-dialog";
  modal.className = "backup-dialog";
  modal.setAttribute("aria-labelledby", "backup-title");
  modal.innerHTML = `<div class="backup-shell"><div class="dialog-head"><h2 id="backup-title">备份与恢复</h2><button type="button" class="close" id="backup-close" aria-label="关闭备份与恢复">×</button></div>
  <div class="dialog-body backup-body">
  <section aria-labelledby="backup-export-title"><h3 id="backup-export-title">导出插件备份</h3>
  <div id="backup-modules">${Object.entries(moduleNames).map(([id, name]) => `<label class="check backup-module"><input type="checkbox" data-module="${id}" checked>${name}<span class="help" data-estimate="${id}">正在估算…</span></label>`).join("")}</div>
  <label class="check"><input type="checkbox" id="backup-future" checked>包含未来一次约定及有效临时调整</label>
  <p class="help" id="backup-dependencies"></p>
  <div class="backup-security"><label class="check"><input type="checkbox" id="backup-encrypt" checked>密码加密</label><label class="check"><input type="checkbox" id="backup-keys">包含 API Key</label></div>
  <div id="backup-password-fields" class="form-row"><div><label for="backup-password">密码</label><input id="backup-password" type="password" autocomplete="new-password" minlength="8" maxlength="1024"></div><div><label for="backup-confirm">确认密码</label><input id="backup-confirm" type="password" autocomplete="new-password" minlength="8" maxlength="1024"></div></div>
  <p class="help" id="backup-password-note">至少8个字符。请保存密码，无法找回。</p>
  <p class="backup-private">备份包含私人内容；不含 API Key 也不会改写原文中自行填写的秘密，请勿分享。</p>
  <div id="backup-unsaved" class="backup-unsaved" hidden><p>设置有未保存的修改，本次使用哪一份？</p><div class="backup-actions"><button type="button" id="backup-save-first">先保存设置</button><button type="button" id="backup-use-saved">使用已保存配置</button><button type="button" id="backup-dismiss-choice">取消</button></div></div>
  <button type="button" id="backup-export" class="primary" disabled>生成备份</button></section>
  <details id="backup-validate-section"><summary>导入与校验备份</summary><label for="backup-file">备份文件</label><input id="backup-file" type="file" accept=".cab,.lumenbackup,.zip,.lumen,.backup,application/zip,application/octet-stream"><label for="backup-decrypt-password">解密密码（明文备份留空）</label><input id="backup-decrypt-password" type="password" autocomplete="off" maxlength="1024"><div class="backup-actions"><button type="button" id="backup-validate" disabled>校验备份</button><button type="button" id="backup-import" disabled>准备导入</button></div></details>
  <section id="backup-restore" hidden></section>
  <details id="backup-archives-section"><summary>待恢复档案与启用检查</summary><div class="backup-actions"><button type="button" id="backup-archives-load">查看档案</button><button type="button" id="backup-pending-load">查看待启用对象</button><button type="button" id="backup-rollback-preview">检查最近一次回退</button></div><div id="backup-archives"></div><div id="backup-pending"></div><div id="backup-rollback"></div></details>
  <p id="backup-status" class="help" role="status" aria-live="polite">正在读取已保存数据概览…</p><p id="backup-error" class="backup-error" role="alert" hidden></p>
  <div id="backup-result" hidden></div></div><div class="dialog-foot"><button type="button" id="backup-cancel" hidden>取消处理</button><button type="button" id="backup-download" class="primary" hidden>下载这份备份</button><button type="button" id="backup-done">关闭</button></div></div>`;
  document.body.append(modal);
  const q = id => modal.querySelector("#backup-" + id);
  let alive = true, busy = false, cancelling = false, taskId = "", operation = "", timer = null;
  let readyDisplayed = false;
  let restore = null;
  let releasePending = Promise.resolve();
  let generation = 0, optionsReady = false, downloading = false, limits = {max_upload_bytes: 600 * 1048576, chunk_bytes: 1048576};
  const valid = () => alive && modal.open && isCurrent();
  const postRelease = id => id ? bridge.apiPost("backup/release", {task_id: id}).catch(() => {}) : Promise.resolve();
  const clearTimer = () => { if (timer !== null) clearTimeout(timer); timer = null; };
  const error = message => { q("error").textContent = message; q("error").hidden = !message; };
  const setBusy = value => {
    busy = value;
    for (const el of modal.querySelectorAll("input, select")) el.disabled = value || downloading;
    q("encrypt").disabled = value || downloading || q("keys").checked;
    q("future").disabled = value || downloading || !modal.querySelector('[data-module="life"]').checked;
    for (const id of ["export", "save-first", "use-saved", "dismiss-choice"]) q(id).disabled = value || downloading || !optionsReady;
    q("validate").disabled = value || downloading;
    q("import").disabled = value || downloading;
    q("cancel").hidden = !value; q("cancel").disabled = cancelling;
    q("download").disabled = value || downloading;
    restore?.sync();
  };
  const clearResult = () => {
    readyDisplayed = false;
    q("result").replaceChildren(); q("result").hidden = true; q("download").hidden = true;
    restore?.clear();
  };
  const resetTask = async () => {
    clearTimer(); const previous = taskId; taskId = ""; generation++;
    clearResult(); releasePending = releasePending.then(() => postRelease(previous)); await releasePending;
  };
  const fieldsChanged = () => {
    if (busy || downloading || !valid()) return;
    void resetTask(); error(""); q("unsaved").hidden = true;
    if (q("keys").checked) q("encrypt").checked = true;
    q("password-fields").hidden = !q("encrypt").checked;
    q("password-note").hidden = !q("encrypt").checked;
    setBusy(false);
  };
  const notes = value => (Array.isArray(value) ? value : []).map(item => {
    const raw = typeof item === "string" ? item : item?.code;
    const table = typeof raw === "string" && raw.startsWith("SOURCE_TABLE_ABSENT:") ? raw.slice(20) : "";
    const text = noteNames[safeCode(raw)] || (table in countNames ? `源库尚无${countNames[table]}表` : "存在需要确认的来源限制");
    return `${text}${Number.isSafeInteger(item?.count) ? `（${item.count}）` : ""}`;
  });
  const showResult = summary => {
    const modules = (summary.modules || []).map(id => moduleNames[id]).filter(Boolean);
    const counts = Object.entries(summary.counts || {}).filter(([key, n]) => key in countNames && Number.isSafeInteger(n) && n >= 0);
    const dependencies = (summary.dependencies || []).map(key => dependencyNames[key]).filter(Boolean);
    const extras = [...notes(summary.limitations), ...notes(summary.omissions)];
    const version = /^\d{1,4}\.\d{1,4}\.\d{1,4}(?:[-+][A-Za-z0-9.-]{1,40})?$/.test(String(summary.plugin_version || "")) ? summary.plugin_version : "";
    const rawDate = String(summary.created_utc || "");
    const parsedDate = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(rawDate) ? new Date(rawDate) : null;
    const created = parsedDate && Number.isFinite(parsedDate.getTime()) ? parsedDate.toLocaleString("zh-CN", {hour12: false}) : "";
    q("result").innerHTML = `<h3>${operation === "export" ? "备份已生成" : operation === "import" ? "文件已准备，尚未恢复" : "校验通过"}</h3><p>${escape(modules.join("、"))} · ${escape(bytes(summary.bytes))} · ${summary.encrypted ? "密码加密" : "未加密"}${summary.credentials_included ? " · 包含凭据" : " · 不含结构化凭据"}</p>
    ${version || created ? `<p class="help">${[version ? `插件 ${escape(version)}` : "", created ? `创建于 ${escape(created)}` : ""].filter(Boolean).join(" · ")}</p>` : ""}
    ${counts.length ? `<dl class="backup-counts">${counts.map(([key, count]) => `<dt>${escape(countNames[key] || key)}</dt><dd>${count}</dd>`).join("")}</dl>` : ""}
    ${dependencies.length ? `<p class="help">必要依赖：${escape(dependencies.join("、"))}</p>` : ""}
    <p class="help">${operation === "export" ? "下载为这次已生成的快照，后续配置变化不会改动文件。" : "文件结构和完整性校验通过，尚未写入或恢复任何业务数据。"}</p>
    ${extras.length ? `<details><summary>恢复限制与省略项（${extras.length}）</summary><ul>${extras.map(text => `<li>${escape(text)}</li>`).join("")}</ul></details>` : ""}`;
    q("result").hidden = false;
    q("download").hidden = operation !== "export";
  };
  const poll = async token => {
    if (!valid() || token !== generation || !taskId) return;
    try {
      const result = await bridge.apiGet("backup/status", {task_id: taskId});
      if (!valid() || token !== generation) return;
      if (!restore?.preserveError()) error("");
      if (["running", "uploading", "cancelling"].includes(result.state)) {
        q("status").textContent = cancelling ? "正在取消，等待当前处理结束…" : operation === "export" ? "正在生成备份…" : "正在校验备份…";
        timer = setTimeout(() => { timer = null; void poll(token); }, 1000);
      } else {
        if (!restore?.waiting()) setBusy(false);
        q("status").textContent = "";
        if (result.state === "ready") {
          if (operation === "restore" && result.operation === "import" && !result.summary?.restore) {
            setBusy(true);
            error("尚未确认恢复结果，请重新检查状态；不会自动重复提交。");
            timer = setTimeout(() => { timer = null; void poll(token); }, 2000);
            return;
          }
          if (!readyDisplayed) {
            if (operation === "restore") restore.completed(result.summary || {});
            else {
              showResult(result.summary || {});
              if (operation === "import") restore.catalog(result.catalog || result.summary?.catalog || {}, result.summary || {});
            }
            readyDisplayed = true;
          }
          // Keep this frozen file leased while it is being reviewed. Do not redraw it.
          timer = setTimeout(() => { timer = null; void poll(token); }, 30000);
        }
        else if (result.state === "cancelled") q("status").textContent = "操作已取消。";
        else error(errorText(result.error));
      }
    } catch (err) {
      if (!valid() || token !== generation) return;
      if (["BACKUP_NOT_FOUND", "TASK_NOT_FOUND", "EXPIRED"].includes(safeCode(err?.message))) {
        clearTimer(); taskId = ""; setBusy(false); q("download").disabled = true;
        restore.clear();
        error("这份临时备份已过期，请重新生成。"); q("status").textContent = "";
        return;
      }
      // Keep the task gate while its server-side outcome is unknown.
      error(errorText(err?.message));
      if (readyDisplayed) q("download").disabled = true;
      timer = setTimeout(() => { timer = null; void poll(token); }, 2000);
    }
  };
  const acceptTask = async (result, token) => {
    const id = typeof result?.task_id === "string" ? result.task_id : "";
    if (!valid() || token !== generation) { await postRelease(id); return false; }
    if (!id) throw new Error("INVALID_RESPONSE");
    taskId = id;
    if (cancelling) await bridge.apiPost("backup/cancel", {task_id: id});
    return true;
  };
  const exportRequest = () => {
    const modules = [...modal.querySelectorAll("[data-module]")].filter(el => el.checked).map(el => el.dataset.module);
    if (!modules.length) { error("请选择至少一个模块。"); return null; }
    const encrypt = q("encrypt").checked;
    if (encrypt && (Array.from(q("password").value).length < 8 || new TextEncoder().encode(q("password").value).length > 1024)) {
      error("密码至少8个字符，最多1024字节。"); q("password").focus(); return null;
    }
    if (encrypt && q("password").value !== q("confirm").value) {
      error("两次密码不一致。"); q("confirm").focus(); return null;
    }
    return {modules, include_keys: q("keys").checked, include_future: q("future").checked, encrypt, password: encrypt ? q("password").value : ""};
  };
  const startExport = async choice => {
    if (!valid() || busy || downloading || !optionsReady) return;
    if (!exportRequest()) return;
    if (!choice && settings?.dirty()) { q("unsaved").hidden = false; q("save-first").focus(); return; }
    q("unsaved").hidden = true; cancelling = false; setBusy(true); error("");
    try {
      if (choice === "save") {
        await settings.saveAll();
        if (!valid()) return;
        if (settings.dirty() || settings.saving) { error("设置尚未保存成功，备份未开始。请检查设置中的错误。无法导出未保存草稿。"); setBusy(false); return; }
      }
      await resetTask();
      if (!valid()) return;
      if (cancelling) { setBusy(false); q("status").textContent = "操作已取消。"; return; }
      operation = "export"; const token = generation;
      q("status").textContent = "正在生成备份…";
      if (await acceptTask(await bridge.apiPost("backup/export", exportRequest()), token)) await poll(token);
    } catch (err) {
      if (valid()) { setBusy(false); error(errorText(err?.message)); q("status").textContent = ""; }
    }
  };
  const startValidate = async (mode = "validate") => {
    if (!valid() || busy || downloading) return;
    const file = q("file").files?.[0];
    if (!file) { error("请选择备份文件。"); q("file").focus(); return; }
    if (!file.size || file.size > limits.max_upload_bytes) { error(`文件为空或超过 ${bytes(limits.max_upload_bytes)} 上限。`); return; }
    cancelling = false; setBusy(true); error("");
    try {
      await resetTask();
      if (!valid()) return;
      if (cancelling) { setBusy(false); q("status").textContent = "操作已取消。"; return; }
      operation = mode; const token = generation;
      const result = await bridge.apiPost(`backup/${mode}`, {size: file.size, password: q("decrypt-password").value});
      if (!await acceptTask(result, token)) return;
      // The host reads one supplied File into an ArrayBuffer. Slice before bridging.
      for (let offset = 0; offset < file.size && !cancelling; offset += limits.chunk_bytes) {
        if (!valid() || token !== generation) return;
        q("status").textContent = `正在上传 ${bytes(offset)} / ${bytes(file.size)}…`;
        await bridge.upload(`backup/upload/${taskId}/${offset}`, file.slice(offset, Math.min(offset + limits.chunk_bytes, file.size)));
      }
      if (!valid() || token !== generation) return;
      if (!cancelling) await bridge.apiPost(`backup/${mode}/start`, {task_id: taskId});
      await poll(token);
    } catch (err) {
      if (valid()) {
        // A partial upload cannot become a validation job and must be released.
        await postRelease(taskId); taskId = "";
        if (valid()) { setBusy(false); error(errorText(err?.message)); q("status").textContent = ""; }
      }
    }
  };
  restore = mountRestore({modal, q, bridge, escape, settings, valid, error,
    busy: () => busy || downloading, setBusy, generation: () => generation, task: () => taskId,
    resetTask, acceptTask, poll, setOperation: (value, preserve = false) => {operation = value; if (!preserve) readyDisplayed = false;},
    stopPolling: clearTimer, resumePolling: () => { clearTimer(); if (taskId) timer = setTimeout(() => {timer = null; void poll(generation);}, 30000); }});
  const dispose = () => {
    if (!alive) return;
    alive = false; generation++; clearTimer(); restore.dispose();
    const previous = taskId; taskId = "";
    for (const el of modal.querySelectorAll('input[type="password"], input[type="file"]')) el.value = "";
    modal.removeEventListener("cancel", onCancel); modal.removeEventListener("close", dispose); modal.removeEventListener("keydown", onKeydown);
    window.removeEventListener("pagehide", dispose);
    if (modal.open) modal.close(); modal.remove();
    if (currentBackup?.modal === modal) currentBackup = null;
    void postRelease(previous);
    if (trigger?.isConnected) trigger.focus();
  };
  const onCancel = event => { event.preventDefault(); dispose(); };
  const onKeydown = event => {
    if (event.key !== "Tab") return;
    const controls = [...modal.querySelectorAll("button, input, select, summary, [tabindex]")].filter(el => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length);
    if (!controls.length) return;
    const index = controls.indexOf(document.activeElement);
    if (event.shiftKey && index <= 0) { event.preventDefault(); controls.at(-1).focus(); }
    else if (!event.shiftKey && (index < 0 || index === controls.length - 1)) { event.preventDefault(); controls[0].focus(); }
  };
  currentBackup = {modal, dispose};
  modal.addEventListener("cancel", onCancel); modal.addEventListener("close", dispose); modal.addEventListener("keydown", onKeydown);
  window.addEventListener("pagehide", dispose, {once: true});
  for (const el of modal.querySelectorAll("input")) el.addEventListener("input", fieldsChanged);
  q("close").onclick = dispose; q("done").onclick = dispose;
  q("export").onclick = () => startExport();
  q("save-first").onclick = () => startExport("save"); q("use-saved").onclick = () => startExport("saved");
  q("dismiss-choice").onclick = () => { q("unsaved").hidden = true; q("export").focus(); };
  q("validate").onclick = () => startValidate();
  q("import").onclick = () => startValidate("import");
  q("cancel").onclick = async () => {
    if (!busy || cancelling) return;
    cancelling = true; q("cancel").disabled = true; q("status").textContent = "正在取消，等待当前处理结束…";
    try { if (taskId) await bridge.apiPost("backup/cancel", {task_id: taskId}); }
    catch (err) { if (valid()) { error(errorText(err?.message)); cancelling = false; q("cancel").disabled = false; } }
  };
  q("download").onclick = async () => {
    if (!taskId || busy || downloading || operation !== "export" || !valid()) return;
    downloading = true; setBusy(busy); const token = generation;
    try { await bridge.download("backup/download", {task_id: taskId}, "companion-backup.cab"); }
    catch (err) { if (valid() && token === generation) error(errorText(err?.message)); }
    finally { downloading = false; if (valid() && token === generation) setBusy(busy); }
  };
  modal.showModal();
  void (async () => {
    try {
      const result = await bridge.apiGet("backup/options");
      if (!valid()) return;
      if (Number.isSafeInteger(result.limits?.max_upload_bytes) && result.limits.max_upload_bytes > 0) limits.max_upload_bytes = Math.min(limits.max_upload_bytes, result.limits.max_upload_bytes);
      if (Number.isSafeInteger(result.limits?.chunk_bytes) && result.limits.chunk_bytes > 0) limits.chunk_bytes = Math.min(limits.chunk_bytes, result.limits.chunk_bytes);
      for (const el of modal.querySelectorAll("[data-estimate]")) {
        const value = result.modules?.[el.dataset.estimate];
        el.textContent = value ? `${Number(value.count) || 0} 项 · 约 ${bytes(value.bytes)}` : "生成后显示实际数量";
      }
      q("dependencies").textContent = "按模块附带必要连接与身份结构；不包含相册成图、宿主聊天原图和向量缓存。";
      q("status").textContent = ""; optionsReady = true; setBusy(false);
    } catch (err) {
      if (valid()) { error(errorText(err?.message)); q("status").textContent = "数据概览读取失败，可校验已有备份；导出请关闭后重新打开。"; setBusy(false); }
    }
  })();
  return currentBackup;
}

// Restoration owns only the controls below the ordinary export/validation form.
// No password, original text or credentials are copied into this controller.
function mountRestore({modal, q, bridge, escape, settings, valid, error, busy,
  setBusy, generation, task, resetTask, acceptTask, poll, setOperation, stopPolling, resumePolling}) {
  let alive = true, version = 0, catalog = null, previewId = "", checkId = "", rollbackId = "";
  let checking = false, allowed = false, pending = [], archiveList = [], failed = false;
  const current = () => alive && valid();
  const list = value => Array.isArray(value) ? value.slice(0, 200) : [];
  const label = value => String(value?.name || value?.label || value?.id || value?.source_id || value?.source_cid || value?.target_id || value?.cid || "未命名对象").slice(0, 512);
  const reasonNames = {
    HOST_UNAVAILABLE: "宿主聊天暂不可读取", CHAT_UNAVAILABLE: "目标聊天不存在",
    IDENTITY_UNPROVEN: "原聊天身份尚未核实", CHAT_EVIDENCE_REQUIRED: "原聊天身份尚未核实",
    TARGET_NOT_EMPTY: "目标聊天已有对话，不能作为新聊天基线", CONNECTION_INCOMPLETE: "连接资料待补齐",
    KEY_REQUIRED: "连接密钥待补齐", PROVIDER_REQUIRED: "宿主模型连接待选择", PERSONA_REQUIRED: "宿主人设待选择",
    REFERENCE_REQUIRED: "参考图片待补齐", TARGET_CHANGED: "目标已变化，请重新预览",
    SOURCE_REFERENCES_UNPROVEN: "旧来源无法核实，资料仍保留在档案中", ARCHIVE_ONLY: "保留档案，尚未绑定",
    ALREADY_IMPORTED: "已导入，跳过重复写入", PENDING_ACTIVATION: "已恢复，待检查启用",
    SETTINGS_CHANGED: "设置已变化，请重新预览", FUTURE_CONFIRM_REQUIRED: "未来约定及调整待确认",
    TIMEZONE_INVALID: "时区待修正", EXPIRED: "已过期，不会补发", RETENTION_CONFIRM_REQUIRED: "保留策略待确认",
    RESTORE_BUSY: "业务任务尚未结束，请稍后重试", PREVIEW_EXPIRED: "预览已失效，请重新生成",
    MAPPING_INVALID: "目标映射不完整或冲突，请检查所选对象", CATALOG_LIMIT: "对象数量超过本次恢复上限",
    ARCHIVE_NOT_FOUND: "原档案不存在", ROLLBACK_CONFLICT: "恢复后已有新写入，不能直接回退",
    RESTORE_TARGET_CHANGED: "目标已有变化，请重新预览；原回退资料仍保留",
    RESTORE_HOST_CHANGED: "目标聊天在预览后发生变化，请重新核对",
    RESTORE_PREVIEW_EXPIRED: "恢复预览已过期，请重新生成", RESTORE_PREVIEW_STALE: "映射预览已失效，请重新生成",
    RESTORE_CHECK_EXPIRED: "启用检查已过期，请重新检查", RESTORE_NOT_PREPARED: "请重新准备导入文件",
    RESTORE_MAPPING_INVALID: "映射格式无效，请检查对象选择", RESTORE_SELECTION_INVALID: "恢复范围无效，请重新选择",
    RESTORE_CHAT_COLLISION: "不能将两份来源聊天合并到同一目标", RESTORE_REPLACE_REQUIRED: "目标已有内容，请明确选择替换",
    RESTORE_CONNECTION_MISMATCH: "复用连接的地址、模型或用途不匹配", RESTORE_CONNECTION_COLLISION: "连接映射发生冲突",
    RESTORE_CONNECTION_LIMIT: "恢复后的连接数量超过上限", RESTORE_CHARACTER_MISSING: "计划依赖的角色尚未映射",
    RESTORE_DUPLICATE_OBJECT: "同一对象被重复选择", RESTORE_CATALOG_LIMIT: "对象数量超过本次恢复上限",
    RESTORE_ID_LIMIT: "来源身份数量超过上限", RESTORE_TARGET_SCHEMA: "目标数据格式不受支持",
    RESTORE_DISK_LIMIT: "恢复所需的总磁盘空间超过上限", RESTORE_TARGET_LIMIT: "目标数据超出本次回退副本上限",
    RESTORE_MAINTENANCE: "正在进行数据维护，请稍后再试", RESTORE_PENDING_ACTIVATION: "对象尚待检查启用",
    RESTORE_STALE_OPERATION: "操作已过期，请重新打开", RESTORE_DEPENDENCY_MISSING: "启用条件仍未满足，请重新检查",
    RESTORE_ADJUSTMENT_MISSING: "未来约定的临时调整缺失，保持待启用", RESTORE_CONFIRM_REQUIRED: "请确认档案、未来约定及保留策略",
    RECOVERY_REQUIRED: "恢复尚未完成，已保持维护状态；请保留回退资料并检查处理结果",
    ROLLBACK_COPY_INVALID: "回退副本未通过校验，未覆盖当前数据", ARCHIVE_LIMIT: "持久档案容量已达上限",
    BINDING_REQUIRED: "接收会话或聊天尚待绑定", CREDENTIALS_REQUIRED: "所需连接密钥待补齐",
    CHAT_IDENTITY_UNPROVEN: "无法证明与原聊天相同，完整资料仍可留档",
    NEW_CHAT_NOT_EMPTY: "新聊天已有对话，不能建立继承基线", CHAT_NOT_EMPTY: "新聊天已有对话，不能建立继承基线",
    HOST_EVIDENCE_UNAVAILABLE: "宿主聊天证据暂不可读取", HOST_EVIDENCE_LIMIT: "宿主聊天证据超过有界核验范围",
    PERSONA_UNAVAILABLE: "宿主人设尚未绑定", PROVIDER_UNAVAILABLE: "宿主模型连接尚未绑定",
    REFERENCE_PENDING: "关联参考角色尚未启用", REFERENCE_UNAVAILABLE: "参考图片缺失或无效",
    CALENDAR_CHANGED: "目标日历已变化，请重新检查", AGREEMENT_EXPIRED: "一次约定已过期，不会补发",
    SOURCE_CHANGED: "原备份内容已变化，请重新准备导入", UPLOAD_INCOMPLETE: "备份文件尚未完整上传",
    DATA_SCHEMA_INVALID: "备份数据结构不受支持", DATA_RELATION_CHANGED: "备份内部引用不一致",
    DATA_INVENTORY_MISMATCH: "备份清单与内容不一致", IMAGE_HASH: "参考文件完整性校验失败",
    FILE_HASH: "档案文件完整性校验失败", RESTORE_CANCELLED: "恢复操作已取消",
    PRIVATE_PATH: "恢复临时区路径不安全",
    SESSION_BINDING_REQUIRED: "参考角色的会话尚待绑定", LIFE_BINDING_REQUIRED: "朝夕计划依赖尚待绑定",
    RESTORE_SCOPE_MISMATCH: "目标聊天与会话映射不一致，请调整映射",
    ADJUSTMENT_CONFLICT: "未来约定的调整发生冲突", ARCHIVE_METADATA_PENDING: "原档案已保存，映射记录尚待写入",
    RESTORE_ARCHIVE_ONLY: "仅保留原档案，尚未写入目标", RESTORE_ROLLBACK_UNAVAILABLE: "当前没有可回退的完整状态",
    SESSION_BINDING_CONFLICT: "目标会话已绑定其他角色，请调整映射", RESTORE_SNAPSHOT_CONFLICT: "目标恢复点身份冲突，未覆盖已有恢复点",
    RESTORE_TRIP_CONFLICT: "恢复后的行程身份冲突，未修改当前日历，请重新准备导入并调整所选范围",
  };
  const reason = item => {
    const raw = typeof item === "string" ? item : item?.code || item?.reason;
    const code = safeCode(raw);
    const table = typeof raw === "string" && raw.startsWith("SOURCE_TABLE_ABSENT:") ? raw.slice(20) : "";
    return reasonNames[code] || noteNames[code] || fixedMessages[code] || (table in countNames ? `源库尚无${countNames[table]}表` : "尚有条件待核对");
  };
  const fail = err => { failed = true; error(reasonNames[safeCode(err?.message)] || errorText(err?.message)); };
  const counts = value => Object.entries(value || {}).filter(([, n]) => Number.isSafeInteger(n) && n >= 0)
    .map(([key, n]) => `${countNames[key] || ({expired_turns: "到期原文", expired_snapshots: "到期恢复点", retention_days: "原文保留天数"})[key] || "项目"} ${n}`).join(" · ");
  const retention = value => Array.isArray(value) ? `<ul>${list(value).map(item => `<li>${escape(item.scope || "")} · ${escape(item.cid || "")}：${item.retention_days === 0 ? "原文长期保留" : `原文保留 ${Number.isSafeInteger(item.retention_days) ? item.retention_days : "待核对"} 天`}，到期原文 ${Number.isSafeInteger(item.expired_turns) ? item.expired_turns : "待核对"}，到期恢复点 ${Number.isSafeInteger(item.expired_snapshots) ? item.expired_snapshots : "待核对"}</li>`).join("")}</ul>` : `<p>${escape(counts(value)) || "未列出到期项目"}</p>`;
  const details = value => {
    const values = list(value);
    return values.length ? `<ul>${values.map(item => `<li>${escape(reason(item))}${Number.isSafeInteger(item?.count) ? `（${item.count}）` : ""}</li>`).join("")}</ul>` : "";
  };
  const summaryRows = values => list(values).map(item => {
    const action = {add: "新增", new: "新增", replace: "替换", keep: "保留目标", skip: "跳过", archive: "仅留档", pending: "待启用", restored: "已恢复，待启用"}[item.action || item.state] || "待核对";
    return `<li>${objectDescription(item, action)}</li>`;
  }).join("");
  const connectionLabel = item => {
    const name = label(item), type = typeof item?.type === "string" ? item.type.slice(0, 80) : "";
    const model = typeof item?.model === "string" ? item.model.slice(0, 120) : "";
    return [name, type, model].filter(Boolean).join(" · ");
  };
  const objectType = value => ({memory: "拾忆聊天", character: "参考角色", life_plan: "朝夕计划", settings: "设置", calendar: "日历"}[value] || "恢复对象");
  const objectDescription = (item, action = "", heading = true) => {
    const errors = list(item.errors);
    const target = item.target_summary || item.target_name || [item.target_scope || item.scope, item.target_cid || item.cid || item.target_id].filter(Boolean).join(" · ");
    const origin = [item.source_scope, item.source_cid || item.source_id].filter(Boolean).join(" · ");
    const state = item.can_enable === true ? "可启用" : item.can_enable === false ? "待处理" : (item.state === "enabled" ? "已启用" : "待检查");
    return `${heading ? `<strong>${escape(label(item))}</strong> · ${escape(objectType(item.kind))} · ` : ""}${escape(action || state)}${origin ? `<p class="help">来源：${escape(origin.slice(0, 512))}</p>` : ""}${target ? `<p class="help">目标：${escape(String(target).slice(0, 512))}</p>` : ""}${item.reason ? `<p class="help">${escape(reason(item.reason))}</p>` : ""}${errors.length ? `<ul>${errors.map(code => `<li>${escape(reason(code))}</li>`).join("")}</ul>` : ""}${item.calendar_scope ? calendarScope(item.calendar_scope) : ""}`;
  };
  const pendingRows = values => list(values).map(item => `<li>${objectDescription(item)}</li>`).join("");
  const calendarScope = value => {
    const count = n => Number.isSafeInteger(n) && n >= 0 ? n : "待核对";
    const global = value?.global || {};
    return `<details><summary>日历与行程范围</summary><p>公共行程：${global.action === "replace" ? "替换" : "保留目标"}（来源 ${count(global.source_trips)} 项，目标 ${count(global.target_trips)} 项）</p>${list(value?.mapped).length ? `<ul>${list(value.mapped).map(item => `<li>${escape(String(item.source_scope || "").slice(0, 256))} → ${escape(String(item.target_scope || "").slice(0, 256))}：来源 ${count(item.source_trips)} 项，目标 ${count(item.target_trips)} 项</li>`).join("")}</ul>` : ""}<p>保留目标未映射行程 ${count(value?.preserved_target_trips)} 项；未选来源行程 ${count(value?.unselected_source_trips)} 项。</p></details>`;
  };
  const sync = () => {
    const locked = busy();
    for (const id of ["archives-load", "pending-load", "rollback-preview"]) q(id).disabled = locked;
    for (const button of modal.querySelectorAll("[data-restore-action]")) button.disabled = locked;
    if (q("restore-commit")) q("restore-commit").disabled = locked || !previewId || !q("restore-keep").checked;
    if (q("archive-save")) q("archive-save").disabled = locked || !q("restore-keep").checked;
    if (q("enable")) q("enable").disabled = locked || !checkId || !allowed ||
      !q("enable-future").checked || !q("enable-old").checked || !q("enable-retention").checked;
    if (q("rollback-confirm")) q("rollback-confirm").disabled = locked || !rollbackId;
    if (checking) q("cancel").hidden = true;
  };
  const invalidate = () => {
    version++; previewId = ""; q("restore-preview")?.replaceChildren();
    if (q("restore-confirm")) q("restore-confirm").hidden = true;
    sync();
  };
  const clear = () => {
    version++; catalog = null; previewId = ""; failed = false;
    q("restore").replaceChildren(); q("restore").hidden = true;
  };
  const run = async work => {
    if (!current() || busy()) return;
    const token = version, session = generation();
    const validReply = () => current() && token === version && session === generation();
    let transferred = false;
    checking = true; failed = false; stopPolling(); setBusy(true); error("");
    try { transferred = await work(validReply) === "task"; }
    catch (err) { if (validReply()) fail(err); }
    finally {
      checking = false;
      if (validReply() && !transferred) { setBusy(false); resumePolling(); }
    }
  };
  const options = (rows, selected = "") => `<option value="">新增对象</option>` + list(rows).map(item => `<option value="${escape(item.id)}"${item.id === selected ? " selected" : ""}>替换：${escape(label(item))}</option>`).join("");
  const readSelection = () => {
    const modules = [...q("restore").querySelectorAll("[data-restore-module]")].filter(el => el.checked).map(el => el.dataset.restoreModule);
    const selection = {modules, characters: [], plans: [], chats: [], scopes: {}, settings: "keep", connections: {}, character_links: {}, calendar: false, life_chats: []};
    for (const kind of ["characters", "plans"]) list(catalog[kind]).forEach((item, index) => {
      if (kind === "characters" && (item.dependency_only || (!modules.includes("reference") && modules.includes("life")))) {
        const target = q(`${kind}-${index}-target`).value;
        if (target && modules.includes("life")) selection.character_links[item.id] = target;
        return;
      }
      if (!q(`${kind}-${index}-use`).checked || !modules.includes(kind === "characters" ? "reference" : "life")) return;
      const targetId = q(`${kind}-${index}-target`).value;
      selection[kind].push({source_id: item.id, target_id: targetId, replace: !!targetId});
    });
    list(catalog.chats).forEach((item, index) => {
      const mode = q(`chat-${index}-mode`).value;
      if (!modules.includes("memory")) return;
      selection.chats.push({source_scope: item.scope, source_cid: item.cid, mode,
        target_scope: q(`chat-${index}-scope`).value.trim(), target_cid: q(`chat-${index}-cid`).value.trim(),
        replace: q(`chat-${index}-replace`).checked});
    });
    if (modules.includes("life")) {
      selection.calendar = q("restore-calendar")?.checked === true;
      list(catalog.life_chats).forEach((item, index) => {
        const scope = q(`life-chat-${index}-scope`).value.trim(), cid = q(`life-chat-${index}-cid`).value.trim();
        if (scope || cid) selection.life_chats.push({source_scope: item.scope, source_cid: item.cid, target_scope: scope, target_cid: cid});
      });
    }
    for (const field of q("restore").querySelectorAll("[data-restore-scope]")) {
      if (field.value.trim()) selection.scopes[field.dataset.restoreScope] = field.value.trim();
    }
    if (modules.includes("settings")) selection.settings = q("restore-settings")?.value || "keep";
    for (const field of q("restore").querySelectorAll("[data-restore-connection]")) {
      if (!field.value) continue;
      const kind = field.dataset.restoreConnection, source = field.dataset.sourceId;
      (selection.connections[kind] ||= {})[source] = field.value;
    }
    return selection;
  };
  const dirtyChoice = async (next, choice) => {
    if (!choice && settings?.dirty()) {
      q("restore-unsaved").hidden = false;
      q("restore-save-first").onclick = () => dirtyChoice(next, "save");
      q("restore-use-saved").onclick = () => dirtyChoice(next, "saved");
      q("restore-dismiss").onclick = () => { q("restore-unsaved").hidden = true; };
      q("restore-save-first").focus(); return;
    }
    if (q("restore-unsaved")) q("restore-unsaved").hidden = true;
    if (choice === "save") {
      await run(async validReply => {
        await settings.saveAll();
        if (!validReply()) return;
        if (settings.dirty() || settings.saving) { error("设置尚未保存成功，恢复未开始。"); return; }
        setBusy(false); invalidate(); await next();
      });
    } else await next();
  };
  const preview = () => dirtyChoice(() => run(async validReply => {
    const selection = readSelection();
    if (!selection.modules.length) { error("请选择至少一个模块。"); return; }
    const result = await bridge.apiPost("backup/restore/preview", {task_id: task(), selection});
    if (!validReply()) return;
    previewId = typeof result.preview_id === "string" ? result.preview_id : "";
    const data = result.preview || result;
    q("restore-preview").innerHTML = `<h3>恢复预览</h3>${data.already_imported ? "<p>相同档案和映射已处理；本次保留现状，不重复写入。</p>" : ""}<ul class="restore-results">${summaryRows(data.objects)}</ul>
      ${counts(data.counts) ? `<p>${escape(counts(data.counts))}</p>` : ""}
      ${list(data.credential_reuse).length ? `<ul>${list(data.credential_reuse).map(item => `<li>复用目标密钥槽位 ${Number.isSafeInteger(item.target_slots) ? item.target_slots : "待核对"} 项（源 ${Number.isSafeInteger(item.source_slots) ? item.source_slots : "待核对"} 项）与当前选择</li>`).join("")}</ul>` : ""}
      ${data.retention ? `<details><summary>保留策略与到期数量</summary>${retention(data.retention)}</details>` : ""}
      ${data.calendar_scope ? calendarScope(data.calendar_scope) : ""}
      ${details(data.limitations)}${details(data.errors)}`;
    q("restore-confirm").hidden = !previewId;
    q("restore-keep").checked = false;
  }));
  const commit = () => dirtyChoice(() => run(async validReply => {
    if (!previewId) { error("配置已保存，请重新生成恢复预览后确认。"); return; }
    if (!q("restore-keep").checked) return;
    const id = previewId; previewId = ""; stopPolling();
    setOperation("restore");
    let result;
    try { result = await bridge.apiPost("backup/restore/commit", {task_id: task(), preview_id: id, keep_archive: true}); }
    catch (err) {
      if (!validReply()) return;
      if (["TARGET_CHANGED", "PREVIEW_EXPIRED", "MAPPING_INVALID", "RESTORE_BUSY", "BACKUP_BUSY", "DISK_FULL", "DISK_SPACE",
        "RESTORE_TARGET_CHANGED", "RESTORE_HOST_CHANGED", "RESTORE_PREVIEW_EXPIRED", "RESTORE_PREVIEW_STALE",
        "RESTORE_NOT_PREPARED", "RESTORE_MAPPING_INVALID", "RESTORE_SELECTION_INVALID", "RESTORE_CONFIRM_REQUIRED",
        "RESTORE_DISK_LIMIT", "RESTORE_TARGET_LIMIT", "RESTORE_MAINTENANCE"].includes(safeCode(err?.message))) {
        setOperation("import", true); invalidate(); setBusy(false); fail(err); resumePolling(); return;
      }
      // The request may have committed before the connection failed. Only the
      // persisted task result may release the write gate or report completion.
      checking = false; setBusy(true); await poll(generation()); return "task";
    }
    if (!validReply()) return;
    q("restore-confirm").hidden = true;
    q("status").textContent = "正在恢复；关闭窗口不会撤回已开始的提交。";
    checking = false;
    if (!result.task_id || result.task_id === task()) await poll(generation());
    else if (await acceptTask(result, generation())) await poll(generation());
    return "task";
  }));
  const showCatalog = (data, summary) => {
    catalog = data; previewId = ""; version++;
    const targets = data.targets || {};
    const choices = kind => list(data[kind]).map((item, index) => `<div class="restore-object">${item.dependency_only ? `<strong>${escape(label(item))}</strong>` : `<label class="check"><input type="checkbox" id="backup-${kind}-${index}-use">${escape(label(item))}</label>`}${kind === "characters" ? `<p id="backup-character-${index}-link-note" class="help"${item.dependency_only ? "" : " hidden"}>朝夕依赖角色，复用目标已有角色；不恢复或替换参考数据。</p>` : ""}<label id="backup-${kind}-${index}-target-label" for="backup-${kind}-${index}-target">${item.dependency_only ? "复用目标角色" : "恢复方式"}</label><select id="backup-${kind}-${index}-target">${item.dependency_only ? `<option value="">待绑定</option>${list(targets.characters).map(target => `<option value="${escape(target.id)}">复用：${escape(label(target))}</option>`).join("")}` : options(targets[kind])}</select></div>`).join("");
    const scopes = [...new Set([...list(data.scopes), ...list(data.plans).map(item => item.scope), ...list(data.characters).flatMap(item => item.sessions || [])].filter(value => typeof value === "string" && value))];
    const chatOptions = `<option value="">手动填写目标身份</option>` + list(targets.chats).map((item, index) => `<option value="${index}">${escape(label(item))} · ${escape(item.scope)}</option>`).join("");
    const connections = Object.entries(data.connections || {}).filter(([kind]) => ["presets", "providers", "search_schemes"].includes(kind)).map(([kind, rows]) => {
      const names = {presets: "拾忆连接", providers: "图片站点", search_schemes: "日历搜索连接"};
      return list(rows).map(item => `<label>${names[kind]}：${escape(connectionLabel(item))}<select data-restore-connection="${kind}" data-source-id="${escape(item.id)}"><option value="">使用档案中的连接（缺密钥时待补齐）</option>${list(targets.connections?.[kind]).map(target => `<option value="${escape(target.id)}">复用：${escape(connectionLabel(target))}</option>`).join("")}</select></label>`).join("");
    }).join("");
    q("restore").innerHTML = `<h3>内容与目标映射</h3><div class="restore-module-list">${list(data.modules || summary.modules).filter(id => id in moduleNames).map(id => `<label class="check"><input type="checkbox" data-restore-module="${id}" checked>${moduleNames[id]}</label>`).join("")}</div>
      ${list(data.characters).length ? `<details open><summary>参考角色（${data.characters.length}）</summary>${choices("characters")}</details>` : ""}
      ${list(data.plans).length ? `<details><summary>朝夕计划（${data.plans.length}）</summary>${choices("plans")}</details>` : ""}
      ${scopes.length ? `<details><summary>接收会话映射</summary>${scopes.map(scope => `<label>${escape(scope)}<input data-restore-scope="${escape(scope)}" maxlength="512" placeholder="目标 scope；留空则待绑定"></label>`).join("")}</details>` : ""}
      ${list(data.life_chats).length ? `<details><summary>未来约定与调整的聊天映射（${data.life_chats.length}）</summary>${list(data.life_chats).map((item, index) => `<div class="restore-object"><p>${escape(item.scope)} · ${escape(item.cid)}</p><div class="form-row"><label>目标 scope<input id="backup-life-chat-${index}-scope" maxlength="512"></label><label>目标聊天 ID<input id="backup-life-chat-${index}-cid" maxlength="512"></label></div></div>`).join("")}</details>` : ""}
      ${list(data.chats).length ? `<details open><summary>拾忆聊天（${data.chats.length}）</summary>${list(data.chats).map((item, index) => `<div class="restore-object"><strong>${escape(label(item))}</strong><p class="help">${escape(item.scope)} · ${escape(item.cid)}</p><label for="backup-chat-${index}-mode">恢复路径</label><select id="backup-chat-${index}-mode"><option value="archive">仅保留档案，稍后绑定</option><option value="original">原聊天也迁移了</option><option value="new">从新聊天继续</option></select><label for="backup-chat-${index}-target">目标聊天</label><select id="backup-chat-${index}-target">${chatOptions}</select><div class="form-row"><label>目标 scope<input id="backup-chat-${index}-scope" maxlength="512"></label><label>目标聊天 ID<input id="backup-chat-${index}-cid" maxlength="512"></label></div><label class="check"><input type="checkbox" id="backup-chat-${index}-replace">替换目标已有插件记忆（不清空宿主聊天）</label></div>`).join("")}</details>` : ""}
      ${data.calendar ? `<label class="check"><input type="checkbox" id="backup-restore-calendar">恢复日历与行程（将在预览中列出替换范围）</label>` : ""}
      ${(data.modules || summary.modules || []).includes("settings") ? `<label for="backup-restore-settings">插件全局设置</label><select id="backup-restore-settings"><option value="keep">保留目标设置</option><option value="replace">替换为档案设置</option></select>` : ""}
      ${connections ? `<details><summary>连接依赖与显式复用</summary>${connections}</details>` : ""}
      <p class="help">未选择的对象不写入活动数据。新聊天只承接语义记忆；旧原文、批次及恢复点留在档案。</p>
      <div id="backup-restore-unsaved" class="backup-unsaved" hidden><p>设置有未保存草稿。恢复基于已保存配置，草稿不会被自动提交或丢弃。</p><div class="backup-actions"><button type="button" id="backup-restore-save-first" data-restore-action>先保存设置</button><button type="button" id="backup-restore-use-saved" data-restore-action>使用已保存配置</button><button type="button" id="backup-restore-dismiss" data-restore-action>取消</button></div></div>
      <button type="button" id="backup-restore-preview-button" data-restore-action>生成恢复预览</button><div id="backup-restore-preview"></div>
      <label class="check"><input type="checkbox" id="backup-restore-keep">保留这份完整原档案（包含未选模块），供以后绑定与重新下载</label>
      <div class="backup-actions"><button type="button" id="backup-archive-save" data-restore-action>仅保留原档案</button><span id="backup-restore-confirm" hidden><button type="button" id="backup-restore-commit" class="primary" data-restore-action>确认恢复所选内容</button></span></div>`;
    q("restore").hidden = false;
    const updateCharacterModes = () => {
      const modules = [...q("restore").querySelectorAll("[data-restore-module]")].filter(el => el.checked).map(el => el.dataset.restoreModule);
      list(data.characters).forEach((item, index) => {
        const reuse = item.dependency_only || (!modules.includes("reference") && modules.includes("life"));
        if (q(`characters-${index}-use`)) q(`characters-${index}-use`).hidden = reuse;
        q(`character-${index}-link-note`).hidden = !reuse;
        q(`characters-${index}-target-label`).textContent = reuse ? "复用目标角色" : "恢复方式";
        const choices = q(`characters-${index}-target`).querySelectorAll("option");
        choices[0].textContent = reuse ? "待绑定" : "新增对象";
        list(targets.characters).forEach((target, optionIndex) => {
          if (choices[optionIndex + 1]) choices[optionIndex + 1].textContent = `${reuse ? "复用" : "替换"}：${label(target)}`;
        });
      });
    };
    updateCharacterModes();
    for (const el of q("restore").querySelectorAll("input, select")) {
      const changed = () => { if (!current() || busy()) return; if (el.dataset.restoreModule) updateCharacterModes(); if (el !== q("restore-keep")) invalidate(); else sync(); };
      el.addEventListener("input", changed); el.addEventListener("change", changed);
    }
    list(data.chats).forEach((item, index) => {
      q(`chat-${index}-target`).onchange = () => {
        const chosen = q(`chat-${index}-target`).value;
        const target = chosen === "" ? null : list(targets.chats)[Number(chosen)];
        if (target) { q(`chat-${index}-scope`).value = target.scope; q(`chat-${index}-cid`).value = target.cid; }
        invalidate();
      };
    });
    q("restore-preview-button").onclick = preview;
    q("restore-commit").onclick = commit;
    q("archive-save").onclick = () => run(async validReply => {
      if (!q("restore-keep").checked) return;
      await bridge.apiPost("backup/archive/save", {task_id: task(), confirm: true});
      if (!validReply()) return;
      q("status").textContent = "原档案已保留，尚未恢复或启用对象。";
      setBusy(false); await loadArchives();
    });
    sync();
  };
  const loadArchivesInto = async validReply => {
    const result = await bridge.apiGet("backup/archives");
    if (!validReply()) return;
    const password = q("archive-password")?.value || "";
    const passwordFocused = document.activeElement === q("archive-password");
    const folds = [...q("archives").querySelectorAll("details")].map((el, index) => [archiveList[index]?.id, el.open]);
    archiveList = list(result.archives);
    q("archives").innerHTML = `<h3>原档案（${archiveList.length}）</h3>${archiveList.map((item, index) => {
      const imported = ["imported", "restored"].includes(item.state);
      const state = imported ? "已有对象恢复；未绑定资料仍留档，启用状态另行检查。" : reason(item.reason || "ARCHIVE_ONLY");
      const source = item.source || {}, bindings = list(item.bindings);
      const text = value => typeof value === "string" ? escape(value.slice(0, 512)) : "未记录";
      const mappingRows = bindings.map(binding => {
        const kind = {character: "参考角色", life_plan: "朝夕计划", memory: "拾忆聊天", settings: "设置", calendar: "日历"}[binding.kind] || "对象";
        const origin = [binding.source_scope, binding.source_cid || binding.source_id].filter(value => typeof value === "string" && value).map(text).join(" · ") || "来源未记录";
        const target = typeof binding.target_summary === "string" && binding.target_summary ? text(binding.target_summary) : [binding.target_scope, binding.target_cid || binding.target_id].filter(value => typeof value === "string" && value).map(text).join(" · ");
        const action = {add: "新增", new: "新增", replace: "替换", keep: "保留目标", skip: "跳过", archive: "仅留档", pending: "待启用"}[binding.action] || "待核对";
        return `<li>${kind}：${binding.name ? `${text(binding.name)}<br>来源：` : ""}${origin}${target ? ` → ${target}` : ""} · ${action}${binding.reason ? `<br>${escape(reason(binding.reason))}` : ""}</li>`;
      }).join("");
      return `<div class="restore-object"><strong>${escape(item.created_utc || label(item))}</strong><p>${escape(list(item.modules).map(id => moduleNames[id]).filter(Boolean).join("、"))} · ${escape(counts(item.counts))}</p>${details(item.limitations)}<p class="help">${escape(state)}</p><details><summary>来源与最近映射</summary><p>来源命名空间：${text(source.namespace)}<br>原档案身份：${text(item.package_id)}${typeof source.captured_at === "string" ? `<br>源快照时间：${text(source.captured_at)}` : ""}${Number.isSafeInteger(source.settings_revision) ? `<br>源设置版本：${source.settings_revision}` : ""}</p>${bindings.length ? `<ul>${mappingRows}</ul>` : "<p>尚未记录映射。</p>"}</details><div class="backup-actions"><button type="button" data-archive-download="${index}" data-restore-action>下载原档案</button><button type="button" data-archive-open="${index}" data-restore-action>重新打开并绑定</button></div></div>`;
    }).join("")}<label for="backup-archive-password">打开加密档案的密码</label><input type="password" id="backup-archive-password" autocomplete="off" maxlength="1024">`;
    q("archive-password").value = password;
    if (passwordFocused) q("archive-password").focus();
    [...q("archives").querySelectorAll("details")].forEach((el, index) => { el.open = folds.find(([id]) => id === archiveList[index]?.id)?.[1] || false; });
    for (const button of q("archives").querySelectorAll("[data-archive-download]")) button.onclick = () => run(async reply => {
      await bridge.download("backup/archive/download", {id: archiveList[Number(button.dataset.archiveDownload)].id}, "companion-archive.cab");
      if (reply()) q("status").textContent = "已下载原档案，身份保持不变。";
    });
    for (const button of q("archives").querySelectorAll("[data-archive-open]")) button.onclick = () => openArchive(archiveList[Number(button.dataset.archiveOpen)].id);
    setBusy(busy());
  };
  const loadArchives = () => run(loadArchivesInto);
  const openArchive = async archiveId => {
    if (!current() || busy()) return;
    setBusy(true); error("");
    try {
      await resetTask();
      if (!current()) return;
      const token = generation();
      setOperation("import");
      const result = await bridge.apiPost("backup/archive/open", {archive_id: archiveId, password: q("archive-password").value});
      if (await acceptTask(result, token)) await poll(token);
    } catch (err) { if (current()) { setBusy(false); fail(err); } }
  };
  const loadPendingInto = async validReply => {
    const result = await bridge.apiGet("backup/restore/pending");
    if (!validReply()) return;
    pending = list(result.objects); checkId = ""; allowed = false;
    q("pending").innerHTML = `<h3>待启用对象（${pending.length}）</h3>${pending.map((item, index) => `<div class="restore-object"><label class="check"><input type="checkbox" data-pending="${index}">${escape(label(item))} · ${escape(objectType(item.kind))}</label><div>${objectDescription({...item, name: "", label: ""}, "", false)}</div></div>`).join("")}<button type="button" id="backup-check" data-restore-action>检查所选对象</button><div id="backup-enable-checks"></div><div id="backup-enable-confirm" hidden><label class="check"><input type="checkbox" id="backup-enable-future">确认所选未来约定及取消、延后调整仍有效</label><label class="check"><input type="checkbox" id="backup-enable-old">确认旧实例已停止自动联系</label><label class="check"><input type="checkbox" id="backup-enable-retention">确认以上保留策略及已到期数量</label><button type="button" id="backup-enable" class="primary" data-restore-action>启用检查通过的对象</button></div>`;
    const selectionChanged = () => { if (busy()) return; checkId = ""; allowed = false; version++; q("enable-confirm").hidden = true; q("enable-checks").replaceChildren(); sync(); };
    for (const field of q("pending").querySelectorAll("[data-pending]")) field.onchange = selectionChanged;
    for (const id of ["enable-future", "enable-old", "enable-retention"]) q(id).onchange = sync;
    q("check").onclick = () => run(async reply => {
      const ids = [...q("pending").querySelectorAll("[data-pending]")].filter(el => el.checked).map(el => pending[Number(el.dataset.pending)].pending_id || pending[Number(el.dataset.pending)].id);
      if (!ids.length) { error("请选择需要检查的对象。"); return; }
      const checked = await bridge.apiPost("backup/restore/check", {ids});
      if (!reply()) return;
      checkId = checked.check_id || ""; allowed = checked.can_enable === true;
      q("enable-checks").innerHTML = `<ul class="restore-results">${pendingRows(checked.objects)}</ul>${details(checked.errors)}<details open><summary>保留策略与到期数量</summary>${retention(checked.retention)}</details>`;
      q("enable-confirm").hidden = !checkId;
      for (const id of ["enable-future", "enable-old", "enable-retention"]) q(id).checked = false;
    });
    q("enable").onclick = () => run(async reply => {
      if (!checkId || !allowed || !q("enable-future").checked || !q("enable-old").checked || !q("enable-retention").checked) return;
      const result = await bridge.apiPost("backup/restore/enable", {check_id: checkId, confirm_future: true, old_instance_stopped: true, retention_confirmed: true});
      if (!reply()) return;
      checkId = ""; q("enable-confirm").hidden = true;
      q("result").hidden = true;
      await loadPendingInto(reply);
      if (!reply()) return;
      q("enable-checks").innerHTML = `<p>所选对象已启用；未自动补发旧计划或补齐历史图片、向量。</p>${details(result.errors)}`;
      if (archiveList.length) await loadArchivesInto(reply);
    });
    setBusy(busy());
  };
  const loadPending = () => run(loadPendingInto);
  q("archives-load").onclick = loadArchives;
  q("pending-load").onclick = loadPending;
  q("rollback-preview").onclick = () => run(async reply => {
    const result = await bridge.apiPost("backup/restore/rollback-preview", {});
    if (!reply()) return;
    rollbackId = result.available === false ? "" : result.preview_id || "";
    q("rollback").innerHTML = `<h3>最近一次恢复前的状态</h3><p>${rollbackId ? "将撤回最近一次恢复。确认时会再次检查此后是否有新写入。" : "当前不能直接回退；原回退资料仍保留。"}</p>${details(result.errors)}${rollbackId ? `<button type="button" id="backup-rollback-confirm" data-restore-action>确认回退</button>` : ""}`;
    if (q("rollback-confirm")) q("rollback-confirm").onclick = () => run(async validReply => {
      if (!rollbackId) return;
      const result = await bridge.apiPost("backup/restore/rollback", {preview_id: rollbackId});
      if (!validReply()) return;
      rollbackId = "";
      if (result.task_id) { setOperation("restore"); if (await acceptTask(result, generation())) await poll(generation()); }
      else {
        q("rollback").innerHTML = "<p>已回退到恢复前状态。</p>";
        q("result").hidden = true;
        await loadPendingInto(validReply);
        if (validReply() && archiveList.length) await loadArchivesInto(validReply);
      }
    });
  });
  return {sync, clear, catalog: showCatalog, waiting: () => checking, preserveError: () => failed,
    completed(summary) {
      clear(); q("result").hidden = false;
      const result = summary.restore || summary;
      const unchanged = result.state === "unchanged", archived = result.state === "archived";
      const title = unchanged ? "相同恢复已处理" : archived ? "原档案已保留，尚未绑定" : "恢复处理已结束";
      const message = unchanged ? "本次未重复写入，现有数据保持不变。仍待启用的对象需检查后启用。" : archived ? "本次只保存原档案，未写入业务数据。可稍后重新打开档案并绑定目标。" : "已写入的对象仍需检查后启用；未绑定资料继续保留在原档案。";
      q("result").innerHTML = `<h3>${title}</h3><p>${message}</p>${!archived && Number.isSafeInteger(result.applied) ? `<p>本次写入 ${result.applied} 项 · 待启用 ${list(result.pending_ids).length} 项</p>` : ""}<ul>${summaryRows(result.objects || summary.objects || summary.pending)}</ul>${details(summary.limitations)}`;
      q("archives-section").open = true;
    },
    dispose() { alive = false; version++; catalog = null; archiveList = []; pending = []; previewId = checkId = rollbackId = ""; },
  };
}
