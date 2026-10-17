(() => {
  const target = document.querySelector('#lanAccess'); if (!target) return;
  const element = (tag, text, className = '') => { const node = document.createElement(tag); node.textContent = text; node.className = className; return node; };
  async function copy(text) {
    try { if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return; } } catch {}
    const input = element('textarea', text, 'copy-buffer'); input.readOnly = true; document.body.append(input); input.select();
    const copied = document.execCommand('copy'); input.remove(); if (!copied) throw Error('请选中地址复制');
  }
  async function refresh() {
    try {
      const response = await fetch('/api/network', {cache: 'no-store'}); if (!response.ok) throw Error();
      const data = await response.json();
      if (!data.lanUrl) { target.replaceChildren(element('span', '局域网访问', 'lan-label'), element('span', '未检测到已连接的物理以太网；连接网线后重启工作台。', 'lan-note')); return; }
      const link = element('a', data.lanUrl); link.href = data.lanUrl;
      const button = element('button', '复制地址'); button.type = 'button'; button.setAttribute('aria-label', '复制局域网访问地址');
      button.addEventListener('click', async () => { try { await copy(data.lanUrl); button.textContent = '已复制'; } catch (error) { button.textContent = error.message; } });
      target.replaceChildren(element('span', '局域网访问', 'lan-label'), link, element('span', `同一局域网设备可访问 · ${data.adapter}`, 'lan-note'), button);
    } catch { target.replaceChildren(element('span', '局域网地址暂不可用，请刷新页面重试。', 'lan-note')); }
  }
  void refresh();
})();
