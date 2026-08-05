const state = {
  workflows: [],
  selected: null,
  activeRun: null,
  lastFields: {},
  lastFile: null,
  pollTimer: null,
  routeBusy: false,
  projectStore: null,
  runProjectSnapshot: null,
  stageOverrideRequired: false,
};

const chat = document.querySelector("#chat");
const composer = document.querySelector("#composer");
const form = document.querySelector("#workflow-form");
const fieldsRoot = document.querySelector("#form-fields");
const submitButton = document.querySelector("#submit-button");
const routeForm = document.querySelector("#route-form");
const routeMessage = document.querySelector("#route-message");
const routeButton = document.querySelector("#route-button");
const routeResult = document.querySelector("#route-result");
const projectSelect = document.querySelector("#project-select");
const projectDialog = document.querySelector("#project-dialog");
const projectFields = document.querySelector("#project-fields");
const projectContextBox = document.querySelector("#form-project-context");

const WORKFLOW_TITLES = { w1: "研究设计", w2: "访谈设计", w3: "质性分析", w4: "质量质检" };
const PROJECT_FIELD_DEFINITIONS = [
  ["research_question", "研究问题", false],
  ["target_population", "目标研究群体", false],
  ["research_context", "研究背景", false],
  ["method_plan", "方法计划", false],
  ["interview_guide", "访谈提纲", false],
  ["candidate_codes", "候选编码（每行一项）", true],
  ["candidate_themes", "候选主题（每行一项）", true],
  ["candidate_claims", "候选结论（每行一项）", true],
  ["audit_status", "质检状态", false],
  ["audit_notes", "质检记录", false],
  ["unresolved_decisions", "待解决问题（每行一项）", true],
];
const PROJECT_FIELD_LABELS = Object.fromEntries(PROJECT_FIELD_DEFINITIONS.map(([key, label]) => [key, label.replace(/（.*$/, "")]));
PROJECT_FIELD_LABELS.materials = "材料卡";

document.addEventListener("DOMContentLoaded", bootstrap);

async function bootstrap() {
  initializeProjectStore();
  document.querySelector("#main-menu-button").addEventListener("click", showMainMenu);
  document.querySelector("#cancel-form-button").addEventListener("click", hideComposer);
  form.addEventListener("submit", submitRun);
  routeForm.addEventListener("submit", submitRoute);
  bindProjectControls();
  try {
    const [health, workflows] = await Promise.all([
      fetchJSON("/api/health"),
      fetchJSON("/api/workflows"),
    ]);
    state.workflows = workflows;
    renderHealth(health);
    renderMenus();
  } catch (error) {
    renderHealth({ status: "error", configuration_error: String(error) });
    addAssistant(`<div class="error-box">无法连接本地后端：${escapeHtml(String(error))}</div>`);
  }
}

function initializeProjectStore() {
  let storage = null;
  try { storage = window.localStorage; } catch { storage = null; }
  state.projectStore = new window.XingxiaodaoProjects.BrowserProjectStore(storage);
  renderProjectSwitcher();
}

function bindProjectControls() {
  projectSelect.addEventListener("change", () => {
    if (state.activeRun) {
      alert("工作流运行期间不能切换项目。");
      renderProjectSwitcher();
      return;
    }
    try {
      if (projectSelect.value) state.projectStore.switch(projectSelect.value);
      else state.projectStore.deactivate();
      renderProjectSwitcher();
    } catch (error) { alert(String(error)); }
  });
  document.querySelector("#new-project-button").addEventListener("click", createProjectFromPrompt);
  document.querySelector("#open-project-button").addEventListener("click", openProjectDialog);
  document.querySelector("#project-card-button").addEventListener("click", openProjectDialog);
  document.querySelector("#save-project-button").addEventListener("click", saveManualProjectEdits);
  document.querySelector("#rename-project-button").addEventListener("click", renameActiveProject);
  document.querySelector("#delete-project-button").addEventListener("click", deleteActiveProject);
  document.querySelector("#export-project-button").addEventListener("click", exportActiveProject);
  document.querySelector("#import-project-button").addEventListener("click", () => document.querySelector("#import-project-file").click());
  document.querySelector("#import-project-file").addEventListener("change", importProjectFile);
}

