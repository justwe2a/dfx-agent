"use strict";
if (location.protocol === "file:") {
  // A source HTML file is not a running workbench. Do not attempt file:// APIs.
  const guide = document.createElement("main");
  guide.style.cssText = "max-width:680px;margin:12vh auto;padding:36px;background:#fff;border:1px solid #d9e0d8;border-radius:20px;line-height:1.9";
  guide.innerHTML = '<p>DFX Agent · 启动工作台</p><h1>请从启动程序进入</h1>' +
    '<p>当前打开的是网页源码文件，尚未连接本地服务。</p>' +
    '<ol><li>首次使用：在 PowerShell 运行 <strong>scripts/start-workbench.ps1 -Install</strong>。</li>' +
    '<li>之后运行 <strong>scripts/start-workbench.ps1</strong>，浏览器会自动打开工作台。</li>' +
    '<li>保持启动窗口运行。不要直接打开 src/dfx_agent/web/index.html。</li></ol>' +
    '<p>服务已启动时，打开 <a href="http://127.0.0.1:8765/">默认工作台地址</a>；自定义端口以启动窗口显示为准。</p>';
  document.body.replaceChildren(guide);
} else {
const $ = (id) => document.getElementById(id);
const fields = [
  "repo",
  "mode",
  "responses",
  "scope",
  "logs",
  "host",
  "max_steps",
  "max_output_tokens",
  "command_timeout",
  "script_timeout",
  "build_timeout",
  "model_timeout",
  "allow_device",
  "allow_dependency_install",
  "allow_system_navigation",
  "allow_screen_unlock",
];
let current = null,
  cursor = 0,
  detail = null,
  config = null,
  busy = false,
  pollTimer = null,
  selectedTab = "activity",
  diffLoaded = false,
  polling = false,
  preparing = false,
  launchData = null;
const labels = {
  Running: "执行中",
  Stopping: "正在停止",
  Submitted: "已提交声明（非验收通过）",
  LimitsExceeded: "额度用尽",
  BLOCKED: "阻塞",
  Interrupted: "已中断",
  Paused: "已暂停",
  Error: "错误",
  LaunchError: "启动失败",
  ExitedWithoutResult: "进程退出，结果缺失",
  Unobserved: "进程状态待核对",
  NOT_RUN: "未运行",
  PASS: "通过",
  FAIL: "失败",
};
const running = (s) => ["Running", "Stopping", "Unobserved"].includes(s);
function node(tag, text, className) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (className) e.className = className;
  return e;
}
function error(e) {
  const message = e.message || String(e);
  $("error").textContent = message;
  $("error").hidden = false;
  const dialog = document.querySelector("dialog[open]");
  if (dialog) {
    let notice = dialog.querySelector(".dialog-error");
    if (!notice) {
      notice = node("p", undefined, "dialog-error");
      dialog.prepend(notice);
    }
    notice.textContent = message;
  }
}
function clearError() {
  $("error").hidden = true;
  document.querySelectorAll(".dialog-error").forEach((e) => e.remove());
}
function toast(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  setTimeout(() => ($("toast").hidden = true), 5000);
}
async function api(path, data) {
  const options =
    data === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        };
  const response = await fetch("/api" + path, options);
  const value = await response.json();
  if (!response.ok)
    throw new Error(
      typeof value.detail === "string"
        ? value.detail
        : JSON.stringify(value.detail || value),
    );
  return value;
}
function readForm() {
  const data = { task: $("task").value };
  for (const key of fields) {
    const e = $(key);
    data[key] =
      e.type === "checkbox"
        ? e.checked
        : e.type === "number"
          ? Number(e.value)
          : e.value.trim();
  }
  return data;
}
function fillForm(values) {
  for (const key of fields) {
    if (values[key] === undefined) continue;
    const e = $(key);
    if (e.type === "checkbox") e.checked = values[key];
    else e.value = values[key];
  }
  if (values.task !== undefined) $("task").value = values.task;
  updateForm();
}
function updateForm() {
  $("project-open").textContent =
    $("repo").value.split(/[\\/]/).pop() || "▱ 选择本地项目";
  $("project-open").title = $("repo").value;
  $("fixture-fields").hidden = $("mode").value !== "fixture";
  $("input-mode").textContent =
    $("mode").value === "fixture"
      ? "零 API 夹具模式：固定动作，不是模型自主修复。"
      : "DeepSeek Flash · 原生工具调用 · 思考开启 / high";
  $("model-open").textContent =
    $("mode").value === "fixture"
      ? "固定夹具 · 原生工具模式"
      : "DeepSeek Flash · 思考开启 / high";
}
function saveForm() {
  localStorage.setItem("dfx-task-settings", JSON.stringify(readForm()));
  updateForm();
}
function showSettings() {
  $("settings").showModal();
}
["project-open", "model-open", "settings-open"].forEach(
  (id) => ($(id).onclick = showSettings),
);
document.querySelectorAll("[data-close]").forEach(
  (b) =>
    (b.onclick = () => {
      if (b.dataset.close === "settings") saveForm();
      $(b.dataset.close).close();
    }),
);
$("repo").onchange = () => {
  for (const key of ["scope", "host", "logs"]) $(key).value = "";
  for (const key of [
    "allow_device",
    "allow_dependency_install",
    "allow_system_navigation",
    "allow_screen_unlock",
  ])
    $(key).checked = false;
};
$("mode").onchange = updateForm;
$("task-file").onchange = async (e) => {
  try {
    $("task").value = (await e.target.files[0].text()).replace(/^\uFEFF/, "");
  } catch (exc) {
    error(exc);
  }
};
function newTask() {
  $("assignments-view").hidden = true;
  current = null;
  detail = null;
  location.hash = "";
  clearTimeout(pollTimer);
  $("new-view").hidden = false;
  $("run-view").hidden = true;
  $("page-title").textContent = "新建任务";
  clearError();
}
$("new-task").onclick = newTask;
$("demo").onclick = async () => {
  if (preparing || busy) return;
  preparing = true;
  $("demo").disabled = true;
  $("start").disabled = true;
  try {
    fillForm({
      ...(await api("/demo", {})),
      scope: "",
      host: "",
      logs: "",
      max_steps: 40,
      allow_device: false,
      allow_dependency_install: false,
      allow_system_navigation: false,
      allow_screen_unlock: false,
    });
    saveForm();
    toast("已创建合成工程和固定夹具。点击开始后才执行本地工具。");
  } catch (e) {
    error(e);
  } finally {
    preparing = false;
    $("demo").disabled = false;
    $("start").disabled = false;
  }
};
$("scope-preview").onclick = async () => {
  try {
    const value = await api("/scope", { path: $("scope").value });
    openText("资料范围", JSON.stringify(value, null, 2));
  } catch (e) {
    error(e);
  }
};
$("start").onclick = () => {
  if (preparing || busy) return;
  clearError();
  const data = readForm();
  if (!data.task.trim() || !data.repo) {
    error(new Error("请先填写任务并选择本机项目。"));
    return;
  }
  launchData = data;
  $("launch-review").textContent = JSON.stringify(
    {
      ...data,
      model: data.mode === "fixture" ? "固定夹具" : "deepseek-flash",
      thinking: "enabled",
      reasoning_effort: "high",
    },
    null,
    2,
  );
  $("confirm").showModal();
};
$("confirm-start").onclick = async () => {
  if (busy) return;
  busy = true;
  $("confirm-start").disabled = true;
  try {
    saveForm();
    const run = await api("/runs", launchData);
    $("confirm").close();
    await openRun(run.id);
  } catch (e) {
    $("confirm").close();
    error(e);
    await refreshHistory();
  } finally {
    busy = false;
    $("confirm-start").disabled = false;
  }
};
async function refreshHistory() {
  const rows = await api("/runs");
  $("history").replaceChildren();
  for (const r of rows) {
    const b = node(
      "button",
      undefined,
      "history" + (r.id === current ? " active" : ""),
    );
    b.append(node("span", "", running(r.status) ? "dot" : "dot gray"));
    const text = node("div");
    text.append(
      node("p", r.title),
      node("span", labels[r.status] || r.status, "small"),
    );
    b.append(text);
    b.onclick = () => openRun(r.id).catch(error);
    $("history").append(b);
  }
  if (!rows.length) $("history").append(node("p", "暂无任务记录", "small"));
}
async function openRun(id) {
  $("assignments-view").hidden = true;
  clearTimeout(pollTimer);
  current = id;
  location.hash = id;
  cursor = 0;
  diffLoaded = false;
  detail = null;
  $("evaluation-path").value = "";
  $("events").replaceChildren();
  $("new-view").hidden = true;
  $("run-view").hidden = false;
  $("page-title").textContent = "任务工作区";
  clearError();
  selectTab("activity");
  await refreshRun();
}
function eventCard(event) {
  const card = node("details", undefined, "event"),
    action = event.action || {},
    result = event.result || {};
  const summary = node(
    "summary",
    `${event.number}. ${action.action}${action.operation ? " / " + action.operation : ""}`,
  );
  summary.append(
    node(
      "span",
      result.returncode !== undefined
        ? "退出码 " + result.returncode
        : result.status || "",
      "small",
    ),
  );
  summary.append(
    node(
      "time",
      result.seconds === undefined ? "" : result.seconds.toFixed(1) + "秒",
    ),
  );
  card.append(
    summary,
    node(
      "div",
      [
        action.path || action.term || "",
        result.status || "",
        result.returncode !== undefined ? "退出码 " + result.returncode : "",
      ]
        .filter(Boolean)
        .join(" · "),
      "description",
    ),
  );
  card.append(node("pre", JSON.stringify(event, null, 2)));
  if (result.log) {
    const b = node("button", "查看完整日志", "ghost");
    b.onclick = () => openFile("tools/" + result.log).catch(error);
    card.append(b);
  }
  return card;
}
async function refreshRun() {
  clearTimeout(pollTimer);
  const id = current;
  if (!id) return;
  if (polling) {
    pollTimer = setTimeout(refreshRun, 200);
    return;
  }
  polling = true;
  try {
    const d = await api("/runs/" + id);
    if (id !== current) return;
    detail = d;
    renderDetail(d);
    const batch = await api("/runs/" + id + "/events?cursor=" + cursor);
    if (id !== current) return;
    batch.events.forEach((event) => $("events").append(eventCard(event)));
    cursor = batch.cursor;
    $("activity-note").textContent =
      batch.pending || d.records_being_written?.length
        ? "记录正在写入，等待完整内容"
        : batch.has_more
          ? "正在加载剩余执行记录…"
        : running(d.status)
          ? "已显示完成动作 · 等待新活动"
          : d.status === "Paused" ? "任务已暂停，候选与原始记录已保留" : "运行已结束，原始记录已保留";
    await refreshHistory();
    if (selectedTab === "changes" && !diffLoaded && d.result) await loadDiff();
    if (id === current && (batch.has_more || running(d.status)))
      pollTimer = setTimeout(refreshRun, batch.has_more ? 0 : 1000);
  } catch (e) {
    if (id === current) {
      error(e);
      $("activity-note").textContent =
        "读取中断；点击当前任务重新查看，不重派任务。";
    }
  } finally {
    polling = false;
  }
}
function renderDetail(d) {
  const r = d.result || {};
  $("run-title").textContent = d.title;
  $("run-status").textContent = labels[d.status] || d.status;
  $("run-meta").textContent = d.repo;
  $("run-prompt").textContent = d.task?.task || d.config?.task || d.title;
  $("detail-mode").textContent =
    d.mode === "fixture" || String(d.mode).includes("fixture")
      ? "零 API 夹具"
      : (r.model_configuration?.model || (d.config ? "DeepSeek Flash" : "未记录")) + " · DFX Agent API运行";
  $("detail-calls").textContent = running(d.status) ? d.recorded_requests : (r.model_calls ?? d.recorded_requests);
  $("detail-api").textContent =
    running(d.status) ? (d.mode === "fixture" ? "0" : "待终态汇总") :
    (r.api_calls ?? (d.mode === "fixture" ? "0" : "待终态汇总"));
  const seconds = r.seconds ?? (["Running", "Stopping"].includes(d.status)
    ? Math.max(0, (Date.now() - Date.parse(d.created)) / 1000) : null);
  $("detail-time").textContent = seconds === null ? "未知" : seconds.toFixed(1) +
    (running(d.status) && d.result ? " 秒（已完成段）" : " 秒");
  $("detail-tokens").textContent = running(d.status) ? "待终态汇总" : (d.total_tokens ?? "未知");
  $("detail-path").textContent = d.run;
  $("run-problem").textContent = r.error || d.spawn_error || d.startup_error || "";
  const preflight = d.environment_preflight;
  const problem = r.error || d.spawn_error || d.startup_error || (preflight ?
    `启动前环境检查：${preflight.stage}。${preflight.error || ''}` : "");
  $("run-notice").hidden = !problem;
  $("run-notice").textContent = problem.includes("DeepSeek HTTP 402")
    ? "DeepSeek返回402（余额不足）。任务已停止，未自动重试；请检查API账户余额，保留本次结果并回传。原始错误：" + problem
    : problem;
  $("agent-state").textContent = labels[d.status] || d.status;
  $("independent-state").textContent =
    d.independent.status === "NOT_RUN" ? "未关联评价" :
    labels[d.independent.status] || d.independent.status;
  $("submission").textContent = r.submission || "尚无最终提交声明。";
  $("self-check").textContent = r ? "查看实际执行记录" : "待记录";
  $("unverified").textContent = r.declaration
    ? "模型声明的未验证项：\n" +
      JSON.stringify(r.declaration.unverified ?? "未填写", null, 2)
    : "";
  $("stop").hidden = !["Running", "Stopping"].includes(d.status);
  $("stop").disabled = d.status === "Stopping";
  $("stop").textContent = d.status === "Stopping" ? "等待停止回执" : "停止任务";
  const waiting = d.status === "Paused" && r.waiting_user;
  $("waiting-user").hidden = !waiting;
  $("waiting-reason").textContent = waiting ? r.waiting_user.reason : "";
  $("waiting-request").textContent = waiting ? r.waiting_user.request : "";
  $("resume-problem").textContent = d.resume_error || "";
  $("resume").disabled = !d.can_resume;
  $("export").disabled = !d.result || running(d.status);
  $("patch-download").hidden = !d.result;
  $("patch-download").href =
    "/api/runs/" + d.id + "/file?path=patch.diff&download=true";
  $("changed-files").replaceChildren();
  for (const file of r.changed_files || []) {
    const name = typeof file === "string" ? file : JSON.stringify(file);
    $("changed-files").append(node("div", name, "file"));
  }
  if (!d.result)
    $("changed-files").append(node("p", "最终补丁将在运行结束后生成。"));
  $("record-buttons").replaceChildren();
  for (const path of [
    "task.json",
    "launch.json",
    "result.json",
    "report.md",
    "patch.diff",
    "trajectory.json",
    "tools/environment-preflight/result.json",
    "tools/environment-preflight/command.log",
    "@worker.log",
  ]) {
    const b = node("button", path, "record-row");
    b.onclick = () => openFile(path).catch(error);
    $("record-buttons").append(b);
  }
}
function selectTab(tab) {
  selectedTab = tab;
  for (const t of ["activity", "overview", "changes", "records"])
    $("tab-" + t).hidden = t !== tab;
  document
    .querySelectorAll("[data-tab]")
    .forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  if (tab === "changes") {
    if (detail?.result && !diffLoaded) loadDiff().catch(error);
    else if (!detail?.result)
      $("diff-status").textContent = "最终补丁将在运行结束后生成。";
  }
}
document
  .querySelectorAll("[data-tab]")
  .forEach((b) => (b.onclick = () => selectTab(b.dataset.tab)));
