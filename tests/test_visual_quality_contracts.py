#!/usr/bin/env python3
"""Tests for Visual Quality Contracts (Phase 7D-3A)."""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import (
    VisualQualityAction,
    VisualIssueCategory,
    VisualIssueSeverity,
    VisualQualityIssue,
    VisualQualityResult,
)


class TestVisualQualityAction(unittest.TestCase):
    """Tests for VisualQualityAction enum."""

    def test_exact_values(self):
        """VisualQualityAction has exact required values."""
        self.assertEqual(VisualQualityAction.PASS.value, "pass")
        self.assertEqual(VisualQualityAction.WARN.value, "warn")
        self.assertEqual(VisualQualityAction.HUMAN_REVIEW.value, "human_review")

    def test_no_retry_action_exists(self):
        """RETRY action does NOT exist."""
        self.assertFalse(hasattr(VisualQualityAction, "RETRY"))
        values = [a.value for a in VisualQualityAction]
        self.assertNotIn("retry", values)

    def test_no_fail_action_exists(self):
        """FAIL action does NOT exist."""
        self.assertFalse(hasattr(VisualQualityAction, "FAIL"))
        values = [a.value for a in VisualQualityAction]
        self.assertNotIn("fail", values)

    def test_no_regenerate_action_exists(self):
        """REGENERATE action does NOT exist."""
        self.assertFalse(hasattr(VisualQualityAction, "REGENERATE"))
        values = [a.value for a in VisualQualityAction]
        self.assertNotIn("regenerate", values)


class TestVisualIssueCategory(unittest.TestCase):
    """Tests for VisualIssueCategory enum."""

    def test_exact_v1_set(self):
        """VisualIssueCategory has exact v1 categories."""
        expected = {
            "layout",
            "responsive",
            "typography",
            "spacing",
            "visual_hierarchy",
            "image",
            "cta",
            "brand_consistency",
            "other",
        }
        actual = {c.value for c in VisualIssueCategory}
        self.assertEqual(actual, expected)


class TestVisualIssueSeverity(unittest.TestCase):
    """Tests for VisualIssueSeverity enum."""

    def test_exact_values(self):
        """VisualIssueSeverity has exact required values."""
        self.assertEqual(VisualIssueSeverity.INFO.value, "info")
        self.assertEqual(VisualIssueSeverity.WARNING.value, "warning")
        self.assertEqual(VisualIssueSeverity.MAJOR.value, "major")


class TestVisualQualityIssue(unittest.TestCase):
    """Tests for VisualQualityIssue dataclass."""

    def test_construction_required_fields(self):
        """VisualQualityIssue construction with required fields."""
        issue = VisualQualityIssue(
            category=VisualIssueCategory.LAYOUT,
            severity=VisualIssueSeverity.WARNING,
            message="Layout issue detected",
        )
        self.assertEqual(issue.category, VisualIssueCategory.LAYOUT)
        self.assertEqual(issue.severity, VisualIssueSeverity.WARNING)
        self.assertEqual(issue.message, "Layout issue detected")
        self.assertIsNone(issue.viewport)
        self.assertIsNone(issue.evidence)

    def test_construction_with_viewport(self):
        """VisualQualityIssue optional viewport."""
        for viewport in ["desktop", "mobile", "cross_viewport", None]:
            issue = VisualQualityIssue(
                category=VisualIssueCategory.RESPONSIVE,
                severity=VisualIssueSeverity.INFO,
                viewport=viewport,
                message="Responsive issue",
            )
            self.assertEqual(issue.viewport, viewport)

    def test_construction_with_evidence(self):
        """VisualQualityIssue optional evidence."""
        evidence = "Primary CTA visually blends into surrounding body content."
        issue = VisualQualityIssue(
            category=VisualIssueCategory.CTA,
            severity=VisualIssueSeverity.MAJOR,
            message="CTA visibility issue",
            evidence=evidence,
        )
        self.assertEqual(issue.evidence, evidence)

    def test_serialization_round_trip(self):
        """VisualQualityIssue serialization round-trip."""
        original = VisualQualityIssue(
            category=VisualIssueCategory.TYPOGRAPHY,
            severity=VisualIssueSeverity.MAJOR,
            viewport="desktop",
            message="Font size too small",
            evidence="Body text renders at 10px on desktop",
        )
        data = original.to_dict()
        restored = VisualQualityIssue.from_dict(data)
        
        self.assertEqual(restored.category, original.category)
        self.assertEqual(restored.severity, original.severity)
        self.assertEqual(restored.viewport, original.viewport)
        self.assertEqual(restored.message, original.message)
        self.assertEqual(restored.evidence, original.evidence)


