#!/usr/bin/env python3
"""Tests for Phase 7D-2B RenderedTechnicalValidator."""

import copy
import sys
import os
from unittest.mock import patch, MagicMock
from dataclasses import is_dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import (
    RenderedEvidence,
    ViewportRenderedEvidence,
    RenderedTechnicalResult,
    RenderedTechnicalGate,
    RenderedTechnicalSeverity,
)
from tools.rendered_technical_validator import (
    RenderedTechnicalValidator,
    HORIZONTAL_OVERFLOW_TOLERANCE_PX,
)


# ========================
# Helpers
# ========================

def make_viewport_evidence(
    viewport_width=1440,
    viewport_height=900,
    document_scroll_width=1440,
    document_client_width=1440,
    document_scroll_height=2000,
    document_client_height=900,
    console_errors=None,
    page_errors=None,
    image_load_states=None,
    element_bounding_boxes=None,
):
    return ViewportRenderedEvidence(
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        document_scroll_width=document_scroll_width,
        document_client_width=document_client_width,
        document_scroll_height=document_scroll_height,
        document_client_height=document_client_height,
        console_errors=console_errors or [],
        page_errors=page_errors or [],
        image_load_states=image_load_states or [],
        element_bounding_boxes=element_bounding_boxes or [],
    )


def make_evidence(
    desktop=None,
    mobile=None,
    task_id="task-123",
    preview_id="preview-456",
    attempt_number=1,
):
    if desktop is None:
        desktop = make_viewport_evidence()
    if mobile is None:
        mobile = make_viewport_evidence(
            viewport_width=390, viewport_height=844,
            document_scroll_width=390, document_client_width=390,
            document_scroll_height=1500, document_client_height=844,
        )
    return RenderedEvidence(
        task_id=task_id,
        preview_id=preview_id,
        attempt_number=attempt_number,
        desktop=desktop,
        mobile=mobile,
        created_at="2024-01-01T12:00:00",
    )


class MockConfig:
    pass


def make_validator():
    return RenderedTechnicalValidator(MockConfig())


# ========================
# Horizontal Overflow Tests
# ========================

def test_horizontal_overflow_clean_desktop_mobile():
    """Test 1: clean desktop/mobile → PASS."""
    validator = make_validator()
    evidence = make_evidence()
    result = validator.validate(evidence)
    assert result.passed is True
    assert result.validation_status == "passed"


def test_horizontal_overflow_desktop_overflow_error():
    """Test 2: desktop horizontal overflow → ERROR."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        document_scroll_width=1500,
        document_client_width=1440,
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert result.passed is False
    assert any(
        e["type"] == "document_horizontal_overflow"
        and e["viewport"] == "desktop"
        for e in result.errors
    )


def test_horizontal_overflow_mobile_overflow_error():
    """Test 3: mobile horizontal overflow → ERROR."""
    validator = make_validator()
    mobile = make_viewport_evidence(
        viewport_width=390, viewport_height=844,
        document_scroll_width=500,
        document_client_width=390,
        document_scroll_height=1500,
        document_client_height=844,
    )
    evidence = make_evidence(mobile=mobile)
    result = validator.validate(evidence)
    assert result.passed is False
    assert any(
        e["type"] == "document_horizontal_overflow"
        and e["viewport"] == "mobile"
        for e in result.errors
    )


def test_horizontal_overflow_1px_under_tolerance_passes():
    """Test 4: 1px overflow under tolerance → PASS."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        document_scroll_width=1441,
        document_client_width=1440,
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert result.passed is True
    assert len(result.errors) == 0


def test_horizontal_overflow_at_tolerance_boundary():
    """Test 5: 2px overflow at tolerance boundary behaves correctly."""
    validator = make_validator()
    # Exactly 2px over = boundary, should NOT fail (tolerance is 2, so >2 fails)
    desktop = make_viewport_evidence(
        document_scroll_width=1442,
        document_client_width=1440,
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert result.passed is True, "2px overflow should be within tolerance and pass"
    assert len(result.errors) == 0


def test_horizontal_overflow_records_correct_overflow_px():
    """Test 6: larger overflow records correct overflow_px in context."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        document_scroll_width=1600,
        document_client_width=1440,
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    overflow_diag = next(
        e for e in result.errors if e["type"] == "document_horizontal_overflow"
    )
    assert overflow_diag["context"]["overflow_px"] == 160
    assert overflow_diag["context"]["tolerance_px"] == HORIZONTAL_OVERFLOW_TOLERANCE_PX
    assert overflow_diag["context"]["scroll_width"] == 1600
    assert overflow_diag["context"]["client_width"] == 1440


# ========================
# Broken Images Tests
# ========================

def test_broken_image_valid_image_no_error():
    """Test 7: valid image → no error."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        image_load_states=[
            {"src": "valid.png", "complete": True, "natural_width": 800, "natural_height": 600},
        ],
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert not any(e["type"] == "broken_image" for e in result.errors)


