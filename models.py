"""
Patient data model — mirrors the standard minimum demographic dataset
required by U.S. healthcare providers for patient registration.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Date, DateTime
from database import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Patient(Base):
    __tablename__ = "patients"

    # Identity
    patient_id = Column(String(36), primary_key=True, default=_new_uuid)

    # Required demographics
    first_name = Column(String(50), nullable=False)
    last_name = Column(String(50), nullable=False)
    date_of_birth = Column(Date, nullable=False)
    sex = Column(String(20), nullable=False)
    phone_number = Column(String(20), nullable=False, index=True)
    address_line_1 = Column(String(255), nullable=False)
    city = Column(String(100), nullable=False)
    state = Column(String(2), nullable=False)
    zip_code = Column(String(10), nullable=False)

    # Optional demographics
    email = Column(String(255), nullable=True)
    address_line_2 = Column(String(255), nullable=True)
    insurance_provider = Column(String(120), nullable=True)
    insurance_member_id = Column(String(60), nullable=True)
    preferred_language = Column(String(60), nullable=False, default="English")
    emergency_contact_name = Column(String(120), nullable=True)
    emergency_contact_phone = Column(String(20), nullable=True)

    # Bookkeeping
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)  # soft-delete marker
