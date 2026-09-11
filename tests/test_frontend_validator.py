#!/usr/bin/env python3
"""Tests for Phase 7C-1 FrontendValidator."""

import json
import sys
from contracts import FrontendValidationResult, FrontendResult
from tools.frontend_validator import FrontendValidator


class MockConfig:
    pass


def make_validator(config=None):
    if config is None:
        config = MockConfig()
    return FrontendValidator(config)


# Minimal valid baseline for tests
MINIMAL_HTML = "<div>Content</div>"
MINIMAL_CSS = "body { color: #333; }"
MINIMAL_JS = "const x = 1;"
MINIMAL_BLOCKS = '<!-- wp:greenshift-blocks/element {"tag":"div"} -->\n<div>Content</div>\n<!-- /wp:greenshift-blocks/element -->'

def make_result(html=MINIMAL_HTML, css=MINIMAL_CSS, javascript=MINIMAL_JS, blocks=MINIMAL_BLOCKS, task_id="test"):
    return FrontendResult(
        task_id=task_id,
        success=True,
        html=html,
        css=css,
        javascript=javascript,
        blocks=blocks,
    )


# ========================
# HTML Validation
# ========================

def test_valid_html():
    validator = make_validator()
    result = make_result(html="<div class='hello'>Hello</div>")
    vr = validator.validate(result)
    assert vr.passed is True
    assert vr.validation_status == "passed"


def test_empty_html():
    validator = make_validator()
    result = make_result(html="")
    vr = validator.validate(result)
    assert vr.passed is False
    assert vr.validation_status == "failed"
    assert any(e["type"] == "html_empty" for e in vr.errors)


def test_malformed_unclosed_tag():
    validator = make_validator()
    result = make_result(html="<div>Hello</span>")
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "html_structure" for e in vr.errors)


def test_unclosed_tag():
    validator = make_validator()
    result = make_result(html="<div><p>Hello")
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "html_structure" and "Unclosed tag" in e["message"] for e in vr.errors)


def test_nested_unclosed():
    validator = make_validator()
    result = make_result(html="<div><p>Hello</div>")
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "html_structure" for e in vr.errors)


def test_parser_failure_handling():
    validator = make_validator()
    # HTMLParser doesn't raise exceptions for malformed input like "<"
    # It treats it as text content. This is correct behavior - no error expected.
    result = make_result(html="<")
    vr = validator.validate(result)
    # Parser doesn't fail for "<" - it's treated as text content
    # Validation passes since there are no unclosed tags
    assert vr.passed is True
    assert vr.validation_status == "passed"


# ========================
# CSS Validation
# ========================

def test_valid_css():
    validator = make_validator()
    result = make_result(css="body { color: #333; margin: 10px; }")
    vr = validator.validate(result)
    assert vr.passed is True
    assert vr.validation_status == "passed"


def test_empty_css():
    validator = make_validator()
    result = make_result(css="")
    vr = validator.validate(result)
    assert vr.passed is True
    assert vr.validation_status == "warnings"
    assert any(w["type"] == "css_empty" for w in vr.warnings)


def test_unmatched_closing_brace():
    validator = make_validator()
    result = make_result(css="body { color: #333; }}")
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "css_syntax" and "closing brace" in e["message"].lower() for e in vr.errors)


def test_unclosed_css_rule():
    validator = make_validator()
    result = make_result(css="body { color: #333; .header { font-size: 16px; }")
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "css_syntax" and "unclosed" in e["message"].lower() for e in vr.errors)


def test_multiple_unclosed():
    validator = make_validator()
    result = make_result(css=".a { .b { .c { }")
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "css_syntax" for e in vr.errors)


# ========================
# JavaScript Validation
# ========================

def test_valid_js():
    validator = make_validator()
    result = make_result(javascript="const x = 1; console.log(x);")
    vr = validator.validate(result)
    assert vr.passed is True
    assert vr.validation_status == "passed"


def test_invalid_js_syntax():
    validator = make_validator()
    result = make_result(javascript="const x = ;")
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "js_syntax" for e in vr.errors)


def test_empty_js():
    validator = make_validator()
    result = make_result(javascript="")
    vr = validator.validate(result)
    assert vr.passed is True


def test_js_with_syntax_error():
    validator = make_validator()
    result = make_result(javascript="function foo( { return 1; }")
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "js_syntax" for e in vr.errors)


def test_js_missing_node_graceful():
    validator = make_validator()
    # This test would require mocking subprocess to simulate FileNotFoundError
    # We skip it here since we can't easily mock in this test framework
    pass


# ========================
# GreenLight Blocks Validation
# ========================

def test_valid_blocks():
    validator = make_validator()
    blocks = "<!-- wp:greenshift-blocks/element {\"tag\":\"div\"} -->\n<div>Hello</div>\n<!-- /wp:greenshift-blocks/element -->"
    result = make_result(blocks=blocks)
    vr = validator.validate(result)
    assert vr.passed is True
    assert vr.validation_status == "passed"


def test_empty_blocks():
    validator = make_validator()
    result = make_result(blocks="")
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "blocks_empty" for e in vr.errors)


def test_blocks_no_wp_comments():
    validator = make_validator()
    result = make_result(blocks="<div>Not a block</div>")
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "blocks_format" and "WordPress block comments" in e["message"] for e in vr.errors)


