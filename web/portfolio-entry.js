(() => {
  const panel = document.querySelector('#portfolioEntry'), form = document.querySelector('#portfolioEntryForm'); if (!form) return;
  const status = document.querySelector('#portfolioEntryStatus'), results = document.querySelector('#portfolioEntryResults'), input = form.elements.text, cancel = document.querySelector('#portfolioEntryCancel');
  let timer, busy = false, generation = 0;
  const node = (tag, text, className = '') => { const element = document.createElement(tag); element.textContent = text; element.className = className; return element; };
  async function api(path, options) { const response = await fetch(path, options), payload = await response.json(); if (!response.ok) throw Error(payload.error || '请求失败'); return payload; }
  function setBusy(value, task = true) {
    busy = value; input.disabled = value;
    for (const control of [...form.querySelectorAll('[type=submit]'), ...results.querySelectorAll('button,input')]) control.disabled = value;
    cancel.hidden = !value || !task;
  }
  const holdingKey = row => `${row.market || 'a'}/${row.symbol}`;
  function focusEdit(key) { results.querySelector(`[data-holding="${key}"]`)?.focus(); }
  async function mutate(row, method, shares) {
    if (busy) return;
    ++generation; setBusy(true, false);
    status.textContent = method === 'DELETE' ? '正在移除持仓…' : '正在保存股数…';
    try {
      const data = await api(`/api/portfolio/${holdingKey(row)}`, {method, ...(method === 'PATCH' ? {headers: {'Content-Type': 'application/json'}, body: JSON.stringify({shares})} : {})});
      setBusy(false); render(data.holdings || []);
      const outcome = method === 'DELETE' ? '已移除。' : `已更新为 ${shares.toLocaleString('zh-CN')} 股。`;
      status.textContent = `${row.market === 'hk' ? '港股' : 'A股'} ${row.symbol} ${outcome}其他持仓、成本和备注已保留。`;
      if (method === 'PATCH') focusEdit(holdingKey(row)); else results.querySelector('button')?.focus();
      document.dispatchEvent(new Event('portfolio-saved'));
    } catch (error) { setBusy(false); status.textContent = error.message; }
  }
  function render(holdings, preview = false) {
    ++generation;
    const heading = node('div', '', 'portfolio-list-heading'), refresh = node('button', preview ? '查看已保存持仓' : '刷新列表'); refresh.type = 'button';
    refresh.onclick = () => { if (!busy) void load(); };
    heading.append(node('h3', preview ? '识别预览 · 尚未保存' : `本地持仓 · ${holdings.length} 条`), refresh); results.replaceChildren(heading);
    if (!holdings.length) { results.append(node('p', '还没有持仓，写下代码和股数即可开始。', 'ranking-note')); return; }
    const list = node('ul', '', 'portfolio-saved-list');
    for (const row of holdings) {
      const item = node('li', ''), identity = node('div', '');
      identity.append(node('strong', row.name || '名称待补全'), node('small', `${row.market === 'hk' ? '港股' : 'A股'} · ${row.symbol}`));
      item.append(identity, node('span', row.shares != null && Number.isFinite(Number(row.shares)) ? `${Number(row.shares).toLocaleString('zh-CN')} 股` : '股数未填写'));
      if (!preview) {
        const actions = node('div', '', 'portfolio-row-actions'), edit = node('button', '编辑股数'), remove = node('button', '删除', 'portfolio-remove');
        const label = `${row.market === 'hk' ? '港股' : 'A股'} ${row.symbol}`;
        edit.type = remove.type = 'button'; edit.dataset.holding = holdingKey(row); edit.setAttribute('aria-label', `编辑 ${label} 的股数`); remove.setAttribute('aria-label', `删除 ${label} 持仓`);
        edit.onclick = () => {
          if (busy) return;
          const editor = node('form', '', 'portfolio-row-editor'), field = document.createElement('input'), fieldLabel = node('label', `${label} · 持股数量（股）`);
          field.type = 'number'; field.name = 'shares'; field.min = '1'; field.max = '1000000000000'; field.step = '1'; field.required = true; field.inputMode = 'numeric'; field.id = `shares-${row.market || 'a'}-${row.symbol}`;
          field.value = row.shares == null ? '' : String(row.shares); fieldLabel.htmlFor = field.id;
          const save = node('button', '保存股数'), discard = node('button', '取消'); save.type = 'submit'; discard.type = 'button';
          const close = () => { editor.replaceWith(actions); edit.focus(); };
          discard.onclick = close; editor.addEventListener('keydown', event => { if (event.key === 'Escape' && !busy) { event.preventDefault(); close(); } });
          editor.onsubmit = event => { event.preventDefault(); if (editor.reportValidity()) void mutate(row, 'PATCH', Number(field.value)); };
          editor.append(fieldLabel, field, save, discard); actions.replaceWith(editor); field.focus(); field.select();
        };
        remove.onclick = () => void mutate(row, 'DELETE'); actions.append(edit, remove); item.append(actions);
      }
      list.append(item);
    }
    results.append(list);
  }
  async function load() { const current = ++generation; try { const data = await api('/api/portfolio'); if (!busy && current === generation) { render(data.holdings || []); status.textContent = ''; } } catch (error) { if (current === generation) status.textContent = error.message; } }
  panel.addEventListener('toggle', () => { if (panel.open && !busy) void load(); });
  document.querySelector('#portfolioEntryOpen').addEventListener('click', () => { panel.open = true; panel.scrollIntoView({behavior: 'smooth', block: 'start'}); input.focus({preventScroll: true}); });
  if (location.hash === '#portfolioEntry') panel.open = true;
  async function track(id) {
    try {
      const {jobs} = await api('/api/jobs'), job = jobs.find(item => item.id === id);
      if (!job) { clearInterval(timer); setBusy(false); status.textContent = '未找到持仓任务，请重试。'; return; }
      const message = job.status === 'running' ? job.progress || 'DSH 正在处理…' : job.error || job.result?.text || '已完成';
      if (status.textContent !== message) status.textContent = message;
      if (job.status === 'running') return;
      clearInterval(timer); setBusy(false);
      if (job.status === 'completed') {
        render(job.result.holdings || [], job.result.preview);
        if (!job.result.preview) { input.value = ''; document.dispatchEvent(new Event('portfolio-saved')); }
      }
    } catch (error) { status.textContent = error.message; }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    const text = input.value.trim(); if (!text) return;
    const preview = event.submitter?.value === 'preview'; setBusy(true); status.textContent = '正在创建持仓识别任务…';
    try {
      const job = await api('/api/jobs', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({kind: 'portfolio', text, preview})});
      cancel.onclick = async () => { try { await api(`/api/jobs/${job.id}/cancel`, {method: 'POST'}); } catch (error) { status.textContent = error.message; } };
      clearInterval(timer); timer = setInterval(() => track(job.id), 2000); await track(job.id);
    } catch (error) { setBusy(false); status.textContent = error.message; }
  });
  void api('/api/jobs').then(({jobs}) => {
    const job = jobs.find(item => item.kind === 'portfolio' && item.status === 'running'); if (!job) return;
    setBusy(true); panel.open = true; cancel.onclick = () => api(`/api/jobs/${job.id}/cancel`, {method: 'POST'}).catch(error => {status.textContent = error.message;});
    timer = setInterval(() => track(job.id), 2000); void track(job.id);
  }).catch(() => {});
})();
