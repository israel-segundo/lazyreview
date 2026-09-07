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
    s1, s2, s3 = severity_counts(findings)
    assert (s1, s2, s3) == (1, 1, 0)
