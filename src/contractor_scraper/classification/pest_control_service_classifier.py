"""High-precision classification engine for Pest Control service providers.

Evaluates crawled website content (homepage, services pages, about, contact)
to determine whether a company genuinely provides active pest control services
(residential/commercial contractors, exterminators, treatment technicians)
vs. product manufacturers, chemical/equipment distributors, retail/e-commerce stores,
software platforms, lead directories, or unrelated businesses.
"""

from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from contractor_scraper.discovery.crawler import CrawlResult


# Specific multi-word service phrases indicating actual on-site pest control services
PEST_SERVICE_OFFERING_PHRASES: tuple[str, ...] = (
    # Core general services
    "residential pest control",
    "commercial pest control",
    "industrial pest control",
    "general pest control",
    "household pest control",
    "pest control service",
    "pest control services",
    "pest management service",
    "pest management services",
    "pest prevention service",
    "pest prevention services",
    "extermination service",
    "extermination services",
    "professional exterminator",
    "professional exterminators",
    "licensed exterminator",
    # Termite & WDO services
    "termite treatment",
    "termite treatments",
    "termite control",
    "termite inspection",
    "termite inspections",
    "termite protection",
    "subterranean termite",
    "drywood termite",
    "tent fumigation",
    "structural fumigation",
    "fumigation service",
    "fumigation services",
    "no tent termite",
    "wood destroying organism",
    "wdo inspection",
    "wdo inspections",
    "wdo report",
    # Bed bug treatments
    "bed bug treatment",
    "bed bug treatments",
    "bed bug removal",
    "bed bug extermination",
    "bed bug heat treatment",
    "heat treatment for bed bugs",
    # Rodent & Wildlife
    "rodent control",
    "rodent removal",
    "rodent exclusion",
    "rodent proofing",
    "rat control",
    "rat extermination",
    "mice control",
    "mouse extermination",
    "wildlife removal",
    "wildlife control",
    "animal trapping",
    "humane wildlife removal",
    "squirrel removal",
    "raccoon removal",
    "bat removal",
    # Mosquito & outdoor
    "mosquito control",
    "mosquito treatment",
    "mosquito treatments",
    "mosquito misting",
    "mosquito fogging",
    "mosquito reduction",
    "lawn pest control",
    "perimeter pest control",
    "perimeter barrier treatment",
    # Insect specific
    "cockroach control",
    "roach control",
    "roach extermination",
    "german cockroach",
    "ant control",
    "fire ant control",
    "fire ant treatment",
    "carpenter ant",
    "spider control",
    "wasp removal",
    "bee removal",
    "hornet removal",
    "flea and tick control",
    "flea treatment",
    "tick treatment",
    # Maintenance & IPM
    "quarterly pest control",
    "annual termite warranty",
    "integrated pest management",
)

# Operational phrases that indicate a contractor sends technicians to service properties
OPERATIONAL_CONTRACTOR_SIGNALS: tuple[str, ...] = (
    "our technicians",
    "licensed technician",
    "licensed technicians",
    "certified technician",
    "certified technicians",
    "trained technician",
    "trained technicians",
    "pest professional",
    "pest professionals",
    "schedule an inspection",
    "schedule your inspection",
    "free inspection",
    "free home inspection",
    "book an inspection",
    "schedule service",
    "schedule a service",
    "request a quote",
    "request an estimate",
    "free estimate",
    "get a quote",
    "get a free estimate",
    "service area",
    "areas we serve",
    "proudly serving",
    "counties served",
    "service plan",
    "service plans",
    "treatment plan",
    "treatment plans",
    "protect your home",
    "protecting your home",
    "treat your home",
    "treat your property",
    "on-site inspection",
    "same-day service",
    "emergency pest control",
    "locally owned and operated",
    "family owned and operated",
    "satisfaction guarantee",
    "money back guarantee",
)

# Disqualifying retail / e-commerce phrases (selling products rather than doing service)
DISQUALIFYING_RETAIL_PHRASES: tuple[str, ...] = (
    "add to cart",
    "shopping cart",
    "view cart",
    "proceed to checkout",
    "item(s) in cart",
    "buy online",
    "shop online",
    "shop now",
    "free shipping on orders",
    "order online",
    "diy pest control",
    "do it yourself pest",
    "pest control products for sale",
    "sprayers for sale",
    "insecticide concentrates",
    "wholesale chemicals",
    "chemical distributor",
    "pest supply store",
    "browse our store",
    "product catalog",
    "traps for sale",
    "bait stations for sale",
)

