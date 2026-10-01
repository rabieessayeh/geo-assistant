"""Evaluation scoring, and consistency of eval/questions.yaml with the tools."""

from pathlib import Path

import pytest

from app.evaluation import Case, args_match, format_report, load_cases, score_case, summarise
from app.tools import TOOLS, Catalog, run_tool

ROOT = Path(__file__).resolve().parents[1]
CASES = load_cases(ROOT / "eval" / "questions.yaml")

WITHIN = Case(
    "c", "q", "within_distance", {"target": "schools", "reference": "stops", "meters": 300}
)


def step(tool: str, **args) -> dict:
    return {"tool": tool, "args": args, "summary": ""}


def test_args_match_tolerates_number_formats_and_extra_arguments():
    assert args_match({"meters": 300}, {"meters": 300.0, "extra": 1})
    assert args_match({"meters": 300}, {"meters": "300"})
    assert not args_match({"meters": 300}, {"meters": 0.3})
    assert not args_match({"meters": 300}, {"meters": "far"})
    assert not args_match({"target": "schools"}, {})
    assert not args_match({"target": "schools"}, {"target": "stops"})


def test_correct_call_after_discovery():
    trace = [
        step("list_layers"),
        step("within_distance", target="schools", reference="stops", meters=300),
    ]
    score = score_case(WITHIN, trace)
    assert (score.tool_ok, score.args_ok) == (True, True)


def test_right_tool_wrong_arguments():
    trace = [step("within_distance", target="stops", reference="schools", meters=300)]
    score = score_case(WITHIN, trace)
    assert (score.tool_ok, score.args_ok) == (True, False)


def test_retry_with_corrected_arguments_counts():
    trace = [
        step("within_distance", target="school", reference="stops", meters=300),
        step("within_distance", target="schools", reference="stops", meters=300),
    ]
    assert score_case(WITHIN, trace).args_ok


@pytest.mark.parametrize(
    "trace",
    [
        [],
        [step("nearest", layer="schools", lon=6.1, lat=49.6)],
        [
            step("within_distance", target="schools", reference="stops", meters=300),
            step("nearest", layer="schools", lon=6.1, lat=49.6),
        ],
    ],
)
def test_wrong_missing_or_extra_tool_fails(trace):
    score = score_case(WITHIN, trace)
    assert (score.tool_ok, score.args_ok) == (False, False)


def test_out_of_scope_case():
    case = Case("c", "q", None)
    assert score_case(case, []).tool_ok
    assert score_case(case, [step("list_layers")]).tool_ok
    assert not score_case(case, [step("nearest", layer="stops", lon=6.1, lat=49.6)]).tool_ok


def test_summary_and_report():
    scores = [
        score_case(
            WITHIN, [step("within_distance", target="schools", reference="stops", meters=300)]
        ),
        score_case(
            WITHIN, [step("within_distance", target="stops", reference="stops", meters=300)]
        ),
    ]
    assert summarise(scores) == {"cases": 2, "tool_accuracy": 1.0, "args_accuracy": 0.5}
    report = format_report(scores)
    assert "Tool accuracy:      100% of 2 cases" in report
    assert "Argument accuracy:  50% of 2 cases" in report


def test_question_file_size():
    assert 5 <= len(CASES) <= 10


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_expected_calls_run_on_the_sample_data(case):
    """Every expected call must be a valid call of a whitelisted tool."""
    if case.tool is None:
        return
    assert case.tool in TOOLS
    result = run_tool(Catalog.from_folder(ROOT / "data" / "sample"), case.tool, case.args)
    assert "error" not in result