async function loadDiff() {
  const id = current;
  const value = await api("/runs/" + id + "/file?path=patch.diff");
  if (id !== current) return;
  $("diff").replaceChildren();
  let pre = null;
  for (const line of value.text.split("\n")) {
    if (line.startsWith("diff --git ") || !pre) {
      const block = node("div", undefined, "diff");
      if (line.startsWith("diff --git "))
        block.append(node("div", line, "diff-head"));
      pre = node("pre");
      block.append(pre);
      $("diff").append(block);
      if (line.startsWith("diff --git ")) continue;
    }
    pre.append(
      node(
        "span",
        line,
        line.startsWith("+") ? "add" : line.startsWith("-") ? "remove" : "",
      ),
    );
  }
  $("diff-status").textContent = value.truncated
    ? "仅显示前256KB，下载补丁查看完整内容。"
    : value.text
      ? "相对原始副本的最终变更"
      : "没有文本变更；二进制变化以 result.json 为准。";
  diffLoaded = true;
}
function openText(title, text) {
  $("file-title").textContent = title;
  $("file-content").replaceChildren(node("pre", text));
  $("file-download").hidden = true;
  if (!$("file-dialog").open) $("file-dialog").showModal();
}
async function openFile(path) {
  const value = await api(
    "/runs/" + current + "/file?path=" + encodeURIComponent(path),
  );
  openText(
    path,
    (value.truncated ? "[预览前256KB；可下载完整文件]\n" : "") + value.text,
  );
  $("file-download").hidden = false;
  $("file-download").href =
    "/api/runs/" +
    current +
    "/file?path=" +
    encodeURIComponent(path) +
    "&download=true";
}
$("resume").onclick = async () => {
  $("resume").disabled = true;
  try {
    await api("/runs/" + current + "/resume", {waiting_tool: detail.result.tool_calls});
    diffLoaded = false;
    await refreshRun();
  } catch (e) { error(e); }
};

