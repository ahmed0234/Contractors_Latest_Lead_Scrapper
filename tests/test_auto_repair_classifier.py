"""Tests for the auto repair classifier (name, site profile, fusion) and its CSV pipeline."""

import asyncio
import csv
import json
from pathlib import Path

import pytest

from contractor_scraper.classification.auto_repair_classifier import (
    NO,
    T_COLLISION,
    T_DEALER,
    T_DEALER_OWNED,
    T_GENERAL,
    T_PARKING,
    T_RENTAL,
    T_TINT,
    T_TOWING,
    T_TRANSPORT,
    T_UNRELATED,
    YES,
    AutoRepairClassifier,
    LeadInfo,
    analyze_name,
    clean_dba,
    profile_site,
)
from contractor_scraper.discovery.crawler import (
    HOLIDAY_LINK_PROFILE,
    CrawledPage,
    CrawlResult,
    WebsiteCrawler,
    score_link,
)
from contractor_scraper.orchestration.auto_repair_enricher import (
    AUTO_REPAIR_LINK_PROFILE,
    ENRICHMENT_HEADERS,
    AutoRepairLeadEnricher,
)


def page(url: str, title: str, text: str, phones: list[str] | None = None) -> CrawledPage:
    return CrawledPage(url=url, status_code=200, title=title, text_content=text, phones=phones or [])


def site(domain: str, pages: list[CrawledPage]) -> CrawlResult:
    home, *rest = pages
    phones = [p for pg in pages for p in pg.phones]
    return CrawlResult(
        domain=domain,
        base_url=f"https://{domain}",
        is_accessible=True,
        homepage=home,
        subpages=rest,
        all_phones=phones,
        combined_text=" ".join(f"{p.title} {p.text_content}" for p in pages),
        pages_fetched=len(pages),
    )


def dead(domain: str, category: str = "DNS Failure") -> CrawlResult:
    return CrawlResult(domain=domain, base_url=f"https://{domain}", is_accessible=False, status_category=category, error_message="failed")


REPAIR_SHOP = site(
    "joesautorepair.com",
    [
        page(
            "https://joesautorepair.com/",
            "Joe's Auto Repair | Auto Repair in Ocala, FL",
            "Joe's Auto Repair | Family owned auto repair shop in Ocala since 1998 | ASE-certified technicians | "
            "Call (352) 555-0101 | 123 SW College Rd, Ocala, FL 34471 | Schedule an appointment | Skip the dealership",
            phones=["(352) 555-0101"],
        ),
        page(
            "https://joesautorepair.com/services",
            "Auto Repair Services",
            "Brake repair and replacement | Oil change and filter | Engine diagnostics and check engine light | "
            "Car A/C repair and recharge | Wheel alignment | Transmission service | Suspension and steering repair",
        ),
    ],
)

TOYOTA_DEALER = site(
    "lakelandtoyota.com",
    [
        page(
            "https://lakelandtoyota.com/",
            "Lakeland Toyota | New & Used Toyota Dealer in Lakeland FL",
            "Shop new inventory | Search used inventory | Schedule a test drive | Value your trade | "
            "Get pre-approved for car financing | Certified pre-owned vehicles | Toyota service center | "
            "Oil change specials | Brake service | Tire rotation | Dealer Inspire",
        ),
        page(
            "https://lakelandtoyota.com/service",
            "Toyota Service Department",
            "Schedule service | Oil change | Brake repair | Factory scheduled maintenance | Genuine Toyota parts",
        ),
    ],
)

TINT_SHOP = site(
    "tintpros.com",
    [
        page(
            "https://tintpros.com/",
            "Tint Pros | Window Tint, Wraps & Car Audio",
            "Automotive window tinting for cars, trucks and SUVs | Vinyl wraps for any vehicle | Car audio installation | "
            "Remote start for your car | Paint protection film | Ceramic window tint for your vehicle | Car window tint",
        ),
    ],
)

