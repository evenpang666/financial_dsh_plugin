import { ensureEnvironment, runInstaller } from './dsh-runtime.mjs';
const runtime = await ensureEnvironment();
await runInstaller(process.execPath, [runtime.dsh, ...process.argv.slice(2)], { env: runtime.env, cwd: process.cwd() });
