"""Pipeline orchestration engine coordinating stages, checkpoints, and structured logs."""

import time
from typing import Any

from contractor_scraper.core.enums import ContractorCategory, RunStatus
from contractor_scraper.core.exceptions import PipelineStageError
from contractor_scraper.core.logging import get_logger
from contractor_scraper.orchestration.stage import BasePipelineStage, StageResult
from contractor_scraper.storage.database import get_db_session
from contractor_scraper.storage.repository import LeadRepository

logger = get_logger("orchestrator")


class LeadGenerationPipeline:
    """Orchestrator for managing multi-stage lead generation workflows.

    Features:
    - Stage lifecycle management
    - Execution timing and structured metrics
    - Database checkpointing and restartability
    - Error isolation ensuring single-record failures do not halt batch runs
    """

    def __init__(self) -> None:
        self._stages: list[BasePipelineStage] = []

    def register_stage(self, stage: BasePipelineStage) -> None:
        """Register a stage in the execution sequence."""
        self._stages.append(stage)

    async def execute_run(
        self,
        category: ContractorCategory,
        state: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Execute all registered pipeline stages for the specified category and state."""
        ctx = context or {}
        ctx["category"] = category
        ctx["state"] = state

        # 1. Initialize persistent run in repository
        with get_db_session() as session:
            repo = LeadRepository(session)
            run = repo.create_run(category=category, state=state)
            run_id = run.id

        stage_logger = get_logger("orchestrator", run_id=run_id)
        stage_logger.info(
            f"Initialized lead generation run {run_id} for {category.value} (State: {state or 'Nationwide'})"
        )

        overall_errors: list[str] = []

        # 2. Iterate through stages
        for stage in self._stages:
            stage_name = stage.stage_name.value
            stage_logger.info(f"Starting stage: {stage_name}")
            start_time = time.perf_counter()

            try:
                result: StageResult = await stage.execute(run_id=run_id, context=ctx)
                duration = time.perf_counter() - start_time
                result.execution_seconds = duration

                # Checkpoint progress to repository
                with get_db_session() as session:
                    repo = LeadRepository(session)
                    repo.update_run_progress(
                        run_id=run_id,
                        processed_delta=result.records_out,
                        success_delta=result.records_out,
                        failed_delta=result.records_failed,
                        skipped_delta=result.records_skipped,
                    )

                stage_logger.record_progress(
                    f"Completed stage {stage_name}",
                    processed=result.records_out,
                    skipped=result.records_skipped,
                    duration_ms=duration * 1000,
                )

                if result.errors:
                    overall_errors.extend(result.errors)

            except Exception as exc:
                duration = time.perf_counter() - start_time
                err_msg = f"Fatal error in stage {stage_name}: {exc}"
                stage_logger.error(err_msg, exc_info=True)
                overall_errors.append(err_msg)

                # Mark run as failed
                with get_db_session() as session:
                    repo = LeadRepository(session)
                    repo.complete_run(
                        run_id=run_id,
                        status=RunStatus.FAILED,
                        error_summary="; ".join(overall_errors),
                    )

                raise PipelineStageError(
                    message=f"Pipeline halted due to unhandled error in {stage_name}",
                    stage=stage_name,
                    run_id=run_id,
                ) from exc

        # 3. Complete run
        final_status = RunStatus.FAILED if overall_errors else RunStatus.COMPLETED
        with get_db_session() as session:
            repo = LeadRepository(session)
            repo.complete_run(
                run_id=run_id,
                status=final_status,
                error_summary="; ".join(overall_errors) if overall_errors else None,
            )

        stage_logger.info(
            f"Finished pipeline execution for run {run_id} with status {final_status.value}"
        )
        return run_id
