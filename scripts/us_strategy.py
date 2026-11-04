"""Separate daily-bar US experiment engine; no A-share lot/limit/T+1 assumptions."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
import numpy as np
import pandas as pd
import us_market as us
from strategy_catalog import STRATEGIES, MODEL_STRATEGIES, rule_scores
from strategy_indicators import _window_indicators
from strategy_config import validate_parameters, parameter_schema

ROOT=Path(__file__).resolve().parent.parent
STORE=ROOT/'data'/'us-models'
FEATURES=['open_momentum','range_position','range_width','vwap_gap','return5','return20','volatility20']

def features(bars,parameters=None):
    frame=bars.sort_values('date').reset_index(drop=True).copy()
    close=frame.close
    frame['open_momentum']=close/frame.open-1
    frame['range_position']=(close-frame.low)/(frame.high-frame.low).replace(0,np.nan)-0.5
    frame['range_width']=frame.high/frame.low-1
    vwap=(close*frame.volume).rolling(20).sum()/frame.volume.rolling(20).sum().replace(0,np.nan)
    frame['vwap_gap']=close/vwap-1
    frame['return5']=close.pct_change(5);frame['return20']=close.pct_change(20)
    frame['volatility20']=close.pct_change().rolling(20).std()
    indicators=_window_indicators(frame,parameters)
    for column in indicators:frame[column]=indicators[column]
    frame['entry_day']=frame.date.shift(-1)
    frame['entry_open']=frame.open.shift(-1);frame['exit_close']=close.shift(-1)
    frame['target']=frame.exit_close/frame.entry_open-1
    return frame.replace([np.inf,-np.inf],np.nan)

def panel(symbols,start,end,strategy,parameters):
    frames=[]
    for symbol in symbols:
        bars=us.history(symbol,str(pd.Timestamp(start).date()-timedelta(days=400)),end,'split_dividend')
        frame=features(bars,{strategy:parameters} if strategy not in MODEL_STRATEGIES else {})
        frame['symbol']=symbol;frames.append(frame)
    return pd.concat(frames,ignore_index=True).sort_values(['date','symbol'])

def save_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8');temp.replace(path)

def checkpoints():
    rows=[]
    for path in STORE.glob('*/checkpoint.json'):
        try:
            row=json.loads(path.read_text(encoding='utf-8'));artifact=path.parent/'weights.json'
            if row['market']=='us' and row['model_id']==path.parent.name and hashlib.sha256(artifact.read_bytes()).hexdigest()==row['artifact_sha256']:rows.append(row)
        except (OSError,ValueError,KeyError):continue
    return sorted(rows,key=lambda r:r['selected_at'],reverse=True)

def options():
    fixed={key:{**value,'name':'美股日线 · '+value['name'],'description':'收盘信号，下一交易日开盘入场、收盘退出。均值回归采用20日日线成交量加权均价。','parameter_schema':parameter_schema(key),'parameters':validate_parameters(key,None)} for key,value in STRATEGIES.items() if key not in MODEL_STRATEGIES}
    models={f"{r['algorithm']}@{r['model_id']}":{'name':f"美股 {r['algorithm']} · {r['model_id']}",'strategy_id':r['algorithm'],'checkpoint_id':r['model_id'],'training_range':r['training'],'parameters':validate_parameters(r['algorithm'],None),'parameter_schema':parameter_schema(r['algorithm'])} for r in checkpoints()}
    return {'market':'us','fixed_strategies':fixed,'models':models,'strategies':{**fixed,**models},'selected':next(iter(fixed)),'selected_kind':'fixed'}

def train(frame,strategy,start,end,parameters):
    clean=frame[(frame.date>=pd.Timestamp(start))&(frame.entry_day<=pd.Timestamp(end))].dropna(subset=FEATURES+['target'])
    days=sorted(clean.date.unique())
    if len(days)<30:raise ValueError('美股训练至少需要30个有完整特征与标签的交易日。')
    split=pd.Timestamp(days[int(len(days)*0.8)])
    fit=clean[clean.entry_day<split];validation=clean[clean.date>=split]
    if len(fit)<20 or validation.empty:raise ValueError('训练或验证样本不足。')
    x=fit[FEATURES].to_numpy();y=fit.target.to_numpy()
    if strategy=='elastic_net':
        from sklearn.linear_model import ElasticNet
        mean=x.mean(axis=0);scale=x.std(axis=0);scale[scale==0]=1
        model=ElasticNet(alpha=parameters.get('alpha_scale',0.01)*max(float(np.std(y)),1e-8),l1_ratio=parameters.get('l1_ratio',0.5),max_iter=int(parameters.get('max_iter',10000))).fit((x-mean)/scale,y)
        weights={'mean':mean.tolist(),'scale':scale.tolist(),'coef':model.coef_.tolist(),'intercept':float(model.intercept_)}
        predict=lambda values:((values-mean)/scale)@model.coef_+model.intercept_
    else:
        from xgboost import XGBRegressor
        model=XGBRegressor(n_estimators=int(parameters.get('n_estimators',100)),max_depth=int(parameters.get('max_depth',3)),learning_rate=parameters.get('learning_rate',0.05),min_child_weight=parameters.get('min_child_weight',20),reg_lambda=parameters.get('reg_lambda',10),subsample=parameters.get('subsample',0.8),random_state=42,n_jobs=2).fit(x,y)
        weights=None;predict=model.predict
    error=float(np.mean((predict(validation[FEATURES].to_numpy())-validation.target.to_numpy())**2))
    identity=f'us-{strategy}-{datetime.now(us.NY):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}'
    artifact=STORE/identity/'weights.json';artifact.parent.mkdir(parents=True)
    if weights is None:model.save_model(artifact)
    else:save_json(artifact,weights)
    result={'market':'us','model_id':identity,'algorithm':strategy,'features':FEATURES,'selected_at':datetime.now(us.NY).isoformat(),'artifact_sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),
            'training':{'first_day':str(fit.date.min().date()),'last_day':str(fit.date.max().date()),'label_end_day':str(clean.entry_day.max().date()),'validation_start':str(split.date())},
            'validation_mse':error,'training_parameters':parameters,'universe':sorted(frame.symbol.unique()),'note':'按日期划分训练/验证，边界清除跨期标签；回测必须晚于验证标签截止日。'}
    save_json(artifact.parent/'checkpoint.json',result);return result

def model_scores(frame,strategy,checkpoint,start):
    record=next((r for r in checkpoints() if r['model_id']==checkpoint and r['algorithm']==strategy),None)
    if not record:raise ValueError('请选择有效的美股模型 checkpoint；不能使用A股模型。')
    if record['training']['label_end_day']>=start:raise ValueError('回测开始日必须晚于训练及验证标签截止日，防止未来数据泄漏。')
    artifact=STORE/checkpoint/'weights.json'
    if strategy=='elastic_net':
        w=json.loads(artifact.read_text());return ((frame[FEATURES].to_numpy()-w['mean'])/w['scale'])@np.array(w['coef'])+w['intercept']
    from xgboost import XGBRegressor
    model=XGBRegressor();model.load_model(artifact);return model.predict(frame[FEATURES].to_numpy())

def simulate(frame,start,end,top_n=5,fee_bps=5,slippage_bps=5):
    initial=100000.0;cash=initial;curve=[];trades=[]
    import exchange_calendars as xc
    calendar=xc.get_calendar('XNYS',start=pd.Timestamp(start)-pd.Timedelta(days=10),end=pd.Timestamp(end)+pd.Timedelta(days=10))
    for session in calendar.sessions_in_range(start,end):
        day=session.tz_localize(None) if session.tzinfo else session
        previous=calendar.previous_session(session)
        previous=previous.tz_localize(None) if previous.tzinfo else previous
        group=frame[(frame.entry_day==day)&(frame.date==previous)&frame.eligible].dropna(subset=['score','entry_open','exit_close'])
        group=group[(group.entry_open>0)&(group.exit_close>0)].nlargest(top_n,'score')
        budget=cash/top_n
        for row in group.itertuples():
            entry=row.entry_open*(1+slippage_bps/10000);exit_price=row.exit_close*(1-slippage_bps/10000)
            shares=math.floor(budget/(entry*(1+fee_bps/10000)))
            if shares<=0:continue
            cost=shares*(entry+exit_price)*fee_bps/10000
            cash+=shares*(exit_price-entry)-cost
            trades.append({'day':str(day.date()),'signal_day':str(row.date.date()),'symbol':row.symbol,'shares':shares,'entry':entry,'exit':exit_price,'cost':cost,'return':exit_price/entry-1})
        curve.append({'day':str(day.date()),'cash':cash,'equity':cash})
    if not curve:raise ValueError('回测区间没有交易日。')
    nav=np.array([initial]+[r['equity'] for r in curve]);returns=nav[1:]/nav[:-1]-1
    stats={'initial_cash':initial,'final_equity':cash,'total_return':cash/initial-1,'annualized_return':(cash/initial)**(252/len(curve))-1,'max_drawdown':float(np.min(nav/np.maximum.accumulate(nav)-1)),
           'sharpe':float(returns.mean()/returns.std()*np.sqrt(252)) if returns.std()>0 else 0.0,'trades':len(trades),'costs':sum(t['cost'] for t in trades),'trading_days':len(curve)}
    return pd.DataFrame(curve),pd.DataFrame(trades,columns=['day','signal_day','symbol','shares','entry','exit','cost','return']),stats

def portfolio_review(items,strategy,checkpoint='',parameters=None):
    params=validate_parameters(strategy,parameters)
    result={'strategy_id':strategy,'strategy_name':'美股日线 · '+STRATEGIES[strategy]['name'],'strategy_parameters':params,
            'scope':'仅已保存美股持仓池；最近完整日线信号','rows':{},'input_count':len(items),'valid_count':0,'available':False}
    rows=[]
    # Use only a fully completed NY session, including holiday/early-close logic.
    now=datetime.now(us.NY);day=us.today();calendar=us.trading_day(str(day))
    if not calendar['is_trading_day'] or now<datetime.fromisoformat(calendar['close']):day-=timedelta(days=1)
    for item in items:
        symbol=item['holding']['symbol']
        try:
            frame=features(us.history(symbol,end=str(day)),{strategy:params} if strategy not in MODEL_STRATEGIES else {})
            last=frame.tail(1).copy()
            if (day-last.iloc[0].date.date()).days>7:raise ValueError('最近日线过期')
            if strategy in MODEL_STRATEGIES:
                scores=model_scores(last,strategy,checkpoint,str(last.iloc[0].date.date()));eligible=scores>0
            else:scores,eligible=rule_scores(last,strategy,params)
            score=float(np.asarray(scores)[0]);entry=bool(np.asarray(eligible)[0])
            if not math.isfinite(score):raise ValueError('指标预热不足')
            rows.append({'symbol':symbol,'score':score,'entry':entry,'as_of':str(last.iloc[0].date.date())})
        except Exception as exc:result['rows'][symbol]={'status':'unavailable','note':str(exc)}
    ranked=sorted([r for r in rows if r['entry']],key=lambda r:r['score'],reverse=True)
    for row in rows:
        rank=next((i+1 for i,r in enumerate(ranked) if r['symbol']==row['symbol']),None)
        result['rows'][row['symbol']]={**row,'rank':rank,'status':'triggered' if row['entry'] else 'not_triggered','allocation_score':1/rank if rank and rank<=params['top_n'] else 0.0,'note':'日线研究信号，不代表下单指令。'}
    result.update(valid_count=len(rows),available=len(rows)==len(items),as_of=min((r['as_of'] for r in rows),default=None),note='最近完整纽约交易日日线；不使用未收盘价格形成策略信号。')
    return result


def run(action,strategy,symbols,start,end,parameters=None,checkpoint=''):
    symbols=list(dict.fromkeys(us.normalize_symbol(s) for s in symbols))
    if not symbols or len(symbols)>50:raise ValueError('美股实验需显式指定1–50只股票，避免把当前股票池当作历史全集。')
    if start>end or pd.Timestamp(end).date()>=us.today():raise ValueError('日期无效；结束日必须早于纽约今日，避免使用未收盘日线。')
    if strategy not in STRATEGIES:raise ValueError('未知策略')
    supplied=dict(parameters or {});fees=supplied.pop('fee_bps',5);slippage=supplied.pop('slippage_bps',5)
    if not 0<=fees<=100 or not 0<=slippage<=100:raise ValueError('费率与滑点必须在0–100 bps。')
    params=validate_parameters(strategy,supplied,'training' if action=='train' else 'execution')
    print(f'准备美股日线：{", ".join(symbols)}',flush=True)
    frame=panel(symbols,start,end,strategy,params)
    if action=='train':
        if strategy not in MODEL_STRATEGIES:raise ValueError('仅模型策略支持训练。')
        return train(frame,strategy,start,end,params)
    if strategy in MODEL_STRATEGIES:
        frame=frame.dropna(subset=FEATURES).copy();frame['score']=model_scores(frame,strategy,checkpoint,start);frame['eligible']=frame.score>0
    else:frame['score'],frame['eligible']=rule_scores(frame,strategy,params)
    equity,trades,stats=simulate(frame,start,end,int(params.get('top_n',5)),fees,slippage)
    identity=f'us-{strategy}-{datetime.now(us.NY):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}'
    directory=ROOT/'data/factor-lab/backtests'/identity;directory.mkdir(parents=True)
    equity.to_csv(directory/'equity.csv',index=False);trades.to_csv(directory/'trades.csv',index=False)
    result={'kind':'us_daily_backtest','schema_version':2,'market':'us','currency':'USD','run_id':identity,'slot':'US daily close -> next open/close','strategy_id':strategy,'strategy_name':'美股日线 · '+STRATEGIES[strategy]['name'],'generated_at':datetime.now(us.NY).isoformat(),'start':start,'end':end,'first_day':start,'last_day':end,'days':len(equity),'execution':'next NY session open','top_n':int(params.get('top_n',5)),'stats':stats,'parameters':params,'checkpoint_id':checkpoint,'universe':symbols,
            'assumptions':{'fee_bps_per_side':fees,'slippage_bps_per_side':slippage,'fractional_shares':False,'shorting':False,'price_basis':'Yahoo split/dividend adjusted OHLC, synthetic total-return units'},
            'limitations':['固定用户股票池存在幸存者选择偏差，不代表全市场收益。','日线模拟不包含盘口、停牌成交约束与融资成本。','复权价格仅用于总回报模拟，不代表当日真实可成交美元价格；无经纪商连接。']}
    save_json(directory/'summary.json',result);return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['train','backtest']);parser.add_argument('--strategy',required=True);parser.add_argument('--symbols',required=True);parser.add_argument('--start',required=True);parser.add_argument('--end',required=True);parser.add_argument('--parameters',default='{}');parser.add_argument('--checkpoint',default='')
    args=parser.parse_args()
    if os_owned := __import__('os').getenv('FINANCE_WEB_OWNED'):
        from web_process_owner import watch_owner
        watch_owner()
    print(json.dumps(run(args.action,args.strategy,args.symbols.split(','),args.start,args.end,json.loads(args.parameters),args.checkpoint),ensure_ascii=False,allow_nan=False))
