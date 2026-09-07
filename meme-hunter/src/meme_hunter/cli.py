from __future__ import annotations

import argparse
import time

from .radar import scan
from .storage import connect


def print_rows(rows: list[dict]) -> None:
    print(f"{'TOKEN':<12} {'SCORE':>6} {'M5%':>8} {'LIQUIDITY':>12} {'RISK':>8}  VETOES")
    for r in rows:
        veto = ",".join(r.get("vetoes") or ())
        print(f"{r['symbol'][:12]:<12} {r['score']:>6.1f} {r['m5']:>8.2f} ${r['liquidity']:>10,.0f} {r['risk']:>8}  {veto}")


def cmd_top(db: str, limit: int) -> None:
    conn = connect(db)
    rows = conn.execute("""
      SELECT symbol,score,price_change_m5,liquidity_usd,risk,vetoes_json,observed_at
      FROM observations ORDER BY observed_at DESC, score DESC LIMIT ?
    """, (limit,)).fetchall()
    print(f"{'TOKEN':<12} {'SCORE':>6} {'M5%':>8} {'LIQUIDITY':>12} {'RISK':>8}  OBSERVED")
    for symbol, score, m5, liq, risk, _, observed in rows:
        print(f"{(symbol or '?')[:12]:<12} {score:>6.1f} {m5:>8.2f} ${liq:>10,.0f} {risk:>8}  {observed}")


def main() -> None:
    p = argparse.ArgumentParser(prog="meme-hunter")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("scan", "watch", "top"):
        s = sub.add_parser(name)
        s.add_argument("--db", default="data/meme-radar.db")
        s.add_argument("--limit", type=int, default=30 if name != "top" else 20)
        if name == "watch":
            s.add_argument("--interval", type=int, default=30)
    args = p.parse_args()

    if args.command == "scan":
        print_rows(scan(args.db, args.limit))
    elif args.command == "top":
        cmd_top(args.db, args.limit)
    else:
        while True:
            print_rows(scan(args.db, args.limit))
            time.sleep(max(args.interval, 10))


if __name__ == "__main__":
    main()
