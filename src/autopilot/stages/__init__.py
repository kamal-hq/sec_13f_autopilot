"""Pipeline stage abstractions and canonical stage exports."""

from __future__ import annotations

from autopilot.stages.base import Stage, StageError, StageResult, StageSkip
from autopilot.stages.detect import DetectStage
from autopilot.stages.facts import FactsStage
from autopilot.stages.ingest import IngestStage
from autopilot.stages.narrate import NarrateStage
from autopilot.stages.publish import PublishStage
from autopilot.stages.review import ReviewStage
from autopilot.stages.validate import ValidateStage

__all__ = [
    "DetectStage",
    "FactsStage",
    "IngestStage",
    "NarrateStage",
    "PublishStage",
    "ReviewStage",
    "Stage",
    "StageError",
    "StageResult",
    "StageSkip",
    "ValidateStage",
]
