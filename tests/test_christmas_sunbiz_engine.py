"""Offline tests for the Sunbiz -> Christmas / holiday lighting lead engine.

Search and crawling are replaced by in-memory fakes; the real ChristmasLightClassifier, identity
matcher, name filter, CSV writer and resume journal are exercised.
"""

import asyncio
import csv
import json
from pathlib import Path

import pytest

from contractor_scraper.classification.christmas_name_filter import (
    SCOPE_BROAD,
    SCOPE_FOCUSED,
    SCOPE_HOLIDAY,
    TIER_ADJACENT,
    TIER_HOLIDAY,
    TIER_LIGHTING,
    evaluate_company_name,
)
from contractor_scraper.config.settings import Settings
from contractor_scraper.discovery.base import WebsiteCandidate
from contractor_scraper.discovery.christmas_website_finder import SearchOutcome
from contractor_scraper.discovery.company_identity import verify_company_identity
from contractor_scraper.discovery.crawler import CrawledPage, CrawlResult
from contractor_scraper.discovery.website_finder import extract_root_domain
from contractor_scraper.extraction.mail_domain import MailDomainChecker
from contractor_scraper.orchestration.christmas_sunbiz_engine import (
    LEAD_COLUMNS,
    ChristmasSunbizConfig,
    ChristmasSunbizLeadEngine,
    IncrementalCsvWriter,
    LeadFileFormatError,
    format_formation_date,
)
from tests.conftest import make_sunbiz_line

# ------------------------------------------------------------------------------------ fixtures


def _crawl(domain: str, title: str, text: str, emails: list[str] | None = None) -> CrawlResult:
    emails = emails or []
    url = f"https://{domain}"
    return CrawlResult(
        domain=domain,
        base_url=url,
        is_accessible=True,
        homepage=CrawledPage(url=url, status_code=200, title=title, text_content=text, emails=emails),
        all_emails=emails,
        combined_text=f"{title} {text}",
        pages_fetched=1,
        status_category="OK",
    )


SITES: dict[str, CrawlResult | Exception] = {
    "redhatchristmaslighting.com": _crawl(
        "redhatchristmaslighting.com",
        "Red Hat Christmas Lighting | Orlando, FL",
        "Red Hat Christmas Lighting provides professional Christmas light installation in Orlando, Florida. "
        "We design, install, maintain, take down and store your Christmas lights. Free estimate. "
        "Email info@redhatchristmaslighting.com",
        ["info@redhatchristmaslighting.com", "studio@webagency-designs.com"],
    ),
    "brightnightsholidaylights.com": _crawl(
        "brightnightsholidaylights.com",
        "Bright Nights Holiday Lights - Tampa FL",
        "Professional holiday lighting installation for homes and businesses in Tampa, Florida. Our crews hang, "
        "maintain and remove your lights every season. Request a quote. Site by studio@webagency-designs.com",
        ["studio@webagency-designs.com"],
    ),
    "sunshinelighting.com": _crawl(
        "sunshinelighting.com",
        "Sunshine Lighting | Licensed Electrician Orlando FL",
        "Sunshine Lighting is a licensed electrician serving Orlando. Panel upgrades, rewiring, EV charger "
        "installation, recessed lighting and generators. Licensed and insured. Free estimate.",
        ["office@sunshinelighting.com"],
    ),
    "palmlandscapelighting.com": _crawl(
        "palmlandscapelighting.com",
        "Palm Landscape Lighting | Naples, FL",
        "Palm Landscape Lighting installs landscape lighting, pathway lighting, garden lighting and uplighting "
        "for homes in Naples, Florida. Licensed and insured. Free estimate.",
    ),
    "coastaloutdoorlighting.com": _crawl(
        "coastaloutdoorlighting.com",
        "Coastal Outdoor Lighting | Sarasota, FL",
        "Coastal Outdoor Lighting designs and installs outdoor lighting, architectural lighting and security "
        "lighting for homes and businesses in Sarasota, Florida. Free estimate.",
    ),
    # A different company's site that happens to offer Christmas lights (must not be assigned to "Glow")
    "acmeroofingmiami.com": _crawl(
        "acmeroofingmiami.com",
        "Acme Roofing Miami",
        "Acme Roofing provides roof repair and replacement in Miami, Florida. We also offer professional "
        "Christmas light installation. We design, install, maintain and take down your Christmas lights.",
        ["info@acmeroofingmiami.com"],
    ),
    "seasonsgreetingsholidaylights.com": _crawl(
        "seasonsgreetingsholidaylights.com",
        "Seasons Greetings Holiday Lights | Jacksonville",
        "Seasons Greetings Holiday Lights offers professional Christmas light installation in Jacksonville, "
        "Florida. We design, install, maintain, take down and store your Christmas lights. Free estimate.",
    ),
    "sparklechristmaslights.com": RuntimeError("connection reset by peer"),
}

