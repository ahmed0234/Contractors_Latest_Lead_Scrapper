"""Tests for the cleaning business classifier (qualification, service extraction, main service) and its pipeline."""

import asyncio
import csv
from pathlib import Path

from contractor_scraper.classification.cleaning_service_classifier import (
    NO,
    SERVICE_PRIORITY,
    T_COMMERCIAL,
    T_DIRECTORY,
    T_PRODUCTS,
    T_UNRELATED,
    YES,
    CleaningServiceClassifier,
    LeadInfo,
    analyze_name,
    non_site_link,
    profile_site,
)
from contractor_scraper.discovery.crawler import CrawledPage, CrawlResult, WebsiteCrawler
from contractor_scraper.orchestration.cleaning_enricher import (
    CLEANING_LINK_PROFILE,
    ENRICHMENT_HEADERS,
    CleaningLeadEnricher,
)


def page(url: str, title: str, text: str) -> CrawledPage:
    return CrawledPage(url=url, status_code=200, title=title, text_content=text)


def site(domain: str, pages: list[CrawledPage]) -> CrawlResult:
    home, *rest = pages
    return CrawlResult(
        domain=domain,
        base_url=f"https://{domain}",
        is_accessible=True,
        homepage=home,
        subpages=rest,
        combined_text=" ".join(f"{p.title} {p.text_content}" for p in pages),
        pages_fetched=len(pages),
    )


def dead(domain: str, category: str = "DNS Failure") -> CrawlResult:
    return CrawlResult(domain=domain, base_url=f"https://{domain}", is_accessible=False, status_category=category, error_message="failed")


def classify(crawl: CrawlResult | None, name: str = "", **kw: str):
    return CleaningServiceClassifier().classify(crawl, LeadInfo(business_name=name, **kw))


FILLER = " | ".join(
    ["Family owned and operated since 2005", "Licensed and insured", "Serving homeowners in Anchorage and Eagle River",
     "Call today for a free estimate", "Read our reviews"] * 6
)

EXTERIOR = site(
    "exteriorclean.com",
    [
        page(
            "https://exteriorclean.com/",
            "Alaska Exterior Cleaning - Roof, Gutter & Window Cleaning",
            "Home | Services | Roof Cleaning | Gutter Cleaning | Window Cleaning | We provide professional exterior cleaning "
            f"for homes in Anchorage. | {FILLER}",
        ),
        page(
            "https://exteriorclean.com/services/gutter-cleaning",
            "Gutter Cleaning",
            "Home | Services | Gutter Cleaning | Clean gutters are essential for protecting your home from water damage and costly "
            "repairs. | Our gutter cleaning service clears debris for homeowners.",
        ),
        page(
            "https://exteriorclean.com/services/roof-cleaning",
            "Roof Cleaning",
            "Home | Services | Roof Cleaning / Moss Removal | Our soft washing method is safe for shingles. | High-pressure washing "
            "can damage shingles and void the warranty. | Regular cleaning helps prevent long-term damage. | We achieve a deep clean.",
        ),
        page(
            "https://exteriorclean.com/blog/how-to-clean-carpet",
            "How to clean carpet stains",
            "Home | Services | Tips: carpet cleaning at home is easy with these DIY steps.",
        ),
    ],
)

MAID = site(
    "sparklemaids.com",
    [
        page(
            "https://sparklemaids.com/",
            "Sparkle Maids | House Cleaning in Anchorage",
            "Home | House Cleaning | Deep Cleaning | Move In / Move Out Cleaning | We clean kitchens, bathrooms and bedrooms. | "
            "Our checklist: dust baseboards, clean sinks, toilets, mirrors and interior window cleaning | Add-Ons | Inside Oven | "
            "Window Cleaning | Laundry | Neighborly brands: Dryer Vent Wizard | Molly Maid | "
            f"Need carpet cleaning? Try Steamy Carpets! | {FILLER}",
        ),
    ],
)

