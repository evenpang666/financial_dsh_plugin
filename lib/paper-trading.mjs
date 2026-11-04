import {replaceFile as rename} from './atomic-file.mjs';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

const fail = (message, status = 400) => Object.assign(new Error(message), {status});
const positive = value => typeof value === 'number' && Number.isFinite(value) && value > 0;
const round = value => Math.round(value * 1e8) / 1e8;
const validCash = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1e12;
const capitalBase = session => session.initialEquity + (session.addedCapital || 0);
const marketDay = market => new Intl.DateTimeFormat('en-CA', {timeZone: {a: 'Asia/Shanghai', hk: 'Asia/Hong_Kong', us: 'America/New_York'}[market], year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date());

// All money and positions belong to this ledger, never to the saved portfolio.
export function advanceSimulation(session, snapshot, now = new Date()) {
  const quotes = new Map((snapshot.quotes || []).map(row => [row.symbol, row]));
  const time = now.toISOString(), day = snapshot.day;
  for (const row of session.positions) {
    const quote = quotes.get(row.symbol);
    if (positive(quote?.price) && Number.isFinite(Date.parse(quote.as_of)) && (!row.quoteAt || Date.parse(quote.as_of) >= Date.parse(row.quoteAt))) Object.assign(row, {price: quote.price, quoteAt: quote.as_of, source: quote.source});
  }
  const equity = () => session.cash + session.positions.reduce((sum, row) => sum + row.shares * row.price, 0);
  session.equity = round(equity()); session.profit = round(session.equity - capitalBase(session));
  session.totalReturn = session.profit / capitalBase(session); session.updatedAt = time;
  const openUntil = Date.parse(snapshot.marketOpenUntil);
  session.marketOpen = snapshot.marketOpen === true && Number.isFinite(openUntil) && now.getTime() < openUntil;
  session.note = snapshot.note || (session.marketOpen ? (session.rebalancedDay === day ? '今日已调仓，持续更新收益' : '等待策略信号') : '休市，等待下一交易时段');
  const original = new Set(session.initialPositions?.map(row => row.symbol) || session.positions.map(row => row.symbol));
  const requiredRows = session.positions.filter(row => row.shares > 0 || original.has(row.symbol));
  const fresh = requiredRows.every(row => {
    const quote = quotes.get(row.symbol), age = now.getTime() - Date.parse(quote?.as_of);
    return positive(quote?.price) && quote.as_of === row.quoteAt && Number.isFinite(age) && age >= -5000 && age <= 120000;
  });
  const rows = snapshot.signals?.rows;
  const complete = /^\d{4}-\d{2}-\d{2}$/.test(snapshot.signals?.asOf || '') && snapshot.signals.asOf < day && Array.isArray(rows) && requiredRows.every(row => rows.some(signal => signal.symbol === row.symbol && typeof signal.entry === 'boolean' && Number.isFinite(signal.score)));
  // Completed daily bars produce one rebalance per market day. Repeated quote
  // polls mark the ledger to market without repeatedly buying the same signal.
  if (session.marketOpen && !fresh) session.note = '行情缺失或超过两分钟，仅更新估值，暂不成交';
  else if (session.marketOpen && !complete) session.note = snapshot.signals?.error || '策略信号不完整，保留模拟持仓';
  else if (session.marketOpen && day && session.rebalancedDay !== day) {
    const positions = new Map(session.positions.map(row => [row.symbol, row]));
    const selected = rows.filter(row => {
      const position = positions.get(row.symbol), quote = quotes.get(row.symbol);
      const age = now.getTime() - Date.parse(quote?.as_of);
      return row.entry && position && (session.allowNewStocks || original.has(row.symbol) || position.shares > 0) && positive(position.price) && positive(quote?.price) && quote.as_of === position.quoteAt && Number.isFinite(age) && age >= -5000 && age <= 120000;
    }).sort((a, b) => b.score - a.score || a.symbol.localeCompare(b.symbol)).slice(0, snapshot.signals.topN || 5);
    const symbols = new Set(selected.map(row => row.symbol)), budget = equity() / Math.max(selected.length, 1);
    const lot = session.market === 'a' ? 100 : 1;
    const targets = new Map(session.positions.map(row => [row.symbol, symbols.has(row.symbol) ? Math.floor(budget / (row.price * 1.001) / lot) * lot : 0]));
    const trade = (row, side, shares) => {
      const price = row.price * (side === 'buy' ? 1.0005 : .9995), fee = shares * price * .0005;
      session.cash = round(session.cash + (side === 'buy' ? -shares * price - fee : shares * price - fee));
      row.shares = round(row.shares + (side === 'buy' ? shares : -shares));
      if (side === 'buy') { row.boughtDay = day; row.boughtShares = round((row.boughtShares || 0) + shares); }
      session.costs = round(session.costs + fee + Math.abs(price - row.price) * shares);
      session.trades.push({at: time, quoteAt: row.quoteAt, signalDay: snapshot.signals.asOf, symbol: row.symbol, side, shares, price, fee});
    };
    for (const row of session.positions) {
      if (row.boughtDay !== day) row.boughtShares = 0;
      const sellable = row.shares - (session.market === 'a' ? row.boughtShares || 0 : 0);
      const shares = Math.min(Math.max(0, row.shares - targets.get(row.symbol)), sellable);
      if (shares > 0) trade(row, 'sell', shares);
    }
    for (const row of session.positions) {
      const required = Math.max(0, targets.get(row.symbol) - row.shares);
      const affordable = Math.floor(session.cash / (row.price * 1.0005 * 1.0005) / lot) * lot;
      const shares = Math.min(required, affordable);
      if (shares > 0) trade(row, 'buy', shares);
    }
    session.rebalancedDay = day; session.signalAsOf = snapshot.signals.asOf;
    session.note = '已按最近完整日线调仓；交易时段持续更新收益';
  }
  if (Array.isArray(snapshot.signals?.skipped)) {
    session.skippedCandidates = snapshot.signals.skipped;
    if (session.skippedCandidates.length) session.note += `；跳过 ${session.skippedCandidates.length} 只缺少日线信号的候选股`;
  }
  session.equity = round(equity()); session.profit = round(session.equity - capitalBase(session));
  session.totalReturn = session.profit / capitalBase(session);
  if (!session.history.length || Date.parse(time) - Date.parse(session.history.at(-1).at) >= 60000) {
    session.history.push({at: time, equity: session.equity, totalReturn: session.totalReturn});
    if (session.history.length > 10000) session.history.splice(1, 1);
  }
  return session;
}

export async function createPaperTrading({directory, portfolio, snapshot, candidates = async () => [], intervalMs = 30000}) {
  const file = join(directory, 'paper-trading.json');
  let sessions = [], queue = Promise.resolve(), closed = false;
  try { sessions = JSON.parse(await readFile(file, 'utf8')); if (!Array.isArray(sessions)) throw Error('模拟交易记录格式无效。'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const serial = action => { const result = queue.then(action); queue = result.catch(() => {}); return result; };
  const save = async () => { await writeFile(file + '.tmp', JSON.stringify(sessions)); await rename(file + '.tmp', file); };
  const view = row => ({...row, trades: row.trades.slice(-200), tradeCount: row.trades.length, history: row.history.slice(-1000)});
  const pause = (row, reason) => { row.status = 'paused'; row.pausedAt = new Date().toISOString(); row.pauseReason = reason; row.note = reason === 'service' ? '服务已断开，模拟已自动暂停' : '模拟已暂停，恢复后继续更新'; };
  async function addCandidates(row) {
    if (!row.allowNewStocks) return;
    const day = marketDay(row.market);
    if (row.candidateDay === day || (row.candidateRetryAt && Date.now() < Date.parse(row.candidateRetryAt))) return;
    try {
      const source = await candidates(row.market);
      if (!Array.isArray(source)) throw Error('候选股票池格式无效。');
      const original = new Set(row.initialPositions.map(item => item.symbol));
      row.positions = row.positions.filter(item => item.shares > 0 || original.has(item.symbol));
      const existing = new Set(row.positions.map(item => item.symbol));
      for (const item of source.slice(0, 20)) {
        if (row.positions.length >= 100) break;
        if (typeof item?.symbol !== 'string' || !/^[A-Z0-9.-]{1,16}$/.test(item.symbol) || existing.has(item.symbol)) continue;
        row.positions.push({symbol: item.symbol, name: typeof item.name === 'string' ? item.name.slice(0, 80) : item.symbol, shares: 0, price: 0});
        existing.add(item.symbol);
      }
      row.candidateDay = day; row.candidateRetryAt = null; row.candidateError = null;
    } catch (error) {
      row.candidateError = `新股候选池暂不可用：${error.message}`;
      row.candidateRetryAt = new Date(Date.now() + 300000).toISOString();
    }
  }
  // A terminated process cannot run close(); recover its saved ledger as paused.
  if (sessions.some(row => row.status === 'running')) {
    for (const row of sessions) if (row.status === 'running') pause(row, 'service');
    await save();
  }
  async function tick() {
    for (const row of sessions.filter(row => row.status === 'running')) {
      try { await addCandidates(row); advanceSimulation(row, await snapshot(row)); row.error = null; if (row.candidateError) row.note += `；${row.candidateError}`; }
      catch (error) { row.error = error.message; row.note = '取数失败，保留上次估值并自动重试'; }
    }
    if (sessions.length) await save();
  }
  // Schedule after completion, so slow providers cannot accumulate queued ticks.
  let timer;
  function schedule() { if (!closed) timer = setTimeout(() => { void serial(tick).catch(() => {}).finally(schedule); }, intervalMs); timer?.unref(); }
  schedule();
  return {
    list: () => sessions.map(view).reverse(),
    start: input => serial(async () => {
      if (!input || !['a', 'hk', 'us'].includes(input.market) || typeof input.strategy !== 'string' || !/^[a-z_]+(?:@[A-Za-z0-9_-]{1,110})?$/.test(input.strategy)) throw fail('请选择有效的市场和策略。');
      if (input.initialCash !== undefined && !validCash(input.initialCash)) throw fail('初始现金须为 0–1 万亿的数值。');
      if (input.allowNewStocks !== undefined && typeof input.allowNewStocks !== 'boolean') throw fail('允许新股入仓须为布尔值。');
      if (sessions.some(row => row.status === 'running')) throw fail('已有模拟正在运行，请先暂停或停止后再开始。', 409);
      const saved = await portfolio();
      const positions = (saved.holdings || []).filter(row => (row.market || 'a') === input.market).map(row => ({symbol: row.symbol, name: row.name || row.symbol, shares: row.shares}));
      if (!positions.length || positions.length > 100 || positions.some(row => !positive(row.shares))) throw fail('当前市场须有 1–100 条有效持仓记录。');
      const row = {id: randomUUID(), market: input.market, strategy: input.strategy, status: 'running', startedAt: new Date().toISOString(), initialPositions: structuredClone(positions), positions, allowNewStocks: input.allowNewStocks === true, cash: input.initialCash || 0, initialCash: input.initialCash || 0, addedCapital: 0, capitalEvents: [], costs: 0, trades: [], history: [], ...(input.aiRecommendation ? {aiRecommendation: input.aiRecommendation} : {})};
      await addCandidates(row);
      const first = await snapshot(row), quotes = new Map((first.quotes || []).map(quote => [quote.symbol, quote]));
      if (row.initialPositions.some(position => !positive(quotes.get(position.symbol)?.price))) throw fail('持仓行情不完整，无法确定初始市值，请稍后重试。');
      for (const position of positions) position.price = quotes.get(position.symbol)?.price || 0;
      row.initialEquity = row.cash + positions.reduce((sum, position) => sum + position.shares * position.price, 0);
      row.strategyName = first.strategyName || input.strategy;
      row.history.push({at: row.startedAt, equity: row.initialEquity, totalReturn: 0});
      advanceSimulation(row, first);
      sessions.push(row); await save(); return view(row);
    }),
    pause: id => serial(async () => {
      const row = sessions.find(row => row.id === id); if (!row) throw fail('模拟记录不存在。', 404);
      if (row.status !== 'running') throw fail('只有运行中的模拟可以暂停。', 409);
      pause(row, 'manual'); await save(); return view(row);
    }),
    switchStrategy: (id, strategy, strategyName) => serial(async () => {
      const row = sessions.find(row => row.id === id); if (!row) throw fail('模拟记录不存在。', 404);
      if (row.status !== 'paused') throw fail('请先暂停模拟，再切换策略。', 409);
      if (row.strategy === strategy) throw fail('请选择不同的策略。', 409);
      const at = new Date().toISOString();
      (row.strategyChanges ||= []).push({at, from: row.strategy, to: strategy, fromName: row.strategyName, toName: strategyName});
      row.strategy = strategy; row.strategyName = strategyName; row.updatedAt = at;
      row.note = '策略已切换；恢复后按新策略继续运行';
      await save(); return view(row);
    }),
    resume: (id, input = {}) => serial(async () => {
      const row = sessions.find(row => row.id === id); if (!row) throw fail('模拟记录不存在。', 404);
      if (row.status !== 'paused') throw fail('只有已暂停的模拟可以恢复。', 409);
      if (sessions.some(item => item.status === 'running')) throw fail('已有模拟正在运行，请先暂停。', 409);
      if (input.addedCash !== undefined && !validCash(input.addedCash)) throw fail('本次新增资金须为 0–1 万亿的数值。');
      if (input.allowNewStocks !== undefined && typeof input.allowNewStocks !== 'boolean') throw fail('允许新股入仓须为布尔值。');
      const addedCash = input.addedCash || 0;
      row.allowNewStocks = input.allowNewStocks ?? row.allowNewStocks ?? false;
      if (addedCash) {
        const at = new Date().toISOString();
        row.cash = round(row.cash + addedCash); row.addedCapital = round((row.addedCapital || 0) + addedCash);
        row.equity = round(row.equity + addedCash); row.profit = round(row.equity - capitalBase(row)); row.totalReturn = row.profit / capitalBase(row);
        (row.capitalEvents ||= []).push({at, amount: addedCash});
        row.history.push({at, equity: row.equity, totalReturn: row.totalReturn});
      }
      await addCandidates(row);
      row.status = 'running'; row.pausedAt = null; row.pauseReason = null; row.resumedAt = new Date().toISOString(); row.note = '已恢复，等待下一次行情更新'; row.error = null;
      await save(); return view(row);
    }),
    stop: id => serial(async () => {
      const row = sessions.find(row => row.id === id); if (!row) throw fail('模拟记录不存在。', 404);
      sessions = sessions.filter(item => item.id !== id); await save(); return {id, deleted: true};
    }),
    async close() {
      if (closed) { await queue; return; }
      closed = true; clearTimeout(timer);
      await serial(async () => {
        const running = sessions.filter(row => row.status === 'running');
        if (running.length) { for (const row of running) pause(row, 'service'); await save(); }
      });
    },
  };
}
