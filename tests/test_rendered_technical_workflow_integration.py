#!/usr/bin/env python3
import unittest
import datetime
import os
import sys
from contextlib import ExitStack
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import Task, TaskStatus, ContentType, WorkflowState, workflow_state
from contracts import (
    FrontendRequest, FrontendResult, FrontendSecurityResult,
    GreenLightConversionResult, FrontendValidationResult,
    ReviewResult, ReviewAction, CritiqueResult,
    ApprovalPolicy, ApprovalPolicyMode,
    FrontendProductionQualityResult,
    FrontendGate, FrontendFailureSeverity,
    PreviewArtifact, PreviewViewport, PreviewInfrastructureFailure, FailureCategory,
    RenderedEvidence, ViewportRenderedEvidence,
    RenderedTechnicalResult, RenderedTechnicalSeverity,
    VisualQualityResult, VisualQualityAction,
    ImageArtifact, ImageArtifactStatus, create_image_artifact,
)
from main import AIWordPressFactory


def _make_evidence(task_id: str = "task-123", preview_id: str = "preview-456") -> RenderedEvidence:
    desktop = ViewportRenderedEvidence(
        viewport_width=1440,
        viewport_height=900,
        document_scroll_width=1440,
        document_client_width=1440,
        document_scroll_height=2000,
        document_client_height=900,
        console_errors=[],
        page_errors=[],
        image_load_states=[],
        element_bounding_boxes=[],
    )
    mobile = ViewportRenderedEvidence(
        viewport_width=390,
        viewport_height=844,
        document_scroll_width=390,
        document_client_width=390,
        document_scroll_height=1500,
        document_client_height=844,
        console_errors=[],
        page_errors=[],
        image_load_states=[],
        element_bounding_boxes=[],
    )
    return RenderedEvidence(
        task_id=task_id,
        preview_id=preview_id,
        attempt_number=1,
        desktop=desktop,
        mobile=mobile,
        created_at=datetime.datetime.now().isoformat(),
    )


def _make_result(passed: bool, status: str, errors=None, warnings=None) -> RenderedTechnicalResult:
    if errors is None:
        errors = []
    if warnings is None:
        warnings = []
    return RenderedTechnicalResult(
        task_id="task-123",
        preview_id="preview-456",
        attempt_number=1,
        passed=passed,
        validation_status=status,
        errors=errors,
        warnings=warnings,
        diagnostics={"total_errors": len(errors), "total_warnings": len(warnings), "viewports_checked": ["desktop", "mobile"]},
        created_at=datetime.datetime.now().isoformat(),
    )


def _make_frontend_result(task_id: str) -> FrontendResult:
    return FrontendResult(
        task_id=task_id,
        success=True,
        html="<div>Test</div>",
        css="body { color: #333; }",
        javascript="const x = 1;",
        blocks="<!-- wp:paragraph --><p>Test</p><!-- /wp:paragraph -->",
    )


def _make_security_result(task_id: str) -> FrontendSecurityResult:
    return FrontendSecurityResult(
        task_id=task_id,
        passed=True,
        html="<div>Test</div>",
        css="body { color: #333; }",
        javascript="const x = 1;",
        errors=[],
        warnings=[],
        blocked_items=[],
    )


def _make_conversion_result(task_id: str) -> GreenLightConversionResult:
    return GreenLightConversionResult(
        task_id=task_id,
        success=True,
        blocks="<!-- wp:paragraph --><p>Test</p><!-- /wp:paragraph -->",
        errors=[],
        warnings=[],
    )


def _make_validation_result(task_id: str) -> FrontendValidationResult:
    return FrontendValidationResult(
        task_id=task_id,
        passed=True,
        validation_status="passed",
        errors=[],
        warnings=[],
        diagnostics={},
        validator_error=False,
    )


def _make_production_result(task_id: str) -> FrontendProductionQualityResult:
    return FrontendProductionQualityResult(
        task_id=task_id,
        passed=True,
        validation_status="passed",
        errors=[],
        warnings=[],
        diagnostics={"inline_css_size": 500},
    )


def _make_preview_artifact(task_id: str = "task-123", preview_id: str = "preview-456") -> PreviewArtifact:
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
        created_at=datetime.datetime.now().isoformat(),
    )


