# Gold-layer ML use cases: literature grounding + MOVER mapping

Research pass to ground gold-layer design in real-world precedent before locking in a
specific ML question. Two parts: (1) a survey of real, verifiable perioperative ML models
— prioritizing ones actually in clinical/commercial production, not just published
proof-of-concept — grouped by outcome/service area; (2) 18 candidate MOVER use cases
mapping those patterns onto this dataset's actual bronze/silver schema, each grounded in
specific tables/columns and flagged with real feasibility gaps found in this dataset.

Independently fact-checked afterward by a second agent with no visibility into the
research process — it re-verified every literature citation via its own searches and
cross-checked every schema claim directly against `DATA_DICTIONARY.md`. Verdict: no
fabrications, no material errors. Full judge output is in the "Independent verification"
section at the bottom, including the handful of soft spots it did find.

**Status:** research complete, 2026-08-31. No use case selected yet — this is the input to
that decision, not the decision itself. See `DATA_DICTIONARY.md` → "Open items / TODO" for
the standing pointer back here.

**Split constraint that applies to every use case below:** grouped by `MRN`, not `LOG_ID`
— 30% of patients have >1 surgery, 57% of all surgeries belong to a repeat patient (max 41),
confirmed in `DATA_DICTIONARY.md` → "MRN corruption and patient-level linkage."

## Part 1 — Production/impactful perioperative ML, grouped by outcome/service area

