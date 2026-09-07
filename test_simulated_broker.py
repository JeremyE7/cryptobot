from decimal import Decimal

import pytest

from crypto_bot.simulation.broker import SimulatedBroker


def test_buy_and_sell_updates_cash_and_pnl() -> None:
    broker = SimulatedBroker(initial_cash="10", fee_rate="0.001", slippage_rate="0")

    buy = broker.buy("SOLUSDT", "100", "5")
    assert buy.fee == Decimal("0.005")
    assert broker.cash == Decimal("5")
    assert broker.position is not None

    sell = broker.sell_all("104")

    assert sell.realized_pnl is not None
    assert sell.realized_pnl > 0
    assert broker.cash > Decimal("10")
    assert broker.position is None


def test_broker_rejects_second_position() -> None:
    broker = SimulatedBroker()
    broker.buy("BTCUSDT", "100000", "5")

    with pytest.raises(RuntimeError):
        broker.buy("ETHUSDT", "4000", "2")
