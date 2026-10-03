"""The Retirement FAQ: the rules behind the planner's figures, in one readable place.

This sheet exists because the previous shape did not work. Every screen in the
retirement family used to print its own provenance sentence under the figure it
produced - "Tables: Federal ordinary income tax brackets (IRS Rev. Proc.
2025-32, 2026)  HHS poverty guidelines (HHS ASPE, 2026) ..." - and raised an
amber triangle beside that sentence when one of those tables was a year old.
Both were mistakes:

* The sentences were unreadable. A citation list wedged under a chart is not
  prose; it is a bibliography, and nobody reads a bibliography in the middle of
  a paragraph. The provenance record is worth keeping and worth SHOWING, but in
  one table, once, where a reader who wants it can go and find it.
* The triangle was an abuse of a settled convention. In Mammon an amber triangle
  means MISSING OR CONFLICTING DATA - a split that does not balance, an account
  with no price on file. A table that is one edition behind is neither missing
  nor conflicting; it is simply a year old, which for an indexed table is the
  normal state of affairs for part of every year. Painting the same triangle for
  that teaches the user to ignore the triangle everywhere else, which is the one
  outcome the convention cannot survive.

So provenance lives HERE, in :func:`provenance_rows` and nowhere else in the UI,
and staleness is a sentence in that table's last column rather than a warning on
a number. If you are tempted to print a citation under a figure again, print the
figure's INPUTS instead - which account was assumed to be the current employer's
plan, which claim age is on file. An input the user can correct belongs beside
the figure; the edition history of an IRS table does not.

The prose answers the household's own questions, and it answers them the way the
rest of this feature does: Mammon states the mechanism and cites the rule, then
lays out both columns of a tradeoff without ordering them. Nothing here names an
amount, a year or an account for the reader - the moment a sentence survives the
removal of its numbers as a complete instruction ("roll it over", "delay to 70")
it is counsel and does not belong in this file.

The window is a plain top-level ``QWidget``, never a modal dialog: it is a
reference sheet the user reads WHILE looking at the planner, and an ``exec_()``
here would both block that and hang the headless tests.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from mammon import retirement

WINDOW_TITLE = "Retirement FAQ"

INTRO = (
    "These are the rules the Retirement Planner's figures rest on, in plain "
    "language, and the arguments on both sides of the questions those figures "
    "raise. Mammon computes a number and cites the rule that produced it. It "
    "does not counsel: where a question is a tradeoff, both columns are printed "
    "and neither is marked as the better one. Nothing here names an amount, a "
    "year or an account for you."
)


# ---------------------------------------------------------------------------
# the content
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FaqSection:
    """One question, the rules that answer it, and the two columns if any.

    ``for_label``/``against_label`` are named for the CHOICE, not for a verdict:
    "Rolling the plan into an IRA" and "Leaving it in the plan", never "Pros"
    and "Cons" of the option Mammon would pick.
    """

    key: str
    question: str
    rules: tuple
    for_label: str = ""
    for_points: tuple = ()
    against_label: str = ""
    against_points: tuple = ()


SECTIONS: tuple = (
    FaqSection(
        key="medicare",
        question="When do I have to sign up for Medicare?",
        rules=(
            "Medicare eligibility begins at 65. The Initial Enrollment Period is "
            "seven months wide: the three months before the birthday month, the "
            "birthday month itself, and the three months after.",
            "Part B carries a late-enrollment penalty of 10% of the premium for "
            "each full 12 months a person was eligible but not enrolled. The "
            "surcharge is permanent - it is added to the premium for as long as "
            "Part B is held, not for a catch-up period.",
            "Enrollment can be delayed without that penalty only while covered by "
            "a CURRENT employer's group health plan at an employer of 20 or more "
            "employees. COBRA and retiree coverage do not qualify, however good "
            "they are. An eight-month Special Enrollment Period follows the loss "
            "of qualifying employer coverage.",
            "The income-related monthly adjustment (IRMAA) is read from the tax "
            "return of two years earlier - \"the last taxable year beginning in "
            "the second calendar year preceding the year involved\" (42 U.S.C. "
            "1395r(i)). Its tiers are cliffs, not phase-ins: one dollar over a "
            "threshold moves the entire year's premium up a tier.",
            "Medicare publishes the penalty rules at "
            "https://www.medicare.gov/basics/costs/medicare-costs/avoid-penalties.",
        ),
    ),
    FaqSection(
        key="rmd",
        question="When do required minimum distributions start, and how is one computed?",
        rules=(
            "The applicable age is 73 for people born 1951 through 1959 and 75 for "
            "those born 1960 or later (IRC 401(a)(9)(C)(v), as rewritten by SECURE "
            "2.0). The 1959 cohort is a genuine drafting glitch - the statute as "
            "enacted assigns that birth year both ages; the IRS proposed "
            "regulations of July 2024 read it as 73, and Mammon follows them.",
            "The first distribution is due by April 1 of the year AFTER the "
            "applicable age is reached. Taking it that late puts two distributions "
            "in a single tax year, because the second year's is still due that "
            "December 31.",
            "The amount is the prior December 31 account balance divided by the "
            "Uniform Lifetime Table divisor for the age reached in the "
            "distribution year (26 CFR 1.401(a)(9)-9; IRS Publication 590-B "
            "Appendix B, Table III). At 73 the divisor is 26.5.",
            "A Roth IRA has no lifetime required minimum distribution (IRC "
            "408A(c)(4)). Roth 401(k) accounts lost theirs as well for tax years "
            "beginning after December 31, 2023 (SECURE 2.0 sec. 325).",
            "A missed distribution carries an excise tax of 25% of the shortfall, "
            "reduced to 10% if corrected within the window the IRS allows.",
            "A Roth conversion is taxable but does not satisfy a required minimum "
            "distribution (IRC 408A(d)(3)(E)): the minimum has to come out first.",
        ),
    ),
    FaqSection(
        key="conversion",
        question="What does converting an IRA to a Roth actually do?",
        rules=(
            "A conversion moves money from a traditional IRA to a Roth IRA and "
            "adds the converted amount to that year's ordinary income, taxed at "
            "the marginal rate it reaches. There has been no income limit on "
            "converting since 2010, and a conversion has been irrevocable since "
            "the Tax Cuts and Jobs Act ended recharacterization (IRC 408A).",
            "What it buys is the other end: a qualified Roth distribution is not "
            "taxed at all, once the five-year clock has run and the holder is 59 "
            "and a half or older (IRC 408A(d)(2)). Money left in the traditional "
            "IRA is instead forced out by the required minimum distribution rules "
            "and taxed as ordinary income at whatever bracket that year's total "
            "income reaches.",
            "There are two distinct five-year clocks. One starts January 1 of the "
            "first year any Roth IRA was owned and never restarts; it governs "
            "whether a distribution is qualified. A separate clock runs per "
            "conversion and governs the 10% penalty on withdrawing converted "
            "principal early; it is moot after 59 and a half (IRC 408A(d)(3)(F)).",
            "The order money comes out of a Roth is fixed by statute, not chosen: "
            "contributions first, then conversions oldest first, then earnings "
            "(IRC 408A(d)(4)).",
            "A conversion draws proportionally from the pretax and after-tax "
            "balance of ALL traditional, SEP and SIMPLE IRAs together, tracked on "
            "Form 8606 line 14 (IRC 408(d)(2)). One IRA cannot be isolated as the "
            "after-tax one.",
            "Conversion income counts for two thresholds that arrive later: the "
            "IRMAA tier read two years afterwards, and - before 65 - the "
            "premium-credit cliff at 400% of the federal poverty level (26 U.S.C. "
            "36B(c)(1)(A)). Both are cliffs.",
            "The Roth Conversions screen prints the bracket the projected required "
            "distributions land in beside the marginal bracket a conversion would "
            "be taxed at now. Which of the two is larger is a fact. What follows "
            "from it is a judgment Mammon does not make.",
        ),
    ),
    FaqSection(
        key="ordering",
        question="Which account should money come out of first?",
        rules=(
            "No rule sets the order. The statutes constrain the pieces rather than "
            "the sequence: a required minimum distribution has to come out once "
            "the applicable age is reached, the two Roth five-year clocks have to "
            "have run, and the Roth's own contributions-then-conversions-then-"
            "earnings order is not optional.",
            "What differs is the cost of a dollar. A dollar from a taxable account "
            "is taxed only on its gain, at long-term rates if it was held long "
            "enough, possibly at 0%. A dollar from a tax-deferred account is "
            "ordinary income in full. A qualified Roth dollar costs nothing and "
            "does not appear in income at all.",
            "The ordinary-income total also drives three separate thresholds: how "
            "much of a Social Security benefit becomes taxable (IRC 86), the ACA "
            "premium credit before 65, and the IRMAA tier two years later.",
            "Any popular ordering rule - and there are several - is a preference "
            "dressed as a rule. Mammon prints what each bucket costs and what each "
            "preserves, and leaves the sequence to the household.",
        ),
        for_label="What drawing the tax-deferred account earlier does",
        for_points=(
            "Uses low-bracket headroom in a year that would otherwise waste it; "
            "bracket space does not carry forward to a later year.",
            "Shrinks the balance the Uniform Lifetime divisor will later act on, "
            "so the forced distributions themselves are smaller.",
            "Leaves the Roth compounding untouched behind it, with no lifetime "
            "required distribution to interrupt it.",
            "A Roth passed to an heir is received free of income tax; a "
            "traditional IRA passed to an heir is ordinary income to them as it "
            "comes out.",
        ),
        against_label="What drawing the Roth earlier does",
        against_points=(
            "Keeps ordinary income down in a year a threshold matters: the IRC 86 "
            "provisional-income tiers, the 400%-of-poverty credit cliff, the 0% "
            "long-term capital gain bracket, an IRMAA tier two years out.",
            "Preserves flexibility in a year income is already lumpy from a sale, "
            "a bonus or a one-off event.",
            "Can suit a household whose heirs are in low brackets, or whose "
            "beneficiary is a charity, which pays no income tax on an inherited "
            "traditional IRA at all.",
            "Spends the asset with the best tax treatment first, which is the cost "
            "of everything in the other column.",
        ),
    ),
    FaqSection(
        key="rollover",
        question="Should a 401(k) be rolled into an IRA?",
        rules=(
            "A direct trustee-to-trustee rollover is not a distribution and is not "
            "taxed. What changes is which body of law governs the money "
            "afterwards, and several protections that exist only inside an "
            "employer plan do not survive the move.",
            "Nothing forces the choice and nothing expires: a balance can be left "
            "in a former employer's plan indefinitely if the plan permits it.",
        ),
        for_label="What rolling into an IRA gains",
        for_points=(
            "The whole investment universe instead of the plan's menu.",
            "Expenses often fall, because an index share class bought directly is "
            "frequently cheaper than the same fund's plan share class.",
            "One account rather than several: one beneficiary form to keep "
            "current, one required-distribution computation.",
            "Conversions become mechanically simple - an IRA converts on request, "
            "while a plan converts only if its own document allows it.",
            "Beneficiary arrangements an employer plan will not accommodate become "
            "available.",
        ),
        against_label="What leaving it in the plan keeps",
        against_points=(
            "The age-55 exception: IRC 72(t)(2)(A)(v) waives the 10% early "
            "withdrawal tax on a plan distribution after separation from service "
            "in or after the year age 55 is reached, and IRC 72(t)(3)(A) makes "
            "that exception inapplicable to IRAs. Rolling it over ends it "
            "permanently.",
            "Creditor protection of a different kind: ERISA's anti-alienation rule "
            "(29 U.S.C. 1056(d)(1)) is federal and broad, while an IRA depends on "
            "state exemption law plus, in bankruptcy, 11 U.S.C. 522(b)(3)(C) with "
            "the 522(n) cap of $1,711,975 (effective April 1, 2025). Money that "
            "came from a qualified plan is excluded from that cap.",
            "Net unrealized appreciation: IRC 402(e)(4)(B) lets employer stock "
            "taken in a lump-sum plan distribution get capital-gain treatment on "
            "its appreciation. After a rollover the whole balance is ordinary "
            "income when it comes out.",
            "The still-working exception: IRC 401(a)(9)(C)(i)(II) lets someone who "
            "is not a 5% owner defer distributions from a CURRENT employer's plan "
            "while still employed. An IRA has no such deferral (IRC 408(a)(6)).",
            "Plan loans, which IRC 72(p) permits and an IRA cannot have at all "
            "(IRC 4975 makes it a prohibited transaction).",
            "Isolation from the pro-rata rule: IRC 408(d)(2) aggregates every "
            "traditional, SEP and SIMPLE IRA when a later conversion's taxable "
            "share is computed, and plan money stays outside that pool.",
        ),
    ),
    FaqSection(
        key="consolidation",
        question=(
            "What are the pros and cons of rolling several 401(k)s into one IRA?"
        ),
        rules=(
            "Consolidating is just several direct rollovers done one after "
            "another. A trustee-to-trustee transfer is not a distribution, is "
            "not taxed, and is not rationed: the one-rollover-per-12-months "
            "limit in IRC 408(d)(3)(B) reaches only 60-day rollovers between "
            "IRAs, not direct transfers out of employer plans.",
            "What the count changes is the bookkeeping the law then demands. "
            "Employer plans are measured one at a time - Treas. Reg. "
            "1.401(a)(9)-1(a)(2) says plans \"are not permitted to be "
            "aggregated\" and that \"the distribution of the benefit of the "
            "employee under each plan must separately meet the requirements of "
            "section 401(a)(9)\". IRAs are the opposite: under Treas. Reg. "
            "1.408-8(e)(1)(i) the required amount is \"calculated separately "
            "for each IRA\" and the sum \"may be distributed from any one or "
            "more of the IRAs\". The IRS states the split in its required "
            "minimum distribution FAQs at "
            "https://www.irs.gov/retirement-plans/"
            "retirement-plan-and-ira-required-minimum-distributions-faqs.",
            "The protections that attach to an employer plan rather than to "
            "the money are lost per plan rolled, so the two columns below are "
            "weighed once for each plan, not once for the decision.",
        ),
        for_label="What one IRA instead of several plans changes",
        for_points=(
            "One required minimum distribution to compute, track and satisfy "
            "each year instead of one per plan. The plan-by-plan rule of "
            "Treas. Reg. 1.401(a)(9)-1(a)(2) leaves with the plans, and the "
            "aggregation rule of Treas. Reg. 1.408-8(e)(1)(i) then lets the "
            "whole year's total be taken from whichever IRA is most "
            "convenient to sell from.",
            "Fewer deadlines to miss: every separate plan distribution is its "
            "own chance at a shortfall, and a shortfall carries the excise tax "
            "of IRC 4974.",
            "One beneficiary designation to keep current instead of one per "
            "plan, each governed by its own plan document.",
            "One portfolio that can actually be rebalanced, instead of several "
            "fixed plan menus that cannot hold the same funds or be traded "
            "against each other.",
            "Nobody else can restructure the account. A former employer's plan "
            "can be amended, frozen, merged into a successor plan or have a "
            "small balance forced out under IRC 401(a)(31)(B); an IRA changes "
            "only when its owner changes it.",
        ),
        against_label="What keeping the plans where they are keeps",
        against_points=(
            "Deferral while still working: IRC 401(a)(9)(C)(i)(II) lets "
            "someone who is not a 5% owner postpone distributions from a "
            "CURRENT employer's plan until retirement. It reaches only that "
            "plan, and IRC 408(a)(6) gives an IRA no equivalent - so rolling a "
            "still-active plan in starts distributions that were postponed.",
            "Creditor protection of a stronger kind: ERISA's anti-alienation "
            "rule (29 U.S.C. 1056(d)(1)) is federal and broad, while an IRA "
            "relies on state exemption law, which in most states protects it "
            "less, plus 11 U.S.C. 522(b)(3)(C) in bankruptcy subject to the "
            "cap in 11 U.S.C. 522(n). Money traceable to a qualified plan is "
            "excluded from that cap.",
            "Access a plan can give and an IRA cannot: a loan under IRC 72(p), "
            "which IRC 4975 makes a prohibited transaction for an IRA, and the "
            "separation-from-service exception of IRC 72(t)(2)(A)(v), which "
            "waives the 10% early-withdrawal tax on a plan distribution after "
            "leaving that employer in or after the year age 55 is reached. IRC "
            "72(t)(3)(A) makes that exception inapplicable to IRAs, and "
            "rolling the plan over ends it permanently.",
            "Net unrealized appreciation: IRC 402(e)(4)(B) lets employer stock "
            "taken in a lump-sum plan distribution get capital-gain treatment "
            "on the appreciation. Once the shares are in an IRA the whole "
            "balance is ordinary income when it comes out.",
            "A clean pro-rata position: IRC 408(d)(2) treats every "
            "traditional, SEP and SIMPLE IRA as one account when a "
            "conversion's taxable share is computed, so a large pre-tax IRA "
            "balance makes a later non-deductible contribution converted to "
            "Roth - the backdoor route - taxable in proportion rather than "
            "nearly tax free. Plan money stays outside that pool.",
        ),
    ),
    FaqSection(
        key="trust",
        question="Is a revocable living trust worth having?",
        rules=(
            "A retirement account passes by beneficiary designation, not by trust "
            "and not by will. The SECURE Act's 10-year payout rule (IRC "
            "401(a)(9)(H)) applies to an inherited account either way.",
            "A trust named as the beneficiary of a retirement account must satisfy "
            "the see-through requirements of Treas. Reg. 1.401(a)(9)-4 to be a "
            "designated beneficiary at all. One that fails them can force a faster "
            "payout than naming a person would have.",
        ),
        for_label="What a trust does",
        for_points=(
            "Controls the timing of what a beneficiary receives, instead of an "
            "outright transfer on the date of death.",
            "Can shelter a beneficiary's inheritance from that beneficiary's "
            "creditors or divorce.",
            "Is private: a funded trust is not filed publicly the way a probated "
            "will is.",
            "Handles a minor beneficiary without a court guardianship, and without "
            "the whole balance landing in their hands at the age a custodial "
            "account ends.",
            "Preserves means-tested benefits for a beneficiary with a disability, "
            "which an outright bequest can destroy.",
            "Avoids probate for the assets actually retitled into it - which "
            "matters most where real property sits in more than one state.",
        ),
        against_label="What a trust costs",
        against_points=(
            "It costs money to draft AND to fund, and funding - retitling each "
            "asset - is the step that most often gets skipped, leaving a document "
            "that governs nothing.",
            "A non-grantor trust's income tax brackets are compressed: the top "
            "37% rate is reached at about $16,000 of taxable income (IRC 1(e), "
            "1(j)(2)(E); the dollar figure is inflation-adjusted yearly).",
            "It has to be administered: a trustee, accounting, and a Form 1041 "
            "every year it has income.",
            "It does not change the tax character of retirement money. An "
            "inherited IRA is ordinary income to whoever receives the "
            "distribution, trust or no trust.",
            "Drafted badly, it makes the payout faster rather than slower.",
        ),
    ),
    FaqSection(
        key="will",
        question="Is a will enough on its own?",
        rules=(
            "A will governs only what has no other destination. Beneficiary "
            "designations are nontestamentary - they override the will entirely "
            "(Uniform Probate Code 6-101) - and so does joint titling with right "
            "of survivorship (UPC 6-212).",
            "The Uniform Probate Code is a model act. Roughly 18 states have "
            "adopted it substantially, so the governing citation is always the "
            "enacting state's own code.",
            "Mammon's part in this is narrow and mechanical: it can list every "
            "account in the ledger and ask, one account at a time, whether a "
            "beneficiary is on file and when that was last checked.",
        ),
        for_label="What a will does",
        for_points=(
            "Replaces the state intestacy statute's fixed shares with the "
            "household's own.",
            "Nominates a guardian for minor children - usually the strongest "
            "single argument for a will, because a court will follow the "
            "nomination absent a reason not to.",
            "Nominates an executor, instead of leaving the court to appoint an "
            "administrator who is often bonded at the estate's expense.",
            "Is cheap next to every other estate instrument, and catches the "
            "residue nothing else covers.",
        ),
        against_label="What a will does not do",
        against_points=(
            "Probate is public and takes months.",
            "It does not reach a retirement account, an annuity or a life policy "
            "with a beneficiary on file - a stale designation beats the will.",
            "It does not reach jointly titled property with right of "
            "survivorship.",
        ),
    ),
    FaqSection(
        key="life_insurance",
        question="Is life insurance still needed in retirement?",
        rules=(
            "A death benefit is not income-taxable to the beneficiary (IRC 101(a), "
            "subject to the transfer-for-value rule of IRC 101(a)(2)). That is not "
            "the same as being outside the taxable estate: a policy the decedent "
            "held incidents of ownership in is included (IRC 2042).",
            "This is the one question on this page the forecast can actually "
            "settle. Re-running the spending projection as a survivor scenario "
            "shows whether a shortfall exists in dollars - without naming a face "
            "amount or a product.",
        ),
        for_label="What the policy covers",
        for_points=(
            "Survivor income: a household loses the smaller of two Social Security "
            "benefits at the first death, while its fixed costs do not halve.",
            "Health-coverage risk when the Medicare-eligible spouse dies first, "
            "leaving a younger survivor buying marketplace coverage alone.",
            "Liquidity for an estate whose assets are hard to sell quickly.",
            "A benefit that reaches the beneficiary free of income tax.",
        ),
        against_label="What the policy costs",
        against_points=(
            "Term premiums climb steeply past 60, and a level-term policy bought "
            "earlier tends to expire at exactly the age the argument for it is "
            "strongest.",
            "Permanent policies carry heavy internal costs - cost of insurance, "
            "commissions, and surrender charges loaded into the early years.",
            "The need may simply have ended, which the survivor scenario can "
            "show.",
            "Every premium is a withdrawal that stops compounding, so the cost "
            "works against the plan for as long as the policy is held.",
        ),
    ),
    FaqSection(
        key="working",
        question="What happens to a Social Security benefit if I keep working?",
        rules=(
            "After full retirement age there is no earnings test at all: earnings "
            "of any size withhold nothing (42 U.S.C. 403(f)(3)).",
            "Before full retirement age, $1 of benefit is withheld for every $2 of "
            "earnings above $24,480 (2026). In the year full retirement age is "
            "reached the test loosens to $1 for every $3 above $65,160, counting "
            "only earnings before the birthday month (42 U.S.C. 403(f)(3), "
            "(f)(8)).",
            "Withheld benefits are not forfeited. At full retirement age SSA "
            "recomputes the reduction factor to credit the months withheld, which "
            "raises the monthly benefit for life (20 CFR 404.412). The cost of the "
            "earnings test is timing, not money.",
            "Earnings can also raise the benefit itself: SSA recomputes the primary "
            "insurance amount annually, and a new year that displaces a lower one "
            "in the top-35 indexed record permanently increases it (20 CFR "
            "404.281(e)).",
            "Working income makes more of the benefit taxable. IRC 86 taxes up to "
            "50% and then up to 85% of benefits above its provisional-income "
            "thresholds ($25,000/$34,000 single, $32,000/$44,000 married filing "
            "jointly). Those thresholds have never been indexed - they are the 1983 "
            "and 1993 figures - so the share taxed rises over time by "
            "construction.",
        ),
        for_label="What working alongside the benefit gains",
        for_points=(
            "Withheld months come back as a higher benefit for life at full "
            "retirement age.",
            "A high earnings year can displace a low or zero year in the top 35 "
            "and raise the primary insurance amount permanently.",
            "Earned income covers spending that would otherwise be a withdrawal "
            "from the portfolio.",
            "Employer coverage before 65 can postpone Medicare enrollment without "
            "the Part B penalty.",
        ),
        against_label="What working alongside the benefit costs",
        against_points=(
            "Real cash flow is lost when it is withheld, whatever the later "
            "recomputation does.",
            "More of the benefit becomes taxable under IRC 86, and the frozen "
            "thresholds make that drag grow every year.",
            "The additional income can cross an IRMAA tier and raise Medicare "
            "premiums two years later.",
        ),
    ),
    FaqSection(
        key="shortfall",
        question="What if Social Security cannot pay full benefits?",
        rules=(
            "The 2026 OASDI Trustees Report projects the old-age trust fund's "
            "reserves depleted in the fourth quarter of 2032, after which "
            "continuing tax income covers 78% of scheduled benefits. Combining the "
            "two funds - which itself requires legislation - moves depletion to "
            "the third quarter of 2034 at 83% payable, declining toward about 65% "
            "by 2100. Source: "
            "https://www.ssa.gov/oact/TR/2026/II_A_highlights.html.",
            "That percentage is what payroll tax revenue alone supports under "
            "current law with no change. The Trustees project the trust fund; they "
            "do not project Congress, and every prior shortfall was closed by "
            "legislation.",
            "Mammon can draw the full-benefit track and the reduced-benefit track "
            "side by side. It does not assert which one happens.",
        ),
        for_label="What modeling a reduced benefit gives you",
        for_points=(
            "An official, specific number to plan against rather than a worry.",
            "A cheap lever: the same projection re-run at a lower benefit.",
            "It turns \"does this matter to us\" into a dollar figure, since the "
            "exposure is just the share of household income the benefit is.",
        ),
        against_label="What planning around it costs",
        against_points=(
            "Planning for a cut that does not arrive means underspending the "
            "healthy years for nothing.",
            "The depletion date has moved in both directions from report to "
            "report; it is a projection of a fund, not of a law.",
            "Claiming early locks in a permanent reduction of the primary "
            "insurance amount, and an across-the-board cut would then apply to the "
            "already-reduced benefit. The two do not offset each other.",
        ),
    ),
)


# ---------------------------------------------------------------------------
# the provenance table - the ONE place provenance is rendered
# ---------------------------------------------------------------------------
PROVENANCE_INTRO = (
    "Every published table behind the figures on the retirement screens, with "
    "the edition Mammon holds. A figure is never flagged as stale on screen: an "
    "indexed table is a year behind for part of every year, and that is normal, "
    "not an error. Check the column on the right against the publisher's page "
    "when a figure matters."
)

PROVENANCE_COLUMNS = (
    "Table",
    "Edition",
    "Published by",
    "Where it is published",
    "Last checked",
)


def rule_tables() -> tuple:
    """Every :class:`retirement.RuleTable` in the domain module, by table name.

    Enumerated from the module rather than listed here on purpose: a table added
    to ``retirement.py`` appears in this sheet without anyone remembering to add
    it, and the FAQ test fails if that stops being true.
    """
    found: list = []
    seen: set = set()
    for name in dir(retirement):
        if name.startswith("__"):
            continue
        obj = getattr(retirement, name)
        if isinstance(obj, retirement.RuleTable) and id(obj) not in seen:
            seen.add(id(obj))
            found.append((name, obj))
    found.sort(key=lambda pair: pair[1].provenance.table.lower())
    return tuple(found)


def checked_note(record, today: Optional[_dt.date] = None) -> str:
    """The 'last checked / may be out of date' cell, as a plain sentence.

    Derived from the Provenance record, not from a warning state: a stable table
    says so and stops, an indexed one says when it was checked and when its
    replacement is normally out.
    """
    today = today or _dt.date.today()
    if record.volatility == "stable":
        return ("Fixed by statute or regulation - it does not change from year "
                "to year.")
    checked = (f"Last checked {record.checked_on}."
               if record.checked_on else "No check on record.")
    if record.is_stale(today):
        return (f"{checked} The {record.effective_year + 1} edition is normally "
                f"out by {record.republished_by} and this one may be out of "
                f"date.")
    return (f"{checked} Reissued yearly, normally by {record.republished_by} of "
            f"the following year.")


def provenance_rows(today: Optional[_dt.date] = None) -> list:
    """One dict per rule table: what the FAQ's last table prints."""
    today = today or _dt.date.today()
    rows = []
    for name, table in rule_tables():
        record = table.provenance
        rows.append({
            "attribute": name,
            "table": record.table,
            "effective_year": record.effective_year,
            "publisher": record.publisher,
            "source": record.source,
            "checked": checked_note(record, today),
            "note": record.note,
        })
    return rows


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _bullets(points) -> str:
    return "".join(f"<li>{_esc(p)}</li>" for p in points)


