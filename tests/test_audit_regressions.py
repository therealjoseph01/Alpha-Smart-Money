"""Regressions found by the PRD/service-path audit; no on-chain sends."""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from asm.config import settings
from asm.domain.enums import Action, ExitReason, OrderStatus, PositionStatus, Reason, SystemState
from asm.domain.models import ApprovedOrder, Position
from asm.domain.money import SOL_MINT, USDC_MINT
from asm.engine.exits import ExitIntent, evaluate_exits, update_marks
from asm.engine.gates import price_chase_gate
from asm.execution.paper import PaperExecutor
from asm.state import keys
from asm.state.ledger import SystemControl
from tests import factories as F


def order(action=Action.BUY, **kw):
    c = F.candidate()
    values = dict(idempotency_key='audit-order', decision_id=c.id, candidate=c,
                  action=action, input_mint=SOL_MINT if action is Action.BUY else F.MINT,
                  output_mint=F.MINT if action is Action.BUY else SOL_MINT,
                  in_amount_raw=1_000_000_000, size_usd=Decimal('100'), slippage_bps=300)
    return ApprovedOrder(**(values | kw))


def position(**kw):
    return Position(**(dict(token_mint=F.MINT, trader_wallet=F.WALLET,
                           status=PositionStatus.OPEN, decimals=6, amount_raw=100_000_000,
                           cost_basis_usd=Decimal('100'), entry_price=Decimal('1'),
                           current_price=Decimal('1'), peak_price=Decimal('1')) | kw))


async def test_pending_entries_count_toward_position_cap(ledger, monkeypatch):
    monkeypatch.setattr(settings.copyability, 'max_open_positions', 1)
    first = await ledger.reserve(mint='a', wallet='a', size_usd=Decimal(20))
    second = await ledger.reserve(mint='b', wallet='b', size_usd=Decimal(20))
    assert first.granted and second.reason is Reason.MAX_OPEN_POSITIONS


async def test_held_reservation_does_not_expire_and_leak_cash(ledger):
    res = await ledger.reserve(mint='a', wallet='a', size_usd=Decimal(20))
    assert await ledger.r.ttl(keys.reservation(res.id, ledger.mode)) == -1
    await ledger.release(res.id)
    assert (await ledger.snapshot()).cash_usd == Decimal(10000)


async def test_kill_cannot_be_cleared_by_pause_then_resume(ledger):
    control = SystemControl(ledger.r)
    await control.kill()
    await control.set(SystemState.SOFT_PAUSED)
    await control.set(SystemState.RUNNING)
    assert await control.get() is SystemState.KILLED
    res = await ledger.reserve(mint='a', wallet='a', size_usd=Decimal(20))
    assert res.reason is Reason.KILL_SWITCH_ACTIVE
    await control.clear_kill()
    assert await control.get() is SystemState.HARD_PAUSED
    await control.set(SystemState.RUNNING)
    assert (await ledger.reserve(mint='a', wallet='a', size_usd=Decimal(20))).granted


async def test_invalid_system_state_fails_closed(ledger):
    await ledger.r.set(keys.SYSTEM_STATE, 'corrupt')
    assert await SystemControl(ledger.r).get() is SystemState.HARD_PAUSED
    assert not (await ledger.reserve(mint='a', wallet='a', size_usd=Decimal(20))).granted


def test_falling_marks_are_persistable():
    p = position()
    assert update_marks(p, F.market(price_usd=Decimal('.9')))
    assert p.current_price == Decimal('.9')


def test_zero_entry_does_not_crash_trailing_rule():
    p = position(entry_price=Decimal(0))
    assert evaluate_exits(p, F.market()) is None


def test_stablecoin_chase_compares_prices_in_same_currency():
    c = F.candidate(source_trade=F.source_trade(quote_mint=USDC_MINT),
                    market=F.market(price=Decimal('.01'), price_usd=Decimal('1.20')))
    assert price_chase_gate(c, settings.copyability).reasons == [Reason.PRICE_MOVED_TOO_FAR]


def test_unknown_source_price_fails_closed():
    c = F.candidate(source_trade=F.source_trade(source_price=Decimal(0)))
    assert not price_chase_gate(c, settings.copyability).passed


