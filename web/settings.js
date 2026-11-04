(() => {
  const form = document.querySelector('#agentSettingsForm');
  const model = document.querySelector('#agentModel'), effort = document.querySelector('#reasoningEffort');
  const save = document.querySelector('#saveAgentSettings'), reload = document.querySelector('#reloadSettings');
  const status = document.querySelector('#agentSettingsStatus');
  const key = row => JSON.stringify([row.provider, row.id]);
  function busy(value) { model.disabled = effort.disabled = save.disabled = reload.disabled = value; }
  async function api(options, query = '') {
    try {
      const response = await fetch('/api/settings' + query, {...options, cache: 'no-store', signal: AbortSignal.timeout(15000)}), data = await response.json();
      if (!response.ok) throw Error(data.error || '设置保存失败，请稍后重试。');
      return data;
    } catch (error) {
      if (error.name === 'TimeoutError') throw Error('请求超时，请检查 DSH 服务后点击“重新读取”。');
      throw error;
    }
  }
  function setEfforts(data, selected = '') {
    effort.replaceChildren(new Option('跟随模型默认', ''), ...data.reasoningEfforts.map(row => new Option(row.label, row.id)));
    effort.value = [...effort.options].some(row => row.value === selected) ? selected : '';
    effort.disabled = !model.value || data.reasoningEfforts.length === 0;
  }
  async function load() {
    busy(true); status.textContent = '正在读取 DSH 模型目录…';
    try {
      const data = await api();
      model.replaceChildren(new Option(`跟随 DSH 宿主（${data.hostModel || '未配置'}）`, ''), ...data.models.map(row => new Option(row.label, key(row))));
      model.value = data.inheritHost ? '' : key({provider: data.provider, id: data.model});
      busy(false); setEfforts(data, data.reasoningEffort);
      status.textContent = '当前模型设置已加载。凭据由 DSH 管理。';
    } catch (error) { status.textContent = `读取失败：${error.message}`; reload.disabled = false; }
  }
  model.addEventListener('change', async () => {
    if (!model.value) { setEfforts({reasoningEfforts: []}); return; }
    const [provider, selected] = JSON.parse(model.value);
    busy(true);
    try { const data = await api(undefined, '?' + new URLSearchParams({provider, model: selected})); busy(false); setEfforts(data); status.textContent = '请选择推理强度并保存。'; }
    catch (error) { status.textContent = `读取失败：${error.message}`; model.disabled = reload.disabled = false; }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    let payload = {inheritHost: true};
    if (model.value) { const [provider, selected] = JSON.parse(model.value); payload = {provider, model: selected, ...(effort.value ? {reasoningEffort: effort.value} : {})}; }
    busy(true); status.textContent = '正在保存…';
    try { await api({method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)}); await load(); status.textContent = '模型设置已保存，将用于后续 DSH 任务。'; }
    catch (error) { status.textContent = `保存失败：${error.message}`; busy(false); }
  });
  reload.addEventListener('click', load);
  const themes = document.querySelectorAll('input[name="theme"]');
  function syncTheme() { themes.forEach(input => { input.checked = input.value === window.financeTheme.get(); }); }
  themes.forEach(input => input.addEventListener('change', () => {
    try { window.financeTheme.set(input.value); document.querySelector('#themeSettingsStatus').textContent = `已切换为${input.value === 'dark' ? '深色' : '浅色'}主题。`; }
    catch { document.querySelector('#themeSettingsStatus').textContent = '浏览器无法保存主题，请允许本地存储后重试。'; }
    syncTheme();
  }));
  window.addEventListener('storage', syncTheme);
  syncTheme(); void load();
})();
