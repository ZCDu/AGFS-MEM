from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Depends, HTTPException, Query

from app.api.models import AppendFactsRequest, RawFactsResponse
from app.auth import require_user
from app.deps import get_raw_log
from app.rawlog.log import RawFactLog

router = APIRouter(dependencies=[Depends(require_user)],
                   prefix="/v1/users/{user_id}/raw-facts", tags=["raw-facts"])

MAX_RANGE_DAYS = 366


@router.post("", response_model=RawFactsResponse, status_code=201)
def append_facts(
    user_id: str,
    body: AppendFactsRequest,
    log: RawFactLog = Depends(get_raw_log),
):
    log.append_batch(user_id, body.facts)
    # RawFactLog shards by the UTC date of the write (see log.py's
    # append_batch), so the read-back must use the same UTC "today" -- a
    # naive date.today() (local date) drifts a day out of sync with what was
    # actually just written for roughly a third of the day in any timezone
    # ahead of UTC, making a just-appended batch briefly invisible.
    today = datetime.now(timezone.utc).date()
    records = log.read_day(user_id, today)
    return RawFactsResponse(date=today.isoformat(), count=len(records), records=records)


@router.get("", response_model=RawFactsResponse)
def get_day(
    user_id: str,
    on: date = Query(..., description="Day to read, YYYY-MM-DD"),
    log: RawFactLog = Depends(get_raw_log),
):
    records = log.read_day(user_id, on)
    return RawFactsResponse(date=on.isoformat(), count=len(records), records=records)


@router.get("/range", response_model=list[dict])
def get_range(
    user_id: str,
    start: date = Query(...),
    end: date = Query(...),
    log: RawFactLog = Depends(get_raw_log),
):
    if end < start:
        raise HTTPException(status_code=422, detail="end must be >= start")
    if (end - start) > timedelta(days=MAX_RANGE_DAYS):
        raise HTTPException(
            status_code=422, detail=f"range too large, max {MAX_RANGE_DAYS} days"
        )
    return list(log.iter_range(user_id, start, end))
