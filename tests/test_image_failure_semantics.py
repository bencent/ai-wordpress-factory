#!/usr/bin/env python3
"""Focused failure semantics for image preparation and approval continuation."""

import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
import base64
from pathlib import Path
from unittest.mock import Mock, patch

from contracts import (
    ApprovalPolicy,
    ApprovalPolicyMode,
    CritiqueResult,
    FailureCategory,
    FrontendProductionQualityResult,
    FrontendResult,
    FrontendSecurityResult,
    FrontendValidationResult,
    GreenLightConversionResult,
    ImageArtifact,
    ImageArtifactStatus,
    PreviewArtifact,
    PreviewInfrastructureFailure,
    PreviewViewport,
    RenderedEvidence,
    RenderedTechnicalResult,
    ReviewAction,
    ReviewResult,
    ViewportRenderedEvidence,
    VisualQualityAction,
    VisualQualityResult,
    create_image_artifact,
)
from main import AIWordPressFactory
from state import Task, TaskStatus, ContentType, workflow_state
from tools.preview_renderer import PreviewRenderer, PreviewRenderError


import base64

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl"
    "0n9EAAAAASUVORK5CYII="
)


class TestImageFailureSemantics(unittest.TestCase):
    def setUp(self):
        self.factory = AIWordPressFactory()
        workflow_state.tasks.clear()
        workflow_state.current_task_id = None

    def _create_task(self, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH):
        task_id = self.factory.create_task(
            title="Image semantics test",
            description="Image failure semantics",
            content_type=ContentType.BLOG_POST,
        )
        task = workflow_state.get_task(task_id)
        task.approval_policy = ApprovalPolicy(mode=approval_mode).to_dict()
        task.max_frontend_retries = 3
        task.frontend_retry_count = 0
        task.frontend_retry_history = []
        return task

    def _ready_artifact(self, artifact_id="img-ready", media_id=123):
        return create_image_artifact(
            artifact_id=artifact_id,
            status=ImageArtifactStatus.READY,
            wordpress_media_id=media_id,
            wordpress_media_url=f"https://wp.example.com/{artifact_id}.jpg",
            source_url=f"https://provider.example.com/{artifact_id}.png",
        )

    def _failed_artifact(self, artifact_id="img-failed", source_url=None):
        metadata = {"error": "Previous image failure"}
        if source_url:
            metadata["source_url_after_failure"] = source_url
        return create_image_artifact(
            artifact_id=artifact_id,
            status=ImageArtifactStatus.FAILED,
            source_url=source_url,
            metadata=metadata,
        )

    def _pending_artifact(self):
        return create_image_artifact(
            artifact_id="img-pending",
            status=ImageArtifactStatus.PENDING,
        )

    def _frontend_result(self, task_id):
        return FrontendResult(
            task_id=task_id,
            success=True,
            html="<main><h1>Test</h1></main>",
            css="main { color: #222; }",
            javascript="",
        )

    def _security_result(self, task_id):
        return FrontendSecurityResult(
            task_id=task_id,
            passed=True,
            html="<main><h1>Test</h1></main>",
            css="main { color: #222; }",
            javascript="",
            errors=[],
            warnings=[],
            blocked_items=[],
        )

    def _conversion_result(self):
        return GreenLightConversionResult(
            success=True,
            blocks="<!-- wp:paragraph --><p>Test</p><!-- /wp:paragraph -->",
            errors=[],
            warnings=[],
        )

    def _validation_result(self, task_id):
        return FrontendValidationResult(
            task_id=task_id,
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={},
        )

    def _production_result(self):
        return FrontendProductionQualityResult(
            passed=True,
            validation_status="passed",
            errors=[],
            warnings=[],
            diagnostics={},
        )

    def _technical_result(self, task_id, passed=True, attempt_number=1):
        return RenderedTechnicalResult(
            task_id=task_id,
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            attempt_number=attempt_number,
            passed=passed,
            validation_status="passed" if passed else "failed",
            errors=[] if passed else [{"code": "rendered_error"}],
            warnings=[],
            diagnostics={},
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _evidence(self, task_id):
        viewport = ViewportRenderedEvidence(
            viewport_width=1440,
            viewport_height=900,
            document_scroll_width=1440,
            document_client_width=1440,
            document_scroll_height=1200,
            document_client_height=900,
            console_errors=[],
            page_errors=[],
            image_load_states=[],
            element_bounding_boxes=[],
        )
        return RenderedEvidence(
            task_id=task_id,
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            attempt_number=1,
            desktop=viewport,
            mobile=viewport,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _preview_artifact(self, task_id, attempt_number=1, preview_id=None):
        if preview_id is None:
            preview_id = "0192f0c1-2345-7abc-8def-0123456789ab"
        viewport = PreviewViewport(width=1440, height=900)
        # Create preview directory structure and actual PNG files
        preview_base = Path("artifacts/previews")
        preview_dir = preview_base / task_id / f"attempt-{attempt_number}" / preview_id
        preview_dir.mkdir(parents=True, exist_ok=True)
        
        # Create 4 PNG files
        paths = {
            'desktop_viewport_screenshot_path': preview_dir / "desktop-viewport.png",
            'desktop_full_page_screenshot_path': preview_dir / "desktop-full.png",
            'mobile_viewport_screenshot_path': preview_dir / "mobile-viewport.png",
            'mobile_full_page_screenshot_path': preview_dir / "mobile-full.png",
        }
        for path in paths.values():
            path.write_bytes(PNG_1X1)
        
        return PreviewArtifact(
            task_id=task_id,
            preview_id=preview_id,
            attempt_number=attempt_number,
            desktop_viewport_screenshot_path=str(paths['desktop_viewport_screenshot_path']),
            desktop_full_page_screenshot_path=str(paths['desktop_full_page_screenshot_path']),
            mobile_viewport_screenshot_path=str(paths['mobile_viewport_screenshot_path']),
            mobile_full_page_screenshot_path=str(paths['mobile_full_page_screenshot_path']),
            desktop_viewport=viewport,
            mobile_viewport=viewport,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _install_frontend_mocks(self, stack, task, *, technical_results=None, renderer=None, visual_action=VisualQualityAction.PASS):
        frontend_agent = Mock()
        frontend_agent.generate_frontend.return_value = self._frontend_result(task.id)

        planner = Mock()
        planner.create_plan.return_value = {"outline": []}
        research = Mock()
        research.gather_research.return_value = []
        writer = Mock()
        writer.write_content.return_value = "Final content"
        critic = Mock()
        critic.critique.return_value = CritiqueResult(score=90, issues=[])
        seo = Mock()
        seo.optimize_content.return_value = ("Final content", {})
        quality = Mock()
        quality.evaluate.return_value = ReviewResult(
            passed=True,
            score=90,
            feedback="Ready",
            suggested_action=ReviewAction.PUBLISH.value,
        )
        router = Mock()
        router.decide.return_value = "publish"
        final_reviewer = Mock()
        final_reviewer.final_review.return_value = "Final content"
        learner = Mock()
        learner.analyze_review.return_value = {"學習提案": []}
        publisher = Mock()
        publisher.publish_content.return_value = (456, "https://wp.example.com/published")
        publisher.validate_config.return_value = True

        agents = {
            "planner": planner,
            "research": research,
            "writer": writer,
            "critic": critic,
            "seo": seo,
            "quality_evaluator": quality,
            "router": router,
            "final_reviewer": final_reviewer,
            "frontend": frontend_agent,
            "learner": learner,
            "publisher": publisher,
        }
        stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=lambda name: agents.get(name)))

        security = Mock()
        security.check.return_value = self._security_result(task.id)
        converter = Mock()
        converter.convert.return_value = self._conversion_result()
        validator = Mock()
        validator.validate.return_value = self._validation_result(task.id)
        production = Mock()
        production.check.return_value = self._production_result()
        stack.enter_context(patch("main.FrontendSecurityGate", return_value=security))
        stack.enter_context(patch("main.GreenLightConverter", return_value=converter))
        stack.enter_context(patch("main.FrontendValidator", return_value=validator))
        stack.enter_context(patch("main.FrontendProductionQualityGate", return_value=production))

        renderer = renderer or Mock()
        if renderer.render.side_effect is None:
            renderer.render.return_value = (
                self._preview_artifact(task.id),
                None,
                self._evidence(task.id),
            )
        stack.enter_context(patch("main.PreviewRenderer", return_value=renderer))

        rendered_validator = Mock()
        if technical_results is None:
            rendered_validator.validate.return_value = self._technical_result(task.id)
        else:
            rendered_validator.validate.side_effect = technical_results
        stack.enter_context(patch("main.RenderedTechnicalValidator", return_value=rendered_validator))

        visual_reviewer = Mock()
        visual_reviewer.review.return_value = VisualQualityResult(
            action=visual_action,
            summary="Visual review complete",
        )
        stack.enter_context(patch("main.VisualQualityReviewer", return_value=visual_reviewer))
        stack.enter_context(patch.object(self.factory, "save_state"))

        return {
            "agents": agents,
            "renderer": renderer,
            "rendered_validator": rendered_validator,
            "visual_reviewer": visual_reviewer,
        }

    def _assert_image_failure(self, task, renderer):
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)
        self.assertEqual(task.final_failed_gate, "IMAGE_PREPARATION")
        self.assertEqual(task.final_failure_category, FailureCategory.IMAGE.value)
        self.assertTrue(task.final_error)
        self.assertTrue(task.failure_timestamp)
        renderer.render.assert_not_called()

    def test_ready_artifact_proceeds_to_preview_renderer(self):
        task = self._create_task()
        artifact = self._ready_artifact()
        task.image_artifact = artifact.to_dict()
        task.hero_image_id = artifact.wordpress_media_id
        task.hero_image_url = artifact.wordpress_media_url
        task.image_status = "success"

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(stack, task)
            result = self.factory.run_workflow(task.id)

        self.assertTrue(result)
        self.assertEqual(task.status, TaskStatus.LEARNING)
        mocks["renderer"].render.assert_called_once()
        self.assertIs(mocks["renderer"].render.call_args.kwargs["image_artifact"].status, ImageArtifactStatus.READY)
        mocks["agents"]["frontend"].generate_frontend.assert_called_once()

    def test_explicit_image_disabled_proceeds_without_hero_image(self):
        task = self._create_task()
        self.factory._get_agent = Mock(return_value=None)
        task.image_artifact = None
        task.hero_image_id = None
        task.image_status = None

        with ExitStack() as stack:
            self.factory._get_agent.return_value = None
            mocks = self._install_frontend_mocks(stack, task)
            stack.enter_context(patch.dict("main.config.agents", {"image": {"enabled": False}}))
            result = self.factory.run_workflow(task.id)

        self.assertTrue(result)
        self.assertEqual(task.status, TaskStatus.LEARNING)
        self.assertIsNone(task.image_artifact)
        self.assertIsNone(task.hero_image_id)
        mocks["renderer"].render.assert_called_once()
        self.assertIsNone(mocks["renderer"].render.call_args.kwargs["image_artifact"])
        mocks["agents"]["publisher"].publish_content.assert_called_once()
        self.assertIsNone(mocks["agents"]["publisher"].publish_content.call_args.kwargs["featured_media_id"])

    def test_existing_failed_artifact_stops_before_preview(self):
        task = self._create_task()
        artifact = self._failed_artifact()
        task.image_artifact = artifact.to_dict()
        task.image_status = "failed"

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(stack, task)
            result = self.factory.run_workflow(task.id)

        self.assertFalse(result)
        self._assert_image_failure(task, mocks["renderer"])
        self.assertEqual(task.final_error, "Image artifact status: failed: Previous image failure")
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(task.frontend_retry_history, [])
        mocks["visual_reviewer"].review.assert_not_called()
        mocks["agents"]["publisher"].publish_content.assert_not_called()

    def test_image_generation_exception_persists_failed_artifact_and_stops(self):
        task = self._create_task()
        image_agent = Mock()
        image_agent.generate_hero_image.side_effect = RuntimeError("image provider unavailable")

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(stack, task)
            mocks["agents"]["image"] = image_agent
            result = self.factory.run_workflow(task.id)

        self.assertFalse(result)
        self._assert_image_failure(task, mocks["renderer"])
        self.assertEqual(task.image_artifact["status"], "failed")
        self.assertIn("image provider unavailable", task.image_artifact["metadata"]["error"])
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(task.frontend_retry_history, [])
        mocks["visual_reviewer"].review.assert_not_called()
        mocks["agents"]["publisher"].publish_content.assert_not_called()

    def test_wordpress_upload_failure_after_provider_success_stops(self):
        task = self._create_task()
        provider_url = "https://provider.example.com/generated.png"
        image_agent = Mock()

        def upload_failure(task_obj):
            task_obj.image_url = provider_url
            task_obj.image_prompt = "generated prompt"
            task_obj.image_status = "failed"
            return None, None

        image_agent.generate_hero_image.side_effect = upload_failure

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(stack, task)
            mocks["agents"]["image"] = image_agent
            result = self.factory.run_workflow(task.id)

        self.assertFalse(result)
        self._assert_image_failure(task, mocks["renderer"])
        self.assertEqual(task.image_artifact["status"], "failed")
        self.assertEqual(task.image_artifact["source_url"], provider_url)
        self.assertIsNone(task.image_artifact["wordpress_media_id"])
        self.assertIsNone(task.image_artifact["wordpress_media_url"])
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(task.frontend_retry_history, [])
        mocks["visual_reviewer"].review.assert_not_called()
        mocks["agents"]["publisher"].publish_content.assert_not_called()

    def test_existing_pending_artifact_stops_without_regeneration(self):
        task = self._create_task()
        task.image_artifact = self._pending_artifact().to_dict()
        image_agent = Mock()

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(stack, task)
            mocks["agents"]["image"] = image_agent
            result = self.factory.run_workflow(task.id)

        self.assertFalse(result)
        self._assert_image_failure(task, mocks["renderer"])
        image_agent.generate_hero_image.assert_not_called()
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(task.frontend_retry_history, [])
        mocks["visual_reviewer"].review.assert_not_called()
        mocks["agents"]["publisher"].publish_content.assert_not_called()

    def test_malformed_persisted_artifact_fails_controlledly(self):
        task = self._create_task()
        task.image_artifact = {"artifact_id": "img-malformed", "status": "not-a-status"}
        image_agent = Mock()

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(stack, task)
            mocks["agents"]["image"] = image_agent
            result = self.factory.run_workflow(task.id)

        self.assertFalse(result)
        self._assert_image_failure(task, mocks["renderer"])
        self.assertEqual(task.image_artifact["status"], "failed")
        self.assertIn("Malformed persisted ImageArtifact", task.image_artifact["metadata"]["error"])
        image_agent.generate_hero_image.assert_not_called()
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(task.frontend_retry_history, [])
        mocks["visual_reviewer"].review.assert_not_called()
        mocks["agents"]["publisher"].publish_content.assert_not_called()

    def test_empty_persisted_artifact_fails_closed_without_regeneration(self):
        task = self._create_task()
        task.image_artifact = {}
        image_agent = Mock()

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(stack, task)
            mocks["agents"]["image"] = image_agent
            result = self.factory.run_workflow(task.id)

        self.assertFalse(result)
        self._assert_image_failure(task, mocks["renderer"])
        self.assertEqual(task.image_artifact["status"], "failed")
        self.assertIn("Malformed persisted ImageArtifact", task.image_artifact["metadata"]["error"])
        image_agent.generate_hero_image.assert_not_called()
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(task.frontend_retry_history, [])
        mocks["visual_reviewer"].review.assert_not_called()
        mocks["agents"]["publisher"].publish_content.assert_not_called()

    def test_incomplete_ready_artifact_fails_closed_without_regeneration(self):
        task = self._create_task()
        task.image_artifact = {"artifact_id": "x", "status": "ready"}
        image_agent = Mock()

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(stack, task)
            mocks["agents"]["image"] = image_agent
            result = self.factory.run_workflow(task.id)

        self.assertFalse(result)
        self._assert_image_failure(task, mocks["renderer"])
        self.assertEqual(task.image_artifact["artifact_id"], "x")
        self.assertEqual(task.image_artifact["status"], "failed")
        self.assertIn("READY artifact requires", task.image_artifact["metadata"]["error"])
        image_agent.generate_hero_image.assert_not_called()
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(task.frontend_retry_history, [])
        mocks["visual_reviewer"].review.assert_not_called()
        mocks["agents"]["publisher"].publish_content.assert_not_called()

    def test_none_while_image_enabled_fails_closed(self):
        task = self._create_task()
        task.image_artifact = None

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(stack, task)
            mocks["agents"]["image"] = None
            result = self.factory.run_workflow(task.id)

        self.assertFalse(result)
        self._assert_image_failure(task, mocks["renderer"])
        self.assertEqual(task.final_error, "Image generation enabled but no artifact produced")
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(task.frontend_retry_history, [])
        mocks["visual_reviewer"].review.assert_not_called()
        mocks["agents"]["publisher"].publish_content.assert_not_called()

    def test_image_failure_does_not_create_frontend_feedback(self):
        task = self._create_task()
        task.image_artifact = self._failed_artifact().to_dict()

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(stack, task)
            feedback_factory = Mock()
            stack.enter_context(patch.object(self.factory, "_create_frontend_failure_feedback", feedback_factory))
            result = self.factory.run_workflow(task.id)

        self.assertFalse(result)
        feedback_factory.assert_not_called()
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(task.frontend_retry_history, [])

    def test_image_failure_does_not_create_preview_infrastructure_failure(self):
        task = self._create_task()
        task.image_artifact = self._failed_artifact().to_dict()

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(stack, task)
            result = self.factory.run_workflow(task.id)

        self.assertFalse(result)
        mocks["renderer"].render.assert_not_called()
        self.assertEqual(task.final_failed_gate, "IMAGE_PREPARATION")
        self.assertNotEqual(task.final_failure_category, FailureCategory.INFRASTRUCTURE.value)

    def test_frontend_retry_reuses_ready_artifact_without_image_agent(self):
        task = self._create_task()
        artifact = self._ready_artifact()
        task.image_artifact = artifact.to_dict()
        task.hero_image_id = artifact.wordpress_media_id
        task.hero_image_url = artifact.wordpress_media_url
        task.image_status = "success"
        task.max_frontend_retries = 2
        image_agent = Mock()

        with ExitStack() as stack:
            mocks = self._install_frontend_mocks(
                stack,
                task,
                technical_results=[
                    self._technical_result(task.id, passed=False, attempt_number=1),
                    self._technical_result(task.id, passed=True, attempt_number=2),
                ],
            )
            mocks["agents"]["image"] = image_agent
            result = self.factory.run_workflow(task.id)

        self.assertTrue(result)
        self.assertEqual(task.frontend_retry_count, 1)
        self.assertEqual(len(task.frontend_retry_history), 1)
        self.assertEqual(mocks["renderer"].render.call_count, 2)
        first_artifact = mocks["renderer"].render.call_args_list[0].kwargs["image_artifact"]
        second_artifact = mocks["renderer"].render.call_args_list[1].kwargs["image_artifact"]
        self.assertEqual(first_artifact.artifact_id, artifact.artifact_id)
        self.assertEqual(second_artifact.artifact_id, artifact.artifact_id)
        image_agent.generate_hero_image.assert_not_called()

    def test_preview_infrastructure_retry_reuses_ready_artifact(self):
        config = Mock()
        config.preview_base_dir = "artifacts/previews"
        config.preview_browser_headless = True
        config.preview_browser_timeout_ms = 1000
        config.preview_page_load_timeout_ms = 1000
        renderer = PreviewRenderer(config)
        artifact = self._ready_artifact()
        frontend_result = self._frontend_result("task-retry")

        with ExitStack() as stack:
            render_with_browser = Mock(
                side_effect=[
                    PreviewRenderError(
                        error_type="browser_crash",
                        message="retryable crash",
                        retryable=True,
                    ),
                    (self._preview_artifact("task-retry"), self._evidence("task-retry")),
                ]
            )
            stack.enter_context(patch.object(renderer, "_render_with_browser", render_with_browser))
            preview, failure, evidence = renderer.render(
                task_id="task-retry",
                frontend_result=frontend_result,
                attempt_number=1,
                image_artifact=artifact,
            )

        self.assertIsNotNone(preview)
        self.assertIsNone(failure)
        self.assertIsNotNone(evidence)
        self.assertEqual(render_with_browser.call_count, 2)
        self.assertIs(render_with_browser.call_args_list[0].kwargs["image_artifact"], artifact)
        self.assertIs(render_with_browser.call_args_list[1].kwargs["image_artifact"], artifact)

    def _run_approval_resume(self, task):
        publisher = Mock()
        publisher.publish_content.return_value = (999, "https://wp.example.com/published")
        learner = Mock()
        learner.analyze_review.return_value = {"proposals": []}
        image_agent = Mock()

        def get_agent(name):
            return {"publisher": publisher, "learner": learner, "image": image_agent}.get(name)

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=get_agent))
            stack.enter_context(patch.object(self.factory, "save_state"))
            result = self.factory.submit_approval_decision(task.id, approved=True)

        return result, publisher, image_agent

    def _approval_task_with_artifact(self, artifact):
        task = self._create_task(ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        task.status = TaskStatus.AWAITING_APPROVAL
        task.approval_status = "pending"
        task.image_artifact = artifact
        task.final_content = "Final content"
        return task

    def test_approval_resume_reuses_valid_ready_artifact_without_image_agent(self):
        artifact = self._ready_artifact(media_id=321)
        task = self._approval_task_with_artifact(artifact.to_dict())

        result, publisher, image_agent = self._run_approval_resume(task)

        self.assertTrue(result)
        publisher.publish_content.assert_called_once_with(task, featured_media_id=321)
        image_agent.generate_hero_image.assert_not_called()

    def test_approval_resume_failed_artifact_fails_closed(self):
        task = self._approval_task_with_artifact(self._failed_artifact().to_dict())

        result, publisher, image_agent = self._run_approval_resume(task)

        self.assertFalse(result)
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)
        self.assertEqual(task.final_failed_gate, "IMAGE_PREPARATION")
        self.assertEqual(task.final_failure_category, FailureCategory.IMAGE.value)
        publisher.publish_content.assert_not_called()
        image_agent.generate_hero_image.assert_not_called()

    def test_approval_resume_pending_artifact_fails_closed(self):
        task = self._approval_task_with_artifact(self._pending_artifact().to_dict())

        result, publisher, image_agent = self._run_approval_resume(task)

        self.assertFalse(result)
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)
        self.assertEqual(task.final_error, "Image artifact status: pending")
        publisher.publish_content.assert_not_called()
        image_agent.generate_hero_image.assert_not_called()

    def test_approval_resume_empty_artifact_fails_closed_without_regeneration(self):
        task = self._approval_task_with_artifact({})

        result, publisher, image_agent = self._run_approval_resume(task)

        self.assertFalse(result)
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)
        self.assertIn("Malformed persisted ImageArtifact", task.final_error)
        self.assertTrue(task.final_feedback)
        self.assertTrue(task.failure_timestamp)
        publisher.publish_content.assert_not_called()
        image_agent.generate_hero_image.assert_not_called()

    def test_approval_resume_malformed_artifact_fails_closed_without_regeneration(self):
        task = self._approval_task_with_artifact(
            {"artifact_id": "img-malformed", "status": "not-a-status"}
        )

        result, publisher, image_agent = self._run_approval_resume(task)

        self.assertFalse(result)
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)
        self.assertIn("Malformed persisted ImageArtifact", task.final_error)
        self.assertEqual(task.frontend_retry_count, 0)
        self.assertEqual(task.frontend_retry_history, [])
        publisher.publish_content.assert_not_called()
        image_agent.generate_hero_image.assert_not_called()

    def test_approval_resume_incomplete_ready_fails_closed_without_regeneration(self):
        task = self._approval_task_with_artifact({"artifact_id": "x", "status": "ready"})

        result, publisher, image_agent = self._run_approval_resume(task)

        self.assertFalse(result)
        self.assertEqual(task.status, TaskStatus.FAILED_NEEDS_ATTENTION)
        self.assertIn("READY artifact requires", task.final_error)
        self.assertEqual(task.image_artifact["status"], "failed")
        publisher.publish_content.assert_not_called()
        image_agent.generate_hero_image.assert_not_called()

    def test_legacy_approval_resume_reuses_hero_image_id_without_image_agent(self):
        task = self._create_task(ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        task.status = TaskStatus.AWAITING_APPROVAL
        task.approval_status = "pending"
        task.image_artifact = None
        task.hero_image_id = 789
        task.hero_image_url = "https://wp.example.com/legacy.jpg"
        task.image_status = "success"
        task.final_content = "Final content"
        publisher = Mock()
        publisher.publish_content.return_value = (999, "https://wp.example.com/published")
        publisher.validate_config.return_value = True
        learner = Mock()
        learner.analyze_review.return_value = {"學習提案": []}
        image_agent = Mock()

        def get_agent(name):
            return {"publisher": publisher, "learner": learner, "image": image_agent}.get(name)

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=get_agent))
            stack.enter_context(patch.object(self.factory, "save_state"))
            result = self.factory.submit_approval_decision(task.id, approved=True)

        self.assertTrue(result)
        self.assertEqual(task.status, TaskStatus.LEARNING)
        publisher.publish_content.assert_called_once_with(task, featured_media_id=789)
        image_agent.generate_hero_image.assert_not_called()

    def test_legacy_approval_resume_without_image_publishes_without_featured_media(self):
        task = self._create_task(ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW)
        task.status = TaskStatus.AWAITING_APPROVAL
        task.approval_status = "pending"
        task.image_artifact = None
        task.hero_image_id = None
        task.hero_image_url = None
        task.image_status = None
        task.final_content = "Final content"
        publisher = Mock()
        publisher.publish_content.return_value = (999, "https://wp.example.com/published")
        publisher.validate_config.return_value = True
        learner = Mock()
        learner.analyze_review.return_value = {"學習提案": []}
        image_agent = Mock()

        def get_agent(name):
            return {"publisher": publisher, "learner": learner, "image": image_agent}.get(name)

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.factory, "_get_agent", side_effect=get_agent))
            stack.enter_context(patch.object(self.factory, "save_state"))
            result = self.factory.submit_approval_decision(task.id, approved=True)

        self.assertTrue(result)
        self.assertEqual(task.status, TaskStatus.LEARNING)
        publisher.publish_content.assert_called_once_with(task, featured_media_id=None)
        image_agent.generate_hero_image.assert_not_called()

    def test_failure_category_image_serialization_and_task_persistence_round_trip(self):
        self.assertEqual(FailureCategory.IMAGE.value, "image")
        failure = PreviewInfrastructureFailure(
            task_id="task-image-category",
            preview_id="0192f0c1-2345-7abc-8def-0123456789ab",
            attempt_number=1,
            error_type="unexpected_error",
            message="image category round trip",
            retryable=False,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            failure_category=FailureCategory.IMAGE,
        )
        restored_failure = PreviewInfrastructureFailure.from_dict(failure.to_dict())
        self.assertIs(restored_failure.failure_category, FailureCategory.IMAGE)

        task = self._create_task()
        task.final_failure_category = FailureCategory.IMAGE.value
        restored_task = Task.from_dict(task.to_dict())
        self.assertEqual(restored_task.final_failure_category, FailureCategory.IMAGE.value)


if __name__ == "__main__":
    unittest.main()
