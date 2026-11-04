import test from 'node:test';
import assert from 'node:assert/strict';
import {parsePortfolio, importPortfolio} from '../lib/portfolio.mjs';
import charts from '../web/portfolio-charts.js';
import ordering from '../web/portfolio.js';

test('holding cards default to market value order without mutating saved records', () => {
  const rows = [{symbol:'000001',shares:1000},{symbol:'000002',shares:10},{symbol:'000003',shares:100}];
  const quotes = new Map([['a:000001',{price:1}],['a:000002',{price:200}],['a:000003',{price:15}],['hk:000001',{price:9999}]]);
  assert.deepEqual(ordering.sortHoldings(rows,quotes,'a').map(row=>row.symbol),['000002','000003','000001']);
  assert.deepEqual(rows.map(row=>row.symbol),['000001','000002','000003']);
  quotes.set('a:000001',{price:3});
  assert.equal(ordering.sortHoldings(rows,quotes,'a')[0].symbol,'000001');
});

test('change sorting respects sign, stable ties and missing quotes in both directions', () => {
  const rows = ['missing','gain','loss','flat','tie'].map(symbol=>({symbol,shares:1}));
  const quotes = new Map([['a:gain',{change_pct:2}],['a:loss',{change_pct:-5}],['a:flat',{change_pct:0}],['a:tie',{change_pct:2}]]);
  assert.deepEqual(ordering.sortHoldings(rows,quotes,'a','change-desc').map(row=>row.symbol),['gain','tie','flat','loss','missing']);
  assert.deepEqual(ordering.sortHoldings(rows,quotes,'a','change-asc').map(row=>row.symbol),['loss','flat','gain','tie','missing']);
  quotes.set('a:missing',{change_pct:''});
  assert.equal(ordering.sortHoldings(rows,quotes,'a','change-asc').at(-1).symbol,'missing');
});

test('invalid market values stay unavailable and sort after known positions', () => {
  for (const price of [null,'',false,Infinity,-1,0]) assert.equal(ordering.positionValue({shares:100},{price}),null);
  assert.equal(ordering.positionValue({shares:Infinity},{price:1}),null);
  assert.equal(ordering.positionValue({shares:1e308},{price:1e308}),null);
  const rows = [{symbol:'missing',shares:100},{symbol:'known',shares:10},{symbol:'also-missing',shares:20}];
  assert.deepEqual(ordering.sortHoldings(rows,new Map([['a:known',{price:3}]]),'a').map(row=>row.symbol),['known','missing','also-missing']);
});

test('holding chart colors and labels distinguish gains, losses, flats and missing values', () => {
  for (const [value, className, label] of [[2.5,'is-up','+2.50%'],[-1.2,'is-down','-1.20%'],[0,'is-flat','0.00%'],[null,'is-missing','—'],['','is-missing','—'],[Infinity,'is-missing','—']]) {
    assert.equal(charts.trendClass(value),className); assert.equal(charts.formatChange(value),label);
  }
});

test('holding trends use full-history moving averages and actual interval drawdown', () => {
  const points = Array.from({length:30},(_,i)=>({date:`2026-10-${String(i+1).padStart(2,'0')}`,close:i+1,volume:100}));
  const {points: visible} = charts.prepareSeries(points,5);
  assert.equal(visible.length,5); assert.equal(visible[0].ma5,24); assert.equal(visible[0].ma20,16.5);
  assert.equal(visible.at(-1).ma5,28); assert.equal(visible.at(-1).ma20,20.5);
  const result = charts.prepareSeries([100,120,90,108].map((close,i)=>({date:`2026-10-0${i+1}`,close})),60);
  assert.ok(Math.abs(result.summary.changePct-8)<1e-8); assert.equal(result.summary.drawdownPct,-25);
  assert.equal(result.summary.low,90); assert.equal(result.summary.high,120);
});

test('holding trends reject invalid prices, preserve missing volume and deduplicate days', () => {
  const result=charts.prepareSeries([{date:'2026-10-02',close:20,volume:-1},{date:'2026-10-01',close:10},{date:'2026-10-02',close:22,volume:0},{date:'2026-10-03',close:null},{date:'2026-10-04',close:Infinity},{date:'2026-10-05',close:0}]);
  assert.deepEqual(result.points.map(row=>[row.date,row.close,row.volume]),[['2026-10-01',10,null],['2026-10-02',22,0]]);
  assert.equal(charts.prepareSeries([{date:'2026-10-01',close:10}]).summary,null);
});

test('candlesticks use open-to-close bodies, high-low wicks, and doji geometry', () => {
  const scale = value => 200 - value * 10;
  const up = charts.candleGeometry({open:10,close:12,high:13,low:9},50,scale,8);
  assert.deepEqual(up,{x:46,width:8,top:70,bottom:110,bodyY:80,bodyHeight:20,direction:'is-up'});
  const down = charts.candleGeometry({open:12,close:10,high:13,low:9},50,scale,8);
  assert.equal(down.direction,'is-down');assert.equal(down.bodyY,up.bodyY);assert.equal(down.bodyHeight,20);
  const flat = charts.candleGeometry({open:10,close:10,high:11,low:9},50,scale,8);
  assert.equal(flat.direction,'is-flat');assert.equal(flat.bodyHeight,1);
});

