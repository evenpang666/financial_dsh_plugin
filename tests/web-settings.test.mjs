import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, readFile, rm, copyFile} from 'node:fs/promises';
import {join} from 'node:path';
import {runInNewContext} from 'node:vm';
import {createWebSettings} from '../lib/settings.mjs';
import {createFinanceServer, listen} from '../lib/server.mjs';

async function fixture(t, cleanup = true) {
  await mkdir('.test-tmp', {recursive: true});
  const root = await mkdtemp(join(process.cwd(), '.test-tmp/settings-'));
  if (cleanup) t.after(() => rm(root, {recursive: true, force: true}));
  let host = {provider: 'host', model: 'default', reasoningEffort: 'high'};
  const currentSelection = () => ({...host});
  const llm = {
    listProviders: () => [{id: 'host', name: 'DSH Provider'}],
    listModels: async () => [{id: 'default', name: 'Default'}, {id: 'vision', name: 'Vision'}],
    resolveModelInfo: async (_provider, model) => ({reasoning: {efforts: model === 'default' ? [{id: 'high', name: '高'}] : [{id: 'low', name: '低'}]}}),
  };
  const settings = await createWebSettings(root, {currentSelection, llm});
  return {root, settings, llm, currentSelection, setHost: value => {host = value;}};
}

test('DSH preferences follow live host defaults and persist only explicit workspace selections', async t => {
  const {root, settings, llm, currentSelection, setHost} = await fixture(t);
  assert.deepEqual(settings.get(), {});
  assert.equal((await settings.describe()).model, 'default');
  setHost({provider: 'host', model: 'vision'});
  assert.equal((await settings.describe()).model, 'vision');
  await Promise.all([settings.update({provider: 'host', model: 'default', reasoningEffort: 'high'}), settings.update({provider: 'host', model: 'vision'})]);
  assert.deepEqual(settings.get(), {provider: 'host', model: 'vision'});
  const restored = await createWebSettings(root, {currentSelection, llm});
  assert.deepEqual(restored.get(), settings.get());
  await assert.rejects(settings.update({provider: 'host', model: 'unknown'}), /目录/);
  await assert.rejects(settings.update({provider: 'host', model: 'vision', reasoningEffort: 'high'}), /推理强度/);
  await assert.rejects(settings.update({provider: 'host', model: 'vision', apiKey: 'should-never-save'}), /有效/);
  await settings.update({inheritHost: true});
  assert.deepEqual(settings.get(), {});
  assert.deepEqual(JSON.parse(await readFile(join(root, 'data/web/settings.json'), 'utf8')), {});
});

test('settings API serves the DSH catalog, enforces origins and applies only saved selections to jobs', async t => {
  const {root, settings, currentSelection} = await fixture(t, false), calls = [];
  await mkdir(join(root, 'web'));
  for (const file of ['settings.html', 'settings.js', 'settings.css', 'theme.js', 'theme.css', 'workspace.js', 'workspace.css']) await copyFile(new URL('../web/' + file, import.meta.url), join(root, 'web', file));
  const site = await createFinanceServer({root, network: null, model: currentSelection, webSettings: settings, research: async (_prompt, options) => {calls.push(options); return {text: '完成'};}});
  const port = await listen(site.server, 0), base = `http://127.0.0.1:${port}`;
  t.after(async () => {await site.close(); await rm(root, {recursive: true, force: true});});
  for (const path of ['/settings', '/settings/', '/settings.js', '/theme.js', '/workspace.js']) assert.equal((await fetch(base + path)).status, 200);
  const data = await (await fetch(base + '/api/settings?provider=host&model=vision')).json();
  assert.deepEqual(data.reasoningEfforts, [{id: 'low', label: '低'}]);
  assert.equal(data.inheritHost, true);
  const request = {method: 'PUT', body: JSON.stringify({provider: 'host', model: 'vision'})};
  assert.equal((await fetch(base + '/api/settings', {...request, headers: {Origin: 'https://foreign.example'}})).status, 403);
  assert.equal((await fetch(base + '/api/settings', request)).status, 200);
  const status = await (await fetch(base + '/api/status')).json();
  assert.equal(status.managedBy, 'dsh'); assert.equal(status.model, 'vision'); assert.equal(status.reasoningEffort, undefined);
  const job = site.startJob({kind: 'research', prompt: '核对持仓'});
  for (let i = 0; i < 100 && job.status === 'running'; i++) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(job.status, 'completed'); assert.equal(calls[0].model, 'vision'); assert.equal(calls[0].provider, 'host');
  assert.equal(calls[0].reasoningEffort, undefined);
});

test('theme defaults to dark, persists selection and follows cross-tab storage changes', async () => {
  const source = await readFile(new URL('../web/theme.js', import.meta.url), 'utf8');
  const listeners = {}, writes = [], document = {documentElement: {dataset: {}}};
  const window = {addEventListener: (name, fn) => {listeners[name] = fn;}};
  runInNewContext(source, {window, document, localStorage: {getItem: () => null, setItem: (...args) => writes.push(args)}});
  assert.equal(window.financeTheme.get(), 'dark'); window.financeTheme.set('light');
  assert.equal(document.documentElement.dataset.theme, 'light'); assert.deepEqual(writes, [['zhiheng-theme', 'light']]);
  listeners.storage({key: 'zhiheng-theme', newValue: 'dark'}); assert.equal(window.financeTheme.get(), 'dark');
});
