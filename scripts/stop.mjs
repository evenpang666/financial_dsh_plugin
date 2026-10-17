import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { runningStudio } from './launch.mjs';
const root = fileURLToPath(new URL('../', import.meta.url));
if (!await runningStudio(root)) console.log('[知衡] 当前没有运行的工作台。');
else {
  const state = JSON.parse(await readFile(new URL('../.runtime/web.json', import.meta.url), 'utf8'));
  if (!Number.isInteger(state.pid) || state.pid <= 0) throw Error('运行状态中的进程 ID 无效。');
  process.kill(state.pid, 'SIGTERM'); console.log('[知衡] 工作台已停止。');
}
