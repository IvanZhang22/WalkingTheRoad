# Changelog

所有重要变更按版本记录。本项目在当前阶段使用三段式版本号。

## [Unreleased]

### Added

- v2.2 多模态 `text`、`input_audio`、`file` 消息契约；
- 厂商无关的 ASR、OCR、文档解析接口和统一材料/片段模型；
- 无密钥 Mock Provider、多附件故障隔离及低置信/定位缺失门控；
- 同一 `/v1/chat/completions` 的多模态预检摘要，不回显附件 URL 或材料全文。

### Security

- 拒绝 data URL、本地路径、显式内网/元数据 IP、URL 凭据和路径穿越文件名；
- Live 环境没有真实 Provider 时失败关闭，不使用 Mock 内容冒充真实识别。

### Known limitations

- 本阶段只有请求契约和 Mock，尚未接入远程下载、阶跃 ASR、真实 OCR 或 W3 正式分析；
- 视频不在 v2.2 范围内。

## [2.1.0] - 2026-08-05

### Added

- Vercel Preview 部署、环境变量和公开健康检查；
- 线上 `GET /v1/models`、Bearer 鉴权和真实 `POST /v1/chat/completions` 验证；
- 显式 UTF-8 JSON 响应，修复 OpenAI 兼容入口中文输出；
- 国内部署与清小搭接入失败的可复现记录。

### Known limitations

- 清小搭服务端无法访问境外 Vercel 地址，连通性检测返回 `SocketException`；
- 本版只完成境外预览部署，不宣称已经接入或上线清小搭；
- 标准 Chat 入口当前只完成意图识别和工作流推荐，尚未开放多模态输入。

## [2.0.0] - 2026-08-05

### Added

- OpenAI 兼容的 `GET /v1/models` 与 `POST /v1/chat/completions`；
- 独立 `AGENT_API_KEY` Bearer 鉴权，和模型供应商密钥完全隔离；
- 标准非流式响应、SSE role/content/stop/[DONE] 帧与最小探测响应；
- 协议契约测试，以及 Vercel/清小搭部署与验收说明；
- Vercel Python Function 入口与 120 秒函数配置。

### Unchanged

- 四工作流、项目卡、模型提示词和业务输出未改动；
- 尚未部署到 Vercel、接入清小搭或开放文件/音频输入。

## [1.4.0] - 2026-08-05

### Added

- GitHub Actions CI：仓库安全、Ruff、Mypy、Pytest、Node项目卡测试和三组Mock回归；
- 标签发布工作流：版本一致性检查、安全源码包、SHA-256和GitHub Release；
- Pull Request、Bug和功能建议模板；
- 仓库安全检查、统一质量门和发布包构建脚本；
- 团队协作、安全、首次建库、发布和回滚说明。

### Changed

- 版本统一升级到 v1.4.0；
- `.gitignore` 扩大到本地数据、运行结果、构建产物和编辑器配置；
- 工程目录作为独立 Git 仓库根目录，不包含赛事说明、团队 Word 或根目录其他资料。

### Unchanged

- W1—W4业务逻辑、v1.2意图路由和v1.3浏览器项目卡保持不变；
- 不增加知识库、云部署、OpenAI兼容协议或多模态能力。

## [1.3.0] - 2026-08-05

- 增加浏览器项目卡、结构化人工写回、四工作流串联、修订记录和过期状态。

## [1.2.0] - 2026-08-05

- 增加自由描述意图识别、工作流推荐和人工纠正。

## [1.1.0] - 2026-08-04

- 完成四工作流本地全代码迁移、文档解析、结构校验和W3/W4确定性引文核验。
