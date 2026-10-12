const backtestMeta = document.querySelector('#backtestMeta')
const backtestContent = document.querySelector('#backtestContent')
const connectionState = document.querySelector('#connectionState')
const backtestIndex = document.querySelector('#backtestIndex')
const backtestIndexPanel = document.querySelector('#backtestIndexPanel')
const backtestIndexSections = document.querySelector('#backtestIndexSections')
const indexBreakpoint = window.matchMedia('(max-width: 1000px)')
const indexSections = [
  ['backtestResults', '策略表现'], ['backtestTable', '策略对比表'],
  ['backtestHistory', '历史记录'], ['backtestMetrics', '关键指标'],
  ['backtestStress', '压力测试'], ['backtestEquity', '累计净值'],
  ['backtestDrawdown', '资金回撤'], ['backtestDownloads', '数据下载'],
  ['backtestMethod', '回测口径'],
]
let indexTargets = []
let indexFrame = 0
let initialSectionApplied = false

function updateIndexPosition() {
  indexFrame = 0
  const topbarBottom = document.querySelector('.topbar').getBoundingClientRect().bottom
  const navigationBoundary = indexBreakpoint.matches
    ? Math.max(topbarBottom, backtestIndexPanel.querySelector('summary').getBoundingClientRect().bottom) + 20
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
  if (current && backtestIndexPanel.open && !indexBreakpoint.matches) {
    const linkRect = current.link.getBoundingClientRect(), navRect = backtestIndex.getBoundingClientRect()
    if (linkRect.top < navRect.top) backtestIndex.scrollTop -= navRect.top - linkRect.top + 8
    else if (linkRect.bottom > navRect.bottom) backtestIndex.scrollTop += linkRect.bottom - navRect.bottom + 8
  }
}

function scheduleIndexPosition() {
  if (!indexFrame) indexFrame = requestAnimationFrame(updateIndexPosition)
}