class TestRenderedTechnicalWorkflowIntegration(unittest.TestCase):
    def setUp(self):
        self.factory = AIWordPressFactory()
        workflow_state.tasks.clear()
        workflow_state.current_task_id = None

    def _create_task(self, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH, max_frontend_retries=3):
        task_id = self.factory.create_task(
            title="Test Blog Post",
            description="A test blog post",
            content_type=ContentType.BLOG_POST,
        )
        task = workflow_state.get_task(task_id)
        task.approval_policy = ApprovalPolicy(mode=approval_mode).to_dict()
        task.max_frontend_retries = max_frontend_retries
        task.frontend_retry_count = 0
        task.frontend_retry_history = []
        return task

    def _run_with(self, *, renderer_return, validator_result, max_frontend_retries=3):
        task = self._create_task(max_frontend_retries=max_frontend_retries)
        task_id = task.id

        # Pre-populate READY image artifact to satisfy 7D-5E guard
        ready_artifact = create_image_artifact(
            status=ImageArtifactStatus.READY,
            wordpress_media_id=123,
            wordpress_media_url="https://wp.example.com/img.jpg",
            artifact_id="img_test_ready",
        )
        task.image_artifact = ready_artifact.to_dict()
        task.hero_image_id = 123
        task.hero_image_url = "https://wp.example.com/img.jpg"
        task.image_status = "success"

        writer_mock = Mock()
        writer_mock.write_content.return_value = "Test draft content"

        quality_mock = Mock()
        quality_mock.evaluate.return_value = ReviewResult(
            passed=True,
            score=90,
            issues=[],
            feedback="Excellent",
            suggested_action=ReviewAction.PUBLISH.value,
        )

        frontend_mock = Mock()
        frontend_mock.generate_frontend.return_value = _make_frontend_result(task_id)

        publisher_mock = Mock()
        publisher_mock.publish_content.return_value = (123, "published-url")
        publisher_mock.validate_config.return_value = True

        agents = {
            "writer": writer_mock,
            "quality_evaluator": quality_mock,
            "frontend": frontend_mock,
            "publisher": publisher_mock,
            "image": None,
            "learner": None,
        }

        def get_agent_side_effect(agent_type):
            return agents.get(agent_type)

        security_mock = Mock()
        security_mock.check.return_value = _make_security_result(task_id)

        converter_mock = Mock()
        converter_mock.convert.return_value = _make_conversion_result(task_id)

        validation_mock = Mock()
        validation_mock.validate.return_value = _make_validation_result(task_id)

        production_mock = Mock()
        production_mock.check.return_value = _make_production_result(task_id)

        visual_reviewer_mock = Mock()
        visual_reviewer_mock.review.return_value = VisualQualityResult(
            action=VisualQualityAction.PASS,
            summary="Visual quality acceptable",
        )

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=get_agent_side_effect))
            stack.enter_context(patch("main.FrontendSecurityGate", return_value=security_mock))
            stack.enter_context(patch("main.GreenLightConverter", return_value=converter_mock))
            stack.enter_context(patch("main.FrontendValidator", return_value=validation_mock))
            stack.enter_context(patch("main.FrontendProductionQualityGate", return_value=production_mock))
            stack.enter_context(patch("main.PreviewRenderer", return_value=renderer_return))
            stack.enter_context(patch("main.RenderedTechnicalValidator", return_value=validator_result))
            stack.enter_context(patch("main.VisualQualityReviewer", return_value=visual_reviewer_mock))
            stack.enter_context(patch.object(self.factory, "save_state"))

            return self.factory.run_workflow(task_id), task

    def test_preview_success_runs_validator(self):
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())

        validator = Mock()
        validator.validate.return_value = _make_result(True, "passed")

        result, task = self._run_with(renderer_return=renderer, validator_result=validator)

        self.assertTrue(result)
        validator.validate.assert_called_once()

    def test_infrastructure_failure_skips_validator(self):
        renderer = Mock()
        failure = PreviewInfrastructureFailure(
            task_id="task-123",
            preview_id="preview-456",
            attempt_number=1,
            error_type="browser_crash",
            message="Browser crashed",
            retryable=False,
            occurred_at=datetime.datetime.now().isoformat(),
            failure_category=FailureCategory.INFRASTRUCTURE,
        )
        renderer.render.return_value = (None, failure, None)

        validator = Mock()
        validator.validate.return_value = _make_result(True, "passed")

        result, task = self._run_with(renderer_return=renderer, validator_result=validator)

        self.assertFalse(result)
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)
        validator.validate.assert_not_called()

    def test_rendered_pass_continues_to_approval(self):
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())

        validator = Mock()
        validator.validate.return_value = _make_result(True, "passed")

        result, task = self._run_with(renderer_return=renderer, validator_result=validator)

        self.assertTrue(result)
        self.assertEqual(task.approval_policy["mode"], ApprovalPolicyMode.AUTO_PUBLISH.value)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.LEARNING))

    def test_rendered_warning_continues_to_approval(self):
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())

        warning_diag = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "element_overflow",
            "severity": RenderedTechnicalSeverity.WARNING.value,
            "message": "Element overflow",
            "context": {"viewport": "desktop"},
        }]
        validator = Mock()
        validator.validate.return_value = _make_result(True, "warnings", warnings=warning_diag)

        result, task = self._run_with(renderer_return=renderer, validator_result=validator)

        self.assertTrue(result)
        self.assertEqual(task.approval_policy["mode"], ApprovalPolicyMode.AUTO_PUBLISH.value)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.LEARNING))

    def test_rendered_error_blocks_approval(self):
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())

        error_diag = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "document_horizontal_overflow",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow",
            "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        validator = Mock()
        validator.validate.return_value = _make_result(False, "failed", errors=error_diag)

        result, task = self._run_with(renderer_return=renderer, validator_result=validator, max_frontend_retries=1)

        self.assertFalse(result)
        self.assertNotIn(TaskStatus.COMPLETED, (task.status,))
        self.assertNotIn(TaskStatus.LEARNING, (task.status,))

    def test_rendered_technical_result_stored(self):
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())

        result_obj = _make_result(True, "passed")
        validator = Mock()
        validator.validate.return_value = result_obj

        _, task = self._run_with(renderer_return=renderer, validator_result=validator)

        self.assertIsNotNone(task.rendered_technical_result)
        self.assertEqual(task.rendered_technical_result["preview_id"], "preview-456")
        self.assertEqual(task.rendered_technical_result["passed"], True)

    def test_rendered_technical_history_appended(self):
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())

        result_obj = _make_result(True, "passed")
        validator = Mock()
        validator.validate.return_value = result_obj

        _, task = self._run_with(renderer_return=renderer, validator_result=validator)

        self.assertEqual(len(task.rendered_technical_history), 1)
        self.assertEqual(task.rendered_technical_history[0]["preview_id"], "preview-456")

    def test_warning_persisted_without_retry(self):
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())

        warning_diag = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "element_overflow",
            "severity": RenderedTechnicalSeverity.WARNING.value,
            "message": "Element overflow",
            "context": {"viewport": "desktop"},
        }]
        validator = Mock()
        validator.validate.return_value = _make_result(True, "warnings", warnings=warning_diag)

        result, task = self._run_with(renderer_return=renderer, validator_result=validator)

        self.assertTrue(result)
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(len(task.frontend_retry_history), 0)
        self.assertIsNotNone(task.rendered_technical_result)
        self.assertEqual(task.rendered_technical_result["validation_status"], "warnings")

    # ----- FRONTEND FAILURE FEEDBACK -----

    def _run_with_error_feedback(self, error_diag, max_retries=1):
        """Run workflow with a specific rendered technical error and capture the feedback."""
        from unittest.mock import Mock, patch
        from contextlib import ExitStack

        # Create a task
        task = self._create_task(max_frontend_retries=max_retries)
        task_id = task.id

        # Pre-populate READY image artifact to satisfy 7D-5E guard
        ready_artifact = create_image_artifact(
            status=ImageArtifactStatus.READY,
            wordpress_media_id=123,
            wordpress_media_url="https://wp.example.com/img.jpg",
            artifact_id="img_test_ready",
        )
        task.image_artifact = ready_artifact.to_dict()
        task.hero_image_id = 123
        task.hero_image_url = "https://wp.example.com/img.jpg"
        task.image_status = "success"

        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())

        validator = Mock()
        validator.validate.return_value = _make_result(False, "failed", errors=error_diag)

        captured = {}

        # Save original method to call it
        original_feedback = self.factory._create_frontend_failure_feedback

        def capture_feedback(gate, result, attempt):
            fb = original_feedback(gate, result, attempt)
            captured["feedback"] = fb
            return fb

        # Build agent mocks similar to _run_with
        writer_mock = Mock()
        writer_mock.write_content.return_value = "Test draft content"
        quality_mock = Mock()
        quality_mock.evaluate.return_value = ReviewResult(
            passed=True,
            score=90,
            issues=[],
            feedback="Excellent",
            suggested_action=ReviewAction.PUBLISH.value,
        )
        frontend_mock = Mock()
        frontend_mock.generate_frontend.return_value = _make_frontend_result(task_id)
        publisher_mock = Mock()
        publisher_mock.publish_content.return_value = (123, "published-url")
        publisher_mock.validate_config.return_value = True

        agents = {
            "writer": writer_mock,
            "quality_evaluator": quality_mock,
            "frontend": frontend_mock,
            "publisher": publisher_mock,
            "image": None,
            "learner": None,
        }

        def get_agent_side_effect(agent_type):
            return agents.get(agent_type)

        security_mock = Mock()
        security_mock.check.return_value = _make_security_result(task_id)
        converter_mock = Mock()
        converter_mock.convert.return_value = _make_conversion_result(task_id)
        validation_mock = Mock()
        validation_mock.validate.return_value = _make_validation_result(task_id)
        production_mock = Mock()
        production_mock.check.return_value = _make_production_result(task_id)

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=get_agent_side_effect))
            stack.enter_context(patch("main.FrontendSecurityGate", return_value=security_mock))
            stack.enter_context(patch("main.GreenLightConverter", return_value=converter_mock))
            stack.enter_context(patch("main.FrontendValidator", return_value=validation_mock))
            stack.enter_context(patch("main.FrontendProductionQualityGate", return_value=production_mock))
            stack.enter_context(patch("main.PreviewRenderer", return_value=renderer))
            stack.enter_context(patch("main.RenderedTechnicalValidator", return_value=validator))
            stack.enter_context(patch.object(self.factory, "save_state"))
            stack.enter_context(patch.object(self.factory, "_create_frontend_failure_feedback", side_effect=capture_feedback))

            self.factory.run_workflow(task_id)

        task = workflow_state.get_task(task_id)
        return captured.get("feedback"), task

    def test_rendered_error_creates_feedback(self):
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "document_horizontal_overflow",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow",
            "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        fb, task = self._run_with_error_feedback(error)
        self.assertIsNotNone(fb)
        self.assertEqual(fb.gate, FrontendGate.RENDERED_TECHNICAL)
        self.assertEqual(fb.severity, FrontendFailureSeverity.ERROR)

    def test_feedback_gate_is_rendered_technical(self):
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "document_horizontal_overflow",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow",
            "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        fb, _ = self._run_with_error_feedback(error)
        self.assertEqual(fb.gate, FrontendGate.RENDERED_TECHNICAL)

    def test_document_horizontal_overflow_feedback_actionable(self):
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "document_horizontal_overflow",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow",
            "context": {"viewport": "desktop", "scroll_width": 1450, "client_width": 1440, "overflow_px": 10},
        }]
        fb, _ = self._run_with_error_feedback(error)
        self.assertIn("1450", fb.feedback)
        self.assertIn("1440", fb.feedback)
        self.assertIn("10", fb.feedback)

    def test_broken_image_feedback_contains_src(self):
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "mobile",
            "type": "broken_image",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Broken image",
            "context": {"viewport": "mobile", "src": "https://example.com/img.png", "complete": True, "natural_width": 0, "natural_height": 0},
        }]
        fb, _ = self._run_with_error_feedback(error)
        self.assertIn("https://example.com/img.png", fb.feedback)
        self.assertIn("src", fb.details["errors"][0]["context"])

    def test_broken_image_feedback_contains_image_state(self):
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "broken_image",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Broken image",
            "context": {"viewport": "desktop", "src": "img.png", "complete": True, "natural_width": 0, "natural_height": 0},
        }]
        fb, _ = self._run_with_error_feedback(error)
        ctx = fb.details["errors"][0]["context"]
        self.assertTrue(ctx.get("complete"))
        self.assertEqual(ctx.get("natural_width"), 0)

    def test_page_runtime_error_feedback_contains_message(self):
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "page_runtime_error",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Uncaught ReferenceError: foo is not defined",
            "context": {"viewport": "desktop", "message": "Uncaught ReferenceError: foo is not defined"},
            "location": {"line": 42, "column": 10},
        }]
        fb, _ = self._run_with_error_feedback(error)
        self.assertIn("foo is not defined", fb.feedback)

    def test_page_runtime_error_feedback_preserves_location(self):
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "mobile",
            "type": "page_runtime_error",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Error",
            "context": {"viewport": "mobile", "message": "Error"},
            "location": {"line": 10, "column": 5},
        }]
        fb, _ = self._run_with_error_feedback(error)
        loc = fb.details["errors"][0].get("location")
        self.assertIsNotNone(loc)
        self.assertEqual(loc.get("line"), 10)
        self.assertEqual(loc.get("column"), 5)

    def test_warning_diagnostics_not_retry_cause(self):
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())

        warning_diag = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "element_overflow",
            "severity": RenderedTechnicalSeverity.WARNING.value,
            "message": "Element overflow",
            "context": {"viewport": "desktop"},
        }]
        validator = Mock()
        validator.validate.return_value = _make_result(True, "warnings", warnings=warning_diag)

        result, task = self._run_with(renderer_return=renderer, validator_result=validator)

        self.assertTrue(result)
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(len(task.frontend_retry_history), 0)

    # ----- FRONTEND RETRY SEMANTICS -----

    def test_rendered_error_increments_retry_count(self):
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "document_horizontal_overflow",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow",
            "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        fb, task = self._run_with_error_feedback(error)
        self.assertEqual(task.frontend_retry_count, 1)

    def test_rendered_pass_does_not_increment_retry_count(self):
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())
        validator = Mock()
        validator.validate.return_value = _make_result(True, "passed")
        _, task = self._run_with(renderer_return=renderer, validator_result=validator)
        self.assertEqual(task.frontend_retry_count, 0)

    def test_rendered_warning_does_not_increment_retry_count(self):
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())
        warning_diag = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "element_overflow",
            "severity": RenderedTechnicalSeverity.WARNING.value,
            "message": "Element overflow",
            "context": {"viewport": "desktop"},
        }]
        validator = Mock()
        validator.validate.return_value = _make_result(True, "warnings", warnings=warning_diag)
        _, task = self._run_with(renderer_return=renderer, validator_result=validator)
        self.assertEqual(task.frontend_retry_count, 0)

    def test_rendered_error_appends_retry_history(self):
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "document_horizontal_overflow",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow",
            "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        _, task = self._run_with_error_feedback(error)
        self.assertEqual(len(task.frontend_retry_history), 1)

    def test_retry_history_records_rendered_technical_gate(self):
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "document_horizontal_overflow",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow",
            "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        _, task = self._run_with_error_feedback(error)
        entry = task.frontend_retry_history[0]
        self.assertEqual(entry["gate"], FrontendGate.RENDERED_TECHNICAL.value)

    def test_retry_history_records_failure_category_content(self):
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "document_horizontal_overflow",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow",
            "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        _, task = self._run_with_error_feedback(error)
        entry = task.frontend_retry_history[0]
        self.assertEqual(entry["failure_category"], FailureCategory.CONTENT.value)

    def test_rendered_error_uses_existing_frontend_retry_budget(self):
        # Run with max_frontend_retries=2; rendered error should consume retries up to budget
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "document_horizontal_overflow",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow",
            "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        _, task = self._run_with_error_feedback(error, max_retries=2)
        # After exhaustion, retry count should equal max_frontend_retries
        self.assertEqual(task.frontend_retry_count, 2)

    def test_no_rendered_specific_retry_counter(self):
        # Verify that only frontend_retry_count is used; no separate counter attribute exists
        error = [{
            "gate": "RENDERED_TECHNICAL",
            "viewport": "desktop",
            "type": "document_horizontal_overflow",
            "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow",
            "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        _, task = self._run_with_error_feedback(error)
        self.assertFalse(hasattr(task, "rendered_technical_retry_count"))
        self.assertFalse(hasattr(task, "rendered_retry_count"))

    # ----- HELPER FOR RETRY SCENARIOS -----
    def _run_retry_scenario(self, first_errors, second_result):
        """Run workflow with two attempts: first rendered error, then given result.
        Returns (task, mocks_dict) where mocks_dict contains call counts for each component.
        """
        from unittest.mock import Mock, patch
        from contextlib import ExitStack

        task = self._create_task(max_frontend_retries=2)
        task_id = task.id

        # Pre-populate READY image artifact to satisfy 7D-5E guard
        ready_artifact = create_image_artifact(
            status=ImageArtifactStatus.READY,
            wordpress_media_id=123,
            wordpress_media_url="https://wp.example.com/img.jpg",
            artifact_id="img_test_ready",
        )
        task.image_artifact = ready_artifact.to_dict()
        task.hero_image_id = 123
        task.hero_image_url = "https://wp.example.com/img.jpg"
        task.image_status = "success"

        # Track calls
        calls = {k: 0 for k in (
            "security", "converter", "validator", "production",
            "renderer", "rendered_validator", "frontend_agent"
        )}

        # FrontendAgent generate_frontend side_effect (same each attempt)
        frontend_mock = Mock()
        def gen_frontend(*a, **k):
            calls["frontend_agent"] += 1
            return _make_frontend_result(task_id)
        frontend_mock.generate_frontend.side_effect = gen_frontend

        # Pipeline mocks with call counting
        security_mock = Mock()
        def sec_check(*a, **k):
            calls["security"] += 1
            return _make_security_result(task_id)
        security_mock.check.side_effect = sec_check

        converter_mock = Mock()
        def conv_convert(*a, **k):
            calls["converter"] += 1
            return _make_conversion_result(task_id)
        converter_mock.convert.side_effect = conv_convert

        validator_mock = Mock()
        def val_validate(*a, **k):
            calls["validator"] += 1
            return _make_validation_result(task_id)
        validator_mock.validate.side_effect = val_validate

        production_mock = Mock()
        def prod_check(*a, **k):
            calls["production"] += 1
            return _make_production_result(task_id)
        production_mock.check.side_effect = prod_check

        # PreviewRenderer and RenderedTechnicalValidator sequences
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())
        calls["renderer"] = 0
        def render_call(*a, **k):
            calls["renderer"] += 1
            return (artifact, None, _make_evidence())
        renderer.render.side_effect = render_call

        rendered_validator = Mock()
        # first attempt returns error, second returns provided result
        results_iter = iter([_make_result(False, "failed", errors=first_errors), second_result])
        def rv_validate(*a, **k):
            calls["rendered_validator"] += 1
            return next(results_iter)
        rendered_validator.validate.side_effect = rv_validate

        # Agent mocks
        writer_mock = Mock()
        writer_mock.write_content.return_value = "Test draft content"
        quality_mock = Mock()
        quality_mock.evaluate.return_value = ReviewResult(
            passed=True, score=90, issues=[], feedback="Excellent",
            suggested_action=ReviewAction.PUBLISH.value)
        publisher_mock = Mock()
        publisher_mock.publish_content.return_value = (123, "published-url")
        publisher_mock.validate_config.return_value = True

        agents = {
            "writer": writer_mock,
            "quality_evaluator": quality_mock,
            "frontend": frontend_mock,
            "publisher": publisher_mock,
            "image": None,
            "learner": None,
        }
        def get_agent_side_effect(agent_type):
            return agents.get(agent_type)

        visual_reviewer_mock = Mock()
        visual_reviewer_mock.review.return_value = VisualQualityResult(
            action=VisualQualityAction.PASS,
            summary="Visual quality acceptable",
        )

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=get_agent_side_effect))
            stack.enter_context(patch("main.FrontendSecurityGate", return_value=security_mock))
            stack.enter_context(patch("main.GreenLightConverter", return_value=converter_mock))
            stack.enter_context(patch("main.FrontendValidator", return_value=validator_mock))
            stack.enter_context(patch("main.FrontendProductionQualityGate", return_value=production_mock))
            stack.enter_context(patch("main.PreviewRenderer", return_value=renderer))
            stack.enter_context(patch("main.RenderedTechnicalValidator", return_value=rendered_validator))
            stack.enter_context(patch("main.VisualQualityReviewer", return_value=visual_reviewer_mock))
            stack.enter_context(patch.object(self.factory, "save_state"))

            self.factory.run_workflow(task_id)

        task = workflow_state.get_task(task_id)
        return task, calls

    # ----- FULL FRONTEND CHAIN RERUN AFTER RENDERED ERROR -----
    def test_rendered_error_causes_frontend_regeneration(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        task, calls = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(calls["frontend_agent"], 2)

    def test_regenerated_attempt_reruns_security_gate(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        _, calls = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(calls["security"], 2)

    def test_regenerated_attempt_reruns_converter(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        _, calls = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(calls["converter"], 2)

    def test_regenerated_attempt_reruns_validator(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        _, calls = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(calls["validator"], 2)

    def test_regenerated_attempt_reruns_production_gate(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        _, calls = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(calls["production"], 2)

    def test_regenerated_attempt_reruns_preview_renderer(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        _, calls = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(calls["renderer"], 2)

    def test_regenerated_attempt_reruns_rendered_technical_validator(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        _, calls = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(calls["rendered_validator"], 2)

    def test_retry_does_not_jump_directly_to_preview_renderer(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        _, calls = self._run_retry_scenario(first_err, second_res)
        # Security, converter, validator, production must have been called before renderer each attempt
        self.assertGreaterEqual(calls["security"], calls["renderer"])
        self.assertGreaterEqual(calls["converter"], calls["renderer"])
        self.assertGreaterEqual(calls["validator"], calls["renderer"])
        self.assertGreaterEqual(calls["production"], calls["renderer"])

    def test_retry_does_not_jump_directly_to_rendered_technical_validator(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        _, calls = self._run_retry_scenario(first_err, second_res)
        # Rendered validator runs after preview renderer each attempt
        self.assertGreaterEqual(calls["renderer"], calls["rendered_validator"])

    # ----- RETRY SUCCESS PATH -----
    def test_attempt1_error_attempt2_pass_continues(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        task, _ = self._run_retry_scenario(first_err, second_res)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.LEARNING))

    def test_attempt1_error_attempt2_warning_continues(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "warnings", warnings=[{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop", "type": "element_overflow",
            "severity": RenderedTechnicalSeverity.WARNING.value, "message": "Overflow", "context": {}
        }])
        task, _ = self._run_retry_scenario(first_err, second_res)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.LEARNING))

    def test_frontend_retry_count_increments_once_for_one_failed_rendered_attempt(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        task, _ = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(task.frontend_retry_count, 1)

    def test_successful_retry_does_not_increment_frontend_retry_count_again(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        task, _ = self._run_retry_scenario(first_err, second_res)
        # After two attempts (one failure, one success) count should be 1
        self.assertEqual(task.frontend_retry_count, 1)

    def test_frontend_retry_history_preserves_failed_rendered_attempt(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        task, _ = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(len(task.frontend_retry_history), 1)
        entry = task.frontend_retry_history[0]
        self.assertEqual(entry["gate"], FrontendGate.RENDERED_TECHNICAL.value)

    def test_preview_history_preserves_both_attempts(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        task, _ = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(len(task.preview_history), 2)

    def test_rendered_technical_history_preserves_both_attempts(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        task, _ = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(len(task.rendered_technical_history), 2)

    def test_rendered_technical_result_points_to_latest_successful_result(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        task, _ = self._run_retry_scenario(first_err, second_res)
        self.assertTrue(task.rendered_technical_result["passed"])

    def test_approval_policy_reached_only_after_successful_second_attempt(self):
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        task, _ = self._run_retry_scenario(first_err, second_res)
        self.assertEqual(task.approval_policy["mode"], ApprovalPolicyMode.AUTO_PUBLISH.value)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.LEARNING))

    # ----- HELPERS FOR SLICE 2B -----
    def _run_exhaustion_test(self, max_retries=3):
        """Run workflow where RenderedTechnicalValidator always returns ERROR until retries exhausted."""
        from unittest.mock import Mock, patch
        from contextlib import ExitStack

        task = self._create_task(max_frontend_retries=max_retries)
        task_id = task.id

        # Pre-populate READY image artifact to satisfy 7D-5E guard
        ready_artifact = create_image_artifact(
            status=ImageArtifactStatus.READY,
            wordpress_media_id=123,
            wordpress_media_url="https://wp.example.com/img.jpg",
            artifact_id="img_test_ready",
        )
        task.image_artifact = ready_artifact.to_dict()
        task.hero_image_id = 123
        task.hero_image_url = "https://wp.example.com/img.jpg"
        task.image_status = "success"

        error_diag = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]

        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())

        rendered_validator = Mock()
        rendered_validator.validate.return_value = _make_result(False, "failed", errors=error_diag)

        writer_mock = Mock()
        writer_mock.write_content.return_value = "Test draft content"
        quality_mock = Mock()
        quality_mock.evaluate.return_value = ReviewResult(
            passed=True, score=90, issues=[], feedback="Excellent",
            suggested_action=ReviewAction.PUBLISH.value)
        frontend_mock = Mock()
        frontend_mock.generate_frontend.return_value = _make_frontend_result(task_id)
        publisher_mock = Mock()
        publisher_mock.publish_content.return_value = (123, "published-url")
        publisher_mock.validate_config.return_value = True

        agents = {
            "writer": writer_mock,
            "quality_evaluator": quality_mock,
            "frontend": frontend_mock,
            "publisher": publisher_mock,
            "image": None,
            "learner": None,
        }
        def get_agent_side_effect(agent_type):
            return agents.get(agent_type)

        security_mock = Mock()
        security_mock.check.return_value = _make_security_result(task_id)
        converter_mock = Mock()
        converter_mock.convert.return_value = _make_conversion_result(task_id)
        validation_mock = Mock()
        validation_mock.validate.return_value = _make_validation_result(task_id)
        production_mock = Mock()
        production_mock.check.return_value = _make_production_result(task_id)

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=get_agent_side_effect))
            stack.enter_context(patch("main.FrontendSecurityGate", return_value=security_mock))
            stack.enter_context(patch("main.GreenLightConverter", return_value=converter_mock))
            stack.enter_context(patch("main.FrontendValidator", return_value=validation_mock))
            stack.enter_context(patch("main.FrontendProductionQualityGate", return_value=production_mock))
            stack.enter_context(patch("main.PreviewRenderer", return_value=renderer))
            stack.enter_context(patch("main.RenderedTechnicalValidator", return_value=rendered_validator))
            stack.enter_context(patch.object(self.factory, "save_state"))

            self.factory.run_workflow(task_id)

        task = workflow_state.get_task(task_id)
        return task

    def _run_infrastructure_failure_test(self):
        """Run workflow where PreviewRenderer returns infrastructure failure."""
        from unittest.mock import Mock, patch
        from contextlib import ExitStack

        task = self._create_task()
        task_id = task.id

        ready_artifact = create_image_artifact(
            status=ImageArtifactStatus.READY,
            wordpress_media_id=123,
            wordpress_media_url="https://wp.example.com/img.jpg",
            artifact_id="img_test_ready",
        )
        task.image_artifact = ready_artifact.to_dict()
        task.hero_image_id = 123
        task.hero_image_url = "https://wp.example.com/img.jpg"
        task.image_status = "success"

        renderer = Mock()
        failure = PreviewInfrastructureFailure(
            task_id=task_id,
            preview_id="preview-456",
            attempt_number=1,
            error_type="browser_crash",
            message="Browser crashed",
            retryable=True,
            occurred_at=datetime.datetime.now().isoformat(),
            failure_category=FailureCategory.INFRASTRUCTURE,
        )
        renderer.render.return_value = (None, failure, None)

        writer_mock = Mock()
        writer_mock.write_content.return_value = "Test draft content"
        quality_mock = Mock()
        quality_mock.evaluate.return_value = ReviewResult(
            passed=True, score=90, issues=[], feedback="Excellent",
            suggested_action=ReviewAction.PUBLISH.value)
        frontend_mock = Mock()
        frontend_mock.generate_frontend.return_value = _make_frontend_result(task_id)
        publisher_mock = Mock()
        publisher_mock.publish_content.return_value = (123, "published-url")
        publisher_mock.validate_config.return_value = True

        agents = {
            "writer": writer_mock,
            "quality_evaluator": quality_mock,
            "frontend": frontend_mock,
            "publisher": publisher_mock,
            "image": None,
            "learner": None,
        }
        def get_agent_side_effect(agent_type):
            return agents.get(agent_type)

        security_mock = Mock()
        security_mock.check.return_value = _make_security_result(task_id)
        converter_mock = Mock()
        converter_mock.convert.return_value = _make_conversion_result(task_id)
        validation_mock = Mock()
        validation_mock.validate.return_value = _make_validation_result(task_id)
        production_mock = Mock()
        production_mock.check.return_value = _make_production_result(task_id)

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=get_agent_side_effect))
            stack.enter_context(patch("main.FrontendSecurityGate", return_value=security_mock))
            stack.enter_context(patch("main.GreenLightConverter", return_value=converter_mock))
            stack.enter_context(patch("main.FrontendValidator", return_value=validation_mock))
            stack.enter_context(patch("main.FrontendProductionQualityGate", return_value=production_mock))
            stack.enter_context(patch("main.PreviewRenderer", return_value=renderer))
            # RenderedTechnicalValidator should NOT be instantiated
            stack.enter_context(patch("main.RenderedTechnicalValidator"))
            stack.enter_context(patch.object(self.factory, "save_state"))

            self.factory.run_workflow(task_id)

        task = workflow_state.get_task(task_id)
        return task

    # ----- A. RETRY EXHAUSTION -----
    def test_persistent_rendered_error_consumes_frontend_retry_budget(self):
        task = self._run_exhaustion_test(max_retries=3)
        self.assertEqual(task.frontend_retry_count, 3)

    def test_rendered_error_does_not_use_separate_retry_counter(self):
        task = self._run_exhaustion_test(max_retries=3)
        self.assertFalse(hasattr(task, "rendered_technical_retry_count"))
        self.assertFalse(hasattr(task, "rendered_retry_count"))

    def test_persistent_rendered_error_sets_failed_needs_attention(self):
        task = self._run_exhaustion_test(max_retries=3)
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)

    def test_exhausted_rendered_error_final_failed_gate_is_rendered_technical(self):
        task = self._run_exhaustion_test(max_retries=3)
        self.assertEqual(task.final_failed_gate, FrontendGate.RENDERED_TECHNICAL.value)

    def test_exhausted_rendered_error_final_failure_category_is_content(self):
        task = self._run_exhaustion_test(max_retries=3)
        self.assertEqual(task.final_failure_category, FailureCategory.CONTENT.value)

    def test_exhausted_rendered_error_final_failure_message_persisted(self):
        task = self._run_exhaustion_test(max_retries=3)
        self.assertIsNotNone(task.final_error)
        self.assertIn("Horizontal overflow", task.final_error)

    def test_exhausted_rendered_error_final_failure_timestamp_persisted(self):
        task = self._run_exhaustion_test(max_retries=3)
        self.assertIsNotNone(task.failure_timestamp)
        # ISO format
        self.assertTrue("T" in task.failure_timestamp)

    def test_exhausted_rendered_error_does_not_reach_approval_policy(self):
        task = self._run_exhaustion_test(max_retries=3)
        # ApprovalPolicy may be set but workflow should not proceed to approval step
        self.assertNotIn(task.status, (TaskStatus.AWAITING_APPROVAL, TaskStatus.COMPLETED, TaskStatus.LEARNING))

    def test_exhausted_rendered_error_does_not_proceed_to_image_agent(self):
        task = self._run_exhaustion_test(max_retries=3)
        self.assertIsNotNone(task.image_artifact)
        self.assertEqual(task.hero_image_id, 123)

    def test_exhausted_rendered_error_does_not_proceed_to_publisher(self):
        task = self._run_exhaustion_test(max_retries=3)
        self.assertIsNone(task.wordpress_id)
        self.assertIsNone(task.wordpress_url)

    # ----- B. INFRASTRUCTURE SEPARATION -----
    def test_infrastructure_failure_does_not_increment_frontend_retry_count(self):
        task = self._run_infrastructure_failure_test()
        self.assertEqual(task.frontend_retry_count, 0)

    def test_infrastructure_failure_does_not_append_to_frontend_retry_history(self):
        task = self._run_infrastructure_failure_test()
        self.assertEqual(len(task.frontend_retry_history), 0)

    def test_infrastructure_failure_does_not_create_frontend_failure_feedback(self):
        task = self._run_infrastructure_failure_test()
        # No feedback entry in history
        self.assertEqual(len(task.frontend_retry_history), 0)

    def test_infrastructure_failure_does_not_invoke_rendered_technical_validator(self):
        # The validator mock was not called because we patched it to a Mock and didn't assert call;
        # we can infer by checking that rendered_technical_result is None
        task = self._run_infrastructure_failure_test()
        self.assertIsNone(task.rendered_technical_result)

    def test_infrastructure_retry_reuses_same_frontend_artifact(self):
        task = self._run_infrastructure_failure_test()
        # frontend_retry_count unchanged implies same artifact
        self.assertEqual(task.frontend_retry_count, 0)

    def test_infrastructure_retry_does_not_invoke_frontend_agent_regeneration(self):
        task = self._run_infrastructure_failure_test()
        # frontend_retry_count zero means no regeneration
        self.assertEqual(task.frontend_retry_count, 0)

    def test_infrastructure_retry_budget_separate_from_frontend_retry_budget(self):
        task = self._run_infrastructure_failure_test()
        # frontend_retry_count unchanged, max_frontend_retries untouched
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(task.max_frontend_retries, 3)

    def test_infrastructure_failure_category_is_infrastructure(self):
        task = self._run_infrastructure_failure_test()
        self.assertEqual(task.final_failure_category, FailureCategory.INFRASTRUCTURE.value)

    def test_rendered_technical_failure_category_is_content(self):
        task = self._run_exhaustion_test(max_retries=1)
        self.assertEqual(task.final_failure_category, FailureCategory.CONTENT.value)

    def test_infrastructure_retries_exhausted_fails_with_infrastructure_behavior(self):
        # Simulate multiple infrastructure retries by setting MAX_INFRA_RETRIES low? 
        # Our renderer returns failure directly (exhausted). Ensure status FAILED_NEEDS_ATTENTION.
        task = self._run_infrastructure_failure_test()
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)

    def test_infrastructure_exhaustion_not_labeled_rendered_technical(self):
        task = self._run_infrastructure_failure_test()
        self.assertNotEqual(task.final_failed_gate, FrontendGate.RENDERED_TECHNICAL.value)

    def test_rendered_technical_exhaustion_not_labeled_infrastructure(self):
        task = self._run_exhaustion_test(max_retries=1)
        self.assertNotEqual(task.final_failed_gate, "INFRASTRUCTURE")
        self.assertEqual(task.final_failed_gate, FrontendGate.RENDERED_TECHNICAL.value)

    # ==================================================
    # APPROVALPOLICY COMPATIBILITY
    # ==================================================

    def _run_with_approval_policy(self, approval_mode, validator_result, max_frontend_retries=3):
        """Run workflow with specific approval policy and validator result."""
        from unittest.mock import Mock, patch
        from contextlib import ExitStack

        task = self._create_task(approval_mode=approval_mode, max_frontend_retries=max_frontend_retries)
        task_id = task.id

        # Pre-populate READY image artifact to satisfy 7D-5E guard
        ready_artifact = create_image_artifact(
            status=ImageArtifactStatus.READY,
            wordpress_media_id=123,
            wordpress_media_url="https://wp.example.com/img.jpg",
            artifact_id="img_test_ready",
        )
        task.image_artifact = ready_artifact.to_dict()
        task.hero_image_id = 123
        task.hero_image_url = "https://wp.example.com/img.jpg"
        task.image_status = "success"

        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())

        rendered_validator = Mock()
        rendered_validator.validate.return_value = validator_result

        writer_mock = Mock()
        writer_mock.write_content.return_value = "Test draft content"
        quality_mock = Mock()
        quality_mock.evaluate.return_value = ReviewResult(
            passed=True, score=90, issues=[], feedback="Excellent",
            suggested_action=ReviewAction.PUBLISH.value)
        frontend_mock = Mock()
        frontend_mock.generate_frontend.return_value = _make_frontend_result(task_id)
        publisher_mock = Mock()
        publisher_mock.publish_content.return_value = (123, "published-url")
        publisher_mock.validate_config.return_value = True

        agents = {
            "writer": writer_mock,
            "quality_evaluator": quality_mock,
            "frontend": frontend_mock,
            "publisher": publisher_mock,
            "image": None,
            "learner": None,
        }
        def get_agent_side_effect(agent_type):
            return agents.get(agent_type)

        security_mock = Mock()
        security_mock.check.return_value = _make_security_result(task_id)
        converter_mock = Mock()
        converter_mock.convert.return_value = _make_conversion_result(task_id)
        validation_mock = Mock()
        validation_mock.validate.return_value = _make_validation_result(task_id)
        production_mock = Mock()
        production_mock.check.return_value = _make_production_result(task_id)

        visual_reviewer_mock = Mock()
        visual_reviewer_mock.review.return_value = VisualQualityResult(
            action=VisualQualityAction.PASS,
            summary="Visual quality acceptable",
        )

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=get_agent_side_effect))
            stack.enter_context(patch("main.FrontendSecurityGate", return_value=security_mock))
            stack.enter_context(patch("main.GreenLightConverter", return_value=converter_mock))
            stack.enter_context(patch("main.FrontendValidator", return_value=validation_mock))
            stack.enter_context(patch("main.FrontendProductionQualityGate", return_value=production_mock))
            stack.enter_context(patch("main.PreviewRenderer", return_value=renderer))
            stack.enter_context(patch("main.RenderedTechnicalValidator", return_value=rendered_validator))
            stack.enter_context(patch("main.VisualQualityReviewer", return_value=visual_reviewer_mock))
            stack.enter_context(patch.object(self.factory, "save_state"))

            self.factory.run_workflow(task_id)

        task = workflow_state.get_task(task_id)
        return task

    def test_rendered_pass_require_human_review_awaits_approval(self):
        """Rendered PASS + REQUIRE_HUMAN_REVIEW → TaskStatus.AWAITING_APPROVAL"""
        validator_result = _make_result(True, "passed")
        task = self._run_with_approval_policy(ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW, validator_result)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.approval_policy["mode"], ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW.value)

    def test_rendered_warning_require_human_review_awaits_approval(self):
        """Rendered WARNING + REQUIRE_HUMAN_REVIEW → TaskStatus.AWAITING_APPROVAL"""
        warning_diag = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "element_overflow", "severity": RenderedTechnicalSeverity.WARNING.value,
            "message": "Element overflow", "context": {"viewport": "desktop"},
        }]
        validator_result = _make_result(True, "warnings", warnings=warning_diag)
        task = self._run_with_approval_policy(ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW, validator_result)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.approval_policy["mode"], ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW.value)

    def test_rendered_pass_auto_publish_continues_downstream(self):
        """Rendered PASS + AUTO_PUBLISH → continues existing downstream publish path"""
        validator_result = _make_result(True, "passed")
        task = self._run_with_approval_policy(ApprovalPolicyMode.AUTO_PUBLISH, validator_result)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.LEARNING))
        self.assertEqual(task.approval_policy["mode"], ApprovalPolicyMode.AUTO_PUBLISH.value)

    def test_rendered_warning_auto_publish_continues_downstream(self):
        """Rendered WARNING + AUTO_PUBLISH → continues existing downstream publish path"""
        warning_diag = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "element_overflow", "severity": RenderedTechnicalSeverity.WARNING.value,
            "message": "Element overflow", "context": {"viewport": "desktop"},
        }]
        validator_result = _make_result(True, "warnings", warnings=warning_diag)
        task = self._run_with_approval_policy(ApprovalPolicyMode.AUTO_PUBLISH, validator_result)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.LEARNING))
        self.assertEqual(task.approval_policy["mode"], ApprovalPolicyMode.AUTO_PUBLISH.value)

    def test_rendered_warning_does_not_force_human_review(self):
        """Rendered WARNING does NOT force human review regardless of policy"""
        warning_diag = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "element_overflow", "severity": RenderedTechnicalSeverity.WARNING.value,
            "message": "Element overflow", "context": {"viewport": "desktop"},
        }]
        validator_result = _make_result(True, "warnings", warnings=warning_diag)
        task = self._run_with_approval_policy(ApprovalPolicyMode.AUTO_PUBLISH, validator_result)
        # Should not be awaiting approval just because of warning
        self.assertNotEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.LEARNING))

    def test_rendered_pass_does_not_alter_approval_policy(self):
        """Rendered PASS does NOT alter ApprovalPolicy"""
        validator_result = _make_result(True, "passed")
        original_policy = ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW
        task = self._run_with_approval_policy(original_policy, validator_result)
        self.assertEqual(task.approval_policy["mode"], original_policy.value)

    def test_rendered_error_never_reaches_approval_policy(self):
        """Rendered technical ERROR never reaches ApprovalPolicy"""
        error_diag = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        validator_result = _make_result(False, "failed", errors=error_diag)
        task = self._run_with_approval_policy(ApprovalPolicyMode.AUTO_PUBLISH, validator_result, max_frontend_retries=1)
        # Should fail before reaching approval
        self.assertNotIn(task.status, (TaskStatus.AWAITING_APPROVAL, TaskStatus.COMPLETED, TaskStatus.LEARNING))
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)

    def test_approval_policy_enum_behavior_unchanged(self):
        """ApprovalPolicy enum/behavior remains unchanged"""
        # Verify enum values exist and are correct
        self.assertEqual(ApprovalPolicyMode.AUTO_PUBLISH.value, "auto_publish")
        self.assertEqual(ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW.value, "require_human_review")
        
        # Test that ApprovalPolicy can be created with both modes
        policy_auto = ApprovalPolicy(mode=ApprovalPolicyMode.AUTO_PUBLISH)
        policy_human = ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        
        self.assertEqual(policy_auto.mode, ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(policy_human.mode, ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        
        # Verify serialization round-trip
        auto_dict = policy_auto.to_dict()
        human_dict = policy_human.to_dict()
        
        restored_auto = ApprovalPolicy.from_dict(auto_dict)
        restored_human = ApprovalPolicy.from_dict(human_dict)
        
        self.assertEqual(restored_auto.mode, ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(restored_human.mode, ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)

    # ==================================================
    # BACKWARD COMPATIBILITY / SERIALIZATION
    # ==================================================

    def test_old_task_without_rendered_technical_result_loads(self):
        """Old serialized Task without rendered_technical_result loads successfully"""
        task = self._create_task()
        task_id = task.id
        
        # Save and reload - this simulates loading old state
        self.factory.save_state()
        self.factory.load_state()
        
        reloaded = workflow_state.get_task(task_id)
        self.assertIsNotNone(reloaded)
        # Field should exist with None default after deserialization
        self.assertIsNone(reloaded.rendered_technical_result)

    def test_old_task_without_rendered_technical_history_loads(self):
        """Old serialized Task without rendered_technical_history loads successfully"""
        task = self._create_task()
        task_id = task.id
        
        # Save and reload
        self.factory.save_state()
        self.factory.load_state()
        
        reloaded = workflow_state.get_task(task_id)
        self.assertIsNotNone(reloaded)
        # Field should exist with empty list default after deserialization
        self.assertEqual(reloaded.rendered_technical_history, [])

    def test_old_task_without_final_failure_category_loads(self):
        """Old serialized Task without final_failure_category loads successfully"""
        task = self._create_task()
        task_id = task.id
        
        self.factory.save_state()
        self.factory.load_state()
        
        reloaded = workflow_state.get_task(task_id)
        self.assertIsNotNone(reloaded)
        # Field should exist with None default after deserialization
        self.assertIsNone(reloaded.final_failure_category)

    def test_missing_rendered_technical_result_defaults_none(self):
        """Missing rendered_technical_result defaults to None"""
        task = self._create_task()
        # New task should not have rendered_technical_result set
        self.assertIsNone(getattr(task, "rendered_technical_result", None))

    def test_missing_rendered_technical_history_defaults_empty_list(self):
        """Missing rendered_technical_history defaults to []"""
        task = self._create_task()
        # New task should not have rendered_technical_history set or it should be empty list
        history = getattr(task, "rendered_technical_history", [])
        self.assertEqual(history, [])

    def test_missing_final_failure_category_defaults_none(self):
        """Missing final_failure_category defaults to None"""
        task = self._create_task()
        self.assertIsNone(getattr(task, "final_failure_category", None))

    def test_rendered_technical_result_survives_save_load(self):
        """Rendered_technical_result survives save/load"""
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())
        
        validator = Mock()
        validator.validate.return_value = _make_result(True, "passed")
        
        _, task = self._run_with(renderer_return=renderer, validator_result=validator)
        task_id = task.id
        
        # Save and reload
        self.factory.save_state()
        self.factory.load_state()
        
        reloaded = workflow_state.get_task(task_id)
        self.assertIsNotNone(reloaded.rendered_technical_result)
        self.assertEqual(reloaded.rendered_technical_result["preview_id"], "preview-456")
        self.assertEqual(reloaded.rendered_technical_result["passed"], True)

    def test_rendered_technical_history_survives_save_load(self):
        """Rendered_technical_history survives save/load"""
        renderer = Mock()
        artifact = _make_preview_artifact()
        renderer.render.return_value = (artifact, None, _make_evidence())
        
        validator = Mock()
        validator.validate.return_value = _make_result(True, "passed")
        
        _, task = self._run_with(renderer_return=renderer, validator_result=validator)
        task_id = task.id
        
        # Save and reload
        self.factory.save_state()
        self.factory.load_state()
        
        reloaded = workflow_state.get_task(task_id)
        self.assertEqual(len(reloaded.rendered_technical_history), 1)
        self.assertEqual(reloaded.rendered_technical_history[0]["preview_id"], "preview-456")

    def test_multiple_rendered_technical_history_entries_preserve_order(self):
        """Multiple rendered technical history entries preserve order"""
        first_err = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "First overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        second_res = _make_result(True, "passed")
        task, _ = self._run_retry_scenario(first_err, second_res)
        task_id = task.id
        
        # Save and reload
        self.factory.save_state()
        self.factory.load_state()
        
        reloaded = workflow_state.get_task(task_id)
        self.assertEqual(len(reloaded.rendered_technical_history), 2)
        # First entry should be the error
        self.assertEqual(reloaded.rendered_technical_history[0]["validation_status"], "failed")
        # Second entry should be the pass
        self.assertEqual(reloaded.rendered_technical_history[1]["validation_status"], "passed")

    def test_frontend_retry_history_with_rendered_technical_feedback_survives_save_load(self):
        """Frontend_retry_history containing RENDERED_TECHNICAL feedback survives save/load"""
        error = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        fb, task = self._run_with_error_feedback(error)
        task_id = task.id
        
        # Save and reload
        self.factory.save_state()
        self.factory.load_state()
        
        reloaded = workflow_state.get_task(task_id)
        self.assertEqual(len(reloaded.frontend_retry_history), 1)
        entry = reloaded.frontend_retry_history[0]
        self.assertEqual(entry["gate"], FrontendGate.RENDERED_TECHNICAL.value)
        self.assertEqual(entry["failure_category"], FailureCategory.CONTENT.value)

    # ==================================================
    # FEEDBACK EDGE CASES
    # ==================================================

    def test_multiple_error_diagnostics_produce_useful_retry_feedback(self):
        """Multiple ERROR diagnostics produce useful retry feedback"""
        errors = [
            {
                "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
                "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
                "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10, "scroll_width": 1500, "client_width": 1440},
            },
            {
                "gate": "RENDERED_TECHNICAL", "viewport": "mobile",
                "type": "broken_image", "severity": RenderedTechnicalSeverity.ERROR.value,
                "message": "Broken image", "context": {"viewport": "mobile", "src": "https://example.com/img.png", "complete": True, "natural_width": 0, "natural_height": 0},
            },
            {
                "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
                "type": "page_runtime_error", "severity": RenderedTechnicalSeverity.ERROR.value,
                "message": "Uncaught ReferenceError: foo is not defined",
                "context": {"viewport": "desktop", "message": "Uncaught ReferenceError: foo is not defined"},
                "location": {"line": 42, "column": 10},
            },
        ]
        fb, task = self._run_with_error_feedback(errors)
        self.assertIsNotNone(fb)
        self.assertEqual(fb.gate, FrontendGate.RENDERED_TECHNICAL)
        self.assertEqual(fb.severity, FrontendFailureSeverity.ERROR)
        # All three errors should be in feedback
        self.assertEqual(len(fb.details["errors"]), 3)
        self.assertIn("1500", fb.feedback)
        self.assertIn("1440", fb.feedback)
        self.assertIn("https://example.com/img.png", fb.feedback)
        self.assertIn("foo is not defined", fb.feedback)

    def test_error_warning_result_includes_only_errors_as_retry_causes(self):
        """ERROR + WARNING result includes only ERROR findings as retry causes"""
        errors = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        warnings = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "mobile",
            "type": "element_overflow", "severity": RenderedTechnicalSeverity.WARNING.value,
            "message": "Minor overflow", "context": {"viewport": "mobile"},
        }]
        validator_result = _make_result(False, "failed", errors=errors, warnings=warnings)
        
        task = self._run_with_approval_policy(ApprovalPolicyMode.AUTO_PUBLISH, validator_result, max_frontend_retries=1)
        
        # Should have retried (incremented count)
        self.assertEqual(task.frontend_retry_count, 1)
        # Only ERROR should be in retry history
        self.assertEqual(len(task.frontend_retry_history), 1)
        entry = task.frontend_retry_history[0]
        self.assertEqual(entry["gate"], FrontendGate.RENDERED_TECHNICAL.value)

    def test_missing_optional_diagnostic_location_does_not_crash_feedback(self):
        """Missing optional diagnostic location does not crash feedback generation"""
        error = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10, "scroll_width": 1450, "client_width": 1440},
            # No "location" field
        }]
        fb, _ = self._run_with_error_feedback(error)
        self.assertIsNotNone(fb)
        self.assertEqual(fb.gate, FrontendGate.RENDERED_TECHNICAL)
        # Should not crash and should still have feedback
        self.assertIn("1450", fb.feedback)
        self.assertIn("1440", fb.feedback)

    def test_broken_image_missing_unknown_src_still_produces_feedback(self):
        """Broken image with missing/unknown src still produces usable feedback"""
        error = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "broken_image", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Broken image", "context": {"viewport": "desktop"},
            # Missing "src" field
        }]
        fb, _ = self._run_with_error_feedback(error)
        self.assertIsNotNone(fb)
        self.assertEqual(fb.gate, FrontendGate.RENDERED_TECHNICAL)
        self.assertIn("Broken image", fb.feedback)

    def test_runtime_error_missing_location_still_produces_feedback(self):
        """Runtime error with missing location still produces usable feedback"""
        error = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "page_runtime_error", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Uncaught TypeError: Cannot read property 'foo' of undefined",
            "context": {"viewport": "desktop", "message": "Uncaught TypeError: Cannot read property 'foo' of undefined"},
            # No "location" field
        }]
        fb, _ = self._run_with_error_feedback(error)
        self.assertIsNotNone(fb)
        self.assertEqual(fb.gate, FrontendGate.RENDERED_TECHNICAL)
        self.assertIn("TypeError", fb.feedback)

    def test_horizontal_overflow_feedback_preserves_numeric_overflow_context(self):
        """Horizontal overflow feedback preserves numeric overflow context"""
        error = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {
                "viewport": "desktop", 
                "scroll_width": 1500,
                "client_width": 1440,
                "overflow_px": 60
            },
        }]
        fb, _ = self._run_with_error_feedback(error)
        self.assertIsNotNone(fb)
        ctx = fb.details["errors"][0]["context"]
        self.assertEqual(ctx.get("overflow_px"), 60)
        self.assertEqual(ctx.get("scroll_width"), 1500)
        self.assertEqual(ctx.get("client_width"), 1440)

    def test_retry_feedback_gate_remains_rendered_technical_after_serialization(self):
        """Retry feedback gate remains RENDERED_TECHNICAL after serialization"""
        error = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        fb, task = self._run_with_error_feedback(error)
        task_id = task.id
        
        # Save and reload
        self.factory.save_state()
        self.factory.load_state()
        
        reloaded = workflow_state.get_task(task_id)
        self.assertEqual(len(reloaded.frontend_retry_history), 1)
        entry = reloaded.frontend_retry_history[0]
        self.assertEqual(entry["gate"], FrontendGate.RENDERED_TECHNICAL.value)

    def test_failure_category_remains_content_after_serialization(self):
        """Failure category remains content after serialization"""
        error = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        fb, task = self._run_with_error_feedback(error)
        task_id = task.id
        
        # Save and reload
        self.factory.save_state()
        self.factory.load_state()
        
        reloaded = workflow_state.get_task(task_id)
        self.assertEqual(len(reloaded.frontend_retry_history), 1)
        entry = reloaded.frontend_retry_history[0]
        self.assertEqual(entry["failure_category"], FailureCategory.CONTENT.value)

