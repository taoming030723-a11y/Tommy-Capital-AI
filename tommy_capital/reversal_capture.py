"""Explicit small-sample real-data capture. Never run by the market scheduler."""
import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

from .data import Provider, SHANGHAI, completed_session, daily_history
from .technical import bars, extended_monthly_history, extended_weekly_history


def capture(codes, directory, as_of=None):
    if not codes or len(codes)>50 or len(set(codes))!=len(codes) or any(not re.fullmatch(r'\d{6}',c) for c in codes):
        raise ValueError('研究抓取须为1–50个不重复六位代码；不是全市场重新扫描')
    root=Path(directory);root.mkdir(parents=True,exist_ok=True)
    now=as_of or datetime.now(SHANGHAI)
    if now.tzinfo is None or now>datetime.now(SHANGHAI):raise ValueError('抓取时点无效/在未来')
    provider=Provider(cache=root/'.cache',timeout=90,retries=1)
    calendar=provider.fetch('tool_trade_date_hist_sina',ttl=86400)
    session=completed_session(calendar,now)
    if session!=now.date():raise ValueError('默认验证只在真实交易日收盘后采集；历史研究请使用已冻结CSV')
    calendar.to_csv(root/'calendar.csv',index=False,date_format='%Y-%m-%d')
    def one(code):
        p=Provider(cache=root/'.cache',timeout=90,retries=1)
        result={'code':code}
        try:
            quote=p.fetch('quote_tx_one',ttl=60,symbol=code).iloc[0]
            clock=pd.Timestamp(quote.quote_at)
            if clock.date()!=session or clock.hour<15:raise ValueError('未取得当天收盘报价')
            raw,warning=daily_history(p,code,'20150101',session.strftime('%Y%m%d'),session.strftime('%Y%m%d'),
                                      required_session=session,expected_close=float(quote.price))
            d=bars(raw);file=code+'.csv';d.to_csv(root/file,index_label='date',date_format='%Y-%m-%d',float_format='%.10g')
            result.update(name=str(quote['name']),file=file,sha256=hashlib.sha256((root/file).read_bytes()).hexdigest(),
                rows=len(d),first=d.index[0].date().isoformat(),last=d.index[-1].date().isoformat(),
                quote_price=float(quote.price),quote_at=clock.isoformat(),fetched_at=datetime.now(SHANGHAI).isoformat(),
                source=p.lineage[-1]['function'],adjust='qfq',anchor_date=session.isoformat(),warning=warning)
            for period,fn in [('monthly',extended_monthly_history),('weekly',extended_weekly_history)]:
                try:
                    frame=fn(p,code,d,session,session);name=code+'-'+period+'.csv'
                    frame.to_csv(root/name,index_label='date',date_format='%Y-%m-%d',float_format='%.10g')
                    result[period]={'file':name,'sha256':hashlib.sha256((root/name).read_bytes()).hexdigest(),
                                    'rows':len(frame),'validation':frame.attrs.get('price_validation')}
                except Exception as exc:
                    result[period]={'status':'unavailable','reason':str(exc)}
        except Exception as exc:
            result.update(status='unavailable',reason=str(exc))
        result['lineage']=p.lineage
        return result
    results=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(one,c) for c in codes]):results.append(future.result())
    manifest={'schema':1,'captured_at':datetime.now(SHANGHAI).isoformat(),'samples':sorted(results,key=lambda r:r['code']),
        'calendar_sha256':hashlib.sha256((root/'calendar.csv').read_bytes()).hexdigest(),'calendar_lineage':provider.lineage,
        'selection':'用户显式指定的小样本，不代表全市场；须记录选择依据和幸存者限制',
        'price_basis':'一致前复权锚点真实历史，不等于历史留存价格/财报PIT快照'}
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,allow_nan=False))
    return manifest


def main():
    parser=argparse.ArgumentParser(description='显式抓取可复现的真实Route C研究小样本')
    parser.add_argument('--codes',required=True,help='逗号分隔代码；不内置任何个股筛选例外')
    parser.add_argument('--out',required=True)
    args=parser.parse_args();result=capture(args.codes.split(','),args.out)
    print(json.dumps({'requested':len(result['samples']),'received':sum(s.get('status')!='unavailable' for s in result['samples'])}))


if __name__=='__main__':main()
