import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, request as httpRequest } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { createFinanceServer, listen } from '../lib/server.mjs';

async function fixture(t, research = async () => ({ text: '工具核对完成', tools: ['get_strategy_checkpoints'] }), {network = null, listenHost = '127.0.0.1', preferredPort = 0, dataHandler} = {}) {
  await mkdir('.test-tmp', { recursive: true }); const root = await mkdtemp(join(process.cwd(), '.test-tmp/web-'));
  await mkdir(join(root, 'web')); await writeFile(join(root, 'web/home.html'), '<h1>知衡</h1>');
  const backend = createServer(async (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (dataHandler && await dataHandler(req, res)) return;
    if (req.url === '/v1/daily-report?market=a&session=morning&candidate_limit=5') return res.end(JSON.stringify({ is_trading_day: true, as_of: '2026-10-16', markdown: '# 原始日报\n工具证据' }));
    if (req.url === '/v1/daily-report?market=a&session=manual&candidate_limit=5') return res.end(JSON.stringify({ is_trading_day: true, as_of: '2026-10-16', report_id: '2026-10-16-a-manual-112233', markdown: '# 手动报告\n工具证据' }));
    if (req.url.startsWith('/v1/daily-report')) return res.end(JSON.stringify({ is_trading_day: false }));
    res.end(JSON.stringify({ path: req.url, origin: req.headers.origin || null }));
  });
  const backendPort = await listen(backend, 0), url = `http://127.0.0.1:${backendPort}`;
  const site = await createFinanceServer({ root, dataUrl: url, reportsUrl: url, research, network });
  const port = await listen(site.server, preferredPort, listenHost), base = `http://127.0.0.1:${port}`;
  t.after(async () => { await site.close(); backend.closeAllConnections(); await new Promise(done => backend.close(done)); await rm(root, { recursive: true, force: true }); });
  return { root, site, base };
}
async function finished(base, id) {
  for (let i = 0; i < 100; i++) { const job = (await (await fetch(base + '/api/jobs')).json()).jobs.find(j => j.id === id); if (job.status !== 'running') return job; await new Promise(done => setTimeout(done, 10)); }
  throw Error('job did not finish');
}
function requestWithHost(base, path, {body, ...options} = {}) {
  return new Promise((resolve, reject) => {
    const request = httpRequest(new URL(path, base), options, response => {
      const chunks = []; response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => resolve({status: response.statusCode, json: async () => JSON.parse(Buffer.concat(chunks).toString())}));
    });
    request.on('error', reject); request.end(body);
  });
}
test('occupied preferred port falls back to an actual available listener', async t => {
  const occupied = createServer(), other = createServer();
  t.after(() => { occupied.close(); other.close(); });
  const a = await listen(occupied, 0), b = await listen(other, a); assert.notEqual(a, b); assert.ok(b > 0);
});
test('home and report subpage route separately, retaining query strings', async t => {
  const { base } = await fixture(t); assert.match(await (await fetch(base)).text(), /知衡/);
  const data = await (await fetch(base + '/reports?report=2026-10-16-a-morning')).json(); assert.equal(data.path, '/index.html?report=2026-10-16-a-morning');
});
test('strategy guide serves every current strategy with its signal and suitable conditions', async t => {
  const {base,root}=await fixture(t);
  for (const file of ['strategy-guide.html','strategy-guide.css']) await writeFile(join(root,'web',file),await readFile(new URL('../web/'+file,import.meta.url)));
  const response=await fetch(base+'/strategy-guide/'); assert.equal(response.status,200);
  const html=await response.text();
  const catalog=await readFile(new URL('../scripts/strategy_catalog.py',import.meta.url),'utf8');
  const names=[...catalog.matchAll(/^    "([a-z_]+)": \{"name":/gm)].map(match=>match[1]);
  const cards=[...html.matchAll(/<article class="guide-card" id="([a-z_]+)"/g)].map(match=>match[1]);
  assert.equal(names.length,10); assert.deepEqual(new Set(cards),new Set(names));
  assert.equal((html.match(/可考虑的股票情形/g)||[]).length,10);
  assert.equal((html.match(/要避开的情形/g)||[]).length,10);
  assert.equal((html.match(/网页默认信号/g)||[]).length,10);
  assert.equal((await fetch(base+'/strategy-guide.css')).status,200);
});
test('LAN URL uses the selected Ethernet address and actual fallback port; hosts and origins stay restricted', async t => {
  const occupied = createServer(); const occupiedPort = await listen(occupied, 0); t.after(() => occupied.close());
  const {base} = await fixture(t, undefined, {network: {adapter: '以太网', address: '192.168.10.42'}, listenHost: '0.0.0.0', preferredPort: occupiedPort});
  const port = new URL(base).port, host = `192.168.10.42:${port}`;
  assert.notEqual(Number(port), occupiedPort);
  const network = await (await fetch(base + '/api/network')).json();
  assert.deepEqual(network, {localUrl: base, lanUrl: `http://${host}`, adapter: '以太网'});
  assert.equal((await requestWithHost(base, '/reports', {headers: {Host: host}})).status, 200);
  assert.equal((await requestWithHost(base, '/api/status', {headers: {Host: `192.168.146.1:${port}`}})).status, 403);
  assert.equal((await requestWithHost(base, '/api/status', {headers: {Host: `192.168.10.42:${occupiedPort}`}})).status, 403);
  const write = {method: 'POST', headers: {Host: host, Origin: `http://${host}`}, body: JSON.stringify({kind: 'research', prompt: '检查同源研究'})};
  const job = await requestWithHost(base, '/api/jobs', write); assert.equal(job.status, 202); await finished(base, (await job.json()).id);
  write.headers.Origin = 'http://evil.example'; assert.equal((await requestWithHost(base, '/api/jobs', write)).status, 403);
});
test('no physical Ethernet reports no LAN URL instead of advertising a virtual adapter', async t => {
  const {base} = await fixture(t);
  assert.equal((await (await fetch(base + '/api/network')).json()).lanUrl, null);
});
test('cross-origin job submission and invalid prompts never invoke the agent', async t => {
  let called = 0; const { base } = await fixture(t, async () => { called++; });
  const options = { method: 'POST', headers: { Origin: 'https://evil.example', 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: 'research', prompt: 'hello' }) };
  assert.equal((await fetch(base + '/api/jobs', options)).status, 403);
  delete options.headers.Origin; options.body = JSON.stringify({ kind: 'research', prompt: '' });
  assert.equal((await fetch(base + '/api/jobs', options)).status, 400); assert.equal(called, 0);
  assert.equal((await fetch(base + '//external.example/path')).status, 400);
});
test('research completes through DSH backend and retains tool attribution', async t => {
  const { base, root } = await fixture(t);
  const response = await fetch(base + '/api/jobs', { method: 'POST', body: JSON.stringify({ kind: 'research', prompt: '核对策略' }) }); assert.equal(response.status, 202);
  const job = await finished(base, (await response.json()).id); assert.equal(job.status, 'completed'); assert.deepEqual(job.result.tools, ['get_strategy_checkpoints']);
  assert.equal((await (await fetch(base + '/api/status')).json()).managedBy, 'dsh');
});