**Intraoperative hemodynamic instability**
- **Acumen Hypotension Prediction Index (HPI)** — Edwards Lifesciences. Predicts hypotension
  ~5–15 min ahead from arterial waveform. FDA-cleared (2018 invasive, 2021 noninvasive),
  commercially sold. [MassDevice](https://www.massdevice.com/fda-clears-edwards-lifesciences-hypotension-index-software/)
- **AlertWatch:OR** — University of Michigan spinout. Composite OR risk dashboard fusing
  monitors/EHR/history. FDA-cleared, published outcomes (patients discharged ~1 day earlier,
  −$3,603/case, Kheterpal et al., *Anesthesiology* 2018;128(2):272-282). Note: a later,
  larger (~27,000-patient) follow-up study found more mixed results (improved ventilation
  management, no significant complication/LOS improvement) — the favorable 2018 study isn't
  the full picture. [Michigan News](https://news.umich.edu/u-m-startup-alertwatch-gains-fda-clearance-to-sell-patient-monitoring-software/)

**Postoperative AKI**
- **MySurgeryRisk / IDEA** — Azra Bihorac, U. Florida. Random-forest AKI/complication
  prediction, live real-time deployment at UF Health. [PLOS One](https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0214904)

**Postoperative mortality / morbidity**
- **POTTER** — Bertsimas (MIT) + Velmahos/Kaafarani (MGH/Harvard). Optimal-classification-
  tree model for 30-day mortality, morbidity, 18 specific complications. Free public app,
  externally validated multiple times. [Annals of Surgery 2018](https://pubmed.ncbi.nlm.nih.gov/30124479/)
- ACS NSQIP calculator — the field's regression-based benchmark, not deep ML itself, but the
  shared data/validation backbone most of the above is built and compared against.

**Inpatient/postop deterioration & early warning**
- **Rothman Index** — PeraHealth. Continuous 26-variable acuity score, FDA-cleared (PeraTrend,
  510(k) May 2018), widely deployed, validated for postop ICU-readmission risk.
  [PMC 2024](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10921657/)
- **eCART** — Dana Edelson, U. Chicago / AgileMD. Predicts arrest/ICU-transfer/death within
  a short horizon (~8h claimed; exact window not independently pinned to a primary source —
  unverified but plausible). FDA-cleared 2024 based on data from 21 hospitals, specifically
  validated on postop surgical inpatients (AUC 0.79 vs NEWS 0.76 vs MEWS 0.75).
  [PubMed 2019](https://pubmed.ncbi.nlm.nih.gov/31082902/)
- **Epic Deterioration Index** — bundled into Epic EHR, 100+ health systems, but **flagged**:
  not independently validated pre-deployment, mixed published accuracy.
  [Fast Company](https://www.fastcompany.com/90641343/epic-deterioration-index-algorithm-pandemic-concerns)

**Sepsis prediction**
- **TREWS** — Suchi Saria, Johns Hopkins. Deployed across 5 hospitals, 590,736 patients
  monitored; prospectively showed reduced mortality when confirmed within 3h. Strongest
  "deployed + measured benefit" citation found, numbers confirmed exactly on independent
  re-check. [Nature Medicine 2022](https://www.nature.com/articles/s41591-022-01894-0)
- **Epic Sepsis Model** — deployed at hundreds of hospitals but **negative case study**:
  Michigan Medicine external validation (Wong et al., *JAMA Internal Medicine* 2021,
  ~27,697 patients) found AUC 0.63 (vs. claimed 0.76–0.83), missed 2/3 of cases (33%
  sensitivity), 109 false alerts per true case — root cause a leaking feature ("antibiotics
  already ordered"). Directly relevant as a leakage-audit cautionary tale.
  [STAT](https://www.statnews.com/2021/09/27/epic-sepsis-algorithm-antibiotics-model/)

**Postop respiratory depression / opioid harm**
- **PRODIGY score** — 16-site multicenter study w/ Medtronic Capnostream monitors.
  Research/validated-stage, not a standalone marketed predictive product. AUC commonly cited
  ~0.7–0.8 range; exact "0.76" figure not independently pinned down — unverified but
  directionally right. [Anesthesia & Analgesia 2020](https://journals.lww.com/anesthesia-analgesia/fulltext/2020/10000/prediction_of_opioid_induced_respiratory.6.aspx)

**OR scheduling / case duration**
- **LeanTaaS iQueue for Operating Rooms** — commercial product, vendor-claimed figures vary
  by product line and date (~500 hospitals in a 2022 report; OR-specific figures elsewhere
  cite ~430 hospitals/92 health systems) — treat as vendor-stated, not third-party verified.
  [Product page](https://leantaas.com/products/operating-rooms/)

**Transfusion, delirium, SSI/readmission, difficult airway** — real, peer-reviewed models
confirmed for each (cardiac-surgery transfusion RF model AUC 0.81; DELPHI-EEG delirium model
AUROC 0.87 on 34,550 cases, Seoul National University Hospital; NSQIP-based SSI DNN AUC
~0.85; CNN facial-image difficult-intubation model AUC 0.81, Cuendet et al.) but **none found
with a confirmed named clinical deployment** — research/validated-stage only. Delirium and
airway models notably depend on EEG and imaging, not structured EHR data.

**Infrastructure**: **MPOG** (Multicenter Perioperative Outcomes Group, U. Michigan-based,
~85–100 hospitals per current sources, >31M anesthesia cases aggregated, confirmed exactly)
— the shared registry backbone much of this research is built/validated on; a scale
reference for what MOVER is a smaller, static analog of.

## Part 2 — 18 candidate use cases for MOVER

**A. Intraoperative hemodynamic instability**
1. **Hypotension early-warning** (Acumen HPI analog) — predict MAP/SBP threshold breach N
   minutes ahead from preceding `flowsheets` (RECORD_TYPE=INTRA-OP) vitals trajectory +
   `PRIMARY_ANES_TYPE_NM`/`ASA_RATING_C` + vasopressor exposure (`patient_medications`).
   **Caveat:** flowsheets are periodic charted readings, not per-beat waveform — much
   coarser signal than Acumen's. Blocked on resolving flowsheets' unresolved 63.6%
   duplicate-row question (carry-forward charting vs. real duplication, still not
   root-caused) before trusting any trajectory feature.
2. **Composite intraoperative decompensation score** (AlertWatch:OR analog) — fuse
   `flowsheets` vitals + `patient_medications` (vasoactive/anesthetic agents) +
   `patient_labs` (intraop panels) into one instability classifier per case.

**B. AKI**
3. **Postoperative AKI prediction** (MySurgeryRisk/IDEA analog) — **label must be built, not
   reused**: MOVER's 11-class complication taxonomy has no renal class, so this needs a
   KDIGO-style postop-vs-preop creatinine-rise definition from `patient_labs`. Features:
   `patient_history`/`patient_visit` comorbidities (CKD, diabetes, HTN), `patient_information`
   (age, ASA, procedure), intraop hypotension-duration from flowsheets. Per the project's
   standing design-approval process, this label definition needs sign-off before building.

**C. Mortality / morbidity**
4. **In-hospital mortality** — `patient_information.DISCH_DISP_C` already contains an
   "Expired" code — a real, existing label, not a proxy. Predict from preop
   demographics/comorbidities/procedure type. MOVER's cleanest POTTER/NSQIP analog.
5. **Composite "any real complication"** — collapse the validated 11-class taxonomy (AQI
   generic flag excluded, `CONTEXT_NAME='ENCOUNTER'` filtered, 97.8% real-label coverage)
   into one binary label, mirroring POTTER's composite morbidity output.
6. **Per-class complication prediction** — separate models per one of the 11 validated
   classes (e.g. Cardiovascular, Respiratory), matching POTTER's 18-specific-complication
   approach.

**D. Deterioration / early warning**
7. **Postop ward/ICU acuity trend** (Rothman/eCART analog) — trend `patient_labs` +
   `flowsheets` (RECORD_TYPE=POST-OP) into a continuous score, validated against
   `ICU_ADMIN_FLAG`/`DISCH_DISP_C`. **Caveat:** RECORD_TYPE is 12.3% null, so a
   timestamp-relative fallback (vs. `AN_STOP_DATETIME`) is needed for full coverage.
8. **Unplanned ICU transfer** — `ICU_ADMIN_FLAG` is already a clean boolean label (0 nulls,
   44% positive), direct analog to eCART's ICU-transfer arm.

**E. Sepsis / infection**
9. **Infection-complication early detection** (TREWS analog, with the Epic Sepsis Model's
   failure as an explicit anti-pattern) — label: "Injury/infection" complication class;
   features: labs + `Abnormal_Flag` critical values (LL/HH, the docs-omitted critical flags).
   **Leakage-audit rule borrowed directly from the Epic Sepsis Model postmortem:**
   antibiotic-order timing from `patient_medications` must be excluded or carefully
   time-windowed, since "antibiotics already ordered" was literally the leak that broke
   Epic's model.

**F. Respiratory / opioid**
10. **Postop respiratory depression / opioid-related event** (PRODIGY analog) — features:
    age/sex, OSA/CHF history (`patient_history`), opioid MAR administration
    (`patient_medications`, filtered to `MAR_ACTION_NM='Given'` per the still-open
    exposure-mapping question in the docs); label: "Respiratory" complication class or a
    `patient_lda`-derived reintubation proxy.

**G. Operational / scheduling (no outcome-risk label needed)**
11. **OR case-duration prediction** (LeanTaaS analog) — from `patient_procedure_events`
    checkpoints (Anesthesia Start/Stop, Induction, Intubation) + `PRIMARY_PROCEDURE_NM` +
    ASA. Purely operational, already scoped in prior project notes as an OR-efficiency
    direction.
12. **Non-operative/turnover time prediction** — same checkpoint events, predicting
    anesthesia-ready-to-incision gaps.

**H. Transfusion**
13. **Perioperative transfusion need** — preop Hgb/Hct (`patient_labs`), procedure type,
    large-bore line placement (`patient_lda`) as a transfusion-readiness proxy, blood
    product orders (`patient_medications`). Literature status here is research-stage only —
    MOVER work would sit in that same category, not replace a deployed system.

**I. Delirium (modality-limited)**
14. **Postop delirium proxy** — literature's real models use intraop EEG (not in MOVER).
    MOVER-feasible substitute: "Neurological" complication class + age + benzodiazepine/
    anesthetic exposure + dementia/psychiatric history — explicitly weaker than EEG-based
    models; state this limitation up front, not after building it.

**J. Airway (modality-limited)**
15. **Difficult airway management proxy** — literature uses preop facial/neck photographs
    (not in MOVER). MOVER-feasible substitute: multiple/repeated intubation timestamps
    already flagged in `DATA_DICTIONARY.md` (0.9% of encounters have >1 `Intubation` event,
    "plausibly real re-events") + `patient_lda` airway device records + obesity/OSA
    comorbidity.

**K. SSI / readmission**
16. **Surgical site infection / readmission risk** — `patient_coding` post-discharge
    ICD-10-CM diagnoses (infection-related) + procedure/comorbidity features. **Major
    caveat: `patient_coding` has no `LOG_ID` and no date column at all** — for the 30% of
    patients with multiple surgeries, there is no way to attribute a billing code to a
    specific encounter. This one may not be buildable at encounter grain without an
    unresolved data gap.

**L. MOVER-differentiated (not literature transplants)**
17. **Regional-anesthesia/block complication prediction** — MOVER's taxonomy uniquely
    isolates "Regional" and "Chronic pain" complication classes, which most EHR extracts
    don't expose. No strong production analog found in literature — a genuine research
    opportunity specific to this dataset.
18. **Repeat-surgery prediction** — leverages the MRN-level patient linkage already built
    during silver-layer work (30% of patients have >1 surgery, max 41): predict whether a
    patient returns for another surgical encounter, using first-encounter features. Enabled
    directly by work already done in this repo, not by importing an external template.

## Independent verification

A second agent, with no visibility into the research above, re-verified every literature
claim via its own web searches and cross-checked every MOVER schema claim directly against
`DATA_DICTIONARY.md`. Full verdict:

**Part 2 (schema claims): flawless.** All 9 specific factual claims checked (DISCH_DISP_C's
Expired code, the 11-class taxonomy details, flowsheets' RECORD_TYPE nulls and duplicate-row
finding, Abnormal_Flag's 5 values, MAR_ACTION_NM's open exposure-mapping question,
patient_coding's missing LOG_ID/date, the Intubation-checkpoint re-event rate,
ICU_ADMIN_FLAG's cleanliness, and the MRN grouped-split requirement) traced to verbatim or
near-verbatim text in `DATA_DICTIONARY.md`. Zero errors found — safe to build on directly.

**Part 1 (literature): strong, no fabrications or material misattributions**, across 13
claims spanning Acumen HPI, AlertWatch:OR, MySurgeryRisk/IDEA, POTTER, Rothman Index, eCART,
Epic Deterioration Index, TREWS, Epic Sepsis Model, PRODIGY, LeanTaaS, the
transfusion/delirium/SSI/airway research-stage cluster, and MPOG. Three soft spots, none
rising to "wrong," already folded into Part 1 above:
- eCART's "8 hours" prediction window — plausible, not pinned to a specific primary source.
- PRODIGY's "AUC 0.76" — directionally right, exact figure not independently confirmed.
- AlertWatch:OR's outcomes citation — accurate but one-sided; omitted a later, larger, more
  mixed-results follow-up study.

**Overall:** trustworthy enough to plan real ML work on. If any of the three soft-spot
numbers ever becomes load-bearing for a go/no-go decision, pull the primary paper rather
than relying on the secondhand citation.
