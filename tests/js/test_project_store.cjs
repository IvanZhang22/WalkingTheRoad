const test = require("node:test");
const assert = require("node:assert/strict");
const {
  STORAGE_KEY,
  BrowserProjectStore,
} = require("../../app/static/project-store.js");

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
}

function proposal(workflow, path, value) {
  const stage = { w1: "w1_confirmed", w2: "w2_confirmed", w3: "w3_confirmed", w4: "w4_audited" }[workflow];
  const next = { w1: "w2", w2: "w3", w3: "w4", w4: null }[workflow];
  return {
    workflow_id: workflow,
    updates: [{ path, proposed_value: value, reason: "测试" }],
    stage_after_confirmation: stage,
    next_workflow: next,
    warning: "",
  };
}

function apply(store, workflow, path, value) {
  const project = store.active();
  return store.applyWorkflowResult(project.project_id, {
    workflow_id: workflow,
    run_id: `run-${workflow}`,
    markdown: `# ${workflow}`,
    patch: proposal(workflow, path, value),
    selected_paths: [path],
    expected_revision: project.revision,
  });
}

test("项目创建后可从同一浏览器存储恢复", () => {
  const storage = new MemoryStorage();
  const store = new BrowserProjectStore(storage);
  const created = store.create("社区助餐研究");
  const restored = new BrowserProjectStore(storage).active();
  assert.equal(restored.project_id, created.project_id);
  assert.equal(restored.project_name, "社区助餐研究");
  assert.ok(storage.getItem(STORAGE_KEY));
});

test("四阶段确认及上游修改会使已完成下游过期", () => {
  const store = new BrowserProjectStore(new MemoryStorage());
  store.create("串联测试");
  apply(store, "w1", "research_question", "问题A");
  apply(store, "w2", "interview_guide", "提纲A");
  apply(store, "w3", "candidate_claims", ["结论A"]);
  apply(store, "w4", "audit_status", "已质检");
  assert.deepEqual(store.active().confirmed_stages, ["w1", "w2", "w3", "w4"]);
  apply(store, "w1", "research_question", "问题B");
  assert.deepEqual(store.active().stale_stages.sort(), ["w2", "w3", "w4"]);
  assert.equal(store.active().stage_result_history.length, 1);
  assert.equal(store.active().stage_result_history[0].workflow_id, "w1");
});

test("运行期间修订号变化时拒绝静默覆盖", () => {
  const store = new BrowserProjectStore(new MemoryStorage());
  const project = store.create("并发测试");
  store.rename(project.project_id, "已经修改");
  assert.throws(() => store.applyWorkflowResult(project.project_id, {
    workflow_id: "w1",
    run_id: "old-run",
    markdown: "旧结果",
    patch: proposal("w1", "research_question", "旧问题"),
    selected_paths: ["research_question"],
    expected_revision: project.revision,
  }), /已变化/);
  assert.equal(store.active().research_question, "");
});

test("材料卡会过滤原始正文并按编号和哈希合并", () => {
  const store = new BrowserProjectStore(new MemoryStorage());
  store.create("材料测试");
  const material = {
    source_id: "PACK-A", display_name: "访谈.txt", source_type: "单份访谈",
    size_bytes: 100, character_count: 50, sha256: "a".repeat(64), source_text: "不得保存",
  };
  apply(store, "w3", "materials", [material]);
  const saved = store.active().materials[0];
  assert.equal(saved.source_id, "PACK-A");
  assert.equal("source_text" in saved, false);
});

test("导出后导入会生成隔离副本", () => {
  const store = new BrowserProjectStore(new MemoryStorage());
  const original = store.create("导出测试");
  apply(store, "w1", "research_question", "研究问题");
  const imported = store.importProject(store.exportProject(original.project_id));
  assert.notEqual(imported.project_id, original.project_id);
  assert.match(imported.project_name, /导入副本/);
  assert.equal(store.list().length, 2);
});

test("损坏或超版本项目拒绝导入", () => {
  const store = new BrowserProjectStore(new MemoryStorage());
  assert.throws(() => store.importProject("not json"));
  assert.throws(() => store.importProject(JSON.stringify({ storage_schema: 99, project: {} })), /不是行小道/);
});

test("localStorage 写入失败时降级到当前页面内存", () => {
  const storage = new MemoryStorage();
  storage.setItem = () => { throw new Error("quota"); };
  const store = new BrowserProjectStore(storage);
  const project = store.create("临时项目");
  assert.equal(store.persistent, false);
  assert.equal(store.active().project_id, project.project_id);
  assert.match(store.warning, /当前页面/);
});
