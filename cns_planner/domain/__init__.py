"""Domain contracts for the schema-v2 workbench and legacy compatibility."""

from ..legacy.project_v1 import Project
from .status import Assessment, ResultStatus, SafetyResult
from .cns_inputs import AircraftCNSProfile, CNSDevice, RequiredCNS, ExistingCNSFacility, CandidateSite
from ..gap.model import GapAnalyzer, GapAnalysisResult, RouteSubsystemGap

__all__ = [
    "Project", "Assessment", "ResultStatus", "SafetyResult",
    "AircraftCNSProfile", "CNSDevice", "RequiredCNS",
    "ExistingCNSFacility", "CandidateSite",
    "GapAnalyzer", "GapAnalysisResult", "RouteSubsystemGap",
]
