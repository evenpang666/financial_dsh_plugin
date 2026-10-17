(() => {
  const panel = document.querySelector('#portfolioEntry'), form = document.querySelector('#portfolioEntryForm'); if (!form) return;
  const status = document.querySelector('#portfolioEntryStatus'), results = document.querySelector('#portfolioEntryResults'), input = form.elements.text, cancel = document.querySelector('#portfolioEntryCancel');
  let timer, busy = false;
  const node = (tag, text, className = '') => { const element = document.createElement(tag); element.textContent = text; element.className = className; return element; };
  async function api(path, options) { const response = await fetch(path, options), payload = await response.json(); if (!response.ok) throw Error(payload.error || '请求失败'); return payload; }
  function setBusy(value) { busy = value; input.disabled = value; for (const button of form.querySelectorAll('[type=submit]')) button.disabled = value; cancel.hidden = !value; }
  function render(holdings, preview = false) {
    results.replaceChildren(node('h3', preview ? '识别预览 · 尚未保存' : `本地持仓 · ${holdings.length} 条`));
    if (!holdings.length) { results.append(node('p', '还没有持仓，写下代码和股数即可开始。', 'ranking-note')); return; }
    const list = node('ul', '', 'portfolio-saved-list');
    for (const row of holdings) {
      const item = node('li', ''), identity = node('div', '');
      identity.append(node('strong', row.name || '名称待补全'), node('small', `${row.market === 'hk' ? '港股' : 'A股'} · ${row.symbol}`));
      item.append(identity, node('span', row.shares != null && Number.isFinite(Number(row.shares)) ? `${Number(row.shares).toLocaleString('zh-CN')} 股` : '股数未填写')); list.append(item);
    }
    results.append(list);
  }
  async function load() { try { const data = await api('/api/portfolio'); if (!busy) render(data.holdings || []); } catch (error) { status.textContent = error.message; } }
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
