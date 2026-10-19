"""The eval harness: seeded scenarios, replayed offline, reported per Run."""

from verirun.eval.harness import format_report, run_eval, write_report
from verirun.eval.models import (
    AblationCase,
    AblationReport,
    ApprovalOutcome,
    EscalationOutcome,
    EvalReport,
    EvalTotals,
    ScenarioReport,
)
from verirun.eval.scenarios import SCENARIOS, SILENT_FAILURE_SCENARIOS, Scenario

__all__ = [
    "SCENARIOS",
    "SILENT_FAILURE_SCENARIOS",
    "AblationCase",
    "AblationReport",
    "ApprovalOutcome",
    "EscalationOutcome",
    "EvalReport",
    "EvalTotals",
    "Scenario",
    "ScenarioReport",
    "format_report",
    "run_eval",
    "write_report",
]
