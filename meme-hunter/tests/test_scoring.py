from meme_hunter.scoring import score_pair


def pair(liq=200_000, m5=18, h1=45, v5=80_000, v1h=300_000, buys=120, sells=40, age_min=30):
    now = 2_000_000_000_000
    return {
        "liquidity": {"usd": liq}, "priceChange": {"m5": m5, "h1": h1},
        "volume": {"m5": v5, "h1": v1h}, "txns": {"m5": {"buys": buys, "sells": sells}},
        "pairCreatedAt": now - age_min * 60_000,
    }, now


def test_good_candidate_scores_without_veto():
    p, now = pair()
    r = score_pair(p, {"top1_pct": 8, "top5_pct": 24}, now_ms=now)
    assert r.score >= 60
    assert not r.vetoes


def test_low_liquidity_is_vetoed():
    p, now = pair(liq=10_000)
    r = score_pair(p, {}, now_ms=now)
    assert "liquidity<25k" in r.vetoes
    assert r.risk == "REJECT"


def test_concentrated_holder_is_vetoed():
    p, now = pair()
    r = score_pair(p, {"top1_pct": 31, "top5_pct": 55}, now_ms=now)
    assert "top1>25%" in r.vetoes
    assert "top5>50%" in r.vetoes
    assert r.risk == "REJECT"
