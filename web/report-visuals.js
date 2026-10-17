/* Local, dependency-free charts. Every number comes from the selected archive. */
;(function (root) {
  const ns = 'http://www.w3.org/2000/svg'
  const colors = {up: '#c34150', down: '#218966', flat: '#98a2b3', blue: '#0071e3'}
  let archiveLabel = ''
  const number = value => value == null || typeof value === 'boolean' || String(value).trim() === '' || !Number.isFinite(Number(value)) ? null : Number(value)
  const signed = value => number(value) == null ? '—' : `${number(value) > 0 ? '+' : ''}${number(value).toFixed(2)}%`
  const el = (tag, text, cls = '') => { const node = document.createElement(tag); if (text != null) node.textContent = text; node.className = cls; return node }
  const empty = (target, message = '本期数据未取得，暂不绘图。') => target.replaceChildren(el('p', message, 'empty-note'))
  const svgNode = (tag, attrs = {}, text) => {
    const node = document.createElementNS(ns, tag)
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value)
    if (text != null) node.textContent = text
    return node
  }
  function chart(title, width, height) {
    const figure = el('figure', null, 'daily-chart')
    const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height + 26}`, role: 'img', 'aria-label': `${title} · ${archiveLabel}`})
    svg.append(svgNode('title', {}, `${title} · ${archiveLabel}`), svgNode('rect', {width, height: height + 26, fill: '#fff'}),
      svgNode('text', {x: 8, y: height + 16, fill: '#667085', 'font-size': 11}, `${archiveLabel} · 归档快照`))
    const download = el('button', '下载 SVG', 'chart-download'); download.type = 'button'
    download.addEventListener('click', () => {
      const copy = svg.cloneNode(true); copy.setAttribute('xmlns', ns)
      const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(copy)], {type: 'image/svg+xml;charset=utf-8'}))
      const link = el('a'); link.href = url; link.download = `${archiveLabel.slice(0, 10)}-${title}.svg`; link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    })
    figure.append(svg, download); return {figure, svg}
  }
  function breadthParts(breadth) {
    const values = [breadth.advancing, breadth.declining, breadth.unchanged].map(number)
    if (values.slice(0, 2).some(value => value == null || value < 0)) return null
    if (values[2] != null && values[2] < 0) return null
    const total = values.reduce((sum, value) => sum + (value ?? 0), 0)
    return total > 0 ? {values, total, hasFlat: values[2] != null} : null
  }
  function scoreData(panorama) {
    const warnings = panorama.unavailable || []
    return [
      ['综合风险', panorama.market_state?.risk_score, false, panorama.market_state?.risk_level],
      ['市场宽度', panorama.market_breadth?.score, warnings.some(item => item.includes('大盘宽度')) || panorama.market_breadth?.label === '数据不足', panorama.market_breadth?.label],
      ['情绪代理', panorama.social_sentiment?.score, warnings.some(item => item.includes('社交情绪')) || panorama.social_sentiment?.label === '数据不足', panorama.social_sentiment?.label],
    ].map(([label, value, missing, description]) => ({label, value: missing || number(value) == null || number(value) < 0 || number(value) > 100 ? null : number(value), description}))
  }
  function coverage(view) {
    return [
      ['市场宽度', !!breadthParts(view.market_panorama?.market_breadth || {})],
      ['全球指数', (view.global_markets?.indices || []).some(row => number(row.change_pct) != null)],
      ['主题板块', (view.sectors?.boards || []).some(row => number(row.change_pct) != null)],
      ['候选排行', !!view.candidates?.ranking?.length],
      ['事件线索', !!view.weekly_events?.events?.length],
    ]
  }
  function renderOverview(view, payload) {
    const target = document.querySelector('#dailyOverview'); target.replaceChildren()
    if (!payload.available) return empty(target, '选择日报后展示本期摘要。')
    const panorama = view.market_panorama || {}, state = panorama.market_state || {}, breadth = panorama.market_breadth || {}
    const headline = el('div', null, 'overview-heading')
    headline.append(el('p', '本期速览', 'eyebrow'), el('h2', state.summary || '本期结构化数据不足，请查看完整正文。'))
    const grid = el('div', null, 'overview-metrics')
    const parts = breadthParts(breadth)
    const rows = [
      ['市场风险', state.risk_level || '待评估', state.risk_score == null ? '风险分未取得' : `${state.risk_score} / 100 · 规则评分`],
      ['上涨占比', parts ? `${(parts.values[0] / parts.total * 100).toFixed(1)}%` : '—', parts ? `${parts.total.toLocaleString('zh-CN')} 只${parts.hasFlat ? '有效样本' : '涨跌样本（未含平盘）'}` : '市场宽度缺失'],
      ['研究候选', view.candidates ? `${view.candidates.ranking?.length || 0} 只` : '—', '有限候选池 · 非买入顺序'],
      ['本周事件', view.weekly_events ? `${view.weekly_events.events?.length || 0} 条` : '—', '归档线索 · 非完整新闻覆盖'],
    ]
    for (const [label, value, note] of rows) { const card = el('div'); card.append(el('small', label), el('strong', value), el('span', note)); grid.append(card) }
    const chips = el('div', null, 'coverage-chips')
    for (const [label, available] of coverage(view)) chips.append(el('span', `${available ? '●' : '○'} ${label}${available ? '' : ' · 无可绘数据'}`, available ? 'coverage-ok' : 'coverage-missing'))
    target.append(headline, grid, chips)
    if (view.warnings?.length || panorama.unavailable?.length) target.append(el('p', '本期部分数据源缺失，综合评分可能使用默认输入；详见正文“数据缺失”。', 'data-notice'))
  }
  function renderBreadth(breadth) {
    const target = document.querySelector('#breadthVisual'), parts = breadthParts(breadth)
    if (!parts) return empty(target)
    const {figure, svg} = chart('市场涨跌分布', 480, 220)
    let offset = 0
    const circumference = 2 * Math.PI * 68
    const labels = ['上涨', '下跌', '平盘'], palette = [colors.up, colors.down, colors.flat]
    parts.values.forEach((value, i) => {
      if (value == null) return
      const length = value / parts.total * circumference
      const ring = svgNode('circle', {cx: 112, cy: 105, r: 68, fill: 'none', stroke: palette[i], 'stroke-width': 23,
        'stroke-dasharray': `${length} ${circumference - length}`, 'stroke-dashoffset': -offset, transform: 'rotate(-90 112 105)'})
      ring.append(svgNode('title', {}, `${labels[i]} ${value} 只，占 ${(value / parts.total * 100).toFixed(1)}%`)); svg.append(ring); offset += length
      svg.append(svgNode('rect', {x: 226, y: 45 + i * 49, width: 9, height: 9, rx: 2, fill: palette[i]}),
        svgNode('text', {x: 245, y: 54 + i * 49, fill: '#475467', 'font-size': 14}, `${labels[i]} ${value.toLocaleString('zh-CN')} 只`),
        svgNode('text', {x: 245, y: 74 + i * 49, fill: palette[i], 'font-size': 13}, `${(value / parts.total * 100).toFixed(1)}%`))
    })
    svg.append(svgNode('text', {x: 112, y: 104, 'text-anchor': 'middle', 'font-size': 27, fill: '#161617', 'font-weight': 700}, `${(parts.values[0] / parts.total * 100).toFixed(1)}%`),
      svgNode('text', {x: 112, y: 127, 'text-anchor': 'middle', 'font-size': 13, fill: '#667085'}, '上涨占比'))
    target.replaceChildren(figure)
    target.append(el('p', `涨跌幅中位数 ${signed(breadth.median_pct_change)} · ${breadth.source || '来源未归档'} · ${breadth.as_of || '日期未归档'}${parts.hasFlat ? '' : ' · 平盘未归档，占比仅含涨跌样本'}`, 'ranking-note'))
  }
  function renderScores(panorama) {
    const target = document.querySelector('#riskVisual'); target.replaceChildren()
    for (const {label, value, description} of scoreData(panorama)) {
      const row = el('div', null, 'score-row'), head = el('div', null, 'score-heading')
      head.append(el('span', label), el('strong', value == null ? '数据不足' : `${value} / 100`))
      row.append(head)
      if (value != null) {
        const svg = svgNode('svg', {viewBox: '0 0 400 24', role: 'img', 'aria-label': `${label} ${value} 分`})
        for (let i = 0; i < 5; i++) svg.append(svgNode('rect', {x: i * 81, y: 5, width: 76, height: 9, rx: 3, fill: '#e8edf3'}))
        svg.append(svgNode('circle', {cx: Math.max(5, Math.min(395, value * 4)), cy: 9, r: 6, fill: label === '综合风险' && value >= 60 ? colors.up : colors.blue}))
        row.append(svg)
      }
      row.append(el('small', description || '评分未归档')); target.append(row)
    }
    target.append(el('p', panorama.method || '规则评分仅反映归档输入，不是涨跌概率。', 'ranking-note'))
  }
  function heatLevel(value) { return number(value) == null ? 'heat-missing' : number(value) === 0 ? 'heat-flat' : `heat-${number(value) > 0 ? 'up' : 'down'}-${Math.min(4, Math.ceil(Math.abs(number(value)) / 5))}` }
  function renderCandidates(candidates) {
    const target = document.querySelector('#candidateVisual'), rows = candidates?.ranking || []
    if (!rows.length) return empty(target, '本期无可绘制的候选排行。历史纯文字日报请查看正文。')
    const wrap = el('div', null, 'table-wrap heatmap-wrap'), table = el('table', null, 'heatmap')
    const head = el('thead'), header = el('tr')
    for (const label of ['候选', '1 日', '5 日', '20 日', '方向倾向（买 / 卖）']) header.append(el('th', label))
    head.append(header); const body = el('tbody')
    for (const row of rows) {
      const tr = el('tr'), name = el('th'); name.scope = 'row'; name.append(el('strong', row.name || row.symbol), el('small', row.symbol)); tr.append(name)
      for (const key of ['pct_change_1d', 'pct_change_5d', 'pct_change_20d']) tr.append(el('td', signed(row[key]), heatLevel(row[key])))
      const tendency = el('td'), buy = number(row.buy_percentage), sell = number(row.sell_percentage)
      tendency.append(el('strong', buy == null || sell == null ? '—' : `${buy}% / ${sell}%`), el('small', row.recommendation || '待核验')); tr.append(tendency); body.append(tr)
    }
    table.append(head, body); wrap.append(table); target.replaceChildren(wrap)
    target.append(el('p', '红涨绿跌：0–5%、5–10%、10–15%、≥15% 依次加深，单元格保留精确值。方向倾向不是仓位或涨跌概率。', 'ranking-note'))
  }
  function eventRows(view) {
    const panorama = view.market_panorama || {}, seen = new Set()
    return [...(view.weekly_events?.events || []), ...(panorama.domestic_policies || []), ...(panorama.international_events || [])]
      .filter(row => { const title = row.title?.trim(); if (!title || seen.has(title)) return false; seen.add(title); return true })
      .sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')))
  }
  function renderEvents(view) {
    const target = document.querySelector('#eventVisual'), events = eventRows(view)
    if (!events.length) return empty(target, '本期未归档可展示事件，暂无时间线。')
    const themes = new Map()
    events.forEach(row => themes.set(row.theme || '综合事件', (themes.get(row.theme || '综合事件') || 0) + 1))
    const chips = el('div', null, 'coverage-chips')
    for (const [theme, count] of themes) chips.append(el('span', `${theme} · ${count} 条`, 'coverage-ok'))
    const list = el('ol', null, 'event-timeline')
    for (const row of events.slice(0, 6)) {
      const item = el('li'); item.append(el('time', row.date || '日期待核验'), el('h3', row.title), el('p', row.possible_impact_path || '先核验权威原文，再判断相关行业与传导路径。')); list.append(item)
    }
    target.replaceChildren(chips, list)
    document.querySelector('#eventMeta').textContent = `${events.length} 条去重归档线索 · 展示最近 ${Math.min(6, events.length)} 条 · ${view.weekly_events?.source || view.market_panorama?.source || '来源未归档'} · 日期未核验的线索不代表当日新闻`
  }
  function renderReview(review, session) {
    const target = document.querySelector('#reviewVisual')
    if (session !== 'afternoon') return empty(target, '上午日报提供研究基线；下午日报生成后展示两次快照的对比。')
    if (review?.status !== 'completed') return empty(target, review?.note || '本期未取得复盘快照。')
    if (!review.comparisons?.length) return empty(target, '上午候选池为空，本期没有可比较标的；不计算一致性比例。')
    const labels = {aligned: '相符', contradicted: '明显背离', inconclusive: '待观察', insufficient: '数据不足'}
    const chips = el('div', null, 'coverage-chips')
    for (const [key, label] of Object.entries(labels)) chips.append(el('span', `${label} ${review.comparisons.filter(row => row.alignment === key).length}`, key === 'contradicted' ? 'coverage-missing' : 'coverage-ok'))
    const rows = review.comparisons.map(row => ({...row, change_pct: row.return_from_morning_pct}))
    const scale = Math.max(1, ...rows.map(row => Math.abs(number(row.change_pct) ?? 0)))
    const figure = el('figure', null, 'review-comparison'), heading = el('div', null, 'review-chart-heading')
    const axis = el('div', null, 'review-axis')
    for (const label of [`−${scale.toFixed(2)}%`, '0', `+${scale.toFixed(2)}%`]) axis.append(el('span', label))
    heading.append(el('span', '上午 → 下午'), axis, el('span', '价格变化')); figure.append(heading)
    const list = el('ul', null, 'review-rows')
    for (const row of rows) {
      const value = number(row.change_pct), item = el('li', null, 'review-row'), identity = el('div', null, 'review-identity')
      const name = el('div'); name.append(el('strong', row.name || row.symbol), el('small', row.symbol || ''))
      identity.append(name, el('span', labels[row.alignment] || '待核验', `review-status review-status-${row.alignment}`))
      const track = el('div', null, 'review-track'); track.setAttribute('aria-hidden', 'true')
      if (value != null) {
        const bar = el('span', null, `review-bar ${value >= 0 ? 'is-up' : 'is-down'}`)
        bar.style.width = `${Math.abs(value) / scale * 50}%`; track.append(bar)
      }
      item.title = `${row.quote_time || '行情时间未归档'} · ${value == null ? '价格数据不足' : signed(value)}`
      item.append(identity, track, el('strong', signed(value), `review-value ${value == null ? '' : value >= 0 ? 'is-up' : 'is-down'}`)); list.append(item)
    }
    const caption = el('figcaption', null, 'review-caption')
    // Reuse the archived SVG export while keeping the on-page layout responsive.
    const download = barFigure(rows, row => `${row.name || row.symbol} · ${labels[row.alignment] || '待核验'}`, '上午至下午价格变化').querySelector('.chart-download')
    caption.append(el('span', `${archiveLabel} · 归档快照`), download); figure.append(list, caption)
    target.replaceChildren(chips, figure)
    target.append(el('p', review.threshold_note || '阈值口径未归档。', 'ranking-note'), el('p', review.method_note || '', 'ranking-note'))
  }
  function barFigure(rows, nameKey, title) {
    const scale = Math.max(1, ...rows.map(row => Math.abs(number(row.change_pct) ?? 0)))
    const {figure, svg} = chart(title, 520, rows.length * 45 + 48), center = 330, half = 108
    svg.append(svgNode('line', {x1: center, x2: center, y1: 26, y2: rows.length * 45 + 27, stroke: '#cbd5e1'}))
    for (const [x, label] of [[center - half, `−${scale.toFixed(1)}%`], [center, '0'], [center + half, `+${scale.toFixed(1)}%`]]) svg.append(svgNode('text', {x, y: 16, 'text-anchor': 'middle', fill: '#667085', 'font-size': 11}, label))
    rows.forEach((row, i) => {
      const y = 45 + i * 45, value = number(row.change_pct), label = String(nameKey(row))
      const name = svgNode('text', {x: 4, y: y + 4, fill: '#475467', 'font-size': 13}, label.length > 23 ? `${label.slice(0, 22)}…` : label)
      name.append(svgNode('title', {}, `${label} · ${row.quote_time || '行情时间未归档'}`)); svg.append(name)
      if (value != null) {
        const width = Math.abs(value) / scale * half
        const bar = svgNode('rect', {x: value >= 0 ? center : center - width, y: y - 9, width, height: 15, rx: 3, fill: value >= 0 ? colors.up : colors.down})
        bar.append(svgNode('title', {}, `${label} ${signed(value)} · ${row.quote_time || '行情时间未归档'}`)); svg.append(bar)
      }
      svg.append(svgNode('text', {x: 516, y: y + 4, 'text-anchor': 'end', 'font-size': 12, fill: value == null ? '#667085' : value >= 0 ? colors.up : colors.down}, signed(value)))
    })
    return figure
  }
  function renderBars(target, rows, nameKey) {
    if (!rows?.length || !rows.some(row => number(row.change_pct) != null)) return empty(target)
    target.replaceChildren(barFigure(rows, nameKey, target.id === 'globalChart' ? '全球指数涨跌幅' : '重点板块涨跌幅'))
    target.append(el('p', '同图统一刻度，零线右侧为涨、左侧为跌；悬停查看数值与归档行情时间。', 'ranking-note'))
  }
  function render(payload) {
    archiveLabel = payload.label || payload.id || '日报'
    const view = payload.view || {}
    renderOverview(view, payload); renderBreadth(view.market_panorama?.market_breadth || {})
    renderScores(view.market_panorama || {}); renderCandidates(view.candidates)
    document.querySelector('#eventMeta').textContent = '按归档日期排列；标题与可能影响路径仍需复核原文'
    renderEvents(view); renderReview(view.intraday_review, payload.session)
  }
  const api = {number, signed, breadthParts, scoreData, coverage, heatLevel, eventRows, renderBars, render}
  if (typeof module !== 'undefined' && module.exports) module.exports = api
  else root.ReportVisuals = api
})(typeof globalThis === 'undefined' ? this : globalThis)