function renderProjectSwitcher() {
  if (!state.projectStore) return;
  const active = state.projectStore.active();
  projectSelect.replaceChildren(new Option("临时单次模式", ""));
  state.projectStore.list().forEach((project) => {
    projectSelect.append(new Option(project.project_name, project.project_id));
  });
  projectSelect.value = active?.project_id || "";
  const status = document.querySelector("#project-storage-status");
  if (state.projectStore.warning) status.textContent = state.projectStore.warning;
  else if (active) status.textContent = `${active.current_stage} · 修订 ${active.revision}${state.projectStore.persistent ? " · 已保存到本浏览器" : " · 仅当前页面"}`;
  else status.textContent = "未创建项目时保持 v1.2 单次运行方式。";
  projectSelect.disabled = Boolean(state.activeRun);
  renderProjectDialog();
}

function createProjectFromPrompt() {
  if (state.activeRun) return alert("工作流运行期间不能新建项目。");
  const name = prompt("请输入项目名称：", "新的社会实践项目");
  if (name === null) return;
  try {
    state.projectStore.create(name);
    renderProjectSwitcher();
    openProjectDialog();
  } catch (error) { alert(String(error)); }
}

function openProjectDialog() {
  renderProjectDialog();
  if (typeof projectDialog.showModal === "function") projectDialog.showModal();
  else projectDialog.setAttribute("open", "");
}

function renderProjectDialog() {
  if (!state.projectStore) return;
  const project = state.projectStore.active();
  const empty = document.querySelector("#project-dialog-empty");
  const content = document.querySelector("#project-dialog-content");
  document.querySelector("#project-dialog-title").textContent = project ? project.project_name : "项目卡";
  empty.hidden = Boolean(project);
  content.hidden = !project;
  ["#save-project-button", "#rename-project-button", "#export-project-button", "#delete-project-button"].forEach((selector) => {
    document.querySelector(selector).disabled = !project || Boolean(state.activeRun);
  });
  if (!project) return;

  const stageGrid = document.querySelector("#project-stage-grid");
  stageGrid.innerHTML = Object.keys(WORKFLOW_TITLES).map((workflow) => {
    const status = project.stale_stages.includes(workflow)
      ? "需要重新生成"
      : project.confirmed_stages.includes(workflow) ? "已确认" : "未完成";
    return `<button type="button" class="stage-card stage-${status === "已确认" ? "done" : status === "需要重新生成" ? "stale" : "pending"}" data-project-stage="${workflow}"><strong>${workflow.toUpperCase()} · ${WORKFLOW_TITLES[workflow]}</strong><span>${status}</span></button>`;
  }).join("");
  stageGrid.querySelectorAll("[data-project-stage]").forEach((button) => {
    button.addEventListener("click", () => {
      projectDialog.close();
      showWorkflowForm(button.dataset.projectStage);
    });
  });

  projectFields.innerHTML = PROJECT_FIELD_DEFINITIONS.map(([key, label, isList]) => {
    const value = isList ? (project[key] || []).join("\n") : (project[key] || "");
    return `<div class="field"><label for="project-field-${key}">${escapeHtml(label)}</label><textarea id="project-field-${key}" data-project-field="${key}" data-list="${isList}">${escapeHtml(value)}</textarea></div>`;
  }).join("");
  const materials = project.materials.length
    ? project.materials.map((item) => `<li><strong>${escapeHtml(item.source_id)}</strong> · ${escapeHtml(item.display_name)} · ${escapeHtml(item.source_type || "未标类型")} · ${formatBytes(item.size_bytes || 0)}<br><small>SHA-256：${escapeHtml(item.sha256 || "未记录")}</small></li>`).join("")
    : "<li>暂无材料卡。原始附件不会保存在浏览器中。</li>";
  const results = Object.entries(project.stage_results).map(([workflow, result]) => `<li><strong>${workflow.toUpperCase()}</strong> · ${escapeHtml(result.confirmed_at || "")} · ${escapeHtml(String(result.markdown || "").slice(0, 180))}${String(result.markdown || "").length > 180 ? "…" : ""}</li>`).join("") || "<li>暂无已确认阶段成果。</li>";
  const historicalResults = (project.stage_result_history || []).slice(-12).reverse().map((result) => `<li><strong>${escapeHtml(result.workflow_id.toUpperCase())}</strong> · ${escapeHtml(result.confirmed_at || "")} · ${escapeHtml(String(result.markdown || "").slice(0, 140))}${String(result.markdown || "").length > 140 ? "…" : ""}</li>`).join("") || "<li>暂无被替换的旧成果。</li>";
  const history = project.revision_history.slice(-20).reverse().map((entry) => `<li>${escapeHtml(entry.at)} · ${escapeHtml(entry.source)} · ${escapeHtml(PROJECT_FIELD_LABELS[entry.path] || entry.path)}</li>`).join("") || "<li>暂无修订记录。</li>";
  document.querySelector("#project-details-content").innerHTML = `<h4>材料卡</h4><ul>${materials}</ul><h4>当前阶段成果</h4><ul>${results}</ul><h4>被替换的旧成果（最近12份）</h4><ul>${historicalResults}</ul><h4>最近修订</h4><ul>${history}</ul>`;
}

