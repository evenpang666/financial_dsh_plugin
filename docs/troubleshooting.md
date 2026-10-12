# 常见问题

[← 项目首页](../README.md) · [文档导航](README.md)

先查看控制面板的运行状态与日志；安装环境缺失时可使用“安装 / 修复”。

**`dsh` 不是内部或外部命令**：关闭并重开 PowerShell；确认 Node.js 已加入 PATH，然后重新双击 `control_panel.cmd` 或运行“方式二”的备选命令。启动引导会自动安装缺失的 dsh。

**工具提示无法连接数据服务**：点击控制面板“刷新”检查服务状态；若数据服务未运行，关闭并重新打开控制面板。也可运行 `Invoke-RestMethod http://127.0.0.1:8765/health` 排查端口占用。

**AKShare 查询失败或字段变化**：升级依赖 `pip install --upgrade akshare`。若仍不稳定，应在 `scripts/stock_data_server.py` 的 Provider 层替换为 Tushare 或企业数据源；不要让模型以旧缓存补写数据。

**需要美股**：须新增经授权的对应市场数据适配器；不要将 A 股或港股代码规则用于其他市场。
