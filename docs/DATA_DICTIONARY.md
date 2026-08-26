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
| `MRN` | string | Patient-level key (`LOG_ID` is encounter-level). Non-null. 39,685 distinct values — matches the paper's reported patient count exactly. ⚠️ **26 of those are corrupted into scientific notation** (`2.75E+15`, `9.04E+18`, …), affecting 38 rows / 37 encounters. Present in the source, not introduced here. See "MRN corruption and patient-level linkage" below before using `MRN` as a join or grouping key. |
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

### patient_visit.csv — visit-level diagnoses — 219,257 rows

4 columns: `LOG_ID`, `mrn` (lowercase!), `diagnosis_code`, `dx_name`. 0 nulls on
`LOG_ID`/`mrn`/`dx_name`. 55,912 distinct `LOG_ID`, 42,116 distinct `mrn`, 3,604 distinct
`diagnosis_code`, 25,331 distinct `dx_name` (full counts). CSV header matches bronze schema
exactly, all 4 columns documented by MOVER. Same `diagnosis_code`/`dx_name` semantics as
`patient_history` — ICD-9-CM claim holds (V/E-code and numeric shapes dominate), `IMO0001`
Epic "No-Map" placeholder reappears (53 rows), `diagnosis_code` has 65,364 nulls (29.8%)
against `dx_name`'s 0% — same plausible billing-coding-lag explanation.

**Investigated finding — `patient_visit` is not scoped 1:1 to `patient_information`'s
surgical cohort.** 6,761 of the 55,912 distinct `LOG_ID`s (12.1%, 21,856 rows / 10.0% of
all rows) don't join back to `patient_information` at all. Investigation trail:
- Diagnosis profile of the orphan `LOG_ID`s (Hypertension, ESRD, Sepsis, CAD, AKI, Trauma,
  Prostate cancer — high-acuity, CMS-HCC-flagged conditions) closely resembles the matched
  `LOG_ID`s' profile — ruled out "these are just routine/wellness visits."
- But only **1,708 of 6,147 (27.8%)** orphan-`LOG_ID` `mrn`s ever appear as a surgical
  patient in `patient_information` — most of these patients never had a surgery in this
  dataset at all.
- And the orphan `LOG_ID`s barely appear anywhere else in the warehouse: only 12 of 6,761
  show up in `patient_labs`, only 16 in `patient_medications`.

**Conclusion:** `patient_visit` appears to be pulled from a broader source population than
the surgical cohort the rest of the bronze tables are built around — same health system,
similar case mix (explaining the diagnosis-profile similarity), but not the same patient/
encounter set, and largely disconnected from the rest of the warehouse (explaining the
near-total absence elsewhere). Not a data corruption issue — a real scope mismatch to keep
in mind for any join against `patient_information`.

### patient_labs.csv — lab results — 29,079,344 rows

10 columns. Docs page `patient-labs.html` documents `MRN`, `LOG_ID`, `Lab Code`,
`Lab Name`, `Observation Value`, `Measurement Units`, `Reference Range`, `Abnormal Flag`,
`Collection Datetime` — `ENC_TYPE_NM` is **undocumented**, same gap pattern as
`patient_medications`/`patient_procedure_events`. CSV header matches bronze schema
exactly (spaces → underscores). Very dense per encounter — good candidate for time-series/
sequence features rather than flat aggregates. Docs confirm "possible for this table to
have duplicate MRNs, indicating a single patient has come in for multiple surgeries" —
verified, 10,653 `MRN`s span more than one `LOG_ID`.

| Column | Type decision | Notes |
|---|---|---|
| `LOG_ID` / `MRN` | string (join keys) | 0 nulls on both. 56,125 / 33,249 distinct. |
| `ENC_TYPE_NM` | categorical, **undocumented** | 0 nulls, but only **1 distinct value** (`"Hospital Encounter"`) across all 29,079,344 rows — completely constant, zero information content. |
| `Lab_Code` | string | 0 nulls, 1,533 distinct. Docs: LOINC code. **Fully confirmed** — 100% of rows (29,079,344/29,079,344) match the LOINC shape (`digits-checkdigit`), no exceptions found. |
| `Lab_Name` | string/categorical | 0 nulls, 1,257 distinct. Paired with `Lab_Code`. |
| `Observation_Value` | float | 89,716 nulls (0.3%). ⚠️ **`9999999.0` sentinel value in 13.7% of all rows (3,976,165)** — confirmed present in the raw CSV at the exact same count, not an ingestion artifact. Classified by cross-referencing `Lab_Name` against clinical/LOINC knowledge into 3 mechanisms: **(1)** purely qualitative tests forced into this numeric column, ~100% sentinel for that test (blood morphology flags, urine drug screen panels, serology, PCR detected/not-detected panels, blood typing, urinalysis dipstick items, method/panel-header rows that were never real results) — **(2)** normally-numeric tests with a small, consistent sentinel minority (<2% — `Potassium`, `Creatinine`, `Platelets`, `ALT`), plausibly lab-comment cases (hemolyzed specimen, instrument error, censored out-of-range value) — **(3)** tests genuinely split between qualitative-screen and quantitative-confirmatory reporting under one `Lab_Name`, 35–85% sentinel (`GFR` ~42–60%, consistent with the standard ">60" censored-reporting convention; `Urobilinogen`/`Ketones` ~83%; `Ethanol` ~78%; `hCG` ~70%; `Base excess` ~48%; `Fibrin D-dimer` ~35%). Confirmed with user. **Any numeric analysis on this column must filter the sentinel first.** |
| `Measurement_Units` | categorical | 0 nulls, 151 distinct. `"Unknown"` in 4,819,392 rows (16.6%) — overlaps with but isn't identical to the `Observation_Value` sentinel set. |
| `Reference_Range` | string | 0 nulls, 726 distinct. `"Unknown"` in 7,719,489 rows (26.6%). |
| `Abnormal_Flag` | categorical | 201,610 nulls (0.7%). ⚠️ **5 distinct values, not the 3 docs describe** ("normal, low, or high") — real values are `N` (Normal, 18,578,102), `L` (Low, 5,320,856), `H` (High, 4,826,661), plus docs-omitted **`LL`** (Critical Low, 82,468) and **`HH`** (Critical High, 69,647) — a more clinically important distinction (critical values trigger different response protocols) that the docs simply don't mention. |
| `Collection_Datetime` | timestamp | 0 nulls, 852,189 distinct. |

⚠️ **Data quality: exact-duplicate rows confirmed at the source.** 54,979 groups
(109,995 rows, 0.38% of table) share identical `LOG_ID` + `MRN` + `Lab_Code` +
`Observation_Value` + `Collection_Datetime` (down to the minute) — verified a genuine
example (a `287.0` Platelets result at the same encounter/timestamp) is present as two
literal separate lines in the raw source CSV, not an ingestion bug.

### patient_lda.csv — lines, drains, airways — 465,801 rows

9 columns, all documented by MOVER (docs page is `patient-lda.html`, not `-table.html`).
CSV header matches bronze schema exactly. Conceptual hierarchy (per user): `Line_Group_Name`
= broadest category → `flo_meas_name` = generic device type → `description` = the specific
device instance (size/location/etc., system-templated, not free text — see below) →
`properties_display` = the insertion/placement record.

⚠️ **Confirmed 22.9% duplicate rows (106,534 of 465,801)** — see "Duplicate-row audit"
section below for the full investigation and per-pairing breakdown. Don't aggregate or
count devices from this table without deduplicating first.

