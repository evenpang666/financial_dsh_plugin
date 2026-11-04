(() => {
  const indicator = document.querySelector('.workspace-connection');
  if (!indicator) return;
  let checking = false;
  async function refreshConnection() {
    if (checking || document.hidden) return;
    checking = true;
    try {
      const response = await fetch('/api/status', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      const status = await response.json();
      if (status.managedBy !== 'dsh') throw Error('Unexpected service');
      indicator.dataset.state = 'connected';
      indicator.textContent = '已连接';
    } catch {
      indicator.dataset.state = 'disconnected';
      indicator.textContent = '连接中断';
    } finally { checking = false; }
  }
  void refreshConnection();
  setInterval(refreshConnection, 30_000);
  document.addEventListener('visibilitychange', refreshConnection);
  window.addEventListener('online', refreshConnection);
})();