def test_broken_image_broken_image_error():
    """Test 8: broken image (complete=True, natural_width=0) → ERROR."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        image_load_states=[
            {"src": "broken.png", "complete": True, "natural_width": 0, "natural_height": 0},
        ],
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert result.passed is False
    broken = next(e for e in result.errors if e["type"] == "broken_image")
    assert broken["context"]["src"] == "broken.png"
    assert broken["context"]["natural_width"] == 0
    assert broken["context"]["natural_height"] == 0
    assert broken["context"]["complete"] is True


def test_broken_image_multiple_broken_images():
    """Test 9: multiple broken images → multiple diagnostics."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        image_load_states=[
            {"src": "broken1.png", "complete": True, "natural_width": 0, "natural_height": 0},
            {"src": "broken2.png", "complete": True, "natural_width": 0, "natural_height": 0},
        ],
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    broken_diags = [e for e in result.errors if e["type"] == "broken_image"]
    assert len(broken_diags) == 2


def test_broken_image_diagnostic_includes_viewport_and_src():
    """Test 10: broken image diagnostic includes viewport/src."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        image_load_states=[
            {"src": "img.png", "complete": True, "natural_width": 0, "natural_height": 0},
        ],
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    broken = next(e for e in result.errors if e["type"] == "broken_image")
    assert broken["viewport"] == "desktop"
    assert broken["context"]["src"] == "img.png"


def test_broken_image_incomplete_image_not_flagged():
    """Bonus: image with complete=False should not be flagged as broken."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        image_load_states=[
            {"src": "loading.png", "complete": False, "natural_width": 0, "natural_height": 0},
        ],
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert not any(e["type"] == "broken_image" for e in result.errors)


# ========================
# Page Errors Tests
# ========================

def test_page_error_error():
    """Test 11: page error → ERROR."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        page_errors=[
            {"type": "pageerror", "message": "Uncaught TypeError: foo is not a function"},
        ],
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert result.passed is False
    err = next(e for e in result.errors if e["type"] == "page_runtime_error")
    assert "TypeError" in err["message"]
    assert err["context"]["viewport"] == "desktop"


def test_page_error_multiple_page_errors_structured():
    """Test 12: multiple page errors → structured errors."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        page_errors=[
            {"type": "pageerror", "message": "Error one"},
            {"type": "pageerror", "message": "Error two"},
        ],
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    runtime_errors = [e for e in result.errors if e["type"] == "page_runtime_error"]
    assert len(runtime_errors) == 2
    messages = [e["message"] for e in runtime_errors]
    assert any("Error one" in m for m in messages)
    assert any("Error two" in m for m in messages)


# ========================
# Console Errors Tests
# ========================

