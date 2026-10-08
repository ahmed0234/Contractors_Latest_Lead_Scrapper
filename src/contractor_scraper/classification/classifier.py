"""Modular, high-precision industry classification architecture.

Determines whether an entity genuinely provides active contractor services
based on strong signals, supporting context, negative exclusions, and composite scoring.
"""

import re
from abc import ABC, abstractmethod
from typing import Any

from contractor_scraper.classification.base import ClassificationResult
from contractor_scraper.core.enums import ContractorCategory
from contractor_scraper.normalization.cleaners import clean_company_name


class BaseCategoryClassifier(ABC):
    """Abstract base classifier defining the rules engine for trade verification."""

    @property
    @abstractmethod
    def category(self) -> ContractorCategory:
        """Target contractor trade category."""
        ...

    @property
    @abstractmethod
    def strong_positive_terms(self) -> list[str]:
        """Specific multi-word phrases or definitive terms indicating service providers."""
        ...

    @property
    @abstractmethod
    def supporting_terms(self) -> list[str]:
        """Secondary terms that increase confidence when combined with core trade terms."""
        ...

    @property
    @abstractmethod
    def negative_terms(self) -> list[str]:
        """Terms indicating suppliers, manufacturers, tech, professional services, or retail."""
        ...

    @property
    @abstractmethod
    def exclusion_patterns(self) -> list[re.Pattern]:
        """Regex patterns identifying false positives, unrelated phrases, or hospitality names."""
        ...

    def evaluate(
        self,
        company_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> ClassificationResult:
        """Classify company name and metadata into an authoritative trade determination."""
        raw_name = company_name.strip()
        cleaned_name = clean_company_name(raw_name).lower()
        metadata = metadata or {}

        # 1. Check strict regex exclusion patterns
        for pattern in self.exclusion_patterns:
            if pattern.search(cleaned_name):
                return ClassificationResult(
                    target_category=self.category,
                    is_match=False,
                    confidence=0.0,
                    exclusion_keywords=[pattern.pattern],
                    reasoning=f"Matched negative phrase pattern: '{pattern.pattern}'",
                )

        # 2. Check negative / exclusion terms (Suppliers, Wholesale, Tech, Legal, etc.)
        matched_negatives = []
        # Word boundary search for negative keywords
        for neg in self.negative_terms:
            if re.search(rf"\b{re.escape(neg)}\b", cleaned_name):
                matched_negatives.append(neg)

        if matched_negatives:
            return ClassificationResult(
                target_category=self.category,
                is_match=False,
                confidence=0.0,
                exclusion_keywords=matched_negatives,
                reasoning=f"Excluded due to non-contractor industry terms: {matched_negatives}",
            )

        # 3. Check strong positive signals
        matched_strong = []
        for strong in self.strong_positive_terms:
            if strong in cleaned_name:
                matched_strong.append(strong)

        # 4. Check supporting terms
        matched_supporting = []
        for supp in self.supporting_terms:
            if re.search(rf"\b{re.escape(supp)}\b", cleaned_name):
                matched_supporting.append(supp)

        # 5. Composite scoring logic
        confidence = 0.0
        reasoning = ""

        if matched_strong:
            # Strong contractor phrase match (e.g. "roofing contractor", "roof repairs", "roofing")
            confidence = 0.90
            if matched_supporting:
                confidence = min(0.98, confidence + 0.05)
            reasoning = f"Strong contractor indicators found: {matched_strong}"

        elif matched_supporting:
            # Has trade mention plus supporting contractor context
            if len(matched_supporting) >= 2:
                confidence = 0.75
                reasoning = f"Multiple supporting contractor trade signals: {matched_supporting}"
            else:
                confidence = 0.40
                reasoning = (
                    f"Only isolated weak term found without strong context: {matched_supporting}"
                )
        else:
            confidence = 0.0
            reasoning = "No relevant trade keywords found"

        # Threshold for high-precision contractor classification
        is_match = confidence >= 0.75

        return ClassificationResult(
            target_category=self.category,
            is_match=is_match,
            confidence=confidence,
            matching_keywords=matched_strong + matched_supporting,
            exclusion_keywords=[],
            reasoning=reasoning,
        )


class RoofingClassifier(BaseCategoryClassifier):
    """Production-grade high-precision classifier for Roofing contractors."""

    @property
    def category(self) -> ContractorCategory:
        return ContractorCategory.ROOFING

    @property
    def strong_positive_terms(self) -> list[str]:
        return [
            "roofing contractor",
            "roof repair",
            "roof replacement",
            "roof restoration",
            "roof inspection",
            "roof maintenance",
            "roof installation",
            "roofing service",
            "roofing system",
            "roofing group",
            "roofing pro",
            "roofing specialist",
            "roofing expert",
            "roofing master",
            "commercial roofing",
            "residential roofing",
            "metal roofing",
            "tile roofing",
            "shingle roofing",
            "flat roofing",
            "re-roofing",
            "reroofing",
            "re roof",
            "re-roof",
            "roofing",
        ]

    @property
    def supporting_terms(self) -> list[str]:
        return [
            "roof",
            "roofs",
            "shingles",
            "gutters",
            "flashing",
            "waterproofing",
            "contractor",
            "contractors",
            "repairs",
            "construction",
            "exteriors",
            "builders",
        ]

    @property
    def negative_terms(self) -> list[str]:
        return [
            # Materials, Wholesale & Suppliers
            "supply",
            "supplies",
            "materials",
            "distributor",
            "distributors",
            "wholesale",
            "manufacturing",
            "manufacturer",
            "hardware",
            "lumber",
            "building products",
            # Technology, Software & Media
            "software",
            "technologies",
            "technology",
            "tech",
            "app",
            "apps",
            "media",
            "marketing",
            # Equipment & Rentals
            "equipment",
            "rental",
            "rentals",
            "tools",
            "machinery",
            # Legal, Real Estate & Financial
            "law",
            "legal",
            "attorney",
            "consulting",
            "accounting",
            "tax",
            "holding",
            "holdings",
            "investments",
            "capital",
            "insurance",
            "realty",
            "properties",
            "real estate",
            # Retail, Apparel, Dining
            "apparel",
            "clothing",
            "tours",
            "salon",
            "spa",
            "restaurant",
            "bar",
            "bistro",
            "cafe",
            "grill",
            "lounge",
            "trucking",
            "logistics",
        ]

    @property
    def exclusion_patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"\btin\s+roof\b", re.IGNORECASE),
            re.compile(r"\bred\s+roof\b", re.IGNORECASE),
            re.compile(r"\bunder\s+one\s+roof\b", re.IGNORECASE),
            re.compile(r"\bover\s+(?:the\s+)?roof\b", re.IGNORECASE),
            re.compile(
                r"\broof\s+(?:bar|bistro|cafe|lounge|restaurant|inn|hotel)\b", re.IGNORECASE
            ),
            re.compile(r"\broof\s+(?:top|deck)\s+(?:bar|lounge|restaurant)\b", re.IGNORECASE),
        ]


