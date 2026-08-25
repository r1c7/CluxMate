"""Tests for AskUserQuestionTool (ask_user_question)."""

import json

import pytest

from cluxmate.tools.ask_user_question import AskUserQuestionTool


class _FakeBuilder:
    pass


def _tool() -> AskUserQuestionTool:
    return AskUserQuestionTool(builder=_FakeBuilder())


def test_risk_level_safe():
    assert _tool().risk_level == "safe"


def test_schema_requires_questions():
    schema = _tool().input_schema
    assert schema["required"] == ["questions"]
    items = schema["properties"]["questions"]["items"]
    assert items["required"] == ["id", "question"]


@pytest.mark.asyncio
async def test_execute_echoes_answer_as_json():
    tool = _tool()
    answer = {
        "answers": [
            {"id": "mode", "selected": ["Plan (read-only)"], "custom": "nope"},
        ]
    }
    questions = [{"id": "mode", "question": "Which mode?"}]
    result = await tool.execute(questions=questions, _answer=answer)
    assert json.loads(result) == answer


@pytest.mark.asyncio
async def test_execute_without_answer_reports_unsupported():
    tool = _tool()
    result = await tool.execute(questions=[{"id": "q", "question": "Q?"}])
    assert "not supported" in result


@pytest.mark.asyncio
async def test_execute_with_no_questions_reports_error():
    tool = _tool()
    result = await tool.execute(questions=[], _answer={"answers": []})
    assert "requires at least one question" in result