async def test_paper_sell_has_adverse_slippage_and_sol_output(monkeypatch):
    o = order(Action.SELL)
    o.candidate.market = F.market(price_usd=Decimal('100'))
    o.candidate.quote = F.quote(input_mint=F.MINT, output_mint=SOL_MINT,
                                out_amount_raw=600_000_000, price_impact_pct=Decimal('2'))
    executor = PaperExecutor()
    monkeypatch.setattr(executor, '_failed', lambda _: False)
    ex = await executor.execute(o)
    assert ex.succeeded and ex.actual_price == Decimal(98)
    assert ex.value_usd == Decimal(98)
    assert ex.out_amount_raw == 600_000_000


async def test_paper_exit_fetches_missing_quote(monkeypatch):
    import asm.adapters.jupiter as J
    o = order(Action.SELL)
    o.candidate.quote = None
    fetch = AsyncMock(return_value=F.quote())
    monkeypatch.setattr(J, 'jupiter', lambda: SimpleNamespace(quote=fetch))
    executor = PaperExecutor()
    monkeypatch.setattr(executor, '_failed', lambda _: False)
    assert (await executor.execute(o)).succeeded
    assert fetch.await_args.kwargs['amount_raw'] == o.in_amount_raw


async def test_missing_holder_data_is_not_safe(monkeypatch):
    import asm.engine.token_safety as T
    h = SimpleNamespace(get_mint_account=AsyncMock(return_value={
        'mintAuthority': None, 'freezeAuthority': None, 'supply': '100'}),
        get_largest_token_accounts=AsyncMock(return_value=[]))
    monkeypatch.setattr(T, 'helius', lambda: h)
    assert (await T._evaluate(F.MINT, None)).unknown
    h.get_mint_account.return_value = {}
    assert (await T._evaluate(F.MINT, None)).unknown


async def test_position_exit_updates_remaining_amount_and_ladder(redis, monkeypatch):
    import asm.services.position_service as P
    svc = P.PositionService()
    svc.executor = PaperExecutor()
    monkeypatch.setattr(svc.executor, '_failed', lambda _: False)
    svc.ledger = SimpleNamespace(release_position=AsyncMock())
    svc.bus = SimpleNamespace(publish=AsyncMock())
    monkeypatch.setattr(P, 'jupiter', lambda: SimpleNamespace(quote=AsyncMock(return_value=F.quote())))
    saved = AsyncMock()
    monkeypatch.setattr(P.repo, 'save_order', AsyncMock())
    monkeypatch.setattr(P.repo, 'save_execution', AsyncMock())
    monkeypatch.setattr(P.repo, 'reduce_position', saved)
    monkeypatch.setattr(P.repo, 'log_event', AsyncMock())

    @asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr(P, 'session_scope', session)
    p = position()
    ok = await svc._execute_exit(p, F.market(decimals=6, price_usd=Decimal(2)),
                                ExitIntent(ExitReason.TAKE_PROFIT, Decimal('.25'), {'ladder_index': 0}))
    assert ok and p.amount_raw == 75_000_000
    assert p.cost_basis_usd == Decimal(75)
    assert p.tp_levels_hit == [0]
    assert saved.await_args.kwargs['ladder_index'] == 0


async def test_unavailable_simulation_is_rejected(monkeypatch):
    import asm.execution.live as L
    monkeypatch.setattr(settings, 'mev_protection_enabled', False)
    monkeypatch.setattr(L, 'jupiter', lambda: SimpleNamespace(
        quote=AsyncMock(return_value=F.quote()),
        build_swap=AsyncMock(return_value={'swapTransaction': 'dGVzdA=='})))
    monkeypatch.setattr(L, 'helius', lambda: SimpleNamespace(
        priority_fee=AsyncMock(return_value=1000), simulate=AsyncMock(return_value=None)))
    ex = await L.ShadowExecutor(signer=SimpleNamespace(pubkey='test')).execute(order())
    assert ex.status is OrderStatus.FAILED
    assert ex.failure_reason == 'simulation_result_unavailable'