def section_html(section: FaqSection) -> str:
    parts = [f'<h2><a name="{_esc(section.key)}"></a>{_esc(section.question)}</h2>']
    for paragraph in section.rules:
        parts.append(f"<p>{_esc(paragraph)}</p>")
    if section.for_points:
        parts.append(f"<p><b>{_esc(section.for_label)}</b></p>")
        parts.append(f"<ul>{_bullets(section.for_points)}</ul>")
    if section.against_points:
        parts.append(f"<p><b>{_esc(section.against_label)}</b></p>")
        parts.append(f"<ul>{_bullets(section.against_points)}</ul>")
    return "".join(parts)


def provenance_html(today: Optional[_dt.date] = None) -> str:
    head = "".join(f"<th align='left'>{_esc(c)}</th>" for c in PROVENANCE_COLUMNS)
    body = []
    for row in provenance_rows(today):
        source = _esc(row["source"])
        link = (f'<a href="{source}">{source}</a>'
                if row["source"].startswith("http") else source)
        table_cell = _esc(row["table"])
        if row["note"]:
            table_cell += f"<br><i>{_esc(row['note'])}</i>"
        body.append(
            "<tr>"
            f"<td valign='top'>{table_cell}</td>"
            f"<td valign='top'>{_esc(row['effective_year'])}</td>"
            f"<td valign='top'>{_esc(row['publisher'])}</td>"
            f"<td valign='top'>{link}</td>"
            f"<td valign='top'>{_esc(row['checked'])}</td>"
            "</tr>"
        )
    return (
        '<h2><a name="provenance"></a>Where these figures come from</h2>'
        f"<p>{_esc(PROVENANCE_INTRO)}</p>"
        "<table border='1' cellpadding='5' cellspacing='0' width='100%'>"
        f"<tr>{head}</tr>{''.join(body)}</table>"
    )


