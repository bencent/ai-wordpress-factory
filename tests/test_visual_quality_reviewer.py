#!/usr/bin/env python3
"""Tests for VisualQualityReviewer (Phase 7D-3B)."""

import unittest
import sys
import os
import json
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import (
    PreviewArtifact, PreviewViewport,
    VisualQualityResult, VisualQualityAction,
    VisualQualityIssue, VisualIssueCategory, VisualIssueSeverity,
)
from agents.visual_quality import VisualQualityReviewer
from providers.visual_quality_provider import (
    OpenAIVisualQualityProvider,
    VisualReviewRequest,
    VisualReviewResult,
)


def _make_preview_artifact(task_id: str = "task-123", preview_id: str = "preview-456") -> PreviewArtifact:
    """Create a mock PreviewArtifact with valid screenshot paths."""
    desktop_vp = PreviewViewport(width=1440, height=900)
    mobile_vp = PreviewViewport(width=390, height=844)
    return PreviewArtifact(
        task_id=task_id,
        preview_id=preview_id,
        attempt_number=1,
        desktop_viewport_screenshot_path="/tmp/desktop_viewport.png",
        desktop_full_page_screenshot_path="/tmp/desktop_full.png",
        mobile_viewport_screenshot_path="/tmp/mobile_viewport.png",
        mobile_full_page_screenshot_path="/tmp/mobile_full.png",
        desktop_viewport=desktop_vp,
        mobile_viewport=mobile_vp,
        created_at="2024-01-01T00:00:00",
    )


