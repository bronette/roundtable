"""Inter-agent JSON. Every model output is validated against one of these before it touches
the store. Field descriptions are sent to the models as part of the JSON schema."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Verdict(StrEnum):
    ACCEPT = "ACCEPT"
    REVISE = "REVISE"
    REJECT = "REJECT"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    NEEDS_EXPERIMENT = "NEEDS_EXPERIMENT"


class Scope(StrEnum):
    SINGLE_TASK = "single_task"
    NEEDS_DECOMPOSITION = "needs_decomposition"


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"


class Answer(BaseModel):
    """Generic one-shot answer, used by `roundtable call`."""
    answer: str = Field(description="The direct answer, concise.")
    reasoning_summary: str = Field(description="Two or three sentences on how you got there.")
    confidence: float = Field(ge=0, le=1)


class Problem(BaseModel):
    severity: Severity = Field(description="blocker: wrong result or unsafe. major: a requirement or criterion unmet. minor: quality.")
    description: str
    location: str | None = Field(default=None, description="Where: a criterion id (AC2), an assumption index, a requirement number, or file:line.")


class AcceptanceCriterion(BaseModel):
    id: str = Field(description="AC1, AC2, ...")
    requirement: str = Field(description="The requirement this criterion covers, quoted or paraphrased.")
    check: str = Field(description="The concrete check that proves it: a test name, a command and expected output, or an observable.")


class CriticismResponse(BaseModel):
    problem: str = Field(description="The critic's problem, quoted or closely paraphrased.")
    action: Literal["fixed", "rejected", "deferred"]
    reason: str


class Proposal(BaseModel):
    scope: Scope = Field(description="single_task if one engineer can finish this in one sitting; otherwise needs_decomposition.")
    suggested_split: list[str] = Field(default_factory=list, description="When needs_decomposition: the ordered sub-tasks.")
    claim: str = Field(description="One sentence: what will be built or what is asserted.")
    approach: str = Field(description="How, in at most ~300 words. Concrete enough to implement from.")
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list, description="Things known and where from. Empty is honest; invented is not.")
    risks: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list, description="The definition of done. Locked before implementation.")
    responses_to_criticism: list[CriticismResponse] = Field(default_factory=list, description="Revisions only: one entry per problem raised.")
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _shape(self) -> "Proposal":
        if self.scope == Scope.NEEDS_DECOMPOSITION and not self.suggested_split:
            raise ValueError("needs_decomposition requires suggested_split")
        if self.scope == Scope.SINGLE_TASK and not self.acceptance_criteria:
            raise ValueError("a single_task proposal needs at least one acceptance criterion")
        return self


class Critique(BaseModel):
    verdict: Verdict
    problems: list[Problem] = Field(default_factory=list)
    counterarguments: list[str] = Field(default_factory=list, description="Alternative views or approaches the proposer did not consider.")
    checks_performed: list[str] = Field(default_factory=list, description="What you examined. Required when accepting: an ACCEPT with no problems and no checks is worthless.")
    tests_required: list[str] = Field(default_factory=list, description="Tests or evidence that must exist before this could be trusted.")
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _honest(self) -> "Critique":
        blockers = [p for p in self.problems if p.severity == Severity.BLOCKER]
        if self.verdict == Verdict.ACCEPT:
            if blockers:
                raise ValueError("ACCEPT with an open blocker is not allowed; use REVISE or REJECT")
            if not self.problems and not self.checks_performed:
                raise ValueError("ACCEPT with no problems must list checks_performed")
        if self.verdict in (Verdict.REVISE, Verdict.REJECT) and not self.problems:
            raise ValueError(f"{self.verdict} requires at least one problem")
        return self


# ---- M2 stages (defined now so the schema does not churn)


class FileChange(BaseModel):
    path: str = Field(description="Relative to the workspace root. No '..'.")
    content: str


class Implementation(BaseModel):
    summary: str = Field(description="What was changed and why, in a few sentences.")
    files: list[FileChange] = Field(default_factory=list, description="Answer-mode engineers return files here; agent-mode engineers leave it empty (the diff is captured from the worktree).")
    test_command: str = Field(default="python -m pytest -q")
    assumptions: list[str] = Field(default_factory=list)
    known_gaps: list[str] = Field(default_factory=list, description="Criteria you did not meet, and why.")


class TestResult(BaseModel):
    """Produced by the orchestrator from a real test run, never by a model."""
    command: str
    exit_code: int
    passed: int = 0
    failed: int = 0
    errors: int = 0
    duration_s: float = 0.0
    stdout_tail: str = ""
    timed_out: bool = False


class RequirementCheck(BaseModel):
    criterion_id: str
    satisfied: bool
    evidence: str = Field(description="A file:line, a test name, or the command output that shows it. 'The code looks right' is not evidence.")


class Review(BaseModel):
    verdict: Verdict
    checks: list[RequirementCheck]
    problems: list[Problem] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class Position(BaseModel):
    role: str
    position: str


class Disagreement(BaseModel):
    topic: str
    positions: list[Position]
    resolved: bool
    resolution: str | None = None


class Synthesis(BaseModel):
    what_was_built: str = Field(description="Plainly. 'Nothing yet; a plan was accepted' is a valid answer.")
    test_summary: str = Field(description="Real test results if any ran, otherwise 'no tests were run'.")
    disagreements: list[Disagreement] = Field(default_factory=list, description="Preserve them; do not force consensus.")
    remaining_risks: list[str] = Field(default_factory=list)
    recommended_next_experiment: str | None = Field(default=None, description="The single most informative next step.")
    overall_verdict: Verdict = Field(description="ACCEPT only if the acceptance criteria were met with evidence.")
    confidence: float = Field(ge=0, le=1)