# Disqualifying software / SaaS phrases
DISQUALIFYING_TECH_PHRASES: tuple[str, ...] = (
    "pest control software",
    "software for pest control",
    "pest control crm",
    "field service management software",
    "pest management software",
    "route optimization software",
    "routing and scheduling software",
    "book a demo",
    "request a demo",
    "pricing per user",
    "pricing per technician",
    "saas platform",
    "cloud-based software for",
)

# Disqualifying directory / lead generator phrases
DISQUALIFYING_DIRECTORY_PHRASES: tuple[str, ...] = (
    "find local exterminators",
    "find pest control near you",
    "top 10 exterminators",
    "top 10 pest control companies",
    "compare pest control quotes",
    "directory of exterminators",
    "directory of pest control",
    "hire a pest control pro",
    "get quotes from local exterminators",
)

# Disqualifying manufacturer / OEM phrases
DISQUALIFYING_MANUFACTURER_PHRASES: tuple[str, ...] = (
    "we manufacture",
    "manufacturer of pest",
    "equipment manufacturer",
    "chemical manufacturer",
    "chemical manufacturing",
    "formulator of",
    "oem supplier",
)


@dataclass
class PestControlClassificationResult:
    """Detailed verdict indicating if a business is a verified pest control service contractor."""

    is_pest_control: bool
    confidence: float
    decision_label: str  # "Yes" or "No"
    evidence_phrases: list[str] = field(default_factory=list)
    operational_signals: list[str] = field(default_factory=list)
    disqualifiers: list[str] = field(default_factory=list)
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_pest_control": self.is_pest_control,
            "decision_label": self.decision_label,
            "confidence": self.confidence,
            "evidence_phrases": self.evidence_phrases,
            "operational_signals": self.operational_signals,
            "disqualifiers": self.disqualifiers,
            "reasoning": self.reasoning,
        }