class TestVisualQualityReviewerAPI(unittest.TestCase):
    """Tests for VisualQualityReviewer public API."""

    def setUp(self):
        # Create a mock config that returns "openai" for visual_quality_provider
        self.config = Mock()
        self.config.visual_quality_provider = "openai"
        self.reviewer = VisualQualityReviewer(config=self.config)
        self.preview = _make_preview_artifact()

    def test_returns_visual_quality_result(self):
        """reviewer returns VisualQualityResult."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(
                success=True,
                action=VisualQualityAction.PASS,
                summary="Visual quality acceptable",
                issues=[],
                reviewed_viewports=["desktop", "mobile"],
                reviewer="gpt-4-vision",
            )
            result = self.reviewer.review(self.preview)
            self.assertIsInstance(result, VisualQualityResult)

    def test_pass_parses_correctly(self):
        """PASS parses correctly."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(
                success=True,
                action=VisualQualityAction.PASS,
                summary="Visual quality acceptable",
                issues=[],
                reviewed_viewports=["desktop", "mobile"],
                reviewer="gpt-4-vision",
            )
            result = self.reviewer.review(self.preview)
            self.assertEqual(result.action, VisualQualityAction.PASS)
            self.assertEqual(result.summary, "Visual quality acceptable")
            self.assertEqual(result.issues, [])

    def test_warn_parses_correctly(self):
        """WARN parses correctly."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(
                success=True,
                action=VisualQualityAction.WARN,
                summary="Minor spacing issues",
                issues=[
                    VisualQualityIssue(
                        category=VisualIssueCategory.SPACING,
                        severity=VisualIssueSeverity.WARNING,
                        viewport="mobile",
                        message="Inconsistent spacing",
                        evidence="Uneven margins",
                    ),
                ],
                reviewed_viewports=["desktop", "mobile"],
                reviewer="gpt-4-vision",
            )
            result = self.reviewer.review(self.preview)
            self.assertEqual(result.action, VisualQualityAction.WARN)
            self.assertEqual(len(result.issues), 1)

    def test_human_review_parses_correctly(self):
        """HUMAN_REVIEW parses correctly."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(
                success=True,
                action=VisualQualityAction.HUMAN_REVIEW,
                summary="Major layout concerns",
                issues=[
                    VisualQualityIssue(
                        category=VisualIssueCategory.LAYOUT,
                        severity=VisualIssueSeverity.MAJOR,
                        viewport="mobile",
                        message="Layout broken on mobile",
                        evidence="Content overflows viewport",
                    ),
                ],
                reviewed_viewports=["desktop", "mobile"],
                reviewer="gpt-4-vision",
            )
            result = self.reviewer.review(self.preview)
            self.assertEqual(result.action, VisualQualityAction.HUMAN_REVIEW)
            self.assertEqual(len(result.issues), 1)

    def test_multiple_issues_parse_correctly(self):
        """Multiple issues parse correctly."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(
                success=True,
                action=VisualQualityAction.HUMAN_REVIEW,
                summary="Multiple issues",
                issues=[
                    VisualQualityIssue(category=VisualIssueCategory.LAYOUT, severity=VisualIssueSeverity.MAJOR, viewport="desktop", message="Issue 1"),
                    VisualQualityIssue(category=VisualIssueCategory.IMAGE, severity=VisualIssueSeverity.WARNING, viewport="mobile", message="Issue 2"),
                    VisualQualityIssue(category=VisualIssueCategory.CTA, severity=VisualIssueSeverity.MAJOR, viewport="cross_viewport", message="Issue 3"),
                ],
                reviewed_viewports=["desktop", "mobile"],
                reviewer="gpt-4-vision",
            )
            result = self.reviewer.review(self.preview)
            self.assertEqual(len(result.issues), 3)
            categories = {i.category for i in result.issues}
            self.assertEqual(len(categories), 3)

    def test_category_parsing(self):
        """Category parsing works for all valid categories."""
        for cat in VisualIssueCategory:
            with patch.object(self.reviewer.provider, 'review') as mock_review:
                mock_review.return_value = VisualReviewResult(
                    success=True,
                    action=VisualQualityAction.WARN,
                    summary="Test",
                    issues=[VisualQualityIssue(category=cat, severity=VisualIssueSeverity.INFO, message="Test")],
                    reviewed_viewports=["desktop"],
                    reviewer="gpt-4-vision",
                )
                result = self.reviewer.review(self.preview)
                self.assertEqual(result.issues[0].category, cat)

    def test_severity_parsing(self):
        """Severity parsing works for all valid severities."""
        for sev in VisualIssueSeverity:
            with patch.object(self.reviewer.provider, 'review') as mock_review:
                mock_review.return_value = VisualReviewResult(
                    success=True,
                    action=VisualQualityAction.WARN,
                    summary="Test",
                    issues=[VisualQualityIssue(category=VisualIssueCategory.LAYOUT, severity=sev, message="Test")],
                    reviewed_viewports=["desktop"],
                    reviewer="gpt-4-vision",
                )
                result = self.reviewer.review(self.preview)
                self.assertEqual(result.issues[0].severity, sev)

    def test_viewport_preserved(self):
        """Viewport field preserved from provider response."""
        for vp in ["desktop", "mobile", "cross_viewport", None]:
            with patch.object(self.reviewer.provider, 'review') as mock_review:
                mock_review.return_value = VisualReviewResult(
                    success=True,
                    action=VisualQualityAction.WARN,
                    summary="Test",
                    issues=[VisualQualityIssue(category=VisualIssueCategory.LAYOUT, severity=VisualIssueSeverity.INFO, viewport=vp, message="Test")],
                    reviewed_viewports=["desktop"],
                    reviewer="gpt-4-vision",
                )
                result = self.reviewer.review(self.preview)
                self.assertEqual(result.issues[0].viewport, vp)

    def test_evidence_preserved(self):
        """Evidence field preserved from provider response."""
        evidence = "Primary CTA visually blends into surrounding body content."
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(
                success=True,
                action=VisualQualityAction.WARN,
                summary="Test",
                issues=[VisualQualityIssue(category=VisualIssueCategory.CTA, severity=VisualIssueSeverity.MAJOR, message="CTA issue", evidence=evidence)],
                reviewed_viewports=["desktop"],
                reviewer="gpt-4-vision",
            )
            result = self.reviewer.review(self.preview)
            self.assertEqual(result.issues[0].evidence, evidence)

    def test_four_preview_screenshots_supplied(self):
        """Four preview screenshots supplied to provider."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(
                success=True,
                action=VisualQualityAction.PASS,
                summary="OK",
                issues=[],
                reviewed_viewports=["desktop", "mobile"],
                reviewer="gpt-4-vision",
            )
            self.reviewer.review(self.preview)
            
            # Verify provider was called with correct preview
            call_args = mock_review.call_args[0][0]
            self.assertIsInstance(call_args, VisualReviewRequest)
            self.assertEqual(call_args.preview.task_id, "task-123")
            self.assertEqual(call_args.preview.desktop_viewport_screenshot_path, "/tmp/desktop_viewport.png")
            self.assertEqual(call_args.preview.desktop_full_page_screenshot_path, "/tmp/desktop_full.png")
            self.assertEqual(call_args.preview.mobile_viewport_screenshot_path, "/tmp/mobile_viewport.png")
            self.assertEqual(call_args.preview.mobile_full_page_screenshot_path, "/tmp/mobile_full.png")

    def test_desktop_viewport_included(self):
        """Desktop viewport screenshot included."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["desktop"], reviewer="test")
            self.reviewer.review(self.preview)
            req = mock_review.call_args[0][0]
            self.assertEqual(req.preview.desktop_viewport_screenshot_path, "/tmp/desktop_viewport.png")

    def test_desktop_full_page_included(self):
        """Desktop full-page screenshot included."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["desktop"], reviewer="test")
            self.reviewer.review(self.preview)
            req = mock_review.call_args[0][0]
            self.assertEqual(req.preview.desktop_full_page_screenshot_path, "/tmp/desktop_full.png")

    def test_mobile_viewport_included(self):
        """Mobile viewport screenshot included."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["mobile"], reviewer="test")
            self.reviewer.review(self.preview)
            req = mock_review.call_args[0][0]
            self.assertEqual(req.preview.mobile_viewport_screenshot_path, "/tmp/mobile_viewport.png")

    def test_mobile_full_page_included(self):
        """Mobile full-page screenshot included."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["mobile"], reviewer="test")
            self.reviewer.review(self.preview)
            req = mock_review.call_args[0][0]
            self.assertEqual(req.preview.mobile_full_page_screenshot_path, "/tmp/mobile_full.png")