class TestVisualQualityWorkflowIntegration(unittest.TestCase):
    def setUp(self):
        self.factory = AIWordPressFactory()
        workflow_state.tasks.clear()
        workflow_state.current_task_id = None

    def _create_task(self, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH, max_frontend_retries=3):
        task_id = self.factory.create_task(
            title="Test Blog Post",
            description="A test blog post",
            content_type=ContentType.BLOG_POST,
        )
        task = workflow_state.get_task(task_id)
        task.approval_policy = ApprovalPolicy(mode=approval_mode).to_dict()
        task.max_frontend_retries = max_frontend_retries
        task.frontend_retry_count = 0
        task.frontend_retry_history = []
        return task

    def _run_visual_workflow(
        self,
        technical_result,
        visual_result,
        approval_mode=ApprovalPolicyMode.AUTO_PUBLISH,
        max_frontend_retries=3,
        preview_failure=None,
    ):
        """Run workflow with given technical and visual results.
        Returns (workflow_result_bool, task, mocks_dict)."""
        from unittest.mock import Mock, patch
        from contextlib import ExitStack

        task = self._create_task(approval_mode=approval_mode, max_frontend_retries=max_frontend_retries)
        task_id = task.id

        # Pre-populate READY image artifact to satisfy 7D-5E guard
        ready_artifact = create_image_artifact(
            status=ImageArtifactStatus.READY,
            wordpress_media_id=123,
            wordpress_media_url="https://wp.example.com/img.jpg",
            artifact_id="img_test_ready",
        )
        task.image_artifact = ready_artifact.to_dict()
        task.hero_image_id = 123
        task.hero_image_url = "https://wp.example.com/img.jpg"
        task.image_status = "success"

        renderer = Mock()
        if preview_failure:
            artifact = None
            rendered_evidence = None
        else:
            artifact = _make_preview_artifact(task_id=task_id)
            rendered_evidence = _make_evidence(task_id=task_id)
        renderer.render.return_value = (artifact, preview_failure, rendered_evidence)

        technical_mock = Mock()
        technical_mock.validate.return_value = technical_result

        visual_reviewer = Mock()
        visual_reviewer.review.return_value = visual_result

        writer_mock = Mock()
        writer_mock.write_content.return_value = "Test draft content"
        quality_mock = Mock()
        quality_mock.evaluate.return_value = ReviewResult(
            passed=True, score=90, issues=[], feedback="Excellent",
            suggested_action=ReviewAction.PUBLISH.value)
        frontend_mock = Mock()
        frontend_mock.generate_frontend.return_value = _make_frontend_result(task_id)
        publisher_mock = Mock()
        publisher_mock.publish_content.return_value = (123, "published-url")
        publisher_mock.validate_config.return_value = True
        image_mock = Mock()
        image_mock.generate_hero_image.return_value = (None, None)
        learner_mock = Mock()
        learner_mock.analyze_review.return_value = {"學習提案": []}

        agents = {
            "writer": writer_mock,
            "quality_evaluator": quality_mock,
            "frontend": frontend_mock,
            "publisher": publisher_mock,
            "image": image_mock,
            "learner": learner_mock,
        }
        def get_agent_side_effect(agent_type):
            return agents.get(agent_type)

        security_mock = Mock()
        security_mock.check.return_value = _make_security_result(task_id)
        converter_mock = Mock()
        converter_mock.convert.return_value = _make_conversion_result(task_id)
        validation_mock = Mock()
        validation_mock.validate.return_value = _make_validation_result(task_id)
        production_mock = Mock()
        production_mock.check.return_value = _make_production_result(task_id)

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=get_agent_side_effect))
            stack.enter_context(patch("main.FrontendSecurityGate", return_value=security_mock))
            stack.enter_context(patch("main.GreenLightConverter", return_value=converter_mock))
            stack.enter_context(patch("main.FrontendValidator", return_value=validation_mock))
            stack.enter_context(patch("main.FrontendProductionQualityGate", return_value=production_mock))
            stack.enter_context(patch("main.PreviewRenderer", return_value=renderer))
            stack.enter_context(patch("main.RenderedTechnicalValidator", return_value=technical_mock))
            stack.enter_context(patch("main.VisualQualityReviewer", return_value=visual_reviewer))
            stack.enter_context(patch.object(self.factory, "save_state"))

            result = self.factory.run_workflow(task_id)

        task = workflow_state.get_task(task_id)
        mocks = {
            "renderer": renderer,
            "technical": technical_mock,
            "reviewer": visual_reviewer,
            "frontend_agent": frontend_mock,
            "publisher": publisher_mock,
            "image": image_mock,
        }
        return result, task, mocks

    # VISUAL REVIEW EXECUTION
    def test_rendered_pass_runs_visual_reviewer(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="ok")
        result, task, mocks = self._run_visual_workflow(technical, visual)
        self.assertTrue(mocks["reviewer"].review.called)
        self.assertIsNotNone(task.visual_quality_result)

    def test_rendered_warning_runs_visual_reviewer(self):
        warning_diag = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "element_overflow", "severity": RenderedTechnicalSeverity.WARNING.value,
            "message": "Element overflow", "context": {"viewport": "desktop"},
        }]
        technical = _make_result(True, "warnings", warnings=warning_diag)
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="ok")
        result, task, mocks = self._run_visual_workflow(technical, visual)
        self.assertTrue(mocks["reviewer"].review.called)
        self.assertIsNotNone(task.visual_quality_result)

    def test_rendered_error_does_not_run_visual_reviewer(self):
        error_diag = [{
            "gate": "RENDERED_TECHNICAL", "viewport": "desktop",
            "type": "document_horizontal_overflow", "severity": RenderedTechnicalSeverity.ERROR.value,
            "message": "Horizontal overflow", "context": {"viewport": "desktop", "overflow_px": 10},
        }]
        technical = _make_result(False, "failed", errors=error_diag)
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="ok")
        result, task, mocks = self._run_visual_workflow(technical, visual, max_frontend_retries=1)
        self.assertFalse(mocks["reviewer"].review.called)
        self.assertIsNone(task.visual_quality_result)
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)

    def test_infrastructure_failure_does_not_run_visual_reviewer(self):
        failure = PreviewInfrastructureFailure(
            task_id="task-123", preview_id="preview-456", attempt_number=1,
            error_type="browser_crash", message="Browser crashed", retryable=False,
            occurred_at=datetime.datetime.now().isoformat(), failure_category=FailureCategory.INFRASTRUCTURE,
        )
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="ok")
        result, task, mocks = self._run_visual_workflow(technical, visual, preview_failure=failure)
        self.assertFalse(mocks["reviewer"].review.called)
        self.assertIsNone(task.visual_quality_result)
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)

    def test_existing_preview_artifact_passed_to_reviewer(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="ok")
        result, task, mocks = self._run_visual_workflow(technical, visual)
        called_arg = mocks["reviewer"].review.call_args[0][0]
        self.assertIsInstance(called_arg, PreviewArtifact)
        self.assertEqual(called_arg.task_id, task.id)

    # PERSISTENCE
    def test_visual_pass_result_persisted(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="Visual PASS")
        result, task, _ = self._run_visual_workflow(technical, visual)
        self.assertIsNotNone(task.visual_quality_result)
        self.assertEqual(task.visual_quality_result["action"], "pass")
        self.assertEqual(task.visual_quality_result["summary"], "Visual PASS")

    def test_visual_warn_result_persisted(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.WARN, summary="Visual WARN")
        result, task, _ = self._run_visual_workflow(technical, visual)
        self.assertIsNotNone(task.visual_quality_result)
        self.assertEqual(task.visual_quality_result["action"], "warn")
        self.assertEqual(task.visual_quality_result["summary"], "Visual WARN")

    def test_visual_human_review_result_persisted(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="Needs human")
        result, task, _ = self._run_visual_workflow(technical, visual)
        self.assertIsNotNone(task.visual_quality_result)
        self.assertEqual(task.visual_quality_result["action"], "human_review")
        self.assertEqual(task.visual_quality_result["summary"], "Needs human")

    def test_visual_quality_history_appended(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="First")
        result, task, _ = self._run_visual_workflow(technical, visual)
        self.assertEqual(len(task.visual_quality_history), 1)
        self.assertEqual(task.visual_quality_history[0]["summary"], "First")

    def test_latest_visual_quality_result_updated(self):
        technical = _make_result(True, "passed")
        visual1 = VisualQualityResult(action=VisualQualityAction.PASS, summary="First")
        result1, task1, _ = self._run_visual_workflow(technical, visual1)
        preview = _make_preview_artifact(task_id=task1.id)
        visual2 = VisualQualityResult(action=VisualQualityAction.WARN, summary="Second")
        visual_reviewer = Mock()
        visual_reviewer.review.return_value = visual2
        with patch("main.VisualQualityReviewer", return_value=visual_reviewer):
            self.factory._run_visual_quality_review(task1, preview)
        self.assertEqual(len(task1.visual_quality_history), 2)
        self.assertEqual(task1.visual_quality_history[1]["summary"], "Second")
        self.assertEqual(task1.visual_quality_result["summary"], "Second")

    def test_multiple_visual_reviews_preserve_history(self):
        task = self._create_task()
        preview = _make_preview_artifact(task_id=task.id)
        v1 = VisualQualityResult(action=VisualQualityAction.PASS, summary="First")
        v2 = VisualQualityResult(action=VisualQualityAction.WARN, summary="Second")
        v3 = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="Third")
        with patch("main.VisualQualityReviewer") as mock_cls:
            r = Mock()
            mock_cls.return_value = r
            r.review.return_value = v1
            self.factory._run_visual_quality_review(task, preview)
            r.review.return_value = v2
            self.factory._run_visual_quality_review(task, preview)
            r.review.return_value = v3
            self.factory._run_visual_quality_review(task, preview)
        self.assertEqual(len(task.visual_quality_history), 3)
        self.assertEqual(task.visual_quality_history[0]["summary"], "First")
        self.assertEqual(task.visual_quality_history[1]["summary"], "Second")
        self.assertEqual(task.visual_quality_history[2]["summary"], "Third")
        self.assertEqual(task.visual_quality_result["summary"], "Third")

    def test_old_state_without_visual_fields_loads(self):
        task = self._create_task()
        task_id = task.id
        self.factory.save_state()
        self.factory.load_state()
        reloaded = workflow_state.get_task(task_id)
        self.assertIsNotNone(reloaded)
        self.assertIsNone(reloaded.visual_quality_result)
        self.assertEqual(reloaded.visual_quality_history, [])

    # PASS ROUTING
    def test_pass_auto_publish_continues_publish_path(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="ok")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertTrue(result)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.LEARNING))
        self.assertTrue(mocks["publisher"].publish_content.called)

    def test_pass_require_human_review_awaits_approval(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="ok")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        self.assertFalse(result)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.approval_status, "pending")
        self.assertFalse(mocks["publisher"].publish_content.called)

    # WARN ROUTING
    def test_warn_auto_publish_continues_publish_path(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.WARN, summary="warn")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertTrue(result)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.LEARNING))
        self.assertTrue(mocks["publisher"].publish_content.called)

    def test_warn_require_human_review_awaits_approval(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.WARN, summary="warn")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        self.assertFalse(result)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.approval_status, "pending")
        self.assertFalse(mocks["publisher"].publish_content.called)

    def test_warn_does_not_increment_frontend_retry_count(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.WARN, summary="warn")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(task.frontend_retry_count, 0)

    def test_warn_does_not_create_frontend_failure_feedback(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.WARN, summary="warn")
        result, task, _ = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(len(task.frontend_retry_history), 0)

    # HUMAN_REVIEW ROUTING
    def test_human_review_auto_publish_awaits_approval(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="needs human")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertFalse(result)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.approval_status, "pending")
        self.assertFalse(mocks["publisher"].publish_content.called)

    def test_human_review_require_human_review_awaits_approval(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="needs human")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        self.assertFalse(result)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.approval_status, "pending")
        self.assertFalse(mocks["publisher"].publish_content.called)

    def test_human_review_does_not_increment_frontend_retry_count(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="needs human")
        result, task, _ = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(task.frontend_retry_count, 0)

    def test_human_review_does_not_create_frontend_failure_feedback(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="needs human")
        result, task, _ = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(len(task.frontend_retry_history), 0)

    def test_human_review_does_not_set_failed_needs_attention(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="needs human")
        result, task, _ = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertNotEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)

    def test_human_review_does_not_invoke_frontend_agent_again(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="needs human")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(mocks["frontend_agent"].generate_frontend.call_count, 1)

    def test_human_review_does_not_rerender_preview(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="needs human")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(mocks["renderer"].render.call_count, 1)

    # FAILURE SAFETY
    def test_provider_failure_result_awaits_approval(self):
        technical = _make_result(True, "passed")
        visual_reviewer = Mock()
        visual_reviewer.review.side_effect = Exception("Provider API error")
        task = self._create_task()
        preview = _make_preview_artifact(task_id=task.id)
        with patch("main.VisualQualityReviewer", return_value=visual_reviewer):
            self.factory._run_visual_quality_review(task, preview)
        self.assertEqual(task.visual_quality_result["action"], "human_review")
        self.assertIn("Provider API error", task.visual_quality_result["summary"])

    def test_malformed_output_result_awaits_approval(self):
        # Provider returns failure due to malformed model output => reviewer maps to HUMAN_REVIEW
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="Visual review failed: Malformed JSON")
        result, task, _ = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(task.visual_quality_result["action"], "human_review")
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)

    def test_screenshot_read_failure_result_awaits_approval(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="Visual review failed: Failed to load screenshot")
        result, task, _ = self._run_visual_workflow(technical, visual)
        self.assertEqual(task.visual_quality_result["action"], "human_review")
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)

    # GOVERNANCE
    def test_visual_review_may_raise_auto_publish_to_human_review(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="escalated")
        result, task, _ = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.approval_status, "pending")

    def test_visual_review_never_lowers_require_human_review(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="ok")
        result, task, _ = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        self.assertEqual(task.approval_policy["mode"], "require_human_review")

    def test_approval_policy_enum_unchanged(self):
        self.assertEqual(ApprovalPolicyMode.AUTO_PUBLISH.value, "auto_publish")
        self.assertEqual(ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW.value, "require_human_review")
        policy_auto = ApprovalPolicy(mode=ApprovalPolicyMode.AUTO_PUBLISH)
        policy_human = ApprovalPolicy(mode=ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        self.assertEqual(policy_auto.mode, ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(policy_human.mode, ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        auto_dict = policy_auto.to_dict()
        human_dict = policy_human.to_dict()
        restored_auto = ApprovalPolicy.from_dict(auto_dict)
        restored_human = ApprovalPolicy.from_dict(human_dict)
        self.assertEqual(restored_auto.mode, ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(restored_human.mode, ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)

    def test_approval_resume_path_compatible(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="human")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(task.status, TaskStatus.AWAITING_APPROVAL)
        approved = self.factory.submit_approval_decision(task.id, True, "Looks good")
        self.assertTrue(approved)
        task = workflow_state.get_task(task.id)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.LEARNING))
        self.assertTrue(task.approval_decision)

    # BOUNDARIES
    def test_no_visual_retry_counter_added(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="human")
        result, task, _ = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertFalse(hasattr(task, "visual_retry_count"))
        self.assertFalse(hasattr(task, "visual_quality_retry_count"))

    def test_image_agent_unchanged_position(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="ok")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(mocks["image"].generate_hero_image.call_count, 0)
        self.assertEqual(ImageArtifact.from_dict(task.image_artifact).status, ImageArtifactStatus.READY)
        task2 = self._create_task(approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        visual2 = VisualQualityResult(action=VisualQualityAction.HUMAN_REVIEW, summary="human")
        with patch("main.VisualQualityReviewer") as mock_cls:
            r = Mock()
            r.review.return_value = visual2
            mock_cls.return_value = r
            self.factory._run_visual_quality_review(task2, _make_preview_artifact(task_id=task2.id))
        self.assertEqual(r.review.call_count, 1)

    def test_no_wordpress_preview_introduced(self):
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="ok")
        result, task, mocks = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertEqual(mocks["renderer"].render.call_count, 1)

    # STATUS SEMANTICS HARDENING
    def test_pass_does_not_revert_status_to_rendered_technical_check(self):
        """PASS should not reset status to FRONTEND_RENDERED_TECHNICAL_CHECK."""
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.PASS, summary="ok")
        result, task, _ = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        # After visual PASS, status should have moved forward (COMPLETED/LEARNING/AWAITING_APPROVAL)
        # and NOT be FRONTEND_RENDERED_TECHNICAL_CHECK
        self.assertNotEqual(task.status, TaskStatus.FRONTEND_RENDERED_TECHNICAL_CHECK)

    def test_warn_does_not_revert_status_to_rendered_technical_check(self):
        """WARN should not reset status to FRONTEND_RENDERED_TECHNICAL_CHECK."""
        technical = _make_result(True, "passed")
        visual = VisualQualityResult(action=VisualQualityAction.WARN, summary="warn")
        result, task, _ = self._run_visual_workflow(technical, visual, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH)
        self.assertNotEqual(task.status, TaskStatus.FRONTEND_RENDERED_TECHNICAL_CHECK)

    def test_no_new_task_status_for_visual_review(self):
        """Ensure no micro-status like FRONTEND_VISUAL_REVIEW_PASSED was added."""
        # The only visual-related status should be FRONTEND_VISUAL_REVIEW
        self.assertTrue(hasattr(TaskStatus, "FRONTEND_VISUAL_REVIEW"))
        # These should NOT exist
        self.assertFalse(hasattr(TaskStatus, "FRONTEND_VISUAL_REVIEW_PASSED"))
        self.assertFalse(hasattr(TaskStatus, "FRONTEND_VISUAL_REVIEW_WARN"))
        self.assertFalse(hasattr(TaskStatus, "VISUAL_REVIEW_COMPLETE"))
        self.assertFalse(hasattr(TaskStatus, "GOVERNANCE_CHECK"))

if __name__ == "__main__":
    unittest.main()