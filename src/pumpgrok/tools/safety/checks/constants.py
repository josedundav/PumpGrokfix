"""Safety-check floors that are not caller-tunable.

These stay constants so a score request cannot lower them. Existing
``SafetyThresholds`` values are unchanged.
"""

from __future__ import annotations

from decimal import Decimal

#: SAFE-7 fails when non-pool wallets buy at least this share of supply in
#: the create slot and the next :data:`BUNDLE_WINDOW_FOLLOWING_SLOTS` slots.
#: Do not lower.
BUNDLE_WINDOW_FAIL_SHARE = Decimal("0.25")
BUNDLE_WINDOW_FOLLOWING_SLOTS = 10

#: SAFE-8 fails when the creator sells or transfers its launch buy inside
#: either of these windows. The 4-6 second case sits inside both.
DEV_SELL_WINDOW_SECONDS = 30
DEV_SELL_WINDOW_SLOTS = 75

#: Funder trace looks this many hops back from the creator.
FUNDER_MAX_HOPS = 2
#: SOL received inside this many seconds before create counts as funding.
FUNDER_LOOKBACK_SECONDS = 600
#: Ignore fee-sized lamport moves. 0.01 SOL is the desk minimum trade.
FUNDER_MIN_LAMPORTS = 10_000_000
#: A funder hop that created a token inside this window can be a launch chain.
LAUNCH_CHAIN_LOOKBACK_SECONDS = 86_400
#: "Within seconds" of that hop's own create. Same window as an instant dump.
INSTANT_SELL_SECONDS = 30

#: Caps so one score stays inside the shared RPC retry budget.
MAX_SIGNATURE_PAGES = 4
SIGNATURE_PAGE_LIMIT = 1_000
MAX_WINDOW_TRANSACTIONS = 200
MAX_FUNDERS_PER_HOP = 3
MAX_FUNDER_SIGNATURES = 20
MAX_BALANCE_WALLETS = 64
