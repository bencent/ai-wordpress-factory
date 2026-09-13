#!/usr/bin/env python3
"""Tests for ImageArtifact contract and Task persistence (Phase 7D-5A)."""

import unittest
from datetime import datetime
from contracts import ImageArtifact, ImageArtifactStatus, create_image_artifact, create_image_artifact_id
from state import Task, TaskStatus, ContentType, WorkflowState, workflow_state


class TestImageArtifactStatus(unittest.TestCase):
    """Test ImageArtifactStatus enum values."""

    def test_exact_values(self):
        """ImageArtifactStatus has exactly PENDING, READY, FAILED."""
        self.assertEqual(ImageArtifactStatus.PENDING.value, "pending")
        self.assertEqual(ImageArtifactStatus.READY.value, "ready")
        self.assertEqual(ImageArtifactStatus.FAILED.value, "failed")

    def test_no_extra_retry_statuses(self):
        """No retry-related statuses exist."""
        enum_values = {e.value for e in ImageArtifactStatus}
        forbidden = {"generating", "uploading", "retrying", "partial", "approved", "published"}
        for f in forbidden:
            self.assertNotIn(f, enum_values, f"Forbidden status '{f}' found in ImageArtifactStatus")


class TestImageArtifactConstruction(unittest.TestCase):
    """Test ImageArtifact construction and defaults."""

    def test_minimal_construction(self):
        """Minimal ImageArtifact requires only artifact_id."""
        artifact = ImageArtifact(artifact_id="img_123")
        self.assertEqual(artifact.artifact_id, "img_123")
        self.assertEqual(artifact.status, ImageArtifactStatus.PENDING)

    def test_ready_artifact_construction(self):
        """Construct READY artifact with all fields."""
        artifact = ImageArtifact(
            artifact_id="img_456",
            status=ImageArtifactStatus.READY,
            provider="openai",
            model="dall-e-3",
            source_url="https://example.com/img.png",
            local_path="/tmp/img.png",
            wordpress_media_id=123,
            wordpress_media_url="https://wp.example.com/wp-content/uploads/img.png",
            prompt="A beautiful landscape",
            alt_text="Landscape",
            width=1792,
            height=1024,
            content_type="image/png",
            metadata={"key": "value"},
        )
        self.assertEqual(artifact.status, ImageArtifactStatus.READY)
        self.assertEqual(artifact.provider, "openai")
        self.assertEqual(artifact.wordpress_media_id, 123)
        self.assertEqual(artifact.metadata, {"key": "value"})

    def test_failed_artifact_construction(self):
        """Construct FAILED artifact."""
        artifact = ImageArtifact(
            artifact_id="img_789",
            status=ImageArtifactStatus.FAILED,
        )
        self.assertEqual(artifact.status, ImageArtifactStatus.FAILED)

    def test_optional_fields_default_correctly(self):
        """Optional fields default to None or empty dict."""
        artifact = ImageArtifact(artifact_id="img_test")
        self.assertIsNone(artifact.provider)
        self.assertIsNone(artifact.model)
        self.assertIsNone(artifact.source_url)
        self.assertIsNone(artifact.local_path)
        self.assertIsNone(artifact.wordpress_media_id)
        self.assertIsNone(artifact.wordpress_media_url)
        self.assertIsNone(artifact.prompt)
        self.assertIsNone(artifact.alt_text)
        self.assertIsNone(artifact.width)
        self.assertIsNone(artifact.height)
        self.assertIsNone(artifact.content_type)
        self.assertEqual(artifact.metadata, {})

    def test_metadata_defaults_to_empty_dict(self):
        """metadata defaults to empty dict, not shared mutable."""
        a1 = ImageArtifact(artifact_id="a1")
        a2 = ImageArtifact(artifact_id="a2")
        a1.metadata["test"] = "value"
        self.assertEqual(a2.metadata, {})

    def test_created_at_auto_generated(self):
        """created_at auto-generates ISO timestamp if not provided."""
        before = datetime.now().isoformat()
        artifact = ImageArtifact(artifact_id="img_time")
        after = datetime.now().isoformat()
        self.assertTrue(before <= artifact.created_at <= after)


