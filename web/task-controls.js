(() => {
  const form = document.querySelector('#taskForm'); if (!form) return;
  const kind = form.dataset.kind, log = document.querySelector('#taskLog'), submit = form.querySelector('[type=submit]');
  const selection = form.querySelector('[name=strategy]'), action = form.querySelector('[name=action]');
  let timer, busy = false, strategyOptions, researchOptions, optionsRequest = 0;
  const tabs = [...document.querySelectorAll('[data-mode]')];
  function updatePool() {
    const aMarket = form.elements.market?.value === 'a';
    const pool = form.elements.pool, symbols = form.elements.symbols;
    if (!pool || !symbols) return;
    if (!aMarket) pool.value = 'custom';
    const preset = aMarket && pool.value !== 'custom';
    document.querySelector('#poolField').hidden = !aMarket;
    document.querySelector('#symbolsField').hidden = preset;
    symbols.required = !preset;
    symbols.disabled = busy || preset;
    pool.disabled = busy || !aMarket;
  }
  function setMode(mode, resetDates = true) {
    if (!action) return;
    action.value = mode === 'train' ? 'train' : 'backtest';
    const training = action.value === 'train';
    for (const tab of tabs) { const active = tab.dataset.mode === action.value; tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1; }
    const panel = document.querySelector('#workbenchPanel');
    if (panel) {
      panel.setAttribute('aria-labelledby', training ? 'trainTab' : 'backtestTab');
      document.querySelector('#runTitle').textContent = training ? '训练模型' : '运行回测';
      document.querySelector('#runDescription').textContent = training ? '选择市场、K线周期和股票池，训练独立的 XGBoost 或 Elastic Net 模型。A股可选沪深300、中证500或自定股票池。' : '选择市场、K线周期、股票池及策略，查看对应周期的净值、回撤与交易明细。';
      document.querySelector('#strategyLabel').textContent = training ? '训练模型' : '策略 / 已训练模型';
      document.querySelector('#startLabel').textContent = training ? '训练开始' : '回测开始';
      document.querySelector('#endLabel').textContent = training ? '训练结束' : '回测结束';
      document.querySelector('#parameterTitle').textContent = training ? '训练超参数' : '策略超参数';
      form.querySelector('details').open = training;
      document.querySelector('#trainedModels').hidden = false;
      const url = new URL(location.href); if (training) url.searchParams.set('mode', 'train'); else url.searchParams.delete('mode'); history.replaceState(null, '', url);
    }
    updateMenu(resetDates); updatePool();
  }
  function setBusy(value) {
    busy = value; submit.disabled = value || Boolean(selection && !strategyOptions);
    for (const field of form.querySelectorAll('input,select,details input')) field.disabled = value;
    for (const tab of tabs) tab.disabled = value;
    for (const button of document.querySelectorAll('#modelList button')) button.disabled = value;
    updatePool();
  }
  for (const tab of tabs) {
    tab.addEventListener('click', () => setMode(tab.dataset.mode));
    tab.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault(); const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (tabs.indexOf(tab) + (event.key === 'ArrowLeft' ? -1 : 1) + tabs.length) % tabs.length;
      tabs[index].focus(); setMode(tabs[index].dataset.mode);
    });
  }
  function renderModels() {
    const target = document.querySelector('#modelList'); if (!target) return;
    const models = Object.entries(strategyOptions.models || {}); target.replaceChildren();
    document.querySelector('#modelsCount').textContent = `${models.length} 个版本`;
    if (!models.length) { const note = document.createElement('p'); note.textContent = '还没有训练记录。从上方选择模型，开始第一次训练。'; target.append(note); }
    for (const [identity, model] of models) {
      const card = document.createElement('article'), title = document.createElement('strong'), detail = document.createElement('p'), button = document.createElement('button');
      title.textContent = model.name; const range = model.training_range || {};
      const verdict = model.validation?.passed === true ? '验证通过' : model.validation ? '未通过验证 · 仅可回测' : '旧模型未记录验证基线 · 仅可回测';
      detail.textContent = `${verdict} · ${model.pool?.name || '自定股票池'}${model.requested_count ? ` · 行情可用 ${model.valid_count ?? model.requested_count}/${model.requested_count} 只` : ''} · ${range.requested_start || range.first_day || '起始日期未归档'} → ${range.requested_end || range.last_day || model.training_last_day || '结束日期未归档'}`;
      const version = document.createElement('small'); version.textContent = model.checkpoint_id;
      button.type = 'button'; button.textContent = '使用此模型回测'; button.disabled = busy;
      button.addEventListener('click', () => {
        setMode('backtest'); selection.value = identity;
        for (const key of ['start', 'end']) form.elements[key].value = researchOptions.ranges[`backtest_${key}`];
        parameters(); document.querySelector('#strategyWorkbench').scrollIntoView({behavior: 'smooth', block: 'start'}); selection.focus({preventScroll: true});
      });
      const info = document.createElement('details'), summary = document.createElement('summary');
      summary.textContent = '查看版本详情'; info.append(summary);
      for (const value of [
        `股票池来源：${model.pool?.source || '用户输入'}；名单日期：${model.pool?.as_of || '—'}`,
        `训练验证起点：${range.validation_start || '—'}；训练标签结束：${range.label_end_day || '—'}`,
        `验证均方误差：${Number.isFinite(model.validation_mse) ? Number(model.validation_mse).toFixed(6) : '未记录'}；零收益基线：${Number.isFinite(model.validation?.zero_baseline_mse) ? model.validation.zero_baseline_mse.toFixed(6) : '未记录'}`,
        `逐日排序相关性：${Number.isFinite(model.validation?.daily_rank_ic) ? model.validation.daily_rank_ic.toFixed(4) : '未记录'}；前五名超额均值：${Number.isFinite(model.validation?.top5_excess_return) ? (model.validation.top5_excess_return * 100).toFixed(2) + '%' : '未记录'}`,
        `预测为正的前五名平均收益：${Number.isFinite(model.validation?.top5_gross_return) ? (model.validation.top5_gross_return * 100).toFixed(2) + '%' : '未记录'}；模拟双边成本约 0.20%`,
        `验证结论：${model.validation?.reason || verdict}`
      ]) { const line = document.createElement('p'); line.textContent = value; info.append(line); }
      const actions = document.createElement('div'); actions.className = 'model-actions';
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'model-remove'; remove.textContent = '删除模型'; remove.disabled = busy;
      remove.addEventListener('click', async () => {
        if (busy || !window.confirm(`确定删除模型 ${model.checkpoint_id}？模型文件将被删除，已有历史回测归档会保留。`)) return;
        const market = form.elements.market.value, timeframe = form.elements.timeframe.value;
        const status = document.querySelector('#modelManageStatus');
        setBusy(true); status.textContent = '正在删除模型…';
        try {
          await api(`/api/bar-models/${market}/${timeframe}/${encodeURIComponent(model.checkpoint_id)}`, {method: 'DELETE'});
          strategyOptions = await api(`/api/bar-strategies?market=${market}&timeframe=${timeframe}`);
          updateMenu(false); renderModels();
          status.textContent = `已删除模型 ${model.checkpoint_id}。`;
        } catch (error) { status.textContent = `删除失败：${error.message}`; }
        finally { setBusy(false); }
      });
      actions.append(button, remove); card.append(title, detail, version, info, actions); target.append(card);
    }
  }
  async function api(path, options) { const response = await fetch(path, options), data = await response.json(); if (!response.ok) throw Error(data.error || '请求失败。'); return data; }
  function parameters() {
    const target = document.querySelector('#taskParameters'); if (!target) return;
    target.replaceChildren();
    const entry = (action.value === 'train' ? strategyOptions?.training : strategyOptions?.strategies)?.[selection.value];
    if (action.value !== 'train' && entry?.training_range) {
      const range = entry.training_range, cutoff = [range.label_end_day, range.requested_end, range.last_day].filter(Boolean).sort().at(-1);
      if (cutoff && form.elements.start.value <= cutoff) { const date = new Date(cutoff + 'T00:00:00Z'); date.setUTCDate(date.getUTCDate() + 1); form.elements.start.value = date.toISOString().slice(0, 10); }
    }
    for (const [key, field] of Object.entries(entry?.parameter_schema || {})) {
      const label = document.createElement('label'); label.textContent = field.label;
      const input = document.createElement('input'); input.type = 'number'; input.dataset.parameter = key;
      input.min = field.min; input.max = field.max; input.step = field.type === 'integer' ? '1' : 'any';
      input.value = entry.parameters?.[key] ?? field.default; input.required = true; label.append(input); target.append(label);
    }
  }
  function updateMenu(resetDates = false) {
    if (!selection || !researchOptions || !strategyOptions) return;
    const training = action.value === 'train', entries = training ? strategyOptions.training : strategyOptions.strategies;
    const previous = selection.value; selection.replaceChildren();
    for (const [value, entry] of Object.entries(entries || {})) selection.add(new Option(entry.name, value));
    if (entries?.[previous]) selection.value = previous;
    else if (!training && strategyOptions.selected) selection.value = strategyOptions.selected;
    if (!selection.value) selection.selectedIndex = 0;
    submit.textContent = training ? '开始训练' : '运行回测';
    if (resetDates) {
      for (const key of ['start', 'end']) form.elements[key].value = researchOptions.ranges[`${training ? 'training' : 'backtest'}_${key}`];
      if (form.elements.timeframe.value === 'weekly') form.elements.start.value = new Date(Date.parse(form.elements.end.value) - (training ? 1000 : 400) * 86400000).toISOString().slice(0, 10);
      if (form.elements.timeframe.value === 'monthly') form.elements.start.value = new Date(Date.parse(form.elements.end.value) - (training ? 2200 : 900) * 86400000).toISOString().slice(0, 10);
    }
    parameters();
  }
  async function loadStrategies() {
    if (!selection) return;
    const request = ++optionsRequest, market = form.elements.market.value, timeframe = form.elements.timeframe.value;
    try {
      const [strategies, research] = await Promise.all([api(`/api/bar-strategies?market=${market}&timeframe=${timeframe}`), api('/api/research-options')]);
      if (request !== optionsRequest) return;
      strategyOptions = strategies; researchOptions = research;
      const manageStatus = document.querySelector('#modelManageStatus'); if (manageStatus) manageStatus.textContent = '';
      updateMenu(true); renderModels(); setBusy(busy);
    } catch (e) { if (request === optionsRequest) log.textContent = e.message; }
  }
  form.elements.market?.addEventListener('change', () => { form.elements.symbols.placeholder = form.elements.market.value === 'us' ? 'AAPL, MSFT, NVDA' : form.elements.market.value === 'hk' ? '00700, 09988' : '600519, 000858'; updatePool(); void loadStrategies(); document.dispatchEvent(new Event('finance-backtest-completed')); });
  form.elements.pool?.addEventListener('change', updatePool);
  form.elements.timeframe?.addEventListener('change', () => { void loadStrategies(); document.dispatchEvent(new Event('finance-backtest-completed')); });
  selection?.addEventListener('change', parameters); action?.addEventListener('change', () => updateMenu(true));
  async function track(id) {
    try {
      const { jobs } = await api('/api/jobs'), job = jobs.find(item => item.id === id);
      if (!job) { clearInterval(timer); setBusy(false); document.querySelector('#cancelTask').hidden = true; return; }
      const pending = job.kind === 'train' ? '正在准备训练数据…' : job.kind === 'backtest' ? '正在准备回测数据…' : 'DSH 正在处理，请稍候。';
      const message = job.status === 'running' ? (job.progress || `${pending} 你可以离开此页，任务会继续运行。`) : job.error || job.result?.text || '已完成。';
      if (log.textContent !== message) log.textContent = message;
      const cancel = document.querySelector('#cancelTask'); cancel.hidden = job.status !== 'running';
      cancel.onclick = async () => { try { await api(`/api/jobs/${id}/cancel`, { method: 'POST' }); } catch (e) { log.textContent = e.message; } };
      if (job.status !== 'running') {
        clearInterval(timer); setBusy(false);
        if (job.status === 'completed' && job.kind === 'backtest') document.dispatchEvent(new Event('finance-backtest-completed'));
        document.querySelector('#refreshButton').click();
        if (job.kind === 'train') { strategyOptions = await api(`/api/bar-strategies?market=${form.elements.market.value}&timeframe=${form.elements.timeframe.value}`); updateMenu(); renderModels(); }
      }
    } catch (e) { log.textContent = e.message; }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return; log.textContent = '正在创建任务…';
    const input = Object.fromEntries(new FormData(form)); input.kind = action?.value || kind; input.symbols = String(input.symbols || '').split(/[,，\s]+/).filter(Boolean);
    const fields = [...form.querySelectorAll('[data-parameter]')];
    if (fields.length) input.parameters = Object.fromEntries(fields.map(field => [field.dataset.parameter, Number(field.value)]));
    setBusy(true);
    try { const job = await api('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) }); clearInterval(timer); timer = setInterval(() => track(job.id), 2000); await track(job.id); }
    catch (e) { log.textContent = e.message; setBusy(false); }
  });
  if (tabs.length) setMode(new URLSearchParams(location.search).get('mode'));
  async function restoreTask() {
    await loadStrategies();
    try {
      const { jobs } = await api('/api/jobs'), job = jobs.find(item => (item.kind === kind || kind === 'backtest' && item.kind === 'train') && item.status === 'running');
      if (!job) return;
      if (tabs.length) setMode(job.kind);
      const configuration = job.configuration;
      if (configuration && selection) {
        if (form.elements.market) form.elements.market.value = configuration.market || "a";
        if (form.elements.timeframe) form.elements.timeframe.value = configuration.timeframe || 'daily';
        await loadStrategies();
        if (form.elements.symbols) form.elements.symbols.value = (configuration.symbols || []).join(", ");
        if (form.elements.pool) form.elements.pool.value = configuration.pool || 'custom';
        updatePool();
        selection.value = configuration.strategy; parameters();
        for (const key of ['start', 'end']) if (configuration[key]) form.elements[key].value = configuration[key];
        for (const field of form.querySelectorAll('[data-parameter]')) if (configuration.parameters?.[field.dataset.parameter] != null) field.value = configuration.parameters[field.dataset.parameter];
      }
      setBusy(true); timer = setInterval(() => track(job.id), 2000); await track(job.id);
    } catch (e) { log.textContent = e.message; }
  }
  void restoreTask();
})();
