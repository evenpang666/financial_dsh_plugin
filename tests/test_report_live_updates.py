"""Opt-in browser regression checks using synthetic data and no live services."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CHROME = shutil.which('google-chrome') or shutil.which('chromium')


@unittest.skipUnless(CHROME and os.getenv('FINANCE_BROWSER_TESTS') == '1', 'Set FINANCE_BROWSER_TESTS=1 to run headless browser checks')
class ReportLiveUpdateTests(unittest.TestCase):
    def test_external_deployment_and_pending_portfolio_survive_report_updates(self):
        html = (ROOT/'web/index.html').read_text()
        html = html.replace('<link rel="stylesheet" href="/styles.css">', '<style>'+(ROOT/'web/styles.css').read_text()+'</style>')
        html = html.replace('<script src="/report-visuals.js" defer></script>', '<script>'+(ROOT/'web/report-visuals.js').read_text()+'</script>')
        setup = r'''
window.EventSource = class {
  constructor(){window.events=this;this.listeners={};}
  addEventListener(type, callback){this.listeners[type]=callback;}
  emit(type, payload){this.listeners[type]?.({data:JSON.stringify(payload)});}
};
window.setInterval=()=>{};
window.requestAnimationFrame=callback=>setTimeout(()=>callback(performance.now()),0);
const media=window.matchMedia.bind(window);
window.matchMedia=query=>query.includes('prefers-reduced-motion')?{matches:true}:media(query);
window.calls=[]; window.activeJob=null; window.jobComplete=false; window.latestNotice={available:false};
window.report={available:true,id:'2026-10-13-a-morning',market:'a',updated_at:'2026-10-13T09:20:00+08:00',markdown:'# 测试日报\n\n## 市场全景\n测试内容',view:{}};
window.menu={selected:'momentum',selected_kind:'fixed',fixed_strategies:{momentum:{name:'盘中动量',parameters:{},parameter_schema:{}},macd:{name:'MACD金叉',parameters:{},parameter_schema:{}}},models:{'elastic_net@test-checkpoint':{name:'Elastic Net 测试版本',parameters:{},parameter_schema:{}}}};
window.menu.strategies={...menu.fixed_strategies,...menu.models};
window.marketResult=(selection,id)=>({available:true,id,scope:'full_market',strategy_id:selection.split('@')[0],checkpoint_id:selection.includes('@')?selection.split('@')[1]:null,strategy_name:selection,generated_at:'2026-10-13T10:30:00+08:00',signal_at:'2026-10-13T10:30:00+08:00',completed_at:'2026-10-13T10:31:00+08:00',model_quality:'fixed_rule',universe_count:100,input_count:90,valid_count:90,failed_count:0,coverage_complete:true,ranking:[{rank:1,symbol:'600888',name:id,price:20,score:1,entry:true}],note:'合成部署样例'});
window.results={};
window.fetch=async(url,options={})=>{
 calls.push({url,options});let payload={};
 if(url==='/api/ranking/strategies'&&window.failMenuOnce){window.failMenuOnce=false;throw Error('transient metadata failure');}
 if(url==='/api/reports')payload={reports:[window.report]};
 else if(url.startsWith('/api/reports/'))payload=window.report;
 else if(url==='/api/ranking/strategies'||url==='/api/portfolio-strategies')payload=window.menu;
 else if(url==='/api/ranking/update')payload=window.latestNotice;
 else if(url==='/api/ranking/active')payload=window.activeJob||{available:false};
 else if(url.startsWith('/api/ranking/latest?'))payload=window.results[new URL(url,'http://local').searchParams.get('strategy')]||{available:false};
 else if(url.startsWith('/api/ranking/jobs/'))payload=jobComplete?{...activeJob,status:'completed',result:window.results[activeJob.selection_id]}:{...activeJob,status:'running',message:'处理中',input_count:90,processed_count:50};
 else if(url==='/api/portfolio-analysis')payload=await new Promise(resolve=>{window.finishPortfolio=resolve});
 else throw Error('unexpected request '+url);
 return {ok:true,status:200,json:async()=>structuredClone(payload)};
};
'''
        checks = r'''
;(async()=>{
 const check=(ok,message)=>{if(!ok)throw Error(message)};
 const pause=()=>new Promise(resolve=>setTimeout(resolve,120));
 try{
  await pause();document.querySelector('#researchTools').open=true;
  check(!calls.some(call=>call.url==='/api/portfolio-analysis'),'initial page read private holdings');
  const button=document.querySelector('#portfolioButton');button.click();
  button.dispatchEvent(new MouseEvent('click',{bubbles:true}));await pause();
  check(calls.filter(call=>call.url==='/api/portfolio-analysis').length===1,'duplicate portfolio request');
  window.report={...window.report,id:'2026-10-13-a-afternoon'};
  events.emit('report',{id:window.report.id});await pause();
  check(displayedReportId===window.report.id,'report did not change while analysis pending');
  finishPortfolio({available:true,market:'a',as_of:'2026-10-13T10:30:00+08:00',note:'已完成样例分析',holdings:[{holding:{symbol:'600001'},analysis:{name:'测试持仓',short_term:{evidence:['测试证据']}},strategy:'测试研究结果'}],position_summary:{rows:[{symbol:'600001',name:'测试持仓',suggestion1:{weight_pct:20},suggestion2:{weight_pct:10},suggestion3:{weight_pct:15}}],cash_reserve_pct:{suggestion1:80,suggestion2:90,suggestion3:85},stock_allocation_total_pct:{suggestion1:20,suggestion2:10,suggestion3:15}}});
  await pause();
  check(portfolioContent.textContent.includes('测试持仓')&&portfolioContent.textContent.includes('15.00%'),'analysis result was discarded on report change');
  check(!button.disabled,'portfolio button stayed disabled');
  showReport({...window.report,id:'2026-10-12-a-afternoon'});
  check(portfolioContent.textContent.includes('测试持仓'),'same market history cleared live analysis');
  showReport({...window.report,id:'2026-10-13-hk-afternoon',market:'hk'});
  check(!portfolioContent.textContent.includes('测试持仓'),'A holdings leaked into HK section');
  showReport(window.report);
  check(portfolioContent.textContent.includes('测试持仓'),'authorized A analysis was not restored');
  const selection='elastic_net@test-checkpoint';
  window.results[selection]=marketResult(selection,'external-model-result');
  events.emit('ranking',{type:'ranking',scope:'full_market',id:'external-model-result',selection_id:selection,strategy_name:'Elastic Net'});await pause();
  check(rankingStrategy.value===selection&&rankingKind.value==='model','external checkpoint selection not followed');
  check(rankingContent.textContent.includes('external-model-result'),'external deployment not refreshed');
  check(!researchUpdateButton.hidden,'collapsed tools have no update notice');
  rankingKind.value='fixed';rankingKind.dispatchEvent(new Event('change'));await pause();
  window.results.macd=marketResult('macd','external-fixed-result');
  events.emit('ranking',{scope:'full_market',id:'external-fixed-result',selection_id:'macd',strategy_name:'MACD'});await pause();
  check(rankingStrategy.value==='momentum','explicit user choice was overridden');
  researchUpdateButton.click();await pause();
  check(rankingStrategy.value==='macd'&&rankingContent.textContent.includes('external-fixed-result'),'view update failed');
  check(document.querySelector('#researchTools').open,'results stayed collapsed');
  window.results.macd=marketResult('macd','missed-event-result');
  window.latestNotice={available:true,scope:'full_market',id:'missed-event-result',selection_id:'macd',strategy_name:'MACD'};
  await refreshRankingPublication();
  check(rankingContent.textContent.includes('missed-event-result'),'missed SSE fallback failed');
  window.results.macd=marketResult('macd','retry-event-result');
  window.latestNotice={...window.latestNotice,id:'retry-event-result'};window.failMenuOnce=true;
  await refreshRankingPublication();
  check(!rankingStrategy.disabled,'transient metadata failure disabled saved selection');
  await refreshRankingPublication();
  check(rankingContent.textContent.includes('retry-event-result'),'same publication was not retried after failure');
  window.activeJob={job_id:'a'.repeat(32),status:'running',selection_id:'macd',strategy_id:'macd',strategy_name:'MACD'};
  await restoreRankingJob();
  check(rankingJob?.job_id===activeJob.job_id&&rankMarketButton.disabled,'did not reattach to running task');
  window.jobComplete=true;await pollRankingJob();
  check(!rankingJob&&!rankMarketButton.disabled&&rankingContent.textContent.includes('retry-event-result'),'reattached result not rendered');
  check(!calls.some(call=>call.url==='/api/ranking/start'),'reattach restarted computation');
  check(calls.filter(call=>call.url==='/api/portfolio-analysis').length===1,'updates reread holdings');
  check(document.documentElement.scrollWidth<=innerWidth,'page overflow');
  document.body.dataset.liveUpdateCheck='passed';
 }catch(error){document.body.dataset.liveUpdateCheck=error.message;}
})();
'''
        html=html.replace('<script src="/app.js" defer></script>','<script>'+setup+(ROOT/'web/app.js').read_text()+checks+'</script>')
        for width, height in ((1440,900),(390,844)):
            with self.subTest(width=width), tempfile.TemporaryDirectory(prefix='akagent-live-update-') as folder:
                fixture=Path(folder)/'fixture.html';fixture.write_text(html)
                result=subprocess.run([CHROME,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',f'--user-data-dir={folder}/profile','--virtual-time-budget=5000',f'--window-size={width},{height}','--dump-dom',fixture.as_uri()],capture_output=True,text=True,timeout=30)
                marker=re.search(r'data-live-update-check="([^"]+)"',result.stdout)
                self.assertIsNotNone(marker,result.stderr[-1000:])
                self.assertEqual(marker.group(1),'passed')


if __name__=='__main__':
    unittest.main()