class TestImageArtifactIdentity(unittest.TestCase):
    """Test artifact_id behavior."""

    def test_artifact_id_preserved(self):
        """artifact_id is preserved in to_dict/from_dict."""
        artifact = ImageArtifact(artifact_id="img_preserve")
        data = artifact.to_dict()
        restored = ImageArtifact.from_dict(data)
        self.assertEqual(restored.artifact_id, "img_preserve")

    def test_two_artifacts_different_ids(self):
        """Two generated artifacts can have different IDs."""
        a1 = ImageArtifact(artifact_id="img_1")
        a2 = ImageArtifact(artifact_id="img_2")
        self.assertNotEqual(a1.artifact_id, a2.artifact_id)

    def test_artifact_id_not_derived_from_wp_media_id(self):
        """artifact_id is independent of WordPress media ID."""
        artifact = ImageArtifact(
            artifact_id="custom_id_123",
            wordpress_media_id=999,
        )
        self.assertEqual(artifact.artifact_id, "custom_id_123")
        self.assertEqual(artifact.wordpress_media_id, 999)

    def test_factory_generates_unique_ids(self):
        """create_image_artifact generates unique artifact_ids."""
        a1 = create_image_artifact()
        a2 = create_image_artifact()
        self.assertNotEqual(a1.artifact_id, a2.artifact_id)
        self.assertTrue(a1.artifact_id.startswith("img_"))
        self.assertTrue(a2.artifact_id.startswith("img_"))

    def test_factory_allows_custom_id(self):
        """create_image_artifact respects custom artifact_id."""
        artifact = create_image_artifact(artifact_id="custom_123")
        self.assertEqual(artifact.artifact_id, "custom_123")

    def test_factory_sets_defaults(self):
        """create_image_artifact sets default status to PENDING."""
        artifact = create_image_artifact(provider="openai")
        self.assertEqual(artifact.status, ImageArtifactStatus.PENDING)
        self.assertEqual(artifact.provider, "openai")


class TestImageArtifactSerialization(unittest.TestCase):
    """Test ImageArtifact serialization round-trip."""

    def test_to_dict(self):
        """to_dict produces expected structure."""
        artifact = ImageArtifact(
            artifact_id="img_serialize",
            status=ImageArtifactStatus.READY,
            provider="openai",
            model="dall-e-3",
            source_url="https://example.com/img.png",
            local_path="/tmp/img.png",
            wordpress_media_id=123,
            wordpress_media_url="https://wp.example.com/img.png",
            prompt="Test prompt",
            alt_text="Test alt",
            width=100,
            height=100,
            content_type="image/png",
            metadata={"custom": "data"},
            created_at="2024-01-01T00:00:00",
        )
        data = artifact.to_dict()
        self.assertEqual(data["artifact_id"], "img_serialize")
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["provider"], "openai")
        self.assertEqual(data["wordpress_media_id"], 123)
        self.assertEqual(data["metadata"], {"custom": "data"})
        self.assertEqual(data["created_at"], "2024-01-01T00:00:00")

    def test_from_dict(self):
        """from_dict reconstructs artifact correctly."""
        data = {
            "artifact_id": "img_from_dict",
            "status": "ready",
            "provider": "openai",
            "model": "dall-e-3",
            "source_url": "https://example.com/img.png",
            "local_path": "/tmp/img.png",
            "wordpress_media_id": 123,
            "wordpress_media_url": "https://wp.example.com/img.png",
            "prompt": "Test prompt",
            "alt_text": "Test alt",
            "width": 100,
            "height": 100,
            "content_type": "image/png",
            "metadata": {"custom": "data"},
            "created_at": "2024-01-01T00:00:00",
        }
        artifact = ImageArtifact.from_dict(data)
        self.assertEqual(artifact.artifact_id, "img_from_dict")
        self.assertEqual(artifact.status, ImageArtifactStatus.READY)
        self.assertEqual(artifact.provider, "openai")
        self.assertEqual(artifact.metadata, {"custom": "data"})
        self.assertEqual(artifact.created_at, "2024-01-01T00:00:00")

    def test_full_round_trip(self):
        """Full to_dict -> from_dict round trip preserves all fields."""
        original = ImageArtifact(
            artifact_id="img_roundtrip",
            status=ImageArtifactStatus.READY,
            provider="openai",
            model="dall-e-3",
            source_url="https://example.com/img.png",
            local_path="/tmp/img.png",
            wordpress_media_id=123,
            wordpress_media_url="https://wp.example.com/img.png",
            prompt="Test prompt",
            alt_text="Test alt",
            width=100,
            height=100,
            content_type="image/png",
            metadata={"custom": "data"},
            created_at="2024-01-01T00:00:00",
        )
        data = original.to_dict()
        restored = ImageArtifact.from_dict(data)
        self.assertEqual(restored.artifact_id, original.artifact_id)
        self.assertEqual(restored.status, original.status)
        self.assertEqual(restored.provider, original.provider)
        self.assertEqual(restored.model, original.model)
        self.assertEqual(restored.source_url, original.source_url)
        self.assertEqual(restored.local_path, original.local_path)
        self.assertEqual(restored.wordpress_media_id, original.wordpress_media_id)
        self.assertEqual(restored.wordpress_media_url, original.wordpress_media_url)
        self.assertEqual(restored.prompt, original.prompt)
        self.assertEqual(restored.alt_text, original.alt_text)
        self.assertEqual(restored.width, original.width)
        self.assertEqual(restored.height, original.height)
        self.assertEqual(restored.content_type, original.content_type)
        self.assertEqual(restored.metadata, original.metadata)
        self.assertEqual(restored.created_at, original.created_at)

    def test_enum_preserved_in_round_trip(self):
        """Enum value preserved through round trip."""
        for status in ImageArtifactStatus:
            artifact = ImageArtifact(artifact_id="img_enum", status=status)
            restored = ImageArtifact.from_dict(artifact.to_dict())
            self.assertEqual(restored.status, status)

    def test_metadata_preserved_in_round_trip(self):
        """Complex metadata preserved through round trip."""
        artifact = ImageArtifact(
            artifact_id="img_meta",
            metadata={"nested": {"key": "value"}, "list": [1, 2, 3]},
        )
        restored = ImageArtifact.from_dict(artifact.to_dict())
        self.assertEqual(restored.metadata, {"nested": {"key": "value"}, "list": [1, 2, 3]})

    def test_created_at_preserved_in_round_trip(self):
        """created_at preserved through round trip."""
        artifact = ImageArtifact(
            artifact_id="img_time_rt",
            created_at="2024-06-15T12:30:45",
        )
        restored = ImageArtifact.from_dict(artifact.to_dict())
        self.assertEqual(restored.created_at, "2024-06-15T12:30:45")


