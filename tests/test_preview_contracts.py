#!/usr/bin/env python3
"""Tests for Phase 7D-1 Preview contracts and renderer."""

import unittest
import sys
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import (
    PreviewViewport, PreviewArtifact, PreviewInfrastructureFailure,
    FailureCategory, FrontendResult
)


class TestPreviewContracts(unittest.TestCase):
    """Tests for Preview contracts serialization and round-trip."""

    def test_preview_viewport_serialization_roundtrip(self):
        """Test PreviewViewport serialization round-trip."""
        viewport = PreviewViewport(width=1440, height=900)
        
        data = viewport.to_dict()
        restored = PreviewViewport.from_dict(data)
        
        self.assertEqual(restored.width, 1440)
        self.assertEqual(restored.height, 900)

    def test_preview_viewport_defaults(self):
        """Test PreviewViewport default values in from_dict."""
        restored = PreviewViewport.from_dict({})
        self.assertEqual(restored.width, 0)
        self.assertEqual(restored.height, 0)

    def test_preview_artifact_serialization_roundtrip(self):
        """Test PreviewArtifact serialization round-trip."""
        artifact = PreviewArtifact(
            task_id="task-123",
            preview_id="preview-456",
            attempt_number=1,
            desktop_viewport_screenshot_path="/artifacts/previews/task-123/attempt-1/preview-456/desktop-viewport.png",
            desktop_full_page_screenshot_path="/artifacts/previews/task-123/attempt-1/preview-456/desktop-full.png",
            mobile_viewport_screenshot_path="/artifacts/previews/task-123/attempt-1/preview-456/mobile-viewport.png",
            mobile_full_page_screenshot_path="/artifacts/previews/task-123/attempt-1/preview-456/mobile-full.png",
            desktop_viewport=PreviewViewport(width=1440, height=900),
            mobile_viewport=PreviewViewport(width=390, height=844),
            created_at="2024-01-01T12:00:00",
        )
        
        data = artifact.to_dict()
        restored = PreviewArtifact.from_dict(data)
        
        self.assertEqual(restored.task_id, "task-123")
        self.assertEqual(restored.preview_id, "preview-456")
        self.assertEqual(restored.attempt_number, 1)
        self.assertEqual(restored.desktop_viewport_screenshot_path, artifact.desktop_viewport_screenshot_path)
        self.assertEqual(restored.desktop_full_page_screenshot_path, artifact.desktop_full_page_screenshot_path)
        self.assertEqual(restored.mobile_viewport_screenshot_path, artifact.mobile_viewport_screenshot_path)
        self.assertEqual(restored.mobile_full_page_screenshot_path, artifact.mobile_full_page_screenshot_path)
        self.assertEqual(restored.desktop_viewport.width, 1440)
        self.assertEqual(restored.desktop_viewport.height, 900)
        self.assertEqual(restored.mobile_viewport.width, 390)
        self.assertEqual(restored.mobile_viewport.height, 844)
        self.assertEqual(restored.created_at, "2024-01-01T12:00:00")

    def test_preview_artifact_defaults(self):
        """Test PreviewArtifact defaults for missing fields."""
        minimal_data = {
            "task_id": "task-123",
            "preview_id": "preview-456",
            "attempt_number": 1,
            "desktop_viewport_screenshot_path": "/path/desktop-viewport.png",
            "desktop_full_page_screenshot_path": "/path/desktop-full.png",
            "mobile_viewport_screenshot_path": "/path/mobile-viewport.png",
            "mobile_full_page_screenshot_path": "/path/mobile-full.png",
        }
        restored = PreviewArtifact.from_dict(minimal_data)
        
        self.assertEqual(restored.desktop_viewport.width, 1440)
        self.assertEqual(restored.desktop_viewport.height, 900)
        self.assertEqual(restored.mobile_viewport.width, 390)
        self.assertEqual(restored.mobile_viewport.height, 844)

    def test_preview_infrastructure_failure_serialization_roundtrip(self):
        """Test PreviewInfrastructureFailure serialization round-trip."""
        failure = PreviewInfrastructureFailure(
            task_id="task-123",
            preview_id="preview-456",
            attempt_number=1,
            error_type="browser_launch_failed",
            message="Chromium executable not found",
            retryable=True,
            occurred_at="2024-01-01T12:00:00",
            failure_category=FailureCategory.INFRASTRUCTURE,
        )
        
        data = failure.to_dict()
        restored = PreviewInfrastructureFailure.from_dict(data)
        
        self.assertEqual(restored.task_id, "task-123")
        self.assertEqual(restored.preview_id, "preview-456")
        self.assertEqual(restored.attempt_number, 1)
        self.assertEqual(restored.error_type, "browser_launch_failed")
        self.assertEqual(restored.message, "Chromium executable not found")
        self.assertTrue(restored.retryable)
        self.assertEqual(restored.failure_category, FailureCategory.INFRASTRUCTURE)

    def test_preview_infrastructure_failure_content_category(self):
        """Test PreviewInfrastructureFailure with CONTENT category."""
        failure = PreviewInfrastructureFailure(
            task_id="task-123",
            preview_id="preview-456",
            attempt_number=1,
            error_type="some_error",
            message="Some message",
            retryable=False,
            occurred_at="2024-01-01T12:00:00",
            failure_category=FailureCategory.CONTENT,
        )
        
        data = failure.to_dict()
        restored = PreviewInfrastructureFailure.from_dict(data)
        
        self.assertEqual(restored.failure_category, FailureCategory.CONTENT)

    def test_failure_category_enum(self):
        """Test FailureCategory enum values."""
        self.assertEqual(FailureCategory.CONTENT.value, "content")
        self.assertEqual(FailureCategory.INFRASTRUCTURE.value, "infrastructure")