$("stop").onclick = async () => {
  try {
    await api("/runs/" + current + "/stop", {});
    await refreshRun();
  } catch (e) {
    error(e);
  }
};
$("export").onclick = async () => {
  try {
    await api("/runs/" + current + "/export", {
      evaluation: $("evaluation-path").value.trim(),
    });
    await refreshRun();
    const a = node("a");
    a.href = "/api/runs/" + current + "/summary";
    a.click();
    toast("摘要已导出；原始结果未修改。");
  } catch (e) {
    error(e);
  }
};
async function browse(directory = "workspace", offset = 0) {
  const data = await api(
    "/runs/" +
      current +
      "/tree?directory=" +
      encodeURIComponent(directory) +
      "&offset=" +
      offset,
  );
  $("file-title").textContent = directory;
  $("file-content").replaceChildren();
  $("file-download").hidden = true;
  if (directory !== "workspace") {
    const back = node("button", "← 上一级", "ghost");
    back.onclick = () =>
      browse(directory.split("/").slice(0, -1).join("/")).catch(error);
    $("file-content").append(back);
  }
  for (const entry of data.entries) {
    const b = node(
      "button",
      (entry.directory ? "▱ " : "") + entry.name,
      "record-row",
    );
    b.onclick = () =>
      entry.directory
        ? browse(entry.path).catch(error)
        : openFile(entry.path).catch(error);
    $("file-content").append(b);
  }
  if (data.next !== null) {
    const next = node("button", "下一页", "secondary");
    next.onclick = () => browse(directory, data.next).catch(error);
    $("file-content").append(next);
  }
  if (!$("file-dialog").open) $("file-dialog").showModal();
}
$("workspace-open").onclick = () => browse().catch(error);
$("import-run").onclick = async () => {
  const path = prompt(
    "已有 solve 运行目录的绝对路径（只读导入，不重新执行）：",
  );
  if (!path) return;
  try {
    const run = await api("/import", { path });
    await openRun(run.id);
  } catch (e) {
    error(e);
  }
};
window.addEventListener("hashchange", () => {
  const id = location.hash.slice(1);
  if (id && id !== current) openRun(id).catch(error);
  else if (!id && current) newTask();
});
function closeNavigation() {
  document.querySelector('.app').classList.remove('nav-open');
  $('navigation-toggle').setAttribute('aria-expanded', 'false');
}
$('navigation-toggle').onclick = () => {
  const open = document.querySelector('.app').classList.toggle('nav-open');
  $('navigation-toggle').setAttribute('aria-expanded', String(open));
};
document.querySelector('.sidebar').addEventListener('click', (event) => {
  if (event.target.closest('button')) closeNavigation();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeNavigation();
});
(async () => {
  try {
    config = await api("/config");
    $("version").textContent = "本地工作台 · " + config.version;
    $("service-mode").textContent = config.allow_live
      ? "真实模型入口已启用"
      : "零 API 模式 · 设备" + (config.allow_device ? "已启用" : "暂停");
    $("mode").querySelector("[value=deepseek]").disabled = !config.allow_live;
    for (const id of [
      "allow_device",
      "allow_system_navigation",
      "allow_screen_unlock",
    ])
      $(id).disabled = !config.allow_device;
    $("local-config").textContent =
      "解释器：" +
      config.python +
      "\n结果目录：" +
      config.data_dir +
      "\nAPI 环境变量：" +
      (config.api_key_configured ? "已配置（不显示密钥）" : "未配置");
    const saved = localStorage.getItem("dfx-task-settings");
    if (saved) {
      const data = JSON.parse(saved);
      if (!config.allow_live) data.mode = "fixture";
      if (!config.allow_device)
        for (const k of [
          "allow_device",
          "allow_system_navigation",
          "allow_screen_unlock",
        ])
          data[k] = false;
      fillForm(data);
    }
    updateForm();
    await refreshHistory();
    if (location.hash) await openRun(location.hash.slice(1));
    else await showAssignments();
  } catch (e) {
    if (!config) {
      $("version").textContent = "本地服务未连接";
      $("service-mode").textContent = "请运行 START.cmd，保持启动窗口打开，然后刷新页面。";
    }
    error(e);
  }
})();