function saveManualProjectEdits() {
  const project = state.projectStore.active();
  if (!project || state.activeRun) return;
  const values = {};
  projectFields.querySelectorAll("[data-project-field]").forEach((control) => {
    values[control.dataset.projectField] = control.dataset.list === "true"
      ? control.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
      : control.value;
  });
  try {
    state.projectStore.applyManual(project.project_id, values, project.revision);
    renderProjectSwitcher();
  } catch (error) { alert(String(error)); }
}

function renameActiveProject() {
  const project = state.projectStore.active();
  if (!project || state.activeRun) return;
  const name = prompt("新的项目名称：", project.project_name);
  if (name === null) return;
  try { state.projectStore.rename(project.project_id, name); renderProjectSwitcher(); } catch (error) { alert(String(error)); }
}

function deleteActiveProject() {
  const project = state.projectStore.active();
  if (!project || state.activeRun) return;
  if (!confirm(`确认只删除项目“${project.project_name}”吗？此操作无法撤销，建议先导出 JSON。`)) return;
  state.projectStore.remove(project.project_id);
  renderProjectSwitcher();
}

function exportActiveProject() {
  const project = state.projectStore.active();
  if (!project) return;
  try {
    const blob = new Blob([state.projectStore.exportProject(project.project_id)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `xingxiaodao-${project.project_id}.json`;
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) { alert(String(error)); }
}

async function importProjectFile(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file || state.activeRun) return;
  try {
    if (file.size > 1_000_000) throw new Error("导入文件超过 1MB 限制。");
    state.projectStore.importProject(await file.text());
    renderProjectSwitcher();
    openProjectDialog();
  } catch (error) { alert(`导入失败：${error}`); }
}

async function submitRoute(event) {
  event.preventDefault();
  if (state.routeBusy || state.activeRun) return;
  const message = routeMessage.value.trim();
  if (!message) {
    routeMessage.focus();
    return;
  }
  setRouteBusy(true);
  routeResult.hidden = false;
  routeResult.innerHTML = '<div class="progress-line"><span class="spinner"></span><span>正在判断当前研究阶段……</span></div>';
  try {
    const response = await fetch("/api/route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (!response.ok) throw new Error(await errorFromResponse(response));
    renderRouteResult(await response.json());
  } catch (error) {
    routeResult.innerHTML = `<div class="error-box">意图识别失败：${escapeHtml(String(error))}</div>`;
  } finally {
    setRouteBusy(false);
  }
}

function renderRouteResult(result) {
  const workflow = state.workflows.find((item) => item.id === result.recommended_workflow);
  const secondary = state.workflows.find((item) => item.id === result.possible_secondary_workflow);
  const confidenceLabels = { high: "高", medium: "中", low: "低" };
  const missing = (result.missing_information || []).length
    ? `<div class="route-missing"><strong>建议补充：</strong><ul>${result.missing_information.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>`
    : "";
  const secondaryText = secondary
    ? `<p><strong>可能的后续步骤：</strong>${escapeHtml(secondary.title)}</p>`
    : "";
  const recommendation = workflow
    ? `<p class="route-recommendation">推荐：<strong>${escapeHtml(workflow.title)}</strong></p>`
    : '<p class="route-recommendation"><strong>暂时无法可靠判断</strong></p>';
  const acceptButton = workflow
    ? `<button class="primary-button" type="button" data-route-workflow="${escapeHtml(workflow.id)}">使用“${escapeHtml(workflow.title)}”</button>`
    : "";
  routeResult.innerHTML = `${recommendation}
    <p>${escapeHtml(result.reason)}</p>
    <p class="route-meta">判断把握：${escapeHtml(confidenceLabels[result.confidence] || result.confidence)}</p>
    ${missing}${secondaryText}
    <div class="route-result-actions">${acceptButton}<button class="secondary-button" type="button" data-route-revise>修改描述后重新判断</button></div>`;
  routeResult.querySelector("[data-route-workflow]")?.addEventListener("click", (event) => {
    showWorkflowForm(event.currentTarget.dataset.routeWorkflow);
  });
  routeResult.querySelector("[data-route-revise]")?.addEventListener("click", () => {
    routeMessage.focus();
    routeMessage.select();
  });
}

function setRouteBusy(busy) {
  state.routeBusy = busy;
  routeButton.disabled = busy;
  routeMessage.disabled = busy;
}

function renderHealth(health) {
  const card = document.querySelector("#health-card");
  const title = document.querySelector("#health-title");
  const detail = document.querySelector("#health-detail");
  card.classList.remove("ready", "error");
  if (health.status === "ok") {
    card.classList.add("ready");
    title.textContent = health.app_mode === "mock" ? "模拟模式就绪" : "模型服务已配置";
    const providerName = health.provider === "stepfun" ? "阶跃星辰" : "DeepSeek";
    detail.textContent = health.provider === "deepseek"
      ? `${providerName} · ${health.model} · 思考模式${health.thinking === "enabled" ? "开启" : "关闭"}`
      : `${providerName} Coding Plan · ${health.model}`;
  } else {
    card.classList.add("error");
    title.textContent = "需要配置 API Key";
    detail.textContent = health.configuration_error
      || "本地请填写 .env；Vercel 请配置 Environment Variables 后重新部署";
  }
}

function renderMenus() {
  const menuGrid = document.querySelector("#menu-grid");
  const nav = document.querySelector("#workflow-nav");
  menuGrid.replaceChildren();
  nav.replaceChildren();
  state.workflows.forEach((workflow, index) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "menu-card";
    card.innerHTML = `<strong>${index + 1}、${escapeHtml(workflow.title)}</strong><span>${escapeHtml(workflow.description)}</span>`;
    card.addEventListener("click", () => showWorkflowForm(workflow.id));
    menuGrid.append(card);

    const navButton = document.createElement("button");
    navButton.type = "button";
    navButton.className = "nav-button";
    navButton.dataset.workflow = workflow.id;
    navButton.innerHTML = `<span>Workflow ${index + 1}</span>${escapeHtml(workflow.title)}`;
    navButton.addEventListener("click", () => showWorkflowForm(workflow.id));
    nav.append(navButton);
  });
}

function projectPrefillFor(workflowId) {
  const project = state.projectStore?.active();
  if (!project) return {};
  const latestMaterial = project.materials.at(-1) || {};
  if (workflowId === "w1") return {
    theme: project.research_question || project.project_name,
    purpose: project.research_question,
    background: project.research_context,
    participants: project.target_population,
  };
  if (workflowId === "w2") return {
    mode: "generate",
    research_question: project.research_question,
    participant_profile: project.target_population,
    existing_questions: project.interview_guide,
    review_topic: project.research_question,
    review_participant: project.target_population,
  };
  if (workflowId === "w3") return {
    research_question: project.research_question,
    source_id: latestMaterial.source_id || "",
    source_type: latestMaterial.source_type || "单份访谈",
    source_context: latestMaterial.source_context || project.research_context,
  };
  return {
    research_question: project.research_question,
    candidate_claim: (project.candidate_claims || []).map((item, index) => `C${String(index + 1).padStart(2, "0")}：${item}`).join("\n"),
    target_population: project.target_population,
    sample_summary: latestMaterial.summary || "",
    source_id: latestMaterial.source_id || "",
    source_context: latestMaterial.source_context || project.research_context,
  };
}

function renderProjectContextForForm(workflowId) {
  const project = state.projectStore?.active();
  state.stageOverrideRequired = false;
  projectContextBox.hidden = !project;
  if (!project) {
    projectContextBox.replaceChildren();
    return;
  }
  const prerequisite = { w2: "w1", w3: "w2", w4: "w3" }[workflowId];
  const missing = prerequisite && (
    !project.confirmed_stages.includes(prerequisite) || project.stale_stages.includes(prerequisite)
  );
  state.stageOverrideRequired = Boolean(missing);
  const used = {
    w1: ["项目名称", "研究背景", "目标群体"],
    w2: ["研究问题", "目标群体", "已有访谈提纲"],
    w3: ["研究问题", "最近材料卡（不含原文）"],
    w4: ["研究问题", "目标群体", "候选结论", "最近材料卡"],
  }[workflowId];
  projectContextBox.innerHTML = `<strong>当前项目：${escapeHtml(project.project_name)}</strong><p>本表单会读取：${used.join("、")}。你仍可修改预填值，运行完成后需再次确认才会写回。</p>${missing ? `<label class="override-check"><input id="stage-override" type="checkbox"> 上一阶段尚未确认或已经过期；我理解风险并继续单独运行 ${workflowId.toUpperCase()}</label>` : ""}`;
}

function showWorkflowForm(workflowId, reuse = false) {
  if (state.activeRun) return;
  const workflow = state.workflows.find((item) => item.id === workflowId);
  if (!workflow) return;
  state.selected = workflow;
  document.querySelector("#page-title").textContent = workflow.title;
  document.querySelector("#form-workflow-id").textContent = workflow.id.toUpperCase();
  document.querySelector("#form-title").textContent = workflow.title;
  document.querySelector("#form-description").textContent = workflow.description;
  document.querySelectorAll(".nav-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.workflow === workflowId);
  });
  fieldsRoot.replaceChildren();
  const projectPrefill = projectPrefillFor(workflowId);
  workflow.fields.forEach((field) => fieldsRoot.append(createField(field, reuse, projectPrefill)));
  wireConditionalFields();
  renderProjectContextForForm(workflowId);
  composer.hidden = false;
  fieldsRoot.querySelector("input, textarea, select")?.focus();
}

