from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require_basic_auth
from app.db import get_db
from app.scheduler import get_schedule_config, reschedule_all
from app.schemas import ScheduleConfigOut, ScheduleConfigUpdate

router = APIRouter(prefix="/api/schedule", tags=["schedule"], dependencies=[Depends(require_basic_auth)])


@router.get("", response_model=ScheduleConfigOut)
def get_schedule(db: Session = Depends(get_db)):
    return get_schedule_config(db)


@router.put("", response_model=ScheduleConfigOut)
def update_schedule(payload: ScheduleConfigUpdate, db: Session = Depends(get_db)):
    if payload.interval_days <= 0 and payload.interval_hours <= 0:
        raise HTTPException(status_code=422, detail="Интервал должен быть больше нуля")

    config = get_schedule_config(db)
    config.interval_days = payload.interval_days
    config.interval_hours = payload.interval_hours
    db.add(config)
    db.commit()
    db.refresh(config)

    reschedule_all(db)

    return config
