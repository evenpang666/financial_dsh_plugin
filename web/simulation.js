const $ = selector => document.querySelector(selector);
const form = $('#simulationForm'), market = form.elements.market, strategy = form.elements.strategy;
const number = value => Number(value).toLocaleString('zh-CN', {maximumFractionDigits: 2});
const pct = value => `${(value * 100).toFixed(2)}%`;
const stamp = value => value ? new Date(value).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}) : '—';
const node = (tag, text, className = '') => { const el = document.createElement(tag); el.textContent = text; el.className = className; return el; };
const currency = value => ({a: 'CNY', hk: 'HKD', us: 'USD'}[value]);
let sessions = [], holdings = [], selected = '', loading = false, busy = false, optionsRequest = 0, optionsReady = false, sessionsReady = false, switchFor = '', switchLoadingFor = '', switchRequest = 0, resumeFor = '';
const aiJobs = new Map(); let aiCardKey = '';
async function api(path, options = {}) { const response = await fetch(path, {cache: 'no-store', ...options}), result = await response.json(); if (!response.ok) throw Error(result.error || '服务暂不可用'); return result; }
function controls() {
  const running = sessions.some(row => row.status === 'running');
  const selectedRow = sessions.find(row => row.id === selected);
  for (const field of form.querySelectorAll('input,select')) field.disabled = busy || running;
  strategy.disabled = busy || running || !optionsReady;
  $('#startSimulation').disabled = busy || running || !sessionsReady || !strategy.value || !holdings.some(row => (row.market || 'a') === market.value);
  $('#startSimulation').textContent = strategy.value === '__ai__' ? '分析并开始 AI 模拟' : '开始模拟';
  $('#analyzeSimulation').disabled = busy || running || !holdings.some(row => (row.market || 'a') === market.value);
  $('#pauseSimulation').hidden = selectedRow?.status !== 'running';
  $('#pauseSimulation').disabled = busy;
  $('#resumeSimulation').hidden = selectedRow?.status !== 'paused';
  $('#resumeSimulation').disabled = busy || running || !$('#resumeAddedCash').validity.valid;
  $('#resumeAddedCash').disabled = busy || running || selectedRow?.status !== 'paused';
  $('#resumeAllowNewStocks').disabled = busy || running || selectedRow?.status !== 'paused';
  $('#stopSimulation').disabled = busy || !selectedRow;
  $('#switchStrategy').disabled = busy || selectedRow?.status !== 'paused' || switchFor !== selectedRow?.id;
  $('#saveSimulationStrategy').disabled = $('#switchStrategy').disabled || !$('#switchStrategy').value || $('#switchStrategy').value === selectedRow?.strategy;
}
async function loadSwitchOptions(row) {
  const request = ++switchRequest;
  switchFor = ''; switchLoadingFor = row.id; $('#switchStrategy').replaceChildren(node('option', '正在读取策略…')); controls();
  try {
    const result = await api('/api/bar-strategies?' + new URLSearchParams({market: row.market, timeframe: 'daily'}));
    if (request !== switchRequest || selected !== row.id) return;
    const choices = Object.entries(result.strategies).filter(([, item]) => !item.checkpoint_id || item.validation?.passed === true);
    const recommended = aiJobs.get(row.market)?.result?.recommendation?.strategy;
    $('#switchStrategy').replaceChildren(...choices.map(([key, item]) => { const option = node('option', key === recommended ? `${item.name} · 当前最优（DSH）` : item.name); option.value = key; return option; }));
    if ([...$('#switchStrategy').options].some(option => option.value === row.strategy)) $('#switchStrategy').value = row.strategy;
    switchFor = row.id;
  } catch (error) { if (request === switchRequest) { $('#switchStrategy').replaceChildren(node('option', '策略读取失败')); $('#switchStrategy').firstChild.value = ''; $('#simulationNotice').textContent = error.message; } }
  finally { if (request === switchRequest) switchLoadingFor = ''; controls(); }
}
async function loadOptions() {
  const request = ++optionsRequest, prior = strategy.value, chosenMarket = market.value; optionsReady = false; controls();
  try {
    const result = await api('/api/bar-strategies?' + new URLSearchParams({market: market.value, timeframe: 'daily'}));
    if (request !== optionsRequest) return;
    const choices = Object.entries(result.strategies).filter(([, row]) => !row.checkpoint_id || row.validation?.passed === true);
    const recommended = aiJobs.get(chosenMarket)?.result?.recommendation?.strategy;
    const aiOption = node('option', 'AI 模拟 · DSH 分析持仓并选择策略'); aiOption.value = '__ai__';
    strategy.replaceChildren(aiOption, ...choices.map(([key, row]) => { const option = node('option', key === recommended ? `${row.name} · 当前最优（DSH）` : row.name); option.value = key; return option; }));
    strategy.value = [...strategy.options].some(option => option.value === prior) ? prior : choices[0]?.[0] || '__ai__';
    optionsReady = true;
  } catch (error) { if (request === optionsRequest) { strategy.replaceChildren(node('option', '策略读取失败')); strategy.firstChild.value = ''; $('#simulationNotice').textContent = error.message; } }
  finally { controls(); }
}
function initialSummary() {
  const rows = holdings.filter(row => (row.market || 'a') === market.value);
  $('#startCashLabel').textContent = `本次新增资金（${currency(market.value)}）`;
  $('#initialHoldings').textContent = rows.length ? `默认初始持仓 · ${rows.map(row => `${row.name || row.symbol}（${row.symbol}）${number(row.shares)} 股`).join('、')}` : '当前市场暂无持仓，请先前往“持仓”页保存。'; controls();
}
function showAiAnalysis(recommendation, status = '') {
  const card = $('#aiRecommendationCard'); card.hidden = false;
  $('#aiAnalysisStatus').textContent = status || (recommendation ? `分析时间 ${stamp(recommendation.generatedAt)} · ${recommendation.stocks.length} 只股票` : 'DSH 正在分析…');
  $('#aiAnalysisResult').hidden = !recommendation;
  if (!recommendation) return;
  const key = `${recommendation.generatedAt}:${recommendation.strategy}`;
  if (aiCardKey === key) return; aiCardKey = key;
  $('#aiStrategyName').textContent = `当前最优：${recommendation.strategyName}`;
  $('#aiSummary').textContent = recommendation.summary;
  $('#aiReason').textContent = `选择依据：${recommendation.reason}`;
  $('#aiLimits').textContent = `数据与结论局限：${recommendation.limits}`;
  $('#aiStockAnalysis').replaceChildren(...recommendation.stocks.map(row => {
    const item = node('article', '', 'ai-stock-item');
    item.append(node('h4', `${row.name}（${row.symbol}）${row.historyAsOf ? ` · 日线截至 ${row.historyAsOf}` : ''}`),
      node('p', row.observation), node('p', `适配判断：${row.fit}`), node('p', `限制：${row.risk}`)); return item;
  }));
}
function selectedRecommendation() {
  const session = sessions.find(row => row.id === selected);
  return aiJobs.get(market.value)?.result?.recommendation || session?.aiRecommendation || null;
}
async function runAiAnalysis() {
  const chosenMarket = market.value;
  aiCardKey = ''; showAiAnalysis(null, '正在读取每只持仓股票的数据…');
  const created = await api('/api/jobs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({kind:'ai_simulation', market:chosenMarket})});
  for (;;) {
    const current = (await api('/api/jobs')).jobs.find(row => row.id === created.id);
    if (!current) throw Error('AI 分析任务记录不存在。');
    if (current.status === 'failed') throw Error(current.error || 'AI 分析失败。');
    if (current.status === 'completed') {
      aiJobs.set(chosenMarket, current); showAiAnalysis(current.result.recommendation);
      await loadOptions(); strategy.value = current.result.recommendation.strategy; controls(); return current;
    }
    showAiAnalysis(null, current.progress || 'DSH 正在分析…');
    await new Promise(resolve => setTimeout(resolve, 1200));
  }
}
function table(target, rows, empty, columns = 7) {
  const body = $(target); body.replaceChildren();
  if (!rows.length) { const tr = node('tr', ''), cell = node('td', empty); cell.colSpan = columns; tr.append(cell); body.append(tr); return; }
  for (const row of rows) { const tr = node('tr', ''); for (const value of row) tr.append(node('td', value)); body.append(tr); }
}
function curve(points) {
  const target = $('#simulationCurve'); target.replaceChildren();
  if (points.length < 2) { target.append(node('p', '已记录起始净值，等待下一次收益更新。', 'empty-note')); return; }
  const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 900 210'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', `累计收益率从 ${pct(points[0].totalReturn)} 至 ${pct(points.at(-1).totalReturn)}`);
  const low = Math.min(0, ...points.map(row => row.totalReturn)) - .001, high = Math.max(0, ...points.map(row => row.totalReturn)) + .001;
  const start = Date.parse(points[0].at), end = Date.parse(points.at(-1).at);
  const x = row => 76 + (Date.parse(row.at) - start) / Math.max(1, end - start) * 798, y = value => 20 + (high - value) / (high - low) * 150;
  const add = (tag, attributes, text = '') => { const el = document.createElementNS(ns, tag); for (const [key, value] of Object.entries(attributes)) el.setAttribute(key, value); el.textContent = text; svg.append(el); return el; };
  for (const value of [low, 0, high]) { add('line', {x1: 76, x2: 874, y1: y(value), y2: y(value), class: 'simulation-grid'}); add('text', {x: 66, y: y(value) + 4, 'text-anchor': 'end'}, pct(value)); }
  add('polyline', {points: points.map(row => `${x(row)},${y(row.totalReturn)}`).join(' '), fill: 'none', class: 'simulation-line'});
  add('text', {x: 76, y: 200}, stamp(points[0].at)); add('text', {x: 874, y: 200, 'text-anchor': 'end'}, stamp(points.at(-1).at)); target.append(svg);
}
function render() {
  const active = sessions.find(row => row.status === 'running');
  if (active && market.value !== active.market) { market.value = active.market; initialSummary(); void loadOptions(); }
  if (active && [...strategy.options].some(option => option.value === active.strategy)) strategy.value = active.strategy;
  const select = $('#simulationSelect');
  if (!sessions.some(row => row.id === selected)) selected = sessions[0]?.id || '';
  select.replaceChildren(...sessions.map(row => { const option = node('option', `${row.status === 'running' ? '运行中' : row.status === 'paused' ? '已暂停' : '旧记录'} · ${row.strategyName} · ${stamp(row.startedAt)}`); option.value = row.id; return option; })); select.value = selected;
  const row = sessions.find(row => row.id === selected); $('#simulationEmpty').hidden = Boolean(row); $('#simulationLedger').hidden = !row; controls(); if (!row) return;
  $('#simulationStrategyEditor').hidden = row.status !== 'paused';
  $('#simulationResumeEditor').hidden = row.status !== 'paused';
  $('#resumeCashLabel').textContent = `本次新增资金（${currency(row.market)}）`;
  if (row.status === 'paused' && resumeFor !== row.id) { $('#resumeAddedCash').value = '0'; $('#resumeAllowNewStocks').checked = row.allowNewStocks === true; resumeFor = row.id; }
  if (row.status !== 'paused') resumeFor = '';
  if (row.status === 'paused' && switchFor !== row.id && switchLoadingFor !== row.id) void loadSwitchOptions(row);
  if (row.status !== 'paused') { switchFor = ''; switchLoadingFor = ''; switchRequest++; }
  $('#simulationName').textContent = `${row.strategyName} · ${currency(row.market)}`;
  $('#simulationMeta').textContent = `开始 ${stamp(row.startedAt)} · 更新 ${stamp(row.updatedAt)}（北京时间） · ${row.tradeCount} 笔成交`;
  $('#simulationState').textContent = row.status === 'paused' ? `已暂停 · ${stamp(row.pausedAt)} · ${row.pauseReason === 'service' ? '服务断开后自动暂停，可手动恢复' : '可手动恢复'}` : row.status === 'stopped' ? `旧版已停止记录 · ${stamp(row.stoppedAt)}` : `${row.note}${row.error ? `：${row.error}` : ''}`;
  $('#simulationStrategyHistory').textContent = row.strategyChanges?.length ? `策略切换：${row.strategyChanges.map(change => `${stamp(change.at)} ${change.fromName || change.from} → ${change.toName || change.to}`).join('；')}` : '';
  $('#simulationCapitalHistory').textContent = row.capitalEvents?.length ? `追加资金：${row.capitalEvents.map(event => `${stamp(event.at)} +${number(event.amount)} ${currency(row.market)}`).join('；')}` : '';
  const outside = row.positions.filter(position => !row.initialPositions.some(initial => initial.symbol === position.symbol));
  $('#simulationCandidatePool').textContent = `新股入仓：${row.allowNewStocks ? '允许' : '关闭'}${outside.length ? ` · 候选池新增 ${outside.length} 只（当前持有 ${outside.filter(position => position.shares > 0).length} 只）` : ''}${row.skippedCandidates?.length ? ` · 本交易日跳过缺少日线信号的候选股：${row.skippedCandidates.map(item => item.symbol).join('、')}` : ''}${row.candidateError ? ` · ${row.candidateError}` : ''}`;
  const recommendation = selectedRecommendation(); if (recommendation) showAiAnalysis(recommendation);
  const values = [['累计收益', `${number(row.profit)} ${currency(row.market)}`], ['累计收益率', pct(row.totalReturn)], ['账户总资产', number(row.equity)], ['可用现金', number(row.cash)], ['累计投入资产', number(row.initialEquity + (row.addedCapital || 0))], ['手续费与滑点', number(row.costs)]];
  $('#simulationMetrics').replaceChildren(...values.map(([label, value], index) => { const item = node('div', ''), strong = node('strong', value); if (index < 2) strong.className = row.profit > 0 ? 'profit-up' : row.profit < 0 ? 'profit-down' : ''; item.append(node('span', label), strong); return item; }));
  curve(row.history);
  const initialSymbols = new Set(row.initialPositions.map(position => position.symbol));
  const visiblePositions = row.positions.filter(position => position.shares > 0 || initialSymbols.has(position.symbol));
  const heldCount = row.positions.filter(position => position.shares > 0).length;
  $('#simulationHoldingsExplanation').textContent = heldCount ? '模拟仓位与持仓页分别记录；已卖出的初始股票仍列在下表，当前股数为 0。' : `本次模拟已清仓，${row.initialPositions.length} 只初始股票的模拟股数均为 0，${number(row.cash)} ${currency(row.market)} 保留为现金。持仓页的原始仓位没有变化；要重新复制，请${row.status === 'running' ? '先暂停，再' : ''}新建模拟。`;
  table('#simulationPositions', visiblePositions.map(position => [ `${position.name} (${position.symbol})`, number(row.initialPositions.find(initial => initial.symbol === position.symbol)?.shares || 0), number(position.shares), number(position.price), number(position.shares * position.price), row.equity > 0 ? pct(position.shares * position.price / row.equity) : '—', stamp(position.quoteAt)]), '暂无股票记录。');
  table('#simulationTrades', [...row.trades].reverse().map(trade => [stamp(trade.at), trade.symbol, trade.side === 'buy' ? '买入' : '卖出', number(trade.shares), number(trade.price), number(trade.fee), trade.signalDay]), '尚无模拟成交；休市、信号未触发或行情过期时会保留仓位。');
  controls();
}
async function refresh() {
  if (loading || busy || document.hidden) return; loading = true;
  try { const result = await api('/api/simulations'); if (busy) return; sessions = result.simulations; sessionsReady = true; render(); }
  catch (error) { $('#simulationNotice').textContent = `连接失败：${error.message}`; }
  finally { loading = false; }
}
form.addEventListener('submit', async event => {
  event.preventDefault(); const aiMode = strategy.value === '__ai__'; busy = true; controls(); $('#simulationNotice').textContent = aiMode ? '正在运行 AI 策略分析…' : '正在复制持仓并读取行情…';
  try {
    if (aiMode) await runAiAnalysis();
    const recommendationJob = aiJobs.get(market.value);
    const recommendationJobId = recommendationJob?.result?.recommendation?.strategy === strategy.value ? recommendationJob.id : undefined;
    const row = await api('/api/simulations', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({market: market.value, strategy: strategy.value, initialCash: Number(form.elements.initialCash.value), allowNewStocks: form.elements.allowNewStocks.checked, ...(recommendationJobId ? {recommendationJobId} : {})})});
    form.elements.initialCash.value = '0';
    selected = row.id; sessions.unshift(row); render(); $('#simulationNotice').textContent = aiMode ? 'AI 模拟已开始；推荐依据保存在分析卡片中。' : '模拟已开始，服务端将持续更新。';
  } catch (error) { $('#simulationNotice').textContent = error.message; }
  finally { busy = false; controls(); }
});
$('#analyzeSimulation').addEventListener('click', async () => {
  busy = true; controls(); $('#simulationNotice').textContent = '正在运行 AI 策略分析…';
  try { await runAiAnalysis(); $('#simulationNotice').textContent = '分析完成；推荐策略已在下拉框标注并选中。'; }
  catch (error) { $('#simulationNotice').textContent = error.message; showAiAnalysis(null, `分析失败：${error.message}`); }
  finally { busy = false; controls(); }
});
$('#stopSimulation').addEventListener('click', async () => {
  if (!window.confirm('停止后将永久删除这次模拟的持仓、成交和收益记录。确定停止吗？')) return;
  busy = true; controls();
  try { const result = await api(`/api/simulations/${selected}/stop`, {method: 'POST'}); sessions = sessions.filter(item => item.id !== result.id); render(); $('#simulationNotice').textContent = '模拟已停止，记录已删除。'; }
  catch (error) { $('#simulationNotice').textContent = error.message; }
  finally { busy = false; controls(); }
});
for (const action of ['pause', 'resume']) $(`#${action}Simulation`).addEventListener('click', async () => {
  if (action === 'resume' && !$('#resumeAddedCash').reportValidity()) return;
  busy = true; controls();
  try {
    const row = await api(`/api/simulations/${selected}/${action}`, {method: 'POST', ...(action === 'resume' ? {headers: {'Content-Type': 'application/json'}, body: JSON.stringify({addedCash: Number($('#resumeAddedCash').value), allowNewStocks: $('#resumeAllowNewStocks').checked})} : {})});
    sessions = sessions.map(item => item.id === row.id ? row : item); render();
    $('#simulationNotice').textContent = action === 'pause' ? '模拟已暂停，账本和收益记录已保存。' : '模拟已恢复，下一次行情更新时继续运行。';
  } catch (error) { $('#simulationNotice').textContent = error.message; }
  finally { busy = false; controls(); }
});
market.addEventListener('change', () => { initialSummary(); const recommendation = aiJobs.get(market.value)?.result?.recommendation; $('#aiRecommendationCard').hidden = !recommendation; if (recommendation) showAiAnalysis(recommendation); void loadOptions(); });
strategy.addEventListener('change', controls);
$('#resumeAddedCash').addEventListener('input', controls);
$('#simulationSelect').addEventListener('change', event => { selected = event.target.value; render(); });
$('#switchStrategy').addEventListener('change', controls);
$('#saveSimulationStrategy').addEventListener('click', async () => {
  const id = selected, next = $('#switchStrategy').value;
  busy = true; controls();
  try {
    const row = await api(`/api/simulations/${id}/strategy`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({strategy: next})});
    sessions = sessions.map(item => item.id === row.id ? row : item); render();
    $('#simulationNotice').textContent = '策略已切换。恢复后按新策略运行，原账本保留。';
  } catch (error) { $('#simulationNotice').textContent = error.message; }
  finally { busy = false; controls(); }
});
async function loadHoldings() { try { holdings = (await api('/api/portfolio')).holdings || []; initialSummary(); } catch (error) { $('#initialHoldings').textContent = error.message; } }
async function loadAiHistory() {
  try {
    for (const job of (await api('/api/jobs')).jobs) if (job.kind === 'ai_simulation' && job.status === 'completed' && !aiJobs.has(job.configuration?.market)) aiJobs.set(job.configuration.market, job);
    const recommendation = selectedRecommendation(); if (recommendation) { showAiAnalysis(recommendation); await loadOptions(); }
  } catch { /* AI history is optional when the page first loads. */ }
}
document.addEventListener('visibilitychange', () => { void refresh(); if (!document.hidden) void loadHoldings(); });
void loadHoldings(); void loadOptions(); void refresh(); void loadAiHistory(); setInterval(refresh, 5000);
