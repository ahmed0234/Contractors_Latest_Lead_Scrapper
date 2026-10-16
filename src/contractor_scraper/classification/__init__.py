"""Classification module exports."""

from contractor_scraper.classification.base import BaseIndustryClassifier, ClassificationResult
from contractor_scraper.classification.classifier import (
    BaseCategoryClassifier,
    HardscapingClassifier,
    HVACClassifier,
    LandscapingClassifier,
    MedSpaClassifier,
    PestControlClassifier,
    PlumbingClassifier,
    RemodelingClassifier,
    RoofingClassifier,
    get_classifier,
)

from contractor_scraper.classification.pest_control_service_classifier import (
    PestControlClassificationResult,
    PestControlServiceClassifier,
)
from contractor_scraper.classification.christmas_light_classifier import (
    ChristmasLightClassificationResult,
    ChristmasLightClassifier,
)

__all__ = [
    "BaseIndustryClassifier",
    "ClassificationResult",
    "BaseCategoryClassifier",
    "RoofingClassifier",
    "HVACClassifier",
    "PlumbingClassifier",
    "LandscapingClassifier",
    "HardscapingClassifier",
    "RemodelingClassifier",
    "PestControlClassifier",
    "MedSpaClassifier",
    "get_classifier",
    "PestControlServiceClassifier",
    "PestControlClassificationResult",
    "ChristmasLightClassifier",
    "ChristmasLightClassificationResult",
]
