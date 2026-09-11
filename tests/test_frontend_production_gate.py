#!/usr/bin/env python3
"""Unit tests for FrontendProductionQualityGate."""

import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.frontend_production_gate import FrontendProductionQualityGate
from tools import BaseTool


class MockConfig:
    """Mock configuration for testing."""
    prod_max_external_scripts = 5
    prod_max_external_stylesheets = 3
    prod_max_external_fonts = 2
    prod_max_font_families = 3
    prod_max_inline_style_size = 1024  # 1KB for testing
    prod_max_inline_script_size = 1024  # 1KB for testing
    prod_max_important_count = 10
    prod_max_selector_depth = 4
    prod_max_inline_style_ratio = 0.15
    prod_max_animated_elements = 10
    prod_suspicious_fixed_width = 800
    prod_min_heading_level_diff = 1


class TestFrontendProductionQualityGate(unittest.TestCase):
    """Tests for FrontendProductionQualityGate."""
    
    def setUp(self):
        self.gate = FrontendProductionQualityGate(MockConfig())
    
    # ==================== PERFORMANCE / ASSET QUALITY ====================
    
    def test_images_pass_with_dimensions_and_lazy(self):
        """Images with width, height, and lazy loading should pass."""
        html = '<img src="test.jpg" width="800" height="600" loading="lazy" alt="Test">'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(result.passed, f"Expected pass, got errors: {result.errors}")
    
    def test_images_fail_missing_dimensions(self):
        """Images missing width/height should produce warning."""
        html = '<img src="test.jpg" loading="lazy" alt="Test">'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(result.passed)  # warning doesn't fail
        self.assertTrue(any(w['type'] == 'image_missing_dimensions' for w in result.warnings))
    
    def test_images_fail_missing_lazy(self):
        """Non-hero images missing loading=lazy should produce warning."""
        html = '<img src="test.jpg" width="800" height="600" alt="Test">'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'image_missing_lazy' for w in result.warnings))
    
    def test_images_fail_missing_alt(self):
        """Images missing alt attribute entirely should fail."""
        html = '<img src="test.jpg" width="800" height="600">'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertFalse(result.passed)
        self.assertTrue(any(e['type'] == 'image_missing_alt' for e in result.errors))
    
    def test_images_alt_empty_is_valid(self):
        """Empty alt=\"\" for decorative images should pass."""
        html = '<img src="decoration.jpg" width="100" height="100" alt="">'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(result.passed, f"Expected pass, got errors: {result.errors}")
    
    def test_external_scripts_within_budget_pass(self):
        """External scripts within budget should pass."""
        html = '<script src="https://cdn.example.com/a.js"></script><script src="https://cdn.example.com/b.js"></script>'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(result.passed, f"Expected pass, got errors: {result.errors}")
    
    def test_external_scripts_exceed_budget_fail(self):
        """External scripts exceeding budget should fail."""
        html = ''.join(f'<script src="https://cdn.example.com/{i}.js"></script>' for i in range(6))
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertFalse(result.passed)
        self.assertTrue(any(e['type'] == 'excessive_external_scripts' for e in result.errors))
    
    def test_external_stylesheets_exceed_budget_fail(self):
        """External stylesheets exceeding budget should fail."""
        html = ''.join(f'<link rel="stylesheet" href="https://cdn.example.com/{i}.css">' for i in range(4))
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertFalse(result.passed)
        self.assertTrue(any(e['type'] == 'excessive_external_stylesheets' for e in result.errors))
    
    def test_external_fonts_exceed_budget_warning(self):
        """External fonts exceeding budget should warn."""
        html = ''.join(f'<link rel="preload" as="font" href="https://fonts.example.com/{i}.woff2">' for i in range(3))
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'excessive_external_fonts' for w in result.warnings))
    
    def test_font_families_within_budget_pass(self):
        """Font families within budget should pass."""
        css = "body { font-family: Inter, Arial, sans-serif; } h1 { font-family: Roboto, sans-serif; }"
        html = "<div>Test</div>"
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(result.passed, f"Expected pass, got errors: {result.errors}")
    
    def test_font_families_exceed_budget_warning(self):
        """Many font families should warn."""
        css = """
        .f1 { font-family: Font1; } .f2 { font-family: Font2; }
        .f3 { font-family: Font3; } .f4 { font-family: Font4; }
        .f5 { font-family: Font5; }
        """
        html = "<div>Test</div>"
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'excessive_font_families' for w in result.warnings))
    
    # ==================== CODE QUALITY / MAINTAINABILITY ====================
    
    def test_inline_css_within_budget_pass(self):
        """Inline CSS within budget should pass."""
        html = '<style>body { color: #333; }</style>'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(result.passed, f"Expected pass, got errors: {result.errors}")
    
    def test_inline_css_exceed_budget_fail(self):
        """Inline CSS exceeding budget should fail."""
        large_css = "body {" + "color: #333; " * 500 + "}"
        html = f'<style>{large_css}</style>'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertFalse(result.passed)
        self.assertTrue(any(e['type'] == 'excessive_inline_css' for e in result.errors))
    
    def test_inline_js_exceed_budget_fail(self):
        """Inline JS exceeding budget should fail."""
        large_js = "const x = " + "'a'.repeat(100); " * 200
        html = f'<script>{large_js}</script>'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertFalse(result.passed)
        self.assertTrue(any(e['type'] == 'excessive_inline_js' for e in result.errors))
    
    def test_important_overuse_fail(self):
        """Excessive !important should fail."""
        css = "body {" + "color: red !important; " * 15 + "}"
        html = "<div>Test</div>"
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertFalse(result.passed)
        self.assertTrue(any(e['type'] == 'excessive_important' for e in result.errors))
    
    def test_important_moderate_warning(self):
        """Moderate !important should warn."""
        css = "body {" + "color: red !important; " * 6 + "}"
        html = "<div>Test</div>"
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'moderate_important' for w in result.warnings))
    
    def test_selector_depth_warning(self):
        """Deep selectors should warn."""
        css = "body > div > section > article > div > p { color: red; }"
        html = "<div>Test</div>"
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'deep_selector' for w in result.warnings))
    
    def test_inline_style_overuse_warning(self):
        """High inline style ratio should warn."""
        html = ''.join(f'<div style="color: red;">Content</div>' for _ in range(20))
        html += '<div>Normal</div>' * 5
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'inline_style_overuse' for w in result.warnings))
    
    # ==================== ANIMATION BUDGET ====================
    
    def test_animation_budget_pass_lightweight(self):
        """Lightweight scope should skip animation budget."""
        html = '<div class="animated fade-in slide-up zoom bounce rotate"></div>' * 15
        css = "@keyframes fade { from { opacity: 0; } to { opacity: 1; } } " * 15
        js = ""
        result = self.gate.check(html, css, js, "article", "BLOG_POST")
        self.assertTrue(result.passed, f"Expected pass for lightweight, got errors: {result.errors}")
    
    def test_animation_budget_fail_full_page(self):
        """Full page scope with too many animations should fail."""
        html = '<div class="animated fade-in slide-up zoom bounce rotate"></div>' * 15
        css = "@keyframes fade { from { opacity: 0; } to { opacity: 1; } } " * 15
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertFalse(result.passed)
        self.assertTrue(any(e['type'] == 'animation_budget_exceeded' for e in result.errors))
    
    def test_scroll_hijacking_warning(self):
        """Scroll hijacking patterns should warn."""
        js = "window.addEventListener('wheel', function(e) { e.preventDefault(); }, { passive: false });"
        html = "<div>Test</div>"
        css = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'scroll_hijacking_risk' for w in result.warnings))
    
    def test_particle_effect_warning(self):
        """Particle system indicators should warn."""
        js = "import Particles from 'particles.js'; new Particles();"
        html = "<div>Test</div>"
        css = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'particle_effect_risk' for w in result.warnings))
    
    def test_webgl_heavy_warning(self):
        """WebGL indicators should warn."""
        js = "const gl = canvas.getContext('webgl');"
        html = "<div>Test</div>"
        css = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'webgl_heavy_risk' for w in result.warnings))
    
    # ==================== RESPONSIVE SAFETY ====================
    
    def test_large_fixed_width_warning(self):
        """Large fixed width elements should warn."""
        html = '<div style="width: 1200px;">Content</div>'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'large_fixed_width' for w in result.warnings))
    
    def test_no_media_queries_with_fixed_width_warning(self):
        """Fixed width without media queries should warn."""
        html = '<div style="width: 1200px;">Content</div>'
        css = "body { color: red; }"
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'no_media_queries' for w in result.warnings))
    
    # ==================== ACCESSIBILITY ====================
    
    def test_missing_alt_fail(self):
        """Missing alt attribute should fail."""
        html = '<img src="test.jpg" width="100" height="100">'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertFalse(result.passed)
        self.assertTrue(any(e['type'] == 'image_missing_alt' for e in result.errors))
    
    def test_unlabeled_form_control_warning(self):
        """Unlabeled form controls should warn."""
        html = '<input type="text" name="email">'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'unlabeled_form_control' for w in result.warnings))
    
    def test_labeled_form_control_pass(self):
        """Properly labeled form controls should pass."""
        html = '<label for="email">Email</label><input type="text" id="email" name="email">'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(result.passed, f"Expected pass, got warnings: {result.warnings}")
    
    def test_aria_labeled_form_control_pass(self):
        """ARIA labeled form controls should pass."""
        html = '<input type="text" aria-label="Email">'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(result.passed, f"Expected pass, got warnings: {result.warnings}")
    
    def test_heading_hierarchy_skip_warning(self):
        """Heading level skips should warn."""
        html = '<h1>Title</h1><h4>Subtitle</h4>'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'heading_hierarchy_skip' for w in result.warnings))
    
    def test_heading_hierarchy_valid_pass(self):
        """Valid heading hierarchy should pass."""
        html = '<h1>Title</h1><h2>Subtitle</h2><h3>Section</h3>'
        css = ""
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(result.passed, f"Expected pass, got warnings: {result.warnings}")
    
    # ==================== SCOPE BEHAVIOR ====================
    
    def test_lightweight_scope_skips_animation_budget(self):
        """Article/blog scope should skip animation budget checks."""
        html = '<div class="animated"></div>' * 20
        css = "@keyframes a { from {} to {} } " * 20
        js = ""
        result = self.gate.check(html, css, js, "article", "BLOG_POST")
        self.assertTrue(result.passed, f"Lightweight scope should skip animation budget, got: {result.errors}")
        self.assertTrue(result.diagnostics.get('is_lightweight'))
    
    def test_full_page_scope_enforces_animation_budget(self):
        """Page/landing scope should enforce animation budget."""
        html = '<div class="animated"></div>' * 20
        css = "@keyframes a { from {} to {} } " * 20
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertFalse(result.passed)
        self.assertFalse(result.diagnostics.get('is_lightweight'))
        self.assertTrue(any(e['type'] == 'animation_budget_exceeded' for e in result.errors))
    
    def test_lightweight_scope_skips_animation_anti_patterns(self):
        """Lightweight scope should skip animation anti-pattern checks."""
        js = "window.addEventListener('wheel', e => e.preventDefault(), { passive: false });"
        html = "<div>Test</div>"
        css = ""
        result = self.gate.check(html, css, js, "section", "BLOG_POST")
        self.assertTrue(result.passed, f"Lightweight scope should skip scroll hijacking check, got: {result.warnings}")
    
    def test_full_page_scope_enforces_animation_anti_patterns(self):
        """Full page scope should enforce animation anti-pattern checks."""
        js = "window.addEventListener('wheel', e => e.preventDefault(), { passive: false });"
        html = "<div>Test</div>"
        css = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertTrue(any(w['type'] == 'scroll_hijacking_risk' for w in result.warnings))
    
    # ==================== DIAGNOSTICS ====================
    
    def test_diagnostics_populated(self):
        """Diagnostics should contain useful information."""
        html = '<img src="test.jpg" width="100" height="100" alt="Test"><script src="a.js"></script>'
        css = "body { font-family: Inter; }"
        js = ""
        result = self.gate.check(html, css, js, "page", "PAGE")
        self.assertIn('image_count', result.diagnostics)
        self.assertIn('external_script_count', result.diagnostics)
        self.assertIn('distinct_font_families', result.diagnostics)
        self.assertEqual(result.diagnostics['image_count'], 1)
        self.assertEqual(result.diagnostics['external_script_count'], 1)
        self.assertEqual(result.diagnostics['distinct_font_families'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)