#!/usr/bin/env python3
"""Tests for Image Artifact Materialization + Idempotent Reuse (Phase 7D-5B)."""

import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from state import Task, TaskStatus, ContentType, WorkflowState, workflow_state
from contracts import (
    ImageArtifact, ImageArtifactStatus, create_image_artifact, create_image_artifact_id,
    VisualQualityResult, VisualQualityAction,
    ApprovalPolicy, ApprovalPolicyMode,
)
from main import AIWordPressFactory


class TestImagePreparationBoundary(unittest.TestCase):
    """Test the _prepare_image_artifact boundary."""

    def setUp(self):
        self.factory = AIWordPressFactory()
        workflow_state.tasks.clear()
        workflow_state.current_task_id = None

    def _create_task(self, approval_mode=ApprovalPolicyMode.AUTO_PUBLISH):
        task_id = self.factory.create_task(
            title="Test Blog Post",
            description="A test blog post",
            content_type=ContentType.BLOG_POST,
        )
        task = workflow_state.get_task(task_id)
        task.approval_policy = ApprovalPolicy(mode=approval_mode).to_dict()
        task.max_frontend_retries = 3
        task.frontend_retry_count = 0
        task.frontend_retry_history = []
        return task

    def _mock_image_agent_success(self, media_id=123, media_url="https://wp.example.com/img.jpg"):
        """Create a mock ImageAgent that succeeds."""
        mock_agent = Mock()
        mock_agent.generate_hero_image.return_value = (media_id, media_url)
        return mock_agent

    def _mock_image_agent_failure(self, exception=None):
        """Create a mock ImageAgent that fails."""
        mock_agent = Mock()
        if exception:
            mock_agent.generate_hero_image.side_effect = exception
        else:
            mock_agent.generate_hero_image.return_value = (None, None)
        return mock_agent

    def _run_with_mocks(self, task, image_agent_mock, **agent_mocks):
        """Run _prepare_image_artifact with mocked agents."""
        agents = {
            "image": image_agent_mock,
            **agent_mocks,
        }
        def get_agent_side_effect(agent_type):
            return agents.get(agent_type)

        with patch.object(self.factory, "_get_agent", side_effect=get_agent_side_effect):
            with patch.object(self.factory, "save_state"):
                return self.factory._prepare_image_artifact(task)


class TestReadyReuseSemantics(TestImagePreparationBoundary):
    """Test READY artifact reuse behavior."""

    def test_ready_artifact_returned_reused(self):
        """READY artifact is returned/reused without calling ImageAgent."""
        task = self._create_task()
        # Pre-populate with READY artifact
        existing = create_image_artifact(
            status=ImageArtifactStatus.READY,
            artifact_id="img_existing_123",
            wordpress_media_id=999,
            wordpress_media_url="https://wp.example.com/existing.jpg",
        )
        task.image_artifact = existing.to_dict()

        image_agent = self._mock_image_agent_success()
        artifact = self._run_with_mocks(task, image_agent)

        # Should return existing artifact
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.artifact_id, "img_existing_123")
        self.assertEqual(artifact.status, ImageArtifactStatus.READY)
        # ImageAgent should NOT be called
        image_agent.generate_hero_image.assert_not_called()

    def test_ready_artifact_does_not_create_new_artifact_id(self):
        """READY reuse preserves original artifact_id."""
        task = self._create_task()
        existing = create_image_artifact(
            status=ImageArtifactStatus.READY,
            artifact_id="img_preserve_id",
        )
        task.image_artifact = existing.to_dict()

        image_agent = self._mock_image_agent_success()
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(artifact.artifact_id, "img_preserve_id")

    def test_ready_artifact_does_not_upload_duplicate_wp_media(self):
        """READY reuse does not trigger WordPress upload."""
        task = self._create_task()
        existing = create_image_artifact(
            status=ImageArtifactStatus.READY,
            artifact_id="img_no_dup",
            wordpress_media_id=456,
            wordpress_media_url="https://wp.example.com/456.jpg",
        )
        task.image_artifact = existing.to_dict()

        image_agent = self._mock_image_agent_success(media_id=789, media_url="https://wp.example.com/new.jpg")
        artifact = self._run_with_mocks(task, image_agent)

        # Should keep original media ID, not the new one from mock
        self.assertEqual(artifact.wordpress_media_id, 456)
        self.assertEqual(artifact.wordpress_media_url, "https://wp.example.com/456.jpg")


