const reportElement = document.querySelector('#report')
const statusCard = document.querySelector('#statusCard')
const updatedAt = document.querySelector('#updatedAt')
const connectionState = document.querySelector('#connectionState')
const reportDate = document.querySelector('#reportDate')
const refreshButton = document.querySelector('#refreshButton')
const requestedReport = new URLSearchParams(window.location.search).get('report') || ''
const rankingMeta = document.querySelector('#rankingMeta')
const rankingContent = document.querySelector('#rankingContent')
const rankingStrategy = document.querySelector('#rankingStrategy')
const rankingKind = document.querySelector('#rankingKind')
let rankingOptions = null
const rankMarketButton = document.querySelector('#rankMarketButton')
let rankingStrategyChosen = false
let rankingJob = null
let rankingStarting = false
let rankingPollTimer = 0
let rankingPayload = null
let rankingPage = 0
const globalChart = document.querySelector('#globalChart')
const sectorChart = document.querySelector('#sectorChart')
const horizonContent = document.querySelector('#horizonContent')
const portfolioContent = document.querySelector('#portfolioContent')
const portfolioStrategy = document.querySelector('#portfolioStrategy')
const portfolioKind = document.querySelector('#portfolioKind')
let portfolioOptions = null
let portfolioStrategyChosen = false
let portfolioAnalyzing = false
let portfolioStarting = false
const portfolioResults = new Map()
document.addEventListener('portfolio-saved', () => { portfolioResults.clear(); portfolioContent.replaceChildren(); document.querySelector('#portfolioMeta').textContent = '持仓已更新，点击“分析我的持仓”查看新结果。' })
const researchUpdateButton = document.querySelector('#researchUpdateButton')
let publishedRanking = null
let rankingPublicationId = ''
let currentView = {}
let currentMarket = 'a'
let selectedHorizon = 'long'
let displayedReportId = ''
const reportIndex = document.querySelector('#reportIndex')
const reportIndexPanel = document.querySelector('#reportIndexPanel')
const indexBreakpoint = window.matchMedia('(max-width: 1000px)')
let indexTargets = []
let indexFrame = 0
let initialSectionApplied = false

