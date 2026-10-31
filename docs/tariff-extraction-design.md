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

## Phase 7 — Pre-orchestrator named steps (from the 7a audit)

Step 7a re-ran every stage against a copy of the fully-active database and
found four defects/gaps. Each was fixed as its own named step before the
orchestrator (7b); the live `tariffs.db` was touched only by step P4.

- **P1 — Parsers are idempotent on active state.** `parse_schedule` and
  `parse_rider_gen` previously matched only `status = 'draft'`, so an active
  version made them insert a duplicate draft. They now match the version
  identity in any status and print `already <status>, skipped` (exit 0).
- **P2 — Zero matched pages is an error.** `parse_schedule <code>` exits 1 and
  writes nothing when no page matches (previously it created an empty draft and
  exited 0). The parser has no exceptions; RG-CM (the only zero-page code, 0
  components and 1 rule, confirmed against the PDF) is created by a separate
  stage, `src/create_cross_reference_versions.py`, driven by the inventory's
  `cross_reference_schedules` key.
- **P3 — Database location.** Every stage honors `TARIFF_DB_PATH` when set;
  unset, behavior is unchanged.
- **P4 — `bills.source_file` is project-relative.** The loader normalizes any
  path spelling to the project-relative POSIX form
  (`parse_usage.canonical_source_path`). `src/migrate_source_file_paths.py`
  migrated the live database once: backup first, one transaction, 31 bill
  rows rewritten, and the 3 current-mapping (`phase5-v3`) `audit_runs.input_hash`
  values recomputed with `audit_bills`' own function (v1/v2 runs untouched;
  `result_hash` and `tariff_snapshot_hash` verified unchanged or abort).
  Proof: `audit_bills` immediately after = 3 × `no-op`, 9 audit runs; re-ingest
  of the EAP by relative or absolute path = `no-op`; migration re-run = 0 changes.
  The migration deliberately changed the database file hash
  (`abc91eed…9218` → `cc765bf3…c6eb`); backup kept as
  `data/tariffs.db.pre-source-file-migration.<stamp>.bak`.

Suite after P1–P4: 198 passed (197 unit, 1 integration). 7d comparison scope
(ratified): all tables, timestamp columns excluded, excluding only
superseded-mapping audit rows (v1/v2) and the legacy `usage_accounts` row,
with the current-mapping audit rows asserted equal.

## Phase 7 — Idempotent pipeline: stages, manifest, acceptance evidence

### Stage table (`src/run_pipeline.py`)

Each stage is an existing CLI run as a subprocess from the project root with
`TARIFF_DB_PATH` pointing at the target database. A stage's effect is measured
by a before/after content snapshot of the whole database (timestamp columns
excluded); `rows changed` is, per table, the larger of rows added-or-modified
and rows removed.

| # | Stage | Commands | 7a classification | Re-run result |
|---|---|---|---|---|
| 1 | create_schema | `create_db.py` | safe as-is | no change |
| 2 | parse_gen | `parse_rider_gen.py` | named fix P1 | no change (`already active, skipped`) |
| 3 | parse_schedules | `parse_schedule.py <code>` per manifest | named fixes P1, P2 | no change |
| 4 | create_cross_reference | `create_cross_reference_versions.py` | new stage (RG-CM) | no change |
| 5 | parse_riders | `parse_rider.py <code>` per manifest | safe as-is | no change |
| 6 | load_rules | `load_rules.py` | safe, orchestrator infers change from snapshot | no change |
| 7 | validate | `validate_draft.py` | vacuous pass with no drafts | no change |
| 8 | activate | gate `validate_draft.py`, then `activate_version.py <id>` per draft listed from the DB | orchestrator handling | no change; already-active versions are reported in notes |
| 9 | ingest_bills | `ingest_usage_history.py <pdf>` per manifest | safe after P4 | no change |
| 10 | enrich_determinants | `enrich_determinants.py <pdf>` per manifest (D3) | promoted Phase 5 backfill | no change |
| 11 | audit | `audit_bills.py --mapping <manifest mapping>` | safe as-is | no change |

The legacy scaffold `ingest_usage.py` is not a stage.

### Manifest format (`docs/pipeline-manifest.json`)

