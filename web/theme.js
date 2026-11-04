(() => {
  const key = 'zhiheng-theme';
  function apply(value) {
    document.documentElement.dataset.theme = value === 'light' ? 'light' : 'dark';
  }
  try { apply(localStorage.getItem(key)); } catch { apply('dark'); }
  window.financeTheme = {
    get: () => document.documentElement.dataset.theme,
    set(value) {
      if (!['dark', 'light'].includes(value)) throw Error('请选择深色或浅色主题。');
      localStorage.setItem(key, value);
      apply(value);
    },
  };
  window.addEventListener('storage', event => { if (event.key === key || event.key === null) apply(event.newValue); });
})();
