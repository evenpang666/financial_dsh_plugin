import {mkdir,readFile,writeFile,rename,rm} from 'node:fs/promises';
import {join,resolve} from 'node:path';
import {homedir} from 'node:os';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {randomUUID} from 'node:crypto';
const execute=promisify(execFile);
const begin='# >>> financial launcher >>>',end='# <<< financial launcher <<<';
export const dshHome = (env=process.env) => resolve(env.DSH_HOME || join(homedir(),'.dsh'));
export function dshWebAddress(env=process.env) {
  const url=new URL(env.DSH_WEB_URL || `http://127.0.0.1:${env.DSH_WEB_PORT || 3080}`);
  if(url.protocol!=='http:' || !['127.0.0.1','localhost','[::1]'].includes(url.hostname) || url.username || url.password || url.pathname!=='/' || url.search || url.hash || Number(url.port||80)<1) throw Error('DSH_WEB_URL 必须是本机 DSH Web 的 HTTP 根地址，不含认证参数。');
  return url.origin;
}
export function dshProfile(args) {
  const index=args.findIndex(arg=>/(?:^|[/\\])dsh(?:[/\\]lib)?[/\\]bin\.[cm]?js$|(?:^|[/\\])dsh(?:\.exe)?$/i.test(arg));
  if(index<0)return null;
  const options=args.slice(index+1);
  if(options.some(arg=>['plugin','--version','-V','--help','-h','--dump-config','--dump-default-config','--dump-config-schema'].includes(arg)))return null;
  const profile=options.indexOf('--profile');
  if(profile>=0)return options[profile+1] || null;
  return options.find(arg=>arg.startsWith('--profile='))?.slice(10) || (options[0]&&!options[0].startsWith('-')?options[0]:'web');
}
export async function dshProcesses({run=execute,platform=process.platform}={}) {
  // Process command lines are used only for identity and ports, and never logged.
  let rows;
  if(platform==='win32') {
    const script='$env:PSModulePath=(Join-Path $PSHOME "Modules")+";"+$env:PSModulePath; $p=Get-CimInstance Win32_Process | Where-Object { $_.Name -match "^(node|dsh)(\\.exe)?$" }; $c=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue); @($p | ForEach-Object { $id=$_.ProcessId; [pscustomobject]@{pid=$id; command=$_.CommandLine; ports=@($c | Where-Object OwningProcess -eq $id | Select-Object -ExpandProperty LocalPort -Unique)} }) | ConvertTo-Json -Depth 4 -Compress';
    const {stdout}=await run('powershell.exe',['-NoProfile','-Command',script],{timeout:15000,windowsHide:true});
    const parsed=JSON.parse(stdout.trim() || '[]');rows=Array.isArray(parsed)?parsed:[parsed];
  } else {
    const {stdout}=await run('ps',['-axo','pid=,command='],{timeout:10000});
    rows=stdout.split('\n').flatMap(line=>{const match=line.trim().match(/^(\d+)\s+(.+)$/);return match?[{pid:Number(match[1]),command:match[2],ports:[]}]:[];});
  }
  return rows.flatMap(row=>{
    if(row.pid===process.pid)return [];
    const args=(row.command||'').match(/"[^"]*"|\S+/g)?.map(arg=>arg.replace(/^"|"$/g,'')) || [], profile=dshProfile(args);
    if(!profile)return [];
    const flag=args.find(arg=>/^--port=\d+$/.test(arg))?.slice(7) || args[args.indexOf('--port')+1];
    const ports=[...new Set([...(Array.isArray(row.ports)?row.ports:[]),Number(flag)].filter(port=>Number.isInteger(port)&&port>0&&port<=65535))];
    return [{pid:row.pid,profile,ports}];
  });
}
export async function probeDshWeb(base,{request=fetch}={}) {
  let response;
  try {response=await request(new URL('/',base),{signal:AbortSignal.timeout(1500),redirect:'manual'});}
  catch(error) {if(error.cause?.code==='ECONNREFUSED')return 'stopped';throw Error(`无法确认 ${base} 的 DSH Web 状态，请检查该服务后重试。`);}
  const reader=response.body?.getReader();let text='';
  try {while(reader&&text.length<16384){const {value,done}=await reader.read();if(done)break;text+=new TextDecoder().decode(value);}}finally{await reader?.cancel().catch(()=>{});}
  return (response.status===401 && /dsh web authentication required|DSH_WEB_AUTH|dsh browser authentication/i.test(text)) || response.ok && /<title>\s*(?:DeepSeek Harness|DSH)(?:\s*[-·|][^<]*)?\s*<\/title>/i.test(text) ? 'running':'occupied';
}
export async function discoverDshWeb(env=process.env,{processes=dshProcesses,probe=probeDshWeb}={}) {
  const preferred=dshWebAddress(env), initial=await probe(preferred);
  if(initial==='running')return {running:true,base:preferred,pids:[]};
  const hosts=(await processes()).filter(row=>row.profile==='web');
  const explicit=Boolean(env.DSH_WEB_URL||env.DSH_WEB_PORT);
  const candidates=new Set([preferred,...(!explicit?hosts.flatMap(row=>row.ports.map(port=>`http://127.0.0.1:${port}`)):[])]);
  for(const base of candidates){if(base===preferred)continue;const status=await probe(base);if(status==='running')return {running:true,base,pids:hosts.map(row=>row.pid)};}
  if(explicit&&hosts.length)throw Error('已检测到 DSH Web，但指定地址未就绪；未启动第二个服务，请检查 DSH_WEB_URL / DSH_WEB_PORT。');
  return {running:hosts.length>0,base:preferred,pids:hosts.map(row=>row.pid),occupied:initial==='occupied'};
}
export async function installFinancialPatch(home,{port=3205,disabled=false,python}={}) {
  await installProfilePatch(home,`- id: financial\n  disabled: ${disabled}\n  config:\n    port: ${port}\n${python?`    python: ${JSON.stringify(python)}\n`:''}`);
}