test('deleting completed research persists removal while running jobs and foreign origins are refused', async t => {
  const {base,root} = await fixture(t, async (_prompt,{signal}) => {
    await new Promise((resolve,reject) => signal.addEventListener('abort',()=>reject(Error('已取消')),{once:true}));
  });
  const running = await (await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify({kind:'research',prompt:'正在研究'})})).json();
  assert.equal((await fetch(base+`/api/jobs/${running.id}`,{method:'DELETE'})).status,409);
  assert.equal((await fetch(base+`/api/jobs/${running.id}`,{method:'DELETE',headers:{Origin:'https://evil.example'}})).status,403);
  await fetch(base+`/api/jobs/${running.id}/cancel`,{method:'POST'}); await finished(base,running.id);
  assert.equal((await fetch(base+`/api/jobs/${running.id}`,{method:'DELETE'})).status,200);
  assert.deepEqual((await (await fetch(base+'/api/jobs')).json()).jobs,[]);
  assert.deepEqual(JSON.parse(await readFile(join(root,'data/web/jobs.json'),'utf8')),[]);
  assert.equal((await fetch(base+`/api/jobs/${running.id}`,{method:'DELETE'})).status,404);
});

test('simulation page and API copy the saved market positions into a separate persistent ledger', async t => {
  const snapshots=[];
  const {base,root}=await fixture(t,undefined,{dataHandler:async(req,res)=>{
    if(req.url==='/v1/portfolio') {res.end(JSON.stringify({holdings:[{symbol:'600001',shares:100},{market:'hk',symbol:'00700',shares:200}]}));return true;}
    if(req.url==='/api/bar-strategies?market=a&timeframe=daily') {res.end(JSON.stringify({strategies:{momentum:{name:'日线动量'},macd:{name:'MACD'},'xgboost@unverified':{name:'未验证模型',checkpoint_id:'unverified',validation:{passed:false}}}}));return true;}
    if(req.url!=='/v1/paper-snapshot') return false;
    let body='';for await(const chunk of req) body+=chunk;
    const input=JSON.parse(body);snapshots.push(input);
    res.end(JSON.stringify({day:'2026-11-04',marketOpen:false,strategyName:'日线动量',quotes:input.symbols.map(symbol=>({symbol,price:10,as_of:new Date().toISOString()}))}));return true;
  }});
  for(const file of ['simulation.html','simulation.js','simulation.css']) {
    await writeFile(join(root,'web',file),await readFile(new URL('../web/'+file,import.meta.url)));
    assert.equal((await fetch(base+'/'+file)).status,200);
  }
  assert.match(await (await fetch(base+'/simulation/')).text(),/当前模拟持仓/);
  const options={method:'POST',body:JSON.stringify({market:'a',strategy:'momentum',initialCash:100,symbols:['UNRELATED']})};
  assert.equal((await fetch(base+'/api/simulations',{...options,headers:{Origin:'https://evil.example'}})).status,403);
  const response=await fetch(base+'/api/simulations',options); assert.equal(response.status,201);
  const row=await response.json();assert.equal(row.initialEquity,1100);assert.equal(row.positions[0].shares,100);
  assert.deepEqual(snapshots,[{market:'a',strategy:'momentum',symbols:['600001'],requiredSymbols:['600001']}]);
  assert.equal((await fetch(base+'/api/simulations',options)).status,409);
  assert.equal((await (await fetch(base+'/api/simulations')).json()).simulations[0].id,row.id);
  const paused=await (await fetch(base+`/api/simulations/${row.id}/pause`,{method:'POST'})).json(); assert.equal(paused.status,'paused');
  assert.equal((await fetch(base+`/api/simulations/${row.id}/pause`,{method:'POST'})).status,409);
  const change = strategy => fetch(base+`/api/simulations/${row.id}/strategy`,{method:'POST',body:JSON.stringify({strategy})});
  assert.equal((await change('not_a_strategy')).status,400);
  assert.equal((await change('xgboost@unverified')).status,400);
  assert.equal((await change('momentum')).status,409);
  const switched=await change('macd'); assert.equal(switched.status,200);
  const switchedRow=await switched.json(); assert.equal(switchedRow.strategy,'macd'); assert.equal(switchedRow.strategyName,'MACD');
  assert.equal(switchedRow.initialEquity,row.initialEquity); assert.deepEqual(switchedRow.positions,row.positions);
  assert.deepEqual(switchedRow.history,row.history); assert.equal(switchedRow.strategyChanges.length,1);
  assert.equal((await fetch(base+`/api/simulations/${row.id}/strategy`,{method:'POST',headers:{Origin:'https://evil.example'},body:JSON.stringify({strategy:'momentum'})})).status,403);
  assert.equal((await (await fetch(base+`/api/simulations/${row.id}/resume`,{method:'POST'})).json()).status,'running');
  assert.equal((await change('momentum')).status,409);
  assert.equal(JSON.parse(await readFile(join(root,'data/web/paper-trading.json'),'utf8'))[0].strategy,'macd');
  assert.deepEqual(await (await fetch(base+`/api/simulations/${row.id}/stop`,{method:'POST'})).json(),{id:row.id,deleted:true});
  assert.deepEqual(JSON.parse(await readFile(join(root,'data/web/paper-trading.json'),'utf8')),[]);
  assert.equal((await fetch(base+'/api/simulations/unknown/stop',{method:'POST'})).status,404);
  const modelResponse=await fetch(base+'/api/simulations',{method:'POST',body:JSON.stringify({market:'a',strategy:'xgboost@active-model'})});
  assert.equal(modelResponse.status,201);
  assert.equal((await fetch(base+'/api/bar-models/a/daily/active-model',{method:'DELETE'})).status,409);
  const modelRow=await modelResponse.json();
  assert.equal((await fetch(base+`/api/simulations/${modelRow.id}/pause`,{method:'POST'})).status,200);
  assert.equal((await fetch(base+'/api/bar-models/a/daily/active-model',{method:'DELETE'})).status,409);
});
test('simulation API accepts new-stock opt-in and cash contribution on resume', async t => {
  const snapshots=[];
  const {base,root}=await fixture(t,undefined,{dataHandler:async(req,res)=>{
    if(req.url==='/v1/portfolio') {res.end(JSON.stringify({holdings:[{market:'a',symbol:'600001',shares:100}]}));return true;}
    if(req.url==='/v1/candidate-ranking?limit=20&market=a') {res.end(JSON.stringify({ranking:[{symbol:'600002',name:'候选股'}]}));return true;}
    if(req.url==='/v1/paper-snapshot') {let data='';for await(const chunk of req)data+=chunk;const input=JSON.parse(data);snapshots.push(input);res.end(JSON.stringify({day:'2026-11-04',marketOpen:false,strategyName:'动量',quotes:input.symbols.map(symbol=>({symbol,price:10,as_of:new Date().toISOString()}))}));return true;}
    return false;
  }});
  const startedResponse=await fetch(base+'/api/simulations',{method:'POST',body:JSON.stringify({market:'a',strategy:'momentum',allowNewStocks:true})});
  assert.equal(startedResponse.status,201);
  const started=await startedResponse.json();
  assert.equal(started.allowNewStocks,true);assert.deepEqual(snapshots[0].symbols,['600001','600002']);
  assert.deepEqual(snapshots[0].requiredSymbols,['600001']);
  assert.equal(started.initialEquity,1000);
  await fetch(base+`/api/simulations/${started.id}/pause`,{method:'POST'});
  assert.equal((await fetch(base+`/api/simulations/${started.id}/resume`,{method:'POST',body:JSON.stringify({addedCash:-1})})).status,400);
  const resumedResponse=await fetch(base+`/api/simulations/${started.id}/resume`,{method:'POST',body:JSON.stringify({addedCash:100000,allowNewStocks:false})});
  assert.equal(resumedResponse.status,200);
  const resumed=await resumedResponse.json();assert.equal(resumed.cash,100000);
  assert.equal(resumed.equity,101000);assert.equal(resumed.profit,0);assert.equal(resumed.addedCapital,100000);
  assert.equal(resumed.allowNewStocks,false);assert.equal(resumed.capitalEvents.length,1);
  assert.equal(JSON.parse(await readFile(join(root,'data/web/paper-trading.json'),'utf8'))[0].addedCapital,100000);
});
test('AI simulation analyzes each saved holding with DSH, marks a supported strategy, and saves its rationale with the ledger', async t => {
  let saved=[{market:'a',symbol:'600001',name:'甲',shares:100},{market:'a',symbol:'600002',name:'乙',shares:200}], called=0;
  const research=async (prompt, options) => {
    called++; assert.equal(options.purpose,'ai_simulation'); assert.deepEqual(options.allowedTools,[]);
    assert.match(prompt,/600001/); assert.match(prompt,/600002/); assert.match(prompt,/return20Pct/);
    return {text:JSON.stringify({strategy:'momentum',summary:'两只股票近期走强。',reason:'动量规则与近期价格变化更匹配。',limits:'仅比较当前可用策略，未证明收益最优。',stocks:saved.map(row=>({symbol:row.symbol,observation:'近 20 根日线呈上涨。',fit:'适合观察动量信号。',risk:'短期波动可能反转。'}))}),provider:'codex',model:'mock-model'};
  };
  const {base,root}=await fixture(t,research,{dataHandler:async(req,res)=>{
    if(req.url==='/v1/portfolio') {res.end(JSON.stringify({holdings:saved}));return true;}
    if(req.url==='/api/bar-strategies?market=a&timeframe=daily') {res.end(JSON.stringify({strategies:{momentum:{name:'日线动量'},macd:{name:'MACD'}}}));return true;}
    if(req.url==='/v1/portfolio-quotes?market=a') {res.end(JSON.stringify({quotes:saved.map(row=>({symbol:row.symbol,price:10,as_of:new Date().toISOString()}))}));return true;}
    if(req.url.startsWith('/v1/portfolio-history?')) {const symbol=new URL(req.url,'http://localhost').searchParams.get('symbol');res.end(JSON.stringify({as_of:'2026-11-03',source:'test',points:Array.from({length:30},(_,i)=>({date:`2026-10-${String(i+1).padStart(2,'0')}`,open:10+i/10,high:11+i/10,low:9+i/10,close:10+i/10,volume:1000}))}));return true;}
    if(req.url==='/v1/paper-snapshot') {let data='';for await(const chunk of req)data+=chunk;const input=JSON.parse(data);res.end(JSON.stringify({day:'2026-11-04',marketOpen:false,strategyName:'日线动量',quotes:input.symbols.map(symbol=>({symbol,price:10,as_of:new Date().toISOString()}))}));return true;}
    return false;
  }});
  const jobResponse=await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify({kind:'ai_simulation',market:'a'})});assert.equal(jobResponse.status,202);
  const job=await finished(base,(await jobResponse.json()).id);assert.equal(job.status,'completed');assert.equal(called,1);
  assert.equal(job.result.recommendation.strategy,'momentum');assert.equal(job.result.recommendation.stocks.length,2);
  assert.equal((await fetch(base+'/api/simulations',{method:'POST',body:JSON.stringify({market:'a',strategy:'macd',recommendationJobId:job.id})})).status,409);
  saved=[...saved,{market:'a',symbol:'600003',name:'丙',shares:100}];
  assert.equal((await fetch(base+'/api/simulations',{method:'POST',body:JSON.stringify({market:'a',strategy:'momentum',recommendationJobId:job.id})})).status,409);
  saved=saved.slice(0,2);
  const response=await fetch(base+'/api/simulations',{method:'POST',body:JSON.stringify({market:'a',strategy:'momentum',recommendationJobId:job.id})});assert.equal(response.status,201);
  const simulation=await response.json();assert.equal(simulation.aiRecommendation.stocks.length,2);
  assert.equal(JSON.parse(await readFile(join(root,'data/web/paper-trading.json'),'utf8'))[0].aiRecommendation.strategy,'momentum');
});
test('strategy task settings remain inspectable without retaining unrelated request fields', async t => {
  const { base } = await fixture(t);
  const configuration = {strategy: 'elastic_net', start: '2025-10-01', end: '2026-06-01', slot: '14:00', parameters: {alpha_scale: .01}};
  const response = await fetch(base + '/api/jobs', {method: 'POST', body: JSON.stringify({kind: 'train', ...configuration, unrelated: 'do not persist'})});
  assert.equal(response.status, 202);
  const job = await response.json(); assert.deepEqual(job.configuration, configuration);
  const saved = (await (await fetch(base + '/api/jobs')).json()).jobs.find(item => item.id === job.id);
  assert.deepEqual(saved.configuration, configuration); assert.equal(saved.unrelated, undefined);
});
test('A-share pool choices reject unsupported task and market combinations', async t => {
  const {base} = await fixture(t, undefined, {dataHandler: async (req, res) => {
    if (!req.url.startsWith('/api/bar-strategies')) return false;
    res.end(JSON.stringify({strategies:{momentum:{}}, training:{elastic_net:{}}})); return true;
  }});
  for (const input of [
    {kind:'backtest',market:'a',pool:'all',strategy:'momentum',start:'2025-01-01',end:'2025-12-31'},
    {kind:'backtest',market:'hk',pool:'csi300',strategy:'momentum',start:'2025-01-01',end:'2025-12-31'},
    {kind:'train',market:'a',pool:'all',strategy:'elastic_net',start:'2025-01-01',end:'2025-12-31'}
  ]) {
    const response = await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify(input)});
    const job = await finished(base,(await response.json()).id);
    assert.equal(job.status,'failed'); assert.match(job.error,/不支持所选股票池/);
  }
});
test('A-share training accepts index pool without custom symbols', async t => {
  const {base, root} = await fixture(t, undefined, {dataHandler: async (req, res) => {
    if (!req.url.startsWith('/api/bar-strategies')) return false;
    res.end(JSON.stringify({strategies:{}, training:{elastic_net:{}}})); return true;
  }});
  await mkdir(join(root,'scripts'));
  await writeFile(join(root,'scripts/bar_strategy.py'), "import json,sys\nprint(json.dumps({'pool':sys.argv[sys.argv.index('--pool')+1], 'symbols_supplied':'--symbols' in sys.argv}))\n");
  const response = await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify({kind:'train',market:'a',pool:'csi500',strategy:'elastic_net',start:'2025-01-01',end:'2025-12-31'})});
  const job = await finished(base,(await response.json()).id);
  assert.equal(job.status,'completed');
  assert.deepEqual(job.result.data,{pool:'csi500',symbols_supplied:false});
  assert.equal(job.configuration.pool,'csi500');
});
test('deployment refuses a model without a passing validation gate before running Python', async t => {
  const {base, root} = await fixture(t, undefined, {dataHandler: async (req, res) => {
    if (!req.url.startsWith('/api/bar-strategies')) return false;
    res.end(JSON.stringify({strategies:{'xgboost@legacy':{checkpoint_id:'legacy',validation:{passed:false}},momentum:{}},training:{}})); return true;
  }});
  const response = await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify({kind:'deploy',market:'a',pool:'csi300',strategy:'xgboost@legacy'})});
  const job = await finished(base,(await response.json()).id);
  assert.equal(job.status,'failed'); assert.match(job.error,/不能部署/);
  await assert.rejects(readFile(join(root,'data/bar-deployments','latest-a-daily-xgboost-legacy-csi300.json')),{code:'ENOENT'});
});

