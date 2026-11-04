(() => {
  const panel = document.querySelector('#portfolioEntry'), form = document.querySelector('#portfolioEntryForm'); if (!form) return;
  const status = document.querySelector('#portfolioEntryStatus'), results = document.querySelector('#portfolioEntryResults'), input = form.elements.text, cancel = document.querySelector('#portfolioEntryCancel');
  const imageInput = document.querySelector('#portfolioEntryImage'), imagePreview = document.querySelector('#portfolioImagePreview'), imageThumbnail = document.querySelector('#portfolioImageThumbnail'), imageName = document.querySelector('#portfolioImageName'), imageRemove = document.querySelector('#portfolioImageRemove'), imageRecognize = document.querySelector('#portfolioImageRecognize'), imageDrop = document.querySelector('#portfolioImageDrop');
  let selectedImage, imageUrl;
  let timer, busy = false, generation = 0;
  const node = (tag, text, className = '') => { const element = document.createElement(tag); element.textContent = text; element.className = className; return element; };
  async function api(path, options) { const response = await fetch(path, options), payload = await response.json(); if (!response.ok) throw Error(payload.error || '请求失败'); return payload; }
  function setBusy(value, task = true) {
    busy = value; input.disabled = value;
    for (const control of [...form.querySelectorAll('[type=submit]'), ...results.querySelectorAll('button,input')]) control.disabled = value;
    imageInput.disabled = imageRemove.disabled = value;
    imageRecognize.disabled = value || !selectedImage;
    cancel.hidden = !value || !task;
  }
  function clearImage() {
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = undefined; selectedImage = undefined; imageInput.value = '';
    imageThumbnail.removeAttribute('src'); imagePreview.hidden = true; imageRecognize.disabled = true;
  }
  function selectImage(file) {
    if (busy || !file) return;
    if (!['image/png','image/jpeg','image/webp'].includes(file.type) || !file.size || file.size > 8 * 1024 * 1024) {
      imageInput.value = ''; status.textContent = '请选择不超过 8 MB 的 PNG、JPEG 或 WebP 图片。'; return;
    }
    clearImage(); selectedImage = file;
    const selection = new DataTransfer(); selection.items.add(file); imageInput.files = selection.files;
    imageUrl = URL.createObjectURL(file);
    imageThumbnail.src = imageUrl; imageName.textContent = `${file.name || '粘贴的截图'} · ${(file.size / 1024).toFixed(0)} KB`;
    imagePreview.hidden = false; imageRecognize.disabled = false;
    status.textContent = '图片已选好，点击“识别图片并填入文字”。识别结果将替换文本框内容，请先保留需要的文字。';
  }
  imageInput.addEventListener('change', () => selectImage(imageInput.files[0]));
  imageRemove.addEventListener('click', () => { if (!busy) { clearImage(); status.textContent = '已移除图片，文字内容已保留。'; } });
  imageDrop.addEventListener('dragover', event => { event.preventDefault(); if (!busy) imageDrop.classList.add('is-dragging'); });
  imageDrop.addEventListener('dragleave', () => imageDrop.classList.remove('is-dragging'));
  imageDrop.addEventListener('drop', event => { event.preventDefault(); imageDrop.classList.remove('is-dragging'); selectImage(event.dataTransfer.files[0]); });
  panel.addEventListener('paste', event => {
    const file = [...(event.clipboardData?.items || [])].find(item => item.kind === 'file' && item.type.startsWith('image/'))?.getAsFile();
    if (file && !busy) { event.preventDefault(); selectImage(file); }
  });
  function imageData(file) {
    return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(Error('图片读取失败，请重新选择。')); reader.readAsDataURL(file); });
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
      status.textContent = `${row.market === 'us' ? '美股' : row.market === 'hk' ? '港股' : 'A股'} ${row.symbol} ${outcome}其他持仓、成本和备注已保留。`;
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
      identity.append(node('strong', row.name || '名称待补全'), node('small', `${row.market === 'us' ? '美股' : row.market === 'hk' ? '港股' : 'A股'} · ${row.symbol}`));
      item.append(identity, node('span', row.shares != null && Number.isFinite(Number(row.shares)) ? `${Number(row.shares).toLocaleString('zh-CN')} 股` : '股数未填写'));
      if (!preview) {
        const actions = node('div', '', 'portfolio-row-actions'), edit = node('button', '编辑股数'), remove = node('button', '删除', 'portfolio-remove');
        const label = `${row.market === 'us' ? '美股' : row.market === 'hk' ? '港股' : 'A股'} ${row.symbol}`;
        edit.type = remove.type = 'button'; edit.dataset.holding = holdingKey(row); edit.setAttribute('aria-label', `编辑 ${label} 的股数`); remove.setAttribute('aria-label', `删除 ${label} 持仓`);
        edit.onclick = () => {
          if (busy) return;
          const editor = node('form', '', 'portfolio-row-editor'), field = document.createElement('input'), fieldLabel = node('label', `${label} · 持股数量（股）`);
          field.type = 'number'; field.name = 'shares'; field.min = row.market === 'us' ? '0.000001' : '1'; field.max = '1000000000000'; field.step = row.market === 'us' ? 'any' : '1'; field.required = true; field.inputMode = 'numeric'; field.id = `shares-${row.market || 'a'}-${row.symbol}`;
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
  document.addEventListener('portfolio-refresh', () => { if (panel.open && !busy) void load(); });
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
        if (typeof job.result.recognizedText === 'string') {
          input.value = job.result.recognizedText;
          results.replaceChildren(node('h3', '图片识别完成 · 尚未保存'), node('p', '请在上方文本框核对内容，每条补齐当前持有股数后，点击“DSH 识别并保存”。', 'ranking-note'));
          if (job.result.issues?.length) results.append(node('p', job.result.issues.join('；'), 'portfolio-entry-status'));
          input.focus({preventScroll:true}); return;
        }
        render(job.result.holdings || [], job.result.preview);
        if (!job.result.preview) { input.value = ''; document.dispatchEvent(new Event('portfolio-saved')); }
      }
    } catch (error) { status.textContent = error.message; }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    const text = input.value.trim(), fromImage = event.submitter?.value === 'image';
    if (fromImage ? !selectedImage : !text) { status.textContent = fromImage ? '请先选择图片。' : '请填写股票代码和股数，或先识别一张截图。'; input.focus(); return; }
    const preview = event.submitter?.value === 'preview'; setBusy(true); status.textContent = fromImage ? '正在上传图片并创建识别任务…' : '正在创建持仓识别任务…';
    try {
      const payload = fromImage ? {kind: 'portfolio', image: await imageData(selectedImage), preview: true} : {kind: 'portfolio', text, preview};
      const job = await api('/api/jobs', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
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
