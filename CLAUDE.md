# CLAUDE.md

Project rules for Claude Code, and how this repo was built with it.

## Conventions the tool must keep

- **This is the reporting layer only.** SQL and Python live in the three
  sibling repositories. Nothing here recomputes a metric; the export script
  reads what they produced and reshapes it for a BI model.
- **The Power BI project is generated, not hand-edited.**
  `scripts/build_pbip.py` writes the whole `powerbi/` folder — the TMDL
  semantic model (tables typed from the Parquet schemas, dimensions,
  relationships, measures) and the PBIR report (pages, visuals). Change the
  generator, re-run it, reopen in Desktop. Desktop's own save normalises
  the files (line endings, a dropped `$schema`, a few annotations) and adds
  `diagramLayout.json`; that is expected and committed.
- **Parquet is the committed format**, CSV is a local fallback and ignored.
  Every table in `data/` is validated by `tests/test_dataset.py` (grain,
  keys, relationships, score ranges) and every field a visual references
  must exist in the model — `tests/test_pbip.py` regenerates the project
  into a temp folder and checks it. A table or field that is not tested is
  not in the model.
- **Explicit measures only**, all in `_Measures`, each with a description,
  a display folder and a format string; `DIVIDE()` never `/`.
- **Relationships are single direction, dimension to fact.** `dim_company`
  and `dim_fiscal_year` join every fundamentals fact; `dim_date` joins the
  daily facts.
- **Nothing proprietary, no keys, no contact details.** The only
  machine-specific value is the `DataFolder` parameter (absolute path of
  `data/`), which Desktop exposes under Transform data > Manage parameters.

## How this was built

Dataset export, star schema, page specifications and DAX starters were
drafted with Claude Code on 2026-09-23. On 2026-09-25 the plan changed from
"build the pages by hand in Desktop" to "generate the project as text": the
Power BI project format (PBIP) stores the model as TMDL and the report as
PBIR JSON, both publicly documented, so the whole report can be authored
and reviewed as code. The formats were learned from Microsoft's official
sample project and the published JSON schemas; Claude Code wrote the
generator, installed Power BI Desktop, opened the project, and verified
every page by screenshot — driving Desktop by mouse and keyboard because it
cannot see a GUI any other way.

What the real product corrected, in order:

1. **`"active": true` on table projections crashes the table renderer.**
   The bar-chart template marks axis columns active; copied onto a table
   that mixes columns and measures, Desktop threw a JavaScript TypeError in
   `PivotTableVisuals` and rendered one column. Desktop-authored tables
   never carry the flag.
2. **Display units "None" is `1D`, not `0D`** (`0` is Auto). Until that
   was right, cards printed "$0bnM": auto units scaled to billions, then
   the format string scaled again.
3. **The `,,` scaling in a format string is ignored when a literal follows
   it.** Money measures divide by 1e6 in DAX and format as `\$#,##0\M`.
4. **A company page must refuse to aggregate 5,505 filers.** Its measures
   return blank unless exactly one filer is in context and pick the latest
   fiscal year in context, so cards show one company's latest year while
   the same measures still work per year in the charts.
5. **Matrix and table totals are off by default here**: a total of monthly
   returns or of correlations is not a number anyone should read.
6. **Slicers need ~60 px** to show their dropdown; at 44 only the header
   rendered.

The `.pbix` and `reports/finance-dashboards.pdf` are exports of the
generated project after a data refresh in Desktop; the PNGs in
`docs/images/` are the PDF pages.
