import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdir, mkdtemp, cp, rm, readFile, writeFile, access} from 'node:fs/promises';
import {join, resolve, sep} from 'node:path';
import {pathToFileURL} from 'node:url';
import {pythonPath} from '../lib/workers.mjs';

// Explicit integration command: requires the Python dependencies, but no live DSH
// profile, model credential or market-data request. The Cordis context is a fixture.
test('DSH plugin lifecycle serves the aligned workspace through real Python workers', {timeout: 45000}, async t => {
  const project = resolve('.'), parent = join(project, '.test-tmp'); await mkdir(parent, {recursive: true});
  const root = await mkdtemp(join(parent, 'dsh-service-')), definitions = new Map(), skills = new Map();
  let cleanup;
  const previousLan = process.env.FINANCE_LAN; process.env.FINANCE_LAN = '0';
  t.after(async () => {
    try { await cleanup?.(); }
    finally {
      if (previousLan === undefined) delete process.env.FINANCE_LAN; else process.env.FINANCE_LAN = previousLan;
      assert.ok(root.startsWith(parent + sep)); await rm(root, {recursive: true, force: true, maxRetries: 10, retryDelay: 100});
    }
  });
  for (const name of ['scripts', 'web', 'lib', 'dsh-finance-agent', 'data-corrections']) await cp(join(project, name), join(root, name), {recursive: true});
  await cp(join(project, 'index.mjs'), join(root, 'index.mjs'));
  await mkdir(join(root, 'config')); await writeFile(join(root, 'config/daily-push.json'), '{"enabled":false}');
  const ctx = {
    effect: callback => {cleanup = callback();}, get: () => null,
    profileContext: {name: 'web', home: join(root, 'host')}, webServer: {port: 3080},
    tools: {register: row => definitions.set(row.name, row)}, skills: {register: row => skills.set(row.name, row)},
    agentDefaultModel: {currentSelection: () => ({provider: 'fixture', model: 'fixture-model'})},
    llm: {listProviders: () => [], listModels: async () => []}, attachments: {},
    agents: {create: async options => {
      const events = [], session = {seq: 0, eventAt: i => events[i]};
      options.setup({tools: {restrict() {}, presentAs() {}}});
      return {dispose: async () => {}, agent: {session, cancel() {}, whenIdle: async () => {}, followup() {
        events.push({type: 'assistant/message', data: {message: {content: [{type: 'text', text: '隔离研究完成'}]}}}, {type: 'turn/end', data: {reason: {kind: 'completed'}}}); session.seq = events.length;
      }}};
    }}, sessions: {flush: async () => {}},
  };
  const {apply} = await import(pathToFileURL(join(root, 'index.mjs')));
  await apply(ctx, {port: 0, python: pythonPath(project)});
  const state = JSON.parse(await readFile(join(root, '.runtime/web.json'), 'utf8'));
  assert.equal(definitions.size, 32); assert.equal(skills.size, 10); assert.equal(state.profile, 'web');
  const base = state.url, assets = new Set();
  assert.equal((await (await fetch(base + '/api/status')).json()).managedBy, 'dsh');
  for (const path of ['/', '/reports', '/portfolio', '/backtests', '/simulation', '/strategy-guide', '/settings']) {
    const response = await fetch(base + path); assert.equal(response.status, 200, path);
    for (const match of (await response.text()).matchAll(/(?:src|href)="(\/[^"?#]+\.(?:js|css|svg))"/g)) assets.add(match[1]);
  }
  for (const path of assets) assert.equal((await fetch(base + path)).status, 200, path);
  for (const market of ['a', 'hk', 'us']) for (const timeframe of ['daily', 'weekly', 'monthly']) {
    const response = await fetch(base + `/api/bar-strategies?market=${market}&timeframe=${timeframe}`);
    assert.equal(response.status, 200); assert.ok((await response.json()).strategies.momentum);
  }
  assert.equal((await definitions.get('get_trading_day').execute({market: 'us', date: '2026-07-03'})).is_trading_day, false);
  assert.deepEqual((await definitions.get('get_saved_portfolio').execute({})).holdings, []);
  const job = await definitions.get('start_financial_job').execute({kind: 'research', prompt: '隔离核对'});
  let current;
  for (let i = 0; i < 100; i++) {
    current = (await definitions.get('get_financial_jobs').execute({})).jobs.find(row => row.id === job.id);
    if (current.status !== 'running') break; await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.equal(current.status, 'completed'); assert.equal(current.result.model, 'fixture-model');
  await cleanup();
  await assert.rejects(access(join(root, '.runtime/web.json')), {code: 'ENOENT'});
  process.kill(process.pid, 0); // Shared host survives plugin disposal.
  t.diagnostic(`Verified 7 pages, ${assets.size} assets, 9 strategy menus and 32 native tools.`);
});
