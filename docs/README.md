# AKAgent 文档

[← 返回项目首页](../README.md)

从安装到研究，再到策略验证。所有命令和配置路径均以**项目根目录**为基准。

| 文档 | 内容 |
| --- | --- |
| [安装、启动与更新](getting-started.md) | Windows / Linux 环境、启动方式、安装修复与更新 |
| [研究工具与市场支持](tools-and-markets.md) | 20 个工具、A 股 / 港股参数、代码规范与持仓保存 |
| [日报、网页与推送](daily-reports.md) | 双时点日报、可视化、持仓复核、全市场排序、局域网与 Webhook |
| [因子研究与回测](factor-lab.md) | 数据准备及验收、两种模型、训练调度、回测与部署 |
| [八种经典策略](fixed-strategies.md) | 入场条件、指标定义、5 分钟线预热与共同执行规则 |
| [回测逻辑审计](backtest-audit.md) | 六类偏差、成交及费用假设、验证与剩余限制 |
| [Skills、提问与证据边界](research-guide.md) | 8 个 Skills、研究提示词、评分含义与数据时效 |
| [常见问题](troubleshooting.md) | 启动失败、服务连接、数据源异常及其他市场 |

## 配置入口

| 示例文件 | 用途 |
| --- | --- |
| [daily-push.example.json](../config/daily-push.example.json) | 日报市场、双时点调度、自动打开与推送 |
| [factor-lab.example.json](../config/factor-lab.example.json) | 策略选择、回测信号时点与每周训练 |
| [agent-profile.example.yml](../config/agent-profile.example.yml) | 智能体配置记录示例 |

本地持仓、凭证和运行数据无需提交到 Git。历史分钟线修正的来源记录单独保留在 [data-corrections/minute](../data-corrections/minute/README.md)。
