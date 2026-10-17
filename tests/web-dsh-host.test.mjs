import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {resolve} from 'node:path';
import {dshProfile,dshWebAddress,discoverDshWeb,probeDshWeb,profilePatch} from '../scripts/dsh-host.mjs';
import {connectWebWorkspace,redactStartup} from '../scripts/web-host.mjs';
import {retireLegacyStudio} from '../scripts/launch.mjs';

test('DSH identity distinguishes Web, legacy financial and installer commands',()=>{
  const cli='C:\\Program Files\\tools\\node_modules\\@deepseek-ai\\dsh\\lib\\bin.js';
  assert.equal(dshProfile(['node',cli,'web','--no-open']),'web');
  assert.equal(dshProfile(['node',cli,'--profile=web','--port','3082']),'web');
  assert.equal(dshProfile(['node',cli,'--profile','financial']),'financial');
  assert.equal(dshProfile(['node',cli,'plugin','--profile','web','add','x']),null);
  assert.equal(dshProfile(['node',cli,'--profile','web','--dump-default-config']),null);
  assert.equal(dshProfile(['node','other-app.js','web']),null);
});
test('DSH URLs stay on loopback and never accept authentication parameters',()=>{
  assert.equal(dshWebAddress({}),'http://127.0.0.1:3080');
  assert.equal(dshWebAddress({DSH_WEB_PORT:'3082'}),'http://127.0.0.1:3082');
  for(const value of ['https://example.com','http://192.168.1.2:3080','http://127.0.0.1:3080/?token=secret','http://user:x@localhost:3080','http://127.0.0.1:3080/path'])assert.throws(()=>dshWebAddress({DSH_WEB_URL:value}));
});
test('discovery reuses custom-port DSH while excluding independent financial and unrelated listeners',async()=>{
  const calls=[];
  const result=await discoverDshWeb({}, {processes:async()=>[{pid:1,profile:'financial',ports:[3205]},{pid:2,profile:'web',ports:[3210,3082]}],probe:async base=>{calls.push(base);return base.endsWith('3082')?'running':base.endsWith('3210')?'occupied':'stopped';}});
  assert.equal(result.base,'http://127.0.0.1:3082');assert.deepEqual(result.pids,[2]);assert.ok(!calls.some(url=>url.endsWith('3205')));
});
test('an existing Web process still starting is retained instead of starting a duplicate',async()=>{
  const result=await discoverDshWeb({}, {processes:async()=>[{pid:2,profile:'web',ports:[]}],probe:async()=> 'stopped'});
  assert.equal(result.running,true);
  await assert.rejects(discoverDshWeb({DSH_WEB_PORT:'3083'},{processes:async()=>[{pid:2,profile:'web',ports:[3082]}],probe:async()=> 'stopped'}),/未启动第二个/);
});
test('HTTP probes identify authenticated DSH, ordinary sites, absence and uncertain failures',async()=>{
  assert.equal(await probeDshWeb('http://localhost:3080',{request:async()=>new Response('dsh web authentication required',{status:401})}),'running');
  assert.equal(await probeDshWeb('http://localhost:3080',{request:async()=>new Response('<title>DeepSeek Harness</title>')}),'running');
  assert.equal(await probeDshWeb('http://localhost:3080',{request:async()=>new Response('<title>Other site</title>')}),'occupied');
  assert.equal(await probeDshWeb('http://localhost:3080',{request:async()=>{throw Object.assign(Error('refused'),{cause:{code:'ECONNREFUSED'}});}}),'stopped');
  await assert.rejects(probeDshWeb('http://localhost:3080',{request:async()=>{throw Error('timed out');}}),/无法确认/);
});
test('financial patch is idempotent and preserves other plugin configuration',()=>{
  const original='# user configuration\n- id: whereto\n  config:\n    port: 3101\n';
  const fragment='- id: financial\n  disabled: false\n  config:\n    port: 3205\n';
  const next=profilePatch(original,fragment);assert.ok(next.startsWith(original));assert.equal(profilePatch(next,fragment),next);
  assert.match(profilePatch(next,'- id: financial\n  disabled: true\n'),/id: whereto/);
  assert.equal((next.match(/id: financial/g)||[]).length,1);
  assert.throws(()=>profilePatch('# >>> financial launcher >>>\n',fragment),/标记不完整/);
});
function fixture(overrides={}) {
  const home=resolve('.test-tmp/fake-dsh-home'),events=[],child=new EventEmitter();child.pid=101;child.kill=()=>{events.push('kill');};
  const options={root:resolve('.'),runtime:{env:{DSH_HOME:home},dsh:'dsh-cli.js',version:'0.2.0'},python:'project-python',
    discover:async()=>({running:true,base:'http://127.0.0.1:3082'}),
    register:async()=>events.push('register'),patch:async(_home,config)=>{assert.equal(_home,home);assert.equal(config.port,3205);events.push('patch');},
    readStudio:async()=>({pid:101,profile:'web',home,url:'http://127.0.0.1:3205',dshWebUrl:'http://127.0.0.1:3082'}),
    spawnImpl:(_command,args,{env})=>{events.push('spawn');assert.deepEqual(args.slice(1,4),['--profile','web','--no-open']);assert.equal(env.DSH_HOME,home);assert.equal(env.FINANCE_PYTHON,'project-python');return child;},
    selectPort:async()=>3082,wait:async()=>{},log:()=>{},timeout:100,...overrides};
  return {options,events,child};
}
test('ready shared Web is attached without spawning or owning the host',async()=>{
  const {options,events}=fixture();const result=await connectWebWorkspace(options);
  assert.deepEqual(events,['register','patch']);assert.equal(result.reused,true);assert.equal(result.child,undefined);
});
test('no existing Web starts the Web profile and waits for the owned plugin',async()=>{
  const {options,events,child}=fixture({discover:async()=>({running:false,base:'http://127.0.0.1:3080'})});
  const result=await connectWebWorkspace(options);assert.equal(result.child,child);assert.equal(result.reused,false);assert.deepEqual(events,['register','patch','spawn']);
  child.emit('exit',0);await result.exited;
});
test('another host appearing during registration is reused',async()=>{
  let calls=0;const {options,events}=fixture({discover:async()=>({running:++calls>1,base:'http://127.0.0.1:3082'})});
  const result=await connectWebWorkspace(options);assert.equal(result.reused,true);assert.ok(!events.includes('spawn'));
});
test('attach failure never starts or kills someone else’s DSH Web',async()=>{
  const {options,events}=fixture({readStudio:async()=>null,timeout:0});
  await assert.rejects(connectWebWorkspace(options),/未启动第二个/);assert.deepEqual(events,['register','patch']);
});
test('startup failure cleans up only the child created by this launcher',async()=>{
  const {options,events}=fixture({discover:async()=>({running:false,base:'http://127.0.0.1:3080'}),readStudio:async()=>null,timeout:0});
  await assert.rejects(connectWebWorkspace(options),/插件未就绪/);assert.deepEqual(events,['register','patch','spawn','kill']);
});
test('legacy migration refuses busy jobs and never terminates a shared host',async()=>{
  const state={pid:5,url:'http://127.0.0.1:3205'},kill=()=>assert.fail('must not kill');
  await assert.rejects(retireLegacyStudio('.', {read:async()=>state,processes:async()=>[{pid:5,profile:'financial'}],request:async()=>new Response(JSON.stringify({jobs:[{status:'running'}]})),kill}),/仍有任务/);
  await retireLegacyStudio('.', {read:async()=>({...state,profile:'web'}),processes:async()=>assert.fail('shared host must be retained'),kill});
});
test('idle legacy finance process is migrated only after positive process identification',async()=>{
  let killed=false;await retireLegacyStudio('.', {read:async()=>killed?null:{pid:5,url:'http://127.0.0.1:3205'},processes:async()=>[{pid:5,profile:'financial'}],request:async()=>new Response(JSON.stringify({jobs:[]})),kill:(pid,signal)=>{assert.equal(pid,5);assert.equal(signal,'SIGTERM');killed=true;}});
  assert.ok(killed);
});
test('startup diagnostics redact DSH authentication links and bearer values',()=>{
  const secret='never-print-this';const line=redactStartup(`dsh web: http://127.0.0.1:3080/?token=${secret}&next=/\nBearer ${secret}`);
  assert.ok(!line.includes(secret));assert.match(line,/\[已隐藏\]/);
});
