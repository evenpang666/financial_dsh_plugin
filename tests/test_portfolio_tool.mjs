import test from 'node:test'
import assert from 'node:assert/strict'
import {apply} from '../dsh-finance-agent/index.js'

test('portfolio review tool supports selected strategy, market, timeout and bundled skill', async () => {
  const tools = new Map(), skills = new Map(), requests = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (url, options) => {
    requests.push({url, options})
    return {ok: true, json: async () => ({available: true, position_summary: {rows: []}})}
  }
  try {
    apply({tools: {register: definition => tools.set(definition.name, definition)},
      skills: {register: definition => skills.set(definition.name, definition)}})
    const tool = tools.get('get_portfolio_position_review')
    assert.ok(tool)
    assert.ok(tool.parameters.properties.strategy.enum.includes('macd'))
    const result = await tool.execute({market: 'hk', strategy: 'macd'})
    assert.equal(result.available, true)
    assert.match(requests[0].url, /\/v1\/portfolio-analysis\?market=hk&strategy=macd&checkpoint=&parameters=null$/)
    assert.ok(requests[0].options.signal instanceof AbortSignal)
    assert.equal(requests[0].options.method, undefined)
    assert.equal(requests.length, 1)
    await tool.execute({market: 'a', strategy: 'elastic_net', checkpoint: '20261013-120000'})
    assert.match(requests[1].url, /strategy=elastic_net&checkpoint=20261013-120000&parameters=null$/)
    await tool.execute({market: 'a', strategy: 'momentum', parameters: {threshold: 0.01, top_n: 3}})
    const query = new URL(requests[2].url).searchParams
    assert.deepEqual(JSON.parse(query.get('parameters')), {threshold: 0.01, top_n: 3})
    await tools.get('get_strategy_checkpoints').execute({})
    assert.match(requests[3].url, /\/v1\/strategy-options$/)
    assert.ok(skills.get('portfolio-daily-review').content.includes('get_portfolio_position_review'))
  } finally { globalThis.fetch = originalFetch }
})
