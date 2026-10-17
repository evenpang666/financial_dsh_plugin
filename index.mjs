import { fileURLToPath } from 'node:url';
import { mkdir, readFile, writeFile, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { apply as registerTools } from './dsh-finance-agent/index.js';
import { startWorker } from './lib/workers.mjs';
import { createResearchAgent } from './lib/agent.mjs';
import { createFinanceServer, listen } from './lib/server.mjs';

export const name = 'financial';
export const inject = ['agents', 'agentDefaultModel', 'sessions', 'tools', 'skills'];
export async function apply(ctx, config = {}) {
  const root = fileURLToPath(new URL('./', import.meta.url)), owned = [];
  let site, scheduler, ownsState = false, statePath = join(root, '.runtime/web.json');
  const register = dataUrl => {
    const names = [];
    registerTools({ skills: ctx.skills, tools: { register(definition) { names.push(definition.name); return ctx.tools.register(definition); } } }, { baseUrl: dataUrl });
    return names;
  };
  const cleanup = async () => { clearInterval(scheduler); if (site) await site.close(); for (const worker of owned.reverse()) worker.close(); if (ownsState) await unlink(statePath).catch(() => {}); };
  ctx.on('dispose', cleanup);
  try {
    let existing;
    try {
      const state = JSON.parse(await readFile(statePath, 'utf8')); process.kill(state.pid, 0);
      if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(state.url) || !/^http:\/\/127\.0\.0\.1:\d+$/.test(state.dataUrl)) throw Error('invalid state');
      const response = await fetch(state.url + '/api/status', { signal: AbortSignal.timeout(1000) });
      if (response.ok && (await response.json()).name === 'dsh-financial') existing = state;
    } catch {}
    if (existing) { register(existing.dataUrl); console.log(`[DSH 插件] 知衡 · 复用已运行的工作台 ${existing.url}`); return; }
    const data = await startWorker(root, 'data'); owned.push(data);
    const reports = await startWorker(root, 'reports', { ...process.env, FINANCE_DATA_URL: data.url }); owned.push(reports);
    const financeTools = register(data.url);
    const research = createResearchAgent(ctx, root, financeTools);
    site = await createFinanceServer({ root, dataUrl: data.url, reportsUrl: reports.url, research, model: () => ctx.agentDefaultModel.currentSelection() });
    const port = await listen(site.server, config.port ?? process.env.FINANCE_PORT ?? 3205, site.network ? '0.0.0.0' : '127.0.0.1');
    await mkdir(join(root, '.runtime'), { recursive: true });
    await writeFile(statePath, JSON.stringify({ pid: process.pid, port, url: `http://127.0.0.1:${port}`, dataUrl: data.url, name: 'dsh-financial' })); ownsState = true;
    // Scheduling shares the same DSH-backed job path as manual generation.
    const stateFile = join(root, 'data/web/schedules.json'); let completed = {};
    try { completed = JSON.parse(await readFile(stateFile, 'utf8')); } catch {}
    let ticking = false;
    scheduler = setInterval(async () => {
      if (ticking) return; ticking = true;
      try {
        let settings = {}; try { settings = JSON.parse(await readFile(join(root, 'config/daily-push.json'), 'utf8')); } catch {}
        if (settings.enabled === false) return;
        const now = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short' }).format(new Date());
        const [day, clock] = now.split(' '), schedules = settings.schedules || { morning: '09:20', afternoon: '14:30' };
        const market = settings.market || 'a';
        for (const session of ['morning', 'afternoon']) {
          const key = `${day}:${market}:${session}`;
          if (clock !== schedules[session] || completed[key]) continue;
          const job = site.startJob({ kind: 'daily', market, session });
          // Poll completion; only successful jobs commit the schedule marker.
          const finish = setInterval(async () => {
            if (job.status === 'running') return; clearInterval(finish);
            if (job.status === 'completed') { completed[key] = true; await writeFile(stateFile, JSON.stringify(Object.fromEntries(Object.entries(completed).filter(([,value]) => value === true)))).catch(console.error); }
            else delete completed[key];
          }, 1000); finish.unref(); completed[key] = 'pending';
        }
      } catch (e) { console.error('[知衡调度] ' + e.message); } finally { ticking = false; }
    }, 30000); scheduler.unref();
    console.log(`[DSH 插件] 知衡 (financial) · 已加载\n[DSH 插件] 本机：http://127.0.0.1:${port}/\n[DSH 插件] 研究：复用 DSH Web Agent、默认模型与凭据`);
    if (site.network) console.log(`[DSH 插件] 局域网：http://${site.network.address}:${port}/ · ${site.network.adapter}`);
  } catch (e) { await cleanup(); throw e; }
}