function jumpToSection(target) {
  if (!target) return
  if (target.tagName === 'DETAILS') target.open = true
  if (indexBreakpoint.matches) reportIndexPanel.open = false
  requestAnimationFrame(() => {
    target.scrollIntoView({behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start'})
    if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1')
    target.focus({preventScroll: true})
    scheduleIndexPosition()
  })
}

function updateIndexPosition() {
  indexFrame = 0
  const topbarBottom = document.querySelector('.topbar').getBoundingClientRect().bottom
  const navigationBoundary = indexBreakpoint.matches
    ? Math.max(topbarBottom, reportIndexPanel.querySelector('summary').getBoundingClientRect().bottom) + 20
    : topbarBottom + 32
  const anchorOffset = indexTargets[0] ? parseFloat(getComputedStyle(indexTargets[0].target).scrollMarginTop) || 0 : 0
  const boundary = Math.max(navigationBoundary, anchorOffset + 2)
  let current = indexTargets[0]
  for (const item of indexTargets) {
    if (item.target.getClientRects().length && item.target.getBoundingClientRect().top <= boundary) current = item
  }
  for (const item of indexTargets) {
    if (item === current) item.link.setAttribute('aria-current', 'location')
    else item.link.removeAttribute('aria-current')
  }
  if (current && reportIndexPanel.open && !indexBreakpoint.matches) {
    const linkRect = current.link.getBoundingClientRect(), navRect = reportIndex.getBoundingClientRect()
    if (linkRect.top < navRect.top) reportIndex.scrollTop -= navRect.top - linkRect.top + 8
    else if (linkRect.bottom > navRect.bottom) reportIndex.scrollTop += linkRect.bottom - navRect.bottom + 8
  }
}

function scheduleIndexPosition() {
  if (!indexFrame) indexFrame = requestAnimationFrame(updateIndexPosition)
}

function rebuildReportIndex() {
  const headingIndex = document.querySelector('#reportHeadingIndex')
  const previousScroll = reportIndex.scrollTop
  headingIndex.replaceChildren()
  if (!reportElement.hidden) {
    for (const [index, heading] of [...reportElement.querySelectorAll('h2, h3')].entries()) {
      heading.id = `report-heading-${index + 1}`
      const link = textNode('a', heading.textContent, heading.tagName === 'H3' ? 'index-subheading' : '')
      link.href = `#${heading.id}`
      link.setAttribute('data-index-target', '')
      headingIndex.append(link)
    }
  }
  indexTargets = [...reportIndex.querySelectorAll('[data-index-target]')]
    .map(link => ({link, target: document.getElementById(link.hash.slice(1))}))
    .filter(item => item.target)
  reportIndex.scrollTop = previousScroll
  scheduleIndexPosition()
}

reportIndexPanel.open = !indexBreakpoint.matches
indexBreakpoint.addEventListener('change', () => {
  reportIndexPanel.open = !indexBreakpoint.matches
  scheduleIndexPosition()
})
reportIndex.addEventListener('click', event => {
  const link = event.target.closest('a[href^="#"]')
  if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return
  const target = document.getElementById(link.hash.slice(1))
  if (!target) return
  event.preventDefault()
  if (window.location.hash !== link.hash) history.pushState(null, '', link.hash)
  jumpToSection(target)
})
reportIndexPanel.addEventListener('keydown', event => {
  if (event.key === 'Escape' && indexBreakpoint.matches) {
    reportIndexPanel.open = false
    reportIndexPanel.querySelector('summary').focus()
  }
})
window.addEventListener('scroll', scheduleIndexPosition, {passive: true})
window.addEventListener('resize', scheduleIndexPosition)
window.addEventListener('hashchange', () => jumpToSection(document.getElementById(window.location.hash.slice(1))))

function textNode(tag, value, className = '') {
  const element = document.createElement(tag)
  element.textContent = String(value ?? '—')
  if (className) element.className = className
  return element
}

function renderBars(target, rows, nameKey) {
  ReportVisuals.renderBars(target, rows, nameKey)
}

function renderHorizon() {
  const rows = currentView.horizon_recommendations?.horizons?.[selectedHorizon] || []
  const labels = {long: '中长线', short: '短线', ultra_short: '超短线'}
  document.querySelector('#horizonMeta').textContent = `${labels[selectedHorizon]} · 候选池 ${currentView.horizon_recommendations?.pool_size ?? '—'} 只 · 证据不足时留空，不构成交易指令`
  horizonContent.replaceChildren()
  if (!rows.length) { horizonContent.append(textNode('p', '本期没有满足该周期证据门槛的标的。', 'empty-note')); return }
  const list = document.createElement('div'); list.className = 'watch-grid'
  for (const row of rows) {
    const card = document.createElement('article'); card.className = 'watch-card'
    card.append(textNode('h3', `${row.name || row.symbol} · ${row.symbol}`))
    card.append(textNode('p', row.basis || '证据不足'))
    card.append(textNode('small', `财报期 ${row.financial_period || '未取得'} · PE ${row.pe_ttm ?? '未取得'} · ${row.risk}`))
    list.append(card)
  }
  horizonContent.append(list)
}

function showInsights(payload) {
  const changedMarket = (payload.market || 'a') !== currentMarket
  displayedReportId = payload.id || ''
  currentView = payload.view || {}
  currentMarket = payload.market || 'a'
  ReportVisuals.render(payload)
  const global = currentView.global_markets || {}
  document.querySelector('#globalMeta').textContent = `${global.source || '数据未归档'} · 各市场行情时间不同，请查看日报明细`
  renderBars(globalChart, global.indices || [], row => `${row.region} · ${row.name}`)
  const breadth = currentView.market_panorama?.market_breadth || {}
  const marketState = currentView.market_panorama?.market_state || {}
  const breadthElement = document.querySelector('#marketBreadth')
  breadthElement.replaceChildren()
  breadthElement.append(textNode('p', marketState.summary || '市场状态未归档'))
  const sectors = currentView.sectors || {}
  document.querySelector('#sectorMeta').textContent = `${sectors.source || '数据未归档'} · 仅 A 股概念板块`
  renderBars(sectorChart, sectors.boards || [], row => `${row.theme} · ${row.name}`)
  renderHorizon()
  if (changedMarket) {
    portfolioContent.replaceChildren()
    const saved = portfolioResults.get(currentMarket)
    if (saved && saved.selection === portfolioStrategy.value) renderPortfolioResult(saved.payload)
    else document.querySelector('#portfolioMeta').textContent = portfolioKind.value === 'model' && !portfolioStrategy.value
      ? '没有已训练模型，请先在回测页训练模型。' : '默认不读取持仓；点击后按当前公开数据分析。'
  }
}

document.querySelectorAll('[data-horizon]').forEach(button => button.addEventListener('click', () => {
  selectedHorizon = button.dataset.horizon
  document.querySelectorAll('[data-horizon]').forEach(item => item.setAttribute('aria-pressed', String(item === button)))
  renderHorizon()
}))

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
    input.addEventListener('input', () => { draft[key] = input.value })
    label.append(input); container.append(label)
  }
  const reset = textNode('button', '恢复推荐默认值')
  reset.type = 'button'
  reset.addEventListener('click', () => {
    executionParameterDrafts.set(draftKey, Object.fromEntries(Object.entries(item.parameter_schema).map(([key, spec]) => [key, spec.default])))
    renderExecutionParameters(prefix, select, payload)
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
  renderExecutionParameters(select === rankingStrategy ? "ranking" : "portfolio", select, payload)
}

async function loadPortfolioStrategies() {
  if (portfolioAnalyzing) return
  try {
    const payload = await requestJson('/api/portfolio-strategies')
    if (portfolioAnalyzing) return
    const selected = portfolioStrategyChosen ? portfolioStrategy.value : payload.selected
    portfolioOptions = payload
    if (!portfolioStrategyChosen) portfolioKind.value = payload.selected_kind || (selected?.includes('@') ? 'model' : 'fixed')
    fillExecutionSelect(portfolioKind, portfolioStrategy, payload, selected)
    document.querySelector('#portfolioButton').disabled = portfolioStarting || portfolioAnalyzing || portfolioStrategy.disabled
    if (portfolioStrategy.disabled) document.querySelector('#portfolioMeta').textContent = '没有已训练模型，请先在回测页训练模型。'
  } catch (error) {
    portfolioStrategy.replaceChildren()
    portfolioStrategy.disabled = true
    document.querySelector('#portfolioButton').disabled = true
  }
}
portfolioKind.addEventListener('change', () => {
  portfolioStrategyChosen = true
  if (portfolioOptions) fillExecutionSelect(portfolioKind, portfolioStrategy, portfolioOptions)
  document.querySelector('#portfolioButton').disabled = !portfolioStrategy.value
  portfolioContent.replaceChildren()
  document.querySelector('#portfolioMeta').textContent = portfolioStrategy.value ? '请选择策略或 checkpoint 后分析持仓。' : '没有已训练模型，请先在回测页训练模型。'
})
portfolioStrategy.addEventListener('change', () => {
  portfolioStrategyChosen = true
  renderExecutionParameters("portfolio", portfolioStrategy, portfolioOptions)
  portfolioContent.replaceChildren()
  document.querySelector('#portfolioMeta').textContent = '策略已切换，请重新点击分析；本页选择不会修改保存配置。'
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
  const requestedMarket = currentMarket
  portfolioStarting = true
  button.disabled = true
  try {
    if (!portfolioStrategyChosen) await loadPortfolioStrategies()
    if (!portfolioStrategy.value || requestedMarket !== currentMarket) return
    const selectedStrategy = portfolioStrategy.value
    const parameters = executionParameters('portfolio')
    portfolioStrategy.disabled = true; portfolioKind.disabled = true
    portfolioStarting = false; portfolioAnalyzing = true
    document.querySelector('#portfolioMeta').textContent = '正在分析财报、估值、技术面与所选策略，生成三组研究配置…'
    portfolioContent.replaceChildren()
    const response = await fetch('/api/portfolio-analysis', {method: 'POST', headers: {'X-Report-Market': requestedMarket, 'X-Portfolio-Strategy': selectedStrategy, 'X-Strategy-Parameters': JSON.stringify(parameters)}})
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`)
    portfolioResults.set(requestedMarket, {selection: selectedStrategy, payload})
    if (requestedMarket === currentMarket) renderPortfolioResult(payload)
  } catch (error) {
    if (requestedMarket === currentMarket) document.querySelector('#portfolioMeta').textContent = `持仓分析失败：${error.message}`
  } finally {
    portfolioStarting = false; portfolioAnalyzing = false
    button.disabled = !portfolioStrategy.value
    portfolioStrategy.disabled = !portfolioStrategy.options.length; portfolioKind.disabled = false
  }
})

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;')
}

function inlineMarkdown(value) {
  return escapeHtml(value).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
}

function isTableDivider(line) {
  return /^\s*\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$/.test(line)
}

function cells(line) {
  return line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim())
}

function renderMarkdown(markdown) {
  const lines = markdown.replaceAll('\r\n', '\n').split('\n')
  const html = []
  let missingSection = false
  for (let index = 0; index < lines.length;) {
    const line = lines[index].trim()
    if (!line) { index += 1; continue }
    if (line.startsWith('# ')) { html.push(`<h1>${inlineMarkdown(line.slice(2))}</h1>`); index += 1; continue }
    if (line.startsWith('## ')) { missingSection = line.slice(3) === '数据缺失'; html.push(`<h2>${inlineMarkdown(line.slice(3))}</h2>`); index += 1; continue }
    if (line.startsWith('### ')) { html.push(`<h3>${inlineMarkdown(line.slice(4))}</h3>`); index += 1; continue }
    if (line.startsWith('|') && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const headings = cells(line)
      index += 2
      const rows = []
      while (index < lines.length && lines[index].trim().startsWith('|')) {
        rows.push(cells(lines[index]))
        index += 1
      }
      html.push('<div class="table-wrap"><table><thead><tr>' + headings.map(value => `<th>${inlineMarkdown(value)}</th>`).join('') + '</tr></thead><tbody>' + rows.map(row => '<tr>' + row.map(value => `<td>${inlineMarkdown(value)}</td>`).join('') + '</tr>').join('') + '</tbody></table></div>')
      continue
    }
    if (line.startsWith('- ')) {
      const items = []
      while (index < lines.length && lines[index].trim().startsWith('- ')) {
        let value = lines[index].trim().slice(2)
        // Older archives contain entire HTTP tracebacks; keep the source label readable.
        if (missingSection && value.length > 200 && value.includes('：')) value = `${value.split('：')[0]}；上游数据未取得，对应图表或分析可能不完整。`
        items.push(`<li>${inlineMarkdown(value)}</li>`)
        index += 1
      }
      html.push(`<ul>${items.join('')}</ul>`)
      continue
    }
    html.push(`<p>${inlineMarkdown(line)}</p>`)
    index += 1
  }
  return html.join('')
}

function showEmpty(title = '尚无可展示的日报', message = '交易日 09:20 和 14:30 生成后，此页面会自动展示日报及盘中复盘。', isError = false) {
  statusCard.hidden = false
  statusCard.classList.toggle('error', isError)
  statusCard.querySelector('h2').textContent = title
  statusCard.querySelector('p').textContent = message
  reportElement.hidden = true
  showInsights({})
  rebuildReportIndex()
}

function showReport(payload) {
  if (!payload.available || !payload.markdown) {
    showEmpty()
    return
  }
  reportElement.innerHTML = renderMarkdown(payload.markdown)
  reportElement.hidden = false
  statusCard.hidden = true
  updatedAt.textContent = `${payload.label || payload.date} · 更新于 ${new Date(payload.updated_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'})}（北京时间）`
  showInsights(payload)
  rebuildReportIndex()
  if (!initialSectionApplied) {
    initialSectionApplied = true
    jumpToSection(document.getElementById(window.location.hash.slice(1)))
  }
}

async function requestJson(url) {
  const response = await fetch(url, { cache: 'no-store' })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}

async function loadRankingStrategies(preferredId = '', force = false) {
  if (rankingJob || rankingStarting) return
  try {
    const payload = await requestJson('/api/ranking/strategies')
    if (rankingJob || rankingStarting) return
    const previous = preferredId && (force || !rankingStrategyChosen) ? preferredId : rankingStrategyChosen ? rankingStrategy.value : payload.selected
    rankingOptions = payload
    if (preferredId && (force || !rankingStrategyChosen)) rankingKind.value = previous.includes('@') ? 'model' : 'fixed'
    else if (!rankingStrategyChosen) rankingKind.value = payload.selected_kind || (previous?.includes('@') ? 'model' : 'fixed')
    fillExecutionSelect(rankingKind, rankingStrategy, payload, previous)
    rankMarketButton.disabled = rankingStrategy.disabled
    document.querySelector('#rankingOptionsNote').textContent = rankingStrategy.disabled ? '没有已训练模型，请先在回测页训练模型。' : ''
    return await loadRanking()
  } catch (error) {
    if (!rankingOptions) {
      rankingStrategy.replaceChildren(); rankingStrategy.disabled = true; rankMarketButton.disabled = true
    }
    document.querySelector('#rankingOptionsNote').textContent = `可用策略暂不可读：${error.message}`
    return false
  }
}

async function handleRankingPublication(payload) {
  if (payload.scope !== 'full_market' || !payload.id || payload.id === rankingPublicationId) return
  publishedRanking = payload
  researchUpdateButton.hidden = false
  researchUpdateButton.textContent = `${payload.strategy_name || '策略'}部署已更新 · 查看`
  if (!rankingJob && !rankingStarting && await loadRankingStrategies(payload.selection_id || '')) rankingPublicationId = payload.id
}

async function refreshRankingPublication() {
  try {
    const payload = await requestJson('/api/ranking/update')
    if (payload.available) await handleRankingPublication(payload)
  } catch (_) { /* SSE and the next refresh will recover a disconnected service. */ }
}

researchUpdateButton.addEventListener('click', async () => {
  if (publishedRanking && !rankingJob && !rankingStarting) {
    await loadRankingStrategies(publishedRanking.selection_id, true)
    rankingStrategyChosen = true
  }
  document.querySelector('#researchTools').open = true
  jumpToSection(document.querySelector('#rankingTools'))
})

async function restoreRankingJob() {
  if (rankingJob || rankingStarting) return
  try {
    const state = await requestJson('/api/ranking/active')
    if (!state?.job_id || state.status !== 'running' || rankingJob || rankingStarting) return
    const selection = state.selection_id || state.strategy_id
    if (rankingStrategyChosen && rankingStrategy.value !== selection) return
    rankingKind.value = selection.includes('@') ? 'model' : 'fixed'
    fillExecutionSelect(rankingKind, rankingStrategy, rankingOptions, selection)
    if (rankingStrategy.value !== selection) return
    rankingJob = state
    rankingStrategy.disabled = true; rankingKind.disabled = true; rankMarketButton.disabled = true
    rankMarketButton.textContent = '策略部署处理中…'
    await pollRankingJob()
  } catch (_) { /* A refresh can reattach once the service is available. */ }
}
rankingKind.addEventListener('change', () => {
  rankingStrategyChosen = true
  if (rankingOptions) fillExecutionSelect(rankingKind, rankingStrategy, rankingOptions)
  rankMarketButton.disabled = !rankingStrategy.value
  rankingPage = 0; rankingPayload = null; rankingContent.replaceChildren()
  document.querySelector('#rankingOptionsNote').textContent = rankingStrategy.value ? '' : '没有已训练模型，请先在回测页训练模型。'
  loadRanking()
})

function renderRanking(payload) {
  rankingPayload = payload
  if (!payload.available || payload.scope !== 'full_market') {
    rankingMeta.textContent = '所选策略尚未生成全市场盘中策略部署。点击“开始盘中策略部署”获取结果。'
    rankingContent.replaceChildren()
    return
  }
  const quality = payload.model_quality === 'exploratory' ? '探索性模拟' : (payload.model_quality === 'fixed_rule' ? '固定规则' : '研究模型')
  const signal = new Date(payload.signal_at || payload.generated_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'})
  const completed = new Date(payload.completed_at || payload.generated_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'})
  rankingMeta.textContent = `信号 ${signal} · 完成 ${completed}（北京时间） · ${quality} ${payload.strategy_name} · 全市场快照 ${payload.universe_count} 只 / 符合范围 ${payload.input_count} 只 / 有效 ${payload.valid_count} 只 / 缺失 ${payload.failed_count} 只${payload.coverage_complete ? '' : ' · 有效子集策略部署'}`
  if (payload.strategy_parameters) {
    const schema = rankingOptions?.strategies?.[rankingStrategy.value]?.parameter_schema || {}
    const used = Object.entries(payload.strategy_parameters).map(([key, value]) => `${schema[key]?.label || key}=${value}`).join(' · ')
    rankingMeta.textContent += ` · 本次结果参数：${used}`
  }
  if (payload.checkpoint_id) rankingMeta.textContent += ` · checkpoint ${payload.checkpoint_id}`
  const rows = payload.ranking || []
  const pageCount = Math.max(1, Math.ceil(rows.length / 50))
  rankingPage = Math.min(rankingPage, pageCount - 1)
  const tableWrap = document.createElement('div'); tableWrap.className = 'table-wrap'
  const table = document.createElement('table'), head = document.createElement('thead'), header = document.createElement('tr')
  for (const label of ['排名', '代码', '名称', '信号价', '策略分数', '入场状态']) header.append(textNode('th', label))
  head.append(header)
  const body = document.createElement('tbody')
  for (const item of rows.slice(rankingPage * 50, (rankingPage + 1) * 50)) {
    const row = document.createElement('tr')
    const status = payload.deployment_candidates?.includes(item.symbol) ? '部署候选' : item.entry === null ? '模型评分' : item.entry ? '已触发' : '未触发'
    for (const value of [item.rank, item.symbol, item.name, item.price, item.score, status]) row.append(textNode('td', value))
    body.append(row)
  }
  table.append(head, body); tableWrap.append(table)
  const pagination = document.createElement('div'); pagination.className = 'ranking-pagination'
  for (const [label, direction] of [['上一页', -1], ['下一页', 1]]) {
    const button = textNode('button', label); button.type = 'button'
    button.disabled = direction < 0 ? rankingPage === 0 : rankingPage >= pageCount - 1
    button.addEventListener('click', () => { rankingPage += direction; renderRanking(rankingPayload) })
    pagination.append(button)
  }
  pagination.append(textNode('span', `第 ${rankingPage + 1} / ${pageCount} 页 · 共 ${rows.length} 条，每页 50 条`))
  const notes = textNode('p', payload.note, 'ranking-note')
  const missing = document.createElement('details'); missing.className = 'position-methods'
  if (payload.failures?.length) {
    missing.append(textNode('summary', `查看 ${payload.failures.length} 只缺失评分及原因`))
    for (const item of payload.failures) missing.append(textNode('p', `${item.symbol}：${item.reason}`, 'ranking-note'))
  }
  rankingContent.replaceChildren(tableWrap, pagination, notes, missing)
}

async function loadRanking() {
  if (rankingJob || rankingStarting || !rankingStrategy.value || rankingStrategy.disabled) return false
  const strategy = rankingStrategy.value
  try {
    const payload = await requestJson(`/api/ranking/latest?scope=full_market&strategy=${encodeURIComponent(strategy)}`)
    if (rankingStrategy.value === strategy && !rankingJob && !rankingStarting) { renderRanking(payload); return true }
  } catch (error) {
    if (rankingStrategy.value === strategy && !rankingJob && !rankingStarting) rankingMeta.textContent = `排名暂不可读：${error.message}`
  }
  return false
}

function finishRankingJob() {
  rankingJob = null
  rankingStarting = false
  clearTimeout(rankingPollTimer)
  rankingStrategy.disabled = !rankingStrategy.options.length
  rankingKind.disabled = false
  rankMarketButton.disabled = !rankingStrategy.value
  rankMarketButton.textContent = '开始盘中策略部署'
}

async function pollRankingJob() {
  if (!rankingJob) return
  const id = rankingJob.job_id
  try {
    const state = await requestJson(`/api/ranking/jobs/${encodeURIComponent(id)}`)
    if (rankingJob?.job_id !== id) return
    rankingMeta.textContent = `${state.strategy_name} · ${state.message}${state.input_count ? ` · ${state.processed_count || 0}/${state.input_count}` : ''}`
    if (state.status === 'completed') {
      finishRankingJob(); rankingPage = 0; renderRanking(state.result); return
    }
    if (state.status === 'failed') { finishRankingJob(); return }
  } catch (error) {
    if (rankingJob?.job_id !== id) return
    if (error.message === 'HTTP 404') {
      finishRankingJob(); rankingMeta.textContent = '部署任务已不存在，请重新发起。'; return
    }
    rankingMeta.textContent = `进度暂不可读：${error.message}；正在重试`
  }
  rankingPollTimer = setTimeout(pollRankingJob, 2000)
}

rankMarketButton.addEventListener('click', async () => {
  if (rankingJob || rankingStarting || !rankingStrategy.value) return
  rankingStarting = true
  const strategy = rankingStrategy.value
  rankingStrategy.disabled = true; rankingKind.disabled = true; rankMarketButton.disabled = true
  rankMarketButton.textContent = '策略部署处理中…'
  rankingMeta.textContent = '正在启动全市场盘中策略部署…'
  try {
    const requestedParameters = executionParameters('ranking')
    const response = await fetch('/api/ranking/start', {method: 'POST', headers: {'X-Ranking-Strategy': strategy, 'X-Strategy-Parameters': JSON.stringify(requestedParameters), 'X-Report-Market': 'a'}})
    const state = await response.json()
    if (response.status === 409 && ((state.selection_id || state.strategy_id) !== strategy
        || Object.entries(requestedParameters).some(([key, value]) => state.strategy_parameters?.[key] !== value))) {
      throw Error(`已有 ${state.strategy_name} 全市场策略部署任务正在运行，请完成后重试。`)
    }
    if (!response.ok && response.status !== 409) throw Error(state.error || `HTTP ${response.status}`)
    rankingJob = state
    rankingStarting = false
    rankingContent.replaceChildren(); rankingPayload = null
    await pollRankingJob()
  } catch (error) {
    finishRankingJob(); rankingMeta.textContent = `盘中策略部署未启动：${error.message}`
  }
})
rankingStrategy.addEventListener('change', () => {
  rankingStrategyChosen = true; rankingPage = 0; rankingPayload = null
  renderExecutionParameters("ranking", rankingStrategy, rankingOptions)
  rankingContent.replaceChildren(); loadRanking()
})

async function loadIndex(preferredId = '') {
  try {
    const index = await requestJson('/api/reports')
    reportDate.replaceChildren()
    if (!index.reports.length) {
      reportDate.add(new Option('暂无日报', ''))
      reportDate.disabled = true
      showEmpty()
    } else {
      index.reports.forEach(item => reportDate.add(new Option(item.label || item.date, item.id || item.date)))
      reportDate.disabled = false
      const selected = preferredId || index.reports[0].id || index.reports[0].date
      reportDate.value = selected
      showReport(await requestJson(`/api/reports/${encodeURIComponent(selected)}`))
    }
    connectionState.textContent = '已连接'
  } catch (error) {
    connectionState.textContent = '连接失败'
    updatedAt.textContent = '无法连接本地日报服务'
    showEmpty('日报服务不可用', '请确认启动器正在运行，并检查局域网或防火墙设置。', true)
  }
}

reportDate.addEventListener('change', () => loadIndex(reportDate.value))
refreshButton.addEventListener('click', () => { loadIndex(reportDate.value); loadRankingStrategies(); loadPortfolioStrategies() })
loadIndex(requestedReport)
loadRankingStrategies().then(async () => { await restoreRankingJob(); await refreshRankingPublication() })
loadPortfolioStrategies()
setInterval(() => loadIndex(reportDate.value), 30_000)
setInterval(loadRanking, 30_000)
setInterval(refreshRankingPublication, 30_000)

const reportEvents = new EventSource('/api/events')
reportEvents.addEventListener('report', event => {
  const payload = JSON.parse(event.data)
  loadIndex(payload.id || payload.date)
})
reportEvents.addEventListener('ranking', event => handleRankingPublication(JSON.parse(event.data)))
reportEvents.addEventListener('open', () => { connectionState.textContent = '实时连接'; refreshRankingPublication(); restoreRankingJob() })
reportEvents.addEventListener('error', () => { connectionState.textContent = '自动刷新中' })