`tariff_pdfs` (amendment, rider_gen), `inventory`, `mapping`, `schedules`
(ordered codes), `riders` (ordered codes), `usage_pdfs`. Order is significant:
version ids follow parse order, and the lists reproduce the live ids
(Rider GEN, 23 schedules, RG-CM from the cross-reference stage, 16 riders).
Nothing is discovered from folders. The tariff parsers read fixed PDF paths;
the manifest's `tariff_pdfs` entries are verified to exist (preflight) but
are not yet passed to those parsers.

### Run record (D2)

One `data/logs/pipeline-<UTC stamp>.json` per run, success or failure:
database, manifest and its SHA-256, selected stages, per-stage status and
summary, commands with exit codes and output tails, rows changed per table,
before/after content hashes, and a `failure` block (stage, command, exit code,
reason). Failure output printed to the console is the last six lines.

### 7c — enrichment and activation (on copies)

- Deleting all `bill_determinants` and running `--stage enrich_determinants`
  restored 96 rows; the whole-database content hash returned to the fresh-build
  value and accounts/bills/bill_charges hashes were unchanged.
- With one invalid component and all 41 versions reset to draft, the activate
  stage failed at its validation gate and all 41 versions stayed draft.
- From drafts, a full run ends with 41 active; from all-active, a run is a no-op.

### 7d — fresh-build acceptance

A full run against an empty database (about 3 minutes, dominated by PDF
parsing) produced the golden state: 41 active versions, 448 components, 112
rules, 31 bills, 894 charges, 96 determinants, 3 current-mapping audit runs
with 96 lines. A second run reported `no change` for all 11 stages with an
identical content hash.

`src/compare_databases.py` compares a fresh build to the live database. The
documented exclusions (ratified): timestamp columns; surrogate ids of
`tariff_components`, `audit_runs`, `audit_lines` (live component ids run to
1487 with gaps from earlier re-parses, a rebuild numbers them 1–448;
component references in audit lines are compared by component content);
audit runs of superseded mapping versions and the legacy `usage_accounts`
row; and the hashes that embed component ids (`tariff_snapshot_hash`,
`input_hash`, `result_hash`, and their keys in `report_json`). Result: every
compared table equal, identical content hashes on both sides. **Phase 8 item:**
the tariff snapshot hash depends on surrogate component ids, so audit
hashes are not portable across databases.

### 7e — sabotage, resume, live run

- Sabotage: on an empty database the manifest's usage PDF pointed at a
  truncated copy. The pipeline stopped at `ingest_bills` (exit 1), named the
  stage and command, left the 41 active versions intact, and `enrich_determinants`
  and `audit` were `not run` (0 bills, 0 determinants, 0 audit runs).
- Resume: the manifest was corrected and the same command re-run. The first
  eight stages reported no change, ingestion and audit completed, and the
  result compared EQUAL to the live database.
- Live: `python src/run_pipeline.py` against `data/tariffs.db`: every stage
  `done (no change)`, content hash `bffd4ebd…` before and after, and the file
  SHA-256 `cc765bf3…c6eb` unchanged.

Suite after Phase 7: 207 passed (206 unit, 1 integration), local-only.
Out of scope and untouched: scheduling/CI, external orchestrators, Postgres,
parallelism, ingesting new accounts, scaffold cleanup.

## Phase 8 — Postgres, API, upload page, Docker, CI

Decisions E1–E5 ratified as written: E1 one static page served by the API; E2
Postgres primary with the SQLite file frozen (not deleted); E3 a connection
factory, no ORM; E4 CI is build-and-boot only; E5 localhost, no auth.

### 8a — Dialect audit (grep-proven)

About 74 SQLite-specific sites, all handled either in `src/db.py` or in the
legacy files that deliberately stay SQLite-only:

| SQLite-ism | Where | Handling |
|---|---|---|
| `sqlite3.connect` / `sqlite3.Error` | every CLI | `db.connect` / `db.Error` (mechanical) |
| `?` placeholders, literal `%` (`LIKE 'RIDER %'`) | all SQL | translated to `%s`, `%` escaped (shim) |
| `BEGIN IMMEDIATE` | activate, audit, enrich, ingest, load_rules, parse_rider | transaction-scoped advisory lock (shim; needs care) |
| `PRAGMA foreign_keys` | enrich, ingest | no-op on Postgres (foreign keys always enforced) |
| `PRAGMA table_info` | create_db, ingest snapshot helpers, run_pipeline, compare | emulated from `information_schema` |
| `sqlite_master` | audit_bills, validate_usage, run_pipeline, compare | subselect over `information_schema.tables` |
| `cursor.lastrowid` | audit, cross-reference, ingest, parse_rider, parse_rider_gen, parse_schedule | `INSERT … RETURNING id` on tables with an `id` column |
| DDL types | create_db | `INTEGER PRIMARY KEY` → identity; `REAL`/`NUMERIC` → `DOUBLE PRECISION` (preserves SQLite float storage); `DATE` → `TEXT` |
| `json_array` | compare_databases | replaced by a Python-side join |
| `ON CONFLICT`, `sqlite3.Row` | legacy `ingest_usage.py`, legacy `validate_usage.main()` | left SQLite-only (scaffold) |
| one-time migration | `migrate_source_file_paths.py` | left SQLite-only (completed) |

Behavioural differences found and handled: text collation (Postgres must be
initialised `--locale=C --encoding=UTF8`, or `ORDER BY` differs from SQLite and
text comes back as bytes — found by the in-container run, fixed in both
`db.connect` and compose); NULL ordering (compare sorts in Python); `5` vs
`5.0` (compare normalizes numbers; the audit loader converts integral floats so
the stored `determinant_value` text is identical on both engines).

### 8b — Factory and Postgres build

`src/db.py` reads `TARIFF_DB_URL` (else `TARIFF_DB_PATH`, else the SQLite file)
and returns a plain `sqlite3` connection or a thin Postgres wrapper. The
pipeline is the migration tool: an empty Postgres was built from the manifest.
`run_pipeline.py --database` accepts a path or a `postgresql://` URL, and
`compare_databases.py` reads either. `parse_schedule.py` gained an optional
`--pdf` argument (default unchanged) so uploads can reach it.

Evidence: Postgres fresh build compared EQUAL to the frozen SQLite under the 7d
exclusions; the second pipeline run reported `no change` for every stage; the
SQLite suite stayed green; the live file SHA-256 stayed `cc765bf3…c6eb`.

### 8c — Id-independent snapshot hash

`tariff_snapshot_hash` is computed over natural content: versions, components
and rules with surrogate ids removed and each row's version identified by
`schedule_code#version`, sorted canonically. Two independent Postgres fresh
builds compare EQUAL with `compare_databases.py --strict` (no id-based
exclusions, audit hashes included). The frozen SQLite artifact keeps its
old-style hashes as history; comparison against it still passes under the
legacy exclusion (now legacy-only). Consequence: auditing a bill on the frozen
SQLite records one new run under the new formula.

### 8d/8e — API and page

Endpoints: `GET /health`, `/tariff-versions[/{id}]`, `/accounts`, `/bills[/{id}]`,
`/bills/{id}/audit`; actions `POST /bills/{id}/audit`,
`/tariff-versions/{id}/activate`, `/uploads/bill`, `/uploads/tariff`.
Refusals (non-PDF, unreadable PDF, gate failures) are 4xx with
`{message, failures[]}`. Tariff upload parses with the existing parsers using
their fixed semantics (schedules are stamped Amendment 12, version 12) and
leaves a validated draft; activation is a separate call. Evidence: the New Kent
EAP uploaded to a scratch database through the API returned
`loaded`, 31 bills; pilot verdicts read through the API equal the stored rows
(test); `/docs` returns 200; in a browser, bill #29 rendered its 32 verdict
rows with $7.59 and $12.21 matching.

### 8f — Docker

`Dockerfile` (python:3.9-slim) and `docker-compose.yml` (`api`, `postgres`,
named volumes `pgdata`/`uploads`/`logs`, `data/raw` mounted read-only, both
ports published on 127.0.0.1 only); `.env` is uncommitted, `.env.example`
documents it. From a clean `down -v` state the stack came up healthy, the
pipeline ran in-container (all stages, content hash `522499b1…`, identical to
the host-side builds), a second run was a no-op, and `down`/`up` preserved the
data.

### 8g — CI

`.github/workflows/ci.yml`: compile-check, build image, boot compose, poll
`/health`. Not run by the test suite (E4). Green status and the red-X
check on a throwaway branch need a push and are performed by the repository
owner.

Suite after Phase 8: 216 passed, local-only.


## Stage 1 — B1: evidence citations

Every `tariff_components` row now carries where its value came from.

Decisions:

- **F1 — two columns.** `source_page INTEGER` (1-based PDF page) and
  `source_cell TEXT` (a human-checkable locator in the printed page's own terms).
- **F2 — mandatory.** `validate_drafts` refuses activation when any component
  lacks a positive `source_page` or a non-blank `source_cell`. Zero-component
  (cross-reference) versions are unaffected.
- **F3 — parsers only.** Citations enter by re-running the parsers through the
  pipeline. No backfill script; the frozen `data/tariffs.db` is not overwritten
  and stays without the columns (`create_db.py` adds them to older databases only
  when it is run against them).
- **F4 — no money moves.** Pilot totals stay $95.75 / $101.67 / $114.45.

Locator formats (`src/citations.py`):

| Parser | `source_page` | `source_cell` |
| --- | --- | --- |
| `parse_rider_gen` | 1 | `row "<schedule>", column "<header>", cell "<printed cell>"` |
| `parse_rider` table riders | page of the table | `row "<rate schedule>", column "<nearest header to the left>", cell "<printed cell>"` |
| `parse_rider` text riders (uniform, K, L, G-CM) | page where the rate sentence starts | `text "<rate sentence>"` |
| `parse_schedule` line-based rates, lighting rows, matrix rows | page of the printed line | `line "<printed line holding the amount>"` |
| `parse_schedule` prose schedules (B, C, D, E, SGCM) | page where the excerpt starts | `text "<excerpt containing the amount>"` |

Where a cell-level location is unavailable (text-only PDF content) the locator is
the page plus the matched printed text; nothing is inferred. A snippet that
cannot be found on its page, or that appears on two pages, raises instead of
guessing.

Snapshot hash: `tariff_snapshot_hash` is computed over each component's natural
content, which now includes the two new columns, so audits rebuilt after B1 have
new hashes. This is the deliberate effect of adding evidence, not a drift.
`compare_databases.py` includes the citation columns only under `--strict`,
because the frozen SQLite artifact predates them.

## Stage 2 — B2: audit report

Decisions:

- **G1 — Markdown.** Reports are rendered as Markdown with no new dependencies.
  Print/PDF presentation polish belongs to the UI stage.
- **G2 — per-run determinism.** Each audit run has one report, rendered from its
  stored run timestamp and audit data; rendering never uses the wall clock.
- **G3 — evidence by disposition.** Direct-mapped lines show their applied rate,
  unit, determinant, and tariff-component citation. Zero-expected and
  out-of-scope lines show their disposition and recorded reason, with no
  fabricated citation.
- **G4 — reconciliation gate.** Rendering refuses to write a report if stored
  line deltas do not sum to the run's stored total delta or any line lacks a
  verdict.
- **G5 — local report files.** CLI reports are written under
  `data/reports/`, which is gitignored because it contains real account data.
- **G6 — API.** `GET /bills/{bill_id}/audit/report` returns the latest report as
  `text/markdown`; a bill without an audit run receives a 404.

For legacy `MISSING` direct lines, `audit_lines.detail` does not persist the
rate or determinant used. B2 reconstructs the rate and citation from the joined
`tariff_components` row and reconstructs the determinant from
`bill_determinants` using the checked-in mapping. This is permitted only when
the checked-in mapping version equals the run's `mapping_version` and the
bill/type lookup returns exactly one value; otherwise rendering refuses and
writes no file. **Deferred durable fix:** `audit_bills` should store the
rate/determinant on `MISSING` lines when audits are created. That schema/data
change is not part of B2 and is folded into Stage 3 (B16), the next time
`audit_bills` is opened.

## Stage 3 — B16: all-schedule repricing, human review, comparison

`src/schedule_review.py` reprices a stored bill under every base schedule and
returns, per schedule, either a full computation or exactly one reason. Nothing
about a candidate is stored; only the human decision is.

**H1′ — selection layer in front of the engine.** The engine's evaluation core
(`evaluate_schedule_components`) is reused but never called raw; `billing_engine.py`
is unmodified. Checks run in this order and the first failure decides the reason:

1. Covering version, via `resolve_period_version` (none, or a period straddling a
   version boundary) → `no_covering_version`.
2. Zero priced components in the base version → `cross_reference_only`.
3. Billing class, from the stored `billing_class_threshold` rule text and the
   account's bill history (demand class only with a `demand` determinant and the
   threshold met; otherwise `non_demand`).