TOWING_CO = site(
    "bigtow.com",
    [
        page(
            "https://bigtow.com/",
            "Big Tow | 24/7 Towing & Roadside Assistance",
            "24/7 towing for cars and trucks | Flatbed towing | Roadside assistance | Jump starts | Lockouts | "
            "Vehicle recovery for any car, truck or SUV | Heavy duty wrecker service | We tow vehicles of all sizes",
        ),
    ],
)

ACCOUNTANT = site(
    "kpmg.com",
    [
        page(
            "https://kpmg.com/",
            "KPMG | Audit, Tax and Advisory",
            "KPMG provides audit, tax and advisory services | Our accounting professionals help clients | "
            "Tax services | Advisory services | Industries we serve: automotive, banking, healthcare | "
            + "Insights for leaders across sectors and markets. "
            * 40,
        ),
    ],
)


# ------------------------------------------------------------------------------------- name analysis
@pytest.mark.parametrize(
    ("business", "dba", "verdict", "btype"),
    [
        ("1365 AUTO CARE, LLC", "Midas Of Vero Beach", YES, None),
        ("BIG 10 TIRE STORES, LLC", "Pepboys Auto Service & Tires # 1522", YES, None),
        ("BRIDGESTONE RETAIL OPERATIONS, LLC", "Tires Plus #7065/239626", YES, None),
        ("JIM'S AUTOMOTIVE REPAIR, LLC", "", YES, T_GENERAL),
        ("CRASH WORKS, LLC", "", YES, T_COLLISION),
        ("NUWASH INC", "Nubrakes", YES, None),
        ("ROBERT L LIPTON, INC", "Lipton Toyota", NO, T_DEALER),
        ("FRLJ-MAZ, LLC", "Mazda Of North Miami", NO, T_DEALER),
        ("WALLACE AUTOMOTIVE GROUP", "", NO, T_DEALER),
        ("CORCYD LLC", "Tint World", NO, T_TINT),
        ("WEBB'S TOWING & RECOVERY SERVICES, LLC", "", NO, T_TOWING),
        ("1 POUND TRANSPORT LLC", "", NO, T_TRANSPORT),
        ("NORTHGATE LINCOLN - MERCURY, INC", "Parks Collision Center; Parks Lincoln Of Wesley Chapel", NO, T_DEALER_OWNED),
        ("MAURICE'S AUTO REPAIR AND TOWING SERVICE, INC.", "", YES, T_GENERAL),
        ("APEX FAMILY, LLC", "", "", None),
        ("GARAGE DOOR GUYS LLC", "", "", None),
        ("DOWNTOWN PARKING GARAGE LLC", "", NO, T_PARKING),
        ("PREMIER VALET SERVICES INC", "", NO, T_PARKING),
        ("SUNSHINE CAR RENTALS LLC", "", NO, T_RENTAL),
        ("EXPRESS HAULING & COURIER LLC", "", NO, T_TRANSPORT),
        ("ABC BUY HERE PAY HERE LLC", "", NO, T_DEALER),
        ("ORLANDO AUTO AUCTION INC", "", NO, T_DEALER),
        ("TRANSPORT TRUCK REPAIR LLC", "", YES, None),
    ],
)
def test_analyze_name(business: str, dba: str, verdict: str, btype: str | None) -> None:
    signal = analyze_name(business, dba)
    assert signal.verdict == verdict
    if btype:
        assert signal.business_type == btype


def test_clean_dba_strips_scraped_javascript() -> None:
    raw = 'Maus Nissan Of New Port Richey; 1; function validateInput() {; var bn = document.forms["form"];'
    assert clean_dba(raw) == "Maus Nissan Of New Port Richey"
    assert analyze_name("ZT AUTOMOTIVE OF TAMPA TWO, LLC", raw).business_type == T_DEALER


def test_known_chain_beats_holding_company_name() -> None:
    signal = analyze_name("AZAREL HOLDINGS INC", "Midas")
    assert signal.verdict == YES and signal.is_known_chain and signal.source == "dba name"


