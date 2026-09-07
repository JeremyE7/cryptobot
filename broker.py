from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


D = Decimal


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    cost_basis: Decimal


@dataclass(frozen=True, slots=True)
class TradeResult:
    symbol: str
    side: str
    execution_price: Decimal
    quantity: Decimal
    fee: Decimal
    cash_after: Decimal
    realized_pnl: Decimal | None = None


class SimulatedBroker:
    """Single-position paper broker for our $10 experiment.

    quote_amount on buy is the total cash budget, fee included. This keeps the
    simulator from spending more cash than requested.
    """

    def __init__(
        self,
        initial_cash: Decimal | str = D("10"),
        fee_rate: Decimal | str = D("0.001"),
        slippage_rate: Decimal | str = D("0.0005"),
    ) -> None:
        self.initial_cash = D(initial_cash)
        self.cash = D(initial_cash)
        self.fee_rate = D(fee_rate)
        self.slippage_rate = D(slippage_rate)
        self.position: Position | None = None
        self.realized_pnl = D("0")
        self.total_fees = D("0")

    def buy(self, symbol: str, market_price: Decimal | str, quote_amount: Decimal | str) -> TradeResult:
        if self.position is not None:
            raise RuntimeError("Only one open position is allowed in v0.1")

        price = D(market_price)
        budget = D(quote_amount)
        if price <= 0 or budget <= 0:
            raise ValueError("market_price and quote_amount must be positive")
        if budget > self.cash:
            raise ValueError("Insufficient simulated cash")

        execution_price = price * (D("1") + self.slippage_rate)
        fee = budget * self.fee_rate
        net_for_asset = budget - fee
        quantity = net_for_asset / execution_price

        self.cash -= budget
        self.total_fees += fee
        self.position = Position(
            symbol=symbol.upper(),
            quantity=quantity,
            entry_price=execution_price,
            cost_basis=budget,
        )

        return TradeResult(
            symbol=symbol.upper(),
            side="BUY",
            execution_price=execution_price,
            quantity=quantity,
            fee=fee,
            cash_after=self.cash,
        )

    def sell_all(self, market_price: Decimal | str) -> TradeResult:
        if self.position is None:
            raise RuntimeError("No open position to sell")

        price = D(market_price)
        if price <= 0:
            raise ValueError("market_price must be positive")

        position = self.position
        execution_price = price * (D("1") - self.slippage_rate)
        gross = position.quantity * execution_price
        fee = gross * self.fee_rate
        net = gross - fee
        pnl = net - position.cost_basis

        self.cash += net
        self.total_fees += fee
        self.realized_pnl += pnl
        self.position = None

        return TradeResult(
            symbol=position.symbol,
            side="SELL",
            execution_price=execution_price,
            quantity=position.quantity,
            fee=fee,
            cash_after=self.cash,
            realized_pnl=pnl,
        )

    def equity(self, market_price: Decimal | str | None = None) -> Decimal:
        if self.position is None:
            return self.cash
        if market_price is None:
            raise ValueError("market_price is required while a position is open")

        price = D(market_price)
        hypothetical_execution = price * (D("1") - self.slippage_rate)
        gross = self.position.quantity * hypothetical_execution
        fee = gross * self.fee_rate
        return self.cash + gross - fee

    @staticmethod
    def money(value: Decimal) -> Decimal:
        return value.quantize(D("0.0001"), rounding=ROUND_DOWN)
