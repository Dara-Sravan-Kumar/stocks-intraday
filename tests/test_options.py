from __future__ import annotations

from datetime import date, datetime

import pytest

import config
from bot import options as optmod
from bot.clock import IST
from bot.execution import LONG, SHORT
from bot.execution.paper_broker import PaperBroker
from bot.indicators import PrevDayLevels
from bot.options import OptionContract
from bot.risk import Approval, DayState, RiskEngine, Skip
from bot.state import MarketState, SymbolState
from bot.strategies import build_strategies


def make_chain(underlying="NIFTY", expiry=date(2026, 7, 14), step=50,
               center=24200, n=4, lot=75) -> list[OptionContract]:
    out = []
    for i in range(-n, n + 1):
        strike = center + i * step
        for t in ("CE", "PE"):
            out.append(OptionContract(
                symbol=f"NSE:{underlying}26JUL{strike}{t}",
                underlying=underlying, expiry=expiry, strike=strike,
                opt_type=t, lot=lot,
            ))
    return out


def test_nearest_expiry_and_chain_window():
    near = make_chain(expiry=date(2026, 7, 14))
    far = make_chain(expiry=date(2026, 7, 28))
    chain = optmod.build_chain("NIFTY", spot=24_210, session_date=date(2026, 7, 13),
                               contracts=near + far, n_strikes=2)
    assert chain
    assert all(c.expiry == date(2026, 7, 14) for c in chain)
    strikes = sorted({c.strike for c in chain})
    assert strikes == [24100, 24150, 24200, 24250, 24300]


def test_pick_option_atm_and_itm():
    chain = make_chain()
    ce = optmod.pick_option(chain, spot=24_212, opt_type="CE")
    assert ce.strike == 24200
    pe_itm = optmod.pick_option(chain, spot=24_212, opt_type="PE", steps_itm=1)
    assert pe_itm.strike == 24250          # ITM put = higher strike


def test_master_row_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    row = ("101126072835100,BANKNIFTY 28 Jul 26 32500 CE,14,30,0.05,,"
           "0915-1530|1815-1915:,2026-07-10,1785232800,"
           "NSE:BANKNIFTY26JUL32500CE,10,11,35156,BANKNIFTY,26009,32500.0,CE,"
           "101000000026009,None,0,0.0")
    (tmp_path / "fyers-nse-fo.csv").write_text(row, encoding="utf-8")
    contracts = optmod.load_contracts(["BANKNIFTY"])
    assert len(contracts) == 1
    c = contracts[0]
    assert c.symbol == "NSE:BANKNIFTY26JUL32500CE"
    assert c.lot == 30 and c.strike == 32500.0 and c.opt_type == "CE"
    assert c.expiry == date(2026, 7, 28)


def test_options_cost_model():
    from bot import costs
    # buy 75 @ 200 = 15000 ; sell 75 @ 250 = 18750
    c = costs.options_costs(15_000, 18_750)
    assert c["brokerage"] == 40.0
    assert c["stt"] == pytest.approx(18_750 * 0.0015, abs=0.01)
    assert c["stamp"] == pytest.approx(15_000 * 0.00003, abs=0.01)
    assert c["total"] > 0


def option_state(symbol="NSE:NIFTY26JUL24200CE", lot=75, premium=200.0):
    meta = OptionContract(symbol, "NIFTY", date(2026, 7, 14), 24200, "CE", lot)
    st = SymbolState(symbol, PrevDayLevels(), option_meta=meta)
    from bot.bars import Bar
    st.bars_1m.append(Bar(symbol, datetime(2026, 7, 6, 10, 0, tzinfo=IST),
                          premium, premium, premium, premium, 100))
    st.bars_5m.append(Bar(symbol, datetime(2026, 7, 6, 10, 0, tzinfo=IST),
                          premium, premium, premium, premium, 100, 5))
    return st


def approve_option(side=LONG, equity=300_000.0, premium=200.0, stop=130.0,
                   lot=75, margin_used=0.0):
    # Pin risk% so these lot-rounding/margin assertions are independent of the
    # (deliberately small) paper-test profile in config.
    engine = RiskEngine(risk_per_trade_pct=1.0)
    day = DayState(start_equity=equity)
    return engine.approve(
        strategy="opt_orb", symbol="NSE:NIFTY26JUL24200CE",
        entry_price=premium, stop_price=stop,
        sym_state=option_state(lot=lot, premium=premium),
        open_positions=[], equity=equity, margin_used=margin_used,
        day=day, now=datetime(2026, 7, 6, 10, 0, tzinfo=IST), side=side,
    )