CARPET = site(
    "andrewscarpet.com",
    [
        page(
            "https://andrewscarpet.com/",
            "Andrew's Carpet Cleaning | Anchorage",
            "Carpet, Upholstery & Tile Cleaning | Water Damage Restoration | Residential Carpet Cleaning Services in Anchorage | "
            f"We deep clean your carpets with hot water extraction | Area Rugs | Regular cleaning keeps carpets fresh | {FILLER}",
        ),
    ],
)

JANITORIAL = site(
    "officeshine.com",
    [
        page(
            "https://officeshine.com/",
            "Office Shine | Commercial Cleaning & Janitorial Services",
            "Commercial Cleaning | Janitorial Services | Office Cleaning | Floor Stripping & Waxing | We serve businesses, medical "
            f"offices and schools with commercial carpet cleaning and construction cleaning for builders. | {FILLER.replace('homeowners', 'businesses')}",
        ),
        page(
            "https://officeshine.com/office-cleaning",
            "Office Cleaning",
            "Office Cleaning | Nightly office cleaning for corporate offices, retail and warehouses.",
        ),
    ],
)

DIRECTORY = site(
    "bestcleanersnearyou.com",
    [
        page(
            "https://bestcleanersnearyou.com/",
            "Top 10 Best House Cleaning Services Near You",
            "Find the best local house cleaning pros | Compare quotes from multiple local cleaners | Top 10 best house cleaning "
            f"companies in Anchorage | Claim your listing | Write a review | {FILLER}",
        ),
    ],
)

PRODUCTS = site(
    "cleansupplyco.com",
    [
        page(
            "https://cleansupplyco.com/",
            "Clean Supply Co | Janitorial Supplies & Cleaning Equipment",
            "Shop now | Free shipping on orders over $99 | Our products | Janitorial supplies | Carpet cleaning machines | "
            f"Add to cart | Wholesale and bulk pricing | Distributor of cleaning chemicals | {FILLER}",
        ),
    ],
)

ROOFER = site(
    "bobsroofing.com",
    [
        page(
            "https://bobsroofing.com/",
            "Bob's Roofing | Roof Replacement & Repairs",
            "Roofing | Roof replacement | Roof repairs | Shingles and metal roofing | We are the top roofing contractor in town. | "
            f"Free roof inspections | {FILLER}",
        ),
    ],
)


# ------------------------------------------------------------------------------- priority + extraction
def test_priority_list_matches_requested_order() -> None:
    assert SERVICE_PRIORITY[:8] == ("hoarding", "mold", "septic", "biohazard", "fire", "water", "post_construction", "roof")
    assert SERVICE_PRIORITY.index("air_duct") < SERVICE_PRIORITY.index("pressure") < SERVICE_PRIORITY.index("carpet")
    assert SERVICE_PRIORITY.index("gutter") < SERVICE_PRIORITY.index("window") < SERVICE_PRIORITY.index("house")
    assert SERVICE_PRIORITY.index("dryer_vent") < SERVICE_PRIORITY.index("house") < SERVICE_PRIORITY.index("trash_can")


def test_exterior_company_main_service_is_highest_value_offered() -> None:
    res = classify(EXTERIOR, "Alaska Exterior Cleaning LLC")
    assert res.decision_label == YES and res.classification == "Yes"
    assert res.main_service == "Roof Cleaning"
    assert res.services[:3] == ["Roof Cleaning", "Gutter Cleaning", "Window Cleaning"]
    # Consequence wording, warnings, roof-method talk, FAQ phrases and blog tips are not services
    joined = " ".join(res.services).lower()
    for absent in ("water damage", "pressure", "house wash", "soft wash", "deep", "regular", "carpet"):
        assert absent not in joined, absent
    assert ";" not in res.main_service


