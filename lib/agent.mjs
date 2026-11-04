import { randomUUID } from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {basename} from 'node:path';
import {decodePortfolioImage} from './portfolio-image.mjs';

// Agent selection and all provider credentials remain owned by DSH.
export function createResearchAgent(ctx, root, financeTools) {
  return async (prompt, { signal = AbortSignal.timeout(600000), allowedTools = financeTools.filter(name => name !== 'set_portfolio'), purpose = 'research', images = [], provider, model, reasoningEffort } = {}) => {
    if (!allowedTools.every(name => financeTools.includes(name))) throw Error('请求了未注册的金融工具。');
    await ctx.get('loader')?.await();
    signal.throwIfAborted();
    const sessionId = `financial-${randomUUID()}`;
    const selection = model ? {provider, model, ...(reasoningEffort ? {reasoningEffort} : {})} : ctx.agentDefaultModel.currentSelection();
    const prefix = purpose === 'portfolio' ? '你是知衡持仓录入助手。严格按提供的结构提取用户原文，不猜测数量，不执行写入或交易，只返回要求的JSON。\n\n'
      : purpose === 'ai_simulation' ? '你是知衡模拟策略分析助手。仅分析给定本地证据与策略，不调用额外工具，不执行交易。按指定 JSON 格式回答，缺失数据明确标出。\n\n'
      : '你是知衡股票研究助手。先调用本地金融工具查证，再用简洁中文回答。注明来源、as_of、缺失数据与结论失效条件；不得编造行情，不执行交易。倾向评分不是仓位或涨跌概率。\n\n';
    let content = [{type: 'text', text: prefix + prompt}];
    if (images.length) {
      if (images.length > 1 || !ctx.attachments?.admitPromptContent) throw Error('DSH 图片附件服务不可用，请检查宿主附件插件。');
      const bytes = await readFile(images[0]);
      const mediaType = bytes[0] === 137 ? 'image/png' : bytes[0] === 255 ? 'image/jpeg' : 'image/webp';
      const data = bytes.toString('base64'); decodePortfolioImage(`data:${mediaType};base64,${data}`);
      content = await ctx.attachments.admitPromptContent([...content, {type: 'image', mediaType, data, name: basename(images[0])}]);
      signal.throwIfAborted();
      if (ctx.llm?.resolveModelInfo) {
        const info = await ctx.llm.resolveModelInfo(selection.provider, selection.model, signal);
        if (info.inputModalities && !info.inputModalities.includes('image')) throw Error('当前 DSH 模型不支持图片，请在设置中选择支持图片的模型，或使用文字录入。');
      }
    }
    const handle = await ctx.agents.create({ sessionId, meta: { cwd: root },
      agentOptions: { ...selection, maxTokens: 12000 }, signal,
      setup(agentCtx) {
        agentCtx.tools.restrict({ allow: allowedTools });
        agentCtx.tools.presentAs('native');
      }
    });
    const { agent } = handle;
    const cancel = () => agent.cancel();
    let aborted;
    signal.addEventListener('abort', cancel, { once: true });
    try {
      await agent.whenIdle();
      const first = agent.session.seq;
      signal.throwIfAborted();
      agent.followup({ id: randomUUID(), role: 'user', source: { kind: 'user' }, content });
      await Promise.race([agent.whenIdle(), new Promise((_, reject) => {
        aborted = () => reject(new Error('研究已取消或超时。'));
        signal.addEventListener('abort', aborted, { once: true });
        if (signal.aborted) aborted();
      })]);
      signal.throwIfAborted();
      const events = [];
      for (let i = first; i < agent.session.seq; i++) events.push(agent.session.eventAt(i));
      const end = events.findLast(e => e?.type === 'turn/end');
      if (end?.data.reason.kind !== 'completed') throw new Error('DSH 未完成研究。请检查 DSH Web 的模型、凭据或会话权限后重试。');
      const message = events.findLast(e => e?.type === 'assistant/message' && e.data.message?.content?.some(b => b.type === 'text'));
      const text = (message?.data.message.content || []).filter(b => b.type === 'text').map(b => b.text).join('\n');
      if (!text.trim()) throw new Error('DSH 未返回研究正文。请检查模型配置。');
      return { text, sessionId, model: selection.model, provider: selection.provider, ...(selection.reasoningEffort ? {reasoningEffort: selection.reasoningEffort} : {}),
        tools: [...new Set(events.filter(e => e?.type === 'tool/call').map(e => e.data.name))] };
    } finally {
      signal.removeEventListener('abort', cancel);
      if (aborted) signal.removeEventListener('abort', aborted);
      try { await ctx.sessions.flush(agent.session); } finally { await handle.dispose(); }
    }
  };
}