test('holding page and its assets are served independently of the custom backtest page', async t => {
  const {base, root} = await fixture(t);
  for (const file of ['portfolio.html', 'portfolio.js', 'portfolio-analysis.js', 'portfolio-charts.js', 'portfolio.css', 'dark-ui.css']) {
    await writeFile(join(root, 'web', file), await readFile(new URL('../web/' + file, import.meta.url)));
    const response = await fetch(base + '/' + file); assert.equal(response.status, 200);
  }
  for (const path of ['/portfolio', '/portfolio/?market=hk']) {
    const html = await (await fetch(base + path)).text();
    assert.match(html, /我的持仓/); assert.match(html, /portfolioTaskForm/); assert.doesNotMatch(html, /src="\/app.js"/);
  }
  const page = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
  assert.match(page, /href="\/portfolio"/); assert.doesNotMatch(page, /id="portfolioTools"|id="portfolioEntryForm"/);
  for (const file of ['home.html', 'index.html', 'backtests.html', 'portfolio.html']) {
    const html = await readFile(new URL('../web/' + file, import.meta.url), 'utf8');
    assert.ok(html.indexOf('href="/dark-ui.css"') > html.indexOf('href="/soft-ui.css"'), `${file} loads the default dark theme after shared styles`);
  }
});

test('holding pool tasks resolve a market-specific server snapshot and ignore submitted symbols', async t => {
  let saved = [{symbol:'600519',shares:100}, {market:'hk',symbol:'00700',shares:200}, {market:'us',symbol:'AAPL',shares:1.5}];
  const {base, root} = await fixture(t, undefined, {dataHandler: async (req, res) => {
    if (req.url === '/v1/portfolio') { res.end(JSON.stringify({holdings:saved})); return true; }
    if (!req.url.startsWith('/api/bar-strategies')) return false;
    res.end(JSON.stringify({strategies:{momentum:{}},training:{}})); return true;
  }});
  await mkdir(join(root,'scripts'));
  await writeFile(join(root,'scripts/bar_strategy.py'), "import json,sys\nargs=sys.argv\nprint(json.dumps({'symbols':args[args.index('--symbols')+1], 'pool':args[args.index('--pool')+1], 'as_of':args[args.index('--pool-as-of')+1]}))\n");
  for (const [market, symbol, kind] of [['a','600519','backtest'], ['hk','00700','deploy'], ['us','AAPL','backtest']]) {
    const response = await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify({kind,market,pool:'portfolio',symbols:['UNRELATED'],strategy:'momentum',start:'2025-01-01',end:'2025-12-31'})});
    const job = await finished(base,(await response.json()).id);
    assert.equal(job.status,'completed', job.error); assert.equal(job.result.data.symbols,symbol); assert.equal(job.result.data.pool,'portfolio');
    assert.deepEqual(job.configuration.symbols,[symbol]); assert.equal(job.configuration.poolAsOf,job.result.data.as_of);
    assert.match(job.title,/持仓股票池/);
  }
  saved = [{market:'us',symbol:'MSFT',shares:4}];
  const response = await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify({kind:'deploy',market:'us',pool:'portfolio',strategy:'momentum'})});
  const job = await finished(base,(await response.json()).id); assert.deepEqual(job.configuration.symbols,['MSFT']);
});

