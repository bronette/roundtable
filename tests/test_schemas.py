import pytest
from pydantic import ValidationError

from roundtable.schemas import Critique, Proposal


def test_accept_with_blocker_rejected():
    with pytest.raises(ValidationError):
        Critique(verdict="ACCEPT", problems=[{"severity": "blocker", "description": "x"}], confidence=0.5)


def test_accept_needs_checks():
    with pytest.raises(ValidationError):
        Critique(verdict="ACCEPT", confidence=0.5)
    Critique(verdict="ACCEPT", checks_performed=["edge cases"], confidence=0.5)


def test_revise_needs_problems():
    with pytest.raises(ValidationError):
        Critique(verdict="REVISE", confidence=0.5)


def test_proposal_shape():
    with pytest.raises(ValidationError):
        Proposal(scope="single_task", claim="c", approach="a", confidence=0.5)   # no criteria
    with pytest.raises(ValidationError):
        Proposal(scope="needs_decomposition", claim="c", approach="a", confidence=0.5)  # no split
