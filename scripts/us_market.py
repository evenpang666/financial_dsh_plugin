"""US market adapter. Every observation keeps provider dates and USD units."""
from __future__ import annotations
import os
import re
import time
import json
import hashlib
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo
import pandas as pd
import requests

NY = ZoneInfo('America/New_York')
_sec_lock = Lock()
_sec_last = 0.0
_cache_root = Path(__file__).resolve().parent.parent / 'data' / 'us-cache'


def _cached(kind, key, fetch, frame=False):
    """Use a recent, explicitly labeled observation if Yahoo temporarily rate limits."""
    path = _cache_root / (hashlib.sha256(json.dumps([kind, key], sort_keys=True).encode()).hexdigest() + '.json')
    try:
        value = fetch()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {'retrieved_at': datetime.now(NY).isoformat(), 'value':
                       {'rows': value.to_dict('records'), 'attrs': value.attrs} if frame else value}
            path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding='utf-8')
        except OSError:
            pass
        return value
    except Exception as error:
        if not any(message in str(error) for message in ('Too Many Requests', 'Rate limited', 'Yahoo Finance 未返回美股日线')):
            raise
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            retrieved = datetime.fromisoformat(payload['retrieved_at'])
            if datetime.now(NY) - retrieved > timedelta(hours=24):
                raise ValueError('缓存已过期')
            value = payload['value']
            label = f'Yahoo Finance via yfinance (cached {retrieved.isoformat()}; upstream rate limited)'
            if frame:
                result = pd.DataFrame(value['rows'])
                result['date'] = pd.to_datetime(result['date'])
                result.attrs.update(value['attrs'])
                result.attrs['source'] = label
                return result
            if kind == 'quote': value['_source'] = label
            else: value['source'] = label
            return value
        except (OSError, ValueError, KeyError, TypeError) as cache_error:
            raise RuntimeError(f'{error}; 缓存回退不可用：{cache_error}') from error

def today():
    return datetime.now(NY).date()

def normalize_symbol(value):
    text = re.sub(r'^(?:US|NYSE|NASDAQ):', '', str(value).strip().upper()).replace('.', '-')
    if not re.fullmatch(r'[A-Z][A-Z0-9]{0,9}(?:-[A-Z0-9]{1,3})?', text):
        raise ValueError('美股 ticker 无效，例如 AAPL、BRK.B / BRK-B。')
    return text

def yf():
    import yfinance
    return yfinance

def metadata(source, as_of=None):
    return {'market':'us', 'currency':'USD', 'timezone':'America/New_York', 'source':source,
            'as_of':str(as_of or today()), 'retrieved_at':datetime.now(NY).isoformat()}

def trading_day(day=''):
    import exchange_calendars as xc
    day = pd.Timestamp(day or today()).normalize()
    calendar = xc.get_calendar('XNYS', start=day-pd.Timedelta(days=10), end=day+pd.Timedelta(days=10))
    opened = calendar.is_session(day)
    result = {**metadata('exchange_calendars XNYS', day.date()), 'date':str(day.date()), 'is_trading_day':bool(opened)}
    if opened:
        result.update(open=calendar.session_open(day).tz_convert(NY).isoformat(), close=calendar.session_close(day).tz_convert(NY).isoformat())
    return result

def history(symbol, start='', end='', adjust='qfq'):
    symbol = normalize_symbol(symbol)
    if adjust not in ('', 'qfq', 'split_dividend'):
        raise ValueError('美股仅支持空值（Yahoo 原始 OHLC）或 qfq/split_dividend（Yahoo 拆股与分红调整），不支持 A股 hfq。')
    start = str(pd.Timestamp(start).date()) if start else str(today()-timedelta(days=730))
    end_date = pd.Timestamp(end or today()).date()
    if pd.Timestamp(start).date() > end_date:
        raise ValueError('开始日期不能晚于结束日期。')
    def fetch():
        frame = yf().Ticker(symbol).history(start=start, end=str(end_date+timedelta(days=1)), interval='1d', auto_adjust=bool(adjust), actions=True, timeout=20)
        if frame.empty:
            raise ValueError('Yahoo Finance 未返回美股日线。请核对 ticker、时间区间和网络。')
        frame = frame.reset_index().rename(columns={'Date':'date','Open':'open','High':'high','Low':'low','Close':'close','Volume':'volume','Dividends':'dividends','Stock Splits':'stock_splits'})
        frame['date'] = pd.to_datetime(frame['date']).dt.tz_localize(None)
        frame = frame.sort_values('date').drop_duplicates('date').reset_index(drop=True)
        frame.attrs.update(source='Yahoo Finance via yfinance', currency='USD', timezone='America/New_York', adjustment='split_dividend' if adjust else 'Yahoo unadjusted (split-normalized upstream)')
        return frame
    return _cached('history', [symbol,start,str(end_date),adjust], fetch, frame=True)