function createField(field, reuse, projectPrefill = {}) {
  const wrapper = document.createElement("div");
  wrapper.className = "field";
  wrapper.dataset.field = field.name;
  if (field.show_when) {
    const [key, value] = Object.entries(field.show_when)[0];
    wrapper.dataset.showKey = key;
    wrapper.dataset.showValue = value;
  }

  const label = document.createElement("label");
  label.htmlFor = `field-${field.name}`;
  label.innerHTML = `${escapeHtml(field.label)}${field.required ? ' <span class="required">*</span>' : ""}`;
  wrapper.append(label);

  let control;
  if (field.kind === "select") {
    control = document.createElement("select");
    field.options.forEach((option) => {
      const el = document.createElement("option");
      el.value = option.value;
      el.textContent = option.label;
      control.append(el);
    });
  } else if (field.kind === "file") {
    control = document.createElement("input");
    control.type = "file";
    control.accept = ".txt,.md,.docx,.pdf";
  } else {
    control = document.createElement("textarea");
  }
  control.id = `field-${field.name}`;
  control.name = field.name;
  control.required = Boolean(field.required);
  if (reuse && field.kind !== "file" && state.lastFields[field.name] !== undefined) {
    control.value = state.lastFields[field.name];
  } else if (field.kind !== "file" && projectPrefill[field.name] !== undefined) {
    control.value = projectPrefill[field.name];
  }
  wrapper.append(control);
  if (field.help) {
    const help = document.createElement("small");
    help.textContent = field.help;
    wrapper.append(help);
  }
  return wrapper;
}

