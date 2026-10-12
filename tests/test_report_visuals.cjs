const test = require('node:test')
const assert = require('node:assert/strict')
const visuals = require('../web/report-visuals.js')

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
