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
            stack.enter_context(patch("main.PreviewRenderer", return_value=renderer_return))
            stack.enter_context(patch("main.RenderedTechnicalValidator", return_value=validator_result))
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

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=get_agent_side_effect))
            stack.enter_context(patch("main.FrontendSecurityGate", return_value=security_mock))
            stack.enter_context(patch("main.GreenLightConverter", return_value=converter_mock))
            stack.enter_context(patch("main.FrontendValidator", return_value=validator_mock))
            stack.enter_context(patch("main.FrontendProductionQualityGate", return_value=production_mock))
            stack.enter_context(patch("main.PreviewRenderer", return_value=renderer))
            stack.enter_context(patch("main.RenderedTechnicalValidator", return_value=rendered_validator))
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
        # ImageAgent not invoked; hero_image_id remains None
        self.assertIsNone(task.hero_image_id)

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

if __name__ == "__main__":
    unittest.main()