from crypto_bot.indicators import build_snapshots, ema, rsi


def test_ema_and_rsi_produce_values_after_warmup() -> None:
    closes = [100 + i * 0.5 for i in range(80)]
    volumes = [1000 + (i % 5) * 100 for i in range(80)]

    assert ema(closes, 9)[8] is not None
    assert rsi(closes, 14)[14] is not None
    snapshots = build_snapshots(closes, volumes)
    assert snapshots[49] is not None
