"""Append-only process action log (§10.1).

Every operator writes an Action row. Outputs include the AGE node/edge ids that
were created so audit can walk back from any artifact to its origin action.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from iknos.db.orm import Action


async def record_action(
    session: AsyncSession,
    *,
    actor: str,
    action_type: str,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    model: str | None = None,
    sampling: dict[str, Any] | None = None,
    raw_judgment: str | None = None,
    calibration: dict[str, Any] | None = None,
) -> uuid.UUID:
    action = Action(
        actor=actor,
        action_type=action_type,
        inputs=inputs or {},
        outputs=outputs or {},
        model=model,
        sampling=sampling,
        raw_judgment=raw_judgment,
        calibration=calibration,
    )
    session.add(action)
    await session.flush()
    return action.id
