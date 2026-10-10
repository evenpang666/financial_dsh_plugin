const reportElement = document.querySelector('#report')
const statusCard = document.querySelector('#statusCard')
const updatedAt = document.querySelector('#updatedAt')
const connectionState = document.querySelector('#connectionState')
const reportDate = document.querySelector('#reportDate')
const refreshButton = document.querySelector('#refreshButton')
const requestedReport = new URLSearchParams(window.location.search).get('report') || ''
const rankingMeta = document.querySelector('#rankingMeta')
const rankingContent = document.querySelector('#rankingContent')
const backtestMeta = document.querySelector('#backtestMeta')
const backtestContent = document.querySelector('#backtestContent')
const globalChart = document.querySelector('#globalChart')
const sectorChart = document.querySelector('#sectorChart')
const horizonContent = document.querySelector('#horizonContent')
const portfolioContent = document.querySelector('#portfolioContent')
let currentView = {}
let currentMarket = 'a'
let selectedHorizon = 'long'
let displayedReportId = ''

function textNode(tag, value, className = '') {
  const element = document.createElement(tag)
  element.textContent = String(value ?? '—')
  if (className) element.className = className
  return element
}

function renderBars(target, rows, nameKey) {
  target.replaceChildren()
  if (!rows?.length) { target.append(textNode('p', '当前未取得可核验数据。', 'empty-note')); return }
  for (const row of rows) {
    const value = row.change_pct == null ? NaN : Number(row.change_pct)
    const item = document.createElement('div'); item.className = 'bar-row'
    item.append(textNode('span', nameKey(row), 'bar-name'))
    const track = document.createElement('div'); track.className = 'bar-track'
    const fill = document.createElement('span'); fill.className = `bar-fill ${value >= 0 ? 'up' : 'down'}`
    fill.style.width = `${Number.isFinite(value) ? Math.min(100, Math.abs(value) * 12) : 0}%`; track.append(fill); item.append(track)
    item.append(textNode('strong', Number.isFinite(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` : '—', value >= 0 ? 'positive' : 'negative'))
    target.append(item)
  }
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
  const global = currentView.global_markets || {}
  document.querySelector('#globalMeta').textContent = `${global.source || '数据未归档'} · 各市场行情时间不同，请查看日报明细`
  renderBars(globalChart, global.indices || [], row => `${row.region} · ${row.name}`)
  const breadth = currentView.market_panorama?.market_breadth || {}
  const marketState = currentView.market_panorama?.market_state || {}
  const breadthElement = document.querySelector('#marketBreadth')
  breadthElement.replaceChildren()
  if (breadth.advancing != null && breadth.declining != null) {
    const up = Number(breadth.advancing), down = Number(breadth.declining)
    const total = up + down
    breadthElement.append(textNode('p', `${marketState.summary || '大盘观察'} · 上涨 ${up} / 下跌 ${down} · 风险 ${marketState.risk_level || '待评估'}`))
    const track = document.createElement('div'); track.className = 'breadth-track'
    const fill = document.createElement('span'); fill.style.width = `${total > 0 ? up / total * 100 : 0}%`
    track.append(fill); breadthElement.append(track)
  }
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

document.querySelector('#portfolioButton').addEventListener('click', async event => {
  const button = event.currentTarget
  const requestedMarket = currentMarket
  const requestedReportId = displayedReportId
  button.disabled = true
  document.querySelector('#portfolioMeta').textContent = '正在读取持仓并逐只分析财报、估值和技术面…'
  portfolioContent.replaceChildren()
  try {
    const response = await fetch('/api/portfolio-analysis', {method: 'POST', headers: {'X-Report-Market': requestedMarket}})
    const payload = await response.json()
    if (requestedReportId !== displayedReportId) return
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`)
    document.querySelector('#portfolioMeta').textContent = payload.note || ''
    if (!payload.available) { portfolioContent.append(textNode('p', '当前市场没有已保存持仓。', 'empty-note')); return }
    const list = document.createElement('div'); list.className = 'watch-grid'
    for (const item of payload.holdings) {
      const analysis = item.analysis || {}
      const card = document.createElement('article'); card.className = 'watch-card'
      card.append(textNode('h3', `${analysis.name || item.holding?.name || item.holding?.symbol} · ${item.holding?.symbol}`))
      card.append(textNode('p', item.strategy))
      card.append(textNode('small', `财报期 ${analysis.latest_report_period || '未取得'} · ${analysis.long_term?.evidence?.join('；') || '基本面待补充'}`))
      card.append(textNode('small', `技术面：${analysis.short_term?.evidence?.join('；') || '待补充'}；缺失：${analysis.unavailable?.join('；') || '无'}`))
      list.append(card)
    }
    portfolioContent.append(list)
  } catch (error) { document.querySelector('#portfolioMeta').textContent = `持仓分析失败：${error.message}` }
  finally { button.disabled = false }
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
  for (let index = 0; index < lines.length;) {
    const line = lines[index].trim()
    if (!line) { index += 1; continue }
    if (line.startsWith('# ')) { html.push(`<h1>${inlineMarkdown(line.slice(2))}</h1>`); index += 1; continue }
    if (line.startsWith('## ')) { html.push(`<h2>${inlineMarkdown(line.slice(3))}</h2>`); index += 1; continue }
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
        items.push(`<li>${inlineMarkdown(lines[index].trim().slice(2))}</li>`)
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
}

function showReport(payload) {
  if (!payload.available || !payload.markdown) {
    showEmpty()
    return
  }
  reportElement.innerHTML = renderMarkdown(payload.markdown)
  reportElement.hidden = false
  statusCard.hidden = true
  updatedAt.textContent = `${payload.label || payload.date} · 更新于 ${new Date(payload.updated_at).toLocaleString('zh-CN')}`
  showInsights(payload)
}

async function requestJson(url) {
  const response = await fetch(url, { cache: 'no-store' })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}

async function loadRanking() {
  try {
    const payload = await requestJson('/api/ranking/latest')
    if (!payload.ranking?.length) {
      rankingMeta.textContent = '尚未生成排名。请在控制面板依次点击“一键训练”“一键排序”。'
      rankingContent.replaceChildren()
      return
    }
    const quality = payload.model_quality === "exploratory" ? "探索性模拟 · 未正式验证" : "已验证"
    const algorithm = payload.algorithm === 'xgboost' ? 'XGBoost' : 'Elastic Net'
    rankingMeta.textContent = `生成于 ${new Date(payload.generated_at).toLocaleString('zh-CN')} · ${quality} ${algorithm} 模型 ${payload.model_id} · ${payload.candidate_count} 只有效候选`
    const description = document.createElement('p')
    description.className = 'ranking-note'
    description.textContent = payload.note || ''
    const tableWrap = document.createElement('div')
    tableWrap.className = 'table-wrap'
    const table = document.createElement('table')
    const head = document.createElement('thead')
    const headRow = document.createElement('tr')
    for (const label of ['排名', '代码', '名称', '快照价', '综合分']) {
      const cell = document.createElement('th'); cell.textContent = label; headRow.append(cell)
    }
    head.append(headRow)
    const body = document.createElement('tbody')
    for (const item of payload.ranking) {
      const row = document.createElement('tr')
      for (const value of [item.rank, item.symbol, item.name, item.price, item.score]) {
        const cell = document.createElement('td'); cell.textContent = String(value ?? '—'); row.append(cell)
      }
      body.append(row)
    }
    table.append(head, body)
    tableWrap.append(table)
    rankingContent.replaceChildren(tableWrap, description)
  } catch (error) {
    rankingMeta.textContent = `排名暂不可读：${error.message}`
  }
}

async function loadBacktest() {
  try {
    const payload = await requestJson('/api/backtest/latest')
    backtestContent.replaceChildren()
    if (!payload.available) {
      backtestMeta.textContent = '暂无回测。请在控制面板点击“一键训练”或“一键回测”。'
      return
    }
    const stats = payload.stats || {}
    backtestMeta.textContent = `${payload.first_day} 至 ${payload.last_day} · ${payload.days} 个交易日 · ${payload.slot} 信号 / ${payload.execution} 模拟成交 · 更新于 ${new Date(payload.generated_at).toLocaleString('zh-CN')}`
    const grid = document.createElement('div'); grid.className = 'backtest-grid'
    const metrics = [
      ['累计收益', stats.total_return == null ? '—' : `${(Number(stats.total_return) * 100).toFixed(2)}%`],
      ['最大回撤', stats.max_drawdown == null ? '—' : `${(Number(stats.max_drawdown) * 100).toFixed(2)}%`],
      ['样本外 Rank IC', payload.oos_rank_ic?.rank_ic ?? '—'],
      ['模拟交易 / 费用', `${stats.trades ?? '—'} 笔 / ${stats.costs == null ? '—' : Number(stats.costs).toLocaleString('zh-CN')} 元`],
    ]
    for (const [label, value] of metrics) {
      const card = document.createElement('div'); card.className = 'backtest-metric'
      card.append(textNode('small', label), textNode('strong', value)); grid.append(card)
    }
    backtestContent.append(grid)
    backtestContent.append(textNode('p', '当前股票池按本次训练时点选取，历史结果存在选股与存续偏差；成交价、滑点和费用为模拟，结果仅供研究。', 'ranking-note'))
  } catch (error) {
    backtestMeta.textContent = `回测暂不可读：${error.message}`
  }
}

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
refreshButton.addEventListener('click', () => { loadIndex(reportDate.value); loadRanking(); loadBacktest() })
loadIndex(requestedReport)
loadRanking()
loadBacktest()
setInterval(() => loadIndex(reportDate.value), 30_000)
setInterval(loadRanking, 30_000)
setInterval(loadBacktest, 30_000)

const reportEvents = new EventSource('/api/events')
reportEvents.addEventListener('report', event => {
  const payload = JSON.parse(event.data)
  loadIndex(payload.id || payload.date)
})
reportEvents.addEventListener('ranking', loadRanking)
reportEvents.addEventListener('open', () => { connectionState.textContent = '实时连接' })
reportEvents.addEventListener('error', () => { connectionState.textContent = '自动刷新中' })
