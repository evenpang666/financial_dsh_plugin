import {replaceFile as rename} from './atomic-file.mjs';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
export function scheduleSlots(settings, now = new Date()) {
  return (settings.markets || [settings.market || 'a']).flatMap(market => {
    if(!['a','hk','us'].includes(market)) throw Error('无效的日报市场');
    const stamp = new Intl.DateTimeFormat('sv-SE',{timeZone:market==='us'?'America/New_York':'Asia/Shanghai',dateStyle:'short',timeStyle:'short'}).format(now);
    const [day,clock]=stamp.split(' ');
    const schedule=settings.market_schedules?.[market]||(market==='us'?{morning:'09:20',afternoon:'15:30'}:settings.schedules||{morning:'09:20',afternoon:'14:30'});
    return ['morning','afternoon'].filter(session=>clock>=schedule[session]&&clock<(session==='morning'?'10:00':market==='us'?'16:00':'15:00')).map(session=>({key:`${day}:${market}:${session}`,kind:'daily',market,session}));
  });
}
export function startScheduler(root,startJob,getCalendar) {
  let stopped=false,busy=false; const pending=new Map(), statePath=join(root,'data/web/schedules.json');
  async function tick() {
    if(stopped||busy)return;busy=true;
    try {
      let settings={},done={};
      try{settings=JSON.parse(await readFile(join(root,'config/daily-push.json'),'utf8'));}catch{}
      if(settings.enabled===false)return;
      if(getCalendar && (settings.markets || [settings.market]).includes('us')) {
        const calendar=await getCalendar('us');
        if(!calendar.is_trading_day) settings={...settings,markets:(settings.markets || [settings.market]).filter(m=>m!=='us')};
        else if(calendar.close) {
          const preclose=new Date(new Date(calendar.close).getTime()-30*60000);
          const clock=new Intl.DateTimeFormat('sv-SE',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'}).format(preclose);
          if(clock<'15:30' && !settings.market_schedules?.us) settings={...settings,market_schedules:{...settings.market_schedules,us:{morning:'09:20',afternoon:clock}}};
        }
      }
      try{done=JSON.parse(await readFile(statePath,'utf8'));}catch{}
      for(const [key,job] of pending){if(job.status==='running')continue;if(job.status==='completed')done[key]=true;pending.delete(key);}
      await mkdir(join(root,'data/web'),{recursive:true});await writeFile(statePath+'.tmp',JSON.stringify(done));await rename(statePath+'.tmp',statePath);
      for(const slot of scheduleSlots(settings)){if(done[slot.key]||pending.has(slot.key))continue;try{pending.set(slot.key,startJob(slot));}catch(e){if(e.status!==409)throw e;}}
    }catch(e){console.error('[日报调度]',e.message);}finally{busy=false;}
  }
  const timer=setInterval(tick,30000);timer.unref();void tick();return()=>{stopped=true;clearInterval(timer);};
}
