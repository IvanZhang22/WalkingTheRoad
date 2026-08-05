# 行小道 GitHub 私库首次建库操作

## 一、当前本机状态

Git 已安装在 `D:\Program Files\Git\cmd\git.exe`，但没有加入系统 PATH；本项目脚本会直接找到该路径。GitHub CLI 尚未安装，因此私库需要先在网页创建。

## 二、网页创建私库

1. 登录你自己的 GitHub；
2. 点击 New repository；
3. Repository name 建议填写 `xingxiaodao-agent`；
4. Visibility 必须选择 Private；
5. 不勾选 README、`.gitignore` 或 License，避免首次推送产生冲突；
6. 创建后复制 HTTPS 仓库地址，不要复制含 Token 的地址。

## 三、首次推送

在项目目录右键运行 PowerShell：

```powershell
.\scripts\连接GitHub私库.ps1 -RepositoryUrl "https://github.com/你的账号/xingxiaodao-agent.git"
```

脚本只在不存在 `origin` 时添加远端，然后推送 `main` 和 `v1.4.0`。如果 Git 弹出浏览器登录窗口，按 Git Credential Manager 的正常流程登录，不要把 Token 写入项目文件。

## 四、邀请与权限

按以下顺序邀请：马俊博、岳承鑫、马仲琪、张一凡。默认只给完成任务所需的最小权限。尚未配置 Git 或不参与代码修改的成员可以先通过 Issue/PR评论参与，不必直接写 `main`。

不要在仓库文档中记录个人邮箱、密码或 Token。

## 五、main保护规则

在 Settings → Branches 或 Rulesets 为 `main` 设置：

- 合并前必须通过 Pull Request；
- 至少一次批准；
- 必须通过状态检查 `quality-gate`；
- 分支必须与最新 `main` 同步；
- 禁止 force push 和删除 `main`；
- 张一凡也遵守同一合并流程。

如果当前 GitHub 套餐无法强制某项保护，团队规则仍禁止直接提交；每次合并前保存PR和Actions通过记录。

## 六、首次核查

1. Actions 中 CI 为绿色；
2. Release 工作流识别 `v1.4.0` 标签并生成ZIP与SHA-256；
3. GitHub文件列表中没有 `.env`、`.venv`、`test-results`、真实材料或根目录其他团队文件；
4. 从另一空目录克隆私库，按 README 启动 Mock 模式并运行质量门；
5. 邀请马仲琪新建练习分支并完成一次小型PR，验证协作流程。