def search(query, limit=10):
    if not str(query).strip(): raise ValueError('query 不能为空。')
    def fetch():
        rows = yf().Search(query, max_results=limit, news_count=0).quotes
        exchanges = {'NYQ','NMS','NGM','NCM','ASE','PCX','BTS','PNK','OEM','OQB','OQX'}
        matches=[]
        for row in rows:
            if row.get('exchange') not in exchanges or row.get('quoteType') not in ('EQUITY','ETF'): continue
            try: symbol=normalize_symbol(row['symbol'])
            except ValueError: continue
            matches.append({'symbol':symbol,'name':row.get('shortname') or row.get('longname') or symbol,'exchange':row.get('exchange')})
        return {**metadata('Yahoo Finance search'),'query':query,'matches':matches[:limit]}
    return _cached('search', [query,limit], fetch)

def quote(symbol):
    symbol=normalize_symbol(symbol)
    def fetch():
        info=yf().Ticker(symbol).get_info()
        price=info.get('regularMarketPrice') or info.get('currentPrice')
        if price is None: raise ValueError('美股行情价格缺失。')
        if info.get('currency') != 'USD': raise ValueError('行情不是 USD 计价，无法作为美股数据使用。')
        stamp=info.get('regularMarketTime')
        if not stamp: raise ValueError('行情缺少 regularMarketTime，不能将抓取时间当作报价时间。')
        as_of=datetime.fromtimestamp(stamp,NY).isoformat()
        return {'代码':symbol,'名称':info.get('shortName') or info.get('longName'), '最新价':price,
                '涨跌幅':info.get('regularMarketChangePercent'), '成交量':info.get('regularMarketVolume'),
                '市盈率-动态':info.get('trailingPE'), '市净率':info.get('priceToBook'), '总市值':info.get('marketCap'),
                '更新时间':as_of,'currency':'USD','timezone':'America/New_York','_source':'Yahoo Finance via yfinance'}
    return _cached('quote',symbol,fetch)

def fundamentals(symbol):
    symbol=normalize_symbol(symbol); ticker=yf().Ticker(symbol)
    statements={'income':ticker.quarterly_income_stmt,'balance':ticker.quarterly_balance_sheet,'cashflow':ticker.quarterly_cashflow}
    records=[]
    for kind,frame in statements.items():
        for period in frame.columns[:8]:
            row={str(k):float(v) if pd.notna(v) else None for k,v in frame[period].items()}
            records.append({'statement':kind,'report_period':str(pd.Timestamp(period).date()),'fields':row})
    if not records: raise ValueError('Yahoo Finance 未返回财务报表。')
    latest=max(row['report_period'] for row in records)
    reporting_currency=ticker.get_info().get('financialCurrency')
    return {**metadata('Yahoo Finance quarterly statements',latest),'symbol':symbol,'latest_report_period':latest,
            'currency':reporting_currency,'quote_currency':'USD','report_period_field':'report_period','records':records,'note':'报表期末日期不是首次公开日期；禁止直接作为点时回测特征。报表币种需用原始披露复核。'}

@lru_cache(maxsize=64)
def _sec_json(url, bucket):
    global _sec_last
    agent=os.getenv('SEC_USER_AGENT','').strip()
    if '@' not in agent: raise ValueError('SEC 披露查询需要配置 SEC_USER_AGENT，例如 YourName contact@example.com。')
    with _sec_lock:
        time.sleep(max(0,0.15-(time.monotonic()-_sec_last)))
        response=requests.get(url,headers={'User-Agent':agent,'Accept-Encoding':'gzip, deflate'},timeout=20)
        _sec_last=time.monotonic()
    response.raise_for_status();return response.json()