test('missing or contradictory OHLC never creates a made-up candle, while volume zero stays visible', () => {
  for (const point of [{close:10},{open:null,close:10,high:12,low:9},{open:10,close:11,high:9,low:8},{open:10,close:11,high:12,low:11.5}]) {
    assert.equal(charts.validCandle(point),false);assert.equal(charts.candleGeometry(point,50,value=>value,5),null);
  }
  const {points}=charts.prepareSeries([{date:'2026-11-01',open:'10',close:'11',high:'12',low:'9',volume:0},{date:'2026-11-02',open:11,close:12,high:13,low:10,volume:123456}]);
  assert.equal(points[0].open,10);assert.equal(points[0].high,12);assert.equal(points[0].low,9);
  assert.equal(points[0].volume,0);assert.equal(points[1].volume,123456);assert.equal(charts.validCandle(points[0]),true);
});

test('weekly bars combine first open, last close, extremes and summed actual volume', () => {
  const raw=[['2026-10-26',10,11,13,9,100],['2026-10-28',11,12,14,10,0],['2026-10-30',12,13,15,11,200],['2026-11-02',13,14,16,12,50]].map(([date,open,close,high,low,volume])=>({date,open,close,high,low,volume}));
  const weekly=charts.aggregatePeriods(raw,'weekly',{from:'2026-10-26',through:'2026-11-02'});
  assert.equal(weekly.length,2);
  assert.deepEqual([weekly[0].open,weekly[0].close,weekly[0].high,weekly[0].low,weekly[0].volume],[10,13,15,9,300]);
  assert.equal(weekly[0].complete,true);assert.equal(weekly[0].trading_days,3);
  assert.equal(weekly[1].complete,false);assert.equal(weekly[1].incomplete_end,true);assert.equal(weekly[1].volume,50);
});

test('monthly grouping respects year boundaries, incomplete starts and missing inputs', () => {
  const raw=[{date:'2025-12-30',open:10,close:11,high:12,low:9,volume:5},{date:'2025-12-31',open:11,close:12,high:13,low:10,volume:7},{date:'2026-01-02',open:12,close:13,high:14,low:11,volume:null},{date:'2026-01-05',open:null,close:14,high:15,low:12,volume:8}];
  const monthly=charts.aggregatePeriods(raw,'monthly',{from:'2025-12-15',through:'2026-01-05'});
  assert.equal(monthly.length,2);assert.equal(monthly[0].period_end,'2025-12-31');assert.equal(monthly[0].volume,12);
  assert.equal(monthly[0].incomplete_start,true);assert.equal(monthly[0].incomplete_end,false);
  assert.equal(monthly[1].open,null);assert.equal(monthly[1].high,null);assert.equal(monthly[1].low,null);assert.equal(monthly[1].close,14);assert.equal(monthly[1].volume,null);
  assert.equal(monthly[1].complete,false);
});

test('period moving averages use grouped closes and panned windows keep correct warmup', () => {
  const raw=Array.from({length:10},(_,i)=>{const date=new Date(Date.UTC(2026,7,3+i*7)).toISOString().slice(0,10);return {date,open:10+i,close:11+i,high:12+i,low:9+i,volume:0};});
  const weekly=charts.aggregatePeriods(raw,'weekly',{through:'2026-12-31'});
  const {points}=charts.prepareSeries(weekly,3,2);
  assert.deepEqual(points.map(point=>point.close),[16,17,18]);assert.equal(points.at(-1).ma5,16);
  assert.equal(weekly[0].volume,0);
});

test('wheel zoom preserves the pointer anchor and panning clamps to loaded history', () => {
  const initial={count:60,offset:30}, ratio=.25;
  const zoom=charts.zoomViewport(300,initial,-120,ratio);
  const anchor=view=>300-view.offset-view.count+ratio*(view.count-1);
  assert.ok(zoom.count<initial.count);assert.ok(Math.abs(anchor(zoom)-anchor(initial))<=1);
  assert.equal(charts.panViewport(300,initial,25).offset,55);
  assert.equal(charts.panViewport(300,initial,-1000).offset,0);
  assert.equal(charts.panViewport(300,initial,1000).offset,240);
  let view=initial;for(let i=0;i<30;i++)view=charts.zoomViewport(300,view,-240,.5);
  assert.equal(view.count,8);for(let i=0;i<30;i++)view=charts.zoomViewport(300,view,240,.5);
  assert.deepEqual(view,{count:300,offset:0});assert.deepEqual(charts.zoomViewport(0,initial,-100),{count:0,offset:0});
});