test('all workspace pages share navigation order and identify their current page', async () => {
  const expected = [{href:'/',label:'研究'},{href:'/reports',label:'日报'},{href:'/portfolio',label:'持仓'},{href:'/backtests',label:'回测'},{href:'/simulation',label:'模拟'},{href:'/strategy-guide',label:'策略解读'},{href:'/settings',label:'设置'}];
  for (const [file, current] of [['home.html','/'],['index.html','/reports'],['portfolio.html','/portfolio'],['backtests.html','/backtests'],['simulation.html','/simulation'],['strategy-guide.html','/strategy-guide'],['settings.html','/settings']]) {
    const html = await readFile(new URL('../web/' + file, import.meta.url), 'utf8');
    const navigation = html.match(/<nav class="workspace-navigation" aria-label="工作台导航">([\s\S]*?)<\/nav>/)?.[1];
    assert.ok(navigation, `${file} has the shared workspace navigation`);
    assert.match(html, /src="\/theme.js"/);
    assert.match(html, /href="\/theme.css"/);
    const links = [...navigation.matchAll(/<a href="([^"]+)"([^>]*)>([^<]+)<\/a>/g)];
    assert.deepEqual(links.map(match=>({href:match[1],label:match[3]})),expected,file);
    assert.deepEqual(links.filter(match=>match[2].includes('aria-current="page"')).map(match=>match[1]),[current],file);
  }
});

