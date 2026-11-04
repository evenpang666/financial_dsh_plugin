import test from 'node:test';
import assert from 'node:assert/strict';
import {createResearchAgent} from '../lib/agent.mjs';
import {supportsDsh} from '../scripts/dsh-version.mjs';
import {mkdtemp, mkdir, writeFile, rm} from 'node:fs/promises';
import {join} from 'node:path';
test('accepts DSH 0.2.0 stable and supported RC while rejecting older versions', () => {
  assert.equal(supportsDsh('0.2.0'), true); assert.equal(supportsDsh('0.2.0-rc.2'), true); assert.equal(supportsDsh('0.1.9'), false);
});

function hostFixture() {
  const captured = {}, events = [], session = {seq: 0, eventAt: i => events[i]};
  const agent = {session, whenIdle: async () => {}, cancel() {}, followup(message) {
    captured.message = message;
    events.push({type: 'assistant/message', data: {message: {content: [{type: 'text', text: '完成'}]}}}, {type: 'turn/end', data: {reason: {kind: 'completed'}}}); session.seq = events.length;
  }};
  const ctx = {get: () => null, agentDefaultModel: {currentSelection: () => ({provider: 'host', model: 'default', reasoningEffort: 'high'})},
    sessions: {flush: async () => {captured.flushed = true;}}, agents: {create: async options => {
      captured.options = options;
      options.setup({tools: {restrict: value => {captured.allowed = value.allow;}, presentAs() {}}});
      return {agent, dispose: async () => {captured.disposed = true;}};
    }}};
  return {ctx, captured};
}

test('DSH bridge applies workspace model overrides and keeps AI analysis free of writing tools', async () => {
  const {ctx, captured} = hostFixture(), research = createResearchAgent(ctx, process.cwd(), ['stock_search', 'set_portfolio']);
  await research('研究'); assert.deepEqual(captured.allowed, ['stock_search']);
  const result = await research('策略JSON', {purpose: 'ai_simulation', allowedTools: [], provider: 'configured', model: 'vision'});
  assert.equal(captured.options.agentOptions.provider, 'configured'); assert.equal(captured.options.agentOptions.model, 'vision');
  assert.equal(captured.options.agentOptions.reasoningEffort, undefined); assert.deepEqual(captured.allowed, []);
  assert.match(captured.message.content[0].text, /仅分析给定本地证据/); assert.equal(result.model, 'vision');
  assert.ok(captured.flushed && captured.disposed);
});

test('DSH screenshots enter the host attachment service as bytes and followup receives durable references', async t => {
  await mkdir('.test-tmp', {recursive: true}); const root = await mkdtemp(join(process.cwd(), '.test-tmp/agent-image-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const image = join(root, 'holdings.png'), bytes = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG04AAAAASUVORK5CYII=', 'base64');
  await writeFile(image, bytes);
  const {ctx, captured} = hostFixture(), attachment = {attachmentId: 'fixture', mediaType: 'image/png', bytes: bytes.length, width: 1, height: 1};
  ctx.attachments = {admitPromptContent: async parts => {
    assert.equal(parts[1].mediaType, 'image/png'); assert.deepEqual(Buffer.from(parts[1].data, 'base64'), bytes);
    return [parts[0], {type: 'image', attachment}];
  }};
  ctx.llm = {resolveModelInfo: async () => ({inputModalities: ['text', 'image']})};
  const research = createResearchAgent(ctx, root, ['stock_search']);
  await research('提取JSON', {purpose: 'portfolio', images: [image], allowedTools: []});
  assert.deepEqual(captured.message.content[1], {type: 'image', attachment}); assert.deepEqual(captured.allowed, []);
  ctx.llm.resolveModelInfo = async () => ({inputModalities: ['text']});
  await assert.rejects(research('提取JSON', {images: [image]}), /不支持图片/);
  ctx.attachments = undefined;
  await assert.rejects(research('提取JSON', {images: [image]}), /附件服务不可用/);
});
test('DSH agent inherits host model, limits tools, flushes and disposes the session', async () => {
  let options, flushed = false, disposed = false, allowed;
  const events = [];
  const session = { seq: 0, eventAt: i => events[i] };
  const agent = { session, whenIdle: async () => {}, cancel() {}, followup() { events.push({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '查证完成' }] } } }, { type: 'turn/end', data: { reason: { kind: 'completed' } } }); session.seq = events.length; } };
  const ctx = { get: () => null, agentDefaultModel: { currentSelection: () => ({ provider: 'host', model: 'configured' }) }, sessions: { flush: async () => { flushed = true; } }, agents: { create: async value => { options = value; value.setup({ tools: { restrict: v => { allowed = v.allow; }, presentAs() {} } }); return { agent, dispose: async () => { disposed = true; } }; } } };
  const result = await createResearchAgent(ctx, process.cwd(), ['get_quote_snapshot'])('问题');
  assert.equal(options.agentOptions.model, 'configured'); assert.deepEqual(allowed, ['get_quote_snapshot']); assert.equal(result.text, '查证完成'); assert.ok(flushed && disposed);
  await createResearchAgent(ctx,process.cwd(),['stock_search','set_portfolio'])('持仓',{purpose:'portfolio',allowedTools:['stock_search']});
  assert.deepEqual(allowed,['stock_search']);
});
