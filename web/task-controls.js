(() => {
  const form = document.querySelector('#taskForm'); if (!form) return;
  const kind = form.dataset.kind, log = document.querySelector('#taskLog'), submit = form.querySelector('[type=submit]');
  const selection = form.querySelector('[name=strategy]'), action = form.querySelector('[name=action]');
  let timer, busy = false, strategyOptions, researchOptions;
  const tabs = [...document.querySelectorAll('[data-mode]')];
  function setMode(mode, resetDates = true) {
    if (!action) return;
    action.value = mode === 'train' ? 'train' : 'backtest';
    const training = action.value === 'train';
    for (const tab of tabs) { const active = tab.dataset.mode === action.value; tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1; }
    const panel = document.querySelector('#workbenchPanel');
    if (panel) {
      panel.setAttribute('aria-labelledby', training ? 'trainTab' : 'backtestTab');
      document.querySelector('#runTitle').textContent = training ? '让因子，成为你的模型。' : '验证你的策略';
      document.querySelector('#runDescription').textContent = training ? '选择 XGBoost 或 Elastic Net，设定历史区间与训练参数。训练进度会保存在本地，完成后即可选择新模型回测。' : '选择经典策略或已训练模型，查看净值、回撤与交易明细。缺少历史数据时自动下载并验收。';
      document.querySelector('#strategyLabel').textContent = training ? '训练模型' : '策略 / 已训练模型';
      document.querySelector('#startLabel').textContent = training ? '训练开始' : '回测开始';
      document.querySelector('#endLabel').textContent = training ? '训练结束' : '回测结束';
      document.querySelector('#slotLabel').textContent = training ? '模型默认时点' : '信号时点';
      document.querySelector('#parameterTitle').textContent = training ? '训练超参数' : '策略超参数';
      form.querySelector('details').open = training;
      document.querySelector('#trainedModels').hidden = !training;
      const url = new URL(location.href); if (training) url.searchParams.set('mode', 'train'); else url.searchParams.delete('mode'); history.replaceState(null, '', url);
    }
    updateMenu(resetDates);
  }
  function setBusy(value) {
    busy = value; submit.disabled = value || Boolean(selection && !strategyOptions);
    for (const field of form.querySelectorAll('input,select,details input')) field.disabled = value;
    for (const tab of tabs) tab.disabled = value;
    for (const button of document.querySelectorAll('#modelList button')) button.disabled = value;
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
      detail.textContent = `${range.requested_start || range.first_day || '起始日期未归档'} → ${range.requested_end || range.last_day || model.training_last_day || '结束日期未归档'}`;
      const version = document.createElement('small'); version.textContent = model.checkpoint_id;
      button.type = 'button'; button.textContent = '使用此模型回测'; button.disabled = busy;
      button.addEventListener('click', () => {
        setMode('backtest'); selection.value = identity;
        for (const key of ['start', 'end']) form.elements[key].value = researchOptions.ranges[`backtest_${key}`];
        parameters(); document.querySelector('#strategyWorkbench').scrollIntoView({behavior: 'smooth', block: 'start'}); selection.focus({preventScroll: true});
      });
      card.append(title, detail, version, button); target.append(card);
    }
  }
  async function api(path, options) { const response = await fetch(path, options), data = await response.json(); if (!response.ok) throw Error(data.error || '请求失败。'); return data; }
  function parameters() {
    const target = document.querySelector('#taskParameters'); if (!target) return;
    target.replaceChildren();
    const entry = (action.value === 'train' ? researchOptions?.training : strategyOptions?.strategies)?.[selection.value];
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
    const training = action.value === 'train', entries = training ? researchOptions.training : strategyOptions.strategies;
    const previous = selection.value; selection.replaceChildren();
    for (const [value, entry] of Object.entries(entries || {})) selection.add(new Option(entry.name, value));
    if (entries?.[previous]) selection.value = previous;
    else if (!training && strategyOptions.selected) selection.value = strategyOptions.selected;
    if (!selection.value) selection.selectedIndex = 0;
    submit.textContent = training ? '开始训练' : '运行回测';
    if (resetDates) for (const key of ['start', 'end']) form.elements[key].value = researchOptions.ranges[`${training ? 'training' : 'backtest'}_${key}`];
    parameters();
  }
  async function loadStrategies() {
    if (!selection) return;
    try { [strategyOptions, researchOptions] = await Promise.all([api('/api/portfolio-strategies'), api('/api/research-options')]); updateMenu(true); renderModels(); setBusy(busy); }
    catch (e) { log.textContent = e.message; }
  }
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
        if (job.kind === 'train') { strategyOptions = await api('/api/portfolio-strategies'); updateMenu(); renderModels(); }
      }
    } catch (e) { log.textContent = e.message; }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return; log.textContent = '正在创建任务…';
    const input = Object.fromEntries(new FormData(form)); input.kind = action?.value || kind;
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
        selection.value = configuration.strategy; parameters();
        for (const key of ['start', 'end', 'slot']) if (configuration[key]) form.elements[key].value = configuration[key];
        for (const field of form.querySelectorAll('[data-parameter]')) if (configuration.parameters?.[field.dataset.parameter] != null) field.value = configuration.parameters[field.dataset.parameter];
      }
      setBusy(true); timer = setInterval(() => track(job.id), 2000); await track(job.id);
    } catch (e) { log.textContent = e.message; }
  }
  void restoreTask();
})();