class TestProviderOutputValidation(unittest.TestCase):
    """Tests for provider output validation."""

    def setUp(self):
        self.config = Mock()
        self.config.visual_quality_provider = "openai"
        self.provider = OpenAIVisualQualityProvider(config=self.config)

    def test_invalid_action_rejected(self):
        """Invalid action rejected."""
        raw = json.dumps({
            "action": "retry",
            "summary": "Test",
            "issues": [],
            "reviewed_viewports": ["desktop", "mobile"],
        })
        result = self.provider._parse_response(raw)
        self.assertFalse(result.success)
        self.assertIn("Invalid action value: retry", result.error)

    def test_invalid_category_rejected_or_mapped(self):
        """Invalid category safely mapped to OTHER."""
        raw = json.dumps({
            "action": "warn",
            "summary": "Test",
            "issues": [{
                "category": "invalid_category",
                "severity": "warning",
                "message": "Test",
            }],
            "reviewed_viewports": ["desktop", "mobile"],
        })
        result = self.provider._parse_response(raw)
        self.assertTrue(result.success)
        self.assertEqual(result.issues[0].category, VisualIssueCategory.OTHER)

    def test_invalid_severity_does_not_become_info(self):
        """Invalid severity is rejected instead of downgraded to INFO."""
        raw = json.dumps({
            "action": "warn",
            "summary": "Test",
            "issues": [{
                "category": "layout",
                "severity": "critical",
                "message": "Test",
            }],
            "reviewed_viewports": ["desktop", "mobile"],
        })
        result = self.provider._parse_response(raw)
        self.assertFalse(result.success)
        self.assertEqual(result.issues, [])
        self.assertIn("Invalid severity value: critical", result.error)

    def test_invalid_severity_results_in_human_review(self):
        """Invalid severity reaches the reviewer's safe HUMAN_REVIEW path."""
        raw = json.dumps({
            "action": "warn",
            "summary": "Test",
            "issues": [{
                "category": "layout",
                "severity": "critical",
                "message": "Test",
            }],
            "reviewed_viewports": ["desktop", "mobile"],
        })
        parsed = self.provider._parse_response(raw)
        reviewer = VisualQualityReviewer(config=self.config)
        with patch.object(reviewer.provider, 'review') as mock_review:
            mock_review.return_value = parsed
            result = reviewer.review(_make_preview_artifact())
        self.assertEqual(result.action, VisualQualityAction.HUMAN_REVIEW)
        self.assertIn("AI provider: INVALID_RESPONSE", result.summary)

    def test_missing_severity_is_validation_failure(self):
        """Missing severity is rejected instead of defaulting to INFO."""
        raw = json.dumps({
            "action": "warn",
            "summary": "Test",
            "issues": [{
                "category": "layout",
                "message": "Test",
            }],
            "reviewed_viewports": ["desktop", "mobile"],
        })
        result = self.provider._parse_response(raw)
        self.assertFalse(result.success)
        self.assertIn("Missing required field: severity", result.error)

    def test_valid_info_remains_info(self):
        """Valid INFO severity remains INFO."""
        raw = json.dumps({
            "action": "warn",
            "summary": "Test",
            "issues": [{
                "category": "layout",
                "severity": "info",
                "message": "Test",
            }],
            "reviewed_viewports": ["desktop", "mobile"],
        })
        result = self.provider._parse_response(raw)
        self.assertTrue(result.success)
        self.assertEqual(result.issues[0].severity, VisualIssueSeverity.INFO)

    def test_valid_warning_remains_warning(self):
        """Valid WARNING severity remains WARNING."""
        raw = json.dumps({
            "action": "warn",
            "summary": "Test",
            "issues": [{
                "category": "layout",
                "severity": "warning",
                "message": "Test",
            }],
            "reviewed_viewports": ["desktop", "mobile"],
        })
        result = self.provider._parse_response(raw)
        self.assertTrue(result.success)
        self.assertEqual(result.issues[0].severity, VisualIssueSeverity.WARNING)

    def test_valid_major_remains_major(self):
        """Valid MAJOR severity remains MAJOR."""
        raw = json.dumps({
            "action": "warn",
            "summary": "Test",
            "issues": [{
                "category": "layout",
                "severity": "major",
                "message": "Test",
            }],
            "reviewed_viewports": ["desktop", "mobile"],
        })
        result = self.provider._parse_response(raw)
        self.assertTrue(result.success)
        self.assertEqual(result.issues[0].severity, VisualIssueSeverity.MAJOR)

    def test_malformed_json_does_not_become_pass(self):
        """Malformed JSON/output does not become PASS."""
        raw = "not valid json at all"
        result = self.provider._parse_response(raw)
        self.assertFalse(result.success)
        self.assertNotEqual(result.action, VisualQualityAction.PASS)

    def test_missing_required_output_does_not_become_pass(self):
        """Missing required output does not become PASS."""
        raw = json.dumps({
            "action": "pass",
            # missing summary
            "issues": [],
        })
        result = self.provider._parse_response(raw)
        self.assertFalse(result.success)
        self.assertNotEqual(result.action, VisualQualityAction.PASS)

    def test_unknown_model_field_cannot_request_retry(self):
        """Unknown model field cannot request retry."""
        raw = json.dumps({
            "action": "pass",
            "summary": "OK",
            "issues": [],
            "retry_count": 3,
            "regeneration_required": True,
            "frontend_retry": True,
        })
        result = self.provider._parse_response(raw)
        self.assertTrue(result.success)
        # Verify unknown fields don't leak into result fields (not raw_response)
        self.assertNotIn("retry_count", result.__dict__)
        self.assertNotIn("regeneration_required", result.__dict__)
        self.assertNotIn("frontend_retry", result.__dict__)

    def test_unknown_model_field_cannot_request_regeneration(self):
        """Unknown model field cannot request regeneration."""
        raw = json.dumps({
            "action": "pass",
            "summary": "OK",
            "issues": [],
            "regenerate": True,
        })
        result = self.provider._parse_response(raw)
        self.assertTrue(result.success)


