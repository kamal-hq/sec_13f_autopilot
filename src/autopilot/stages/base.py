"""Abstract base class and execution primitives for pipeline stages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from autopilot.models import PipelineContext, StageStatus


class StageError(Exception):
    """Raised when a pipeline stage encounters an unrecoverable error."""


class StageSkip(Exception):
    """Raised by a stage to signal that execution should be skipped cleanly."""


@dataclass(slots=True)
class StageResult:
    """Structured outcome returned by `Stage.run(context)`."""

    status: StageStatus = StageStatus.SUCCEEDED
    message: str = ""
    attempts: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


class Stage(ABC):
    """Abstract base class for a discrete, auditable stage in the 13F autopilot pipeline."""

    name: str = "stage"

    def should_run(self, context: PipelineContext) -> bool:
        """Return True if this stage should execute for the given `PipelineContext`."""
        _ = context
        return True

    @abstractmethod
    def run(self, context: PipelineContext) -> StageResult | StageStatus | None:
        """Execute stage logic against `context` and return a `StageResult` or `StageStatus`."""
