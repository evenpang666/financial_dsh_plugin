/* Small, dependency-free holding charts; calculations are also tested in Node. */
;(function (root) {
  const numeric = value => value == null || typeof value === 'boolean' || String(value).trim() === '' || !Number.isFinite(Number(value)) ? null : Number(value);
  const trendClass = value => { const number = numeric(value); return number == null ? 'is-missing' : number > 0 ? 'is-up' : number < 0 ? 'is-down' : 'is-flat'; };
  const formatChange = value => { const number = numeric(value); return number == null ? '—' : `${number > 0 ? '+' : ''}${number.toFixed(2)}%`; };
  const price = value => numeric(value) == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits: 3});
  const chartModes = new Map();
  const extendedHistories = new Map();
  const periodLabels = {daily: '日', weekly: '周', monthly: '月'};
  const indicatorPreferences = new Map();
  const defaultIndicators = {ma5: true, ma20: true, boll: false, volume: true, subVisible: true, oscillator: 'macd'};
  const indicatorNumber = value => numeric(value) == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits: 4});
  let expandedDialog = null;
  const validCandle = point => ['open', 'high', 'low', 'close'].every(key => numeric(point[key]) != null && Number(point[key]) > 0)
    && Number(point.low) <= Math.min(Number(point.open), Number(point.close)) && Number(point.high) >= Math.max(Number(point.open), Number(point.close));
  function candleGeometry(point, x, scaleY, width) {
    if (!validCandle(point)) return null;
    return {x: x - width / 2, width, top: scaleY(Number(point.high)), bottom: scaleY(Number(point.low)),
      bodyY: Math.min(scaleY(Number(point.open)), scaleY(Number(point.close))),
      bodyHeight: Math.max(1, Math.abs(scaleY(Number(point.open)) - scaleY(Number(point.close)))), direction: trendClass(Number(point.close) - Number(point.open))};
  }
  function normalizePoints(raw) {
    const days = new Map();
    for (const point of raw || []) {
      const close = numeric(point.close);
      if (!/^\d{4}-\d{2}-\d{2}$/.test(point.date || '') || close == null || close <= 0) continue;
      const date = new Date(point.date + 'T00:00:00Z');
      if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== point.date) continue;
      const volume = numeric(point.volume);
      days.set(point.date, {...point, open: numeric(point.open), high: numeric(point.high), low: numeric(point.low), close, volume: volume != null && volume >= 0 ? volume : null});
    }
    return [...days.values()].sort((a, b) => a.date.localeCompare(b.date));
  }
  function aggregatePeriods(raw, period = 'daily', limits = {}) {
    const points = normalizePoints(raw);
    if (period === 'daily') return points;
    if (!['weekly', 'monthly'].includes(period)) throw Error('未知K线周期。');
    const groups = new Map();
    for (const point of points) {
      const day = new Date(point.date + 'T00:00:00Z'); let start, end;
      if (period === 'weekly') {
        day.setUTCDate(day.getUTCDate() - (day.getUTCDay() + 6) % 7); start = day.toISOString().slice(0, 10);
        day.setUTCDate(day.getUTCDate() + 4); end = day.toISOString().slice(0, 10);
      } else {
        start = point.date.slice(0, 7) + '-01'; end = new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth() + 1, 0)).toISOString().slice(0, 10);
      }
      if (!groups.has(start)) groups.set(start, {rows: [], start, end}); groups.get(start).rows.push(point);
    }
    return [...groups.values()].map(({rows, start, end}) => {
      const fullOHLC = rows.every(validCandle), fullVolume = rows.every(point => point.volume != null);
      return {date: rows.at(-1).date, period_start: rows[0].date, period_end: rows.at(-1).date,
        incomplete_start: Boolean(limits.from && start < limits.from), incomplete_end: end > (limits.through || points.at(-1).date),
        complete: (!limits.from || start >= limits.from) && end <= (limits.through || points.at(-1).date),
        open: fullOHLC ? rows[0].open : null, high: fullOHLC ? Math.max(...rows.map(point => point.high)) : null,
        low: fullOHLC ? Math.min(...rows.map(point => point.low)) : null, close: rows.at(-1).close,
        volume: fullVolume ? rows.reduce((sum, point) => sum + point.volume, 0) : null, trading_days: rows.length};
    });
  }
  function normalizeViewport(total, view) {
    if (total <= 0) return {count: 0, offset: 0};
    const count = Math.max(Math.min(8, total), Math.min(total, Math.round(view.count || 60)));
    return {count, offset: Math.max(0, Math.min(total - count, Math.round(view.offset || 0)))};
  }
  function zoomViewport(total, view, delta, anchor = .5) {
    const current = normalizeViewport(total, view); if (!current.count) return current;
    const ratio = Math.max(0, Math.min(1, anchor));
    const count = normalizeViewport(total, {count: current.count * Math.exp(Math.max(-240, Math.min(240, delta)) * .0018)}).count;
    const anchorIndex = total - current.offset - current.count + ratio * (current.count - 1);
    const start = Math.round(anchorIndex - ratio * (count - 1));
    return normalizeViewport(total, {count, offset: total - start - count});
  }
  function panViewport(total, view, bars) {
    return normalizeViewport(total, {...view, offset: (view.offset || 0) + bars});
  }
  const pointLabel = point => `${point.period_start ? `${point.period_start}～${point.period_end}` : point.date}${point.incomplete_end ? '（未完结）' : point.incomplete_start ? '（起始周期不完整）' : point.complete === false ? '（未完结）' : ''}`;
  function seededAverage(values, period, alpha) {
    let seed = [], previous = null;
    return values.map(raw => {
      const value = numeric(raw);
      if (value == null) { seed = []; previous = null; return null; }
      if (previous == null) { seed.push(value); if (seed.length < period) return null; previous = seed.reduce((sum, item) => sum + item, 0) / period; seed = []; }
      else previous += alpha * (value - previous);
      return previous;
    });
  }
  function calculateIndicators(raw) {
    const points = normalizePoints(raw);
    const closes = points.map(point => point.close), fast = seededAverage(closes, 12, 2 / 13), slow = seededAverage(closes, 26, 2 / 27);
    const dif = closes.map((_, index) => fast[index] != null && slow[index] != null ? fast[index] - slow[index] : null), dea = seededAverage(dif, 9, 2 / 10);
    const deltas = closes.map((value, i) => i ? value - closes[i - 1] : null);
    const gains = seededAverage(deltas.map(value => value == null ? null : Math.max(0, value)), 14, 1 / 14);
    const losses = seededAverage(deltas.map(value => value == null ? null : Math.max(0, -value)), 14, 1 / 14);
    return points.map((point, i) => {
      const ma5 = i >= 4 ? closes.slice(i - 4, i + 1).reduce((sum, value) => sum + value, 0) / 5 : null;
      const ma20 = i >= 19 ? closes.slice(i - 19, i + 1).reduce((sum, value) => sum + value, 0) / 20 : null;
      const deviation = ma20 == null ? null : Math.sqrt(closes.slice(i - 19, i + 1).reduce((sum, value) => sum + (value - ma20) ** 2, 0) / 20);
      const rsi14 = gains[i] == null || losses[i] == null ? null : gains[i] + losses[i] === 0 ? 50 : 100 * gains[i] / (gains[i] + losses[i]);
      return {...point, ma5, ma20, dif: dif[i], dea: dea[i], macdHistogram: dif[i] == null || dea[i] == null ? null : 2 * (dif[i] - dea[i]),
        rsi14, bollMid: ma20, bollUpper: deviation == null ? null : ma20 + 2 * deviation, bollLower: deviation == null ? null : ma20 - 2 * deviation,
        direction: i ? trendClass(point.close - points[i - 1].close) : 'is-flat'};
    });
  }
  function prepareSeries(raw, window = 60, offset = 0) {
    const enriched = calculateIndicators(raw);
    const end = Math.max(0, enriched.length - offset), visible = enriched.slice(Math.max(0, end - window), end);
    if (visible.length < 2) return {points: visible, summary: null};
    let peak = visible[0].close, drawdown = 0;
    for (const point of visible) { peak = Math.max(peak, point.close); drawdown = Math.min(drawdown, point.close / peak - 1); }
    return {points: visible, summary: {changePct: (visible.at(-1).close / visible[0].close - 1) * 100,
      low: Math.min(...visible.map(point => point.close)), high: Math.max(...visible.map(point => point.close)), drawdownPct: drawdown * 100}};
  }
  function render(target, payload, window = 60, options = {}) {
    const el = (tag, value, className = '') => { const element = document.createElement(tag); if (value != null) element.textContent = value; element.className = className; return element; };
    const ns = 'http://www.w3.org/2000/svg';
    const svgNode = (tag, attributes = {}, value) => { const element = document.createElementNS(ns, tag); for (const [key, entry] of Object.entries(attributes)) element.setAttribute(key, String(entry)); if (value != null) element.textContent = value; return element; };
    const {points, summary} = prepareSeries(payload.points, window, options.offset || 0); target.replaceChildren();
    if (!summary) { target.append(el('p', '有效历史行情不足，暂不能绘制走势。', 'holding-chart-empty')); return; }
    const identity = `${payload.market}:${payload.symbol}`, mode = options.mode || chartModes.get(identity) || 'line', expanded = Boolean(options.expanded);
    const indicators = {...defaultIndicators, ...options.indicators}, showSub = expanded && indicators.subVisible, showVolume = !expanded || indicators.volume;
    const direction = trendClass(summary.changePct), figure = el('figure', null, 'holding-trend');
    const caption = el('figcaption'), change = el('strong', `区间 ${formatChange(summary.changePct)}`, `holding-trend-return ${direction}`);
    const unit = periodLabels[options.period || 'daily'];
    caption.append(el('span', options.expanded ? `${points.length} 根${unit}线 · ${points[0].date} → ${points.at(-1).date}` : `近 ${points.length} 个交易日`), change); figure.append(caption);
    if (!expanded) {
      const tools = el('div', null, 'holding-chart-tools'); tools.setAttribute('role', 'group'); tools.setAttribute('aria-label', `${payload.name || payload.symbol} 图表操作`);
      for (const [value, label] of [['line', '走势'], ['candle', 'K线']]) {
        const button = el('button', label); button.type = 'button'; button.setAttribute('aria-pressed', String(mode === value));
        button.addEventListener('click', () => { chartModes.set(identity, value); render(target, payload, window, {...options, mode: value}); target.querySelector('button[aria-pressed="true"]')?.focus({preventScroll: true}); }); tools.append(button);
      }
      const enlarge = el('button', '放大 ↗', 'holding-enlarge'); enlarge.type = 'button'; enlarge.setAttribute('aria-label', `放大 ${payload.name || payload.symbol} 图表`);
      enlarge.addEventListener('click', () => openDialog(payload, window, mode, enlarge, target)); tools.append(enlarge); figure.append(tools);
    }
    const width = expanded ? 1000 : 520, left = expanded ? 65 : 52, right = width - 14;
    const priceBottom = expanded ? showSub ? 150 : showVolume ? 180 : 215 : 155;
    const volumeHeight = expanded ? showSub ? 35 : 48 : 34, volumeBase = expanded ? priceBottom + (showVolume ? volumeHeight + 20 : 0) : 202;
    const indicatorTop = volumeBase + 30, indicatorBottom = indicatorTop + 75;
    const height = expanded ? showSub ? indicatorBottom + 28 : showVolume ? 280 : 240 : 230;
    const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', tabindex: '0', class: `holding-trend-chart${expanded ? ' is-expanded' : ' is-expandable'}`,
      'aria-label': `${payload.symbol} ${mode === 'candle' ? `${unit}K线` : `${unit}线收盘价`}，${points[0].date} 至 ${points.at(-1).date}。左右方向键逐根查看${expanded ? '，滚轮缩放，按住左右拖动' : '，点击或按回车放大'}。`});
    const values = points.flatMap(point => [point.close, ...(indicators.ma5 ? [point.ma5] : []), ...(indicators.ma20 ? [point.ma20] : []),
      ...(indicators.boll ? [point.bollUpper, point.bollLower] : []), ...(mode === 'candle' && validCandle(point) ? [point.high, point.low] : [])].filter(value => value != null));
    const low = Math.min(...values), high = Math.max(...values), padding = (high - low) * .1 || high * .01;
    const minimum = low - padding, maximum = high + padding, span = maximum - minimum;
    const x = index => left + index * (right - left) / (points.length - 1), y = value => priceBottom - (value - minimum) / span * (priceBottom - 16);
    for (const ratio of [0, .5, 1]) {
      const value = minimum + span * ratio, axisY = y(value);
      svg.append(svgNode('line', {x1: left, x2: right, y1: axisY, y2: axisY, class: 'holding-chart-grid'}),
        svgNode('text', {x: left - 8, y: axisY + 4, 'text-anchor': 'end', class: 'holding-chart-axis'}, price(value)));
    }
    const maxVolume = Math.max(0, ...points.map(point => point.volume || 0));
    if (showVolume && maxVolume) for (const [i, point] of points.entries()) {
      if (point.volume == null) continue;
      const height = point.volume / maxVolume * volumeHeight;
      const candleDirection = mode === 'candle' && validCandle(point) ? trendClass(point.close - point.open) : point.direction;
      const barWidth = Math.min(expanded ? 13 : 7, (right - left) / points.length * .7);
      svg.append(svgNode('rect', {x: x(i) - barWidth / 2, y: volumeBase - height, width: barWidth, height,
        class: `holding-volume ${candleDirection}`}));
    }
    const pathFor = field => {
      let started = false, path = '';
      for (const [i, point] of points.entries()) {
        if (point[field] == null) { started = false; continue; }
        path += `${started ? 'L' : 'M'}${x(i).toFixed(2)},${y(point[field]).toFixed(2)} `; started = true;
      }
      return path;
    };
    if (mode === 'candle') for (const [i, point] of points.entries()) {
      const candle = candleGeometry(point, x(i), y, Math.min(expanded ? 13 : 8, (right - left) / points.length * .65));
      if (!candle) continue;
      const group = svgNode('g', {class: `holding-candle ${candle.direction}`});
      group.append(svgNode('line', {x1: x(i), x2: x(i), y1: candle.top, y2: candle.bottom}),
        svgNode('rect', {x: candle.x, y: candle.bodyY, width: candle.width, height: candle.bodyHeight})); svg.append(group);
    }
    const paths = [];
    if (indicators.boll) paths.push(['bollUpper', 'holding-boll-edge'], ['bollLower', 'holding-boll-edge'], ['bollMid', 'holding-boll-mid']);
    if (indicators.ma20) paths.push(['ma20', 'holding-ma20']);
    if (indicators.ma5) paths.push(['ma5', 'holding-ma5']);
    if (mode === 'line') paths.push(['close', `holding-close ${direction}`]);
    for (const [field, className] of paths) svg.append(svgNode('path', {d: pathFor(field), class: className, fill: 'none'}));
    if (showSub) {
      const isRSI = indicators.oscillator === 'rsi', fields = isRSI ? ['rsi14'] : ['dif', 'dea', 'macdHistogram'];
      const indicatorValues = points.flatMap(point => fields.map(field => point[field]).filter(value => value != null));
      const title = isRSI ? 'RSI (14)' : 'MACD (12,26,9)';
      svg.append(svgNode('text', {x: left, y: indicatorTop - 9, class: 'holding-indicator-title'}, title));
      if (!indicatorValues.length) svg.append(svgNode('text', {x: (left + right) / 2, y: (indicatorTop + indicatorBottom) / 2, 'text-anchor': 'middle', class: 'holding-chart-axis'}, `历史数据不足，${isRSI ? 'RSI需要15根' : 'MACD需要34根'}K线预热`));
      else {
        const minimum = isRSI ? 0 : Math.min(0, ...indicatorValues), maximum = isRSI ? 100 : Math.max(0, ...indicatorValues);
        const padding = isRSI ? 0 : (maximum - minimum) * .1 || 1;
        const bottom = minimum - padding, top = maximum + padding;
        const yIndicator = value => indicatorBottom - (value - bottom) / (top - bottom) * (indicatorBottom - indicatorTop);
        const marks = isRSI ? [0, 30, 50, 70, 100] : [bottom, 0, top];
        for (const value of marks) svg.append(svgNode('line', {x1: left, x2: right, y1: yIndicator(value), y2: yIndicator(value), class: isRSI && [30, 70].includes(value) ? 'holding-rsi-threshold' : 'holding-chart-grid'}),
          svgNode('text', {x: left - 8, y: yIndicator(value) + 4, 'text-anchor': 'end', class: 'holding-chart-axis'}, indicatorNumber(value)));
        if (!isRSI) for (const [i, point] of points.entries()) {
          if (point.macdHistogram == null) continue; const barWidth = Math.min(12, (right - left) / points.length * .65);
          svg.append(svgNode('rect', {x: x(i) - barWidth / 2, y: Math.min(yIndicator(0), yIndicator(point.macdHistogram)), width: barWidth,
            height: Math.max(.5, Math.abs(yIndicator(point.macdHistogram) - yIndicator(0))), class: `holding-macd-bar ${trendClass(point.macdHistogram)}`}));
        }
        for (const [field, className] of isRSI ? [['rsi14', 'holding-rsi-line']] : [['dif', 'holding-dif-line'], ['dea', 'holding-dea-line']]) {
          let started = false, path = '';
          for (const [i, point] of points.entries()) { if (point[field] == null) { started = false; continue; } path += `${started ? 'L' : 'M'}${x(i).toFixed(2)},${yIndicator(point[field]).toFixed(2)} `; started = true; }
          svg.append(svgNode('path', {d: path, fill: 'none', class: className}));
        }
      }
    }
    for (const i of [0, Math.floor((points.length - 1) / 2), points.length - 1]) svg.append(svgNode('text', {x: x(i), y: height - 8,
      'text-anchor': i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle', class: 'holding-chart-axis'}, options.period === 'monthly' ? points[i].date.slice(0, 7) : points[i].date.slice(5)));
    const crosshair = svgNode('line', {y1: 12, y2: (showSub ? indicatorBottom : volumeBase) + 2, class: 'holding-crosshair'}), marker = svgNode('circle', {r: 4, class: `holding-chart-marker ${direction}`});
    svg.append(crosshair, marker);
    const readout = el('p', '', 'holding-chart-readout'); readout.setAttribute('aria-live', 'polite');
    const indicatorReadout = el('p', '', 'holding-indicator-readout'); indicatorReadout.setAttribute('aria-live', 'polite');
    let selected = points.length - 1;
    const select = index => {
      selected = Math.max(0, Math.min(points.length - 1, index)); const point = points[selected];
      crosshair.setAttribute('x1', x(selected)); crosshair.setAttribute('x2', x(selected)); marker.setAttribute('cx', x(selected)); marker.setAttribute('cy', y(point.close));
      readout.textContent = `${pointLabel(point)} · 开 ${price(point.open)} · 收 ${price(point.close)} · 高 ${price(point.high)} · 低 ${price(point.low)} ${payload.currency} · 成交量 ${price(point.volume)} ${payload.volume_unit || '原始单位'}`;
      const items = [];
      if (indicators.boll) items.push(el('span', `BOLL 中 ${indicatorNumber(point.bollMid)} / 上 ${indicatorNumber(point.bollUpper)} / 下 ${indicatorNumber(point.bollLower)}`, 'holding-boll-value'));
      if (showSub && indicators.oscillator === 'macd') items.push(el('span', `DIF ${indicatorNumber(point.dif)}`, 'holding-dif-value'), el('span', `DEA ${indicatorNumber(point.dea)}`, 'holding-dea-value'), el('span', `MACD柱 ${indicatorNumber(point.macdHistogram)}`, trendClass(point.macdHistogram)));
      if (showSub && indicators.oscillator === 'rsi') items.push(el('span', `RSI(14) ${indicatorNumber(point.rsi14)} · 参考线 30 / 70`, 'holding-rsi-value'));
      indicatorReadout.replaceChildren(...items);
      options.onSelect?.(point, selected, points);
    };
    const indexAt = event => { const rect = svg.getBoundingClientRect(); return Math.round(((event.clientX - rect.left) / rect.width * width - left) / (right - left) * (points.length - 1)); };
    svg.addEventListener('pointermove', event => { if (!options.isPanning?.() && indexAt(event) !== selected) select(indexAt(event)); });
    svg.addEventListener('click', event => { if (options.preventClick?.()) return; select(indexAt(event)); if (!expanded) openDialog(payload, window, mode, svg, target, points[selected].date); });
    svg.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !expanded) { event.preventDefault(); openDialog(payload, window, mode, svg, target, points[selected].date); return; }
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault(); select(event.key === 'Home' ? 0 : event.key === 'End' ? points.length - 1 : selected + (event.key === 'ArrowLeft' ? -1 : 1));
    });
    const requested = points.findIndex(point => point.date === options.selectedDate);
    select(requested >= 0 ? requested : selected); figure.append(svg);
    const legend = el('div', null, 'holding-chart-legend');
    const legends = [[mode === 'candle' ? 'K线（红阳绿阴）' : '收盘', mode === 'candle' ? 'is-flat' : direction]];
    if (indicators.ma5) legends.push([`MA5${unit !== '日' ? `（5${unit}）` : ''}`, 'holding-ma5']);
    if (indicators.ma20) legends.push([`MA20${unit !== '日' ? `（20${unit}）` : ''}`, 'holding-ma20']);
    if (indicators.boll) legends.push(['BOLL(20,2)', 'holding-boll-value']);
    for (const [label, className] of legends) legend.append(el('span', label, className));
    if (showVolume) legend.append(el('span', maxVolume ? '相对成交量' : points.some(point => point.volume != null) ? '成交量为 0' : '成交量暂缺', 'holding-volume-legend'));
    figure.append(legend, readout); if (expanded && (showSub || indicators.boll)) figure.append(indicatorReadout);
    const missingCandles = mode === 'candle' ? points.filter(point => !validCandle(point)).length : 0;
    if (missingCandles) figure.append(el('p', `${missingCandles} 根K线的开高低数据缺失或异常，对应K线留空；可在行情明细中查看已有字段。`, 'holding-chart-source'));
    const metrics = el('dl', null, 'holding-chart-metrics');
    for (const [label, value] of [['收盘最低', `${price(summary.low)} ${payload.currency}`], ['收盘最高', `${price(summary.high)} ${payload.currency}`], ['最大回撤', formatChange(summary.drawdownPct)]]) {
      const item = el('div'); item.append(el('dt', label), el('dd', value, label === '最大回撤' ? trendClass(summary.drawdownPct) : '')); metrics.append(item);
    }
    if (!expanded) figure.append(metrics, el('p', `${points[0].date} → ${points.at(-1).date} · ${payload.adjust === 'split_dividend' ? '拆股分红调整' : '前复权'}日线`, 'holding-chart-source'),
      el('p', `来源：${payload.source || '未取得'} · 数据截至 ${payload.as_of || points.at(-1).date}`, 'holding-chart-source'));
    target.append(figure);
    return {select, points, summary};
  }
  function openDialog(payload, initialWindow, initialMode, opener, inlineTarget, initialDate) {
    expandedDialog?.close();
    const el = (tag, value, className = '') => { const element = document.createElement(tag); if (value != null) element.textContent = value; element.className = className; return element; };
    const dialog = el('dialog', null, 'holding-chart-dialog'); expandedDialog = dialog;
    dialog.setAttribute('aria-labelledby', 'holding-chart-dialog-title');
    const header = el('header', null, 'holding-detail-heading'), title = el('h2', payload.name ? `${payload.name} · ${payload.symbol}` : payload.symbol);
    title.id = 'holding-chart-dialog-title';
    const intro = el('div'); intro.append(title, el('p', '滚轮缩放 · 按住图表左右拖动 · 方向键逐根查看'));
    if (payload.contextNote) intro.append(el('p', payload.contextNote, 'holding-chart-context'));
    const close = el('button', '关闭 ×', 'holding-dialog-close'); close.type = 'button'; close.setAttribute('aria-label', '关闭放大图表'); close.addEventListener('click', () => dialog.close()); header.append(intro, close);
    const toolbar = el('div', null, 'holding-detail-toolbar');
    const group = name => { const item = el('div', null, 'holding-chart-tools'); item.setAttribute('role', 'group'); item.setAttribute('aria-label', name); toolbar.append(item); return item; };
    const types = group('图表类型'), periods = group('K线周期'), ranges = group('放大图表区间');
    let mode = initialMode, period = 'daily', selectedDate = initialDate, controller, workingPayload = payload;
    let view = {count: initialWindow, offset: 0}, selectedIndex = 0, rowElements = [], frame = 0, drag = null, suppressClickUntil = 0, loadingLong = false;
    const indicators = {...defaultIndicators, ...indicatorPreferences.get(`${payload.market}:${payload.symbol}`)};
    const indicatorToolbar = el('div', null, 'holding-indicator-toolbar'); indicatorToolbar.setAttribute('aria-label', '技术指标设置');
    const mainControls = el('div', null, 'holding-indicator-controls'), subControls = el('div', null, 'holding-indicator-controls');
    mainControls.append(el('span', '主图', 'holding-indicator-caption')); subControls.append(el('span', '副图', 'holding-indicator-caption'));
    const saveIndicators = () => { indicatorPreferences.set(`${payload.market}:${payload.symbol}`, {...indicators}); redraw(); };
    const toggle = (target, key, text) => {
      const label = el('label'), input = document.createElement('input'); input.type = 'checkbox'; input.checked = indicators[key]; input.dataset.indicator = key;
      input.addEventListener('change', () => { indicators[key] = input.checked; if (key === 'subVisible') oscillator.disabled = !input.checked; saveIndicators(); });
      label.append(input, el('span', text)); target.append(label);
    };
    for (const [key, text] of [['ma5', 'MA5'], ['ma20', 'MA20'], ['boll', 'BOLL(20,2)']]) toggle(mainControls, key, text);
    toggle(subControls, 'subVisible', '显示副图');
    const oscillator = document.createElement('select'); oscillator.setAttribute('aria-label', '副图指标');
    oscillator.add(new Option('MACD (12,26,9)', 'macd')); oscillator.add(new Option('RSI (14)', 'rsi')); oscillator.value = indicators.oscillator; oscillator.disabled = !indicators.subVisible;
    oscillator.addEventListener('change', () => { indicators.oscillator = oscillator.value; saveIndicators(); }); subControls.append(oscillator); toggle(subControls, 'volume', '成交量');
    indicatorToolbar.append(mainControls, subControls);
    const seriesCache = new Map(), modeButtons = [], periodButtons = [], rangeButtons = [];
    const presets = {daily: [20, 60, 120, 260], weekly: [13, 26, 52, 104], monthly: [12, 24, 36, 60]}, defaults = {daily: 60, weekly: 52, monthly: 36};
    const fullSeries = () => {
      if (!seriesCache.has(period)) seriesCache.set(period, aggregatePeriods(workingPayload.points, period, {from: workingPayload.history_from, through: workingPayload.history_through || workingPayload.as_of}));
      return seriesCache.get(period);
    };
    for (const [value, text] of [['line', '走势图'], ['candle', 'K线图']]) {
      const button = el('button', text); button.type = 'button'; button.addEventListener('click', () => { mode = value; chartModes.set(`${payload.market}:${payload.symbol}`, mode); redraw(); }); modeButtons.push({button, value}); types.append(button);
    }
    for (const [value, text] of [['daily', '日K'], ['weekly', '周K'], ['monthly', '月K']]) {
      const button = el('button', text); button.type = 'button'; button.addEventListener('click', () => {
        period = value; mode = 'candle'; chartModes.set(`${payload.market}:${payload.symbol}`, mode); view = {count: defaults[period], offset: 0}; selectedDate = null; redraw();
      }); periodButtons.push({button, value}); periods.append(button);
    }
    for (let index = 0; index < 4; index++) {
      const entry = {button: el('button'), value: presets.daily[index]}; entry.button.type = 'button';
      entry.button.addEventListener('click', () => { view = {count: entry.value, offset: 0}; selectedDate = null; redraw(); }); rangeButtons.push(entry); ranges.append(entry.button);
    }
    const reset = el('button', '重置视图', 'holding-view-reset'); reset.type = 'button'; reset.addEventListener('click', () => { view = {count: defaults[period], offset: 0}; selectedDate = null; redraw(); }); toolbar.append(reset);
    const body = el('div', null, 'holding-detail-body'), plot = el('div', null, 'holding-detail-plot');
    const dayControls = el('div', null, 'holding-day-controls'), previous = el('button'), next = el('button'); previous.type = next.type = 'button';
    const label = el('label'), dayCaption = el('span', '查看交易日'), day = document.createElement('select'); day.setAttribute('aria-label', '查看交易日'); label.append(dayCaption, day); dayControls.append(previous, label, next);
    const values = el('dl', null, 'holding-day-values'), fields = new Map(), fieldLabels = new Map();
    for (const [key, name] of [['open', '开盘价'], ['close', '收盘价'], ['high', '最高价'], ['low', '最低价'], ['volume', '成交量']]) {
      const item = el('div'), value = el('dd', '—'), caption = el('dt', `${name}（${key === 'volume' ? payload.volume_unit || '原始单位' : payload.currency}）`); item.append(caption, value); fieldLabels.set(key, caption); fields.set(key, value); values.append(item);
    }
    const details = el('details', null, 'holding-daily-table'), summary = el('summary'), wrap = el('div', null, 'table-wrap'), table = document.createElement('table');
    const head = document.createElement('thead'), headings = document.createElement('tr');
    const dateHeading = el('th', '日期'); headings.append(dateHeading);
    for (const text of ['开盘价', '收盘价', '最高价', '最低价', `成交量（${payload.volume_unit || '原始单位'}）`]) headings.append(el('th', text));
    head.append(headings); const tableBody = document.createElement('tbody'); table.append(head, tableBody); wrap.append(table); details.append(summary, wrap);
    const status = el('div', null, 'holding-view-status'), viewportStatus = el('span'), historyStatus = el('span'), retry = el('button', '重试历史'); retry.type = 'button'; retry.hidden = true; retry.addEventListener('click', loadLongHistory); status.append(viewportStatus, historyStatus, retry);
    const methods = el('details', null, 'holding-indicator-methods'); methods.append(el('summary', '指标参数与计算方式'));
    for (const text of ['MA5、MA20按对应周期收盘价计算；BOLL(20,2)使用20根收盘价的总体标准差。',
      'MACD(12,26,9)：EMA以首段简单均值初始化；DIF=EMA12−EMA26，DEA=EMA9(DIF)，柱值=2×(DIF−DEA)。',
      'RSI(14)采用Wilder平滑，参考线为30和70。指标先使用完整已加载历史计算，再截取当前视图；历史预热不足时留空。']) methods.append(el('p', text));
    const source = el('p', '', 'holding-detail-note'); body.append(status, plot, dayControls, values, details, methods, source); dialog.append(header, toolbar, indicatorToolbar, body);
    const select = (point, index) => {
      selectedIndex = index; selectedDate = point.date; day.value = point.date;
      for (const [key, field] of fields) { field.textContent = price(point[key]); field.className = key === 'close' && point.open != null ? trendClass(point.close - point.open) : ''; }
      const current = normalizeViewport(fullSeries().length, view), absolute = fullSeries().length - current.offset - current.count + index;
      previous.disabled = absolute === 0; next.disabled = absolute === fullSeries().length - 1;
      for (const [i, row] of rowElements.entries()) row.setAttribute('aria-selected', String(i === index));
    };
    function redraw() {
      const all = fullSeries(), current = normalizeViewport(all.length, view), end = all.length - current.offset;
      for (const entry of modeButtons) entry.button.setAttribute('aria-pressed', String(entry.value === mode));
      for (const entry of periodButtons) entry.button.setAttribute('aria-pressed', String(entry.value === period));
      for (const [index, entry] of rangeButtons.entries()) {
        entry.value = presets[period][index]; entry.button.textContent = `${entry.value}${periodLabels[period]}`;
        entry.button.setAttribute('aria-pressed', String(entry.value === current.count && current.offset === 0));
      }
      const points = all.slice(Math.max(0, end - current.count), end); day.replaceChildren(); tableBody.replaceChildren(); rowElements = [];
      previous.textContent = period === 'daily' ? '← 上一交易日' : `← 上一${periodLabels[period]}`;
      next.textContent = period === 'daily' ? '下一交易日 →' : `下一${periodLabels[period]} →`;
      dayCaption.textContent = period === 'daily' ? '查看交易日' : '查看周期'; day.setAttribute('aria-label', dayCaption.textContent); dateHeading.textContent = period === 'daily' ? '日期' : '周期';
      for (const [index, point] of points.entries()) {
        day.add(new Option(pointLabel(point), point.date)); const row = document.createElement('tr'), date = document.createElement('td'), button = el('button', pointLabel(point));
        button.type = 'button'; button.setAttribute('aria-label', `查看 ${pointLabel(point)} 行情`); button.addEventListener('click', () => controller.select(index)); date.append(button); row.append(date);
        for (const key of ['open', 'close', 'high', 'low', 'volume']) row.append(el('td', price(point[key])));
        tableBody.append(row); rowElements.push(row);
      }
      summary.textContent = `${period === 'daily' ? '逐日' : period === 'weekly' ? '逐周' : '逐月'}行情明细 · ${points.length} 根K线`;
      viewportStatus.textContent = points.length ? `${points[0].date} → ${points.at(-1).date} · 显示 ${points.length} / ${all.length} 根${periodLabels[period]}线` : '当前周期暂无行情';
      const volumeUnit = workingPayload.volume_unit || '原始单位';
      fieldLabels.get('volume').textContent = `成交量（${volumeUnit}）`; headings.lastElementChild.textContent = `成交量（${volumeUnit}）`;
      source.textContent = `价格：${workingPayload.adjust === 'split_dividend' ? '拆股分红调整' : '前复权'} · ${workingPayload.currency}；成交量保留数据源单位。${period === 'daily' ? '仅展示已完成交易日。' : '开盘取首日，收盘取末日，高低价取周期极值，成交量累加；未完结周期仅累计至数据截至日。'}来源：${workingPayload.source || '未取得'} · 截至 ${workingPayload.as_of || '未取得'}`;
      controller = render(plot, {...workingPayload, points: all}, current.count, {expanded: true, mode, period, indicators, offset: current.offset, selectedDate, onSelect: select,
        isPanning: () => Boolean(drag?.active), preventClick: () => Date.now() < suppressClickUntil});
      if (!controller) {
        controller = {points, select: index => { if (points.length) select(points[Math.max(0, Math.min(points.length - 1, index))], Math.max(0, Math.min(points.length - 1, index))); }};
        if (points.length) controller.select(points.length - 1); else { previous.disabled = next.disabled = true; for (const value of fields.values()) value.textContent = '—'; }
      }
    }
    function scheduleDraw() {
      if (frame) return; frame = requestAnimationFrame(() => { frame = 0; if (dialog.isConnected) redraw(); });
    }
    const chartLocation = event => {
      const svg = plot.querySelector('svg'); if (!svg) return null; const rect = svg.getBoundingClientRect();
      return {ratio: Math.max(0, Math.min(1, ((event.clientX - rect.left) / rect.width * 1000 - 65) / 921)), width: rect.width * .921};
    };
    plot.addEventListener('wheel', event => {
      if (!event.target.closest?.('svg') || !event.deltaY) return; const position = chartLocation(event); if (!position) return;
      event.preventDefault(); view = zoomViewport(fullSeries().length, view, event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 200 : 1), position.ratio); scheduleDraw();
    }, {passive: false});
    plot.addEventListener('pointerdown', event => {
      if (event.button !== 0 || !event.target.closest?.('svg')) return; const position = chartLocation(event); if (!position) return;
      drag = {id: event.pointerId, x: event.clientX, view: normalizeViewport(fullSeries().length, view), width: position.width, active: false};
      plot.setPointerCapture(event.pointerId); controller?.select(Math.round(position.ratio * (controller.points.length - 1))); event.preventDefault();
    });
    plot.addEventListener('pointermove', event => {
      if (!drag || drag.id !== event.pointerId) return; const movement = event.clientX - drag.x;
      if (!drag.active && Math.abs(movement) < 4) return;
      drag.active = true; plot.classList.add('is-dragging'); event.preventDefault(); view = panViewport(fullSeries().length, drag.view, movement / drag.width * drag.view.count); scheduleDraw();
    });
    const endDrag = event => {
      if (!drag || drag.id !== event.pointerId) return; if (drag.active) suppressClickUntil = Date.now() + 250;
      drag = null; plot.classList.remove('is-dragging'); if (plot.hasPointerCapture(event.pointerId)) plot.releasePointerCapture(event.pointerId);
    };
    plot.addEventListener('pointerup', endDrag); plot.addEventListener('pointercancel', endDrag);
    plot.addEventListener('lostpointercapture', event => { if (drag?.id === event.pointerId) { drag = null; plot.classList.remove('is-dragging'); } });
    function navigate(delta) {
      const all = fullSeries(), current = normalizeViewport(all.length, view), start = all.length - current.offset - current.count;
      const absolute = Math.max(0, Math.min(all.length - 1, start + selectedIndex + delta)); if (!all[absolute]) return;
      if (absolute < start || absolute >= start + current.count) { selectedDate = all[absolute].date; view = panViewport(all.length, current, -delta); redraw(); }
      else controller.select(absolute - start);
    }
    previous.addEventListener('click', () => navigate(-1)); next.addEventListener('click', () => navigate(1));
    day.addEventListener('change', () => { const index = controller.points.findIndex(point => point.date === day.value); if (index >= 0) controller.select(index); });
    async function loadLongHistory() {
      if (loadingLong || workingPayload.history_years === 5) return; loadingLong = true; retry.hidden = true; historyStatus.textContent = '正在补充5年历史…';
      if (!workingPayload.points?.length) { historyStatus.textContent = '正在加载5年历史…'; plot.textContent = '正在加载历史行情…'; }
      const endpoint = payload.historyEndpoint === '/api/stock-history' ? '/api/stock-history' : '/api/portfolio-history';
      const key = `${endpoint}:${payload.market}:${payload.symbol}:${payload.history_through || payload.as_of}`;
      try {
        if (!extendedHistories.has(key)) extendedHistories.set(key, fetch(endpoint + '?' + new URLSearchParams({market: payload.market, symbol: payload.symbol, years: '5'}))
          .then(async response => { const data = await response.json(); if (!response.ok || !data.available || !Array.isArray(data.points) || data.symbol !== payload.symbol || data.market !== payload.market) throw Error('历史行情不可用'); return data; })
          .catch(error => { extendedHistories.delete(key); throw error; }));
        const data = await extendedHistories.get(key); if (!dialog.isConnected) return;
        workingPayload = {...payload, ...data}; seriesCache.clear(); loadingLong = false; historyStatus.textContent = '已加载较长历史'; redraw();
      } catch { if (dialog.isConnected) { loadingLong = false; historyStatus.textContent = workingPayload.points?.length ? '较长历史暂不可用，仍可查看已加载数据' : '暂未取得该股票历史行情，请稍后重试'; retry.hidden = false; } }
    }
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return; const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
    dialog.addEventListener('close', () => {
      if (frame) cancelAnimationFrame(frame); dialog.remove();
      if (expandedDialog === dialog) { expandedDialog = null; document.documentElement.classList.remove('holding-dialog-open'); }
      if (inlineTarget?.isConnected) { render(inlineTarget, payload, initialWindow); inlineTarget.querySelector('.holding-enlarge')?.focus({preventScroll: true}); }
      else if (opener?.isConnected) opener.focus({preventScroll: true});
    });
    document.body.append(dialog); redraw(); document.documentElement.classList.add('holding-dialog-open'); dialog.showModal(); close.focus({preventScroll: true}); void loadLongHistory();
  }
  function openStock(stock, opener, contextNote = '') {
    openDialog({...stock, points: [], currency: {a: 'CNY', hk: 'HKD', us: 'USD'}[stock.market],
      historyEndpoint: '/api/stock-history', contextNote}, 60, 'candle', opener);
  }
  const api = {numeric, trendClass, formatChange, calculateIndicators, prepareSeries, aggregatePeriods, normalizeViewport, zoomViewport, panViewport, validCandle, candleGeometry, render, openStock};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PortfolioCharts = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
