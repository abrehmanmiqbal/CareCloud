"""
API + service-layer tests for the CareCloud patient registration system.

Run:
    SEED_DEMO_PATIENTS=0 DATABASE_URL=sqlite:////tmp/carecloud_test.db pytest test_api.py -v
"""
import os
import tempfile

os.environ["SEED_DEMO_PATIENTS"] = "0"
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.gettempdir(), "carecloud_test.db")

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import database
import validation as v
import patient_service as svc
import voice_agent
from main import app

VALID = {
    "first_name": "Test",
    "last_name": "Patient",
    "date_of_birth": "1990-03-15",
    "sex": "Female",
    "phone_number": "4702566802",
    "address_line_1": "1 Main St",
    "city": "Atlanta",
    "state": "GA",
    "zip_code": "30301",
}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def db():
    session = database.SessionLocal()
    yield session
    session.close()


# ---------------------------------------------------------------- validation


def test_validate_dob_future_rejected():
    future = (date.today() + timedelta(days=30)).isoformat()
    assert v.validate_dob(future) is not None


def test_validate_dob_ok():
    assert v.validate_dob("1990-03-15") is None


def test_validate_phone_rejects_short_and_accepts_10_digits():
    assert v.validate_phone("123") is not None
    assert v.validate_phone("470-256-6802") is None
    assert v.validate_phone("14702566802") is None  # 11 digits with leading 1 is normalized downstream
    assert v.digits_only("+1 (470) 256-6802") == "14702566802"


def test_validate_zip_and_state():
    assert v.validate_zip("1234") is not None
    assert v.validate_zip("30301") is None
    assert v.validate_zip("30301-1234") is None
    assert v.validate_state("Georgia") is not None
    assert v.validate_state("ga") is None  # normalized to upper


def test_validate_email():
    assert v.validate_email("not-an-email") is not None
    assert v.validate_email("a@b.co") is None
    assert v.validate_email("") is None


def test_missing_required_fields_flagged():
    errors = v.validate_patient_payload({"first_name": "X"})
    for f in ["last_name", "date_of_birth", "sex", "phone_number",
              "address_line_1", "city", "state", "zip_code"]:
        assert f in errors


# ---------------------------------------------------------------- REST API


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_create_and_get(client):
    r = client.post("/patients", json=VALID)
    assert r.status_code == 201
    body = r.json()
    assert body["error"] is None and body["data"]["patient_id"]
    pid = body["data"]["patient_id"]
    assert body["data"]["preferred_language"] == "English"  # default applied

    r2 = client.get(f"/patients/{pid}")
    assert r2.status_code == 200
    assert r2.json()["data"]["first_name"] == "Test"

    r3 = client.get("/patients/does-not-exist")
    assert r3.status_code == 404
    assert r3.json() == {"data": None, "error": "Patient not found."}


@pytest.mark.parametrize("field,bad", [
    ("date_of_birth", "2999-01-01"),
    ("phone_number", "123"),
    ("zip_code", "12"),
    ("state", "Georgia"),
    ("email", "nope"),
    ("sex", "Nope"),
])
def test_create_invalid_fields_return_422(client, field, bad):
    payload = dict(VALID, phone_number="4705550123")  # avoid dup phone across cases
    payload[field] = bad
    r = client.post("/patients", json=payload)
    assert r.status_code == 422
    assert field in r.json()["error"]


def test_list_and_filters(client):
    r = client.get("/patients")
    assert r.status_code == 200 and isinstance(r.json()["data"], list)
    r2 = client.get("/patients", params={"last_name": "Patient"})
    assert all(p["last_name"] == "Patient" for p in r2.json()["data"])
    r3 = client.get("/patients", params={"phone_number": "4702566802"})
    assert len(r3.json()["data"]) >= 1
    r4 = client.get("/patients", params={"date_of_birth": "not-a-date"})
    assert r4.status_code == 422


def test_partial_update(client):
    r = client.post("/patients", json=dict(VALID, phone_number="4705550199"))
    pid = r.json()["data"]["patient_id"]
    r2 = client.put(f"/patients/{pid}", json={"city": "Decatur", "zip_code": "30030"})
    assert r2.status_code == 200
    d = r2.json()["data"]
    assert d["city"] == "Decatur" and d["state"] == "GA"  # untouched field preserved

    r3 = client.put(f"/patients/{pid}", json={"phone_number": "1"})
    assert r3.status_code == 422
    r4 = client.put("/patients/nope", json={"city": "X"})
    assert r4.status_code == 404


def test_soft_delete(client):
    r = client.post("/patients", json=dict(VALID, phone_number="4705550177"))
    pid = r.json()["data"]["patient_id"]
    r2 = client.delete(f"/patients/{pid}")
    assert r2.status_code == 200 and r2.json()["data"]["deleted"] is True
    assert client.get(f"/patients/{pid}").status_code == 404
    hidden = [p for p in client.get("/patients").json()["data"] if p["patient_id"] == pid]
    assert hidden == []
    visible = client.get("/patients", params={"include_deleted": "true"}).json()["data"]
    assert any(p["patient_id"] == pid for p in visible)
    assert client.delete(f"/patients/{pid}").status_code == 404  # already deleted


# ---------------------------------------------------------------- voice agent tool layer


def test_agent_cannot_save_without_confirmation(db):
    """The confirmed:true gate is enforced in code, not just the prompt."""
    args = dict(VALID, phone_number="4705550166")
    result = voice_agent._execute_tool("register_patient", args)  # confirmed missing
    assert result["status"] == "confirmation_required"
    assert svc.find_by_phone(db, "4705550166") is None  # nothing was written


def test_agent_register_with_confirmation(db):
    args = dict(VALID, phone_number="4705550155", confirmed=True)
    result = voice_agent._execute_tool("register_patient", args)
    assert result["status"] == "success" and result["patient_id"]
    # duplicate detection by phone
    found = svc.find_by_phone(db, "4705550155")
    assert found is not None and found.patient_id == result["patient_id"]


def test_agent_register_validation_error_is_field_specific(db):
    args = dict(VALID, phone_number="4705550144", zip_code="12", confirmed=True)
    result = voice_agent._execute_tool("register_patient", args)
    assert result["status"] == "validation_error"
    assert "zip_code" in result["errors"]
    assert svc.find_by_phone(db, "4705550144") is None