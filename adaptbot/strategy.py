"""Early Conviction / Early Cut.

One idea, many parameter sets ("profiles"):

1. Watch the first part of each 15-minute market.  Enter only inside an
   early window, only as a taker, only when the model's probability beats
   the ask by a clear margin after fees, and only after that edge has held
   for a few seconds.
2. Size with fractional Kelly, capped hard.
3. Manage the position every second:
   - early cut: leave as soon as the model says the thesis is broken
     (probability fell `cut_loss` below the entry price, or `trail` below
     its best level since entry);
   - take profit: sell when the bid pays more than the model thinks the
     contract is worth;
   - late exit: near the close, only keep positions the model is confident in.

There is never a resting order and never a "residual" kept by design.

Every profile runs in shadow on every market; the adaptive layer trades the
one with the best risk-adjusted record (see adaptive.py).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields

from .fees import fee_per_contract
from .market import Position, View


@dataclass
class Profile:
    name: str
    entry_start_s: float = 45        # earliest entry (seconds after open)
    entry_end_s: float = 360         # latest entry - "early determination"
    min_edge: float = 0.06           # model prob - ask - fee, per contract
    confirm_s: float = 3             # edge must persist this long
    min_price: float = 0.20
    max_price: float = 0.80
    max_spread: float = 0.06         # need a real bid to be able to cut
    cut_loss: float | None = 0.12    # cut when p_side <= entry_price - cut_loss
    trail: float | None = None       # cut when p_side <= peak_p - trail
    tp_margin: float = 0.03          # sell when bid - fee >= p_side + margin
    exit_before_close_s: float = 90  # late window
    hold_conf: float = 0.85          # inside the late window, hold only if p_side >= this
    max_entries: int = 1
    add_edge: float = 0.10           # edge needed for a 2nd entry (if max_entries > 1)
    kelly_mult: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


# Starting set.  They differ mostly in WHEN they commit and HOW FAST they
# cut, so the shadow results answer "does early determination + early
# cut-off really beat the alternatives?" with data instead of opinion.
DEFAULT_PROFILES = [
    Profile("early_cut", entry_start_s=30, entry_end_s=300, min_edge=0.06, cut_loss=0.10),
    Profile("early_trail", entry_start_s=30, entry_end_s=300, min_edge=0.06, cut_loss=0.15, trail=0.10),
    Profile("early_hold", entry_start_s=30, entry_end_s=300, min_edge=0.06, cut_loss=None,
            tp_margin=0.05, exit_before_close_s=0),
    Profile("sniper", entry_start_s=30, entry_end_s=180, min_edge=0.10, cut_loss=0.08, confirm_s=5),
    Profile("balanced", entry_start_s=60, entry_end_s=480, min_edge=0.07, cut_loss=0.12),
    Profile("patient", entry_start_s=120, entry_end_s=600, min_edge=0.05, cut_loss=0.15),
    Profile("late_favorite", entry_start_s=540, entry_end_s=810, min_edge=0.04, min_price=0.55,
            max_price=0.93, cut_loss=0.12, hold_conf=0.75),
]


@dataclass
class Intent:
    action: str        # "buy" | "sell"
    side: str          # "yes" | "no"
    qty: int
    limit: float       # worst acceptable price, dollars
    reason: str
    p_side: float = 0.0
    edge: float = 0.0


@dataclass
class Sizing:
    bankroll: float
    kelly_fraction: float
    max_contracts: int
    max_dollars_market: float
    fee_rate: float
    max_slippage: float = 0.01
    size_mult: float = 1.0
    fixed_contracts: int | None = None   # warm-up: ignore Kelly, use this


def kelly_contracts(p: float, price: float, s: Sizing, prof: Profile, spent: float) -> int:
    if s.fixed_contracts is not None:
        n = s.fixed_contracts
    else:
        if price <= 0 or price >= 1 or p <= price:
            return 0
        f = (p - price) / (1.0 - price)               # full Kelly for a binary
        stake = f * s.kelly_fraction * prof.kelly_mult * s.size_mult * s.bankroll
        n = int(math.floor(stake / price))
    room = s.max_dollars_market - spent
    n = min(n, s.max_contracts, int(math.floor(room / max(price + s.max_slippage, 0.01))))
    return max(n, 0)


def side_p(p_yes: float, side: str) -> float:
    return p_yes if side == "yes" else 1.0 - p_yes


def decide(view: View, pos: Position, prof: Profile, sizing: Sizing,
           memo: dict, allow_entry: bool = True) -> Intent | None:
    """Return at most one action for this market right now.

    `memo` is per (profile, market) scratch space owned by the caller.
    """
    if view.p_yes is None or view.remaining <= 0:
        return None
    book = view.book
    if pos.qty > 0:
        return _manage(view, pos, prof, sizing, memo)
    if not allow_entry or pos.entries >= prof.max_entries:
        return None
    if not (prof.entry_start_s <= view.elapsed <= prof.entry_end_s):
        memo.pop("edge_since", None)
        return None
    spread = book.spread()
    if spread is None or spread > prof.max_spread:
        memo.pop("edge_since", None)
        return None

    best = None
    for side in ("yes", "no"):
        ask = book.best_ask(side)
        if ask is None or not (prof.min_price <= ask <= prof.max_price):
            continue
        p = side_p(view.p_yes, side)
        edge = p - ask - fee_per_contract(ask, 1, sizing.fee_rate)
        need = prof.min_edge if pos.entries == 0 else prof.add_edge
        if edge >= need and (best is None or edge > best[2]):
            best = (side, ask, edge, p)
    if best is None:
        memo.pop("edge_since", None)
        return None
    side, ask, edge, p = best
    since = memo.get("edge_since")
    if since is None or since[0] != side:
        memo["edge_since"] = (side, view.ts)
        if prof.confirm_s > 0:
            return None
    elif view.ts - since[1] < prof.confirm_s:
        return None

    qty = kelly_contracts(p, ask, sizing, prof, spent=pos.cost)
    qty = min(qty, int(book.ask_qty(side)))
    if qty < 1:
        return None
    # never pay more than the price at which the edge would be gone
    limit = min(ask + sizing.max_slippage, p - prof.min_edge / 2)
    limit = math.floor(limit * 100 + 1e-9) / 100
    if limit < ask:
        limit = ask
    return Intent("buy", side, qty, limit, "entry", p_side=p, edge=edge)


def _manage(view: View, pos: Position, prof: Profile, sizing: Sizing, memo: dict) -> Intent | None:
    side = pos.side
    p = side_p(view.p_yes, side)
    pos.peak_p = max(pos.peak_p, p)
    bid = view.book.best_bid(side)
    if bid is None or bid < 0.02:
        return None                       # nothing to sell into; hold
    qty = int(pos.qty)
    if qty < 1:
        return None
    net = bid - fee_per_contract(bid, qty, sizing.fee_rate)
    limit = max(0.01, round(bid - sizing.max_slippage, 2))

    if net >= p + prof.tp_margin:
        return Intent("sell", side, qty, limit, "take_profit", p_side=p)
    if prof.cut_loss is not None and p <= pos.entry_price - prof.cut_loss:
        return Intent("sell", side, qty, limit, "early_cut", p_side=p)
    if prof.trail is not None and p <= pos.peak_p - prof.trail and p < pos.entry_price:
        return Intent("sell", side, qty, limit, "trail_cut", p_side=p)
    if view.remaining <= prof.exit_before_close_s and p < prof.hold_conf:
        return Intent("sell", side, qty, limit, "late_exit", p_side=p)
    return None