test('MACD uses seeded EMA warmup and the Chinese doubled histogram convention', () => {
  const series=Array.from({length:45},(_,i)=>({date:new Date(Date.UTC(2026,0,i+1)).toISOString().slice(0,10),close:i+1}));
  const points=charts.calculateIndicators(series);
  assert.equal(points[24].dif,null);assert.equal(points[25].dif,7);assert.equal(points[32].dea,null);
  assert.ok(Math.abs(points[33].dea-7)<1e-10);assert.ok(Math.abs(points[33].macdHistogram)<1e-10);
  series.push({date:'2026-02-15',close:60});
  const last=charts.calculateIndicators(series).at(-1);
  assert.ok(last.dif>7);assert.ok(last.macdHistogram>0);assert.ok(Math.abs(last.macdHistogram-2*(last.dif-last.dea))<1e-12);
});

test('Wilder RSI handles rising, falling and flat prices without bogus warmup zeros', () => {
  const dated=values=>values.map((close,i)=>({date:new Date(Date.UTC(2026,0,i+1)).toISOString().slice(0,10),close}));
  const up=charts.calculateIndicators(dated(Array.from({length:30},(_,i)=>100+i)));
  const down=charts.calculateIndicators(dated(Array.from({length:30},(_,i)=>100-i)));
  const flat=charts.calculateIndicators(dated(Array(40).fill(100)));
  assert.equal(up[13].rsi14,null);assert.equal(up[14].rsi14,100);assert.equal(down[14].rsi14,0);assert.equal(flat[14].rsi14,50);
  assert.equal(flat[33].dif,0);assert.equal(flat[33].dea,0);assert.equal(flat[33].macdHistogram,0);
  const alternating=Array.from({length:15},(_,i)=>100+i%2);alternating.push(102);
  const mixed=charts.calculateIndicators(dated(alternating));assert.equal(mixed[14].rsi14,50);assert.ok(Math.abs(mixed[15].rsi14-56.666666666666664)<1e-10);
});

test('Bollinger bands use 20-bar population variance and retain unavailable warmup', () => {
  const points=charts.calculateIndicators(Array.from({length:20},(_,i)=>({date:`2026-01-${String(i+1).padStart(2,'0')}`,close:i+1})));
  assert.equal(points[18].bollUpper,null);assert.equal(points[19].bollMid,10.5);
  assert.ok(Math.abs(points[19].bollUpper-(10.5+2*Math.sqrt(33.25)))<1e-10);
  assert.ok(Math.abs(points[19].bollLower-(10.5-2*Math.sqrt(33.25)))<1e-10);
});

test('technical indicators are causal and stay unchanged when zooming or panning', () => {
  const raw=Array.from({length:100},(_,i)=>({date:new Date(Date.UTC(2026,0,i+1)).toISOString().slice(0,10),close:100+i*.3+Math.sin(i)}));
  const full=charts.calculateIndicators(raw), prefix=charts.calculateIndicators(raw.slice(0,70));
  for(const field of ['dif','dea','macdHistogram','rsi14','bollUpper','bollLower'])assert.equal(prefix[69][field],full[69][field]);
  const panned=charts.prepareSeries(raw,10,20).points;
  assert.equal(panned[0].date,full[70].date);
  for(const field of ['dif','dea','macdHistogram','rsi14','bollUpper','bollLower'])assert.equal(panned[0][field],full[70][field]);
  const weekly=charts.aggregatePeriods(raw,'weekly',{through:'2026-12-31'});
  assert.equal(charts.calculateIndicators(weekly).at(-1).dea,null);
});

const row = (source, symbol, quantity, market = 'a') => ({market, symbol, quantity, source});
const response = (holdings, issues = []) => JSON.stringify({holdings, issues});
test('mixed text preserves A/HK identity and derives share counts from verbatim quantities', () => {
  const text = '600519 100股\n港股 700 1.5万股\n000858，1,000股';
  assert.deepEqual(parsePortfolio(text, '```json\n' + response([row('600519 100股', '600519', '100股'), row('港股 700 1.5万股', '00700', '1.5万股', 'hk'), row('000858，1,000股', '000858', '1,000股')]) + '\n```'), [{market:'a',symbol:'600519',shares:100},{market:'hk',symbol:'00700',shares:15000},{market:'a',symbol:'000858',shares:1000}]);
  assert.equal(parsePortfolio('HK00700 200股', response([row('HK00700 200股', '00700', '200股', 'hk')]))[0].shares, 200);
  assert.deepEqual(parsePortfolio('700 200股\n09988 50股', response([row('700 200股', '00700', '200股', 'hk'), row('09988 50股', '09988', '50股', 'hk')])), [{market:'hk',symbol:'00700',shares:200},{market:'hk',symbol:'09988',shares:50}]);
});
test('invented records, quantities, ambiguous units and duplicate codes never reach storage', () => {
  const text = '600519 100股';
  for (const entry of [row(text, '000858', '100股'), row('600519 999股','600519','999股'), row(text,'600519','999股'), row(text,'600519','600519')]) assert.throws(() => parsePortfolio(text, response([entry])));
  for (const quantity of ['0股','-100股','100.5股','100手','100-200股']) { const source = '600519 '+quantity; assert.throws(()=>parsePortfolio(source,response([row(source,'600519',quantity)]))); }
  assert.throws(()=>parsePortfolio('港股 600519 200股',response([row('港股 600519 200股','600519','200股','a')])), /市场/);
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