4. Variant selection: metered/unmetered from the account's meter status and the
   stored `metered_unmetered_charge_switch` test on bill history (the excluded
   variant and its amount are recorded on the candidate); primary/secondary from
   the account voltage.
5. Contracted-capacity basis (see the SGCM ruling), then determinant
   pre-check for every base and rider component in order, naming the first
   missing input. The engine's silent `total_consumption` fallback never makes
   a candidate computable.
6. Election/contract requirements (`percent`/`multiplier` components, contract
   demand rules, `contribution_options`).

An unexpected engine `ValueError` is a bug and surfaces as `RepricingError`; it
is never converted into a reason.

**H2 — riders.** Riders are selected by version identity (versions carrying rows
for the billed schedule), never by the `RIDER %` name prefix, and each candidate
uses its own `billing_category` rows. Rider G-CM (optional contribution, no
election stored) is excluded from every candidate and the exclusion is stated in
the candidate output and the comparison.

**H3 — closed reason set.** `cross_reference_only`, `no_covering_version`,
`missing_determinant` (naming the input), `needs_election_data`,
`needs_contracted_amount`. A candidate never carries a partial total.

**SGCM ruling.** A per-kW schedule whose stored rules define no measured demand
(no rule type containing `demand`) is priced on a contracted capacity the bill
cannot carry: it returns `needs_contracted_amount` naming the contracted standby
capacity (SGCM, SGCM-1). Schedules 130/131/132/134 have demand rules and keep
`missing_determinant: demand`. This check precedes the determinant pre-check.

**H4 — decisions.** `schedule_review_decisions` (bill, schedule, `works` |
`doesnt_work`, reviewer, note, decided_at, `selected`; unique per bill and
schedule) is created additively by `create_db.py` (`CREATE TABLE IF NOT EXISTS`).
Selecting requires a computed candidate and a `works` decision; a `doesnt_work`
schedule or a reason candidate is refused. At most one schedule is selected per
bill; rejecting the selected schedule clears the selection. Repricing is
computed on every read.

**H5 — comparison.** `src/schedule_comparison.py` renders the bill's billed
lines against the selected schedule's computed lines as Markdown
(`data/reports/`, gitignored). The like-for-like difference excludes
out-of-scope billed lines (tax) and is computed − billed. It refuses when no
schedule is selected. API: `GET /bills/{id}/schedule-review`,
`POST .../schedule-review/decision`, `POST .../schedule-review/select`,
`GET /bills/{id}/schedule-comparison`.

**H6 — MISSING lines.** `audit_bills._missing_result` now stores `rate`, `unit`,
`determinant_type` and `determinant_value` in the line's `detail`. Existing runs
are untouched and not re-audited; the B2 reconstruction rule still applies to
them.

**Named limitation.** The engine cannot evaluate tiered `usd_per_kw` groups or
`usd_per_rkva`, so repricing a demand schedule (130/131/134 and similar) for a
bill that carries a demand determinant raises `RepricingError`. This is future
engine work; bills without a demand determinant (including the October 2025
pilot bill) are unaffected. Pre-Amendment-12 bills have no covering version and
report `no_covering_version` for every schedule.

## Stage 5 — B3 + B4: tariff diff and change alerts

**Scope note (honest).** The live data holds exactly one version per schedule
(Amendment 12). No real version pair exists yet, so B3 and B4 are proven on
crafted fixture versions and on a scratch database; nothing was added to the
primary database. Live alerts begin with the first real version pair — the next
amendment loaded and activated.

**B3 — `src/tariff_diff.py`.** `diff_versions(conn, old_id, new_id)` is a pure
function of stored data. It refuses versions of different schedules (or
state/utility), identical ids, unknown ids, and any version whose components
share a natural key (ambiguous). Components are matched on `(component_type,
unit, variant, billing_class, season, tier_min, tier_max, billing_category)`.
Output, deterministically ordered: `added`, `removed`, `rate_changed` (old, new,
delta; Decimal), `formula_changed`, a rules diff (added/removed by
`rule_type` + `rule_text`), both versions' metadata, and each changed row's
old/new citation (`source_page`, `source_cell`). A component whose only
difference is its citation is not a change.

