import { spawn } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import {fileURLToPath} from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
import { pythonPath } from '../lib/workers.mjs';
const temp=join(root,'.test-tmp');await mkdir(temp,{recursive:true});
const child=spawn(pythonPath(root),['-m','unittest','discover','-s','tests','-t','.','-p','test_*.py'],{cwd:root,windowsHide:true,stdio:'inherit',env:{...process.env,TEMP:temp,TMP:temp,PYTHONUTF8:'1',PYTHONIOENCODING:'utf-8'}});
child.once('error',e=>{console.error(e.message);process.exitCode=1;});child.once('exit',code=>{process.exitCode=code||0;});
