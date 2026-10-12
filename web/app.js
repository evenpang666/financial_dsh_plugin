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
let portfolioStrategyChosen = false
let portfolioAnalyzing = false
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
  const changedReport = (payload.id || '') !== displayedReportId
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
  if (changedReport) {
    portfolioContent.replaceChildren()
    document.querySelector('#portfolioMeta').textContent = '默认不读取持仓；点击后按当前公开数据分析。'
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

async function loadPortfolioStrategies() {
  if (portfolioAnalyzing) return
  try {
    const payload = await requestJson('/api/portfolio-strategies')
    if (portfolioAnalyzing) return
    const selected = portfolioStrategyChosen ? portfolioStrategy.value : payload.selected
    portfolioStrategy.replaceChildren()
    for (const [id, strategy] of Object.entries(payload.strategies || {})) portfolioStrategy.add(new Option(strategy.name, id))
    portfolioStrategy.value = selected
    portfolioStrategy.disabled = false
  } catch (error) {
    portfolioStrategy.replaceChildren(new Option('沿用控制面板所选策略', ''))
    portfolioStrategy.disabled = true
  }
}
portfolioStrategy.addEventListener('change', () => {
  portfolioStrategyChosen = true
  portfolioContent.replaceChildren()
  document.querySelector('#portfolioMeta').textContent = '策略已切换，请重新点击分析；本页选择不会修改控制面板配置。'
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

document.querySelector('#portfolioButton').addEventListener('click', async event => {
  const button = event.currentTarget
  const requestedMarket = currentMarket
  const requestedReportId = displayedReportId
  button.disabled = true
  if (!portfolioStrategyChosen) await loadPortfolioStrategies()
  if (requestedReportId !== displayedReportId) { button.disabled = false; return }
  const selectedStrategy = portfolioStrategy.value
  const strategyWasDisabled = portfolioStrategy.disabled
  portfolioStrategy.disabled = true
  portfolioAnalyzing = true
  document.querySelector('#portfolioMeta').textContent = '正在分析财报、估值、技术面与所选策略，生成三组研究配置…'
  portfolioContent.replaceChildren()
  try {
    const response = await fetch('/api/portfolio-analysis', {method: 'POST', headers: {'X-Report-Market': requestedMarket, 'X-Portfolio-Strategy': selectedStrategy}})
    const payload = await response.json()
    if (requestedReportId !== displayedReportId) return
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`)
    document.querySelector('#portfolioMeta').textContent = payload.note || ''
    if (!payload.available) { portfolioContent.append(textNode('p', '当前市场没有已保存持仓。', 'empty-note')); return }
    renderPositionSummary(payload)
    const list = document.createElement('div'); list.className = 'watch-grid'
    for (const item of payload.holdings) {
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
  } catch (error) { document.querySelector('#portfolioMeta').textContent = `持仓分析失败：${error.message}` }
  finally {
    button.disabled = false; portfolioAnalyzing = false; portfolioStrategy.disabled = strategyWasDisabled
    if (strategyWasDisabled) loadPortfolioStrategies()
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

async function loadRankingStrategies() {
  if (rankingJob || rankingStarting) return
  try {
    const payload = await requestJson('/api/ranking/strategies')
    if (rankingJob || rankingStarting) return
    const previous = rankingStrategyChosen ? rankingStrategy.value : payload.selected
    rankingStrategy.replaceChildren()
    for (const [kind, label] of [['fixed_rule', '固定交易策略'], ['model', '已训练因子模型']]) {
      const group = document.createElement('optgroup'); group.label = label
      for (const [id, strategy] of Object.entries(payload.strategies || {})) {
        if (strategy.kind !== kind) continue
        group.append(new Option(strategy.name + (strategy.trained_at ? ` · 训练 ${strategy.trained_at.slice(0, 10)}` : ''), id))
      }
      if (group.children.length) rankingStrategy.append(group)
    }
    if ([...rankingStrategy.options].some(option => option.value === previous)) rankingStrategy.value = previous
    rankingStrategy.disabled = !rankingStrategy.options.length
    rankMarketButton.disabled = rankingStrategy.disabled
    document.querySelector('#rankingOptionsNote').textContent = Object.entries(payload.unavailable_models || {})
      .map(([id, reason]) => `${id === 'xgboost' ? 'XGBoost' : 'Elastic Net'}：${reason}`).join(' ')
    await loadRanking()
  } catch (error) {
    document.querySelector('#rankingOptionsNote').textContent = `可用策略暂不可读：${error.message}`
  }
}

function renderRanking(payload) {
  rankingPayload = payload
  if (!payload.available || payload.scope !== 'full_market') {
    rankingMeta.textContent = '所选策略尚未生成全市场盘中排序。点击“开始盘中排序”获取结果。'
    rankingContent.replaceChildren()
    return
  }
  const quality = payload.model_quality === 'exploratory' ? '探索性模拟' : (payload.model_quality === 'fixed_rule' ? '固定规则' : '研究模型')
  const signal = new Date(payload.signal_at || payload.generated_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'})
  const completed = new Date(payload.completed_at || payload.generated_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'})
  rankingMeta.textContent = `信号 ${signal} · 完成 ${completed}（北京时间） · ${quality} ${payload.strategy_name} · 全市场快照 ${payload.universe_count} 只 / 符合范围 ${payload.input_count} 只 / 有效 ${payload.valid_count} 只 / 缺失 ${payload.failed_count} 只${payload.coverage_complete ? '' : ' · 有效子集排序'}`
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
    const status = item.entry === null ? '模型评分' : item.entry ? '已触发' : '未触发'
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
  if (rankingJob || rankingStarting || !rankingStrategy.value || rankingStrategy.disabled) return
  const strategy = rankingStrategy.value
  try {
    const payload = await requestJson(`/api/ranking/latest?scope=full_market&strategy=${encodeURIComponent(strategy)}`)
    if (rankingStrategy.value === strategy && !rankingJob && !rankingStarting) renderRanking(payload)
  } catch (error) {
    if (rankingStrategy.value === strategy && !rankingJob && !rankingStarting) rankingMeta.textContent = `排名暂不可读：${error.message}`
  }
}

function finishRankingJob() {
  rankingJob = null
  rankingStarting = false
  clearTimeout(rankingPollTimer)
  rankingStrategy.disabled = false
  rankMarketButton.disabled = false
  rankMarketButton.textContent = '开始盘中排序'
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
    rankingMeta.textContent = `进度暂不可读：${error.message}；正在重试`
  }
  rankingPollTimer = setTimeout(pollRankingJob, 2000)
}

rankMarketButton.addEventListener('click', async () => {
  if (rankingJob || rankingStarting || !rankingStrategy.value) return
  rankingStarting = true
  const strategy = rankingStrategy.value
  rankingStrategy.disabled = true; rankMarketButton.disabled = true
  rankMarketButton.textContent = '排序处理中…'
  rankingMeta.textContent = '正在启动全市场盘中排序…'
  try {
    const response = await fetch('/api/ranking/start', {method: 'POST', headers: {'X-Ranking-Strategy': strategy, 'X-Report-Market': 'a'}})
    const state = await response.json()
    if (response.status === 409 && state.strategy_id !== strategy) {
      throw Error(`已有 ${state.strategy_name} 全市场排序任务正在运行，请完成后重试。`)
    }
    if (!response.ok && response.status !== 409) throw Error(state.error || `HTTP ${response.status}`)
    rankingJob = state
    rankingStarting = false
    rankingContent.replaceChildren(); rankingPayload = null
    await pollRankingJob()
  } catch (error) {
    finishRankingJob(); rankingMeta.textContent = `盘中排序未启动：${error.message}`
  }
})
rankingStrategy.addEventListener('change', () => {
  rankingStrategyChosen = true; rankingPage = 0; rankingPayload = null
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
loadRankingStrategies()
loadPortfolioStrategies()
setInterval(() => loadIndex(reportDate.value), 30_000)
setInterval(loadRanking, 30_000)

const reportEvents = new EventSource('/api/events')
reportEvents.addEventListener('report', event => {
  const payload = JSON.parse(event.data)
  loadIndex(payload.id || payload.date)
})
reportEvents.addEventListener('ranking', loadRanking)
reportEvents.addEventListener('open', () => { connectionState.textContent = '实时连接' })
reportEvents.addEventListener('error', () => { connectionState.textContent = '自动刷新中' })
