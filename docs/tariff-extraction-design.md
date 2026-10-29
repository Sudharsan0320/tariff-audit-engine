# Tariff Extraction Design

## Phase 3d–3f addendum

This addendum records the rider-ingestion, rules-capture, and activation plan
for Amendment 12. The source amendment is effective 2025-07-01.

### 3d — Rider sheets

Each active rider in Amendment 12 receives its own v12 draft, effective
2025-07-01. Rider `schedule_code` values are prefixed with `RIDER ` so that,
for example, Fuel Charge Rider A cannot collide with Schedule A. The existing
April 2025 `RIDER GEN` v1 remains a separate active version; Amendment 12's
`RIDER GEN-CM` is a different rider.

Each printed schedule rate becomes one component. The component type is the
slug of the printed rider name, `billing_category` is the applicable schedule
code, and cents are normalized to dollars at ingestion. Demand-basis footnotes
are tariff rules, not component data.

| Rider group | Riders | Expected rows |
|---|---|---:|
| Uniform prose | A, CCR-CM, RPS-CM, PIPP-CM | 52 |
| Schedule tables | T-CM, GEN-CM, DIST-CM, CE-CM, OSW-CM, SNA-CM, E-CM, RBB-CM, SMR-CM | 117 |
| Special | G-CM, K, L | 12 |
| **Total** | **16 riders** | **181** |

Special-rider data: G-CM has seven positive REC rates; K and L have negative
monthly-credit rates on their applicable schedules. Formula alternatives
remain rules. The reader uses only each rider's individual sheet; the
pp. 95–96 matrix is a validation witness, never an ingestion source.

### 3e — Rules

`tariff_rules` stores one row per rule with a concise `rule_type` and faithful
source text. Rule rows are loaded by `src/load_rules.py` from a reviewed
intermediate inventory, not entered through ad hoc SQL.

Rule types include minimum charge, demand determination, proration, peak hours,
billed kWh, outage credit, day classification, voltage classes, eligibility,
cross-reference, formula, discount, and definition.

Deferred schedule topics include:

- Minimum-charge formulas, demand determination and ratchets, proration,
  peak-hour definitions, billing-month seasons, and voltage classes.
- Schedule 132 day classifications, defaults, and period definitions; SSL
  tier bands; lighting billed-kWh tables; and Schedule 154 unmetered
  estimation.
- Schedule 152's Schedule 150 reference and bridge-pole adder; Schedule 153's
  four charging mechanisms; Schedule A's net-cost charge, floor, and excess
  rule; SP's discounts and minimum-charge floor; and RG-CM settlement terms.
- Schedule D alternative conversion formulas and zero floor; Schedule B's
  TERF and percentage updates; Schedule C paragraph 4, cross-read with
  Agreement Section XIII and with unresolved garble explicitly flagged.
- Rider demand-basis footnotes, G-CM's contribution option, K/L formulas, and
  withdrawn-rider consolidation notes on GEN-CM and DIST-CM.

Each of the 40 Amendment 12 schedule/rider versions must receive its reviewed
rules. Verification compares per-version rule counts to the reviewed
inventory and spot-checks rule text against the source PDFs.

The checked-in inventory is [`tariff-rules-inventory.json`](./tariff-rules-inventory.json).
It preserves the 101 bullets and per-section counts in the supplied RULES
inventory, then tags 11 source-verified supplements for plan items omitted
there: Schedule A's net-cost/floor, future-permanent-service and external SCC
terms; Schedule B's TERF interaction; Schedule 110's summer/base billing
seasons; Schedule 132's on-peak periods; Schedule D's three alternative
conversion formulas; and Schedule SSL's tier bands. The final asserted count
is 112 rows across the 40 v12 versions. Four uniform riders have zero separate
rule bullets in the supplied inventory; their rates and applicability are
stored as components.

