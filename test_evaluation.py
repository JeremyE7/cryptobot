from crypto_bot.evaluation.walkforward import _aggregate_pf
from crypto_bot.backtest.trend_engine import TrendTrade


def trade(pnl: float) -> TrendTrade:
    return TrendTrade("X",0,1,1,1,1,1,2,0.5,0.1,0.01,1.0,55,pnl,pnl,0,0,pnl*100,"TAKE" if pnl>0 else "STOP")


def test_aggregate_profit_factor():
    assert _aggregate_pf([trade(2), trade(-1)]) == 2
