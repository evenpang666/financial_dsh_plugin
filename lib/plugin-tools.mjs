import {replaceFile as rename} from './atomic-file.mjs';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
const market = {type:'string',enum:['a','hk','us']};
const schema = (properties={},required=[]) => ({type:'object',properties,required,additionalProperties:false});
export function extraTools(getRuntime, root) {
  async function web(path, options={}) {
    const r=await getRuntime(true);
    const response=await fetch(r.url+path,{...options,signal:AbortSignal.timeout(300000)});
    const value=await response.json();if(!response.ok)throw Error(value.error||`HTTP ${response.status}`);return value;
  }
  let scheduleSave = Promise.resolve();
  const definition=(name,description,parameters,execute)=>({name,description,parameters,execute});
  return [
    definition('open_financial_workspace','启动本地研究、日报、回测和训练工作台，返回可打开的地址。',schema(),async()=>({url:(await getRuntime(true)).url})),
    definition('start_financial_job','开始异步研究、手动报告、策略部署、策略回测或模型训练。手动报告只需选择市场；策略任务需指定 market、strategy，timeframe 默认为 daily。A股回测和训练的 pool 可选 custom、csi300、csi500；A股部署还可选 all。三市场回测和部署均可用 portfolio，自动读取对应市场的已保存持仓（1–100只），无需 symbols；不支持持仓池训练。港股和美股的手动股票池仅支持 custom。自定股票池须提供1–50只 symbols。回测与训练还需 start 和 end。返回任务ID，用 get_financial_jobs 观察结果。',schema({kind:{type:'string',enum:['research','daily','deploy','backtest','train']},market,prompt:{type:'string',maxLength:6000},strategy:{type:'string'},pool:{type:'string',enum:['custom','portfolio','csi300','csi500','all']},timeframe:{type:'string',enum:['daily','weekly','monthly']},start:{type:'string',format:'date'},end:{type:'string',format:'date'},symbols:{type:'array',items:{type:'string'},maxItems:50},parameters:{type:'object',additionalProperties:{type:'number'}}},['kind']),input=>web('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(input)})),
    definition('get_financial_jobs','读取已保存的任务、状态、进度与研究结果。',schema(),()=>web('/api/jobs')),
    definition('cancel_financial_job','取消指定正在运行的研究或计算任务。',schema({id:{type:'string',pattern:'^[a-f0-9-]+$'}},['id']),({id})=>web(`/api/jobs/${id}/cancel`,{method:'POST'})),
    definition('list_financial_reports','列出本地已归档日报。',schema(),()=>web('/api/reports')),
    definition('read_financial_report','读取指定日报正文。',schema({id:{type:'string',pattern:'^\\d{4}-\\d{2}-\\d{2}(?:-(?:a|hk|us)-(?:morning|afternoon|scheduled-(?:morning|afternoon)|manual-\\d{6}))?$'}},['id']),async({id})=>{if(typeof id !== 'string' || !/^\d{4}-\d{2}-\d{2}(?:-(?:a|hk|us)-(?:morning|afternoon|scheduled-(?:morning|afternoon)|manual-\d{6}))?$/.test(id)) throw Error('日报编号无效。');return {id,markdown:await readFile(join(root,'data/reports',id+'.md'),'utf8')};}),
    definition('list_backtest_results','列出已归档的策略回测结果。',schema(),()=>web('/api/backtests')),
    definition('update_portfolio_holding','修改单条本地研究持仓的股数，不影响其他条目。',schema({market,symbol:{type:'string'},shares:{type:'number',exclusiveMinimum:0,maximum:1e12}},['market','symbol','shares']),({market,symbol,shares})=>web(`/api/portfolio/${market}/${encodeURIComponent(symbol)}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({shares})})),
    definition('delete_portfolio_holding','删除一条本地研究持仓。',schema({market,symbol:{type:'string'}},['market','symbol']),({market,symbol})=>web(`/api/portfolio/${market}/${encodeURIComponent(symbol)}`,{method:'DELETE'})),
    definition('get_report_schedule','读取日报调度配置；美股按纽约时间与夏令时执行，需工作台保持运行。',schema(),async()=>JSON.parse(await readFile(join(root,'config/daily-push.json'),'utf8').catch(()=>'{"enabled":true,"markets":["a"]}'))),
    definition('set_report_schedule','保存本地日报调度配置，不设置外部消息推送。',schema({enabled:{type:'boolean'},markets:{type:'array',items:market,minItems:1,uniqueItems:true},market_schedules:{type:'object',additionalProperties:false,properties:Object.fromEntries(['a','hk','us'].map(m=>[m,schema({morning:{type:'string',pattern:'^(?:[01]\\d|2[0-3]):[0-5]\\d$'},afternoon:{type:'string',pattern:'^(?:[01]\\d|2[0-3]):[0-5]\\d$'}},['morning','afternoon'])]))}},['enabled','markets']),input=>{
      if (!input || typeof input.enabled !== 'boolean' || !Array.isArray(input.markets) || !input.markets.length || new Set(input.markets).size !== input.markets.length || input.markets.some(value => !['a','hk','us'].includes(value)) || Object.keys(input).some(key => !['enabled','markets','market_schedules'].includes(key))) throw Error('日报调度配置无效。');
      if (input.market_schedules !== undefined && (!input.market_schedules || Array.isArray(input.market_schedules) || typeof input.market_schedules !== 'object' || Object.entries(input.market_schedules).some(([market, times]) => !['a','hk','us'].includes(market) || !times || Object.keys(times).length !== 2 || !['morning','afternoon'].every(key => typeof times[key] === 'string' && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(times[key]))))) throw Error('日报时间须为有效的 HH:MM。');
      const next = JSON.parse(JSON.stringify(input));
      const operation = scheduleSave.catch(()=>{}).then(async()=>{
        await mkdir(join(root,'config'),{recursive:true});const path=join(root,'config/daily-push.json');
        await writeFile(path+'.tmp',JSON.stringify(next,null,2));await rename(path+'.tmp',path);return {saved:true,...next};
      });
      scheduleSave = operation; return operation;
    }),
  ];
}