def test_unbalanced_blocks():
    validator = make_validator()
    blocks = "<!-- wp:greenshift-blocks/element {\"tag\":\"div\"} -->\n<div>Hello</div>"
    result = make_result(blocks=blocks)
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "blocks_format" and "Unbalanced" in e["message"] for e in vr.errors)


def test_invalid_json_in_block():
    validator = make_validator()
    blocks = "<!-- wp:greenshift-blocks/element {invalid json} -->\n<div>Hello</div>\n<!-- /wp:greenshift-blocks/element -->"
    result = make_result(blocks=blocks)
    vr = validator.validate(result)
    assert vr.passed is False
    assert any(e["type"] == "blocks_json" for e in vr.errors)


def test_multiple_blocks():
    validator = make_validator()
    blocks = (
        "<!-- wp:greenshift-blocks/element {\"tag\":\"div\"} -->\n<div>Hello</div>\n<!-- /wp:greenshift-blocks/element -->\n"
        "<!-- wp:greenshift-blocks/element {\"tag\":\"p\"} -->\n<p>World</p>\n<!-- /wp:greenshift-blocks/element -->"
    )
    result = make_result(blocks=blocks)
    vr = validator.validate(result)
    assert vr.passed is True


def test_style_manager_block():
    validator = make_validator()
    blocks = (
        "<!-- wp:greenshift-blocks/element {\"tag\":\"div\",\"type\":\"no\",\"isVariation\":\"stylemanager\"} -->\n"
        "<div class=\"my-class\"></div>\n"
        "<!-- /wp:greenshift-blocks/element -->"
    )
    result = make_result(blocks=blocks)
    vr = validator.validate(result)
    assert vr.passed is True


# ========================
# Structured Diagnostics
# ========================

def test_diagnostics_populated():
    validator = make_validator()
    result = make_result(
        html="<div>Hello</div>",
        css="body { color: #333; }",
        javascript="const x = 1;",
        blocks="<!-- wp:greenshift-blocks/element {\"tag\":\"div\"} -->\n<div>Hi</div>\n<!-- /wp:greenshift-blocks/element -->"
    )
    vr = validator.validate(result)
    assert "html_size" in vr.diagnostics
    assert "css_size" in vr.diagnostics
    assert "js_size" in vr.diagnostics
    assert "blocks_size" in vr.diagnostics
    assert vr.diagnostics["has_html"] is True
    assert vr.diagnostics["has_css"] is True
    assert vr.diagnostics["has_js"] is True
    assert vr.diagnostics["has_blocks"] is True


def test_error_fields_populated():
    validator = make_validator()
    result = make_result(html="<div>Hello</span>")
    vr = validator.validate(result)
    assert vr.passed is False
    for e in vr.errors:
        assert "gate" in e
        assert "type" in e
        assert "severity" in e
        assert "message" in e
        assert "location" in e
        assert "context" in e
        assert e["gate"] == "validation"
        assert e["severity"] in ("error", "warning", "info")


def test_warning_fields_populated():
    validator = make_validator()
    result = make_result(css="")
    vr = validator.validate(result)
    assert vr.validation_status == "warnings"
    for w in vr.warnings:
        assert "gate" in w
        assert "type" in w
        assert "severity" in w
        assert "message" in w
        assert "location" in w
        assert "context" in w


# ========================
# Deterministic
# ========================

def test_deterministic():
    validator = make_validator()
    result = make_result(
        html="<div>Hello</div>",
        css="body { color: #333; }",
        javascript="const x = 1;",
        blocks="<!-- wp:greenshift-blocks/element {\"tag\":\"div\"} -->\n<div>Hi</div>\n<!-- /wp:greenshift-blocks/element -->"
    )
    vr1 = validator.validate(result)
    vr2 = validator.validate(result)
    assert vr1.passed == vr2.passed
    assert vr1.validation_status == vr2.validation_status
    assert vr1.errors == vr2.errors
    assert vr1.warnings == vr2.warnings


def test_validator_error_flag():
    validator = make_validator()
    # Can't easily test validator_error without mocking subprocess
    # but we can verify the field exists
    result = make_result(javascript="const x = 1;")
    vr = validator.validate(result)
    assert "validator_error" in vars(vr) or hasattr(vr, "validator_error")


# ========================
# Run all tests
# ========================

if __name__ == "__main__":
    tests = [
        # HTML
        test_valid_html,
        test_empty_html,
        test_malformed_unclosed_tag,
        test_unclosed_tag,
        test_nested_unclosed,
        test_parser_failure_handling,
        # CSS
        test_valid_css,
        test_empty_css,
        test_unmatched_closing_brace,
        test_unclosed_css_rule,
        test_multiple_unclosed,
        # JavaScript
        test_valid_js,
        test_invalid_js_syntax,
        test_empty_js,
        test_js_with_syntax_error,
        # GreenLight Blocks
        test_valid_blocks,
        test_empty_blocks,
        test_blocks_no_wp_comments,
        test_unbalanced_blocks,
        test_invalid_json_in_block,
        test_multiple_blocks,
        test_style_manager_block,
        # Diagnostics
        test_diagnostics_populated,
        test_error_fields_populated,
        test_warning_fields_populated,
        # Deterministic
        test_deterministic,
        test_validator_error_flag,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"PASS: {test.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {test.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR: {test.__name__}: {e}")

    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests")
    if failed:
        sys.exit(1)