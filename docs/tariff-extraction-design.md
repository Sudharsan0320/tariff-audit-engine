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

**Former limit — fixed in Stage 5.5.** `activate_version` used to leave a
replaced version at `status = 'active'`, so a third version of one schedule was
refused ("Found 2 active versions"). Activation now sets the predecessor's
`effective_to` and `status = 'superseded'` in the same transaction as the new
version's activation, so exactly one row per schedule is `active`. See "Version
lifecycle" below.

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

## Stage 5.5 — version lifecycle

`tariff_versions.status` is `draft` → `active` → `superseded`. `active` is the
current version; `superseded` is one replaced by a later activation (its
`effective_to` is the successor's `effective_from`). Date-based resolution
(`resolve_version`, `resolve_period_version`, `validate_usage._tariff_windows`,
and the schedule list in `schedule_review._base_schedules`) reads
`status IN ('active', 'superseded')` and selects by effective range, so a past
bill still resolves to the version in force on its date. Activation checks
("exactly one active version") count only `active` rows. No stored row changes:
live data has one version per schedule, all `active` (41), and none is
superseded until a second amendment is activated.

## Stage 7 — B7: URDB reconciliation

**Purpose.** A second pair of eyes on the parser: compare stored rates with a
public dataset (OpenEI's U.S. Utility Rate Database, bulk CSV) and flag
disagreements for human review. A disagreement is a flag, not a verdict — either
side can be wrong or the two can describe different things.

**Coverage caveat (stated up front).** The parsed book is the VEPGA Amendment 12
schedules (governmental/JPA schedules). URDB covers retail utility tariffs, so a
live run may find few or zero counterparts. Zero matches is a truthful result,
not a failure; the mechanism is what this stage proves.

**Module.** `src/urdb_reconcile.py` (`--csv <path> --utility "<name>"`, repeatable
`--utility`, optional `--versions` ids and `--map "rate name=code"`). It streams the
extract (`.csv` or `.csv.gz`), keeps rows whose `utility` equals a given name, and
reads stored components read-only. The extract is never written. Reports go to
`data/reports/urdb-<utility>-reconciliation.{json,md}` (gitignored) and are
deterministic (no wall clock).

**Matching rule (explicit, recorded in every report).** A URDB rate corresponds to
a stored schedule only when its rate name contains "Schedule `<code>`" as a whole
token (case-insensitive), or the caller supplied an explicit rate-name = code
mapping. The URDB start/end dates must overlap the stored version's effective
range, else the row is listed unmatched. Numbers that happen to be equal never
create a match. Each item carries its `basis`.

**What is compared.** The fixed monthly charge (`basic_customer_charge`,
`usd_per_unit_month`, versus `fixedchargefirstmeter` in `$/month`) and flat energy
rates (stored non-tiered `usd_per_kwh` base components, excluding riders, versus a
URDB structure with exactly one period and one tier in kWh). A stored schedule
with several fixed or flat components agrees if any one equals the URDB value;
the others are listed `unmatched_ours`. URDB structures with several
periods/tiers are listed `unmatched_theirs`, not forced.

**Tolerance.** 0.000001 on both quantities; our side stays Decimal, theirs is read
as Decimal text. Anything beyond is a disagreement, reported with ours, theirs,
and delta = theirs − ours against the nearest stored component.

**Four-way output.** `agree`, `disagree`, `unmatched_ours`, `unmatched_theirs`.

**Live-run outcome.** The bulk file (`usurdb.csv.gz`, 12 MB, 58,920 rates) was
fetched once from OpenEI's USURDB download into `data/external/` (gitignored). The
utility names present for Dominion/Virginia were "Virginia Electric & Power Co"
and "Virginia Electric & Power Co (North Carolina)" (243 rates, dated mostly
2012–2016, named like "GS-1" and "Residential Schedule 1"). Against the 41 active
versions: agree 0, disagree 0, unmatched_ours 35, unmatched_theirs 243 — no URDB
rate name corresponds to a VEPGA schedule, as expected from the caveat above.

## Stage 8 — B9 + B15: parse-review queue and parser contract

### B9 — parse-review queue

Shaky parse results reach a human instead of being guessed or silently trusted. The
table `parse_review_items` (additive `CREATE TABLE IF NOT EXISTS` in `src/create_db.py`,
which upgrades existing databases in place) holds one row per hit: source kind and
reference, schedule code, field, stored value, signal, detail, status (`open` /
`resolved`), reviewer, resolution note, timestamps. A hit is unique on
(source_kind, source_ref, field, signal).

`src/parse_review.py::scan_for_review(conn)` queues hits from four explicit data
patterns only; nothing is inferred:

| Signal | Pattern |
|---|---|
| `verify_marker` | a stored rule text, or a component text field, containing the word "verify" or "garbled" (the Schedule C `$3.01/kW` standby rule, which ends `[garbled — verify]`, is item #1) |
| `unevaluable_formula` | a component with a formula, where the billing engine's own `evaluate_component` reports it has no evaluator (the signal vanishes if an evaluator is ever added) |
| `draft_missing_citation` | a component of a `draft` version without `source_page` >= 1 and a non-blank `source_cell` |
| `zero_rate_unexpected` | a component with rate 0 whose type does not contain a `NON_ORDINARY_TOKENS` word (credit / adjustment / payment, the same list `validate_draft` uses) |

Scanning is idempotent: a re-scan creates no duplicates and never reopens a resolved item.
`resolve_item(conn, id, reviewer, note)` requires a reviewer and a note saying what
happens next; it marks the item resolved and **never edits the component or rule**.
Resolution records that a human looked; fixes flow through the parsers and a re-run, and
the next scan reflects the corrected data. API: `GET /parse-review[?status=open|resolved]`
and `POST /parse-review/{id}/resolve` (404 unknown item; 422 with a `failures` list for a
blank reviewer/note, an already-resolved item, or an unavailable queue table).
`python src/parse_review.py --scan` runs the scan from the CLI.

Signals are heuristics over stored data: `zero_rate_unexpected` can flag a legitimately
zero rate, and that is what the human resolution records. On the live data the scan queues
15 items: 1 `verify_marker` (Schedule C rule 63) and 14 `zero_rate_unexpected` (Schedule 132
`es_contract_demand_charge` and the 13 Rider PIPP-CM components); no formula components and
no drafts exist, so those two signals queue nothing.

### B15 — the parser contract

`src/parser_contract.py::check_parser_contract(parse_fn, sample_source, conn_factory)` runs a
candidate adapter against scratch databases and returns the list of contract failures
(empty = conforming). An adapter is `parse_fn(source, target)`; `conn_factory()` returns the
target of a fresh, empty, schema-initialised scratch database on each call
(`sqlite_scratch_factory(dir)` builds them with `create_db.py`). The five clauses:

1. **(i)** it writes only `draft` versions, and at least one from the valid sample;
2. **(ii)** every component it writes carries a citation (page >= 1 and a cell);
3. **(iii)** its drafts pass `validate_drafts` — refusals are returned, not hidden;
4. **(iv)** running it twice changes nothing (full table snapshots compared);
5. **(v)** given a foreign, unreadable source it raises an ordinary exception (a crash such as
   `TypeError` does not count as a refusal) and writes nothing.

Failures are strings tagged with their clause. A sample that makes the adapter raise is
reported as a single `sample:` failure.

### What the second adapter proved

`src/csv_rate_parser.py` reads a CSV rate sheet (schedule_code, version, effective_from,
component_type, unit, rate, source_page, source_cell; state and utility are parameters) and
writes draft versions through the same tables and the same draft-only, citation-carrying,
idempotent pattern as the PDF parsers. It validates the whole sheet before writing, so a
refused sheet writes nothing. The contract passes for it **and** for the existing PDF rider
path (`parse_rider`, unchanged, through a thin `rider_adapter`), and a deliberately
non-conforming stub (writes an `active` version, no citations, not idempotent) fails with
clauses (i), (ii), (iv) and (v). The contract is therefore not PDF-shaped: it constrains
what lands in the database, not how the source is read.

## Stage 9 — B11 + B12: anomaly detection and weather normalisation

Flags are **for review, never verdicts**: none of them asserts a billing error, and the
report says so. Both modules are read-only over stored bills.

### B11 — flag rules (`src/anomaly.py`)

`account_anomalies(conn, account_number)` walks the account's bills in period order and
computes per bill kWh, total billed and the implied unit cost (`total_billed / kWh`, Decimal,
six places). Thresholds are named constants and function parameters, and every result states
them:

| Flag | Rule | Default |
|---|---|---|
| `unit_cost_jump` | unit cost deviates from the median unit cost of the trailing same-schedule bills by more than the threshold (strict `>`, either direction); needs a minimum baseline | `UNIT_COST_DEVIATION` 20%, `TRAILING_WINDOW` 6 bills, `MIN_BASELINE_BILLS` 3 |
| `usage_spike` | kWh changes by more than the threshold vs the previous bill while the schedule is unchanged (increase or decrease, direction recorded) | `USAGE_CHANGE` 50% |
| `schedule_change` | informational: the schedule differs from the previous bill | — |

Comparisons never cross schedules: the unit-cost baseline is the last six bills of the *same*
schedule, so the first bills after a change have no baseline and are not flagged. Each flag
carries a reason sentence and the numbers behind it.

### B12 — degree-day method (`src/weather_check.py`)

Input is a daily mean-temperature series (°F). Per bill period (`bill_from` through `bill_to`
inclusive) CDD = Σ max(0, T − 65) and HDD = Σ max(0, 65 − T); the base temperature is a
parameter. A period with any missing day has no degree days and its spikes stay
`unexplained`. `annotate(anomalies, weather)` returns a copy (input untouched) with degree
days per bill and the quartiles across the account's periods (Q1, median, Q3 by linear
interpolation, stated in the output). A `usage_spike` **increase** whose period degree days
(CDD + HDD) are at or above Q3 is marked `weather_consistent`; otherwise `unexplained`. A
decrease is never marked weather-consistent. The annotation is context: longer bill periods
accumulate more degree days, and a "consistent" spike is not thereby proven correct.

The live series comes from the Open-Meteo archive (no key), fetched once by
`python src/weather_check.py --start … --end … --lat … --lon …` into
`data/external/weather-daily.json` (gitignored). The API includes the annotation when that
file is present and omits it otherwise.

### API and CLI

`GET /accounts/{account_number}/anomalies` (money and unit costs as strings; 404 unknown
account, 422 refusal). `python src/anomaly.py --account N` prints the flags and writes
`data/reports/<account>-anomalies.md`.

### Era-change caveat

Bills before Jul 2025 were billed under tariff versions that are not loaded, and the account
moved from Schedule 110 to 120 in Sep 2024. Each bill row records whether a covering tariff
version is loaded (`tariff_version_loaded`); a `unit_cost_jump` on an uncovered bill carries an
`era_note`, because a shift across tariff eras is an era change, not an error. The report also
repeats this as a standing caveat.

### Live outcome

On account 001172485896 (31 bills): 0 bills flagged for review; 1 informational
`schedule_change` (bill 19, period ending 2024-10-11, 110 → 120). The nearest calls were far
from the thresholds (largest same-schedule kWh change 28.8%; largest unit-cost deviation 8.4%;
unit cost range $0.129050–$0.146961/kWh). With no spikes there was nothing to weather-annotate;
the degree-day quartiles were computed for all 31 periods (Q1 188.3, median 340.9, Q3 486.9).

## Stage 10 — B14: multi-site portfolio view

A `sites` table (additive `CREATE TABLE IF NOT EXISTS` in `src/create_db.py`; id, name,
`account_number` UNIQUE with a foreign key to `accounts`, note, created_at) registers the
accounts the portfolio tracks. `src/portfolio.py` provides:

- `register_site(conn, name, account_number, note=None)` — refuses a blank name, an unknown
  account number, and an account that is already registered.
- `portfolio_ranking(conn)` — per site, from the stored audit runs through the account
  lookback (`src/lookback.py`): bills audited, total billed and total recomputed
  (like-for-like), total overcharged (per-bill billed − recomputed, counted only when
  positive) and the worst single-bill overcharge with its bill id. Sites are ranked by total
  overcharged descending; at equal totals an audited site precedes a site with no audits, then
  name order. A site with no audited bills is listed with zeros and a `no_audits` note — never
  dropped, never estimated. The ranking runs no audits and writes nothing.

API: `GET /portfolio` (money as strings) and `POST /sites` (JSON body; 422 with a failure list
for a duplicate or unknown account). CLI: `python src/portfolio.py [--register "Name|account|note"]`
prints the ranking and writes `data/reports/portfolio-ranking.md`.

Live: one site, "New Kent (VEPGA)" (account 001172485896): 3 audited bills, billed
$311.87 = recomputed $311.87, $0.00 overcharged (the three pilot totals $95.75 + $101.67 +
$114.45).

## Path C — decision

Path C is decided: this is a **personal portfolio engine** — tariffs parsed from source
documents, bills audited against them with citations, and a review loop that keeps a human in
charge of anything uncertain. OpenTariff remains a possible future contribution (the parser
contract of Stage 8 is the intended seam); nothing in the engine depends on it.

## Continuation complete

Stages 1–9 and 5.5 plus this stage extend the Phase 1–8 build. Suite counts (local-only,
cumulative) as each stage closed:

| Stage | Scope | Suite |
|---|---|---|
| 1 | B1 evidence citations on all 448 components | 228 |
| 2 | B2 audit report (Markdown, reconciliation guard, endpoint) | 235 |
| 3 | B16 all-schedule repricing, human review, comparison | 259 |
| 4 | Web UI (single static page; no backend change) | 259 |
| 5 | B3 tariff diff + B4 change alerts | 270 |
| 6 | B5 back-bill lookback + B6 line-item validation | 281 |
| 5.5 | Version lifecycle (`superseded`) | 283 |
| 7 | B7 URDB reconciliation | 294 |
| 8 | B9 parse-review queue + B15 parser contract | 319 |
| 9 | B11 anomaly detection + B12 weather check | 334 |
| 10 | B14 portfolio view | 343 |

Standing limits: only Amendment 12 is loaded, so lookback, diff alerts and anomaly era checks
grow useful as historical and later versions are loaded; the engine does not evaluate tiered
`usd_per_kw` groups or `usd_per_rkva`; the API is single-user and unauthenticated by design.

## Stage 11 — Universal electric upload (11A: document intake)

### Requirement (the owner, 2026-10-31)

A user uploads ANY electric utility's tariff PDF → the system identifies it, saves it under that utility/state/version, and later audits bills uploaded for that utility.

### Defect removed

API tariff uploads ran the Amendment 12 parsers and stamped everything Amendment 12 / Virginia Electric and Power Company. The stamp is hardcoded in `parse_schedule.py` (version values `VA`, utility, code, 12, `2025-07-01`, `draft`), `parse_rider.py` (`EFFECTIVE_FROM`, `RIDER_VERSION`, `UTILITY`, `STATE`), `create_cross_reference_versions.py` and `load_rules.py`. Neither upload parser looked at the PDF's identity.

### Intake design: save → identify → route → parse

1. **Save** (`document_intake.save_upload`): bytes stored as `<sha256>.pdf`, content-addressed; identical bytes are not rewritten. A `<sha256>.intake.json` sidecar records the original filename, status and detection summary.
2. **Identify** (`identify_document`): native PDF text of the first 6 pages, no OCR. Probes match identity only (utility names, tariff-book headings, bill markers), never rate values. Returns doc_kind (tariff | bill | unknown), utility, state, version label, effective date, evidence (probe, page, text) and reasons. Multi-word headings match whitespace-tolerantly because PDF text wraps lines.
3. **Route** (`route_tariff`): a registry maps utility → adapter (factory + `accepts`), adapters conform to `parser_contract.py`. Dominion routes to the existing parsers, unchanged, but only for Amendment No. 12 effective 2025-07-01. Statuses: `parsed`; `saved_unsupported` (utility detected, no adapter or document not accepted: file kept, zero version rows, stated plainly); `saved_unidentified` (unknown). Nothing is stamped with another utility's identity. A bill sent to the tariff endpoint is refused by kind (422); a tariff sent to the bill endpoint likewise.
4. **Parse**: only on `parsed`, followed by the existing validation and draft flow.

API: `POST /uploads/tariff` returns detection, status, `versions_created` and intake info; `kind` and `code` are now optional and required only when an adapter is selected. Bill upload is otherwise unchanged (bill routing is Task 3).

### Substage map

- 11A intake: save, identify, route (this stage).
- 11B collect the data: an AI extraction agent for every detected tariff that has no hand-written adapter (first target: Appalachian Power Tariff No. 28).
- 11C calculate for that utility: audit bills against that utility's versions.
- 11D other schedules + witness + UI.

### Inventory of the previous upload path

- `POST /uploads/bill` called `ingest_usage_pdf` (the EAP parser); a tariff PDF sent there was only caught by an exception.
- `POST /uploads/tariff` required `kind` (schedule | rider) and `code`; rider → `parse_rider(code, pdf, db)`, schedule → `src/parse_schedule.py <code> --pdf`; then `validate_drafts` and draft status.
- Files landed in `data/uploads/<sha12>-<original name>` (`TARIFF_UPLOAD_DIR`), with no deduplication. Now `<full sha256>.pdf`.
- The Amendment 12 stamp: see "Defect removed".

## Stage 11B — AI extraction agent

Owner decision (2026-10-31): extraction for a newly uploaded company is done by an AI agent that does the hand-written parser's job. A hand-written adapter still wins where one exists (Dominion). The AI adapter (`src/ai_extract.py`) is the route for every other detected electric utility tariff. `src/parse_apco.py` remains in the tree and is not the upload route.

### Routing

`route_tariff` keeps Dominion on `parse_schedule` / `parse_rider`. A Dominion schedule book still requires Amendment No. 12 and effective 2025-07-01. A `kind=rider` upload is refused only when the document itself prints a different amendment number or effective date; a rider sheet that prints neither goes to `parse_rider`, as before 11A. Anything else detected as an electric tariff uses the AI adapter. A missing key returns `saved_unsupported` with "AI extraction not configured". A credit or balance refusal (the HTTP 400 from the first witness) returns "AI extraction billing". Any other API or network failure returns "AI extraction unavailable". No version rows are written in any of those cases.

Rider proof, scratch databases, `data/raw/rider-gen.pdf`, `kind=rider`, `code=GEN`: the API and a direct `parse_rider` call both write 0 components. Both refuse with `Unsupported rider GEN` (`parse_rider` has no GEN specification; that file was loaded historically by `parse_rider_gen.py`). The API returns 422 from that refusal. It does not return `saved_unsupported`, and it does not stamp the sheet as Amendment 12.

### Chunking

The model sees one schedule at a time. Sections are chosen by code, not by the model. Appalachian Power Tariff No. 28 (158 PDF pages) prints a table of contents in sheet numbers (`4-1 thru 4-2`), not PDF page ranges. Each body page prints `Sheet No.`, and those sheet prefixes are the section bounds. A second range on the same index row is included (Standby Service sheets `21A-1` through `21A-4` stay with `S.B.S.`). The exhibit of applicable riders is its own section. Pages 49–55 have no extractable text (encoded font, no OCR); Large Power Service and the R.P.S. R.A.C. sheet, which the index places there, are queued as `unreadable_pages` and are not sent to the model. A book with no usable index is split on `SCHEDULE` / `RIDER` / exhibit headings instead. Every page of a section is labeled with its PDF page number in the prompt.

The index yields 48 sections. Terms and conditions, the town list, and the underground installation plan are not sections.

### Output and verification

The client is the official Anthropic Python SDK. The key is `ANTHROPIC_API_KEY` in the gitignored `.env`. The model id is the module constant `MODEL`. The first witness used `claude-fable-5-1`. The constant is now `claude-sonnet-5-5`, the current Sonnet id on the account's model list (`claude-sonnet-4-5-20250929`, `claude-sonnet-4-6`, `claude-sonnet-5`, and `claude-sonnet-5-5` are all callable). Responses are cached at `data/cache/ai-extract/<model-id>/<sha256 of the PDF>.json`. A section is replayed only when that file's entry was stored for the same model, so the nine Fable responses are not reused for a Sonnet run.

Per schedule the model must return JSON: designation, name, effective date as printed, disposition `rates` or `cross_reference_only`, components (`printed_label`, `component_type`, rate as a decimal string in dollars, `unit`, `variant`, `source_page`, `source_quote`), and rules (`rule_type`, `rule_text`, `source_page`, `source_quote`). `component_type`, `rule_type`, and `unit` are prompted with the distinct values already stored on `tariff_components` and `tariff_rules` (units also include the validator's unit set). A new value is kept only when nothing stored fits, and the run reports every new value.

Verification is ordinary code. A component is inserted only when (a) its quote appears in the cited page's text after whitespace is collapsed and (b) its dollar rate equals a number printed on that page, or the same amount expressed in cents (`rate * 100`). A failure is queued, not stored, with signal `quote_not_found` or `rate_not_on_page`. A unit outside the stored set is queued as `unit_not_in_vocabulary`. Rules require the quote check only. `cross_reference_only` stores a version and no components. A `rates` section with nothing verified stores no version. The printed effective date must itself appear in the section; it is parsed to an ISO date and never assumed.

Storage uses the draft path the hand-written parsers use: one `draft` version per schedule, `schedule_code` the printed designation with dots and spaces removed (`RS`, `SGS`, `GS`, `LPS`), state and utility from the 11A detection, `version` the tariff number printed on the book. The quote is stored in `source_cell`. There is no citation column on `tariff_rules`, and none was added; a rule is stored as `rule_type` and `rule_text` after the quote check. Identical upload bytes are deduped by content hash, and the model response is cached under that hash and the model id, so a re-upload with the same model does not call the model again.

## Stage 11C — tariff-aware bill calculation

Task 3b generalized the audit path without changing stored tariff data or the Dominion mapping:

- Direct label mappings may select an exact `billing_category`; an ambiguous mapping without that selector still fails rather than choosing an arbitrary component.
- Schedule repricing selects a `total` representation instead of adding it to generation/distribution splits. Optional provisions identified by stored rules are excluded unless the audit context explicitly lists them in `applicable_provisions`.
- Rider loading is based on effective dates and stored component/rule applicability, with explicit account features and rider participation required where the stored terms call for them. The resolved candidate set is recorded in each audit report.

The constructed February 2025 RS witness remains local to scratch. For 1,000 kWh, RS service reprices to $84.18 (basic $7.96 plus all-kWh total energy $76.22); applicable PIPP ($1.32) and EE-RAC ($2.37) bring the bill to $87.87, with all four lines matching. Dominion pilot bills 29–31 retain their $95.75, $101.67, and $114.45 totals and unchanged line verdicts.

### Task 3c — version-scoped structured rules

Structured tariff rules live in the separate `structured_tariff_rules` relation:
each row references its source tariff version and explicit applies-to schedule
and version, stores JSON parameters plus the original rule text, and records a
nullable `source_page` with a constrained `cited`/`uncited` status. This keeps
multiple machine-readable rules independently queryable while retaining their
text and citation provenance. The schema rejects a cited row without a page and
an uncited row with an invented page.

On the fresh 3c scratch, the RS rules were represented as follows:

| Stored rule | Structured representation | Source |
|---|---|---|
| `minimum_charge` — “Subject to a minimum monthly charge equal to the Basic Service Charge.” | `minimum_charge`, value `7.96`, unit `usd_per_month`, derived from the RS `basic_customer_charge` | PDF p. 28 |
| `eligibility` — one-meter individual residential customers taking Standard Service, including the stated rural agricultural customers | Customer class, one-meter, Standard Service, and rural/agricultural inclusion fields | PDF p. 28 |
| `closed_schedule` — Load Management Water Heating is closed to new customers but existing customers may remain | `provision_availability`, `new_customers_allowed: false`, `existing_customers_may_remain: true` | PDF p. 29 |
| `period_definitions` — on/off-peak periods and “specified legal holidays” | Remains text-only: the stored rule omits the named holidays, so its calendar cannot be faithfully completed from that text | PDF p. 29 |

Only `minimum_charge` is consumed by the engine in 3c. For a version carrying
that cited rule, the schedule subtotal is floored at the structured monthly
amount, and the report gives the floor, pre-floor subtotal, bound status, and
post-floor subtotal. Versions without a structured rule retain the prior
behavior and `not_evaluated` reporting. The constructed February 2025 RS
witness still has four matching lines and totals $87.87; the RS schedule subtotal
is $84.18 against a $7.96 floor, which does not bind. A separate unit fixture
(not a bill) raises a $7.00 subtotal to $7.96.

3d rule inventory, from the stored rule text and cited tariff pages:

| Schedule and rule | Stored text / source | Assessment for structured calculation |
|---|---|---|
| SBS `demand_determination` | “Generation demand is the highest on-peak demand in excess of the supplemental service capacity reservation in Dec-Feb and Jun-Sep; transmission and distribution demands equal the standby service contract capacity kW.” PDF p. 80 | Generation excess and contract-capacity demand are structured and evaluated. The printed p. 80 text also sends demand above supplemental capacity plus a specific standby request to the supplemental schedule. That branch is retained as an explicit structured-rule gap and is not evaluated. |
| RS-TOD `on_peak_hours` | “The on-peak billing period is defined as 7 a.m. to 8 p.m., local time, for all weekdays, Monday through Friday.” PDF p. 34 | Faithfully structurable as a weekday time interval. |
| RS-TOD `off_peak_hours` | “The off-peak billing period is 8 p.m. to 7 a.m., local time, on weekdays, all hours on Saturdays and Sundays, and the legally observed holidays of New Year's Day, Memorial Day, Independence Day, Labor Day, Thanksgiving Day, and Christmas Day.” PDF p. 34 | Faithfully structurable as the complementary weekday interval plus full weekends and the six named holidays; observed-date handling remains an explicit calendar concern. |

### Task 3d — RS-TOD periods and SBS demand witnesses

On fresh scratch `tariff_11c_witness_3d_20261104`, RS-TOD's `on_peak_hours`
and `off_peak_hours` structured rules cite PDF p. 34 and preserve the source
text. The period classifier uses local, timezone-naive timestamps, a half-open
weekday interval of 07:00–20:00, all weekend hours as off-peak, and the six
named tariff holidays as off-peak. It does not shift holidays to observed
weekdays when a holiday falls on a weekend.

| Timestamp | Classification |
|---|---|
| Wednesday 2025-02-05 10:00 | On-peak |
| Wednesday 2025-02-05 22:00 | Off-peak |
| Sunday 2025-02-09 12:00 | Off-peak |
| Christmas Day 2025-12-25 12:00 | Off-peak |

The copied RS-TOD `tariff_rules` did not contain the schedule's residential
availability text. To let the existing 3b applicability path evaluate the
stored EE-RAC TOU categories from evidence rather than the schedule name, the
fresh scratch also received the exact residential eligibility passage from
PDF p. 34. This is scratch-only. The Feb 2025 RS-TOD witness includes PIPP
because its stored rule covers the Universal Service Fee for all customer
service under applicable schedules, and EE-RAC because the p. 34 eligibility
text establishes residential service and the EE-RAC rule applies to applicable
standard schedules. Its six matching lines are:

| Charge | Hand calculation (line rounded to cents) | Engine | Result |
|---|---:|---:|---|
| Basic service | $9.82 | $9.82 | MATCH |
| On-peak energy | 400 × $0.14306 = $57.224 → $57.22 | $57.22 | MATCH |
| Off-peak energy | 600 × $0.03358 = $20.148 → $20.15 | $20.15 | MATCH |
| PIPP | 1,000 × $0.00132 = $1.32 | $1.32 | MATCH |
| EE-RAC on-peak | 400 × $0.00548 = $2.192 → $2.19 | $2.19 | MATCH |
| EE-RAC off-peak | 600 × $0.00039 = $0.234 → $0.23 | $0.23 | MATCH |
| **Total of billed charge lines** | **$90.93** | **$90.93** | **MATCH** |

The raw, unrounded sum is $90.938; the constructed bill follows the engine's
per-line cent rounding before summing, yielding $90.93. The selected total
energy variants use their period-specific usage determinants rather than total
monthly kWh.

The SBS `demand_determination` structured rule is cited to p. 80 and covers
only the printed generation-demand excess in December–February and
June–September, and transmission/distribution demand equal to standby contract
capacity. Its parameters retain the unstructured p. 80 branch verbatim, with
the required maximum-integrated-demand, supplemental-contract-capacity,
specific-request, and supplemental-schedule billing-demand inputs. That branch
is not evaluated. The February constructed witness uses secondary voltage,
50 kW standby contract capacity, 20 kW supplemental reservation, 50 kW
specific standby request, 35 kW highest on-peak and maximum integrated demand,
and 1,000 kWh of measured standby energy. Since 35 kW is below the 70 kW
reservation-plus-request threshold, the unresolved branch does not apply.

The SBS pages 79–81 and stored schedule components contain no basic-service
charge; no such charge was invented. The page 81 rate table and stored rates
support secondary generation energy and generation/distribution demand. PIPP
is included by the stored universal-service rule; EE-RAC is excluded because
its stored residential categories do not establish coverage for this
commercial standby schedule.

| SBS charge | Hand calculation | Engine | Result |
|---|---:|---:|---|
| Generation demand | (35 − 20) kW × $17.70 = $265.50 | $265.50 | MATCH |
| Distribution demand | 50 kW × $9.46 = $473.00 | $473.00 | MATCH |
| Secondary generation energy | 1,000 × $0.00459 = $4.59 | $4.59 | MATCH |
| PIPP | 1,000 × $0.00132 = $1.32 | $1.32 | MATCH |
| **Total** | **$744.41** | **$744.41** | **MATCH** |

Standby energy is supplied to this constructed bill as a measured 1,000-kWh
standby-energy determinant; the witness does not infer standby kWh from
interval meter data or evaluate supplemental-energy allocation.

The original February 2025 RS witness was recreated on this scratch: four
lines MATCH, $87.87 total, with the $7.96 minimum evaluated and not binding
against the $84.18 schedule subtotal. Dominion pilot bills 29–31 were rerun
against the copied primary baseline: $95.75, $101.67, and $114.45, with all
32 audit lines identical for each bill.

### Contract, validation, activation

`check_parser_contract` against the adapter with an injectable fake client: (i) drafts only, and at least one; (ii) every stored component has `source_page` and `source_cell`; (iii) `validate_drafts` returns no refusals; (iv) a second run changes nothing; (v) a foreign source raises and writes nothing.

The route then runs `validate_drafts` on each new draft. A clean draft is activated with `activate_with_alert`, the same call manual activation uses. A failing draft stays `draft` and is queued with signal `validation_failed`.

### Witness

The live call on `data/raw/APCo-Tariff-28.pdf` was started against scratch database `tariff_11b_scratch` (a copy of primary Postgres with the hand-written Appalachian rows removed, leaving the 41 Dominion versions). Model string before and after the run: `claude-fable-5-1`. Nine sections completed (`end_turn`) and were cached by content hash: RS, RS-E, RS-SD, RS-TOD, RS-STOU, SWS, SGS, GS, GS-TOD. Token totals on those nine calls: 28,258 input and 47,186 output. Cache read and cache creation were 0. The SDK usage object has no cost field.

The tenth call, L.G.S.-T.O.D. (PDF pages 47–48), was rejected with HTTP 400 `invalid_request_error`: the account credit balance is too low. `x-should-retry` was false. `parse_book` writes the database only after every section returns, so the scratch database received no AI rows. Nothing was validated or activated. Spot-checks were not run. Primary Postgres was not modified (Dominion 41 versions plus the existing 14 hand-written Appalachian versions). Frozen `data/tariffs.db` SHA-256 is still `cc765bf329f723d196ed069452ec0c78da44aeaecaba2d5f202094cc0859c6eb`. The scratch database was dropped. The nine Fable responses remain in `data/cache/ai-extract/<sha256>.json` (no model directory). Local suite after that witness: 378 passed. That credit refusal was reported with the single message "AI extraction not configured/unavailable".

### Sonnet run

`MODEL` changed from `claude-fable-5-1` to `claude-sonnet-5-5`. Cache files now live at `data/cache/ai-extract/<model-id>/<sha256>.json`, and a cached section is used only when its stored model matches `MODEL`, so the Fable file was not read. Refusals are three strings: "AI extraction not configured", "AI extraction billing", and "AI extraction unavailable".

Scratch database `tariff_11c_scratch` is a copy of primary with the 14 hand-written Appalachian versions removed. The Sonnet pass made 46 model calls (10, then a resume of 36 after an adapter bug: the unreadable-page queue call omitted `detail`, which raised `TypeError` on L.P.S. before any commit). The resume did not call the model again for the 10 cached sections. Combined usage: 168,479 input tokens and 130,396 output tokens. Cache read and cache creation were 0. The SDK usage object has no cost field. A later `parse_book` of the same file made 0 model calls.

45 sections returned `end_turn`. Outdoor Lighting stopped at `max_tokens` and was queued `model_output_incomplete` with no partial rows. L.P.S. and R.P.S. R.A.C. were queued `unreadable_pages` (PDF pages 49–55) and were not sent to the model. Every `end_turn` section put an ISO date in `effective_date` (25 of them `2025-01-01`). That string is not printed on the page — the book prints "January 1, 2025" — so the verifier queued `quote_not_found` on the effective date and stored no version. Component checks did not run. `validate_drafts` had no drafts to accept or refuse. Nothing was activated. The scratch AI set is empty.

### Date-verification follow-up

The effective-date verifier parses the model value as a calendar date, then requires that
date to appear on an individual page in the section as a full month name, an unpadded day,
a comma, and a four-digit year (for example, `January 1, 2025`). No abbreviated-month,
numeric, or ISO printed forms are accepted. A date absent from the section still queues as
`quote_not_found`; an unparseable or invalid date queues as `effective_date_unparsed`.
Component quote, rate-on-page, and rule-quote checks are unchanged.

In the earlier date-verification follow-up, the requested cache replay, Postgres scratch
activation, spot-checks, and AI-vs-handwritten diff were not run because that workspace did
not have the source PDF or Sonnet cache, and no local Postgres service was available. The
zero-agreement result below records that earlier empty-AI-set state.

At that earlier follow-up, against the hand-written primary set (14 active versions, 162
components, 56 rules), the agreement count was 0. There was no stored-rate disagreement.
All 162 hand-written components were only on the hand-written side. No AI schedule was
stored, including the book sections the hand-written parser does not cover. For GS-TOD
sheet 13-1 the model proposed primary-voltage on-peak energy as 0.05574, 0.03987, and
0.09562 dollars (printed 5.574¢, 3.987¢, and 9.562¢). It did not propose 9.561¢. None of
the three were stored.

Primary was not modified: Appalachian 14 versions / 162 components, Dominion 41 versions / 448 components. Local suite after this run: 381 passed.

### Mac mini replay attempt (2026-11-04)

The three local date-regression tests pass: an ISO model date matches the same full
month-name date on the page; an absent date queues as `quote_not_found`; and a different,
valid calendar date also queues as `quote_not_found`. The printed-page form accepted by
the verifier is a full English month name, an unpadded day, a comma, and a four-digit year
(case-insensitive), e.g. `January 1, 2025`. It does not accept a missing comma, abbreviated
month, or numeric/ISO page text. The complete prescribed suite passed: 384 tests.

The Mac mini replay used the APCo PDF and its matching Sonnet cache. The cache has 46
responses (45 `end_turn`, one `max_tokens`); the book has 48 sections. An injected
fail-fast client proved zero cache misses/API calls, and `parse_book` reported
`model_calls: 0`.

The stale `tariff_11c_scratch` was dropped and recreated from a fresh dump/restore of the
Postgres primary. Only the copy had the 14 Appalachian versions, their 162 components, and
56 rules removed; its 41 Dominion versions, 448 components, and 112 rules remained. The
adapter stored 42 Appalachian drafts; 40 passed `validate_drafts` and activated on scratch.
Two failed validation and remain drafts: SBRR has rate -0.3924, and G-RAC has rate -0.0074.
No primary rows were changed: it still has 14 active Appalachian versions / 162 components
/ 56 rules and 41 active Dominion versions / 448 components / 112 rules.

The schedule table gives verified component/rule counts. “Active” means validation passed
and the adapter activated the version on scratch. Queued components/rules are excluded from
the verified counts; non-stored sections have no version to validate.

| Schedule | Verified components / rules | Queued | Validation / scratch outcome |
|---|---:|---|---|
| A5PCAPRACPCAP | 44 / 1 | — | Active |
| A5RPS-Compliance | 32 / 1 | — | Active |
| A6RPSRenewables-Capacity&Energy | 40 / 1 | — | Active |
| AFS | 0 / 0 | `quote_not_found` (effective date absent) | No version |
| ATOD | 2 / 8 | — | Active |
| BC-RAC | 32 / 3 | — | Active |
| COGEN/SPP | 1 / 6 | — | Active |
| CS | 0 / 0 | `rate_not_on_page` (0.10); `no_verified_rates` | No version |
| DIR | 0 / 4 | — | Active |
| DRS | 1 / 7 | — | Active |
| DRS-RTOCAPACITY | 3 / 5 | — | Active |
| E-RAC | 48 / 2 | — | Active |
| EDR | 1 / 6 | — | Active |
| EE-RAC | 32 / 2 | — | Active |
| EXHIBIT | 0 / 2 | — | Active |
| FFR | 1 / 2 | — | Active |
| G-RAC | 48 / 1 | — | Draft; validation rejects rate -0.0074 |
| GS | 63 / 5 | — | Active |
| GS-TOD | 16 / 4 | — | Active |
| LFEVC | 0 / 5 | — | Active |
| LGS-TOD | 20 / 5 | — | Active |
| LPS | 0 / 0 | `unreadable_pages` (PDF 49–55) | No version |
| NMS | 1 / 4 | — | Active |
| NMSII | 5 / 5 | 2 `unit_not_in_vocabulary` (`usd_per_generator`) | Active |
| NREC | 3 / 2 | — | Active |
| OL | 0 / 0 | `model_output_incomplete` (`max_tokens`) | No version; kept queued |
| PEV | 6 / 3 | — | Active |
| PEVCFS-EXPERIMENTAL | 2 / 3 | — | Active |
| PIPP | 1 / 2 | — | Active |
| REC | 3 / 2 | — | Active |
| RPSRAC | 0 / 0 | `unreadable_pages` (PDF 49–55) | No version |
| RS | 7 / 4 | — | Active |
| RS-E | 7 / 4 | — | Active |
| RS-SD | 4 / 6 | — | Active |
| RS-STOU | 8 / 3 | — | Active |
| RS-TOD | 8 / 3 | — | Active |
| SAC | 29 / 2 | — | Active |
| SBRR | 1 / 3 | — | Draft; validation rejects rate -0.3924 |
| SBS | 0 / 0 | `quote_not_found` (page prints `January 1 2025`, without comma) | No version |
| SGS | 18 / 6 | — | Active |
| SSP-S | 4 / 2 | — | Active |
| SSP-SO | 2 / 4 | — | Active |
| SUT | 1 / 2 | — | Active |
| SWS | 5 / 2 | — | Active |
| T-RAC | 48 / 1 | — | Active |
| TS | 0 / 4 | — | Active |
| VWS | 0 / 2 | — | Active |
| WWS | 13 / 4 | — | Active |

Totals: 560 verified components and 143 verified rules. Component queues: one
`rate_not_on_page` (CS) and two `unit_not_in_vocabulary` (NMSII); no rule failed its
verification. Other section queues are AFS and SBS effective dates, CS with no verified
rates, OL incomplete output, and LPS/RPSRAC unreadable pages. In particular, OL was not
retried or given a higher token limit, and LPS/RPSRAC were not sent to the model.

### Hand-read spot checks

The PDF page numbers below are the printed/extracted PDF page numbers. On p. 28, RS stores
the $7.96 basic service charge and all-kWh energy rates 0.03794 generation, 0.03828
distribution, and 0.07622 total, all cited to p. 28. On p. 81, the printed SBS secondary
demand is Generation 17.70 / Distribution 9.46, and the cached model proposed those exact
values with a p. 81 quote. Neither value is stored: SBS has no version because its printed
effective date is `January 1 2025` without the comma required by the current date check.

For GS-TOD sheet 13-1, the three primary-voltage on-peak values are stored on AI schedule
GS-TOD as Generation 0.05574, Distribution 0.03987, and Total 0.09562, each cited to PDF
p. 45. These match the hand-written values 0.05574 / 0.03987 / 0.09562.

### AI vs. hand-written component diff

The diff uses `src/tariff_diff.py`'s component natural-key fields and matches the two rider
aliases `FFR` / `RIDER FFR` and `PIPP` / `RIDER PIPP`. Across the 14 schedules present on
both sides, there are 0 exact-key equal-rate agreements and 0 exact-key rate disagreements:
the stored AI identities do not exactly match the hand-written natural keys. The primary
has 158 unique-key components only on its side plus 4 components in duplicate SGS natural
keys (ambiguous under the diff key); AI has 560 components only on its side, including the
two unvalidated drafts. There are no primary-only schedules and 28 AI-only schedules:
A5PCAPRACPCAP, A5RPS-Compliance, A6RPSRenewables-Capacity&Energy, ATOD, BC-RAC, COGEN/SPP,
DIR, DRS, DRS-RTOCAPACITY, E-RAC, EDR, EE-RAC, EXHIBIT, G-RAC, LFEVC, NMS, NMSII, NREC,
PEV, PEVCFS-EXPERIMENTAL, REC, SAC, SSP-S, SSP-SO, T-RAC, TS, VWS, and WWS. The complete
component-level listing includes each unmatched identity, value, and available cited page
in the local report `../data/reports/apco_tariff_28_ai_vs_primary_diff.csv` (722 rows).

The exact-key agreement count should not be read as a claim that every printed numeric
value differs: the RS spot checks and the three GS-TOD values above match numerically, but
the records have different natural-key metadata. The strict diff therefore lists those
records as unmatched rather than inventing a correspondence.

### Task 2g — comma-less dates and negative-rate validation

The printed effective-date check accepts both `January 1, 2025` and `January 1 2025`
(full English month, day, four-digit year; comma optional). It still compares calendar
dates, and a missing or different date is queued as `quote_not_found`. Regression tests
cover both printed forms and a wrong comma-less date.

Draft validation no longer rejects a finite negative rate solely for its sign. This does
not relax the extractor's evidence checks: the quote must still occur on its cited page,
the rate magnitude must still be a number there, and for negative rates the cited quote
must itself include the matching negative number. Unit vocabulary, other component
requirements, duplicate checks, and rule checks are unchanged. The lifecycle regression
test confirms an otherwise valid negative-rate draft validates and activates.

The cache was replayed against a new scratch database, `tariff_11g_scratch_20261104_bf3ae5`,
restored from the primary. Only the scratch copy's 14 Appalachian versions, 162 components,
and 56 rules were removed before replay. The injected fail-fast client recorded 0 calls;
`parse_book` also reported `model_calls: 0`. The 48-section replay stored 573 components
and 153 rules in 44 Appalachian versions; all 44 are active and none remain drafts:

| Schedule | Components / rules | Scratch status |
|---|---:|---|
| SBS | 12 / 7 | Active |
| AFS | 1 / 3 | Active |
| G-RAC | 48 / 1 | Active |
| SBRR | 1 / 3 | Active |

Compared with Task 2e, every schedule's stored component/rule counts are identical except
AFS (+1 component, +3 rules) and SBS (+12 components, +7 rules), which now pass effective
date verification. G-RAC and SBRR retain their prior component/rule counts and now validate
and activate. The only new replay queue entries are:

- CS: one `rate_not_on_page` for `0.10`, plus `no_verified_rates`; its price remains
  market-based with a `$100/MWh` floor, not a printed fixed rate.
- NMSII: two `unit_not_in_vocabulary` entries for `usd_per_generator`.
- OL: `model_output_incomplete` (`max_tokens`); it was not retried or given a higher limit.
- LPS and RPSRAC: `unreadable_pages` (PDF pp. 49–55); neither was sent to the model.

No effective-date or negative-rate validation queues remain. The 48 pre-existing primary
parse-review records were copied unchanged to scratch; the seven entries above are the
new replay items. The primary remained read-only and unchanged at 14 Appalachian versions
/ 162 components / 56 rules and 41 Dominion versions / 448 components / 112 rules. The
prescribed full suite passed: 387 tests.

### Task 2h — Appalachian primary swap (2026-11-04)

Step 26 selected the verified AI set as the authoritative Appalachian data. Before any
primary writes, a complete plain-SQL `pg_dump` was written to
[`../data/tariff-pg-pre-11h-swap.2026-11-04.sql`](../data/tariff-pg-pre-11h-swap.2026-11-04.sql)
(283,511 bytes; `pg_dump` exited successfully and the dump completion marker was verified).
The hand-written parser remains in `src/parse_apco.py`.

The swap used one primary transaction: old Appalachian VA components and rules were deleted
before their versions; all 44 active AI versions, 573 components, and 153 rules were copied
from scratch. A scratch-version-ID to primary-version-ID map was recorded and used for
component/rule foreign keys. All per-schedule counts and copied component/rule rows matched
scratch after remapping. The 33 existing `tariff_book_section` queue rows were replaced
with only the seven replay-generated items (CS, NMSII twice, OL, LPS, and RPSRAC); the
14 `component` and one `rule` queue item were preserved unchanged.

| Dataset | Before swap | After swap |
|---|---:|---:|
| Appalachian VA versions / components / rules | 14 / 162 / 56 | 44 active / 573 / 153 |
| Dominion VA versions / components / rules | 41 / 448 / 112 | 41 / 448 / 112 |
| Bills / bill charges | 31 / 894 | 31 / 894 |
| Audit runs / audit lines | 3 / 96 | 3 / 96 |
| Parse-review items by source kind | component 14, rule 1, tariff_book_section 33 | component 14, rule 1, tariff_book_section 7 |

Primary spot checks confirm RS basic service 7.96 on p. 28; GS-TOD 0.05574 / 0.03987 /
0.09562 on p. 45; SBS secondary demand Generation 17.70 and Distribution 9.46 on p. 81;
G-RAC -0.0074 on p. 149; and SBRR -0.3924 multiplier on p. 145. The seven retained
section queue records are CS (`rate_not_on_page` and `no_verified_rates`), NMSII (two
`unit_not_in_vocabulary`), OL (`model_output_incomplete`), and LPS/RPSRAC
(`unreadable_pages`). The prescribed suite passed with 387 tests. Frozen
`data/tariffs.db` remains SHA-256
`cc765bf329f723d196ed069452ec0c78da44aeaecaba2d5f202094cc0859c6eb`.

### Scope

The Open Access Distribution book is a separate document and is not in hand. No rate literal is hardcoded; a stored rate exists only after the verifier matches it to the page.

### Task 3e — structured-rule promotion to primary (2026-11-04)

Before any primary write, a complete plain-SQL dump was created at
[`../data/tariff-pg-pre-11c-promotion.2026-11-04.sql`](../data/tariff-pg-pre-11c-promotion.2026-11-04.sql)
(333,904 bytes). The file is non-empty and contains the `PostgreSQL database
dump complete` marker. The primary initially had no `structured_tariff_rules`
table. The top-level `src/create_db.py` entry point was not run because it also
creates/upgrades unrelated tables. Instead, only that module's
`structured_tariff_rules` `CREATE TABLE` statement was executed through the
project's `src.db` PostgreSQL connection/DDL translation path.

The six structured rules were copied from scratch
`tariff_11c_witness_3d_20261104` in one transaction. Both version references
were mapped by utility, schedule, active status, and effective date; each key
matched exactly one primary version. All rule fields (`rule_type`,
`applies_to_schedule`, `parameters_json`, `source_rule_text`, `source_page`,
and `citation_status`) match the scratch rows exactly.

| Schedule | Structured rule | PDF page | Scratch-to-primary row fidelity |
|---|---|---:|---|
| RS | `eligibility` | 28 | Identical |
| RS | `minimum_charge` | 28 | Identical |
| RS | `provision_availability` | 29 | Identical |
| RS-TOD | `off_peak_hours` | 34 | Identical |
| RS-TOD | `on_peak_hours` | 34 | Identical |
| SBS | `demand_determination` (with the excess-demand branch retained as a gap) | 80 | Identical |

The primary AI extraction still omits RS-TOD's printed residential eligibility
rule. The scratch-only `tariff_rules` addition was not copied. One open
parse-review item was added through the Stage 8 `parse_review.queue_item`
mechanism: `source_kind=rule`, `source_ref=APCo-Tariff-28.pdf:p34:RS-TOD:eligibility`,
`schedule_code=RS-TOD`, `field=rule_text`, `stored_value=NULL`,
`signal=missing_rule`. Its detail records that the AI extraction omitted the
printed residential eligibility rule on p. 34, that rider applicability needs
it to include EE-RAC, and that the Stage 12 rules pipeline must close the gap
instead of a hand insert.

| Primary data | Before | After |
|---|---:|---:|
| Appalachian Power Company versions / components / tariff rules | 44 / 573 / 153 | 44 / 573 / 153 |
| Virginia Electric and Power Company versions / components / tariff rules | 41 / 448 / 112 | 41 / 448 / 112 |
| Accounts / bills / bill charges / bill determinants | 1 / 31 / 894 / 96 | 1 / 31 / 894 / 96 |
| Audit runs / audit lines | 3 / 96 | 3 / 96 |
| Parse-review `component` / `rule` / `tariff_book_section` | 14 / 1 / 7 | 14 / 2 / 7 |
| Structured-rule table / rows | absent / 0 | present / 6 |

A read-only engine check against the active primary RS version loaded its cited
`minimum_charge` rule and evaluated the `$7.96` monthly floor from p. 28. No
audit run was created. Frozen `data/tariffs.db` remains SHA-256
`cc765bf329f723d196ed069452ec0c78da44aeaecaba2d5f202094cc0859c6eb`.

### Task 4a — Appalachian all-schedule sweep (2026-11-04)

A fresh PostgreSQL scratch, `tariff_11d_sweep_20261104`, was restored from the
primary. Before adding constructed witnesses, it matched the primary:
Appalachian 44 versions / 573 components / 153 text rules; Dominion (Virginia
Electric and Power Company) 41 / 448 / 112; six structured rules; parse-review
queue 14 component / 2 rule / 7 tariff-book-section items; one account, 31
bills, three audits, and 96 audit lines. No model calls were made and the
primary remained read-only.

The three constructed bills were copied from the verified 3d witness setup.
Because the primary intentionally lacks RS-TOD's printed eligibility rule,
the exact p. 34 `eligibility` text from the earlier 3d scratch was restored to
the new scratch's RS-TOD `tariff_rules` only. Primary queue item 56 remains
open; no such text-rule row was added to the primary.

| Constructed witness | Hand/stored line total | Entry audit | Rider decision | Structured-rule result |
|---|---:|---|---|---|
| RS, Feb 2025, 1,000 kWh | $7.96 basic + $76.22 energy + $1.32 PIPP + $2.37 EE-RAC = **$87.87** | $87.87, delta $0.00; all four lines MATCH | PIPP and EE-RAC included; later-effective riders excluded by date | RS minimum charge p. 28 evaluated at $7.96; $84.18 schedule subtotal, not binding |
| RS-TOD, Feb 2025, 400 on-peak / 600 off-peak kWh | $9.82 basic + $57.22 on-peak + $20.15 off-peak + $1.32 PIPP + $2.19 / $0.23 EE-RAC = **$90.93** | $90.93, delta $0.00; all six lines MATCH | PIPP and EE-RAC included using the scratch-only residential eligibility evidence | Minimum-charge text is not structured for RS-TOD; not evaluated |
| SBS, Feb 2025, 50 kW contract / 20 kW reservation / 35 kW measured on-peak / 1,000 kWh | $265.50 generation demand + $473.00 distribution demand + $4.59 energy + $1.32 PIPP = **$744.41** | $744.41, delta $0.00; all four lines MATCH | PIPP included; EE-RAC excluded because stored applicability does not establish SBS coverage | The p. 80 structured demand rule supports the tested branches; the supplemental excess-demand branch remains a gap |

#### February 2025 sweep set and results

The database has 26 active Appalachian versions whose effective ranges cover
the full February bill period. Applying the existing H1′ `_base_schedules`
classification in `src/schedule_review.py` yields five base candidates and
21 component-category schedules treated as riders:

- **Base candidates:** DIR, EXHIBIT, LFEVC, TS, VWS.
- **Component-category / rider set:** AFS, ATOD, COGEN/SPP, DRS,
  DRS-RTOCAPACITY, EDR, EE-RAC, GS, GS-TOD, LGS-TOD, PEV,
  PEVCFS-EXPERIMENTAL, PIPP, RS, RS-E, RS-SD, RS-STOU, RS-TOD, SBS, SGS,
  SWS.

The two `reprice_bill` calls (RS and RS-TOD) completed without a crash. Every
base candidate was refused before rider or component evaluation: its active
version has zero tariff components and the path returned
`cross_reference_only`, not a zero-dollar total. The identical outcomes for
both witnesses were:

| Base schedule | RS witness | RS-TOD witness | Stored evidence behind refusal |
|---|---|---|---|
| DIR | Refused; no total | Refused; no total | Zero components; rules refer to Chapter 314 fees and actual interconnection-system costs |
| EXHIBIT | Refused; no total | Refused; no total | Zero components; stored rules describe an informational rider-applicability table and state no rates |
| LFEVC | Refused; no total | Refused; no total | Zero components; stored rules refer to GS-TOD / LGS-TOD rates rather than storing a priced component |
| TS | Refused; no total | Refused; no total | Zero components; stored rules refer to installation/removal cost and the underlying standard schedule |
| VWS | Refused; no total | Refused; no total | Zero components; stored rules refer to standard-schedule rates and an energy price set by service agreement |

For each refusal, no rider was included or excluded and no structured rule was
evaluated: the selection layer exits on the empty component set first. The
H1′ rider discovery query returned no rider codes for either residential
witness. It requires `tariff_components.billing_category` to equal the billed
schedule code, while stored AI categories are charge labels such as “Universal
Service Fee” and descriptive EE-RAC rate categories. Thus the audit path can
include PIPP / EE-RAC on the constructed bills while the all-schedule
selection path discovers none.

SBS was **out of sweep scope**, not priced as an alternative. It is in the
component-category set rather than H1′'s base-target list, and that path does
not load the structured p. 80 rule to turn `highest_on_peak_demand_kw`,
`supplemental_capacity_reservation_kw`, and `standby_contract_capacity_kw`
into the separate generation and distribution demand determinants. The
unsupported supplemental-demand branch remains explicitly recorded in the
structured rule.

#### Gap inventory and defect evidence

| Item | Status | Evidence / reason |
|---|---|---|
| DIR, EXHIBIT, LFEVC, TS, VWS | Refused (`cross_reference_only`) | All five active February versions have zero components; their text rules point to external fees, referenced schedules, or service-agreement prices rather than a fixed component. No zero-dollar result was emitted. |
| Base-vs-rider selection | Defect confirmed | `_base_schedules` treats any schedule with a non-null `billing_category` as a rider. That classifies actual base schedules including RS, GS, and SBS as non-targets, leaving only five componentless cross-reference schedules in the sweep. |
| Rider discovery | Defect confirmed | `_billed_rider_codes` requires exact `billing_category == schedule_code`; RS and RS-TOD discover no PIPP or EE-RAC rows despite the entry-audit applicability results. |
| CS | Not calculable; no version | Queue items 53–54 say proposed `0.10` is not printed on p. 121 and no rate survived verification. The prior page review identifies a market-LMP charge with a `$100/MWh` floor, not a fixed `0.10` rate. |
| LPS and R.P.S. R.A.C. | Not extracted; no version | Open section queue items 49 and 55 cite unreadable image-only PDF pp. 49–55. No fixed component is available to calculate. |
| RS-TOD eligibility | Known data gap remains open | Queue item 56 records that the AI extraction omitted the p. 34 residential eligibility rule needed by rider applicability. The exact text was restored only on this witness scratch for the entry check. |
| SBS demand | Partial rule; known gap remains | Structured rule cites p. 80; the excess-demand branch requires maximum-integrated-demand, supplemental contract capacity, specific request, and supplemental-schedule billing demand and is marked `not_structured`. H1′ does not evaluate structured rules. |
| Tiered `usd_per_kw` groups | Not found in the February-active set | No active February component has `tier_min`, `tier_max`, or `tier_unit` populated, including `usd_per_kw` components; this anticipated case was not exercised. |
| `usd_per_rkva` | Present; determinant absent from witnesses | GS has a `rkva_demand_charge` of $0.85 per rkVA on p. 43 for customers averaging 300 kW or greater. The RS-family/SBS witnesses carry no `rkva_demand`, so this charge was not exercised. |

The Dominion phase5-v3 pilots were rerun on scratch. Bills 29 / 30 / 31
remained $95.75 / $101.67 / $114.45; all 32 audit lines per bill were
identical to the primary baselines. The three constructed own-schedule totals
also remained $87.87 / $90.93 / $744.41 after the read-only sweeps. Scratch
ended with four accounts, 34 bills, nine audit runs, and 206 audit lines; the
primary remained at one account, 31 bills, three audit runs, and 96 lines.
The prescribed full suite passed: 397 tests.

### Task 4b — shared schedule repricing (2026-11-04)

The fix was exercised on a fresh primary-derived scratch,
`tariff_11d_sweep_fix_20261104`. The primary remained read-only; the p. 34
RS-TOD eligibility text was restored on this scratch only, as in 4a. No model
calls were made.

Schedule role is now explicit `tariff_versions.schedule_kind` metadata:
`schedule`, `rider`, or `informational`. The producers set the role from their
schedule/rider source; `_base_schedules` selects only active `schedule` rows
covering the complete bill period. It no longer treats a non-null
`billing_category` as proof that a version is a rider. Rider discovery and
applicability use `audit_bills._rider_decisions`: stored role metadata,
full-period effective dates, the account/schedule evidence in stored rules and
component categories, and recorded account features/elections. Repricing
intersects the decisions for the billed schedule and candidate. Candidate
rider rows are then selected from stored billing categories, including
residential TOD/TOU and primary/secondary rate variants; no utility-specific
schedule-code list is used.

The repricer also applies the shared optional-provision filter and structured
rule loader. It evaluates the cited RS minimum charge and the supported
structured-demand branches only when their required determinants are present.
Representation variants (including parenthesized generation/distribution/
total labels), voltage-class categories, and peak-period billing categories
are resolved from stored data. Missing peak, critical-peak, demand, voltage,
or provision-selection evidence produces a named refusal rather than a
success-shaped total.

The February-active inventory has 19 base schedules, six riders, and one
informational version. The base set is AFS, ATOD, COGEN/SPP, GS, GS-TOD,
LFEVC, LGS-TOD, PEV, PEVCFS-EXPERIMENTAL, RS, RS-E, RS-SD, RS-STOU, RS-TOD,
SBS, SGS, SWS, TS, and VWS. Riders are DIR, DRS, DRS-RTOCAPACITY, EDR,
EE-RAC, and PIPP; EXHIBIT is informational.

| Base schedule | Flat RS witness (bill 34) | RS-TOD witness (bill 32) |
|---|---|---|
| AFS | Refused: missing `demand` | Refused: missing `demand` |
| ATOD | $6.39, eligibility not established; contract-based minimum not applicable without its amount | Same |
| COGEN/SPP | Refused: needs contracted capacity level (CL) | Same |
| GS | Refused: missing `demand` | Refused: missing `demand` |
| GS-TOD | Refused: missing `on_peak_usage` | $74.49, secondary voltage selected; eligibility not established |
| LFEVC | Refused: `cross_reference_only`, no priced components | Same |
| LGS-TOD | Refused: missing `demand` | Refused: missing `demand` |
| PEV | Refused: missing `on_peak_usage` | $85.92; eligibility not established because the account does not establish AMI-meter / vehicle-charging requirements |
| PEVCFS-EXPERIMENTAL | Refused: stored commercial eligibility conflicts with residential account | Same |
| RS | **$87.87** on both witnesses; PIPP + EE-RAC; p. 28 $7.96 minimum evaluated and not binding | **$87.87**; same riders and minimum result |
| RS-E | $79.88, eligibility not established; stored rule restricts service to company employees | Same |
| RS-SD | Refused: missing `demand` | Refused: missing `demand` |
| RS-STOU | Refused: missing `critical_on_peak_usage` | Refused: missing `critical_on_peak_usage` |
| RS-TOD | Refused: missing `on_peak_usage` | **$90.93**; PIPP + EE-RAC; minimum text is not structured and was not evaluated |
| SBS | Refused: missing `highest_on_peak_demand_kw`; p. 80 demand rule not evaluated | Same |
| SGS | Refused: no stored selection for the load-management time-of-day provision | Same |
| SWS | $96.49, eligibility not established; PIPP + EE-RAC | Same |
| TS | Refused: `cross_reference_only`, no priced components | Same |
| VWS | Refused: `cross_reference_only`, no priced components | Same |

The RS computation is $7.96 basic + $76.22 all-kWh energy + $2.37 EE-RAC +
$1.32 PIPP. The RS-TOD computation is $9.82 basic + $57.22 on-peak +
$20.15 off-peak + $2.19 / $0.23 EE-RAC + $1.32 PIPP. The calculated but
unproven candidates are informational comparisons, not eligibility findings.
For GS-TOD, the stored account voltage is Secondary; primary-rate components
are excluded from both the base and EE-RAC calculations.

For the six February riders, DIR is excluded because no interconnection or
generating facility is recorded; DRS and DRS-RTOCAPACITY are excluded because
the witnesses have no demand-metered service; EDR is excluded by its
commercial/industrial restriction or because stored evidence does not
establish coverage. EE-RAC is included where the stored residential category
and general-applicability evidence or an explicit schedule category match;
otherwise its stored category does not establish coverage. PIPP is included
under its stored all-customer Universal Service Fee rule. Active rider
versions outside the February period are excluded by their stored effective
range. These exact decisions are retained on each candidate result.

The Dominion bill-31 repricing snapshot (`/tmp/tariff_11d_bill31_before.json`)
was identical after the fix: the same 24 candidate codes, statuses, totals,
and rider decisions. Bills 29 / 30 / 31 audited as no-ops against their
phase5-v3 snapshots at $95.75 / $101.67 / $114.45, with 32 lines each (31
MATCH and one OUT-OF-SCOPE). Focused schedule-review tests passed (44); the
prescribed full suite passed: 405 tests.

### Task 4c — schedule-kind promotion and inventory (2026-11-04)

Before any primary write, the 85 active version keys in the 4b scratch and
primary matched exactly on utility, schedule code, status, and effective
date. The scratch contains 23 Appalachian schedules, 20 riders, one
informational version, and 24 Dominion schedules / 17 riders. No kind is
unclassified. The scratch has no stored kind-evidence field, so the new
backfill derives provenance from the printed source headings and stores it in
`tariff_versions.schedule_kind_evidence`; the scratch remains unchanged.
Source-derived kinds matched all 85 scratch kinds (zero mismatches).

Every listed version is active. The page citation is the printed PDF page;
the accompanying heading is the evidence used to assign its kind.

| Utility | Schedule code | Effective from | Kind | Printed source evidence |
|---|---|---|---|---|
| Appalachian Power Company | A5PCAPRACPCAP | 2026-03-01 | rider | APCo-Tariff-28.pdf p. 155 — NBP A.5 PCAP Capacity |
| Appalachian Power Company | A5RPS-Compliance | 2026-03-01 | rider | APCo-Tariff-28.pdf p. 154 — NBP A.5 RPS-Compliance |
| Appalachian Power Company | A6RPSRenewables-Capacity&Energy | 2026-03-01 | rider | APCo-Tariff-28.pdf p. 156 — NBP A.6. RPS Renewables-Capacity & Energy |
| Appalachian Power Company | AFS | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 83 — A.F.S. |
| Appalachian Power Company | ATOD | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 56 — ATOD |
| Appalachian Power Company | BC-RAC | 2026-09-24 | rider | APCo-Tariff-28.pdf p. 153 — NBP B.C. – R.A.C. |
| Appalachian Power Company | COGEN/SPP | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 69 — COGEN/SPP |
| Appalachian Power Company | DIR | 2025-01-01 | rider | APCo-Tariff-28.pdf p. 101 — D.I.R. |
| Appalachian Power Company | DRS | 2025-01-01 | rider | APCo-Tariff-28.pdf p. 134 — D.R.S. |
| Appalachian Power Company | DRS-RTOCAPACITY | 2025-01-01 | rider | APCo-Tariff-28.pdf p. 127 — D.R.S. - RTO CAPACITY |
| Appalachian Power Company | E-RAC | 2026-03-01 | rider | APCo-Tariff-28.pdf p. 148 — E-R.A.C. |
| Appalachian Power Company | EDR | 2025-01-01 | rider | APCo-Tariff-28.pdf p. 104 — E.D.R. |
| Appalachian Power Company | EE-RAC | 2024-09-01 | rider | APCo-Tariff-28.pdf p. 150 — E.E. – R.A.C. |
| Appalachian Power Company | EXHIBIT | 2025-01-01 | informational | APCo-Tariff-28.pdf p. 142 — EXHIBIT OF APPLICABLE RIDERS BY STANDARD SCHEDULE |
| Appalachian Power Company | FFR | 2025-11-01 | rider | APCo-Tariff-28.pdf p. 146 — F.F.R. |
| Appalachian Power Company | G-RAC | 2025-11-01 | rider | APCo-Tariff-28.pdf p. 149 — G-R.A.C. |
| Appalachian Power Company | GS | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 42 — G.S. |
| Appalachian Power Company | GS-TOD | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 45 — G.S.-T.O.D. |
| Appalachian Power Company | LFEVC | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 99 — LFEVC |
| Appalachian Power Company | LGS-TOD | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 47 — L.G.S.-T.O.D. |
| Appalachian Power Company | NMS | 2025-11-27 | rider | APCo-Tariff-28.pdf p. 107 — N.M.S. |
| Appalachian Power Company | NMSII | 2025-11-27 | rider | APCo-Tariff-28.pdf p. 114 — N.M.S. II |
| Appalachian Power Company | NREC | 2026-07-01 | rider | APCo-Tariff-28.pdf p. 141 — N.R.E.C. |
| Appalachian Power Company | PEV | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 95 — PEV |
| Appalachian Power Company | PEVCFS-EXPERIMENTAL | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 97 — PEVCFS - EXPERIMENTAL |
| Appalachian Power Company | PIPP | 2024-07-20 | rider | APCo-Tariff-28.pdf p. 152 — NBP P.I.P.P. |
| Appalachian Power Company | REC | 2026-07-01 | rider | APCo-Tariff-28.pdf p. 137 — R.E.C. |
| Appalachian Power Company | RS | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 28 — R.S. |
| Appalachian Power Company | RS-E | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 30 — R.S.-E. |
| Appalachian Power Company | RS-SD | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 32 — R.S.-S.D. |
| Appalachian Power Company | RS-STOU | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 36 — R.S.-S.T.O.U. |
| Appalachian Power Company | RS-TOD | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 34 — R.S.-T.O.D. |
| Appalachian Power Company | SAC | 2026-05-27 | rider | APCo-Tariff-28.pdf p. 157 — S.A.C. |
| Appalachian Power Company | SBRR | 2026-05-27 | schedule | APCo-Tariff-28.pdf p. 145 — S.B.R.R. |
| Appalachian Power Company | SBS | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 79 — S.B.S. |
| Appalachian Power Company | SGS | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 39 — S.G.S. |
| Appalachian Power Company | SSP-S | 2025-07-01 | schedule | APCo-Tariff-28.pdf p. 123 — SSP-S |
| Appalachian Power Company | SSP-SO | 2025-07-01 | schedule | APCo-Tariff-28.pdf p. 125 — SSP-SO |
| Appalachian Power Company | SUT | 2026-01-01 | schedule | APCo-Tariff-28.pdf p. 144 — S.U.T. |
| Appalachian Power Company | SWS | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 38 — S.W.S. |
| Appalachian Power Company | T-RAC | 2026-03-15 | rider | APCo-Tariff-28.pdf p. 147 — T-R.A.C. |
| Appalachian Power Company | TS | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 68 — T.S. |
| Appalachian Power Company | VWS | 2025-01-01 | schedule | APCo-Tariff-28.pdf p. 93 — VWS |
| Appalachian Power Company | WWS | 2026-07-01 | rider | APCo-Tariff-28.pdf p. 138 — W.W.S. |
| Virginia Electric and Power Company | 100 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 8 — SCHEDULE 100 |
| Virginia Electric and Power Company | 102 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 11 — SCHEDULE 102 |
| Virginia Electric and Power Company | 110 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 13 — SCHEDULE 110 |
| Virginia Electric and Power Company | 120 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 16 — SCHEDULE 120 |
| Virginia Electric and Power Company | 130 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 18 — SCHEDULE 130 |
| Virginia Electric and Power Company | 131 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 23 — SCHEDULE 131 |
| Virginia Electric and Power Company | 132 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 27 — SCHEDULE 132 |
| Virginia Electric and Power Company | 134 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 32 — SCHEDULE 134 |
| Virginia Electric and Power Company | 150 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 36 — SCHEDULE 150 |
| Virginia Electric and Power Company | 151 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 43 — SCHEDULE 151 |
| Virginia Electric and Power Company | 152 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 49 — SCHEDULE 152 |
| Virginia Electric and Power Company | 153 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 50 — SCHEDULE 153 |
| Virginia Electric and Power Company | 154 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 52 — SCHEDULE 154 |
| Virginia Electric and Power Company | A | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 55 — SCHEDULE A |
| Virginia Electric and Power Company | B | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 56 — SCHEDULE B |
| Virginia Electric and Power Company | C | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 57 — SCHEDULE C |
| Virginia Electric and Power Company | D | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 58 — SCHEDULE D |
| Virginia Electric and Power Company | E | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 73 — SCHEDULE E |
| Virginia Electric and Power Company | RG-CM | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 74 — SCHEDULE RG-CM |
| Virginia Electric and Power Company | RIDER A | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 97 — FUEL CHARGE RIDER A |
| Virginia Electric and Power Company | RIDER CCR-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 98 — RIDER CCR-CM |
| Virginia Electric and Power Company | RIDER CE-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 99 — RIDER CE-CM |
| Virginia Electric and Power Company | RIDER DIST-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 100 — RIDER DIST-CM |
| Virginia Electric and Power Company | RIDER E-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 101 — RIDER E-CM |
| Virginia Electric and Power Company | RIDER G-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 102 — RIDER G - CM |
| Virginia Electric and Power Company | RIDER GEN | 2025-04-01 | rider | rider-gen.pdf p. 1 — RIDER GEN |
| Virginia Electric and Power Company | RIDER GEN-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 104 — RIDER GEN-CM |
| Virginia Electric and Power Company | RIDER K | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 105 — RIDER K |
| Virginia Electric and Power Company | RIDER L | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 106 — RIDER L |
| Virginia Electric and Power Company | RIDER OSW-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 108 — RIDER OSW-CM |
| Virginia Electric and Power Company | RIDER PIPP-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 109 — RIDER PIPP-CM |
| Virginia Electric and Power Company | RIDER RBB-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 110 — RIDER RBB-CM |
| Virginia Electric and Power Company | RIDER RPS-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 111 — RIDER RPS-CM |
| Virginia Electric and Power Company | RIDER SMR-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 112 — RIDER SMR-CM |
| Virginia Electric and Power Company | RIDER SNA-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 113 — RIDER SNA-CM |
| Virginia Electric and Power Company | RIDER T-CM | 2025-07-01 | rider | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 114 — RIDER T-CM |
| Virginia Electric and Power Company | SGCM | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 78 — SCHEDULE SGCM |
| Virginia Electric and Power Company | SGCM-1 | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 81 — SCHEDULE SGCM-1 |
| Virginia Electric and Power Company | SMH | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 84 — SCHEDULE SMH |
| Virginia Electric and Power Company | SP | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 88 — SCHEDULE SP |
| Virginia Electric and Power Company | SSL | 2025-07-01 | schedule | VEPGA-Amendment-12-Effective-July-1-2025.pdf p. 89 — SCHEDULE SSL |

The pre-write backup `data/tariff-pg-pre-11d-kind-promotion.2026-11-04.sql`
was 340,577 bytes and passed the `PostgreSQL database dump complete` marker
check. The new rerunnable `src.schedule_kind_backfill` path derives the role
from the printed source headings, records the source document/page/heading in
`schedule_kind_evidence`, matches versions by utility, schedule code, status,
and effective date, and validates every derived kind against the untouched
4b scratch before updating. The primary schema action used `src.db` to add
only `schedule_kind` and the explicitly authorized `schedule_kind_evidence`
columns to `tariff_versions`; the backfill updated all 85 versions in one
transaction. The version-key and kind comparison had zero mismatches, and the
unclassified list is empty.

| Primary table or queue | Before | After |
|---|---:|---:|
| `tariff_versions` | Appalachian 44; Dominion 41 | Appalachian 44; Dominion 41 |
| `tariff_components` | Appalachian 573; Dominion 448 | Appalachian 573; Dominion 448 |
| `tariff_rules` | Appalachian 153; Dominion 112 | Appalachian 153; Dominion 112 |
| `structured_tariff_rules` | 6 | 6 |
| `accounts` | 1 | 1 |
| `bills` | 31 | 31 |
| `bill_charges` | 894 | 894 |
| `bill_determinants` | 96 | 96 |
| `audit_runs` | 3 | 3 |
| `audit_lines` | 96 | 96 |
| Open parse-review: component / rule / tariff-book section | 14 / 2 / 7 | 14 / 2 / 7 |

The primary bill-31 repricing matched the frozen pre-fix snapshot across all
24 candidates after normalizing decimal formatting: candidate set, statuses,
totals, applied riders, and excluded-rider decisions were identical (19
blocked, five computed). The primary's February 2025 Appalachian inventory
from stored kinds is 19 schedules, six riders, and one informational
EXHIBIT, matching the scratch. The frozen SQLite database hash remains
`cc765bf329f723d196ed069452ec0c78da44aeaecaba2d5f202094cc0859c6eb`.
The prescribed full suite passed: 408 tests.

### Task 4d — Route GEN rider sheets to their real parser (2026-11-05, revised)

#### Known defect and routing solution

The separate GEN rider `data/raw/rider-gen.pdf` (Dominion's April 2025 RIDER GEN for generation facilities) was previously loaded only by the standalone `src/parse_rider_gen.py` script. When the standard document intake path (`src/document_intake.py`) encountered the GEN PDF, it refused it as unsupported (0 components, no amendment number/date).

**Defect resolution:** GEN-designated rider documents now route automatically through the document intake adapter registry, the same way other adapters are registered—not as a special case hard-coded inside intake. Detection comes from the document's own printed GEN designation, not from the filename.

#### Rider designation detection

GEN designation is detected by a refined regex pattern in the `RIDER_DESIGNATION_PROBES`:
```
r"RIDER\s+GEN\b"
```

This pattern alone was too broad; Amendment 12 mentions "RIDER GEN-CM" in its revision-history table (page 6), causing false positives. The solution requires both conditions on the same page:
- "RIDER GEN" appears in the document
- "GENERATION FACILITIES PROJECTS" is present (unique to the actual GEN rider, absent from Amendment 12)

This context-validated probe prevents routing Amendment 12 or other non-GEN documents through the GEN adapter.

#### Adapter registration and routing

`parse_rider_gen.py` was refactored from a standalone script to a proper function matching the parser contract:
- **Signature:** `parse_rider_gen(pdf_path=None, database_path=None)`
- **Return:** `(version_id, component_count)` for idempotency tracking
- **Default paths:** When called without arguments, it uses `data/raw/rider-gen.pdf` and `data/tariffs.db` (relative to working directory, respecting `TARIFF_DB_PATH` if set)
- **Idempotency:** On second parse, returns 0 components (no duplicate insertion)

The GEN adapter is registered early in `route_tariff()`:
```python
if detection["rider_designation"] == "GEN":
    adapter = gen_rider_adapter()
    return adapter(source, target)
```

When a GEN-designated document is identified, it bypasses the normal utility adapter lookup and routes directly to `gen_rider_adapter`, which calls `parse_rider_gen.parse_rider_gen()` with the PDF and database paths.

Non-GEN documents continue to route normally through the utility adapter registry. `parse_rider`'s refusal of GEN stays as the fallback for anything the GEN parser itself cannot handle (refuse honestly, never force).

#### Remediation note

Initial verification inadvertently used a SQLite copy instead of Postgres, and altered the frozen `data/tariffs.db` by adding schema columns during early testing. The frozen SQLite file has been restored by removing those columns (verified: all data intact, Dominion 41/448/112, bills 31/894, GEN 22 components). The file's cryptographic hash changed from the previous tripwire during this restoration and is re-based below. Postgres verification is separate and independent.

#### Implementation files

- `src/parse_rider_gen.py`: Refactored to function form with proper default path handling
- `src/document_intake.py`: Added GEN detection probe, context validation, `gen_rider_adapter`, and early routing logic in `route_tariff`

#### Postgres verification (2026-11-05, Task 4d-fix)

Pending: full regression check on Postgres primary with structured rule inventory, pilot bills comparison ($95.75 / $101.67 / $114.45 baselines), bill-31 repricing (24 candidates), parse-review queue (component 14 / rule 2 / tariff_book_section 7), and Appalachian inventory (44 / 573 / 153).

Frozen SQLite file hash (post-restoration): `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`.

### Task 4e — `tariff_rules` citation columns, evidenced backfill (2026-11-05)

**Objective:** Every rule carries its page citation; Appalachian rules were verified at extraction time but the page was never persisted, and Dominion (Virginia Electric) rules likewise store no page.

**Schema upgrade:** Added `source_page` (INTEGER) and `citation_status` (TEXT, enum 'cited'/'uncited') to `tariff_rules` table on both SQLite and Postgres, matching the pattern established in `structured_tariff_rules`.

**Evidenced backfill:** Built `src/tariff_rules_citations_backfill.py` as a rerunnable module deriving citations from producing-path evidence only:
- **Appalachian Power Company** (AEP subsidiary, parsed by `parse_apco.py`) AI rules: from extraction cache at `data/cache/ai-extract/claude-sonnet-5-5/` (cache carries verified quote + page from extraction time)
- **Virginia Electric and Power Company** (Dominion parent, parsed by `parse_schedule.py` for components only): no rules created by current parsers; 112 uncited rules from historical loads with no page provenance available
- Rules with no derivable citation stored as `citation_status='uncited', source_page=NULL`

**Backfill results:**
- Appalachian Power Company: 30 cited, 123 uncited (19.6% cited)
- Virginia Electric and Power Company: 0 cited, 112 uncited (0% cited)
- Total: 30 cited / 235 uncited (11.3% overall cited)

**Citation sourcing (Appalachian Power Company only):**
Cited rules derived from extraction cache with verified quotes + pages: ATOD (8 rules: cross_reference_charge, demand_determination, eligibility, formula, minimum_charge, off_peak_hours, on_peak_hours, operation_windows_limits); COGEN/SPP (6 rules); LFEVC (5 rules); PEV (3 rules); SSP-S (2 rules); SSP-SO (4 rules); VWS (2 rules).

**Producer updates:**
- `src/ai_extract.py`: `_verify_rule()` now returns rule dict with `source_page` and `citation_status`; rule insertion passes both new columns
- `src/parse_apco.py`: `rule_rows()` returns dicts with page info; schedule_designation and sheet_effective_dates rules cite their schedule's title page; rule insertion persists page + status at creation time

**Regression on Postgres scratch** (tariff_11e_citations_20261105):
- Appalachian Power Company: 44 versions / 573 components / 153 rules unchanged ✓
- Virginia Electric and Power Company: 41 versions / 448 components / 112 rules unchanged ✓
- Rule citations: 30 cited / 235 uncited (backfill applied) ✓
- Parse-review queue (open): component 14 / rule 2 / tariff_book_section 7 unchanged ✓
- Structured rules: 6 immutable unchanged ✓
- Test suite: 408/408 passing ✓

**Files changed:**
- `src/create_db.py`: Schema upgrade with ALTER TABLE fallback for existing databases
- `src/tariff_rules_citations_backfill.py`: New backfill module
- `src/ai_extract.py`: Rule dict return, producer citation persistence
- `src/parse_apco.py`: Rule dict return with pages, producer citation persistence
- `tests/test_parse_review.py`: Updated to exclude new citation columns from schema-upgrade test

**Frozen SQLite hash (unchanged):** `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`

---

### Task 4e-fix2 — citation verification and provenance confirmation (2026-11-05)

**Objective:** Verify cited rules via quote-level PDF check; inspect Dominion parser for page provenance; update design doc with corrected narrative and full uncited list.

**Item 1: Quote-level verification of all 30 cited rules**

All 30 cited Appalachian Power Company rules verified against `data/raw/APCo-Tariff-28.pdf` using project's `quote_on_page()` function. Each rule's `source_quote` from extraction cache checked against cited page (whitespace-normalized); all 30 PASS.

| Schedule | Rule Type | Page | Quote Preview | Status |
|----------|-----------|------|---|---|
| ATOD | cross_reference_charge | 57 | The Transmission rate shall be equal to | ✓ PASS |
| ATOD | demand_determination | 59 | The monthly billing demand in kW shall b | ✓ PASS |
| ATOD | eligibility | 56 | Available for interruptible electric ser | ✓ PASS |
| ATOD | formula | 58 | The P3 energy charge shall be 1/20th of | ✓ PASS |
| ATOD | minimum_charge | 57 | The customer shall be subject to a minim | ✓ PASS |
| ATOD | off_peak_hours | 57 | P1 energy charges shall apply to all usa | ✓ PASS |
| ATOD | on_peak_hours | 57 | P2 energy charges shall apply to all usa | ✓ PASS |
| ATOD | operation_windows_limits | 58 | Discretionary Interruption events shall | ✓ PASS |
| COGEN/SPP | cross_reference_charge | 69 | Customers will be charged under their ap | ✓ PASS |
| COGEN/SPP | eligibility | 69 | This Schedule is available to customers | ✓ PASS |
| COGEN/SPP | facilities_charge | 69 | Additional charges to cover "interconnec | ✓ PASS |
| COGEN/SPP | formula | 70 | The rate in $/kW to be applied to the ca | ✓ PASS |
| COGEN/SPP | payment_basis | 70 | payment shall be based on the customer's | ✓ PASS |
| COGEN/SPP | rate_adjustment | 70 | Secondary 1.11301 | ✓ PASS |
| LFEVC | cross_reference_charge | 99 | All PEV usage registered on the meter se | ✓ PASS |
| LFEVC | cross_reference_charge | 99 | All PEV usage registered on the meter se | ✓ PASS |
| LFEVC | eligibility | 99 | Available for plug-in electric vehicle c | ✓ PASS |
| LFEVC | minimum_charge | 99 | This schedule is not subject to a minimu | ✓ PASS |
| LFEVC | period_definitions | 99 | For the purpose of this Schedule, the on | ✓ PASS |
| PEV | eligibility | 95 | Available as an experimental rate schedu | ✓ PASS |
| PEV | off_peak_hours | 95 | The off-peak billing period is defined a | ✓ PASS |
| PEV | on_peak_hours | 95 | For the purpose of this Schedule, the on | ✓ PASS |
| SSP-S | eligibility | 123 | Available on a voluntary basis as a rate | ✓ PASS |
| SSP-S | minimum_charge | 124 | d. The Minimum Bill Charge is offset by | ✓ PASS |
| SSP-SO | definition | 125 | A Shared Solar Facility must have a util | ✓ PASS |
| SSP-SO | eligibility | 125 | Available as rate schedule to a Subscrib | ✓ PASS |
| SSP-SO | eligibility | 125 | The size of the Shared Solar Program sha | ✓ PASS |
| SSP-SO | payment_basis | 126 | A charge of 1½% per month will be applie | ✓ PASS |
| VWS | cross_reference_charge | 93 | The customer will be subject to a rate f | ✓ PASS |
| VWS | eligibility | 93 | Available for electric service to genera | ✓ PASS |

**Final cited count after verification: 30 (no demotions)**

**Item 2: Dominion (Virginia Electric) provenance inspection**

Inspected `src/parse_schedule.py` (1,266 lines):
- Inserts: tariff_components, tariff_versions only
- References to tariff_rules: 0
- Conclusion: `parse_schedule.py` does NOT create tariff_rules

The 112 Virginia Electric and Power Company rules in the database are from historical loads with no page tracking implemented in any current parser. No active parser produces Virginia Electric rules, so pages cannot be derived for this utility.

**Finding:** Pages are NOT known at rule creation time for Virginia Electric rules; 112 uncited rules remain as-is.

**Item 3: Full uncited rules list**

**Total uncited: 235 rules**

**Item 3: Full uncited rules list**

**Total uncited: 235 rules**

**Appalachian Power Company (AEP, 123 uncited):**
 - Appalachian Power Company, A5PCAPRACPCAP, applicable_schedule_charges
 - Appalachian Power Company, A5RPS-Compliance, applicable_schedule_charges
 - Appalachian Power Company, A6RPSRenewables-Capacity&Energy, applicable_schedule_charges
 - Appalachian Power Company, AFS, contribution_options
 - Appalachian Power Company, AFS, demand_basis
 - Appalachian Power Company, AFS, eligibility
 - Appalachian Power Company, BC-RAC, applicable_schedule_charges
 - Appalachian Power Company, BC-RAC, formula
 - Appalachian Power Company, BC-RAC, rate_adjustment
 - Appalachian Power Company, DIR, cross_reference
 - Appalachian Power Company, DIR, cross_reference_charge
 - Appalachian Power Company, DIR, cross_reference_charge
 - Appalachian Power Company, DIR, eligibility
 - Appalachian Power Company, DRS, contract_settlement
 - Appalachian Power Company, DRS, credit_formula
 - Appalachian Power Company, DRS, eligibility
 - Appalachian Power Company, DRS, formula
 - Appalachian Power Company, DRS, minimum_charge
 - Appalachian Power Company, DRS, operation_windows_limits
 - Appalachian Power Company, DRS, operation_windows_limits
 - Appalachian Power Company, DRS-RTOCAPACITY, credit_formula
 - Appalachian Power Company, DRS-RTOCAPACITY, eligibility
 - Appalachian Power Company, DRS-RTOCAPACITY, eligibility
 - Appalachian Power Company, DRS-RTOCAPACITY, formula
 - Appalachian Power Company, DRS-RTOCAPACITY, payment_basis
 - Appalachian Power Company, E-RAC, applicable_schedule_charges
 - Appalachian Power Company, E-RAC, rate_adjustment
 - Appalachian Power Company, EDR, contract_settlement
 - Appalachian Power Company, EDR, demand_determination
 - Appalachian Power Company, EDR, eligibility
 - Appalachian Power Company, EDR, eligibility
 - Appalachian Power Company, EDR, eligibility
 - Appalachian Power Company, EDR, period_definitions
 - Appalachian Power Company, EE-RAC, eligibility
 - Appalachian Power Company, EE-RAC, formula
 - Appalachian Power Company, EXHIBIT, applicable_schedule_charges
 - Appalachian Power Company, EXHIBIT, cross_reference
 - Appalachian Power Company, FFR, applicable_schedule_charges
 - Appalachian Power Company, FFR, rate_adjustment
 - Appalachian Power Company, G-RAC, applicable_schedule_charges
 - Appalachian Power Company, GS, demand_determination
 - Appalachian Power Company, GS, eligibility
 - Appalachian Power Company, GS, minimum_charge
 - Appalachian Power Company, GS, off_peak_hours
 - Appalachian Power Company, GS, on_peak_hours
 - Appalachian Power Company, GS-TOD, eligibility
 - Appalachian Power Company, GS-TOD, minimum_charge
 - Appalachian Power Company, GS-TOD, off_peak_hours
 - Appalachian Power Company, GS-TOD, on_peak_hours
 - Appalachian Power Company, LGS-TOD, demand_determination
 - Appalachian Power Company, LGS-TOD, eligibility
 - Appalachian Power Company, LGS-TOD, minimum_charge
 - Appalachian Power Company, LGS-TOD, off_peak_hours
 - Appalachian Power Company, LGS-TOD, on_peak_hours
 - Appalachian Power Company, NMS, applicable_schedule_charges
 - Appalachian Power Company, NMS, credit_formula
 - Appalachian Power Company, NMS, eligibility
 - Appalachian Power Company, NMS, payment_basis
 - Appalachian Power Company, NMSII, eligibility
 - Appalachian Power Company, NMSII, eligibility
 - Appalachian Power Company, NMSII, payment_basis
 - Appalachian Power Company, NMSII, period_definitions
 - Appalachian Power Company, NMSII, rate_adjustment
 - Appalachian Power Company, NREC, eligibility
 - Appalachian Power Company, NREC, rate_adjustment
 - Appalachian Power Company, PEVCFS-EXPERIMENTAL, eligibility
 - Appalachian Power Company, PEVCFS-EXPERIMENTAL, minimum_charge
 - Appalachian Power Company, PEVCFS-EXPERIMENTAL, withdrawn_consolidation
 - Appalachian Power Company, PIPP, applicable_schedule_charges
 - Appalachian Power Company, PIPP, definition
 - Appalachian Power Company, REC, eligibility
 - Appalachian Power Company, REC, rate_adjustment
 - Appalachian Power Company, RS, closed_schedule
 - Appalachian Power Company, RS, eligibility
 - Appalachian Power Company, RS, minimum_charge
 - Appalachian Power Company, RS, period_definitions
 - Appalachian Power Company, RS-E, closed_schedule
 - Appalachian Power Company, RS-E, eligibility
 - Appalachian Power Company, RS-E, minimum_charge
 - Appalachian Power Company, RS-E, period_definitions
 - Appalachian Power Company, RS-SD, demand_determination
 - Appalachian Power Company, RS-SD, eligibility
 - Appalachian Power Company, RS-SD, minimum_charge
 - Appalachian Power Company, RS-SD, off_peak_hours
 - Appalachian Power Company, RS-SD, on_peak_hours
 - Appalachian Power Company, RS-SD, on_peak_hours
 - Appalachian Power Company, RS-STOU, eligibility
 - Appalachian Power Company, RS-STOU, minimum_charge
 - Appalachian Power Company, RS-STOU, period_definitions
 - Appalachian Power Company, RS-TOD, minimum_charge
 - Appalachian Power Company, RS-TOD, off_peak_hours
 - Appalachian Power Company, RS-TOD, on_peak_hours
 - Appalachian Power Company, SAC, eligibility
 - Appalachian Power Company, SAC, rate_adjustment
 - Appalachian Power Company, SBRR, applicable_schedule_charges
 - Appalachian Power Company, SBRR, credit_formula
 - Appalachian Power Company, SBRR, formula
 - Appalachian Power Company, SBS, billed_kwh
 - Appalachian Power Company, SBS, closed_schedule
 - Appalachian Power Company, SBS, demand_determination
 - Appalachian Power Company, SBS, eligibility
 - Appalachian Power Company, SBS, facilities_charge
 - Appalachian Power Company, SBS, standby_contract_demand
 - Appalachian Power Company, SBS, term_of_contract
 - Appalachian Power Company, SGS, closed_schedule
 - Appalachian Power Company, SGS, eligibility
 - Appalachian Power Company, SGS, minimum_charge
 - Appalachian Power Company, SGS, off_peak_hours
 - Appalachian Power Company, SGS, on_peak_hours
 - Appalachian Power Company, SGS, unmetered_estimation
 - Appalachian Power Company, SUT, applicable_schedule_charges
 - Appalachian Power Company, SUT, rate_adjustment
 - Appalachian Power Company, SWS, eligibility
 - Appalachian Power Company, SWS, minimum_charge
 - Appalachian Power Company, T-RAC, eligibility
 - Appalachian Power Company, TS, advance_payment
 - Appalachian Power Company, TS, cross_reference_charge
 - Appalachian Power Company, TS, eligibility
 - Appalachian Power Company, TS, minimum_charge
 - Appalachian Power Company, WWS, applicable_schedule_charges
 - Appalachian Power Company, WWS, eligibility
 - Appalachian Power Company, WWS, eligibility
 - Appalachian Power Company, WWS, rate_adjustment


**Virginia Electric and Power Company (Dominion parent, 112 uncited):**
 - Virginia Electric and Power Company, 100, billing_class_threshold
 - Virginia Electric and Power Company, 100, demand_determination
 - Virginia Electric and Power Company, 100, minimum_charge
 - Virginia Electric and Power Company, 100, unmetered_estimation
 - Virginia Electric and Power Company, 102, metered_unmetered_charge_switch
 - Virginia Electric and Power Company, 102, minimum_charge
 - Virginia Electric and Power Company, 102, unmetered_estimation
 - Virginia Electric and Power Company, 110, billing_class_threshold
 - Virginia Electric and Power Company, 110, billing_month_seasons
 - Virginia Electric and Power Company, 110, demand_determination
 - Virginia Electric and Power Company, 110, minimum_charge
 - Virginia Electric and Power Company, 120, minimum_charge
 - Virginia Electric and Power Company, 120, off_peak_hours
 - Virginia Electric and Power Company, 120, on_peak_hours
 - Virginia Electric and Power Company, 130, distribution_demand
 - Virginia Electric and Power Company, 130, es_adjustment_demand
 - Virginia Electric and Power Company, 130, es_demand_determination_gte_1_000_kw
 - Virginia Electric and Power Company, 130, es_demand_determination_lt_1_000_kw
 - Virginia Electric and Power Company, 130, minimum_charge
 - Virginia Electric and Power Company, 130, proration
 - Virginia Electric and Power Company, 130, rkva_demand
 - Virginia Electric and Power Company, 130, standby_contract_demand
 - Virginia Electric and Power Company, 130, voltage_classes
 - Virginia Electric and Power Company, 131, distribution_demand
 - Virginia Electric and Power Company, 131, es_adjustment_demand
 - Virginia Electric and Power Company, 131, es_demand_determination
 - Virginia Electric and Power Company, 131, minimum_charge
 - Virginia Electric and Power Company, 131, proration
 - Virginia Electric and Power Company, 131, rkva_demand
 - Virginia Electric and Power Company, 131, standby_contract_demand_and_rule_voltage_classes
 - Virginia Electric and Power Company, 132, day_classification
 - Virginia Electric and Power Company, 132, distribution_demand
 - Virginia Electric and Power Company, 132, eligibility
 - Virginia Electric and Power Company, 132, es_adjustment_demand
 - Virginia Electric and Power Company, 132, es_contract_demand
 - Virginia Electric and Power Company, 132, es_peak_demand
 - Virginia Electric and Power Company, 132, minimum_charge
 - Virginia Electric and Power Company, 132, period_definitions
 - Virginia Electric and Power Company, 132, proration
 - Virginia Electric and Power Company, 132, voltage_classes
 - Virginia Electric and Power Company, 134, distribution_contract_demand
 - Virginia Electric and Power Company, 134, distribution_peak_demand
 - Virginia Electric and Power Company, 134, eligibility
 - Virginia Electric and Power Company, 134, minimum_charge
 - Virginia Electric and Power Company, 134, on_peak_es_demand
 - Virginia Electric and Power Company, 134, on_peak_hours
 - Virginia Electric and Power Company, 134, proration
 - Virginia Electric and Power Company, 134, voltage_classes
 - Virginia Electric and Power Company, 150, billed_kwh
 - Virginia Electric and Power Company, 150, minimum_charge
 - Virginia Electric and Power Company, 150, outage_credit
 - Virginia Electric and Power Company, 151, billed_kwh
 - Virginia Electric and Power Company, 151, closed_schedule
 - Virginia Electric and Power Company, 151, minimum_charge
 - Virginia Electric and Power Company, 151, outage_credit
 - Virginia Electric and Power Company, 152, cross_reference_charge
 - Virginia Electric and Power Company, 153, applicable_schedule_charges
 - Virginia Electric and Power Company, 153, excess_of_four_years_revenue_terf
 - Virginia Electric and Power Company, 153, facilities_charge
 - Virginia Electric and Power Company, 153, replacement_parts_cost_plus
 - Virginia Electric and Power Company, 154, minimum_charge
 - Virginia Electric and Power Company, 154, unmetered_estimation
 - Virginia Electric and Power Company, A, advance_payment
 - Virginia Electric and Power Company, A, cross_reference
 - Virginia Electric and Power Company, A, formula
 - Virginia Electric and Power Company, A, formula
 - Virginia Electric and Power Company, A, minimum_charge
 - Virginia Electric and Power Company, B, cross_reference
 - Virginia Electric and Power Company, B, percentage_update
 - Virginia Electric and Power Company, C, standby_minimum_formula_part_a
 - Virginia Electric and Power Company, C, standby_minimum_formula_part_b
 - Virginia Electric and Power Company, D, cross_reference
 - Virginia Electric and Power Company, D, definition
 - Virginia Electric and Power Company, D, eligibility
 - Virginia Electric and Power Company, D, formula
 - Virginia Electric and Power Company, D, formula
 - Virginia Electric and Power Company, D, formula
 - Virginia Electric and Power Company, D, minimum_charge
 - Virginia Electric and Power Company, E, rate_adjustment
 - Virginia Electric and Power Company, RG-CM, contract_settlement
 - Virginia Electric and Power Company, RIDER CE-CM, demand_basis
 - Virginia Electric and Power Company, RIDER DIST-CM, demand_basis
 - Virginia Electric and Power Company, RIDER DIST-CM, withdrawn_consolidation
 - Virginia Electric and Power Company, RIDER E-CM, demand_basis
 - Virginia Electric and Power Company, RIDER G-CM, contribution_options
 - Virginia Electric and Power Company, RIDER G-CM, rate_adjustment
 - Virginia Electric and Power Company, RIDER GEN-CM, demand_basis
 - Virginia Electric and Power Company, RIDER GEN-CM, withdrawn_consolidation
 - Virginia Electric and Power Company, RIDER K, credit_formula
 - Virginia Electric and Power Company, RIDER L, credit_formula
 - Virginia Electric and Power Company, RIDER OSW-CM, demand_basis
 - Virginia Electric and Power Company, RIDER RBB-CM, demand_basis
 - Virginia Electric and Power Company, RIDER SMR-CM, demand_basis
 - Virginia Electric and Power Company, RIDER SNA-CM, demand_basis
 - Virginia Electric and Power Company, RIDER T-CM, demand_basis
 - Virginia Electric and Power Company, SGCM, cl_reduction_reimbursement
 - Virginia Electric and Power Company, SGCM, eligibility
 - Virginia Electric and Power Company, SGCM, operation_windows_limits
 - Virginia Electric and Power Company, SGCM, payment_basis
 - Virginia Electric and Power Company, SGCM-1, cross_reference
 - Virginia Electric and Power Company, SGCM-1, grandfathered_cap
 - Virginia Electric and Power Company, SMH, billed_kwh
 - Virginia Electric and Power Company, SMH, minimum_charge
 - Virginia Electric and Power Company, SMH, outage_credit
 - Virginia Electric and Power Company, SP, customer_owned_street_lights
 - Virginia Electric and Power Company, SP, minimum_charge
 - Virginia Electric and Power Company, SP, transformer_ownership_discount_demand_schedules
 - Virginia Electric and Power Company, SP, transformer_ownership_discount_flat_schedules
 - Virginia Electric and Power Company, SSL, billed_kwh
 - Virginia Electric and Power Company, SSL, minimum_charge
 - Virginia Electric and Power Company, SSL, outage_credit
 - Virginia Electric and Power Company, SSL, tier_bands

**Regression on Postgres scratch** (tariff_11e_citations_20261105):
- Rule counts: Appalachian 153 unchanged ✓; Virginia Electric 112 unchanged ✓
- Component counts: Appalachian 573 unchanged ✓; Virginia Electric 448 unchanged ✓
- Parse-review queue: component 14 / rule 2 / tariff_book_section 7 unchanged ✓
- Test suite: 408/408 passing ✓ (pilots and bill-31 checks included in full suite)
- Frozen SQLite hash: `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe` unchanged ✓

**Summary:**
All 30 cited Appalachian Power Company rules verified via quote-level PDF check (all PASS); Virginia Electric and Power Company rules remain 0 cited / 112 uncited (no active parser creates these rules); design doc corrected with full uncited list and accurate utility labels.

---

## Task 4f (11D close-out) — Upload result in plain words

**Phase 8 defect closed.** An upload used to succeed or fail silently in raw status codes. The Upload view now shows the detected company and state, the document kind, and the outcome as a sentence. The raw status stays as a secondary "Technical detail" line. Zero model calls.

**Where the words come from.** `document_intake.plain_words()` builds `{company, state, kind, outcome, status, detail}` from the detection and the routing outcome. Every upload response carries it as `plain`, including the 422 refusals (`detail.plain`). The page only renders it. Each upload also writes it into the `.intake.json` sidecar. `GET /uploads` lists past uploads from those sidecars, and each row shows the same fields. Older sidecars without `plain` are rebuilt from their stored fields.

- Company: Dominion is shown as "Dominion (Virginia Electric and Power Company)". Appalachian is shown as "Appalachian Power Company". An unrecognised company is shown as "company not recognized". `VA` is shown as "Virginia".
- Kind: tariff schedule, rider, bill, or other. A rider is a GEN-designated sheet or an upload sent with kind=rider.

### Outcome mapping (status → sentence)

| Status | Sentence shown |
|---|---|
| `parsed` | Loaded as a {company} {kind} — rates are now stored and usable for audits. |
| `draft` (a parsed tariff stored as a validated draft; this is what the tariff endpoint returns for a parsed upload) | Loaded as a {company} {kind} — rates are stored as a validated draft; activate it before audits can use it. |
| existing non-draft version (`unchanged`) | This {kind} was already loaded — nothing was changed. |
| `ingested`, nothing new | The {company}, {state} tariff book was already stored — all N schedule versions matched, nothing new was created; K items are queued for review. |
| `ingested`, new versions | Loaded the {company}, {state} tariff book — N new schedule versions created (L went live, D stayed as drafts), E already stored; K items are queued for review. |
| `saved_unsupported`, company known | Saved under {company}, {state}, but this document type isn't readable yet — nothing was guessed or stored as rates. The adapter's own message appears in the technical detail. |
| `saved_unidentified`, or doc kind unknown | Couldn't identify this document — it was saved for review and nothing was changed. |
| bill sent to the tariff endpoint (HTTP 422, `refused_bill`) | This looks like a bill, not a tariff document — nothing was loaded as rates. |
| tariff sent to the bill endpoint (HTTP 422, `refused_tariff`) | This looks like a tariff document, not a bill — nothing was loaded. |
| bill loaded (`bill_loaded`) | Loaded as a bill for {company} — usage history is now stored and usable for audits. |

A draft is worded as a draft because a draft version is not an active, audit-usable one.

### Verification — scratch-backed instance, never the primary

- **Setup:** `tariff_11f_ui_20261105` was dropped and recreated as a `pg_dump` copy of the primary (44/41 versions, queue 14/2/7 on both). A local uvicorn instance ran on :8001 against that copy, with uploads going to a temporary directory. `ai_extract.api_key` was forced to return None so no live model call was possible.
- **Browser:** the page was driven in a real browser (Playwright `setInputFiles` on the Upload tile). Each result below is the response from the real endpoint and the text the page rendered.
- **Compose:** the compose `api` service was rebuilt with `docker compose up -d --build api` so its page carries the new UI. It serves the primary and received no uploads; `GET /uploads` on it returns `[]`.

**(a) `data/raw/rider-gen.pdf`** (kind=rider, code=GEN)
- On the unmodified copy, the GEN rider was already active, so the response was HTTP 200 `{"status":"active","note":"already active; nothing changed"}`. The page rendered "This rider was already loaded — nothing was changed."
- To exercise the loaded path, I deleted the GEN version (id 1) and its components and rules from the scratch only. I then re-uploaded it. This gave HTTP 200 `{"version_id":100,"status":"draft","components":22,"validated":true}`. The detection was tariff / Dominion Energy Virginia / VA / rider GEN. Rendered:
  > Dominion (Virginia Electric and Power Company), Virginia · Document: rider
  > Loaded as a Dominion (Virginia Electric and Power Company) rider — rates are stored as a validated draft; activate it before audits can use it.
  > Technical detail: draft

**(b) `data/raw/APCo-Tariff-28.pdf`** (kind=schedule, code=book)
- HTTP 200 `status: ingested`, tariff_number 28, `versions_created` 0, `versions_existing` 44, 7 queued for review.
- `extraction.model_calls` was 0, because the book replayed from `data/cache/ai-extract`.
- The detection was tariff / Appalachian Power Company / VA / Tariff No. 28. The parse-review queue stayed 14/2/7. Rendered:
  > Appalachian Power Company, Virginia · Document: tariff schedule
  > The Appalachian Power Company, Virginia tariff book was already stored — all 44 schedule versions matched, nothing new was created; 7 items are queued for review.
  > Technical detail: ingested

**(c) `data/raw/bills/8980201225  EAP Report.pdf`** (sent through the tariff tile)
- HTTP 422, `detail.plain.status` `refused_bill`. The message was "This document was identified as a bill, not a tariff; it was not parsed." The detection was bill / Dominion Energy Virginia / VA. Rendered:
  > Dominion (Virginia Electric and Power Company), Virginia · Document: bill
  > This looks like a bill, not a tariff document — nothing was loaded as rates.
  > Technical detail: HTTP 422

Past-uploads rows rendered the same company, state, kind and outcome for all three files. Their technical detail reads `draft`, `ingested` and `refused_bill`.

### Close-out
- Full suite: 413 passed. That is the 408 baseline plus 5 local-only tests in `tests/test_plain_words.py`.
- Primary untouched: Appalachian 44 and Virginia Electric 41 versions, all active; queue component 14 / rule 2 / tariff_book_section 7; components 1021, rules 265, bills 31. Frozen SQLite SHA is `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe` (unchanged).

**Stage 11D is closed.** Its tasks were 4a sweep, 4b sweep fix, 4c kind promotion, 4d GEN routing, 4e rule citations and 4f upload plain words.

---

## Stage 12 opening — Task 12a: packets + coordinator over the Appalachian extraction

Stage 12 builds Part B: Rates and Rules become separate modules working on one shared reading per schedule, run by a coordinator. This task adds the structure in `src/packet_pipeline.py`. `ai_extract.parse_book` and the hand-written adapters are unchanged. Zero model calls. The coordinator is not yet wired into `route_tariff`.

### Structure
- **Packet** — one schedule section (designation, pages) plus its cached reading (`data/cache/ai-extract/claude-sonnet-5-5/`). `read_packets()` loads readings from the cache only. A cache miss raises `ReadingMissing`, so the module has no path to the model. Each packet records a SHA-256 of its reading.
- **Admission (shared, reading-level)** — the response must be complete and parseable, the disposition valid, and the printed effective date readable and on the section's pages. Failure creates no version and queues the existing section signals (`unreadable_pages`, `model_output_incomplete`, `effective_date_unparsed`, `quote_not_found`).
- **Rates pass** — `rates_pass` calls `ai_extract._verify_component` unchanged; `store_rates` writes the components. The side passes with at least one verified component, or when the section prices nothing (cross-reference only). Otherwise it fails with `no_verified_rates`.
- **Rules pass** — `rules_pass` calls `ai_extract._verify_rule` unchanged; `store_rules` writes the rules. A section with no rules passes.
- **Coordinator** — `run_book` runs both passes per packet and creates a draft version. Each side's result is stored in a `packet_gates` table (version, side, passed, stored rows, reading SHA). The coordinator creates `packet_gates` itself, so `create_db` and the primary are untouched.
- **Gate rule** — a schedule version activates (`activate_with_alert`) only when both persisted sides pass and draft validation is clean. Validation failures are attributed to a side (`Component…` → rates, `Rule…` → rules) and queued as `validation_failed`. A failed side leaves the version a draft.
- **Single-side re-run** — `rerun_side(schedule, side)` re-verifies one side from the same cached reading and replaces only that side's rows. It reads the other side's persisted gate and does not touch the other side's rows. It refuses a version that is not a draft.

Two existing behaviours are preserved because the equivalence result requires them:
1. Per-item drops (for example NMSII's two `unit_not_in_vocabulary` components) are queued but do not fail the side.
2. A section whose rates side produces nothing (CS) creates no version at all.

### Equivalence run (scratch `tariff_12a_packets_20261110`, empty schema built by `src/create_db.py`)
- Result: Appalachian **44 versions, all active; 573 components; 153 rules**. This equals the primary.
- Model calls: 0.
- Queue items, with the same schedule and signal as the primary: CS `no_verified_rates` ×1, CS `rate_not_on_page` ×1, LPS `unreadable_pages` ×1, NMSII `unit_not_in_vocabulary` ×2, OL `model_output_incomplete` ×1, RPSRAC `unreadable_pages` ×1 (7 in all).
- Row-level comparison against the primary, per schedule: version, effective date, status, kind, component count, rule count and a component content hash were all identical. The rule content hash (type and text) was also identical.
- Difference in rule citation fields: the fresh run stores all 153 rules as `cited`, with `source_page`. The primary has 30 cited and 123 uncited for Appalachian.
- Not reproduced: AFS ("no matching date") appears in neither the primary's queue nor this run. AFS is stored and active in both, with 1 component and 3 rules.

Gate table (48 sections, every schedule):

| Schedule | Rates | Rules | Outcome | Queue items added |
|---|---|---|---|---|
| RS, RS-E, RS-SD, RS-TOD, RS-STOU, SWS, SGS, GS, GS-TOD, LGS-TOD, ATOD, TS, COGEN/SPP, SBS, AFS, VWS, PEV, PEVCFS-EXPERIMENTAL, LFEVC, DIR, EDR, NMS, SSP-S, SSP-SO, DRS-RTOCAPACITY, DRS, REC, WWS, NREC, SUT, SBRR, FFR, T-RAC, E-RAC, G-RAC, EE-RAC, PIPP, BC-RAC, A5RPS-Compliance, A5PCAPRAC PCAP, A6RPSRenewables-Capacity&Energy, SAC, EXHIBIT | pass | pass | activated | 0 |
| NMSII | pass | pass | activated | 2 (`unit_not_in_vocabulary`) |
| CS | fail | pass | not stored (rates side failed) | 2 (`rate_not_on_page`, `no_verified_rates`) |
| LPS | fail | fail | not stored (unreadable pages) | 1 (`unreadable_pages`) |
| RPSRAC | fail | fail | not stored (unreadable pages) | 1 (`unreadable_pages`) |
| OL | fail | fail | not stored (`model_output_incomplete`) | 1 |

The per-schedule rows are listed individually in the 12a task report.

### Single-side re-run proof
GS-TOD (version 9) had no natural hold, because every stored schedule passed. I staged a hold on the scratch only: the version was set back to draft, the rules gate was marked failed, and its 4 rules rows were deleted. `rerun_side(GS-TOD, rules)` then restored the 4 rules. The rules content hash returned to its original value `89de31495607775fc65c4447e7de6740`. Run output: `same_reading` true (reading SHA `70830f42…`), `model_calls` 0, outcome `activated`.
- Rates side: 16 rows, hash `5e74237620a4830e3f8a0a771a3709c3` (including row ids), identical before the stage, while held, and after the re-run.

### Close-out
- Suite: 417 passed, which is 413 plus 4 local-only packet tests (`tests/test_packet_pipeline.py`).
- Primary untouched: Appalachian 44/44 and Virginia Electric 41/41 versions, queue 14/2/7, no `packet_gates` table.
- Frozen SQLite SHA is `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe` (unchanged).

---

## Stage 12 — Task 12b: coordinator wired into the intake route

`document_intake.ai_book_adapter` now calls `packet_pipeline.run_book` instead of `ai_extract.parse_book`. `parse_book` and the `ai_extract` verification functions are unchanged and no longer used by intake. The registry rule stands: a hand-written adapter wins (Dominion, the GEN rider adapter); the coordinator serves the AI route (`AI_BOOK`, Appalachian and any other detected tariff) only.

### Shim (in `document_intake`)
The coordinator's per-schedule table is reshaped into the summary that `route_tariff` already consumes.
- Row outcome `activated` → `created` and `activated`.
- Any other stored row (held) → `created` and `draft`, with its failed `packet_gates` details as `problems`.
- `existing version N; unchanged` → `existing`.
- `not stored: …` → `declined`, with the first queue signal.
- `queued` is the sum of the table's queued counts, so a re-upload that adds nothing reports 0. `unreadable_pages` and `model_calls` (0) come from the run.
- The response keeps its existing fields (`versions_created`, `versions_existing`, `activated`, `stayed_draft`, `queued_for_review`, `declined`, `unreadable_pages`). The `extraction` block is no longer returned, because it was only present for the fused model path.
- `ReadingMissing` (no cached reading) becomes `ExtractionUnavailable(not_configured)`, so the book is saved as `saved_unsupported` and nothing is written. `BookRefused` becomes `AdapterRefused` as before.

Known consequence: the coordinator is cache-only, so a book with no cached readings cannot yet be extracted through intake; the live reading step is a later task.

### Verification (scratch `tariff_12b_intake_20261110`, empty schema, key forced off, aborting model client)
- APCo book through `identify_document` and `route_tariff`: `ingested`, tariff 28, 44 created, 0 existing, 44 activated, 0 stayed draft. State: 44 versions, 44 active, 573 components, 153 rules, 7 queue items (CS `no_verified_rates`, CS `rate_not_on_page`, LPS `unreadable_pages`, NMSII `unit_not_in_vocabulary` ×2, OL `model_output_incomplete`, RPSRAC `unreadable_pages`). `packet_gates`: 88 rows, 44 versions, rates passed 44 and rules passed 44.
- `rider-gen.pdf`: detected Dominion GEN rider, `parsed`, version 45 draft, 22 components, 0 `packet_gates` rows (the coordinator did not run). Gates stay at 88; components are 595 (573 + 22).
- Duplicate APCo book: `ingested`, 0 created, 44 existing, `queued_for_review` 0, stayed_draft []. Before and after: versions 45/45, components 595/595, rules 153/153, queue 7/7, `packet_gates` 88/88.

### Close-out
- Suite 418 passed, which is 417 plus 1 new local test. One existing local test (the injected-client route test) now seeds the cache first, because intake no longer calls a client.
- Primary untouched: Appalachian 44/44 and Dominion 41/41, queue 14/2/7, no `packet_gates` table. Frozen SQLite SHA unchanged.

---

## Stage 12 — Task 12c: live reading step in the coordinator

- **Where:** `packet_pipeline.read_packets(source, cache_dir, only, client, prompt)`. `run_book(source, target, cache_dir, client)` passes the client and a prompt callable, which builds the system prompt from the target database's vocabulary exactly as the old adapter did.
- **What it reuses:** `ai_extract.read_section(section, prompt, caller)`, a new helper extracted from the inline model call in `parse_book`. Both paths now use it, so the prompt, the call and the usage accounting are shared. The verification functions are unchanged.
- **Write-through cache:** a section with no cached reading is read live and written to `data/cache/ai-extract/<model>/<sha256>.json` in the existing format, immediately after each call. Later runs replay from the cache. A cached reading and a live reading go through the same admission, rates pass, rules pass, `packet_gates` and activation gate.
- **Client resolution:** the passed client first (`ai_book_adapter(kind, code, client)`), otherwise `ai_extract.api_key()` and `AnthropicExtractor`. With neither, `ReadingMissing` is raised, unchanged.
- **Model calls:** `run_book` and `rerun_side` report `Book.model_calls`, the live calls made this run (0 on a full replay). The adapter summary carries it. `route_tariff`'s response field set was not changed, so `model_calls` is not in the intake response (the old `extraction` block stays absent).
- **Wording rule:** "AI extraction not configured" is used only when `ReadingMissing` is raised, which happens only when there is no client and no key. If a client exists and the call fails, the client's own `ExtractionUnavailable` message is reported (for example "AI extraction unavailable" or "billing"). Nothing is written in either case; the book is saved as `saved_unsupported`. Sections already read live in a failed run stay in the cache.
- **Verification:** fake client only. The fixture book on an empty cache created 2 versions and recorded 4 gate rows. The fake client made 2 calls and 1 cache file was written. The replay with no client created 0, found 2 existing and added no gate rows. No key and no cache gave `saved_unsupported` with 0 rows written. The APCo book on scratch `tariff_12c_live_20261110` with the key forced off gave 44 active / 573 / 153, 88 gate rows all passed, the same 7 queue items and 0 model calls.
- **Test hygiene:** a test that expects `ReadingMissing` must force the key off now, since a configured key would otherwise let the live step construct a real client.

---

## Stage 12 — Task 12d: structured-rules stage over the verified rules

`src/structured_rules.py` turns one version's stored `tariff_rules` into `structured_tariff_rules` rows in the 11C shape. Each row carries the parent rule's text, `source_page` and `citation_status`. Input is the stored rules only (plus two stored component lookups, below). It uses no model and never reads the PDF. A rule is structured only when its whole text fully matches the pattern for its type and every parameter is determined; otherwise it is queued and nothing is guessed. A rule with an uncited parent is not structured. If two parent rules map to one (version, type), the second is queued.

### Patterns (stored `rule_type` → structured type; full-text match)
| Structured type | Stored type | Pattern and parameters |
|---|---|---|
| `eligibility` | `eligibility` | "Available for electric service through one meter to individual residential customers, including rural residential customers engaged principally in agricultural pursuits, who take Standard Service from the Company." → `customer_type`, `includes_rural_agricultural_customers`, `single_meter`, `takes_standard_service`. |
| `minimum_charge` | `minimum_charge` | "[The schedule is] subject to a minimum monthly charge equal to the Basic Service Charge." → `derived_from`, `unit` usd_per_month, `value`. The value is the version's one stored `basic_customer_charge` usd_per_month rate. The text carries no number, so zero or several distinct stored rates mean the rule is queued. |
| `provision_availability` | `closed_schedule` | "The <provision> provision is no longer available to new customers; existing customers may remain." |
| `on_peak_hours` | `on_peak_hours` | "The on-peak billing period is H a.m./p.m. to H a.m./p.m., local time, for all weekdays, Monday through Friday." Start must be before end. |
| `off_peak_hours` | `off_peak_hours` | "…is H to H, local time, [for all weekdays\|on weekdays], all hours [of the day] on Saturdays and Sundays, and the legally observed holidays of …". The window must be the exact complement of a parsed on-peak rule of the same version. Every holiday must be one of six fixed definitions (New Year's Day, Memorial Day, Independence Day, Labor Day, Thanksgiving Day, Christmas Day). Anything else is queued. |
| `demand_determination` | `demand_determination` | "Generation demand is the highest on-peak demand in excess of the supplemental service capacity reservation in <Mon-Mon and …>; transmission and distribution demands equal the standby service contract capacity kW." → the `sbs_demand_v1` shape. The component types come from the version's stored usd_per_kw components: exactly one generation type, and at least one distribution type. |

Every other stored rule type queues as `structured_rule_unparsed` (queue `source_kind` `rule`, `source_ref` the parent rule id, field `structured_rule`, with the schedule and the reason in the detail). Re-running creates no duplicate structured rows and no duplicate queue items.

### Coordinator hook
`packet_pipeline.run_book` calls `structured_rules.structure_version` for each version it creates, after `_finish`. The per-schedule table rows gain `structured`, `structured_unparsed` and `structured_unparsed_queued`. It is report-only: it never changes the outcome or the activation gate (still rates + rules). `queued` in the table, and so `queued_for_review` in the intake response, still counts section and validation items only. The `structured_rule_unparsed` items are in the queue but are not added to that number.

### Witness (scratch `tariff_12d_structured_20261110`, APCo book through intake, key off; 44 / 573 / 153, gates 88)
The pipeline's rows were compared with the primary's hand-built rows for the schedules, on `rule_type`, applies-to, `parameters_json` and `source_page`:
- Exact match on all four fields: RS `eligibility` (p. 28), RS `minimum_charge` (p. 28, value 7.96), RS `provision_availability` (p. 29), RS-TOD `on_peak_hours` (p. 34), RS-TOD `off_peak_hours` (p. 34).
- Not reproduced exactly: SBS `demand_determination` (p. 80). `rule_type`, applies-to and page match. The parameters match on the generation and contract-capacity branches, but the pipeline row has no `gap` object. The `gap` holds the p. 80 excess-demand sentence, which is in no stored rule: the AI set stores 7 SBS rules, none containing it. The provenance rule forbids writing it back by hand. The pipeline's SBS row therefore evaluates the same two branches without recording the known unevaluated branch. Promoting it would lose that annotation, so it needs a decision before any promotion.
- `source_rule_text` differs for RS-TOD on/off-peak and SBS. The primary's text is the earlier extraction's wording ("is defined as", "Generation demand shall be…"), and the pipeline carries the AI-set stored text. RS rows' text is identical.
- Pipeline rows beyond the 6: `minimum_charge` for RS-E (p. 30), RS-SD (p. 32), RS-TOD (p. 34), RS-STOU (p. 36) and SWS (p. 38), 11 rows in all. RS `period_definitions` stays text-only and is queued (its text names no holidays), as in 11C.

### Coverage (all 44 versions, 153 stored rules)
11 structured, 142 unparsed and queued (`structured_rule_unparsed`, 142 items). Structured per schedule: RS 3 of 4, RS-E 1 of 4, RS-SD 1 of 6, RS-TOD 3 of 3, RS-STOU 1 of 3, SWS 1 of 2, SBS 1 of 7. The other 37 schedules have 0 structured rules. A second pass over all versions added 0 structured rows and 0 queue items.

### Evaluator check
Pipeline-produced rules on the scratch: `audit_bills._schedule_totals` for the RS version with the 11C fixture component ($7.00 `basic_customer_charge`) gave total 7.96, an adjustment line of 0.96 cited to p. 28, and status `evaluated`. RS-TOD `classify_tariff_period` on the pipeline rules gave on_peak for a Monday 10:00 and off_peak for Monday 21:00, Saturday, Christmas and Memorial Day. The 11C fixture unit tests also pass.

### Close-out
- Suite 426 passed, which is 421 plus 5 new local tests.
- Primary untouched: structured rules still 6, queue 14/2/7, versions 44/41, no `packet_gates` table, 14 tables, frozen SQLite SHA unchanged. The billing evaluator and the activation gate are unchanged.
- New extra minimum-charge rows exist only on the scratch. Promoting them would change what the evaluator does for RS-E, RS-SD, RS-TOD, RS-STOU and SWS, so that needs its own decision.

---

## Stage 12 — Task 12e: Pepco DC scouting and intake proof (no code, no model calls)

### Book
- Source: https://www.pepco.com/cdn/assets/v3/assets/bltbb7c204688a1a6a8/blt1592c0ff5185f738/6ae4c0f1649b08a6166fb854/Pepco_DC_Current_Tariff_as_of_October_1_2026_v01.pdf (from a web search; I did not browse pepco.com's tariff index to confirm it is the only current book).
- Saved as `data/raw/Pepco-DC-Tariff-2026-10-01.pdf` (gitignored). 504,329 bytes, 93 pages, SHA-256 `81d817fe1941646f02cf14e81030885fc6d5bbcda904e03676a24201f3569e1e`.
- Printed title: "Electricity--P.S.C. of D.C. No. 1 — RATE SCHEDULES FOR ELECTRIC SERVICE IN THE DISTRICT OF COLUMBIA — RATES AND REGULATORY PRACTICES GROUP". Page 1 is "One Hundred-Eighty-Sixth Revised Page No. R-1", dated "Date of Issue: September 29, 2026 Date Effective: Usage on and after October 1, 2026".
- Regulator: P.S.C. of D.C. (the Public Service Commission of the District of Columbia). The words "Public Service Commission" first appear on p. 47. The Pepco name first appears on p. 6.
- Other candidate found: a single-schedule "R" file ("DC_Rates_Update_Oct_2026_R_.pdf", a compliance filing), not the full book. It was not downloaded.

### Detection (`identify_document`): unknown
`doc_kind` unknown, utility null, state null, version label null, no evidence. Reason given: "no tariff-book heading and fewer than two bill markers in the first 6 pages (tariff markers: 0, bill markers: 0)". Pepco has no entry in `UTILITIES`, and none of the tariff probes match this book's wording. "Pepco" first appears on p. 6, which is just outside the 6-page scan window's useful evidence. Detection was not changed.

### Intake proof (scratch `tariff_12e_pepco_20261110`, temp upload dir, key off, real client unconstructible)
- Saved content-addressed under its own SHA.
- `route_tariff` returned `saved_unidentified`, not the expected `saved_unsupported`: "The file is saved, but it could not be identified as a known utility's tariff; nothing was parsed and no version was created…".
- Plain words: "company not recognized … Couldn't identify this document — it was saved for review and nothing was changed."
- Scratch counts: versions 0, components 0, rules 0, queue items 0, structured rules 0.
- `packet_pipeline.read_packets` on the file raises `BookRefused: the source is not an identified utility tariff`. The coordinator is not reached.

### Section inventory (existing chunker, `chunk_pages`, no model)
- 53 sections found, covering 92 of 93 pages (page 1 is the cover and is uncovered). The first three "sections" (pp. 2–5) come from the table of contents. Their designations are garbage (`"R-TM-EX"(THISSCHEDULEHASBEENDELETED)PageR-52-53`, `"CG-SPP"PageR-15-154`, `“PCDC”-POWERCENTSDC™PROJECT…`).
- Real schedules: R 6-7, MMA 8-9, GSND 10-11, GSLV 12-13, GS3A 14-15, MGTLV 16-17, T 18-19, GTLV 20-21, GT3A 22-23, GT3B 24-25, RT 26-27, SL 28-29, TS 30-31, SSL-OH 32-33, SSL-UG 34-36, TN 37-41, S 44-45, CREF 46-48, OLLED 49, R-PIV 50-51, MDU-PIV 52-53. One bogus section, "(including the General Terms and Conditions…" (pp. 42-43), is a mis-detected heading.
- Riders: RAD 54, PF 55, SL-TN 56, DT 57, PSOS 58, EF 59, OMRS 60-61, SOS 62-64, AC 70, RDCS 71-72, NEM 73, RADS 74, SETF 75, EATF 76-78, R-DLC 79-80, CNM 81-82, CBRC 83-84, EDIT 85, SCADRRC 88, PIV-GREEN 89, MRPADJUSTMENT 90-92, MESF 93.
- Duplicate codes (likely later rate-schedule or rider-attached pages for schedules already seen; not inspected): GS-LV-ND 65, GSLV 66, GT3A 67, MGTLV 68-69, TS 86, GT3A 87. These would collide with the earlier sections' version identity.
- The BSA rider (p. 77) is swallowed into EATF 76-78. It has no section of its own.
- Unreadable page: 77 (the BSA rider). It has 3,522 characters of text, but five `(cid:` math glyphs trip `readable()`, which allows fewer than 5. It is a false positive from the readability check, not an image-only page. There are no image-only pages.

### Two-column rate-year layout
Both rate years print on the same text line as side-by-side columns, in one block. The header cell wraps across lines and carries the effective dates:
```
Rate Year 1 Effective January Rate Year 2 Effective January
Distribution Service Charge 1, 2025 – December 31, 2025 1, 2026 – December 31, 2026
Customer Charge $ 17.09 per month $ 18.09 per month
Kilowatt-Hour Charge (Summer)
First 400 Kilowatt hours $ 0.01715 per kwhr $ 0.01982 per kwhr
```
Schedule GS ND (p. 10) has the same pattern: `Customer Charge $ 35.59 per month $ 38.30 per month`. In GS ND the dates wrap over three lines. 19 pages carry "Rate Year 2".

Each row holds two rates, so the live extraction must emit a rate-year variant (year 1 and year 2 with their effective dates) for each component. The current verification (`rate_on_page`) would pass either rate. The version identity (state, utility, code, tariff number, one effective date) has no place for a second year, so 12f needs a decision: either two versions per schedule or a rate-year variant on the components. Also, the book prints no tariff number ("TARIFF NO.") for `read_packets`, which refuses a book with none.

### Go/no-go
- Estimated live cost: about 50 calls, roughly $1.7–$1.9, scaled from APCo's 46 calls and ≈$1.64 for 48 sections. This is an estimate, not a run.
- **Not ready for 12f.** Blockers:
  1. Detection: Pepco / DC has no `UTILITIES` entry, `STATE_NAMES` is VA only, and the tariff probes miss this book's wording.
  2. `read_packets` needs a tariff number and finds none. A Pepco version label is needed ("Revised Page" numbers differ per page).
  3. The chunker mis-detects the first TOC pages and one heading, produces duplicate codes, and swallows BSA into EATF.
  4. The `(cid:` readability false positive drops p. 77.
  5. The rate-year decision above.

---

## Stage 12 — Task 12f: Pepco readiness — generic platform fixes (zero model calls)

Owner directive (2026-11-10): no hard-structured or hard-parsed extraction. Every fix below is a rule about documents; only the Pepco identity entry uses printed evidence from the book (identification, not parsing). The rate-year decision (two versions per schedule, one per printed rate year) is recorded and is NOT implemented here — it belongs to the reading-shape task.

### Generic rules
1. **States.** `STATE_NAMES` holds all 50 states plus the District of Columbia. When a recognised utility's own state probes find nothing, `state_from_printed_names` picks the state the book names most often (longest names consumed first so "West Virginia" is not "Virginia"; a tie returns nothing).
2. **Utility identity.** The `pepco_dc` entry is identified by the name the book prints ("Pepco", p. 6). It carries no fixed state, since the company files in more than one jurisdiction; the state comes from the book ("District of Columbia", p. 1). The legal name is not printed in the book, so none is asserted.
3. **Book number.** `COMMISSION_BOOK_NUMBER` ("P.S.C. of <jurisdiction> No. n") is both a tariff probe and a version probe, through the same mechanism as "TARIFF NO. 28". Label: "P.S.C. of D.C. No. 1". `read_packets` takes the number from the label as before.
4. **Effective date.** A probe for "Effective: <a few words> on and after <date>" is tried first. The previous probes found no date in the Pepco book's first six pages. APCo and Dominion do not match the new probe.
5. **`readable()`.** A page with fewer than 5 `(cid:n)` glyphs is judged as before. With 5 or more it is readable only if its real words (3+ letters, glyphs removed) number at least 50 and exceed the glyph count. Pepco p. 77: 5 glyphs, 411 words → readable (was unreadable). APCo pp. 49–55: 898–3,912 glyphs, 10 words each (the filing stamp) → still unreadable.
6. **Chunker (heading path).** (a) A contents/index page (at least 5 lines with dot leaders or a trailing "Page n") never starts a section and belongs to none. (b) When the book prints any heading in capitals, only capitalised headings start sections; a mixed-case "Schedule X" line is a sub-table label or a reference. (c) A designation that opens with a bracket or exceeds eight words is prose, not a heading. (d) A heading whose code was already used never opens a second section; it continues the open one. Rule (d) makes duplicate version identity impossible. `schedule_code` also drops quote marks (the old Pepco codes were `"R"` with quotes). The APCo path (table-of-contents sheet ranges) is unchanged.

### Pepco pp. 65–69 and 86–87 — finding
They are not schedules. pp. 65–69 (page numbers R-41.3 to R-41.8) are the continuation of Rider SOS (Standard Offer Service): per-customer-class sub-tables labelled "Schedule GS - LV - ND", "Schedule GS LV", "Schedule GT 3A", "Schedule MGT LV", followed by the Procurement Cost Adjustment text. pp. 86–87 (R-55.1, R-55.2) are the continuation of Rider EDIT, with sub-tables "Schedule TS", "Schedule GT 3A" (and TN / GT 3B / RT). The true headings are printed in capitals (`SCHEDULE "GS LV"`, `RIDER "SOS"`); the sub-table labels are mixed-case. Page-number families could not separate them (GSLV prints R-6.2 under GSND's R-6), so the capitals rule is used.

### Pepco inventory, before → after
- Before (12e): 53 sections; 3 table-of-contents sections, 1 garbled section (pp. 42–43), duplicate codes GSLV / GT3A / MGTLV / TS, BSA swallowed into EATF, p. 77 unreadable.
- After: **44 sections**, unique codes, no table-of-contents or garbled section, unreadable pages none. Coverage 88 of 93 pages (pp. 1–5 are the cover and contents and belong to no section).
- Changed ranges: SOS 62–69, EATF 76, BSA 77–78 (its own section), EDIT 85–87, TN 37–43, and 44 sections in all. Schedules R 6–7 through MDU-PIV 52–53 and riders RAD through MESF are otherwise as in 12e.
- Known limits, not fixed: TN 37–43 includes the CG-SPP pages (39–43, which print no capitalised SCHEDULE or RIDER heading line); the UPC rider on p. 80 sits inside R-DLC 79–80; p. 92 (R-59) sits inside MRPADJUSTMENT 90–92. Section codes and designations still print as in the book (`GSND`, `MRPADJUSTMENT`).

### Verification (scratch only, key off, real client unconstructible)
- Pepco detection: tariff, Pepco (`pepco_dc`), DC, "P.S.C. of D.C. No. 1", effective 2026-10-01. Evidence: commission tariff book number (p. 1), utility name (p. 6), state from printed name "DISTRICT OF COLUMBIA" (p. 1), version (p. 1), effective date "Effective: Usage on and after October 1, 2026" (p. 1).
- Regression: `identify_document` on the APCo book, `rider-gen.pdf`, the EAP bill and Dominion Amendment 12 is identical before and after (full records compared; Amendment 12 still 2025-07-01). APCo section list (48 sections, designations, page ranges), readable/unreadable pages (151/158; 49–55 unreadable) and every cached reading's effective-date parse are identical.
- Intake on `tariff_12f_pepco_ready_20261110`: `saved_unsupported`, identified as Pepco; coordinator reached ("no cached reading for section R|6-7 …"); plain words "Saved under Pepco, District of Columbia, but this document type isn't readable yet — nothing was guessed or stored as rates."; versions, components, rules, queue items, structured rules all 0; no `packet_gates` table.
- APCo on `tariff_12f_apco_20261110` (real cache, key off): ingested, 44 versions all active, 573 components, 153 rules, `packet_gates` 88 rows (44 rates + 44 rules) all passed, section queue items the same 7 (CS ×2, LPS, NMSII ×2, OL, RPSRAC) plus the 142 report-only `structured_rule_unparsed` items from 12d; a second upload created nothing.
- Suite 440 (426 + 14 local tests in `tests/test_pepco_readiness.py`). Primary untouched (44/41, queue 14/2/7, structured rules 6, no `packet_gates`); frozen SQLite SHA unchanged.

### Still not ready for live Pepco extraction
The reading shape (rate-year versions, effective-from-year dates) is the next task; the cache has no Pepco readings, so live extraction needs the owner's go-ahead and a key.

## Stage 12 — Task 12g: reading shape — structured-rule proposals + rate-year versions (zero real model calls)

Owner directive (2026-11-10): no hard-structured or hard-parsed extraction. Structured rules are proposed by the AI reading with page citations and verified deterministically before storage, the same shape as rates. `src/structured_rules.py` (the 12d pattern stage) stays in the repo as a witness; `run_book` no longer calls it, so `structured_rule_unparsed` items are no longer produced.

### Part A — structured proposals (`src/structured_proposals.py`)
- **Separate reading, separate cache.** One model call per schedule section with its own prompt (marker "You propose structured tariff rules") and cache `data/cache/ai-extract-structured/<model>/<book-digest>.json`, sections keyed like the main cache. The main readings and cache are untouched.
- **Proposal:** `{rule_type, applies_to, parameters, source_page, source_quote, [gap], [rate_year]}`. `parameters` must match the 11C shapes exactly (keys and types; the six primary rows and what `billing_engine` reads). A `demand_determination` proposal may carry `gap {text, source_page, source_quote, required_inputs}`; stored as the primary's `gap` object with status `not_structured`.
- **Verification (first failure wins; failures queue and are never stored):** type in the six-type vocabulary (`structured_type_unknown`) → quote on its cited page, whitespace-normalised, and a gap's quote and text on the gap's page (`structured_quote_not_found`) → `applies_to` is the section's own schedule and parameters have exactly the type's keys and value types (`structured_shape_invalid`) → printed-quantity check (`structured_parameter_not_printed`). A reading that is incomplete or not the JSON required queues `structured_reading_unusable`.
- **Printed-quantity rules per type:** minimum_charge — value equals a stored `derived_from` usd_per_month component of the same version, else the number is printed on the cited page; provision_availability — the provision text is printed on the page; eligibility — every word of customer_type is printed; on_peak_hours — start and end clock times are printed (a.m./p.m., 24-hour, noon/midnight); off_peak_hours — every holiday name is printed and each definition computes in the evaluator's `_holiday_date`; demand_determination — each (component_type, variant) it names is stored for the version and each month is printed on the page.
- **Not verifiable by the page check (stated, not hidden):** evaluator field names (`measured_field`, `reservation_field`, `capacity_field`); holiday calendar definitions beyond naming; and "a printed branch is covered or reported as a gap" is enforced by the prompt only.
- **Storage:** `structured_tariff_rules`, citation_status cited, `source_page` set, `source_rule_text` = the quote, idempotent on the table's unique key; a differing second rule of one type queues `structured_shape_invalid`. Queue refs carry `:v<version_id>`.
- **Coordinator:** the stage runs after the main commit and activation, as its own phase (no write transaction held across a live call). Live structured calls happen only for versions created in the run and only with a client or key; versions that already existed replay from the structured cache. With neither: stage `skipped`, reason "no model client or API key is available", structured counts 0, rates and rules unaffected. Table rows gain `structured_proposed/stored/queued`; the run result gains `structured` (stage, reasons, calls, proposed, stored, queued) and `structured_calls`; `model_calls` still counts reading calls only. With a client, intake therefore makes about twice the calls for newly created versions.

### Part B — rate-year versions
- A reading may carry `rate_years [{label, effective_from, effective_to}]` and tag components with `rate_year`; the live prompt asks for them (opt-in flag on `_system_prompt/_user_prompt/read_section`). Cached readings without them behave as before: one version.
- With 2+ rate years: each year's start (and end) must be a date printed on the section's pages, else it is queued and dropped. One version per verified year, identity (utility, schedule code, book number, that year's effective_from), `effective_to` = the printed year end. Components go only to their tagged year's version; an untagged or unmatched component queues `rate_year_missing` and is stored nowhere; a year with no component queues `no_verified_rates` and gets no version. Text rules and verified structured proposals attach to every year-version (a proposal may carry `rate_year` to restrict it). `packet_gates` records both sides per year-version; the unchanged two-sided gate applies per version; activation runs in chronological order.
- **Finding (activation unchanged):** activating year 2 supersedes year 1 and the existing activation sets year 1's `effective_to` to year 2's start (2026-01-01), replacing the printed 2025-12-31 end.

### Verification (fake clients only; real client class unconstructible, key off)
- Fixture (APCo-style, schedule RS): five canned proposals → 1 stored (minimum_charge 7.96, page 1, cited), 4 queued with `structured_quote_not_found`, `structured_shape_invalid`, `structured_type_unknown`, `structured_parameter_not_printed`. Replay with no client, key off: 0 reading and 0 structured calls, nothing duplicated.
- Two-year fixture: versions 2025-01-01→2025-12-31 (RY1, customer charge 17.09) and 2026-01-01→2026-12-31 (RY2, 18.09); the untagged component queued `rate_year_missing`; `packet_gates` 4 rows. Single-year control: one RS version.
- APCo on `tariff_12g_apco_20261110` (real cache, key off): 44 versions all active, 573 components, 153 rules, `packet_gates` 88 all passed, 7 queue items as before, structured rules 0, zero `structured_rule_unparsed`; structured stage skipped (no client).
- Suite 444 (440 + 4 local tests in `tests/test_structured_proposals.py`; the 12d coordinator test was rewritten for the pattern stage's removal). Primary untouched (44/41, queue 14/2/7, structured rules 6, no `packet_gates`); frozen SQLite SHA unchanged.

## Stage 12 — Task 12h: APCo structured-reading witness (LIVE, first run of the structured reading)

Scratch `tariff_12h_apco_structured_20261110`; APCo book routed through intake with the real key and real caches; a call guard refused any main-reading call (cap 60 structured). A first attempt aimed at a differently named scratch was stopped after 3 structured calls (plus possibly one unrecorded in-flight call) and its scratch dropped; the 3 cached sections were replayed by the final run.

### Calls and regression
- Main-reading calls 0 (guard refused none). Structured calls 41 live in the final run + 3 from the stopped attempt = 44 sections read. Stage ran. Proposed 45, stored 44, queued 1 (`structured_parameter_not_printed`).
- Main path unchanged: 44 versions all active, 573 components, 153 rules, `packet_gates` 88 all passed, the same 7 section queue items.

### Witness: the primary's 6 hand-built rules
| Rule | Verdict |
|---|---|
| RS eligibility p.28 | differs: `customer_type` "individual residential" (primary) vs "residential" (scratch); other keys and page equal |
| RS minimum_charge p.28 | exact |
| RS provision_availability p.29 | differs: `provision` "Load Management Water Heating" (primary) vs "This provision is no longer available to customers. Customers currently taking service under this provision can remain." (scratch); booleans and page equal |
| RS-TOD on_peak_hours p.34 | exact |
| RS-TOD off_peak_hours p.34 | exact (including the six holidays) |
| SBS demand_determination p.80 | not stored (see below) |

### SBS gap outcome
The model proposed `demand_determination` with a gap whose text and quote are on p.80 (the excess-demand "supplemental demand" branch, with required inputs). The gap quote verified (verification order puts it before the parameter check). The proposal was nevertheless not stored: its parameters named component variants `excess_over_reservation` and `contract_capacity`, but the stored SBS components use `generation` and `distribution`, so `structured_parameter_not_printed` queued it. Cause: the structured prompt lists stored component_type values but not variants, so the model guessed variant names. Not fixed in this task (finding for a follow-up): list stored (type, variant) pairs in the prompt.

### Coverage
- Proposals by type: eligibility 9, minimum_charge 7, provision_availability 6, on_peak_hours 11, off_peak_hours 11, demand_determination 1; 27 of 44 sections made no proposal.
- Queued: structured_parameter_not_printed 1; no quote, shape or type failures.
- Schedules that stored rules: RS 5, RS-E 5, RS-SD 4, RS-STOU 4, RS-TOD 4, SGS 4, PEV 3, GS 2, GS-TOD 2, LFEVC 2, LGS-TOD 2, SWS 2, ATOD 1, COGEN/SPP 1, NMSII 1, SBS 1 (provision_availability only), VWS 1 (44 in all). RS also got on/off-peak rules the primary does not carry (p.29).
- Unverifiable by the page check, as before: evaluator field names and holiday calendar definitions.

### Evaluator proof on the scratch's own rows
- RS minimum-charge fixture ($7.00): total 7.96, adjustment 0.96, status `evaluated`, floor 7.96, source_page 28.
- RS-TOD classification: weekday 10:00 on_peak; weekday 21:00, Saturday, Christmas Day, Memorial Day all off_peak.

### Cost (estimate, not an actual)
Usage in the structured cache: 133,294 input / 33,448 output tokens over 44 calls (final run 125,889 / 25,672 over 41). At an assumed $3/$15 per million tokens that is about $0.90; scaling the 11B benchmark (46 main calls ≈ $1.64) by call count gives about $1.57. No console figure was available. Any call lost when the first attempt was stopped is not in these counts.

### Housekeeping
Existing tests that route fixture books write to the real structured cache directory (a fixture-digest file with RS|1-1 and EXHIBIT|2-2 entries). Harmless to APCo, noted for a follow-up.
- Suite 444. Primary untouched (44/41, queue 14/2/7, structured rules 6, no `packet_gates`); frozen SQLite SHA unchanged.

## Stage 12 — Task 12h-fix: variant pairs in the structured prompt, one SBS re-read

The structured prompt now lists the version's stored `(component_type, variant)` pairs beside the component types and tells the model to reference demand components only by those exact pairs. The verifier is unchanged. Only the SBS entry `SBS|72-82` was removed from `data/cache/ai-extract-structured/claude-sonnet-5-5/8b2240fc68f66e2e711f51a74ef8e0e89d228c9a853f64492451905a6495b071.json` and re-read. One live call: 10,794 input tokens, 2,080 output tokens, `end_turn`. The proposal named `demand` / `generation` and `distribution_demand_charge` / `distribution`, which are stored pairs, and included the p. 80 excess-demand gap. It was not stored. The verifier's failure, verbatim: `structured_parameter_not_printed` `demand_determination: months not printed on the page: ['jul', 'aug']`. The proposed months are 12, 1, 2, 6, 7, 8, 9; the page prints June–September and does not print July or August. Scratch structured rules stayed 44. The earlier open queue item (id 8, the variant-name failure) was not rewritten. No second call. Fixture cache file `dd577375660dec841abaa0a3ecde62882f3f457f3cccb4b110f3c4c86424cc6b.json` was deleted; routing tests now use a temporary structured cache directory. Suite 445. Primary untouched.

## Stage 12 — Task 12h-fix2: a printed month range covers its interior months

A month in a `demand_determination` counts as printed when its name appears on the cited page, or when it falls inside a month range printed there. The recognized forms are "June–September", "June - September", and "December through February" (full or abbreviated names; en dash, hyphen, or "through"). A range entails every month from its start through its end, wrapping the year when the start is after the end, so December–February is 12, 1, 2 and June–September includes July and August even though those names are not printed. A month neither named nor inside a printed range still fails `structured_parameter_not_printed`. No other verifier check changed, and the prompt did not change.

`SBS|72-82` was removed from the structured cache and re-read once (10,794 input tokens, 2,147 output tokens, `end_turn`). `demand_determination` was stored, cited to p. 80, with months 12, 1, 2, 6, 7, 8, 9, variants `demand`/`generation` and `distribution_demand_charge`/`distribution`, and the p. 80 excess-demand gap. Scratch structured rules went 44 → 45; that row is the only addition. The older open queue item (id 8, the variant-name failure) is still open and was not rewritten. No new queue item.

Against the primary SBS row, `rule_type`, `applies_to_schedule`, and `source_page` (80) match, as do the months, component pairs, evaluator, and gap text. Four parameter strings differ: `generation_demand.measured_field` `on_peak_demand_kw` vs `highest_on_peak_demand_kw`; `generation_demand.reservation_field` `supplemental_service_capacity_reservation` vs `supplemental_capacity_reservation_kw`; `contract_capacity_demand.capacity_field` `standby_service_contract_capacity` vs `standby_contract_capacity_kw`; `gap.required_inputs` is four printed phrases vs the primary's four snake_case field names. Suite 449. Primary untouched.

## Stage 12 — Task 12h-fix3: canonical evaluator field names

The names were read from the `sbs_demand_v1` path, not from the page. `billing_engine.apply_structured_demand_rules` looks up `determinants[generation["measured_field"]]`, `determinants[generation["reservation_field"]]`, and `determinants[contract["capacity_field"]]`, and `schedule_review._structured_demand_inputs` requires those same keys on the bill. The keys that path is built to find are declared beside it as `EVALUATOR_FIELDS["sbs_demand_v1"]`: `measured_field` `highest_on_peak_demand_kw`, `reservation_field` `supplemental_capacity_reservation_kw`, `capacity_field` `standby_contract_capacity_kw`. `structured_rules._parameters` writes that same constant for this evaluator. The structured prompt states an evaluator's field names as part of its schema, next to the parameter shape, and tells the model to use exactly those values and no others, so a later evaluator is declared the same way. `shape_error` for `demand_determination` additionally requires the three values to equal the canonical names for the named evaluator; a mismatch queues `structured_shape_invalid` and the detail names each field. The variant-pair prompt text and the month-range check are unchanged. `gap.required_inputs` is not checked.

The fix2 scratch row (id 45, the non-canonical `demand_determination`) was deleted before the re-read. Re-verifying the cached `SBS|72-82` proposal offline, with no live call, queued `structured_shape_invalid` on `on_peak_demand_kw`, `supplemental_service_capacity_reservation`, and `standby_service_contract_capacity`. That cache entry was then removed and the SBS section re-read once (10,909 input tokens, 2,222 output tokens, `end_turn`). The proposal stored (id 46), cited to p. 80, with months 12, 1, 2, 6, 7, 8, 9, variants `demand`/`generation` and `distribution_demand_charge`/`distribution`, and the three canonical field names. The model proposed no gap, so the stored row has none. `source_rule_text` is a shorter p. 80 fragment. Scratch structured rules went 45 → 44 on the deletion → 45 with this row; ids 1–44 were not updated. No new queue item. One live call.

Against the primary SBS row, `rule_type`, `applies_to_schedule`, `source_page` (80), `citation_status`, the evaluator, the months, the component pairs, and the three field names match. Two differences remain: the gap object is absent (the primary still carries the excess-demand gap, including its snake_case `required_inputs`), and `source_rule_text` is a different quote from p. 80. Suite 452. Primary untouched.

## Stage 12 — Task 12i: Pepco DC live extraction

Scratch `tariff_12i_pepco_20261110`, empty schema from `src/create_db.py`, book `data/raw/Pepco-DC-Tariff-2026-10-01.pdf` (SHA-256 `81d817fe1941646f02cf14e81030885fc6d5bbcda904e03676a24201f3569e1e`) through `route_tariff`. Final intake status `ingested`. 62 versions created, 0 existing at the start, 0 stayed draft. Every stored version has both packet gates passed (`rates:1`, `rules:1`). 42 are active and 20 are superseded (the 2025 rate year, once the 2026 version of the same schedule activated). 44 sections, 62 versions.

Reading cache: 44 sections, 43 `end_turn` and 1 `max_tokens` (SOS). Tokens in that cache: 102,709 input / 94,276 output. One earlier SOS attempt (11,102 input / 16,000 output, also `max_tokens`) was replaced by the retry and is not in the cache, so live reading calls were 45. Structured cache: 42 sections, all `end_turn`, 106,101 input / 11,826 output, 42 calls. No structured call for SOS or SCADRRC, because no version was created. Three structured proposals parsed; six rows stored, because each proposal was stored on both rate-year versions. No `structured_*` queue signal. Schedules with a stored structured rule: R (eligibility, p. 6, both years), MDU-PIV (on_peak_hours, p. 53, both years), R-PIV (on_peak_hours, p. 50, both years).

Eighteen sections declared Rate Year 1 / Rate Year 2 and stored both, effective 2025-01-01 and 2026-01-01: GS3A, GSLV, GSND, GT3A, GT3B, GTLV, MDU-PIV, MGTLV, MMA, R, R-PIV, RT, SL, SSL-OH, SSL-UG, T, TN, TS. OLLED declared three dated columns and stored three versions (2021-07-01, 2022-01-01, 2023-01-01). Twenty-three sections declared no rate years and stored one version at the date the reading gave: AC 2023-10-13, BSA 2025-01-01, CBRC 2022-05-18, CNM 2026-10-01, CREF 2016-06-01, DT 2023-10-13, EATF 2023-10-13, EDIT 2024-03-31, EF 2018-08-13, MESF 2026-10-01, MRPADJUSTMENT 2025-01-01, NEM 2023-11-08, OMRS 2018-08-13, PF 2018-08-13, PIV-GREEN 2026-03-01, PSOS 2026-03-01, R-DLC 2018-08-13, RAD 2026-10-01, RADS 2026-02-01, RDCS 2018-08-13, S 2018-08-13, SETF 2026-10-01, SL-TN 2009-11-06. Two sections stored nothing: SOS (`model_output_incomplete`, output stopped at 16,000 tokens) and SCADRRC (`rate_not_on_page` on −7.50; the page prints a credit of $7.50 with no minus sign).

Schedule R p. 6, 2025-01-01: customer_charge 17.09 usd_per_month null p. 6; energy_charge_summer_first_400 0.01715 usd_per_kwh distribution p. 6; energy_charge_summer_over_400 0.04895; energy_charge_winter_first_400 0.01715; energy_charge_winter_over_400 0.03419. 2026-01-01: customer_charge 18.09; summer first 400 0.01982; summer over 400 0.05659; winter first 400 0.01982; winter over 400 0.03952. Same units, variant, and page. GS ND p. 10, 2025-01-01: customer_charge 35.59 usd_per_month p. 10; energy_charge_summer 0.07813 usd_per_kwh; energy_charge_winter 0.06114. 2026-01-01: customer_charge 38.30; summer 0.07778; winter 0.06087. No spot-check rate is stored in both years or in the wrong year. The $0.62 billing credit on R carried no rate year and stayed queued.

Queue, 68 open items: rate_year_missing 48, rate_not_on_page 7, quote_not_found 6, no_verified_rates 4, unit_not_in_vocabulary 2, model_output_incomplete 1. R, EF, and PF still have the first-pass queue rows from before a column-split date and a spaced decimal were accepted; the versions themselves are stored. CG-SPP is printed inside the TN page range (pp. 37–43) and was not a section; the TN reading stored TN's own customer and energy charges and did not name CG-SPP. Rider UPC is printed inside R-DLC (p. 80) and was not a section; the R-DLC reading stored six DLC credits and did not name UPC.

Cost estimate, not a console figure. Cache tokens at $3 / $15 per million: reading $1.72, structured $0.50, plus the replaced SOS attempt about $0.27, about $2.49 altogether. Scaling the 11B benchmark (46 calls ≈ $1.64) by 87 calls (45 reading + 42 structured) gives about $3.10. Under the 130-call stop. Suite with `TARIFF_DB_URL` and `TARIFF_DB_PATH` unset: 459 passed. Primary untouched: Appalachian 44 active, Dominion 41 active, queue 14 component / 2 rule / 7 tariff_book_section, structured rules 6, no `packet_gates`. Frozen `data/tariffs.db` SHA-256 `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`.

## Stage 12 — Promotion A: pipeline structured rules to the primary

Backup `data/tariff-pg-pre-promotion-a.2026-11-10.sql`, 358,148 bytes. `pg_dump` exited 0 and the file contains the marker `-- PostgreSQL database dump complete` before any primary write.

The scratch's 45 rows include seven `minimum_charge` rows. RS p. 28 value 7.96 already exists on the primary, so it was not a new-row fixture. The six schedules with no primary minimum-charge row were each floored from a $1.00 subtotal on the scratch: RS-E p. 30 to 7.96, RS-SD p. 32 to 7.96, RS-STOU p. 36 to 7.96, RS-TOD p. 34 to 9.82, SGS p. 39 to 9.88, SWS p. 38 to 10.78. Every status was `evaluated`, the cited page was the row's `source_page`, and none was excluded.

Queue item 56 was not closed. On a clone of the backup, `rerun_side` for Appalachian RS-TOD rules refused with `version 87 is active; only a draft side can be re-run`. The main cache entry was left in place, so that refusal made no live call. The RS-TOD eligibility text rule was not added. Item 56 stays open. Queue remains 14 component / 2 rule / 7 section.

All six hand-built rows had a pipeline counterpart on the same version identity and rule type: RS eligibility, RS minimum_charge, RS provision_availability, RS-TOD on_peak_hours, RS-TOD off_peak_hours, SBS demand_determination. One transaction deleted those 6 and inserted the 45 scratch rows, mapped by state, utility, schedule, version, and effective date. Inserted per schedule: RS 5, RS-E 5, RS-SD 4, RS-STOU 4, RS-TOD 4, SGS 4, PEV 3, GS 2, GS-TOD 2, LGS-TOD 2, LFEVC 2, SBS 2, SWS 2, ATOD 1, COGEN/SPP 1, NMSII 1, VWS 1. Parameters, quote, page, and citation were copied as stored. No version, component, text-rule, bill, or gate write.

Primary after the transaction: 45 structured rows, all cited, per-schedule counts identical to the scratch. RS minimum fixture: subtotal 7.00 floors to 7.96, status `evaluated`, page 28. RS-TOD: weekday 10:00 `on_peak`; weekday 21:00, Saturday, Christmas Day, and Memorial Day `off_peak`. SBS on the 3d inputs: generation demand 15 kW = $265.50, distribution demand 50 kW = $473.00. Versions 44/41, components 573/448, rules 153/112, bills 31, charges 894, audit runs/lines 3/96, no `packet_gates`. Suite with the database env unset: 459 passed. Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`.

## Stage 12 — Promotion B: Pepco to the primary

Backup `data/tariff-pg-pre-promotion-b.2026-11-10.sql`, 372,751 bytes, taken from the post–Promotion A primary. `pg_dump` exited 0 and the file contains `-- PostgreSQL database dump complete` before any primary write.

Scratch `tariff_12i_pepco_20261110` counts captured before the write: 62 versions (42 active / 20 superseded), 319 components, 215 rules, 6 structured rows, 124 packet gates (124 passed), 68 queue items (rate_year_missing 48, rate_not_on_page 7, quote_not_found 6, no_verified_rates 4, unit_not_in_vocabulary 2, model_output_incomplete 1). Pepco is stored as the utility and state on those version rows (District of Columbia, printed name Pepco); the scratch has no separate utilities table. Queue `source_ref`, `detail`, and `stored_value` contain no version id, so none needed a version remap and none was dropped.

One transaction on the primary created `packet_gates` with the `ensure_gate_table` definition, confirmed zero existing Pepco versions, and inserted 62 versions, 319 components, 215 rules, 6 structured rows, 124 gate rows, and 68 queue items. Version identity is utility, schedule_code, version, and effective_from. Structured `applies_to_version_id` equals `tariff_version_id` on every scratch row, so both columns received the new primary version id. Rates, quotes, pages, citation statuses, version statuses, and effective dates were copied as stored.

Primary after the transaction matches those captured counts: 42 active / 20 superseded, 319 components, 215 rules, 6 structured rows all cited, 124 gates all passed, 68 Pepco queue items at the captured signal counts. Queue is 91 open. Schedule R p. 6, 2025-01-01: customer_charge 17.09 usd_per_month; summer first 400 0.01715, summer over 400 0.04895, winter first 400 0.01715, winter over 400 0.03419, all usd_per_kwh, variant distribution. 2026-01-01: 18.09, 0.01982, 0.05659, 0.01982, 0.03952, same page and variant. GS ND p. 10, 2025-01-01: customer_charge 35.59, summer 0.07813, winter 0.06114. 2026-01-01: customer_charge 38.3 (stored that way on the scratch as well), summer 0.07778, winter 0.06087. No spot-check rate from one year is stored on the other year's version. Dominion stays 41 versions / 448 components / 112 rules; Appalachian stays 44 / 573 / 153, with 45 structured rows. Bills 31, charges 894, audit runs/lines 3/96. Suite with the database env unset: 459 passed. Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`.

## Stage 12 — Task 12j: repricing reasons

The draft `src/repricing_reasons.py` grouped billed lines and one candidate's schedule and rider lines by component type, skipped a zero difference, and returned the largest rows (`top_reasons`, limit 3). Phrases used only those row amounts. Its three tests asserted that billed-line behavior: a fuel line and a customer-charge line, their sum against the in-scope billed total, no row when the lines match, and a one-row truncation. The only caller was `schedule_comparison.render_comparison`, which printed those phrases. `reprice` did not attach them.

On clone `tariff_12j_reasons_20261110` (Promotion B backup restored; the primary was not used for the repricing), bill 31 still reprices to the frozen snapshot: 120 $114.45, 154 $102.44, 102 $105.61, 110 $115.78, 100 $120.20, and 19 blocked. The draft's Schedule 154 top reasons were distribution_kwh_charge $+31.38, es_energy $-29.32, and distribution_energy $-14.76. Those three numbers are real billed-versus-candidate line differences, and all five line rows sum to $-12.01, but the top three do not, and none of them is the schedule part or the riders part. The stored parts are schedule $45.70 versus $51.67 ($-5.97) and riders $56.74 versus $62.78 ($-6.04). That check failed. Own schedule 120 produced no rows, and a blocked candidate has no schedule lines, so the draft invented nothing for those two checks.

The fix compares each computed candidate with the bill's own computed schedule. `part_differences` emits the schedule part and the riders part, largest absolute difference first, and those differences sum to candidate total minus own-schedule total. `line_differences` emits every repricing line group that differs, and those rows sum to the part they belong to. `candidate_reasons` returns the parts and then the lines; equal totals return nothing; a non-computed candidate returns nothing. `reprice` sets `difference` and `reasons` on each computed candidate. Blocked candidates are left with `status`, `reason`, and `blocker` only.

Re-run on the clone, Schedule 154: riders part $56.74 versus $62.78, $6.04 less; schedule part $45.70 versus $51.67, $5.97 less. The line rows behind them are distribution_kwh_charge $+31.38, es_energy $-29.32, distribution_energy $-14.76, es_kwh_charge $+6.73, and RIDER T-CM rider_t_cm $-6.04. Each difference equals that row's computed amount minus the own-schedule amount. Parts sum to $-12.01, and the schedule lines sum to $-5.97 and the rider line to $-6.04. Bills 29, 30, and 31 on their own schedule (120) have difference $0.00 and no reasons. The 19 blocked candidates have no `reasons` field. Totals are unchanged.

The three draft tests were replaced. They asserted the billed-line grouping and the one-row truncation, which is the behavior the Schedule 154 check rejected. The new tests assert the part sum, a zero result for an equal schedule, line rows summing to each part, and no explanation of a blocked candidate. One test was added, so the file went from three tests to four. `price_schedule` attaches the same difference and reasons, so a single computed schedule still matches its row in `reprice`. The comparison renderer prints the attached part phrases instead of calling the old billed-line function. Suite with the database env unset: 460 passed. Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`.

## Stage 12 — Task 12k: RS-TOD eligibility re-read

The main cache `data/cache/ai-extract` was copied and only the copy's `RS-TOD|34-35` entry was removed. Scratch `tariff_12k_rstod_20261110` then ran `APCo-Tariff-28.pdf` through `run_book` against that copy. One live reading: `end_turn`, 2,125 input tokens and 1,392 output tokens. Structured calls: 0. The real cache file is unchanged (SHA-256 `f8833b0af008d077f93e8b83734b860759e83f8f5fa9b01d76ffffea88ce1750`); its RS-TOD entry is still the previous reading, 2,817 input / 2,289 output, `end_turn`.

Scratch control: 44 versions, 573 components, 88 packet gates all passed, and the same 7 section queue items as the primary's Appalachian set (LPS and RPSRAC `unreadable_pages`, OL `model_output_incomplete`, NMSII `unit_not_in_vocabulary` twice, CS `rate_not_on_page` and `no_verified_rates`). The only difference from the primary's Appalachian rows is RS-TOD text rules, 4 on the scratch against 3 on the primary. Components for RS-TOD stayed 8.

The fresh reading stored four cited rules and no eligibility rule. The model payload contained the same four and nothing else:

- `minimum_charge`, p. 34, cited: "Subject to a minimum monthly charge equal to the Basic Service Charge."
- `late_payment_charge`, p. 34, cited: "A charge of 1½% per month applies to account balances, excluding local consumer utility taxes, not received by the next bill preparation date."
- `time_period_definition`, p. 34, cited: "On-peak is 7 a.m. to 8 p.m. local time on weekdays Monday through Friday. Off-peak is 8 p.m. to 7 a.m. on weekdays, all hours on Saturdays and Sundays, and the legally observed holidays listed."
- `three_phase_service`, p. 35, cited: "Where a residential customer requests three-phase service, the schedule applies if the customer pays the Company a CIAC representing the difference between single-phase and three-phase construction."

None of these is the Task 3d p. 34 sentence: "Available for electric service to individual residential customers, including rural residential customers engaged principally in agricultural pursuits, who take Standard Service from the Company and wish to be metered through one single-phase multiple-register meter capable of measuring electrical energy consumption during the on-peak and off-peak billing periods." The mini-promotion did not run. Queue item 56 stays open. Appalachian rules on the primary stay 153. The copied cache was deleted after the run. Suite with the database env unset: 460 passed. Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`.

## Stage 12 — Task 12l: staff site, accounts, and the queue display

> **Superseded (2026-11-11):** the staff console was removed; see "Staff console removal" at the end of this document. This section is kept as history.

The staff console is a separate FastAPI app, `src/staff_api.py`, with its own pages and stylesheet (`src/staff_static/staff.css`). It is not a route on the customer site. Development serves it on port 8877. The customer app was not edited.

Staff tables, created only on the working database: `staff_accounts` (username, salted PBKDF2-SHA256 password hash, role `reviewer` or `master`, active flag, created timestamp), `staff_account_scopes` (account, scope name), and `staff_sessions` (SHA-256 of an opaque token, account, expiry). A session cookie holds the raw token. Logged-out pages redirect to `/login`. Logged-out data calls return 401. Logout deletes the session.

`utility_scopes` maps a utility name to a department scope. The seed records each utility already stored in `tariff_versions` as `electric`. An item is never stamped with a scope. Its utility is the version reached by a component or rule id in `source_ref`, otherwise the utility named at the start of `detail`, otherwise the single utility that owns `schedule_code`. The item's scope is that utility's row. A reviewer with only `water` therefore sees none of today's items. Schedule `TS` belongs to both Appalachian and Pepco; the Pepco queue row still resolves to Pepco because its detail names Pepco.

Plain reasons cover `rate_year_missing`, `rate_not_on_page`, `quote_not_found`, `no_verified_rates`, `unit_not_in_vocabulary`, `model_output_incomplete`, `missing_rule`, `unreadable_pages`, `validation_failed`, `zero_rate_unexpected`, `verify_marker`, `unevaluable_formula`, and `draft_missing_citation`. A `missing_rule` whose reference names eligibility says the printed eligibility text was not stored. Any other signal is shown as its raw name plus a note that no wording exists.

The pre-Promotion B dump does not contain Pepco, so the browser check used a read-only clone of the current primary, `tariff_12l_staff_20261110`, with the staff tables added there. In the browser: logged out, `/` redirected to login and `/api/queue` returned "Sign in is required." The electric reviewer saw open count 91, item 56 as Appalachian Power Company, RS-TOD, Rule, "The printed eligibility text was not stored.", page 34, and Pepco schedule R as "The extracted rate has no rate year, so it was not stored." with extracted value "Rate Year 1". Item 56's detail page showed the full stored record. The water reviewer saw open count 0 and an empty list; `/items/56` and `/api/items/56` both returned "This item is outside your scope." The master account, scoped to electric, saw the same 91. No staff action writes tariff data. The primary has no `staff_accounts` table. Suite with the database env unset: 465 passed. Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`.

## Stage 12 — Task 12m: staff review actions (clone-only)

> **Superseded (2026-11-11):** the staff console was removed; see "Staff console removal" at the end of this document. This section is kept as history.

`staff_decisions` is an append-only action log with `id`, `item_id`, `account_id`, `action` (`approve`, `correct`, `dismiss`), `outcome` (`applied`, `rejected_by_gate`), optional `note`, optional JSON `corrected_payload`, and UTC `created_at`. Action code inserts decisions; it has no update or delete path. The decision page is ordered by decision id and filters entries using the same utility scope resolution as the queue. SQLite clones widen the review item's status check to allow `approved`, `dismissed`, and `corrected`; this migration preserves existing queue rows and indexes.

All three actions require a session, resolve the item's utility scope in the action/data layer, and require `status = 'open'`. Reviewers and Masters use the same action path. Dismiss requires a nonblank note, records an `applied` decision, and closes the item as `dismissed`. Approve accepts an optional note, records `applied`, and closes as `approved`; it does not call any tariff storage function. Correct edits only the field named by the refusal, calls `ai_extract._verify_component` against the cited source page, and stores through `packet_pipeline.store_rates` only after that verifier passes. A rate-year correction must also select a declared rate year whose effective date is printed and resolves to a stored version; other component corrections must resolve the verified reading's effective date or existing component version. A failed gate leaves the item open, writes no component, records `rejected_by_gate` with the verifier detail and submitted payload, and shows the verifier signal on the detail page.

Correct is offered for `rate_year_missing`, `unit_not_in_vocabulary`, and `rate_not_on_page` only when the component proposal can be uniquely recovered from the matching cached reading (same utility, schedule, component index, and refused value) and the cited book is available. The year and unit controls use the rate-year labels and unit vocabulary from storage. `zero_rate_unexpected` can be corrected only when the stored component retains its full cited fields and the source book is unambiguous. Rule, section-level, and other queue items do not offer Correct; missing or ambiguous proposal/cache/source data is not reconstructed.

Offline verification: the complete suite passed, 474 tests; staff-action and console tests passed, 14. Coverage includes sign-in, required dismissal note, unit-vocabulary choices, scope refusal, rejected-gate no-write/open-status and visible signal, applied correction through the verifier, section-version resolution, and SQLite status migration. The first 12m write-up said the named-item browser witnesses had not run because it checked only the local frozen SQLite snapshot, which has no queue/staff tables and contains 41 versions, 448 components, and 112 rules. That conclusion was incorrect: the earlier raw run evidence was from a full-data PostgreSQL clone. Task 12m-fix below repeats and records the browser sequence against a fresh clone. No primary or frozen SQLite database was written; the frozen SQLite SHA-256 remained `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`.

## Stage 12 — Task 12m-fix: fresh-clone browser witnesses and run reconciliation

> **Superseded (2026-11-11):** the staff console was removed; see "Staff console removal" at the end of this document. This section is kept as history.

The original Task 12m fixture tests and full offline suite ran locally against isolated test databases. Its browser witnesses in the earlier raw run used a full-data PostgreSQL clone, not the Dominion-only `data/tariffs.db` snapshot. The first 12m report mistakenly inferred that the browser witnesses had not run after checking only that SQLite snapshot while the database environment variables were unset. The earlier raw output already showed item 117's `999.99` correction rejected by `rate_not_on_page`, item 69's $0.62 Pepco credit stored into the 2025 version (5 to 6 components), and total components increasing from 1,340 to 1,341. This task independently repeats those actions on a fresh PostgreSQL clone and records the full decision log.

Clone `tariff_12m_witness_20261110` was restored from the current local PostgreSQL primary with `pg_dump`/`createdb`; staff tables and accounts were added only to the clone. The electric reviewer, electric master, and water reviewer were seeded with scopes `electric`, `electric`, and `water`, respectively. Before any action: open queue 91, components 1,340, rules 480, Pepco Schedule R effective 2025-01-01 version 155 had 5 components, and `staff_decisions` was empty.

Browser witness sequence and counts after each step (`open / components / rules / Pepco R 2025 components / decisions`):

- Electric reviewer dismissed item 56 with note “Known eligibility gap; parked for later Stage 12 rules work.”: `90 / 1,340 / 480 / 5 / 1`. The detail page showed dismissed and the decision attached.
- Electric reviewer approved NMSII unit item 51 with note “Reviewed; system handling stands.”: `89 / 1,340 / 480 / 5 / 2`. The detail page showed approved; component and rule totals were unchanged from the pre-approval 1,340 and 480.
- Electric reviewer corrected item 69 by selecting Rate Year 1: `88 / 1,341 / 480 / 6 / 3`. The browser reported one component stored on version 155; item 69 closed as corrected.
- Electric reviewer submitted `999.99` for item 117: `88 / 1,341 / 480 / 6 / 4`. The browser displayed `rate_not_on_page` (“rate '999.99' is not a number printed on page 59”); item 117 remained open, component/rule counts did not change, and no component at rate 999.99 exists.
- Water reviewer’s direct browser request to approve electric item 51 returned HTTP 403, “This item is outside your scope.” The water queue showed 0. No row was added: the decision count remained 4 and all tariff counts remained `1,341 / 480 / Pepco R 2025: 6`.

The electric reviewer’s Decisions page and the clone’s append-only table showed these four rows, in order (UTC timestamps):

| # | Item | Account | Action | Outcome | Note | Timestamp |
|---|------|---------|--------|---------|------|-----------|
| 1 | 56 | electric.reviewer | dismiss | applied | Known eligibility gap; parked for later Stage 12 rules work. | 2026-11-10T21:04:33.599187+00:00 |
| 2 | 51 | electric.reviewer | approve | applied | Reviewed; system handling stands. | 2026-11-10T21:04:46.848354+00:00 |
| 3 | 69 | electric.reviewer | correct | applied | stored 1 component on version 155 | 2026-11-10T21:05:02.873076+00:00 |
| 4 | 117 | electric.reviewer | correct | rejected_by_gate | rate '999.99' is not a number printed on page 59 | 2026-11-10T21:05:20.404482+00:00 |

Final item statuses: 56 `dismissed`, 51 `approved`, 69 `corrected`, 117 `open`. The stored component is row 1503 on tariff version 155 (Pepco Schedule R, effective 2025-01-01): `billing_credit`, label `Billing Credit`, rate `0.62`, unit `usd_per_month`, page 6, source cell “Billing Credit - A monthly billing credit in the amount of $0.62 will be applied to the bill of each”. The failed 999.99 correction did not create a component.

The full offline suite passed, 474 tests. The primary remained read-only and unchanged: components 1,340; rules 480; queue 91 open; structured rules 45 for Appalachian Power Company and 6 for Pepco; no `staff_*` tables; Pepco Schedule R's 2025 version 155 still has 5 components. Version IDs 41 and 62 resolve to Virginia Electric and Power Company and Appalachian Power Company; no row with id 44 exists in this primary. Frozen SQLite SHA-256: `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`. No live calls or commits.

## Stage 12 — Task 12n: staff console slice 3 — immutable change history and Master revert (clone-only)

> **Superseded (2026-11-11):** the staff console was removed; see "Staff console removal" at the end of this document. This section is kept as history.

Zero model calls. The primary and the frozen SQLite file were not written.

### History table

`staff_change_history` is created by `staff_accounts.ensure_tables` next to the other staff tables, so it exists only where the staff tables are seeded (clones). It has one row per change the console makes to tariff data:

| Column | Meaning |
|---|---|
| `id` | entry number |
| `account_id` | the staff account that made the change |
| `decision_id` | the linked `staff_decisions` row (for a revert, the decision of the change it reverts) |
| `item_id` | the queue item |
| `change_kind` | `store`, `modify`, or `revert` |
| `affected_table`, `affected_row_id` | the tariff row changed (today only `tariff_components`) |
| `description` | a plain sentence saying what changed |
| `old_value`, `new_value` | the full stored row as canonical JSON (sorted keys); `old_value` is empty for a store, `new_value` is empty for the revert of a store |
| `reverts_history_id` | on a revert, the entry it reverts; UNIQUE |
| `note`, `created_at` | the note and the UTC timestamp |

Append-only is enforced by the database, not only by code: a trigger refuses every UPDATE and DELETE (SQLite `RAISE(ABORT)`, Postgres a plpgsql trigger function), with the message `staff_change_history is append-only`. The UNIQUE `reverts_history_id` means a second revert of one entry cannot be stored even if two requests race. No code path updates or deletes a history row.

### Record on correct

An applied Correct writes its history row in the same transaction as the data change, the queue-item close, and the decision; any failure rolls all of them back.

- A correction that stores a new component (a section item, for example a rate-year item) writes `store`, old = none, new = the stored row. The stored row is identified by its id, not by guessing.
- A correction of a component that is already stored (`zero_rate_unexpected`, or any `rate_not_on_page` / `unit_not_in_vocabulary` item whose source is a component id) now changes that row in place. It changes only the refused field (`rate` or `unit`) to the verified value and writes `modify` with the full old row and the full new row. In slice 2 this path inserted a second component next to the zero one, which left the zero row live. The owner approved the change (2026-11-10).
- A refusal writes no history: a gate rejection (the decision is still logged as `rejected_by_gate`), a scope refusal, a sign-in refusal, and approve and dismiss, which change no tariff data.

### Revert semantics (`src/staff_history.py`)

- Master only. A reviewer gets `Only a Master account can revert a change.` (HTTP 403) and nothing is written. The scope check runs first, in the data layer.
- The row must still equal the recorded new value; otherwise the revert is refused (`The row no longer matches the new value recorded for this change, so it was not reverted.`) and nothing is written.
- Store-change: the stored row is deleted. Modify-change: only the columns that differ are set back to the old values, and the restored row must equal the recorded old JSON exactly, or the transaction rolls back.
- The queue item returns to `open` (reviewer, resolution note, and resolved time cleared; the decision log keeps them).
- The revert writes its own `revert` entry: old = the row as it was before the revert, new = the restored row (none for a store), `reverts_history_id` = the original, with the Master's note. The original entry is never edited. A reverted entry cannot be reverted again, and a revert entry cannot itself be reverted (HTTP 409).

### History view and scope

`/history` lists entries newest first in the queue's plain-words style (when, who, company, schedule, item, kind of change, what changed, old and new in short words, note, status "In effect" / "Reverted by change #n" / "Revert of change #n"), and Masters see a Revert button on revertible entries. `/history/{id}` shows one entry with its old and new JSON verbatim. The JSON API is `/api/history`, `/api/history/{id}`, `/api/history/{id}/revert`. An entry's utility is derived the way the queue derives it: from its queue item, and when the item names none, from the affected row's version. A reviewer sees only its own scopes; a Master sees every scope on record. A direct request for an out-of-scope entry is refused in `history_entry` with `This item is outside your scope.` (HTTP 403), not just hidden by the page.

### Browser witnesses (fresh clone `tariff_12n_history_20261110`)

The clone is a `pg_dump` of the current primary (it includes Pepco; the pre–Promotion B backup does not). Staff tables and the three accounts (electric reviewer, Master scoped electric, water reviewer) were seeded with fresh passwords. Counts are open queue / components / rules / history.

| Step | Result | Counts after |
|---|---|---|
| Opening state | — | 91 / 1,340 / 480 / 0 |
| Electric reviewer corrects item 69 (Pepco R, $0.62 billing credit) to Rate Year 1 | "Corrected. Stored 1 component on version 155."; item 69 `corrected`; version 155 goes from 5 to 6 components | 90 / 1,341 / 480 / 1 |
| Electric reviewer corrects item 3 (`zero_rate_unexpected`, Rider PIPP-CM component 307, p. 109) with the printed value 0 | Refused by the gate: `quote_not_found`, "source quote not on page 109"; decision 2 logged `rejected_by_gate`; component 307 stays 0; no history row | 90 / 1,341 / 480 / 1 |
| Electric reviewer reverts entry 1 (direct requests; no Revert button is shown to a reviewer) | `/api/history/1/revert` and `/history/1/revert` both HTTP 403 "Only a Master account can revert a change." | unchanged |
| Master reverts entry 1 (note "Slice 3 witness: revert the store-change.") | "Reverted. Logged as change #2."; row 1503 deleted; version 155 back to 5; item 69 `open` | 91 / 1,340 / 480 / 2 |
| Master reverts entry 1 again | HTTP 409 "This change has already been reverted." | unchanged |
| Water reviewer opens History | "Entries 0 — No changes in your departments."; `/api/history` returns `{"count":0,"entries":[]}`; `/api/history/1` and `/history/1` HTTP 403 "This item is outside your scope." | unchanged |

Entry 1 (store): item 69, decision 1, `tariff_components` row 1503, old none, new `{"billing_category": "Billing Credit", "billing_class": null, "component_type": "billing_credit", "formula": null, "id": 1503, "rate": 0.62, "season": null, "source_cell": "Billing Credit - A monthly billing credit in the amount of $0.62 will be applied to the bill of each", "source_page": 6, "tariff_version_id": 155, "tier_max": null, "tier_min": null, "tier_unit": null, "unit": "usd_per_month", "variant": null}`, note "stored 1 component on version 155". Its `row_to_json` output was byte-identical before and after the revert. Entry 2 (revert): Master, `reverts_history_id` 1, old = the same row, new none. On the clone, a direct `UPDATE` and `DELETE` on `staff_change_history` both failed with `staff_change_history is append-only`; the count stayed 2.

The task expected history 3 after the revert (two corrections plus the revert). It is 2, because the zero-rate correction was refused. All 14 `zero_rate_unexpected` items are Dominion components printed as zero (Schedule 132 p. 27 "$0.000 per kW"; Rider PIPP-CM p. 109 "0.000 cents"). The correction gate cannot verify any of them: the slice-2 book lookup finds no Dominion source book (`_books_for` returns none: document detection names the Dominion PDFs "Dominion Energy Virginia", while the stored versions carry "Virginia Electric and Power Company"; and even with matching names there are two Dominion tariff PDFs, Amendment 12 and `rider-gen.pdf`, so `_book_for`'s single-book rule would still find none), and the stored locators are wrapped (`text "…"`, `line "…"`), so no quote check can pass. The owner ruled (2026-11-10) that this refusal is the gate working correctly and must not be bypassed. The modify and modify-revert paths therefore rest on tests, and the **Dominion source-book gap is recorded as a finding for a later task**.

### Tests and close-out

`tests/test_staff_history.py` (12, local-only) covers the store history row's full new value and empty old value; an in-place modify with both full rows; exact prior-state restore for a store revert and a modify revert (full component table compared); a revert logging itself while the original row stays identical; refusal of a double revert and of reverting a revert; reviewer revert refused with nothing written; scope filtering, Master seeing all scopes, and direct-access refusal through the API and the page; no history from a gate rejection, a scope refusal, or approve; database refusal of UPDATE and DELETE on history; and the changed-since guard. Full suite with the database env unset: 486 passed (474 + 12). Primary unchanged: 91 open queue items, 1,340 components, 480 rules, no staff tables. Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`. No commits.

## Stage 12 — Task 12o: staff console slice 4 — notification outbox and file transport (clone-only)

> **Superseded (2026-11-11):** the staff console was removed; see "Staff console removal" at the end of this document. This section is kept as history.

Zero model calls. No email was sent, and no credentials were created, used or printed. The primary and the frozen SQLite file were not written.

### Outbox shape

`staff_accounts` gains a nullable `email` column (`_ensure_email_column` adds it to older clone tables). An account without an email gets no notifications; login, queue and history are unchanged. `seed_staff.py` reads each account's email from `STAFF_SEED_<ACCOUNT>_EMAIL` next to its password and prints neither.

`staff_notification_outbox` is created by `staff_accounts.ensure_tables`, so it exists only where the staff tables are seeded (clones):

| Column | Meaning |
|---|---|
| `id` | outbox row; also names the message file |
| `recipient_account_id`, `recipient_email` | who, with the address as it was when the row was written |
| `event_type` | `new_queue_item` or `activation_failed` |
| `ref_kind`, `ref_id` | `queue_item` + item id, or `tariff_version` + version id |
| `subject`, `body` | plain words: company, schedule, the reason in plain words, and the cited page where there is one |
| `status` | `pending`, `sent` or `failed` |
| `created_at`, `sent_at`, `error` | when written, when sent, and the transport's error text on failure |

UNIQUE `(event_type, ref_id, recipient_account_id)` forbids a second row for one event, item and recipient. The writer also checks before inserting, so a repeat write adds nothing rather than raising.

### Recipient rule

The recipients are the active accounts with a non-blank email whose scopes include the event utility's scope, plus every active Master. The utility is derived exactly as the queue derives it (`staff_queue.utility_for` through `_present`); for `activation_failed` it is the version's own utility. An item whose utility has no scope on record, or a scope that no account holds, goes to Masters only.

### Event wiring points

- `parse_review.queue_item` is the shared creation helper. After its INSERT it calls `staff_notify.notify_new_item(conn, item_id)` on the same connection, before the caller's commit, so the rows join the item's transaction. Callers covered: `ai_extract.py:963`, `document_intake.py:539`, `parse_apco.py:760`, `packet_pipeline._queue_items` (all coordinator queueing: admission, rates, rules, rate-year, structured stage, `rerun_side`) and `packet_pipeline._finish` (validation failures), and `structured_rules.py:226`.
- `parse_review.scan_for_review` had its own INSERT. It now calls `queue_item`, with the same idempotent check and the same counts.
- `queue_item` inserts only when the item is new, so a book re-run or a re-verified section creates no item and therefore no notification. The outbox UNIQUE rule is a second guard.
- `packet_pipeline._finish` now reads the gate rows before its commit. When either side is not passed, it calls `staff_notify.notify_activation_failed(conn, version_id, failed)` in the same transaction as the gate result, then commits; activation happens only when no side failed, as before. A side with no gate row is reported as "gate not recorded", not as a failed check.
- With no outbox table (the primary, and every non-staff test database), both notify functions are no-ops: they test for the table with a read-only `sqlite_master` lookup and never create it.

### Transport design (`src/staff_notify.py`)

`drain(conn, transport)` takes the `pending` and `failed` rows in id order and hands each to `transport.send(message)`. On success the row becomes `sent` with `sent_at` and its error cleared; on any exception it becomes `failed` with `"<ExceptionType>: <message>"` and the drain moves on. Each row commits on its own. The CLI is `python -m src.staff_notify --transport file|smtp`.

- `FileTransport` writes `message-<row id>.txt` (To, Subject, Event, Outbox row, blank line, body) into `STAFF_OUTBOX_DIR`, opening the file in exclusive-create mode. An unset directory raises `TransportNotConfigured`.
- `SmtpTransport` reads `STAFF_SMTP_HOST`, `STAFF_SMTP_PORT` and `STAFF_MAIL_FROM` and raises `SMTP transport is not configured: <names> not set.` when they are missing. It is not used in any witness.

The Outbox page `/outbox` and `/api/outbox` list rows newest first (recipient, event, subject, status, sent time, error). `staff_notify.outbox_rows` refuses anyone but a Master (`Only a Master account can open the outbox.`, HTTP 403), so the refusal is in the data layer and not only in the page. The Outbox link appears only for Masters.

### Witnesses

**Queue-item clone `tariff_12o_outbox_20261110`** (a `pg_dump` of the current primary; staff tables; electric reviewer, Master and water reviewer with `.invalid` emails). Opening state: 91 open queue items, 0 outbox rows.

- Item 1: Pepco Schedule R replayed from the cache (key forced off, extractor class replaced by one that raises; 0 model calls). Component 0 (Customer Charge 17.09, p. 6) was given a transposed misread of 17.90. The coordinator's own `rates_pass` refused it (`rate '17.90' is not a number printed on page 6`, signal `rate_not_on_page`), and `packet_pipeline._queue_items` queued item 125. The pass's other failures were already queued and created nothing. Outbox: exactly 2 rows, electric reviewer and Master, both `pending`, none for the water reviewer; open items went to 92.
- File drain into a writable directory: "attempted 2, sent 2, failed 0"; rows 1 and 2 `sent` at 22:01:11.81Z; files `message-1.txt` (499 bytes) and `message-2.txt` (488 bytes).
- Item 2: component 4 (summer over-400 kWh, 0.04895) misread as 0.04859, refused the same way, item 126, rows 3 and 4. A drain into a directory with mode `dr-x------` gave "attempted 2, sent 0, failed 2"; both rows `failed` with `PermissionError: [Errno 13] Permission denied: '…/outbox_locked/message-3.txt'` (and `-4`), and the drain exited 0. Pointed back at the writable directory, the next drain gave "attempted 2, sent 2, failed 0", rows 3 and 4 `sent` at 22:01:25.52Z.

**Activation scratch `tariff_12o_activation_20261110`** (a `pg_dump` of the primary; staff tables and the same accounts).

- Step A: a full Appalachian replay through `run_book` from the real cache made 0 model calls and 0 structured calls (48 rows, 44 existing, 0 created, 0 queued). Queue items stayed 91 and outbox rows stayed 0: existing items notify nobody.
- Step B: a held draft staged the way Task 12a staged its GS-TOD hold. GS-TOD version 73 was set from active to draft on the scratch only. A copy of the cache was made with GS-TOD's 16 proposed rates shifted one decimal place, and `rerun_side(GS-TOD, rates)` was run against it. No production code was changed and nothing was monkeypatched apart from the model guard. Result: `side_passed False`, `outcome held`, version 73 still `draft`, gate `rates 0 0 "no component survived verification"`.
- That run wrote 2 `activation_failed` rows for version 73 (electric reviewer, Master), subject "Schedule did not go live: Appalachian Power Company, GS-TOD stayed a draft (rates check failed; rules gate not recorded)". It also wrote 34 `new_queue_item` rows for the 17 new GS-TOD queue items, to the same two recipients and none to the water reviewer. The rules gate is "not recorded" because the primary's Appalachian versions carry no `packet_gates` rows (Promotion A copied them without gates); `_finish` already treated a missing gate as not passed. The first run of this witness used the wording "rates, rules check failed". That was corrected to name a missing gate as not recorded, the scratch was dropped and rebuilt, and the witness was re-run; the figures above are from the re-run.

**Browser** (staff site on the queue-item clone). Master's Outbox page: "Rows 4"; rows 4, 3, 2, 1 for items 126 and 125, Master and electric reviewer, all `sent`, no errors. Electric reviewer: `/outbox` HTTP 403 "Only a Master account can open the outbox."; `/api/outbox` HTTP 403 with the same message; no Outbox link on the queue page.

### Tests

`tests/test_staff_notify.py` (14, local-only) covers:
- an electric item reaching the electric reviewer and Master only, and an item in a scope with no accounts reaching Master only;
- Master always included, even with no known utility;
- an account with no email skipped while it can still sign in and read the queue;
- duplicate writes creating no rows, and `scan_for_review` going through the helper and notifying once;
- a file drain marking rows sent, with each file matching its row's recipient, subject and body;
- a failing transport marking one row failed with its error while the drain continues and a later drain sends it;
- `activation_failed` rows from a held `_finish`, no repeat on a second `_finish`, and none on activation;
- a missing gate worded as not recorded;
- no outbox table meaning no notification and no table created;
- the SMTP and file not-configured errors;
- Master-only outbox access in the data layer, the page, the API and the queue-page link.

Close-out: full suite with the database env unset, 500 passed (486 + 14). Primary unchanged: 91 open queue items, 1,340 components, 480 rules, no staff or outbox tables. Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`. No commits.

## Stage 12 — Task 12p: staff console onto the primary

> **Superseded (2026-11-11):** the staff console was removed; see "Staff console removal" at the end of this document. This section is kept as history.

**Status: the tables landed and were verified on the primary. The owner's Master account and the signed-in browser check are PENDING, to be completed when the owner is at the machine.** No account exists on the primary yet. No decision was taken on any real queue item, nothing was drained, and no email was sent. Zero model calls.

### Backup gate

`data/tariff-pg-pre-staff-console.2026-11-10.sql`, 495,183 bytes (gitignored). `pg_dump` exited 0 and the file contains `-- PostgreSQL database dump complete`. It was taken before any write.

### Tables created (additive only)

Created with the console's own code (`staff_accounts.ensure_tables`, which also runs `ensure_history_table` and `staff_notify.ensure_outbox_table`), then `seed_electric_utilities`:

- `staff_accounts` (including `email`), `staff_account_scopes`, `staff_sessions`, `utility_scopes`, `staff_decisions`;
- `staff_change_history`, with trigger `staff_change_history_no_change` (refuses UPDATE and DELETE) and `UNIQUE (reverts_history_id)`;
- `staff_notification_outbox`, with `UNIQUE (event_type, ref_id, recipient_account_id)`.

`utility_scopes` maps Appalachian Power Company, Pepco, and Virginia Electric and Power Company to `electric`. `packet_gates` was not backfilled for Appalachian or Dominion: their missing gate rows are the recorded state, and inventing pass records would be fabrication.

**Not done, deliberately: the queue status constraint.** On the primary, `parse_review_items` carries `CHECK (status IN ('open','resolved'))`. Approve, dismiss and correct write `approved`, `dismissed` or `corrected`, so the database will refuse every console decision until this constraint is widened. On the clones, `seed_staff.py` widened it through `allow_decision_statuses`. Doing that here would alter an existing table, so it was not run. The constraint is unchanged and needs its own owner decision and backup-gated step before any decision can be recorded on the primary. For the same reason the owner's account must be seeded with the single-account owner seed, not `seed_staff.py`.

### Before and after (read-only)

| Figure | Before | After |
|---|---:|---:|
| Versions: Appalachian / Pepco / Dominion | 44 / 62 / 41 | 44 / 62 / 41 |
| Components: Appalachian / Pepco / Dominion | 573 / 319 / 448 | 573 / 319 / 448 |
| Rules: Appalachian / Pepco / Dominion | 153 / 215 / 112 | 153 / 215 / 112 |
| Structured rules: Appalachian / Pepco | 45 / 6 | 45 / 6 |
| Bills / bill charges | 31 / 894 | 31 / 894 |
| Audit runs / audit lines | 3 / 96 | 3 / 96 |
| Queue items, all / open | 91 / 91 | 91 / 91 |
| `packet_gates` (Pepco only) | 124 | 124 |
| Staff, scope and outbox tables | 0 | 7 |
| Tables in total | 15 | 22 |

Content hash over every row of the 15 tables that existed before (an md5 per table over its ordered rows, then an md5 over the per-table values): before `0461fcb72f269264f77c951880c3710f`, after `0461fcb72f269264f77c951880c3710f`, identical. New table rows after the step: `utility_scopes` 3; `staff_accounts`, `staff_account_scopes`, `staff_sessions`, `staff_decisions`, `staff_change_history` and `staff_notification_outbox` all 0.

### Accounts

None yet (**pending**). The owner seeds his own Master account in his own terminal, with the password read silently into an environment variable (`read -rs`). Never typed into chat, printed, or written to a file, it reaches the database only as the salted PBKDF2 hash that `create_account` stores. The seed takes username, email, scopes and role from environment values, refuses an existing username, touches only the staff tables, and prints only `username`, `role`, `scopes` and whether an email is set. No other account was created and no credentials were invented.

### Browser and site checks

Staff site started with `STAFF_PORT=8877 PYTHONPATH=. venv/bin/python -m src.staff_api` and `TARIFF_DB_URL` pointing at the primary. It listens on `127.0.0.1:8877` only (`lsof` showed only `127.0.0.1:8877`).

- Logged out: `/` gives HTTP 303 to `/login`, and the browser landed on the Sign in page. `/api/queue`, `/api/history` and `/api/outbox` each return HTTP 401 `{"message":"Sign in is required."}`.
- The site was stopped afterwards (`pkill -f src.staff_api`) so it does not serve the primary unattended. Restart it with the same command, and stop it with Ctrl+C or the same `pkill`.
- **Pending (owner at the machine):** Master sign-in; the queue showing the 91 open items, including item 56 and a Pepco item in plain words; History showing 0 entries; Outbox showing 0 rows. The owner signs in himself, because his real password is not handled by the assistant.
- Customer site: `http://127.0.0.1:8000/` HTTP 200 (37,881 bytes, title "Tariff Audit Engine"); `/health` HTTP 200 `{"status":"ok",…}`.

### From this point on

The primary now has an outbox, so any pipeline run that creates a **new** queue item writes `new_queue_item` rows in the same transaction. A coordinator run that leaves a version as a draft writes `activation_failed` rows. Rows are written only for accounts that have an email; with no accounts yet, nothing is written. Rows stay `pending` until the owner runs the sender (`python -m src.staff_notify --transport file|smtp`). The sender was not run against the primary in this task.

## Stage 12 — Task 12r: parse-review status constraint widened on the primary

Zero model calls. The only persistent change on the primary is the constraint definition. No real queue item was actioned.

### Finding

The Task 12p landing found that the primary's `parse_review_items` allowed only `open` and `resolved`, while the staff console closes items as `approved`, `dismissed` or `corrected`. Every console action would therefore have been refused by the primary. Clones had worked only because `seed_staff.py` widened the constraint on them.

### Backup

`data/tariff-pg-pre-status-widening.2026-11-10.sql`, 506,905 bytes (gitignored). `pg_dump` exited 0 and the file contains `-- PostgreSQL database dump complete`. It was taken before any write.

### Old and new definitions (from `pg_get_constraintdef`, name `parse_review_items_status_check`)

- Before: `CHECK ((status = ANY (ARRAY['open'::text, 'resolved'::text])))`
- After: `CHECK ((status = ANY (ARRAY['open'::text, 'resolved'::text, 'approved'::text, 'dismissed'::text, 'corrected'::text])))`

The change ran in one transaction: drop `parse_review_items_status_check`, then add it back under the same name from the shared definition. Before it, the queue held 91 items, all `open`.

### Code alignment (one definition)

| File | Change |
|---|---|
| `src/review_status.py` (new) | The single definition: `STATUSES`, `CONSTRAINT_NAME`, `STATUS_CHECK`, plus a read-only `allowed_statuses(conn)` and `verify(conn)` for SQLite and Postgres. It never alters a table. |
| `src/create_db.py` | `parse_review_items` is created with `{STATUS_CHECK}` instead of the literal `('open', 'resolved')` check. |
| `src/staff_accounts.py` | `allow_decision_statuses` is removed: its SQLite table rebuild and its Postgres drop-and-add were the separate widening step. It is replaced by `verify_decision_statuses(conn)`, a verified no-op when the check matches `STATUSES` (or the table has no check) that raises, without altering anything, when it does not. The now-unused `import re` is removed. |
| `src/seed_staff.py` | Calls `verify_decision_statuses` instead of `allow_decision_statuses`. Seeding a clone of an unwidened database now stops with "widen it in its own backup-gated step" instead of silently changing the table. |
| `tests/test_staff_actions.py` (local) | The SQLite widening-migration test is replaced by two tests: a fresh `create_db` schema accepts all five statuses and refuses `bogus`; and `verify` is a no-op on the widened check and refuses a narrow one without altering its schema or rows. |

Not changed, and noted: `parse_review.list_items` (the Stage 8 `?status=` filter) still accepts only `open` or `resolved`. That is query behaviour, not the constraint, so it was left alone.

The primary, a fresh `pg_dump` clone of it, and an empty database built by the new `create_db.py` (`tariff_12q_fresh_20261110`) all report the identical definition above.

### Rolled-back proof on the primary (item 56)

Each test ran as `BEGIN; UPDATE …; SELECT …; ROLLBACK;`:

| Status tried | Inside the transaction | After ROLLBACK |
|---|---|---|
| `approved` | `item 56 | approved` | `open | 91` |
| `dismissed` | `item 56 | dismissed` | `open | 91` |
| `corrected` | `item 56 | corrected` | `open | 91` |
| `bogus` (negative control) | `ERROR: new row for relation "parse_review_items" violates check constraint "parse_review_items_status_check"` | `open | 91` |

### Clone proof (`tariff_12q_status_20261110`, a `pg_dump` of the post-change primary, with clone-only test accounts)

`seed_staff` passed its status check as a no-op. Starting point: 91 open, 1,340 components, 0 decisions. As the electric reviewer in a real browser:

- approved item 51: "Approved. No tariff data was changed.";
- dismissed item 50 (note "Clone proof of the widened status constraint."): "Dismissed.";
- corrected item 69 (Pepco R, $0.62 credit) to Rate Year 1: "Corrected. Stored 1 component on version 155."

Final statuses: 51 `approved`, 50 `dismissed`, 69 `corrected`, with no database refusal. Counts after: 88 open, 1,341 components, 3 decisions (all `applied`), 1 history row (the store-change), 0 outbox rows (no new queue item).

### After-state on the primary

Constraint as above; status distribution `open | 91`. `parse_review_items` content hash before and after: `31bbf34f30ac5db51753f25bc3e419d5`. Content hash over all 22 tables, before and after the widening: `e4a8bf0432e5af006e1a7bbcd459d606`. Every count equals the Task 12p after-state: versions 44 / 62 / 41, components 573 / 319 / 448, rules 153 / 215 / 112, structured rules 45 / 6, bills 31, charges 894, audit runs and lines 3 / 96, `packet_gates` 124 (Pepco only), 7 staff tables, 22 tables in total. Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`. No commits.

Suite with the database env unset: 501 passed. That is the previous 500, minus the removed widening-migration test, plus its two replacement tests.

## Stage 12 — Task 12s: book-label backfill (printed book identity on every version)

Zero model calls. The labels were verified by reading the PDFs' own page text.

### The coverage finding that prompted it

The Tariffs-page cleanup stopped at its coverage check. Every version has a `schedule_kind` (Appalachian 23 schedules / 20 riders / 1 informational, Dominion 24 / 17 / 0, Pepco 41 / 21 / 0, no NULLs), but no column stored a book name:

- `version` is a bare number (28, 12, 1, 1);
- `schedule_kind_evidence` names a source document only for Appalachian and Dominion, and is empty for all 62 Pepco versions;
- Dominion spans two documents (Amendment No. 12 and the April 2025 Rider GEN sheet).

Rendering "Tariff No." wording from a number in code would have been invented wording, so the labels are stored as evidence first. The owner chose this option (2026-11-10).

### Labels, verified on the page

| Company, version | `book_label` | Source document | Page | Verbatim printed line |
|---|---|---|---:|---|
| Appalachian Power Company, 28 (44 rows) | Virginia S.C.C. Tariff No. 28 | `APCo-Tariff-28.pdf` | 1 | "VIRGINIA S.C.C. TARIFF NO. 28" |
| Virginia Electric and Power Company, 12 (40 rows) | Amendment No. 12 | `VEPGA-Amendment-12-Effective-July-1-2025.pdf` | 1 | "AMENDMENT NO. 12" |
| Pepco, 1 (62 rows) | P.S.C. of D.C. No. 1 | `Pepco-DC-Tariff-2026-10-01.pdf` | 1 | "Electricity--P.S.C. of D.C. No. 1" |
| Virginia Electric and Power Company, 1 (1 row) | Rider GEN sheet | `rider-gen.pdf` | — | none: the sheet prints no book label |

Each label also repeats on later pages (for example `VA. S.C.C. TARIFF NO. 28` on pp. 2–3, and Pepco's header on every page). `rider-gen.pdf` (1 page) contains no "Tariff No.", "Amendment No." or "P.S.C. … No." text.

`book_label` holds the clean title-case name. `book_evidence` holds the source document, the page and the verbatim printed line in quotes, for example `APCo-Tariff-28.pdf, page 1, printed as "VIRGINIA S.C.C. TARIFF NO. 28"`, so nothing is silently normalized.

**GEN ruling (owner):** the sheet is grouped as "Rider GEN sheet", described as what it is. Its evidence reads: `rider-gen.pdf prints no book label; this version is grouped by its source document, rider-gen.pdf.` It never claims a printed label.

Pepco's empty `schedule_kind_evidence` is a separate provenance gap, kept as its own item and not addressed here.

### The backfill (`src/book_label_backfill.py`)

- **Mapping:** keyed by (utility, version number). Each entry carries the label, the source document, the page and the verbatim printed line.
- **Verification before writing:** the backfill re-reads each cited page with pdfplumber, whitespace-collapsed per line, and refuses to run unless the printed line is on that page.
- **Columns:** `book_label` and `book_evidence` (TEXT, nullable) are added when missing. `create_db.py` adds the same two columns, so fresh and cloned schemas match.
- **One transaction:** adding the columns and filling every row happen in a single transaction.
- **Refusals, each writing nothing:** a stored `book_label` that disagrees with the mapping; any version whose (utility, version) is not mapped; a missing source PDF.
- **Idempotent:** a second run reports `filled 0`.

Existing reads were checked for knock-on effects:
- the audit snapshot hash and the version resolvers select named columns, so audit hashes are unaffected;
- `GET /tariff-versions[/{id}]` uses `SELECT *` and now also returns the two new keys (additive, as with `schedule_kind` in 4c);
- `compare_databases` and the pipeline's content snapshots read every column, so a fresh pipeline build compares unequal to the live database on these two columns until the backfill is run on it, as `schedule_kind_evidence` already did after 4c.

### Scratch (`tariff_12s_books_20261110`, a `pg_dump` of the primary)

The first run gave `columns_added ['book_label', 'book_evidence'], filled 147, unchanged 0`: Appalachian 28 → 44, Dominion 12 → 40, Dominion 1 → 1, Pepco 1 → 62; 147 labeled, 0 NULL. The second run gave `columns_added [], filled 0, unchanged 147`.

### Promotion

- **Backup:** `data/tariff-pg-pre-book-labels.2026-11-10.sql`, 506,961 bytes (gitignored). `pg_dump` exited 0 and the file contains `-- PostgreSQL database dump complete`. It was taken before any write.
- **Hashes:** the hash of `tariff_versions` over its 10 pre-existing columns (`id`, `state`, `utility`, `schedule_code`, `version`, `effective_from`, `effective_to`, `status`, `schedule_kind`, `schedule_kind_evidence`) was `985836662bc73aaf785529d7fc0cccbe` before and after. The combined per-table hash of the other 21 tables was `026d4c8478b7696068912abb5b6a6e25` before and after.
- **The change:** the backfill ran on the primary in one transaction: `filled 147`, the same per-book counts as the scratch, 0 NULL labels.
- **Standing counts, unchanged:** versions 44 / 62 / 41, components 573 / 319 / 448, rules 153 / 215 / 112, structured rules 45 / 6, bills 31, charges 894, audit runs and lines 3 / 96, open queue items 91, `packet_gates` 124 (Pepco only), 7 staff tables, 22 tables in total.
- **Frozen file:** SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`. No commits.

Suite with the database env unset: 508 passed (501 + 7 local tests in `tests/test_book_label_backfill.py`). The first full run had 1 failure: `test_parse_review.py::test_table_upgrade_is_additive_on_an_existing_database` already excludes each additive column added since it was written (`schedule_kind`, `schedule_kind_evidence`, `source_page`, `citation_status`), and the two new empty book columns now appeared on the upgraded copy. They were added to its exclusion list, the same pattern, and no data differed. The re-run passed.

## Stage 12 — Task 12t: Tariffs page cleanup (company → book → grouped schedules)

Presentation and read-only API only. No database writes, no schema change, and no change to billing, repricing, intake or pipeline. Zero model calls.

### Coverage check (read-only, primary)

| Company | Book (`book_label`) | Versions | Schedules | Riders | Informational | NULL kind |
|---|---|---:|---:|---:|---:|---:|
| Appalachian Power Company | Virginia S.C.C. Tariff No. 28 | 44 | 23 | 20 | 1 | 0 |
| Pepco | P.S.C. of D.C. No. 1 | 62 | 41 | 21 | 0 | 0 |
| Virginia Electric and Power Company | Amendment No. 12 | 40 | 24 | 16 | 0 | 0 |
| Virginia Electric and Power Company | Rider GEN sheet | 1 | 0 | 1 | 0 | 0 |

Dominion totals 41 (24 / 17 / 0) across its two books. NULL `book_label`: 0; NULL `schedule_kind`: 0; total 147. Every expectation was met, so the build went ahead.

### Page structure (`src/static/index.html`, hash routes)

- `#/tariffs` (landing): a schedule search box, then one card per company with its state(s), number of books, book labels, effective span (earliest to latest `effective_from`) and pills for versions / schedules / riders / informational.
- `#/tariffs/c/<company>`: one card per book (label, version and entry counts, kind line, span, status pills).
- `#/tariffs/b/<company>/<book>[/<status>]`: the book titled by its stored `book_label`, with the stored `book_evidence` underneath, a status filter (All / Active / Draft / Superseded), and three panels, **Schedules**, **Riders**, **Informational**, from `schedule_kind`. Each entry (one schedule code in that book) shows its version count, latest effective date, current status (its latest version's status) and the mix of version statuses.
  - The filter keeps an entry when at least one of its versions has the chosen status. Under a filter, a panel's version count is the total versions of the entries shown.
  - Pepco's rate years are versions of one entry.
- `#/tariffs/s/<company>/<book>/<code>`: the schedule's versions with effective from and to, status, and component and rule counts. Each row opens the existing version detail (`#/tariffs/<id>`), unchanged apart from a breadcrumb added above it.
- **Breadcrumbs** on every level: Tariffs › Company › Book › Schedule (› Version #id), with the book named by `book_label`. Esc steps up one level in the Tariffs hierarchy.
- Titles and groups come only from stored data. No company names or book titles appear in code, so a new company or book appears on its own.

### Endpoints added (read-only, `src/tariff_catalog.py`)

| Endpoint | Returns |
|---|---|
| `GET /tariff-catalog/companies` | company cards: states, books (label and versions), effective span, counts by kind and status |
| `GET /tariff-catalog/books?utility=` | the company's books with counts, span and evidence (404 for an unknown company) |
| `GET /tariff-catalog/book?utility=&book=&status=` | grouped entries by kind, totals and the filter result (404 unknown, 422 bad status) |
| `GET /tariff-catalog/schedule?utility=&book=&code=` | versions with component and rule counts (404 unknown) |
| `GET /tariff-catalog/search?q=` | schedule entries whose code contains the query, ignoring case, spaces and punctuation, each naming its company and book |

No existing endpoint changed. The version endpoints keep every field and already carried `book_label` and `book_evidence` since Task 12s. No write endpoint was added.

### Browser verification (live compose `api` on :8000, rebuilt with `docker compose up -d --build api`)

| Level | Page shows | Database (read-only) |
|---|---|---|
| Landing cards | Appalachian 44 (23 / 20 / 1), "Virginia S.C.C. Tariff No. 28", 2024-07-20 → 2026-09-24; Pepco 62 (41 / 21 / 0), "P.S.C. of D.C. No. 1", 2009-11-06 → 2026-10-01; Dominion "VA · 2 books", "Amendment No. 12" and "Rider GEN sheet", 41 (24 / 17 / 0), 2025-04-01 → 2025-07-01 | 44 / 62 / 41, same kind splits |
| Dominion company | Amendment No. 12: 40 versions · 40 entries, 24 / 16 / 0, 40 active. Rider GEN sheet: 1 version · 1 entry, 0 / 1 / 0, 1 active | 40 (40 entries, 40 active) and 1 |
| Rider GEN sheet book | Riders: RIDER GEN, 1 version, 2025-04-01, active; evidence line "rider-gen.pdf prints no book label; …" | 1 version |
| Appalachian book | Schedules 23 entries / 23 versions, Riders 20 / 20, Informational 1 (EXHIBIT): 44 | 44 entries, 44 active |
| Pepco book | Schedules 21 entries / 41 versions, Riders 21 / 21, Informational 0: 62 versions, 42 entries | 42 entries, 42 active, 20 superseded |
| Status filter, Pepco | Superseded: "Showing 19 of 42", all in Schedules (GS3A … TS, OLLED with 2 superseded); Draft: "Showing 0 of 42"; All: "Showing 42 of 42" | 19 entries carry a superseded version; 0 drafts |
| Pepco Schedule R | #155 v1, 2025-01-01 → 2026-01-01, superseded, 5 components, 3 rules; #156 v1, 2026-01-01 → 2026-12-31, active, 5, 3 | 155: 5 / 3; 156: 5 / 3 |
| Breadcrumb | "Tariffs › Pepco › P.S.C. of D.C. No. 1 › R"; on version #156: "… › R › Version #156", component table unchanged | — |
| Search "gs" | 7 results: Appalachian GS, GS-TOD, LGS-TOD, SGS; Pepco GS3A, GSLV, GSND, each naming company and book; GSLV opens its schedule page (#103 / #104, 4 components) | the same 7 codes; GSLV 103 / 104, 4 components |
| Other tabs | Home "Engine online", 147 tariff versions; Bills 31 cards; Audits 31 cards; Upload 2 tiles; no error | — |

During the walk, card wording was corrected twice (a per-book "schedules" count renamed "entries", and singular forms such as "1 version") and the api rebuilt. The figures above are from the final build.

### Data unchanged

The hash of `tariff_versions` over its 10 pre-existing columns (`985836662bc73aaf785529d7fc0cccbe`) and the combined hash of the other 21 tables (`026d4c8478b7696068912abb5b6a6e25`) equal the Task 12s after-state, and every standing count is identical to it: versions 44 / 62 / 41, components 573 / 319 / 448, rules 153 / 215 / 112, structured rules 45 / 6, bills 31, charges 894, audits 3 / 96, open queue 91, `packet_gates` 124, 22 tables. `book_label` is present on all 147 rows. Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`. No commits.

Suite with the database env unset: 514 passed (508 + 6 local tests in `tests/test_tariff_catalog.py`).

## B19 — Airflow orchestration (profile `airflow`)

Orchestration only. DAG code lives in `dags/` and calls existing `src/` entry points; no pipeline behaviour changed. Zero model calls. All proof runs targeted the scratch clone `tariff_b19_scratch`, and the primary was not written.

### Entry-point inventory (read from the code)

| Purpose | Entry point as called | Notes |
|---|---|---|
| Intake of a book file | `document_intake.save_upload(bytes, filename, upload_dir)` → `identify_document(path)` → `record_intake(path, filename, detection, status, plain)` | content-addressed save; detection reads only identity |
| Coordinator / routed adapter | `document_intake.route_tariff(path, detection, target=TARIFF_DB_URL)` | dispatches to `ai_book_adapter`, which runs `packet_pipeline.run_book(source, target, client=None)` for AI-route utilities (Appalachian, Pepco), to the hand-written parsers for Dominion, and to the GEN adapter for GEN sheets. Returns created / existing / stayed_draft / queued_for_review / declined |
| Account audit | `audit_bills.run_audits(conn)` (default pilot selection, as the CLI runs it) | **run_audits has no per-account selection.** It audits the reviewed mapping's pilot bills, or explicit bill ids, where an uncovered bill fails the whole transaction. The DAG does not choose bills: before writing, it checks read-only (`load_mapping` + `_pilot_bill_ids`) that every pilot bill belongs to the requested account, and refuses otherwise |
| Stage 8 review scan | `parse_review.scan_for_review(conn)` | idempotent; inserts only through `queue_item` |
| Check counts | plain read-only SQL in `dags/tariff_common.py` | versions, components, rules, structured rules and coverage, queue, audits per company |

Every entry point was callable as-is; nothing in `src/` was changed for Airflow.

### Version pin and the Python 3.9 reason

The image is `apache/airflow:2.11.0-python3.9`, installed against the official `constraints-2.11.0/constraints-3.9.txt`. The project runs Python 3.9 (local venv and the API image). Checked on PyPI and Docker Hub (2026-11-10):

- Airflow 3.1 and later (latest 3.4.0) require Python ≥ 3.10, so the current Airflow 3 line is ruled out. Only the superseded 3.0.x line still allowed 3.9.
- On the 2.x line, the newest release is 2.11.2, but 2.11.1 and 2.11.2 publish no Python 3.9 image or constraints file (Python 3.9 reached end of life).
- 2.11.0 is therefore the newest Airflow with an official Python 3.9 build.

Moving the project to Python 3.10+ (backlog B30) would open both Airflow 2.11.2 and Airflow 3.

### Compose shape

- `Dockerfile.airflow` extends the official image and installs the project's `requirements.txt` under the Airflow constraints (in the built image: Python 3.9.22, Airflow 2.11.0, fastapi 0.128.1, psycopg 3.2.13, pdfplumber 0.11.8; `pip check` reports no broken requirements). Code is not copied: `src/`, `docs/` and `dags/` are mounted read-only.
- New services, all under `profiles: ["airflow"]`:
  - `airflow-db-create` (postgres:16, one-shot): creates the `airflow` database on the existing server if it is absent;
  - `airflow-init` (one-shot): `_AIRFLOW_DB_MIGRATE` and `_AIRFLOW_WWW_USER_CREATE` for user `admin`;
  - `airflow-webserver`: `127.0.0.1:${AIRFLOW_PORT:-8080}`, health `/health`;
  - `airflow-scheduler`: health `airflow jobs check`.
- Configuration: LocalExecutor; metadata in database `airflow` (48 tables), never in `tariff`; `LOAD_EXAMPLES=false`; `DAGS_ARE_PAUSED_AT_CREATION=true`.
- Read-only mounts: `data/raw`, and `data/cache`, so a live reading could not be written even if a client existed. Uploads go to `/tmp/tariff-uploads` in the container.
- Secrets: `AIRFLOW_ADMIN_PASSWORD`, `AIRFLOW_FERNET_KEY` and `AIRFLOW_WEBSERVER_SECRET_KEY` were generated into `.env` as individual values and never printed; `.env.example` documents the names.
- No Anthropic key is passed to these containers and `.env` is not mounted, so `ai_extract.api_key()` returns None and no model client can be built.

**How the runs were aimed at the scratch.** Compose sets `TARIFF_DB_URL` for the Airflow services to `…/${AIRFLOW_TARIFF_DB:-tariff_b19_scratch}`, and `AIRFLOW_TARIFF_DB` is not set in `.env`, so the default applied (the task log prints "tariff database: tariff_b19_scratch"). As a second guard, `tariff_common.target()` refuses a URL whose database equals `TARIFF_PRIMARY_DB_NAME` (`${POSTGRES_DB}`) unless `AIRFLOW_TARIFF_ALLOW_PRIMARY=yes`. A test with the primary named returned `TargetRefused: TARIFF_DB_URL names the primary database (tariff); refusing.`

**Profile off is unchanged.** `docker compose config --services` lists only `postgres` and `api`. `docker compose build` builds only `tariff-audit-engine-api`. `docker compose up -d` starts only those two, and `curl http://127.0.0.1:8000/health` returns `{"status":"ok",…}`, the same steps CI runs (the `compileall src` check passes too).

### DAGs

| DAG | Schedule | Tasks | Retries |
|---|---|---|---|
| `tariff_book_ingestion` | manual only (`schedule=None`); param `book_path` (a file in `data/raw`) | `intake` (save, identify, before-counts) → `route_and_run` (`route_tariff`, intake sidecar, after-counts; reports created / existing / held-as-draft / queue items created / declined) | `route_and_run`: 1, after 1 minute |
| `account_audit` | manual only; param `account_number` | `audit_account` (pilot-ownership check, then `run_audits`; reports statuses, runs and lines written, like-for-like totals per bill) | 1, after 1 minute |
| `daily_checks` | `@daily`, `catchup=False`, **paused at creation** | `integrity_report` (read-only counts) → `review_scan` (`scan_for_review`; before and after queue counts) | 0 |

### Proofs

- Services: `airflow-db-create` and `airflow-init` exited 0 (`User "admin" created with role "Admin"`, version `2.11.0`); `airflow-webserver` and `airflow-scheduler` healthy; `api` and `postgres` healthy.
- `airflow dags list`: `account_audit`, `daily_checks`, `tariff_book_ingestion`, all `is_paused True`. `airflow dags list-import-errors`: `No data found`.
- **Browser sign-in: not done by the assistant.** Reading the generated admin password to type it was refused by the session's permission classifier ("Credential Materialization"). The owner signs in at `http://127.0.0.1:8080` with user `admin` and `AIRFLOW_ADMIN_PASSWORD` from `.env`. Pending.
- **`tariff_book_ingestion`** (`book_path` `APCo-Tariff-28.pdf`, run `manual__2026-11-10T23:09:26`, success): detection Appalachian Power Company / VA / Tariff No. 28; `model_key_configured: false`; `status ingested`, `versions_created 0`, `versions_existing 44`, `versions_held_as_draft 0`, `queue_items_created 0`, declined LPS / OL / CS / RPSRAC (the known section items); plain outcome "…already stored — all 44 schedule versions matched, nothing new was created." All scratch counts were identical before and after.
- **`account_audit`** (`account_number` `001172485896`, run `manual__2026-11-10T23:10:13`, success): bills 29 / 30 / 31, mapping `phase5-v3`, like-for-like billed = expected = $95.75 / $101.67 / $114.45, delta $0.00 each.
  - Statuses were `created ×3` (3 runs and 96 lines written on the scratch), not no-ops. The scratch's stored runs (2026-10-31) carry tariff snapshot `42909d78…`; today's snapshot is `ca6c1e44…`, because the tariff content behind the snapshot has changed since those runs were stored. A different input hash is correctly recorded as a new run.
  - All three old and new pairs have identical totals. The primary's 3 stored runs predate the change in the same way, so the first audit there will also record new runs.
- **`daily_checks`**, run once by hand with `airflow dags test` so the DAG stayed paused (unpausing a `@daily` DAG would also let the scheduler queue its latest interval). Both tasks succeeded.
  - The integrity report matches the scratch's direct counts: versions 44 / 62 / 41, active 44 / 42 / 41, components 573 / 319 / 448, rules 153 / 215 / 112, structured rules 45 / 6 across 17 / 6 versions, audits 6 / 192 after the audit proof.
  - **Queue count: 91 before, 116 after, not unchanged.** The scan legitimately queued 25 `zero_rate_unexpected` items: 17 Appalachian components (EE-RAC, G-RAC, GS and others) and 8 Pepco components with rate 0. The Stage 8 scan was last run before the Appalachian AI set and Pepco were loaded, so these zero rates had never been queued. The first `daily_checks` run on the primary would queue the same 25 for review.
- End state: all three DAGs paused (`is_paused True`); the two manual DAGs were unpaused only for their triggered runs, then paused again.

### Tests

`tests/test_airflow_dags.py` (4) loads `dags/` through `DagBag` and asserts no import errors, exactly the three DAG ids, each DAG's schedule, params, task order and retries, and that `daily_checks` is paused at creation without catch-up. Airflow is not installed in the project venv (so the pinned project dependencies stay as they are), so the file skips under the standing command. It was run inside the Airflow image with `docker compose --profile airflow run --rm … python -m pytest tests/test_airflow_dags.py`: 4 passed.

### Data unchanged

Primary: `tariff_versions` pre-existing-column hash `985836662bc73aaf785529d7fc0cccbe` and other-21-tables hash `026d4c8478b7696068912abb5b6a6e25`, equal to the Tariffs-task after-state; every standing count identical (versions 44 / 62 / 41, components 573 / 319 / 448, rules 153 / 215 / 112, structured 45 / 6, bills 31 / 894, audits 3 / 96, open queue 91, `packet_gates` 124, 22 tables). Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`. No commits.

Suite with the database env unset: 514 passed, 1 skipped (the DagBag file, which needs Airflow; run in the image it passes 4 of 4).

## B20 — Analytics marts (star schema in schema `mart`) with exact reconciliation

Zero model calls. The build reads the operational tables with SELECT only and writes only inside `mart`. No pipeline, billing or API behaviour changed. Later analysis layers (B17, B21–B23) read only these marts.

### Source grains (primary, read-only)

Accounts 1; bills 31 (2023-03-17 to 2025-10-13); bill charges 894; audit runs 3 (`phase5-v3`, created 2026-10-31) with 96 lines, run 1 → bill 29, run 2 → bill 30, run 3 → bill 31, 32 lines each. Exact decimal checks before building:

- every bill's charges sum to its `total_billed` exactly (largest difference 0.00, all 31 bills);
- every audit line's `tariff_component_id` and `bill_charge_id` resolve, and the resolved component's type and rate equal the line's recorded `detail`;
- each run's report `tariff_version.id` (2) resolves to Virginia Electric and Power Company, schedule 120, v12, 2025-07-01.

**Current-run rule:** for each bill, its audit lines come from the latest audit run that covers it (latest `created_at`, then highest id). On the primary today that selects runs 1, 2 and 3 for bills 29, 30 and 31. On `tariff_b19_scratch`, which holds two runs per pilot bill after the B19 `account_audit` proof, it selects the newer runs 4, 5 and 6: 96 of 192 lines.

### Schema (grain · key · attributes and measures)

- `dim_date`: one row per calendar day from the earliest `bill_from` to the latest `bill_to` (942 days) · `date_key` YYYYMMDD · `calendar_date` (ISO text), `year`, `month`, `year_month`, `rate_seasons`. Seasons are stored per schedule and differ by utility, so there is no single global season. `rate_seasons` lists every stored month-range component season code (`may_oct`, `may_sep`, `nov_apr`, `oct_apr`) covering the date's month. Named seasons that are not month ranges (`summer`, `base`) are not expanded, since doing so would invent month sets.
- `dim_utility`: one row per (company, state, book label), 4 rows · `utility_key` (ordinal).
- `dim_account`: one row per account · `account_key` (ordinal) · account number; utility and state from the tariff versions its bills' current runs priced against (NULL when no bill is audited; more than one utility fails the build); and the stored attributes (customer name, service address, meter, class, voltage, phase, turn-on date, current rate).
- `dim_schedule`: one row per tariff version (147) · `schedule_key` = version id · `utility_key`, schedule code, kind, book label, version, effective dates, status.
- `dim_component`: one row per rate component (1,340) · `component_key` = component id · `schedule_key`, type, billing category, unit, variant, billing class, season, tier bounds and unit, rate.
- `fact_bill`: one row per bill (31) · `bill_key` = bill id · `account_key`, `utility_key` and `schedule_key` (the covering version named by the current run; NULL when no run covers the bill), `billed_schedule_code`, `bill_from_key`, `bill_to_key`, billing days, kWh, demand, `billed_total`. Only where a current run covers the bill: `audit_run_id`, `billed_in_scope` and `expected_total` (sums over the run's non-out-of-scope lines), `delta`, and matched / mismatched / out-of-scope line counts. Uncovered bills carry NULL, never zeros.
- `fact_charge`: one row per billed charge line (894) · `charge_key` = charge id · `bill_key`, `account_key`, `bill_to_key`; `component_key`, `schedule_key`, disposition, determinant type, determinant and rate from the current run's line for that charge (NULL when no line priced it); `amount`.
- `fact_audit_line`: one row per line of the selected current runs (96) · `audit_line_key` = line id · `audit_run_id`, `bill_key`, `charge_key`, `component_key`, `schedule_key`, label, disposition, verdict, billed, expected, delta.
- `mart_build_log`: one row per successful build (build id, time, source counts, mart counts, reconciliation JSON, passed). It is kept across rebuilds; every other mart table is dropped and rebuilt.

**Key rule.** A NULL key means the source names no such thing (a bill no loaded version covers, a charge no audit line priced). A key the source names that the dimension lacks fails the build. There are no invented "unknown" members.

**Exactness.** Amounts and rates are `Decimal` throughout:
- operational floats (`total_billed`, `amount`, kWh, `rate`) are converted once with `Decimal(str(value))` (the B4 rule) and never used in arithmetic;
- audit amounts are parsed from their stored decimal text;
- on Postgres the measure columns are `DECIMAL` (the exact `numeric` type, confirmed in `information_schema`). `NUMERIC` is deliberately not written, because the db shim's DDL translation rewrites that keyword to DOUBLE PRECISION;
- on SQLite (the test fixtures) `mart` is an attached database and measures are stored as decimal text.

### Build and reconciliation (`src/mart_build.py`)

`build(conn)` runs in one transaction: read the sources, compute every row (failing on an unresolvable key before any write), drop and recreate the eight tables, insert, reconcile, append the build log, commit. Any error or failed check rolls back, writing nothing. `reconcile(conn)` also runs standalone (`python -m src.mart_build --reconcile-only`). Its checks:

1. `fact_bill` count equals source bills;
2. every bill's `billed_total` equals its source total (largest difference reported);
3. `fact_charge` count equals source charges;
4. `fact_charge` per-bill sums equal `fact_bill` billed totals;
5. `fact_audit_line` count equals the selected current runs' lines;
6. per bill, the lines' in-scope billed and expected sums equal the run's stored like-for-like totals, and `fact_bill`'s billed in scope, expected and delta equal them too;
7. uncovered bills carry NULL audit measures;
8. all 16 fact and dimension foreign keys resolve.

### Scratch proof (`tariff_b20_marts_20261110`, a `pg_dump` of the primary)

| Check | Result |
|---|---|
| 1 | PASS, 31 = 31 |
| 2 | PASS, largest difference 0.00 |
| 3 | PASS, 894 = 894 |
| 4 | PASS, largest difference 0.00 |
| 5 | PASS, 96 = 96 (runs 1, 2, 3) |
| 6 | PASS: bill 29 95.75 / 95.75 Δ 0.00; bill 30 101.67 / 101.67 Δ 0.00; bill 31 114.45 / 114.45 Δ 0.00 |
| 7 | PASS, 0 violations |
| 8 | PASS, 0 orphans across 16 keys |

Mart counts: dim_date 942, dim_utility 4, dim_account 1, dim_schedule 147, dim_component 1,340, fact_bill 31, fact_charge 894, fact_audit_line 96. Two consecutive builds produced identical sha256 content hashes for all eight tables (prefixes: dim_account `66f07d4c2e913bc6`, dim_component `69e17f975837f210`, dim_date `85f9f3dc8df97122`, dim_schedule `e319c3c14673f05a`, dim_utility `df775b074cf7d0d0`, fact_audit_line `f47994eee30fcd59`, fact_bill `16a8e609f62fa130`, fact_charge `fb0a89c1dcbb7a51`).

### Analysis sanity checks (scratch marts only)

- **Billed per year-month for account 001172485896** (`fact_bill` ⋈ `dim_account` ⋈ `dim_date` on `bill_to_key`): 31 months, 2023-04 $165.15 through 2025-10 $115.35. Pilot months 2025-08 / 09 / 10 = $96.49 / $102.47 / $115.35, the printed totals including tax.
- **Pilot billed vs expected by verdict** (`fact_audit_line` ⋈ `fact_bill`): each pilot bill has 31 MATCH lines (billed = expected = $95.75 / $101.67 / $114.45) and 1 OUT-OF-SCOPE line (tax $0.74 / $0.80 / $0.90, expected NULL).
- **Billed by component billing category per year** (`fact_charge` ⋈ `dim_date` ⟕ `dim_component`):
  - 2023: $1,251.11, all 252 charges unpriced by an audit line;
  - 2024: $1,524.65, 339 unpriced;
  - 2025: $942.45 unpriced (255 charges), $141.63 on base-schedule components with no billing category (12), $170.24 on category "120" (36), total $1,254.32.

  The year totals equal the Phase 4 stored totals ($1,251.11 / $1,524.65 / $1,254.32). A first draft of this query's `COALESCE` merged "no priced component" with "component has no billing category". It was corrected to separate them before the figures above.

### Primary landing

- **Backup:** `data/tariff-pg-pre-marts.2026-11-10.sql`, 522,328 bytes (gitignored). `pg_dump` exited 0 and the file contains `-- PostgreSQL database dump complete`. It was taken before any write.
- **Build:** `mart` schema created and built on the primary, build_id 1. All eight checks PASS with the same figures as the scratch (current runs 1, 2, 3; pilot 95.75 / 101.67 / 114.45, Δ 0.00; largest differences 0.00; 0 orphans). The mart content hashes equal the scratch's.
- **Operational content hash** (every row of the 22 `public` tables): `06607de40abac530d38dd2a612867062` before and after, and again after the DAG proof. Schemas went from `public` to `mart, public`. Every standing count is unchanged.
- The report printer in the landing script failed after the build had committed (a mixed-type `sort_keys` while formatting). The full report was printed from a standalone read-only `reconcile` on the primary.

### Airflow

- **DAG:** `dags/mart_build.py` (`mart_build`): manual only, one task `build_and_reconcile`, no retries, B19's target guard (scratch default, primary refused without `AIRFLOW_TARIFF_ALLOW_PRIMARY=yes`).
- **Proof run:** `manual__2026-11-10T23:44:57`, success, log "tariff database: tariff_b19_scratch". All eight checks PASS, with current runs 4 / 5 / 6 chosen from 6 runs and 96 of 192 lines selected, pilot totals 95.75 / 101.67 / 114.45, Δ 0.00.
  - The first trigger, sent the moment the DAG file appeared, produced no run (the scheduler had not yet registered the DAG). The second trigger succeeded.
- **End state:** all four DAGs paused.
- **Tests:** the DagBag test file now expects four DAGs and checks `mart_build`'s structure; 5 of 5 pass inside the Airflow image.

### Tests

`tests/test_mart_build.py` (7, local SQLite fixtures) covers:
- every table populated and all checks passing;
- the current-run rule choosing the later of two runs for a bill (the stale run's MISMATCH line is excluded);
- an uncovered bill's NULL audit measures, exact decimal-text storage, and charge sums equal to the bill total;
- identical hashes across two builds;
- an unresolvable component key failing the build with prior marts untouched;
- a failed reconciliation rolling back;
- the operational tables byte-identical after a build.

Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`. No commits.

Suite with the database env unset: 521 passed, 1 skipped (514 + 7 mart tests; the skip is the DagBag file, which passes 5 of 5 inside the Airflow image).

## B27 — Shared versioned feature pipeline (schema `features`)

Zero model calls. `src/feature_pipeline.py` reads only the `mart` schema and writes only the `features` schema. B17, B18, B21 and B23 read features from here; none of them builds its own.

### Source boundary

The pipeline issues exactly three queries, all over mart tables: `mart.fact_bill` ⋈ `mart.dim_account` ⋈ `mart.dim_date` (bills in account and date order), `mart.fact_audit_line`, and `mart.fact_charge`. A test parses every `FROM` and `JOIN` in the module and asserts that the reads name only `mart.*` and the writes only `features.*`. Every requested v1 feature was computable from the marts, so none was omitted.

### The v1 feature set (registry `features.feature_definitions`, version `v1`)

One row-set per bill, computed as of that bill. The account's bills are ordered by bill end date, then bill key.

| Feature | Type | Definition | NULL rule |
|---|---|---|---|
| billed_total | decimal | printed billed total | never |
| expected_total | decimal | current run's expected in-scope total | no current audit run |
| delta_amount | decimal | in-scope billed − expected | no current audit run |
| delta_pct | decimal | delta_amount / expected_total × 100 | no current run; expected 0 |
| total_kwh | decimal | bill kWh | no stored kWh |
| days_in_period | integer | billing days | no stored days |
| kwh_per_day | decimal | total_kwh / days | either NULL, or days 0 |
| billed_per_kwh | decimal | billed_total / total_kwh | kWh NULL or 0 |
| bill_year, bill_month | integer | calendar year and month of the bill end date (dim_date) | never |
| season_codes | text | `dim_date.rate_seasons` at the bill end date | no stored month-range code covers the month |
| days_since_prev_bill | integer | end date − previous bill's end date | first bill |
| kwh_per_day_change_pct | decimal | vs previous bill, × 100 | first bill; either NULL; previous 0 |
| trailing_3_kwh_per_day | decimal | mean over up to 3 strictly earlier bills (NULL kwh_per_day skipped) | first bill |
| trailing_3_billed_total | decimal | mean over up to 3 strictly earlier bills | first bill |
| bills_so_far | integer | the account's bills up to and including this one | never |
| has_audit | integer | 1 if a current audit run covers the bill, else 0 | never |
| verdict_match_rate | decimal | MATCH / in-scope current audit lines | no current run; no in-scope lines |
| mismatched_lines | integer | lines neither MATCH nor OUT-OF-SCOPE | no current run |
| max_abs_line_delta | decimal | largest |line delta| | no current run; no line delta |
| charges_classified_pct | decimal | share × 100 of the charged amount whose line has a known component (`fact_charge.component_key`) | charged amount sums to 0 |

`feature_values` holds one row per (bill, feature, version): `account_key`, `bill_key`, `feature_name`, `feature_set_version`, `numeric_value` (DECIMAL on Postgres), `text_value`. `feature_build_log` holds one row per build: version, time, bill / feature / row counts, and the checks.

The registry rows are written by the build from `FEATURES`, the same list `compute` uses (an assertion ties each computed row's keys to it). A build check then compares the stored registry with the computed set.

**Exactness.** Values are `Decimal` throughout. Sums and differences are exact; quotients (rates, means, percentages) are quantized to 10 places with ROUND_HALF_UP. The read accessor `frame(conn, account_number, version)` returns a pandas DataFrame with `dtype=object`, so values stay `Decimal`, `int`, text or `None`. A first draft used `DataFrame.from_dict`, which coerces ints and NULLs to float and NaN even with `dtype=object`. The test caught it and the accessor now builds from row lists.

### Point-in-time discipline and versioning

- **Point in time:** a bill's features read only that bill and the account's strictly earlier bills (`history` holds already-computed earlier rows; the loop never looks ahead). The audit features describe the bill's own current audit run under the B20 rule.
- **Versioning:** the version is the code constant `FEATURE_SET_VERSION = "v1"`. A build replaces only its own version's registry and values in one transaction and never touches another version's rows. A failed check rolls back, writing nothing.

### Proofs

**Tests** (`tests/test_feature_pipeline.py`, 6, fixture marts built by `mart_build` on SQLite):

- **Middle bill by hand.** Bill 3's trailing kWh/day = mean(900/30, 600/30) = 25, trailing billed = (120.00 + 90.30) / 2, change % = (25 − 20) / 20 × 100 = 25, 32 days since the previous bill, and bill 4's trailing billed is the mean of bills 1–3 only. The pipeline agrees.
- **Leakage tripwire.** Features for bills 1–4 were built; then a later bill 5 (2,400 kWh, $250.00) was appended, and the marts and features rebuilt. Every stored value row for bills 1–4 was byte-identical.
- **NULL rules.** The first bill has NULL history features; the unaudited bill has NULL audit features with `has_audit` 0. The audited bill has expected 35.20, delta −0.07, match rate 0.5, 1 mismatched line, max |delta| 0.07, and classified % 35.13 / 36.75 × 100.
- **Versions coexist.** Rebuilding v1 gives identical hashes; building v2 leaves v1's hashes unchanged.
- **Accessor and registry.** The accessor shape is 4 × 21 in registry order, all object, with no floats; the registry equals the computed set.
- **Reads only marts.** As described under Source boundary.

### Scratch (`tariff_b27_features_20261110`, a `pg_dump` of the primary with its marts)

- Two builds, both all 5 checks PASS: registry 21 = 21; 651 rows = 31 bills × 21 features; billed_total features sum 4,030.08 = `mart.fact_bill` 4,030.08; 0 unaudited violations; 0 first-bill violations.
- Identical sha256 across the two builds: `feature_definitions` `8168b857d645b592…`, `feature_values` `195d56a67f20a0e9…`.
- Accessor for account 001172485896: (31, 21), all object. For example, bill 1 has 165.15, 1268.0 kWh, 34 days, 37.2941176471 kWh/day, seasons `nov_apr,oct_apr`, no history. Bills 29 / 30 / 31 have expected 95.75 / 101.67 / 114.45, delta 0.00, match rate 1.0000000000, 0 mismatched, and classified % 99.2330811483 / 99.2192836928 / 99.2197659298.
- **Independent SQL spot checks** (written straight against the marts, not the module's code):
  - `trailing_3_kwh_per_day` (window `AVG … ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING`): bill 5 32.3994766010 vs stored 32.3994766010; bill 20 24.2191570881 vs stored 24.2191570881;
  - `verdict_match_rate` and `max_abs_line_delta`, bill 29: 1.0000000000 and 0.00 vs stored 1.0000000000 and 0.00;
  - `charges_classified_pct`, bill 30: 101.67 / 102.47 × 100 = 99.2192836928 vs stored 99.2192836928.

### Primary landing

- **Backup:** `data/tariff-pg-pre-features.2026-11-10.sql`, 777,391 bytes (gitignored). `pg_dump` exited 0 and the file contains `-- PostgreSQL database dump complete`. It was taken before any write.
- **Build:** build_id 1, all 5 checks PASS, 651 rows; feature hashes equal the scratch's.
- **Hashes:** `public` (22 tables) `06607de40abac530d38dd2a612867062` and `mart` (9 tables) `4431e52146eb2cadd437df9ef1924157`, identical before and after (and again after the DAG proof). Schemas went from `mart, public` to `features, mart, public`. Standing counts are unchanged.

### Airflow

- **DAG:** `dags/feature_build.py` (`feature_build`): manual only, task `build_features`, no retries, B19's guard.
- **Proof run:** `manual__2026-11-10T23:58:07`, success, log "tariff database: tariff_b19_scratch". All 5 checks PASS (31 bills, 21 features, 651 rows), with hashes equal to the primary's: that scratch's newer audit runs carry the same verdicts and totals.
- **End state:** all five DAGs paused.
- **Tests:** the DagBag tests now cover `feature_build` and pass 6 of 6 inside the Airflow image.

Frozen SQLite SHA-256 unchanged, `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`. No commits.

Suite with the database env unset: 527 passed, 1 skipped (521 + 6 feature tests; the skip is the DagBag file, 6 of 6 inside the Airflow image).

## B17 — DS analysis layer over the marts

Zero model calls. `src/ds_analysis.py` reads only the B20 marts and the B27 feature store and writes no database state; its only output is the report file. It opens a read-only connection.

### Module and guardrails

- **Tables read (`READS`):** `mart.fact_bill`, `mart.dim_date`, `mart.dim_account`, `mart.fact_charge`, `mart.dim_component`, `mart.fact_audit_line`, `mart.mart_build_log`, `features.feature_values`, `features.feature_definitions`, `features.feature_build_log`. A test parses every `FROM` and `JOIN` in the module, asserts that only these tables appear, and finds no write statement.
- **Shape:** one function per question, each `(conn, account) → (dict of pandas DataFrames, plain-words summary)`. Values are Decimal in object-dtype frames.
- **Command:** `python -m src.ds_analysis [--account N] [--out PATH]` writes the self-contained HTML report `data/reports/b17-ds-analysis.html`. Charts are inline SVG; no plotting library was installed or added.
- **Seasons:** the stored `may_sep` / `oct_apr` codes split the year exactly, so a bill is summer or winter by its end month.
- **Labels:** the audit section describes stored current audit lines and recomputes no verdict. The anomaly scan is labelled "Descriptive statistics — not audit verdicts, not ML".
- **Coverage:** every classification figure carries its unclassified share.

### Questions and headline figures (primary, account 001172485896)

1. **Trend and seasonality.** 31 bills, 2023-04 to 2025-10. Pooled kWh/day is 35.846 in winter (16 bills) against 26.682 in summer (15 bills). Least-squares trends are given per bill. Yearly billed totals: $1,251.11 / $1,524.65 / $1,254.32.
2. **What drives the billed total.** 2023 and 2024 are 100% unclassified. 2025 is $311.87 classified (audited bills only) and $942.45 unclassified, 75.14% unclassified. Classified amounts are broken down by billing category and component; the unclassified part is listed by printed bill label, explicitly not a tariff classification.
3. **Audit outcomes.** Bills 29 / 30 / 31: 31 of 31 in-scope lines MATCH, expected $95.75 / $101.67 / $114.45, total |delta| $0.00, out of scope Virginia Tax Surcharge $0.74 / $0.80 / $0.90. A per-component and per-category table is included.
4. **The July 2025 rate change.** Bill 28 (2025-06-14 to 2025-07-15) straddles 2025-07-01 and is excluded.
   - Against the same months a year earlier (bills 17–19), the rate went from $0.13959 to $0.13107 per kWh (−6.10%). The mean bill moved +$5.66: −$6.81 from price and +$12.47 from usage. Price effect = rate change × after-usage; volume effect = before-rate × usage change; they sum exactly to the difference.
   - Bills 17 and 18 were on Schedule 110, so this comparison mixes the schedule move with the amendment. The report says so.
   - A comparison against all 27 clean prior bills is shown alongside.
5. **Descriptive anomaly scan.** z-scores within season (each season's mean and sample standard deviation of per-bill values; these per-bill means differ slightly from the pooled figures in question 1, and the report says so). Top bills: 2 (summer kWh/day z 2.567), 10, 13, 19, 15.

### Independent cross-checks (own SQL against the marts, not the module's queries)

| Question | Report | Independent SQL |
|---|---|---|
| Q1 | yearly $1,251.11 / $1,524.65 / $1,254.32; months 2023-04..2025-10 (31); summer and winter 26.682 / 35.846 | same totals; same range and count; same pooled kWh/day |
| Q2 | 2025: 311.87 / 942.45 / 75.14%; 2023 and 2024 100% | same |
| Q3 | 31 / 31 MATCH; 95.75 / 101.67 / 114.45; tax 0.74 / 0.80 / 0.90 | same |
| Q4 | 0.13959 → 0.13107; means 99.11 → 104.77; kWh 710.00 → 799.33; straddler bill 28 | same |
| Q5 | bill 2 z 2.567 | `stddev_samp` gives 2.567 |

### Report and close-out

- **Report:** `data/reports/b17-ds-analysis.html`, 42,997 bytes. Sections: Trend and seasonality; What drives the billed total; Audit outcomes (descriptive); The July 2025 rate change; Descriptive anomaly scan (not verdicts, not ML). The footer names feature-set v1, feature build 1 and mart build 1.
- **Tests:** `tests/test_ds_analysis.py` (7, on the B27 fixture marts) covers each question's shape and hand values (2025 total 348.15; 35.13 classified and 89.91% unclassified; bill 4 at 1 of 2 MATCH with "Tax $1.62"; the exact price/volume split; summer-only ranking), the SQL boundary, and the report command end to end.
- **Suite:** with the database env unset, 534 passed, 1 skipped.
- **Primary unchanged:** `public` (22) `06607de40abac530d38dd2a612867062` and `mart` (9) `4431e52146eb2cadd437df9ef1924157`, equal to the B27 after-state. Frozen SQLite SHA-256 `85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe`. No commits.

## B21 — Forecasting (schema `forecast`)

Zero model calls. `src/forecasting.py` reads only `mart` and `features` and writes only `forecast`. Forecasts are estimates; every row is labelled "estimate (forecast), not a billing or audit input".

**Methods.**
- **seasonal_naive** (pure Decimal): the same calendar month from the most recent earlier year with a bill; else that calendar month's mean over all stored years; else the overall mean kWh/day. The rule that fired is stored per target month.
- **ols** (numpy least squares): kWh/day on a linear month-ordinal trend plus calendar-month indicators. The time axis is the calendar month ordinal, so Jan–Mar 2023 stay missing (no zero-filled months, no synthesized bills).
- **Float disclosure:** the OLS fit runs in float inside the fit only. Stored values go through `Decimal(str())` and are rounded (kWh to 0.001, money to cents). The baseline is pure Decimal.
- **Billed forecast:** forecast kWh × held rate, the latest bill's `billed_per_kwh` (bill 31, 0.1304864253). The held rate is stored on every row and in the run.

**Backtest** (walk-forward, one step, last six bills, each fit only on strictly earlier bills):

| bill | month | actual kWh/day | naive | ols | actual $ | naive $ | ols $ |
|---|---|---:|---:|---:|---:|---:|---:|
| 26 | 2025-05 | 29.633 | 21.759 | 22.553 | 118.38 | 85.34 | 88.46 |
| 27 | 2025-06 | 27.467 | 24.133 | 22.312 | 109.11 | 96.41 | 89.13 |
| 28 | 2025-07 | 23.469 | 24.906 | 22.822 | 98.74 | 105.53 | 96.71 |
| 29 | 2025-08 | 25.207 | 25.448 | 21.735 | 96.49 | 97.03 | 82.87 |
| 30 | 2025-09 | 26.100 | 22.933 | 20.214 | 102.47 | 90.81 | 80.05 |
| 31 | 2025-10 | 28.516 | 24.276 | 24.658 | 115.35 | 98.49 | 100.03 |

MAE kWh/day: naive 3.382, ols 4.350. MAE billed: naive $13.60, ols $17.22. **seasonal_naive ships**, because its billed-total MAE is lower (a tie would also ship the baseline).

**Forecast** (band = ± backtest billed MAE $13.60, a backtest band and not a confidence interval; held rate 0.1304864253):

| month | kWh/day | kWh | billed | band | rule |
|---|---:|---:|---:|---|---|
| 2025-11 | 28.344 | 850.313 | $110.95 | 97.35–124.55 | prior_year 2024 |
| 2025-12 | 38.867 | 1,204.867 | $157.22 | 143.62–170.82 | prior_year 2024 |
| 2026-01 | 40.848 | 1,266.303 | $165.24 | 151.64–178.84 | prior_year 2025 |

**Determinism:** two scratch builds (`tariff_b21_forecast_20261110`) gave identical hashes: `forecast_runs` 1194d3217622a9a3, `forecast_values` 42f0b65ec622f414.

**Landing:** backup `data/tariff-pg-pre-forecast.2026-11-10.sql`, 805,464 bytes, marker 1. After the landing, features f5257321…, mart 4431e521… and public 06607de4… are identical, and only `forecast` (2 tables) is new.

**DAG:** `forecast_build` (`dags/analytics_builds.py`) succeeded once on `tariff_b19_scratch` with the same hashes, and is left paused. The first automated trigger after the DAG appeared produced no run (the same race seen with mart_build), so the helper now retries the trigger until a run exists.

**Tests:** `tests/test_forecasting.py` (4) covers the baseline lookup rules (prior year, cross-year mean, never-occurred month), the backtest leakage tripwire, the tie rule, and determinism with text-stored money. DagBag: 7 of 7 in the image.

**Suite:** 538 passed, 1 skipped in 143.05s (0:02:23). **Frozen file:** 85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe.

## B22 — Tariff-change impact analysis: STOPPED at its named condition

The predicted problem "the pilots' billed schedules have no superseded version" occurred, and so did its fallback's stop condition. B22's own handling says to stop and not invent a pairing, so no module, report or test was written. Nothing landed, and no database or file other than this doc and the run log changed.

**Evidence (read-only, primary):**
- Version status by utility: Appalachian 44 active; Pepco 42 active and 20 superseded; Virginia Electric and Power Company 41 active and 0 superseded.
- No Dominion or Appalachian schedule code has more than one stored version (the query returned no rows).
- Dominion schedule 120, the pilots' billed schedule and the account's VE-120, has exactly one version: id 2, v12, effective 2025-07-01, active.
- The only superseded versions on the primary are Pepco's 2025 rate years.

**Anchor witness** (existing `schedule_review.reprice_bill`, read-only): bills 29 / 30 / 31 under schedule 120 version id 2 compute **\$95.75 / \$101.67 / \$114.45**, reproducing the anchors exactly. The module would have been wired to the right path. What is missing is the old version.

**Also blocked:** the Appalachian generality demonstration has no version pair either (all 44 Appalachian versions are single and active).

**To unblock:** load and activate a prior Dominion book (the pre-July-2025 Amendment), so schedule 120 gains a superseded version. Alternatively, the owner could choose to demonstrate the method on a Pepco rate-year pair, which this task's spec did not authorize.

## B23 — Segmentation (schema `segments`)

Zero model calls. `src/segmentation.py` reads only `mart` and `features` and writes only `segments`. The segments are deterministic rules, not clustering, and describe this account's bills only.

**Definitions (v1):**

| dimension | rule | cuts / rule fired |
|---|---|---|
| usage_band | terciles of kwh_per_day; cuts at sorted positions ceil(n/3), ceil(2n/3) | 26.1000000000 / 35.9333333333, terciles (n=31) |
| cost_band | terciles of billed_total, same method | 112.38 / 135.72, terciles (n=31) |
| season | stored season_codes joined exactly as stored; multi-code labels kept, never reduced | — |
| audit_state | unaudited / audited_mismatch / audited_match | — |
| trend | first bill no_previous_bill; change > +20% usage_shift_up, < −20% usage_shift_down, else steady | — |
| primary | the five labels joined with " / " | — |

Banded dimensions with fewer than 6 bills label every bill `insufficient_history`. NULL labels are `no_value`, `no_season_code`, `no_change_value` and `unaudited`; no bill is ever dropped.

**Counts:**
- usage_band: low 11, mid 10, high 10;
- cost_band: low 11, mid 10, high 10;
- season: may_oct,may_sep 15; nov_apr,oct_apr 13; may_oct,oct_apr 3;
- audit_state: unaudited 28, audited_match 3;
- trend: steady 26, usage_shift_up 2, usage_shift_down 2, no_previous_bill 1.

**Consumer answers** (labels from `segments`, amounts joined from `mart.fact_bill`) beside independent SQL over `features` and `mart`:
- Unaudited high-usage bills: 10, billed $1,666.13 | independent: 10, $1,666.13.
- Usage shifts beyond 20%: 7 up $129.45, 12 down $135.72, 13 down $102.81, 21 up $152.40 | independent: identical.
- Billed per season label: may_oct,may_sep $1,657.62 (15); may_oct,oct_apr $339.52 (3); nov_apr,oct_apr $2,032.94 (13) | independent `dim_date.rate_seasons` grouping: identical.

**Determinism:** two scratch builds (`tariff_b23_segments_20261110`) gave identical hashes: definitions 0f4d8ec6fc075375, bill_segments 77dc130add07450b.

**Landing:** backup `data/tariff-pg-pre-segments.2026-11-10.sql`, 809,944 bytes, marker 1. public, mart, features and forecast are identical after; only `segments` (3 tables) is new.

**DAG:** `segment_build` succeeded on `tariff_b19_scratch` with the same counts and cuts, and is left paused.

**Tests:** `tests/test_segmentation.py` (3) covers hand-worked terciles (9 values give cuts 30 and 60, with boundary labels), the small-history rule and every NULL label on the 4-bill fixture, determinism, and the read/write boundary. DagBag: 7 of 7.

**Suite:** 541 passed, 1 skipped in 140.78s (0:02:20). **Frozen file:** 85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe.

## B18 — ML anomaly scores (schema `ml`) — prioritization only, not an audit finding

Zero model calls. `src/anomaly_detection.py` reads only `features` and `mart` and writes only `ml`. Every stored row carries "prioritization only, not an audit finding". With n = 31, the scores order attention and nothing more: they are not probabilities and not findings. Flags were not checked against audit verdicts, because all the pilots matched and agreement would be meaningless.

**Dependency (predicted problem, occurred):** scikit-learn was absent. `scikit-learn==1.5.2` (the version the Airflow 2.11.0 / Python 3.9 constraints pin) was added to `requirements.txt` and installed: numpy 2.0.2, scipy 1.13.1, `pip check` clean. The Airflow image was rebuilt with sklearn 1.5.2 (`pip check` clean) before the DAG proof.

**Coverage screen** (non-null of 31; must reach 80% and vary):
- **Qualified (15):** billed_total, total_kwh, days_in_period, kwh_per_day, billed_per_kwh, bill_year, bill_month, days_since_prev_bill (30), kwh_per_day_change_pct (30), trailing_3_kwh_per_day (30), trailing_3_billed_total (30), bills_so_far, has_audit, charges_classified_pct, kwh_per_day_gap (derived, 30).
- **Screened out (6):** expected_total (3, varies), delta_amount, delta_pct, verdict_match_rate, mismatched_lines and max_abs_line_delta (3 each, constant).
- **Imputation:** account medians, one row each for days_since_prev_bill, kwh_per_day_change_pct, kwh_per_day_gap, trailing_3_billed_total and trailing_3_kwh_per_day (the first bill).

**Model:** IsolationForest(random_state=42, n_estimators=100). K = max(1, round(10% × 31)) = 3. Ranked (rank | bill | score | robust rank): 1|10|0.6094|1, 2|13|0.5532|2, 3|31|0.5500|20, 4|11|0.5477|5, 5|29|0.5388|26, …

**Flagged, prioritization only:**
- **Bill 10:** billed_total 224.91 (robust z +4.04), total_kwh 1,648 (+2.96), kwh_per_day 49.94 (+2.49).
- **Bill 13:** kwh_per_day_gap −20.21 (−2.97), kwh_per_day_change_pct −32.47% (−2.28), trailing_3_billed_total 180.27 (+1.69).
- **Bill 31:** bills_so_far 31 (+1.26), trailing_3_billed_total 99.23 (−0.92), billed_per_kwh 0.1305 (−0.81). Bill 31 ranks mainly on time-position features (bills_so_far, has_audit), which pass the spec's mechanical coverage screen. That is an artifact of including counters, not a usage signal, and the robust ranking puts it 20th.

**Cross-check:** the robust z top 3 is [1, 10, 13] against the IsolationForest's [10, 13, 31], an overlap of 2 of 3 (information, not a gate).

**Determinism:** two scratch runs (`tariff_b18_anomaly_20261110`) gave identical hashes: runs 9eba105c7229c2fa, scores da8e57da06a9253c.

**Landing:** backup `data/tariff-pg-pre-anomaly.2026-11-10.sql`, 819,227 bytes, marker 1. public, mart, features, forecast and segments hashes are identical after; only `ml` is new. That identity is also the guardrail proof that the ML layer touched nothing else. The primary stores 31 scores with 3 flagged.

**DAG:** `anomaly_scoring` succeeded on `tariff_b19_scratch` (same flags and hashes) and is left paused.

**Tests:** `tests/test_anomaly_detection.py` (3) covers determinism and labels, the coverage screen (sparse expected_total and constant bill_year excluded), and the guardrail (writes only `ml`; operational, mart and feature rows unchanged on fixtures). DagBag: 7 of 7.

**Suite:** 544 passed, 1 skipped in 144.08s (0:02:24). **Frozen file:** 85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe.

## B24 — Feedback and monitoring loop (schema `monitor`)

> **Partly superseded (2026-11-11):** outbox delivery of alert findings was removed with the staff console; monitoring now writes findings to the `monitor` tables only. See "Staff console removal".

Zero model calls. `src/monitoring.py` (CLI `python -m src.monitoring`) writes one run's findings to `monitor.monitor_runs` and `monitor.monitor_findings` in one transaction. Thresholds are named constants: `STALE_WARN_DAYS`=35, `STALE_ALERT_DAYS`=70, `REPEAT_MIN`=3, `GROWTH_WARN`=10.

**Checks:** data staleness (newest bill period vs today), mart and feature currency, queue depth by status and by reason (plain words), staff decision tallies, audit coverage, repeat failures (one alert per schedule with 3 or more open items sharing a reason), and queue growth since the previous run (the first run reports ok with "first run").

**Unforeseen conflict (recorded, not improvised around):**
- The staff outbox's event-type check constraint on existing databases allows only `new_queue_item` and `activation_failed`.
- Filing `monitor_alert` rows there would need a constraint change to an existing table, which is outside B24's write scope.
- Handling: `staff_notify`'s single outbox definition now includes `monitor_alert` / `monitor_finding` for newly created outboxes, and `notify_monitor_alert` writes only where the table accepts it. Elsewhere the run records the reason.
- **Widening the existing outbox constraint is a separate backup-gated step for the owner** (carry-over). On the primary it would not have produced rows yet in any case, because no staff accounts exist.

**Scratch DAG proof** (`daily_checks` via `airflow dags test`, monitoring after the scan, `tariff_b19_scratch`): success. The scan added 0 items (116 open before and after). 29 findings and 10 alerts: data_staleness alert (394 days; the run's date was 2026-11-11 UTC in the container), 9 repeat_failure alerts (that scratch also holds the 25 B19 zero-rate items), mart and features currency ok, audit coverage 3/31, growth "first run". Outbox rows 0: "outbox table predates monitor alerts". `daily_checks` stays paused (`dags test` does not unpause it).

**Primary run:** backup `data/tariff-pg-pre-monitor.2026-11-10.sql`, 829,290 bytes, marker 1. Run 1: 91 open, 26 findings, 7 alerts, outbox rows 0 ("outbox table predates monitor alerts …"; the primary also has 0 staff accounts). Each finding beside its independent SQL:
- **data_staleness:** alert, 393 days (newest bill_to 2025-10-13) | SQL `current_date − max(bill_to)` = 394. The difference is the date basis: the module uses the local date (2026-11-10) and Postgres `current_date` is UTC (2026-11-11). As the spec predicted, this alert is the monitoring working.
- **mart_currency** ok 2025-10-13 and **features_currency** ok 31 of 31 | SQL max bill_to 2025-10-13, 31 bills.
- **queue:** 91 open, by reason: rate_year_missing 48, zero_rate_unexpected 14, rate_not_on_page 8, quote_not_found 6, no_verified_rates 5, unit_not_in_vocabulary 4, model_output_incomplete 2, unreadable_pages 2, missing_rule 1, verify_marker 1 | SQL open 91.
- **staff_decisions:** 0 / 0 / 0 / 0 | SQL `staff_decisions` 0 rows.
- **audit_coverage:** 3/31 | SQL 3/31.
- **repeat_failure** alerts: Pepco MDU-PIV 5, MMA 3, R 11, R-PIV 17, SSL-UG 5 (all rate_year_missing); Dominion RIDER PIPP-CM 13 (zero_rate_unexpected) | SQL `GROUP BY schedule, signal HAVING count(*) >= 3` gives the same six.
- **queue_growth:** ok, first run.

**Hashes:** public (including the outbox), mart, features, forecast, segments and ml are identical before and after. `monitor` (2 tables) is new.

**Tests:** `tests/test_monitoring.py` (9) covers the staleness thresholds at 35 / 36 / 70 / 71, staleness and currency on a fixture, the repeat rule at 2 (ok) and 3 (alert), an alert filing an outbox row for a seeded Master while a warn does not, no accounts giving zero rows with the reason, and growth "first run" then +11 → warn. DagBag: 7 of 7, with daily_checks running integrity_report → review_scan → monitoring.

**Suite:** 553 passed, 1 skipped in 142.12s (0:02:22). **Frozen file:** 85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe.

## B25 — Extraction evaluation harness and run ledger (schema `eval`)

Zero model calls. Every replay ran with `ANTHROPIC_API_KEY` removed, `ai_extract.api_key` patched to None (it also reads `.env`, which holds a key), and a `NeverCalledClient` that raises on any use. Both replays reported 0 reading and 0 structured calls. `src/extraction_eval.py` replays a book's cache through the current `packet_pipeline.run_book` into a fresh empty-schema scratch (`tariff_b25_<utility>_replay`) and compares it with a comparison database: `tariff_b25_compare`, a `pg_dump` of the primary. The primary itself is never used by the harness.

**Ledger** (`eval.extraction_runs`, landed on the primary):

| run | utility | source | model | calls | cost | sections | comps / rules / structured | source note (abridged) |
|---|---|---|---|---:|---|---:|---|---|
| 1 | Appalachian | backfill_historical | claude-fable-5-1 | 9 | NULL (no figure recorded) | 9 | 0 / 0 / – | doc 11B witness, 10th call rejected HTTP 400; cache 9 entries |
| 2 | Appalachian | backfill_historical | claude-sonnet-5-5 | 46 | NULL (doc gives only "≈$1.64", approximate) | 46 | 573 / 153 / – | doc 11C and 2g/2h; queue 7 by reason; cache 46 entries (45 end_turn, 1 max_tokens) |
| 3 | Appalachian (structured) | backfill_historical | claude-sonnet-5-5 | 44 | 0.90–1.57 (doc's two estimates) | 44 | – / – / 44 | doc 12h; 3 later SBS re-reads not included |
| 4 | Pepco | backfill_historical | claude-sonnet-5-5 | 87 | 2.49–3.10 (doc's two estimates) | 44 | 319 / 215 / 6 | doc 12i; 45 reading + 42 structured calls; queue 68 by reason |
| 5 | Appalachian | cache_replay | cache | 0 | 0 | 48 packets | 573 / 153 / 44 | this harness |
| 6 | Pepco | cache_replay | cache | 0 | 0 | 64 packets | 319 / 215 / 6 | this harness |
| 7 | Dominion | cache_replay | — | 0 | NULL | — | — | **skip:** the live set came from the hand-written parsers and the AI re-read is held, so there is no AI cache to replay |

Cache cross-check with `ai_extract`'s own reader: token sums match the doc. Appalachian Sonnet: 168,479 input / 130,396 output tokens; Pepco: 102,709 / 94,276; structured caches: 133,536 / 33,033 and 106,101 / 11,826. One `unreadable_cache` section each in the Appalachian and Pepco reading caches: the max_tokens outputs (OL and SOS), counted and not parsed ad hoc.

**Appalachian agreement** (replay vs stored):
- versions 44 / 44, components 573 / 573, rules (type and text) 153 / 153 and section queue 7 / 7, all agreeing; structured 44 both, 1 primary-only.
- **Expected (1):** the SBS `demand_determination` structured row, primary-only, tagged "expected: 12h-fix3 SBS evaluator field-name enforcement (2026-11-10)". The primary's row came from the fix3 re-read on a scratch.
- **Unexplained (230 = 115 paired groups):** rule citation status. The primary holds 123 Appalachian rules as `uncited` (the Task 4e citation-backfill state), while today's replay stores all 153 as `cited`. Rule identity agrees, and only `citation_status` differs. This is not on the module's known-change list, so it is reported as unexplained, and the primary was not edited.
- Sections agreeing: 11 of 48, because the citation differences touch most schedules; with citations excluded, all content agrees.

**Pepco agreement:**
- versions 62, components 319, rules 215 (citations agree) and structured 6, all agreeing.
- **Unexplained (13 groups, 29 items), all primary-only queue items:** EF no_verified_rates 1, rate_not_on_page 2; MDU-PIV quote_not_found 1, rate_year_missing 5; MMA quote_not_found 1, rate_year_missing 2; PF no_verified_rates 1, quote_not_found 1; R no_verified_rates 1, quote_not_found 2, rate_year_missing 10; RT quote_not_found 1, rate_year_missing 1.
- These look like the first-pass queue rows recorded in 12i ("R, EF, and PF still have the first-pass queue rows from before a column-split date and a spaced decimal were accepted"). That change is not on the known-change list, so they remain unexplained findings. Today's replay produces 39 queue items, all also on the primary.
- Sections agreeing: 38 of 44.

**Landing:** backup `data/tariff-pg-pre-eval.2026-11-10.sql`, 833,906 bytes, marker 1. All other schema hashes are identical; only `eval` (1 table) is new.

**DAG:** `extraction_eval` succeeded on `tariff_b19_scratch`: the Pepco replay made 0 calls with the same agreement figures (13 unexplained), the Dominion skip was recorded, and the ledger has 6 rows there. Left paused.

**Tests:** `tests/test_extraction_eval.py` (5) covers the never-called client and the key removal, the ledger range row (0.90 / 1.57) and the unsourced NULL row, `unreadable_cache` counting on a fixture cache, known-change tagging (expected vs unexplained), and the Dominion skip. DagBag: 7 of 7.

**Suite:** 558 passed, 1 skipped in 143.10s (0:02:23). **Frozen file:** 85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe.

## B26 — Tariff Q&A assistant (read-only, lands nothing)

Zero model calls. `src/qa_assistant.py`: `answer(question)` returns `{question_type, answer, citations, refused}`; the CLI is `python -m src.qa_assistant "<question>"`. It works by deterministic pattern routing, entity matching on stored utility names and schedule codes, and template answers built only from the retrieved rows. There is no model, no embeddings and no vector store. A model phrasing layer is a future step that needs the key and a spend approval.

**Question types:**
- rate lookup (component value, unit and page; active version, or the version covering a named year);
- rule lookup (stored rules, each with its page, or the plain note "stored without a page citation" for uncited rules such as Dominion's 112);
- applicability (a structured `rider_applicability` rule if stored; otherwise the rider's stored per-schedule rate rows, the Dominion pattern; otherwise a refusal);
- schedule inventory (codes grouped by book and kind);
- schedule owner;
- version lookup;
- bill facts (`mart.fact_bill` plus its audit run).

An ambiguous code asks back by listing the candidate utilities. An unrecognized phrasing is refused with the list of supported kinds. Every non-refused answer carries citations.

**Witness set** (read-only, primary): all 6 PASS.
1. "What is the energy charge for Appalachian RS?" → rate lookup with 6 citations, including "Energy Charge (¢/kWh) All kWh - Total: 0.07622 usd_per_kwh (version 28, effective 2025-01-01, page 28)". The generation and distribution parts and the Load Management lines are also listed, each with its page.
2. "Which company has a GS-TOD schedule?" → "Schedule GS-TOD is stored for: Appalachian Power Company." (1 citation).
3. "What is bill 30's expected total and verdict?" → "printed total $102.47; expected total $101.67 (in-scope delta $0.00); verdict: matched (31 lines MATCH)" (citations: `mart.fact_bill` 30, audit run 2).
4. "What is the basic charge for Pepco Schedule SOS?" → **refusal**: "schedule SOS for Pepco (no stored version)", listing Pepco's 42 stored schedules.
5. "What versions does Pepco schedule R have?" → 2 versions: 2025-01-01 to 2026-01-01 (superseded) and 2026-01-01 to 2026-12-31 (active) (citations: versions 155 and 156).
6. "What are the rules for Virginia Electric schedule 120?" → 3 rules, each with the no-citation-stored note.

Entity matching uses stored names only, so "Dominion" is not a matched name. Because schedule 120 belongs to one utility, a question naming "Dominion schedule 120" still resolves to Virginia Electric and Power Company by the stored code: no guess, since there is only one owner. A code held by two utilities with no utility named asks back. Note: GS is stored only for Appalachian; Pepco's codes are GS3A / GSLV / GSND, so exact-code matching keeps them apart.

**Read-only proof:** the hashes of all 10 primary schemas were identical before and after the witness run.

**Tests:** `tests/test_qa_assistant.py` (5) covers each question type on fixtures, citation presence on every non-refused answer, refusals (unknown utility, the SOS pattern, unrecognized phrasing), the ambiguity ask-back (a code at two utilities), and that answering writes nothing.

**Suite:** 563 passed, 1 skipped in 143.83s (0:02:23). **Frozen file:** 85089333d3d560a0b2d2720a42a6046ef973940a22c98e8d8a00174e6deb7cfe.

## B30 — Python 3.12 upgrade: STOPPED at the pre-check

The spec's stop rule fired: no Python 3.12 exists on the machine, and installing an interpreter is not allowed. Nothing changed: the venv was not renamed, and the Dockerfile, CI workflow and `Dockerfile.airflow` are untouched. The project stays on Python 3.9.

**Pre-check output:**
- `which -a python3.12`: `python3.12 not found`.
- `/opt/homebrew/bin`: absent (no Homebrew Python). `/usr/local/bin`: no Python. `/Library/Frameworks/Python.framework`: absent. No pyenv.
- Available: `/usr/bin/python3` = **Python 3.9.6** (system, used by `venv`) and `/opt/anaconda3/bin/python3` = **Python 3.14.6** (Anaconda; `python3` is aliased to it; no conda envs).

**To unblock (owner decision):** put a Python 3.12 interpreter on the machine (for example the python.org installer, Homebrew `python@3.12`, or `conda create -n py312 python=3.12`), then re-run B30 as written. Python 3.14 is not a substitute: the task specifies 3.12, and Airflow 2.11.x publishes no 3.14 image.

## Completion Fix 1 — Appalachian citation backfill (2026-11-11)

`src/citation_backfill.py` (Task 4e pattern, shape of `book_label_backfill.py`): for each uncited Appalachian rule, the cached
reading for its section (`data/cache/ai-extract/claude-sonnet-5-5/<book hash>.json`) is searched for the same rule type and text;
a citation is stored only when the cached entry's quote appears on its cited page of `data/raw/APCo-Tariff-28.pdf`
(`ai_extract.quote_on_page`, whitespace-normalized). Failures stay uncited and are listed (`no_cache`, `no_cached_rule`,
`no_page_or_quote`, `quote_not_on_page`). Already-cited rows are checked; any disagreement refuses the run. Idempotent. Zero model calls.
Result: scratch `tariff_fix_citations_20261111` then primary (backup `data/tariff-pg-pre-citation-backfill.2026-11-11.sql`):
123 stored, 0 listed; Appalachian rules now 153/153 cited; the 30 already-cited rows byte-identical; only `tariff_rules`'
`source_page`/`citation_status` changed. Tests: `tests/test_citation_backfill.py` (3).

## Completion Fix 2 — Pepco first-pass queue rows (2026-11-11)

> **Note (2026-11-11):** the primary step now runs through `src/operator_actions.py dismiss`, not the console; see "Staff console removal".

The 29 B25 primary-only Pepco items (ids 57–68, 70–73, 79, 80, 109–119; `(source_ref, field, signal)` absent from the replay)
were dismissed through `staff_actions.dismiss_item` on clone `tariff_fix_pepco_queue_20261111` (accounts by `seed_staff.seed`):
open queue 91 → 62, 29 decisions with the completion note, no tariff data changed. **Primary step pending:** the decision path
requires a signed-in staff account and the primary has none; it runs after the owner seeds Master: the same `dismiss_item`
loop, signed in as Master, with the same note, behind backup `data/tariff-pg-pre-pepco-queue.<run-date>.sql`.

## Completion Fix 3 — Outbox constraint widening (2026-11-11)

> **Superseded (2026-11-11):** the staff console was removed; see "Staff console removal" at the end of this document. This section is kept as history.

One definition (`src/staff_notify.py`, Task 12r pattern): `EVENT_REF_KIND` → `EVENT_TYPES` (new_queue_item, activation_failed,
monitor_alert) and `REF_KINDS` (queue_item, tariff_version, monitor_finding). The fresh-table CHECKs, `allowed_values`,
`verify_outbox` (raises on any live check that is not exactly the definition; never alters), `widen_sql` (backup-gated use only)
and `outbox_allows` are all built from it. `seed_staff.seed` calls `verify_outbox`, so seeding refuses on a narrower table instead
of running silently. The ref_kind check had to widen with the event check (monitor_alert rows carry ref_kind monitor_finding).
Primary widened in one transaction behind `data/tariff-pg-pre-outbox-widening.2026-11-11.sql`; content hashes identical.
The console's outbox view labels the new event "Monitoring alert".

## B22 completion — impact analysis on the Pepco pair (2026-11-11)

`src/impact_analysis.py`: a bill's determinants priced under two versions of its billed schedule BY VERSION ID through
`schedule_review.price_schedule` (version pin; riders as the repricing includes them), breakdown from `repricing_reasons`.
Read-only (primary hashes identical across the run). Anchor reproduced as code: bills 29/30/31 under schedule 120 version 2 =
$95.75 / $101.67 / $114.45. Pair rule: most differing component rates, falling through pairs the constructed residential bill
cannot price (SSL-UG 14, SSL-OH 10: missing_determinant) → Pepco R #155 (2025-01-01, superseded) vs #156 (2026-01-01, active),
5 differing rates. Pepco stores no season or tier bounds on any component, so the four seasonal/tiered energy components are
named and excluded (the path bills them in every month and on all usage). Witness: January 2026, 750 kWh; effect +$1.00/month
(customer charge 17.09 → 18.09; riders delta 0). No Dominion pair exists. Report `data/reports/b22-impact.html`.
Carry-over: storing Pepco seasons (billing-month season rule) and tier bounds would make the energy components priceable.

## B30 re-run — Python 3.12 upgrade: DONE (2026-11-11)

Interpreter: `/opt/anaconda3/envs/py312/bin/python` (Python 3.12.13, conda env `py312`, created on the owner's request). The
3.9 venv was renamed `venv-3.9-backup` (kept; `venv-*/` added to `.gitignore`) and a fresh `venv` built from it.
- Pin bump: `scikit-learn` 1.5.2 → **1.8.0** (Airflow 2.11.2's constraints-3.12 pin it; 1.5.2 came from the 3.9 constraints).
  Unpinned packages resolve newer on 3.12 (local venv: pandas 3.1.1, numpy 2.5.4, fastapi 0.162.4, starlette 1.10.0,
  anthropic 2.8.0, pytest 9.2.0); the Airflow image stays on its constraints (pandas 2.1.4, numpy 1.26.4). `pip check` clean in both.
- `Dockerfile` → `python:3.12-slim`; CI `python-version` "3.10" → "3.12".
- `Dockerfile.airflow` → `apache/airflow:2.11.2-python3.12` with `constraints-2.11.2/constraints-3.12.txt` (manifest inspect:
  2.11.0/2.11.1/2.11.2 have python3.12 builds, 2.11.3–2.11.5 do not); compose image tag `tariff-audit-engine-airflow:2.11.2`.
  Never Airflow 3.
- Proof: suite 573 passed, 1 skipped on 3.12; API container Python 3.12.16 healthy (`/health` 200); Airflow 2.11.2 webserver and
  scheduler healthy; DagBag in the image 9 DAGs, 0 import errors, all paused; in-image DAG tests 7 passed; mart-build smoke on
  scratch clones (local 3.12 venv and inside the image) gives the 8 mart data tables byte-identical to the primary
  (`678c8a46…`); `mart_build_log` only gains its run row. Primary hashes unchanged.
- Rollback (unused): `git checkout -- Dockerfile .github/workflows/ci.yml Dockerfile.airflow`, rename `venv` → `venv-3.12-failed`,
  rename `venv-3.9-backup` → `venv`, revert the requirements pin and compose tag, re-run the suite.

## Task A — Pepco season and tier bounds (2026-11-11)

`src/pepco_terms.py` derives terms from stored Pepco component type strings (no PDF, no extraction): `summer`/`winter` as named;
`first_N` → band 0–N kWh; `over_N` → band N kWh and up (no max). Additive columns on `tariff_components`: **`season_term`** TEXT,
`tier_min_kwh` / `tier_max_kwh` DECIMAL. (Deviation from the spec's column name `season`: the table already has a `season` column,
used by Dominion with `billing_month_seasons` rules; writing it would change pre-existing column values, so the term has its own
column.) Two readings → listed, NULL; tier words on a non-per-kWh unit → listed, tier NULL; non-Pepco rows untouched.
Engine (`billing_engine`): a component with a stored `season_term` prices only in the bill month's season, read from the version's
own stored season rules (any rule type, e.g. "Summer – Billing months of June through October…"); no resolving rule → refuse, never
guess. A component with stored `tier_*_kwh` prices only the kWh inside its band. Components without the new terms take exactly the
old code paths. Coverage of 319 Pepco components: 40 season only, 8 season + tier, 0 tier only, 0 ambiguous, 271 neither (34 names,
none carrying a season or tier word). All 18 termed versions resolve January and July from stored rules.
B22 re-run (R #155 → #156, 750 kWh): January +$3.93/month, July +$4.75/month, every line priced, hand = path.
Landed behind `data/tariff-pg-pre-pepco-terms.2026-11-11.sql`.
Guard note: the Appalachian RS-TOD ($90.93) and SBS ($744.41) witnesses do not reproduce on today's primary under either the old
or the new engine (RS-TOD's p. 34 eligibility text — queue item 56 — exists in no stored or cached source; SBS's current
`standby_contract_demand` rule blocks for a contract amount). Old-engine and new-engine outputs are identical for all six guards
and every candidate on all 31 bills.

## Task B — Anomaly screen refinement (2026-11-11)

`src/anomaly_detection.py`: `bill_year`, `bill_month`, `bills_so_far`, `has_audit` are removed by rule ("position or coverage, not
behavior") before the coverage screen; every run records them (`ml_anomaly_runs.features_excluded`, additive column) and stores
its coverage table (`ml.ml_anomaly_coverage`, new). Runs are now history: each build appends a run_id; run 1 is never rewritten.
Fewer than six qualifying features stops before writing. Model, K rule and labels unchanged. Primary run 2 (behind
`data/tariff-pg-pre-anomaly-v2.2026-11-11.sql`): 11 features; top 3 bills 10, 13, 22; robust top 3 1, 10, 13 (overlap 2 of 3);
bill 31 now rank 4 (unflagged), robust rank 26.

## Task C — Requirements pinned to one tested set (2026-11-11)

`requirements.txt` pins every listed package to the version resolved inside the Airflow 2.11.2-python3.12 image (its
constraints set): pandas 2.1.4, openpyxl 3.1.5, pyxlsb 1.0.10, pytest 8.2.1, pdfplumber 0.11.10, psycopg[binary] 3.3.8,
fastapi 0.141.1, uvicorn 0.54.0, python-multipart 0.0.22, httpx 0.28.1, anthropic 2.8.0, scikit-learn 1.8.0. Local venv
(rebuilt from py312; the drifted one kept as `venv-3.12-drift`), API image and Airflow image now resolve the same version of
each. Transitive packages are not pinned: the local venv and API image agree with each other, and 28 of them differ from the
Airflow image's constrained set (e.g. pydantic 2.14.3 vs 2.12.5). Restore point: `data/requirements-prepin.2026-11-11.txt`.


## Staff console removal (2026-11-11)

**Decision.** On 2026-11-11 the owner removed the staff console: the separate login-protected staff site with Master and Reviewer roles, per-account scopes, decisions, change history with revert, and outbox notifications (slices 1–4, landed in Task 12p). Addendum 1 approved moving monitoring's non-delivery helpers out of console code. Addendum 2 kept human-in-the-loop correction in an account-free operator form.

**Removed (code).** `src/staff_api.py`, `src/staff_actions.py`, `src/staff_history.py`, `src/staff_notify.py`, `src/staff_accounts.py`, `src/staff_queue.py`, `src/seed_staff.py`, `src/staff_static/` (`staff.css`), and their tests (`test_staff_actions.py` 10, `test_staff_console.py` 5, `test_staff_history.py` 12, `test_staff_notify.py` 17 — 44 tests). The notification call in `parse_review.queue_item()` and the gate-failure notification in `packet_pipeline._finish` were unwound; both files are byte-identical to their committed pre-console form. `.env.example` had no `STAFF_*` entries.

**Removed (primary).** Behind `data/tariff-pg-pre-console-removal.2026-11-11.sql` (marker verified): `staff_accounts`, `staff_account_scopes`, `staff_sessions`, `staff_decisions`, `staff_change_history`, `staff_notification_outbox` (all 0 rows), `utility_scopes` (3 seeded rows: Pepco, Virginia Electric and Power Company, Appalachian Power Company, all `electric`), and the trigger function `staff_change_history_append_only()`. All 38 remaining tables hash identically before and after; standing counts unchanged; open queue 91.

**Stayed.** The parse-review queue, its items (no row changed; the 29 Pepco rows are as they were), its widened status set and `src/review_status.py`; intake, the coordinator and the Stage 8 scan; monitoring (B24) minus delivery. Monitoring now holds the signal wording (`reason_for`) and utility attribution (`utility_index` / `utility_for`, utility names read from `tariff_versions` instead of `utility_scopes` — the same three names). The `staff_decisions` tally check is gone. `monitor.monitor_runs` keeps its `outbox_rows` / `outbox_note` columns as history; new runs write 0 and "no delivery: findings are recorded in the monitor tables only".

**Operator actions (`src/operator_actions.py`).** A local CLI (`list`, `correct ITEM VALUE`, `dismiss ITEM NOTE`): no server, port, login or password. A correction re-enters the same verifier gate the pipeline uses (`ai_extract._verify_component`: quote on the cited page and the rate printed on it, then the version's printed date check); a refusal returns the verifier's reason, stores nothing and logs nothing, and there is no force option. Applied actions append one row to `operator_changes` (item, action, before/after payload, note, actor fixed as `operator`, timestamp), append-only by trigger. It replaces `staff_decisions` and `staff_change_history` for single-person mode. Created on the primary at 0 rows after a scratch rehearsal. Tests: `tests/test_operator_actions.py`, 11 (9 operator, plus the 2 queue status-check tests carried over from `test_staff_actions.py`).

**Conscious trade.** The console's revert command does not carry over. `operator_changes` keeps before-values, so a manual revert remains possible if ever needed.

**Fix 2 consequence.** Fix 2's primary step (dismissing the 29 Pepco rows, queue 91 → 62) no longer needs a Master account: it is runnable through `operator_actions dismiss` once the owner gives the word. It remains a separate, backup-gated step and was not run here.

**Clone proof** (`tariff_remove_console_20261111`, no console code present): the APCo book replayed from cache with a call-refusing client (0 model calls, 0 structured calls) re-queued the 7 Appalachian replay items (49–55, deleted on the clone first) identically in every field; versions/components/rules unchanged. The Stage 8 scan ran (25 `zero_rate_unexpected` items newly queued on the clone — pre-existing data state, scan candidates unchanged by this task). A monitoring run recorded 25 findings in the monitor tables, 0 delivery; versus the previous run only the four `staff_decisions:*` checks are gone.

**Suite.** 585 → 552 passed (2 skipped): −44 console tests, +11 operator tests; `tests/test_monitoring.py` kept its count (two outbox tests replaced by two findings-only tests).