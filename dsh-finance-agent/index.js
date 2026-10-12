import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'finance-agent'
export const inject = ['tools', 'skills']

const OUTPUT = {
  schema: { type: 'object' },
  render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
}

const DESCRIPTIONS = {
  'portfolio-daily-review': '按需分析已保存持仓，生成研究建议1、所选交易策略配置2及综合复核3，解释分歧、配置分母、现金、时点及缺失证据；不执行交易。',
  'market-event-briefing': 'Summarize daily or weekly market events with source dates, explicit uncertainty, and relevance to A-share or Hong Kong stock research.',
  'daily-stock-candidate-screen': 'Create a research candidate list from current evidence; never present it as a personalized buy list or trading order.',
  'a-share-research': '在用户要求研究、比较或复盘A股标的时使用；以工具事实为依据，输出可追溯的研究报告和非交易性动作建议。',
  'hk-share-research': '在用户要求研究、比较或复盘港股标的时使用；调用工具时必须传 market=hk，并使用 5 位标准代码。',
  'investment-risk-control': '每次涉及股票走势、估值、预测或动作建议时使用；约束数据时效、证据和非投资建议边界。',
  'earnings-and-announcement-analysis': '在解读财报、业绩预告、回购、减持、监管问询或其他公告时使用。',
  'quant-strategy-research': '用户要求策略规则、回测或历史表现评估时使用；要求避免未来函数并报告交易摩擦和样本外结果。',
}