| Column | Type decision | Notes |
|---|---|---|
| `LOG_ID` / `MRN` | string (join keys) | 0 nulls on both. 61,798 / 36,979 distinct. |
| `description` | string, **templated** (not free text) | 0 nulls, 87,791 distinct. Confirmed system-generated concatenation, consistent slot structure per device type: `[STATUS] {Device Name} - {date} [time] {placer role} {material/attributes} {size} {flag}` (e.g. `[REMOVED] Indwelling Urinary Catheter -  10/12/18 OR Standard (Latex) 14 fr 10 ml Yes`). **99.2% of rows (461,960) carry a `[REMOVED]` prefix** — but this does *not* map cleanly to `removal_instant` being populated (that's true for 99.996% of rows regardless of the tag), so `[REMOVED]` isn't simply "device has since been removed." Open item: real meaning of the tag not yet resolved — the 3,839 untagged rows turned out to be the duplicate-row group (see below), so the tag may relate to which of a duplicate pair got the display-status treatment, not device status itself. |
| `properties_display` | string, free text | 34 nulls, 76,937 distinct. Per user: the insertion/placement record — real samples confirm this (`Inserted By: RN  Insertion attempts: 1  Size: 22 G  Orientation: Right  PIV Location: Forearm`), also sometimes carries removal reason/post-removal assessment. Contains a recurring `(c)` token prefix on some sub-fields (e.g. `Inserted By: (c) OR`) — meaning not yet investigated. |
| `flo_meas_name` | categorical | 2 nulls, 97 distinct (docs' implied ~83 was a sample undercount). The clean, standardized device/event category name in ALL CAPS — the structured counterpart to `description`'s free-text instance. |
| `site` | categorical | **178,572 nulls (38.3%)** — not random: null is concentrated where the concept doesn't apply (e.g. `Incision` rows have no `site`, since location is embedded in `description` instead). 451 distinct values. |
| `placement_instant` | timestamp | **13,850 nulls** — open item: asymmetric with `removal_instant`'s near-zero null rate; working theory is the device was placed before this encounter's monitoring window started but removed within it, not yet confirmed. |
| `removal_instant` | timestamp | Only 2 nulls. 0 rows where `removal_instant < placement_instant` — full temporal integrity confirmed. |
| `Line_Group_Name` | categorical | 215 nulls. **15 distinct values including 14 real non-null categories** — docs claim "12 categories," undercounts by 2. Investigated `"Line Type"` (3,874 rows) specifically since it read like a generic fallback bucket (similar shape to the AQI generic-flag finding in `patient_post_op_complications`) — **turned out to be a real, specific category**, not a fallback: groups specialized vascular-access devices (Introducer, Large Bore Access-VAD/ECMO, Arterial/Venous Sheath, Distal Perfusion Cannula-ECMO, Hemostasis Pressure Device, Esophageal Temperature Probe) — just an ambiguous category name, not a data quality issue. |

**Duplicate-row finding detail:** 53,174 groups (106,534 rows, 22.9% of table) share
identical `LOG_ID` + `description` + `placement_instant` + `site` but differ in
`Line_Group_Name` — the same physical device charted under two different navigator
categories simultaneously. All 5 pairings and counts:

| `flo_meas_name` | Duplicated across | Groups |
|---|---|---|
| Indwelling Urinary Catheter | `Drain` + `Urinary Drainage` | 37,698 |
| Wound Vac | `Drain` + `Wound Therapy` | 7,135 |
| Pressure Ulcer Injury | `Pressure Ulcer Injury` + `Wound` | 5,833 |
| Hemodialysis/Pheresis Catheter Access | `CVC Line` + `Drain` | 2,413 |
| NG/OG/NJ Feeding Tube (NICU) | `Drain` + `Nasogastric/Orogastric tube` | 95 |

`Drain` is the generic partner in 4 of 5 pairings — reads like Epic's LDA navigator files
certain device types under both a generic "Drain" tab and their specific clinical tab for
visibility, and the export captures both navigator placements as separate rows rather than
one row with two tags.

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

### patient_procedure events.csv — note the literal space in the filename — 640,223 rows

5 columns. Docs page (`patient-procedure-events.html`) documents `MRN`, `LOG_ID`,
`EVENT_DISPLAY_NAME`, `EVENT_TIME` — `NOTE_TEXT` is **undocumented**, same pattern as
`patient_medications`' gaps. CSV header matches bronze schema exactly.

| Column | Type decision | Notes |
|---|---|---|
| `LOG_ID` / `MRN` | string (join keys) | 0 nulls on both. 43,600 / 28,316 distinct. |
| `EVENT_DISPLAY_NAME` | categorical | 0 nulls, 90 distinct (docs' implied ~76 was a sample undercount). Docs: "name of the anesthesia event." Real values are a mix of true timeline checkpoints (`Anesthesia Start`/`Stop`, `Sign In`, `Induction`, `Intubation`, `Extubation`, `Emergence`, `Start`/`Stop Data Collection`, `Transported to PACU/ICU...`, `Visit Signed`) and clinical-count/administrative events (`Two Anti-Emetics Administered`, `IV Antibiotics`, `Narcotic Balance`, `Quick Note`, `Mark Now`, `Case Delayed`, `Data Artifact`). **Planned use (per user): estimate procedure/phase timing and build OR-efficiency quality metrics from the checkpoint subset** — see [[mover_or_efficiency_metrics]] for the full direction (anesthesia wait time, non-operative time). |
| `EVENT_TIME` | timestamp | 0 nulls, 406,848 distinct. For the checkpoint event types specifically: the large majority of `LOG_ID`s have exactly one timestamp per checkpoint (clean for interval math), but a real minority have genuinely multiple *distinct* times — `Start Data Collection` 2.5% (967/38,335), `Emergence` 1.2% (419/35,199), `Stop Data Collection` 1.1% (438/38,322), `Intubation` 0.9% (273/30,000) — plausibly real re-events (failed intubation attempt, case pause/restart) rather than errors. Any duration feature needs an explicit first/last-occurrence rule for these. ⚠️ **`Anesthesia Stop` can genuinely occur after `Transported to PACU/ICU`** — confirmed by user, not a data error: of 36,967 encounters with both events, 85.9% have them at the exact same timestamp, 12.6% have `Anesthesia Stop` *after* PACU transport (commonly by tens of minutes to a few hours — anesthesiologist still documenting/monitoring in PACU before formal sign-off), and only 1.6% have the "expected" strict before-order. Two extreme outliers (~7.00 days after PACU, almost exactly 10,080/10,074 minutes) are likely a **system auto-close on undocumented records** — confirmed by user as a known (and considered weak) EHR governance pattern: an anesthesia record left uncharted gets administratively closed after a fixed timeout (here, ~7 days) rather than reviewed and completed the next day, which is how another system the user worked with handled it. |
| `NOTE_TEXT` | string, free text, **undocumented by MOVER** | **640,013 nulls (99.97%)** — confirmed this is a genuine source-data characteristic, not an ingestion failure: raw CSV has exactly 210 non-empty values, matching bronze's 210 non-null rows exactly. 79 distinct non-null values — largely boilerplate/checklist text (e.g. Aldrete-score-style scoring criteria: `"alert and oriented (or baseline) [2 points]  SpO2 > 92% on room air [2 points]"`), not rich narrative free text. |

⚠️ **Data quality: exact-duplicate rows found, but with a nuance size-2 pairs might be
legitimate.** 31,534 groups (67,393 of 640,223 rows, 10.5%) share identical `LOG_ID` +
`MRN` + `EVENT_DISPLAY_NAME` + `EVENT_TIME` + `NOTE_TEXT`. Group-size distribution splits
into two distinct phenomena:
- **93.8% of duplicate groups (29,588) are exact size-2 pairs.** The top contributor,
  `"Two Anti-Emetics Administered"` (18,730 of these pairs), might not be a bug at all —
  the event name itself implies exactly 2 drugs given, so a 1-row-per-drug charting
  convention is plausible. Not yet resolved whether all size-2 duplicates should be treated
  as data quality issues or left alone — open item.
- **A small number of extreme outliers, all `"Mark Now"`** (a generic anesthesia-record
  timestamp-marker event): 8 groups with 10+ copies, up to **345 identical rows** for one
  single `LOG_ID`/timestamp. This does look like a genuine charting glitch (repeated
  clicks/stuck event) rather than a legitimate convention.

### patient_coding.csv — billing codes — 2,033,948 rows

6 columns, all documented by MOVER (docs page is at `patient-coding.html`, not the
`patient-coding-table.html` pattern most other tables use). CSV header matches bronze
schema exactly.

| Column | Type decision | Notes |
|---|---|---|
| `MRN` | string | 0 nulls, 42,526 distinct |
| `SOURCE_KEY` | int/categorical | 0 nulls, 7 distinct values. Docs: "represents the reference billing code set being used." |
| `SOURCE_NAME` | categorical | 0 nulls, 8 distinct values (see truncation note below — 8 not 7 because of a corrupted row). Real code sets found: `Final Diagnosis Primary Code Set`, `External Cause of Injury Primary Code Set` (both ICD-10-CM), `ICD Procedure Primary Code Set` (ICD-10-PCS), and 4 CPT variants — `Charge CPT Code`, `Inpatient CPT Code`, `Combined CPT Code`, `Coding CPT Code`. Notably **no ICD-9** here (unlike `patient_history`/`patient_visit`) — consistent with this being later-stage billing coding, done post-2015-cutover, vs. `patient_history`'s legacy problem-list entries. |
| `REF_BILL_CODE_SET_NAME` | categorical | **622,985 nulls (30.6%)** — but not random: populated only for the two ICD code sets (`ICD-10-CM`, `ICD-10-PCS`); null for every CPT-flavored `SOURCE_NAME` row. Docs describe it as the set's "abbreviation," but that abbreviation is only actually captured for ICD rows — CPT rows rely on `SOURCE_NAME` text alone to identify the code system. |
| `NAME` | string, free text | 179 nulls, 34,350 distinct. "Name of what the patient is being billed for" (procedure/diagnosis description). |
| `REF_BILL_CODE` | string | 1 null, 29,325 distinct. Format checked against `SOURCE_NAME`: ICD-10-CM and ICD-10-PCS rows are correctly shaped (e.g. `D75.839`, `02HV33Z`). ⚠️ The 4 "CPT" `SOURCE_NAME`s are actually a **mixed bag**, not pure CPT — true 5-digit-numeric CPT (`27130`), Category III CPT (`0184T`), and HCPCS Level II codes (`J0171`, `C1887`, `A6254`) all share the same "CPT Code" buckets; only 31.4% of `Charge CPT Code` rows (169,765/540,336) are actually 5-digit numeric. Don't assume `SOURCE_NAME` containing "CPT" means the code itself is CPT. Also found 2 rows with a lowercase code (`g0480`, should be `G0480`) — negligible, noted for completeness. |

⚠️ **Data quality: 1 fully-corrupted row** (`MRN 7ace7e3e41b4f9c0`, `SOURCE_KEY=3`,
`SOURCE_NAME='Final Di'`, with `NAME`/`REF_BILL_CODE_SET_NAME`/`REF_BILL_CODE` all null) —
`SOURCE_NAME` is truncated mid-word (should be `"Final Diagnosis Primary Code Set"`, the
other 989,632 `SOURCE_KEY=3` rows). Shape suggests a CSV parsing artifact (a record split
mid-row upstream), not a wording/dictionary variant like earlier findings. Accounts for 1
of `NAME`'s 179 nulls and the sole `REF_BILL_CODE` null. Negligible at 1-in-2M rows.

### flowsheets_cleaned/ — patient measurements (vitals), the 10th EPIC table

19 parts, **1,440,918,933 rows total** (~1.44 billion). (An earlier draft of this doc
said 1,440,918,952 — that count included the header row in each file's `wc -l`, an
off-by-one repeated across all 19 parts. The figure below is the corrected, verified
count — confirmed three independent ways during ingestion: live per-chunk accounting,
a raw Python `csv` module count, and a post-ingestion DuckDB query. No data was lost;
this was a documentation bug in the original audit script, not an ingestion defect.)
Confirmed via official docs:
`FLO_NAME` = category of measurement, `FLO_DISPLAY_NAME` = measurement name, `UNITS` =
unit for the value, `RECORD_TYPE` = **PRE-OP / INTRA-OP / POST-OP** (corrected from an
earlier "peri-op" — the real value is literally `INTRA-OP`; per user, on a clinical
flowsheet, perioperative staff document body systems across these three windows — pre,
intra, post op), `RECORDED_TIME` = exact timestamp (docs call this column `RECORD_TIME`,
CSV header says `RECORDED_TIME` — same field). `MRN`/`LOG_ID` defined same as elsewhere.

**Full column audit completed 2026-08-21** (all 9 real columns, ~1.44B rows — every
aggregation below ran directly against Iceberg/Parquet via DuckDB, no pandas load,
typically sub-3-second even at this scale thanks to column pruning):

| Column | Type decision | Notes |
|---|---|---|
| `LOG_ID` / `MRN` | string (join keys) | 0 nulls on both. |
| `FLO_NAME` | categorical | 148 distinct. ⚠️ **Same trailing-whitespace duplicate pattern found elsewhere in this dataset, but at by far the largest scale**: `"Vital Signs "` (trailing space, 399,407,875 rows — **27.7% of the entire table**) vs. `"Vital Signs"` (15,662,822 rows); same for `"CCP Vital Signs "` vs `"CCP Vital Signs"`. Added to the silver-layer TODO as a standard trim-all-categoricals step, not a one-off fix. |
| `FLO_DISPLAY_NAME` | categorical | 121 distinct. Many-to-many with `FLO_NAME` **by design, not a data quality issue** — e.g. `Resp`/`Pulse`/`SpO2`/`Temp` each appear under 40–63 different `FLO_NAME` categories, because different care-unit flowsheet templates (Vital Signs, CCP Vital Signs, ED Vitals, Dialysis Vitals, General Respiratory, etc.) reuse the same underlying measurement names. `FLO_NAME` = which template, `FLO_DISPLAY_NAME` = which measurement. |
| `RECORD_TYPE` | categorical | **177,471,630 nulls (12.3%)** — not previously documented (earlier notes only covered the 3 real values). Null rate is fairly even across the top flowsheet categories (8.4%–16.3%, no single type dominating) and spans the same full 2017–2023 date range as non-null rows — reads as scattered/normal missingness (charting outside the strict pre/intra/post-op window boundary), not a systematic gap. |
| `RECORDED_TIME` | timestamp | **1 null out of 1,440,918,933 rows** — essentially fully populated. |
| `MEAS_VALUE_NUM` / `MEAS_VALUE_TXT` | float / string | Confirmed cleanly mutually exclusive — **0 rows have both populated**. 278,722,196 rows (19.3%) have `MEAS_VALUE_NUM` null (i.e. text-only), 1,165,971,902 (80.9%) have `MEAS_VALUE_TXT` null (numeric-only), and **3,775,165 rows (0.26%) have neither** — investigated and this isn't a bug: the "no value" rows are concentrated in checkbox-style (`OR COMPLICATIONS` — blank plausibly means "nothing checked"), conditionally-populated (`Arterial Line BP/MAP`, only present when that line exists), and computed (`Custom Formula Data` min/max fields, blank when insufficient underlying readings exist) flowsheet rows — normal sparse EHR flowsheet-grid behavior, not missing data. `MEAS_VALUE_TXT` content confirmed rich and clinically real: cardiac rhythm (`NSR`, `ST`), probe/measurement site (`Left arm`, `Axillary`, `Bladder`), O2 device (`None (Room air)`, `ETT`, `NC`, `Trach mask`), pain scale (`CPOT`). |
| `UNITS` | categorical | 946,121,623 nulls (65.7% — expected, most qualitative flowsheet items have no unit). ⚠️ **Same casing/typo duplicate pattern as `FLO_NAME`, but for units**: `cmH20` (digit zero, 23,612,311 rows) vs. `cmH2O` (letter O, 23,580,046 rows) — a typo, not a different unit; `l/min` (45,585,284) vs. `L/min` (17,904,997) — case variant of the same unit; `ml` (20,113,755) vs. `mL` (28,146,660) — same. Also added to the silver trim/normalize TODO. Found one tiny (6-row) genuine **CSV-escaping bug**: an unescaped double-quote inside a free-text `MEAS_VALUE_TXT` note (`"...verbalized im okay" when asked..."`) broke CSV field parsing, spilling garbage (`,",",488"`) into `UNITS` for that record — a different corruption class than the whitespace/casing issues, negligible in volume. |

Dropped `Unnamed: 0` pandas index column confirmed harmless — verified directly against the
raw CSV, it's just a monotonic `0,1,2,...` row counter, no real data.

🚨 **Duplicate-row check completed (took ~267s, much heavier than every other query on
this table) with an extreme, unresolved result: 916,048,965 of 1,440,918,933 rows —
63.6% of the entire table — are involved in exact-duplicate groups (133,971,114 groups,
full 9-column match).** This is far higher than any other table checked this session
(`patient_lda` was the next-highest at 22.9%). **Not yet investigated at all** — this
number alone doesn't distinguish between two very different explanations: (a) a genuine
massive data/ingestion quality issue, or (b) a legitimate EHR "carry-forward" charting
behavior (the same value re-charted into interval placeholder cells so a flowsheet cell
never appears blank) that would look identical to duplication in a raw row count but
isn't actually redundant data. **Top priority item for next session** — needs the same
treatment `patient_lda`'s duplicates got (find what's actually shared across the
"duplicate" rows, check a few real examples, bring in the user's clinical read on
whether repeat-charting is a real workflow) before concluding anything about severity or
how to handle it in silver.

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

## OR-efficiency timing / event sequencing (patient_procedure_events)

Analysis run 2026-08-21 as groundwork for the planned OR-efficiency metrics direction (see
`mover_or_efficiency_metrics` project memory) — working out the standard sequence of
anesthesia/surgical timeline checkpoint events and which durations are computable from
them, prompted by a user-recalled OR-efficiency model (anesthesia wait time = time from
"room ready" to actual cutting, framed as unproductive/lost time).

**Empirical sequence and gap durations**, from first-occurrence timestamps per `LOG_ID`
(43,576 encounters have at least one of these checkpoints; the pairwise `n` below is
lower per step since not every encounter has every checkpoint charted):

| Step | n (both present) | Median gap | Mean gap |
|---|---|---|---|
| `Anesthesia Start` → `Start Data Collection` | 37,965 | 0 min | 0.8 min |
| `Start Data Collection` → `Sign In` | 37,729 | 6 min | 6.4 min |
| `Sign In` → `Induction` | 32,700 | 3 min | 4.9 min |
| `Induction` → `Intubation` | 29,629 | 3 min | 3.6 min |
| `Intubation` → `Anesthesia Ready` | 29,569 | 5 min | 9.4 min |
| **`Anesthesia Ready` → `Emergence`** | 34,702 | **130 min** | **164 min** |
| `Emergence` → `Extubation` | 29,299 | 5 min | 8.1 min |
| `Extubation` → `Stop Data Collection` | 29,452 | 5 min | 8.3 min |
| `Stop Data Collection` → `Anesthesia Stop` | 37,955 | 8 min | 10.1 min |

Note on `Anesthesia Ready`: average rank-order places it *after* `Induction` and
`Intubation` (not before, despite what the name might suggest in isolation) — confirmed
this means "patient is intubated/monitored and genuinely ready for the surgeon to start,"
not a pre-induction readiness check. That makes it the right conceptual anchor for an
anesthesia-wait-style metric.

**⚠️ Known gap: cannot split the `Anesthesia Ready → Emergence` window into "anesthesia
wait" vs. "actual surgical time."** The literal cut-time marker, `Skin Incision`, is
charted in only **51 of ~65,728 surgeries** — essentially unusable as a general-purpose
signal. `patient_information.IN_OR_DTTM`/`OUT_OR_DTTM`/`AN_START_DATETIME`/
`AN_STOP_DATETIME` are candidate proxies, not yet cross-validated against this table's
events — open item.

**Full-chain order validation** (all 10 checkpoints present, n=27,053 of 39,138
encounters with ≥1 checkpoint — 31% missing at least one entirely):
- Full sequence holds in **96.2%** of complete cases (26,014/27,053).
- `Anesthesia Ready → Emergence` (the surgery-spanning gap) is essentially bulletproof:
  only 4 violations.
- Violations concentrate elsewhere: `Stop Data Collection → Anesthesia Stop` (336,
  consistent with the `Anesthesia Stop`-lags-PACU pattern documented above),
  `Intubation → Anesthesia Ready` (188), `Emergence → Extubation` (148),
  **`Sign In → Induction` (134 in the complete-10 subset; 334 of 32,700 / 1.0% in the
  broader sign-in-and-induction-present population)**.

**`Sign In`/`Induction` order violation — investigated further per user request** (WHO
Surgical Safety Checklist mandates Sign In before induction, so this deviation is real,
not just an artifact of missing data):
- Declining over time: **1.8% (2018) → 1.05% (2019) → 1.12% (2020) → 0.79% (2021) →
  0.53% (2022)** — reads as genuine documentation-compliance improvement, not noise.
- Inpatient encounters violate at **1.21%** (243/20,009) vs. Outpatient at **0.42%**
  (43/10,275) — inpatients ~2.9x more likely. Plausible explanation: inpatient cases
  (already admitted, potentially more urgent/complex, transported from floor/ICU) may
  have a less standardized pre-op workflow than scheduled outpatient elective surgery.
  Note: this correlation surfaced a previously-unflagged data quality issue — see
  `patient_information` row-duplication note below.
- 97.2% of violations (278/286) are `General` anesthesia, but General is also the
  dominant type overall — flagged as directional only, not a confirmed differential
  signal without a proper base-rate comparison.
- Timestamps alone can't distinguish "Sign In physically happened first but was charted
  late" from "the safety checklist step was actually skipped/delayed" — open
  interpretation question, not resolvable from this data alone.

**Side finding — added to "Duplicate-row audit" below**: while joining violation
`LOG_ID`s back to `patient_information`, found that table has **65,728 total rows but
only 64,354 distinct `LOG_ID`** (1,374 excess rows) — some `LOG_ID`s have more than one
row. This wasn't flagged during `patient_information`'s own column audit (which noted the
distinct count but didn't investigate the gap) — needs a proper look at whether these are
exact duplicates or genuinely different rows sharing a `LOG_ID`.

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

## Duplicate-row audit (tracking)

Found during the `patient_lda` column audit: 22.9% of that table's rows (106,534 of
465,801) are exact duplicates of another row differing only in `Line_Group_Name` — same
physical device charted under two navigator categories at once (e.g. `Drain` +
`Urinary Drainage` for the same catheter). That's a large enough fraction to warrant a
systematic duplicate-row check across every bronze table before silver, not just the ones
already flagged.

| Table | Dedup status | Notes |
|---|---|---|
| `patient_information` | **Gap confirmed, externally corroborated, row content not yet inspected** | 65,728 total rows but only 64,354 distinct `LOG_ID` (1,374 excess rows). Found incidentally during the `patient_procedure_events` sequencing analysis (2026-08-21). **2026-08-23: externally confirmed 64,354 is the correct count** — the official MOVER paper's own stated surgery count for the EPIC dataset is 64,354, matching the distinct-`LOG_ID` count exactly, not the raw row count. Still not checked whether the 1,374 excess rows are exact duplicates or genuinely divergent content sharing a `LOG_ID` — see "Silver-layer design checklist" below. |
| `patient_history` | **Confirmed and investigated (2026-08-25) — real, mechanistic, dedup implemented** | 76% of rows (734,073/970,741) duplicated on `(mrn, diagnosis_code, dx_name)`. No encounter id in this table, so a chronic diagnosis re-exports once per clinical encounter — ratio scales ~1:1 with each patient's surgery count (1.09× to 52×), grep-confirmed real at the source. Silver collapses to 437,721 rows with an `n_occurrences` count — see "Silver-layer design checklist" below. |
| `patient_visit` | **Confirmed and investigated (2026-08-25) — real, dedup implemented** | 57% of rows (125,405/219,257) duplicated on `(LOG_ID, mrn, diagnosis_code, dx_name)`, but *within* one encounter, not across encounters like `patient_history` — likely one row per clinical note reiterating the diagnosis list. Grep-confirmed real at the source. Silver collapses to 131,455 rows with an `n_occurrences` count. |
| `patient_coding` | **Confirmed and investigated (2026-08-26) — real, dedup implemented** | 55% of rows (1,124,044/2,033,948) duplicated on `(MRN, SOURCE_KEY, SOURCE_NAME, NAME, REF_BILL_CODE_SET_NAME, REF_BILL_CODE)` — same no-encounter-id re-export-per-encounter mechanism as `patient_history`. Grep-confirmed real at the source (612× top group). Silver collapses to 1,244,633 rows with an `n_occurrences` count. Separately, 1 corrupted row found during the column audit (see above) — different issue, not part of this duplication. |
| `patient_medications` | **Confirmed and investigated (2026-08-27) — export artifact, dedup implemented** | Only 1.24% of rows (345,530/27,961,524) duplicated. Full investigation (see "MAR duplicate-row investigation" above) confirmed this is an export/ETL artifact — contiguous multi-day duplicate blocks, one-time actions repeating at the identical second, the pattern recurring identically across one patient's multiple encounters — not charting noise and not periodic infusion-continuation checks. Silver collapses to `n_occurrences` (27,773,144 rows), matching `patient_history`/`patient_visit`/`patient_coding`'s treatment; never multiply `ADMIN_SIG` by `n_occurrences` for a dose/fluid total. Separately still open: the `ORDER_STATUS_NM` `"0 "`-prefix string-variant issue. |
| `patient_post_op_complications` | **Confirmed and investigated (2026-08-27) — real, dedup implemented** | 79% of rows duplicated, 98% of that the generic `AN AQI POST-OP COMPLICATIONS` reporting flag (null value, up to 49×/encounter) — same re-emission-per-note mechanism as `patient_visit`. A small remainder of real complication values duplicates the same way, grep-confirmed real. Silver collapses to 84,776 rows with an `n_occurrences` count. |
| `patient_lda` | **Confirmed — 22.9% of rows duplicated** | See above; concentrated in 5 device-type pairs, `Drain` is the generic partner in 4 of 5 |
| `patient_procedure_events` | **Confirmed — 10.5% of rows duplicated, mixed severity** | 93.8% of duplicate groups are exact size-2 pairs (possibly legitimate one-row-per-drug charting, e.g. "Two Anti-Emetics Administered" — not yet resolved as bug vs. convention); a small number of extreme outliers ("Mark Now", up to 345 copies) look like a genuine charting glitch |
| `patient_labs` | **Confirmed — 0.38% of rows duplicated, source-level** | 109,995 rows (54,979 groups) — verified a real example is a literal duplicate line in the raw CSV, not an ingestion bug. Small fraction relative to `patient_lda`/`patient_procedure_events` but still real. |
| `flowsheets` | **Confirmed and investigated (2026-08-23) — mechanism still unconfirmed, dedup recommended regardless** | 63.6% of rows duplicated (916,048,965/1,440,918,933) — by far the highest rate in the warehouse. 133,971,114 distinct groups, median size 4, max 323 (a single static field re-emitted 323 times within one encounter — not plausibly a coincidence). Concentrated in `PRE-OP`/`POST-OP` core vitals (`Pulse`, `SpO2`, `Resp`, `BP`, `MAP`, etc.); `INTRA-OP` barely affected. Since `RECORDED_TIME` is part of the exact-match tuple, this is not "same value re-charted over time" (carry-forward) — it's the literal same row appearing multiple times. Two candidate mechanisms, neither confirmed: an export process re-emitting the full unchanged flowsheet state on every subsequent save event, or device-integrated vitals (bedside monitor → Epic) getting duplicated on retry without a dedup key. No public MOVER documentation of this found despite searching. See "Silver-layer design checklist" below for the recommended fix. |

## MRN corruption and patient-level linkage

**Confirmed 2026-08-23.** `patient_information.MRN` splits into two populations:

| Format | Rows | Distinct | Length |
|---|---|---|---|
| 16-char lowercase hex (the de-identified surrogate) | 65,690 | 39,659 | 16 |
| Scientific notation (corrupted) | 38 | 26 | 8–11 |

39,659 + 26 = **39,685**, exactly the patient count reported in the MOVER paper — so the
corrupted values are real patients, each absent from the clean population, not strays.

**Mechanism (confirmed, not hypothesis).** Every one of the 39,659 clean MRNs contains at
least one letter `a`–`f`; **zero** all-digit hex16 MRNs survive, against ~21.5 expected by
chance ((10/16)^16 × 39,659). A CSV/spreadsheet step upstream coerced to a number exactly
those hashes that happened to contain no letters, and nothing else. Two variants, both
lossy and irreversible:

- **23 values** — the 16-digit string read as a decimal number, so ≤10^16 → renders `E+14`/`E+15`.
- **3 values** — the hex parsed as hex, giving its true 64-bit value → `E+18`/`E+19`
  (`1.45E+19` falls between 2^63 and 2^64, as it must).

**This is not a de-identification leak.** No raw MRN escaped; these are mangled surrogate
hashes. It is also not an artifact of this pipeline — bronze types `MRN` as `string`, and
the values are already corrupted in the source CSV. Not documented anywhere in the MOVER
paper or the known community repos; worth reporting upstream.

**Why the corruption is worse than missing data: it is ambiguous, not blank.** Where
truncation happened to be consistent it still groups correctly (`9.04E+18` correctly
gathers one patient's three June–July 2022 cardiac encounters — left heart cath through
CABG). Where it was not, one patient splits in two: `LOG_ID 250823bea3d3396f` carries both
`2.75E+15` and `2.75162E+15`. And nothing prevents two genuinely different patients from
truncating to the same string and being **silently merged**. A false merge produces no
error and no null — it just quietly fabricates a patient.

**What still works.** `LOG_ID` on all 37 affected encounters is intact hex16 and joins
normally:

| Table | Rows | Encounters matched |
|---|---|---|
| `patient_medications` | 6,750 | 36/37 |
| `patient_labs` | 9,776 | 35/37 |
| `patient_post_op_complications` | 84 | 34/37 |
| `patient_lda` | 167 | 31/37 |
| `patient_procedure_events` | 195 | 17/37 |
| `patient_coding` | — | **no `LOG_ID` column — MRN-keyed only, so unreachable** |

**What patient-level linkage buys, and therefore what is lost.** `MRN` is what makes
several `LOG_ID`s one person. Among clean MRNs: 39,659 patients hold 64,320 surgeries
(**1.62 each**); **30% of patients have more than one surgery**, and **36,685 surgeries —
57% of the dataset — belong to repeat patients** (the maximum is 41). Depending on it:

1. **Grouped train/test splitting.** Splitting by encounter puts the same patient on both
   sides, so a model can memorise the person rather than the physiology and metrics come
   back optimistic. At 57% of surgeries this is a severe leak, not a rounding error —
   splits must be grouped by `MRN`. (Gold-layer concern, but it originates here.)
2. **Longitudinal features** — prior-surgery count, prior complications, lab trajectories
   from earlier admissions.
3. **Repeat-event outcomes** — reoperation, readmission, anything defined as "again".
4. **`patient_coding`** — MRN-keyed, so comorbidity/billing codes are unreachable for
   these 26 patients.
5. **Per-patient denominators** — any rate expressed per person rather than per case.

**Confirmed 2026-08-23 — full-warehouse scan complete.** The same corruption reappears,
independently, in five of the ten tables (not shared from one broken master file — each
table's export pipeline mangled its own copy):

| Table | Corrupted? |
|---|---|
| `patient_coding` | ✅ 386 rows / 14 distinct |
| `patient_information` | ✅ 38 rows / 26 distinct |
| `patient_lda` | ✅ 172 rows / 24 distinct |
| `patient_post_op_complications` | ✅ 86 rows / 23 distinct |
| `patient_procedure_events` | ✅ 209 rows / 15 distinct |
| `patient_history`, `patient_labs`, `patient_medications`, `patient_visit`, `flowsheets` | clean |

Bucketing the corrupted values by magnitude (truncation precision varies table to table,
so exact string matching undercounts) confirms it's the same ~25 patients recurring
across all five affected tables, not five independent populations. `patient_history` has
no `LOG_ID` column at all (MRN-only); `patient_visit` does carry `LOG_ID` and is clean.

**Recoverable for most of these patients.** `patient_history` stores 22 of the affected
MRNs as correct, full-precision 16-digit strings (never coerced to scientific notation —
this table just wasn't vulnerable to the bug) that decode exactly to the same corrupted
buckets found elsewhere. So a silver-layer join through `patient_history` (`MRN` →
correct 16-hex string) can repair `patient_information` / `patient_lda` / `patient_coding`
/ `patient_post_op_complications` / `patient_procedure_events` for 22 of the ~25 patients,
restoring true patient-level linkage instead of only flagging it broken. **Not yet
implemented** — `build_silver.py` still only flags `mrn_corrupt`, it doesn't repair via
this recovery path. The remaining ~3 patients (the `E+18`/`E+19` cases, which are
letter-containing hex parsed as hex rather than all-digit-as-decimal — see mechanism
above) aren't recoverable this way and need a separate check.

## LOG_ID collisions across different patients

**Confirmed 2026-08-23.** Of `patient_information`'s 7 truly-divergent duplicate
`LOG_ID`s (see the dedup rule below), 3 shared a `LOG_ID` across two rows with different
`MRN`. Initial read of `patient_information` alone made this look like it could be a
random 64-bit hash collision — implausible at ~65K rows, but not ruled out by that table
alone. **Cross-checking against every other table's real clinical data settles it, and
the three cases split into two entirely different phenomena:**

**`ebfbb4fcfd39fdff` and `c468eb4fb4f54ddb` — genuine collisions between two real,
distinct patients.** Both `LOG_ID`s carry substantial, independently-verifiable clinical
data for **both** `MRN`s, a year apart:

| LOG_ID | MRN | flowsheet rows | date range |
|---|---|---|---|
| `ebfbb4fcfd39fdff` | `3c573ad3…` | 10,841 | 2020-06-11 → 06-22 |
| `ebfbb4fcfd39fdff` | `d592dd4d…` | 3,876 | 2021-03-20 only |
| `c468eb4f…` | `3c573ad3…` | 10,841 | 2020-06-11 → 06-22 |
| `c468eb4f…` | `d592dd4d…` | 3,876 | 2021-03-20 only |

Both `LOG_ID` values carry real bedside-vitals charting for both `MRN`s, a year apart —
this is two humans' actual data, not a merge artifact on one chart. It also explains the
row-content pattern cleanly: each of these two real patients had an initial op and a
reop 3 days later within one admission (`ebfbb4fcfd39fdff`=initial, `c468eb4f…`=reop for
*both* patients) — vitals were charted continuously through the stay, so the same
admission's flowsheet rows legitimately attach to both of that patient's `LOG_ID`s. Not a
random 64-bit collision — the same two patients collide at *matched sequence positions*
(both patients' 1st op share one `LOG_ID`, both patients' reop share a different one),
which only makes sense if `LOG_ID` isn't purely random. Working theory, not confirmed:
MOVER's de-identification pipeline derives `LOG_ID` at least partly from something like
"clinical-similarity cohort + encounter-sequence position," so two patients in the same
cohort at the same position in their own encounter sequence can land on the same value.
This is a real defect in MOVER's `LOG_ID` generation, worth reporting upstream — not
sampling noise, and not something this pipeline can fix at the source.

**`0c6b137659f5df02` — not a collision. A partial-duplication artifact.** The second
`MRN` (`fc63c830038a1f83`) has **zero rows in every encounter-level clinical table in the
warehouse** — no labs, no meds, no flowsheets, no procedure events, no complications, no
LDA, no coding. Every real clinical trace for this `LOG_ID` belongs to the first `MRN`
(`18d889ebcda81db9`) alone. Digging into what *does* exist for the phantom `MRN`:

- `patient_visit`: both `MRN`s show the exact same single row on this `LOG_ID` —
  byte-for-byte identical `dx_name` ("Acute infective endocarditis...").
- `patient_history`: the phantom `MRN` has 4 diagnoses (`255.9 Adrenal mass`,
  endocarditis ×2, `289.59 Splenic abscess`). The real `MRN` has those exact same 4,
  **plus a 5th** (`786.50 Chest pain`) the phantom is missing.

So the phantom `MRN` isn't an independent patient — it's a **strict subset copy** of the
real patient's diagnosis history, missing one row, attached to a fabricated `MRN` and a
surgery date shifted exactly two months later (`2020-11-09` vs the real `2020-09-06`),
with zero corroborating activity anywhere else. Categorically different from the two
genuine collisions above, which both had substantial independent clinical footprints.

**Fix — proposed, not yet implemented in `build_silver.py`.** Reclassify
`0c6b137659f5df02`: drop the `fc63c830038a1f83` row (no clinical substance), keep only
`18d889ebcda81db9` (real data). That would leave **2 true collision `LOG_ID`s (4 rows —
`ebfbb4fcfd39fdff`/`c468eb4fb4f54ddb`)** instead of 3 (6 rows), and total silver row
count would drop by 1 (64,356 instead of 64,357; distinct `LOG_ID` stays 64,354 either
way). `log_id_collision` count would drop from 6 to 4. Not applied yet — this is a report
of the finding and the recommended fix, not a code change; `build_silver.py` still
treats all 3 as collisions and keeps all 6 rows as of this writing.

## MAR duplicate-row investigation (patient_medications)

**Confirmed 2026-08-27.** `patient_medications` has 345,530 duplicated rows (1.24% of
27,961,524) — small next to `patient_history`/`patient_coding`'s 55-76%, since this
table already has an encounter id (`LOG_ID`). Initial framing (2026-08-26) called this
"MAR charting noise" and collapsed it via `SELECT DISTINCT`. That was wrong on two
counts, both caught before merging:

1. **It silently deleted dose-bearing rows.** `ADMIN_SIG` (dose) is populated on 95.7%
   of `Given` rows, 60% of `Rate Verify`, 69% of `New Bag`, 93% of `Rate Change` — these
   aren't logistics-only actions. 11,509 duplicate groups where `MAR_ACTION_NM='Given'`
   and `ADMIN_SIG` was non-null lost 11,783 rows to the `SELECT DISTINCT` collapse.
2. **The theory itself was untested.** "Charting noise" was never verified against real
   timelines before being used to justify deletion.

**A competing theory was raised and tested: could duplicates represent a fluid infusion
correctly continuing at that minute** (e.g. a periodic "still running, still at rate X"
check), rather than an artifact? Tested directly by pulling full real timelines:

- **Ruled out by same-second repeats of one-time actions.** One heparin infusion
  (`LOG_ID ee04fd363d4c4fbf`) shows a clean ~12-hour window (2020-07-13 07:15–19:00)
  where *every* action is doubled — including `Rate Change Dual Sign` and `New Bag`,
  which are one-time events, not recurring checks. A second case (`LOG_ID
  8172c57dc6cc59fd`, sodium chloride infusion) shows `MAR Hold` and `MAR Unhold` each
  repeated 5× at the **exact same second** (`14:31:24` and `17:02:48`). A Hold/Unhold is
  a discrete state change — it cannot be 5 genuine separate real-world events at one
  instant. This rules out the infusion-continuing theory as the general explanation (it
  remains plausible for `Rate Verify` in isolation, just not for the pattern as a whole).
- **What the pattern actually is: contiguous multi-day blocks, not scattered
  coincidence.** For both timelines, duplication doesn't scatter randomly across the
  encounter — it runs in long clean blocks (multiple days at 2-3× duplication), with
  sharp transitions to blocks of clean singleton rows. That shape is inconsistent with
  bedside charting behavior and consistent with an **export/ETL artifact**: an
  overlapping date-range extraction window in MOVER's own per-encounter export, similar
  in spirit to the confirmed `flowsheets` re-emission mechanism (see "Duplicate-row
  audit" above).
- **Corroborating context for the second case:** the block (2022-11-10 to -24) recurs
  **identically across 5 different `LOG_ID`s, all the same `MRN`** — one patient's single
  continuous ICU admission spanning 5 procedures 13 days apart (CABG → tracheostomy →
  laparoscopy → ECMO line removal → laparotomy). The sodium chloride infusion ran
  continuously through the whole stay; its MAR history got attributed to (and
  independently duplicated within) multiple of that admission's `LOG_ID`s — the same
  "no clean encounter-scoping for multi-surgery admissions" mechanism already found in
  `patient_history`/`patient_visit`/`patient_coding`, compounded by the block-duplication
  artifact on top.

**Conclusion:** this is a confirmed export artifact, not real repeat clinical events —
safe to collapse. **Fix (revised 2026-08-27):** collapse to `n_occurrences`, same
pattern as `patient_history`/`patient_visit`/`patient_coding`, instead of the
row-deleting `SELECT DISTINCT` used the day before. `n_occurrences` must never be
multiplied against `ADMIN_SIG` to compute a dose/fluid total — that conflates export
duplication with real repeat administration; totals are a deliberate gold-layer decision.

## Silver-layer design checklist

Organized by table, each item tagged with which data-quality dimension it belongs to
(see taxonomy: dedup, missingness, standardization, validity, accuracy, sentinel,
semantic, casing/join). Items marked **[open]** need further investigation before the
fix can be finalized — don't implement those blind. Everything here is a silver-layer
(apply-once, universal) concern; gold-layer concerns (temporal leakage windowing,
per-model imputation/encoding choices, cohort scoping for a specific question) are
deliberately out of scope for this checklist — those get designed per model, not here.

**Cross-table / global rules (apply to every table):**
- [ ] *[casing]* Normalize `LOG_ID`/`MRN` casing everywhere — `mrn` is lowercase in
      `patient_history`/`patient_visit`, `MRN` uppercase elsewhere. Silent join failures
      otherwise.
- [ ] *[standardization]* One universal trim-and-normalize pass over every categorical
      string column (leading/trailing whitespace + casing variants) — this exact pattern
      recurred independently in `patient_medications.PRIMARY_ANES_TYPE_NM` ("MAC"),
      `flowsheets.FLO_NAME`/`UNITS` (at massive scale), `patient_medications
      .ORDER_STATUS_NM` ("0 " prefix). Handle once as a standard step, not per-column.

**`patient_information`:** — **implemented in `scripts/build_silver.py::build_patient_information`, 2026-08-23.**
- [x] *[dedup]* 1,374 excess rows on `LOG_ID` resolved. Row content was inspected: 1,364
      were exact duplicates (trivial `SELECT DISTINCT`) plus 4 whitespace/wording-only
      variants (collapse the same way after normalizing). Of the remaining 7 genuinely
      divergent `LOG_ID`s: **3 are real collisions** between different patients (`MRN`
      and every other column differ) — both rows kept, flagged `log_id_collision`,
      **`LOG_ID` is not a unique key for these 6 rows**; **1 is an `MRN`-corruption
      artifact** (`MRN` differs but everything else is identical, and both `MRN` values
      are the scientific-notation kind) — collapsed to 1 row, flagged `mrn_corrupt`;
      **3 are genuine same-encounter conflicting values** (e.g. `DISCH_DISP_C` 15 vs 20)
      — tie-broken to 1 row via a fixed, reproducible `ORDER BY` over every column,
      flagged `has_conflicting_duplicate`. Verified in production output: 64,357 rows /
      64,354 distinct `LOG_ID` (matches the paper exactly), `mrn_corrupt`=37,
      `log_id_collision`=6, `has_conflicting_duplicate`=3.
  - [ ] *[correction, open]* Of the 3 `log_id_collision` cases, only 2
        (`ebfbb4fcfd39fdff`/`c468eb4fb4f54ddb`) are genuine cross-patient collisions —
        confirmed via independent clinical data (real flowsheets for both `MRN`s). The
        3rd, `0c6b137659f5df02`, is a partial-duplication artifact, not a real second
        patient — its second `MRN` has zero footprint in any encounter-level table. See
        "LOG_ID collisions across different patients" above for the full investigation.
        **Fix not yet applied**: `build_silver.py` should drop `0c6b137659f5df02`'s
        `fc63c830038a1f83` row and keep only `18d889ebcda81db9`, dropping
        `log_id_collision` from 6 rows to 4 and total silver rows from 64,357 to 64,356.
- [x] *[accuracy/join]* `mrn_corrupt` boolean flag added, not repaired. 37 rows flagged
      (38 minus the 1 collapsed by the dedup rule above). Any patient-level grouping/join
      downstream must filter or otherwise account for these rows — see "MRN corruption
      and patient-level linkage" above.
- [x] *[validity]* `BIRTH_DATE` → `age_years` (int), with `age_capped` boolean
      (`age_years = 90`) — 642 rows in the final 64,357, down slightly from bronze's 668
      because some capped-age rows were among the collapsed exact duplicates.
- [x] *[validity]* `HEIGHT` parsed to `height_in` (float, total inches) via
      `F' I(.I)?` regex. `LOG_ID bd14c293acb63fcc` (8'2.3", physically implausible) is
      explicitly nulled; `LOG_ID 8944ca07ff7952b2` (7'7"/91in) parses through unchanged
      and is kept — verified as the table's actual max after the build.
- [x] *[validity]* `WEIGHT` → `weight_kg` (`WEIGHT * 0.0283495`). Post-build range
      23.0–296.3 kg, avg 79.1 kg — plausible for a mixed adult/peds surgical population.
- [x] *[standardization]* `PRIMARY_ANES_TYPE_NM` (and every other string column) trimmed.
- [ ] *[semantic]* Document (don't alter) that `PATIENT_CLASS_NM`'s two Inpatient
      subtypes' labels read backwards from their real clinical meaning — flag clearly
      for anyone using this field, since the name alone actively misleads. Column is
      carried through to silver unchanged; the doc note itself is still owed.
- [ ] *[missingness]* `ASA_RATING_C` (10.6% null) and `HOSP_DISCH_TIME` (14 nulls) are
      both informative/structural missingness, not random — do not blind-impute. No
      code change needed here, just a standing rule for gold-layer feature building.

**`patient_history`:** — **dedup implemented in `scripts/build_silver.py::build_patient_history`, 2026-08-25.**
- [x] *[dedup]* 76% of bronze rows (734,073 of 970,741) were "duplicates" of
      `(mrn, diagnosis_code, dx_name)`. **Investigated, not a data-quality bug —
      confirmed real and mechanistic.** This table carries no encounter id, so a
      chronic problem-list diagnosis gets re-exported once per clinical encounter that
      patient had; duplication ratio scales ~1:1 with each patient's surgery count in
      `patient_information` (1.09× for a 1-surgery patient, up to 52× for the one
      41-surgery patient — the single largest group, `mrn 1bb09d5761661c7d` /
      `Cervical cancer, FIGO stage IIB (CMS-HCC)`, appears exactly 104 times).
      Grep-verified a real example (104 literal matching lines) against the raw source
      CSV before trusting the finding — confirmed present at the source, not an
      ingestion artifact. Collapsed to one row per `(mrn, diagnosis_code, dx_name)`,
      with the repeat count kept explicitly as `n_occurrences` rather than left as an
      implicit, easy-to-lose row count. Verified in production output: 437,721 rows
      (from 970,741 bronze), `sum(n_occurrences)` matches the bronze row count exactly,
      known 104× group collapses correctly.
- [ ] *[sentinel]* `IMO0001` (Epic "No-Map" placeholder) is not a real diagnosis code —
      treat as null-equivalent, not a valid code. Not yet applied to silver output.
- [ ] *[missingness]* `diagnosis_code` ~25-30% null while `dx_name` is never null — leave
      as-is, this is a real billing-timing gap, not a defect.

**`patient_visit`** (shares the same `diagnosis_code`/`dx_name` shape as `patient_history`,
but is encounter-level via `LOG_ID`) — **dedup implemented in
`scripts/build_silver.py::build_patient_visit`, 2026-08-25.**
- [x] *[dedup]* 57% of bronze rows (125,405 of 219,257) duplicated on
      `(LOG_ID, mrn, diagnosis_code, dx_name)`. **Different mechanism than
      `patient_history`** — this repeats *within one encounter* rather than across
      encounters (the largest group: one `LOG_ID`, `Hemorrhagic shock (CMS-HCC)`,
      54 rows), likely one row per clinical note/document that reiterates the visit's
      diagnosis list — not resolvable further since bronze carries no note id.
      Grep-verified (54 literal matching lines) against the raw source CSV — real, not
      an ingestion artifact. Same treatment as `patient_history`: collapsed to one row
      per `(LOG_ID, mrn, diagnosis_code, dx_name)`, repeat count kept as
      `n_occurrences`. Verified in production output: 131,455 rows (from 219,257
      bronze), `sum(n_occurrences)` matches the bronze row count exactly, known 54×
      group collapses correctly.
- [ ] *[sentinel]* `IMO0001` (Epic "No-Map" placeholder) is not a real diagnosis code —
      treat as null-equivalent, not a valid code. Not yet applied to silver output.
- [ ] *[missingness]* `diagnosis_code` ~25-30% null while `dx_name` is never null — leave
      as-is, this is a real billing-timing gap, not a defect.
- [ ] *[join scope]* 12.1% of `LOG_ID`s don't join back to `patient_information` (broader
      source population). Document the join caveat explicitly rather than force-joining
      or silently dropping orphans.

**`patient_coding`:** — **dedup implemented in `scripts/build_silver.py::build_patient_coding`, 2026-08-26.**
- [x] *[dedup]* Same mechanism as `patient_history`: 55% of bronze rows (1,124,044 of
      2,033,948) duplicated on `(MRN, SOURCE_KEY, SOURCE_NAME, NAME,
      REF_BILL_CODE_SET_NAME, REF_BILL_CODE)`. No encounter id in this table either, so a
      billing code re-exports once per clinical encounter that patient had — the top
      group (612 occurrences of ICD-10-PCS `0HDAXZZ` for one 34-surgery patient)
      grep-confirmed as 612 literal matching lines in the raw source CSV before
      trusting it. Note `SOURCE_KEY` is a 7-value code-set-type lookup id, not a row
      id — don't mistake it for a unique key. Collapsed to one row per group, repeat
      count kept as `n_occurrences`. Verified in production output: 1,244,633 rows
      (from 2,033,948 bronze), `sum(n_occurrences)` matches the bronze row count
      exactly, known 612× group collapses correctly.
- [ ] *[accuracy]* Drop the 1 fully-corrupted row (`SOURCE_NAME='Final Di'`, truncated
      mid-word, every other column null). Not yet applied to silver output.
- [ ] *[semantic]* Don't trust `SOURCE_NAME`'s code-system claim — "CPT"-labeled rows are
      actually a mix of true CPT, Category III CPT, and HCPCS Level II. If a clean code
      system is needed downstream, derive it from the actual code format, not the label.

**`patient_medications`:** — **dedup implemented in `scripts/build_silver.py::build_patient_medications`, 2026-08-26, revised 2026-08-27 after investigation (see "MAR duplicate-row investigation" below).**
- [x] *[dedup]* 1.24% of bronze rows (345,530 of 27,961,524) were duplicated — a much
      smaller scale than `patient_history`/`patient_coding` (55-76%), since this table
      already has an encounter id. **Confirmed via full investigation to be an export
      artifact, not real clinical events** — see the dedicated writeup below for the
      evidence (block-clustered duplication, same-second repeats of one-time actions,
      the pattern recurring identically across one patient's multiple encounters).
      Collapsed to one row per exact-duplicate group, repeat count kept as
      `n_occurrences` (same pattern as `patient_history`/`patient_visit`/
      `patient_coding`) rather than deleted — an earlier version of this fix used plain
      `SELECT DISTINCT`, which silently discarded 11,783 dose-bearing `Given` rows;
      caught before merging, see the writeup. **`n_occurrences` must never be multiplied
      against `ADMIN_SIG` (dose) to compute a total** — it reflects export duplication,
      not repeat administration. Verified in production output: 27,773,144 rows (from
      27,961,524 bronze), `sum(n_occurrences)` matches bronze exactly, the known 15×
      group shows `n_occurrences=15`.
- [ ] *[standardization]* Trim the `ORDER_STATUS_NM` `"0 "`-prefix variant (9,377 rows).
      Not yet applied to silver output.
- [ ] *[semantic, open]* Classify every `MAR_ACTION_NM` value (Given/Hold/Rate
      Verify/Rate Change/New Bag/etc.) into "counts as real drug exposure" vs.
      "logistics event" — open since 2026-08-20, blocks any dose/exposure feature.
- [ ] *[enrichment, not urgent]* External vocabulary mapping (`MEDICATION_ID`→RxNorm/ATC,
      `ENC_TYPE_C`→Epic foundation categories) — flagged, not started.

**`patient_post_op_complications`:** — **dedup implemented in `scripts/build_silver.py::build_patient_post_op_complications`, 2026-08-27.**
- [x] *[dedup]* 79% of bronze rows (161,000-ish of 203,945) duplicated. 98% of that is
      the generic `AN AQI POST-OP COMPLICATIONS` reporting flag (`SMRTDTA_ELEM_VALUE`
      always null, up to 49× per encounter) — same re-emission-per-note mechanism
      confirmed for `patient_visit`, despite this table having `LOG_ID`
      (`CONTEXT_NAME='NOTE'` is one of three contexts, consistent with "one row per
      document/note referencing this element"). A small remainder (21 groups, 42 rows)
      of real, non-null complications — `Unplanned Postoperative Ventillation`,
      `Respiratory Failure`, `Hypotension (SBP<80 for 10 min)` — duplicate the same way;
      grep-confirmed real at the source. Unlike `patient_medications` there's no
      dose/quantity column at risk — a complication's presence doesn't need multiplying
      into a total, so this carried no equivalent silent-data-loss risk. Collapsed to one
      row per `(LOG_ID, MRN, Element_Name, CONTEXT_NAME, Element_abbr,
      SMRTDTA_ELEM_VALUE)`, repeat count kept as `n_occurrences`. Verified in production
      output: 84,776 rows (from 203,945 bronze), `sum(n_occurrences)` matches bronze
      exactly, the real "Unplanned Postoperative Ventillation" duplicate shows
      `n_occurrences=2`.
- [ ] *[semantic]* When using as ML labels, filter out the generic
      `AN AQI POST-OP COMPLICATIONS` reporting flag (200,139 rows) — it's not a
      complication class, it dwarfs every real label and will corrupt a naive label
      distribution. Not yet applied to silver output (still present, now collapsed to
      33,934+5,233+2,637 ≈ 41,804 distinct rows via `n_occurrences` rather than 200,139).
- [ ] *[scope]* Prefer `CONTEXT_NAME='ENCOUNTER'` rows (97.8% of real specific-class
      labels) — `ORDER`/`NOTE` are mostly just the generic flag.

**`patient_lda`:** — **implemented in `scripts/build_silver.py::build_patient_lda`, 2026-08-24.**
- [x] *[dedup]* Collapsed the cross-navigator duplication: grouped on every column except
      `Line_Group_Name` (after trimming), one canonical row per real device event.
      Renamed `Line_Group_Name` → `line_group_names` (`list<string>`), holding every
      navigator category the device was filed under instead of duplicating the row; added
      `multi_navigator` boolean flag for rows this collapsed. Verified in production
      output: 412,312 rows (from 465,801 bronze — a 53,489-row reduction, close to but not
      exactly the 106,534/53,174 estimated during the column audit, since trimming
      whitespace here merges a handful of near-duplicate groups the earlier untrimmed scan
      counted separately), `multi_navigator`=53,218.
- [ ] *[open]* `[REMOVED]` description-prefix's real meaning still unclear. Column carried
      through to silver unchanged.
- [ ] *[open]* `placement_instant` vs. `removal_instant` asymmetric null rate
      uninvestigated. Columns carried through to silver unchanged.

**`patient_procedure_events`:**
- [ ] *[dedup]* Collapse the extreme "Mark Now" outlier duplicates (up to 345 exact
      copies — clear charting glitch).
- [ ] *[dedup, open]* The size-2-pair majority (93.8% of duplicate groups) is
      **unresolved bug-vs-convention** — don't dedup these until the drug/dose-field
      investigation (flagged 2026-08-21) settles whether they're a legitimate
      one-row-per-item charting pattern.
- [ ] *[accuracy]* The 2 extreme (~7-day) auto-close outliers are system artifacts, not
      real durations — exclude from any duration-based feature, don't just clip.
- [ ] *[not a defect]* `Anesthesia Stop` occurring after `Transported to PACU/ICU`
      (12.6% of encounters) is legitimate charting lag, confirmed by the user — leave as
      real data, don't "fix."

**`patient_labs`:**
- [ ] *[dedup]* Collapse the 0.38% exact duplicates (confirmed literal duplicate lines
      in the raw source CSV).
- [ ] *[sentinel]* The `9999999.0` sentinel (13.7% of all rows) needs a derived
      `is_sentinel`/result-type column, not numeric treatment — it spans 3 different
      mechanisms (pure-qualitative tests, a small stable censored-value minority, and
      tests genuinely split between qualitative-screen/quantitative-confirmatory
      reporting) that may need different downstream handling.
- [ ] *[standardization]* `Abnormal_Flag` has 5 real values (N/L/H/LL/HH), not the 3 the
      docs describe — make sure all 5 are handled, not just N/L/H.
- [ ] *[cleanup]* `ENC_TYPE_NM` is constant (1 distinct value) — zero information,
      candidate to drop rather than carry forward.

**`flowsheets`** (biggest table, biggest silver impact):
- [ ] *[dedup]* Collapse the 63.6% duplication (916M/1.44B rows) to 1 row per exact
      `(LOG_ID, FLO_NAME, FLO_DISPLAY_NAME, RECORD_TYPE, RECORDED_TIME, MEAS_VALUE_NUM,
      MEAS_VALUE_TXT, UNITS)` tuple — see "Duplicate-row audit" above for the mechanism
      discussion (unconfirmed root cause, but dedup is recommended regardless).
- [ ] *[standardization]* Trim `FLO_NAME` whitespace duplicate (`"Vital Signs "` alone is
      27.7% of the entire table) and `UNITS` casing/typo variants (`cmH20`/`cmH2O`,
      `l/min`/`L/min`, `ml`/`mL`).
- [ ] *[accuracy]* Fix/null the 6-row CSV-escaping bug (an unescaped quote in a free-text
      note corrupted `UNITS` for those records).
- [ ] *[missingness]* `RECORD_TYPE` (12.3% null) is scattered, not systematic — decide
      null vs. an explicit `UNKNOWN` category based on downstream need.
- [ ] *[not a defect]* `FLO_DISPLAY_NAME`'s many-to-many relationship with `FLO_NAME` is
      by design (same measurement reused across care-unit templates) — document, don't
      "fix." Same for the 0.26% rows with neither `MEAS_VALUE_NUM` nor `MEAS_VALUE_TXT`
      populated (normal sparse EHR grid behavior).

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
- [ ] Design silver layer — see "Silver-layer design checklist" above for the full,
      per-table breakdown (dedup, missingness, standardization, validity, sentinel,
      semantic corrections)
- [ ] Run the duplicate-row audit across all remaining bronze tables — see "Duplicate-row
      audit" section above. `patient_lda` already confirmed at 22.9% duplicated; every
      other table still needs checking before silver dedup logic is designed
- [ ] Scan the other nine tables for the same `MRN` scientific-notation corruption found
      in `patient_information` — see "MRN corruption and patient-level linkage" above.
      Also determine whether `patient_history` / `patient_visit` (lowercase `mrn`) carry
      `LOG_ID` at all, since MRN-keyed-only tables are unreachable for the 26 affected
      patients the way `patient_coding` already is
- [ ] Design gold-layer feature tables once a specific ML question is chosen (see
      candidate directions in project memory / earlier conversation — real-time
      intraoperative deterioration prediction was the favored direction)
