import { fileURLToPath } from 'node:url';
import { mkdir, readFile, writeFile, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { apply as registerTools } from './dsh-finance-agent/index.js';
import { startWorker } from './lib/workers.mjs';
import { createResearchAgent } from './lib/agent.mjs';
import { createFinanceServer, listen } from './lib/server.mjs';
import { logWorkspaceAccess } from './lib/access-log.mjs';
import { randomUUID } from 'node:crypto';
import {startScheduler} from './lib/scheduler.mjs';
import {createWebSettings} from './lib/settings.mjs';
import {extraTools} from './lib/plugin-tools.mjs';

export const name = 'financial';
export const inject = ['agents', 'agentDefaultModel', 'sessions', 'tools', 'skills', 'profileContext', 'webServer', 'llm', 'attachments'];
export async function apply(ctx, config = {}) {
  const root = fileURLToPath(new URL('./', import.meta.url)), owned = [], instance = randomUUID();
  let site, scheduler, ownsState = false, statePath = join(root, '.runtime/web.json');
  const register = dataUrl => {
    const names = [];
    registerTools({ skills: ctx.skills, tools: { register(definition) { names.push(definition.name); return ctx.tools.register(definition); } } }, { baseUrl: dataUrl });
    return names;
  };
  const registerWorkspace = url => {
    for (const definition of extraTools(async () => ({url}), root)) ctx.tools.register({
      output: {schema: {type: 'object'}, render: (_args, value) => [{type: 'text', text: JSON.stringify(value)}]},
      ...definition,
    });
  };
  let disposing;
  const cleanup = () => disposing ??= (async () => {
    scheduler?.(); if (site) await site.close(); for (const worker of owned.reverse()) worker.close();
    if (ownsState) { try { if (JSON.parse(await readFile(statePath, 'utf8')).instance === instance) await unlink(statePath); } catch {} }
  })();
  ctx.effect(() => cleanup);
  try {
    let existing;
    try {
      const state = JSON.parse(await readFile(statePath, 'utf8')); process.kill(state.pid, 0);
      if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(state.url) || !/^http:\/\/127\.0\.0\.1:\d+$/.test(state.dataUrl)) throw Error('invalid state');
      const response = await fetch(state.url + '/api/status', { signal: AbortSignal.timeout(1000) });
      if (response.ok && (await response.json()).name === 'dsh-financial') existing = state;
    } catch {}
    if (existing) { register(existing.dataUrl); registerWorkspace(existing.url); console.log('[DSH 插件] 知衡 · 复用已运行的工作台'); await logWorkspaceAccess(existing.url); return; }
    const workerEnv = {...process.env, ...(config.python ? {FINANCE_PYTHON: config.python} : {})};
    const data = await startWorker(root, 'data', workerEnv); owned.push(data);
    const reports = await startWorker(root, 'reports', { ...workerEnv, FINANCE_DATA_URL: data.url }); owned.push(reports);
    const financeTools = register(data.url);
    const research = createResearchAgent(ctx, root, financeTools);
    const model = () => ctx.agentDefaultModel.currentSelection();
    const webSettings = await createWebSettings(root, {currentSelection: model, llm: ctx.llm});
    site = await createFinanceServer({ root, dataUrl: data.url, reportsUrl: reports.url, research, env: workerEnv, model, webSettings, network: workerEnv.FINANCE_LAN === '0' ? null : undefined });
    const port = await listen(site.server, config.port ?? process.env.FINANCE_PORT ?? 3205, site.network ? '0.0.0.0' : '127.0.0.1');
    registerWorkspace(`http://127.0.0.1:${port}`);
    await mkdir(join(root, '.runtime'), { recursive: true });
    await writeFile(statePath, JSON.stringify({ pid: process.pid, port, url: `http://127.0.0.1:${port}`, dataUrl: data.url, name: 'dsh-financial', profile: ctx.profileContext.name, home: ctx.profileContext.home, dshWebUrl: `http://127.0.0.1:${ctx.webServer.port}`, instance })); ownsState = true;
    scheduler = startScheduler(root, slot => site.startJob(slot, {scheduled: true}), async market => {
      const response = await fetch(data.url + '/v1/trading-day?market=' + market, {signal: AbortSignal.timeout(10000)});
      const value = await response.json(); if (!response.ok) throw Error(value.error); return value;
    });
    console.log('[DSH 插件] 知衡 (financial) · 已加载');
    await logWorkspaceAccess(`http://127.0.0.1:${port}/`, {network: site.network});
    console.log('[DSH 插件] 研究：复用 DSH Web Agent、默认模型与凭据');
  } catch (e) { await cleanup(); throw e; }
}
