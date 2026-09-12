"""Operator CLI.  python -m asm.cli <command>"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal

from asm.config import settings
from asm.logging import get_logger, setup_logging

log = get_logger(__name__)


async def cmd_seed(args) -> int:
    from asm.seeding import import_seed_file

    result = await import_seed_file(args.path or settings.seed_file,
                                    enqueue_backfill=not args.no_backfill)
    print(json.dumps(result, indent=2))
    if result["imported"] == 0 and result.get("total", 0) == 0:
        print("\nNothing imported. Add wallet addresses to "
              f"{args.path or settings.seed_file} (one per line).", file=sys.stderr)
        return 1
    return 0


async def cmd_backfill(args) -> int:
    from asm.db import repo
    from asm.db.session import session_scope
    from asm.services.tasks.intelligence import backfill_wallet

    if args.wallet:
        wallets = [args.wallet]
    else:
        async with session_scope() as s:
            wallets = await repo.watchlist_wallets(s)
    if not wallets:
        print("no wallets to backfill; run: asm seed", file=sys.stderr)
        return 1

    for w in wallets:
        try:
            result = await backfill_wallet({}, w, pages=args.pages)
            print(json.dumps(result))
        except Exception as exc:
            print(f"{w[:8]}: FAILED {exc!r}", file=sys.stderr)
    return 0


async def cmd_status(args) -> int:
    from asm.state.ledger import RiskLedger, SystemControl

    snap = await RiskLedger().snapshot()
    state = await SystemControl().get()
    print(json.dumps({
        "mode": settings.mode.value,
        "system_state": state.value,
        "equity_usd": str(snap.total_equity_usd),
        "cash_usd": str(snap.cash_usd),
        "deployed_usd": str(snap.deployed_usd),
        "open_positions": snap.open_positions,
        "drawdown_pct": str(snap.drawdown_pct),
        "realized_pnl_today_usd": str(snap.realized_pnl_today_usd),
        "harvested_total_usd": str(snap.harvested_total_usd),
    }, indent=2))
    return 0


async def cmd_reset(args) -> int:
    from asm.state.ledger import RiskLedger

    if settings.is_live and not args.force:
        print("refusing to reset the ledger in live mode without --force", file=sys.stderr)
        return 1
    await RiskLedger().bootstrap(Decimal(str(args.capital)), force=True)
    print(f"ledger reset: {args.capital} USD ({settings.mode.value})")
    return 0


async def cmd_start(args) -> int:
    from asm.state.ledger import SystemControl

    try:
        await SystemControl().start(actor="cli")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("system_state=running — trading is live")
    return 0


async def cmd_stop(args) -> int:
    from asm.state.ledger import SystemControl

    await SystemControl().stop(actor="cli")
    print("system_state=stopped — no new positions; exits still managed")
    return 0


async def cmd_pause(args) -> int:
    from asm.domain.enums import SystemState
    from asm.state.ledger import SystemControl

    state = SystemState.HARD_PAUSED if args.hard else SystemState.SOFT_PAUSED
    await SystemControl().set(state, actor="cli")
    print(f"system_state={state.value}")
    return 0


async def cmd_resume(args) -> int:
    from asm.domain.enums import SystemState
    from asm.state.ledger import SystemControl

    await SystemControl().set(SystemState.RUNNING, actor="cli")
    print("system_state=running")
    return 0


async def cmd_kill(args) -> int:
    from asm.state.ledger import SystemControl

    await SystemControl().kill(actor="cli")
    print("KILL SWITCH ACTIVE - signing is refused until cleared")
    return 0


async def cmd_promote(args) -> int:
    from asm.db import repo
    from asm.db.session import session_scope
    from asm.domain.enums import TraderStatus

    async with session_scope() as s:
        await repo.set_trader_status(s, args.wallet, TraderStatus(args.status))
        await repo.log_audit(s, actor="cli", action="set_trader_status",
                             target=args.wallet, after={"status": args.status})
    print(f"{args.wallet}: {args.status}")
    return 0


async def cmd_backtest(args) -> int:
    """PRD 44 - replay history and print the verdict. This gates live trading."""
    from decimal import Decimal as D

    from asm.backtest import BacktestConfig, BacktestEngine, load_from_chain, load_traders
    from asm.db import repo
    from asm.db.session import session_scope

    if args.wallet:
        wallets = [args.wallet]
    else:
        async with session_scope() as s:
            wallets = await repo.watchlist_wallets(s)
    if not wallets:
        print("no wallets; run: asm seed", file=sys.stderr)
        return 1

    sol_price = D(str(args.sol_price))
    print(f"loading history for {len(wallets)} wallet(s)...", file=sys.stderr)
    trades, histories = await load_from_chain(wallets, pages=args.pages, sol_price=sol_price)
    if not trades:
        print("no on-chain history found. Is HELIUS_API_KEY set in .env?", file=sys.stderr)
        return 1

    traders = await load_traders(wallets)
    if not traders:
        print("wallets have no scored profiles; run: asm backfill", file=sys.stderr)
        return 1

    engine = BacktestEngine(
        histories=histories, traders=traders,
        config=BacktestConfig(
            starting_capital_usd=D(str(args.capital)),
            assumed_detection_latency_ms=args.latency_ms,
            seed=args.seed, sol_price=sol_price,
        ),
    )
    result = await engine.run(trades)
    summary = result.summary()
    print(json.dumps(summary, indent=2))
    print("\n" + "=" * 62, file=sys.stderr)
    print(summary["verdict"], file=sys.stderr)
    print("=" * 62, file=sys.stderr)
    return 0 if summary["verdict"].startswith("VIABLE") else 2


async def cmd_research(args) -> int:
    from asm.services.tasks.research import research_summary

    print(json.dumps(await research_summary({}, args.days), indent=2))
    return 0


async def cmd_graph(args) -> int:
    from asm.services.tasks.graph import rebuild_wallet_graph

    print(json.dumps(await rebuild_wallet_graph({}, args.days), indent=2))
    return 0


async def cmd_retention(args) -> int:
    from asm.services.tasks.retention import apply_retention

    print(json.dumps(await apply_retention({}, dry_run=not args.apply), indent=2))
    return 0


async def cmd_harvest(args) -> int:
    from asm.engine.harvest import maybe_harvest
    from asm.state.ledger import RiskLedger

    decision = await maybe_harvest(RiskLedger())
    print(json.dumps({
        "harvested": decision.should_harvest,
        "amount_usd": str(decision.amount_usd),
        "retained_usd": str(decision.retained_usd),
        "reason": decision.reason,
    }, indent=2))
    return 0


async def cmd_alert(args) -> int:
    from asm.alerts import Category, Level, alerter

    a = alerter()
    if not a.channels:
        print("no alert channels configured; set TELEGRAM_* / DISCORD_WEBHOOK_URL",
              file=sys.stderr)
    ok = await a.send(Level(args.level), Category.SYSTEM, args.title,
                      body="test alert from the asm CLI")
    print(f"delivered={ok} channels={[c.name for c in a.channels]}")
    return 0


COMMANDS = {
    "seed": cmd_seed, "backfill": cmd_backfill, "status": cmd_status,
    "reset": cmd_reset, "pause": cmd_pause, "resume": cmd_resume,
    "kill": cmd_kill, "promote": cmd_promote, "backtest": cmd_backtest,
    "start": cmd_start, "stop": cmd_stop,
    "research": cmd_research, "graph": cmd_graph, "retention": cmd_retention,
    "harvest": cmd_harvest, "alert": cmd_alert,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="asm", description="Adaptive smart-money copy trading")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("seed", help="import the manual wallet list")
    s.add_argument("path", nargs="?", default=None)
    s.add_argument("--no-backfill", action="store_true")

    b = sub.add_parser("backfill", help="reconstruct history and score wallets")
    b.add_argument("wallet", nargs="?", default=None)
    b.add_argument("--pages", type=int, default=10)

    sub.add_parser("status", help="portfolio and system state")

    r = sub.add_parser("reset", help="reset the risk ledger")
    r.add_argument("--capital", default="1000")
    r.add_argument("--force", action="store_true")

    pa = sub.add_parser("pause", help="stop new entries")
    pa.add_argument("--hard", action="store_true", help="also block the signer")

    sub.add_parser("start", help="START trading (required; nothing runs until this)")
    sub.add_parser("stop", help="stop opening new positions")
    sub.add_parser("resume", help="resume after a pause")
    sub.add_parser("kill", help="emergency kill switch")

    pr = sub.add_parser("promote", help="manually set a trader's status")
    pr.add_argument("wallet")
    pr.add_argument("status")

    bt = sub.add_parser("backtest", help="replay history; gates live trading (PRD 44)")
    bt.add_argument("wallet", nargs="?", default=None)
    bt.add_argument("--pages", type=int, default=10)
    bt.add_argument("--capital", default="10000")
    bt.add_argument("--latency-ms", type=int, default=2500)
    bt.add_argument("--seed", type=int, default=1337)
    bt.add_argument("--sol-price", default="150")

    rs = sub.add_parser("research", help="gate effectiveness, latency, contribution")
    rs.add_argument("--days", type=int, default=30)

    gr = sub.add_parser("graph", help="rebuild the wallet relationship graph")
    gr.add_argument("--days", type=int, default=30)

    rt = sub.add_parser("retention", help="show (or apply) the data retention policy")
    rt.add_argument("--apply", action="store_true", help="actually delete")

    sub.add_parser("harvest", help="evaluate and run profit harvesting now")

    al = sub.add_parser("alert", help="send a test alert through configured channels")
    al.add_argument("--level", default="warning", choices=["info", "warning", "critical"])
    al.add_argument("--title", default="ASM test alert")
    return p


def main() -> int:
    setup_logging()
    args = build_parser().parse_args()
    return asyncio.run(COMMANDS[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())
