import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, writeFile, rename } from 'node:fs/promises';
import { join, relative, isAbsolute, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { pythonPath } from '../lib/workers.mjs';
import { versions, runInstaller } from './dsh-runtime.mjs';
import { compareVersions } from './dsh-version.mjs';

const execute = promisify(execFile);
const options = env => ({env, windowsHide: true, timeout: 30000, encoding: 'utf8'});
const pythonBinary = directory => join(directory, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
export async function inspectPython(command, env, args = []) {
  try {
    const {stdout} = await execute(command, [...args, '-c', 'import sys,json; print(json.dumps({"version":".".join(map(str,sys.version_info[:3])),"path":sys.executable}))'], options(env));
    return JSON.parse(stdout.trim());
  } catch { return null; }
}
const dependencyCheck = `import importlib, importlib.metadata as metadata, json, sys
from pip._vendor.packaging.requirements import Requirement
modules = {"scikit-learn": "sklearn"}
issues = []
for line in open(sys.argv[1], encoding="utf-8"):
    if not line.strip() or line.lstrip().startswith("#"): continue
    requirement = Requirement(line.strip())
    try:
        installed = metadata.version(requirement.name)
        if installed not in requirement.specifier:
            issues.append(f"{requirement.name} {installed} 不满足 {requirement.specifier}")
        else:
            importlib.import_module(modules.get(requirement.name, requirement.name.replace("-", "_")))
    except metadata.PackageNotFoundError:
        issues.append(f"{requirement.name} 未安装")
    except Exception:
        issues.append(f"{requirement.name} 无法导入")
print(json.dumps(issues, ensure_ascii=False))`;
export async function inspectDependencies(python, root, env) {
  try { return JSON.parse((await execute(python, ['-c', dependencyCheck, join(root, 'requirements.txt')], options(env))).stdout.trim()); }
  catch { return ['pip 或金融数据依赖不可用']; }
}

// Only used when no suitable Python exists. uv keeps its binaries and Python in .runtime.
export async function installManagedPython(root, env, {run = runInstaller, probe = execute, download = fetch, log = console.log} = {}) {
  const runtime = join(root, '.runtime'), toolDir = join(runtime, 'tools/uv');
  env = {...env, UV_UNMANAGED_INSTALL: toolDir, UV_NO_MODIFY_PATH: '1', UV_PYTHON_INSTALL_REGISTRY: '0', UV_PYTHON_INSTALL_BIN: '0', UV_PYTHON_INSTALL_DIR: join(runtime, 'python'), UV_PYTHON_BIN_DIR: join(runtime, 'python/bin'), UV_CACHE_DIR: join(runtime, 'uv-cache')};
  let uv = 'uv';
  try { await probe(uv, ['--version'], options(env)); }
  catch {
    uv = join(toolDir, process.platform === 'win32' ? 'uv.exe' : 'uv');
    try { await probe(uv, ['--version'], options(env)); }
    catch {
      await mkdir(toolDir, {recursive: true});
      const windows = process.platform === 'win32', installer = join(toolDir, windows ? 'install.ps1' : 'install.sh');
      log('[环境] 补齐项目内 Python 安装工具 uv…');
      const response = await download(`https://astral.sh/uv/install.${windows ? 'ps1' : 'sh'}`, {signal: AbortSignal.timeout(60000)});
      if (!response.ok) throw new Error(`Python 安装工具下载失败 (${response.status})。`);
      await writeFile(installer, await response.text());
      await run(windows ? 'powershell.exe' : 'sh', windows ? ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', installer] : [installer], {cwd: root, env});
      await probe(uv, ['--version'], options(env));
    }
  }
  log(`[环境] Python 缺失或低于 ${versions.pythonMinimum}，安装项目内 Python ${versions.python}…`);
  await run(uv, ['python', 'install', versions.python, '--no-config'], {cwd: root, env});
  const {stdout} = await probe(uv, ['python', 'find', '--managed-python', versions.python, '--no-config'], options(env));
  return stdout.trim();
}

export async function ensurePython({root, env = process.env, inspect = inspectPython, dependencies = inspectDependencies, managed = installManagedPython, run = runInstaller, log = console.log} = {}) {
  const compatible = info => info && compareVersions(info.version, versions.pythonMinimum) >= 0;
  const selected = pythonPath(root, env);
  let info = await inspect(selected, env), explicit = Boolean(env.FINANCE_PYTHON) && compatible(info);
  if (!compatible(info)) {
    log(`[环境] 检查 Python ${versions.pythonMinimum}+；当前 ${info?.version || '未安装或不可用'}`);
    const fallback = pythonPath(root, {...env, FINANCE_PYTHON: ''});
    for (const [command, args] of [[fallback, []], ['python3', []], ['python', []], ...(process.platform === 'win32' ? [['py', ['-3']]] : [])]) {
      info = await inspect(command, env, args); if (compatible(info)) break;
    }
    if (!compatible(info)) info = await inspect(await managed(root, env, {run, log}), env);
    if (!compatible(info)) throw new Error('Python 安装后版本校验失败，原环境已保留。');
  }
  let python = info.path;
  const location = relative(resolve(root), resolve(python));
  const projectLocal = !isAbsolute(location) && !location.startsWith('..') && /^(?:\.venv[/\\]|\.runtime[/\\]venvs[/\\])/.test(location);
  if (!explicit && !projectLocal) {
    const directory = join(root, '.runtime/venvs', `python-${info.version}-${randomUUID().slice(0,8)}`);
    await mkdir(directory, {recursive: true}); log(`[环境] 使用 Python ${info.version} 创建项目虚拟环境…`);
    await run(python, ['-m', 'venv', directory], {cwd: root, env}); python = pythonBinary(directory);
    info = await inspect(python, env);
    if (!compatible(info)) throw new Error('项目 Python 虚拟环境校验失败，原环境已保留。');
  }
  log(`[环境] 复用 Python ${info.version}`);
  let issues = await dependencies(python, root, env);
  if (issues.length) {
    log(`[环境] 补齐或升级金融数据依赖：${issues.join('；')}`);
    await run(python, ['-m', 'ensurepip', '--upgrade'], {cwd: root, env});
    // pip's default only-if-needed strategy keeps already compatible higher versions.
    await run(python, ['-m', 'pip', 'install', '--disable-pip-version-check', '-r', join(root, 'requirements.txt')], {cwd: root, env});
    issues = await dependencies(python, root, env);
    if (issues.length) throw new Error(`Python 依赖验收失败：${issues.join('；')}`);
  } else log('[环境] 金融数据依赖版本满足要求，直接复用');
  const managedRoot = join(root, '.runtime/venvs'), managedRelative = relative(managedRoot, python);
  if (!explicit && !isAbsolute(managedRelative) && !managedRelative.startsWith('..')) {
    const temporary = join(root, '.runtime', `python-${randomUUID()}.tmp`);
    await writeFile(temporary, JSON.stringify({path: relative(root, python)}));
    await rename(temporary, join(root, '.runtime/python.json'));
  }
  return python;
}
