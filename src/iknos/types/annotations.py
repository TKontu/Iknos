"""The two-annotation pair (§12).

Every reasoning node and evidential edge carries both:
- Layer A: integer support/derivation count (truth-maintenance ledger)
- Layer B: [0,1] confidence (semiring valuation)

The two are NEVER collapsed into a single number.
"""

from pydantic import BaseModel, ConfigDict, Field


class Annotations(BaseModel):
    model_config = ConfigDict(frozen=True)

    support_count: int = Field(..., ge=0, description="Layer A — derivation count")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Layer B — [0,1] confidence")
