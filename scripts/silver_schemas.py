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
        # True on the single row kept after a same-LOG_ID, same-MRN, genuinely
        # conflicting-value duplicate was tie-broken (deterministic, not a data claim).
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
        ("_silver_built_at", TimestampType(), True),
    ]),
}
