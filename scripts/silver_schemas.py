from pyiceberg.schema import Schema
from pyiceberg.types import (
    NestedField, StringType, LongType, DoubleType, TimestampType, BooleanType, ListType,
)


class _IdGen:
    """Shared, monotonically-increasing field-id counter -- needed because a nested type
    like ListType has its own field id for the element, distinct from the column's id."""
    def __init__(self):
        self.n = 0

    def next(self):
        self.n += 1
        return self.n


def _schema(fields):
    """fields: list of (name, type_or_builder, required) tuples, in output-column order.
    type_or_builder is either a concrete Type instance, or a callable(ids: _IdGen) -> Type
    for nested types (e.g. ListType) that need to mint their own element field id."""
    ids = _IdGen()
    out = []
    for name, type_or_builder, required in fields:
        field_id = ids.next()
        ftype = type_or_builder(ids) if callable(type_or_builder) else type_or_builder
        out.append(NestedField(field_id=field_id, name=name, field_type=ftype, required=required))
    return Schema(*out)


# Bronze partitions its three large time-series tables by year (see schemas.py) so query
# engines can prune irrelevant files; that partitioning was silently dropped when silver
# was originally written (data-engineering review, 2026-08-27) -- reinstated here on the
# same three tables, same source column, same transform. Looked up by column name via
# Schema.find_field() rather than a hardcoded field id, so this stays correct if a
# table's column order ever changes.
SILVER_PARTITION_COLUMNS = {
    "flowsheets": ("RECORDED_TIME", "recorded_year"),
    "patient_labs": ("Collection_Datetime", "collection_year"),
    "patient_medications": ("MED_ACTION_TIME", "med_action_year"),
}


def partition_spec_for(table_key):
    """Returns the silver PartitionSpec for table_key, or None for tables with no time
    dimension worth partitioning on (everything outside SILVER_PARTITION_COLUMNS)."""
    if table_key not in SILVER_PARTITION_COLUMNS:
        return None
    from pyiceberg.partitioning import PartitionSpec, PartitionField
    from pyiceberg.transforms import YearTransform

    col_name, partition_name = SILVER_PARTITION_COLUMNS[table_key]
    source_id = SILVER_TABLES[table_key].find_field(col_name).field_id
    return PartitionSpec(PartitionField(
        source_id=source_id, field_id=1000, transform=YearTransform(), name=partition_name,
    ))


