"""Deterministic quality contract for source-grounded Timeline summaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

TIMELINE_QUALITY_CONTRACT_VERSION = "timeline-summary-quality-v2"


@dataclass(frozen=True, slots=True)
class TimelineQualityIssue:
    code: str
    message: str
    field: str = ""
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class TimelineQualityReport:
    status: str
    issues: list[TimelineQualityIssue] = field(default_factory=list)
    source_message_count: int = 0
    grounded_fact_count: int = 0
    covered_message_count: int = 0
    contract_version: str = TIMELINE_QUALITY_CONTRACT_VERSION

    @property
    def errors(self) -> list[TimelineQualityIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[TimelineQualityIssue]:
        return [issue for issue in self.issues if issue.severity != "error"]

    @property
    def acceptable(self) -> bool:
        return not self.errors

    def rejected(self) -> "TimelineQualityReport":
        return TimelineQualityReport(
            status="rejected",
            issues=list(self.issues),
            source_message_count=self.source_message_count,
            grounded_fact_count=self.grounded_fact_count,
            covered_message_count=self.covered_message_count,
            contract_version=self.contract_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "issues": [issue.to_dict() for issue in self.issues],
            "source_message_count": self.source_message_count,
            "grounded_fact_count": self.grounded_fact_count,
            "covered_message_count": self.covered_message_count,
            "contract_version": self.contract_version,
        }


class TimelineSummaryQualityError(ValueError):
    """Raised when a Timeline summary still violates the contract after repair."""

    def __init__(self, report: TimelineQualityReport):
        self.report = report.rejected()
        codes = ", ".join(issue.code for issue in self.report.errors) or "unknown"
        super().__init__(f"Timeline summary rejected by quality contract: {codes}")


__all__ = [
    "TIMELINE_QUALITY_CONTRACT_VERSION",
    "TimelineQualityIssue",
    "TimelineQualityReport",
    "TimelineSummaryQualityError",
]
