"""Tests for category classification architecture and high-precision roofing inclusion/exclusion rules."""

import pytest

from contractor_scraper.classification.classifier import RoofingClassifier, get_classifier
from contractor_scraper.core.enums import ContractorCategory


def test_roofing_classifier_positive_cases() -> None:
    """Verify genuine roofing contractors are accurately classified with high confidence."""
    classifier = RoofingClassifier()

    positives = [
        "Apex Roofing LLC",
        "Titan Roof Repairs Inc",
        "Sunshine Roofing Contractors",
        "Coastal Commercial Roofing Services",
        "Elite Re-Roofing Solutions",
        "Tri-County Metal Roofing Systems",
        "Orlando Residential Roofing Pro",
        "Summit Roof Restoration & Waterproofing",
    ]

    for name in positives:
        res = classifier.evaluate(name)
        assert res.is_match is True, f"Failed for {name}: {res.reasoning}"
        assert res.confidence >= 0.75, f"Low confidence for {name}: {res.confidence}"
        assert res.target_category == ContractorCategory.ROOFING


def test_roofing_classifier_negative_and_exclusion_cases() -> None:
    """Verify suppliers, software, wholesale, dining, and unrelated 'roof' names are rejected."""
    classifier = RoofingClassifier()

    negatives = [
        # Materials & Suppliers
        "ABC Roofing Materials & Supply",
        "Florida Roofing Supplies LLC",
        "National Roofing Wholesale Distributors",
        "Roofing Hardware & Lumber Corp",
        "Commercial Roofing Products Manufacturing",
        # Software & Tech
        "Roofing Software Technologies LLC",
        "Roof CRM Apps & Marketing",
        # Equipment & Tools
        "Heavy Roofing Equipment Rentals",
        "Roofing Tools & Machinery Co",
        # Professional / Financial
        "Roofing Claims Legal Consulting",
        "Roofing Capital Investments Group",
        "Roofing Holdings & Realty LLC",
        # Hospitality / Unrelated 'roof'
        "Tin Roof Bistro & Bar",
        "Red Roof Inn Orlando",
        "Under One Roof Family Services",
        "Over The Roof Logistics & Trucking",
        "Roof Top Lounge & Grill",
    ]

    for name in negatives:
        res = classifier.evaluate(name)
        assert res.is_match is False, (
            f"Should have excluded {name}, but matched with reasoning: {res.reasoning}"
        )
        assert res.confidence < 0.75


def test_other_category_classifiers() -> None:
    """Verify HVAC, Plumbing, and Landscaping classifiers identify appropriate trades."""
    hvac = get_classifier("hvac")
    assert hvac.evaluate("Cool Air HVAC Solutions LLC").is_match is True
    assert hvac.evaluate("HVAC Parts Wholesale Supply").is_match is False

    plumbing = get_classifier("plumbing")
    assert plumbing.evaluate("Reliable Plumbing & Drain Service").is_match is True
    assert plumbing.evaluate("Plumbing Fixtures Showroom Supply").is_match is False

    landscaping = get_classifier("landscaping")
    assert landscaping.evaluate("Evergreen Landscaping & Lawn Care").is_match is True
    assert landscaping.evaluate("Landscaping Equipment Wholesale").is_match is False

    mattress = get_classifier("mattress")
    assert mattress.evaluate("Sleep Center Mattress Store LLC").is_match is True
    assert mattress.evaluate("Mattress Freight & Moving Co").is_match is False

    spa = get_classifier("spa")
    assert spa.evaluate("Tranquil Day Spa & Wellness LLC").is_match is True
    assert spa.evaluate("Hot Tub & Spa Parts Wholesale Supply").is_match is False


def test_unsupported_category_rejection() -> None:
    """Verify invalid category names raise ValueError with helpful options."""
    with pytest.raises(ValueError) as exc:
        get_classifier("invalid-trade-category")
    assert "Unsupported category" in str(exc.value)
