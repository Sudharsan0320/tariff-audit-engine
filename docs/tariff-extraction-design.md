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

## Phase 5 — Config-driven billing and audits

### Decisions and pre-flight

The Phase 5 B1–B5 decisions are implemented as written:

- **B1:** August–October 2025 are the pilot; the July straddle bill and
  proration are deferred.
- **B2:** [`bill-label-mapping.json`](./bill-label-mapping.json) is the
  reviewed mapping artifact. The engine has no bill-label, schedule-code, or
  tariff-rate constants.
- **B3:** `audit_runs` and `audit_lines` store mapping-versioned, tariff-
  snapshot-bound outputs. Identical input hashes are a no-op.
- **B4:** database numeric values are read through `Decimal(str(value))`;
  line expected amounts round with `ROUND_HALF_UP`. A delta of at most $0.01
  is a match; over/under flags begin at $0.02.
- **B5:** determinant enrichment adds source values without changing existing
  account, bill, or charge rows.

The pre-flight found no determinant table. `parse_usage.py` now retains every
numeric row in each bill's **Bill Determinants** block with its printed label,
normalized type, and value. The 31 stored bills received 96 determinant rows.
For the pilot bills, stored total/on-peak/off-peak usage is:

| Bill to | Total kWh | On-peak kWh | Off-peak kWh |
|---|---:|---:|---:|
| 2025-08-13 | 731 | 289 | 442 |
| 2025-09-12 | 783 | 289 | 494 |
| 2025-10-13 | 884 | 364 | 520 |

The enrichment ran through `load_usage_draft`'s exact-match path. The stable
hashes of `accounts`, `bills`, and `bill_charges` were identical before and
after; the original three-table snapshot hash also remained unchanged. Demand
is present for older periods but is null for these 120 bills and is not needed
for this pilot.

### Census and mapping worksheet

The pilot label census contains 32 labels. The active 120 component census
contains 18 components: four schedule components and 14 rider components.
Every printed label is dispositioned in the config, and every direct mapping
has a passing rate-times-determinant witness in at least one pilot bill.

| Printed bill label | Disposition | Amendment 12 component / evidence |
|---|---|---|
| Basic Cust. Charges | direct | `basic_customer_charge` |
| Energy DIS | direct | `distribution_energy` |
| On Peak Energy ESS | direct | `es_energy`, variant `on_peak` |
| Off Peak Energy ESS | direct | `es_energy`, variant `off_peak` |
| Transmission Energy | direct | `rider_t_cm` |
| Fuel Chg | direct | `fuel_charge_rider_a` |
| Rider CCR | direct | `rider_ccr_cm` |
| Rider CE kWh | direct | `rider_ce_cm` |
| Rider DIST kWh | direct | `rider_dist_cm` |
| Rider E kWh | direct | `rider_e_cm` |
| Rider GEN kWh | direct | `rider_gen_cm` |
| Rider OSW KWh | direct | `rider_osw_cm` |
| Rider RBB kWh | direct | `rider_rbb_cm` |
| Rider RPS | direct | `rider_rps_cm` |
| Rider SMR KWh | direct | `rider_smr_cm` |
| Rider SNA KWh | direct | `rider_sna_cm` |
| Rider B kWh | zero expected | printed as zero in all pilot bills |
| Rider BW kWh | zero expected | printed as zero in all pilot bills |
| Rider GV kWh | zero expected | printed as zero in all pilot bills |
| Ridr GT | zero expected | raw misspelled label retained; printed as zero |
| Rider U1 kWh | zero expected | printed as zero in all pilot bills |
| Rider U2 kWh | zero expected | printed as zero in all pilot bills |
| Rider US-2 kWh | zero expected | printed as zero in all pilot bills |
| Rider US-3 kWh | zero expected | printed as zero in all pilot bills |
| Rider US-4 kWh | zero expected | printed as zero in all pilot bills |
| Rider W kWh | zero expected | printed as zero in all pilot bills |
| Rider S kWh | zero expected | printed as zero in all pilot bills |
| Rider R kWh | zero expected | printed as zero in all pilot bills |
| Rider RGGI | zero expected | printed as zero in all pilot bills |
| Rider PPA | zero expected | printed as zero in all pilot bills |
| Rider PIPP | zero expected | printed as zero in all pilot bills |
| Virginia Tax Surcharge | out of scope | statutory tax; present-only, excluded from tariff totals |

The remaining component census entries are `rider_g_cm`, which is optional by
its stored contribution-options rule and has no customer-election evidence,
and `rider_pipp_cm`, whose stored rate is zero. Rider K and L have no 120
billing-category component. These are not treated as missing universally
applicable line items.

### Computation and August hand-check