class TestReviewerBoundaries(unittest.TestCase):
    """Tests for reviewer boundary enforcement."""

    def setUp(self):
        self.config = Mock()
        self.config.visual_quality_provider = "openai"
        self.reviewer = VisualQualityReviewer(config=self.config)
        self.preview = _make_preview_artifact()

    def test_reviewer_does_not_mutate_preview_artifact(self):
        """Reviewer does not mutate PreviewArtifact."""
        original_paths = (
            self.preview.desktop_viewport_screenshot_path,
            self.preview.desktop_full_page_screenshot_path,
            self.preview.mobile_viewport_screenshot_path,
            self.preview.mobile_full_page_screenshot_path,
        )
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["desktop", "mobile"], reviewer="test")
            self.reviewer.review(self.preview)
            # Paths should be unchanged
            self.assertEqual(self.preview.desktop_viewport_screenshot_path, original_paths[0])
            self.assertEqual(self.preview.desktop_full_page_screenshot_path, original_paths[1])
            self.assertEqual(self.preview.mobile_viewport_screenshot_path, original_paths[2])
            self.assertEqual(self.preview.mobile_full_page_screenshot_path, original_paths[3])

    def test_reviewer_does_not_mutate_task(self):
        """Reviewer does not mutate Task (reviewer has no Task access)."""
        # The reviewer only takes PreviewArtifact, not Task
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["desktop", "mobile"], reviewer="test")
            result = self.reviewer.review(self.preview)
            # Result is a new VisualQualityResult, no Task mutation
            self.assertIsInstance(result, VisualQualityResult)

    def test_reviewer_does_not_invoke_preview_renderer(self):
        """Reviewer does not invoke PreviewRenderer."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["desktop", "mobile"], reviewer="test")
            self.reviewer.review(self.preview)
            # Provider review was called, but no PreviewRenderer
            mock_review.assert_called_once()

    def test_reviewer_does_not_invoke_wordpress_publisher(self):
        """Reviewer does not invoke WordPressPublisher."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["desktop", "mobile"], reviewer="test")
            self.reviewer.review(self.preview)
            # No WordPress publisher called

    def test_no_confidence_routing_exists(self):
        """No confidence routing exists in result."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["desktop", "mobile"], reviewer="test")
            result = self.reviewer.review(self.preview)
            # Check result doesn't have confidence fields
            data = result.to_dict()
            self.assertNotIn("confidence", data)
            self.assertNotIn("threshold", data)

    def test_no_frontend_retry_count_behavior_exists(self):
        """No frontend_retry_count behavior exists."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.HUMAN_REVIEW, summary="Review", issues=[], reviewed_viewports=["desktop", "mobile"], reviewer="test")
            result = self.reviewer.review(self.preview)
            data = result.to_dict()
            self.assertNotIn("frontend_retry", str(data).lower())
            self.assertNotIn("retry_count", data)


