"""Closed finding taxonomy normalizer + persist helpers."""

from __future__ import annotations

import json

from lazyreview.findings_taxonomy import (
    coerce_findings_for_persist,
    findings_to_json,
    normalize_finding,
    normalize_findings_list,
    normalize_severity,
)


def test_normalize_unknown_category_and_labels():
    out = normalize_finding(
        {
            "id": "C1",
            "severity": "High",
            "category": "reliability",
            "labels": ["bug", "ux", "made-up"],
            "title": "x",
        }
    )
    assert out["severity"] == 1
    # Unknown category falls back to label inference (bug → correctness).
    assert out["category"] == "correctness"
    assert out["labels"] == ["bug"]
    assert out["origin"] == "model"
    assert out["human_edited"] is False


def test_normalize_unknown_category_without_labels_defaults_quality():
    out = normalize_finding(
        {"id": "C1", "severity": 2, "category": "UX", "labels": ["pretty"], "title": "x"}
    )
    assert out["category"] == "quality"
    assert out["labels"] == []


def test_normalize_infers_category_from_labels():
    out = normalize_finding(
        {"id": "C1", "severity": 2, "labels": ["security", "compliance"], "title": "x"}
    )
    assert out["category"] == "security"
    assert out["labels"] == ["security", "compliance"]


def test_normalize_severity_aliases():
    assert normalize_severity("High") == 1
    assert normalize_severity("medium") == 2
    assert normalize_severity("nit") == 3
    assert normalize_severity("Sev Critical") == 2  # unknown → default


def test_normalize_origin_aliases():
    assert normalize_finding({"title": "a"}, default_origin="agent")["origin"] == "model"
    assert normalize_finding({"title": "a", "origin": "user"})["origin"] == "human"
    assert normalize_finding({"title": "a", "origin": "hybrid"})["origin"] == "model"


def test_prior_preserves_model_origin_and_marks_edited():
    prior = {
        "C1": {
            "id": "C1",
            "severity": 1,
            "title": "Old",
            "body": "a",
            "category": "correctness",
            "labels": ["bug"],
            "origin": "model",
            "human_edited": False,
        }
    }
    out = normalize_findings_list(
        [
            {
                "id": "C1",
                "severity": 2,
                "title": "Old",
                "body": "a",
                "category": "correctness",
                "labels": ["bug"],
            }
        ],
        default_origin="human",
        prior_by_id=prior,
    )
    assert out[0]["origin"] == "model"
    assert out[0]["human_edited"] is True
    assert out[0]["severity"] == 2


def test_new_chat_finding_is_human_origin():
    out = normalize_findings_list(
        [{"id": "C9", "severity": 3, "title": "Nit", "body": "x", "category": "housekeeping", "labels": ["nit"]}],
        default_origin="human",
        prior_by_id={},
    )
    assert out[0]["origin"] == "human"
    assert out[0]["human_edited"] is False


def test_unchanged_model_finding_not_marked_edited_on_chat_path():
    prior = {
        "C1": {
            "id": "C1",
            "severity": 1,
            "title": "Same",
            "body": "body",
            "category": "correctness",
            "labels": ["bug"],
            "origin": "model",
            "human_edited": False,
        }
    }
    out = normalize_findings_list(
        [
            {
                "id": "C1",
                "severity": 1,
                "title": "Same",
                "body": "body",
                "category": "correctness",
                "labels": ["bug"],
            }
        ],
        default_origin="human",
        prior_by_id=prior,
    )
    assert out[0]["origin"] == "model"
    assert out[0]["human_edited"] is False


def test_coerce_and_dump_strips_junk_taxonomy():
    raw = [
        {
            "id": "C1",
            "severity": "banana",
            "category": "UX",
            "labels": ["pretty", "bug"],
            "title": "t",
            "body": "b",
            "origin": "llm",
        }
    ]
    cleaned = coerce_findings_for_persist(raw, default_origin="model")
    assert cleaned[0]["severity"] == 2
    assert cleaned[0]["category"] == "correctness"
    assert cleaned[0]["labels"] == ["bug"]
    assert cleaned[0]["origin"] == "model"
    dumped = json.loads(findings_to_json(cleaned))
    assert dumped[0]["category"] == "correctness"
    assert "pretty" not in json.dumps(dumped)
    assert "UX" not in json.dumps(dumped)