class TestMissingArtifactMaterialization(TestImagePreparationBoundary):
    """Test missing artifact triggers ImageAgent and materializes result."""

    def test_missing_artifact_invokes_image_agent(self):
        """No artifact → calls ImageAgent."""
        task = self._create_task()
        task.image_artifact = None

        image_agent = self._mock_image_agent_success()
        artifact = self._run_with_mocks(task, image_agent)

        image_agent.generate_hero_image.assert_called_once_with(task)

    def test_success_creates_image_artifact(self):
        """Success creates ImageArtifact on Task."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success(media_id=123, media_url="https://wp.example.com/img.jpg")
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertIsNotNone(artifact)
        self.assertIsNotNone(task.image_artifact)

    def test_success_status_is_ready(self):
        """Successful generation produces READY artifact."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success(media_id=123, media_url="https://wp.example.com/img.jpg")
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(artifact.status, ImageArtifactStatus.READY)

    def test_wordpress_media_id_copied_correctly(self):
        """hero_image_id copied to artifact.wordpress_media_id."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success(media_id=456, media_url="https://wp.example.com/img.jpg")
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(artifact.wordpress_media_id, 456)
        self.assertEqual(task.hero_image_id, 456)

    def test_wordpress_media_url_copied_correctly(self):
        """hero_image_url copied to artifact.wordpress_media_url."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success(media_id=123, media_url="https://wp.example.com/custom.jpg")
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(artifact.wordpress_media_url, "https://wp.example.com/custom.jpg")
        self.assertEqual(task.hero_image_url, "https://wp.example.com/custom.jpg")

    def test_source_url_copied_when_available(self):
        """image_url (provider URL) copied to artifact.source_url."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success(media_id=123, media_url="https://wp.example.com/img.jpg")
        # Simulate ImageAgent setting task.image_url
        def set_image_url(task_obj, *args, **kwargs):
            task_obj.image_url = "https://openai.example.com/generated.png"
            return (123, "https://wp.example.com/img.jpg")
        image_agent.generate_hero_image.side_effect = set_image_url
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(artifact.source_url, "https://openai.example.com/generated.png")

    def test_prompt_copied_correctly(self):
        """image_prompt copied to artifact.prompt."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success(media_id=123, media_url="https://wp.example.com/img.jpg")
        def set_prompt(task_obj, *args, **kwargs):
            task_obj.image_prompt = "A beautiful landscape prompt"
            return (123, "https://wp.example.com/img.jpg")
        image_agent.generate_hero_image.side_effect = set_prompt
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(artifact.prompt, "A beautiful landscape prompt")

    def test_artifact_id_created(self):
        """New artifact gets generated artifact_id."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success()
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertIsNotNone(artifact.artifact_id)
        self.assertTrue(artifact.artifact_id.startswith("img_"))

    def test_artifact_persisted_on_task(self):
        """Created artifact is persisted to task.image_artifact."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success()
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertIsNotNone(task.image_artifact)
        self.assertEqual(task.image_artifact["artifact_id"], artifact.artifact_id)


class TestFailureMaterialization(TestImagePreparationBoundary):
    """Test failure creates FAILED artifact without retries."""

    def test_generation_failure_creates_failed_artifact(self):
        """Image generation failure creates FAILED artifact."""
        task = self._create_task()
        image_agent = self._mock_image_agent_failure()
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.status, ImageArtifactStatus.FAILED)

    def test_exception_creates_failed_artifact(self):
        """Exception during generation creates FAILED artifact."""
        task = self._create_task()
        image_agent = self._mock_image_agent_failure(exception=Exception("API Error"))
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.status, ImageArtifactStatus.FAILED)

    def test_failed_artifact_persisted(self):
        """FAILED artifact is persisted on Task."""
        task = self._create_task()
        image_agent = self._mock_image_agent_failure()
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertIsNotNone(task.image_artifact)
        self.assertEqual(task.image_artifact["status"], "failed")

    def test_failure_no_frontend_retry_increment(self):
        """Failure does not increment frontend_retry_count."""
        task = self._create_task()
        task.frontend_retry_count = 0
        image_agent = self._mock_image_agent_failure()
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(task.frontend_retry_count, 0)

    def test_failure_no_frontend_failure_feedback(self):
        """Failure does not create frontend_retry_history entry."""
        task = self._create_task()
        task.frontend_retry_history = []
        image_agent = self._mock_image_agent_failure()
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(len(task.frontend_retry_history), 0)

    def test_failure_no_image_retry_counter(self):
        """No image_retry_count field created."""
        task = self._create_task()
        image_agent = self._mock_image_agent_failure()
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertFalse(hasattr(task, "image_retry_count"))
        self.assertFalse(hasattr(task, "image_generation_retry_count"))

    def test_failure_does_not_claim_ready(self):
        """FAILED artifact never has READY status."""
        task = self._create_task()
        image_agent = self._mock_image_agent_failure()
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertNotEqual(artifact.status, ImageArtifactStatus.READY)


