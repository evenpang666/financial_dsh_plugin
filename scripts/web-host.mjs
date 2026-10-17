import {spawn} from 'node:child_process';
import {createServer} from 'node:net';
import {discoverDshWeb,dshHome,installFinancialPatch} from './dsh-host.mjs';
import {registerFinancial} from './register-plugin.mjs';
const delay=ms=>new Promise(done=>setTimeout(done,ms));
export const redactStartup = text => text.replace(/([?&](?:token|access_token|auth|key)=)[^\s&#\x1b]+/gi,'$1[已隐藏]').replace(/(Bearer\s+)[\w.\-]+/gi,'$1[已隐藏]');
function relay(stream,write) {
  let buffer='';stream?.setEncoding('utf8');
  stream?.on('data',chunk=>{buffer+=chunk;const lines=buffer.split(/\r?\n/);buffer=lines.pop();for(const line of lines)write(redactStartup(line)+'\n');});
  stream?.once('end',()=>{if(buffer)write(redactStartup(buffer)+'\n');});
}
export async function selectWebPort(preferred) {
  const bind=port=>new Promise((done,reject)=>{const server=createServer();server.once('error',reject);server.listen(port,'127.0.0.1',()=>{const actual=server.address().port;server.close(error=>error?reject(error):done(actual));});});
  try{return await bind(preferred);}catch(error){if(!['EADDRINUSE','EACCES'].includes(error.code))throw error;return bind(0);}
}
export async function connectWebWorkspace({root,runtime,python,preferredPort=3205,readStudio,
  discover=discoverDshWeb,register=registerFinancial,patch=installFinancialPatch,spawnImpl=spawn,selectPort=selectWebPort,
  wait=delay,timeout=90000,log=console.log}={}) {
  const env={...runtime.env,FINANCE_PYTHON:python,DSH_HOME:dshHome(runtime.env),DSH_TELEMETRY_DISABLED:'1'};
  let host=await discover(env);
  await register({root,env,locate:async()=>runtime.dsh,version:runtime.version});
  await patch(env.DSH_HOME,{port:preferredPort,python});
  // Another launcher may have brought up Web during plugin registration.
  host=await discover(env);
  let child,exited,stop,exitResult;
  if(host.running)log(`[知衡] 复用现有 DSH Web：${host.base}；正在接入知衡插件…`);
  else {
    if(host.occupied&&(env.DSH_WEB_URL||env.DSH_WEB_PORT))throw Error('指定的 DSH Web 地址被其他服务占用，请检查配置。');
    const port=await selectPort(Number(new URL(host.base).port || 80));
    host.base=`http://127.0.0.1:${port}`;
    log(`[知衡] 未检测到 DSH Web，启动共享 Web 服务：${host.base}`);
    child=spawnImpl(process.execPath,[runtime.dsh,'--profile','web','--no-open','--host','127.0.0.1','--port',String(port)],{cwd:root,env,stdio:['ignore','pipe','pipe'],windowsHide:true});
    relay(child.stdout,text=>process.stdout.write(text));relay(child.stderr,text=>process.stderr.write(text));
    exited=new Promise(done=>{child.once('error',error=>{exitResult={error};done(exitResult);});child.once('exit',(code,signal)=>{exitResult={code,signal};done(exitResult);});});
    stop=()=>child.kill('SIGTERM');
  }
  const deadline=Date.now()+timeout;
  try {
    while(Date.now()<deadline) {
      if(exitResult)throw Error(`DSH Web 启动失败（${exitResult.error?.message || exitResult.code || exitResult.signal}）。`);
      const studio=await readStudio(root);
      if(studio?.profile==='web'&&(!studio.home||studio.home===env.DSH_HOME)&&(!child||studio.pid===child.pid)) {
        log(`[知衡] DSH Web：${studio.dshWebUrl||host.base}`);
        return {url:studio.url,child,exited,stop,reused:!child};
      }
      await wait(250);
    }
    throw Error(child?'DSH Web 已启动，但知衡插件未就绪，请检查插件日志。':'已复用 DSH Web，但知衡插件未完成热加载。请检查原服务的插件启用状态和 HMR；未启动第二个 DSH 服务。');
  } catch(error){if(child)stop();throw error;}
}