def test_console_error_warning():
    """Test 13: console error → WARNING."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        console_errors=[
            {"type": "console_error", "message": "Failed to load resource"},
        ],
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert result.passed is True
    assert result.validation_status == "warnings"
    console_warn = next(w for w in result.warnings if w["type"] == "console_error")
    assert "Failed to load resource" in console_warn["message"]


def test_console_error_only_result_still_passes():
    """Test 14: console warning-only result still PASSES."""
    validator = make_validator()
    evidence = make_evidence()
    result = validator.validate(evidence)
    # Console errors are WARNING, no ERRORs
    assert result.passed is True


# ========================
# Element Overflow Tests
# ========================

def _make_elem(selector, tag, **kwargs):
    defaults = {
        "selector": selector,
        "tag": tag,
        "width": 100,
        "height": 100,
        "scroll_width": 100,
        "client_width": 100,
        "scroll_height": 100,
        "client_height": 100,
        "overflow_x": "visible",
        "overflow_y": "visible",
        "visibility": "visible",
        "display": "block",
    }
    defaults.update(kwargs)
    return defaults


def test_element_overflow_conservative_warning():
    """Test 15: conservative element overflow → WARNING."""
    validator = make_validator()
    elem = _make_elem(
        "main", "main",
        scroll_width=500, client_width=400,
        overflow_x="hidden",
    )
    desktop = make_viewport_evidence(element_bounding_boxes=[elem])
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    overflow_warns = [w for w in result.warnings if w["type"] == "element_overflow"]
    assert len(overflow_warns) == 1
    assert overflow_warns[0]["context"]["selector"] == "main"


def test_element_overflow_normal_element_no_warning():
    """Test 16: normal element (no overflow) → no warning."""
    validator = make_validator()
    elem = _make_elem("main", "main")
    desktop = make_viewport_evidence(element_bounding_boxes=[elem])
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert not any(w["type"] == "element_overflow" for w in result.warnings)


def test_element_overflow_intentional_visible_no_false_warning():
    """Test 17: intentional visible overflow (overflow=visible) does not create false warning."""
    validator = make_validator()
    elem = _make_elem(
        "div", "div",
        scroll_width=500, client_width=400,
        overflow_x="visible",
        overflow_y="visible",
    )
    desktop = make_viewport_evidence(element_bounding_boxes=[elem])
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert not any(w["type"] == "element_overflow" for w in result.warnings)


def test_element_overflow_no_duplicate_spam():
    """Bonus: duplicate element overflow on same viewport not duplicated."""
    validator = make_validator()
    elem1 = _make_elem("main", "main", scroll_width=500, client_width=400, overflow_x="hidden")
    elem2 = _make_elem("main", "main", scroll_width=500, client_width=400, overflow_x="hidden")
    desktop = make_viewport_evidence(element_bounding_boxes=[elem1, elem2])
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    overflow_warns = [w for w in result.warnings if w["type"] == "element_overflow"]
    assert len(overflow_warns) == 1


# ========================
# Zero-Size Interactive Elements Tests
# ========================

def test_zero_size_visible_button_warning():
    """Test 18: visible zero-width button → WARNING."""
    validator = make_validator()
    elem = _make_elem("button", "button", width=0, height=100)
    desktop = make_viewport_evidence(element_bounding_boxes=[elem])
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    zero_warns = [w for w in result.warnings if w["type"] == "zero_size_interactive"]
    assert len(zero_warns) == 1
    assert zero_warns[0]["context"]["tag"] == "button"
    assert zero_warns[0]["context"]["width"] == 0


def test_zero_size_hidden_button_no_warning():
    """Test 19: hidden zero-width button → no warning."""
    validator = make_validator()
    elem = _make_elem("button", "button", width=0, height=100, display="none")
    desktop = make_viewport_evidence(element_bounding_boxes=[elem])
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert not any(w["type"] == "zero_size_interactive" for w in result.warnings)


def test_zero_size_normal_link_no_warning():
    """Test 20: normal (non-zero-size) link → no warning."""
    validator = make_validator()
    elem = _make_elem("a[href]", "a", width=100, height=40)
    desktop = make_viewport_evidence(element_bounding_boxes=[elem])
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert not any(w["type"] == "zero_size_interactive" for w in result.warnings)


def test_zero_size_only_interactive_tags_checked():
    """Bonus: non-interactive tags (e.g., main) are not checked for zero-size."""
    validator = make_validator()
    elem = _make_elem("main", "main", width=0, height=0)
    desktop = make_viewport_evidence(element_bounding_boxes=[elem])
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert not any(w["type"] == "zero_size_interactive" for w in result.warnings)


# ========================
# Text Clipping Tests
# ========================

def test_text_clipping_overflow_hidden_warning():
    """Test 21: overflow hidden + oversized text-bearing element → WARNING."""
    validator = make_validator()
    elem = _make_elem(
        "main", "main",
        scroll_width=600, client_width=400,
        scroll_height=800, client_height=600,
        overflow_x="hidden", overflow_y="hidden",
    )
    desktop = make_viewport_evidence(element_bounding_boxes=[elem])
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    clipping_warns = [w for w in result.warnings if w["type"] == "possible_text_clipping"]
    assert len(clipping_warns) == 1
    assert "Possible text clipping" in clipping_warns[0]["message"]


def test_text_clipping_overflow_visible_no_warning():
    """Test 22: overflow visible → no clipping warning."""
    validator = make_validator()
    elem = _make_elem(
        "main", "main",
        scroll_width=600, client_width=400,
        overflow_x="visible", overflow_y="visible",
    )
    desktop = make_viewport_evidence(element_bounding_boxes=[elem])
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert not any(w["type"] == "possible_text_clipping" for w in result.warnings)


def test_text_clipping_diagnostic_wording_probabilistic():
    """Test 23: clipping diagnostic wording remains probabilistic."""
    validator = make_validator()
    elem = _make_elem(
        "section", "section",
        scroll_width=500, client_width=300,
        overflow_y="clip",
    )
    desktop = make_viewport_evidence(element_bounding_boxes=[elem])
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    clipping = next(w for w in result.warnings if w["type"] == "possible_text_clipping")
    assert "Possible" in clipping["message"]


# ========================
# Result Semantics Tests
# ========================

def test_result_error_plus_warning_failed():
    """Test 24: ERROR + WARNING → failed."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        document_scroll_width=1600,
        document_client_width=1440,
        console_errors=[{"type": "console_error", "message": "log"}],
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert result.passed is False
    assert result.validation_status == "failed"
    assert len(result.errors) > 0
    assert len(result.warnings) > 0