def sec_json(url): return _sec_json(url, int(time.time()//3600))

def announcements(symbol,start='',end='',limit=20):
    symbol=normalize_symbol(symbol)
    catalog=sec_json('https://www.sec.gov/files/company_tickers.json')
    match=next((r for r in catalog.values() if r['ticker'].upper().replace('.','-')==symbol),None)
    if not match: raise ValueError('SEC 公司代码表未找到该 ticker（ETF 等可能不在此表）。')
    cik=int(match['cik_str']);payload=sec_json(f'https://data.sec.gov/submissions/CIK{cik:010d}.json')
    lower=str(pd.Timestamp(start).date()) if start else str(today()-timedelta(days=180))
    upper=str(pd.Timestamp(end).date()) if end else str(today())
    if lower>upper:raise ValueError('开始日期不能晚于结束日期。')
    blocks=[payload.get('filings',{}).get('recent',{})]
    for file in payload.get('filings',{}).get('files',[]):
        if file.get('filingTo','')>=lower and file.get('filingFrom','')<=upper:
            blocks.append(sec_json('https://data.sec.gov/submissions/'+file['name']))
    rows=[]
    for block in blocks:
        for i,day in enumerate(block.get('filingDate',[])):
            if lower<=day<=upper:
                accession=block['accessionNumber'][i];doc=block['primaryDocument'][i]
                rows.append({'date':day,'form':block['form'][i], 'title':block['form'][i]+' '+block.get('primaryDocDescription',['']*len(block['filingDate']))[i],
                             'accession':accession,'url':f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace("-", "")}/{doc}'})
    rows=sorted({r['accession']:r for r in rows}.values(),key=lambda r:r['date'],reverse=True)
    return {**metadata('SEC EDGAR submissions'),'symbol':symbol,'cik':cik,'announcements':rows[:limit]}

def cross_section():
    # This is an explicitly bounded liquid-stock pool, not a full-exchange census.
    rows=[]
    for screen in ('most_actives','day_gainers','day_losers'):
        for item in yf().screen(screen,count=100).get('quotes',[]):
            if item.get('currency')!='USD':continue
            try:symbol=normalize_symbol(item['symbol'])
            except ValueError:continue
            stamp=item.get('regularMarketTime')
            if not stamp:continue
            rows.append({'代码':symbol,'名称':item.get('shortName',symbol),'最新价':item.get('regularMarketPrice'),
                         '涨跌幅':item.get('regularMarketChangePercent'),'成交量':item.get('regularMarketVolume'),
                         '总市值':item.get('marketCap'),'市盈率':item.get('trailingPE'),
                         '更新时间':datetime.fromtimestamp(stamp,NY).isoformat()})
    if not rows: raise ValueError('美股筛选数据源未返回可验证记录。')
    frame=pd.DataFrame(rows).drop_duplicates('代码'); frame.attrs['source']='Yahoo Finance predefined US screeners (bounded liquid-stock pool)'
    return frame

def market_events(days=7,limit=20):
    cutoff=today()-timedelta(days=days-1); events=[]
    for row in yf().Search('US stock market Federal Reserve earnings',max_results=0,news_count=limit).news:
        stamp=row.get('providerPublishTime')
        if not stamp:continue
        when=datetime.fromtimestamp(stamp,NY)
        if when.date()<cutoff:continue
        events.append({'date':when.date().isoformat(),'published_at':when.isoformat(),'title':row.get('title',''),'url':row.get('link',''),'source':row.get('publisher','Yahoo Finance')})
    return {**metadata('Yahoo Finance dated news'),'window_days':days,'window_start':str(cutoff),'window_end':str(today()),'events':events[:limit]}

def brief(limit=20):
    indices=[];errors=[]
    for symbol,name in [('^GSPC','S&P 500'),('^IXIC','NASDAQ Composite'),('^DJI','Dow Jones')]:
        try:
            frame=yf().Ticker(symbol).history(period='5d',auto_adjust=False)
            if len(frame)<2:raise ValueError('不足两根日线')
            last=float(frame['Close'].iloc[-1]);previous=float(frame['Close'].iloc[-2])
            indices.append({'symbol':symbol,'name':name,'price':last,'change_pct':(last/previous-1)*100,'as_of':str(frame.index[-1])})
        except Exception as exc:errors.append(f'{symbol}: {exc}')
    try:events=market_events(7,limit)['events']
    except Exception as exc:events=[];errors.append(str(exc))
    return {**metadata('Yahoo Finance indices and news'),'indices':indices,'events':events,'unavailable':errors}

def peers(symbol,days=30,limit=3):
    info=yf().Ticker(normalize_symbol(symbol)).get_info();industry=info.get('industryKey')
    if not industry:raise ValueError('美股行业标识缺失。')
    leaders=yf().Industry(industry).top_companies
    result=[]
    for ticker in leaders.index:
        if ticker==symbol:continue
        try:result.append({'symbol':ticker,'quote':quote(ticker),'announcements':announcements(ticker,str(today()-timedelta(days=days)),limit=3)['announcements']})
        except Exception as exc:result.append({'symbol':ticker,'unavailable':[str(exc)]})
        if len(result)>=limit:break
    return {**metadata('Yahoo Finance industry leaders + SEC EDGAR'),'symbol':symbol,'industry':industry,'enterprises':result}
