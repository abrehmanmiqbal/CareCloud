"""
Server-side validation — the API never trusts the voice agent (or any other
caller) to have validated data correctly. Every field is re-checked here,
on every create and every update, regardless of where the request came from.
"""
import re
from datetime import date, datetime

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
VALID_SEX = {"Male", "Female", "Other", "Decline to Answer"}

NAME_RE = re.compile(r"^[A-Za-z'\-\s]{1,50}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")

REQUIRED_FIELDS = [
    "first_name", "last_name", "date_of_birth", "sex", "phone_number",
    "address_line_1", "city", "state", "zip_code",
]


def digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def validate_name(value, field):
    if not value or not NAME_RE.match(value.strip()):
        return f"{field} must be 1-50 letters (hyphens and apostrophes allowed)."
    return None


def validate_dob(value):
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return "date_of_birth must be a valid date in YYYY-MM-DD format."
    if value > date.today():
        return "date_of_birth cannot be in the future."
    if value.year < 1900:
        return "date_of_birth does not look like a valid birth date."
    return None


def validate_sex(value):
    if value not in VALID_SEX:
        return f"sex must be one of: {', '.join(sorted(VALID_SEX))}."
    return None


def validate_phone(value, field="phone_number", required=True):
    if not value:
        return f"{field} is required." if required else None
    if len(digits_only(value)) != 10:
        return f"{field} must be a valid 10-digit U.S. phone number."
    return None


def validate_email(value):
    if not value:
        return None
    if not EMAIL_RE.match(value.strip()):
        return "email must be a valid email address."
    return None


def validate_state(value):
    if not value or value.strip().upper() not in US_STATES:
        return "state must be a valid 2-letter U.S. state abbreviation."
    return None


def validate_zip(value):
    if not value or not ZIP_RE.match(value.strip()):
        return "zip_code must be a 5-digit or ZIP+4 U.S. format."
    return None


def validate_required_text(value, field, max_len=255):
    if not value or not str(value).strip():
        return f"{field} is required."
    if len(value) > max_len:
        return f"{field} must be under {max_len} characters."
    return None


_CHECKS = {
    "first_name": lambda v: validate_name(v, "first_name"),
    "last_name": lambda v: validate_name(v, "last_name"),
    "date_of_birth": validate_dob,
    "sex": validate_sex,
    "phone_number": lambda v: validate_phone(v, "phone_number"),
    "email": validate_email,
    "address_line_1": lambda v: validate_required_text(v, "address_line_1"),
    "city": lambda v: validate_required_text(v, "city", 100),
    "state": validate_state,
    "zip_code": validate_zip,
    "emergency_contact_phone": lambda v: validate_phone(v, "emergency_contact_phone", required=False),
}


def validate_patient_payload(data: dict, partial: bool = False) -> dict:
    """Returns {field: error_message} for every invalid/missing field.
    An empty dict means the payload is fully valid.
    `partial=True` skips the "is it present" check (used for PATCH-style
    updates where only changed fields are sent)."""
    errors = {}

    def present(f):
        return f in data and data[f] not in (None, "")

    if not partial:
        for field in REQUIRED_FIELDS:
            if not present(field):
                errors[field] = f"{field} is required."

    for field, validator in _CHECKS.items():
        if present(field) and field not in errors:
            err = validator(data[field])
            if err:
                errors[field] = err

    return errors
