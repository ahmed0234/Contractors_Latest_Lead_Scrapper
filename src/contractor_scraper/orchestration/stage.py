"""Abstract pipeline stage interfaces and telemetry result containers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from contractor_scraper.core.enums import PipelineStage


@dataclass
class StageResult:
    """Execution telemetry and outcome of a pipeline stage."""

    stage: PipelineStage
    records_in: int = 0
    records_out: int = 0
    records_skipped: int = 0
    records_failed: int = 0
    execution_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_successful(self) -> bool:
        return len(self.errors) == 0


class BasePipelineStage(ABC):
    """Abstract interface representing a distinct step in the lead generation engine."""

    @property
    @abstractmethod
    def stage_name(self) -> PipelineStage:
        """Name of the pipeline stage."""
        ...

    @abstractmethod
    async def execute(self, run_id: str, context: dict[str, Any]) -> StageResult:
        """Execute stage processing with checkpointing and error isolation."""
        ...
