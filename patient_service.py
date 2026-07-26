"""
Service layer
-------------
The one place patient records are actually created, read, updated, or
soft-deleted. Both the REST API (patients.py) and the voice agent's tool
calls (voice_agent.py) call *these* functions — never the ORM directly and
never each other. That means a patient registered over the phone is
validated by the exact same rules, and lands in the exact same table, as one
created through the API.
"""
import logging
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from models import Patient
import validation as v

logger = logging.getLogger("carecloud.service")

FIELDS = [
    "first_name", "last_name", "date_of_birth", "sex", "phone_number", "email",
    "address_line_1", "address_line_2", "city", "state", "zip_code",
    "insurance_provider", "insurance_member_id", "preferred_language",
    "emergency_contact_name", "emergency_contact_phone",
]


def serialize(p: Patient) -> dict:
    out = {"patient_id": p.patient_id}
    for f in FIELDS:
        val = getattr(p, f)
        out[f] = val.isoformat() if isinstance(val, date) else val
    out["created_at"] = p.created_at.isoformat() if p.created_at else None
    out["updated_at"] = p.updated_at.isoformat() if p.updated_at else None
    return out


def _clean(data: dict) -> dict:
    clean = dict(data)
    if clean.get("phone_number"):
        clean["phone_number"] = v.digits_only(clean["phone_number"])
    if clean.get("emergency_contact_phone"):
        clean["emergency_contact_phone"] = v.digits_only(clean["emergency_contact_phone"])
    if clean.get("state"):
        clean["state"] = clean["state"].strip().upper()
    if clean.get("date_of_birth") and isinstance(clean["date_of_birth"], str):
        clean["date_of_birth"] = datetime.strptime(clean["date_of_birth"], "%Y-%m-%d").date()
    return clean


def list_patients(db: Session, last_name=None, date_of_birth=None, phone_number=None, include_deleted=False):
    q = db.query(Patient)
    if not include_deleted:
        q = q.filter(Patient.deleted_at.is_(None))
    if last_name:
        q = q.filter(Patient.last_name.ilike(last_name.strip()))
    if phone_number:
        q = q.filter(Patient.phone_number == v.digits_only(phone_number))
    if date_of_birth:
        try:
            dob = datetime.strptime(date_of_birth, "%Y-%m-%d").date()
        except ValueError:
            return None, {"date_of_birth": "must be in YYYY-MM-DD format."}
        q = q.filter(Patient.date_of_birth == dob)
    patients = q.order_by(Patient.created_at.desc()).all()
    return [serialize(p) for p in patients], None


def get_patient(db: Session, patient_id: str):
    return db.query(Patient).filter(Patient.patient_id == patient_id, Patient.deleted_at.is_(None)).first()


def find_by_phone(db: Session, phone_number: str):
    phone = v.digits_only(phone_number)
    if not phone:
        return None
    return db.query(Patient).filter(Patient.phone_number == phone, Patient.deleted_at.is_(None)).first()


def create_patient(db: Session, data: dict):
    """Returns (patient, None) on success or (None, errors_dict) on failure."""
    errors = v.validate_patient_payload(data)
    if errors:
        return None, errors

    clean = _clean(data)
    clean.setdefault("preferred_language", "English")
    patient = Patient(**{k: clean.get(k) for k in FIELDS})
    db.add(patient)
    db.commit()
    db.refresh(patient)
    logger.info(
        "patient_registered patient_id=%s name=%r phone=%s",
        patient.patient_id, f"{patient.first_name} {patient.last_name}", patient.phone_number,
    )
    return patient, None


def update_patient(db: Session, patient_id: str, data: dict):
    """Partial update. Returns (patient, None) or (None, errors_dict)."""
    patient = get_patient(db, patient_id)
    if not patient:
        return None, {"patient_id": "No active patient found with that ID."}

    payload = {k: val for k, val in data.items() if val is not None and k in FIELDS}
    if not payload:
        return patient, None

    errors = v.validate_patient_payload(payload, partial=True)
    if errors:
        return None, errors

    clean = _clean(payload)
    for k, val in clean.items():
        setattr(patient, k, val)
    patient.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(patient)
    logger.info("patient_updated patient_id=%s fields=%s", patient.patient_id, list(clean.keys()))
    return patient, None


def soft_delete(db: Session, patient_id: str) -> bool:
    patient = get_patient(db, patient_id)
    if not patient:
        return False
    patient.deleted_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("patient_soft_deleted patient_id=%s", patient.patient_id)
    return True
