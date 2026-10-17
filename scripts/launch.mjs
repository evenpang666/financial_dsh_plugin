import {spawn} from 'node:child_process';
import {readFile,mkdir,unlink,open} from 'node:fs/promises';
import {join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {ensureEnvironment} from './dsh-runtime.mjs';
import {ensurePython} from './python-runtime.mjs';
import {logWorkspaceAccess} from '../lib/access-log.mjs';
import {dshProcesses,dshWebAddress} from './dsh-host.mjs';
import {connectWebWorkspace} from './web-host.mjs';

const root=fileURLToPath(new URL('../',import.meta.url));
const delay=ms=>new Promise(done=>setTimeout(done,ms));
export async function readStudioState(directory=root) {
  try {
    const state=JSON.parse(await readFile(join(directory,'.runtime/web.json'),'utf8'));
    if(!Number.isInteger(state.pid)||state.pid<=0||!Number.isInteger(state.port)||state.port<1||state.port>65535)return null;
    process.kill(state.pid,0);
    const url=`http://127.0.0.1:${state.port}`;
    const response=await fetch(url+'/api/status',{signal:AbortSignal.timeout(1500)});
    return response.ok&&(await response.json()).name==='dsh-financial'?{...state,url}:null;
  }catch{return null;}
}
export async function runningStudio(directory=root){return (await readStudioState(directory))?.url || null;}
function openBrowser(url) {
  if(process.argv.includes('--no-open'))return;
  const command=process.platform==='win32'?'rundll32':process.platform==='darwin'?'open':'xdg-open';
  const child=spawn(command,process.platform==='win32'?['url.dll,FileProtocolHandler',url]:[url],{stdio:'ignore',detached:true,windowsHide:true});
  child.on('error',()=>console.log(`请手动打开 ${url}`));child.unref();
}
export async function retireLegacyStudio(directory=root,{read=readStudioState,processes=dshProcesses,request=fetch,kill=process.kill,wait=delay}={}) {
  const state=await read(directory);if(!state||state.profile==='web')return;
  const owner=(await processes()).find(row=>row.pid===state.pid);
  if(owner?.profile==='web')return;
  if(owner?.profile!=='financial')throw Error('检测到旧知衡服务，但无法确认其宿主，请先关闭原启动窗口再重试。');
  const response=await request(state.url+'/api/jobs',{signal:AbortSignal.timeout(1500)});
  if(!response.ok)throw Error('无法检查旧知衡任务状态，保留原服务。');
  if((await response.json()).jobs.some(job=>job.status==='running'))throw Error('旧知衡仍有任务运行，请等任务结束后再次双击启动，迁移到 DSH Web。');
  console.log('[知衡] 旧独立金融服务已空闲，正在迁移到共享 DSH Web…');kill(state.pid,'SIGTERM');
  const deadline=Date.now()+15000;while(await read(directory)){if(Date.now()>deadline)throw Error('旧金融服务未退出，请关闭原启动窗口后重试。');await wait(200);}
}
export async function launch() {
  const preferredPort=Number(process.env.FINANCE_PORT || 3205);dshWebAddress();
  if(!Number.isInteger(preferredPort)||preferredPort<0||preferredPort>65535)throw Error('FINANCE_PORT 必须为 0–65535 的整数。');
  const runtime=await ensureEnvironment(),python=await ensurePython({root,env:runtime.env});
  await retireLegacyStudio(root);
  const runtimeDir=join(root,'.runtime');await mkdir(runtimeDir,{recursive:true});const lock=join(runtimeDir,'launch.lock');
  let ownsLock=false;
  const release=async()=>{if(ownsLock){ownsLock=false;await unlink(lock).catch(()=>{});}};
  try {
    const deadline=Date.now()+90000;
    while(!ownsLock) {
      try {const file=await open(lock,'wx');await file.writeFile(String(process.pid));await file.close();ownsLock=true;}
      catch(error){
        if(error.code!=='EEXIST')throw error;
        let alive=false;try{const pid=Number(await readFile(lock,'utf8'));if(Number.isInteger(pid)&&pid>0){process.kill(pid,0);alive=true;}}catch{}
        if(!alive){await unlink(lock).catch(()=>{});continue;}
        const ready=await readStudioState(root);if(ready?.profile==='web'){await logWorkspaceAccess(ready.url,{prefix:'[知衡]'});openBrowser(ready.url);return;}
        if(Date.now()>deadline)throw Error('另一启动流程尚未完成，请检查原启动窗口。');await delay(250);
      }
    }
    const host=await connectWebWorkspace({root,runtime,python,preferredPort,readStudio:readStudioState});
    await release();await logWorkspaceAccess(host.url,{prefix:'[知衡]'});openBrowser(host.url);
    if(host.exited){
      process.once('SIGINT',host.stop);process.once('SIGTERM',host.stop);
      try{const result=await host.exited;if(result.error||result.code)throw Error(`DSH Web 退出 (${result.error?.message || result.code})。`);}
      finally{process.off('SIGINT',host.stop);process.off('SIGTERM',host.stop);}
    }
  }finally{await release();}
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url))launch().catch(error=>{console.error('[知衡] '+error.message);process.exitCode=1;});