# ------------------------------------------------------------------------------------ site profiles
def test_repair_shop_profile() -> None:
    profile = profile_site(REPAIR_SHOP)
    assert profile.verdict == YES
    assert {"brakes", "oil", "ac", "alignment", "transmission"} <= set(profile.repair)
    # "Skip the dealership" is a comparison, not dealership evidence
    assert "dealer_word" not in profile.dealer


def test_franchise_dealer_profile_is_no_even_with_service_department() -> None:
    profile = profile_site(TOYOTA_DEALER)
    assert profile.verdict == NO and profile.site_type == T_DEALER
    assert profile.franchise_brand == "toyota"
    assert profile.repair_score > 0  # the service department exists but does not count


def test_tint_and_towing_only_profiles_are_no() -> None:
    assert profile_site(TINT_SHOP).site_type == T_TINT
    assert profile_site(TOWING_CO).site_type == T_TOWING


def test_corporate_site_with_passing_automotive_mention_is_not_automotive() -> None:
    profile = profile_site(ACCOUNTANT)
    assert profile.site_type == T_UNRELATED and not profile.is_automotive
    assert "accounting" in profile.industry


def test_negated_service_is_ignored() -> None:
    crawl = site("x.com", [page("https://x.com/", "X", "We do not offer brake repair or oil changes. Window tint only.")])
    assert "brakes" not in profile_site(crawl).repair


# ------------------------------------------------------------------------------------------- fusion
classifier = AutoRepairClassifier()


def test_repair_site_and_name_high_confidence_yes() -> None:
    res = classifier.classify(REPAIR_SHOP, LeadInfo(business_name="JOE'S AUTO REPAIR INC"))
    assert res.decision_label == YES and res.classification == YES
    assert res.confidence >= 0.9
    assert "brakes" in res.services_found and res.evidence_urls
    assert res.decision_basis == "Website + Name"
    assert res.short_reason.startswith("Yes: website lists") and not res.borderline


def test_row_website_is_accepted_without_ownership_check() -> None:
    # The name / phone / address never have to appear on the site: the row's website is the business's site
    res = classifier.classify(REPAIR_SHOP, LeadInfo(business_name="AZ HOLDINGS 22 LLC"))
    assert res.decision_label == YES and res.classification == YES
    assert res.decision_basis == "Website" and res.confidence >= 0.8
    assert not hasattr(res, "website_match")


def test_dealer_with_service_department_is_no() -> None:
    lead = LeadInfo(business_name="ROBERT L LIPTON, INC", dba_name="Lakeland Toyota")
    res = classifier.classify(TOYOTA_DEALER, lead)
    assert res.decision_label == NO and res.business_type == T_DEALER
    assert "service department does not count" in res.reasoning.lower()


def test_non_automotive_site_vs_weak_repair_name_is_borderline() -> None:
    lead = LeadInfo(business_name="1 UP AUTO WORX, LLC", dba_name="1 Up Auto Worx")
    res = classifier.classify(ACCOUNTANT, lead)
    assert res.decision_label == YES and res.confidence < 0.6
    assert res.classification == "Uncertain"  # needs review
    assert res.borderline and "conflict" in res.decision_basis
    assert "accounting" in res.short_reason and "review manually" in res.reasoning.lower()


def test_non_automotive_site_vs_strong_repair_name_is_flagged() -> None:
    res = classifier.classify(ACCOUNTANT, LeadInfo(business_name="SMITH COLLISION & AUTO BODY LLC"))
    assert res.decision_label == YES and res.borderline
    assert "review recommended" in res.reasoning.lower()


def test_known_chain_on_non_automotive_site_is_confident() -> None:
    lead = LeadInfo(business_name="BIG 10 TIRE STORES, LLC", dba_name="Pepboys Auto Service & Tires # 1522")
    res = classifier.classify(ACCOUNTANT, lead)
    assert res.decision_label == YES and res.classification == YES
    assert res.decision_basis == "Name (website conflicts)"


