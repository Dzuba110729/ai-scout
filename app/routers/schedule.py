from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require_basic_auth
from app.db import get_db
from app.scheduler import get_schedule_config, set_interval
from app.schemas import ScheduleConfigOut, ScheduleConfigUpdate

router = APIRouter(prefix="/api/schedule", tags=["schedule"], dependencies=[Depends(require_basic_auth)])


@router.get("", response_model=ScheduleConfigOut)
def get_schedule(db: Session = Depends(get_db)):
    return get_schedule_config(db)


@router.put("", response_model=ScheduleConfigOut)
def update_schedule(payload: ScheduleConfigUpdate, db: Session = Depends(get_db)):
    try:
        return set_interval(db, payload.interval_days, payload.interval_hours)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