class HVACClassifier(BaseCategoryClassifier):
    """High-precision classifier for HVAC & Air Conditioning contractors."""

    @property
    def category(self) -> ContractorCategory:
        return ContractorCategory.HVAC

    @property
    def strong_positive_terms(self) -> list[str]:
        return [
            "hvac",
            "air conditioning",
            "heating and cooling",
            "heating & cooling",
            "ac repair",
            "a/c repair",
            "ac service",
            "a/c service",
            "ac contractor",
            "air mechanical",
            "climate control",
            "furnace repair",
            "heat pump",
        ]

    @property
    def supporting_terms(self) -> list[str]:
        return ["cooling", "heating", "ventilation", "mechanical", "contractor", "services", "duct"]

    @property
    def negative_terms(self) -> list[str]:
        return ["supply", "parts", "wholesale", "manufacturing", "software", "law", "realty"]

    @property
    def exclusion_patterns(self) -> list[re.Pattern]:
        return []


class PlumbingClassifier(BaseCategoryClassifier):
    """High-precision classifier for Plumbing contractors."""

    @property
    def category(self) -> ContractorCategory:
        return ContractorCategory.PLUMBING

    @property
    def strong_positive_terms(self) -> list[str]:
        return [
            "plumbing",
            "plumber",
            "plumbing contractor",
            "plumbing repairs",
            "drain cleaning",
            "sewer and drain",
            "pipe repair",
            "water heater repair",
        ]

    @property
    def supporting_terms(self) -> list[str]:
        return ["plumbing", "drain", "sewer", "pipes", "contractor", "rooter"]

    @property
    def negative_terms(self) -> list[str]:
        return ["supply", "fixtures", "wholesale", "manufacturing", "law", "realty"]

    @property
    def exclusion_patterns(self) -> list[re.Pattern]:
        return []


class LandscapingClassifier(BaseCategoryClassifier):
    """High-precision classifier for Landscaping contractors."""

    @property
    def category(self) -> ContractorCategory:
        return ContractorCategory.LANDSCAPING

    @property
    def strong_positive_terms(self) -> list[str]:
        return [
            "landscaping",
            "landscape contractor",
            "lawn care",
            "lawn maintenance",
            "landscape design",
            "grounds maintenance",
            "tree care",
            "tree service",
        ]

    @property
    def supporting_terms(self) -> list[str]:
        return ["landscape", "lawn", "turf", "greens", "irrigation", "maintenance"]

    @property
    def negative_terms(self) -> list[str]:
        return ["supply", "nursery", "wholesale", "seeds", "equipment", "law", "realty"]

    @property
    def exclusion_patterns(self) -> list[re.Pattern]:
        return []


class HardscapingClassifier(BaseCategoryClassifier):
    """High-precision classifier for Hardscaping contractors."""

    @property
    def category(self) -> ContractorCategory:
        return ContractorCategory.HARDSCAPING

    @property
    def strong_positive_terms(self) -> list[str]:
        return [
            "hardscaping",
            "hardscape contractor",
            "paver installation",
            "retaining walls",
            "stone masonry",
            "patio installation",
        ]

    @property
    def supporting_terms(self) -> list[str]:
        return ["hardscape", "pavers", "stonework", "masonry", "patio", "contractor"]

    @property
    def negative_terms(self) -> list[str]:
        return ["supply", "stone quarry", "materials", "wholesale"]

    @property
    def exclusion_patterns(self) -> list[re.Pattern]:
        return []


