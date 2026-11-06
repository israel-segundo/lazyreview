from lazyreview.services.findings_parse import parse_findings_md, severity_counts


def test_parse_findings_md():
    text = """
# Report

## F1 — [High] Shared module mutates all envs
- Editing `infra/root.tf:12` re-plans everything.
- Fix: scope the variable.

## F2 — [Medium] Test would stay green if reverted
- See `tests/test_foo.py:40`.
"""
    findings = parse_findings_md(text)
    assert len(findings) == 2
    assert findings[0]["external_id"] == "F1"
    assert findings[0]["severity"] == 1
    assert "Shared module" in findings[0]["title"]
    assert findings[0]["path"] == "infra/root.tf"
    assert findings[0]["line"] == 12
    assert findings[0]["category"] == "quality"
    assert findings[0]["origin"] == "model"
    s1, s2, s3 = severity_counts(findings)
    assert (s1, s2, s3) == (1, 1, 0)


def test_parse_findings_md_with_taxonomy_bracket():
    text = """
## F1 — [High] [correctness:bug,edge-case] Shared module mutates all envs
- Editing `infra/root.tf:12` re-plans everything.

## F2 — [Low] [housekeeping:nit] Typo in README
- See `README.md:1`.
"""
    findings = parse_findings_md(text)
    assert len(findings) == 2
    assert findings[0]["category"] == "correctness"
    assert findings[0]["labels"] == ["bug", "edge-case"]
    assert findings[0]["severity"] == 1
    assert findings[1]["category"] == "housekeeping"
    assert findings[1]["labels"] == ["nit"]
    assert findings[1]["severity"] == 3
