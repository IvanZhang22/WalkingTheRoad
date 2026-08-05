# 行小道本地 Agent v2.0.0

v2.0.0 保留 v1.4.0 的协作与发布基线，并新增清小搭所需的 OpenAI 兼容接口：独立 Bearer 鉴权、`/v1/models`、`/v1/chat/completions` 与标准 SSE 流式响应。本版先完成文本协议与意图识别，不宣称已上线或支持多模态。

项目卡只保存在当前浏览器，不是数据库。上传文件正文、API Key 和完整节点轨迹不会进入项目卡。

## 一、最快启动方法

1、双击 `启动行小道.bat`。

2、首次运行会创建 `.venv`、安装依赖并生成 `.env`。

3、在自动打开的 `.env` 中填写：

```text
MODEL_API_KEY=你的阶跃星辰STEP_API_KEY
```

4、保存后回到命令窗口按回车。Chrome 会自动打开：

```text
http://127.0.0.1:8000
```

5、停止服务时，在命令窗口按 `Ctrl+C`。

API Key 只能写在 `.env`，不要发到聊天、截图或提交到 GitHub。

## 二、命令行启动方法

在 PowerShell 中进入本目录：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
notepad .env
python run_local.py
```

如果 PowerShell 不允许激活脚本，可以不激活，直接使用：

```powershell
.\.venv\Scripts\python.exe run_local.py
```

## 三、当前四条工作流

1、W1 研究设计助手

```text
1I-1-1 → 3L-1-1 → 3L-1-2 → 2O-1-1 → 9E-1-1
```

2、W2 访谈设计助手

```text
从零生成：2O-2-1 → 1I-2-1 → 3L-2-1 → 3L-2-2 → 2O-2-2 → 9E-2-2
已有审查：2O-2-1 → 1I-2-2 ─────────→ 3L-2-2 → 2O-2-2 → 9E-2-2
```

3、W3 质性材料分析

```text
1I-3-1 → 3L-3-1 → 7C-3-1 → 3L-3-2 → 2O-3-1 → 9E-3-1
```

4、W4 研究质量质检

```text
1I-4-1 → 3L-4-1 → 7C-4-1 → 3L-4-2 → 2O-4-1 → 9E-4-1
```

W3、W4 的 `7C` 节点只接受原文完全匹配或仅空白差异匹配。模型改写、拼接或虚构的引文会被清空并加入拒绝清单。

活动项目模式会在各工作流最终输出前增加 `3L-1-3`、`3L-2-3`、`3L-3-3` 或 `3L-4-3` 项目卡写回建议节点。临时单次模式不会调用该节点，也不会增加对应 API 用量。

## 四、项目卡与串联

1、在左侧“当前项目”区域新建项目，或继续使用“临时单次模式”。

2、项目模式下，W1—W4 表单会预填已经确认的项目字段；文件输入始终需要重新上传。

3、工作流完成后，逐项勾选需要写回的字段，再选择进入下一步或返回项目总览。

4、修改上游字段时，已经完成的下游阶段会标记为“需要重新生成”；旧结果保留用于比较。

5、项目卡支持切换、重命名、删除、导出和导入。导入时总是生成隔离副本。

## 五、模型配置

默认配置是阶跃星辰 Coding Plan：

```text
MODEL_PROVIDER=stepfun
MODEL_BASE_URL=https://api.stepfun.com/step_plan/v1
MODEL_NAME=step-router-v1
APP_MODE=live
```

`step-router-v1` 是 Coding Plan 的智能路由模型，会在复杂请求与高频请求之间自动选择合适引擎。`.env` 下半部分保留了 DeepSeek 备用块；切换时只保留其中一组未注释的 `MODEL_*` 配置即可。

开发时可将 `APP_MODE=mock`，这样不会调用真实 API，也不会产生费用。模拟结果只能用于检查工程链路，不能评价 Agent 的研究能力。

## 六、质量门禁与测试命令

提交代码前优先运行完整质量门禁：

```powershell
.\.venv\Scripts\python.exe scripts\run_quality_gate.py
```

它会依次检查敏感文件和大文件、Python 规范与类型、后端测试、浏览器项目卡测试、前端语法，以及三组模拟回归；全程不调用付费模型。

运行不产生 API 费用的自动测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
node --test tests\js\test_project_store.cjs
```

