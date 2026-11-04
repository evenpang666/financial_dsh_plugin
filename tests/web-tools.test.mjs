import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdir, mkdtemp, rm, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {createServer} from 'node:http';
import {apply} from '../dsh-finance-agent/index.js';
import {extraTools} from '../lib/plugin-tools.mjs';
import {scheduleSlots} from '../lib/scheduler.mjs';
import {listen} from '../lib/server.mjs';

test('DSH exposes 21 multi-market research tools, 11 workspace tools and 10 bundled skills', () => {
  const tools = [], skills = [];
  apply({tools: {register: row => tools.push(row)}, skills: {register: row => skills.push(row)}});
  assert.equal(tools.length, 21); assert.equal(skills.length, 10);
  assert.ok(skills.some(row => row.name === 'us-share-research'));
  for (const tool of tools) if (tool.parameters.properties.market) assert.deepEqual(tool.parameters.properties.market.enum, ['a', 'hk', 'us']);
  const workspace = extraTools(async () => ({}), process.cwd());
  assert.equal(workspace.length, 11); assert.equal(new Set([...tools, ...workspace].map(row => row.name)).size, 32);
});

test('US schedules use New York trading dates and daylight saving time', () => {
  const settings = {markets: ['us']};
  for (const date of ['2026-07-06T13:20:00Z', '2026-01-06T14:20:00Z']) assert.equal(scheduleSlots(settings, new Date(date))[0].session, 'morning');
  assert.equal(scheduleSlots(settings, new Date('2026-07-06T19:30:00Z'))[0].key, '2026-07-06:us:afternoon');
  assert.equal(scheduleSlots(settings, new Date('2026-07-06T01:20:00Z')).length, 0);
});

test('workspace tools forward jobs to the DSH service and constrain local report and schedule access', async t => {
  await mkdir('.test-tmp', {recursive: true}); const root = await mkdtemp(join(process.cwd(), '.test-tmp/tools-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const requests = [], server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    requests.push({url: req.url, body: Buffer.concat(chunks).toString()});
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({id: 'fixture', jobs: []}));
  });
  const port = await listen(server, 0), url = `http://127.0.0.1:${port}`;
  t.after(() => {server.closeAllConnections(); return new Promise(resolve => server.close(resolve));});
  const tools = new Map(extraTools(async () => ({url}), root).map(row => [row.name, row]));
  assert.deepEqual(await tools.get('open_financial_workspace').execute({}), {url});
  await tools.get('start_financial_job').execute({kind: 'daily', market: 'us'});
  assert.equal(requests[0].url, '/api/jobs'); assert.deepEqual(JSON.parse(requests[0].body), {kind: 'daily', market: 'us'});
  await tools.get('get_financial_jobs').execute({}); assert.equal(requests[1].url, '/api/jobs');
  const config = {enabled: true, markets: ['a', 'us'], market_schedules: {us: {morning: '09:20', afternoon: '15:30'}}};
  await tools.get('set_report_schedule').execute(config);
  assert.deepEqual(await tools.get('get_report_schedule').execute({}), config);
  assert.throws(() => tools.get('set_report_schedule').execute({...config, markets: ['other']}));
  assert.throws(() => tools.get('set_report_schedule').execute({...config, market_schedules: {us: {morning: '25:00', afternoon: '15:30'}}}));
  await mkdir(join(root, 'data/reports'), {recursive: true}); await writeFile(join(root, 'data/reports/2026-11-04-us-manual-112233.md'), '# 证据');
  assert.equal((await tools.get('read_financial_report').execute({id: '2026-11-04-us-manual-112233'})).markdown, '# 证据');
  await assert.rejects(tools.get('read_financial_report').execute({id: '../../package'}), /编号无效/);
});
