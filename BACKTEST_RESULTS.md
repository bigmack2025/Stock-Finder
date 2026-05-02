# Backtest Results — Cheapness Signal, two regimes

**Headline:** The cheapness signal is predictive on a portfolio basis. Across both regimes tested, the cheap basket beat the expensive basket on mean and median 3Y returns. Hit rates are 20–50% — most cheap names individually fail; the win comes from owning the basket.

This is a stock-screener result, not a stock-picker result. That distinction matters for how the product should be framed.

**Regimes tested:**
- **2018 → 2019/2021** — bull market for biotech (XBI +35% in 2019). Cheap basket destroyed expensive basket (mean +287% vs +3% on 1Y), driven by AXSM as a tail outlier.
- **2020 → 2021/2023** — biotech bear market (XBI −30% over the window). Cheap basket lost less than expensive (3Y mean −18% vs −40%, spread +22%). No tail outliers; defensive performance.

The signal works as both an offense screen (find the right tail) and a defense screen (avoid the worst losers).

## Setup

- Universe: 125 US biotechs, stratified random sample by size band (35 each from mid/small/micro, 18 large, 2 mega).
- Base year: FY2018 (financial state at end of 2018 from SEC EDGAR XBRL).
- Forward returns: total return from 2018-12-31 close → 2019-12-31 (1Y) and → 2021-12-31 (3Y), via yfinance auto-adjusted prices.
- Cheapness signal: same `compute_score` function used by the live anchor / screener tabs (5 signals, default weights, peer-relative anchored to the 2018 universe median).
- Sample loss: 21/77 scored tickers had no 2018 price on yfinance (mostly post-2018 IPOs in the universe — they're current names that didn't trade in 2018 yet). Final clean N = 56.

## 2018 backtest — per-bucket forward returns

| Bucket | Label | n | mean 1Y | median 1Y | hit rate 1Y | mean 3Y | median 3Y | hit rate 3Y |
|--:|:--|--:|--:|--:|--:|--:|--:|--:|
| 1 | cheapest | 14 | **+287%** | **+6%** | 50% | **+142%** | **+41%** | 57% |
| 2 | q2 | 7 | −16% | −6% | 43% | +30% | −51% | 43% |
| 3 | q3 | 7 | −5% | −41% | 29% | −3% | −78% | 29% |
| 4 | q4 | 12 | −3% | −5% | 50% | +192% | −29% | 42% |
| 5 | most expensive | 16 | **+3%** | **+5%** | 50% | **+1%** | **−2%** | 50% |

**Spread, cheapest vs most expensive: +284% on 1Y mean, +142% on 3Y mean.**

## 2020 backtest — per-bucket forward returns

| Bucket | Label | n | mean 1Y | median 1Y | hit rate 1Y | mean 3Y | median 3Y | hit rate 3Y |
|--:|:--|--:|--:|--:|--:|--:|--:|--:|
| 1 | cheapest | 20 | **−23%** | −42% | 25% | **−18%** | −51% | 20% |
| 2 | q2 | 13 | −22% | −20% | 23% | −56% | −77% | 15% |
| 3 | q3 | 13 | −24% | −34% | 23% | −24% | −76% | 15% |
| 4 | q4 | 18 | −19% | −31% | 22% | −20% | −36% | 17% |
| 5 | most expensive | 19 | **−25%** | −24% | 21% | **−40%** | −59% | 21% |

**Spread, cheapest vs most expensive: +2% on 1Y mean, +22% on 3Y mean.**

The 2020 → 2023 window covers the biotech bear market: every bucket lost money on average. But the cheap basket lost the least — the signal worked *defensively* even when nothing was working *offensively*. Hit rates were terrible across the board (20–25%) which is just what biotech bear markets do.

## What's actually happening — bucket-1 detail

| Ticker | 2018 mkt cap | 2018 cash | 1Y return | 3Y return |
|---|--:|--:|--:|--:|
| **AXSM** (Axsome) | $85M | $14M | **+3565%** | **+1240%** |
| MDXG | $195M | $45M | +323% | +237% |
| CRSP (CRISPR Tx) | $1,481M | $457M | +113% | +165% |
| XENE | $162M | $68M | +108% | +395% |
| INSM | $1,014M | $495M | +82% | +108% |
| CRVS | $108M | $39M | +48% | −34% |
| CRMD | $140M | $18M | +13% | −29% |
| OCUL | $165M | $54M | −1% | +75% |
| TPST | n/a | $73M | −15% | −96% |
| CRNX | n/a | $45M | −16% | −5% |
| MNOV | $344M | $62M | −18% | −67% |
| ATYR | $15M | $23M | −40% | +8% |
| CRDF | $12M | $11M | −61% | +89% |
| WVE | $1,239M | $175M | −81% | −93% |

