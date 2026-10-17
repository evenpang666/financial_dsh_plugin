import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtemp, mkdir, writeFile, readFile, access, rm, unlink} from 'node:fs/promises';
import {resolve, join, sep, dirname} from 'node:path';

const root = resolve('.'), hooks = join(root, '.githooks'), temporary = join(root, '.test-tmp');
function git(directory, args, {input, fail = false} = {}) {
  const result = spawnSync('git', ['-C', directory, ...args], {input, encoding: 'utf8', windowsHide: true});
  if (result.error) throw result.error;
  if (!fail && result.status !== 0) throw Error(`Git test command failed: ${result.stderr}`);
  return result;
}
async function fixture(t) {
  await mkdir(temporary, {recursive: true}); const directory = await mkdtemp(join(temporary, 'git-hygiene-'));
  t.after(async () => { if (!resolve(directory).startsWith(temporary + sep)) throw Error('Unexpected cleanup path'); await rm(directory, {recursive: true, force: true}); });
  git(directory, ['init', '-q']);
  git(directory, ['config', 'user.name', 'Git hygiene test']); git(directory, ['config', 'user.email', 'test@example.invalid']);
  git(directory, ['config', 'core.hooksPath', hooks]);
  await writeFile(join(directory, '.gitignore'), await readFile(join(root, '.gitignore')));
  git(directory, ['add', '.gitignore']); git(directory, ['commit', '-qm', 'Initial test repository']);
  return directory;
}
test('ignore rules protect runtime data and credentials while retaining templates and verified corrections', async t => {
  const directory = await fixture(t);
  const ignored = ['.env', '.env.production', 'config/tushare.token', '.credentials.yaml', 'credentials.json', '.npmrc', 'id_ed25519', 'data/new-cache.json', 'data/factor-lab/models/model.json', 'output/chart.png', '.pytest_cache/state', 'scripts/__pycache__/server.pyc', 'worker.log'];
  const publicFiles = ['.env.example', '.env.production.example', 'credentials.example.json', 'config/agent-profile.example.yml', 'config/daily-push.example.json', 'data-corrections/minute/verified.csv', 'docs/assets/screenshot.png'];
  const result = git(directory, ['check-ignore', '--no-index', '--stdin'], {input: [...ignored, ...publicFiles].join('\n') + '\n'});
  assert.deepEqual(new Set(result.stdout.trim().split(/\r?\n/)), new Set(ignored));
});
test('pre-commit rejects force-added ignored files while preserving their local copies', async t => {
  const directory = await fixture(t), path = join(directory, 'scripts/__pycache__/server.pyc');
  await mkdir(dirname(path), {recursive: true}); await writeFile(path, 'cache');
  git(directory, ['add', '-f', 'scripts/__pycache__/server.pyc']);
  const commit = git(directory, ['commit', '-qm', 'Must be refused'], {fail: true});
  assert.notEqual(commit.status, 0); assert.match(commit.stderr, /ignored credentials, local settings or generated files/); await access(path);
});
test('pre-commit rejects embedded provider tokens without printing their values', async t => {
  const directory = await fixture(t), secret = 'sk-' + 'A'.repeat(36);
  await writeFile(join(directory, 'settings.js'), `export const apiKey = '${secret}';\n`);
  git(directory, ['add', 'settings.js']);
  const commit = git(directory, ['commit', '-qm', 'Must be refused'], {fail: true});
  assert.notEqual(commit.status, 0); assert.match(commit.stderr, /possible credential content/);
  assert.equal((commit.stdout + commit.stderr).includes(secret), false);
});
test('pre-push catches a secret added then removed from history', async t => {
  const directory = await fixture(t), secret = 'sk-' + 'B'.repeat(36), disabled = join(directory, 'disabled-hooks');
  await mkdir(disabled); await writeFile(join(directory, 'settings.js'), `export const apiKey = '${secret}';\n`);
  git(directory, ['add', 'settings.js']); git(directory, ['-c', `core.hooksPath=${disabled}`, 'commit', '-qm', 'Simulated historical leak']);
  await unlink(join(directory, 'settings.js')); git(directory, ['add', '-u']); git(directory, ['commit', '-qm', 'Remove test credential']);
  const remote = join(directory, 'remote.git'); git(directory, ['init', '--bare', '-q', remote]);
  const push = git(directory, ['push', '--dry-run', remote, 'HEAD:refs/heads/audit'], {fail: true});
  assert.notEqual(push.status, 0); assert.match(push.stderr, /possible credential content/);
  assert.equal((push.stdout + push.stderr).includes(secret), false);
});
test('pre-push permits public templates but rejects a removed credential file in history', async t => {
  const directory = await fixture(t), disabled = join(directory, 'disabled-hooks'), remote = join(directory, 'remote.git');
  await mkdir(disabled); await writeFile(join(directory, '.env.example'), 'API_KEY=\n');
  git(directory, ['add', '.env.example']); git(directory, ['commit', '-qm', 'Public template']);
  git(directory, ['init', '--bare', '-q', remote]); git(directory, ['push', '--dry-run', remote, 'HEAD:refs/heads/audit']);
  await writeFile(join(directory, '.env'), 'LOCAL_SETTING=unused\n'); git(directory, ['add', '-f', '.env']);
  git(directory, ['-c', `core.hooksPath=${disabled}`, 'commit', '-qm', 'Simulated credential file']);
  await unlink(join(directory, '.env')); git(directory, ['add', '-u']); git(directory, ['commit', '-qm', 'Remove local file']);
  const push = git(directory, ['push', '--dry-run', remote, 'HEAD:refs/heads/audit'], {fail: true});
  assert.notEqual(push.status, 0); assert.match(push.stderr, /reachable history contains a credential file/);
});
