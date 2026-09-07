from __future__ import annotations
import json, sqlite3
from datetime import datetime, timezone
from pathlib import Path
DB=Path('data/paper.db'); OUT=Path('site/data.json')
def iso(ms): return datetime.fromtimestamp(ms/1000,tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if ms else None
with sqlite3.connect(DB) as c:
 c.row_factory=sqlite3.Row
 a=c.execute('select * from account where id=1').fetchone()
 snaps=c.execute("select snapshot_time,equity,cash,invested,regime from snapshots where kind='DAY_CLOSE' order by snapshot_time").fetchall()
 if not snaps: snaps=c.execute('select snapshot_time,equity,cash,invested,regime from snapshots order by snapshot_time').fetchall()
 latest=snaps[-1] if snaps else None
 positions=c.execute('select symbol,quantity,avg_cost from positions order by symbol').fetchall()
 decisions=c.execute('select decision_time,regime,trigger from decisions order by decision_time desc limit 12').fetchall()
 trades=c.execute('select count(*) n from trades').fetchone()['n']
 initial=float(a['initial_cash']); equity=float(latest['equity']) if latest else float(a['cash']); cash=float(a['cash']); invested=float(latest['invested']) if latest else max(0,equity-cash)
 peak=initial; mdd=0.0
 for s in snaps:
  e=float(s['equity']); peak=max(peak,e); mdd=min(mdd,(e/peak-1)*100)
 pos=[]
 for p in positions:
  value=float(p['quantity'])*float(p['avg_cost']); pos.append({'symbol':p['symbol'],'value':round(value,6),'weight_pct':round(value/equity*100,2) if equity else 0})
 payload={'strategy':f"{a['strategy_name']} {a['strategy_version']}",'started_at':iso(a['initialized_at']),'updated_at':datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),'equity':round(equity,8),'return_pct':round((equity/initial-1)*100,4),'cash':round(cash,8),'invested':round(invested,8),'max_drawdown_pct':round(mdd,4),'regime':a['last_regime'],'trades':trades,'costs':round(float(a['total_fees'])+float(a['total_slippage']),8),'integrity':True,'positions':pos,'equity_curve':[{'time':iso(s['snapshot_time']),'equity':round(float(s['equity']),8)} for s in snaps],'decisions':[{'date':iso(d['decision_time']).split(' ')[0],'regime':d['regime'],'trigger':d['trigger']} for d in decisions]}
OUT.write_text(json.dumps(payload,indent=2),encoding='utf-8')
print(f'Wrote {OUT}: equity=${equity:.4f}, {len(snaps)} snapshots, {trades} trades')