**B4 — `src/tariff_alerts.py`.** `build_alert(conn, old, new, sample_bill_id)`
returns Markdown: header (schedule, old → new version, effective dates), the
changes list, and the sample bill repriced under each version — the schedule
plus the same in-force riders that were billed, through the Stage 3 repricing
path. It uses no wall clock, so an alert is reproducible. Repricing gained an
optional version pin (`price_schedule(..., pins={code: version_id})`); without a
pin the code path is the one Stage 3 already used (covered by tests asserting
identical candidates). A pin bypasses the period-covering check for that code
only, so a bill predating the new version can still be used as the sample; riders
still resolve for the bill's own period. For a rider version pair the bill's
billed schedule is repriced with that rider pinned. If either side of the
repricing reduces to a reason, the alert states the reason and shows no partial
total. CLI: `python src/tariff_alerts.py --old A --new B --bill N` writes
`data/reports/<schedule>-v<old>-to-v<new>-alert.md` (gitignored).

**Rounding.** The engine rounds each line half-up to cents, so the impact is the
difference of rounded line amounts, which can differ by a cent from
(new rate − old rate) × determinant. Example: 0.0167 → 0.0177 on 884 kWh is
0.884 unrounded, but 14.76 → 15.65 per line, i.e. +0.89.

**Activation hook.** `activate_with_alert` (used by the API and the CLI) activates
and then, if a previous version of the same schedule exists, writes that alert
with the most recent bill on that schedule as the sample (diff-only when no such
bill exists). An alert failure never fails the activation; it is returned as a
`warnings` entry in the response (`alert_path` is null).

**API.** `GET /tariff-versions/{new_id}/diff?against={old_id}` (JSON, money as
strings) and `GET /tariff-versions/{new_id}/alert?against={old_id}&bill={id}`
(`text/markdown`); 404 for an unknown version or bill, 422 with a `failures`
list for a refused pair or an uncomputable alert. `POST
/tariff-versions/{id}/activate` now also returns `alert_path` and `warnings`.

**Known pre-existing limit (not changed here).** `activate_version` counts every
row with `status = 'active'` for a schedule, and a superseded version keeps that
status (only `effective_to` is set). Activating a third version of one schedule
is therefore refused ("Found 2 active versions"). The first real pair works;
the lifecycle needs a `superseded` status before a third amendment.

## Stage 6 — B5 + B6: back-bill lookback and line-item validation

**Scope, stated honestly.** Only Amendment 12 (effective 2025-07-01) is loaded.
The live lookback therefore covers the three audited bills of Aug–Oct 2025
(bills 29–31); the other 28 bills predate or straddle the version and are listed
`no_covering_version`. They are never estimated against a version that was not in
force; they become reauditable as historical versions are loaded.

**B5 — `src/lookback.py`.** `account_lookback(conn, account_number)` is read-only:
it uses stored audit runs and never creates one. Per bill, in period order, the
status is `audited` (latest run's billed like-for-like, recomputed, delta;
overcharge = billed − recomputed when positive), `no_covering_version`, or
`not_audited` (a covering version exists but no run is stored — an addition to
the two specified statuses, so the gap is visible rather than silent). Totals:
total overcharged, audited count, overcharged count, net delta, and the count and
reasons of not-reauditable bills. CLI `--account N` writes
`data/reports/<account>-lookback.md` (gitignored). API:
`GET /accounts/{account_number}/lookback` (404 unknown account, 422 refusal).

**B6 — `src/line_validation.py`.** `validate_bill_lines(conn, bill_id)` reads the
bill's latest audit run and returns one row per billed line with `status` (`ok`
or the first flag) and a `flags` list (a line may carry several). Flags:
`zero_expected_billed` (zero-expected line billed above 0.00); `unmapped_label`
(a billed charge with no mapping entry — such a bill cannot get a run, so this is
detected by comparing `bill_charges` with the mapping); `amount_mismatch` (direct
line, |billed − recomputed| above one cent; a MISSING line, billed 0.00 against an
expected amount, is flagged here); `rate_disagreement` (implied unit rate, billed
÷ determinant, differs from the stored tariff rate by more than half a cent of the
billed amount). `rate_disagreement` applies only to non-tiered `usd_per_kwh` and
fixed-monthly components; other units are not judged. Refusals: no audit run
(422), unknown bill (404), a mapped charge missing from the run (stale run), and
a checked-in mapping version that differs from the run's. API:
`GET /bills/{bill_id}/line-validation`.