SEARCH_RESULTS: dict[str, list[str]] = {
    "Red Hat Christmas Lighting": ["https://redhatchristmaslighting.com"],
    "Red Hat Holiday Lighting": ["https://redhatchristmaslighting.com"],
    "Bright Nights Holiday Lights": ["https://brightnightsholidaylights.com"],
    "Sunshine Lighting": ["https://sunshinelighting.com"],
    "Palm Landscape Lighting": ["https://palmlandscapelighting.com"],
    "Coastal Outdoor Lighting": ["https://coastaloutdoorlighting.com"],
    "Glow Holiday Lighting": ["https://acmeroofingmiami.com"],
    "Seasons Greetings Holiday Lights": ["https://seasonsgreetingsholidaylights.com"],
    "Sparkle Christmas Lights": ["https://sparklechristmaslights.com"],
}


class FakeFinder:
    def __init__(self, results: dict[str, list[str]], responded: bool = True, on_discover=None) -> None:
        self.results = results
        self.responded = responded
        self.on_discover = on_discover
        self.calls: list[str] = []

    async def discover(self, company_name, city, state="FL", street_address="", zip_code=""):
        self.calls.append(company_name)
        if self.on_discover:
            self.on_discover(company_name)
        urls = self.results.get(company_name, [])
        return SearchOutcome(
            candidates=[
                WebsiteCandidate(url=u, domain=extract_root_domain(u), discovery_source="public_search")
                for u in urls
            ],
            search_responded=self.responded,
        )


class FakeCrawler:
    def __init__(self, sites: dict[str, CrawlResult | Exception]) -> None:
        self.sites = sites
        self.calls: list[str] = []

    async def crawl_domain(self, url: str, extra_seed_urls=None) -> CrawlResult:
        domain = extract_root_domain(url)
        self.calls.append(domain)
        site = self.sites.get(domain)
        if isinstance(site, Exception):
            raise site
        if site is None:
            return CrawlResult(domain=domain, base_url=url, is_accessible=False, status_category="Connection Error")
        return site

    async def aclose(self) -> None:
        pass


REGISTRATIONS = [
    # (doc, name, city, zip, file_date)
    ("L25000000001", "RED HAT CHRISTMAS LIGHTING, LLC", "ORLANDO", "32801", "01152025"),
    ("L25000000002", "BRIGHT NIGHTS HOLIDAY LIGHTS LLC", "TAMPA", "33602", "02032025"),
    ("L25000000003", "SUNSHINE LIGHTING LLC", "ORLANDO", "32803", "03012025"),
    ("L25000000004", "PALM LANDSCAPE LIGHTING LLC", "NAPLES", "34102", "03022025"),
    ("L25000000005", "COASTAL OUTDOOR LIGHTING LLC", "SARASOTA", "34236", "03032025"),
    ("L25000000006", "GLOW HOLIDAY LIGHTING LLC", "MIAMI", "33101", "03042025"),
    ("L25000000007", "SEASONS GREETINGS HOLIDAY LIGHTS LLC", "JACKSONVILLE", "32202", ""),
    ("L25000000008", "SPARKLE CHRISTMAS LIGHTS LLC", "OCALA", "34470", "03062025"),
    ("L25000000009", "APEX ROOFING CONTRACTORS LLC", "ORLANDO", "32801", "03072025"),
]


def _write_sunbiz(root: Path, registrations=REGISTRATIONS, file_name: str = "20250315c.txt") -> None:
    year_dir = root / "raw" / "sunbiz" / "2025"
    year_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        make_sunbiz_line(corp_num=doc, name=name, city=city, zip_code=zip_code, file_date=date)
        for doc, name, city, zip_code, date in registrations
    ]
    (year_dir / file_name).write_text("\n".join(lines) + "\n", encoding="latin-1")


