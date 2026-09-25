import pytest
from pydantic import BaseModel

from roundtable.providers.base import Message, SchemaError, json_schema_for, parse_json_loose, split_messages, strictify


class Inner(BaseModel):
    a: int
    b: str = "x"


class Outer(BaseModel):
    items: list[Inner]
    note: str | None = None


def test_strictify_sets_additional_properties_and_required_everywhere():
    s = json_schema_for(Outer, strict=True)
    assert s["additionalProperties"] is False
    assert s["required"] == ["items", "note"]
    inner = s["$defs"]["Inner"]
    assert inner["additionalProperties"] is False
    assert inner["required"] == ["a", "b"]
    assert "default" not in inner["properties"]["b"]


def test_non_strict_schema_untouched():
    s = json_schema_for(Outer)
    assert "additionalProperties" not in s
    assert s["required"] == ["items"]


@pytest.mark.parametrize("text", [
    '{"ok": true}',
    'Sure! ```json\n{"ok": true}\n```',
    'Here is the answer: {"ok": true} hope it helps',
])
def test_parse_json_loose(text):
    assert parse_json_loose(text) == {"ok": True}


def test_parse_json_loose_rejects_non_object():
    with pytest.raises(SchemaError):
        parse_json_loose("[1,2,3]")
    with pytest.raises(SchemaError):
        parse_json_loose("no json here")


def test_split_messages_single_turn():
    system, prompt = split_messages([Message("system", "S"), Message("user", "U")])
    assert system == "S" and prompt == "U"


def test_split_messages_renders_repair_transcript():
    system, prompt = split_messages([
        Message("system", "S"), Message("user", "U1"), Message("assistant", "bad"), Message("user", "fix"),
    ])
    assert "[USER]\nU1" in prompt and "[ASSISTANT (your previous reply)]\nbad" in prompt and prompt.endswith("fix")


def test_strictify_handles_lists():
    assert strictify([{"type": "object", "properties": {"x": {}}}])[0]["required"] == ["x"]
