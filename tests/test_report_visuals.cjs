const test = require('node:test')
const assert = require('node:assert/strict')
const visuals = require('../web/report-visuals.js')
const stocks = require('../web/report-stocks.js')

test('report stock identities retain their market and normalize HK and US tickers', () => {
  assert.deepEqual(stocks.identity({symbol: '700', name: '腾讯'}, 'hk'), {market: 'hk', symbol: '00700', name: '腾讯'})
  assert.equal(stocks.identity({symbol: 'BRK.B'}, 'us').symbol, 'BRK-B')
  assert.equal(stocks.identity({symbol: '00700', market: 'hk'}, 'a').market, 'hk')
  for (const row of [{symbol: ''}, {symbol: '600519/inject'}, {symbol: '000000'}, {symbol: 'AAPL'}, {symbol: '123456', market: 'bad'}]) assert.equal(stocks.identity(row, 'a'), null)
})

test('legacy report tables identify stocks only in explicit security-code columns', () => {
  assert.equal(stocks.tableIdentity(['排名', '代码', '名称'], ['1', '002946', '新乳业'], 'a').symbol, '002946')
  assert.equal(stocks.tableIdentity(['日期', '成交量'], ['2026-11-03', '600519'], 'a'), null)
  assert.equal(stocks.tableIdentity(['代码', '名称'], ['1', '排名'], 'a'), null)
})

test('report stock mentions respect ticker boundaries and avoid ambiguous names', () => {
  const rows = [stocks.identity({symbol: 'AAPL', name: 'Apple'}, 'us'), stocks.identity({symbol: 'BRK.B'}, 'us')]
  assert.deepEqual(stocks.mentions('AAPL、BRK.B / BRK-B；XAAPL、AAPL2、AAPL.O', rows).map(m => m.stock.symbol), ['AAPL', 'BRK-B', 'BRK-B'])
  const ambiguous = [stocks.identity({symbol: '600519', name: '同名'}, 'a'), stocks.identity({symbol: '00700', name: '同名'}, 'hk')]
  assert.equal(stocks.mentions('同名', ambiguous).length, 0)
  assert.equal(stocks.mentions('持仓600519，金额16005190', ambiguous).length, 1)
})

test('missing and non-finite values never become zero returns', () => {
  for (const value of [null, undefined, '', ' ', false, Infinity, NaN, 'invalid']) {
    assert.equal(visuals.number(value), null)
    assert.equal(visuals.signed(value), '—')
    assert.equal(visuals.heatLevel(value), 'heat-missing')
  }
  assert.equal(visuals.signed(0), '0.00%')
})

test('breadth denominator includes flat stocks and discloses legacy missing flats', () => {
  const full = visuals.breadthParts({advancing: 20, declining: 30, unchanged: 50})
  assert.equal(full.total, 100)
  assert.equal(full.hasFlat, true)
  const legacy = visuals.breadthParts({advancing: 20, declining: 30})
  assert.equal(legacy.total, 50)
  assert.equal(legacy.hasFlat, false)
  for (const breadth of [{}, {advancing: 0, declining: 0}, {advancing: -1, declining: 20}, {advancing: 10, declining: 20, unchanged: -3}]) {
    assert.equal(visuals.breadthParts(breadth), null)
  }
})

test('fallback neutral inputs remain unavailable rather than measured neutral scores', () => {
  const data = visuals.scoreData({market_state: {risk_score: 50}, market_breadth: {score: 50}, social_sentiment: {score: 50}, unavailable: ['大盘宽度不可用：timeout', '社交情绪不可用：timeout']})
  assert.equal(data[0].value, 50)
  assert.equal(data[1].value, null)
  assert.equal(data[2].value, null)
  assert.equal(visuals.scoreData({market_state: {risk_score: 101}})[0].value, null)
})

test('events deduplicate repeated headlines and keep newest dates first', () => {
  const rows = visuals.eventRows({weekly_events: {events: [{title: '旧事件', date: '2026-10-01'}, {title: '政策事件', date: '2026-10-12'}]}, market_panorama: {domestic_policies: [{title: '政策事件', date: '2026-10-12'}, {title: '另一事件', date: '2026-10-10'}, {title: ''}]}})
  assert.deepEqual(rows.map(row => row.title), ['政策事件', '另一事件', '旧事件'])
})

test('a source name or an empty payload alone does not count as chart coverage', () => {
  const coverage = Object.fromEntries(visuals.coverage({global_markets: {source: 'test', indices: [{change_pct: null}]}, sectors: {boards: [{change_pct: 'NaN'}]}, candidates: {ranking: []}}))
  assert.equal(coverage['全球指数'], false)
  assert.equal(coverage['主题板块'], false)
  assert.equal(coverage['候选排行'], false)
})