let memberListing = null, selectedAssignment = null, preparedAssignment = null;
async function showAssignments() {
  $('feedback-download').hidden = true;
  current = null;
  location.hash = '';
  clearTimeout(pollTimer);
  $('new-view').hidden = true;
  $('run-view').hidden = true;
  $('assignments-view').hidden = false;
  $('page-title').textContent = '预配置任务';
  clearError();
  memberListing = await api('/assignments');
  const settings = memberListing.settings;
  $('member-status').textContent = settings.member
    ? `${settings.member === 'member1' ? '组员1' : '组员2'} · ${memberListing.tasks.length} 项任务 · 环境已保存`
    : '首次使用：配置数据目录和测试环境，之后直接选题。';
  $('assignment-list').replaceChildren();
  for (const task of memberListing.tasks) {
    const card = node('button', undefined, 'assignment-card');
    card.append(node('span', String(task.order).padStart(2, '0'), 'eyebrow'),
      node('h3', task.title), node('p', task.task_id, 'task-key'));
    const last = task.attempts.at(-1);
    card.append(node('span', last ? (labels[last.status] || last.status) :
      task.feedback.status === 'BLOCKED' ? '已记录阻塞' : '待执行', 'tag'));
    if (task.known_issue) card.append(node('p', task.known_issue, 'small'));
    if (task.environment) card.append(node('p', task.environment.summary, 'small'));
    card.onclick = () => showAssignment(task).catch(error);
    $('assignment-list').append(card);
  }
}
$('assigned-tasks').onclick = () => showAssignments().catch(error);
async function openEnvironment() {
  memberListing = await api('/assignments');
  const settings = memberListing.settings;
  $('member-select').replaceChildren(...memberListing.members.map(m => {
    const option = node('option', m === 'member1' ? '组员1' : '组员2'); option.value = m; return option;
  }));
  $('member-select').value = settings.member || memberListing.members[0];
  $('dataset-root').value = settings.dataset || '';
  $('deveco-root').value = settings.deveco || '';
  $('member-device').value = settings.device || '';
  $('device-status').textContent = '';
  $('member-unlock').checked = !!settings.swipe_only;
  $('environment-dialog').showModal();
  await discoverEnvironment();
}
async function discoverEnvironment() {
  const env = await api('/environment/discover');
  $('deveco-candidates').replaceChildren(...env.deveco.map(p => { const o = node('option'); o.value = p; return o; }));
  if (!$('deveco-root').value && env.deveco.length === 1) $('deveco-root').value = env.deveco[0];
  $('member-key-status').textContent = 'API密钥：' + (env.api_key_configured ? '已配置（不显示值）' : '未配置，真实运行前设置DEEPSEEK_API_KEY并重启工作台');
}
$('environment-open').onclick = () => openEnvironment().catch(error);
$('environment-detect').onclick = () => discoverEnvironment().catch(error);
$('device-detect').onclick = async () => {
  $('device-detect').disabled = true;
  $('device-status').textContent = '正在识别已连接设备…';
  try {
    const result = await api('/environment/devices', {deveco: $('deveco-root').value.trim()});
    $('device-candidates').replaceChildren(...result.devices.map(p => {const o = node('option');o.value = p;return o;}));
    if (result.devices.length === 1) $('member-device').value = result.devices[0];
    $('device-status').textContent = result.message || (result.devices.length === 1 ? '已选择唯一设备：' + result.devices[0] : '发现多个设备，请选择本次测试模拟器');
  } catch (e) {$('device-status').textContent = '设备识别失败：' + e.message; error(e);} finally {$('device-detect').disabled = false;}
};
$('environment-save').onclick = async () => {
  $('environment-save').disabled = true;
  try {
    await api('/environment', {member:$('member-select').value, dataset:$('dataset-root').value.trim(),
      deveco:$('deveco-root').value.trim(), device:$('member-device').value.trim(), swipe_only:$('member-unlock').checked});
    $('environment-dialog').close();
    await showAssignments();
    toast('环境已保存，选择任务即可开始。');
  } catch (e) {error(e);} finally {$('environment-save').disabled = false;}
};
async function showAssignment(task) {
  if (!memberListing.settings.member) {await openEnvironment(); return;}
  selectedAssignment = task; preparedAssignment = null;
  $('assignment-title').textContent = task.title;
  $('assignment-id').textContent = task.task_id;
  $('assignment-issue').textContent = [task.environment?.summary, task.known_issue].filter(Boolean).join('\n');
  $('assignment-prompt').textContent = task.task;
  $('assignment-policy').textContent = memberListing.policy;
  $('assignment-check').textContent = task.check;
  $('member-verdict').value = task.feedback.status || 'NOT_RUN';
  $('member-notes').value = task.feedback.notes || '';
  $('assignment-start').disabled = true;
  $('prepared-summary').textContent = '';
  $('assignment-dialog').showModal();
  if (task.attempts.length && !task.environment_recheck_allowed) {
    $('assignment-start').textContent = '查看已有任务';
    $('assignment-start').disabled = false;
    $('prepared-summary').textContent = '本题已有记录，直接查看和回传，不自动重跑。';
    return;
  }
  $('assignment-start').textContent = '正在准备任务资料…';
  $('prepared-summary').textContent = '自动创建工作输入、精简日志和本机配置，不修改原始数据。';
  try {
    const prepared = await api('/assignments/prepare', {task_id:task.task_id});
    if (selectedAssignment !== task) return;
    preparedAssignment = prepared;
    const excerpts = prepared.logs.filter(x => x.status === 'EXCERPT').length;
    $('prepared-summary').textContent = `资料已准备：${prepared.files}文件，${prepared.editable}可编辑，${excerpts}份精简日志。` +
      (prepared.input_changes.length ? '已移除原机签名配置，源码未变。' : '') +
      (excerpts ? '' : '没有可用历史日志，Agent将尝试本机复现。');
    $('prepared-summary').textContent += '启动后先检查环境并自动准备依赖；通过后才调用模型，不预先编译。';
    if (task.environment_recheck_allowed) $('prepared-summary').textContent += '上次仅环境检查阻塞、模型0调用；本次显式重新检查，旧记录保留。';
    $('assignment-start').textContent = config.allow_live ?
      (task.environment_recheck_allowed ? '环境已修复，重新检查并启动 ↗' : '检查环境并自主修复 ↗') : '当前为零API检查模式';
    $('assignment-start').disabled = !config.allow_live;
  } catch (e) {
    $('prepared-summary').textContent = '资料准备未完成：' + e.message;
    $('assignment-start').textContent = '暂不能启动';
    error(e);
  }
}
$('assignment-start').onclick = async () => {
  if (!selectedAssignment || busy) return;
  if (selectedAssignment.attempts.length && !selectedAssignment.environment_recheck_allowed) {
    $('assignment-dialog').close(); await openRun(selectedAssignment.attempts.at(-1).id); return;
  }
  if (!preparedAssignment) return;
  busy = true; $('assignment-start').disabled = true;
  try {
    const run = await api('/assignments/start', {prepared_id:preparedAssignment.prepared_id});
    $('assignment-dialog').close(); await openRun(run.id);
  } catch (e) {error(e);} finally {busy = false; $('assignment-start').disabled = false;}
};
$('member-feedback-save').onclick = async () => {
  if (!selectedAssignment) return;
  try {
    await api('/assignments/feedback', {task_id:selectedAssignment.task_id, status:$('member-verdict').value,
      notes:$('member-notes').value, run_id:selectedAssignment.attempts.at(-1)?.id || ''});
    toast('独立观察已保存，原Agent结果不变。');
    $('assignment-dialog').close(); await showAssignments();
  } catch (e) {error(e);}
};
$('batch-export').onclick = async () => {
  $('batch-export').disabled = true;
  try {
    const result = await api('/assignments/export', {});
    const link = node('a', '下载：' + result.filename);
    link.href = result.url; link.download = result.filename;
    $('feedback-download').replaceChildren('反馈包已生成。若下载未开始，请点击：', link);
    $('feedback-download').hidden = false;
    link.click();
    toast('反馈包已生成，包含12题汇总和已运行任务的原始记录。');
  } catch (e) {error(e);} finally {$('batch-export').disabled = false;}
};
}