@pytest.mark.parametrize('send_error,confirm_result', [(True, None), (False, None)])
async def test_ambiguous_live_submission_retains_signature(redis, monkeypatch, send_error, confirm_result):
    import base64

    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction

    import asm.execution.live as L
    kp = Keypair()
    tx = VersionedTransaction(MessageV0.try_compile(kp.pubkey(), [], [], Hash.default()), [kp])
    signature = str(tx.signatures[0])
    signed = base64.b64encode(bytes(tx)).decode()
    executor = L.LiveExecutor(signer=SimpleNamespace(sign=AsyncMock(return_value=signed)))
    monkeypatch.setattr(executor, '_prepare', AsyncMock(return_value='unsigned'))
    sender = AsyncMock(side_effect=TimeoutError()) if send_error else AsyncMock(return_value=signature)
    monkeypatch.setattr(L, 'helius', lambda: SimpleNamespace(
        send_transaction=sender, confirm=AsyncMock(return_value=confirm_result)))
    ex = await executor.execute(order())
    assert ex.status is OrderStatus.SUBMITTED and ex.signature == signature
    assert await executor.dedupe.lookup_order('audit-order') == signature


def test_closed_token_account_preserves_six_decimals():
    from asm.ingest.parser import parse_transaction
    from tests.test_parser import WALLET, build_tx
    tx = build_tx(token_delta=-1_000_000, sol_delta_lamports=500_000_000)
    tx['meta']['postTokenBalances'] = []
    parsed = parse_transaction(tx, WALLET, sol_price_usd=Decimal(200))
    assert parsed.token_decimals == 6 and parsed.source_price == Decimal('.5')


def test_stablecoin_trade_not_misread_as_sol_rent():
    from asm.ingest.parser import parse_transaction
    from tests.test_parser import WALLET, build_tx
    tx = build_tx(token_delta=-1_000_000, sol_delta_lamports=-2_000_000)
    for key, amount in [('preTokenBalances', '0'), ('postTokenBalances', '100000000')]:
        tx['meta'][key].append({'accountIndex': 2, 'mint': USDC_MINT, 'owner': WALLET,
                               'uiTokenAmount': {'amount': amount, 'decimals': 6}})
    parsed = parse_transaction(tx, WALLET, sol_price_usd=Decimal(200))
    assert parsed.action is Action.SELL and parsed.quote_mint == USDC_MINT
    assert parsed.source_value_usd == Decimal(100)


def test_partial_source_exit_records_actual_fraction():
    from asm.ingest.parser import parse_transaction
    from tests.test_parser import WALLET, build_tx
    tx = build_tx(token_delta=-250_000, sol_delta_lamports=500_000_000, post_token=750_000)
    parsed = parse_transaction(tx, WALLET, sol_price_usd=Decimal(200))
    assert Decimal(parsed.raw['sold_fraction']) == Decimal('.25')


def test_lp_deposit_with_payment_is_not_a_buy():
    from asm.ingest.parser import parse_transaction
    from tests.test_parser import WALLET, build_tx
    tx = build_tx(token_delta=1_000_000, sol_delta_lamports=-500_000_000,
                  logs=['Program log: addLiquidity'])
    assert parse_transaction(tx, WALLET, sol_price_usd=Decimal(200)).action is Action.LP_INTERACTION


def test_nested_risk_config_loads_dotenv(tmp_path, monkeypatch):
    from asm.config import Settings
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env').write_text('RISK_MAX_DAILY_LOSS_PCT=2.25\nCOPY_MAX_OPEN_POSITIONS=3\n')
    s = Settings()
    assert s.risk.max_daily_loss_pct == Decimal('2.25')
    assert s.copyability.max_open_positions == 3


async def test_treasury_rejects_unapproved_destination(ledger):
    from asm.engine.treasury import Treasury, TreasuryTransferError
    with pytest.raises(TreasuryTransferError, match='allowlist'):
        await Treasury(ledger).transfer(Decimal(10), destination='unapproved')


async def test_unrealized_profit_does_not_trigger_harvest(ledger):
    from asm.engine.harvest import _set_baseline, maybe_harvest
    await _set_baseline(ledger, Decimal(10000))
    await ledger.mark(Decimal(5000))
    result = await maybe_harvest(ledger)
    assert not result.should_harvest


async def test_default_api_token_cannot_authorize_mutations(monkeypatch):
    """The shipped placeholder must never authenticate, by header or by session.

    require_token now also accepts an httpOnly session cookie, so it takes a Request;
    the guarantee under test is unchanged.
    """
    from fastapi import HTTPException

    from asm.services.api.deps import require_token, token_is_valid

    monkeypatch.setattr(settings, "api_token", "dev-token-change-me")

    assert not token_is_valid("dev-token-change-me")

    class _Req:
        def __init__(self):
            self.cookies: dict = {}

    with pytest.raises(HTTPException):
        await require_token(_Req(), "Bearer dev-token-change-me")