The Amendment 12 PDF does not include the referenced Agreement Section XIII
text for Schedule C's $3.01/kW phrase. That clause remains explicitly marked
`[garbled — verify]`; no missing amount or formula language is inferred.

### 3f — Validation and activation

Before activation, validate all 40 v12 drafts, recheck matrix witnesses and
unit normalization, and confirm parser source contains no tariff-rate
constants. Activate each version independently after validation. The target
version is validated inside its activation transaction so unrelated drafts
cannot block it. Preserve `RIDER GEN` v1 as a separate active version.

Completion totals: 41 active version rows (the existing `RIDER GEN` v1 plus
40 Amendment 12 versions), 448 components (245 schedule components, 181
Amendment 12 rider components, and 22 existing `RIDER GEN` v1 components),
and 112 rules.

## Implementation status

- 3d: implemented in `src/parse_rider.py`; 16 v12 rider drafts and 181 rows
  parsed from the individual sheets.
- 3e: implemented in `src/load_rules.py`; the reviewed inventory and
  source-verified supplements load 112 rule rows with per-version count
  assertions.
- 3f activator behavior: `src/activate_version.py` validates only the target
  draft in its transaction.
- 3f: all 40 v12 versions were validated and activated individually; the
  standalone April `RIDER GEN` v1 remains active.
- Full totals: 41 active versions, 448 components, and 112 rules. The earlier
  267-row pre-3d snapshot comprises 245 schedule rows plus 22 `RIDER GEN` v1
  rows; its 261-row predecessor predates the six SGCM/SGCM-1 components.

## Phase 4 — Validated usage ingestion

### 4a — Usage schema decisions

Ratified decisions:

- **U1 — Shared database and normalized tables.** Usage data lives beside the
  tariff tables in `data/tariffs.db`. `accounts` stores one account per text
  `account_number`; `bills` stores one billing period per account; and
  `bill_charges` stores each printed charge line. The account number remains
  text so leading zeros are significant.
- **U2 — Printed amounts are evidence.** Billed totals and charge amounts are
  stored as printed. Ingestion does not adjust historical prices or reconcile
  differences against tariff versions. Component mapping and bill auditing
  belong to Phase 5.
- **U3 — Preserve and normalize rates.** `current_rate_raw` and
  `billed_rate_raw` retain the printed rate strings; `schedule_code` stores the
  normalized code with the `VE-` prefix removed.
- **U4 — Bill idempotency.** `(account_number, bill_from, bill_to)` is unique
  in `bills`, so a billing period cannot be duplicated for an account.
- **U5 — Evidence-first staging.** Source PDFs in `data/raw/` remain untouched.
  The pipeline extracts a reviewable draft, validates it, and only then loads
  clean rows. Rejected drafts produce a failure report with reasons.

The additive schema in `src/create_db.py` creates `accounts`, `bills`, and
`bill_charges` without changing the tariff tables. `bills.account_number`
references the text account key, and each charge references its bill. Charge
labels remain raw, including duplicate printed basic-charge lines and riders
that are no longer active. Rate normalization does not imply a tariff audit;
historical bills without an effective tariff version can still be ingested.

### 4b–4d — Implementation and verification status

The generic native-PDF implementation is in `src/parse_usage.py`,
`src/validate_usage.py`, and `src/ingest_usage_history.py`. It extracts a
reviewable draft, maps month cells by PDF position (including sparse rider
rows), runs the V1–V7 checks and W1 coverage warnings, and loads only passing
drafts in one transaction. Re-ingestion is accepted only when the complete
account and bill set matches; the three-table ordered snapshot hash is exposed
for repeat-run verification. The earlier `usage_accounts` parser and loader
files, table, and stored profile were not changed by this path. The existing
`validate_account_profile` function and its default CLI behavior remain intact;
the U1 draft gate is implemented as separate functions in
`src/validate_usage.py`.