function wireConditionalFields() {
  const controllers = new Set(
    [...fieldsRoot.querySelectorAll("[data-show-key]")].map((el) => el.dataset.showKey),
  );
  controllers.forEach((name) => {
    const control = fieldsRoot.querySelector(`[name="${name}"]`);
    control?.addEventListener("change", updateConditionalFields);
  });
  updateConditionalFields();
}

function updateConditionalFields() {
  fieldsRoot.querySelectorAll("[data-show-key]").forEach((wrapper) => {
    const control = fieldsRoot.querySelector(`[name="${wrapper.dataset.showKey}"]`);
    const visible = control?.value === wrapper.dataset.showValue;
    wrapper.hidden = !visible;
    const input = wrapper.querySelector("input, textarea, select");
    if (input) input.disabled = !visible;
  });
}

async function submitRun(event) {
  event.preventDefault();
  if (!state.selected || state.activeRun) return;
  if (state.stageOverrideRequired && !document.querySelector("#stage-override")?.checked) {
    alert("请先确认你理解跳过或使用过期上游阶段的风险。");
    return;
  }

  const fields = {};
  let sourceFile = null;
  new FormData(form).forEach((value, key) => {
    if (value instanceof File) {
      if (value.size) sourceFile = value;
    } else {
      fields[key] = value;
    }
  });
  state.lastFields = { ...fields };
  state.lastFile = sourceFile;
  const activeProject = state.projectStore?.active();
  state.runProjectSnapshot = activeProject
    ? { project_id: activeProject.project_id, revision: activeProject.revision }
    : null;
  hideComposer();
  addUser(summaryForUser(state.selected, fields, sourceFile));

  const runningBubble = addAssistant(
    `<div class="running-card"><div class="progress-line"><span class="spinner"></span><span class="progress-text">正在创建运行任务……</span></div><div class="trace-slot"></div></div>`,
  );
  setBusy(true);

  try {
    const payload = new FormData();
    payload.append("workflow_id", state.selected.id);
    payload.append("fields_json", JSON.stringify(fields));
    if (activeProject) payload.append("project_context_json", JSON.stringify(state.projectStore.context()));
    if (sourceFile) payload.append("source_file", sourceFile);
    const response = await fetch("/api/runs", { method: "POST", body: payload });
    if (!response.ok) throw new Error(await errorFromResponse(response));
    const created = await response.json();
    state.activeRun = created.run_id;
    await pollRun(created.run_id, runningBubble);
  } catch (error) {
    runningBubble.innerHTML = `<div class="error-box">${escapeHtml(String(error))}</div>`;
    state.activeRun = null;
    state.runProjectSnapshot = null;
    setBusy(false);
  }
}