export function profilePatch(original, fragment) {
  let text = original;
  const first = text.indexOf(begin), last = text.indexOf(end);
  if ((first >= 0) !== (last >= 0) || last >= 0 && last < first) throw new Error('知衡配置标记不完整，请检查 DSH profile 的 cordis.patch.yml。');
  if (first >= 0) text = text.slice(0, first) + text.slice(last + end.length).replace(/^\r?\n/, '');
  if (text.split(/\r?\n/).filter(line => line.trim() && !line.trim().startsWith('#')).join('\n').trim() === '[]') text = text.replace(/^\s*\[\]\s*$/m, '');
  if (text.trim().startsWith('[')) {
    let entries;
    try { entries = JSON.parse(text); } catch { throw new Error('DSH profile 使用了无法安全追加的流式 YAML，请改为标准 YAML 列表后重试。'); }
    text = entries.map(entry => `- ${JSON.stringify(entry)}`).join('\n');
  }
  return `${text}${text && !text.endsWith('\n') ? '\n' : ''}${begin}\n${fragment.trimEnd()}\n${end}\n`;
}

export async function installProfilePatch(home, fragment) {
  const profile = join(home, 'profiles/web'), path = join(profile, 'cordis.patch.yml');
  await mkdir(profile, { recursive: true });
  let original;
  try { original = await readFile(path, 'utf8'); } catch (error) { if (error.code !== 'ENOENT') throw error; original = ''; }
  const next = profilePatch(original, fragment);
  if (next === original) return;
  const temporary = `${path}.${randomUUID()}.tmp`;
  try {
    await writeFile(temporary, next, { mode: 0o600 });
    let current;
    try { current = await readFile(path, 'utf8'); } catch (error) { if (error.code !== 'ENOENT') throw error; current = ''; }
    if (current !== original) throw new Error('DSH 配置同时被修改，请重新启动知衡以重试。');
    await rename(temporary, path);
  } finally { await rm(temporary, { force: true }); }
}
