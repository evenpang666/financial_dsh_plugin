// Kept identical in the four standalone plugins; no sibling checkout is required.
import { existsSync, readFileSync, mkdirSync, mkdtempSync, writeFileSync, renameSync, rmSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { homedir } from 'node:os';
import { spawnSync } from 'node:child_process';

export const REQUIRED_DSH = '0.2.0-rc.2';
const pattern = /^(\d+)\.(\d+)\.(\d+)(?:-([\w.-]+))?(?:\+[\w.-]+)?$/;
export function compareVersions(left, right) {
  const a = pattern.exec(left), b = pattern.exec(right);
  if (!a || !b) throw new Error(`无法识别 DSH 版本：${left} / ${right}`);
  for (let i = 1; i <= 3; i++) if (+a[i] !== +b[i]) return +a[i] > +b[i] ? 1 : -1;
  if (a[4] === b[4]) return 0;
  if (!a[4] || !b[4]) return a[4] ? -1 : 1;
  const x = a[4].split('.'), y = b[4].split('.');
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    if (x[i] === y[i]) continue;
    if (x[i] === undefined || y[i] === undefined) return x[i] === undefined ? -1 : 1;
    const nx = /^\d+$/.test(x[i]), ny = /^\d+$/.test(y[i]);
    if (nx && ny) return +x[i] > +y[i] ? 1 : -1;
    if (nx !== ny) return nx ? -1 : 1;
    return x[i] > y[i] ? 1 : -1;
  }
  return 0;
}
export const supportsDsh = version => typeof version === 'string' && pattern.test(version) && compareVersions(version, REQUIRED_DSH) >= 0;

// Old installations remain usable if a download fails. All four launchers share
// one verified, versioned runtime under DSH_HOME instead of downloading four copies.
export function ensureDshVersion({ cli, version, npm, env = process.env,
  directory = join(resolve((env.DSH_HOME || join(homedir(), '.dsh')).replace(/^~(?=$|[/\\])/, homedir())), 'runtimes/web/versions', REQUIRED_DSH),
  run = spawnSync, log = console.log } = {}) {
  if (cli && supportsDsh(version)) return { cli, version, upgraded: false };
  const entry = prefix => join(prefix, 'node_modules/@deepseek-ai/dsh/lib/bin.js');
  const probe = bin => {
    if (!existsSync(bin)) return null;
    const result = run(process.execPath, [bin, '--version'], { env, encoding: 'utf8', windowsHide: true, timeout: 15000 });
    return result.status === 0 ? result.stdout?.trim() : null;
  };
  const managed = entry(directory), installed = probe(managed);
  if (supportsDsh(installed)) {
    log(`[环境] DSH ${version || '未安装'} → 复用共享 DSH ${installed}`);
    return { cli: managed, version: installed, upgraded: true };
  }
  if (!npm) throw new Error('找不到 npm，无法升级 DSH。');
  log(`[环境] DSH ${version || '未安装'} 低于要求，正在安装 DSH ${REQUIRED_DSH}…`);
  mkdirSync(dirname(directory), { recursive: true });
  const stage = mkdtempSync(join(dirname(directory), '.install-'));
  let backup;
  try {
    writeFileSync(join(stage, 'package.json'), JSON.stringify({ private: true }));
    const result = run(process.execPath, [npm, 'install', '--prefix', stage, '--no-save', '--no-package-lock', '--no-audit', '--no-fund', `@deepseek-ai/dsh@${REQUIRED_DSH}`], { cwd: stage, env, stdio: 'inherit', windowsHide: true });
    if (result.error || result.status !== 0) throw new Error(`DSH 升级失败（${result.error?.message || result.status}），请检查网络和目录权限后重试。`);
    if (probe(entry(stage)) !== REQUIRED_DSH) throw new Error('DSH 升级后的版本校验失败，保留原有环境。');
    try { renameSync(stage, directory); }
    catch (error) {
      // A concurrent launcher may already have published the same version.
      if (probe(managed) !== REQUIRED_DSH) {
        if (!existsSync(directory)) throw error;
        backup = `${stage}.old`;
        renameSync(directory, backup);
        try { renameSync(stage, directory); }
        catch (failure) { renameSync(backup, directory); backup = undefined; throw failure; }
      }
    }
    log(`[环境] DSH 已更新至 ${REQUIRED_DSH}；将重新注册 Web 插件。`);
    return { cli: managed, version: REQUIRED_DSH, upgraded: true };
  } finally {
    rmSync(stage, { recursive: true, force: true });
    if (backup) rmSync(backup, { recursive: true, force: true });
  }
}

export function registrationCurrent(profile, name, version) {
  if (!version) return true;
  try { return readFileSync(join(profile, `.${name}-dsh-version`), 'utf8').trim() === version; }
  catch (error) { if (error.code !== 'ENOENT') throw error; return false; }
}
export function rememberRegistration(profile, name, version) {
  if (version) writeFileSync(join(profile, `.${name}-dsh-version`), version + '\n');
}
