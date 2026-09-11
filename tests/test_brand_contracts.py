#!/usr/bin/env python3
"""Tests for Phase 7C-4A Brand contracts and production rules."""

import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import (
    BrandProductionRules, BrandProfile, ClientProfile,
    ApprovalPolicy, ApprovalPolicyMode, RuleSource
)


class TestBrandContracts(unittest.TestCase):
    """Tests for BrandProductionRules, BrandProfile, ClientProfile integration."""

    def test_brand_production_rules_serialization_roundtrip(self):
        """Test BrandProductionRules serialization round-trip."""
        rules = BrandProductionRules(
            allowed_font_families=["Noto Sans TC", "Inter", "Roboto"],
            allowed_colors=["#2C2420", "#FFFFFF", "#0066CC"],
            max_content_width_px=1200,
            allowed_border_radius_px=[2, 4, 8, 12],
        )
        
        data = rules.to_dict()
        restored = BrandProductionRules.from_dict(data)
        
        self.assertEqual(restored.allowed_font_families, ["Noto Sans TC", "Inter", "Roboto"])
        self.assertEqual(restored.allowed_colors, ["#2C2420", "#FFFFFF", "#0066CC"])
        self.assertEqual(restored.max_content_width_px, 1200)
        self.assertEqual(restored.allowed_border_radius_px, [2, 4, 8, 12])

    def test_brand_production_rules_partial_serialization(self):
        """Test BrandProductionRules with only required fields."""
        rules = BrandProductionRules(allowed_font_families=["Arial"])
        
        data = rules.to_dict()
        restored = BrandProductionRules.from_dict(data)
        
        self.assertEqual(restored.allowed_font_families, ["Arial"])
        self.assertIsNone(restored.allowed_colors)
        self.assertIsNone(restored.max_content_width_px)
        self.assertIsNone(restored.allowed_border_radius_px)

    def test_brand_profile_serialization_roundtrip(self):
        """Test BrandProfile serialization round-trip."""
        rules = BrandProductionRules(
            allowed_font_families=["Noto Sans TC"],
            allowed_colors=["#2C2420"],
            max_content_width_px=1200,
            allowed_border_radius_px=[4, 8],
        )
        profile = BrandProfile(
            brand_id="brand-123",
            display_name="Test Brand",
            production_rules=rules,
        )
        
        data = profile.to_dict()
        restored = BrandProfile.from_dict(data)
        
        self.assertEqual(restored.brand_id, "brand-123")
        self.assertEqual(restored.display_name, "Test Brand")
        self.assertEqual(restored.production_rules.allowed_font_families, ["Noto Sans TC"])
        self.assertEqual(restored.production_rules.allowed_colors, ["#2C2420"])
        self.assertEqual(restored.production_rules.max_content_width_px, 1200)
        self.assertEqual(restored.production_rules.allowed_border_radius_px, [4, 8])

    def test_client_profile_with_brand_profile_serialization_roundtrip(self):
        """Test ClientProfile with BrandProfile serialization round-trip."""
        rules = BrandProductionRules(allowed_font_families=["Noto Sans TC"])
        brand_profile = BrandProfile(
            brand_id="brand-123",
            display_name="Test Brand",
            production_rules=rules,
        )
        approval_policy = ApprovalPolicy(mode=ApprovalPolicyMode.AUTO_PUBLISH)
        client = ClientProfile(
            client_id="client-456",
            display_name="Test Client",
            approval_policy=approval_policy,
            brand_profile=brand_profile,
        )
        
        data = client.to_dict()
        restored = ClientProfile.from_dict(data)
        
        self.assertEqual(restored.client_id, "client-456")
        self.assertEqual(restored.display_name, "Test Client")
        self.assertIsNotNone(restored.approval_policy)
        self.assertEqual(restored.approval_policy.mode, ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertIsNotNone(restored.brand_profile)
        self.assertEqual(restored.brand_profile.brand_id, "brand-123")
        self.assertEqual(restored.brand_profile.production_rules.allowed_font_families, ["Noto Sans TC"])

    def test_client_profile_without_brand_profile_still_loads(self):
        """Test old ClientProfile without brand_profile still loads (backward compat)."""
        # Simulate old saved state without brand_profile
        old_data = {
            "client_id": "client-old",
            "display_name": "Old Client",
            "locale": "en_US",
            "approval_policy": {"mode": "require_human_review"},
            "metadata": {},
        }
        
        restored = ClientProfile.from_dict(old_data)
        
        self.assertEqual(restored.client_id, "client-old")
        self.assertEqual(restored.display_name, "Old Client")
        self.assertIsNotNone(restored.approval_policy)
        self.assertEqual(restored.approval_policy.mode, ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        self.assertIsNone(restored.brand_profile)

    def test_rule_source_enum(self):
        """Test RuleSource enum values."""
        self.assertEqual(RuleSource.GLOBAL.value, "global")
        self.assertEqual(RuleSource.BRAND.value, "brand")


class TestBrandProductionRulesIntegration(unittest.TestCase):
    """Integration tests for brand rules in FrontendProductionQualityGate."""

    def setUp(self):
        from tools.frontend_production_gate import FrontendProductionQualityGate
        
        class MockConfig:
            prod_max_external_scripts = 5
            prod_max_external_stylesheets = 3
            prod_max_external_fonts = 2
            prod_max_font_families = 3
            prod_max_inline_style_size = 1024
            prod_max_inline_script_size = 1024
            prod_max_important_count = 10
            prod_max_selector_depth = 4
            prod_max_inline_style_ratio = 0.15
            prod_max_animated_elements = 10
            prod_suspicious_fixed_width = 800
            prod_min_heading_level_diff = 1
        
        self.gate = FrontendProductionQualityGate(MockConfig())

    # ==================== FONT RULE TESTS ====================

    def test_brand_allowed_font_pass(self):
        """Brand allowed font should PASS."""
        brand_rules = BrandProductionRules(allowed_font_families=["Noto Sans TC"])
        css = 'body { font-family: "Noto Sans TC", Arial, sans-serif; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed, f"Expected pass, got errors: {result.errors}")
        # No BRAND font error
        brand_errors = [e for e in result.errors if e.get("rule_source") == "brand" and "font" in e.get("type", "")]
        self.assertEqual(len(brand_errors), 0)

    def test_brand_disallowed_primary_font_error(self):
        """Disallowed primary font should produce BRAND ERROR."""
        brand_rules = BrandProductionRules(allowed_font_families=["Noto Sans TC"])
        css = 'body { font-family: Arial, sans-serif; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertFalse(result.passed)
        brand_errors = [e for e in result.errors if e.get("rule_source") == "brand" and "font" in e.get("type", "")]
        self.assertEqual(len(brand_errors), 1)
        self.assertEqual(brand_errors[0]["severity"], "error")
        self.assertIn("Arial", brand_errors[0]["message"])

    def test_brand_fallback_font_not_violation(self):
        """Fallback font should NOT trigger violation."""
        brand_rules = BrandProductionRules(allowed_font_families=["Noto Sans TC"])
        css = 'body { font-family: "Noto Sans TC", Arial, sans-serif; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed, f"Expected pass, got errors: {result.errors}")
        brand_errors = [e for e in result.errors if e.get("rule_source") == "brand" and "font" in e.get("type", "")]
        self.assertEqual(len(brand_errors), 0)

    def test_brand_generic_fallback_not_violation(self):
        """Generic fallback families should not be violations."""
        brand_rules = BrandProductionRules(allowed_font_families=["CustomFont"])
        css = 'body { font-family: CustomFont, serif, sans-serif, monospace; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed, f"Expected pass, got errors: {result.errors}")

    def test_brand_multiple_font_declarations(self):
        """Multiple font-family declarations all checked."""
        brand_rules = BrandProductionRules(allowed_font_families=["Noto Sans TC", "Roboto"])
        css = '''
        body { font-family: "Noto Sans TC", sans-serif; }
        h1 { font-family: Roboto, sans-serif; }
        .special { font-family: Arial, sans-serif; }
        '''
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertFalse(result.passed)
        brand_errors = [e for e in result.errors if e.get("rule_source") == "brand" and "font" in e.get("type", "")]
        self.assertEqual(len(brand_errors), 1)
        self.assertIn("Arial", brand_errors[0]["message"])

    # ==================== COLOR RULE TESTS ====================

    def test_brand_allowed_literal_color_pass(self):
        """Allowed literal color should pass."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_colors=["#2C2420", "#FFFFFF", "rgb(0, 102, 204)"],
        )
        css = 'body { color: #2C2420; background: #FFFFFF; border-color: rgb(0, 102, 204); }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed, f"Expected pass, got warnings: {result.warnings}")
        brand_warnings = [w for w in result.warnings if w.get("rule_source") == "brand" and "color" in w.get("type", "")]
        self.assertEqual(len(brand_warnings), 0)

    def test_brand_disallowed_literal_color_warning(self):
        """Disallowed literal color should produce BRAND WARNING."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_colors=["#2C2420"],
        )
        css = 'body { color: #FF0000; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        # Gate should still PASS (warning only)
        self.assertTrue(result.passed)
        brand_warnings = [w for w in result.warnings if w.get("rule_source") == "brand" and "color" in w.get("type", "")]
        self.assertEqual(len(brand_warnings), 1)
        self.assertEqual(brand_warnings[0]["severity"], "warning")
        self.assertIn("#FF0000", brand_warnings[0]["message"])

    def test_brand_color_hex_normalization(self):
        """Hex color normalization (#rgb == #rrggbb)."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_colors=["#FF0000"],  # red
        )
        # #f00 should normalize to #ff0000 and match
        css = 'body { color: #f00; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed, f"Expected pass, got warnings: {result.warnings}")
        brand_warnings = [w for w in result.warnings if w.get("rule_source") == "brand" and "color" in w.get("type", "")]
        self.assertEqual(len(brand_warnings), 0)

    def test_brand_color_rgb_normalization(self):
        """rgb() should normalize to hex and match."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_colors=["#FF0000"],
        )
        css = 'body { color: rgb(255, 0, 0); }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed, f"Expected pass, got warnings: {result.warnings}")

    def test_brand_color_hsl_normalization(self):
        """hsl() should normalize to hex and match."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_colors=["#FF0000"],  # red = hsl(0, 100%, 50%)
        )
        css = 'body { color: hsl(0, 100%, 50%); }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed, f"Expected pass, got warnings: {result.warnings}")

    def test_brand_color_css_variable_skipped(self):
        """CSS variables should be skipped (not error, not warning)."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_colors=["#2C2420"],
        )
        css = 'body { color: var(--brand-primary); }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed)
        # Should not produce any brand color warning/error for unresolved var
        brand_color_diags = [d for d in result.warnings + result.errors 
                            if d.get("rule_source") == "brand" and "color" in d.get("type", "")]
        self.assertEqual(len(brand_color_diags), 0)

    def test_brand_color_currentcolor_skipped(self):
        """currentColor should be skipped."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_colors=["#2C2420"],
        )
        css = 'body { color: currentColor; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed)
        brand_color_diags = [d for d in result.warnings + result.errors 
                            if d.get("rule_source") == "brand" and "color" in d.get("type", "")]
        self.assertEqual(len(brand_color_diags), 0)

    def test_brand_color_gradient_skipped(self):
        """Gradients should be skipped."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_colors=["#2C2420"],
        )
        css = 'body { background: linear-gradient(#FF0000, #00FF00); }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed)
        brand_color_diags = [d for d in result.warnings + result.errors 
                            if d.get("rule_source") == "brand" and "color" in d.get("type", "")]
        self.assertEqual(len(brand_color_diags), 0)

    # ==================== MAX CONTENT WIDTH TESTS ====================

    def test_brand_max_content_width_warning(self):
        """Exceeding max content width should produce BRAND WARNING."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            max_content_width_px=1200,
        )
        css = 'body { max-width: 1400px; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        # Gate should still PASS (warning only)
        self.assertTrue(result.passed)
        brand_warnings = [w for w in result.warnings if w.get("rule_source") == "brand" and "max_content_width" in w.get("type", "")]
        self.assertEqual(len(brand_warnings), 1)
        self.assertEqual(brand_warnings[0]["severity"], "warning")
        self.assertIn("1400", brand_warnings[0]["message"])

    def test_brand_max_content_width_within_limit_pass(self):
        """Within max content width should pass."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            max_content_width_px=1200,
        )
        css = 'body { max-width: 1000px; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed)
        brand_warnings = [w for w in result.warnings if w.get("rule_source") == "brand" and "max_content_width" in w.get("type", "")]
        self.assertEqual(len(brand_warnings), 0)

    def test_brand_width_declaration_warning(self):
        """Fixed width declaration exceeding limit should warn."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            max_content_width_px=1200,
        )
        css = '.container { width: 1400px; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed)
        brand_warnings = [w for w in result.warnings if w.get("rule_source") == "brand" and "max_content_width" in w.get("type", "")]
        self.assertEqual(len(brand_warnings), 1)

    # ==================== BORDER RADIUS TESTS ====================

    def test_brand_border_radius_warning(self):
        """Disallowed border radius should produce BRAND WARNING."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_border_radius_px=[2, 4, 8],
        )
        css = '.card { border-radius: 24px; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        # Gate should still PASS (warning only)
        self.assertTrue(result.passed)
        brand_warnings = [w for w in result.warnings if w.get("rule_source") == "brand" and "border_radius" in w.get("type", "")]
        self.assertEqual(len(brand_warnings), 1)
        self.assertEqual(brand_warnings[0]["severity"], "warning")
        self.assertIn("24", brand_warnings[0]["message"])

    def test_brand_border_radius_allowed_pass(self):
        """Allowed border radius should pass."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_border_radius_px=[2, 4, 8, 12],
        )
        css = '.card { border-radius: 8px; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed)
        brand_warnings = [w for w in result.warnings if w.get("rule_source") == "brand" and "border_radius" in w.get("type", "")]
        self.assertEqual(len(brand_warnings), 0)

    def test_brand_border_radius_complex_skipped(self):
        """Complex border-radius values should be skipped."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_border_radius_px=[4, 8],
        )
        # Percentage, slash syntax, calc, var
        css = '''
        .a { border-radius: 50%; }
        .b { border-radius: 8px / 16px; }
        .c { border-radius: calc(4px + 2px); }
        .d { border-radius: var(--radius); }
        '''
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertTrue(result.passed)
        brand_warnings = [w for w in result.warnings if w.get("rule_source") == "brand" and "border_radius" in w.get("type", "")]
        self.assertEqual(len(brand_warnings), 0)

    # ==================== RULE SOURCE TESTS ====================

    def test_global_rule_source(self):
        """Global violations should have rule_source = GLOBAL."""
        css = 'body { color: red !important; }' * 15  # excessive !important
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE")
        
        self.assertFalse(result.passed)
        global_errors = [e for e in result.errors if e.get("rule_source") == "global"]
        self.assertTrue(len(global_errors) > 0)

    def test_brand_rule_source(self):
        """Brand violations should have rule_source = BRAND."""
        brand_rules = BrandProductionRules(allowed_font_families=["Noto Sans TC"])
        css = 'body { font-family: Arial; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertFalse(result.passed)
        brand_errors = [e for e in result.errors if e.get("rule_source") == "brand"]
        self.assertTrue(len(brand_errors) > 0)

    # ==================== NO BRAND PROFILE TESTS ====================

    def test_no_brand_profile_unchanged_behavior(self):
        """No BrandProfile: existing global behavior unchanged."""
        css = 'body { font-family: Arial; }'  # Would violate if brand rules existed
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE")
        
        # Should pass global checks (Arial is fine globally)
        # No brand rules evaluated
        self.assertTrue(result.passed or len([e for e in result.errors if e.get("rule_source") == "brand"]) == 0)
        self.assertFalse(result.diagnostics.get('brand_rules_evaluated', False))

    # ==================== RETRY INTEGRATION TESTS ====================

    def test_brand_error_triggers_retry_feedback(self):
        """Brand ERROR should produce failure feedback suitable for retry."""
        brand_rules = BrandProductionRules(allowed_font_families=["Noto Sans TC"])
        css = 'body { font-family: Arial; }'
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        self.assertFalse(result.passed)
        # Error should have enough info for FrontendAgent to understand it's a brand font issue
        brand_errors = [e for e in result.errors if e.get("rule_source") == "brand"]
        self.assertEqual(len(brand_errors), 1)
        error = brand_errors[0]
        self.assertEqual(error["type"], "brand_font_family_not_allowed")
        self.assertIn("Arial", error["message"])
        self.assertIn("Noto Sans TC", error["context"])

    def test_brand_warning_does_not_fail_gate(self):
        """Brand WARNING should not fail the gate."""
        brand_rules = BrandProductionRules(
            allowed_font_families=["Inter"],
            allowed_colors=["#2C2420"],
            max_content_width_px=1200,
            allowed_border_radius_px=[4, 8],
        )
        css = '''
        body { color: #FF0000; max-width: 1400px; }
        .card { border-radius: 24px; }
        '''
        html = "<div>Test</div>"
        js = ""
        
        result = self.gate.check(html, css, js, "page", "PAGE", brand_rules=brand_rules)
        
        # Should PASS with warnings
        self.assertTrue(result.passed)
        self.assertEqual(result.validation_status, "warnings")
        
        # Count brand warnings
        brand_warnings = [w for w in result.warnings if w.get("rule_source") == "brand"]
        self.assertEqual(len(brand_warnings), 3)  # color, width, border-radius


if __name__ == '__main__':
    unittest.main(verbosity=2)