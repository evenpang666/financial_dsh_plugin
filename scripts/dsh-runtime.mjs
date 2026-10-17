import { access, realpath, readFile, readdir, mkdir, mkdtemp, writeFile, rename, rm, open } from 'node:fs/promises';
import { dirname, join, delimiter, resolve, basename } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { ensureDshVersion } from './dsh-version.mjs';

export const projectRoot = fileURLToPath(new URL('../', import.meta.url));
export const versions = JSON.parse(await readFile(new URL('./runtime-versions.json', import.meta.url), 'utf8'));
const probeCommand = promisify(execFile);
const pathDirs = env => (env.PATH || env.Path || '').split(delimiter).filter(Boolean).map(dir => dir.replace(/^"(.*)"$/, '$1'));
const dshEntry = prefix => join(prefix, 'node_modules/@deepseek-ai/dsh/lib/bin.js');

async function packageCli(prefix, name, fallback) {
  const directory = join(prefix, 'node_modules', name);
  try {
    const manifest = JSON.parse(await readFile(join(directory, 'package.json'), 'utf8'));
    const entry = typeof manifest.bin === 'string' ? manifest.bin : manifest.bin?.[name.split('/').at(-1)];
    if (entry) return join(directory, entry);
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  return join(prefix, 'node_modules', fallback);
}

async function cliPath(candidate, shimEntry) {
  try {
    const path = await realpath(candidate);
    if (/\.(cmd|ps1)$/i.test(path)) {
      const parts = shimEntry.split('/');
      const packageName = parts.slice(0, parts[0].startsWith('@') ? 2 : 1).join('/');
      const entry = await packageCli(dirname(path), packageName, shimEntry);
      await access(entry); return entry;
    }
    return path;
  } catch (error) { if (['ENOENT', 'ENOTDIR'].includes(error.code)) return null; throw error; }
}

async function onPath(name, entry, env) {
  for (const dir of pathDirs(env)) {
    for (const suffix of process.platform === 'win32' ? ['.cmd', ''] : ['']) {
      const found = await cliPath(join(dir, name + suffix), entry);
      if (found) return found;
    }
  }
}

// Resolve npm's Windows shim to JavaScript so no shell interpolation is needed.
export async function findDsh(env = process.env, root = projectRoot) {
  if (env.DSH_BIN) {
    const explicit = await cliPath(resolve(env.DSH_BIN), '@deepseek-ai/dsh/lib/bin.js');
    if (!explicit) throw new Error('DSH_BIN 指定的文件不存在，请修正该路径。');
    return explicit;
  }
  const system = await onPath('dsh', '@deepseek-ai/dsh/lib/bin.js', env);
  if (system) return system;
  const home = resolve(env.DSH_HOME || join(homedir(), '.dsh'));
  const candidates = [dshEntry(root), dshEntry(join(root, '.runtime/tools/dsh'))];
  try {
    for (const name of await readdir(join(home, 'runtimes'))) candidates.push(dshEntry(join(home, 'runtimes', name, 'current')));
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  for (const candidate of candidates) {
    const found = await cliPath(candidate, '@deepseek-ai/dsh/lib/bin.js');
    if (found) return found;
  }
  throw Object.assign(new Error('未找到 dsh。启动器将补装项目内的固定版本。'), { code: 'DSH_MISSING' });
}

export async function findNpm(env = process.env) {
  const candidates = [env.npm_execpath && basename(env.npm_execpath) === 'npm-cli.js' ? env.npm_execpath : undefined, await onPath('npm', 'npm/bin/npm-cli.js', env),
    join(dirname(process.execPath), 'node_modules/npm/bin/npm-cli.js'),
    resolve(dirname(process.execPath), '../lib/node_modules/npm/bin/npm-cli.js')];
  for (const candidate of candidates.filter(Boolean)) {
    try { await access(candidate); return candidate; } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  throw new Error('未找到 npm，请运行 start.sh / Start.bat 自动补齐项目运行环境。');
}

export async function versionOf(cli, env = process.env, probe = probeCommand) {
  const file = await open(cli, 'r');
  const prefix = Buffer.alloc(256);
  try { await file.read(prefix, 0, prefix.length, 0); } finally { await file.close(); }
  const javascript = /\.[cm]?js$/i.test(cli) || /^#![^\r\n]*\bnode\b/.test(prefix.toString());
  const { stdout } = await probe(javascript ? process.execPath : cli, javascript ? [cli, '--version'] : ['--version'], { env, timeout: 15000, windowsHide: true });
  const version = stdout.trim();
  if (!/^\d+\.\d+\.\d+(?:-[\w.-]+)?$/.test(version)) throw new Error(`无法识别工具版本：${cli}`);
  return version;
}

export function runInstaller(command, args, options) {
  return new Promise((done, reject) => {
    const child = spawn(command, args, { ...options, stdio: 'inherit', windowsHide: true });
    child.once('error', reject);
    child.once('exit', (code, signal) => code === 0 ? done() : reject(new Error(`环境补装失败（${signal || code}），请检查网络后重试。`)));
  });
}

// Stage a missing tool before publishing it. Existing installations are never updated.
export async function installTool({ name, version, entry, directory, npm, env, run = runInstaller, versionOf: inspect = versionOf }) {
  await mkdir(dirname(directory), { recursive: true });
  const stage = await mkdtemp(join(dirname(directory), '.install-'));
  try {
    await writeFile(join(stage, 'package.json'), JSON.stringify({ private: true }));
    await run(process.execPath, [npm, 'install', '--prefix', stage, '--no-save', '--no-package-lock', '--no-audit', '--no-fund', '--no-update-notifier', `${name}@${version}`], { cwd: stage, env });
    if (await inspect(await packageCli(stage, name, entry), env) !== version) throw new Error(`${name} 补装后的版本校验失败。`);
    try { await rename(stage, directory); }
    catch (error) {
      if (!['EEXIST', 'ENOTEMPTY'].includes(error.code)) throw error;
      // Another launcher may have installed the same fixed version concurrently.
      if (await inspect(await packageCli(directory, name, entry), env) !== version) throw new Error(`${directory} 已存在，请检查项目内的工具安装；未覆盖已有环境。`);
    }
  } finally { await rm(stage, { recursive: true, force: true }); }
}

async function profileTools(env) {
  const profile = join(resolve(env.DSH_HOME || join(homedir(), '.dsh')), 'profiles/web');
  let metadata = {};
  try {
    const text = await readFile(join(profile, 'node_modules/.modules.yaml'), 'utf8');
    try { metadata = JSON.parse(text); } catch {
      for (const key of ['packageManager', 'storeDir']) metadata[key] = text.match(new RegExp(`^${key}:\\s*["']?([^"'\\r\\n]+)["']?\\s*$`, 'm'))?.[1]?.trim();
    }
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (!metadata.packageManager) {
    try { metadata.packageManager = JSON.parse(await readFile(join(profile, 'package.json'), 'utf8')).packageManager; }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  return metadata;
}

export async function ensureEnvironment({ root = projectRoot, env = process.env, locateDsh = findDsh, locateNpm = findNpm,
  inspect = versionOf, install = installTool, upgrade = ensureDshVersion, log = console.log } = {}) {
  if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('需要 Node.js 22+，请运行 start.sh / Start.bat 自动补齐项目运行环境。');
  env = { ...env, PATH: [dirname(process.execPath), ...pathDirs(env)].join(delimiter), npm_config_update_notifier: 'false', PNPM_DISABLE_SELF_UPDATE_CHECK: '1' };
  log(`[环境] 检查 Node.js ${process.versions.node}、npm、DSH 最低版本和 pnpm`);
  const npm = await locateNpm(env);
  log(`[环境] 复用 npm ${await inspect(npm, env)}`);
  let dsh;
  try { dsh = await locateDsh(env, root); }
  catch (error) { if (error.code !== 'DSH_MISSING') throw error; }
  if (!dsh) {
    const directory = join(root, '.runtime/tools/dsh');
    log(`[环境] 补齐项目内 DSH ${versions.dsh}…`);
    await install({ name: '@deepseek-ai/dsh', version: versions.dsh, entry: '@deepseek-ai/dsh/lib/bin.js', directory, npm, env });
    dsh = dshEntry(directory);
    log('[环境] 首次安装后请配置模型：npm run dsh -- web');
  }
  const checked = await upgrade({ cli: dsh, version: await inspect(dsh, env), npm, env, log });
  dsh = checked.cli;
  log(`[环境] 复用 DSH ${checked.version}`);
  const metadata = await profileTools(env);
  const required = metadata.packageManager?.match(/^pnpm@(\d+\.\d+\.\d+(?:-[\w.-]+)?)/)?.[1];
  const directory = join(root, '.runtime/tools', `pnpm-${required || versions.pnpm}`);
  const candidates = [await onPath('pnpm', 'pnpm/bin/pnpm.cjs', env), await packageCli(directory, 'pnpm', 'pnpm/bin/pnpm.cjs')];
  let pnpm, pnpmVersion;
  for (const cli of candidates.filter(Boolean)) {
    try {
      const version = await inspect(cli, env);
      if (required && version.split('.')[0] !== required.split('.')[0]) continue;
      pnpm = cli; pnpmVersion = version; break;
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  if (!pnpm) {
    log(`[环境] 补齐项目内 pnpm ${required || versions.pnpm}…`);
    await install({ name: 'pnpm', version: required || versions.pnpm, entry: 'pnpm/bin/pnpm.cjs', directory, npm, env });
    pnpm = await packageCli(directory, 'pnpm', 'pnpm/bin/pnpm.cjs');
    pnpmVersion = await inspect(pnpm, env);
  }
  // pnpm must be visible to DSH's child process without changing the global PATH.
  const pnpmBin = basename(pnpm).startsWith('pnpm.') ? resolve(dirname(pnpm), '../../.bin') : dirname(pnpm);
  env.PATH = [pnpmBin, ...pathDirs(env)].join(delimiter);
  if (metadata.storeDir) {
    const store = /^v\d+$/.test(basename(metadata.storeDir)) ? dirname(metadata.storeDir) : metadata.storeDir;
    delete env.NPM_CONFIG_STORE_DIR;
    env.npm_config_store_dir = store; env.PNPM_CONFIG_STORE_DIR = store;
  }
  log(`[环境] 复用 pnpm ${pnpmVersion}；知衡 无需额外项目依赖或编译`);
  return { dsh, version: checked.version, npm, pnpm, env };
}