test('empty holdings and holding-pool training fail before launching a strategy process', async t => {
  const {base} = await fixture(t, undefined, {dataHandler: async (req,res) => {
    if (req.url === '/v1/portfolio') {res.end(JSON.stringify({holdings:[]})); return true;}
    if (!req.url.startsWith('/api/bar-strategies')) return false;
    res.end(JSON.stringify({strategies:{momentum:{}},training:{elastic_net:{}}})); return true;
  }});
  for (const [kind,strategy,message] of [['deploy','momentum',/没有已保存持仓/],['train','elastic_net',/不支持所选股票池/]]) {
    const response = await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify({kind,strategy,pool:'portfolio',start:'2025-01-01',end:'2025-12-31'})});
    const job = await finished(base,(await response.json()).id); assert.equal(job.status,'failed'); assert.match(job.error,message);
  }
});

test('holding quote endpoint validates the market before forwarding', async t => {
  const paths = []; const {base} = await fixture(t,undefined,{dataHandler:async(req,res)=>{
    if (!req.url.startsWith('/v1/portfolio-quotes')) return false;
    paths.push(req.url); res.end(JSON.stringify({market:'hk',quotes:[]})); return true;
  }});
  assert.equal((await fetch(base+'/api/portfolio-quotes?market=invalid')).status,400);
  assert.equal((await fetch(base+'/api/portfolio-quotes?market=hk')).status,200);
  assert.deepEqual(paths,['/v1/portfolio-quotes?market=hk']);
});