function jumpToSection(target) {
  if (!target) return
  if (indexBreakpoint.matches) backtestIndexPanel.open = false
  requestAnimationFrame(() => {
    target.scrollIntoView({behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start'})
    if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1')
    target.focus({preventScroll: true})
    scheduleIndexPosition()
  })
}

function rebuildBacktestIndex(ready = false) {
  const previousScroll = backtestIndex.scrollTop
  backtestIndexSections.replaceChildren()
  for (const [id, title] of indexSections) {
    if (!document.getElementById(id)) continue
    const link = document.createElement('a')
    link.href = `#${id}`
    link.setAttribute('data-index-target', '')
    link.append(textNode('span', String(backtestIndexSections.children.length + 2).padStart(2, '0')), document.createTextNode(title))
    backtestIndexSections.append(link)
  }
  indexTargets = [...backtestIndex.querySelectorAll('[data-index-target]')]
    .map(link => ({link, target: document.getElementById(link.hash.slice(1))}))
    .filter(item => item.target)
  backtestIndex.scrollTop = previousScroll
  scheduleIndexPosition()
  if (ready && !initialSectionApplied) {
    const target = document.getElementById(window.location.hash.slice(1))
    if (target) { initialSectionApplied = true; jumpToSection(target) }
  }
}

backtestIndexPanel.open = !indexBreakpoint.matches
indexBreakpoint.addEventListener('change', () => {
  backtestIndexPanel.open = !indexBreakpoint.matches
  scheduleIndexPosition()
})
backtestIndex.addEventListener('click', event => {
  const link = event.target.closest('a[href^="#"]')
  if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return
  const target = document.getElementById(link.hash.slice(1))
  if (!target) return
  event.preventDefault()
  if (window.location.hash !== link.hash) history.pushState(null, '', link.hash)
  jumpToSection(target)
})
backtestIndexPanel.addEventListener('keydown', event => {
  if (event.key === 'Escape' && indexBreakpoint.matches) {
    backtestIndexPanel.open = false
    backtestIndexPanel.querySelector('summary').focus()
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

async function requestJson(url) {
  const response = await fetch(url, { cache: 'no-store' })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}

let selectedBacktestRun = ''
const percent = value => value == null || !Number.isFinite(Number(value)) ? '—' : `${(Number(value) * 100).toFixed(2)}%`

function strategyChart(points, key, title, format) {
  const figure = document.createElement('figure'); figure.className = 'strategy-chart'
  figure.append(textNode('figcaption', title))
  if (!points.length) { figure.append(textNode('p', '暂无可绘制的净值数据。', 'empty-note')); return figure }
  const ns = 'http://www.w3.org/2000/svg'
  const svg = document.createElementNS(ns, 'svg')
  svg.setAttribute('viewBox', '0 0 760 240'); svg.setAttribute('role', 'img')
  svg.setAttribute('aria-label', `${title}，${points[0].day} 至 ${points.at(-1).day}`)
  const add = (tag, attrs, text = '') => {
    const item = document.createElementNS(ns, tag)
    for (const [name, value] of Object.entries(attrs)) item.setAttribute(name, value)
    if (text) item.textContent = text
    svg.append(item); return item
  }
  const values = points.map(point => Number(point[key]))
  let low = Math.min(...values, key === 'drawdown' ? 0 : 1)
  let high = Math.max(...values, key === 'drawdown' ? 0 : 1)
  const pad = Math.max((high - low) * 0.1, key === 'drawdown' ? 0.005 : 0.01)
  low -= pad; high += pad
  const x = i => 66 + i / Math.max(points.length - 1, 1) * 676
  const y = value => 18 + (high - value) / (high - low) * 178
  for (let i = 0; i < 5; i++) {
    const value = low + (high - low) * i / 4
    add('line', { x1: 66, x2: 742, y1: y(value), y2: y(value), class: 'chart-grid' })
    add('text', { x: 58, y: y(value) + 4, 'text-anchor': 'end', class: 'chart-axis' }, format(value))
  }
  add('polyline', { points: points.map((point, i) => `${x(i)},${y(Number(point[key]))}`).join(' '),
                    fill: 'none', class: key === 'drawdown' ? 'chart-line drawdown-line' : 'chart-line' })
  for (const i of [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])]) {
    add('text', { x: x(i), y: 222, 'text-anchor': i === 0 ? 'start' : (i === points.length - 1 ? 'end' : 'middle'), class: 'chart-axis' }, points[i].day)
  }
  for (const [i, point] of points.entries()) {
    const dot = add('circle', { cx: x(i), cy: y(Number(point[key])), r: 5, class: 'chart-point' })
    const hint = document.createElementNS(ns, 'title'); hint.textContent = `${point.day}：${format(Number(point[key]))}`
    dot.append(hint)
  }
  figure.append(svg); return figure
}

async function loadBacktestDetail(runId, target) {
  selectedBacktestRun = runId
  target.replaceChildren(textNode('p', '正在读取回测曲线…', 'empty-note'))
  rebuildBacktestIndex()
  try {
    const payload = await requestJson(`/api/backtests/${encodeURIComponent(runId)}`)
    if (selectedBacktestRun !== runId || !target.isConnected) return
    const stats = payload.stats || {}
    target.replaceChildren(textNode('h3', payload.strategy_name))
    if (payload.engine_version !== 6) target.append(textNode('p', '这是旧版引擎的历史结果。旧版经典策略可能扣除了 120 日预备期，早期模型回测可能滚动拟合；当前引擎按所选日期与参数评估，并冻结模型权重，请按各自口径比较。', 'ranking-note'))
    target.append(textNode('p', `${payload.first_day} 至 ${payload.last_day} · ${payload.days} 日 · ${payload.slot} 信号 / ${payload.execution} 成交 · ${new Date(payload.generated_at).toLocaleString('zh-CN')}`, 'insight-meta'))
    if (payload.requested_start) target.append(textNode('p', `所选日期：${payload.requested_start} 至 ${payload.requested_end} · 候选/持仓上限：${payload.top_n} 只`, 'ranking-note'))
    if (payload.strategy_description) target.append(textNode('p', payload.strategy_description, 'ranking-note'))
    if (payload.checkpoint_id) target.append(textNode('p', `checkpoint：${payload.checkpoint_id} · 训练时间：${payload.checkpoint_trained_at} · 训练标签截至：${payload.training_label_end} · 回测期间不重新拟合`, 'ranking-note'))
    if (payload.holding_policy) target.append(textNode('p', payload.holding_policy, 'ranking-note'))
    if (payload.audit?.selection_bias) target.append(textNode('p', payload.audit.selection_bias, 'ranking-note'))
    const metrics = [
      ['累计收益', percent(stats.total_return)], ['年化收益', percent(stats.annualized_return)],
      ['最大回撤', percent(stats.max_drawdown)], ['Sharpe', stats.sharpe ?? '—'],
      ['模拟交易', `${stats.trades ?? '—'} 笔`], ['显式费用', `${Number(stats.costs || 0).toLocaleString('zh-CN')} 元`],
      ['估计滑点', `${Number(stats.slippage_costs || 0).toLocaleString('zh-CN')} 元`], ['样本外 Rank IC', payload.oos_rank_ic?.rank_ic ?? '不适用'],
      ['未成交 / 受限订单', stats.blocked_orders ?? '—'], ['部分成交', stats.partial_fills ?? '—'],
      ['分红税预留', `${Number(stats.dividend_tax_reserved || 0).toLocaleString('zh-CN')} 元`],
      ['最高成交参与率', percent(stats.max_fill_participation)],
    ]
    const grid = document.createElement('div'); grid.className = 'backtest-grid'; grid.id = 'backtestMetrics'
    for (const [label, value] of metrics) {
      const card = document.createElement('div'); card.className = 'backtest-metric'
      card.append(textNode('small', label), textNode('strong', String(value))); grid.append(card)
    }
    target.append(grid)
    if (payload.stress_tests?.length) {
      target.append(textNode('h4', '费用与流动性压力测试（相同信号，不重新选模）'))
      const wrap = document.createElement('div'); wrap.className = 'table-wrap'; wrap.id = 'backtestStress'
      const table = document.createElement('table'); const head = document.createElement('thead'); const row = document.createElement('tr')
      for (const label of ['情景', '累计收益', '最大回撤', '受限订单', '部分成交']) row.append(textNode('th', label))
      head.append(row); const body = document.createElement('tbody')
      for (const scenario of payload.stress_tests) {
        const item = document.createElement('tr')
        for (const value of [scenario.scenario, percent(scenario.stats.total_return), percent(scenario.stats.max_drawdown), scenario.stats.blocked_orders, scenario.stats.partial_fills]) item.append(textNode('td', String(value)))
        body.append(item)
      }
      table.append(head, body); wrap.append(table); target.append(wrap)
    }
    const curves = (payload.curve || []).filter(point => Number.isFinite(Number(point.net_value)) && Number.isFinite(Number(point.drawdown)))
    const equityChart = strategyChart(curves, 'net_value', '累计净值（初始值 1）', value => value.toFixed(3))
    equityChart.id = 'backtestEquity'
    const drawdownChart = strategyChart(curves, 'drawdown', '资金回撤', percent)
    drawdownChart.id = 'backtestDrawdown'
    target.append(equityChart, drawdownChart)
    const links = document.createElement('div'); links.className = 'backtest-downloads'; links.id = 'backtestDownloads'
    for (const [name, label] of [['equity.csv', '下载净值数据'], ['trades.csv', '下载交易明细']]) {
      if (!payload.downloads?.[name]) continue
      const link = document.createElement('a'); link.href = payload.downloads[name]; link.download = `${runId}-${name}`; link.textContent = label
      links.append(link)
    }
    if (links.childElementCount) target.append(links)
  } catch (error) { target.replaceChildren(textNode('p', `回测详情暂不可读：${error.message}`, 'empty-note')) }
  rebuildBacktestIndex()
}

async function loadBacktest() {
  try {
    const payload = await requestJson('/api/backtests')
    const runs = payload.runs || []
    backtestContent.replaceChildren()
    rebuildBacktestIndex()
    backtestMeta.textContent = runs.length ? `已保存 ${runs.length} 次回测。表格展示各策略最新结果；可切换历史记录查看 checkpoint、净值、回撤与交易明细。` : '暂无已完成的策略回测。请在控制面板选择经典策略或已训练 checkpoint，或点击“回测全部经典策略”。'
    const wrapper = document.createElement('div'); wrapper.className = 'table-wrap'
    const table = document.createElement('table'); const head = document.createElement('thead'); const header = document.createElement('tr')
    for (const label of ['策略', '评估期间 / 信号', '累计收益', '最大回撤', 'Sharpe', '交易', '数据图']) header.append(textNode('th', label))
    head.append(header)
    const body = document.createElement('tbody')
    const detail = document.createElement('div'); detail.className = 'backtest-detail'
    const selector = document.createElement('select'); selector.id = 'backtestRun'
    for (const run of runs) selector.add(new Option(`${run.strategy_name}${run.checkpoint_id ? ` · checkpoint ${run.checkpoint_id}` : ''} · ${run.slot} · ${new Date(run.generated_at).toLocaleString('zh-CN')}`, run.run_id))
    selector.addEventListener('change', () => loadBacktestDetail(selector.value, detail))
    for (const [id, strategy] of Object.entries(payload.strategies || {})) {
      const run = runs.find(item => item.strategy_id === id)
      const row = document.createElement('tr')
      row.append(textNode('td', strategy.name), textNode('td', run ? `${run.first_day} 至 ${run.last_day} / ${run.slot}` : '尚未回测'))
      for (const value of [percent(run?.stats?.total_return), percent(run?.stats?.max_drawdown), run?.stats?.sharpe ?? '—', run?.stats?.trades ?? '—']) row.append(textNode('td', String(value)))
      const action = document.createElement('td')
      if (run) {
        const button = textNode('button', '查看'); button.type = 'button'
        button.addEventListener('click', () => { selector.value = run.run_id; loadBacktestDetail(run.run_id, detail) }); action.append(button)
      } else action.textContent = '—'
      row.append(action); body.append(row)
    }
    table.append(head, body); wrapper.append(table); wrapper.id = 'backtestTable'; backtestContent.append(wrapper)
    if (runs.length) {
      const label = textNode('label', '回测历史'); label.htmlFor = selector.id; label.className = 'backtest-history'
      label.id = 'backtestHistory'; label.append(selector); backtestContent.append(label, detail)
      if (!runs.some(run => run.run_id === selectedBacktestRun)) selectedBacktestRun = runs[0].run_id
      selector.value = selectedBacktestRun
      await loadBacktestDetail(selectedBacktestRun, detail)
    }
    const method = textNode('p', '各策略统一使用前一交易日股票池、T+1、涨跌停、费用与延后 VWAP 成交；经典策略按所选日期评估，模型回测使用训练集及标签结束后的区间。不同日期、时点或数据版本的记录请分别比较。固定规则仅在触发条件时入场，无信号时保持现金。', 'ranking-note')
    method.id = 'backtestMethod'
    backtestContent.append(method)
    rebuildBacktestIndex(true)
  } catch (error) { backtestMeta.textContent = `回测暂不可读：${error.message}`; rebuildBacktestIndex(true) }
}

document.querySelector('#refreshButton').addEventListener('click', loadBacktest)
loadBacktest()
setInterval(loadBacktest, 30_000)

const backtestEvents = new EventSource('/api/events')
backtestEvents.addEventListener('backtest', loadBacktest)
backtestEvents.addEventListener('open', () => { connectionState.textContent = '实时连接' })
backtestEvents.addEventListener('error', () => { connectionState.textContent = '自动刷新中' })
