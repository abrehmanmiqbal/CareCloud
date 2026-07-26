"""
Patient REST API — thin HTTP layer over patient_service.
Every response uses the consistent envelope: {"data": ..., "error": ...}
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from schemas import PatientCreate, PatientUpdate
import patient_service as svc

router = APIRouter(prefix="/patients", tags=["patients"])


@router.get("")
def list_patients(
    last_name: Optional[str] = None,
    date_of_birth: Optional[str] = None,
    phone_number: Optional[str] = None,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
):
    data, err = svc.list_patients(db, last_name, date_of_birth, phone_number, include_deleted)
    if err:
        raise HTTPException(422, detail=err)
    return {"data": data, "error": None}


@router.get("/{patient_id}")
def get_patient(patient_id: str, db: Session = Depends(get_db)):
    p = svc.get_patient(db, patient_id)
    if not p:
        raise HTTPException(404, detail="Patient not found.")
    return {"data": svc.serialize(p), "error": None}


@router.post("", status_code=201)
def create_patient(payload: PatientCreate, db: Session = Depends(get_db)):
    data = payload.model_dump()
    data["date_of_birth"] = data["date_of_birth"].isoformat()
    patient, err = svc.create_patient(db, data)
    if err:
        raise HTTPException(422, detail=err)
    return {"data": svc.serialize(patient), "error": None}


@router.put("/{patient_id}")
def update_patient(patient_id: str, payload: PatientUpdate, db: Session = Depends(get_db)):
    data = payload.model_dump(exclude_unset=True)
    if data.get("date_of_birth") is not None:
        data["date_of_birth"] = data["date_of_birth"].isoformat()
    patient, err = svc.update_patient(db, patient_id, data)
    if err:
        status = 404 if set(err.keys()) == {"patient_id"} else 422
        raise HTTPException(status, detail=err)
    return {"data": svc.serialize(patient), "error": None}


@router.delete("/{patient_id}")
def delete_patient(patient_id: str, db: Session = Depends(get_db)):
    ok = svc.soft_delete(db, patient_id)
    if not ok:
        raise HTTPException(404, detail="Patient not found.")
    return {"data": {"patient_id": patient_id, "deleted": True}, "error": None}
