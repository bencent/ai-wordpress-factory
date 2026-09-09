#!/usr/bin/env python3
"""Focused tests for Phase 7B-1 frontend contracts."""

from contracts import FrontendRequest, FrontendResult, GreenLightConversionResult


def test_frontend_request_defaults():
    req = FrontendRequest()
    assert req.task_id == ""
    assert req.frontend_scope == "page"
    assert req.animation_required is False
    assert req.design_brief == ""
    assert req.brand_constraints == {}
    assert req.seo_title == ""
    assert req.seo_description == ""
    assert req.seo_keywords == []
    assert req.content_outline == []
    assert req.metadata == {}


def test_frontend_request_list_fields_independent():
    req1 = FrontendRequest()
    req2 = FrontendRequest()
    req1.seo_keywords.append("test")
    assert req2.seo_keywords == []


def test_frontend_result_defaults():
    res = FrontendResult()
    assert res.task_id == ""
    assert res.success is False
    assert res.html is None
    assert res.css is None
    assert res.javascript is None
    assert res.blocks is None
    assert res.validation_status == "pending"
    assert res.errors == []
    assert res.warnings == []
    assert res.animation_used is False
    assert res.gsap_subskills_loaded == []
    assert res.metadata == {}


def test_frontend_result_html_css_js_fields():
    res = FrontendResult(html="<div></div>", css="body{}", javascript="void 0")
    assert res.html == "<div></div>"
    assert res.css == "body{}"
    assert res.javascript == "void 0"


def test_greenlight_conversion_result_success():
    res = GreenLightConversionResult(success=True, blocks="<!-- wp:... -->")
    assert res.success is True
    assert res.blocks == "<!-- wp:... -->"
    assert res.errors == []
    assert res.warnings == []


def test_greenlight_conversion_result_failure():
    res = GreenLightConversionResult(success=False, errors=["convert failed"])
    assert res.success is False
    assert res.blocks is None
    assert res.errors == ["convert failed"]


def test_greenlight_conversion_result_mutable_defaults_independent():
    res1 = GreenLightConversionResult()
    res2 = GreenLightConversionResult()
    res1.errors.append("e1")
    res1.warnings.append("w1")
    assert res2.errors == []
    assert res2.warnings == []


def test_existing_contracts_unchanged():
    from contracts import ReviewResult, ImageGenerationRequest, ImageGenerationResult
    rr = ReviewResult()
    assert rr.passed is False
    assert rr.score == 0
    igr = ImageGenerationRequest()
    assert igr.task_id == ""
    assert igr.provider == "openai"
    igs = ImageGenerationResult()
    assert igs.task_id == ""
    assert igs.success is False


if __name__ == "__main__":
    test_frontend_request_defaults()
    test_frontend_request_list_fields_independent()
    test_frontend_result_defaults()
    test_frontend_result_html_css_js_fields()
    test_greenlight_conversion_result_success()
    test_greenlight_conversion_result_failure()
    test_greenlight_conversion_result_mutable_defaults_independent()
    test_existing_contracts_unchanged()
    print("All Phase 7B-1 contract tests passed.")