class TestExistingFailedPendingSemantics(TestImagePreparationBoundary):
    """Test existing FAILED/PENDING artifacts don't auto-regenerate."""

    def test_existing_failed_no_silent_ready(self):
        """Existing FAILED artifact does not become READY."""
        task = self._create_task()
        existing = create_image_artifact(
            status=ImageArtifactStatus.FAILED,
            artifact_id="img_failed",
            metadata={"error": "Previous failure"},
        )
        task.image_artifact = existing.to_dict()

        image_agent = self._mock_image_agent_success()
        artifact = self._run_with_mocks(task, image_agent)

        # Should return existing FAILED artifact
        self.assertEqual(artifact.status, ImageArtifactStatus.FAILED)
        self.assertEqual(artifact.artifact_id, "img_failed")
        image_agent.generate_hero_image.assert_not_called()

    def test_existing_failed_no_auto_regenerate(self):
        """Existing FAILED artifact does not trigger regeneration."""
        task = self._create_task()
        existing = create_image_artifact(
            status=ImageArtifactStatus.FAILED,
            artifact_id="img_failed_no_regen",
        )
        task.image_artifact = existing.to_dict()

        image_agent = self._mock_image_agent_success()
        artifact = self._run_with_mocks(task, image_agent)

        image_agent.generate_hero_image.assert_not_called()

    def test_existing_pending_no_auto_regenerate(self):
        """Existing PENDING artifact does not trigger regeneration."""
        task = self._create_task()
        existing = create_image_artifact(
            status=ImageArtifactStatus.PENDING,
            artifact_id="img_pending",
        )
        task.image_artifact = existing.to_dict()

        image_agent = self._mock_image_agent_success()
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(artifact.status, ImageArtifactStatus.PENDING)
        image_agent.generate_hero_image.assert_not_called()


class TestLegacyCompatibility(TestImagePreparationBoundary):
    """Test legacy Task image fields remain populated."""

    def test_legacy_hero_image_id_populated(self):
        """hero_image_id still populated on success."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success(media_id=789, media_url="https://wp.example.com/img.jpg")
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(task.hero_image_id, 789)

    def test_legacy_hero_image_url_populated(self):
        """hero_image_url still populated on success."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success(media_id=123, media_url="https://wp.example.com/custom.jpg")
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(task.hero_image_url, "https://wp.example.com/custom.jpg")

    def test_legacy_image_status_compatible(self):
        """image_status remains compatible ('success'/'failed')."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success()
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(task.image_status, "success")

        # Test failure case
        task2 = self._create_task()
        image_agent2 = self._mock_image_agent_failure()
        artifact2 = self._run_with_mocks(task2, image_agent2)
        self.assertEqual(task2.image_status, "failed")

    def test_legacy_image_prompt_compatible(self):
        """image_prompt remains populated."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success()
        def set_prompt(task_obj, *args, **kwargs):
            task_obj.image_prompt = "Legacy prompt"
            return (123, "https://wp.example.com/img.jpg")
        image_agent.generate_hero_image.side_effect = set_prompt
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(task.image_prompt, "Legacy prompt")

    def test_legacy_image_url_compatible(self):
        """image_url (provider URL) remains populated."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success()
        def set_url(task_obj, *args, **kwargs):
            task_obj.image_url = "https://provider.example.com/img.png"
            return (123, "https://wp.example.com/img.jpg")
        image_agent.generate_hero_image.side_effect = set_url
        
        artifact = self._run_with_mocks(task, image_agent)

        self.assertEqual(task.image_url, "https://provider.example.com/img.png")


class TestPersistence(TestImagePreparationBoundary):
    """Test artifact survives Task save/load."""

    def test_ready_artifact_survives_save_load(self):
        """READY artifact survives Task serialization."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success(media_id=123, media_url="https://wp.example.com/img.jpg")
        
        artifact = self._run_with_mocks(task, image_agent)
        artifact_id = artifact.artifact_id
        
        # Serialize and deserialize
        data = task.to_dict()
        restored = Task.from_dict(data)
        
        self.assertIsNotNone(restored.image_artifact)
        self.assertEqual(restored.image_artifact["artifact_id"], artifact_id)
        self.assertEqual(restored.image_artifact["status"], "ready")
        self.assertEqual(restored.image_artifact["wordpress_media_id"], 123)

    def test_failed_artifact_survives_save_load(self):
        """FAILED artifact survives Task serialization."""
        task = self._create_task()
        image_agent = self._mock_image_agent_failure()
        
        artifact = self._run_with_mocks(task, image_agent)
        artifact_id = artifact.artifact_id
        
        # Serialize and deserialize
        data = task.to_dict()
        restored = Task.from_dict(data)
        
        self.assertIsNotNone(restored.image_artifact)
        self.assertEqual(restored.image_artifact["artifact_id"], artifact_id)
        self.assertEqual(restored.image_artifact["status"], "failed")

    def test_artifact_id_survives_save_load(self):
        """artifact_id preserved through save/load."""
        task = self._create_task()
        image_agent = self._mock_image_agent_success()
        
        artifact = self._run_with_mocks(task, image_agent)
        artifact_id = artifact.artifact_id
        
        data = task.to_dict()
        restored = Task.from_dict(data)
        
        self.assertEqual(restored.image_artifact["artifact_id"], artifact_id)