class RemodelingClassifier(BaseCategoryClassifier):
    """High-precision classifier for Remodeling contractors."""

    @property
    def category(self) -> ContractorCategory:
        return ContractorCategory.REMODELING

    @property
    def strong_positive_terms(self) -> list[str]:
        return [
            "remodeling",
            "remodeler",
            "home renovation",
            "kitchen remodel",
            "bathroom remodel",
            "residential renovation",
            "general contractor",
        ]

    @property
    def supporting_terms(self) -> list[str]:
        return ["renovation", "remodel", "home improvements", "contractor"]

    @property
    def negative_terms(self) -> list[str]:
        return ["supply", "furniture", "retail", "decor", "software"]

    @property
    def exclusion_patterns(self) -> list[re.Pattern]:
        return []


class PestControlClassifier(BaseCategoryClassifier):
    """High-precision classifier for Pest Control contractors."""

    @property
    def category(self) -> ContractorCategory:
        return ContractorCategory.PEST_CONTROL

    @property
    def strong_positive_terms(self) -> list[str]:
        return [
            "pest control",
            "termite control",
            "exterminator",
            "exterminating",
            "fumigation",
            "wildlife removal",
            "bed bug treatment",
        ]

    @property
    def supporting_terms(self) -> list[str]:
        return ["pest", "termite", "bugs", "insects", "extermination"]

    @property
    def negative_terms(self) -> list[str]:
        return ["chemical supply", "wholesale", "manufacturing", "research"]

    @property
    def exclusion_patterns(self) -> list[re.Pattern]:
        return []


class MedSpaClassifier(BaseCategoryClassifier):
    """High-precision classifier for Med Spa businesses."""

    @property
    def category(self) -> ContractorCategory:
        return ContractorCategory.MED_SPA

    @property
    def strong_positive_terms(self) -> list[str]:
        return [
            "med spa",
            "medical spa",
            "aesthetics",
            "aesthetic clinic",
            "laser aesthetics",
            "botox and fillers",
            "skin clinic",
        ]

    @property
    def supporting_terms(self) -> list[str]:
        return ["medspa", "skin", "laser", "wellness", "rejuvenation"]

    @property
    def negative_terms(self) -> list[str]:
        return ["cosmetics wholesale", "equipment supply", "software"]

    @property
    def exclusion_patterns(self) -> list[re.Pattern]:
        return []


# Classifier Registry
_CLASSIFIERS: dict[str, BaseCategoryClassifier] = {
    "roofing": RoofingClassifier(),
    "hvac": HVACClassifier(),
    "plumbing": PlumbingClassifier(),
    "landscaping": LandscapingClassifier(),
    "hardscaping": HardscapingClassifier(),
    "remodeling": RemodelingClassifier(),
    "pest-control": PestControlClassifier(),
    "pest_control": PestControlClassifier(),
    "med-spa": MedSpaClassifier(),
    "med_spa": MedSpaClassifier(),
}


_KEYWORD_ALIASES: dict[str, str] = {
    "roof": "roofing",
    "roofer": "roofing",
    "roofers": "roofing",
    "ac": "hvac",
    "air-conditioning": "hvac",
    "air conditioning": "hvac",
    "heating": "hvac",
    "cooling": "hvac",
    "plumber": "plumbing",
    "plumbers": "plumbing",
    "lawn": "landscaping",
    "landscape": "landscaping",
    "hardscape": "hardscaping",
    "paver": "hardscaping",
    "pavers": "hardscaping",
    "remodel": "remodeling",
    "remodeler": "remodeling",
    "renovation": "remodeling",
    "pest": "pest-control",
    "exterminator": "pest-control",
    "medspa": "med-spa",
    "medspas": "med-spa",
    "med-spas": "med-spa",
    "med spas": "med-spa",
    "medical-spa": "med-spa",
    "medical spa": "med-spa",
    "medical-spas": "med-spa",
    "medical spas": "med-spa",
    "aesthetics": "med-spa",
}


def get_classifier(category: str | ContractorCategory) -> BaseCategoryClassifier:
    """Retrieve classifier instance for a requested contractor category or keyword."""
    if isinstance(category, ContractorCategory):
        key = category.value.lower().replace(" ", "-")
    else:
        key = category.strip().lower().replace(" ", "-").replace("_", "-")

    # Resolve keyword aliases
    if key in _KEYWORD_ALIASES:
        key = _KEYWORD_ALIASES[key]

    # Match normalized key
    for reg_key, classifier in _CLASSIFIERS.items():
        if key == reg_key.replace("_", "-"):
            return classifier

    # Check by category value match
    for classifier in _CLASSIFIERS.values():
        if key == classifier.category.value.lower().replace(" ", "-"):
            return classifier

    raise ValueError(
        f"Unsupported category or keyword '{category}'. Available categories are: {list(_CLASSIFIERS.keys())}"
    )
