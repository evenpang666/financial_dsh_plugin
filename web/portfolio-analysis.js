(() => {
async function requestJson(path) {
  const response = await fetch(path), data = await response.json()
  if (!response.ok) throw Error(data.error || `HTTP ${response.status}`)
  return data
}
function textNode(tag, value, className = '') {
  const node = document.createElement(tag); node.textContent = String(value ?? '—')
  if (className) node.className = className
  return node
}
const portfolioContent = document.querySelector('#portfolioContent')
const portfolioStrategy = document.querySelector('#portfolioStrategy')
const portfolioKind = document.querySelector('#portfolioKind')
let portfolioOptions = null
let portfolioStrategyChosen = false
let portfolioAnalyzing = false
let portfolioStarting = false
let portfolioOptionsRequest = 0
const portfolioResults = new Map()
let portfolioRevision = 0
document.addEventListener('portfolio-saved', () => { portfolioRevision++; portfolioResults.clear(); portfolioContent.replaceChildren(); document.querySelector('#portfolioMeta').textContent = '持仓已更新，点击“分析我的持仓”查看新结果。' })
function portfolioResultKey(market, strategy, parameters) {
  return JSON.stringify([market, strategy, Object.entries(parameters).sort(([a], [b]) => a.localeCompare(b))])
}
function selectedPortfolioResultKey(market = document.querySelector('#portfolioMarket').value) {
  if (!portfolioStrategy.value) return null
  const parameters = {}
  for (const input of document.querySelectorAll('#portfolioParameters input[data-parameter]')) {
    if (!input.checkValidity()) return null
    parameters[input.dataset.parameter] = Number(input.value)
  }
  return portfolioResultKey(market, portfolioStrategy.value, parameters)
}
function restorePortfolioResult(market = document.querySelector('#portfolioMarket').value) {
  const saved = portfolioResults.get(selectedPortfolioResultKey(market))
  if (saved) { renderPortfolioResult(saved); return true }
  portfolioContent.replaceChildren()
  document.querySelector('#portfolioMeta').textContent = portfolioStrategy.disabled && portfolioKind.value === 'model'
    ? '没有已训练模型，请先在回测页训练模型。' : '当前选择暂无分析记录，点击“分析我的持仓”生成结果。'
  return false
}
function friendlyMissing(values) {
  return [...new Set((values || []).map(value => {
    const raw = String(value)
    if (raw.includes('HTTPSConnectionPool') || raw.includes('ProxyError') || raw.length > 200) {
      const label = raw.includes('不可用') ? raw.slice(0, raw.indexOf('不可用') + 3) : raw.split('：')[0].slice(0, 100)
      return `${label}（${raw.includes('ProxyError') ? '网络代理连接失败' : '数据源暂不可用'}）`
    }
    return raw
  }))].join('；') || '无'
}

const executionParameterDrafts = new Map()

function renderExecutionParameters(prefix, select, payload) {
  const container = document.getElementById(`${prefix}Parameters`)
  if (!container) return
  container.replaceChildren()
  const item = payload?.strategies?.[select.value]
  if (!item) return
  const draftKey = `${prefix}:${select.value}`
  const draft = executionParameterDrafts.get(draftKey) || {...item.parameters}
  executionParameterDrafts.set(draftKey, draft)
  for (const [key, spec] of Object.entries(item.parameter_schema || {})) {
    const label = document.createElement('label')
    label.className = 'select-control'
    label.append(textNode('span', spec.label))
    const input = document.createElement('input')
    input.type = 'number'; input.min = spec.min; input.max = spec.max
    input.step = spec.type === 'integer' ? '1' : 'any'
    input.value = draft[key] ?? spec.default
    input.dataset.parameter = key
    input.required = true
    input.addEventListener('input', () => { draft[key] = input.value; if (prefix === 'portfolio') restorePortfolioResult() })
    label.append(input); container.append(label)
  }
  const reset = textNode('button', '恢复推荐默认值')
  reset.type = 'button'
  reset.addEventListener('click', () => {
    executionParameterDrafts.set(draftKey, Object.fromEntries(Object.entries(item.parameter_schema).map(([key, spec]) => [key, spec.default])))
    renderExecutionParameters(prefix, select, payload)
    if (prefix === 'portfolio') restorePortfolioResult()
  })
  container.append(reset)
  if (item.training_parameters && Object.keys(item.training_parameters).length) {
    const parameters = Object.entries(item.training_parameters).map(([key, value]) => `${item.training_parameter_schema?.[key]?.label || key}：${value}`).join(' · ')
    container.append(textNode('p', `此 checkpoint 的训练参数：${parameters}`, 'ranking-note'))
  }
  if (item.training_range) {
    container.append(textNode('p', `训练集 ${item.training_range.requested_start || item.training_range.first_day || '历史版本未记录'} 至 ${item.training_range.requested_end || item.training_range.last_day} · 训练标签截至 ${item.training_range.label_end_day || '未记录'}；部署使用此 checkpoint 的冻结权重。`, 'ranking-note'))
  }
}

function executionParameters(prefix) {
  const parameters = {}
  for (const input of document.querySelectorAll(`#${prefix}Parameters input[data-parameter]`)) {
    if (!input.reportValidity()) throw Error('请修正策略超参数。')
    parameters[input.dataset.parameter] = Number(input.value)
  }
  return parameters
}

function fillExecutionSelect(kind, select, payload, selected = '') {
  const items = kind.value === 'model' ? payload.models : payload.fixed_strategies
  select.replaceChildren()
  for (const [id, item] of Object.entries(items || {})) select.add(new Option(item.name, id))
  if ([...select.options].some(option => option.value === selected)) select.value = selected
  select.disabled = !select.options.length
  renderExecutionParameters("portfolio", select, payload)
}

async function loadPortfolioStrategies() {
  if (portfolioAnalyzing) return
  const request = ++portfolioOptionsRequest, market = document.querySelector('#portfolioMarket').value
  portfolioStrategy.disabled = true
  document.querySelector('#portfolioButton').disabled = true
  try {
    const payload = await requestJson('/api/portfolio-strategies?market=' + market)
    if (portfolioAnalyzing || request !== portfolioOptionsRequest || market !== document.querySelector('#portfolioMarket').value) return
    const selected = portfolioStrategyChosen ? portfolioStrategy.value : payload.selected
    portfolioOptions = payload
    if (!portfolioStrategyChosen) portfolioKind.value = payload.selected_kind || (selected?.includes('@') ? 'model' : 'fixed')
    fillExecutionSelect(portfolioKind, portfolioStrategy, payload, selected)
    document.querySelector('#portfolioButton').disabled = portfolioStarting || portfolioAnalyzing || portfolioStrategy.disabled
    if (!portfolioAnalyzing) restorePortfolioResult()
  } catch (error) {
    if (request !== portfolioOptionsRequest) return
    portfolioStrategy.replaceChildren()
    portfolioStrategy.disabled = true
    document.querySelector('#portfolioButton').disabled = true
    document.querySelector('#portfolioMeta').textContent = `策略读取失败：${error.message}`
  }
}
portfolioKind.addEventListener('change', () => {
  portfolioStrategyChosen = true
  if (portfolioOptions) fillExecutionSelect(portfolioKind, portfolioStrategy, portfolioOptions)
  document.querySelector('#portfolioButton').disabled = !portfolioStrategy.value
  restorePortfolioResult()
})
portfolioStrategy.addEventListener('change', () => {
  portfolioStrategyChosen = true
  renderExecutionParameters("portfolio", portfolioStrategy, portfolioOptions)
  restorePortfolioResult()
})

function renderPositionSummary(payload) {
  const summary = payload.position_summary
  if (!summary?.rows?.length) return
  const title = textNode('h3', '仓位总结｜三组研究配置')
  const meta = textNode('p', `${payload.strategy_review?.strategy_name || '策略未取得'} · ${payload.strategy_review?.scope || ''} · 信号 ${payload.strategy_review?.as_of || '未取得'}`, 'ranking-note')
  const note = textNode('p', summary.basis, 'ranking-note')
  const percentValue = value => value == null || !Number.isFinite(Number(value)) ? '待补充' : `${Number(value).toFixed(2)}%`
  const wrap = document.createElement('div'); wrap.className = 'table-wrap position-summary'
  const table = document.createElement('table'), head = document.createElement('thead'), header = document.createElement('tr')
  for (const label of ['持仓 / 数据日', '当前股票市值占比（不含现金）', '研究建议1', '策略建议2', '综合结果3']) header.append(textNode('th', label))
  head.append(header)
  const body = document.createElement('tbody')
  for (const row of summary.rows) {
    const tr = document.createElement('tr'), name = document.createElement('td')
    name.append(textNode('strong', `${row.name || row.symbol} · ${row.symbol}`), textNode('small', `研究 ${row.analysis_as_of || '未取得'} / 策略 ${row.strategy_as_of || '未取得'}`))
    tr.append(name, textNode('td', percentValue(row.current_holding_share_pct)))
    for (const [key, subtitle] of [['suggestion1', row.suggestion1?.action], ['suggestion2', row.suggestion2?.rank ? `策略入场排名 ${row.suggestion2.rank} · 分数 ${row.suggestion2.score}` : row.suggestion2?.status === 'not_triggered' ? '未触发新增入场' : '策略证据不足'], ['suggestion3', '综合复核']]) {
      const item = row[key] || {}, cell = document.createElement('td')
      cell.append(textNode('strong', percentValue(item.weight_pct)), textNode('small', subtitle || '待核验'), textNode('small', item.reason || '证据不足'))
      tr.append(cell)
    }
    body.append(tr)
  }
  for (const [label, key] of [['股票配置合计', 'stock_totals_pct'], ['预留现金', 'cash_reserve_pct']]) {
    const tr = document.createElement('tr'); tr.className = 'position-total'
    tr.append(textNode('th', label), textNode('td', '—'))
    for (const suggestion of ['suggestion1', 'suggestion2', 'suggestion3']) tr.append(textNode('td', percentValue(summary[key]?.[suggestion])))
    body.append(tr)
  }
  table.append(head, body); wrap.append(table)
  portfolioContent.append(title, meta, note, wrap)
  const methods = document.createElement('details'); methods.className = 'position-methods'
  methods.append(textNode('summary', '查看三组计算方法与限制'))
  for (const value of [summary.method1, summary.method2, summary.method3, `研究股票配置上限 ${percentValue(summary.exposure_limit_pct)}；单股上限 ${percentValue(summary.single_name_limit_pct)}。缺失关键证据时不生成数值配置。`]) methods.append(textNode('p', value, 'ranking-note'))
  portfolioContent.append(methods)
}

function renderPortfolioResult(payload) {
  portfolioContent.replaceChildren()
  const stamp = payload.as_of ? `分析完成 ${new Date(payload.as_of).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'})}（北京时间） · ` : ''
  document.querySelector('#portfolioMeta').textContent = stamp + (payload.note || '')
  if (payload.strategy_review?.checkpoint_id) document.querySelector('#portfolioMeta').textContent += ` · checkpoint ${payload.strategy_review.checkpoint_id} · 训练 ${payload.strategy_review.checkpoint_trained_at}`
  if (!payload.available) { portfolioContent.append(textNode('p', '当前市场没有已保存持仓。', 'empty-note')); return }
  renderPositionSummary(payload)
  const list = document.createElement('div'); list.className = 'watch-grid'
  for (const item of payload.holdings || []) {
    const analysis = item.analysis || {}
    const card = document.createElement('article'); card.className = 'watch-card'
    card.append(textNode('h3', `${analysis.name || item.holding?.name || item.holding?.symbol} · ${item.holding?.symbol}`))
    const shares = Number(item.holding?.shares), price = Number(item.price)
    card.append(textNode('p', `持股 ${Number.isFinite(shares) ? shares.toLocaleString('zh-CN') : '—'} 股 · 数据价格 ${item.price != null && Number.isFinite(price) ? price.toLocaleString('zh-CN') : '—'} · ${analysis.as_of || '日期未取得'}`))
    card.append(textNode('p', item.strategy))
    card.append(textNode('small', `财报期 ${analysis.latest_report_period || '未取得'} · ${analysis.long_term?.evidence?.join('；') || '基本面待补充'}`))
    card.append(textNode('small', `技术面：${analysis.short_term?.evidence?.join('；') || '待补充'}`))
    card.append(textNode('small', `缺失：${friendlyMissing(analysis.unavailable)}`))
    list.append(card)
  }
  portfolioContent.append(list)
}

document.querySelector('#portfolioButton').addEventListener('click', async event => {
  if (portfolioStarting || portfolioAnalyzing) return
  const button = event.currentTarget
  const requestedMarket = document.querySelector('#portfolioMarket').value
  portfolioStarting = true
  button.disabled = true
  button.dataset.analyzing = 'true'
  document.dispatchEvent(new Event('portfolio-analysis-state'))
  try {
    if (!portfolioStrategyChosen) await loadPortfolioStrategies()
    if (!portfolioStrategy.value || requestedMarket !== document.querySelector('#portfolioMarket').value) return
    const selectedStrategy = portfolioStrategy.value
    const parameters = executionParameters('portfolio')
    const resultKey = portfolioResultKey(requestedMarket, selectedStrategy, parameters)
    const requestedRevision = portfolioRevision
    portfolioStrategy.disabled = true; portfolioKind.disabled = true
    portfolioStarting = false; portfolioAnalyzing = true
    document.querySelector('#portfolioMeta').textContent = '正在分析财报、估值、技术面与所选策略，生成三组研究配置…'
    portfolioContent.replaceChildren()
    const response = await fetch('/api/portfolio-analysis', {method: 'POST', headers: {'X-Report-Market': requestedMarket, 'X-Portfolio-Strategy': selectedStrategy, 'X-Strategy-Parameters': JSON.stringify(parameters)}})
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`)
    if (requestedRevision === portfolioRevision) {
      portfolioResults.set(resultKey, payload)
      if (requestedMarket === document.querySelector('#portfolioMarket').value && resultKey === selectedPortfolioResultKey()) renderPortfolioResult(payload)
    }
  } catch (error) {
    if (requestedMarket === document.querySelector('#portfolioMarket').value) document.querySelector('#portfolioMeta').textContent = `持仓分析失败：${error.message}`
  } finally {
    portfolioStarting = false; portfolioAnalyzing = false
    button.disabled = !portfolioStrategy.value
    portfolioStrategy.disabled = !portfolioStrategy.options.length; portfolioKind.disabled = false
    button.dataset.analyzing = 'false'
    document.dispatchEvent(new Event('portfolio-analysis-state'))
  }
})

document.querySelector('#portfolioMarket').addEventListener('change', () => { portfolioStrategyChosen = false; portfolioContent.replaceChildren(); void loadPortfolioStrategies() })
document.addEventListener('portfolio-ready', () => { portfolioStrategyChosen = false; void loadPortfolioStrategies() })
loadPortfolioStrategies()
})()
