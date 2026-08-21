# Mover Data Dictionary & Ingestion Notes

Living document. Update this whenever a table gets ingested, a column's meaning/type
is clarified, or a data quality issue is found. See `README.md` for the project overview,
data source locations, and tooling/container docs, and `BUILD_LOG.md` for the project-wide
build/experiment log (this file's "Ingestion log" below is the data-specific slice of it).

Official source data dictionary (per-table column definitions):
https://mover.ics.uci.edu/documentation.html

## Data sources

| Location | Contents |
|---|---|
| `D:\Data_Science_Projects\Mover\EMR\EPIC_EMR\` | 9 EMR CSVs + `flowsheets_cleaned/` (19 parts) — working copy used for ingestion |
| `G:\MOVER\MOVER\` (external drive, connects as `G:`) | Full raw MOVER download: `EPIC_EMR.tar.gz`, `Epic_flowsheets_cleaned.tar.gz` (source of the flowsheets above), `EPIC_patient_measurments.tar.gz` (raw, uncleaned — prefer the cleaned version), `epic_wave_1/2/3_v2.tar.gz` (~293GB total, raw ECG/arterial/pulse-ox waveforms, out of scope for now), `sis_emr.tar.gz` (older SIS system, separate schema, not linked to EPIC patient IDs) |

MOVER paper: Samad et al., *"Medical Informatics Operating Room Vitals and Events
Repository (MOVER): a public-access operating room database"*, JAMIA Open 2023,
https://doi.org/10.1093/jamiaopen/ooad084

## Environment

Native Windows Python is unusable here — Windows Smart App Control blocks pip-installed
compiled binaries (e.g. pyarrow's DLLs) and can only be disabled permanently (never do
this). Everything runs in an isolated Docker image instead, deliberately kept separate
from the user's existing `Ubuntu-22.04` WSL distro (actively used for other projects).

- `Dockerfile` (project root) → image `mover-laboratory:latest` (renamed from
  `mover-iceberg` once it was confirmed to serve ingestion, EDA, and network-serving
  roles — see `BUILD_LOG.md`). `python:3.12-slim` base + `pyiceberg[sql-sqlite,pyarrow]`,
  `duckdb`, `pandas`, `pyarrow`, `dask[dataframe]`.
- Build: `docker build -t mover-laboratory:latest .`
- Run with the project mounted (data never gets baked into the image):
  `docker run --rm -v "/d/Data_Science_Projects/Mover:/work" mover-laboratory:latest <cmd>`
  (from Git Bash, prefix with `MSYS_NO_PATHCONV=1` so `/work` isn't mangled into a
  Windows path). The warehouse itself now lives in a separate `mover-warehouse` Docker
  volume, not under `/work` on the host — see `README.md` → "Data sources".
- No GPU in this image — ingestion is I/O-bound, doesn't need it. A separate image
  extending `pytorch/pytorch:2.7.0-cuda12.8-cudnn9-devel` (CUDA 12.8, matches the
  RTX 5090 laptop GPU, already pulled locally) will be built when we reach ML training.
- `scripts/audit_schema.py` — the schema-audit script used to produce the numbers below.
  Re-run it any time the source CSVs change.

## EPIC dataset tables

Join keys: `LOG_ID` (single surgery/encounter) and `MRN` (patient, can span multiple
`LOG_ID`s). **Casing is inconsistent across files** — uppercase `LOG_ID`/`MRN` in most
tables, lowercase `mrn` in `patient_history.csv` and `patient_visit.csv` (also lowercase
`LOG_ID` is fine there, only `mrn` is lowercase). Normalize casing during bronze/silver
ingestion. All `LOG_ID`/`MRN` columns audited so far are fully non-null.

Column stats below are from a sample (first 200,000 rows unless noted) via
`scripts/audit_schema.py`, run 2026-08-19 — nulls/uniques are sample-based, not exact
population counts, except where noted as "confirmed" from the official docs.

### patient_information.csv — 64,354 unique LOG_ID (matches paper's reported EPIC surgery count exactly)

23 columns. One row per surgery.

| Column | Type decision | Notes |
|---|---|---|
| `LOG_ID` | string (surrogate key) | non-null, 64,354 unique in sample of 65,728 rows |
| `MRN` | string | non-null |
| `DISCH_DISP_C` | **int/categorical**, not float | Standard Epic hospital discharge-disposition code set (Expired, Home Routine, SNF, Hospice, AMA, Rehab, etc.), not MOVER-specific. ⚠️ Docs claim "class 1–100" — **wrong**, real range goes up to **109** (`Home Healthcare Outpatient Related`), confirmed against full distinct-value dump. **7 nulls** (of 65,728 rows) — not fully non-null as the automated pass assumed. Pandas infers float64 due to source formatting — cast explicitly. |
| `DISCH_DISP` | string/categorical | Name paired with `DISCH_DISP_C`. Code `69` has two name variants in the data — `"Designated Disaster Alternative Care Site"` vs `"Designated Disaster Alternate Care Site"` (3 rows each) — confirmed by the user to be a mid-dataset Epic dictionary wording update, not a data quality bug. Leave both mapped to the same code in silver. |
| `HOSP_ADMSN_TIME`, `HOSP_DISCH_TIME` | timestamp | Currently plain strings in CSV — parse at bronze time. `HOSP_ADMSN_TIME`: 0 nulls. `HOSP_DISCH_TIME`: **14 nulls**, confirmed as genuinely still-open admissions at extraction time, not a data gap — all 14 trace back to just 2 distinct `HOSP_ADMSN_TIME` values, each shared across multiple `LOG_ID`s (i.e. one long admission containing several separate surgical encounters), and their linked `patient_medications.ORDER_STATUS_NM` skews less-terminal than baseline (78.6% `Discontinued` vs. 91.1% dataset-wide). `LOS` matches `date_diff('day', HOSP_ADMSN_TIME, HOSP_DISCH_TIME)` exactly for every non-null row — confirms `LOS` is directly derived from these two columns. No rows where discharge precedes admission. Range 2017-11-09 to 2023-08-10. |
| `LOS` | float | Length of stay, in **days** (confirmed) |
| `ICU_ADMIN_FLAG` | boolean/categorical | `Yes`/`No`, 0 nulls. Whether patient was admitted to ICU during visit (confirmed). Docs page names it `ICU_ADMIN` (no `_FLAG` suffix) — treated as the same field, just loose docs naming; no other ICU-related column exists in the schema. 29,131 of 65,728 (44%) are `Yes`. |
| `SURGERY_DATE` | date | 0 nulls. Confirmed as the scheduled/intended surgery day, not simply `date(IN_OR_DTTM)` — 187 rows (0.28%) diverge, every sampled case being a surgery whose `IN_OR_DTTM` rolled past midnight into the next calendar day (e.g. `SURGERY_DATE=2018-11-26`, `IN_OR_DTTM=2018-11-27 01:09`). `SURGERY_DATE` stays pinned to the scheduled day in those cases. |
| `BIRTH_DATE` | **int, "age_years"** — NOT a date | ⚠️ Misleadingly named. Confirmed via official docs: "BIRTH_DATE gives the patient age in years." 0 nulls. Range 17–90, exactly 74 distinct values (90−17+1, no gaps) — consistent with an age field capped at 90 per HIPAA safe-harbor de-identification rules; 668 rows sit at the cap. Rename to `age_years` in silver. |
| `HEIGHT` | string, feet+inches | Real format is `F' I` / `F' I.FFF` (feet, apostrophe, **space**, inches — sometimes decimal, e.g. `5' 10.984`), not `F'I"` — confirm exact regex before parsing. 13,033 nulls (19.8%). 1,950 rows carry decimal-fraction inches, suggesting these were converted from a metric height rather than entered natively. ⚠️ **2 outlier rows flagged for silver-layer clipping/investigation**: `LOG_ID bd14c293acb63fcc` = 8'2.3" (98.3in/~250cm, age 66) — physically implausible, likely a data/unit error; `LOG_ID 8944ca07ff7952b2` = 7'7" (91in/~231cm, age 46) — extreme but within real documented human height (gigantism-range), not obviously wrong. Neither is explained by pediatric age. Min height is 35in (2'11", plausible pediatric case) — low end has no similar outliers. |
| `WEIGHT` | float, **ounces** (EPIC dataset) | ⚠️ Confirmed via official docs — SIS dataset uses grams, EPIC uses ounces. 2,409 nulls (3.7%). Range 811.29–10,451.57oz (≈50.7–653lbs / 23–296kg), avg 174.6lbs — reasonable for a mixed adult/peds surgical population; low end plausible pediatric, high end extreme but medically real (bariatric). Values carry decimal fractions throughout (not raw scale readings) — convert during silver cleaning if kg/lbs needed. |
| `SEX` | categorical | 0 nulls. `Male` 34,911 / `Female` 30,816 / `Unknown` 1. Docs describe as "genotypical sex." |
| `PRIMARY_ANES_TYPE_NM` | categorical | 560 nulls. ⚠️ `"Monitored Anesthesia Care (MAC)"` exists as two distinct strings — one with a trailing space (4,957 rows), one without (264 rows) — pure whitespace artifact, trim in silver. Trimmed distribution: General 52,829 / MAC 5,221 / Moderate Sedation-non-anesthesia-staff 3,726 / Regional 2,576 / Local 374 / Epidural 288 / Spinal 114 / Choice Per Patient on Day of Surgery 38 / Topical 2. |
| `ASA_RATING_C` | int/categorical | Code 1–6, ASA Physical Status Class (confirmed, values match standard scale exactly: Healthy→Brain Dead). **6,970 nulls (10.6%)**, perfectly paired with `ASA_RATING` nulls. Null rate is **highly concentrated by anesthesia type**, not random: 99.8% null for Moderate Sedation (non-anesthesia staff), 99.2% for Local, 100% for Topical — vs. <5% null for anesthesiologist-staffed types (General 3.9%, MAC 4.4%, Regional 2.4%, Spinal 1.8%, Epidural 0.7%). Consistent with ASA rating being an anesthesiologist's preoperative assessment — no anesthesiologist involved (non-anesthesia-staff-administered sedation) means no ASA classification gets generated. |
| `ASA_RATING` | categorical | Name paired with `ASA_RATING_C` (confirmed) — see null analysis above. |
| `PATIENT_CLASS_GROUP`, `PATIENT_CLASS_NM` | categorical | 0 nulls on both. `PATIENT_CLASS_GROUP`: `Inpatient`/`Outpatient` (2 values). `PATIENT_CLASS_NM`: `Outpatient` maps to one value (`Hospital Outpatient Surgery`, 23,844); `Inpatient` splits into two subtypes (`Inpatient Admission` 27,841, `Hospital Inpatient Surgery` 14,043) with a real, confirmed clinical distinction: `Hospital Inpatient Surgery` = admitted specifically for a planned/scheduled surgery — median 0 days from `HOSP_ADMSN_TIME` to `SURGERY_DATE`, shorter stays (median LOS 3 days, avg 4.2), lower ICU rate (53.0%). `Inpatient Admission` = admitted as an inpatient first (for another/broader reason), surgery happens partway through a more complex course — median 2 days (avg 5.5) admission→surgery gap, much longer stays (median LOS 9 days, avg 14.9), higher ICU rate (73.2%). Confirmed with the user; note the label names read as the *opposite* of this distinction at face value — don't assume from the name alone. |
| `PRIMARY_PROCEDURE_NM` | string/categorical | 6 nulls. **1,768 distinct values confirmed exact** (matches earlier sample-based estimate). Free-text procedure names; top values are a plausible clinical mix (cardiac catheterization, laparoscopic cholecystectomy, diagnostic laparoscopy, exploratory laparotomy, D&E uterus, etc.), no structural issues found. |
| `IN_OR_DTTM`, `OUT_OR_DTTM`, `AN_START_DATETIME`, `AN_STOP_DATETIME` | timestamp | Nulls: 6,558 / 6,626 / 7,484 / 7,498 respectively — not one field gating the rest. Of the 6,558 `IN_OR_DTTM`-null rows, 5,285 (80.6%) have all four blank together; largely explained by **non-OR procedural locations** (top procedures: cardiac catheterization, apheresis/stem cell collection, right-heart-catheter insertion, lumbar puncture, AV fistulogram — cath lab/IR/bedside, not an OR), consistent with `PRIMARY_ANES_TYPE_NM` skewing to "Moderate Sedation - by non-anesthesia staff only" (37% vs. 13% baseline). A smaller true-OR subset (cholecystectomy, exploratory laparotomy, ORIF) is also blank — likely a genuine charting gap. ⚠️ Either way, surgery duration and anesthesia duration are unrecoverable for these rows — clinically and billing-relevant, since anesthesia time is billed by duration. |

### patient_history.csv — patient diagnosis history — 970,741 rows

3 columns: `mrn` (lowercase!), `diagnosis_code`, `dx_name`. 0 nulls on `mrn`/`dx_name`.
43,547 distinct `mrn`, 6,050 distinct `diagnosis_code`, 59,056 distinct `dx_name` (full
counts, not sample-based). CSV header matches bronze schema exactly, all 3 columns
documented by MOVER — no gaps.

| Column | Type decision | Notes |
|---|---|---|
| `mrn` | string | lowercase column name (inconsistent with most other tables' `MRN`), 0 nulls |
| `diagnosis_code` | string/categorical | Docs: ICD-9-CM. **Mostly confirmed** — real codes are genuinely ICD-9-CM shaped (numeric/decimal, plus 79,304 V-code and 2,844 E-code rows), despite the dataset spanning 2017–2023 (post ICD-10-CM mandate) — plausible since this is *history*, so entries can predate the ICD-10 cutover and never get recoded. **245,444 nulls (25.3%)** while `dx_name` is never null — per user, plausibly because diagnosis coding is a billing-system step that happens later/separately from the clinical record, so the name gets captured at time of care even when no code has been assigned yet. ⚠️ Found `IMO0001` (527 rows) is **not a real diagnosis code** — it's Epic's Intelligent Medical Objects "No-Map" placeholder, attached here to ~26 completely unrelated `dx_name` values (`No known problems`, `Opioid use agreement exists`, `Advanced age`, `Research study patient`, etc.). Don't treat `diagnosis_code` as 1:1 with `dx_name` for these rows. Also found 3 rows with a genuine ICD-10-CM-shaped code (`C44.91`) — a small leak past the stated ICD-9-CM standard. |
| `dx_name` | string, free text | 0 nulls, always present even when `diagnosis_code` isn't (see above) |

### patient_visit.csv — visit-level diagnoses

4 columns: `LOG_ID`, `mrn` (lowercase!), `diagnosis_code`, `dx_name`. Sample: 52,923
unique `LOG_ID`, 40,296 unique `mrn`, 3,525 unique `diagnosis_code` (58,188 sample nulls).

### patient_labs.csv — lab results

10 columns. Sample of 200k rows collapses to just 982 unique `LOG_ID` / 829 unique `MRN`
— **very dense per encounter**, good candidate for time-series/sequence features rather
than flat aggregates. `Observation Value` is float64 (554 sample nulls). `Collection
Datetime` is a plain string, needs parsing.

### patient_lda.csv — lines, drains, airways

9 columns. `site` has 77,577 sample nulls (many LDAs have no site recorded).
`placement_instant`/`removal_instant` are plain strings, needs parsing.
`flo_meas_name` — 83 unique values in sample, low cardinality, dictionary-encodes well.

### patient_medications.csv — medication orders + MAR records

MAR = Medication Administration Record (the log of actual administration events for an
order, as distinct from the order itself — see `MAR_ACTION_NM` below).

18 real columns (+ `_source_file`/`_ingested_at` provenance). **27,961,524 rows, 65,742
distinct `LOG_ID`s, ~425 rows/encounter** (full population, not sample-based) — dense
MAR-style sequential administration data. Strong candidate for medication-*sequence*
modeling, not flat per-surgery aggregation (see project memory on candidate ML
directions).

Column-by-column audit below went through every column against the official MOVER docs
(`mover.ics.uci.edu/patient-meds.html`) and real data, reviewed with the user (clinical
domain expertise) column by column — several corrections and open questions came out of
that review that a docs-only pass would have missed.

**Join key note:** `LOG_ID` here is the visit/admission/encounter number (not
surgery-specific). Only 94.8% of this table's `LOG_ID`s (62,298 / 65,742) match
`patient_information`, and 72.5% (47,660 / 65,742) match `patient_visit` — expected, not
a data quality issue: `patient_information` is filtered to encounters with a captured
surgical procedure, a narrower scope than "every encounter with a medication order."

| Column | Source | Meaning | Status |
|---|---|---|---|
| `MRN` | docs | Patient ID | confirmed |
| `LOG_ID` | docs (refined) | Visit/admission/encounter number — broader than "surgery," see join note above | confirmed |
| `ENC_TYPE_C` / `ENC_TYPE_NM` | **undocumented**, inferred | Encounter type: `3`=Hospital Encounter (91%), `52`=Anesthesia (8.7%), rare Infusion Ctr Visit/Office Visit/Consultation/Nurse Only/Procedure Visit. `_C`/`_NM` is standard Epic Clarity naming (Category code / resolved Name) — `_C` is the only paired code column in this table | ⚠️ **investigate**: whether `ENC_TYPE_C` maps to Epic's standard/foundation category values (shared across Epic installations) rather than a UCI-specific code |
| `ORDERING_DATE` | docs | When the order was placed | confirmed — verified `ORDERING_DATE ≤ START_DATE` holds 99.97% of rows (92.5% same-day, 7.5% ordered days ahead); 0.03% (9,656 rows) backwards, likely retroactive/backdated entry |
| `ORDER_CLASS_NM` | docs | How the order was placed: Inpatient (~97%), ePrescribe, Normal, Security Rx Print (587 rows) | ⚠️ **investigate**: real-world relevance unclear — `Security Rx Print` may relate to restricted/controlled substances; needs Epic community forum research |
| `MEDICATION_ID` | docs (**wrong**) | Docs claim "CPT code" — false. Values (99 to 99,999,204,200, inconsistent digit counts) don't fit CPT's fixed 5-digit format. Actually Epic's internal medication-master surrogate key, 3,960 distinct | ⚠️ **investigate**: whether hospital-specific (UCI-built formulary), Epic-specific (shared foundation medication master), or maps to an external med DB (FDB/Multum) — determines if this ID is usable outside this dataset |
| `DISPLAY_NAME` | **undocumented**, inferred | The medication *order* string as entered — formulation/concentration/instructions as ordered (e.g. `"phenylephrine (NEO-SYNEPHRINE) 40 mg in sodium chloride 0.9% 250 mL infusion"`). 16,624 distinct. This is the order, not a record of what was actually delivered — actual administration is tracked separately via `MAR_ACTION_NM`/`MED_ACTION_TIME` | confirmed (order-level, not administration-level) |
| `MEDICATION_NM` | docs | Internal formulary/order-set name, facility-specific (e.g. `(UCI)` suffix), 4,344 distinct | 🔶 **hypothesis, unconfirmed**: may represent the specific product/formulation as stocked/procured by the pharmacy (tied to manufacturer/packaging/NDC), vs. `DISPLAY_NAME`'s clinician-facing order description — needs investigation |
| `START_DATE` / `END_DATE` | docs (**imprecise**) | Docs call `START_DATE` "when administered" — actually the **order-level** date range (date-only, midnight timestamps), not an administration event | confirmed via `ORDERING_DATE` check above |
| `ORDER_STATUS_NM` | **undocumented**, inferred | Order lifecycle: Discontinued (91%), Completed, Dispensed, Verified, Sent, Canceled | ⚠️ **data quality + investigate**: 9,377 rows have a stray `"0 "` text prefix (`"0 Discontinued"` vs `"Discontinued"`, `"0 Dispensed"` vs `"Dispensed"`) — same shape as the known `ADMIN_SIG` `"0 NULL"` ingestion bug. Compare the two groups directly (other column values) before assuming it's pure export noise — could be clinically significant |
| `RECORD_TYPE` | docs | preop/periop/postop, same pattern as elsewhere in the dataset | 🔶 **working hypothesis, not fully confirmed**: which phase of the encounter the order was prescribed and administered under |
| `MAR_ACTION_NM` | **undocumented**, inferred | The administration-record action per row. Scheduled meds (e.g. "every 4h" orders): system auto-generates mandatory administration slots; each resolves to `Given` (actually administered — counts toward dose/exposure) vs. `Hold`/`MAR Hold` (acknowledged but not given — no exposure). Infusion meds: separate vocabulary — `Started`, `Stopped`, `Rate Verify` (a check/confirmation, not necessarily a change), `Rate Change` (actual adjustment), `New Bag` (fluid replacement) | ⚠️ **central open task**: classify every `MAR_ACTION_NM` value into "counts toward actual drug exposure" vs. "logistics/non-exposure event" — required before this table can correctly answer "what was this patient's actual medication exposure" |
| `MED_ACTION_TIME` | **undocumented**, inferred | The real administration-action timestamp (actual time-of-day) — the correct clock for MAR events, unlike date-only `START_DATE`/`END_DATE` | confirmed |
| `ADMIN_SIG` | docs | Dose administered, float. Known ingestion bug (malformed `"0 NULL"` value, fixed via automatic type coercion from schema) | ⚠️ **investigate**: meaning depends on `MAR_ACTION_NM`, not just "populated vs. null" — e.g. `Rate Verify` legitimately has no dose documented (pump-rate confirmation only, not a new dose event). Need a `MAR_ACTION_NM` → expected-`ADMIN_SIG`-behavior mapping |
| `DOSE_UNIT_NM` | docs | Dose unit, paired with `ADMIN_SIG` | confirmed |
| `MED_ROUTE_NM` | docs | Administration route | confirmed |

Note: the official docs also mention a `SURGERY_DATE` column for this table — it does not
exist in the actual source CSV (verified against the raw file header, ruling out an
ingestion bug). Likely boilerplate carried over from `patient_information`'s docs page,
which does have `SURGERY_DATE`.

**Overarching open item, not yet resolved:** we do not yet have a complete, verified
picture of exactly how this system documents medication administration end to end — the
interplay between `MAR_ACTION_NM`, `ADMIN_SIG`, `START_DATE`/`END_DATE`, and
`MED_ACTION_TIME` across scheduled vs. infusion medications needs systematic
investigation before this table can be trusted for exposure/dose-based features. The
per-row flags above are pieces of that larger question, not independent issues.

### patient_post_op_complications.csv — SmartData postop elements

6 real columns (+ provenance), 203,945 rows. All 6 columns are actually documented by
MOVER (unlike `patient_medications`) — reviewed column by column against docs + real
data with the user, same process as before.

**Label taxonomy — RESOLVED (previously an open item):** `Element_Name`/`Element_abbr`
has 12 distinct values, not 11. One of them, `AN AQI POST-OP COMPLICATIONS` /
`AN Post-op Complications` (200,139 rows — dwarfs everything else), is a generic
"a postop complication was documented" flag, almost certainly tied to required AQI
(Anesthesia Quality Institute / NACOR) reporting — not a specific complication type.
**The remaining 11 values match the MOVER paper's own Table 4 (11 complication classes)
almost exactly, same names, same order:**

| Class | Paper count | Our count |
|---|---|---|
| Other | 1,093 | 1,094 |
| Cardiovascular | 861 | 866 |
| Respiratory | 735 | 748 |
| Airway | 373 | 375 |
| Metabolic | 154 | 154 |
| Neurological | 147 | 147 |
| Administrative | 118 | 118 |
| Injury/infection | 117 | 117 |
| Medication | 94 | 94 |
| Regional | 60 | 61 |
| Chronic pain | 32 | 32 |

(Samad et al., JAMIA Open 2023 — small per-class discrepancies are consistent with a
slightly different dataset snapshot, not a mapping error.) The paper's own complications
table never mentions AQI or a generic flag — confirming it's excluded from their 11-class
taxonomy, not an unlabeled 12th class. **Usable directly as ML labels once the AQI-flag
row is filtered out.**

| Column | Source | Meaning | Notes |
|---|---|---|---|
| `MRN` | docs | Patient ID | confirmed |
| `LOG_ID` | docs | Encounter number; duplicate `MRN`s expected (multiple surgeries per patient) | confirmed |
| `Element_Name` / `Element_abbr` | docs | Complication type / abbreviation | see label taxonomy above |
| `CONTEXT_NAME` | docs, confirmed | `ENCOUNTER` (85.6%), `ORDER` (7.9%), `NOTE` (6.5%) | **97.8% of the specific-class labels (3,722/3,806) sit under `CONTEXT_NAME = 'ENCOUNTER'`** — `ORDER`/`NOTE` are almost entirely just the generic AQI flag (only 84 specific-class rows between them). Filtering to `ENCOUNTER` gets nearly all real labels with much less noise |
| `SMRTDTA_ELEM_VALUE` | docs | Free text detail | genuine free text, mostly null (most complications have no extra note) |

### patient_procedure events.csv — note the literal space in the filename

5 columns. `EVENT_DISPLAY_NAME` — 76 unique values in sample (low cardinality).
`NOTE_TEXT` — 199,940 sample nulls out of 200k rows (almost always empty; only 23 unique
non-null values in sample — largely boilerplate, not rich free text).

### patient_coding.csv — billing codes

6 columns. `REF_BILL_CODE_SET_NAME` — only 2 unique values, 68,616 sample nulls.

### flowsheets_cleaned/ — patient measurements (vitals), the 10th EPIC table

19 parts, **1,440,918,933 rows total** (~1.44 billion). (An earlier draft of this doc
said 1,440,918,952 — that count included the header row in each file's `wc -l`, an
off-by-one repeated across all 19 parts. The figure below is the corrected, verified
count — confirmed three independent ways during ingestion: live per-chunk accounting,
a raw Python `csv` module count, and a post-ingestion DuckDB query. No data was lost;
this was a documentation bug in the original audit script, not an ingestion defect.)
Confirmed via official docs:
`FLO_NAME` = category of measurement, `FLO_DISPLAY_NAME` = measurement name, `UNITS` =
unit for the value, `RECORD_TYPE` = **pre-op / peri-op / post-op** (confirmed 3 values),
`RECORDED_TIME` = exact timestamp (docs call this column `RECORD_TIME`, CSV header says
`RECORDED_TIME` — same field). `MRN`/`LOG_ID` defined same as elsewhere.

`MEAS_VALUE` is genuinely mixed-type (confirmed via sampling 500k rows each of parts
1 and 2): **~75–80% numeric, ~20–23% text, <0.4% null**. Must be split into
`MEAS_VALUE_NUM` (float) and `MEAS_VALUE_TXT` (string) at bronze time — this was already
the plan from the original handoff doc, now confirmed necessary by real data.

Each part CSV also carries a leftover pandas index column (`Unnamed: 0`) — drop it
during ingestion, it's not real data.

Row counts per part (corrected/verified during actual ingestion, 2026-08-19 — see note
above; these supersede the original `audit_schema.py` figures, each of which was 1 row
too high due to counting the header line):

| Part | Rows | Size |
|---|---|---|
| flowsheet_part1.csv | 487,423,896 | 51.96GB |
| flowsheet_part2.csv | 46,157,622 | 4.81GB |
| flowsheet_part3.csv | 8,691,534 | 0.89GB |
| flowsheet_part4.csv | 55,690,063 | 5.89GB |
| flowsheet_part5.csv | 55,650,761 | 5.87GB |
| flowsheet_part6.csv | 55,724,417 | 5.88GB |
| flowsheet_part7.csv | 55,673,130 | 5.88GB |
| flowsheet_part8.csv | 55,873,150 | 5.89GB |
| flowsheet_part9.csv | 55,912,816 | 5.90GB |
| flowsheet_part10.csv | 56,121,437 | 5.89GB |
| flowsheet_part11.csv | 55,815,972 | 5.89GB |
| flowsheet_part12.csv | 56,124,949 | 5.92GB |
| flowsheet_part13.csv | 56,796,759 | 5.95GB |
| flowsheet_part14.csv | 56,959,094 | 5.94GB |
| flowsheet_part15.csv | 56,714,311 | 5.93GB |
| flowsheet_part16.csv | 56,388,188 | 5.91GB |
| flowsheet_part17.csv | 56,494,910 | 5.89GB |
| flowsheet_part18.csv | 56,605,235 | 5.90GB |
| flowsheet_part19.csv | 56,100,689 | 5.84GB |

⚠️ `flowsheet_part1.csv` is **not corrupted** despite being ~9x larger than parts 4–19
— its row count is proportionally ~8.7x larger too (487M vs ~55-57M), so the size is
legitimate, just an unevenly split source export. `part2`/`part3` are also smaller than
parts 4-19 for the same reason (uneven upstream chunking, not a data problem).

Given ~1.44 billion rows, this table is never loaded wholesale into pandas — all
aggregation happens via DuckDB SQL directly against the Iceberg/Parquet files (see
architecture notes below).

## Architecture: bronze / silver / gold

- **Bronze**: one Iceberg table per source file (9 EMR tables + flowsheets), typed
  per the corrections above, minimal transformation otherwise.
- **Silver**: cleaned + conformed, consistent `LOG_ID`/`MRN` casing across all tables so
  DuckDB joins work directly. Not aggregated — `silver.vitals` still has ~1.44B rows,
  just cleaned and typed.
- **Gold**: per-model, denormalized feature+label tables produced by a feature-engineering
  SQL query against silver (e.g. `gold.icu_transfer_features`, one row per surgery).
  Built on demand per ML question, not up front.

Partitioning: flowsheets/labs/meds by year (via Iceberg's `years()` transform on the
relevant timestamp column, not a manually derived year column) — organizes files for
query-time partition pruning. This is a query-speed optimization, not a storage-size
optimization — the actual space savings vs. raw CSV come from Parquet's columnar
encoding (dictionary encoding on low-cardinality columns like `FLO_NAME`, type-aware
numeric encoding, block compression), not from partitioning.

## Ingestion log

- 2026-08-19: Flowsheets extracted from `G:\MOVER\MOVER\Epic_flowsheets_cleaned.tar.gz`
  to `D:\...\EPIC_EMR\flowsheets_cleaned\` (19 parts).
- 2026-08-19: `mover-iceberg:latest` Docker image built and verified (pyiceberg 0.11.1,
  duckdb 1.5.5, pandas 3.0.5, pyarrow 25.0.1, dask 2026.7.1).
- 2026-08-19: Full schema audit run across all 9 EMR tables + flowsheets (see tables
  above).
- 2026-08-19: `git init` + `.gitignore` set up (excludes all CSVs/Parquet/Iceberg
  data/logs — clinical data never enters git history).
- 2026-08-19: Bronze Iceberg schemas designed (`scripts/schemas.py`), local PyIceberg
  `SqlCatalog` set up (`scripts/catalog.py`, SQLite-backed, warehouse at
  `iceberg_warehouse/`), ingestion driver written (`scripts/ingest.py`, resumable via
  `iceberg_warehouse/_ingestion_status.json` and a `purge_source_file` idempotency
  mechanism — deletes any partial rows for a source file before re-ingesting it, so a
  mid-file crash can be safely retried without duplicating data).
- 2026-08-19: All 9 EMR tables ingested to `bronze.*` — every table's row count matches
  its source CSV exactly (verified via DuckDB `iceberg_scan`):
  `patient_information`=65,728, `patient_history`=970,741, `patient_visit`=219,257,
  `patient_coding`=2,033,948, `patient_post_op_complications`=203,945,
  `patient_lda`=465,801, `patient_procedure_events`=640,223, `patient_labs`=29,079,344,
  `patient_medications`=27,961,524. (One bug hit and fixed along the way: `ADMIN_SIG`
  and other `DoubleType` columns weren't covered by the original manual
  `int_cols`/`timestamp_cols` config lists, causing a crash on a malformed value
  `"0 NULL"` partway through `patient_medications` — fixed by deriving type coercion
  automatically from the schema instead of manual lists, then safely retried thanks to
  the purge-based idempotency.)
- 2026-08-19: Flowsheets ingested to `bronze.flowsheets` (all 19 parts unified into one
  table with `_source_file`/`_ingested_at` provenance columns) — **1,440,918,933 rows**,
  wall-clock time **60.5 minutes** (21:05–22:06), throughput ~370-490k rows/sec.
  Verified three independent ways (live ingestion accounting, a raw Python `csv` module
  count on part3, and a post-hoc DuckDB `GROUP BY _source_file` count) — all agree
  exactly, confirming complete, non-duplicated data. See the flowsheets section above
  for the row-count correction this surfaced in the original audit numbers.
- 2026-08-20: **Re-ingested the entire bronze layer** to fix a structural path bug that
  broke external SQL tools (DBeaver) from reading the tables. Root cause: Iceberg bakes
  absolute warehouse paths into every manifest/snapshot file at write time. The original
  warehouse was `file:///work/...` (the container's mount path) — fine for querying from
  inside the container, but native Windows DuckDB (used by DBeaver) strips exactly one
  leading `/` and resolves the remainder relative to the current working directory, not
  any drive root, so `/work/...` references were unresolvable from Windows regardless of
  CWD tricks or junctions. Fixed by having `entrypoint.sh` create a symlink
  `/D:/Data_Science_Projects/Mover -> /work` inside the container and pointing the
  catalog's warehouse at `file:///D:/Data_Science_Projects/Mover/iceberg_warehouse`
  instead — every baked-in path is then already a valid Windows path
  (`D:/Data_Science_Projects/Mover/...`) once that one leading slash is stripped, from
  *either* side. Verified working from native Windows DuckDB (installed via
  `winget install DuckDB.cli`), queried from `C:\` (a different drive than the data, to
  rule out CWD-relative resolution) — both `patient_information` (65,728 rows) and
  `flowsheets` (1,440,918,933 rows) returned exact correct counts. All 10 tables
  reconfirmed after re-ingestion, byte-for-byte matching the prior run's counts.
  `scripts/dbeaver_setup.sql` regenerated (via `scripts/gen_dbeaver_sql.py`) with the
  corrected paths — no `SET unsafe_enable_version_guessing` needed at all now, since it
  uses direct metadata.json paths.
- **Bronze layer is now fully built, verified, and externally queryable (confirmed via
  native Windows DuckDB, not just from inside the container). Total across all 10 bronze
  tables: 1,502,559,444 rows.**

## Open items / TODO

- [x] Design + write bronze Iceberg schemas incorporating the type corrections above
- [x] Set up local PyIceberg `SqlCatalog` + warehouse directory
- [x] Write chunked ingestion script (CSV → typed → bronze Iceberg tables)
- [x] `git init` + `.gitignore`
- [x] Ingest all 9 EMR tables + flowsheets to bronze, verified via DuckDB
- [x] Verify the postop-complications `Element_Name`/`Element_abbr` values actually map
      to the paper's 11 complication classes — confirmed, see `patient_post_op_complications`
      section above (11 of 12 values match the paper's Table 4 almost exactly; the 12th is
      a generic AQI reporting flag, not a class)
- [ ] Design silver layer: consistent `LOG_ID`/`MRN` casing across tables, `HEIGHT`
      parsed to numeric, `WEIGHT` unit conversion if needed, `BIRTH_DATE` renamed to
      `age_years`
- [ ] Design gold-layer feature tables once a specific ML question is chosen (see
      candidate directions in project memory / earlier conversation — real-time
      intraoperative deterioration prediction was the favored direction)