def test_non_automotive_site_without_repair_name_is_no() -> None:
    res = classifier.classify(ACCOUNTANT, LeadInfo(business_name="APEX FAMILY, LLC", registry="Motor Vehicle Repair"))
    assert res.decision_label == NO and res.business_type == T_UNRELATED
    assert res.classification == NO


def test_dead_site_name_only_goes_to_unreachable_bucket() -> None:
    lead = LeadInfo(business_name="SMITH COLLISION LLC")
    res = classifier.classify(dead("smithcollision.com"), lead)
    assert res.decision_label == YES and res.classification == "Unreachable"
    assert "does not resolve" in res.reasoning


def test_no_name_signal_uses_registry_default() -> None:
    lead = LeadInfo(business_name="APEX FAMILY, LLC", registry="Motor Vehicle Repair")
    res = classifier.classify(dead("apexfamily.com"), lead)
    assert res.decision_label == YES and res.decision_basis == "Registry Default" and res.confidence < 0.5


def test_brand_parent_site_with_known_chain() -> None:
    corporate = site(
        "bfusa.com",
        [
            page(
                "https://bfusa.com/",
                "Bridgestone Americas",
                "Our retail brands: Firestone Complete Auto Care, Tires Plus, Wheel Works and Hibdon Tires. Tires Plus locations.",
            )
        ],
    )
    lead = LeadInfo(business_name="BRIDGESTONE RETAIL OPERATIONS, LLC", dba_name="Tires Plus #7065/239626")
    res = classifier.classify(corporate, lead)
    assert res.decision_label == YES and res.classification == YES


def test_dealer_group_collision_center_counts_as_dealer() -> None:
    lead = LeadInfo(business_name="FERMAN MOTOR CAR COMPANY", dba_name="Ferman Collision Center")
    res = classifier.classify(TOYOTA_DEALER, lead)
    assert res.decision_label == NO and res.business_type == T_DEALER_OWNED


def test_verified_non_automotive_site_is_no() -> None:
    realty = site(
        "sunshinerealtygroup.com",
        [
            page(
                "https://sunshinerealtygroup.com/",
                "Sunshine Realty Group",
                "Sunshine Realty Group real estate agents. Homes for sale, property management and realtors serving Tampa. " * 12,
            )
        ],
    )
    res = classifier.classify(realty, LeadInfo(business_name="SUNSHINE REALTY GROUP LLC"))
    assert res.decision_label == NO and res.business_type == T_UNRELATED


def test_thin_site_never_proves_non_automotive() -> None:
    thin = site(
        "a1bodyandglass.com",
        [
            page(
                "https://a1bodyandglass.com/",
                "A1 Body and Glass",
                "A1 Body and Glass | Call 561-555-0100 | Locations | Home | About Us | Contact Us | Hours Monday to Friday 8 to 5 | "
                "Saturday by appointment | Serving our neighbors since 1995 | Copyright 2024 all rights reserved",
            )
        ],
    )
    res = classifier.classify(thin, LeadInfo(business_name="A1 BODY AND GLASS OF WEST BOCA, INC"))
    assert res.decision_label == YES and "too little content" in res.reasoning


def test_repair_shop_that_also_sells_cars_is_yes() -> None:
    shop = site(
        "cnccarsinc.com",
        [
            page(
                "https://cnccarsinc.com/",
                "C&C Auto Repair & Sales",
                "Complete auto repair | Brake repair | Engine diagnostics | Oil change | A/C repair | "
                "Pre-owned vehicles for every budget | Auto financing available | Apply for credit",
            )
        ],
    )
    res = classifier.classify(shop, LeadInfo(business_name="C & C AUTO REPAIR OF PINELLAS PARK, INC"))
    assert res.decision_label == YES and res.borderline


def test_non_automotive_site_of_unknown_industry_is_low_confidence_no() -> None:
    crawl = site(
        "airmaticgarage.com",
        [page("https://airmaticgarage.com/", "Airmatic", "Welcome to our store. Shop our collection of products. " * 30)],
    )
    res = classifier.classify(crawl, LeadInfo(business_name="AIRMATICGARAGE LLC"))
    assert res.decision_label == NO and res.classification == "Uncertain" and res.borderline