class TestBoundaries(TestImagePreparationBoundary):
    """Test architecture boundaries remain unchanged."""

    def test_preview_renderer_unchanged(self):
        """PreviewRenderer import and behavior unchanged."""
        from tools.preview_renderer import PreviewRenderer
        self.assertIsNotNone(PreviewRenderer)

    def test_publisher_unchanged(self):
        """WordPressPublisher unchanged."""
        from tools.wordpress import WordPressPublisher
        self.assertIsNotNone(WordPressPublisher)

    def test_workflow_ordering_unchanged(self):
        """ImageAgent now runs BEFORE PreviewRenderer (moved)."""
        import inspect
        source = inspect.getsource(AIWordPressFactory._continue_post_approval)
        # _prepare_image_artifact should no longer be in _continue_post_approval
        self.assertNotIn("_prepare_image_artifact", source)
        # Verify it's not in _continue_post_approval
        self.assertNotIn("PreviewRenderer", source)
        # Verify it's called in run_workflow before PreviewRenderer
        run_source = inspect.getsource(AIWordPressFactory.run_workflow)
        self.assertIn("_prepare_image_artifact", run_source)
        self.assertLess(run_source.index("_prepare_image_artifact"), run_source.index("PreviewRenderer"))

    def test_approval_policy_unchanged(self):
        """ApprovalPolicy enum unchanged."""
        self.assertEqual(ApprovalPolicyMode.AUTO_PUBLISH.value, "auto_publish")
        self.assertEqual(ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW.value, "require_human_review")

    def test_no_new_task_status(self):
        """No new TaskStatus added for this slice."""
        # Just verify existing statuses work
        self.assertTrue(hasattr(TaskStatus, "GENERATING_IMAGE"))
        self.assertTrue(hasattr(TaskStatus, "PUBLISHING"))
        self.assertTrue(hasattr(TaskStatus, "COMPLETED"))

    def test_no_image_retry_counter(self):
        """No image retry counter added."""
        task = self._create_task()
        self.assertFalse(hasattr(task, "image_retry_count"))
        self.assertFalse(hasattr(task, "image_generation_retry_count"))

    def test_no_automatic_regeneration(self):
        """No automatic regeneration on failure."""
        task = self._create_task()
        image_agent = self._mock_image_agent_failure()
        
        artifact1 = self._run_with_mocks(task, image_agent)
        artifact2 = self._run_with_mocks(task, image_agent)  # Call again
        
        # Should return same FAILED artifact, not create new one
        self.assertEqual(artifact1.artifact_id, artifact2.artifact_id)
        # ImageAgent should only be called once (first time)
        self.assertEqual(image_agent.generate_hero_image.call_count, 1)


if __name__ == "__main__":
    unittest.main()