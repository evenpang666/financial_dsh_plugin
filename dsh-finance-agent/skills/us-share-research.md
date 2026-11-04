# 美股研究

调用 financial 的 stock_search，显式传 market=us，确认 ticker 和公司；BRK.B 规范为 BRK-B。
先调用 analyze_stock，再按需要调用 get_quote_snapshot、get_ohlcv、get_technical_indicators、get_fundamentals、get_announcements、get_related_large_enterprises。每次保持 market=us。

回答需注明来源、原始 as_of、USD 报价、America/New_York 时区以及常规/盘前/盘后口径。不能用抓取日期代替报价日期。财报的报告币种可能不同于报价币种，不能默认均为美元。SEC 查询需用户设置 SEC_USER_AGENT 联系方式；缺失或被限流时如实报告，不能用新闻冒充公告。

美股 qfq/split_dividend 指 Yahoo 的拆股分红调整，不等同于中国市场的复权算法；hfq 会被拒绝。筛选与市场宽度是有边界的活跃股票样本，不能称全市场。

持仓用 get_saved_portfolio、set_portfolio、update_portfolio_holding、delete_portfolio_holding。仅按用户明确要求写入；美股支持碎股，禁止混合币种直接汇总为账户市值。用 get_portfolio_position_review 比较个股研究、日线信号和综合复核。

策略先 get_strategy_checkpoints(market=us)。使用 start_financial_job，kind=train/backtest，market=us，明确 symbols、start、end 和 strategy。美股独立日线模型与A股盘中模型不可互换，模型回测必须晚于验证标签截止日；用 get_financial_jobs 跟踪结果。说明固定股票池偏差、复权模拟单位、费用、滑点与缺失成交约束。

结论区分事实、推断与待核实信息；倾向分数不是上涨概率或账户仓位。不得连接券商或执行交易。