def test_maid_main_service_ignores_addons_checklists_footers_and_referrals() -> None:
    res = classify(MAID, "Sparkle Maids")
    assert res.decision_label == YES
    assert res.main_service == "Move-In / Move-Out Cleaning"
    assert "House Cleaning" in res.services and "Deep Cleaning" in res.services
    assert not any("dryer" in s.lower() for s in res.services)  # "Dryer Vent Wizard" is a sister brand
    assert not any("carpet" in s.lower() for s in res.services)  # "Need carpet cleaning? Try ..." is a referral
    assert res.main_service != "Window Cleaning"  # add-on extra, never the main service


def test_carpet_company_coordinated_list_and_generic_phrases() -> None:
    res = classify(CARPET, "Andrew's Carpet Cleaning")
    assert res.main_service == "Water Damage Restoration"  # listed as a service and ranked above carpet cleaning
    assert {"Carpet Cleaning", "Upholstery Cleaning", "Tile Cleaning"} <= set(res.services)
    # "deep clean your carpets" / "regular cleaning keeps carpets fresh" do not make it a maid service
    assert not any(s in res.services for s in ("Deep Cleaning", "House Cleaning", "Regular Cleaning", "Residential Cleaning"))
    assert "Andrew's Carpet Cleaning" not in res.services  # brand names are not service names


# ---------------------------------------------------------------------------------------- exclusions
def test_commercial_only_janitorial_is_rejected_without_services() -> None:
    res = classify(JANITORIAL, "Office Shine Janitorial")
    assert res.decision_label == NO and res.classification == "No"
    assert res.business_type == T_COMMERCIAL
    assert res.main_service == "" and res.services == []


def test_directory_products_and_unrelated_sites_are_rejected() -> None:
    assert classify(DIRECTORY, "Best Cleaners Near You").business_type == T_DIRECTORY
    assert classify(PRODUCTS, "Clean Supply Co").business_type == T_PRODUCTS
    roofer = classify(ROOFER, "Bob's Roofing")
    assert roofer.decision_label == NO and roofer.business_type == T_UNRELATED and roofer.services == []
    for res in (classify(DIRECTORY, "x"), classify(PRODUCTS, "y")):
        assert res.decision_label == NO and res.main_service == ""


def test_unrelated_site_with_strong_cleaning_name_goes_to_review() -> None:
    res = classify(ROOFER, "Sparkle Carpet Cleaning")
    assert res.decision_label == YES and res.classification == "Uncertain"
    assert res.main_service == ""  # nothing on the website to take it from


# ---------------------------------------------------------------------------------- unusable websites
def test_unreachable_site_uses_name_with_low_confidence_and_no_services() -> None:
    res = classify(dead("gonecleaning.com"), "Gone Carpet Cleaning")
    assert res.decision_label == YES and res.classification == "Unreachable"
    assert res.confidence < 0.6 and res.main_service == "" and res.services == []
    assert "carpet" in res.reasoning.lower()
    no_url = classify(None, "Acme Holdings", category="Roofing")
    assert no_url.decision_label == NO and no_url.classification == "Unreachable"


def test_name_analysis() -> None:
    assert analyze_name("Merry Maids of Anchorage").service == "house"
    assert analyze_name("Clean Sweep Cleaning Services").service != "chimney"
    assert analyze_name("ABC Dry Cleaners").verdict == NO
    assert analyze_name("Bob's Roofing & Gutters").verdict == NO
    assert analyze_name("Jan-Pro of Alaska").commercial
    assert analyze_name("", category="Carpet Cleaning").service == "carpet"


def test_non_site_links_are_recognised() -> None:
    assert non_site_link("https://share.google/5DBryGbwZHjHglCwO") == "share.google"
    assert non_site_link("https://clienthub.getjobber.com/hubs/x") == "clienthub.getjobber.com"
    assert non_site_link("https://brand.page/solarscrub") == "brand.page"
    assert non_site_link("https://www.realcleaning.com/") == ""


