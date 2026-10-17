#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { readFile, realpath } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { findDsh, ensureEnvironment } from './dsh-runtime.mjs';
import { registrationCurrent, rememberRegistration } from './dsh-version.mjs';

const packageName = 'dsh-financial';
const packageRoot = fileURLToPath(new URL('../', import.meta.url));

export async function financialInstalled(home, root) {
  const profile = join(home, 'profiles', 'web');
  try {
    const manifest = JSON.parse(await readFile(join(profile, 'package.json'), 'utf8'));
    return Object.hasOwn(manifest.dependencies || {}, packageName) && manifest.dsh?.profile?.bundles?.includes(packageName) &&
      await realpath(join(profile, 'node_modules', packageName)) === await realpath(root);
  } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

function execute(command, args, options) {
  return new Promise((done, reject) => {
    const child = spawn(command, args, { ...options, stdio: 'inherit', windowsHide: true });
    child.once('error', reject);
    child.once('exit', (code, signal) => code === 0 ? done() : reject(new Error(`DSH 插件注册失败（${signal || code}），请检查 DSH 和 pnpm 安装。`)));
  });
}

export async function registerFinancial({ env = process.env, root = packageRoot,
  home = resolve(env.DSH_HOME || join(homedir(), '.dsh')), locate = findDsh, run = execute, log = console.log, version } = {}) {
  root = resolve(root);
  const profilePath = join(home, 'profiles/web/package.json');
  let legacy = false;
  try { legacy = Boolean(JSON.parse(await readFile(profilePath, 'utf8')).dependencies?.['dsh-finance-agent']); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  const current = await financialInstalled(home, root) && registrationCurrent(join(home, 'profiles/web'), packageName, version);
  if (current && !legacy) { log('知衡已注册到 DSH Web 插件库。'); return; }
  const cli = await locate(env);
  log('正在将 知衡 注册到 DSH Web 插件库…');
  // Let DSH maintain dependencies and bundle selections, preserving other plugins.
  if (!current) await run(process.execPath, [cli, 'plugin', '--profile', 'web', 'add', `link:${root.replaceAll('\\', '/')}`],
    { cwd: root, env: { ...env, DSH_HOME: home } });
  if (!await financialInstalled(home, root)) throw new Error('DSH 未完成 知衡 bundle 注册，请检查插件安装日志。');
  if (legacy) {
    log('迁移旧金融工具插件，避免重复注册；研究工具由知衡插件统一提供。');
    await run(process.execPath, [cli, 'plugin', '--profile', 'web', 'remove', 'dsh-finance-agent'], { cwd: root, env: { ...env, DSH_HOME: home } });
  }
  rememberRegistration(join(home, 'profiles/web'), packageName, version);
  log('知衡 已注册。重新启动 dsh web 后可在插件库查看名称、简介与 logo，并看到加载日志。');
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const runtime = await ensureEnvironment();
    await registerFinancial({ env: runtime.env, locate: async () => runtime.dsh, version: runtime.version });
  }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
