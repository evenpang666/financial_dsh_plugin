# 研究工具与市场支持

[项目首页](../README.md) · [文档导航](README.md)

插件提供 **21 个研究工具和 11 个工作台工具**。默认市场为 A 股，显式传 `market=hk` 选择港股，`market=us` 选择美股；以各工具参数定义为准。

## 研究工具

| 工具 | 用途 |
| --- | --- |
| `stock_search` | 按代码或名称搜索对应市场标的 |
| `get_quote_snapshot` | 最近可得报价、成交与可得估值字段 |
| `get_ohlcv` | 带数据区间、来源和复权说明的历史日线 |
| `get_technical_indicators` | 程序计算的 MA、RSI、MACD、波动率和最大回撤 |
| `get_fundamentals` | 供应商可得财务指标与报告期 |
| `get_announcements` | A 股巨潮或美股 SEC 披露 |
| `get_related_large_enterprises` | 同行业龙头及可得公告背景 |
| `get_valuation` | PE、PB 等供应商可得估值字段 |
| `get_saved_portfolio` | 读取用户保存的本地研究持仓 |
| `set_portfolio` | 替换完整持仓列表，不执行交易 |
| `get_portfolio_position_review` | 对照个股研究、指定策略排序与综合结果 |
| `get_strategy_checkpoints` | 列出经典策略与实际训练的模型版本 |
| `get_trading_day` | 查询对应市场交易日 |
| `get_market_brief` | 指数快照与近期事件线索 |
| `get_market_events` | 指定回看窗口内的事件线索 |
| `get_market_movers` | 当日异动的有界研究候选池 |
| `get_market_panorama` | 宽度、情绪代理、风险等级和参考风险预算 |
| `get_candidate_ranking` | 透明规则下的候选排序 |
| `analyze_stock` | 汇总单股事实、评分、覆盖状态与风险 |
| `get_daily_research_report` | 上午/下午日报的结构化证据与快照 |
| `get_research_reflections` | 读取历史日报的候选一致性复盘 |

## 工作台工具

| 工具 | 用途 |
| --- | --- |
| `open_financial_workspace` | 启动或复用服务，返回实际地址 |
| `start_financial_job` | 异步启动研究、手动报告、部署、回测或训练 |
| `get_financial_jobs` / `cancel_financial_job` | 查询任务状态或取消任务 |
| `list_financial_reports` / `read_financial_report` | 列出归档或读取日报正文 |
| `list_backtest_results` | 查询历史回测 |
| `update_portfolio_holding` / `delete_portfolio_holding` | 按市场与代码编辑股数或删除持仓 |
| `get_report_schedule` / `set_report_schedule` | 查看或保存本地多市场调度 |

手动日报使用 `start_financial_job(kind=daily, market=...)`，只选择市场，按当前时刻单独归档。`get_daily_research_report` 的 `morning` / `afternoon` 参数用于获取相应时段的证据，不等同于手动报告任务。

## 市场代码与数据

| 市场 | 代码示例 | 数据与已知边界 |
| --- | --- | --- |
| A 股 | `600519` | AKShare 等公共数据源；公告来自巨潮 |
| 港股 | `00700`，输入 `700` 可规范化 | 行情、日线与可得财务；HKEX 公告和同行披露暂未接入 |
| 美股 | `AAPL`、`MSFT`、`BRK.B` → `BRK-B` | Yahoo Finance / yfinance、SEC EDGAR、XNYS 日历；支持美国上市 ETF 与 ADR，按可得字段返回 |

美股交易日覆盖节假日、纽约夏令时和提前收市。报价保留供应商时间，财报币种和美元报价分开陈述；跨市场币种不能直接相加为账户价值。美股持仓支持碎股。

搜索、报价和日线遭遇美股上游限流时，可回退到 24 小时内同参数的缓存，来源明确标注缓存获取时间；无可用缓存时返回错误。报价不是逐笔实时行情，报告需引用返回的 `as_of`。

SEC 披露需要在启动环境配置访问者名称和联系邮箱：

```powershell
$env:SEC_USER_AGENT = 'YourName contact@example.com'
npm start
```

这不是 API Key。未配置或被限流时，公告工具返回可解释错误；不将缺失披露编造成事实。美股 `qfq` / `split_dividend` 对应 Yahoo 拆股分红调整，不支持 `hfq`。

## 策略与回测口径

网页策略部署、训练和回测支持三市场，默认日线，可选周线与月线。信号在完整 K 线结束后形成，在下一周期执行；A 股日线退出遵守当前引擎的 T+1 延后规则。模型 checkpoint 按市场与周期隔离，回测必须晚于训练标签截止日。

A 股训练和回测支持沪深300、中证500或自定股票池，部署还可选全盘；港股、美股仅支持 1–50 只自定股票。A 股历史指数预设使用研究开始日可得的历史成分，在区间内固定名单，不以未来成分替代。历史名单不是逐日动态调整，结果不等同于同期指数复制策略。

日/周/月线引擎按单边手续费 5 bps 和滑点 5 bps 模拟。固定股票池存在幸存者选择偏差，未完整模拟盘口、涨跌停、停牌、融资和税费细项。美股使用复权总回报单位模拟，不代表当日实际美元成交价。候选榜是有界样本，不能当作全市场全集。

原 A 股盘中分钟线部署、因子周训练与历史归档保留供核对，港股盘中执行未接入；这些限制不代表港股日/周/月线网页引擎不可用。不同引擎与数据口径的收益不直接比较。

历史计算细节见[固定策略](fixed-strategies.md)、[因子研究](factor-lab.md)和[回测审计](backtest-audit.md)。数据来源、缺失处理与研究结论的边界见[研究指南](research-guide.md)。
