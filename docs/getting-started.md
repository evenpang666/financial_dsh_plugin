# 安装、启动与更新

[← 项目首页](../README.md) · [网页指南](web-workspace.md)

需要 Node.js 22.13+、Python 3.11+。首次启动需要联网检查 DSH、pnpm 和 Python 数据依赖。

```sh
npm start
```

Windows 可双击 `start.bat`；Linux/macOS 使用 `bash start.sh`。浏览器默认打开 `http://127.0.0.1:3205`，占用时自动使用空闲端口，以启动窗口输出为准。`npm run dev` 不打开浏览器，`npm stop` 停止工作台。

在 DSH Web 的模型设置中配置模型与 API Key。知衡复用 DSH Agent 和凭据，不需要在本项目中填写 Key。`npm run dsh -- web` 可进入 DSH Web，`npm run dsh:register` 可重新注册本地插件。

日报在 `/reports`，训练与回测在 `/backtests`。日期、信号时点和超参数在网页设置，任务日志保存在本地。旧格式缓存自动重建，只有通过数据验收才进入回测。

更新代码前运行 `npm stop`，然后使用 Git 获取更新，再运行 `npm start`。启动器会检查缺失依赖。保留自己的未提交修改；不要用强制重置更新项目。

更多环境变量和数据目录见[网页指南](web-workspace.md)。