检查代码规范：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy app
```

运行 11 例模拟回归：

```powershell
.\.venv\Scripts\python.exe scripts\run_regression.py
```

运行 40 例意图路由回归：

```powershell
.\.venv\Scripts\python.exe scripts\run_routing_regression.py
```

运行四工作流项目卡写回回归：

```powershell
.\.venv\Scripts\python.exe scripts\run_project_regression.py
```

运行 11 例真实模型回归，会使用 `.env` 当前启用的服务商并产生 API 费用：

```powershell
.\.venv\Scripts\python.exe scripts\run_regression.py --live
.\.venv\Scripts\python.exe scripts\run_routing_regression.py --live
.\.venv\Scripts\python.exe scripts\run_project_regression.py --live
```

结果保存在 `test-results/mock/` 或 `test-results/live/`。每例的输入、逐节点输出和最终回答都保持原样。

## 七、后端接口

- `GET /api/health`：配置与模型状态；
- `GET /api/workflows`：四条工作流及字段定义；
- `POST /api/route`：自由描述任务并获得工作流推荐，不创建运行记录；
- `POST /api/runs`：创建一次运行；项目模式可附带白名单化的 `project_context_json`；
- `GET /api/runs/{run_id}`：轮询节点进度和结果；
- `GET /api/runs/{run_id}/download.md`：下载最终 Markdown；
- `GET /docs`：FastAPI 自动生成的接口调试页。

本地页面继续使用 `/api/*` 接口。面向清小搭的标准接口为：

- `GET /v1/models`：Bearer 鉴权后的模型列表；
- `POST /v1/chat/completions`：文本消息意图识别与工作流引导；
- `stream: true`：返回标准 Server-Sent Events，最后依次给出 stop 帧和 `[DONE]`；
- `max_tokens: 1`：返回最小探测响应，不调用模型。

调用标准接口时使用 `AGENT_API_KEY`；模型服务使用 `MODEL_API_KEY`。两者必须不同。完整契约与部署准备见 `docs/v2.0.0-协议与验收.md` 和 `docs/v2.1.0-Vercel与清小搭接入.md`。

## 八、数据与版本边界

- 不使用数据库，不创建用户账号；
- 上传文件只存在于本次请求内，不写入项目目录；
- 运行记录保存在后端内存，服务重启即清空；
- 项目卡使用当前浏览器 `localStorage`，不跨浏览器或设备同步；
- 单项目上限约 1MB、全部项目安全预算约 4MB，存储失败时降级为当前页面临时项目；
- 项目卡只保存材料编号、文件名、类型、摘要、字符数和哈希，不保存原始附件；
- PDF 仅支持文字层，扫描件、图片和音频留到 v2.2；
- 同一浏览器同时只运行一条工作流；
- Markdown 仍是单次运行的正式成果导出格式；项目整体可另行导出 JSON。

## 九、GitHub 协作与发布

- 首次建立私库：见 `docs/GitHub首次建库操作.md`；
- 分支、提交和评审规则：见 `CONTRIBUTING.md`；
- v1.4.0 发布、验收与回滚：见 `docs/v1.4.0-协作发布与回滚.md`；
- 本地安全检查：`.\.venv\Scripts\python.exe scripts\check_repository_safety.py`；
- 生成源码发布包：`.\.venv\Scripts\python.exe scripts\build_release.py`。

GitHub Actions 会在 Pull Request 和 `main` 更新时自动执行完整质量门；推送与 `pyproject.toml` 一致的 `v*` 标签时，会再次验收并创建带 SHA-256 校验文件的 GitHub Release。

v1.1.0—v1.4.0 历史说明继续保留；v1.3.0 功能搭建见 `docs/v1.3.0-搭建与验收.md`，v1.4.0 工程化升级见 `docs/v1.4.0-协作发布与回滚.md`。