class TestVisualQualityResult(unittest.TestCase):
    """Tests for VisualQualityResult dataclass."""

    def test_pass_construction(self):
        """VisualQualityResult PASS construction."""
        result = VisualQualityResult(
            action=VisualQualityAction.PASS,
            summary="Visual quality acceptable",
        )
        self.assertEqual(result.action, VisualQualityAction.PASS)
        self.assertEqual(result.summary, "Visual quality acceptable")
        self.assertEqual(result.issues, [])
        self.assertEqual(result.reviewed_viewports, [])
        self.assertIsNone(result.reviewer)

    def test_warn_construction(self):
        """VisualQualityResult WARN construction."""
        issue = VisualQualityIssue(
            category=VisualIssueCategory.SPACING,
            severity=VisualIssueSeverity.WARNING,
            message="Minor spacing inconsistency",
        )
        result = VisualQualityResult(
            action=VisualQualityAction.WARN,
            summary="Minor visual imperfections",
            issues=[issue],
        )
        self.assertEqual(result.action, VisualQualityAction.WARN)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].category, VisualIssueCategory.SPACING)

    def test_human_review_construction(self):
        """VisualQualityResult HUMAN_REVIEW construction."""
        issues = [
            VisualQualityIssue(
                category=VisualIssueCategory.VISUAL_HIERARCHY,
                severity=VisualIssueSeverity.MAJOR,
                viewport="mobile",
                message="Heading hierarchy unclear on mobile",
                evidence="H1 and H2 render at same size",
            ),
            VisualQualityIssue(
                category=VisualIssueCategory.BRAND_CONSISTENCY,
                severity=VisualIssueSeverity.MAJOR,
                viewport="cross_viewport",
                message="Brand colors not applied consistently",
                evidence="Primary button uses non-brand blue",
            ),
        ]
        result = VisualQualityResult(
            action=VisualQualityAction.HUMAN_REVIEW,
            summary="Multiple major visual concerns",
            issues=issues,
            reviewed_viewports=["desktop", "mobile"],
        )
        self.assertEqual(result.action, VisualQualityAction.HUMAN_REVIEW)
        self.assertEqual(len(result.issues), 2)
        self.assertEqual(result.reviewed_viewports, ["desktop", "mobile"])

    def test_result_with_multiple_issues(self):
        """Result with multiple issues."""
        issues = [
            VisualQualityIssue(category=VisualIssueCategory.LAYOUT, severity=VisualIssueSeverity.INFO, message="Issue 1"),
            VisualQualityIssue(category=VisualIssueCategory.IMAGE, severity=VisualIssueSeverity.WARNING, message="Issue 2"),
            VisualQualityIssue(category=VisualIssueCategory.CTA, severity=VisualIssueSeverity.MAJOR, message="Issue 3"),
        ]
        result = VisualQualityResult(
            action=VisualQualityAction.WARN,
            summary="Multiple issues",
            issues=issues,
        )
        self.assertEqual(len(result.issues), 3)
        categories = {i.category for i in result.issues}
        self.assertEqual(len(categories), 3)

    def test_serialization_round_trip(self):
        """VisualQualityResult serialization round-trip."""
        original = VisualQualityResult(
            action=VisualQualityAction.HUMAN_REVIEW,
            summary="Visual review needed",
            issues=[
                VisualQualityIssue(
                    category=VisualIssueCategory.LAYOUT,
                    severity=VisualIssueSeverity.MAJOR,
                    viewport="desktop",
                    message="Layout broken",
                    evidence="Container overflow",
                ),
            ],
            reviewed_viewports=["desktop", "mobile"],
            reviewer="gpt-4-vision",
        )
        data = original.to_dict()
        restored = VisualQualityResult.from_dict(data)
        
        self.assertEqual(restored.action, original.action)
        self.assertEqual(restored.summary, original.summary)
        self.assertEqual(len(restored.issues), len(original.issues))
        self.assertEqual(restored.issues[0].category, original.issues[0].category)
        self.assertEqual(restored.issues[0].severity, original.issues[0].severity)
        self.assertEqual(restored.issues[0].viewport, original.issues[0].viewport)
        self.assertEqual(restored.issues[0].message, original.issues[0].message)
        self.assertEqual(restored.issues[0].evidence, original.issues[0].evidence)
        self.assertEqual(restored.reviewed_viewports, original.reviewed_viewports)
        self.assertEqual(restored.reviewer, original.reviewer)

    def test_reviewed_viewports_preserved(self):
        """reviewed_viewports field preserved."""
        result = VisualQualityResult(
            action=VisualQualityAction.PASS,
            summary="OK",
            reviewed_viewports=["desktop", "mobile", "tablet"],
        )
        data = result.to_dict()
        restored = VisualQualityResult.from_dict(data)
        self.assertEqual(restored.reviewed_viewports, ["desktop", "mobile", "tablet"])

    def test_reviewer_optional(self):
        """reviewer field is optional."""
        result = VisualQualityResult(
            action=VisualQualityAction.PASS,
            summary="OK",
        )
        self.assertIsNone(result.reviewer)
        
        result_with_reviewer = VisualQualityResult(
            action=VisualQualityAction.PASS,
            summary="OK",
            reviewer="gpt-4-vision",
        )
        self.assertEqual(result_with_reviewer.reviewer, "gpt-4-vision")

    def test_severity_does_not_imply_retry_fields(self):
        """Severity does not imply retry fields."""
        issue = VisualQualityIssue(
            category=VisualIssueCategory.LAYOUT,
            severity=VisualIssueSeverity.MAJOR,
            message="Major layout issue",
        )
        result = VisualQualityResult(
            action=VisualQualityAction.HUMAN_REVIEW,
            summary="Major issues",
            issues=[issue],
        )
        # Check that result dict has no retry-related fields
        data = result.to_dict()
        self.assertNotIn("retry_count", data)
        self.assertNotIn("regeneration_required", data)
        self.assertNotIn("frontend_retry", str(data).lower())
        
        # Check issue dict
        issue_data = data["issues"][0]
        self.assertNotIn("retry", issue_data)
        self.assertNotIn("regenerate", issue_data)

    def test_result_contains_no_retry_count(self):
        """Result contains no retry_count field."""
        result = VisualQualityResult(
            action=VisualQualityAction.HUMAN_REVIEW,
            summary="Review needed",
        )
        data = result.to_dict()
        self.assertNotIn("retry_count", data)

    def test_result_contains_no_regeneration_required(self):
        """Result contains no regeneration_required field."""
        result = VisualQualityResult(
            action=VisualQualityAction.WARN,
            summary="Warn",
        )
        data = result.to_dict()
        self.assertNotIn("regeneration_required", data)


if __name__ == "__main__":
    unittest.main()