class PestControlServiceClassifier:
    """Examines crawled website pages to verify active pest control contractor operations."""

    def evaluate_crawl(
        self,
        crawl: CrawlResult,
        company_name: str | None = None,
    ) -> PestControlClassificationResult:
        """Classify website based on crawled content across homepage and subpages."""
        if not crawl.is_accessible:
            return PestControlClassificationResult(
                is_pest_control=False,
                confidence=0.0,
                decision_label="No",
                reasoning=f"Website inaccessible: {crawl.error_message or 'Connection failed'}",
            )

        text_lower = (crawl.combined_text or "").lower()
        homepage_text = ""
        if crawl.homepage:
            homepage_text = f"{crawl.homepage.title} {crawl.homepage.meta_description} {crawl.homepage.text_content}".lower()

        # Check subpages specifically for services pages
        services_text = ""
        for sp in crawl.subpages:
            sp_url = sp.url.lower()
            if any(k in sp_url for k in ["/service", "/pest", "/termite", "/treatment", "/residential", "/commercial"]):
                services_text += f" {sp.title} {sp.text_content}".lower()

        full_corpus = f"{homepage_text} {services_text} {text_lower}"

        # 1. Identify disqualifying negative signals
        found_retail: list[str] = [
            phrase for phrase in DISQUALIFYING_RETAIL_PHRASES if phrase in full_corpus
        ]
        found_tech: list[str] = [
            phrase for phrase in DISQUALIFYING_TECH_PHRASES if phrase in full_corpus
        ]
        found_dir: list[str] = [
            phrase for phrase in DISQUALIFYING_DIRECTORY_PHRASES if phrase in full_corpus
        ]
        found_mfg: list[str] = [
            phrase for phrase in DISQUALIFYING_MANUFACTURER_PHRASES if phrase in full_corpus
        ]

        all_disqualifiers = found_retail + found_tech + found_dir + found_mfg

        # 2. Identify positive service offering phrases
        found_services: list[str] = [
            phrase for phrase in PEST_SERVICE_OFFERING_PHRASES if phrase in full_corpus
        ]

        # 3. Identify operational contractor signals (technicians, inspections, service areas)
        found_operations: list[str] = [
            phrase for phrase in OPERATIONAL_CONTRACTOR_SIGNALS if phrase in full_corpus
        ]

        # 4. Strict disqualifier handling
        # A. Software platform
        if found_tech and not found_operations:
            return PestControlClassificationResult(
                is_pest_control=False,
                confidence=0.1,
                decision_label="No",
                disqualifiers=found_tech,
                reasoning=f"Identified as software / tech provider: {found_tech[:3]}",
            )

        # B. Lead Directory or Review Portal
        if found_dir and not found_operations:
            return PestControlClassificationResult(
                is_pest_control=False,
                confidence=0.1,
                decision_label="No",
                disqualifiers=found_dir,
                reasoning=f"Identified as directory / lead portal: {found_dir[:3]}",
            )

        # C. Chemical / Equipment Manufacturer
        if found_mfg and len(found_services) < 2 and not found_operations:
            return PestControlClassificationResult(
                is_pest_control=False,
                confidence=0.1,
                decision_label="No",
                disqualifiers=found_mfg,
                reasoning=f"Identified as manufacturer / distributor: {found_mfg[:3]}",
            )

        # D. E-Commerce / DIY Retail Store
        # If shopping cart / checkout is present and operational technician signals are absent
        if len(found_retail) >= 2 and not found_operations:
            return PestControlClassificationResult(
                is_pest_control=False,
                confidence=0.15,
                decision_label="No",
                disqualifiers=found_retail,
                reasoning=f"Identified as pest product retail / e-commerce store: {found_retail[:3]}",
            )

        # 5. Service qualification scoring
        # A contractor qualifies if:
        # Rule 1: At least 2 distinct pest service offering phrases AND at least 1 operational contractor signal
        if len(found_services) >= 2 and len(found_operations) >= 1:
            confidence = min(0.98, 0.75 + (len(found_services) * 0.04) + (len(found_operations) * 0.03))
            return PestControlClassificationResult(
                is_pest_control=True,
                confidence=round(confidence, 3),
                decision_label="Yes",
                evidence_phrases=found_services[:8],
                operational_signals=found_operations[:5],
                disqualifiers=all_disqualifiers,
                reasoning=(
                    f"Strong service provider evidence: {len(found_services)} service offerings "
                    f"and {len(found_operations)} operational contractor signals."
                ),
            )

        # Rule 2: Substantial dedicated service offerings (>= 3 distinct treatments, e.g. termite, bed bug, rodent)
        if len(found_services) >= 3:
            confidence = min(0.92, 0.70 + (len(found_services) * 0.05))
            return PestControlClassificationResult(
                is_pest_control=True,
                confidence=round(confidence, 3),
                decision_label="Yes",
                evidence_phrases=found_services[:8],
                operational_signals=found_operations,
                disqualifiers=all_disqualifiers,
                reasoning=(
                    f"Multiple specific treatment services offered: {found_services[:4]}"
                ),
            )

        # Rule 3: Single strong service offering combined with prominent operational signals (>= 2 operations)
        if len(found_services) >= 1 and len(found_operations) >= 2:
            confidence = 0.85
            return PestControlClassificationResult(
                is_pest_control=True,
                confidence=confidence,
                decision_label="Yes",
                evidence_phrases=found_services,
                operational_signals=found_operations[:5],
                disqualifiers=all_disqualifiers,
                reasoning=(
                    f"Service offering ({found_services[0]}) with active contractor operations "
                    f"({found_operations[:2]})."
                ),
            )

        # Rule 4: Exterminator branding with explicit local residential / commercial protection
        has_exterminator = "exterminator" in full_corpus or "exterminating" in full_corpus
        has_pest_control = "pest control" in full_corpus
        if (has_exterminator or has_pest_control) and len(found_operations) >= 1:
            # Check if there is also general insect / pest mentions
            insect_matches = re.findall(
                r"\b(termites?|bed\s*bugs?|roaches?|cockroaches?|ants?|rodents?|rats?|mice|mosquitoes?|wasps?|fleas?|ticks?)\b",
                full_corpus,
            )
            if len(set(insect_matches)) >= 2:
                return PestControlClassificationResult(
                    is_pest_control=True,
                    confidence=0.80,
                    decision_label="Yes",
                    evidence_phrases=list(set(insect_matches))[:5],
                    operational_signals=found_operations[:3],
                    disqualifiers=all_disqualifiers,
                    reasoning=(
                        f"Active pest extermination operations targeting {list(set(insect_matches))[:3]} "
                        f"with service operations ({found_operations[:2]})."
                    ),
                )

        # Disqualified / Insufficient evidence
        return PestControlClassificationResult(
            is_pest_control=False,
            confidence=0.25 if found_services else 0.0,
            decision_label="No",
            evidence_phrases=found_services,
            operational_signals=found_operations,
            disqualifiers=all_disqualifiers,
            reasoning=(
                "Insufficient evidence that entity provides active on-site pest control services. "
                f"Found {len(found_services)} service phrases and {len(found_operations)} operational signals."
            ),
        )