test('holding history validates identities and forwards the exact selected market and symbol', async t => {
  const paths=[]; const {base}=await fixture(t,undefined,{dataHandler:async(req,res)=>{
    if(!req.url.startsWith('/v1/portfolio-history'))return false;
    paths.push(req.url);res.end(JSON.stringify({available:true,points:[]}));return true;
  }});
  for(const query of ['market=invalid&symbol=AAPL','market=a&symbol=00700','market=us&symbol=../secret','market=a&symbol=600519&years=20'])assert.equal((await fetch(base+'/api/portfolio-history?'+query)).status,400);
  assert.equal((await fetch(base+'/api/portfolio-history?market=hk&symbol=00700')).status,200);
  assert.deepEqual(paths,['/v1/portfolio-history?market=hk&symbol=00700']);
  assert.equal((await fetch(base+'/api/portfolio-history?market=a&symbol=600519&years=5')).status,200);
  assert.equal(paths[1],'/v1/portfolio-history?market=a&symbol=600519&years=5');
});

test('report stock history is a separate read-only route for stocks outside the holding pool', async t => {
  const paths = []; const {base} = await fixture(t, undefined, {dataHandler: async (req, res) => {
    if (!req.url.startsWith('/v1/stock-history')) return false;
    paths.push(req.url); res.end(JSON.stringify({available: true, symbol: '002946', market: 'a', points: []})); return true;
  }});
  for (const query of ['market=a&symbol=../../secret', 'market=a&symbol=00700', 'market=us&symbol=<script>', 'market=a&symbol=002946&years=100']) {
    assert.equal((await fetch(base + '/api/stock-history?' + query)).status, 400);
  }
  assert.equal((await fetch(base + '/api/stock-history?market=a&symbol=002946&years=5')).status, 200);
  assert.deepEqual(paths, ['/v1/stock-history?market=a&symbol=002946&years=5']);
});
test('trained model deletion removes only a verified checkpoint and preserves backtest archives', async t => {
  const {base, root} = await fixture(t);
  const id = 'a-daily-elastic_net-20261102T194145-ab12cd34';
  const directory = join(root,'data/bar-models/a/daily',id);
  await mkdir(directory,{recursive:true});
  const weights = Buffer.from('{"coef":[0.1]}');
  await writeFile(join(directory,'weights.json'),weights);
  await writeFile(join(directory,'checkpoint.json'),JSON.stringify({market:'a',timeframe:'daily',model_id:id,algorithm:'elastic_net',artifact_sha256:createHash('sha256').update(weights).digest('hex')}));
  const archive = join(root,'data/factor-lab/backtests','prior','summary.json');
  await mkdir(join(root,'data/factor-lab/backtests','prior'),{recursive:true}); await writeFile(archive,'{}');
  const path = `/api/bar-models/a/daily/${id}`;
  assert.equal((await fetch(base+path,{method:'DELETE',headers:{Origin:'https://other.example'}})).status,403);
  assert.equal((await fetch(base+'/api/bar-models/a/daily/../bad',{method:'DELETE'})).status,400);
  assert.equal((await fetch(base+path,{method:'DELETE',headers:{Origin:base}})).status,200);
  await assert.rejects(readFile(join(directory,'checkpoint.json')),{code:'ENOENT'});
  assert.equal(await readFile(archive,'utf8'),'{}');
  assert.equal((await fetch(base+path,{method:'DELETE'})).status,404);
});
test('model deletion refuses malformed or extra files', async t => {
  const {base, root} = await fixture(t);
  const id = 'a-daily-xgboost-20261102T194145-ab12cd34';
  const directory = join(root,'data/bar-models/a/daily',id);
  await mkdir(directory,{recursive:true});
  await writeFile(join(directory,'weights.json'),'weights');
  await writeFile(join(directory,'checkpoint.json'),JSON.stringify({market:'a',timeframe:'daily',model_id:id,algorithm:'xgboost',artifact_sha256:'wrong'}));
  const path = `/api/bar-models/a/daily/${id}`;
  assert.equal((await fetch(base+path,{method:'DELETE'})).status,409);
  await writeFile(join(directory,'checkpoint.json'),JSON.stringify({market:'a',timeframe:'daily',model_id:id,algorithm:'xgboost',artifact_sha256:createHash('sha256').update('weights').digest('hex')}));
  await writeFile(join(directory,'notes.txt'),'keep');
  assert.equal((await fetch(base+path,{method:'DELETE'})).status,409);
  assert.equal(await readFile(join(directory,'notes.txt'),'utf8'),'keep');
});
test('model deletion waits while a strategy task is running', async t => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const {base, site} = await fixture(t, undefined, {dataHandler: async (req, res) => {
    if (!req.url.startsWith('/api/bar-strategies')) return false;
    await gate; res.end(JSON.stringify({strategies:{},training:{elastic_net:{}}})); return true;
  }});
  const job = site.startJob({kind:'train',market:'a',pool:'csi300',strategy:'elastic_net',start:'2025-01-01',end:'2025-12-31'});
  const response = await fetch(base+'/api/bar-models/a/daily/a-daily-elastic_net-20261102T120000-test',{method:'DELETE'});
  assert.equal(response.status,409);
  await fetch(base+`/api/jobs/${job.id}/cancel`,{method:'POST'});
  release(); await finished(base,job.id);
});
test('text portfolio job uses DSH, resolves metadata and saves mixed markets through the data service', async t => {
  let saved = [{market:'a',symbol:'000858',shares:50}], imports = 0;
  const {base} = await fixture(t, async (_prompt, options) => {
    assert.deepEqual(options.allowedTools,['stock_search']); assert.equal(options.purpose,'portfolio');
    return {text:JSON.stringify({holdings:[{market:'a',symbol:'600519',quantity:'100股',source:'600519 100股'},{market:'hk',symbol:'00700',quantity:'200股',source:'港股 00700 200股'}],issues:[]}),model:'host-model',tools:[]};
  }, {dataHandler:async(req,res)=>{
    if (req.url === '/v1/portfolio') {res.end(JSON.stringify({holdings:saved}));return true;}
    if (!req.url.startsWith('/v1/portfolio/')) return false;
    const chunks=[];for await(const chunk of req) chunks.push(chunk); const payload=JSON.parse(Buffer.concat(chunks));
    if (req.url.endsWith('/resolve')) res.end(JSON.stringify({holdings:payload.holdings.map(row=>({...row,name:row.market==='hk'?'腾讯控股':'贵州茅台'})),unavailable:[]}));
    else {imports++; saved=[...saved,...payload.holdings];res.end(JSON.stringify({holdings:saved,added:2,updated:0}));}
    return true;
  }});
  const response=await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify({kind:'portfolio',text:'600519 100股\n港股 00700 200股'})});
  assert.equal(response.status,202); const job=await finished(base,(await response.json()).id);
  assert.equal(job.status,'completed'); assert.equal(job.result.model,'host-model'); assert.equal(imports,1);
  const current=await(await fetch(base+'/api/portfolio')).json(); assert.equal(current.holdings.length,3);assert.equal(current.holdings.at(-1).market,'hk');
  const preview=await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify({kind:'portfolio',text:'600519 100股\n港股 00700 200股',preview:true})});
  const previewJob=await finished(base,(await preview.json()).id);assert.equal(previewJob.result.preview,true);assert.equal(imports,1);
});
test('image jobs accept screenshot-sized uploads, stay preview-only and omit image data from saved jobs', async t => {
  let calls = 0, imagePath;
  const bytes = Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]), Buffer.alloc(30000)]);
  const image = 'data:image/png;base64,' + bytes.toString('base64');
  const {base, root} = await fixture(t, async (_prompt, options) => {
    calls++; imagePath=options.images[0];
    assert.deepEqual(await readFile(imagePath), bytes); assert.deepEqual(options.allowedTools, []);
    return {text:JSON.stringify({text:'002297 博云新材\n000066 中国长城',issues:[]})};
  }, {dataHandler:async(req)=>{if(req.url.startsWith('/v1/portfolio/')) throw Error('Image preview must not touch stored holdings'); return false;}});
  const response=await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify({kind:'portfolio',image,preview:false})});
  assert.equal(response.status,202); const job=await finished(base,(await response.json()).id);
  assert.equal(job.status,'completed'); assert.equal(job.result.preview,true);assert.match(job.result.recognizedText,/002297/);
  await assert.rejects(readFile(imagePath),{code:'ENOENT'});
  const persisted=await readFile(join(root,'data/web/jobs.json'),'utf8');assert.doesNotMatch(persisted,/data:image|base64/);
  for (const invalid of [image.replace('image/png','image/jpeg'), 'data:image/svg+xml;base64,PHN2Zz4=', {}]) {
    assert.equal((await fetch(base+'/api/jobs',{method:'POST',body:JSON.stringify({kind:'portfolio',image:invalid})})).status,400);
  }
  assert.equal((await fetch(base+'/api/jobs',{method:'POST',headers:{Origin:'https://foreign.example'},body:JSON.stringify({kind:'portfolio',image})})).status,403);
  assert.equal(calls,1);
});

