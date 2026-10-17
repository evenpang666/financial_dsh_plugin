import { spawn, spawnSync } from 'node:child_process';
import { readFile, writeFile, mkdir, unlink, open } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { ensureEnvironment, runInstaller } from './dsh-runtime.mjs';
import { registerFinancial } from './register-plugin.mjs';
import { pythonPath } from '../lib/workers.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
export async function runningStudio(directory = root) {
  try {
    const state = JSON.parse(await readFile(join(directory, '.runtime/web.json'), 'utf8'));
    if (!Number.isInteger(state.port) || state.port < 1 || state.port > 65535) return null;
    process.kill(state.pid, 0);
    const url = `http://127.0.0.1:${state.port}`;
    const response = await fetch(url + '/api/status', { signal: AbortSignal.timeout(1500) });
    return response.ok && (await response.json()).name === 'dsh-financial' ? url : null;
  } catch { return null; }
}
function openBrowser(url) {
  if (process.argv.includes('--no-open')) return;
  const command = process.platform === 'win32' ? 'rundll32' : process.platform === 'darwin' ? 'open' : 'xdg-open';
  const child = spawn(command, process.platform === 'win32' ? ['url.dll,FileProtocolHandler', url] : [url], { stdio: 'ignore', detached: true, windowsHide: true });
  child.on('error', () => console.log(`请手动打开 ${url}`)); child.unref();
}
async function ensurePython(env) {
  const local = join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  let python = pythonPath(root, env);
  const check = spawnSync(python, ['-c', 'import sys; assert sys.version_info >= (3,11)'], { env, windowsHide: true, encoding: 'utf8' });
  if (check.error || check.status) throw new Error('需要 Python 3.11+，请先安装 Python 或设置 FINANCE_PYTHON。');
  if (!env.FINANCE_PYTHON && !existsSync(local)) {
    console.log('[环境] 创建项目 Python 虚拟环境…');
    await runInstaller(python, ['-m', 'venv', join(root, '.venv')], { cwd: root, env }); python = local;
  }
  const deps = spawnSync(python, ['-c', 'import akshare,numpy,pandas,baostock,sklearn,xgboost'], { env, windowsHide: true });
  if (deps.error || deps.status) { console.log('[环境] 安装金融数据依赖…'); await runInstaller(python, ['-m', 'pip', 'install', '-r', join(root, 'requirements.txt')], { cwd: root, env }); }
  return python;
}
export async function launch() {
  const preferredPort = Number(process.env.FINANCE_PORT || 3205);
  if (!Number.isInteger(preferredPort) || preferredPort < 0 || preferredPort > 65535) throw new Error('FINANCE_PORT 必须为 0–65535 的整数。');
  const existing = await runningStudio();
  if (existing) { console.log(`[知衡] 复用已运行的工作台：${existing}`); openBrowser(existing); return; }
  const runtimeDir = join(root, '.runtime'); await mkdir(runtimeDir, { recursive: true });
  const lock = join(runtimeDir, 'launch.lock');
  try { const file = await open(lock, 'wx'); await file.writeFile(String(process.pid)); await file.close(); }
  catch (e) {
    if (e.code !== 'EEXIST') throw e;
    let alive = false;
    try { process.kill(Number(await readFile(lock, 'utf8')), 0); alive = true; } catch {}
    if (alive) { console.log('[知衡] 启动已在进行，请查看原启动窗口。'); return; }
    await unlink(lock); return launch();
  }
  try {
    const runtime = await ensureEnvironment();
    const python = await ensurePython(runtime.env);
    await registerFinancial({ env: runtime.env, locate: async () => runtime.dsh, version: runtime.version });
    // Launch an app profile with the same settings and credential paths as DSH Web.
    // No keys or provider configuration are copied into the project.
    const originalHome = resolve(runtime.env.DSH_HOME || join(homedir(), '.dsh'));
    const localHome = join(runtimeDir, 'dsh'), profile = join(localHome, 'profiles', 'financial');
    await mkdir(profile, { recursive: true });
    await writeFile(join(profile, 'package.json'), JSON.stringify({ private: true, type: 'module', dsh: { profile: { bundles: ['@deepseek-ai/dsh-base'], patchReload: 'startup' } } }, null, 2));
    await writeFile(join(profile, 'cordis.patch.yml'), JSON.stringify([
      { id: 'settings', config: { path: join(originalHome, 'settings.yaml'), watch: true } },
      { id: 'credentials', config: { path: join(originalHome, '.credentials.yaml'), watch: true } },
      { id: 'session-title-llm', disabled: true }, { id: 'tools', config: { mode: 'native' } },
      { insert: [{ id: 'financial', name: join(root, 'index.mjs'), config: { port: preferredPort } }] }
    ], null, 2));
    console.log('[知衡] 启动 DSH Agent；网页默认端口 3205，占用时自动使用空闲端口。');
    const child = spawn(process.execPath, [runtime.dsh, '--profile', 'financial'], { cwd: root, stdio: 'inherit', windowsHide: true, env: { ...runtime.env, FINANCE_PYTHON: python, DSH_HOME: localHome, DSH_TELEMETRY_DISABLED: '1' } });
    const startupTimeout = setTimeout(() => { console.error('[知衡] 启动未在 90 秒内完成，请检查插件错误日志。'); child.kill('SIGTERM'); }, 90000);
    let checking = false, opened = false;
    const poll = setInterval(async () => { if (checking || opened) return; checking = true; try { const url = await runningStudio(); if (url) { opened = true; clearInterval(poll); clearTimeout(startupTimeout); console.log(`[知衡] 工作台：${url}`); openBrowser(url); } } finally { checking = false; } }, 700);
    const stop = () => child.kill('SIGTERM'); process.once('SIGINT', stop); process.once('SIGTERM', stop);
    try { await new Promise((done, reject) => { child.once('error', reject); child.once('exit', code => code ? reject(new Error(`DSH 退出 (${code})，请检查以上日志。`)) : done()); }); }
    finally { clearInterval(poll); clearTimeout(startupTimeout); process.off('SIGINT', stop); process.off('SIGTERM', stop); }
  } finally { await unlink(lock).catch(() => {}); }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) launch().catch(e => { console.error('[知衡] ' + e.message); process.exitCode = 1; });
