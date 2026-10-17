import test from 'node:test';
import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdtemp,mkdir,readFile,rm,readdir} from 'node:fs/promises';
import {join,resolve,dirname} from 'node:path';
const execute=promisify(execFile);
for (const corrupt of [false,true]) test(corrupt?'Windows bootstrap rejects a bad checksum and preserves the old runtime':'Windows bootstrap rejects old Node, provisions a checked local runtime when absent and forwards arguments',{skip:process.platform!=='win32'},async t=>{
  const parent=resolve('.test-tmp');await mkdir(parent,{recursive:true});const root=await mkdtemp(join(parent,'bootstrap with spaces-'));
  t.after(async()=>{assert.equal(dirname(root),parent);await rm(root,{recursive:true,force:true});});
  const {stdout}=await execute('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',resolve('tests/windows_bootstrap.ps1'),'-FixtureRoot',root,'-ActualNode',process.execPath,...(corrupt?['-CorruptChecksum']:[])],{timeout:60000,windowsHide:true});
  if(corrupt) {
    assert.match(stdout,/checksum mismatch/);assert.match(await readFile(join(root,'.runtime/node/current/retained.txt'),'utf8'),/old runtime retained/);
    assert.deepEqual(await readdir(join(root,'.runtime/node')),['current']);return;
  }
  const result=JSON.parse(await readFile(join(root,'result.json'),'utf8'));
  assert.equal(result.node,join(root,'.runtime/node/current/node.exe'));assert.deepEqual(result.args,['--no-open']);
  assert.equal(result.npm,join(root,'.runtime/node/current/node_modules/npm/bin/npm-cli.js'));
  assert.deepEqual(await readdir(join(root,'.runtime/node')),['current']);
});