def test_parking_site_is_no() -> None:
    parking = site(
        "cityparkingllc.com",
        [
            page(
                "https://cityparkingllc.com/",
                "City Parking | Monthly Parking & Valet Parking in Miami",
                "Reserve your parking online | Monthly parking for residents | Valet parking for events and hotels | "
                "Parking garages downtown | Parking management services for property owners | Airport parking shuttle",
            )
        ],
    )
    profile = profile_site(parking)
    assert profile.site_type == T_PARKING and profile.verdict == NO
    res = classifier.classify(parking, LeadInfo(business_name="CITY PARKING LLC"))
    assert res.decision_label == NO and res.classification == NO


def test_freight_site_is_transport_not_unrelated() -> None:
    freight = site(
        "fastfreightlines.com",
        [
            page(
                "https://fastfreightlines.com/",
                "Fast Freight Lines | Trucking & Logistics",
                "Full truckload freight across the Southeast | Logistics solutions | Dispatch available 24/7 | "
                "Now hiring CDL drivers and owner-operators | Request a freight quote",
            )
        ],
    )
    assert profile_site(freight).site_type == T_TRANSPORT
    res = classifier.classify(freight, LeadInfo(business_name="FAST FREIGHT LINES LLC"))
    assert res.decision_label == NO


def test_repair_shop_mentioning_parking_and_towing_is_still_yes() -> None:
    crawl = site(
        "eastsideauto.com",
        [
            page(
                "https://eastsideauto.com/",
                "Eastside Auto Repair",
                "Complete auto repair | Brake repair | Oil change | Free customer parking lot | Towing available | "
                "Engine diagnostics | Wheel alignment | Our certified technicians",
            )
        ],
    )
    res = classifier.classify(crawl, LeadInfo(business_name="EASTSIDE AUTO REPAIR LLC"))
    assert res.decision_label == YES and res.classification == YES


# --------------------------------------------------------------------------------------- link profile
def test_holiday_link_profile_scores_unchanged() -> None:
    assert score_link("/christmas-lights") == 100
    assert score_link("/outdoor-lighting") == 60
    assert score_link("/services") == 40
    assert score_link("/contact") == 20
    assert HOLIDAY_LINK_PROFILE.fast_path_exclusions


def test_auto_repair_link_profile() -> None:
    assert AUTO_REPAIR_LINK_PROFILE.score("/brake-repair") == 100
    assert AUTO_REPAIR_LINK_PROFILE.score("/contact-us") == 60
    assert AUTO_REPAIR_LINK_PROFILE.score("/new-vehicles") == 40
    assert AUTO_REPAIR_LINK_PROFILE.score("/blog/holiday-party") == 0
    assert not AUTO_REPAIR_LINK_PROFILE.fast_path_exclusions


# ------------------------------------------------------------------------------------ CSV pipeline
class MockAutoCrawler(WebsiteCrawler):
    """Deterministic crawler: no network, counts crawls per domain."""

    SITES = {
        "joesautorepair.com": REPAIR_SHOP,
        "lakelandtoyota.com": TOYOTA_DEALER,
        "kpmg.com": ACCOUNTANT,
    }

    def __init__(self, cache_dir: Path) -> None:
        super().__init__(cache_dir=cache_dir)
        self.calls: list[str] = []

    async def crawl_domain(self, base_url: str, extra_seed_urls: list[str] | None = None) -> CrawlResult:
        domain = base_url.split("://", 1)[-1].split("/")[0].replace("www.", "")
        self.calls.append(domain)
        return self.SITES.get(domain) or dead(domain, "Timeout")