test('saved portfolio edits and deletions route exact market identities without invoking DSH', async t => {
  const calls = []; let agentCalls = 0;
  const {base} = await fixture(t, async () => {agentCalls++;}, {dataHandler: async (req,res) => {
    if (!req.url.startsWith('/v1/portfolio/')) return false;
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    calls.push({path:req.url,method:req.method,payload:JSON.parse(Buffer.concat(chunks))});
    res.end(JSON.stringify({saved:true,holdings:[]})); return true;
  }});
  for (const [market,symbol] of [['a','000001'],['hk','00001']]) {
    const response = await fetch(`${base}/api/portfolio/${market}/${symbol}`, {method:'PATCH',headers:{Origin:base},body:JSON.stringify({shares:345,note:'ignore',symbol:'600519'})});
    assert.equal(response.status,200); assert.equal((await response.json()).saved,true);
    assert.equal((await fetch(`${base}/api/portfolio/${market}/${symbol}`, {method:'DELETE',headers:{Origin:base}})).status,200);
  }
  assert.deepEqual(calls, [
    {path:'/v1/portfolio/update',method:'POST',payload:{market:'a',symbol:'000001',shares:345}},
    {path:'/v1/portfolio/delete',method:'POST',payload:{market:'a',symbol:'000001'}},
    {path:'/v1/portfolio/update',method:'POST',payload:{market:'hk',symbol:'00001',shares:345}},
    {path:'/v1/portfolio/delete',method:'POST',payload:{market:'hk',symbol:'00001'}}
  ]);
  assert.equal(agentCalls,0);
});
test('invalid portfolio edits and foreign origins never reach the data service', async t => {
  let calls = 0;
  const {base} = await fixture(t, undefined, {dataHandler:async (_req,res) => {calls++;res.end('{}');return true;}});
  for (const shares of [null,true,0,-1,1.5,'100',1e12+1]) assert.equal((await fetch(base+'/api/portfolio/a/600519',{method:'PATCH',body:JSON.stringify({shares})})).status,400);
  for (const path of ['/hk/600519','/a/000000','/unknown/600519','/a/１２３４５６']) assert.equal((await fetch(base+'/api/portfolio'+path,{method:'DELETE'})).status,400);
  for (const method of ['PATCH','DELETE']) assert.equal((await fetch(base+'/api/portfolio/hk/00700',{method,headers:{Origin:'https://evil.example'},body:method==='PATCH'?'{}':undefined})).status,403);
  assert.equal(calls,0);
});
test('edits wait for running portfolio imports and propagate missing holding errors', async t => {
  let calls = 0;
  const {base,site} = await fixture(t, (_prompt,{signal}) => new Promise((_,reject) => signal.addEventListener('abort',()=>reject(Error('cancelled')),{once:true})), {dataHandler:async (_req,res) => {calls++;res.statusCode=400;res.end(JSON.stringify({error:'这条持仓已不存在，请刷新列表。'}));return true;}});
  const missing = await fetch(base+'/api/portfolio/hk/00700',{method:'DELETE'});
  assert.equal(missing.status,400); assert.match((await missing.json()).error,/已不存在/);
  const job = site.startJob({kind:'portfolio',text:'600519 100股'});
  await new Promise(done => setTimeout(done,20));
  for (const method of ['PATCH','DELETE']) assert.equal((await fetch(base+'/api/portfolio/a/600519',{method,body:method==='PATCH'?JSON.stringify({shares:100}):undefined})).status,409);
  assert.equal(calls,1);
  await fetch(base+`/api/jobs/${job.id}/cancel`,{method:'POST'}); await finished(base,job.id);
});
test('daily generation preserves original evidence, appends agent review and archives only on success', async t => {
  const { base, root, site } = await fixture(t); const job = site.startJob({ kind: 'daily', market: 'a', session: 'morning' }, {scheduled:true});
  const result = await finished(base, job.id); assert.equal(result.status, 'completed');
  assert.equal(result.result.reportId, '2026-10-16-a-scheduled-morning'); assert.equal(result.title, '定时上午日报');
  const text = await readFile(join(root, 'data/reports', result.result.reportId + '.md'), 'utf8'); assert.match(text, /原始日报/); assert.match(text, /DSH 智能体复核/); assert.match(text, /工具核对完成/);
});
test('manual reports need no session and do not replace scheduled reports', async t => {
  const { base, root, site } = await fixture(t);
  const scheduled = await finished(base, site.startJob({kind:'daily', market:'a', session:'morning'}, {scheduled:true}).id);
  const manual = await finished(base, site.startJob({kind:'daily', market:'a'}).id);
  assert.equal(manual.status, 'completed'); assert.equal(manual.title, '手动报告');
  assert.equal(manual.result.reportId, '2026-10-16-a-manual-112233');
  assert.match(await readFile(join(root, 'data/reports', scheduled.result.reportId + '.md'), 'utf8'), /原始日报/);
  assert.match(await readFile(join(root, 'data/reports', manual.result.reportId + '.md'), 'utf8'), /手动报告/);
});
test('non-trading day skips the model and does not create a report', async t => {
  let called = 0; const { base, site } = await fixture(t, async () => { called++; });
  const result = await finished(base, site.startJob({ kind: 'daily', market: 'hk', session: 'afternoon' }, {scheduled:true}).id); assert.equal(result.result.skipped, true); assert.equal(called, 0);
});
test('concurrent jobs are refused and cancellation reaches the model', async t => {
  const { base, site } = await fixture(t, (_prompt, { signal }) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(Error('cancelled')), { once: true })));
  const job = site.startJob({ kind: 'research', prompt: 'pending' }); await new Promise(done => setTimeout(done, 20));
  assert.throws(() => site.startJob({ kind: 'research', prompt: 'second' }), /正在运行/);
  assert.equal((await fetch(base + `/api/jobs/${job.id}/cancel`, { method: 'POST' })).status, 202);
  assert.equal((await finished(base, job.id)).status, 'failed');
});