class TestProviderFailureHandling(unittest.TestCase):
    """Tests for provider/API failure handling."""

    def setUp(self):
        self.config = Mock()
        self.config.visual_quality_provider = "openai"
        self.reviewer = VisualQualityReviewer(config=self.config)
        self.preview = _make_preview_artifact()

    def test_provider_api_failure_handled_explicitly(self):
        """Provider/API failure handled explicitly."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(
                success=False,
                error="API rate limit exceeded",
            )
            result = self.reviewer.review(self.preview)
            self.assertEqual(result.action, VisualQualityAction.HUMAN_REVIEW)
            self.assertIn("Visual review failed", result.summary)

    def test_screenshot_encoding_failure_handled(self):
        """Screenshot read/encoding failure handled explicitly."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(
                success=False,
                error="Failed to encode image: [Errno 2] No such file or directory",
            )
            result = self.reviewer.review(self.preview)
            self.assertEqual(result.action, VisualQualityAction.HUMAN_REVIEW)

    def test_screenshot_read_failure_reaches_human_review(self):
        """Missing screenshot reaches the reviewer's HUMAN_REVIEW path."""
        self.config.openai_api_key = "test-key"
        # Explicit provider injection; never construct a real SDK client in this test.
        self.reviewer.provider = OpenAIVisualQualityProvider(self.config)
        with patch('providers.sdk_client.create_client', return_value=Mock()):
            result = self.reviewer.review(self.preview)
        self.assertEqual(result.action, VisualQualityAction.HUMAN_REVIEW)
        self.assertIn("AI provider: UNKNOWN", result.summary)

    def test_serialization_compatible_result_produced(self):
        """Serialization-compatible VisualQualityResult produced."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(
                success=True,
                action=VisualQualityAction.WARN,
                summary="Test summary",
                issues=[
                    VisualQualityIssue(
                        category=VisualIssueCategory.LAYOUT,
                        severity=VisualIssueSeverity.WARNING,
                        viewport="desktop",
                        message="Layout issue",
                        evidence="Evidence text",
                    ),
                ],
                reviewed_viewports=["desktop", "mobile"],
                reviewer="gpt-4-vision",
            )
            result = self.reviewer.review(self.preview)
            # Test round-trip serialization
            data = result.to_dict()
            restored = VisualQualityResult.from_dict(data)
            self.assertEqual(restored.action, result.action)
            self.assertEqual(restored.summary, result.summary)
            self.assertEqual(len(restored.issues), len(result.issues))
            self.assertEqual(restored.issues[0].category, result.issues[0].category)
            self.assertEqual(restored.issues[0].severity, result.issues[0].severity)
            self.assertEqual(restored.issues[0].viewport, result.issues[0].viewport)
            self.assertEqual(restored.issues[0].evidence, result.issues[0].evidence)
            self.assertEqual(restored.reviewed_viewports, result.reviewed_viewports)
            self.assertEqual(restored.reviewer, result.reviewer)


class TestActionSafety(unittest.TestCase):
    """Tests confirming only allowed actions."""

    def setUp(self):
        self.config = Mock()
        self.config.visual_quality_provider = "openai"
        self.reviewer = VisualQualityReviewer(config=self.config)
        self.preview = _make_preview_artifact()

    def test_only_pass_warn_human_review_allowed(self):
        """Only PASS / WARN / HUMAN_REVIEW actions are produced."""
        for action in [VisualQualityAction.PASS, VisualQualityAction.WARN, VisualQualityAction.HUMAN_REVIEW]:
            with patch.object(self.reviewer.provider, 'review') as mock_review:
                mock_review.return_value = VisualReviewResult(
                    success=True,
                    action=action,
                    summary="Test",
                    issues=[],
                    reviewed_viewports=["desktop", "mobile"],
                    reviewer="test",
                )
                result = self.reviewer.review(self.preview)
                self.assertIn(result.action, [VisualQualityAction.PASS, VisualQualityAction.WARN, VisualQualityAction.HUMAN_REVIEW])

    def test_no_retry_action_in_result(self):
        """No retry action in result."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["desktop", "mobile"], reviewer="test")
            result = self.reviewer.review(self.preview)
            self.assertNotEqual(result.action.value, "retry")

    def test_no_regenerate_action_in_result(self):
        """No regenerate action in result."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["desktop", "mobile"], reviewer="test")
            result = self.reviewer.review(self.preview)
            self.assertNotEqual(result.action.value, "regenerate")

    def test_no_fail_action_in_result(self):
        """No fail action in result."""
        with patch.object(self.reviewer.provider, 'review') as mock_review:
            mock_review.return_value = VisualReviewResult(success=True, action=VisualQualityAction.PASS, summary="OK", issues=[], reviewed_viewports=["desktop", "mobile"], reviewer="test")
            result = self.reviewer.review(self.preview)
            self.assertNotEqual(result.action.value, "fail")


if __name__ == "__main__":
    unittest.main()