HEADERS = [
    "industry",
    "dba_name",
    "Business_Name",
    "Email",
    "Website",
    "Is Auto Repair ?",
    "City",
    "Phone",
    "address1",
    "zip",
    "license_type",
]
ROWS = [
    [
        "auto_repair",
        "",
        "JOE'S AUTO REPAIR INC",
        "joe@joesautorepair.com",
        "joesautorepair.com",
        "",
        "Ocala",
        "352-555-0101",
        "123 SW COLLEGE RD",
        "34471",
        "Motor Vehicle Repair",
    ],
    [
        "auto_repair",
        "Lakeland Toyota",
        "ROBERT L LIPTON, INC",
        "sales@lakelandtoyota.com",
        "lakelandtoyota.com",
        "",
        "Lakeland",
        "863-555-0000",
        "1 MAIN ST",
        "33801",
        "Motor Vehicle Repair",
    ],
    [
        "auto_repair",
        "Pepboys Auto Service & Tires # 1522",
        "BIG 10 TIRE STORES, LLC",
        "a@kpmg.com",
        "kpmg.com",
        "",
        "Miami",
        "305-555-0000",
        "9 NW 7TH ST",
        "33101",
        "Motor Vehicle Repair",
    ],
    [
        "auto_repair",
        "1 Up Auto Worx",
        "1 UP AUTO WORX, LLC",
        "b@kpmg.com",
        "kpmg.com",
        "",
        "Port Charlotte",
        "941-276-4365",
        "4780 TAMIAMI TRL",
        "33980",
        "Motor Vehicle Repair",
    ],
    [
        "auto_repair",
        "",
        "SLOW SITE COLLISION LLC",
        "c@slowsite.com",
        "slowsite.com",
        "",
        "Tampa",
        "813-555-0000",
        "5 A ST",
        "33602",
        "Motor Vehicle Repair",
    ],
    ["auto_repair", "", "NO WEBSITE TIRE LLC", "", "", "", "Tampa", "", "", "", "Motor Vehicle Repair"],
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


def test_pipeline_per_row_verdicts_preserve_columns_and_resume(tmp_path: Path) -> None:
    (tmp_path / "input").mkdir()
    in_csv = _write_input(tmp_path / "input" / "leads.csv")
    crawler = MockAutoCrawler(tmp_path / "cache")
    enricher = AutoRepairLeadEnricher(crawler=crawler, concurrency_limit=2)
    telemetry = asyncio.run(enricher.process_leads(in_csv, second_pass=False))

    out_dir = tmp_path / "output" / "leads"  # <folder>/input/x.csv -> <folder>/output/x/
    all_rows = _read(out_dir / "all_leads.csv")
    assert [r["Business_Name"] for r in all_rows] == [r[2] for r in ROWS]  # order preserved
    assert list(all_rows[0])[: len(HEADERS)] == HEADERS  # original columns first, unchanged
    assert all(h in all_rows[0] for h in ENRICHMENT_HEADERS)
    assert all(r["Is Auto Repair ?"] in ("Yes", "No") for r in all_rows)

    by_name = {r["Business_Name"]: r for r in all_rows}
    assert by_name["JOE'S AUTO REPAIR INC"]["Is Auto Repair ?"] == "Yes"
    assert by_name["ROBERT L LIPTON, INC"]["Is Auto Repair ?"] == "No"
    # Same domain (kpmg.com), different businesses, different handling
    assert by_name["BIG 10 TIRE STORES, LLC"]["Qualification_Status"] == "Qualified"
    assert by_name["1 UP AUTO WORX, LLC"]["Qualification_Status"] == "Needs Review"
    assert by_name["NO WEBSITE TIRE LLC"]["Crawl_Status"].startswith("Missing URL")
    assert crawler.calls.count("kpmg.com") == 1  # crawled once for both rows
    assert "Website_Match" not in all_rows[0]  # no ownership verification
    # No duplicates in this input: the sheet exists with just its header
    assert _read(out_dir / "duplicates.csv") == []
    report_rows = _read(out_dir / "classification_report.csv")
    assert [r["Business_Name"] for r in report_rows] == [r[2] for r in ROWS]
    assert all(r["Reason"].startswith(("Yes:", "No:")) for r in report_rows)
    assert {r["Borderline"] for r in report_rows} <= {"Yes", "No"}

    assert telemetry.validation["passed"]
    assert telemetry.validation["decision_column_filled"]
    report = json.loads((out_dir / "run_report.json").read_text())
    assert report["classifier_version"]

    # Resume: completed sites are restored from the checkpoint; only the timed-out site is retried
    crawler2 = MockAutoCrawler(tmp_path / "cache")
    telemetry2 = asyncio.run(AutoRepairLeadEnricher(crawler=crawler2).process_leads(in_csv, second_pass=False))
    assert crawler2.calls == ["slowsite.com"]
    assert telemetry2.websites_from_checkpoint == 3
    assert [r["Is Auto Repair ?"] for r in _read(out_dir / "all_leads.csv")] == [r["Is Auto Repair ?"] for r in all_rows]


# ------------------------------------------------------------------- regressions from the 50-row trial
def test_placeholder_page_is_not_an_unrelated_business() -> None:
    crawl = site("1poundtransport.com", [page("https://1poundtransport.com/", "", "COMING SOON")])
    res = classifier.classify(crawl, LeadInfo(business_name="1 POUND TRANSPORT LLC"))
    assert res.website_status == "Placeholder Page" and not res.site_accessible
    assert res.classification == "Unreachable" and res.business_type != T_UNRELATED

    hosting = site(
        "accuratecollision.net",
        [
            page(
                "https://accuratecollision.net/",
                "Web Hosting by InMotion Hosting",
                "Website coming soon! Stay tuned. This page belongs to a member of the InMotion Hosting network.",
            )
        ],
    )
    res = classifier.classify(hosting, LeadInfo(business_name="ACCURATE COLLISION OF POMPANO, INC."))
    assert res.website_status == "Placeholder Page"
    assert res.decision_label == YES and "placeholder" in res.reasoning


def test_bot_challenge_page_is_unreadable() -> None:
    crawl = site(
        "312customs.com",
        [page("https://312customs.com/", "Checking your browser...", "Checking your browser | This will only take a few seconds...")],
    )
    res = classifier.classify(crawl, LeadInfo(business_name="312 CUSTOMS LLC"))
    assert res.website_status == "Bot Challenge" and "bot protection" in res.reasoning


def test_tint_shop_with_generic_staff_wording_is_no() -> None:
    tint = site(
        "solartintinc.com",
        [
            page(
                "https://solartintinc.com/",
                "Best Window Tinting in Miami for Your Home, Business & Car",
                "Automotive window tinting | Vehicle wraps | 3M paint protection film | Ceramic window tint for your car | "
                "Car care goes beyond routine maintenance | Our certified technicians protect any vehicle | Car window tinting",
            )
        ],
    )
    profile = profile_site(tint)
    assert profile.site_type == T_TINT and profile.verdict == NO


def test_used_parts_site_is_parts_retail() -> None:
    parts = site(
        "ecogautoparts.com",
        [
            page(
                "https://ecogautoparts.com/",
                "Eco Green Auto Parts",
                "Quality used auto parts for every vehicle | Find used parts for your car or truck | Dismantling | Our logistics team ships parts for any car",
            )
        ],
    )
    assert profile_site(parts).site_type == "Auto Parts Retail"


def test_aaa_car_care_is_known_chain() -> None:
    signal = analyze_name("AAA CAR CARE CENTERS, LLC")
    assert signal.verdict == YES and signal.is_known_chain


def test_parent_domain_for_dead_email_subdomain() -> None:
    assert AutoRepairLeadEnricher._parent_domain("acg.aaa.com") == "aaa.com"
    assert AutoRepairLeadEnricher._parent_domain("joesauto.com") == ""
    assert AutoRepairLeadEnricher._parent_domain("shop.example.co.uk") == ""


def test_duplicates_classified_once_and_listed_in_duplicates_sheet(tmp_path: Path) -> None:
    joe = ROWS[0]
    rows = [
        joe,
        ROWS[1],
        # same Business_Name + same Email (case / punctuation differ): duplicate of row 1; it has a dba,
        # so it is the row the group is classified from
        [joe[0], "Joe's Collision", "Joe's Auto Repair, Inc", "JOE@joesautorepair.com ", *joe[4:7], "352-555-9999", "77 OTHER ST", *joe[9:]],
        list(joe),  # exact copy: also a duplicate, kept in the outputs
        # same name, different email: NOT a duplicate
        [*joe[:3], "other@joesautorepair.com", *joe[4:]],
        ROWS[2],
        ROWS[2],
    ]
    in_csv = tmp_path / "leads.csv"
    with open(in_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        w.writerows(rows)

    crawler = MockAutoCrawler(tmp_path / "cache")
    enricher = AutoRepairLeadEnricher(crawler=crawler, concurrency_limit=2)
    out_dir = tmp_path / "out"
    telemetry = asyncio.run(enricher.process_leads(in_csv, output_dir=out_dir, second_pass=False))

    all_rows = _read(out_dir / "all_leads.csv")
    assert len(all_rows) == len(rows)  # every row kept, exact copies included
    assert [r["Is Auto Repair ?"] for r in all_rows] == ["Yes", "No", "Yes", "Yes", "Yes", "Yes", "Yes"]
    assert all_rows[0]["Evidence_Summary"] == all_rows[2]["Evidence_Summary"] == all_rows[3]["Evidence_Summary"]

    dups = _read(out_dir / "duplicates.csv")
    assert [r["Phone"] for r in dups] == [joe[7], "352-555-9999", joe[7], ROWS[2][7], ROWS[2][7]]
    assert [r["Duplicate_Group"] for r in dups] == ["1", "1", "1", "2", "2"]
    assert [r["Duplicate_Count"] for r in dups] == ["3", "3", "3", "2", "2"]
    assert [r["Duplicate_Classified_Row"] for r in dups] == ["4", "4", "4", "7", "7"]  # spreadsheet rows
    assert all("is our niche company ?" in r for r in dups)
    assert telemetry.duplicate_groups == 2 and telemetry.duplicate_rows == 5

    # qualified_leads.csv: each duplicate group once (first occurrence); same name + different email stays
    qualified = _read(out_dir / "qualified_leads.csv")
    assert [(r["Business_Name"], r["Email"]) for r in qualified] == [
        (joe[2], joe[3]),
        (joe[2], "other@joesautorepair.com"),
        (ROWS[2][2], ROWS[2][3]),
    ]
    assert telemetry.qualified_duplicates_removed == 3
    assert telemetry.validation["passed"] and telemetry.validation["duplicate_rows_share_verdict"]
    assert telemetry.validation["duplicates_sheet_rows"] == 5

    report = _read(out_dir / "classification_report.csv")
    assert [r["Duplicate_Group"] for r in report] == ["1", "", "1", "1", "", "2", "2"]
    assert [r["Input_Row"] for r in report] == [str(i) for i in range(2, 2 + len(rows))]

    # Each group classified once (input data indexes: group 1 via row 2, group 2 via row 5)
    run_report = json.loads((out_dir / "run_report.json").read_text())
    assert run_report["totals"]["duplicate_groups"] == 2
    restored = [json.loads(line) for line in (out_dir / "crawl_checkpoint.jsonl").read_text().splitlines()]
    classified = {idx for e in restored for idx in e["results"]}
    assert classified == {"1", "2", "4", "5"}


def test_duplicate_group_is_classified_from_its_most_informative_row() -> None:
    from contractor_scraper.orchestration.niche_enricher import EnrichmentLead

    def lead(i: int, dba: str) -> EnrichmentLead:
        return EnrichmentLead(i, [], "FRANK'S DETAIL, INC.", "", "", {"business": "FRANK'S DETAIL, INC.", "dba": dba})

    group = [lead(0, "Fd Reconditioning; Fd Reconditioning Specialist"), lead(1, "Fd Collision; Fd Reconditioning"), lead(2, "")]
    assert AutoRepairLeadEnricher(crawler=MockAutoCrawler(Path("unused")))._duplicate_representative(group).index == 1
