# 行小道 Agent v3.4.2

面向大学生社会实践的研究协作智能体。它支持研究设计、访谈设计、质性材料分析和研究质量核查，并以清小搭所需的 OpenAI 兼容接口对外提供服务。

## 一、当前能力

1、文本与常见文档：TXT、Markdown、Word、PDF、Excel。

2、图片与扫描件：通过百度 OCR 提取文字；是否可在清小搭中使用以平台实测为准。

3、音频：清小搭上传后进入后台任务，服务器将音频归一化为 16kHz 单声道、约 5 分钟分段，并通过 StepFun SSE 转写。用户可在对话中发送“查看进度”“查看首段转写”“继续处理”“取消任务”。原始音频默认保留 24 小时，任务文本和状态保留 30 天。

4、研究边界：不会编造访谈、原始引文、数据或研究结论。音频首段核对只是抽样核对，不替代对整份材料的人工复核。

## 二、服务接口

- `GET /api/health`：服务、模型、OCR、ASR 队列状态。
- `GET /v1/models`：OpenAI 兼容模型列表，Bearer 鉴权。
- `POST /v1/chat/completions`：清小搭对话入口，支持流式输出与文本/附件消息。
- `GET /api/audio-jobs/{job_id}`：受 Bearer 鉴权保护的音频后台任务状态。

## 三、服务器部署

腾讯云正式部署使用 Nginx + systemd：Nginx 对外提供 HTTPS，应用监听 `127.0.0.1:8000`，服务名为 `xingxiaodao.service`。

每次部署均上传对应版本的 ZIP 和 `upgrade-vX.Y.Z.sh` 到 `/home/ubuntu/`，再执行：

```bash
sudo bash /home/ubuntu/upgrade-vX.Y.Z.sh
```

脚本会继承生产 `.env`、创建旧版本备份，并在健康检查失败时恢复旧版本。

## 四、必要环境变量

```text
MODEL_PROVIDER=stepfun
MODEL_API_KEY=服务端模型密钥
AGENT_API_KEY=清小搭接入密钥

ASR_PROVIDER=stepfun_sse
STEPFUN_ASR_API_KEY=可留空；MODEL_PROVIDER=stepfun 时复用 MODEL_API_KEY
STEPFUN_ASR_SSE_BASE_URL=https://api.stepfun.com/step_plan/v1
STEPFUN_ASR_SSE_MODEL=stepaudio-2.5-asr

OCR_PROVIDER=baidu
BAIDU_OCR_API_KEY=百度OCR API Key
BAIDU_OCR_SECRET_KEY=百度OCR Secret Key
```

真实密钥只能保存在服务器 `.env`，不得提交仓库或发送到聊天中。

## 五、本地质量检查

```powershell
python -m pytest -q
python -m ruff check app tests
python -m mypy app
```
