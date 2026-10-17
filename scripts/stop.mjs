import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { runningStudio } from './launch.mjs';
import { installFinancialPatch } from './dsh-host.mjs';
const root = fileURLToPath(new URL('../', import.meta.url));
if (!await runningStudio(root)) console.log('[知衡] 当前没有运行的工作台。');
else {
  const state = JSON.parse(await readFile(new URL('../.runtime/web.json', import.meta.url), 'utf8'));
  if (!Number.isInteger(state.pid) || state.pid <= 0) throw Error('运行状态中的进程 ID 无效。');
  if (state.profile === 'web') {
    if (!state.home) throw Error('缺少 DSH Web 宿主信息，请在 DSH 插件管理中停用知衡。');
    await installFinancialPatch(state.home, {port: state.port, disabled: true});
    const deadline=Date.now()+15000;
    while(await runningStudio(root)){if(Date.now()>deadline)throw Error('已请求停用知衡，请检查 DSH Web 插件热加载状态；未停止共享宿主。');await new Promise(done=>setTimeout(done,250));}
    console.log('[知衡] 工作台已停用，共享 DSH Web 与其他插件继续运行。');
  } else {process.kill(state.pid, 'SIGTERM'); console.log('[知衡] 旧独立工作台已停止。');}
}
