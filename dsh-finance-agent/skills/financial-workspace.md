# 知衡工作台

使用 open_financial_workspace 返回已运行的 DSH 知衡工作台地址。start_financial_job 发起研究、手动报告、回测、训练或部署；用 get_financial_jobs 跟踪，必要时 cancel_financial_job 取消。通过 list_financial_reports、read_financial_report 和 list_backtest_results 读取归档。

研究与模型调用复用 DSH 的模型与凭据，可在知衡设置页选择已配置模型。持仓仅在用户明确要求时用 set_portfolio、update_portfolio_holding、delete_portfolio_holding 修改；调度通过 get_report_schedule、set_report_schedule 管理。支持 A股、港股、美股，市场参数为 a、hk、us；美股按纽约时间执行定时任务。关闭或停用知衡插件时保留共享 DSH Web 与其他插件。
