(() => {
  const sortNumber = value => value == null || typeof value === 'boolean' || String(value).trim() === '' || !Number.isFinite(Number(value)) ? null : Number(value);
  function positionValue(row, quote) {
    const shares = sortNumber(row.shares), price = sortNumber(quote?.price);
    const value = shares != null && shares > 0 && price != null && price > 0 ? shares * price : null;
    return Number.isFinite(value) ? value : null;
  }
  function sortHoldings(rows, quotes, market, order = 'position-desc') {
    const byChange = ['change-desc', 'change-asc'].includes(order), ascending = order === 'change-asc';
    return rows.map((row, index) => {
      const quote = quotes.get(`${market}:${row.symbol}`);
      return {row, index, value: byChange ? sortNumber(quote?.change_pct) : positionValue(row, quote)};
    }).sort((a, b) => {
      if (a.value == null || b.value == null) return (a.value == null) - (b.value == null) || a.index - b.index;
      return (ascending ? a.value - b.value : b.value - a.value) || a.index - b.index;
    }).map(item => item.row);
  }
  if (typeof module === 'object' && module.exports) { module.exports = {positionValue, sortHoldings}; return; }
  const $ = selector => document.querySelector(selector);
  const market = $('#holdingsMarket'), form = $('#portfolioTaskForm'), submit = form.querySelector('[type=submit]');
  const log = $('#portfolioTaskLog'), cancel = $('#portfolioTaskCancel'), overview = $('#holdingsOverview');
  const currency = () => ({a: 'CNY', hk: 'HKD', us: 'USD'}[market.value]);
  const charts = window.PortfolioCharts;
  const node = (tag, value, className = '') => { const element = document.createElement(tag); element.textContent = String(value ?? '—'); element.className = className; return element; };
  const number = value => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits: 3});
  const pct = value => value == null || !Number.isFinite(Number(value)) ? '—' : `${(Number(value) * 100).toFixed(2)}%`;
  const stamp = value => value ? new Date(value).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}) + '（北京时间）' : '时间未记录';
  let holdings = [], holdingsReady = false, quotes = new Map(), options, ranges, currentJob = null, timer, starting = false;
  let holdingsRequest = 0, quoteRequest = 0, optionsRequest = 0, historyRequest = 0, detailRequest = 0, deploymentRequest = 0, selectedRun = '';
  const trendData = new Map(), trendTargets = new Map();
  let trendWindow = 60, trendRequest = 0, trendController;
  const currentHoldings = () => holdings.filter(row => (row.market || 'a') === market.value);
  async function api(path, init) { const response = await fetch(path, init), result = await response.json(); if (!response.ok) throw Error(result.error || `HTTP ${response.status}`); return result; }
  function updateBusy() {
    const busy = starting || Boolean(currentJob);
    for (const field of form.querySelectorAll('input,select')) field.disabled = busy;
    market.disabled = busy || $('#portfolioButton').dataset.analyzing === 'true';
    $('#portfolioMarket').disabled = market.disabled;
    form.elements.strategy.disabled = busy || !options;
    submit.disabled = busy || !holdingsReady || !currentHoldings().length || !form.elements.strategy.value;
    cancel.hidden = !currentJob;
  }
  function renderHoldings() {
    overview.replaceChildren();
    trendTargets.clear();
    const rows = sortHoldings(currentHoldings(), quotes, market.value, $('#holdingsSort').value);
    $('#portfolioPoolSummary').textContent = rows.length ? `当前持仓池 · ${rows.length} 只：${rows.map(row => row.symbol).join('、')}` : '当前市场暂无持仓，请先添加并保存。';
    $('#holdingsMeta').textContent = `当前市场 ${rows.length} 只 · 共保存 ${holdings.length} 条持仓记录`;
    if (!rows.length) overview.append(node('p', '还没有仓内股票。点击“添加 / 管理持仓”开始。', 'empty-note'));
    let total = 0, complete = rows.length > 0;
    const allocations = [];
    for (const row of rows) {
      const quote = quotes.get(`${market.value}:${row.symbol}`), card = node('article', '', 'holding-card');
      card.append(node('h3', row.name || row.symbol), node('small', row.symbol), node('p', `持股 ${number(row.shares)} 股`));
      const value = positionValue(row, quote);
      if (value == null) complete = false; else total += value;
      const change = charts.numeric(quote?.change_pct), direction = charts.trendClass(change);
      card.append(node('p', `${number(quote?.price)} ${currency()}`, `holding-price ${direction}`));
      const daily = node('p', '', 'holding-daily-change');
      daily.append(node('span', `${change == null ? '—' : change > 0 ? '↑' : change < 0 ? '↓' : '—'} 当日 ${charts.formatChange(change)}`, `holding-change-badge ${direction}`));
      card.append(daily, node('p', `市值 ${number(value)} ${currency()}`));
      card.append(node('small', quote ? `数据时间 ${quote.as_of || '未取得'} · ${quote.source || quote.error || '行情暂缺'}` : '尚未读取行情'));
      const allocation = node('div', '', 'holding-allocation'); allocations.push({target: allocation, value}); card.append(allocation);
      const key = `${market.value}:${row.symbol}`, trend = node('div', '', 'holding-chart-slot');
      trend.setAttribute('aria-label', `${row.name || row.symbol} 价格走势`); trendTargets.set(key, trend); card.append(trend);
      overview.append(card);
    }
    if (complete) $('#holdingsMeta').textContent += ` · 股票市值 ${number(total)} ${currency()}（不含现金）`;
    if (complete && total > 0) for (const {target, value} of allocations) {
      const weight = value / total * 100, label = node('span', `本市场股票市值占比 ${weight.toFixed(1)}%`), bar = document.createElement('progress');
      bar.max = 100; bar.value = weight; bar.setAttribute('aria-label', label.textContent); target.append(label, bar);
    }
    for (const key of trendTargets.keys()) renderTrend(key);
    updateBusy();
  }
  function renderTrend(key) {
    const target = trendTargets.get(key); if (!target) return;
    const data = trendData.get(key);
    if (!data) { target.replaceChildren(node('p', '正在读取历史日线…', 'holding-chart-empty')); target.setAttribute('aria-busy', 'true'); return; }
    target.setAttribute('aria-busy', 'false');
    if (data.error) {
      const retry = node('button', '重试走势', 'holding-chart-retry'); retry.type = 'button';
      retry.addEventListener('click', () => { trendData.delete(key); void loadTrends(); });
      target.replaceChildren(node('p', `历史走势暂不可用：${data.error}`, 'holding-chart-empty'), retry); return;
    }
    const holding = currentHoldings().find(row => `${market.value}:${row.symbol}` === key);
    charts.render(target, {...data, name: holding?.name}, trendWindow);
  }
  async function loadTrends() {
    const request = ++trendRequest, selectedMarket = market.value;
    trendController?.abort(); trendController = new AbortController(); const signal = trendController.signal;
    const queue = currentHoldings().filter(row => !trendData.has(`${selectedMarket}:${row.symbol}`));
    for (const row of queue) renderTrend(`${selectedMarket}:${row.symbol}`);
    async function worker() {
      while (queue.length && request === trendRequest) {
        const row = queue.shift(), key = `${selectedMarket}:${row.symbol}`;
        try {
          const result = await api('/api/portfolio-history?' + new URLSearchParams({market: selectedMarket, symbol: row.symbol}), {signal});
          if (request !== trendRequest || selectedMarket !== market.value) return;
          trendData.set(key, result);
        } catch (error) {
          if (signal.aborted || request !== trendRequest) return;
          trendData.set(key, {error: error.message});
        }
        renderTrend(key);
      }
    }
    await Promise.all(Array.from({length: Math.min(3, queue.length)}, worker));
  }
  async function loadHoldings() {
    const request = ++holdingsRequest;
    holdingsReady = false; updateBusy();
    try {
      const result = await api('/api/portfolio'); if (request !== holdingsRequest) return;
      holdings = result.holdings || []; holdingsReady = true; renderHoldings();
      void loadTrends();
      if (currentHoldings().length) void loadQuotes();
    } catch (error) { if (request === holdingsRequest) { $('#holdingsMeta').textContent = `持仓读取失败：${error.message}`; updateBusy(); } }
  }
  async function loadQuotes() {
    const request = ++quoteRequest, selectedMarket = market.value, button = $('#portfolioQuotes');
    button.disabled = true; button.textContent = '正在读取行情…';
    try {
      const result = await api(`/api/portfolio-quotes?market=${selectedMarket}`);
      if (request !== quoteRequest || selectedMarket !== market.value) return;
      for (const quote of result.quotes || []) quotes.set(`${selectedMarket}:${quote.symbol}`, quote);
      renderHoldings();
    } catch (error) { if (request === quoteRequest) $('#holdingsMeta').textContent = `行情读取失败：${error.message}`; }
    finally { if (request === quoteRequest) { button.disabled = false; button.textContent = '刷新行情'; } }
  }
  function renderParameters() {
    const target = $('#portfolioTaskParameters'), entry = options?.strategies?.[form.elements.strategy.value]; target.replaceChildren();
    for (const [key, spec] of Object.entries(entry?.parameter_schema || {})) {
      const label = node('label', spec.label), field = document.createElement('input');
      field.type = 'number'; field.min = spec.min; field.max = spec.max; field.step = spec.type === 'integer' ? '1' : 'any'; field.required = true;
      field.value = entry.parameters?.[key] ?? spec.default; field.dataset.parameter = key; label.append(field); target.append(label);
    }
    const range = entry?.training_range, cutoff = range && [range.label_end_day, range.requested_end, range.last_day].filter(Boolean).sort().at(-1);
    if (cutoff && form.elements.start.value <= cutoff) form.elements.start.value = new Date(Date.parse(cutoff) + 86400000).toISOString().slice(0, 10);
  }
  function updateMenu(resetDates = false) {
    const deploying = form.elements.kind.value === 'deploy', previous = form.elements.strategy.value;
    const select = form.elements.strategy; select.replaceChildren();
    for (const [identity, entry] of Object.entries(options?.strategies || {})) {
      if (!deploying || !entry.checkpoint_id || entry.validation?.passed === true) select.add(new Option(entry.name, identity));
    }
    if ([...select.options].some(option => option.value === previous)) select.value = previous;
    $('#portfolioStartField').hidden = $('#portfolioEndField').hidden = deploying;
    form.elements.start.required = form.elements.end.required = !deploying;
    submit.textContent = deploying ? '部署持仓股票池' : '运行持仓池回测';
    if (resetDates && ranges) {
      form.elements.end.value = ranges.backtest_end;
      form.elements.start.value = ranges.backtest_start;
      const days = {weekly: 400, monthly: 900}[form.elements.timeframe.value];
      if (days) form.elements.start.value = new Date(Date.parse(ranges.backtest_end) - days * 86400000).toISOString().slice(0, 10);
    }
    renderParameters(); updateBusy();
  }
  async function loadOptions(resetDates = true) {
    const request = ++optionsRequest, selectedMarket = market.value, timeframe = form.elements.timeframe.value;
    options = null; updateBusy();
    try {
      const [catalog, research] = await Promise.all([api(`/api/bar-strategies?market=${selectedMarket}&timeframe=${timeframe}`), api('/api/research-options')]);
      if (request !== optionsRequest) return;
      options = catalog; ranges = research.ranges; updateMenu(resetDates);
      await loadDeployment();
    } catch (error) { if (request === optionsRequest) log.textContent = `策略读取失败：${error.message}`; }
  }
  function snapshot(parent, symbols, asOf) {
    parent.append(node('p', `任务持仓快照 · ${stamp(asOf)} · ${(symbols || []).join('、') || '名单未记录'}`, 'portfolio-snapshot'));
    if (symbols && [...symbols].sort().join(',') !== currentHoldings().map(row => row.symbol).sort().join(',')) parent.append(node('p', '此记录的股票名单与当前持仓不同，保留原任务快照供核对。', 'ranking-note'));
  }
  async function loadDeployment() {
    const request = ++deploymentRequest, target = $('#portfolioDeployment'); target.replaceChildren();
    if (!form.elements.strategy.value) return;
    const [strategy, checkpoint = ''] = form.elements.strategy.value.split('@');
    const query = new URLSearchParams({market: market.value, timeframe: form.elements.timeframe.value, strategy, checkpoint, pool: 'portfolio'});
    try {
      const result = await api('/api/bar-deployment?' + query); if (request !== deploymentRequest) return;
      if (!result.available) { if (form.elements.kind.value === 'deploy') target.append(node('p', '当前选择尚无持仓池部署记录。', 'empty-note')); return; }
      target.append(node('h3', '已保存的持仓池部署'), node('p', `${result.strategy_name} · ${result.period_label} · 信号 ${result.as_of} · ${result.valid_count}/${result.input_count} 只有效`, 'ranking-note'));
      snapshot(target, result.symbols, result.pool?.as_of);
      target.append(node('p', `归档参数：${JSON.stringify(result.strategy_parameters || {})}`, 'ranking-note'));
      const wrap = node('div', '', 'table-wrap'), table = document.createElement('table'), head = document.createElement('thead'), tr = document.createElement('tr');
      for (const label of ['排名', '股票', '信号价格', '策略分数', '状态']) tr.append(node('th', label)); head.append(tr);
      const body = document.createElement('tbody');
      for (const row of result.ranking || []) { const line = document.createElement('tr'); for (const value of [row.rank, row.symbol, number(row.price), number(row.score), row.entry ? '已触发' : '未触发']) line.append(node('td', value)); body.append(line); }
      table.append(head, body); wrap.append(table); target.append(wrap);
    } catch (error) { if (request === deploymentRequest) target.append(node('p', `部署记录暂不可读：${error.message}`, 'empty-note')); }
  }
  function curveChart(points, field, title) {
    const section = document.createElement('section'); section.append(node('h3', title));
    const values = points.map(point => Number(point[field]));
    if (!points.length || values.some(value => !Number.isFinite(value))) { section.append(node('p', '暂无有效曲线。', 'empty-note')); return section; }
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 800 220'); svg.classList.add('portfolio-curve'); svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', `${title}，${points[0].day} 至 ${points.at(-1).day}`);
    const low = Math.min(...values), high = Math.max(...values), span = high - low || .01;
    const path = document.createElementNS(svg.namespaceURI, 'polyline');
    path.setAttribute('points', values.map((value, i) => `${30 + i * 740 / Math.max(values.length - 1, 1)},${190 - (value - low) * 160 / span}`).join(' '));
    path.setAttribute('fill', 'none'); path.setAttribute('stroke', '#6c59f4'); path.setAttribute('stroke-width', '3'); svg.append(path);
    section.append(svg, node('p', `${points[0].day} → ${points.at(-1).day} · 范围 ${field === 'drawdown' ? pct(low) : number(low)} 至 ${field === 'drawdown' ? pct(high) : number(high)}`, 'ranking-note'));
    return section;
  }
  async function loadDetail(id) {
    const request = ++detailRequest, target = $('#portfolioRunDetail'); selectedRun = id; target.replaceChildren(node('p', '正在读取回测详情…', 'empty-note'));
    try {
      const result = await api('/api/backtests/' + encodeURIComponent(id)); if (request !== detailRequest) return;
      target.replaceChildren(node('h3', `${result.strategy_name} · ${result.first_day} 至 ${result.last_day}`));
      snapshot(target, result.universe, result.pool?.as_of);
      target.append(node('p', `归档参数：${JSON.stringify(result.parameters || {})}${result.checkpoint_id ? ` · checkpoint ${result.checkpoint_id}` : ''}`, 'ranking-note'));
      const stats = result.stats || {}, metrics = node('div', '', 'portfolio-metrics');
      for (const [label, value] of [['累计收益', pct(stats.total_return)], ['最大回撤', pct(stats.max_drawdown)], ['Sharpe', number(stats.sharpe)], ['模拟交易', `${number(stats.trades)} 笔`]]) { const item = document.createElement('div'); item.append(node('span', label), node('strong', value)); metrics.append(item); }
      target.append(metrics, curveChart(result.curve || [], 'net_value', '累计净值（初始值 1）'), curveChart(result.curve || [], 'drawdown', '资金回撤'));
      if (result.curve_error) target.append(node('p', result.curve_error, 'empty-note'));
      const downloads = node('div', '', 'backtest-downloads');
      for (const [file, label] of [['equity.csv', '下载净值数据'], ['trades.csv', '下载交易明细']]) { if (!result.downloads?.[file]) continue; const link = node('a', label); link.href = result.downloads[file]; link.download = `${id}-${file}`; downloads.append(link); }
      target.append(downloads);
      for (const limitation of result.limitations || []) target.append(node('p', limitation, 'ranking-note'));
    } catch (error) { if (request === detailRequest) target.replaceChildren(node('p', `回测详情暂不可读：${error.message}`, 'empty-note')); }
  }
  async function loadHistory() {
    const request = ++historyRequest, selectedMarket = market.value, timeframe = form.elements.timeframe.value;
    try {
      const result = await api('/api/backtests?pool=portfolio'); if (request !== historyRequest) return;
      const runs = (result.runs || []).filter(run => run.market === selectedMarket && run.timeframe === timeframe && run.pool?.id === 'portfolio');
      $('#portfolioHistoryMeta').textContent = runs.length ? `当前市场与周期共 ${runs.length} 次持仓池回测；按任务时的持仓快照归档。` : '当前市场与周期暂无持仓池回测，选择策略后即可运行。';
      const target = $('#portfolioRuns'); target.replaceChildren();
      if (!runs.length) { ++detailRequest; $('#portfolioRunDetail').replaceChildren(); return; }
      const label = node('label', '选择历史记录', 'portfolio-run-select'), select = document.createElement('select');
      for (const run of runs) select.add(new Option(`${run.strategy_name} · ${run.start} → ${run.end} · ${stamp(run.generated_at)}`, run.run_id));
      if (runs.some(run => run.run_id === selectedRun)) select.value = selectedRun;
      label.append(select); target.append(label); select.addEventListener('change', () => loadDetail(select.value)); await loadDetail(select.value);
    } catch (error) { if (request === historyRequest) $('#portfolioHistoryMeta').textContent = `回测记录暂不可读：${error.message}`; }
  }
  async function poll() {
    if (!currentJob) return;
    const id = currentJob.id;
    try {
      const {jobs} = await api('/api/jobs'); if (currentJob?.id !== id) return;
      const job = jobs.find(item => item.id === id); if (!job) throw Error('任务记录已不存在，请刷新页面。');
      log.textContent = job.status === 'running' ? job.progress || '正在准备持仓池行情，可离开页面，任务会继续运行。' : job.error || job.result?.text || '已完成。';
      if (job.status === 'running') return;
      clearInterval(timer); currentJob = null; updateBusy();
      if (job.status === 'completed') { if (job.kind === 'backtest') { selectedRun = job.result?.data?.run_id || ''; await loadHistory(); } else await loadDeployment(); }
    } catch (error) { log.textContent = `任务状态暂不可读：${error.message}`; }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (currentJob || starting || submit.disabled) return;
    const input = {kind: form.elements.kind.value, market: market.value, timeframe: form.elements.timeframe.value, strategy: form.elements.strategy.value, pool: 'portfolio',
      parameters: Object.fromEntries([...$('#portfolioTaskParameters').querySelectorAll('[data-parameter]')].map(field => [field.dataset.parameter, Number(field.value)]))};
    if (input.kind === 'backtest') { input.start = form.elements.start.value; input.end = form.elements.end.value; }
    starting = true; updateBusy(); log.textContent = '正在读取已保存持仓并创建任务…';
    try {
      currentJob = await api('/api/jobs', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(input)});
      timer = setInterval(poll, 2000); await poll();
    } catch (error) { log.textContent = `任务未启动：${error.message}`; }
    finally { starting = false; updateBusy(); }
  });
  cancel.addEventListener('click', async () => { if (!currentJob) return; try { await api(`/api/jobs/${currentJob.id}/cancel`, {method: 'POST'}); await poll(); } catch (error) { log.textContent = error.message; } });
  function changeMarket(value) {
    market.value = value;
    const analysisMarket = $('#portfolioMarket');
    if (analysisMarket.value !== value) { analysisMarket.value = value; analysisMarket.dispatchEvent(new Event('change')); }
    ++quoteRequest; $('#portfolioQuotes').disabled = false; $('#portfolioQuotes').textContent = '刷新行情';
    renderHoldings(); void loadTrends(); if (currentHoldings().length) void loadQuotes(); void loadOptions(); void loadHistory();
  }
  market.addEventListener('change', () => changeMarket(market.value));
  $('#holdingsSort').addEventListener('change', renderHoldings);
  $('#portfolioMarket').addEventListener('change', () => { if ($('#portfolioMarket').value !== market.value) changeMarket($('#portfolioMarket').value); });
  form.elements.timeframe.addEventListener('change', () => { void loadOptions(); void loadHistory(); });
  form.elements.kind.addEventListener('change', () => { updateMenu(); void loadDeployment(); });
  form.elements.strategy.addEventListener('change', () => { renderParameters(); void loadDeployment(); });
  $('#portfolioRefresh').addEventListener('click', () => { quotes.clear(); trendData.clear(); trendController?.abort(); ++trendRequest; ++quoteRequest; $('#portfolioQuotes').disabled = false; $('#portfolioQuotes').textContent = '刷新行情'; void loadHoldings(); document.dispatchEvent(new Event('portfolio-refresh')); void loadHistory(); void loadDeployment(); });
  $('#portfolioQuotes').addEventListener('click', loadQuotes);
  document.addEventListener('portfolio-saved', () => { quotes.clear(); ++quoteRequest; $('#portfolioQuotes').disabled = false; $('#portfolioQuotes').textContent = '刷新行情'; void loadHoldings(); });
  document.addEventListener('portfolio-analysis-state', updateBusy);
  for (const button of document.querySelectorAll('[data-trend-window]')) button.addEventListener('click', () => {
    trendWindow = Number(button.dataset.trendWindow);
    for (const control of document.querySelectorAll('[data-trend-window]')) control.setAttribute('aria-pressed', String(control === button));
    for (const key of trendTargets.keys()) renderTrend(key);
  });
  async function initialize() {
    try {
      const {jobs} = await api('/api/jobs');
      currentJob = jobs.find(job => ['backtest', 'deploy'].includes(job.kind) && job.configuration?.pool === 'portfolio' && job.status === 'running') || null;
      if (currentJob) {
        const config = currentJob.configuration; market.value = config.market || 'a'; $('#portfolioMarket').value = market.value;
        form.elements.kind.value = currentJob.kind; form.elements.timeframe.value = config.timeframe || 'daily';
        await loadOptions(); form.elements.strategy.value = config.strategy; updateMenu();
        for (const key of ['start', 'end']) if (config[key]) form.elements[key].value = config[key];
        for (const field of $('#portfolioTaskParameters').querySelectorAll('[data-parameter]')) if (config.parameters?.[field.dataset.parameter] != null) field.value = config.parameters[field.dataset.parameter];
        updateBusy(); timer = setInterval(poll, 2000); void poll();
      } else await loadOptions();
    } catch (error) { log.textContent = `任务恢复失败：${error.message}`; await loadOptions(); }
    await loadHoldings(); void loadHistory();
    document.dispatchEvent(new Event('portfolio-ready'));
  }
  void initialize();
})();
