/* Stock identities come from report records and explicitly labelled table columns. */
;(function (root) {
  let reportLabel = '';
  function identity(row, market = 'a') {
    market = row?.market || market;
    const symbol = String(row?.symbol ?? '').trim().toUpperCase();
    if (!['a', 'hk', 'us'].includes(market)) return null;
    const pattern = market === 'a' ? /^\d{6}$/ : market === 'hk' ? /^\d{1,5}$/ : /^[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9]{1,3})?$/;
    if (!pattern.test(symbol) || (market !== 'us' && Number(symbol) === 0)) return null;
    return {market, symbol: market === 'hk' ? symbol.padStart(5, '0') : market === 'us' ? symbol.replaceAll('.', '-') : symbol,
      name: String(row.name || '').trim()};
  }
  function button(row, market, text) {
    const stock = identity(row, market);
    const node = document.createElement(stock ? 'button' : 'span');
    node.textContent = text ?? (row.name || row.symbol || '—');
    if (!stock) return node;
    node.type = 'button'; node.className = 'report-stock-link';
    node.dataset.stockMarket = stock.market; node.dataset.stockSymbol = stock.symbol;
    node.setAttribute('aria-label', `查看 ${stock.name || stock.symbol} ${stock.symbol} 行情图表`);
    node.title = '点击查看K线、成交量与技术指标';
    node.addEventListener('click', () => root.PortfolioCharts.openStock(stock, node,
      `${reportLabel || '日报'} · 当前可用历史行情，数据截至日见图表；日报归档快照保持不变。`));
    return node;
  }
  function records(view, market) {
    return [...(view?.candidates?.ranking || []), ...Object.values(view?.horizon_recommendations?.horizons || {}).flat(),
      ...(view?.intraday_review?.comparisons || [])].map(row => identity(row, market)).filter(Boolean);
  }
  function tableIdentity(headers, cells, market) {
    const code = headers.findIndex(text => /^(?:股票代码|证券代码|代码|symbol|ticker)$/i.test(text.trim()));
    const name = headers.findIndex(text => /^(?:股票名称|证券名称|名称|name)$/i.test(text.trim()));
    if (code < 0) return null;
    return identity({symbol: cells[code]?.trim(), name: name >= 0 ? cells[name]?.trim() : ''}, market);
  }
  function mentions(text, stocks) {
    const aliases = new Map();
    for (const stock of stocks) {
      for (const alias of [stock.symbol, stock.market === 'us' ? stock.symbol.replaceAll('-', '.') : '', stock.name]) {
        if (alias && alias.length >= 2) aliases.set(alias, aliases.has(alias) && (aliases.get(alias)?.symbol !== stock.symbol || aliases.get(alias)?.market !== stock.market) ? null : stock);
      }
    }
    const matches = [];
    for (let index = 0; index < text.length;) {
      const found = [...aliases].filter(([alias, stock]) => stock && text.startsWith(alias, index)
        && (!/^[A-Z0-9.-]+$/.test(alias) || (!/[A-Za-z0-9.-]/.test(text[index - 1] || '') && !/[A-Za-z0-9.-]/.test(text[index + alias.length] || ''))))
        .sort((a, b) => b[0].length - a[0].length)[0];
      if (found) { matches.push({start: index, end: index + found[0].length, stock: found[1]}); index += found[0].length; }
      else index++;
    }
    return matches;
  }
  function decorateReport(target, view, market) {
    const stocks = records(view, market);
    for (const table of target.querySelectorAll('table')) {
      const headers = [...table.querySelectorAll('thead th')].map(cell => cell.textContent);
      for (const row of table.querySelectorAll('tbody tr')) {
        const stock = tableIdentity(headers, [...row.cells].map(cell => cell.textContent), market);
        if (stock) stocks.push(stock);
      }
    }
    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT), nodes = [];
    while (walker.nextNode()) if (!walker.currentNode.parentElement.closest('button,a,code,pre,th')) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const text = node.textContent, matches = mentions(text, stocks);
      if (!matches.length) continue;
      const fragment = document.createDocumentFragment(); let offset = 0;
      for (const match of matches) { fragment.append(text.slice(offset, match.start), button(match.stock, market, text.slice(match.start, match.end))); offset = match.end; }
      fragment.append(text.slice(offset)); node.replaceWith(fragment);
    }
  }
  const api = {identity, records, tableIdentity, mentions, button, decorateReport, setReport: payload => { reportLabel = payload.label || payload.id || ''; }};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ReportStocks = api;
})(typeof globalThis === 'undefined' ? this : globalThis);