class TestTaskImageArtifactPersistence(unittest.TestCase):
    """Test Task stores and serializes image_artifact."""

    def setUp(self):
        workflow_state.tasks.clear()
        workflow_state.current_task_id = None

    def test_task_can_store_image_artifact(self):
        """Task can store image_artifact dict."""
        task = Task(
            id="task_1",
            title="Test",
            content_type=ContentType.BLOG_POST,
        )
        artifact = ImageArtifact(
            artifact_id="img_task",
            status=ImageArtifactStatus.READY,
            provider="openai",
        )
        task.image_artifact = artifact.to_dict()
        self.assertIsNotNone(task.image_artifact)
        self.assertEqual(task.image_artifact["artifact_id"], "img_task")

    def test_task_serialization_preserves_image_artifact(self):
        """Task.to_dict includes image_artifact."""
        task = Task(
            id="task_2",
            title="Test",
            content_type=ContentType.BLOG_POST,
        )
        artifact = ImageArtifact(artifact_id="img_ser", status=ImageArtifactStatus.READY)
        task.image_artifact = artifact.to_dict()
        data = task.to_dict()
        self.assertIn("image_artifact", data)
        self.assertEqual(data["image_artifact"]["artifact_id"], "img_ser")

    def test_task_deserialization_restores_image_artifact(self):
        """Task.from_dict restores image_artifact."""
        task = Task(
            id="task_3",
            title="Test",
            content_type=ContentType.BLOG_POST,
        )
        artifact = ImageArtifact(artifact_id="img_deser", status=ImageArtifactStatus.READY)
        task.image_artifact = artifact.to_dict()
        data = task.to_dict()
        restored = Task.from_dict(data)
        self.assertIsNotNone(restored.image_artifact)
        self.assertEqual(restored.image_artifact["artifact_id"], "img_deser")

    def test_old_task_without_image_artifact_loads_none(self):
        """Old serialized Task without image_artifact loads as None."""
        # Simulate old state without image_artifact field
        old_data = {
            "id": "task_old",
            "title": "Old Task",
            "content_type": "BLOG_POST",
            "status": "PENDING",
            "priority": 0,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            # No image_artifact field
        }
        restored = Task.from_dict(old_data)
        self.assertIsNone(restored.image_artifact)


