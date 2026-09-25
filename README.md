# finance-dashboards-powerbi

Power BI dashboards over the data produced by my other three repositories:
[market-data-warehouse](https://github.com/davrod-dev/market-data-warehouse),
[equity-fundamentals-sql](https://github.com/davrod-dev/equity-fundamentals-sql) and
[portfolio-risk-report](https://github.com/davrod-dev/portfolio-risk-report).
The SQL and Python live there. This repo is the reporting layer on top: a
reproducible dataset export, a star-schema model, and three report pages.

Status: dataset exported, dashboards in progress.

## Dataset

`scripts/export_dataset.py` reads the DuckDB databases and cached FRED
series from the sibling repos and writes each table as Parquet (primary)
and CSV (fallback) into `data/`. Power BI Desktop reads both natively.

| table | rows | grain | from |
|---|---:|---|---|
| `fundamentals_annual` | 16,340 | one row per filer per fiscal year end, 2020 to 2025, with a `sector` label from SIC major group (three filers changed year end, so key on `fiscal_year_end`, not `fiscal_year`) | equity-fundamentals-sql `core.annual` |
| `dim_company` | 5,505 | one row per filer: name, SIC, sector, years reported | derived |
| `piotroski_f_score` | 5,929 | filer x year, the nine signals, `f_score` (only when all nine could be evaluated: 73 filer-years) and `f_score_partial` with `signals_available` for the rest | query 05 |
| `altman_z_double_prime` | 2,450 | latest year per non-financial filer with >= $100M assets, Z'' and zone | query 06 |
| `sector_percentiles` | 3,023 | filer x year, net margin / ROA / FCF margin percentile within SIC2 peers | query 03 |
| `dupont` | 2,156 | filer x year, ROE decomposed into margin x turnover x leverage | query 07 |
| `mdw_fundamental_metrics` | 135 | eight large filers x year, 29 metrics from the warehouse mart | market-data-warehouse |
| `mdw_dim_company` | 8 | the warehouse company dimension | market-data-warehouse |
| `portfolio_daily` | 2,436 | trading day: portfolio and benchmark return, growth index, drawdown | portfolio-risk-report |
| `asset_returns_long` | 14,616 | day x asset daily return | portfolio-risk-report |
| `asset_levels_long` | 14,622 | day x asset price level | portfolio-risk-report |
| `risk_contributions` | 6 | asset: weight, volatility, marginal and component risk, share | portfolio-risk-report |
| `monthly_returns` | 11 | year x month return grid | portfolio-risk-report |
| `monthly_returns_long` | 117 | one row per year and month, for a matrix visual | portfolio-risk-report |
| `summary_metrics` | 2 | portfolio and benchmark: return, vol, Sharpe, Sortino, max drawdown, Calmar | portfolio-risk-report |
| `tail_risk` | 2 | VaR and CVaR at 95 and 99 | portfolio-risk-report |
| `correlation_long` | 36 | asset pair correlation | portfolio-risk-report |

Rebuild after the upstream repos change:

```bash
python scripts/export_dataset.py
```

## Model

Star schema, one fact per grain, `dim_company` shared by every fundamentals
fact on `cik`, and a date table generated in Power BI for the daily facts.

```
dim_company (cik) 1 --- * fundamentals_annual (cik, fiscal_year)
                  1 --- * piotroski_f_score    (cik, fiscal_year)
                  1 --- * altman_z_double_prime (cik)
                  1 --- * sector_percentiles   (cik, fiscal_year)
                  1 --- * dupont               (cik, fiscal_year)
dim_date (date)   1 --- * portfolio_daily      (date)
                  1 --- * asset_returns_long   (date, asset_id)
```

Relationships are single direction, dimension to fact. Measures live in a
dedicated `_Measures` table, not on the facts.

## Report pages

**1. Fundamentals screen**
Slicers: sector, fiscal year, filer status. Visuals: F-score distribution
(column; use `f_score_partial` with a `signals_available` slicer, since the
full nine-signal score exists for only 73 filer-years), Altman zone share (100% stacked bar by sector), scatter of ROA
percentile vs net-margin percentile sized by total assets, and a
conditionally formatted table of the top filers by F-score with the nine
signals as green/red cells.

**2. Company drill-through**
Reached by right-clicking any filer on page 1. Revenue, net income and
operating cash flow by year (clustered column), DuPont waterfall for the
latest year, F-score by year (line), and the filer's percentile rank within
its SIC peers.

**3. Portfolio risk**
Growth of $1 portfolio vs benchmark (line), drawdown (area, both series),
Euler risk contribution vs weight (clustered bar), correlation heat map
(matrix with conditional formatting), monthly return grid (matrix), and a
KPI row from `summary_metrics`: annualized return, volatility, Sharpe, max
drawdown, and 95% VaR from `tail_risk`.

## Measures (DAX)

```dax
Filers = DISTINCTCOUNT(fundamentals_annual[cik])
Median F-Score = MEDIAN(piotroski_f_score[f_score])
Share Distress =
    DIVIDE(
        CALCULATE(COUNTROWS(altman_z_double_prime), altman_z_double_prime[zone] = "distress"),
        COUNTROWS(altman_z_double_prime)
    )
Portfolio Return YTD =
    VAR d = MAX(portfolio_daily[date])
    VAR s = CALCULATE(MIN(portfolio_daily[date]), YEAR(portfolio_daily[date]) = YEAR(d))
    RETURN
        CALCULATE(PRODUCTX(portfolio_daily, 1 + portfolio_daily[portfolio_return]),
                  portfolio_daily[date] >= s, portfolio_daily[date] <= d) - 1
Max Drawdown = MIN(portfolio_daily[portfolio_drawdown])
```

## Getting started in Power BI Desktop

1. Install Power BI Desktop from the Microsoft Store (free, no account needed to build locally).
2. Home > Get data > Parquet. Point at `data/dim_company.parquet`, then repeat for each fact you need. Or use Get data > Folder on `data/` and keep only the Parquet rows.
3. Model view: create the relationships in the diagram above. Set cardinality many-to-one, filter direction single.
4. Modeling > New table:
   `dim_date = CALENDAR(MIN(portfolio_daily[date]), MAX(portfolio_daily[date]))`
   and mark it as a date table.
5. Build the three pages. Save the `.pbix` under `reports/`.
6. File > Export > PDF for a static copy under `reports/` so the output is viewable without Power BI.

## Data notes

- SEC Financial Statement Data Sets are what filers reported, not a cleaned
  vendor feed. About 38% of filer-years have no revenue tag (banks, REITs,
  holding companies and shells report differently). The scores already
  handle this: an F-score is only reported when all nine signals can be
  evaluated, and Altman excludes financials and sub-$100M filers.
- "Unclassified" sector means the SEC record has no SIC code.
- The portfolio is a multi-asset proxy built from public FRED index levels.
  It demonstrates the analytics; it is not an investable allocation.