`billing_engine.py` resolves exactly one active version by the bill-to date
and refuses a bill period that is not wholly covered by that version. This
rejects pre-July 2025 bills and the July straddle rather than estimating
proration. Schedule components and applicable rider components come from the
database; on/off-peak routing uses the component variant and the stored
determinants. Flat, per-unit, and marginal tier blocks are supported. Formula
components have no general expression evaluator and fail explicitly if one is
requested.

The August bill-to 2025-08-13 hand-check below uses the persisted tariff rates
and determinants. Each expected amount is rounded at the line level:

| Bill line | Hand calculation | Expected |
|---|---:|---:|
| Basic Cust. Charges | Flat monthly charge | $7.59 |
| Energy DIS | 731 × $0.01670 = $12.20770 | $12.21 |
| On Peak Energy ESS | 289 × $0.04289 = $12.39521 | $12.40 |
| Off Peak Energy ESS | 442 × $0.02637 = $11.65554 | $11.66 |
| Transmission Energy | 731 × $0.018961 = $13.860491 | $13.86 |
| Fuel Chg | 731 × $0.020968 = $15.327608 | $15.33 |
| Rider CCR | 731 × $0.001242 = $0.907902 | $0.91 |
| Rider CE kWh | 731 × $0.001673 = $1.222963 | $1.22 |
| Rider DIST kWh | 731 × $0.004063 = $2.970053 | $2.97 |
| Rider E kWh | 731 × $0.000730 = $0.533630 | $0.53 |
| Rider GEN kWh | 731 × $0.004320 = $3.157920 | $3.16 |
| Rider OSW KWh | 731 × $0.007003 = $5.119193 | $5.12 |
| Rider RBB kWh | 731 × $0.000473 = $0.345763 | $0.35 |
| Rider RPS | 731 × $0.008637 = $6.313647 | $6.31 |
| Rider SMR KWh | 731 × $0.000222 = $0.162282 | $0.16 |
| Rider SNA KWh | 731 × $0.002699 = $1.972969 | $1.97 |

The 16 direct amounts sum to $95.75. The printed bill total is $96.49; the
$0.74 Virginia Tax Surcharge is itemized out of scope, leaving an exact
like-for-like match.

### Audit results and bounded checks

Each pilot bill has 32 stored charge lines and 32 verdict rows: 16 direct
matches, 15 zero-expected matches, and one out-of-scope tax line. There are no
missing lines, over/under charges, or withdrawn-rider violations. Like-for-like
comparisons are:

| Bill to | Billed | Expected | Delta | Tax, excluded |
|---|---:|---:|---:|---:|
| 2025-08-13 | $95.75 | $95.75 | $0.00 | $0.74 |
| 2025-09-12 | $101.67 | $101.67 | $0.00 | $0.80 |
| 2025-10-13 | $114.45 | $114.45 | $0.00 | $0.90 |

The `audit_runs` input fingerprint includes the mapping-file hash, bill data,
determinants, charge rows, and tariff snapshot. The current
`phase5-v3` mapping has one run per pilot bill; repeating the same run creates
no rows and retains the same snapshot hash. Earlier mapping-version runs are
retained separately.

The minimum charge is reported as not applicable because the stored rule
requires a contracted amount that is absent. Rider G-CM is reported as
conditional and not applied without an election. Schedule 110 is repriced
informationally as non-demand by its stored billing-class structure; its
schedule-only totals exceed Schedule 120 in all three months. Eligibility for
110 is not established by the stored rules, so no cheaper-rate flag is raised.
Schedule 120 matches the reviewed account/service eligibility note in the
mapping. A separate price-anomaly flag cannot be evaluated because stored
bill lines contain amounts but no printed per-line billed rates; amount
differences remain explicit OVER/UNDER misbill findings.

The full test suite passes with `PYTHONPATH=. pytest -q`. July proration,
tax calculation, contracted minimums, and pre-July 2025 bills remain outside
Phase 5.

## Phase 6 — Formal test evidence

Phase 6 keeps tests local in the gitignored `tests/` directory. `pytest-cov`
is installed only in the local virtualenv; no dependency manifest or
production behavior changed. The 57-case organic baseline is now a
191-case suite: 190 `unit` cases and one `integration` case.

Commands:

```sh
# Full suite, including the temporary-database end-to-end path
PYTHONPATH=. venv/bin/python -m pytest --cov=src --cov-report=term-missing -q

# Fast unit-only suite
PYTHONPATH=. venv/bin/python -m pytest -m unit -q

# Slower local-PDF / temporary-database integration path
PYTHONPATH=. venv/bin/python -m pytest -m integration -q
```

Final measured results: full suite **191 passed in 8.76s** with **90% total
coverage**; unit-only **190 passed in 0.23s**; integration-only **1 passed in
5.28s**.
The core per-module coverage report is:

| Module | Raw line coverage | Core-floor disposition |
|---|---:|---|
| `activate_version.py` | 69% | Core logic covered; raw shortfall is CLI entry-point glue. |
| `audit_bills.py` | 90% | Meets the floor. |
| `billing_engine.py` | 97% | Meets the floor. |
| `create_db.py` | 95% | Meets the floor. |
| `explore_amendment.py` | 0% | Named scratch-explorer exemption. |
| `ingest_usage.py` | 64% | Core logic covered; raw shortfall is CLI entry-point glue. |
| `ingest_usage_history.py` | 88% | Core logic covered; raw shortfall is CLI entry-point glue. |
| `inspect_db.py` | 0% | CLI-only inspection utility. |
| `load_rules.py` | 98% | Meets the floor; CLI entry point is tested. |
| `normalize_schedule.py` | 100% | Meets the floor. |
| `parse_account_profile.py` | 90% | Meets the floor. |
| `parse_rider.py` | 93% | Meets the floor. |
| `parse_rider_gen.py` | 94% | Meets the floor. |
| `parse_schedule.py` | 91% | Meets the floor. |
| `parse_usage.py` | 94% | Meets the floor. |
| `read_pdf.py` | 0% | CLI-only PDF dump utility. |
| `validate_draft.py` | 87% | Core logic covered; raw shortfall is CLI entry-point glue. |
| `validate_usage.py` | 91% | Meets the floor. |

`load_rules.py` retains one reviewed but unreachable duplicate-code check
after an exact set-and-length check; the latter makes duplicates impossible
to reach, and the module still measures 98%. The scratch explorer and
CLI-only/entry-point lines are the named coverage exclusions. The local gap
map in [`tests/phase6-gap-map.md`](../tests/phase6-gap-map.md) records the
baseline inventory and the closed rule matrix.

### Independent money witnesses

The Phase 5 August table above records each printed line's rate × determinant
product and its independently rounded amount. Additional cases were
calculated before encoding as assertions:

| Case | Hand arithmetic | Expected |
|---|---|---:|
| Demand-only synthetic | $2.50/kW × 3 kW = $7.50; $7.50 + $6.25 flat = $13.75 | $13.75 |
| Schedule 110 marginal tiers | 300 kWh × $0.04677 + 300 × $0.03577 + 200 × $0.02940 = $30.642 | $30.64 |
| Zero usage | 0 kWh × $0.01670 + $7.59 basic charge = $7.59 | $7.59 |
| Half-cent, odd lower cent | $1.225 → ROUND_HALF_UP | $1.23 |
| Half-cent, even lower cent | $1.235 → ROUND_HALF_UP | $1.24 |
| August Energy DIS | 731 kWh × $0.01670 = $12.20770 | $12.21 |
| August On Peak Energy ESS | 289 kWh × $0.04289 = $12.39521 | $12.40 |
| August Off Peak Energy ESS | 442 kWh × $0.02637 = $11.65554 | $11.66 |

The inclusive version-start, uncovered prior-day, and April Rider GEN v1
boundary cases assert selection/refusal explicitly. The August table's 16
line amounts sum to $95.75; August, September, and October pilot totals are
$95.75, $101.67, and $114.45 respectively, each with a $0.00 like-for-like
delta.

### Golden path and mutation sanity

The integration test rebuilds the schema and the entire parse → validate →
activate → rules load → EAP ingest → pilot audit story under `tmp_path`, using
the local source PDFs. Its assertions cover all 41 version component/rule
counts, the 245/181/22 component split, 112 rules, 31 bills, 894 charges, and
the three pilot totals. It hashes the live `tariffs.db` before and after and
requires an unchanged hash.

Three deliberate mutations were applied one at a time and restored; each
targeted check went red for the intended reason:

| Mutation | Test that caught it | Observed failure |
|---|---|---|
| Change August Energy DIS fixture rate from $0.01670 to $0.01671 | `test_august_direct_line_amounts_reproduce_hand_checked_fixture` | Hand amount changed from $12.21 to $12.22. |
| Change engine rounding from `ROUND_HALF_UP` to `ROUND_HALF_EVEN` | `test_decimal_rounding_uses_half_up` | $1.225 became $1.22 instead of $1.23. |
| Shift Schedule 110's 150 kWh/kW boundary to 151 | `test_tiered_evaluation_uses_stored_marginal_blocks` | Tier total changed from $30.64 to $30.66. |

After restoration, the full suite returned to green. Final test counts by
functional category are 51 billing/audit, 64 usage ingestion/gate, 42 tariff
validation/activation/rule loading, 30 parser, 3 schedule normalization, and
one end-to-end integration case. The tests remain local-only; CI and automated
mutation/property-based tooling remain out of scope.
