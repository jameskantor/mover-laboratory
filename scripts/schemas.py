from pyiceberg.schema import Schema
from pyiceberg.types import (
    NestedField, StringType, LongType, DoubleType, TimestampType,
)
from pyiceberg.partitioning import PartitionSpec, PartitionField
from pyiceberg.transforms import YearTransform

# Every table gets these two provenance columns appended at ingestion time.
PROVENANCE_FIELDS = [
    ("_source_file", StringType(), False),
    ("_ingested_at", TimestampType(), False),
]


def _schema(fields, start_id=1):
    """fields: list of (name, type, required) tuples, in source-column order."""
    all_fields = list(fields) + PROVENANCE_FIELDS
    return Schema(*[
        NestedField(field_id=i, name=name, field_type=ftype, required=required)
        for i, (name, ftype, required) in enumerate(all_fields, start=start_id)
    ])


TABLES = {
    "patient_information": {
        "source_csv": "patient_information.csv",
        "schema": _schema([
            ("LOG_ID", StringType(), True),
            ("MRN", StringType(), True),
            ("DISCH_DISP_C", LongType(), False),
            ("DISCH_DISP", StringType(), False),
            ("HOSP_ADMSN_TIME", TimestampType(), False),
            ("HOSP_DISCH_TIME", TimestampType(), False),
            ("LOS", DoubleType(), False),
            ("ICU_ADMIN_FLAG", StringType(), False),
            ("SURGERY_DATE", TimestampType(), False),
            ("BIRTH_DATE", LongType(), False),  # actually age in years, see docs/DATA_DICTIONARY.md
            ("HEIGHT", StringType(), False),  # raw ft/in text, parsed at silver
            ("WEIGHT", DoubleType(), False),  # ounces, EPIC dataset
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
        ]),
        "timestamp_cols": ["HOSP_ADMSN_TIME", "HOSP_DISCH_TIME", "SURGERY_DATE",
                            "IN_OR_DTTM", "OUT_OR_DTTM", "AN_START_DATETIME", "AN_STOP_DATETIME"],
        "int_cols": ["DISCH_DISP_C", "BIRTH_DATE", "ASA_RATING_C"],
        "partition_spec": None,
    },
    "patient_history": {
        "source_csv": "patient_history.csv",
        "schema": _schema([
            ("mrn", StringType(), True),
            ("diagnosis_code", StringType(), False),
            ("dx_name", StringType(), False),
        ]),
        "timestamp_cols": [],
        "int_cols": [],
        "partition_spec": None,
    },
    "patient_visit": {
        "source_csv": "patient_visit.csv",
        "schema": _schema([
            ("LOG_ID", StringType(), True),
            ("mrn", StringType(), True),
            ("diagnosis_code", StringType(), False),
            ("dx_name", StringType(), False),
        ]),
        "timestamp_cols": [],
        "int_cols": [],
        "partition_spec": None,
    },
    "patient_labs": {
        "source_csv": "patient_labs.csv",
        # note: source columns with spaces are normalized to underscores
        "column_rename": {
            "Lab Code": "Lab_Code", "Lab Name": "Lab_Name",
            "Observation Value": "Observation_Value", "Measurement Units": "Measurement_Units",
            "Reference Range": "Reference_Range", "Abnormal Flag": "Abnormal_Flag",
            "Collection Datetime": "Collection_Datetime",
        },
        "schema": _schema([
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
        ]),
        "timestamp_cols": ["Collection_Datetime"],
        "int_cols": [],
        "partition_spec": PartitionSpec(PartitionField(
            source_id=10, field_id=1000, transform=YearTransform(), name="collection_year")),
    },
    "patient_lda": {
        "source_csv": "patient_lda.csv",
        "schema": _schema([
            ("LOG_ID", StringType(), True),
            ("MRN", StringType(), True),
            ("description", StringType(), False),
            ("properties_display", StringType(), False),
            ("site", StringType(), False),
            ("placement_instant", TimestampType(), False),
            ("removal_instant", TimestampType(), False),
            ("flo_meas_name", StringType(), False),
            ("Line_Group_Name", StringType(), False),
        ]),
        "timestamp_cols": ["placement_instant", "removal_instant"],
        "int_cols": [],
        "partition_spec": None,
    },
    "patient_medications": {
        "source_csv": "patient_medications.csv",
        "schema": _schema([
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
        ]),
        "timestamp_cols": ["ORDERING_DATE", "START_DATE", "END_DATE", "MED_ACTION_TIME"],
        "int_cols": ["ENC_TYPE_C", "MEDICATION_ID"],
        "partition_spec": PartitionSpec(PartitionField(
            source_id=15, field_id=1000, transform=YearTransform(), name="med_action_year")),
    },
    "patient_post_op_complications": {
        "source_csv": "patient_post_op_complications.csv",
        "schema": _schema([
            ("LOG_ID", StringType(), True),
            ("MRN", StringType(), True),
            ("Element_Name", StringType(), False),
            ("CONTEXT_NAME", StringType(), False),
            ("Element_abbr", StringType(), False),
            ("SMRTDTA_ELEM_VALUE", StringType(), False),
        ]),
        "timestamp_cols": [],
        "int_cols": [],
        "partition_spec": None,
    },
    "patient_procedure_events": {
        "source_csv": "patient_procedure events.csv",  # literal space in source filename
        "schema": _schema([
            ("LOG_ID", StringType(), True),
            ("MRN", StringType(), True),
            ("EVENT_DISPLAY_NAME", StringType(), False),
            ("EVENT_TIME", TimestampType(), False),
            ("NOTE_TEXT", StringType(), False),
        ]),
        "timestamp_cols": ["EVENT_TIME"],
        "int_cols": [],
        "partition_spec": None,
    },
    "patient_coding": {
        "source_csv": "patient_coding.csv",
        "schema": _schema([
            ("MRN", StringType(), True),
            ("SOURCE_KEY", LongType(), False),
            ("SOURCE_NAME", StringType(), False),
            ("NAME", StringType(), False),
            ("REF_BILL_CODE_SET_NAME", StringType(), False),
            ("REF_BILL_CODE", StringType(), False),
        ]),
        "timestamp_cols": [],
        "int_cols": ["SOURCE_KEY"],
        "partition_spec": None,
    },
    "flowsheets": {
        "source_dir": "flowsheets_cleaned",  # 19 parts, unified into one table
        "drop_cols": ["Unnamed: 0"],
        "schema": _schema([
            ("LOG_ID", StringType(), True),
            ("MRN", StringType(), True),
            ("FLO_NAME", StringType(), False),
            ("FLO_DISPLAY_NAME", StringType(), False),
            ("RECORD_TYPE", StringType(), False),
            ("RECORDED_TIME", TimestampType(), False),
            ("MEAS_VALUE_NUM", DoubleType(), False),
            ("MEAS_VALUE_TXT", StringType(), False),
            ("UNITS", StringType(), False),
        ]),
        "timestamp_cols": ["RECORDED_TIME"],
        "int_cols": [],
        "partition_spec": PartitionSpec(PartitionField(
            source_id=6, field_id=1000, transform=YearTransform(), name="recorded_year")),
    },
}
