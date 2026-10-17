import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join, resolve, relative, isAbsolute } from 'node:path';

export function pythonPath(root, env = process.env) {
  if (env.FINANCE_PYTHON) return env.FINANCE_PYTHON;
  try {
    const state = JSON.parse(readFileSync(join(root, '.runtime/python.json'), 'utf8'));
    const candidate = resolve(root, state.path), local = relative(join(root, '.runtime/venvs'), candidate);
    if (!isAbsolute(local) && !local.startsWith('..') && existsSync(candidate)) return candidate;
  } catch {}
  const local = join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  return existsSync(local) ? local : process.platform === 'win32' ? 'python' : 'python3';
}
export function startWorker(root, kind, env = process.env) {
  return new Promise((resolve, reject) => {
    const child = spawn(pythonPath(root, env), ['-u', join(root, 'scripts/web_worker.py'), kind], {
      cwd: root, env: { ...env, PYTHONIOENCODING: 'utf-8', FINANCE_OWNER_PID: String(process.pid) }, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe']
    });
    let buffer = '', settled = false;
    const fail = error => { if (!settled) { settled = true; clearTimeout(timer); child.kill(); reject(error); } };
    const timer = setTimeout(() => fail(new Error(`金融 ${kind} 服务启动超时，请运行启动器检查 Python 依赖。`)), 60000);
    child.once('error', fail);
    child.once('exit', code => fail(new Error(`金融 ${kind} 服务退出 (${code})。请检查终端日志。`)));
    child.stderr.on('data', chunk => process.stderr.write(chunk));
    child.stdout.on('data', chunk => {
      buffer += chunk;
      const match = buffer.match(/FINANCE_READY:(\d+)/);
      if (!settled && match) { settled = true; clearTimeout(timer); resolve({ child, url: `http://127.0.0.1:${match[1]}`, close: () => { child.stdin.end(); child.kill(); } }); }
      if (buffer.length > 10000) buffer = buffer.slice(-1000);
    });
  });
}
