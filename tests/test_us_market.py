import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import us_market as us
import us_strategy as strategy
import stock_data_server as data

class USMarketTests(unittest.TestCase):
    def test_symbol_and_market_routing(self):
        self.assertEqual(us.normalize_symbol('NYSE:brk.b'),'BRK-B')
        self.assertEqual(data.normalize_market('美股'),'us')
        self.assertEqual(data.normalize_symbol('AAPL','us'),'AAPL')
        for bad in ('../AAPL','600519','AAPL;foo','AAPL/USD'):
            with self.assertRaises(ValueError):us.normalize_symbol(bad)

    def test_holidays_dst_and_early_close(self):
        self.assertFalse(us.trading_day('2026-07-03')['is_trading_day'])
        self.assertFalse(us.trading_day('2026-07-04')['is_trading_day'])
        self.assertIn('09:30:00-04:00',us.trading_day('2026-07-06')['open'])
        self.assertIn('09:30:00-05:00',us.trading_day('2026-01-06')['open'])
        self.assertIn('13:00:00',us.trading_day('2026-11-27')['close'])

    def test_history_inclusive_end_adjustment_and_fields(self):
        provider=MagicMock();frame=pd.DataFrame({'Open':[10.],'High':[12.],'Low':[9.],'Close':[11.],'Volume':[100]},index=pd.DatetimeIndex(['2026-01-05'],tz=us.NY,name='Date'))
        provider.Ticker.return_value.history.return_value=frame
        with patch.object(us,'yf',return_value=provider):
            result=us.history('BRK.B','2026-01-01','2026-01-05','qfq')
            kwargs=provider.Ticker.return_value.history.call_args.kwargs
            self.assertEqual(kwargs['end'],'2026-01-06');self.assertTrue(kwargs['auto_adjust'])
            self.assertEqual(result.attrs['currency'],'USD');self.assertEqual(result.iloc[-1].close,11)
            with self.assertRaises(ValueError):us.history('AAPL',adjust='hfq')

    def test_history_uses_labeled_recent_cache_when_provider_returns_empty(self):
        provider=MagicMock()
        provider.Ticker.return_value.history.return_value=pd.DataFrame({'Open':[10.],'High':[12.],'Low':[9.],'Close':[11.],'Volume':[100]},index=pd.DatetimeIndex(['2026-01-05'],tz=us.NY,name='Date'))
        with tempfile.TemporaryDirectory() as tmp,patch.object(us,'_cache_root',Path(tmp)),patch.object(us,'yf',return_value=provider):
            us.history('AAPL','2026-01-05','2026-01-05','')
            provider.Ticker.return_value.history.return_value=pd.DataFrame()
            cached=us.history('AAPL','2026-01-05','2026-01-05','')
            self.assertEqual(cached.iloc[0]['close'],11)
            self.assertIn('cached',cached.attrs['source'])

    def test_quote_requires_provider_time_and_currency(self):
        provider=MagicMock();provider.Ticker.return_value.get_info.return_value={'regularMarketPrice':100,'currency':'USD','regularMarketTime':1767623400}
        with patch.object(us,'yf',return_value=provider):
            row=us.quote('AAPL');self.assertIn('2026-01-05',row['更新时间'])
            provider.Ticker.return_value.get_info.return_value.pop('regularMarketTime')
            with self.assertRaises(ValueError):us.quote('AAPL')

    def test_sec_filings_dates_and_urls(self):
        def source(url):
            if url.endswith('company_tickers.json'):return {'0':{'ticker':'AAPL','cik_str':320193}}
            return {'filings':{'recent':{'filingDate':['2026-01-05','2025-01-05'],'accessionNumber':['0000320193-26-000001','0000320193-25-000001'],'primaryDocument':['report.htm','old.htm'],'form':['10-Q','10-K']}}}
        with patch.object(us,'sec_json',side_effect=source):
            result=us.announcements('AAPL','2026-01-01','2026-01-31')
            self.assertEqual(len(result['announcements']),1)
            self.assertEqual(result['announcements'][0]['url'],'https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/report.htm')

    def test_us_branches_never_call_hk_provider(self):
        with patch.object(us,'quote',return_value={'最新价':10,'更新时间':'2026-01-05T16:00:00-05:00'}),patch.object(data.ak,'stock_hk_spot_em',create=True,side_effect=AssertionError('wrong market')):
            self.assertEqual(data.quote_response('AAPL','us')['currency'],'USD')

    def test_us_holdings_support_fractional_shares(self):
        self.assertEqual(data.import_holdings({'holdings':[{'market':'us','symbol':'AAPL','shares':1.25}]})[0]['shares'],1.25)
        with self.assertRaises(ValueError):data.import_holdings({'holdings':[{'market':'a','symbol':'600519','shares':1.25}]})

class USStrategyTests(unittest.TestCase):
    def bars(self):
        dates=pd.bdate_range('2025-01-02',periods=150);values=100+np.arange(150)*.1+np.sin(np.arange(150))
        return pd.DataFrame({'date':dates,'open':values,'high':values+2,'low':values-2,'close':values+1,'volume':10000})

    def test_features_are_causal(self):
        bars=self.bars();first=strategy.features(bars)
        bars.loc[100:,'close']*=2;second=strategy.features(bars)
        pd.testing.assert_frame_equal(first.loc[:98,strategy.FEATURES],second.loc[:98,strategy.FEATURES])

    def test_simulation_uses_next_session_and_costs(self):
        frame=pd.DataFrame([{'date':pd.Timestamp('2026-01-05'),'entry_day':pd.Timestamp('2026-01-06'),'entry_open':100.,'exit_close':110.,'score':1.,'eligible':True,'symbol':'AAPL'}])
        curve,trades,stats=strategy.simulate(frame,'2026-01-05','2026-01-06',1,5,5)
        self.assertEqual(curve.iloc[0].equity,100000.)
        self.assertEqual(trades.iloc[0].signal_day,'2026-01-05')
        self.assertGreater(stats['costs'],0);self.assertLess(stats['total_return'],.1)
        frame['date']=pd.Timestamp('2026-01-02')
        self.assertEqual(strategy.simulate(frame,'2026-01-05','2026-01-06')[2]['trades'],0)

    def test_training_checkpoint_and_leakage_boundary(self):
        frame=strategy.features(self.bars());frame['symbol']='AAPL'
        with tempfile.TemporaryDirectory() as tmp,patch.object(strategy,'STORE',Path(tmp)):
            record=strategy.train(frame,'elastic_net','2025-01-01','2025-07-31',{'alpha_scale':.01,'l1_ratio':.5,'max_iter':1000})
            self.assertEqual(len(strategy.checkpoints()),1)
            with self.assertRaises(ValueError):strategy.model_scores(frame.tail(1),'elastic_net',record['model_id'],'2025-01-01')
            values=strategy.model_scores(frame.tail(1),'elastic_net',record['model_id'],'2025-08-01')
            self.assertTrue(np.isfinite(values).all())
            (Path(tmp)/record['model_id']/'weights.json').write_text('{}')
            self.assertEqual(strategy.checkpoints(),[])

if __name__=='__main__':unittest.main()
