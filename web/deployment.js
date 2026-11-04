(() => {
  const form = document.querySelector('#barDeploymentForm');
  if (!form) return;
  const meta = document.querySelector('#barDeploymentMeta');
  const target = document.querySelector('#barDeploymentContent');
  const parameters = document.querySelector('#barDeploymentParameters');
  const submit = form.querySelector('[type=submit]');
  let options, timer, currentJob, optionsRequest = 0;
  async function api(path, init) {
    const response = await fetch(path, init);
    const value = await response.json();
    if (!response.ok) throw Error(value.error || `HTTP ${response.status}`);
    return value;
  }
  function text(tag, value) {
    const node = document.createElement(tag); node.textContent = String(value ?? '—'); return node;
  }
  function updatePool() {
    const aMarket = form.elements.market.value === 'a';
    const pool = form.elements.pool, symbols = form.elements.symbols;
    if (!aMarket) pool.value = 'custom';
    const preset = aMarket && pool.value !== 'custom';
    document.querySelector('#deploymentPoolField').hidden = !aMarket;
    document.querySelector('#deploymentSymbolsField').hidden = preset;
    symbols.required = !preset;
    symbols.disabled = preset;
    pool.disabled = !aMarket;
  }
  function renderParameters() {
    parameters.replaceChildren();
    const entry = options?.strategies?.[form.elements.strategy.value];
    for (const [key, field] of Object.entries(entry?.parameter_schema || {})) {
      const label = text('label', field.label);
      const input = document.createElement('input');
      input.type = 'number'; input.dataset.parameter = key; input.min = field.min; input.max = field.max;
      input.step = field.type === 'integer' ? '1' : 'any'; input.required = true;
      input.value = entry.parameters?.[key] ?? field.default;
      label.append(input); parameters.append(label);
    }
  }
  function render(result) {
    target.replaceChildren();
    if (!result?.available) { meta.textContent = '当前选择尚无策略部署记录。'; return; }
    meta.textContent = `${result.period_label} · ${result.strategy_name} · ${result.pool?.name || '自定股票池'}（${result.pool?.as_of || '用户输入'}）· ${result.as_of} 已完成K线 · 行情可用 ${result.valid_count}/${result.input_count} 只 · 形成排名 ${result.ranked_count ?? result.ranking?.length ?? 0} 只 · ${result.note}`;
    const table = document.createElement('table');
    const head = document.createElement('thead');
    const headings = document.createElement('tr');
    for (const value of ['排名', '代码', '信号收盘价', '策略分数', '状态']) headings.append(text('th', value));
    head.append(headings);
    const body = document.createElement('tbody');
    const ranking = result.ranking || [];
    let shown = 0;
    function addRows() {
      for (const row of ranking.slice(shown, shown + 100)) {
      const line = document.createElement('tr');
      for (const value of [row.rank, row.symbol, row.price, Number(row.score).toFixed(4), row.entry ? '已触发' : '未触发']) line.append(text('td', value));
      body.append(line);
      }
      shown = Math.min(shown + 100, ranking.length);
      if (more) more.hidden = shown >= ranking.length;
    }
    table.append(head, body);
    const wrapper = document.createElement('div'); wrapper.className = 'table-wrap'; wrapper.append(table);
    const more = ranking.length > 100 ? text('button', '显示更多（每次100只）') : null;
    if (more) { more.type = 'button'; more.addEventListener('click', addRows); }
    addRows(); target.append(wrapper); if (more) target.append(more);
  }
  async function latest() {
    const {market, timeframe, strategy, pool} = form.elements;
    if (!strategy.value) return;
    const [algorithm, checkpoint = ''] = strategy.value.split('@');
    try { render(await api(`/api/bar-deployment?market=${market.value}&timeframe=${timeframe.value}&strategy=${encodeURIComponent(algorithm)}&checkpoint=${encodeURIComponent(checkpoint)}&pool=${pool.value}`)); }
    catch (error) { meta.textContent = `部署记录暂不可读：${error.message}`; }
  }
  async function loadOptions() {
    const {market, timeframe, strategy, symbols} = form.elements;
    const request = ++optionsRequest, selectedMarket = market.value, selectedTimeframe = timeframe.value;
    const previous = strategy.value;
    submit.disabled = true;
    strategy.replaceChildren(new Option('正在读取…', ''));
    symbols.placeholder = market.value === 'us' ? 'AAPL, MSFT, NVDA' : market.value === 'hk' ? '00700, 09988' : '600519, 000858';
    updatePool();
    try {
      const result = await api(`/api/bar-strategies?market=${selectedMarket}&timeframe=${selectedTimeframe}`);
      if (request !== optionsRequest) return;
      options = result;
      strategy.replaceChildren();
      for (const [key, entry] of Object.entries(options.strategies)) {
        if (!entry.checkpoint_id || entry.validation?.passed === true) strategy.add(new Option(entry.name, key));
      }
      if ([...strategy.options].some(option => option.value === previous)) strategy.value = previous;
      renderParameters(); submit.disabled = Boolean(currentJob);
      await latest();
    } catch (error) { if (request === optionsRequest) meta.textContent = `策略列表暂不可读：${error.message}`; }
  }
  async function poll() {
    if (!currentJob) return;
    try {
      const {jobs} = await api('/api/jobs');
      const job = jobs.find(item => item.id === currentJob);
      if (!job) throw Error('任务记录已不存在。');
      if (job.status === 'running') { meta.textContent = job.progress || '正在读取历史行情并计算策略…'; return; }
      clearInterval(timer); currentJob = null; submit.disabled = false;
      if (job.status === 'completed') await latest();
      else meta.textContent = `策略部署失败：${job.error || '未知错误'}`;
    } catch (error) { meta.textContent = `任务状态暂不可读：${error.message}`; }
  }
  form.elements.market.addEventListener('change', loadOptions);
  form.elements.pool.addEventListener('change', () => { updatePool(); void latest(); });
  form.elements.timeframe.addEventListener('change', loadOptions);
  form.elements.strategy.addEventListener('change', () => { renderParameters(); void latest(); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (currentJob) return;
    const input = {kind: 'deploy', market: form.elements.market.value, timeframe: form.elements.timeframe.value,
                   strategy: form.elements.strategy.value, pool: form.elements.pool.value,
                   symbols: form.elements.symbols.value.split(/[,，\s]+/).filter(Boolean),
                   parameters: Object.fromEntries([...parameters.querySelectorAll('[data-parameter]')].map(field => [field.dataset.parameter, Number(field.value)]))};
    submit.disabled = true; meta.textContent = '正在创建策略部署任务…';
    try {
      const job = await api('/api/jobs', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(input)});
      currentJob = job.id; timer = setInterval(poll, 2000); await poll();
    } catch (error) { submit.disabled = false; meta.textContent = `策略部署未启动：${error.message}`; }
  });
  void loadOptions();
})();
