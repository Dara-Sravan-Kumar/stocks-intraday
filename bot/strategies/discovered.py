"""DISCOVERED channels: one ExprStrategy instance drives MANY discovered specs.

Each spec is a variant (variant_key == spec.name) so it builds its own ledger
and holds its own one-per-instrument lock, while a single managing class routes
manage()/note_entry() for the whole channel.

  DISCOVERED_EQ  — evaluates each spec's entry_expr on a stock's snapshot and
                   emits an equity Signal on that stock.
  DISCOVERED_OPT — evaluates on the INDEX snapshot (volume-free vocabulary) and
                   buys the ATM CE (LONG spec) or PE (SHORT spec), exactly like
                   opt_orb: index signal, option instrument, premium stop.

Every entry is intraday; the engine squares off at session close.
"""
from __future__ import annotations

import logging
from datetime import datetime

import config
from bot import clock
from bot.discovery.expr import CompiledExpr, compile_expr
from bot.discovery.spec import StrategySpec
from bot.discovery.vocab import build_snapshot, channel_vocab
from bot.execution import LONG, Position
from bot.state import MarketState, SymbolState
from bot.strategies import ExitRequest, Signal, Strategy

log = logging.getLogger(__name__)

INDEX_NAMES = set(config.INDEX_SYMBOLS)


class ExprStrategy(Strategy):
    """Base for the discovered channels. `name` IS the channel key."""

    channel = "DISCOVERED_EQ"

    def __init__(self, specs: list[StrategySpec] | None = None):
        # params are self-contained — discovered channels aren't in STRATEGY_PARAMS
        super().__init__(params={})
        self.specs: list[StrategySpec] = []
        self.compiled: dict[str, CompiledExpr] = {}
        self._picks_today = 0
        for spec in (specs or []):
            self.add_spec(spec)

    name = "DISCOVERED_EQ"

    def add_spec(self, spec: StrategySpec) -> bool:
        try:
            self.compiled[spec.name] = compile_expr(spec.entry_expr,
                                                    channel_vocab(spec.channel))
        except Exception as exc:  # noqa: BLE001 — never let one bad spec break the channel
            log.warning("skip spec %s (%s): %s", spec.name, self.name, exc)
            return False
        self.specs.append(spec)
        return True

    def on_session_start(self) -> None:
        super().on_session_start()
        self._picks_today = 0

    def note_entry(self, symbol: str, side: str) -> None:
        super().note_entry(symbol, side)
        self._picks_today += 1

    def _budget_left(self) -> bool:
        return self._picks_today < config.MAX_DISCOVERED_PICKS_PER_DAY

    def _past_deadline(self, now: datetime) -> bool:
        deadline = clock.parse_hhmm(config.DISCOVERED_ENTRY_DEADLINE,
                                    now.astimezone(clock.IST).date())
        return now >= deadline

    def on_bar_5m(self, st: SymbolState, market: MarketState,
                  now: datetime) -> list[Signal]:
        if not self.specs or self._past_deadline(now) or not self._budget_left():
            return []
        return self._scan(st, market, now)

    def _scan(self, st, market, now) -> list[Signal]:   # overridden per channel
        raise NotImplementedError

    def manage(self, pos: Position, st: SymbolState,
               now: datetime) -> ExitRequest | None:
        return None   # engine handles stop/target/square-off


class DiscoveredEquity(ExprStrategy):
    name = "DISCOVERED_EQ"
    channel = "DISCOVERED_EQ"
    requires_options = False

    def _scan(self, st: SymbolState, market: MarketState, now: datetime) -> list[Signal]:
        # equity underlyings only — never the index or an option leg
        if st.symbol in INDEX_NAMES or st.option_meta is not None:
            return []
        ref = st.bars_5m[-1].close if st.bars_5m else None
        if not ref:
            return []
        env = build_snapshot(st, market).as_env()
        out: list[Signal] = []
        stop_pct = config.DISCOVERED_EQ_STOP_PCT
        for spec in self.specs:
            from bot.discovery.expr import eval_expr
            if not eval_expr(self.compiled[spec.name], env):
                continue
            long_ = spec.side == "LONG"
            stop = ref * (1 - stop_pct / 100.0) if long_ else ref * (1 + stop_pct / 100.0)
            risk = abs(ref - stop)
            target = ref + spec.min_reward_risk * risk if long_ \
                else ref - spec.min_reward_risk * risk
            out.append(Signal(self.name, st.symbol, spec.side, stop=stop,
                              target=target, reason=f"{spec.name}: {spec.entry_expr}",
                              variant_key=spec.name))
        return out


class DiscoveredOptions(ExprStrategy):
    name = "DISCOVERED_OPT"
    channel = "DISCOVERED_OPT"
    requires_options = True

    def _scan(self, st: SymbolState, market: MarketState, now: datetime) -> list[Signal]:
        if st.symbol not in config.OPTIONS["underlyings"]:
            return []
        bar = st.bars_5m[-1] if st.bars_5m else None
        if bar is None:
            return []
        chain = market.option_chains.get(st.symbol, [])
        if not chain:
            return []
        env = build_snapshot(st, market).as_env()
        from bot import options as optmod
        from bot.discovery.expr import eval_expr
        stop_pct = config.DISCOVERED_OPT_PREMIUM_STOP_PCT
        out: list[Signal] = []
        for spec in self.specs:
            if (spec.underlying or "NIFTY") != st.symbol:
                continue
            if not eval_expr(self.compiled[spec.name], env):
                continue
            opt_type = "CE" if spec.side == "LONG" else "PE"   # buy CE up / PE down
            contract = optmod.pick_option(chain, bar.close, opt_type)
            if contract is None:
                continue
            opt_st = market.get(contract.symbol)
            premium = opt_st.last_price if opt_st else None
            if not premium:
                continue
            stop = premium * (1 - stop_pct / 100.0)
            risk = premium - stop
            out.append(Signal(
                self.name, contract.symbol, LONG, stop=stop,
                target=premium + spec.min_reward_risk * risk,
                reason=f"{spec.name}: {st.symbol} {opt_type}",
                variant_key=spec.name))
        return out