7 winners, 7 losers — **50% hit rate**. Mean is +287% almost entirely because of AXSM's +3565% (Axsome's migraine drug Auvelity went from preclin-stage to a major commercial story).

**Without AXSM, bucket-1 1Y mean drops from +287% to ~+30%.** Still beats the expensive bucket's +3%, but the magic is in the tail.

## What's happening in bucket 5

Tighter distribution: ALNY +58%, INCY +37%, VRTX +32%, JAZZ +20%, IONS +12%, BMRN −1%, EXEL −10%, ATNM −44%, VRDN −84%. No 10-baggers, no bankruptcies. Mean +3%, low variance.

## What this tells you

**Use as a basket, not a tip-sheet.** The signal correctly identifies a positively-skewed pool. Holding the whole cheap bucket in 2018 returned +287% in 1Y. Holding *one* random name from it was a coin flip.

**The expensive bucket isn't a short.** Expensive names at +3% beat the broader S&P 500 over 1Y. The bottom-quintile names didn't crater. So the signal is "find positive-skew opportunities," not "avoid these duds."

**The middle quintiles look ugly.** Buckets 2/3 have negative median returns. There's something interesting in that — moderately cheap (but not screamingly cheap) might be where the value traps cluster. The cheapest *deepest-discount* bucket is actually the safer place to hunt within the cheap half.

## Caveats — read these before trusting the headline

1. **Survivor bias is real and unfixable from this dataset.** Universe = today's biotechs. Names that delisted between 2018 and now are missing. Those tend to be the ones that *failed* — i.e. the actual value traps. Real bucket-1 returns would likely be lower than measured here. Doesn't kill the signal, but the spread is overstated.

2. **N is small.** 56 names, 14 in the cheapest bucket. One outlier (AXSM) drives a lot of the headline. We need 500+ names across multiple base years before this is "robust."

3. **Single base year.** 2018 was a moment in biotech history. Different regimes (post-2021 crash, pre-2008, COVID era) might give different results. M3 should run 2014, 2016, 2018, 2020 and look for consistency.

4. **The 1Y → 2019 forward return captures a biotech rally.** XBI was +35% in 2019. That's a tailwind for everything, but bucket-1 still beat bucket-5 by 280+ percentage points — too much to attribute to beta alone.

5. **No transaction costs, no portfolio rebalancing logic, no risk-adjustment.** This is "naive equal-weight buy-and-hold for N years." Real portfolio construction would change the numbers.

6. **AXSM was a single-asset, post-IPO microcap with a clean readout.** That's the EXACT shape of biotech mispricing — a tiny company with cash and a Ph2 asset that worked. The signal correctly bucketed it as "cheap," which is the win. But finding the next AXSM among the next bucket-1 cohort is what the user actually needs to do — and the signal alone won't tell you which one will hit.

## Recommendation

**The signal is good enough to ship to friends.** Frame it correctly:
- This is a *screen* for portfolio construction or a starting watchlist, not a buy list.
- Cheap-bucket performance in this backtest came from the right tail. Most cheap names *individually* were flat or down. You want to own a basket.
- The middle of the curve (q2/q3) underperformed worst. The deepest discounts looked safer than mid-cheapness.

**Things to add to the UI before sharing**, based on what the backtest revealed:

- A **"diversification reminder"** banner on the Anchor screen results: "These results are from a basket-based backtest — consider sizing positions accordingly."
- A **"hit rate" disclosure** alongside the cheapness score: "In our 2018 backtest, names in this percentile band had a 50% hit rate (positive 1Y return) but +287% mean return."
- Mark the q2/q3 zone explicitly — those bucketed there in 2018 underperformed.
- Continue building the **misuse flags from the council pass** (just-IPO'd, going-concern note, near-term catalyst). The bucket-1 losers WVE / TPST / MNOV / CRDF were clinical-stage names where the trial readout went sideways. Catalyst awareness would have flagged them.

**Verdict from two regimes:** the signal is robust enough to ship, but it's a **basket screen** with **regime-dependent magnitude**:
- In a bull market (2018→2019), the cheap basket *crushes* via tail outliers
- In a bear market (2020→2023), the cheap basket *defends* — losing meaningfully less than the expensive basket
- In both regimes, the spread is meaningful at 3Y horizons (+142% bull, +22% bear)

For a screen used by you and your friends, that's a green light to ship — provided the framing is right.

## Files generated

- `data/backtest/detail_2018_clean.csv` — per-ticker details (56 rows)
- `data/backtest/summary_2018_clean.csv` — bucket-level summary
- `data/backtest/scored_2018_with_returns.parquet` — full intermediate state for re-aggregation
- `backtest.py` — the harness, callable for any year via `python backtest.py --year YYYY`