def test_profile_is_computed_once_per_site() -> None:
    profile = profile_site(EXTERIOR)
    a = CleaningServiceClassifier().classify(EXTERIOR, LeadInfo("Alaska Exterior Cleaning"), profile=profile)
    b = CleaningServiceClassifier().classify(EXTERIOR, LeadInfo("Bob's Roofing"), profile=profile)
    assert a.main_service == b.main_service == "Roof Cleaning"
    assert a.confidence > b.confidence  # the name agreeing raises confidence; disagreeing lowers it


def test_link_profile_prioritises_service_pages() -> None:
    assert CLEANING_LINK_PROFILE.score("/services/carpet-cleaning") == 100
    assert CLEANING_LINK_PROFILE.score("/pricing") == 60
    assert CLEANING_LINK_PROFILE.score("/about-us") == 40
    assert CLEANING_LINK_PROFILE.score("/careers") == 0


# ------------------------------------------------------------------------------------------ pipeline
class MockCleaningCrawler(WebsiteCrawler):
    """Deterministic crawler: no network, counts crawls per domain."""

    SITES = {
        "exteriorclean.com": EXTERIOR,
        "sparklemaids.com": MAID,
        "officeshine.com": JANITORIAL,
        "andrewscarpet.com": CARPET,
    }

    def __init__(self, cache_dir: Path) -> None:
        super().__init__(cache_dir=cache_dir)
        self.calls: list[str] = []

    async def crawl_domain(self, base_url: str, extra_seed_urls: list[str] | None = None) -> CrawlResult:
        domain = base_url.split("://", 1)[-1].split("/")[0].replace("www.", "")
        self.calls.append(domain)
        return self.SITES.get(domain) or dead(domain, "Timeout")


HEADERS = ["Primary Email", "Business Name", "Website", "Main Service", "Is Cleaning Company?", "City", "Segment",
           "Category (Overture)", "Email Type", "domain"]
ROWS = [
    ["info@exteriorclean.com", "Alaska Exterior Cleaning", "https://www.exteriorclean.com/", "", "", "Anchorage",
     "Pressure Washing / Exterior", "Gutter Service", "Business domain", "exteriorclean.com"],
    ["hi@sparklemaids.com", "Sparkle Maids", "http://sparklemaids.com", "", "", "Anchorage", "Residential / Maid",
     "Home Cleaning", "Business domain", "sparklemaids.com"],
    ["ops@officeshine.com", "Office Shine", "officeshine.com", "", "", "Anchorage", "Commercial / Janitorial",
     "Janitorial Service", "Business domain", "officeshine.com"],
    # Google share link instead of a website: the business email domain is crawled instead
    ["andrew@andrewscarpet.com", "Andrew's Carpet", "https://share.google/abc123", "", "", "Anchorage",
     "Carpet / Upholstery / Floor", "Carpet Cleaning", "Business domain", "andrewscarpet.com"],
    # Share link and a free-mail address: nothing to crawl, decided from the name
    ["joe@gmail.com", "Joe's Window Washing", "https://share.google/xyz", "", "", "Anchorage", "Window Cleaning",
     "Window Washing", "Free webmail", "gmail.com"],
    ["slow@slowsite.com", "Slow Site Maids", "slowsite.com", "", "", "Anchorage", "Residential / Maid", "Home Cleaning",
     "Business domain", "slowsite.com"],
    # Same Business Name AND email as row 2: a duplicate, classified once
    ["hi@sparklemaids.com", "SPARKLE MAIDS", "http://sparklemaids.com/branch", "", "", "Eagle River", "Residential / Maid",
     "Home Cleaning", "Business domain", "sparklemaids.com"],
]


def _write_input(path: Path) -> Path:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        w.writerows(ROWS)
    return path