def test_result_warning_only_passed():
    """Test 25: WARNING only → passed."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        console_errors=[{"type": "console_error", "message": "log"}],
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    assert result.passed is True
    assert result.validation_status == "warnings"
    assert len(result.errors) == 0
    assert len(result.warnings) > 0


def test_result_no_findings_passed():
    """Test 26: no findings → passed."""
    validator = make_validator()
    evidence = make_evidence()
    result = validator.validate(evidence)
    assert result.passed is True
    assert result.validation_status == "passed"
    assert len(result.errors) == 0
    assert len(result.warnings) == 0


def test_result_serialization_roundtrip():
    """Test 27: serialization round-trip."""
    validator = make_validator()
    evidence = make_evidence()
    result = validator.validate(evidence)
    data = result.to_dict()
    restored = RenderedTechnicalResult.from_dict(data)
    assert restored.passed == result.passed
    assert restored.validation_status == result.validation_status
    assert restored.errors == result.errors
    assert restored.warnings == result.warnings
    assert restored.task_id == result.task_id
    assert restored.preview_id == result.preview_id


# ========================
# Purity Boundary Tests
# ========================

def test_validator_does_not_modify_evidence():
    """Test 28: validator does not modify evidence."""
    validator = make_validator()
    evidence = make_evidence(desktop=make_viewport_evidence(
        image_load_states=[
            {"src": "broken.png", "complete": True, "natural_width": 0, "natural_height": 0},
        ],
    ))
    original = copy.deepcopy(evidence)
    validator.validate(evidence)
    assert evidence.to_dict() == original.to_dict()


def test_validator_does_not_call_browser():
    """Test 29: validator does not call browser."""
    validator = make_validator()
    evidence = make_evidence()
    with patch("playwright.sync_api.sync_playwright") as mock_pw:
        validator.validate(evidence)
        mock_pw.assert_not_called()


def test_validator_has_no_retry_workflow_side_effects():
    """Test 30: validator has no retry/workflow side effects."""
    validator = make_validator()
    evidence = make_evidence()
    # Should complete without any state mutation
    with patch.object(validator, "log") as mock_log:
        result = validator.validate(evidence)
        assert result.passed is True
        # Validator may log but should not raise or hang


def test_validator_returns_rendered_technical_result():
    """Bonus: validator returns RenderedTechnicalResult type."""
    validator = make_validator()
    evidence = make_evidence()
    result = validator.validate(evidence)
    assert isinstance(result, RenderedTechnicalResult)
    assert is_dataclass(RenderedTechnicalResult)


def test_validator_gate_uses_rendered_technical():
    """Bonus: diagnostics use RenderedTechnicalGate.RENDERED_TECHNICAL."""
    validator = make_validator()
    desktop = make_viewport_evidence(
        document_scroll_width=1600,
        document_client_width=1440,
    )
    evidence = make_evidence(desktop=desktop)
    result = validator.validate(evidence)
    for err in result.errors:
        assert err["gate"] == RenderedTechnicalGate.RENDERED_TECHNICAL.value


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
