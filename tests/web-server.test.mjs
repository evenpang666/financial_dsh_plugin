import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, request as httpRequest } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { createFinanceServer, listen } from '../lib/server.mjs';
import { supportsDsh } from '../scripts/dsh-version.mjs';
import { createResearchAgent } from '../lib/agent.mjs';

async function fixture(t, research = async () => ({ text: '工具核对完成', tools: ['get_strategy_checkpoints'] }), {network = null, listenHost = '127.0.0.1', preferredPort = 0, dataHandler} = {}) {
  await mkdir('.test-tmp', { recursive: true }); const root = await mkdtemp(join(process.cwd(), '.test-tmp/web-'));
  await mkdir(join(root, 'web')); await writeFile(join(root, 'web/home.html'), '<h1>知衡</h1>');
  const backend = createServer(async (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (dataHandler && await dataHandler(req, res)) return;
    if (req.url === '/v1/daily-report?market=a&session=morning&candidate_limit=5') return res.end(JSON.stringify({ is_trading_day: true, markdown: '# 原始日报\n工具证据' }));
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
test('accepts DSH 0.2.0 stable and supported RC while rejecting older versions', () => {
  assert.equal(supportsDsh('0.2.0'), true); assert.equal(supportsDsh('0.2.0-rc.2'), true); assert.equal(supportsDsh('0.1.9'), false);
});
test('occupied preferred port falls back to an actual available listener', async t => {
  const occupied = createServer(), other = createServer();
  t.after(() => { occupied.close(); other.close(); });
  const a = await listen(occupied, 0), b = await listen(other, a); assert.notEqual(a, b); assert.ok(b > 0);
});
test('home and report subpage route separately, retaining query strings', async t => {
  const { base } = await fixture(t); assert.match(await (await fetch(base)).text(), /知衡/);
  const data = await (await fetch(base + '/reports?report=2026-10-16-a-morning')).json(); assert.equal(data.path, '/index.html?report=2026-10-16-a-morning');
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
test('strategy task settings remain inspectable without retaining unrelated request fields', async t => {
  const { base } = await fixture(t);
  const configuration = {strategy: 'elastic_net', start: '2025-10-01', end: '2026-06-01', slot: '14:00', parameters: {alpha_scale: .01}};
  const response = await fetch(base + '/api/jobs', {method: 'POST', body: JSON.stringify({kind: 'train', ...configuration, unrelated: 'do not persist'})});
  assert.equal(response.status, 202);
  const job = await response.json(); assert.deepEqual(job.configuration, configuration);
  const saved = (await (await fetch(base + '/api/jobs')).json()).jobs.find(item => item.id === job.id);
  assert.deepEqual(saved.configuration, configuration); assert.equal(saved.unrelated, undefined);
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
  const { base, root, site } = await fixture(t); const job = site.startJob({ kind: 'daily', market: 'a', session: 'morning' });
  const result = await finished(base, job.id); assert.equal(result.status, 'completed');
  const text = await readFile(join(root, 'data/reports', result.result.reportId + '.md'), 'utf8'); assert.match(text, /原始日报/); assert.match(text, /DSH 智能体复核/); assert.match(text, /工具核对完成/);
});
test('non-trading day skips the model and does not create a report', async t => {
  let called = 0; const { base, site } = await fixture(t, async () => { called++; });
  const result = await finished(base, site.startJob({ kind: 'daily', market: 'hk', session: 'afternoon' }).id); assert.equal(result.result.skipped, true); assert.equal(called, 0);
});
test('concurrent jobs are refused and cancellation reaches the model', async t => {
  const { base, site } = await fixture(t, (_prompt, { signal }) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(Error('cancelled')), { once: true })));
  const job = site.startJob({ kind: 'research', prompt: 'pending' }); await new Promise(done => setTimeout(done, 20));
  assert.throws(() => site.startJob({ kind: 'research', prompt: 'second' }), /正在运行/);
  assert.equal((await fetch(base + `/api/jobs/${job.id}/cancel`, { method: 'POST' })).status, 202);
  assert.equal((await finished(base, job.id)).status, 'failed');
});
test('DSH agent inherits host model, limits tools, flushes and disposes the session', async () => {
  let options, flushed = false, disposed = false, allowed;
  const events = [];
  const session = { seq: 0, eventAt: i => events[i] };
  const agent = { session, whenIdle: async () => {}, cancel() {}, followup() { events.push({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '查证完成' }] } } }, { type: 'turn/end', data: { reason: { kind: 'completed' } } }); session.seq = events.length; } };
  const ctx = { get: () => null, agentDefaultModel: { currentSelection: () => ({ provider: 'host', model: 'configured' }) }, sessions: { flush: async () => { flushed = true; } }, agents: { create: async value => { options = value; value.setup({ tools: { restrict: v => { allowed = v.allow; }, presentAs() {} } }); return { agent, dispose: async () => { disposed = true; } }; } } };
  const result = await createResearchAgent(ctx, process.cwd(), ['get_quote_snapshot'])('问题');
  assert.equal(options.agentOptions.model, 'configured'); assert.deepEqual(allowed, ['get_quote_snapshot']); assert.equal(result.text, '查证完成'); assert.ok(flushed && disposed);
  await createResearchAgent(ctx,process.cwd(),['stock_search','set_portfolio'])('持仓',{purpose:'portfolio',allowedTools:['stock_search']});
  assert.deepEqual(allowed,['stock_search']);
});