def test_option_sizing_rounds_to_lots():
    # risk 1% of 3L = 3000; per-unit risk 70 -> 42 units -> 0 lots of 75 -> skip
    res = approve_option()
    assert isinstance(res, Skip) and "lot" in res.reason
    # wider budget: risk/unit 20 -> 150 units -> 2 lots of 75
    res2 = approve_option(premium=200.0, stop=180.0)
    assert isinstance(res2, Approval)
    assert res2.qty == 150
    assert res2.margin == pytest.approx(200.0 * 150)   # long option = premium


def test_short_option_margin_blocks_small_book():
    # 1.5L book: risk budget allows exactly 1 lot, but short margin (~1.4L)
    # exceeds the 90% margin cap (1.35L) -> blocked at the margin gate
    res = approve_option(side=SHORT, equity=150_000.0, premium=200.0, stop=180.0)
    assert isinstance(res, Skip) and "margin" in res.reason.lower()
    # 3L book can
    res2 = approve_option(side=SHORT, equity=300_000.0, premium=200.0, stop=180.0)
    assert isinstance(res2, Approval)
    assert res2.margin == pytest.approx(
        config.OPTIONS["short_margin_per_lot"]["NIFTY"] *
        (res2.qty // 75))


def test_paper_broker_option_costs_and_margin(mem_db):
    b = PaperBroker(300_000)
    pos = b.open_position("opt_orb", "NSE:NIFTY26JUL24200CE", LONG, 75,
                          200.0, datetime(2026, 7, 6, 10, 0, tzinfo=IST),
                          stop=130.0, target=340.0,
                          margin=200.0 * 75, instrument="OPT")
    assert pos.instrument == "OPT"
    assert pos.margin_used == pytest.approx(15_000)
    slip = 200.0 * config.OPTION_SLIPPAGE_PCT / 100
    assert pos.entry_price == pytest.approx(200.0 + slip)
    trade = b.close_position(pos, 300.0, datetime(2026, 7, 6, 12, 0, tzinfo=IST),
                             "TARGET")
    assert trade.net_pnl > 0
    assert trade.costs > 40   # flat brokerage + premium-based charges


def test_build_strategies_options_mode():
    eq = build_strategies()
    assert not any(s.requires_options for s in eq)
    opt = build_strategies(options_mode=True)
    assert {s.name for s in opt} == {"opt_orb", "opt_trend_day", "opt_straddle"}


def test_straddle_emits_two_short_legs():
    from bot.strategies.opt_straddle import OptStraddle
    strat = OptStraddle()
    strat.on_session_start()

    chain = make_chain()
    contracts = {c.symbol: c for c in chain}
    idx_symbols = ["NIFTY"] + list(contracts)
    market = MarketState(idx_symbols, option_contracts=contracts)

    from bot.bars import Bar
    ts0 = datetime(2026, 7, 6, 9, 25, tzinfo=IST)
    nifty = market.get("NIFTY")
    nifty.bars_5m.append(Bar("NIFTY", ts0, 24_195, 24_220, 24_180, 24_205, 0, 5))
    for sym in (f"NSE:NIFTY26JUL24200CE", f"NSE:NIFTY26JUL24200PE"):
        stt = market.get(sym)
        stt.bars_1m.append(Bar(sym, ts0, 180, 182, 178, 180, 50))

    legs = strat.on_bar_5m(nifty, market, datetime(2026, 7, 6, 9, 30, tzinfo=IST))
    assert legs is not None and len(legs) == 2
    assert all(sig.side == SHORT for sig in legs)
    assert {sig.symbol for sig in legs} == {
        "NSE:NIFTY26JUL24200CE", "NSE:NIFTY26JUL24200PE"}
    assert all(sig.stop == pytest.approx(180 * 1.30) for sig in legs)
    # second call same day: capped
    assert strat.on_bar_5m(nifty, market,
                           datetime(2026, 7, 6, 9, 35, tzinfo=IST)) is None
