"""Append-only process action log (§10.1).

Every operator writes an Action row. Outputs include the AGE node/edge ids that
were created so audit can walk back from any artifact to its origin action.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from iknos.db.orm import Action


def build_action(
    *,
    actor: str,
    action_type: str,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    model: str | None = None,
    sampling: dict[str, Any] | None = None,
    raw_judgment: str | None = None,
    calibration: dict[str, Any] | None = None,
) -> Action:
    """Construct (but do not persist) the ``Action`` row for an operator's write.

    The pure, DB-free seam of :func:`record_action`: it maps the operator's arguments
    onto the ORM row and applies the one piece of defaulting logic — ``inputs``/``outputs``
    coerce ``None`` → ``{}`` so the §10.1 provenance edges always have an object to read,
    while the optional ``model``/``sampling``/``raw_judgment``/``calibration`` stay ``None``
    when absent (never zeroed). ``id`` and ``timestamp`` are DB-side server defaults, so the
    returned row carries neither until it is flushed.
    """
    return Action(
        actor=actor,
        action_type=action_type,
        inputs=inputs or {},
        outputs=outputs or {},
        model=model,
        sampling=sampling,
        raw_judgment=raw_judgment,
        calibration=calibration,
    )


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
    action = build_action(
        actor=actor,
        action_type=action_type,
        inputs=inputs,
        outputs=outputs,
        model=model,
        sampling=sampling,
        raw_judgment=raw_judgment,
        calibration=calibration,
    )
    session.add(action)
    await session.flush()
    return action.id
