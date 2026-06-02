"""Bitemporal fields (§7.4). Fields defined now; supersession logic in Phase 5."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class BitemporalFields(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_time: datetime | None = None
    ingested_at: datetime
    valid_from: datetime
    valid_to: datetime | None = None
