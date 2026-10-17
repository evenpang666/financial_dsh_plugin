import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile, readFile, rm} from 'node:fs/promises';
import {join, resolve, dirname} from 'node:path';
import {ensureEnvironment, versions} from '../scripts/dsh-runtime.mjs';
import {ensurePython, installManagedPython} from '../scripts/python-runtime.mjs';
import {pythonPath} from '../lib/workers.mjs';
import {logWorkspaceAccess} from '../lib/access-log.mjs';

async function temporary(t) {
  const parent = resolve('.test-tmp'); await mkdir(parent,{recursive:true});
  const root = await mkdtemp(join(parent,'runtime-'));
  t.after(async()=>{assert.equal(dirname(root),parent);await rm(root,{recursive:true,force:true});});
  return root;
}
async function environment(t, {npmVersion='11.19.0',pnpmVersion='13.0.0',nodeVersion='24.21.0',required}={}) {
  const root=await temporary(t), installed=[], tools=new Map([['system-npm',npmVersion],['system-dsh','0.2.0'],['system-pnpm',pnpmVersion]]);
  if(required) {const profile=join(root,'profiles/web');await mkdir(profile,{recursive:true});await writeFile(join(profile,'package.json'),JSON.stringify({packageManager:`pnpm@${required}`}));}
  const options={root,env:{PATH:'',DSH_HOME:root},nodeVersion,locateNpm:async()=> 'system-npm',locateDsh:async()=> 'system-dsh',locatePnpm:async()=> 'system-pnpm',log:()=>{},
    inspect:async cli=>{if(!tools.has(cli))throw Object.assign(Error('missing'),{code:'ENOENT'});return tools.get(cli);},
    upgrade:({cli,version})=>({cli,version}),
    install:async spec=>{installed.push(spec);tools.set(join(spec.directory,'node_modules',spec.entry),spec.version);}
  };
  return {root,installed,options};
}
test('higher Node/npm/pnpm and supported DSH are reused without installation',async t=>{
  const {installed,options}=await environment(t);const result=await ensureEnvironment(options);
  assert.equal(result.npm,'system-npm');assert.equal(result.pnpm,'system-pnpm');assert.equal(result.version,'0.2.0');assert.equal(installed.length,0);
});
test('low npm and pnpm are upgraded only in the project and rechecked',async t=>{
  const {root,installed,options}=await environment(t,{npmVersion:'9.0.0',pnpmVersion:'10.0.0',required:'12.4.2'});
  const result=await ensureEnvironment(options);
  assert.deepEqual(installed.map(({name,version})=>[name,version]),[['npm',versions.npm],['pnpm','12.4.2']]);
  assert.ok(installed.every(spec=>spec.directory.startsWith(join(root,'.runtime/tools'))));assert.equal(installed[1].npm,result.npm);
  assert.equal(result.env.npm_execpath,result.npm);
});
test('a too-old Node fails clearly before package installation',async t=>{
  const {installed,options}=await environment(t,{nodeVersion:'22.12.9'});
  await assert.rejects(ensureEnvironment(options),/start\.bat/);assert.equal(installed.length,0);
});
test('a failed npm upgrade does not continue with the older tool',async t=>{
  const {options}=await environment(t,{npmVersion:'9.0.0'});options.install=async()=>{throw Error('download failed; original retained');};
  await assert.rejects(ensureEnvironment(options),/download failed/);
});
test('low DSH is passed through the shared version upgrade and higher DSH is retained',async t=>{
  const {options}=await environment(t);let upgraded=false;
  const inspect=options.inspect;options.inspect=cli=>cli==='system-dsh'?'0.1.9':inspect(cli);
  options.upgrade=({version})=>{assert.equal(version,'0.1.9');upgraded=true;return{cli:'shared-dsh',version:'0.2.1'};};
  const result=await ensureEnvironment(options);assert.ok(upgraded);assert.equal(result.dsh,'shared-dsh');assert.equal(result.version,'0.2.1');
});

