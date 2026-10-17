import { randomUUID } from 'node:crypto';

// Agent selection and all provider credentials remain owned by DSH.
export function createResearchAgent(ctx, root, financeTools) {
  return async (prompt, { signal = AbortSignal.timeout(600000), allowedTools = financeTools, purpose = 'research' } = {}) => {
    if (!allowedTools.every(name => financeTools.includes(name))) throw Error('请求了未注册的金融工具。');
    await ctx.get('loader')?.await();
    signal.throwIfAborted();
    const sessionId = `financial-${randomUUID()}`;
    const selection = ctx.agentDefaultModel.currentSelection();
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
      agent.followup({ id: randomUUID(), role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text:
        (purpose === 'portfolio' ? '你是知衡持仓录入助手。严格按提供的结构提取用户原文，不猜测数量，不执行写入或交易，只返回要求的JSON。\n\n' : '你是知衡股票研究助手。先调用本地金融工具查证，再用简洁中文回答。注明来源、as_of、缺失数据与结论失效条件；不得编造行情，不执行交易。倾向评分不是仓位或涨跌概率。\n\n') + prompt }] });
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
      return { text, sessionId, model: selection.model, provider: selection.provider,
        tools: [...new Set(events.filter(e => e?.type === 'tool/call').map(e => e.data.name))] };
    } finally {
      signal.removeEventListener('abort', cancel);
      if (aborted) signal.removeEventListener('abort', aborted);
      try { await ctx.sessions.flush(agent.session); } finally { await handle.dispose(); }
    }
  };
}