async function pollRun(runId, bubble) {
  while (state.activeRun === runId) {
    let run;
    try {
      run = await fetchJSON(`/api/runs/${runId}`);
    } catch (error) {
      bubble.innerHTML = `<div class="error-box">读取运行状态失败：${escapeHtml(String(error))}</div>`;
      break;
    }
    renderRunProgress(run, bubble);
    if (run.status === "succeeded" || run.status === "failed") {
      state.activeRun = null;
      setBusy(false);
      return;
    }
    await delay(700);
  }
  state.activeRun = null;
  setBusy(false);
}

function renderRunProgress(run, bubble) {
  if (run.status === "failed") {
    state.runProjectSnapshot = null;
    bubble.innerHTML = `${run.error ? `<div class="error-box">${escapeHtml(run.error)}</div>` : ""}${renderTracePanel(run.traces, true)}`;
    return;
  }
  if (run.status === "succeeded") {
    bubble.innerHTML = `<div class="markdown">${renderMarkdown(run.final_markdown || "")}</div>${renderActions(run)}${renderTracePanel(run.traces, true)}`;
    wireResultActions(bubble, run);
    scrollToBottom();
    return;
  }
  const current = run.current_node ? `正在运行 ${run.current_node}` : "等待工作流启动";
  bubble.innerHTML = `<div class="running-card"><div class="progress-line"><span class="spinner"></span><span>${escapeHtml(current)}</span></div>${renderTracePanel(run.traces, false)}</div>`;
}

function renderActions(run) {
  const patch = run.proposed_project_patch;
  const snapshot = state.runProjectSnapshot;
  if (patch && snapshot) {
    const updates = (patch.updates || []).map((update) => `<label class="writeback-update">
      <input type="checkbox" data-writeback-path="${escapeHtml(update.path)}" checked>
      <span><strong>${escapeHtml(PROJECT_FIELD_LABELS[update.path] || update.path)}</strong><small>${escapeHtml(update.reason)}</small><code>${escapeHtml(formatUpdatePreview(update.proposed_value))}</code></span>
    </label>`).join("") || '<p class="muted">本次没有可自动提取的字段；仍可确认阶段成果。</p>';
    return `<section class="writeback-panel">
      <h3>拟写回项目卡</h3>
      <p>请选择要接受的字段。确认前不会修改项目卡。</p>
      ${patch.warning ? `<div class="warning-box">${escapeHtml(patch.warning)}</div>` : ""}
      <div class="writeback-updates">${updates}</div>
      <div class="result-actions">
        ${patch.next_workflow ? `<button class="primary-button" type="button" data-writeback-action="continue">确认所选并进入 ${escapeHtml(patch.next_workflow.toUpperCase())}</button>` : ""}
        <button class="action-button" type="button" data-writeback-action="save">确认所选并返回项目总览</button>
        <button class="action-button" type="button" data-writeback-action="revise">修改本阶段并重新运行</button>
        <button class="action-button" type="button" data-writeback-action="discard">放弃写回</button>
        <a class="action-button" href="/api/runs/${encodeURIComponent(run.run_id)}/download.md">导出 Markdown</a>
      </div>
    </section>`;
  }
  return `<div class="result-actions">
    <button class="action-button" type="button" onclick="reuseWorkflow()">1、重新使用“${escapeHtml(state.selected?.title || "当前助手") }”</button>
    <button class="action-button" type="button" onclick="showMainMenu()">2、返回主菜单</button>
    <a class="action-button" href="/api/runs/${encodeURIComponent(run.run_id)}/download.md">3、导出 Markdown</a>
    <button class="action-button" type="button" onclick="endConversation()">4、直接结束</button>
  </div>`;
}