class TestLegacyImageFieldsCompatibility(unittest.TestCase):
    """Test legacy Task image fields remain unchanged."""

    def setUp(self):
        workflow_state.tasks.clear()
        workflow_state.current_task_id = None

    def test_image_prompt_remains(self):
        """image_prompt field still exists on Task."""
        task = Task(id="t1", title="T", content_type=ContentType.BLOG_POST)
        task.image_prompt = "test prompt"
        self.assertEqual(task.image_prompt, "test prompt")

    def test_image_url_remains(self):
        """image_url field still exists on Task."""
        task = Task(id="t2", title="T", content_type=ContentType.BLOG_POST)
        task.image_url = "https://example.com/img.png"
        self.assertEqual(task.image_url, "https://example.com/img.png")

    def test_hero_image_id_remains(self):
        """hero_image_id field still exists on Task."""
        task = Task(id="t3", title="T", content_type=ContentType.BLOG_POST)
        task.hero_image_id = 123
        self.assertEqual(task.hero_image_id, 123)

    def test_hero_image_url_remains(self):
        """hero_image_url field still exists on Task."""
        task = Task(id="t4", title="T", content_type=ContentType.BLOG_POST)
        task.hero_image_url = "https://wp.example.com/img.png"
        self.assertEqual(task.hero_image_url, "https://wp.example.com/img.png")

    def test_image_status_remains(self):
        """image_status field still exists on Task."""
        task = Task(id="t5", title="T", content_type=ContentType.BLOG_POST)
        task.image_status = "success"
        self.assertEqual(task.image_status, "success")

    def test_legacy_fields_serialized(self):
        """Legacy fields still serialize in to_dict."""
        task = Task(id="t6", title="T", content_type=ContentType.BLOG_POST)
        task.image_prompt = "prompt"
        task.image_url = "url"
        task.hero_image_id = 1
        task.hero_image_url = "wp_url"
        task.image_status = "ready"
        data = task.to_dict()
        self.assertEqual(data["image_prompt"], "prompt")
        self.assertEqual(data["image_url"], "url")
        self.assertEqual(data["hero_image_id"], 1)
        self.assertEqual(data["hero_image_url"], "wp_url")
        self.assertEqual(data["image_status"], "ready")

    def test_legacy_fields_deserialized(self):
        """Legacy fields still deserialize from from_dict."""
        data = {
            "id": "t7",
            "title": "T",
            "content_type": "BLOG_POST",
            "status": "PENDING",
            "priority": 0,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
            "image_prompt": "prompt",
            "image_url": "url",
            "hero_image_id": 1,
            "hero_image_url": "wp_url",
            "image_status": "ready",
        }
        task = Task.from_dict(data)
        self.assertEqual(task.image_prompt, "prompt")
        self.assertEqual(task.image_url, "url")
        self.assertEqual(task.hero_image_id, 1)
        self.assertEqual(task.hero_image_url, "wp_url")
        self.assertEqual(task.image_status, "ready")


class TestImageArtifactSecurityDurability(unittest.TestCase):
    """Test ImageArtifact does not contain prohibited fields."""

    def test_no_image_bytes_field(self):
        """ImageArtifact has no image_bytes field."""
        artifact = ImageArtifact(artifact_id="img_sec")
        self.assertFalse(hasattr(artifact, "image_bytes"))

    def test_no_base64_field(self):
        """ImageArtifact has no base64 field."""
        artifact = ImageArtifact(artifact_id="img_sec")
        self.assertFalse(hasattr(artifact, "base64"))

    def test_no_api_key_field(self):
        """ImageArtifact has no api_key field."""
        artifact = ImageArtifact(artifact_id="img_sec")
        self.assertFalse(hasattr(artifact, "api_key"))

    def test_no_raw_provider_response_field(self):
        """ImageArtifact has no raw provider response field."""
        artifact = ImageArtifact(artifact_id="img_sec")
        self.assertFalse(hasattr(artifact, "raw_response"))
        self.assertFalse(hasattr(artifact, "provider_response"))


class TestArchitectureBoundaries(unittest.TestCase):
    """Test architecture boundaries remain unchanged in this slice."""

    def test_image_agent_not_moved(self):
        """ImageAgent import location unchanged."""
        from agents.image import ImageAgent
        # Just verify import works
        self.assertIsNotNone(ImageAgent)

    def test_preview_renderer_unchanged(self):
        """PreviewRenderer import location unchanged."""
        from tools.preview_renderer import PreviewRenderer
        self.assertIsNotNone(PreviewRenderer)

    def test_publisher_unchanged(self):
        """WordPressPublisher import location unchanged."""
        from tools.wordpress import WordPressPublisher
        self.assertIsNotNone(WordPressPublisher)

    def test_workflow_ordering_unchanged(self):
        """Main workflow structure imports work."""
        from main import AIWordPressFactory
        self.assertIsNotNone(AIWordPressFactory)

    def test_no_image_retry_counter_added(self):
        """No image retry counter added to Task."""
        task = Task(id="t", title="T", content_type=ContentType.BLOG_POST)
        self.assertFalse(hasattr(task, "image_retry_count"))
        self.assertFalse(hasattr(task, "image_generation_retry_count"))

    def test_approval_policy_unchanged(self):
        """ApprovalPolicy enum unchanged."""
        from contracts import ApprovalPolicyMode
        self.assertEqual(ApprovalPolicyMode.AUTO_PUBLISH.value, "auto_publish")
        self.assertEqual(ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW.value, "require_human_review")


if __name__ == "__main__":
    unittest.main()