# Each silver table is fully recomputed from bronze on every build run (not appended
# incrementally like bronze ingestion) -- see docs/DATA_DICTIONARY.md's "Silver-layer
# design checklist" for the per-column rationale behind every transform.
SILVER_TABLES = {
    "patient_information": _schema([
        ("LOG_ID", StringType(), True),
        ("MRN", StringType(), True),
        # Not repaired -- see "MRN corruption and patient-level linkage" in
        # DATA_DICTIONARY.md. Excludes these rows from MRN grouping/joins downstream.
        ("mrn_corrupt", BooleanType(), True),
        # LOG_ID is not a unique key for these rows -- two different real patients
        # share this LOG_ID value. Both rows are kept; any downstream LOG_ID join
        # must also disambiguate by MRN for rows where this is true.
        ("log_id_collision", BooleanType(), True),
        # LOG_ID is not a unique key for these rows either (revised 2026-08-27,
        # supervisor review) -- same-LOG_ID, same-MRN rows with a genuinely conflicting
        # value (e.g. age 64 vs 66, DISCH_DISP_C 15 vs 20) are both kept, not tie-broken,
        # since silently picking one discarded a real alternative with no recovery path.
        ("has_conflicting_duplicate", BooleanType(), True),
        ("DISCH_DISP_C", LongType(), False),
        ("DISCH_DISP", StringType(), False),
        ("HOSP_ADMSN_TIME", TimestampType(), False),
        ("HOSP_DISCH_TIME", TimestampType(), False),
        ("LOS", DoubleType(), False),
        ("ICU_ADMIN_FLAG", StringType(), False),
        ("SURGERY_DATE", TimestampType(), False),
        ("age_years", LongType(), False),  # renamed from bronze BIRTH_DATE
        ("age_capped", BooleanType(), False),  # true where age_years hit the HIPAA safe-harbor cap of 90
        ("height_in", DoubleType(), False),  # parsed from bronze HEIGHT (ft/in text)
        ("weight_kg", DoubleType(), False),  # converted from bronze WEIGHT (ounces)
        ("SEX", StringType(), False),
        ("PRIMARY_ANES_TYPE_NM", StringType(), False),
        ("ASA_RATING_C", LongType(), False),
        ("ASA_RATING", StringType(), False),
        ("PATIENT_CLASS_GROUP", StringType(), False),
        ("PATIENT_CLASS_NM", StringType(), False),
        ("PRIMARY_PROCEDURE_NM", StringType(), False),
        ("IN_OR_DTTM", TimestampType(), False),
        ("OUT_OR_DTTM", TimestampType(), False),
        ("AN_START_DATETIME", TimestampType(), False),
        ("AN_STOP_DATETIME", TimestampType(), False),
        ("_silver_built_at", TimestampType(), True),
    ]),

    "patient_lda": _schema([
        ("LOG_ID", StringType(), True),
        ("MRN", StringType(), True),
        ("description", StringType(), False),
        ("properties_display", StringType(), False),
        ("site", StringType(), False),
        ("placement_instant", TimestampType(), False),
        ("removal_instant", TimestampType(), False),
        ("flo_meas_name", StringType(), False),
        # Replaces bronze's singular Line_Group_Name. 22.9% of bronze rows were the same
        # physical device charted under two different LDA navigator categories at once
        # (e.g. "Drain" + "Urinary Drainage" for one catheter) -- rows identical on every
        # other column. Collapsed to one canonical row per device event; every navigator
        # category it was filed under is kept here instead of duplicating the row.
        ("line_group_names", lambda ids: ListType(
            element_id=ids.next(), element_type=StringType(), element_required=False,
        ), False),
        # True where this device event was the cross-navigator duplication described above.
        ("multi_navigator", BooleanType(), False),
        # Total raw bronze rows collapsed into this group (added 2026-08-27, supervisor
        # review) -- covers both multi_navigator rows and a separate small set of plain
        # exact duplicates (271 rows / 231 groups) that array_agg(DISTINCT...) alone
        # would silently absorb with no record they existed. No information is at risk
        # here (collapsed rows are identical), but this keeps every table in the batch
        # consistent about never discarding multiplicity information silently.
        ("n_occurrences", LongType(), True),
        ("_silver_built_at", TimestampType(), True),
    ]),

    "patient_history": _schema([
        # Renamed from bronze's lowercase `mrn` (2026-08-27, global casing pass) -- every
        # other silver table uses `MRN`; leaving this lowercase was a silent-join-failure
        # risk for anyone joining across tables.
        ("MRN", StringType(), True),
        ("diagnosis_code", StringType(), False),
        ("dx_name", StringType(), False),
        # Bronze has no encounter id for this table, so a diagnosis gets re-exported once
        # per clinical encounter that patient had -- confirmed real (grep-verified against
        # the raw source CSV), scaling ~1:1 with each patient's surgery count in
        # patient_information (1.09x for 1-surgery patients up to 52x for a 41-surgery
        # patient). Collapsed to one row per (MRN, diagnosis_code, dx_name), with the
        # repeat count kept explicitly here instead of left as an implicit row count.
        # Collapses 970,741 bronze rows to 437,721 distinct groups (unchanged by the
        # 2026-08-27 trim pass -- 0 merges for this table's string columns).
        ("n_occurrences", LongType(), True),
        ("_silver_built_at", TimestampType(), True),
    ]),

    "patient_visit": _schema([
        ("LOG_ID", StringType(), True),
        # Renamed from bronze's lowercase `mrn` (2026-08-27, global casing pass) -- same
        # reasoning as patient_history above.
        ("MRN", StringType(), True),
        ("diagnosis_code", StringType(), False),
        ("dx_name", StringType(), False),
        # Unlike patient_history, this repeats WITHIN one encounter (57% of bronze rows),
        # grep-verified real in the source CSV -- likely one row per clinical note/document
        # that reiterates the visit's diagnosis list. Same treatment as patient_history:
        # collapsed to one row per (LOG_ID, MRN, diagnosis_code, dx_name), repeat count
        # kept explicitly. Collapses 219,257 bronze rows to 131,455 distinct groups
        # (unchanged by the 2026-08-27 trim pass -- 0 merges for this table).
        ("n_occurrences", LongType(), True),
        ("_silver_built_at", TimestampType(), True),
    ]),

    "patient_coding": _schema([
        ("MRN", StringType(), True),
        ("SOURCE_KEY", LongType(), False),
        ("SOURCE_NAME", StringType(), False),
        ("NAME", StringType(), False),
        ("REF_BILL_CODE_SET_NAME", StringType(), False),
        ("REF_BILL_CODE", StringType(), False),
        # Same mechanism as patient_history: no encounter id in this table, so a billing
        # code gets re-exported once per clinical encounter that patient had (grep-
        # verified real against the raw source CSV: the top group, 612 occurrences of
        # ICD-10-PCS 0HDAXZZ for one 34-surgery patient, matches exactly). Collapsed to
        # one row per (MRN, SOURCE_KEY, SOURCE_NAME, NAME, REF_BILL_CODE_SET_NAME,
        # REF_BILL_CODE), repeat count kept explicitly rather than left implicit.
        # Collapses 2,033,948 bronze rows to 1,244,633 distinct groups.
        ("n_occurrences", LongType(), True),
        ("_silver_built_at", TimestampType(), True),
    ]),

    "patient_medications": _schema([
        ("ENC_TYPE_C", LongType(), False),
        ("ENC_TYPE_NM", StringType(), False),
        ("LOG_ID", StringType(), True),
        ("MRN", StringType(), True),
        ("ORDERING_DATE", TimestampType(), False),
        ("ORDER_CLASS_NM", StringType(), False),
        ("MEDICATION_ID", LongType(), False),
        ("DISPLAY_NAME", StringType(), False),
        ("MEDICATION_NM", StringType(), False),
        ("START_DATE", TimestampType(), False),
        ("END_DATE", TimestampType(), False),
        ("ORDER_STATUS_NM", StringType(), False),
        ("RECORD_TYPE", StringType(), False),
        ("MAR_ACTION_NM", StringType(), False),
        ("MED_ACTION_TIME", TimestampType(), False),
        ("ADMIN_SIG", DoubleType(), False),
        ("DOSE_UNIT_NM", StringType(), False),
        ("MED_ROUTE_NM", StringType(), False),
        # CONFIRMED EXPORT ARTIFACT, not real clinical events (investigated 2026-08-27,
        # see docs/DATA_DICTIONARY.md "MAR duplicate-row investigation" for the full
        # writeup). Duplicate groups cluster in contiguous multi-day blocks, and discrete
        # one-time actions (MAR Hold/Unhold) repeat at the identical second within those
        # blocks -- e.g. one MAR Hold at the exact same timestamp charted 5 times. This
        # rules out both charting noise (a nurse re-clicking) and periodic infusion-
        # continuation checks as the general explanation. The block pattern also recurs
        # identically across every LOG_ID belonging to one patient's single continuous
        # multi-surgery admission, consistent with an overlapping date-range extraction
        # window in MOVER's own per-encounter export. Collapsed to one row per exact
        # duplicate group, repeat count kept as n_occurrences (same pattern as
        # patient_history/patient_visit/patient_coding) rather than deleted outright.
        # *** n_occurrences reflects export duplication, not repeat administration --
        # NEVER multiply ADMIN_SIG (dose) by n_occurrences to compute a patient's med or
        # fluid total. That is a deliberate gold-layer decision, not a silver default. ***
        # Collapses 27,961,524 bronze rows to 27,773,144 distinct groups.
        ("n_occurrences", LongType(), True),
        ("_silver_built_at", TimestampType(), True),
    ]),

    "patient_post_op_complications": _schema([
        ("LOG_ID", StringType(), True),
        ("MRN", StringType(), True),
        ("Element_Name", StringType(), False),
        ("CONTEXT_NAME", StringType(), False),
        ("Element_abbr", StringType(), False),
        ("SMRTDTA_ELEM_VALUE", StringType(), False),
        # 79% of bronze rows duplicated -- 98% of that is the generic
        # `AN AQI POST-OP COMPLICATIONS` reporting flag (SMRTDTA_ELEM_VALUE always null,
        # up to 49x per encounter), same re-emission-per-note mechanism confirmed for
        # patient_visit (this table has LOG_ID yet still duplicates heavily within one
        # encounter; CONTEXT_NAME='NOTE' is one of three contexts, consistent with "one
        # row per document/note referencing this element"). A small remainder (21 groups,
        # 42 rows) of real, non-null complication values duplicate the same way -- grep-
        # verified real against the raw source CSV, not an ingestion artifact. Collapsed
        # to one row per (LOG_ID, MRN, Element_Name, CONTEXT_NAME, Element_abbr,
        # SMRTDTA_ELEM_VALUE), repeat count kept as n_occurrences. Unlike
        # patient_medications there is no dose/quantity column at risk here -- a
        # complication's presence doesn't need multiplying into a total.
        # Collapses 203,945 bronze rows to 84,776 distinct groups.
        ("n_occurrences", LongType(), True),
        ("_silver_built_at", TimestampType(), True),
    ]),

    "flowsheets": _schema([
        ("LOG_ID", StringType(), True),
        ("MRN", StringType(), True),
        ("FLO_NAME", StringType(), False),
        ("FLO_DISPLAY_NAME", StringType(), False),
        ("RECORD_TYPE", StringType(), False),
        ("RECORDED_TIME", TimestampType(), False),
        ("MEAS_VALUE_NUM", DoubleType(), False),
        ("MEAS_VALUE_TXT", StringType(), False),
        # Trimmed and casing/typo-normalized (cmH20->cmH2O, l/min->L/min, ml->mL); the
        # 6-row CSV-escaping corruption found during the column audit (an unescaped quote
        # in a free-text note spilling garbage into UNITS) is nulled here rather than kept.
        ("UNITS", StringType(), False),
        # 63.6% of bronze rows (916,048,965 of 1,440,918,933) were exact duplicates on the
        # full 9-column tuple -- by far the highest rate in the warehouse, mechanism still
        # unconfirmed (export re-emission vs. device-feed retry, see "Duplicate-row audit"
        # in DATA_DICTIONARY.md), but collapsing is recommended regardless of root cause:
        # a genuinely distinct reading landing on a fully identical tuple by chance is
        # implausible at minute-granularity/this scale. Collapsed to one row per exact
        # (LOG_ID, MRN, FLO_NAME, FLO_DISPLAY_NAME, RECORD_TYPE, RECORDED_TIME,
        # MEAS_VALUE_NUM, MEAS_VALUE_TXT, UNITS) tuple, repeat count kept as
        # n_occurrences -- same pattern as every other table in this batch.
        ("n_occurrences", LongType(), True),
        ("_silver_built_at", TimestampType(), True),
    ]),

    "patient_labs": _schema([
        ("LOG_ID", StringType(), True),
        ("MRN", StringType(), True),
        ("ENC_TYPE_NM", StringType(), False),
        ("Lab_Code", StringType(), False),
        ("Lab_Name", StringType(), False),
        ("Observation_Value", DoubleType(), False),
        ("Measurement_Units", StringType(), False),
        ("Reference_Range", StringType(), False),
        ("Abnormal_Flag", StringType(), False),
        ("Collection_Datetime", TimestampType(), False),
        # A narrow 5-column key (LOG_ID, MRN, Lab_Code, Observation_Value,
        # Collection_Datetime) was considered and rejected: 86% of the "duplicate" groups
        # it would produce actually diverge on columns it ignores -- 673 groups with a
        # genuinely different Reference_Range (e.g. Potassium 4.10 reported against two
        # different reference ranges) and 43,060 groups where Abnormal_Flag is NULL on one
        # row and computed/filled on the other (the flag being derived a moment after the
        # result, not a duplicate). Collapsing on the narrow key would have silently merged
        # both into one row, the same category of mistake caught in patient_medications.
        # Correct key is the full 9-column exact match (excludes ingestion metadata only):
        # 7,536 groups / 15,072 rows (0.05% of the table) are true exact duplicates,
        # grep-confirmed literal duplicate lines in the raw source CSV, not an ingestion
        # bug. Collapsed to one row per group, repeat count kept as n_occurrences; the
        # 673 Reference_Range conflicts and 43,060 Abnormal_Flag completions are left as
        # separate rows, untouched -- collapsing either would discard real information.
        # Collapses 29,079,344 bronze rows to 29,071,808 distinct groups.
        ("n_occurrences", LongType(), True),
        ("_silver_built_at", TimestampType(), True),
    ]),

    "patient_procedure_events": _schema([
        ("LOG_ID", StringType(), True),
        ("MRN", StringType(), True),
        ("EVENT_DISPLAY_NAME", StringType(), False),
        ("EVENT_TIME", TimestampType(), False),
        ("NOTE_TEXT", StringType(), False),
        # 10.5% of bronze rows duplicated. The "1 row per drug" theory for size-2 groups
        # (event name `Two Anti-Emetics Administered` implies exactly 2) was investigated
        # and rejected: (1) only 60% of encounters with that event show exactly 2 rows --
        # 40% show just 1, and a handful show 3 or 4, inconsistent with a fixed 2-rows-per-
        # event convention; (2) duplication rate is wildly uneven across event types --
        # true singular checkpoints (Anesthesia Start, Sign In, Extubation, etc.) sit at a
        # uniform ~0.1-0.6% background rate, while `Two Anti-Emetics Administered` spikes
        # to 74.2% of its own rows -- nothing about the name implying a count explains that
        # rate or why unrelated checkpoint events duplicate at the same small uniform floor;
        # (3) duplicate rows are byte-identical including EVENT_TIME to the minute -- two
        # real distinct drug-administration events landing on the exact same timestamp with
        # zero differentiating data is far more consistent with one action re-emitted than
        # two real events. Grep-confirmed both a real size-2 pair and the extreme 345x
        # `Mark Now` outlier (a stuck-click-style charting glitch) as literal duplicate
        # lines in the raw source CSV. Collapsed to one row per group (full column set,
        # nothing excluded from the key), repeat count kept as n_occurrences.
        # Collapses 640,223 bronze rows to 604,364 distinct groups.
        ("n_occurrences", LongType(), True),
        ("_silver_built_at", TimestampType(), True),
    ]),
}