function formatUpdatePreview(value) {
  const text = Array.isArray(value)
    ? value.map((item) => typeof item === "string" ? item : `${item.source_id || "材料"} · ${item.display_name || ""}`).join("\n")
    : String(value ?? "");
  return text.length > 500 ? `${text.slice(0, 500)}…` : text;
}

function wireResultActions(bubble, run) {
  bubble.querySelectorAll("[data-writeback-action]").forEach((button) => {
    button.addEventListener("click", () => handleWritebackAction(button.dataset.writebackAction, bubble, run));
  });
}

function handleWritebackAction(action, bubble, run) {
  if (action === "revise") {
    showWorkflowForm(run.workflow_id, true);
    return;
  }
  if (action === "discard") {
    bubble.querySelector(".writeback-panel").innerHTML = '<p class="muted">已放弃本次项目卡写回；研究结果仍可导出。</p>';
    state.runProjectSnapshot = null;
    return;
  }
  const snapshot = state.runProjectSnapshot;
  if (!snapshot) return alert("本次运行没有可写回的项目快照。");
  try {
    if (state.projectStore.persistent) state.projectStore.load();
    const active = state.projectStore.active();
    if (!active || active.project_id !== snapshot.project_id) throw new Error("当前项目已经切换，不能把结果写入另一个项目。");
    const selectedPaths = [...bubble.querySelectorAll("[data-writeback-path]:checked")].map((item) => item.dataset.writebackPath);
    state.projectStore.applyWorkflowResult(active.project_id, {
      workflow_id: run.workflow_id,
      run_id: run.run_id,
      markdown: run.final_markdown || "",
      patch: run.proposed_project_patch,
      selected_paths: selectedPaths,
      expected_revision: snapshot.revision,
    });
    const nextWorkflow = run.proposed_project_patch?.next_workflow;
    state.runProjectSnapshot = null;
    renderProjectSwitcher();
    bubble.querySelector(".writeback-panel").innerHTML = '<div class="success-box">项目卡已按你的选择更新，并记录了修订历史。</div>';
    if (action === "continue" && nextWorkflow) showWorkflowForm(nextWorkflow);
    else openProjectDialog();
  } catch (error) {
    alert(`项目卡写回失败：${error}`);
  }
}

function renderTracePanel(traces, open) {
  const items = (traces || []).map((trace) => {
    const output = typeof trace.output === "string" ? trace.output : JSON.stringify(trace.output, null, 2);
    return `<article class="trace-item">
      <div class="trace-head"><strong>${escapeHtml(trace.legacy_node_id)} · ${escapeHtml(trace.internal_name)}</strong><span class="status-${trace.status}">${escapeHtml(trace.status)}${trace.duration_ms !== null ? ` · ${trace.duration_ms}ms` : ""}</span></div>
      <div class="trace-body">
        ${trace.system_prompt ? `<details><summary>系统提示词</summary><pre>${escapeHtml(trace.system_prompt)}</pre></details>` : ""}
        ${trace.user_prompt ? `<details><summary>用户提示词</summary><pre>${escapeHtml(trace.user_prompt)}</pre></details>` : ""}
        ${trace.output !== null ? `<details><summary>节点输出</summary><pre>${escapeHtml(output || "")}</pre></details>` : ""}
        ${trace.error ? `<div class="error-box">${escapeHtml(trace.error)}</div>` : ""}
      </div>
    </article>`;
  }).join("");
  return `<details class="trace-panel" ${open ? "open" : ""}><summary>开发调试面板 · ${traces.length} 个节点</summary><div class="trace-list">${items || "暂无节点记录"}</div></details>`;
}

