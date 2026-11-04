import test from 'node:test';
import assert from 'node:assert/strict';
import {stockEvidence, recommendationPrompt, parseRecommendation} from '../lib/ai-simulation.mjs';

const points = Array.from({length:30}, (_, index) => ({date:`2026-10-${String(index + 1).padStart(2,'0')}`,open:10+index/10,high:11+index/10,low:9+index/10,close:10+index/10,volume:1000+index*10}));
test('AI evidence uses completed adjusted bars and keeps the live quote separate', () => {
  const row=stockEvidence({symbol:'600001',name:'测试股',shares:100},{points,as_of:'2026-10-30',source:'test'}, {price:20,as_of:'2026-11-04T02:00:00Z',source:'quote'});
  assert.equal(row.historyAsOf,'2026-10-30'); assert.equal(row.adjustedClose,12.9); assert.equal(row.quote.price,20);
  assert.ok(row.return5Pct>0); assert.ok(row.volatility20Pct>=0);
  assert.match(recommendationPrompt('a',[row],{momentum:{name:'日线动量'}}),/口径不同，不得直接相除/);
});
test('DSH recommendation must choose an available strategy and cover every holding', () => {
  const evidence=[{symbol:'600001',name:'甲'},{symbol:'600002',name:'乙'}], strategies={momentum:{name:'动量'}};
  const valid={strategy:'momentum',summary:'两只股票近期走强。',reason:'动量比反弹更贴近当前走势。',limits:'样本短，需回测。',stocks:evidence.map(row=>({symbol:row.symbol,observation:'近日上涨。',fit:'可观察动量。',risk:'可能回落。'}))};
  assert.equal(parseRecommendation(JSON.stringify(valid),'a',evidence,strategies).stocks.length,2);
  assert.throws(()=>parseRecommendation(JSON.stringify({...valid,strategy:'unknown'}),'a',evidence,strategies),/有效策略/);
  assert.throws(()=>parseRecommendation(JSON.stringify({...valid,stocks:valid.stocks.slice(0,1)}),'a',evidence,strategies),/全部持仓/);
});
