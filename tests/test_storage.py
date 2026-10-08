"""Tests for repository CRUD operations, checkpointing, and lead exporting."""

from pathlib import Path

from sqlalchemy.orm import Session

from contractor_scraper.core.enums import (
    AttemptStatus,
    CompanyStatus,
    ContractorCategory,
    PipelineStage,
    RunStatus,
)
from contractor_scraper.exporting.exporter import LeadExporter
from contractor_scraper.models.company import CompanyCreate
from contractor_scraper.models.lead import FinalLead
from contractor_scraper.storage.repository import LeadRepository


def test_repository_run_lifecycle(test_session: Session) -> None:
    """Verify run creation, checkpoint increments, and completion."""
    repo = LeadRepository(test_session)
    run = repo.create_run(category=ContractorCategory.ROOFING, state="TX", total_records=100)
    assert run.id is not None
    assert run.status == RunStatus.RUNNING.value

    repo.update_run_progress(run.id, processed_delta=10, success_delta=8, failed_delta=2)
    updated_run = repo.get_run(run.id)
    assert updated_run.processed_records == 10
    assert updated_run.successful_records == 8
    assert updated_run.failed_records == 2

    repo.complete_run(run.id, status=RunStatus.COMPLETED)
    finished_run = repo.get_run(run.id)
    assert finished_run.status == RunStatus.COMPLETED.value
    assert finished_run.completed_at is not None


def test_repository_company_upsert_and_deduplication(test_session: Session) -> None:
    """Verify upserting with identical canonical fingerprint returns existing record."""
    repo = LeadRepository(test_session)
    comp1 = CompanyCreate(
        name="Sun City HVAC",
        city="El Paso",
        state="TX",
        category=ContractorCategory.HVAC,
    )
    model1 = repo.upsert_company(comp1, canonical_fingerprint="fp-suncity-1")

    # Second insert with same fingerprint
    comp2 = CompanyCreate(
        name="Sun City HVAC, LLC",
        city="El Paso",
        state="TX",
        category=ContractorCategory.HVAC,
    )
    model2 = repo.upsert_company(comp2, canonical_fingerprint="fp-suncity-1")

    assert model1.id == model2.id


def test_repository_checkpoint_and_restartability(test_session: Session) -> None:
    """Verify get_pending_companies_for_stage excludes records with completed SUCCESS attempts."""
    repo = LeadRepository(test_session)
    run = repo.create_run(category=ContractorCategory.PLUMBING, state="TX")

    comp1 = repo.upsert_company(
        CompanyCreate(
            name="Plumber A", city="Austin", state="TX", category=ContractorCategory.PLUMBING
        )
    )
    comp2 = repo.upsert_company(
        CompanyCreate(
            name="Plumber B", city="Austin", state="TX", category=ContractorCategory.PLUMBING
        )
    )

    # Record SUCCESS attempt for comp1 in WEBSITE_DISCOVERY stage
    repo.record_attempt(
        run_id=run.id,
        company_id=comp1.id,
        stage=PipelineStage.WEBSITE_DISCOVERY,
        status=AttemptStatus.SUCCESS,
    )

    # Query pending companies for WEBSITE_DISCOVERY
    pending = repo.get_pending_companies_for_stage(stage=PipelineStage.WEBSITE_DISCOVERY)
    pending_ids = [c.id for c in pending]

    # comp1 already succeeded, so only comp2 should be returned
    assert comp1.id not in pending_ids
    assert comp2.id in pending_ids


def test_repository_get_verified_leads(test_session: Session) -> None:
    """Verify only companies with verified status and both email & website are returned."""
    repo = LeadRepository(test_session)

    # 1. Fully verified lead
    repo.upsert_company(
        CompanyCreate(
            name="True Roofs",
            city="Dallas",
            state="TX",
            category=ContractorCategory.ROOFING,
            website="https://trueroofs.com",
            email="office@trueroofs.com",
            status=CompanyStatus.VERIFIED_LEAD,
            confidence_score=0.95,
        )
    )

    # 2. Company missing email
    repo.upsert_company(
        CompanyCreate(
            name="No Email Roofs",
            city="Dallas",
            state="TX",
            category=ContractorCategory.ROOFING,
            website="https://noemailroofs.com",
            email=None,
            status=CompanyStatus.VERIFIED_LEAD,
            confidence_score=0.95,
        )
    )

    # 3. Company with unverified status
    repo.upsert_company(
        CompanyCreate(
            name="Pending Roofs",
            city="Dallas",
            state="TX",
            category=ContractorCategory.ROOFING,
            website="https://pendingroofs.com",
            email="info@pendingroofs.com",
            status=CompanyStatus.DISCOVERED,
            confidence_score=0.5,
        )
    )

    leads = repo.get_verified_leads(category="Roofing", state="TX")
    assert len(leads) == 1
    assert leads[0].company_name == "True Roofs"
    assert leads[0].email == "office@trueroofs.com"
    assert leads[0].website == "https://trueroofs.com"


def test_lead_exporter_formats(tmp_path: Path) -> None:
    """Verify LeadExporter exports CSV, Parquet, and JSON properly."""
    leads = [
        FinalLead(
            company_name="Apex Roofing",
            email="sales@apexroofing.com",
            website="https://apexroofing.com",
            city="Austin",
            state="TX",
        ),
        FinalLead(
            company_name="Lone Star HVAC",
            email="info@lonestarhvac.com",
            website="https://lonestarhvac.com",
            city="Houston",
            state="TX",
        ),
    ]

    exporter = LeadExporter()
    csv_path = exporter.export_to_csv(leads, tmp_path / "leads.csv")
    parquet_path = exporter.export_to_parquet(leads, tmp_path / "leads.parquet")
    json_path = exporter.export_to_json(leads, tmp_path / "leads.json")

    assert csv_path.exists() and csv_path.stat().st_size > 0
    assert parquet_path.exists() and parquet_path.stat().st_size > 0
    assert json_path.exists() and json_path.stat().st_size > 0