def _read(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_pipeline_fills_columns_and_writes_every_sheet(tmp_path: Path) -> None:
    in_csv = _write_input(tmp_path / "cleaning.csv")
    crawler = MockCleaningCrawler(tmp_path / "cache")
    telemetry = asyncio.run(CleaningLeadEnricher(crawler=crawler, concurrency_limit=2).process_leads(in_csv, second_pass=False))
    out = tmp_path / "output" / "cleaning"

    all_rows = _read(out / "all_leads.csv")
    assert [r["Business Name"] for r in all_rows] == [r[1] for r in ROWS]  # every row, order preserved
    assert list(all_rows[0])[: len(HEADERS)] == HEADERS  # original columns first, unchanged
    assert all(h in all_rows[0] for h in ENRICHMENT_HEADERS)
    assert all(r["Is Cleaning Company?"] in ("Yes", "No") for r in all_rows)
    assert all(";" not in r["Main Service"] for r in all_rows)  # one main service only

    by_name = {r["Business Name"]: r for r in all_rows}
    assert by_name["Alaska Exterior Cleaning"]["Main Service"] == "Roof Cleaning"
    assert by_name["Alaska Exterior Cleaning"]["Services"].startswith("Roof Cleaning; Gutter Cleaning; Window Cleaning")
    assert by_name["Sparkle Maids"]["Main Service"] == "Move-In / Move-Out Cleaning"
    assert by_name["Office Shine"]["Is Cleaning Company?"] == "No" and by_name["Office Shine"]["Services"] == ""
    assert by_name["Andrew's Carpet"]["Main Service"] == "Water Damage Restoration"  # crawled via its email domain
    assert "share.google" in by_name["Andrew's Carpet"]["Evidence_Summary"]
    assert by_name["Joe's Window Washing"]["Main Service"] == ""  # no website: nothing invented
    assert by_name["SPARKLE MAIDS"]["Main Service"] == by_name["Sparkle Maids"]["Main Service"]  # duplicate shares verdict

    qualified = {r["Business Name"] for r in _read(out / "qualified_leads.csv")}
    rejected = {r["Business Name"] for r in _read(out / "not_qualified_leads.csv")}
    review = {r["Business Name"] for r in _read(out / "uncertain_leads.csv")} | {r["Business Name"] for r in _read(out / "unreachable_leads.csv")}
    assert {"Alaska Exterior Cleaning", "Sparkle Maids", "Andrew's Carpet"} <= qualified
    assert "SPARKLE MAIDS" not in qualified  # duplicate listed once in the qualified sheet
    assert rejected == {"Office Shine"}
    assert {"Joe's Window Washing", "Slow Site Maids"} <= review
    assert len(_read(out / "duplicates.csv")) == 2
    assert len(_read(out / "classification_report.csv")) == len(ROWS)
    assert telemetry.validation["passed"]
    assert telemetry.validation["main_service_single_value"] and telemetry.validation["no_services_on_rejected"]
    assert crawler.calls.count("sparklemaids.com") == 1  # one crawl per domain
    assert "share.google" not in crawler.calls and "gmail.com" not in crawler.calls

    # Resume: nothing is re-crawled except the temporary failure
    crawler2 = MockCleaningCrawler(tmp_path / "cache")
    asyncio.run(CleaningLeadEnricher(crawler=crawler2).process_leads(in_csv, second_pass=False))
    assert crawler2.calls == ["slowsite.com"]


def test_existing_main_service_kept_when_website_shows_none(tmp_path: Path) -> None:
    in_csv = tmp_path / "in.csv"
    with open(in_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        w.writerow(["a@slowsite.com", "Slow Site Maids", "slowsite.com", "House Cleaning", "", "X", "", "", "Business domain", "slowsite.com"])
    asyncio.run(CleaningLeadEnricher(crawler=MockCleaningCrawler(tmp_path / "c")).process_leads(in_csv, second_pass=False))
    row = _read(tmp_path / "output" / "in" / "all_leads.csv")[0]
    assert row["Main Service"] == "House Cleaning"  # original value preserved
    assert row["Is Cleaning Company?"] == "Yes"