class TestPreviewRenderer(unittest.TestCase):
    """Tests for PreviewRenderer tool."""

    def setUp(self):
        from tools.preview_renderer import PreviewRenderer
        
        class MockConfig:
            preview_base_dir = tempfile.mkdtemp()
            preview_browser_headless = True
            preview_browser_timeout_ms = 30000
            preview_page_load_timeout_ms = 30000
        
        self.config = MockConfig()
        self.renderer = PreviewRenderer(self.config)
        self.temp_dir = self.config.preview_base_dir

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_renderer_creates_correct_directory_structure(self):
        """Test that renderer creates correct directory structure."""
        from tools.preview_renderer import PreviewRenderer
        
        # Create a mock frontend result
        frontend_result = FrontendResult(
            task_id="task-123",
            success=True,
            html="<div>Test content</div>",
            css="body { color: #333; }",
            javascript="console.log('test');",
        )
        
        preview_dir = self.renderer._create_preview_directory("task-123", 1, "preview-456")
        expected = Path(self.temp_dir) / "task-123" / "attempt-1" / "preview-456"
        self.assertEqual(preview_dir, expected)
        self.assertTrue(preview_dir.exists())

    def test_assemble_preview_html_with_blocks(self):
        """Test HTML assembly prefers blocks over raw HTML."""
        frontend_result = FrontendResult(
            task_id="task-123",
            success=True,
            html="<div>Raw HTML</div>",
            css="body { color: #333; }",
            javascript="",
            blocks="<!-- wp:paragraph --><p>Converted blocks</p><!-- /wp:paragraph -->",
        )
        
        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)
            
            content = html_path.read_text(encoding='utf-8')
            self.assertIn("Converted blocks", content)
            self.assertNotIn("Raw HTML", content)
            self.assertIn("body { color: #333; }", content)

    def test_assemble_preview_html_without_blocks(self):
        """Test HTML assembly uses raw HTML when no blocks."""
        frontend_result = FrontendResult(
            task_id="task-123",
            success=True,
            html="<div>Raw HTML content</div>",
            css="body { color: #333; }",
            javascript="console.log('test');",
            blocks="",
        )
        
        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)
            
            content = html_path.read_text(encoding='utf-8')
            self.assertIn("Raw HTML content", content)
            self.assertIn("body { color: #333; }", content)
            self.assertIn("console.log('test');", content)

    def test_assemble_preview_html_structure(self):
        """Test assembled HTML has valid structure."""
        frontend_result = FrontendResult(
            task_id="task-123",
            success=True,
            html="<div>Test</div>",
            css="body { color: red; }",
            javascript="const x = 1;",
        )
        
        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)
            
            content = html_path.read_text(encoding='utf-8')
            self.assertTrue(content.startswith("<!DOCTYPE html>"))
            self.assertIn('<meta charset="UTF-8">', content)
            self.assertIn('<meta name="viewport"', content)
            self.assertIn("<style>", content)
            self.assertIn("</style>", content)
            self.assertIn("<script>", content)
            self.assertIn("</script>", content)
            self.assertIn("</html>", content)

    @patch('playwright.sync_api.sync_playwright')
    def test_render_with_browser_captures_four_screenshots(self, mock_sync_playwright):
        """Test that renderer captures four screenshots via Playwright."""
        from tools.preview_renderer import PreviewRenderer
        from pathlib import Path
        
        # Setup mock playwright
        mock_playwright = MagicMock()
        mock_sync_playwright.return_value.__enter__.return_value = mock_playwright
        
        mock_browser = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser
        
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context
        
        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page
        
        # Create test frontend result
        frontend_result = FrontendResult(
            task_id="task-123",
            success=True,
            html="<div>Test</div>",
            css="body { color: red; }",
            javascript="",
        )
        
        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)
            
            # Mock screenshot paths to exist and have size
            desktop_vp = preview_dir / "desktop-viewport.png"
            desktop_fp = preview_dir / "desktop-full.png"
            mobile_vp = preview_dir / "mobile-viewport.png"
            mobile_fp = preview_dir / "mobile-full.png"
            
            def mock_screenshot(path, **kwargs):
                Path(path).write_bytes(b"fake png data")
            
            mock_page.screenshot.side_effect = mock_screenshot
            
            artifact = self.renderer._render_with_browser(
                task_id="task-123",
                preview_id="preview-456",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )
            
            # Verify four screenshots were captured
            self.assertEqual(mock_page.screenshot.call_count, 4)
            
            # Verify viewport sizes were set
            viewport_calls = mock_page.set_viewport_size.call_args_list
            self.assertEqual(len(viewport_calls), 2)
            # Check positional args (call_args[0]) since viewport is passed as positional dict
            self.assertEqual(viewport_calls[0][0][0], {"width": 1440, "height": 900})
            self.assertEqual(viewport_calls[1][0][0], {"width": 390, "height": 844})
            
            # Verify artifact has correct paths
            self.assertEqual(artifact.desktop_viewport_screenshot_path, str(desktop_vp))
            self.assertEqual(artifact.desktop_full_page_screenshot_path, str(desktop_fp))
            self.assertEqual(artifact.mobile_viewport_screenshot_path, str(mobile_vp))
            self.assertEqual(artifact.mobile_full_page_screenshot_path, str(mobile_fp))
            
            # Verify viewport dimensions in artifact
            self.assertEqual(artifact.desktop_viewport.width, 1440)
            self.assertEqual(artifact.desktop_viewport.height, 900)
            self.assertEqual(artifact.mobile_viewport.width, 390)
            self.assertEqual(artifact.mobile_viewport.height, 844)
            
            # Verify attempt_number
            self.assertEqual(artifact.attempt_number, 1)

    @patch('playwright.sync_api.sync_playwright')
    def test_render_infrastructure_failure_browser_launch(self, mock_sync_playwright):
        """Test infrastructure failure when browser launch fails."""
        from tools.preview_renderer import PreviewRenderer, PreviewRenderError
        
        # Mock the context manager to raise exception on enter
        mock_cm = MagicMock()
        mock_cm.__enter__.side_effect = Exception("Playwright not installed")
        mock_sync_playwright.return_value = mock_cm
        
        frontend_result = FrontendResult(
            task_id="task-123",
            success=True,
            html="<div>Test</div>",
            css="",
            javascript="",
        )
        
        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)
            
            with self.assertRaises(PreviewRenderError) as cm:
                self.renderer._render_with_browser(
                    task_id="task-123",
                    preview_id="preview-456",
                    attempt_number=1,
                    html_path=html_path,
                    preview_dir=preview_dir,
                )
            
            self.assertEqual(cm.exception.error_type, "browser_launch_failed")
            self.assertTrue(cm.exception.retryable)

    @patch('playwright.sync_api.sync_playwright')
    def test_render_screenshot_capture_failure(self, mock_sync_playwright):
        """Test infrastructure failure when screenshot capture fails."""
        mock_playwright = MagicMock()
        mock_sync_playwright.return_value.__enter__.return_value = mock_playwright
        
        mock_browser = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser
        
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context
        
        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page
        
        # Make screenshot fail by not creating file
        def mock_screenshot_fail(path, **kwargs):
            pass  # Don't create file
        
        mock_page.screenshot.side_effect = mock_screenshot_fail
        
        frontend_result = FrontendResult(
            task_id="task-123",
            success=True,
            html="<div>Test</div>",
            css="",
            javascript="",
        )
        
        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)
            
            from tools.preview_renderer import PreviewRenderError
            with self.assertRaises(PreviewRenderError) as cm:
                self.renderer._render_with_browser(
                    task_id="task-123",
                    preview_id="preview-456",
                    attempt_number=1,
                    html_path=html_path,
                    preview_dir=preview_dir,
                )
            
            self.assertEqual(cm.exception.error_type, "screenshot_capture_failed")
            self.assertTrue(cm.exception.retryable)


if __name__ == '__main__':
    unittest.main(verbosity=2)