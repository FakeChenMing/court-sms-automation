"""Reproducible arithmetic only. Legal applicability and anchor proof are reviewed separately."""
from datetime import date,timedelta
from pathlib import Path
import json,calendar
DATA=json.loads((Path(__file__).resolve().parent.parent/'references/holidays.json').read_text())
def working(day):
 cfg=DATA['years'].get(str(day.year))
 if not cfg:raise ValueError('该年度官方节假日尚未核验')
 s=day.isoformat()
 return s in cfg['working_weekends'] or day.weekday()<5 and s not in cfg['holidays']
def calculate(spec):
 anchor=date.fromisoformat(spec['anchor_date']);n=spec['amount'];assert isinstance(n,int) and not isinstance(n,bool) and 0<=n<=3660, '期限数量须为0至3660的整数'
 unit=spec['unit'];direction=spec.get('direction',1);assert direction in (-1,1)
 if unit=='calendar_days':result=anchor+timedelta(days=direction*n)
 elif unit=='working_days':
  result=anchor;remaining=n
  while remaining:
   result+=timedelta(days=direction)
   if working(result):remaining-=1
 elif unit in ('months','years'):
  total=anchor.year*12+anchor.month-1+direction*n*(12 if unit=='years' else 1)
  year,month=divmod(total,12);month+=1
  result=date(year,month,min(anchor.day,calendar.monthrange(year,month)[1]))
 else:raise ValueError('不支持的期限单位')
 raw=result
 adjustment=spec.get('adjustment','none')
 assert adjustment in ('none','next_working','previous_working')
 if adjustment!='none':
  while not working(result):result+=timedelta(days=1 if adjustment=='next_working' else -1)
 # Even an unadjusted legal date must have a known calendar; the rule decides whether to adjust.
 working(result)
 return {'date':result.isoformat(),'unadjusted':raw.isoformat(),'calendar':DATA['years'][str(result.year)]['official_url'],'calendar_checked_at':DATA['checked_at']}
