(function attachProjectStore(root, factory) {
  const api = factory(root);
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.XingxiaodaoProjects = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function projectStoreFactory(globalRoot) {
  "use strict";

  const STORAGE_KEY = "xingxiaodao.projects.v1";
  const STORAGE_SCHEMA = 1;
  const PROJECT_SCHEMA = 1;
  const MAX_PROJECT_CHARS = 1_000_000;
  const MAX_CONTAINER_CHARS = 2_000_000;
  const MAX_STAGE_MARKDOWN = 50_000;
  const MAX_HISTORY = 200;
  const WORKFLOWS = ["w1", "w2", "w3", "w4"];
  const STAGES = {
    w1: "w1_confirmed",
    w2: "w2_confirmed",
    w3: "w3_confirmed",
    w4: "w4_audited",
  };
  const NEXT = { w1: "w2", w2: "w3", w3: "w4", w4: null };
  const ARRAY_FIELDS = new Set([
    "materials", "candidate_codes", "candidate_themes", "candidate_claims",
    "unresolved_decisions",
  ]);
  const EDITABLE_FIELDS = new Set([
    "research_question", "target_population", "research_context", "method_plan",
    "interview_guide", "materials", "candidate_codes", "candidate_themes",
    "candidate_claims", "audit_status", "audit_notes", "unresolved_decisions",
  ]);
  const INVALIDATION = {
    research_question: ["w2", "w3", "w4"],
    target_population: ["w2", "w3", "w4"],
    research_context: ["w2", "w3", "w4"],
    method_plan: ["w2", "w3", "w4"],
    interview_guide: ["w3", "w4"],
    materials: ["w4"],
    candidate_codes: ["w4"],
    candidate_themes: ["w4"],
    candidate_claims: ["w4"],
  };

  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function now() { return new Date().toISOString(); }
  function uniqueStrings(values, limit) {
    return [...new Set((Array.isArray(values) ? values : []).map((item) => String(item).trim()).filter(Boolean))].slice(0, limit);
  }
  function projectId() {
    if (globalRoot.crypto?.randomUUID) return `P-${globalRoot.crypto.randomUUID()}`;
    return `P-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
  function emptyProject(name, id = projectId()) {
    const timestamp = now();
    return {
      schema_version: PROJECT_SCHEMA,
      project_id: id,
      project_name: String(name || "未命名项目").trim().slice(0, 200) || "未命名项目",
      created_at: timestamp,
      updated_at: timestamp,
      revision: 0,
      research_question: "",
      target_population: "",
      research_context: "",
      method_plan: "",
      interview_guide: "",
      materials: [],
      candidate_codes: [],
      candidate_themes: [],
      candidate_claims: [],
      audit_status: "",
      audit_notes: "",
      current_stage: "draft",
      confirmed_stages: [],
      stale_stages: [],
      unresolved_decisions: [],
      stage_results: {},
      stage_result_history: [],
      revision_history: [],
    };
  }
  function normalizeMaterial(item) {
    if (!item || typeof item !== "object") throw new Error("材料卡必须是对象。");
    const sourceId = String(item.source_id || "").trim().slice(0, 200);
    const displayName = String(item.display_name || "").trim().slice(0, 300);
    if (!sourceId || !displayName) throw new Error("材料卡缺少编号或显示名。");
    return {
      source_id: sourceId,
      display_name: displayName,
      source_type: String(item.source_type || "").slice(0, 100),
      source_context: String(item.source_context || "").slice(0, 4000),
      size_bytes: Math.max(0, Number(item.size_bytes) || 0),
      character_count: Math.max(0, Number(item.character_count) || 0),
      sha256: String(item.sha256 || "").slice(0, 64),
      summary: String(item.summary || "").slice(0, 4000),
    };
  }
  function normalizeProject(input, { importCopy = false } = {}) {
    if (!input || typeof input !== "object" || Number(input.schema_version) !== PROJECT_SCHEMA) {
      throw new Error("项目卡 Schema 版本不受支持。");
    }
    const project = emptyProject(input.project_name, importCopy ? projectId() : String(input.project_id || ""));
    if (!project.project_id) throw new Error("项目卡缺少 project_id。 ");
    const textFields = [
      "research_question", "target_population", "research_context", "method_plan",
      "interview_guide", "audit_status", "audit_notes",
    ];
    textFields.forEach((field) => { project[field] = String(input[field] || "").slice(0, 50_000); });
    project.materials = (Array.isArray(input.materials) ? input.materials : []).slice(0, 50).map(normalizeMaterial);
    project.candidate_codes = uniqueStrings(input.candidate_codes, 200);
    project.candidate_themes = uniqueStrings(input.candidate_themes, 100);
    project.candidate_claims = uniqueStrings(input.candidate_claims, 100);
    project.unresolved_decisions = uniqueStrings(input.unresolved_decisions, 100);
    project.current_stage = Object.values(STAGES).includes(input.current_stage) ? input.current_stage : "draft";
    project.confirmed_stages = uniqueStrings(input.confirmed_stages, 4).filter((item) => WORKFLOWS.includes(item));
    project.stale_stages = uniqueStrings(input.stale_stages, 4).filter((item) => WORKFLOWS.includes(item));
    project.stage_results = {};
    Object.entries(input.stage_results || {}).forEach(([workflow, result]) => {
      if (!WORKFLOWS.includes(workflow) || !result || typeof result !== "object") return;
      project.stage_results[workflow] = {
        run_id: String(result.run_id || "").slice(0, 100),
        markdown: String(result.markdown || "").slice(0, MAX_STAGE_MARKDOWN),
        confirmed_at: String(result.confirmed_at || "").slice(0, 50),
        warning: String(result.warning || "").slice(0, 2000),
      };
    });
    project.stage_result_history = (Array.isArray(input.stage_result_history) ? input.stage_result_history : []).slice(-12).flatMap((entry) => {
      if (!entry || !WORKFLOWS.includes(entry.workflow_id)) return [];
      return [{
        workflow_id: entry.workflow_id,
        run_id: String(entry.run_id || "").slice(0, 100),
        markdown: String(entry.markdown || "").slice(0, MAX_STAGE_MARKDOWN),
        confirmed_at: String(entry.confirmed_at || "").slice(0, 50),
        warning: String(entry.warning || "").slice(0, 2000),
      }];
    });
    project.revision_history = (Array.isArray(input.revision_history) ? input.revision_history : []).slice(-MAX_HISTORY).map((entry) => ({
      at: String(entry.at || "").slice(0, 50),
      source: String(entry.source || "").slice(0, 100),
      workflow_id: WORKFLOWS.includes(entry.workflow_id) ? entry.workflow_id : null,
      path: String(entry.path || "").slice(0, 100),
      old_value: entry.old_value ?? null,
      new_value: entry.new_value ?? null,
    }));
    project.revision = Math.max(0, Number(input.revision) || 0);
    project.created_at = String(input.created_at || project.created_at).slice(0, 50);
    project.updated_at = String(input.updated_at || project.updated_at).slice(0, 50);
    if (importCopy) project.project_name = `${project.project_name}（导入副本）`.slice(0, 200);
    if (JSON.stringify(project).length > MAX_PROJECT_CHARS) throw new Error("单个项目超过 1MB 限制。");
    return project;
  }
  function contextFromProject(project) {
    if (!project) return null;
    return {
      schema_version: 1,
      project_id: project.project_id,
      project_name: project.project_name,
      revision: project.revision,
      research_question: project.research_question,
      target_population: project.target_population,
      research_context: project.research_context,
      method_plan: project.method_plan,
      interview_guide: project.interview_guide,
      materials: clone(project.materials),
      candidate_codes: clone(project.candidate_codes),
      candidate_themes: clone(project.candidate_themes),
      candidate_claims: clone(project.candidate_claims),
      audit_status: project.audit_status,
      audit_notes: project.audit_notes,
      current_stage: project.current_stage,
      unresolved_decisions: clone(project.unresolved_decisions),
    };
  }
  function mergeMaterials(oldItems, newItems) {
    const map = new Map(oldItems.map((item) => [`${item.source_id}:${item.sha256}`, item]));
    newItems.map(normalizeMaterial).forEach((item) => map.set(`${item.source_id}:${item.sha256}`, item));
    return [...map.values()].slice(-50);
  }
  function invalidatedBy(paths) {
    return uniqueStrings(paths.flatMap((path) => INVALIDATION[path] || []), 4);
  }

  class BrowserProjectStore {
    constructor(storage) {
      this.storage = storage;
      this.persistent = Boolean(storage);
      this.warning = "";
      this.container = { storage_schema: STORAGE_SCHEMA, active_project_id: null, projects: {} };
      this.load();
    }
    load() {
      if (!this.storage) return;
      try {
        const raw = this.storage.getItem(STORAGE_KEY);
        if (!raw) return;
        const parsed = JSON.parse(raw);
        if (parsed.storage_schema !== STORAGE_SCHEMA || typeof parsed.projects !== "object") {
          throw new Error("浏览器项目容器版本不受支持。");
        }
        const projects = {};
        Object.values(parsed.projects).forEach((item) => {
          const normalized = normalizeProject(item);
          projects[normalized.project_id] = normalized;
        });
        this.container = {
          storage_schema: STORAGE_SCHEMA,
          active_project_id: projects[parsed.active_project_id] ? parsed.active_project_id : null,
          projects,
        };
      } catch (error) {
        this.persistent = false;
        this.warning = `浏览器项目卡读取失败，已进入临时内存模式：${error}`;
      }
    }
    persist() {
      const serialized = JSON.stringify(this.container);
      if (serialized.length > MAX_CONTAINER_CHARS) throw new Error("全部项目已接近浏览器 4MB 安全容量上限。");
      if (!this.storage || !this.persistent) return;
      try {
        this.storage.setItem(STORAGE_KEY, serialized);
      } catch (error) {
        this.persistent = false;
        this.warning = `浏览器存储不可用，后续修改仅保存在当前页面：${error}`;
      }
    }
    list() {
      return Object.values(this.container.projects).sort((a, b) => b.updated_at.localeCompare(a.updated_at)).map(clone);
    }
    active() {
      const project = this.container.projects[this.container.active_project_id];
      return project ? clone(project) : null;
    }
    create(name) {
      const project = emptyProject(name);
      this.container.projects[project.project_id] = project;
      this.container.active_project_id = project.project_id;
      this.persist();
      return clone(project);
    }
    switch(projectIdValue) {
      if (!this.container.projects[projectIdValue]) throw new Error("项目不存在。");
      this.container.active_project_id = projectIdValue;
      this.persist();
      return this.active();
    }
    deactivate() {
      this.container.active_project_id = null;
      this.persist();
    }
    rename(projectIdValue, name) {
      const project = this.container.projects[projectIdValue];
      if (!project) throw new Error("项目不存在。");
      const nextName = String(name || "").trim().slice(0, 200);
      if (!nextName) throw new Error("项目名称不能为空。");
      project.project_name = nextName;
      project.updated_at = now();
      project.revision += 1;
      this.persist();
      return clone(project);
    }
    remove(projectIdValue) {
      if (!this.container.projects[projectIdValue]) return;
      delete this.container.projects[projectIdValue];
      if (this.container.active_project_id === projectIdValue) {
        this.container.active_project_id = this.list()[0]?.project_id || null;
      }
      this.persist();
    }
    applyManual(projectIdValue, values, expectedRevision) {
      const existing = this.container.projects[projectIdValue];
      if (!existing) throw new Error("项目不存在。");
      if (existing.revision !== expectedRevision) throw new Error("项目已在其他页面发生变化，请刷新后重试。");
      const project = clone(existing);
      const changedPaths = [];
      Object.entries(values).forEach(([path, rawValue]) => {
        if (!EDITABLE_FIELDS.has(path) || path === "materials") return;
        const nextValue = ARRAY_FIELDS.has(path) ? uniqueStrings(rawValue, 200) : String(rawValue || "").slice(0, 50_000);
        if (JSON.stringify(project[path]) === JSON.stringify(nextValue)) return;
        project.revision_history.push({ at: now(), source: "manual", workflow_id: null, path, old_value: clone(project[path]), new_value: clone(nextValue) });
        project[path] = nextValue;
        changedPaths.push(path);
      });
      const newlyStale = invalidatedBy(changedPaths).filter((item) => project.confirmed_stages.includes(item));
      project.stale_stages = uniqueStrings([...project.stale_stages, ...newlyStale], 4);
      project.revision_history = project.revision_history.slice(-MAX_HISTORY);
      if (changedPaths.length) {
        project.revision += 1;
        project.updated_at = now();
        if (JSON.stringify(project).length > MAX_PROJECT_CHARS) throw new Error("本次修改会使项目超过 1MB 限制，未保存。");
        this.container.projects[projectIdValue] = project;
        try { this.persist(); } catch (error) {
          this.container.projects[projectIdValue] = existing;
          throw error;
        }
      }
      return clone(project);
    }
    applyWorkflowResult(projectIdValue, payload) {
      const existing = this.container.projects[projectIdValue];
      if (!existing) throw new Error("项目不存在。");
      if (existing.revision !== payload.expected_revision) throw new Error("运行期间项目卡已变化，不能静默覆盖；请重新确认。");
      const project = clone(existing);
      const workflow = payload.workflow_id;
      if (!WORKFLOWS.includes(workflow)) throw new Error("未知工作流。");
      const selected = new Set(payload.selected_paths || []);
      const changedPaths = [];
      (payload.patch?.updates || []).forEach((update) => {
        const path = update.path;
        if (!selected.has(path) || !EDITABLE_FIELDS.has(path)) return;
        let nextValue = update.proposed_value;
        if (path === "materials") nextValue = mergeMaterials(project.materials, Array.isArray(nextValue) ? nextValue : []);
        else if (ARRAY_FIELDS.has(path)) nextValue = uniqueStrings(nextValue, 200);
        else nextValue = String(nextValue || "").slice(0, 50_000);
        if (JSON.stringify(project[path]) === JSON.stringify(nextValue)) return;
        project.revision_history.push({ at: now(), source: "workflow", workflow_id: workflow, path, old_value: clone(project[path]), new_value: clone(nextValue) });
        project[path] = nextValue;
        changedPaths.push(path);
      });
      if (project.stage_results[workflow]) {
        project.stage_result_history.push({
          workflow_id: workflow,
          ...clone(project.stage_results[workflow]),
        });
        project.stage_result_history = project.stage_result_history.slice(-12);
      }
      project.stage_results[workflow] = {
        run_id: String(payload.run_id || "").slice(0, 100),
        markdown: String(payload.markdown || "").slice(0, MAX_STAGE_MARKDOWN),
        confirmed_at: now(),
        warning: String(payload.patch?.warning || "").slice(0, 2000),
      };
      project.confirmed_stages = uniqueStrings([...project.confirmed_stages, workflow], 4);
      const newlyStale = invalidatedBy(changedPaths).filter(
        (item) => item !== workflow && project.confirmed_stages.includes(item),
      );
      project.stale_stages = uniqueStrings(
        [...project.stale_stages.filter((item) => item !== workflow), ...newlyStale],
        4,
      );
      project.current_stage = STAGES[workflow];
      project.revision_history.push({ at: now(), source: "workflow_confirmation", workflow_id: workflow, path: "current_stage", old_value: null, new_value: project.current_stage });
      project.revision_history = project.revision_history.slice(-MAX_HISTORY);
      project.revision += 1;
      project.updated_at = now();
      if (JSON.stringify(project).length > MAX_PROJECT_CHARS) throw new Error("本次写回会使项目超过 1MB 限制，未保存。");
      this.container.projects[projectIdValue] = project;
      try { this.persist(); } catch (error) {
        this.container.projects[projectIdValue] = existing;
        throw error;
      }
      return clone(project);
    }
    exportProject(projectIdValue) {
      const project = this.container.projects[projectIdValue];
      if (!project) throw new Error("项目不存在。");
      return JSON.stringify({ storage_schema: STORAGE_SCHEMA, exported_at: now(), project: clone(project) }, null, 2);
    }
    importProject(serialized) {
      if (String(serialized).length > MAX_PROJECT_CHARS) throw new Error("导入文件超过 1MB 限制。");
      const parsed = JSON.parse(serialized);
      if (parsed.storage_schema !== STORAGE_SCHEMA || !parsed.project) throw new Error("不是行小道 v1 项目导出文件。");
      const project = normalizeProject(parsed.project, { importCopy: true });
      this.container.projects[project.project_id] = project;
      this.container.active_project_id = project.project_id;
      this.persist();
      return clone(project);
    }
    context() { return contextFromProject(this.active()); }
    nextWorkflow(workflow) { return NEXT[workflow] || null; }
  }

  return {
    STORAGE_KEY,
    BrowserProjectStore,
    emptyProject,
    normalizeProject,
    contextFromProject,
    invalidatedBy,
  };
}));