The target PDF is now present at `data/raw/1172485896__EAP_Report.pdf`.
Extraction confirms the profile fields, the annual witnesses, the VE-110 to
VE-120 transition in October 2024, and the 2024/2025 usage witnesses. The
history table printed in the PDF contains only April through December 2023;
the annual witness is 12,877 kWh, while those nine printed periods sum to
9,077 kWh. January through March detail is absent from the source and is not
inferred from the annual summary.

#### V3 amendment — negative billed lines

Negative charge-line amounts are accepted as-printed evidence and listed as
informational gate findings. Rider PPA and Rider U1 are negative in the source
without credit/payment words, and their signed amounts participate in the
printed bill totals. V3 still rejects negative consumption, demand, or total
billed values and rejects non-numeric or non-finite amounts. V6 and V7
reconciliation are the arithmetic checks for signed charge lines.

#### V6 finding — duplicate basic-charge display row

`V6-F1 — Basic Customer Chg is a display duplicate.` After excluding bold
summary rollups and footer text from `bill_charges`, retaining both basic
charge rows leaves each of the 31 available monthly sums higher than its
printed total by exactly the amount on `Basic Customer Chg`. Excluding that
second line makes every available month reconcile to its printed total within
the $0.01 gate tolerance. The draft retains the line and its label as source
evidence; the validated loader omits it from stored charge rows under this
finding. The duplicate is not silently renamed or merged.

#### V7 split — monthly gate and annual variance

For all 12 available 2024 periods and all 10 2025 YTD periods, each extracted
monthly `* Subtotal` equals that period's printed `** Total Charges` row. Their
monthly sums are $1,524.65 for 2024 and $1,254.32 for 2025, versus annual
witnesses of $1,514.62 and $1,248.12, respectively. Usage exactly matches the
annual witnesses (11,009 and 9,566 kWh). The dollar differences (+$10.03 and
+$6.20) are therefore not caused by a misread monthly total; no tolerance was
changed and no annual basis is inferred.

Ratified V7 split:

- **V7a — blocking, per-bill three-way reconciliation.** After applying V6,
  detail charges must equal the printed subtotal and printed total within
  $0.01. All 31 available bills pass. A source-derived `history_year_status`
  is stored on each bill: 2023 `partial_source`, 2024 `complete`, and 2025
  `year_to_date`. No rows are synthesized for the missing 2023 periods.
- **V7b — informational annual variance.** Each run reports detail totals,
  annual witnesses, deltas, and history status. Findings: 2022 has no detail
  (`-$1,154.76`, `-9,160 kWh`); 2023 is partial (`-$489.85`, `-3,800 kWh`);
  2024 is complete (`+$10.03`, `0 kWh`); 2025 is YTD (`+$6.20`, `0 kWh`).
  Annual variance does not block bills that pass V7a.

#### 4d — Load and verification

The amended gate passes the New Kent draft with informational annual variances
and W1 coverage warnings. `accounts` contains one New Kent row, `bills`
contains 31 periods (9/12/10 by history year), and `bill_charges` contains 894
detail rows after omitting the V6 display duplicate. Stored totals and kWh
recompute to $1,251.11 / 9,077 for 2023, $1,524.65 / 11,009 for 2024, and
$1,254.32 / 9,566 for 2025. All 2023 bills carry `partial_source`.

The complete parse→validate→load chain was run twice. The second run returned
`no-op`; per-account counts stayed at 1 account, 31 bills, and 894 charges, and
the ordered three-table snapshot hash stayed
`0b21f11d296288115727b799b8fe93de67293a2085545516ad6e8433a2afebb9`.
Visual, line-by-line checks against the PDF passed for the March 17–April 19,
2023 bill, the September 13–October 11, 2024 VE-120 bill, and the September
13–October 13, 2025 bill. In each, stored metadata and ordered charge labels
and amounts match the source after the documented V6 omission.

The full suite passes with `PYTHONPATH=. pytest -q`. The legacy
`usage_accounts` row remains unchanged. No study-guide file is present in this
workspace; the README states that Phase 4 uses native PDFs, not CSV.