def faq_html(today: Optional[_dt.date] = None) -> str:
    """The whole sheet as Qt rich text."""
    parts = [f"<h1>{_esc(WINDOW_TITLE)}</h1>", f"<p>{_esc(INTRO)}</p>"]
    contents = "".join(
        f'<li><a href="#{_esc(s.key)}">{_esc(s.question)}</a></li>'
        for s in SECTIONS
    )
    parts.append(
        f'<ul>{contents}<li><a href="#provenance">Where these figures come '
        f"from</a></li></ul>"
    )
    for section in SECTIONS:
        parts.append(section_html(section))
    parts.append(provenance_html(today))
    return "".join(parts)


class RetirementFaqWindow(QWidget):
    """The FAQ sheet as its own top-level window.

    Deliberately NOT a QDialog: nothing here is a decision, the user reads it
    beside the planner, and a modal would block the screen it explains (and hang
    a headless test, per CLAUDE.md's headless-modal hazard).
    """

    def __init__(self, parent=None, *, today: Optional[_dt.date] = None):
        super().__init__(parent, Qt.Window)
        self._today = today or _dt.date.today()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(780, 660)
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.view = QTextBrowser(self)
        self.view.setOpenExternalLinks(True)
        self.view.setHtml(faq_html(self._today))
        layout.addWidget(self.view, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        row.addWidget(self.close_button)
        layout.addLayout(row)

    def text(self) -> str:
        """The rendered sheet as plain text - what a reader actually sees."""
        return self.view.toPlainText()