function summaryForUser(workflow, fields, file) {
  const lines = Object.entries(fields)
    .filter(([, value]) => String(value).trim())
    .map(([key, value]) => {
      const field = workflow.fields.find((item) => item.name === key);
      const text = String(value);
      return `<strong>${escapeHtml(field?.label || key)}：</strong>${escapeHtml(text.length > 180 ? `${text.slice(0, 180)}…` : text)}`;
    });
  if (file) lines.push(`<strong>上传文件：</strong>${escapeHtml(file.name)}（${formatBytes(file.size)}）`);
  return lines.join("<br>");
}

function reuseWorkflow() {
  if (state.selected) showWorkflowForm(state.selected.id, true);
}

function showMainMenu() {
  if (state.activeRun) return;
  state.selected = null;
  document.querySelector("#page-title").textContent = "选择一位研究助手";
  document.querySelectorAll(".nav-button").forEach((button) => button.classList.remove("active"));
  hideComposer();
  addAssistant("已返回主菜单。请选择下一项任务。", true);
}

function endConversation() {
  hideComposer();
  addAssistant("本次任务已结束。运行记录只保存在当前后端进程内；关闭服务后将被清除。", true);
}

function hideComposer() { composer.hidden = true; }
function setBusy(busy) {
  submitButton.disabled = busy;
  document.querySelectorAll(".menu-card, .nav-button").forEach((button) => { button.disabled = busy; });
  routeButton.disabled = busy || state.routeBusy;
  routeMessage.disabled = busy || state.routeBusy;
  projectSelect.disabled = busy;
  document.querySelector("#new-project-button").disabled = busy;
  renderProjectDialog();
}

function addAssistant(html, plain = false) {
  const article = document.createElement("article");
  article.className = "message assistant";
  article.innerHTML = `<div class="avatar">行</div><div class="bubble">${plain ? `<p>${escapeHtml(html)}</p>` : html}</div>`;
  chat.append(article);
  scrollToBottom();
  return article.querySelector(".bubble");
}

function addUser(html) {
  const article = document.createElement("article");
  article.className = "message user";
  article.innerHTML = `<div class="bubble">${html}</div>`;
  chat.append(article);
  scrollToBottom();
}

function renderMarkdown(markdown) {
  const lines = escapeHtml(markdown).split("\n");
  let html = "";
  let inCode = false;
  let code = [];
  let listType = null;
  const closeList = () => {
    if (listType) html += `</${listType}>`;
    listType = null;
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      closeList();
      if (inCode) {
        html += `<pre><code>${code.join("\n")}</code></pre>`;
        code = [];
      }
      inCode = !inCode;
      continue;
    }
    if (inCode) { code.push(line); continue; }
    if (
      line.includes("|")
      && i + 1 < lines.length
      && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])
    ) {
      closeList();
      const headers = parseTableRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(parseTableRow(lines[i]));
        i += 1;
      }
      i -= 1;
      html += `<table><thead><tr>${headers.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>`;
      rows.forEach((row) => {
        html += `<tr>${row.map((cell) => `<td>${inlineMarkdown(cell)}</td>`).join("")}</tr>`;
      });
      html += "</tbody></table>";
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      html += `<h${level}>${inlineMarkdown(heading[2])}</h${level}>`;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
      html += `<li>${inlineMarkdown(line.replace(/^\s*[-*]\s+/, ""))}</li>`;
      continue;
    }
    if (/^\s*\d+[、.]\s*/.test(line)) {
      if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; }
      html += `<li>${inlineMarkdown(line.replace(/^\s*\d+[、.]\s*/, ""))}</li>`;
      continue;
    }
    closeList();
    if (!line.trim()) { html += "<br>"; continue; }
    if (line.startsWith("&gt;")) html += `<blockquote>${inlineMarkdown(line.slice(4))}</blockquote>`;
    else html += `<p>${inlineMarkdown(line)}</p>`;
  }
  closeList();
  if (inCode) html += `<pre><code>${code.join("\n")}</code></pre>`;
  return html;
}

function inlineMarkdown(value) {
  return value
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

function parseTableRow(line) {
  return line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((cell) => cell.trim());
}

async function fetchJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await errorFromResponse(response));
  return response.json();
}

async function errorFromResponse(response) {
  try {
    const body = await response.json();
    return body.detail || JSON.stringify(body);
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function delay(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }
function scrollToBottom() { window.setTimeout(() => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }), 30); }
function formatBytes(bytes) { return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }

window.reuseWorkflow = reuseWorkflow;
window.showMainMenu = showMainMenu;
window.endConversation = endConversation;
