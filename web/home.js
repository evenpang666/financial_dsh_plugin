const $ = selector => document.querySelector(selector);
const labels = { research: '研究', daily: '日报', backtest: '回测', train: '训练', portfolio: '持仓' };
async function api(path, options) {
  const response = await fetch(path, options); const value = await response.json();
  if (!response.ok) throw new Error(value.error || '服务暂不可用。'); return value;
}
let lastJobs = '', refreshing = false, failures = 0;
function node(tag, className, text) { const el = document.createElement(tag); if (className) el.className = className; if (text) el.textContent = text; return el; }
function answer(text, error = false) {
  const container = node('div', `job-result${error ? ' job-error' : ''}`);
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    const heading = /^#{1,4}\s+/.test(line), list = /^[-*]\s+/.test(line);
    const row = node(heading ? 'h4' : 'p', list ? 'answer-list' : '');
    for (const part of line.replace(/^#{1,4}\s+|^[-*]\s+/g, '').split(/(\*\*[^*]+\*\*|`[^`]+`)/g)) {
      row.append(part.startsWith('**') ? node('strong', '', part.slice(2, -2)) : part.startsWith('`') ? node('code', '', part.slice(1, -1)) : document.createTextNode(part));
    }
    container.append(row);
  }
  return container;
}
async function refresh() {
  if (refreshing || document.hidden) return; refreshing = true;
  try {
    const { jobs } = await api('/api/jobs'); failures = 0;
    $('#researchSubmit').disabled = jobs.some(job => job.status === 'running');
    const signature = JSON.stringify(jobs); if (lastJobs === signature) return; lastJobs = signature;
    if (!jobs.length) return;
    $('#jobList').replaceChildren(...jobs.map(job => {
      const card = node('article', 'job-card'), heading = node('div', 'job-heading'), title = node('div');
      title.append(node('h3', '', job.title)); const time = node('time', '', new Date(job.createdAt).toLocaleString('zh-CN')); time.dateTime = job.createdAt; title.append(time); heading.append(title);
      const status = node('div', 'job-status', `${labels[job.kind]} · ${{ running: '进行中', completed: '已完成', failed: '未完成' }[job.status]}`);
      if (job.status === 'running') {
        status.prepend(node('span', 'spinner')); const cancel = node('button', 'quiet', '取消'); cancel.type = 'button';
        cancel.addEventListener('click', async () => { try { await api(`/api/jobs/${job.id}/cancel`, { method: 'POST' }); cancel.disabled = true; } catch (e) { $('#notice').textContent = e.message; } }); status.append(cancel);
      }
      heading.append(status); card.append(heading);
      if (job.result?.text || job.error) card.append(answer(job.error || job.result.text, Boolean(job.error)));
      if (job.progress) { const details = node('details', 'job-progress'); details.append(node('summary', '', '运行日志'), node('pre', '', job.progress)); card.append(details); }
      if (job.result?.reportId) { const link = node('a', 'quiet', '阅读日报 ↗'); link.href = '/reports?report=' + encodeURIComponent(job.result.reportId); card.append(link); }
      return card;
    }));
  } catch (e) { if (++failures >= 2) { $('#notice').textContent = '与本地服务的连接已断开，请检查启动窗口。'; $('#navStatus').textContent = '连接中断'; } }
  finally { refreshing = false; }
}
$('#researchForm').addEventListener('submit', async event => {
  event.preventDefault(); const prompt = $('#question').value.trim(); if (!prompt) return;
  $('#researchSubmit').disabled = true; $('#notice').textContent = '';
  try { await api('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: 'research', prompt }) }); await refresh(); $('#recentTitle').scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  catch (e) { $('#notice').textContent = e.message; $('#researchSubmit').disabled = false; }
});
document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => { $('#question').value = button.dataset.prompt; $('#question').focus(); }));
$('#refreshJobs').addEventListener('click', refresh);
void api('/api/status').then(status => { $('#navStatus').textContent = 'Agent 已连接'; $('#modelLabel').textContent = status.model || 'DSH 默认模型'; }).catch(() => { $('#navStatus').textContent = '连接失败'; $('#modelLabel').textContent = '请检查 DSH 启动窗口'; });
void api('/api/reports').then(data => { $('#reportCount').textContent = `${data.reports.length} 份本地日报`; }).catch(() => { $('#reportCount').textContent = '归档暂不可用'; });
void api('/api/backtests').then(data => { $('#backtestCount').textContent = `${data.runs.length} 次回测记录`; }).catch(() => { $('#backtestCount').textContent = '记录暂不可用'; });
void refresh(); setInterval(refresh, 2500); document.addEventListener('visibilitychange', refresh);
