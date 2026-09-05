"""A synthetic demo ledger: what `--demo` seeds and the README screenshots show.

Why this is a module and not a handful of rows in ``app.sample_data``: the
parts of Mammon worth looking at only appear when there is enough history to
look at. The account bar's group subtotals need an account in each group; the
running-balance column needs a balance that moves; the Financial Calendar --
the window's landing page -- is BLANK without scheduled definitions; and
holdings read as unpriced without both lots and price history. A six-row sample
exercises the schema and shows none of that.

**Shape borrowed, content invented.** The cadence here is calibrated against
aggregate statistics from a real three-decade ledger -- transactions per month,
the ratio of transfers to ordinary rows, how often a row is cleared or carries a
memo or a check number, how heavily splits are used, and the relative weight of
each spending category. None of that ledger's *rows* were copied and none of its
names survive: profiling it turned up real people's names used as category
names, which is exactly the kind of thing that walks into a public repository
unnoticed. Every payee, institution, category and security below is invented,
and amounts are scaled to the present day rather than carried across.

Amounts are drawn from a fixed seed so regenerating a screenshot does not
reshuffle every row, while dates are anchored to *today* so the calendar and the
reminders are populated whenever it runs.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from mammon import asset_values, investments, ledger, scheduled

SEED = 20260904

# Calibration targets from the reference profile (see the module docstring).
CLEARED_RATE = 0.97
MEMO_RATE = 0.20
NUM_RATE = 0.18

# Invented merchants, grouped the way the reference ledger's categories were
# weighted: groceries and fuel dominate the row count, gifts and travel are rare
# but large. Payee variety per category is deliberate -- one payee per category
# would make the learned-rename engine look trivial.
PAYEES = {
    "Groceries": ["Northwind Market", "Greenfield Grocery", "Harbor Foods",
                  "Cedar Lane Produce", "Kettle & Crumb"],
    "Auto:Fuel": ["Summit Fuel", "Crossroads Gas", "Halfway Station"],
    "Household": ["Lantern Hardware", "Tidewater Supply", "Birch & Board",
                  "Maple Mercantile"],
    "Dining": ["Alder Cafe", "The Copper Pot", "Rivet Coffee",
               "Ninth Street Deli", "Saffron House"],
    "Gifts": ["Fernwood Books", "Marigold Florist", "Kestrel Outfitters"],
    "Clothing": ["Willowbrook Apparel", "Ridge & Root", "Trellis Clothing"],
    "Books": ["Fernwood Books", "Paper Lantern Shop"],
    "Recreation": ["Orpheum Cinema", "Blue Heron Theatre", "Foxglove Records",
                   "Larkspur Lanes"],
    "Health:Doctor": ["Pinecrest Family Practice", "Anytown Dental"],
    "Health:Pharmacy": ["Pinecrest Pharmacy", "Corner Drug"],
    "Home:Service": ["Ironwood Home Repair", "Clearview Window Co.",
                     "Thistle Lawn Care"],
    "Office Supplies": ["Quill & Ledger", "Anytown Office Supply"],
    "Travel": ["Meridian Airlines", "Wayfarer Inn", "Cobblestone Motel"],
    "Misc": ["Anytown Market Days", "Sundry Shop"],
}

# (category, rows per month, low, high) -- amounts in cents, scaled to
# present-day prices rather than the reference era's.
SPEND = [
    ("Groceries",        9.0,  22_00, 210_00),
    ("Auto:Fuel",        4.5,  28_00,  92_00),
    ("Household",        4.0,  12_00, 180_00),
    ("Dining",           2.4,   9_00,  74_00),
    ("Gifts",            1.7,  15_00, 260_00),
    ("Clothing",         1.2,  18_00, 165_00),
    ("Books",            0.9,   8_00,  62_00),
    ("Recreation",       0.9,  11_00,  88_00),
    ("Health:Doctor",    0.6,  25_00, 240_00),
    ("Health:Pharmacy",  0.6,   8_00,  74_00),
    ("Home:Service",     0.5,  65_00, 640_00),
    ("Office Supplies",  0.5,   9_00,  85_00),
    ("Travel",           0.5,  85_00, 720_00),
    ("Misc",             0.9,   6_00,  55_00),
]

# Fixed monthly bills: (day, category, payee, low, high, on_card)
BILLS = [
    (1,  "Bills:Gas & Electric", "Cascade Power & Light",  96_00, 232_00, False),
    (1,  "Bills:Internet",       "Riverbend Internet",     74_99,  74_99, False),
    (4,  "Bills:Water",          "Anytown Water District", 38_00,  96_00, False),
    (12, "Auto:Insurance",       "Brightline Insurance",   68_00,  68_00, False),
    (18, "Bills:Telephone",      "Clearwater Mobile",      46_00,  72_00, False),
    (24, "Charity",              "Anytown Food Bank",     120_00, 120_00, False),
]

# A few categories carry a tag. Tags are a column in the register, and one that
# is empty in every row shows the user a feature that looks broken rather than
# unused -- but tagging everything is not what anybody does either.
TAGS = {
    "Travel": "vacation",
    "Charity": "deductible",
    "Home:Service": "house",
    "Health:Doctor": "medical",
    "Health:Pharmacy": "medical",
    "Office Supplies": "business",
}

FUNDS = [("BMKT", "Broad Market Index Fund", 82.40, 0.9),
         ("GTEC", "Global Technology Fund", 141.10, 1.6),
         ("CBND", "Core Bond Fund", 51.75, 0.2)]


def _iso(d: date) -> str:
    return d.isoformat()


def _months_back(anchor: date, months: int) -> date:
    """``anchor`` shifted back whole months, clamped to a day every month has."""
    y, m = anchor.year, anchor.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(anchor.day, 28))


def build(conn, today: date | None = None, months: int = 30) -> dict:
    """Seed a complete demo ledger. Returns the account ids by role.

    Safe only on an empty database -- the caller decides that (see
    ``app._ensure_seed``), because refusing here would make this useless to a
    test that seeds a fixture deliberately.
    """
    rng = random.Random(SEED)
    today = today or date.today()
    start = _months_back(today, months)

    # ---- accounts, one in each account-bar group -------------------------
    chk = ledger.create_account(conn, "Everyday Checking", "checking",
                                opening_balance=2_412_00, opening_date=_iso(start),
                                institution="Anytown Credit Union")
    sav = ledger.create_account(conn, "Emergency Savings", "savings",
                                opening_balance=9_150_00, opening_date=_iso(start),
                                institution="Anytown Credit Union")
    card = ledger.create_account(conn, "Rewards Card", "credit",
                                 opening_balance=0, opening_date=_iso(start))
    broker = ledger.create_account(conn, "Brokerage", "investment",
                                   opening_balance=0, opening_date=_iso(start))
    plan = ledger.create_account(conn, "Retirement 401(k)", "investment",
                                 opening_balance=0, opening_date=_iso(start))
    house = ledger.create_account(conn, "Maple Street House", "asset",
                                  opening_balance=305_000_00,
                                  opening_date=_iso(start))
    mortgage = ledger.create_account(conn, "Home Mortgage", "liability",
                                     opening_balance=-198_400_00,
                                     opening_date=_iso(start))

    names = ["Income:Salary", "Income:Interest", "Income:Reimbursement",
             "Bank Charges", "Cash"]
    names += [c for c, *_ in SPEND] + [b[1] for b in BILLS]
    cats = {n: ledger.resolve_category(conn, n) for n in dict.fromkeys(names)}

    def _extras(on_card: bool) -> dict:
        """Cleared / check-number flags at the reference ledger's rates. A
        register where every row is identical in these columns looks generated,
        because a real one never is."""
        out: dict = {"cleared": 1 if rng.random() < CLEARED_RATE else 0}
        if not on_card and rng.random() < NUM_RATE:
            out["num"] = str(rng.randrange(1200, 1900))
        return out

    # ---- security prices, built BEFORE the walk so purchases price off them --
    # Fictional tickers: a real one beside an invented price would read as a
    # claim about a real security.
    series: dict[str, list] = {}
    rows: list[tuple] = []
    for sym, _desc, base, drift in FUNDS:
        px, d, pts = base, start, []
        while d <= today:                      # a monthly close per fund
            px = max(px + rng.uniform(-drift, drift * 1.35), 5.0)
            pts.append((d, px))
            rows.append((sym, _iso(d), f"{px:.2f}", "demo"))
            d += timedelta(days=30)
        series[sym] = pts
    investments.record_prices(conn, rows)

    def _price_on(sym: str, when: date) -> float:
        """The last recorded close at or before ``when``. Buying at the series
        price keeps cost basis and market value telling one story; an
        independent draw shows gains the price history cannot explain."""
        prior = [px for d, px in series[sym] if d <= when] or [series[sym][0][1]]
        return prior[-1]

    def _invest_cash(acct: int, sym: str, when: date, cap: int) -> None:
        """Buy with cash the account actually holds, never more.

        Sizing a purchase independently of the funding transfers is what put a
        swinging negative in the Cash Bal column: a quarterly buy spent three
        months of contributions two months in. Reading the balance back caps
        every purchase at what has arrived."""
        on_hand = investments.account_valuation(
            conn, acct, as_of=_iso(when)).cash
        dollars = min(cap, (on_hand // 100) * 100)
        if dollars < 100:
            return
        px = _price_on(sym, when)
        qty = round(dollars / (px * 100), 3)
        amount = int(round(qty * px * 100))
        if amount > on_hand:                   # rounding may nudge it over
            qty = round((on_hand // 100 * 100) / (px * 100), 3)
            amount = int(round(qty * px * 100))
        investments.record_investment(
            conn, acct, _iso(when), "Buy", symbol=sym, quantity=str(qty),
            price=f"{px:.2f}", amount=-amount)

    # ---- the daily walk --------------------------------------------------
    day, payday, turn = start, start, 0
    while day <= today:
        iso = _iso(day)

        if day >= payday:
            # A paycheck is a SPLIT: gross salary less the 401(k) deferral,
            # which posts into the retirement account as a transfer leg. One row
            # exercising splits, transfers and categories at once -- and it is
            # how a real paycheck is actually entered.
            gross = 3_500_00 + rng.randrange(-40_00, 41_00, 5_00)
            deferral = 350_00
            tid = ledger.add_transaction(conn, chk, iso, gross - deferral,
                                         payee="Ridgeline Systems", num="DEP",
                                         cleared=1)
            ledger.set_splits(conn, tid, [
                {"category_id": cats["Income:Salary"], "amount": gross,
                 "memo": "salary"},
                {"transfer_account_id": plan, "amount": -deferral,
                 "memo": "401(k) deferral"},
            ])
            payday = day + timedelta(days=14)

        for bday, cat, payee, lo, hi, on_card in BILLS:
            if day.day == bday:
                amt = -(lo if lo == hi else rng.randrange(lo, hi))
                bill_extra = _extras(on_card)
                if cat in TAGS:
                    bill_extra["tag"] = TAGS[cat]
                ledger.add_transaction(conn, card if on_card else chk, iso, amt,
                                       payee=payee, category_id=cats[cat],
                                       **bill_extra)

        if day.day == 1:
            ledger.create_transfer(conn, chk, sav, iso, 400_00,
                                   memo="monthly savings", cleared=1)
            ledger.create_transfer(conn, chk, broker, iso, 500_00,
                                   memo="brokerage funding", cleared=1)
            ledger.create_transfer(conn, chk, mortgage, iso, 1_486_22,
                                   memo="mortgage payment", cleared=1)
        if day.day == 28:
            # Interest was the single most common income row in the reference
            # ledger; a savings account that never pays any looks inert.
            ledger.add_transaction(conn, sav, iso, rng.randrange(4_00, 22_00),
                                   payee="Anytown Credit Union",
                                   category_id=cats["Income:Interest"], cleared=1)

        # Discretionary spending, weighted to the reference row count.
        for cat, per_month, lo, hi in SPEND:
            if rng.random() < per_month / 24.0:
                on_card = rng.random() < 0.55
                payee = rng.choice(PAYEES.get(cat, ["Sundry Shop"]))
                amt = -rng.randrange(lo, hi)
                extra = _extras(on_card)
                if rng.random() < MEMO_RATE:
                    extra["memo"] = rng.choice(
                        ["weekly shop", "reimbursable", "gift", "on sale",
                         "annual", "replacement", "for the trip"])
                if cat in TAGS and rng.random() < 0.8:
                    extra["tag"] = TAGS[cat]
                tid = ledger.add_transaction(
                    conn, card if on_card else chk, iso, amt, payee=payee,
                    category_id=cats[cat], **extra)
                # A warehouse run is really two categories; splitting it is what
                # the reference ledger did thousands of times.
                if cat == "Groceries" and amt < -120_00 and rng.random() < 0.5:
                    share = int(amt * 0.3)
                    ledger.set_splits(conn, tid, [
                        {"category_id": cats["Groceries"], "amount": amt - share,
                         "memo": "food"},
                        {"category_id": cats["Household"], "amount": share,
                         "memo": "household"},
                    ])

        if rng.random() < 0.06:
            ledger.add_transaction(conn, chk, iso, -rng.randrange(40_00, 200_00),
                                   payee="ATM Withdrawal",
                                   category_id=cats["Cash"], cleared=1)
        if day.day == 6 and rng.random() < 0.5:
            ledger.add_transaction(conn, chk, iso, -rng.randrange(2_00, 12_00),
                                   payee="Anytown Credit Union",
                                   category_id=cats["Bank Charges"], cleared=1)
        if rng.random() < 0.02:
            ledger.add_transaction(conn, chk, iso, rng.randrange(60_00, 900_00),
                                   payee="Ridgeline Systems",
                                   category_id=cats["Income:Reimbursement"],
                                   memo="expense report", cleared=1)

        # Invest what the transfers brought in: brokerage a few days after the
        # 1st, the plan late in the month once its paycheck deferrals have landed.
        if day.day == 5:
            _invest_cash(broker, FUNDS[turn % len(FUNDS)][0], day, 500_00)
            turn += 1
        if day.day == 26:
            _invest_cash(plan, "BMKT", day, 800_00)

        if day.day == 15:                     # pay the card, as a transfer
            owed = -ledger.account_balance(conn, card)
            if owed > 0:
                ledger.create_transfer(conn, chk, card, iso, owed,
                                       memo="card payment", cleared=1)
        day += timedelta(days=1)

    for acct in (broker, plan):
        investments.rebuild_holdings(conn, acct)

    # ---- the house: a valuation series distinct from its cost basis ------
    asset_values.set_address(conn, house, "148 Maple Street, Anytown")
    for back, value in ((24, 318_000_00), (12, 341_500_00), (6, 352_000_00),
                        (0, 358_750_00)):
        asset_values.set_value(conn, house, _iso(_months_back(today, back)),
                               value, source="demo")
    asset_values.set_lien(conn, mortgage, house)

    # ---- scheduled definitions: what makes the calendar a calendar -------
    for days, payee, amount, freq, cat, to_acct in (
            (3,  "Cascade Power & Light", -148_00, "monthly",
             "Bills:Gas & Electric", None),
            (6,  "Riverbend Internet",     -74_99, "monthly",
             "Bills:Internet", None),
            (8,  "Ridgeline Systems",    2_700_00, "biweekly",
             "Income:Salary", None),
            (11, "Brightline Insurance",   -68_00, "monthly",
             "Auto:Insurance", None),
            (13, "Anytown Water District", -62_00, "monthly",
             "Bills:Water", None),
            (14, None,                  -1_486_22, "monthly", None, mortgage),
            (17, None,                    -400_00, "monthly", None, sav)):
        scheduled.add_scheduled(
            conn, chk, payee=payee, amount=amount, frequency=freq,
            next_date=_iso(today + timedelta(days=days)),
            category_id=cats[cat] if cat else None,
            transfer_account_id=to_acct,
            memo=("mortgage payment" if to_acct == mortgage
                  else "monthly savings" if to_acct else None))

    return {"checking": chk, "savings": sav, "card": card, "brokerage": broker,
            "plan": plan, "house": house, "mortgage": mortgage}