export function apply(ctx, config = {}) {
  const baseUrl = (config.baseUrl || 'http://127.0.0.1:8765').replace(/\/+$/, '')
  const here = dirname(fileURLToPath(import.meta.url))
  const skillDir = join(here, 'skills')

  for (const filename of readdirSync(skillDir).filter((file) => file.endsWith('.md'))) {
    const skillName = filename.replace(/\.md$/, '')
    ctx.skills.register({
      name: skillName,
      description: DESCRIPTIONS[skillName] || `股票研究辅助技能：${skillName}`,
      content: readFileSync(join(skillDir, filename), 'utf8'),
      source: 'bundled',
    })
  }

  async function call(path, options = {}) {
    const { timeoutMs = 30000, ...fetchOptions } = options
    let response
    try {
      response = await fetch(`${baseUrl}${path}`, { signal: AbortSignal.timeout(timeoutMs), ...fetchOptions })
    } catch (error) {
      throw new Error(`本地股票数据服务不可用（${baseUrl}）。请启动 scripts/stock_data_server.py；原始错误：${error.message}`)
    }
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.error || `数据服务返回 HTTP ${response.status}`)
    return data
  }

  const market = (value = 'a') => {
    const normalized = String(value).trim().toLowerCase()
    if (!['a', 'hk'].includes(normalized)) throw new Error('market 必须是 a（A股）或 hk（港股）。')
    return normalized
  }
  const code = (value, selectedMarket = 'a') => {
    const normalizedMarket = market(selectedMarket)
    const raw = String(value).trim().toUpperCase().replace(/^HK:?/, '').replace(/\.HK$/, '')
    if (normalizedMarket === 'a' && !/^\d{6}$/.test(raw)) throw new Error('A 股 symbol 必须是 6 位代码，例如 600519；请先用 stock_search 确认代码。')
    if (normalizedMarket === 'hk' && !/^\d{1,5}$/.test(raw)) throw new Error('港股 symbol 必须是 1–5 位代码，例如 00700；请先用 stock_search 确认代码。')
    return normalizedMarket === 'hk' ? raw.padStart(5, '0') : raw
  }
  const marketParam = (selectedMarket) => `market=${encodeURIComponent(market(selectedMarket))}`
  const register = (definition) => ctx.tools.register({ output: OUTPUT, ...definition })

  register({
    name: 'stock_search',
    description: 'Search the local A-share or Hong Kong stock universe by code or company name. Set market=hk for Hong Kong stocks.',
    parameters: { type: 'object', properties: {
      query: { type: 'string', minLength: 1, description: 'Symbol or part/all of a company name.' },
      market: { type: 'string', enum: ['a', 'hk'], description: 'a=A股（默认）；hk=港股。' },
      limit: { type: 'integer', minimum: 1, maximum: 50 },
    }, required: ['query'], additionalProperties: false },
    execute: ({ query, market: selectedMarket = 'a', limit = 10 }) => call(`/v1/search?query=${encodeURIComponent(query)}&limit=${limit}&${marketParam(selectedMarket)}`),
  })
  register({
    name: 'get_saved_portfolio',
    description: 'Read the locally saved research portfolio. It contains only user-supplied symbols, optional cost basis, shares, and notes; it has no broker connection.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    execute: () => call('/v1/portfolio'),
  })
  register({
    name: 'get_portfolio_position_review',
    description: '按需读取当前市场的已保存持仓，比较个股研究配置1、指定策略在持仓池上的入场排序配置2和综合结果3。权重基于假设研究预算，非账户仓位。工具不调用LLM、不执行交易或写入日报；智能体需进一步解释证据差异。',
    parameters: { type: 'object', properties: {
      market: { type: 'string', enum: ['a', 'hk'] },
      strategy: { type: 'string', enum: ['xgboost', 'elastic_net', 'mean_reversion', 'momentum', 'breakout', 'ma_crossover', 'macd', 'rsi_rebound', 'bollinger_reversion', 'donchian_breakout'], description: '不传时沿用控制面板设置；当前策略仅支持A股。' },
      parameters: { type: 'object', description: '本次策略超参数；字段与范围从 get_strategy_checkpoints 的 parameter_schema 读取。不传时使用保存配置及推荐默认值。', additionalProperties: { type: 'number' } },
      checkpoint: { type: 'string', description: '多因子模型策略必须指定已训练 checkpoint ID；先用 get_strategy_checkpoints 获取。经典策略不传。' },
    }, additionalProperties: false },
    execute: ({market: selectedMarket = 'a', strategy = '', checkpoint = '', parameters = null}) => call(`/v1/portfolio-analysis?${marketParam(selectedMarket)}&strategy=${encodeURIComponent(strategy)}&checkpoint=${encodeURIComponent(checkpoint)}&parameters=${encodeURIComponent(JSON.stringify(parameters))}`, {timeoutMs: 180000}),
  })
  register({
    name: 'get_strategy_checkpoints',
    description: '列出经典策略和实际已训练的模型 checkpoint，含训练时间及版本 ID。模型列表为空时不能运行模型部署。',
    parameters: {type: 'object', properties: {}, additionalProperties: false},
    execute: () => call('/v1/strategy-options'),
  })
  register({
    name: 'set_portfolio',
    description: 'Replace the locally saved research portfolio. This only stores the provided entries in the project data folder and cannot place orders or access a brokerage account.',
    parameters: { type: 'object', properties: {
      holdings: { type: 'array', minItems: 0, maxItems: 100, items: { type: 'object', properties: {
        symbol: { type: 'string', description: 'A股 6 位代码（如 600519）或港股 1–5 位代码（如 00700）。' },
        market: { type: 'string', enum: ['a', 'hk'], description: 'a=A股（默认）；hk=港股。' },
        name: { type: 'string', description: 'Optional display name.' },
        cost_basis: { type: 'number', minimum: 0, description: 'Optional average cost for research context.' },
        shares: { type: 'number', minimum: 0, description: 'Optional share count for exposure review.' },
        note: { type: 'string', description: 'Optional non-sensitive research note.' },
      }, required: ['symbol'], additionalProperties: false } },
    }, required: ['holdings'], additionalProperties: false },
    execute: ({ holdings }) => call('/v1/portfolio', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ holdings }),
    }),
  })
  register({
    name: 'get_trading_day',
    description: 'Check whether a date is a trading day for A shares or Hong Kong stocks.',
    parameters: { type: 'object', properties: { date: { type: 'string', description: 'YYYY-MM-DD; defaults to today.' }, market: { type: 'string', enum: ['a', 'hk'] } }, additionalProperties: false },
    execute: ({ date = '', market: selectedMarket = 'a' }) => call(`/v1/trading-day?date=${encodeURIComponent(date)}&${marketParam(selectedMarket)}`),
  })
  register({
    name: 'get_market_brief',
    description: 'Get an A-share or Hong Kong market index snapshot and recent market-event headlines. Set market=hk for Hong Kong stocks; headlines are leads to verify, not proof of price impact.',
    parameters: { type: 'object', properties: { event_limit: { type: 'integer', minimum: 1, maximum: 50 }, market: { type: 'string', enum: ['a', 'hk'] } }, additionalProperties: false },
    execute: ({ event_limit = 20, market: selectedMarket = 'a' }) => call(`/v1/market-brief?event_limit=${event_limit}&${marketParam(selectedMarket)}`),
  })
  register({
    name: 'get_market_events',
    description: 'Get dated market-event leads for an explicit daily, weekly or monthly lookback window.',
    parameters: { type: 'object', properties: {
      days: { type: 'integer', minimum: 1, maximum: 90 },
      limit: { type: 'integer', minimum: 1, maximum: 100 },
    }, additionalProperties: false },
    execute: ({ days = 7, limit = 20 }) => call(`/v1/market-events?days=${days}&limit=${limit}`),
  })
  register({
    name: 'get_market_movers',
    description: 'Get the day’s leading A-share or Hong Kong stock gainers and losers as an evidence-based research universe. Set market=hk for Hong Kong stocks.',
    parameters: { type: 'object', properties: { limit: { type: 'integer', minimum: 1, maximum: 50 }, market: { type: 'string', enum: ['a', 'hk'] } }, additionalProperties: false },
    execute: ({ limit = 20, market: selectedMarket = 'a' }) => call(`/v1/market-movers?limit=${limit}&${marketParam(selectedMarket)}`),
  })
  register({
    name: 'get_market_panorama',
    description: 'Get the selected market panorama: international events, policy, social/attention proxy, breadth, risk level, reference position range, and the adjustment applied to stock buy/sell tendency percentages.',
    parameters: { type: 'object', properties: { market: { type: 'string', enum: ['a', 'hk'] } }, additionalProperties: false },
    execute: ({ market: selectedMarket = 'a' }) => call(`/v1/market-panorama?${marketParam(selectedMarket)}`, { timeoutMs: 120000 }),
  })
  register({
    name: 'get_candidate_ranking',
    description: 'Rank 1-20 non-ST research candidates in the selected market with a transparent momentum, liquidity and valuation score. Set market=hk for Hong Kong stocks; not a buy list.',
    parameters: { type: 'object', properties: { limit: { type: 'integer', minimum: 1, maximum: 20 }, market: { type: 'string', enum: ['a', 'hk'] } }, additionalProperties: false },
    execute: ({ limit = 5, market: selectedMarket = 'a' }) => call(`/v1/candidate-ranking?limit=${limit}&${marketParam(selectedMarket)}`),
  })
  register({
    name: 'get_daily_research_report',
    description: 'Build report 1 before open or report 2 at 14:30. The afternoon report compares report-1 calls with current prices, records reasons/reflections, and persists feedback for later analyses.',
    parameters: { type: 'object', properties: {
      candidate_limit: { type: 'integer', minimum: 1, maximum: 20 },
      market: { type: 'string', enum: ['a', 'hk'] },
      session: { type: 'string', enum: ['morning', 'afternoon'], description: 'morning=日报1；afternoon=日报2并对照日报1复盘。' },
    }, additionalProperties: false },
    execute: ({ candidate_limit = 5, market: selectedMarket = 'a', session = 'morning' }) => call(`/v1/daily-report?candidate_limit=${candidate_limit}&${marketParam(selectedMarket)}&session=${encodeURIComponent(session)}`, { timeoutMs: 180000 }),
  })

  register({
    name: 'get_research_reflections',
    description: 'Read persisted report-1/report-2 reflection records. Use them to explain historical misses and the bounded feedback adjustment applied to later analysis.',
    parameters: { type: 'object', properties: {
      market: { type: 'string', enum: ['a', 'hk'] },
      symbol: { type: 'string', description: 'Optional stock code; omit for market-wide recent reflections.' },
      limit: { type: 'integer', minimum: 1, maximum: 200 },
    }, additionalProperties: false },
    execute: ({ market: selectedMarket = 'a', symbol = '', limit = 50 }) => call(`/v1/reflections?${marketParam(selectedMarket)}&symbol=${symbol ? code(symbol, selectedMarket) : ''}&limit=${limit}`),
  })

  register({
    name: 'get_quote_snapshot',
    description: '获取A股或港股最近可得的价格快照、成交和可得估值字段；港股须传 market=hk。它不是逐笔实时行情；回复必须引用返回的 as_of。',
    parameters: { type: 'object', properties: { symbol: { type: 'string', description: 'A股 6 位或港股 1–5 位代码。' }, market: { type: 'string', enum: ['a', 'hk'] } }, required: ['symbol'], additionalProperties: false },
    execute: ({ symbol, market: selectedMarket = 'a' }) => call(`/v1/quote?symbol=${code(symbol, selectedMarket)}&${marketParam(selectedMarket)}`),
  })
  register({
    name: 'get_ohlcv',
    description: '获取A股或港股历史日线OHLCV；港股须传 market=hk。用于描述走势时必须说明复权方式和数据区间。',
    parameters: { type: 'object', properties: { symbol: { type: 'string' }, market: { type: 'string', enum: ['a', 'hk'] }, start: { type: 'string', description: 'YYYY-MM-DD，可选' }, end: { type: 'string', description: 'YYYY-MM-DD，可选' }, adjust: { type: 'string', enum: ['qfq', 'hfq', ''], description: 'qfq前复权、hfq后复权、空为不复权' } }, required: ['symbol'], additionalProperties: false },
    execute: ({ symbol, market: selectedMarket = 'a', start = '', end = '', adjust = 'qfq' }) => call(`/v1/ohlcv?symbol=${code(symbol, selectedMarket)}&${marketParam(selectedMarket)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&adjust=${encodeURIComponent(adjust)}`),
  })
  register({
    name: 'get_technical_indicators',
    description: '由程序基于历史日线计算MA、RSI、MACD、波动率和最大回撤。不得要求模型自行计算这些指标。',
    parameters: { type: 'object', properties: { symbol: { type: 'string' }, market: { type: 'string', enum: ['a', 'hk'] }, lookback: { type: 'integer', minimum: 60, maximum: 1000, description: '计算使用的最近交易日数量，默认260' }, adjust: { type: 'string', enum: ['qfq', 'hfq', ''] } }, required: ['symbol'], additionalProperties: false },
    execute: ({ symbol, market: selectedMarket = 'a', lookback = 260, adjust = 'qfq' }) => call(`/v1/indicators?symbol=${code(symbol, selectedMarket)}&${marketParam(selectedMarket)}&lookback=${lookback}&adjust=${encodeURIComponent(adjust)}`),
  })
  register({
    name: 'get_fundamentals',
    description: '获取A股或港股财务指标；港股须传 market=hk。只能基于返回的报告期与字段陈述事实；缺失数据不得补写。',
    parameters: { type: 'object', properties: { symbol: { type: 'string' }, market: { type: 'string', enum: ['a', 'hk'] } }, required: ['symbol'], additionalProperties: false },
    execute: ({ symbol, market: selectedMarket = 'a' }) => call(`/v1/fundamentals?symbol=${code(symbol, selectedMarket)}&${marketParam(selectedMarket)}`),
  })
  register({
    name: 'get_announcements',
    description: '查询 A 股巨潮公告。港股公告目前会明确返回“尚未接入 HKEX 披露源”，不会错误查询巨潮。',
    parameters: { type: 'object', properties: { symbol: { type: 'string' }, market: { type: 'string', enum: ['a', 'hk'] }, start: { type: 'string', description: 'YYYY-MM-DD，可选' }, end: { type: 'string', description: 'YYYY-MM-DD，可选' }, limit: { type: 'integer', minimum: 1, maximum: 100 } }, required: ['symbol'], additionalProperties: false },
    execute: ({ symbol, market: selectedMarket = 'a', start = '', end = '', limit = 20 }) => call(`/v1/announcements?symbol=${code(symbol, selectedMarket)}&${marketParam(selectedMarket)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&limit=${limit}`),
  })
  register({
    name: 'get_related_large_enterprises',
    description: 'Identify the target A-share industry, select up to five same-industry leaders by total market capitalization, and retrieve their recent CNINFO announcements for context and risk checks.',
    parameters: { type: 'object', properties: {
      symbol: { type: 'string' }, market: { type: 'string', enum: ['a', 'hk'] },
      days: { type: 'integer', minimum: 1, maximum: 180 },
      limit: { type: 'integer', minimum: 1, maximum: 5 },
    }, required: ['symbol'], additionalProperties: false },
    execute: ({ symbol, market: selectedMarket = 'a', days = 30, limit = 3 }) => call(`/v1/related-enterprises?symbol=${code(symbol, selectedMarket)}&${marketParam(selectedMarket)}&days=${days}&limit=${limit}`, { timeoutMs: 120000 }),
  })
  register({
    name: 'get_valuation',
    description: '获取数据源可用的PE、PB等估值快照。它不是完整估值模型；使用时必须标记数据日期和不可用字段。',
    parameters: { type: 'object', properties: { symbol: { type: 'string' }, market: { type: 'string', enum: ['a', 'hk'] } }, required: ['symbol'], additionalProperties: false },
    execute: ({ symbol, market: selectedMarket = 'a' }) => call(`/v1/valuation?symbol=${code(symbol, selectedMarket)}&${marketParam(selectedMarket)}`),
  })
  register({
    name: 'analyze_stock',
    description: 'Analyze one A-share or Hong Kong stock using the selected market’s financials, technicals, valuation and market context. Set market=hk for Hong Kong stocks; unavailable HKEX announcements and peer comparison are explicitly disclosed.',
    parameters: { type: 'object', properties: {
      symbol: { type: 'string' }, market: { type: 'string', enum: ['a', 'hk'] },
      announcement_days: { type: 'integer', minimum: 1, maximum: 730 },
    }, required: ['symbol'], additionalProperties: false },
    execute: ({ symbol, market: selectedMarket = 'a', announcement_days = 180 }) => call(`/v1/analysis?symbol=${code(symbol, selectedMarket)}&${marketParam(selectedMarket)}&announcement_days=${announcement_days}`),
  })

  ctx.logger?.info?.('finance-agent: local data tools and %d bundled skills registered (baseUrl=%s)', Object.keys(DESCRIPTIONS).length, baseUrl)
}
