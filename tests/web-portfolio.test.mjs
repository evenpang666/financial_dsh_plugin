import test from 'node:test';
import assert from 'node:assert/strict';
import {parsePortfolio, importPortfolio} from '../lib/portfolio.mjs';

const row = (source, symbol, quantity, market = 'a') => ({market, symbol, quantity, source});
const response = (holdings, issues = []) => JSON.stringify({holdings, issues});
test('mixed text preserves A/HK identity and derives share counts from verbatim quantities', () => {
  const text = '600519 100股\n港股 700 1.5万股\n000858，1,000股';
  assert.deepEqual(parsePortfolio(text, '```json\n' + response([row('600519 100股', '600519', '100股'), row('港股 700 1.5万股', '00700', '1.5万股', 'hk'), row('000858，1,000股', '000858', '1,000股')]) + '\n```'), [{market:'a',symbol:'600519',shares:100},{market:'hk',symbol:'00700',shares:15000},{market:'a',symbol:'000858',shares:1000}]);
  assert.equal(parsePortfolio('HK00700 200股', response([row('HK00700 200股', '00700', '200股', 'hk')]))[0].shares, 200);
});
test('invented records, quantities, ambiguous units and duplicate codes never reach storage', () => {
  const text = '600519 100股';
  for (const entry of [row(text, '000858', '100股'), row('600519 999股','600519','999股'), row(text,'600519','999股'), row(text,'600519','600519')]) assert.throws(() => parsePortfolio(text, response([entry])));
  for (const quantity of ['0股','-100股','100.5股','100手','100-200股']) { const source = '600519 '+quantity; assert.throws(()=>parsePortfolio(source,response([row(source,'600519',quantity)]))); }
  assert.throws(()=>parsePortfolio('700 200股',response([row('700 200股','00700','200股','hk')])), /注明/);
  assert.throws(()=>parsePortfolio(text,response([row(text,'600519','100股'),row(text,'600519','100股')])), /重复/);
  assert.throws(()=>parsePortfolio(text,response([],['需要当前总股数'])), /当前总股数/);
  for (const invalid of ['null','[]','正文而非JSON']) assert.throws(()=>parsePortfolio(text,invalid));
});
test('DSH resolves text with read-only tools and preview never imports holdings', async () => {
  const paths = [], controller = new AbortController();
  const result = await importPortfolio({text:'600519 100股',preview:true},{signal:controller.signal,progress(){},research:async (_prompt,options)=>{assert.deepEqual(options.allowedTools,['stock_search']); assert.equal(options.purpose,'portfolio'); return {text:response([row('600519 100股','600519','100股')]),model:'host-model'};}, data:async(path,payload)=>{paths.push(path); return {holdings:payload.holdings.map(item=>({...item,name:'贵州茅台'})),unavailable:[]};}});
  assert.deepEqual(paths,['/v1/portfolio/resolve']); assert.equal(result.preview,true); assert.equal(result.holdings[0].name,'贵州茅台');
});
test('cancellation during metadata lookup prevents the subsequent cache write', async () => {
  const controller = new AbortController(), paths = [];
  await assert.rejects(importPortfolio({text:'600519 100股'},{signal:controller.signal,progress(){},research:async()=>({text:response([row('600519 100股','600519','100股')])}),data:async(path,payload)=>{paths.push(path);controller.abort(); return payload;}}));
  assert.deepEqual(paths,['/v1/portfolio/resolve']);
});