const binary=directory=>join(directory,process.platform==='win32'?'Scripts/python.exe':'bin/python');
async function localPython(root) {const path=binary(join(root,'.venv'));await mkdir(dirname(path),{recursive:true});await writeFile(path,'original Python');return path;}
test('suitable project Python and higher dependencies require no installer',async t=>{
  const root=await temporary(t), python=await localPython(root);
  const result=await ensurePython({root,env:{},inspect:async()=>({path:python,version:'3.14.0'}),dependencies:async()=>[],run:async()=>assert.fail('must reuse higher versions'),managed:async()=>assert.fail('must not download'),log:()=>{}});
  assert.equal(result,python);assert.equal(await readFile(python,'utf8'),'original Python');
});
test('an old Python is replaced by an isolated venv using suitable system Python',async t=>{
  const root=await temporary(t), old=await localPython(root), calls=[];
  const result=await ensurePython({root,env:{},
    inspect:async command=>({path:command===old?old:command.includes('venvs')?command:resolve(root,'system/python'),version:command===old?'3.10.9':'3.13.1'}),
    dependencies:async()=>[],run:async(command,args)=>{calls.push([command,args]);const path=binary(args[2]);await mkdir(dirname(path),{recursive:true});await writeFile(path,'new Python');},managed:async()=>assert.fail('system Python is sufficient'),log:()=>{}});
  assert.equal(calls.length,1);assert.deepEqual(calls[0][1].slice(0,2),['-m','venv']);
  assert.equal(await readFile(old,'utf8'),'original Python');assert.equal(pythonPath(root,{}),result);
});
test('missing Python invokes the managed installer and checks the new runtime',async t=>{
  const root=await temporary(t);let downloads=0;
  const result=await ensurePython({root,env:{},inspect:async command=>command==='downloaded-python'||command.includes('venvs')?{path:command==='downloaded-python'?resolve(root,'base/python'):command,version:'3.12.9'}:null,
    managed:async()=>{downloads++;return'downloaded-python';},dependencies:async()=>[],run:async(_command,args)=>{const path=binary(args[2]);await mkdir(dirname(path),{recursive:true});await writeFile(path,'managed');},log:()=>{}});
  assert.equal(downloads,1);assert.equal(pythonPath(root,{}),result);
});
test('low or missing Python packages install requirements then verify, without --upgrade',async t=>{
  const root=await temporary(t), python=await localPython(root), calls=[];let checks=0;
  await ensurePython({root,env:{},inspect:async()=>({path:python,version:'3.11.7'}),dependencies:async()=>checks++?[]:['numpy 1.25.0 不满足 >=1.26.0'],run:async(command,args)=>calls.push([command,args]),log:()=>{}});
  assert.equal(checks,2);assert.deepEqual(calls[1][1],['-m','pip','install','--disable-pip-version-check','-r',join(root,'requirements.txt')]);
  assert.equal(await readFile(python,'utf8'),'original Python');
});
test('dependency failures remain explicit instead of starting an unusable worker',async t=>{
  const root=await temporary(t), python=await localPython(root);
  await assert.rejects(ensurePython({root,env:{},inspect:async()=>({path:python,version:'3.11.7'}),dependencies:async()=>['numpy 无法导入'],run:async()=>{},log:()=>{}}),/依赖验收失败/);
});
test('managed uv bootstrap installs locally and disables shell changes',async t=>{
  const root=await temporary(t), calls=[];let probes=0;
  const result=await installManagedPython(root,{}, {log:()=>{},download:async url=>{assert.match(url,/^https:\/\/astral\.sh\/uv\/install\./);return{ok:true,text:async()=> 'installer fixture'};},
    probe:async(_command,args)=>{if(probes++<2)throw Error('not installed');return{stdout:args[0]==='python'?resolve(root,'managed/python'):'uv 1.0.0'};},
    run:async(command,args,{env})=>{calls.push({command,args});assert.equal(env.UV_UNMANAGED_INSTALL,join(root,'.runtime/tools/uv'));assert.equal(env.UV_NO_MODIFY_PATH,'1');assert.equal(env.UV_PYTHON_INSTALL_REGISTRY,'0');assert.equal(env.UV_PYTHON_INSTALL_BIN,'0');assert.equal(env.UV_PYTHON_INSTALL_DIR,join(root,'.runtime/python'));}
  });
  assert.equal(calls.length,2);assert.equal(calls[1].args[0],'python');assert.equal(result,resolve(root,'managed/python'));
});
test('Python state cannot point outside the project managed environment',async t=>{
  const root=await temporary(t), python=await localPython(root);await mkdir(join(root,'.runtime'),{recursive:true});await writeFile(join(root,'.runtime/python.json'),JSON.stringify({path:'../../unrelated/python.exe'}));
  assert.equal(pythonPath(root,{}),python);
});
test('new and reused plugin logs include Ethernet URL with the actual port',async()=>{
  const lines=[];await logWorkspaceAccess('http://127.0.0.1:54321/',{network:{address:'192.168.10.106',adapter:'以太网'},log:line=>lines.push(line),request:()=>assert.fail('network already known')});
  assert.match(lines[1],/http:\/\/192\.168\.10\.106:54321\//);
  const reused=[];await logWorkspaceAccess('http://127.0.0.1:54321',{log:line=>reused.push(line),request:async url=>{assert.equal(String(url),'http://127.0.0.1:54321/api/network');return{ok:true,json:async()=>({lanUrl:'http://192.168.10.106:54321',adapter:'以太网'})};}});
  assert.match(reused[0],/本机/);assert.match(reused[1],/192\.168\.10\.106:54321/);
});
test('no Ethernet log is explicit rather than inventing a LAN address',async()=>{
  const lines=[];await logWorkspaceAccess('http://127.0.0.1:3205/',{network:null,log:line=>lines.push(line)});
  assert.match(lines[1],/未检测到/);assert.doesNotMatch(lines[1],/http/);
});
