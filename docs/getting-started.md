# 安装、启动与更新

[← 项目首页](../README.md) · [文档导航](README.md)

以下命令均在项目根目录执行。先准备运行环境，再启动控制面板；首次安装需要联网。

## 需要安装的内容

| 项目 | 用途 | 是否必须 |
|---|---|---:|
| Node.js 22 LTS | 运行 dsh 和插件 | 是 |
| Python 3.11+ | 本地数据和指标服务 | 是 |
| `@deepseek-ai/dsh` | Agent 运行环境 | 是 |
| 本包 `dsh-finance-agent` | 本地行情、指标、估值工具与 Skills | 是 |
| AKShare / pandas / numpy | 数据适配与指标计算 | 是 |

请从 Node.js 和 Python 官方网站安装对应 Windows x64 版本；安装 Node 时勾选“Add to PATH”。安装后，在**新的 PowerShell**运行：

```powershell
node --version
python --version
```

## 启动与使用（Windows / Linux）

先安装 Node.js 22 LTS 和 Python 3.11+；其余组件由启动器自动处理。日常使用有两种等价方式。

### 方式一：控制台简易启动（推荐）

双击项目根目录的 `control_panel.cmd`。可见命令行会依次检查 Node.js、Python、dsh、pnpm、`.venv`、Python 依赖和本地插件；缺少的组件会显示安装进度。准备完成后，启动器会使用 `.venv\Scripts\pythonw.exe` 打开控制面板。

控制面板会静默启动日报智能体、数据服务、日报页面和 DSH Web，但不会自动打开浏览器。需要检索时点击“打开 DSH Web”；需要查看归档日报时点击“打开日报”。控制面板最小化后服务会继续运行；关闭控制面板会关闭相关服务。

控制面板支持拖动边框缩放和最大化，初始尺寸按屏幕空间调整。宽窗口将网页入口与策略研究并排展示，窄窗口自动纵向排列；按钮按实际字体尺寸换行，状态与说明文字随可用宽度折行。内容超过窗口高度时，可用右侧滚动条或鼠标滚轮查看；日志保留独立滚动，Tab 切换到下方控件时会自动滚动显示。Windows 与 Linux 共用这一布局。

“网页入口”区域的“手动生成日报”会立即重新获取当前市场数据并更新本地归档：按配置的下午日报时点（默认 14:30）分界选择日报1或日报2，生成过程显示在运行日志中。手动生成不受定时推送开关和当天重复限制影响，不改变定时任务完成状态，也不执行远程 Webhook 推送；之后的定时日报仍照常执行。生成成功按 `auto_open_report` 配置打开页面；休市日会提示跳过，生成失败会保留原日报。状态检测、服务启动或其他任务进行时，该按钮禁用；生成过程中仍可打开 DSH Web、日报和回测页面。

### Linux 桌面启动

先安装 Node.js、Python 3.11+ 和 Git。在项目根目录打开终端运行：

```bash
./control_panel.sh
```

启动脚本会显示检查和安装进度；缺少 dsh、pnpm 时装入项目的 `.local/`，创建 `.venv`、安装 Python 依赖并注册插件。Linux 界面由 `/usr/bin/python3` 的系统 Tk 运行，数据和因子任务仍使用项目 `.venv`；缺少系统 Tk 时，会通过 apt、dnf 或 pacman 请求安装。界面选用系统已安装的中文字体。随后打开与 Windows 相同的控制面板，自动启用数据服务、日报页面、DSH Web 和当前登录会话的双时点日报调度器。最小化后保持后台运行；关闭控制面板会停止这些服务。Linux 调度器不会跨注销或重启自动运行，重新登录后请再次启动控制面板。

Linux 备选命令：`bash control_panel.sh`。无需界面时，`./close_agent.sh` 停止服务，`./update_agent.sh` 停止服务、拉取代码、更新 `.venv` 依赖并重新注册插件；更新后仍需重新启动控制面板。Linux 与 Windows 共享同一套行情、因子、日报和网页代码。

### 方式二：PowerShell 备选命令

在项目根目录打开 PowerShell，执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-control-panel.ps1
```

该命令与双击 `control_panel.cmd` 使用完全相同的检查、安装与启动流程，适合快捷方式失效或需要保留命令行进度时使用。

### 仅打开 DSH 对话页面

不使用本项目的日报、行情服务或控制面板时，也可以在任意工作目录的 PowerShell 中直接运行：

```powershell
dsh web
```

该命令只启动 DSH Web 对话页面，并会按 DSH 默认行为打开浏览器；不会启动日报任务、本地数据服务或日报网页。若此前已注册本项目插件，它仍会作为 DSH 的可选工具存在，但普通对话不调用这些工具即可独立使用。

首次启动后，点击“打开 DSH Web”，在“设置 → 模型”中填写 DeepSeek API Key。API Key 不要写入本项目文件或提交到 Git。

“安装 / 修复”按钮会停止服务并关闭当前控制面板，然后重新执行上述启动检查；缺失或失效的 dsh、pnpm、虚拟环境、Python 依赖和本地插件会自动修复，完成后自动重新打开控制面板。使用“更新项目”还需要系统已安装 Git，且当前目录是 Git 仓库。

## 更新项目

可以点击控制台中的“更新项目”，或双击项目根目录的 `update_agent.cmd`。更新会先停止全部项目服务和日报任务，再执行 `git pull --ff-only`，更新 `.venv` 中的 Python 依赖，并重新注册本地 `dsh-finance-agent` 插件；不会更新 dsh 或 pnpm。更新完成后所有服务保持关闭；关闭并重新打开控制台后会自动启用全部服务。`config/daily-push.json`、持仓、历史日报和反思数据均不会被 Git 覆盖。

若存在未提交的受版本控制文件修改，Git 会安全地拒绝可能造成覆盖的更新；根据窗口中的提示处理修改后重新双击即可。