def _engine(tmp_path: Path, finder=None, crawler=None, **overrides) -> ChristmasSunbizLeadEngine:
    config = ChristmasSunbizConfig(
        output_csv=tmp_path / "out" / "christmas_holiday_lighting_leads.csv",
        concurrency=1,
        check_mail_domains=False,
        **overrides,
    )
    engine = ChristmasSunbizLeadEngine(
        config=config,
        finder=finder or FakeFinder(SEARCH_RESULTS),
        crawler=crawler or FakeCrawler(SITES),
        mail_checker=MailDomainChecker(enabled=False),
    )
    engine.settings = Settings(raw_data_dir=tmp_path / "raw", output_dir=tmp_path / "out", cache_dir=tmp_path / "cache")
    return engine


def _rows(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _journal(engine: ChristmasSunbizLeadEngine) -> dict[str, dict]:
    return engine.journal.load()


# ------------------------------------------------------------------------------ name filter / dates


def test_name_filter_tiers_and_scope() -> None:
    assert evaluate_company_name("RED HAT CHRISTMAS LIGHTING, LLC").tier == TIER_HOLIDAY
    assert evaluate_company_name("FREEDOM HOLIDAY LIGHTS LLC").tier == TIER_HOLIDAY
    assert evaluate_company_name("TAMPA BAY PERMANENT LIGHTING LLC").tier == TIER_HOLIDAY
    assert evaluate_company_name("SUNSHINE LIGHTING LLC").tier == TIER_LIGHTING
    assert evaluate_company_name("SUNSHINE LIGHTING LLC", SCOPE_HOLIDAY) is None
    assert evaluate_company_name("ACE PRESSURE WASHING LLC", SCOPE_FOCUSED) is None
    assert evaluate_company_name("ACE PRESSURE WASHING LLC", SCOPE_BROAD).tier == TIER_ADJACENT
    # Holiday without a lighting/decor word is a travel/hospitality name, not a candidate
    assert evaluate_company_name("HOLIDAY INN EXPRESS LLC", SCOPE_FOCUSED) is None


@pytest.mark.parametrize(
    "name",
    [
        "CHRISTMAS AT THE CABIN FILM LLC",
        "CHRISTMAS TREE LANE, LLC",
        "JUANITA CHRISTMAS, NOTARY PUBLIC LLC",
        "HOLIDAY LIGHTS REALTY LLC",
    ],
)
def test_name_filter_excludes_non_service_registrations(name: str) -> None:
    assert evaluate_company_name(name, SCOPE_BROAD) is None


def test_formation_date_is_iso_or_empty() -> None:
    assert format_formation_date("01152025") == "2025-01-15"
    assert format_formation_date("") == ""
    assert format_formation_date("13452025") == ""  # invalid month/day is never guessed
    assert format_formation_date("0115") == ""


# -------------------------------------------------------------------------------------- identity


def test_identity_accepts_official_site() -> None:
    match = verify_company_identity(
        SITES["redhatchristmaslighting.com"], "Red Hat Christmas Lighting", "Orlando", "32801"
    )
    assert match.is_match
    assert "exact_name_in_title" in match.checks


def test_identity_rejects_other_company_with_shared_niche_words() -> None:
    match = verify_company_identity(SITES["acmeroofingmiami.com"], "Glow Holiday Lighting", "Miami", "33101")
    assert not match.is_match


def test_identity_rejects_single_brand_word_without_corroboration() -> None:
    site = _crawl("freedomroofing.com", "Freedom Roofing", "Freedom Roofing - roof repair in Atlanta, Georgia.")
    assert not verify_company_identity(site, "Freedom Holiday Lights", "Pensacola", "32501").is_match


def test_identity_rejects_unrelated_business_sharing_the_brand_word() -> None:
    # Seen in a live run: "Freedom Holiday Lights LLC" (The Villages) was matched to a mortgage lender
    site = _crawl(
        "freedommortgage.com",
        "Freedom Mortgage | Home Loans & Refinancing",
        "Freedom Mortgage home loans. Branch locations include The Villages, Florida 32162. Holiday hours.",
    )
    assert not verify_company_identity(site, "Freedom Holiday Lights", "The Villages", "32162").is_match
    # Blog/news page titles on the wrong site must not supply the missing name words
    site.subpages = [
        CrawledPage(url="https://freedommortgage.com/news", status_code=200, title="Annual Holiday Toy Drive Lights Up"),
    ]
    assert not verify_company_identity(site, "Freedom Holiday Lights", "The Villages", "32162").is_match
    # Same brand word on the company's own lighting site still matches
    own = _crawl("freedomlights.com", "Freedom Lights | Holiday Lighting", "Freedom Lights hangs Christmas lights.")
    assert verify_company_identity(own, "Freedom Holiday Lights", "The Villages", "32162").is_match


def test_identity_rejects_famous_brand_domain_without_niche_or_location() -> None:
    site = _crawl("redhat.com", "Red Hat - Open Source Enterprise Software", "Red Hat Enterprise Linux and OpenShift.")
    assert not verify_company_identity(site, "Red Hat Holiday Lighting", "Belleview", "34420").is_match


def test_identity_generic_name_needs_location_or_domain() -> None:
    site = _crawl(
        "brightlights-ohio.com",
        "Holiday Lighting Installation",
        "Holiday Lighting Installation - we hang Christmas lights in Columbus, Ohio.",
    )
    assert not verify_company_identity(site, "Holiday Lighting Installation", "Miami", "33101").is_match
    located = _crawl(
        "xmaspros.com",
        "Holiday Lighting Installation",
        "Holiday Lighting Installation - we hang Christmas lights across Miami, FL 33101.",
    )
    assert verify_company_identity(located, "Holiday Lighting Installation", "Miami", "33101").is_match


# ---------------------------------------------------------------------------------- csv writer


def test_csv_writer_escapes_and_survives_file_lock(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "leads.csv"
    writer = IncrementalCsvWriter(path, LEAD_COLUMNS)
    writer.ensure_header()
    tricky = dict.fromkeys(LEAD_COLUMNS, "")
    tricky.update(company_name='SANTA\'S "BEST" CHRISTMAS LIGHTS, LLC', evidence_summary="line one\nline two")

    real_write = writer._write
    calls = {"n": 0}

    def flaky_write(rows):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("locked by Excel")
        real_write(rows)

    monkeypatch.setattr(writer, "_write", flaky_write)
    assert writer.append(tricky) is False
    assert writer.pending and _rows(path) == []
    second = dict(tricky, company_name="SECOND LLC")
    assert writer.append(second) is True
    rows = _rows(path)
    assert [r["company_name"] for r in rows] == ['SANTA\'S "BEST" CHRISTMAS LIGHTS, LLC', "SECOND LLC"]
    assert rows[0]["evidence_summary"] == "line one\nline two"


def test_csv_writer_refuses_foreign_header(tmp_path: Path) -> None:
    path = tmp_path / "leads.csv"
    path.write_text("name,phone\nx,1\n", encoding="utf-8")
    with pytest.raises(LeadFileFormatError):
        IncrementalCsvWriter(path, LEAD_COLUMNS).ensure_header()


# ------------------------------------------------------------------------------------ end to end


def test_engine_accepts_only_verified_holiday_installers(tmp_path: Path) -> None:
    _write_sunbiz(tmp_path)
    engine = _engine(tmp_path)
    summary = asyncio.run(engine.run())

    rows = _rows(engine.output_csv)
    assert list(rows[0].keys())[:4] == ["company_name", "email", "website", "formation_date"]
    assert list(rows[0].keys()) == LEAD_COLUMNS
    by_doc = {r["sunbiz_document_number"]: r for r in rows}
    assert set(by_doc) == {"L25000000001", "L25000000002", "L25000000007"}

    red_hat = by_doc["L25000000001"]
    assert red_hat["company_name"] == "RED HAT CHRISTMAS LIGHTING, LLC"
    assert red_hat["website"] == "https://redhatchristmaslighting.com"
    assert red_hat["email"] == "info@redhatchristmaslighting.com"
    assert red_hat["formation_date"] == "2025-01-15"
    assert "Christmas Light Installation" in red_hat["service_category"]

    bright = by_doc["L25000000002"]
    assert bright["email"] == ""  # only a web-designer address was listed: never assigned or invented
    assert "Holiday Light Installation" in bright["service_category"]

    assert by_doc["L25000000007"]["formation_date"] == ""  # missing in Sunbiz -> stays empty

    journal = _journal(engine)
    assert journal["L25000000003"]["status"] == "not_qualified"  # electrician
    assert journal["L25000000004"]["status"] == "not_qualified"  # landscape lighting
    assert journal["L25000000005"]["status"] == "not_qualified"  # outdoor lighting
    assert journal["L25000000006"]["status"] == "website_not_verified"  # other company's site
    assert journal["L25000000008"]["status"] == "error"  # crawl failure did not stop the run
    assert "L25000000009" not in journal  # roofing name is outside the focused scope
    assert summary.new_leads == 3
    assert summary.researched == 8


def test_leads_are_written_before_the_run_finishes(tmp_path: Path) -> None:
    _write_sunbiz(tmp_path)
    seen_while_running: list[list[str]] = []
    holder: dict[str, ChristmasSunbizLeadEngine] = {}

    def on_discover(name: str) -> None:
        if name == "Bright Nights Holiday Lights":
            out = holder["engine"].output_csv
            seen_while_running.append([r["company_name"] for r in _rows(out)])

    engine = _engine(tmp_path, finder=FakeFinder(SEARCH_RESULTS, on_discover=on_discover))
    holder["engine"] = engine
    asyncio.run(engine.run())
    assert seen_while_running == [["RED HAT CHRISTMAS LIGHTING, LLC"]]


def test_resume_skips_processed_registrations_and_never_duplicates(tmp_path: Path) -> None:
    _write_sunbiz(tmp_path)
    first = _engine(tmp_path, limit=2)  # simulates a run stopped early
    asyncio.run(first.run())
    assert len(_rows(first.output_csv)) == 2

    finder = FakeFinder(SEARCH_RESULTS)
    second = _engine(tmp_path, finder=finder)
    asyncio.run(second.run())
    assert "Red Hat Christmas Lighting" not in finder.calls
    assert "Bright Nights Holiday Lights" not in finder.calls
    rows = _rows(second.output_csv)
    assert len(rows) == 3
    assert len({r["sunbiz_document_number"] for r in rows}) == 3

    # Only the temporary failure is retried on the next run
    finder3 = FakeFinder(SEARCH_RESULTS)
    third = _engine(tmp_path, finder=finder3)
    asyncio.run(third.run())
    assert finder3.calls == ["Sparkle Christmas Lights"]
    assert len(_rows(third.output_csv)) == 3
    assert _journal(third)["L25000000008"]["attempts"] == 2


def test_same_website_is_saved_once(tmp_path: Path) -> None:
    _write_sunbiz(
        tmp_path,
        [
            ("L25000000001", "RED HAT CHRISTMAS LIGHTING, LLC", "ORLANDO", "32801", "01152025"),
            ("L25000000010", "RED HAT HOLIDAY LIGHTING LLC", "ORLANDO", "32801", "05012025"),
        ],
    )
    engine = _engine(tmp_path)
    asyncio.run(engine.run())
    assert [r["sunbiz_document_number"] for r in _rows(engine.output_csv)] == ["L25000000001"]
    assert _journal(engine)["L25000000010"]["status"] == "duplicate"


def test_blocked_search_is_retryable_not_final(tmp_path: Path) -> None:
    _write_sunbiz(tmp_path, [REGISTRATIONS[0]])
    blocked = _engine(tmp_path, finder=FakeFinder({}, responded=False))
    asyncio.run(blocked.run())
    assert _journal(blocked)["L25000000001"]["status"] == "search_unavailable"
    assert _rows(blocked.output_csv) == []

    recovered = _engine(tmp_path)
    asyncio.run(recovered.run())
    assert [r["sunbiz_document_number"] for r in _rows(recovered.output_csv)] == ["L25000000001"]


def test_verified_lead_left_pending_by_lock_is_restored_on_next_run(tmp_path: Path) -> None:
    _write_sunbiz(tmp_path, [REGISTRATIONS[0]])
    engine = _engine(tmp_path)

    def always_locked(rows):
        raise PermissionError("locked")

    engine.lead_writer._write = always_locked
    summary = asyncio.run(engine.run())
    assert summary.pending_unwritten_rows == 1
    assert _rows(engine.output_csv) == []
    state = [json.loads(line) for line in engine.state_path.read_text(encoding="utf-8").splitlines()]
    assert state[-1]["status"] == "qualified" and state[-1]["row"]["website"]

    finder = FakeFinder(SEARCH_RESULTS)
    rerun = _engine(tmp_path, finder=finder)
    asyncio.run(rerun.run())
    assert finder.calls == []  # restored from the journal, not re-researched
    assert [r["sunbiz_document_number"] for r in _rows(rerun.output_csv)] == ["L25000000001"]


def test_csv_exists_with_header_even_when_nothing_qualifies(tmp_path: Path) -> None:
    _write_sunbiz(tmp_path, [REGISTRATIONS[2]])
    engine = _engine(tmp_path)
    asyncio.run(engine.run())
    with open(engine.output_csv, encoding="utf-8-sig", newline="") as f:
        assert next(csv.reader(f)) == LEAD_COLUMNS
    assert _rows(engine.output_csv) == []
