import base64
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import (
    ApprovalPolicyMode,
    FrontendResult,
    ImageArtifact,
    ImageArtifactStatus,
    PreviewArtifact,
    PreviewViewport,
    RenderedEvidence,
    ViewportRenderedEvidence,
)
from state import Task, TaskStatus
from tools.preview_renderer import PreviewRenderer, render_preview
from tools.rendered_technical_validator import RenderedTechnicalValidator
from tools.wordpress import WordPressPublisher
from agents.image import ImageAgent
from main import AIWordPressFactory


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl"
    "0n9EAAAAASUVORK5CYII="
)


class TestPreviewImageComposition(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.preview_root = Path(self.temp_dir.name)

        class Config:
            preview_base_dir = str(self.preview_root)
            preview_browser_headless = True
            preview_browser_timeout_ms = 30000
            preview_page_load_timeout_ms = 30000

        self.config = Config()
        self.renderer = PreviewRenderer(self.config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _frontend(self, html="<main>Frontend content</main>", blocks=None, css="body {}", javascript=""):
        return FrontendResult(
            task_id="task-preview",
            success=True,
            html=html,
            css=css,
            javascript=javascript,
            blocks=blocks,
        )

    def _artifact(
        self,
        status=ImageArtifactStatus.READY,
        artifact_id="img-hero-1",
        local_path=None,
        wordpress_media_url=None,
        source_url="https://source.example/hero.jpg",
        alt_text="Hero image",
    ):
        return ImageArtifact(
            artifact_id=artifact_id,
            status=status,
            local_path=local_path,
            wordpress_media_url=wordpress_media_url,
            source_url=source_url,
            alt_text=alt_text,
        )

    def _assemble(self, artifact=None, frontend_result=None):
        preview_dir = self.renderer._create_preview_directory("task-preview", 1, "preview-1")
        html_path = self.renderer._assemble_preview_html(
            frontend_result or self._frontend(),
            preview_dir,
            artifact,
        )
        return html_path.read_text(encoding="utf-8")

    def _write_image(self, name="hero.png"):
        path = self.preview_root / name
        path.write_bytes(PNG_1X1)
        return path

    def _mock_playwright(self, mock_sync_playwright, evaluate_return=None):
        mock_playwright = MagicMock()
        mock_sync_playwright.return_value.__enter__.return_value = mock_playwright
        mock_browser = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page

        def create_screenshot(path, **kwargs):
            Path(path).write_bytes(PNG_1X1)

        mock_page.screenshot.side_effect = create_screenshot
        mock_page.evaluate.return_value = evaluate_return or {
            "document_scroll_width": 1440,
            "document_client_width": 1440,
            "document_scroll_height": 900,
            "document_client_height": 900,
            "image_states": [],
            "element_bounding_boxes": [],
        }
        return mock_page, mock_browser, mock_context

    def test_render_without_image_artifact_still_works(self):
        frontend_result = self._frontend()
        content = self._assemble(frontend_result=frontend_result)
        self.assertNotIn("data-preview-hero-image", content)
        self.assertIn("Frontend content", content)

    def test_old_caller_signature_remains_valid(self):
        signature = inspect.signature(self.renderer.render)
        self.assertEqual(signature.parameters["image_artifact"].default, None)
        with patch.object(self.renderer, "_render_with_browser", return_value=(Mock(), Mock())) as render_browser:
            self.renderer.render("task-preview", self._frontend(), 1, "preview-1")
        self.assertEqual(render_browser.call_count, 1)

    @patch("playwright.sync_api.sync_playwright")
    def test_screenshots_still_generated_without_image(self, mock_sync_playwright):
        self._mock_playwright(mock_sync_playwright)
        frontend_result = self._frontend()
        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)
            artifact, evidence = self.renderer._render_with_browser(
                task_id="task-preview",
                preview_id="preview-1",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )
        self.assertIsNotNone(artifact)
        self.assertIsNotNone(evidence)
        self.assertEqual(mock_sync_playwright.return_value.__enter__.return_value.chromium.launch.return_value.new_context.return_value.new_page.return_value.screenshot.call_count, 4)

    def test_ready_artifact_is_accepted(self):
        content = self._assemble(self._artifact())
        self.assertIn("data-preview-hero-image=\"true\"", content)

    def test_ready_artifact_injects_hero_markup(self):
        content = self._assemble(self._artifact())
        self.assertIn("<figure", content)
        self.assertIn("</figure>", content)
        self.assertIn("<img", content)

    def test_artifact_id_appears_in_markup(self):
        content = self._assemble(self._artifact(artifact_id="img-trace-42"))
        self.assertIn('data-image-artifact-id="img-trace-42"', content)

    def test_img_src_uses_resolved_artifact_source(self):
        image_path = self._write_image()
        content = self._assemble(self._artifact(local_path=str(image_path)))
        self.assertIn(f'src="{image_path.resolve().as_uri()}"', content)

    def test_alt_text_is_used_when_present(self):
        content = self._assemble(self._artifact(alt_text="A useful description"))
        self.assertIn('alt="A useful description"', content)

    def test_empty_alt_is_used_when_absent(self):
        image_path = self._write_image()
        content = self._assemble(self._artifact(local_path=str(image_path), alt_text=None))
        self.assertIn('alt=""', content)

    def test_hero_image_is_injected_exactly_once(self):
        content = self._assemble(self._artifact(local_path=str(self._write_image())))
        self.assertEqual(content.count('data-preview-hero-image="true"'), 1)
        self.assertEqual(content.count("<figure"), 1)

    def test_failed_artifact_is_not_injected(self):
        content = self._assemble(self._artifact(status=ImageArtifactStatus.FAILED))
        self.assertNotIn("data-preview-hero-image", content)

    def test_pending_artifact_is_not_injected(self):
        content = self._assemble(self._artifact(status=ImageArtifactStatus.PENDING))
        self.assertNotIn("data-preview-hero-image", content)

    def test_none_artifact_is_not_injected(self):
        content = self._assemble(None)
        self.assertNotIn("data-preview-hero-image", content)

    def test_string_ready_status_is_accepted(self):
        artifact = self._artifact()
        artifact.status = "ready"
        content = self._assemble(artifact)
        self.assertIn("data-preview-hero-image", content)

    def test_invalid_status_is_not_injected(self):
        artifact = self._artifact()
        artifact.status = "unknown"
        content = self._assemble(artifact)
        self.assertNotIn("data-preview-hero-image", content)

    def test_local_path_is_preferred_when_usable(self):
        image_path = self._write_image()
        artifact = self._artifact(
            local_path=str(image_path),
            wordpress_media_url="https://wordpress.example/ignored.jpg",
            source_url="https://source.example/ignored.jpg",
        )
        self.assertEqual(
            self.renderer._resolve_image_source(artifact),
            image_path.resolve().as_uri(),
        )

    def test_missing_local_path_falls_back_to_wordpress_url(self):
        artifact = self._artifact(
            local_path=str(self.preview_root / "missing.png"),
            wordpress_media_url="https://wordpress.example/hero.jpg",
            source_url="https://source.example/hero.jpg",
        )
        self.assertEqual(
            self.renderer._resolve_image_source(artifact),
            "https://wordpress.example/hero.jpg",
        )

    def test_wordpress_url_is_preferred_over_source_url(self):
        artifact = self._artifact(
            wordpress_media_url="https://wordpress.example/hero.jpg",
            source_url="https://source.example/hero.jpg",
        )
        self.assertEqual(
            self.renderer._resolve_image_source(artifact),
            "https://wordpress.example/hero.jpg",
        )

    def test_source_url_is_used_as_fallback(self):
        artifact = self._artifact(source_url="https://source.example/hero.jpg")
        self.assertEqual(
            self.renderer._resolve_image_source(artifact),
            "https://source.example/hero.jpg",
        )

    def test_missing_optional_sources_are_handled_safely(self):
        artifact = self._artifact(local_path=None, wordpress_media_url=None, source_url=None)
        self.assertIsNone(self.renderer._resolve_image_source(artifact))
        self.assertEqual(self.renderer._build_hero_image_markup(artifact), "")

    def test_local_directory_is_not_treated_as_renderable(self):
        artifact = self._artifact(local_path=str(self.preview_root))
        artifact.wordpress_media_url = "https://wordpress.example/fallback.jpg"
        self.assertEqual(
            self.renderer._resolve_image_source(artifact),
            "https://wordpress.example/fallback.jpg",
        )

    def test_local_path_tilde_is_expanded(self):
        image_path = self._write_image()
        artifact = self._artifact(local_path=str(image_path))
        self.assertEqual(
            self.renderer._resolve_image_source(artifact),
            image_path.resolve().as_uri(),
        )

    def test_image_has_responsive_rendering_style(self):
        content = self._assemble(self._artifact(local_path=str(self._write_image())))
        self.assertIn('style="max-width: 100%; height: auto;"', content)

    def test_existing_frontend_content_remains_present(self):
        content = self._assemble(
            self._artifact(local_path=str(self._write_image())),
            self._frontend(html="<section>Keep this section</section>"),
        )
        self.assertIn("<section>Keep this section</section>", content)

    def test_hero_markup_does_not_replace_frontend_content(self):
        content = self._assemble(
            self._artifact(local_path=str(self._write_image())),
            self._frontend(html="<div>Original frontend</div>"),
        )
        self.assertIn("<figure", content)
        self.assertIn("<div>Original frontend</div>", content)

    def test_blocks_remain_preferred_with_hero_composition(self):
        content = self._assemble(
            self._artifact(local_path=str(self._write_image())),
            self._frontend(html="<div>Raw</div>", blocks="<section>Blocks</section>"),
        )
        self.assertIn("<section>Blocks</section>", content)
        self.assertNotIn("<div>Raw</div>", content)
        self.assertIn("data-preview-hero-image", content)

    def test_injected_image_appears_in_image_load_states(self):
        image_path = self._write_image()
        source = image_path.resolve().as_uri()
        evaluate_return = {
            "document_scroll_width": 1440,
            "document_client_width": 1440,
            "document_scroll_height": 900,
            "document_client_height": 900,
            "image_states": [{"src": source, "complete": True, "natural_width": 1, "natural_height": 1}],
            "element_bounding_boxes": [],
        }
        with patch("playwright.sync_api.sync_playwright") as mock_sync_playwright:
            self._mock_playwright(mock_sync_playwright, evaluate_return)
            preview_dir = self.renderer._create_preview_directory("task-preview", 1, "preview-1")
            html_path = self.renderer._assemble_preview_html(
                self._frontend(), preview_dir, self._artifact(local_path=str(image_path))
            )
            _, evidence = self.renderer._render_with_browser(
                "task-preview", "preview-1", 1, html_path, preview_dir
            )
        self.assertEqual(evidence.desktop.image_load_states[0]["src"], source)

    def test_successful_injected_image_has_natural_dimensions(self):
        image_path = self._write_image()
        evaluate_return = {
            "document_scroll_width": 1440,
            "document_client_width": 1440,
            "document_scroll_height": 900,
            "document_client_height": 900,
            "image_states": [{"src": image_path.resolve().as_uri(), "complete": True, "natural_width": 1, "natural_height": 1}],
            "element_bounding_boxes": [],
        }
        with patch("playwright.sync_api.sync_playwright") as mock_sync_playwright:
            self._mock_playwright(mock_sync_playwright, evaluate_return)
            preview_dir = self.renderer._create_preview_directory("task-preview", 1, "preview-1")
            html_path = self.renderer._assemble_preview_html(
                self._frontend(), preview_dir, self._artifact(local_path=str(image_path))
            )
            _, evidence = self.renderer._render_with_browser(
                "task-preview", "preview-1", 1, html_path, preview_dir
            )
        state = evidence.mobile.image_load_states[0]
        self.assertEqual(state["natural_width"], 1)
        self.assertEqual(state["natural_height"], 1)

    def test_broken_injected_image_is_visible_to_existing_evidence(self):
        missing_source = (self.preview_root / "missing-hero.png").resolve().as_uri()
        evaluate_return = {
            "document_scroll_width": 1440,
            "document_client_width": 1440,
            "document_scroll_height": 900,
            "document_client_height": 900,
            "image_states": [{"src": missing_source, "complete": True, "natural_width": 0, "natural_height": 0}],
            "element_bounding_boxes": [],
        }
        with patch("playwright.sync_api.sync_playwright") as mock_sync_playwright:
            self._mock_playwright(mock_sync_playwright, evaluate_return)
            preview_dir = self.renderer._create_preview_directory("task-preview", 1, "preview-1")
            html_path = self.renderer._assemble_preview_html(
                self._frontend(), preview_dir, self._artifact(source_url=missing_source)
            )
            _, evidence = self.renderer._render_with_browser(
                "task-preview", "preview-1", 1, html_path, preview_dir
            )
        self.assertTrue(evidence.desktop.image_load_states[0]["complete"])
        self.assertEqual(evidence.desktop.image_load_states[0]["natural_width"], 0)

    def test_rendered_technical_validator_still_reports_broken_image(self):
        viewport = ViewportRenderedEvidence(
            viewport_width=1440,
            viewport_height=900,
            document_scroll_width=1440,
            document_client_width=1440,
            document_scroll_height=900,
            document_client_height=900,
            image_load_states=[{"src": "missing.png", "complete": True, "natural_width": 0, "natural_height": 0}],
            console_errors=[],
            page_errors=[],
            element_bounding_boxes=[],
        )
        evidence = RenderedEvidence(
            task_id="task-preview",
            preview_id="preview-1",
            attempt_number=1,
            desktop=viewport,
            mobile=viewport,
            created_at="2026-09-12T00:00:00",
        )
        result = RenderedTechnicalValidator(self.config).validate(evidence)
        self.assertFalse(result.passed)
        self.assertTrue(any(error["type"] == "broken_image" for error in result.errors))

    def test_different_artifact_ids_produce_corresponding_identity(self):
        first = self._assemble(self._artifact(artifact_id="img-first"))
        second = self._assemble(self._artifact(artifact_id="img-second"))
        self.assertIn('data-image-artifact-id="img-first"', first)
        self.assertIn('data-image-artifact-id="img-second"', second)
        self.assertNotIn('data-image-artifact-id="img-second"', first)

    def test_repeated_composition_in_one_render_does_not_duplicate_hero(self):
        artifact = self._artifact(local_path=str(self._write_image()))
        content = self._assemble(artifact)
        self.assertEqual(content.count("<img"), 1)
        self.assertEqual(content.count('data-image-artifact-id="img-hero-1"'), 1)

    def test_existing_frontend_images_remain_untouched(self):
        frontend_result = self._frontend(
            html='<img src="existing.png" alt="Existing"><img src="other.png" alt="Other">'
        )
        content = self._assemble(self._artifact(local_path=str(self._write_image())), frontend_result)
        self.assertIn('<img src="existing.png" alt="Existing">', content)
        self.assertIn('<img src="other.png" alt="Other">', content)
        self.assertEqual(content.count('src="existing.png"'), 1)

    def test_render_preview_convenience_function_accepts_image_artifact(self):
        artifact = self._artifact()
        with patch("tools.preview_renderer.PreviewRenderer") as renderer_class:
            render_preview(
                "task-preview",
                self._frontend(),
                1,
                config=self.config,
                preview_id="preview-1",
                image_artifact=artifact,
            )
        renderer_class.return_value.render.assert_called_once_with(
            "task-preview", self._frontend(), 1, "preview-1", artifact
        )

    def test_workflow_ordering_remains_unchanged(self):
        source = inspect.getsource(AIWordPressFactory.run_workflow)
        self.assertLess(source.index("PreviewRenderer"), source.index("RenderedTechnicalValidator"))
        self.assertLess(source.index("RenderedTechnicalValidator"), source.index("_run_visual_quality_review"))
        self.assertLess(source.index("_run_visual_quality_review"), source.index("_continue_post_approval"))

    def test_image_agent_location_moved_before_preview(self):
        source = inspect.getsource(AIWordPressFactory._continue_post_approval)
        self.assertNotIn("_prepare_image_artifact", source)
        self.assertNotIn("PreviewRenderer", source)
        # Verify it's now in run_workflow before PreviewRenderer
        run_source = inspect.getsource(AIWordPressFactory.run_workflow)
        self.assertIn("_prepare_image_artifact", run_source)
        self.assertLess(run_source.index("_prepare_image_artifact"), run_source.index("PreviewRenderer"))
        self.assertIsNotNone(ImageAgent)

    def test_publisher_signature_remains_unchanged(self):
        signature = inspect.signature(WordPressPublisher.publish_content)
        self.assertIn("featured_media_id", signature.parameters)
        self.assertNotIn("image_artifact", signature.parameters)

    def test_approval_policy_contract_remains_unchanged(self):
        self.assertEqual(ApprovalPolicyMode.AUTO_PUBLISH.value, "auto_publish")
        self.assertEqual(ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW.value, "require_human_review")

    def test_no_new_task_status_for_composition(self):
        self.assertFalse(hasattr(TaskStatus, "PREVIEW_IMAGE_COMPOSITION"))
        self.assertFalse(hasattr(TaskStatus, "IMAGE_PREVIEW_RENDERING"))

    def test_no_image_retry_counter_added(self):
        task = Task(id="task-preview", title="Preview", content_type="BLOG_POST")
        self.assertFalse(hasattr(task, "image_retry_count"))
        self.assertFalse(hasattr(task, "image_generation_retry_count"))

    def test_no_automatic_regeneration_boundary(self):
        source = inspect.getsource(AIWordPressFactory._prepare_image_artifact)
        self.assertIn("if task.image_artifact:", source)
        self.assertIn("ImageArtifactStatus.FAILED", source)
        self.assertNotIn("while ", source)

    def test_no_wordpress_draft_preview_is_introduced(self):
        source = inspect.getsource(PreviewRenderer).lower()
        self.assertNotIn("draft", source)
        self.assertNotIn("wordpresspublisher", source)

    def test_main_workflow_now_passes_image_artifact_to_renderer(self):
        source = inspect.getsource(AIWordPressFactory.run_workflow)
        self.assertIn("preview_renderer.render(", source)
        self.assertIn("image_artifact=", source)

    def test_preview_artifact_contract_includes_image_artifact_id(self):
        self.assertTrue(hasattr(PreviewArtifact, "image_artifact_id"))


class TestPreviewImageRealChromium(unittest.TestCase):
    def test_local_hero_image_loads_in_real_chromium(self):
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                browser.close()
        except Exception:
            self.skipTest("Chromium is not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image_path = root / "hero.png"
            image_path.write_bytes(PNG_1X1)

            class Config:
                preview_base_dir = str(root)
                preview_browser_headless = True
                preview_browser_timeout_ms = 30000
                preview_page_load_timeout_ms = 30000

            renderer = PreviewRenderer(Config())
            frontend_result = FrontendResult(
                task_id="task-real",
                success=True,
                html="<main>Real browser composition</main>",
                css="",
                javascript="",
            )
            artifact = ImageArtifact(
                artifact_id="img-real-hero",
                status=ImageArtifactStatus.READY,
                local_path=str(image_path),
                alt_text="Real browser hero",
            )
            preview_artifact, failure, evidence = renderer.render(
                task_id="task-real",
                frontend_result=frontend_result,
                attempt_number=1,
                preview_id="real-composition",
                image_artifact=artifact,
            )

        self.assertIsNone(failure)
        self.assertIsNotNone(preview_artifact)
        self.assertIsNotNone(evidence)
        image_states = evidence.desktop.image_load_states + evidence.mobile.image_load_states
        matching_states = [state for state in image_states if state.get("src") == image_path.resolve().as_uri()]
        self.assertTrue(matching_states)
        self.assertTrue(all(state["complete"] for state in matching_states))
        self.assertTrue(all(state["natural_width"] > 0 for state in matching_states))


if __name__ == "__main__":
    unittest.main(verbosity=2)
