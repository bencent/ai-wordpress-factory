#!/usr/bin/env python3
"""Tests for Phase 7D-2A Rendered Evidence Collection."""

import unittest
import sys
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import (
    PreviewArtifact, PreviewViewport, PreviewInfrastructureFailure,
    FailureCategory, FrontendResult,
    RenderedEvidence, ViewportRenderedEvidence,
)
from tools.preview_renderer import PreviewRenderer, PreviewRenderError


class MockConfig:
    preview_base_dir = tempfile.mkdtemp()
    preview_browser_headless = True
    preview_browser_timeout_ms = 30000
    preview_page_load_timeout_ms = 30000


MOCK_EVIDENCE = {
    "document_scroll_width": 1440,
    "document_client_width": 1440,
    "document_scroll_height": 2000,
    "document_client_height": 900,
    "image_states": [],
    "element_bounding_boxes": [],
}


class TestRenderedEvidenceContracts(unittest.TestCase):
    """Test 11: RenderedEvidence serialization round-trip."""

    def test_rendered_evidence_serialization_roundtrip(self):
        """Test RenderedEvidence serialization round-trip (Test 11)."""
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
        evidence = RenderedEvidence(
            task_id="task-123",
            preview_id="preview-456",
            attempt_number=1,
            desktop=desktop,
            mobile=mobile,
            created_at="2024-01-01T12:00:00",
        )

        data = evidence.to_dict()
        restored = RenderedEvidence.from_dict(data)

        self.assertEqual(restored.task_id, "task-123")
        self.assertEqual(restored.preview_id, "preview-456")
        self.assertEqual(restored.attempt_number, 1)
        self.assertEqual(restored.desktop.viewport_width, 1440)
        self.assertEqual(restored.desktop.viewport_height, 900)
        self.assertEqual(restored.desktop.document_scroll_width, 1440)
        self.assertEqual(restored.desktop.document_client_width, 1440)
        self.assertEqual(restored.desktop.document_scroll_height, 2000)
        self.assertEqual(restored.desktop.document_client_height, 900)
        self.assertEqual(restored.mobile.viewport_width, 390)
        self.assertEqual(restored.mobile.viewport_height, 844)
        self.assertEqual(restored.created_at, "2024-01-01T12:00:00")


class TestRenderedEvidenceCollection(unittest.TestCase):
    """Tests 1-12 for evidence collection behavior."""

    def setUp(self):
        from tools.preview_renderer import PreviewRenderer
        self.config = MockConfig()
        self.renderer = PreviewRenderer(self.config)
        self.temp_dir = self.config.preview_base_dir

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _make_frontend_result(self, html="<div>Test</div>", css="body { color: red; }", javascript=""):
        return FrontendResult(
            task_id="task-123",
            success=True,
            html=html,
            css=css,
            javascript=javascript,
        )

    def _setup_mock_playwright(self, mock_sync_playwright, evaluate_return=MOCK_EVIDENCE):
        """Common mock setup for Playwright browser simulation."""
        mock_playwright = MagicMock()
        mock_sync_playwright.return_value.__enter__.return_value = mock_playwright
        mock_browser = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page

        def mock_screenshot(path, **kwargs):
            Path(path).write_bytes(b"fake png data")
        mock_page.screenshot.side_effect = mock_screenshot
        mock_page.evaluate.return_value = evaluate_return
        return mock_page, mock_browser, mock_context

    @patch('playwright.sync_api.sync_playwright')
    def test_rendered_evidence_produced_on_successful_render(self, mock_sync_playwright):
        """Test 1: RenderedEvidence produced on successful render."""
        mock_page, mock_browser, mock_context = self._setup_mock_playwright(mock_sync_playwright)
        frontend_result = self._make_frontend_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)

            artifact, evidence = self.renderer._render_with_browser(
                task_id="task-123",
                preview_id="preview-456",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )

            self.assertIsNotNone(artifact)
            self.assertIsNotNone(evidence)
            self.assertIsInstance(evidence, RenderedEvidence)

    @patch('playwright.sync_api.sync_playwright')
    def test_desktop_viewport_evidence_1440(self, mock_sync_playwright):
        """Test 2: Desktop viewport evidence contains 1440 client context."""
        mock_page, _, _ = self._setup_mock_playwright(mock_sync_playwright)
        frontend_result = self._make_frontend_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)

            _, evidence = self.renderer._render_with_browser(
                task_id="task-123",
                preview_id="preview-456",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )

            self.assertEqual(evidence.desktop.viewport_width, 1440)
            self.assertEqual(evidence.desktop.viewport_height, 900)

    @patch('playwright.sync_api.sync_playwright')
    def test_mobile_viewport_evidence_390(self, mock_sync_playwright):
        """Test 3: Mobile viewport evidence contains 390 client context."""
        mock_page, _, _ = self._setup_mock_playwright(mock_sync_playwright)
        frontend_result = self._make_frontend_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)

            _, evidence = self.renderer._render_with_browser(
                task_id="task-123",
                preview_id="preview-456",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )

            self.assertEqual(evidence.mobile.viewport_width, 390)
            self.assertEqual(evidence.mobile.viewport_height, 844)

    @patch('playwright.sync_api.sync_playwright')
    def test_document_scrollwidth_clientwidth_captured(self, mock_sync_playwright):
        """Test 4: Document scrollWidth/clientWidth captured."""
        evaluate_returns = [
            {
                "document_scroll_width": 1460,
                "document_client_width": 1440,
                "document_scroll_height": 3000,
                "document_client_height": 900,
                "image_states": [],
                "element_bounding_boxes": [],
            },
            {
                "document_scroll_width": 420,
                "document_client_width": 390,
                "document_scroll_height": 2800,
                "document_client_height": 844,
                "image_states": [],
                "element_bounding_boxes": [],
            },
        ]
        mock_page, _, _ = self._setup_mock_playwright(mock_sync_playwright)
        mock_page.evaluate.side_effect = evaluate_returns
        frontend_result = self._make_frontend_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)

            _, evidence = self.renderer._render_with_browser(
                task_id="task-123",
                preview_id="preview-456",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )

            self.assertEqual(evidence.desktop.document_scroll_width, 1460)
            self.assertEqual(evidence.desktop.document_client_width, 1440)
            self.assertEqual(evidence.mobile.document_scroll_width, 420)
            self.assertEqual(evidence.mobile.document_client_width, 390)

    @patch('playwright.sync_api.sync_playwright')
    def test_document_scrollheight_clientheight_captured(self, mock_sync_playwright):
        """Test 5: Document scrollHeight/clientHeight captured."""
        evaluate_returns = [
            {
                "document_scroll_width": 1440,
                "document_client_width": 1440,
                "document_scroll_height": 2500,
                "document_client_height": 900,
                "image_states": [],
                "element_bounding_boxes": [],
            },
            {
                "document_scroll_width": 390,
                "document_client_width": 390,
                "document_scroll_height": 1800,
                "document_client_height": 844,
                "image_states": [],
                "element_bounding_boxes": [],
            },
        ]
        mock_page, _, _ = self._setup_mock_playwright(mock_sync_playwright)
        mock_page.evaluate.side_effect = evaluate_returns
        frontend_result = self._make_frontend_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)

            _, evidence = self.renderer._render_with_browser(
                task_id="task-123",
                preview_id="preview-456",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )

            self.assertEqual(evidence.desktop.document_scroll_height, 2500)
            self.assertEqual(evidence.desktop.document_client_height, 900)
            self.assertEqual(evidence.mobile.document_scroll_height, 1800)
            self.assertEqual(evidence.mobile.document_client_height, 844)

    @patch('playwright.sync_api.sync_playwright')
    def test_valid_image_load_state_captured(self, mock_sync_playwright):
        """Test 6: Valid image load state captured."""
        evaluate_return = {
            "document_scroll_width": 1440,
            "document_client_width": 1440,
            "document_scroll_height": 1000,
            "document_client_height": 900,
            "image_states": [
                {
                    "src": "test-image.png",
                    "complete": True,
                    "natural_width": 800,
                    "natural_height": 600,
                }
            ],
            "element_bounding_boxes": [],
        }
        mock_page, _, _ = self._setup_mock_playwright(mock_sync_playwright)
        mock_page.evaluate.return_value = evaluate_return
        frontend_result = self._make_frontend_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)

            _, evidence = self.renderer._render_with_browser(
                task_id="task-123",
                preview_id="preview-456",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )

            self.assertEqual(len(evidence.desktop.image_load_states), 1)
            self.assertTrue(evidence.desktop.image_load_states[0]["complete"])
            self.assertEqual(evidence.desktop.image_load_states[0]["natural_width"], 800)
            self.assertEqual(evidence.desktop.image_load_states[0]["natural_height"], 600)

    @patch('playwright.sync_api.sync_playwright')
    def test_broken_image_evidence_captured(self, mock_sync_playwright):
        """Test 7: Broken image naturalWidth=0 captured as evidence only (not verdict)."""
        evaluate_return = {
            "document_scroll_width": 1440,
            "document_client_width": 1440,
            "document_scroll_height": 1000,
            "document_client_height": 900,
            "image_states": [
                {
                    "src": "broken-image.png",
                    "complete": True,
                    "natural_width": 0,
                    "natural_height": 0,
                }
            ],
            "element_bounding_boxes": [],
        }
        mock_page, _, _ = self._setup_mock_playwright(mock_sync_playwright)
        mock_page.evaluate.return_value = evaluate_return
        frontend_result = self._make_frontend_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)

            _, evidence = self.renderer._render_with_browser(
                task_id="task-123",
                preview_id="preview-456",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )

            self.assertEqual(len(evidence.desktop.image_load_states), 1)
            self.assertTrue(evidence.desktop.image_load_states[0]["complete"])
            self.assertEqual(evidence.desktop.image_load_states[0]["natural_width"], 0)
            self.assertEqual(evidence.desktop.image_load_states[0]["natural_height"], 0)
            # No verdict — just evidence
            self.assertIsInstance(evidence, RenderedEvidence)

    @patch('playwright.sync_api.sync_playwright')
    def test_pageerror_captured(self, mock_sync_playwright):
        """Test 8: Page JS error captured in evidence."""
        mock_playwright = MagicMock()
        mock_sync_playwright.return_value.__enter__.return_value = mock_playwright
        mock_browser = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page

        def mock_screenshot(path, **kwargs):
            Path(path).write_bytes(b"fake png data")
        mock_page.screenshot.side_effect = mock_screenshot
        mock_page.evaluate.return_value = MOCK_EVIDENCE

        # Simulate page.on("pageerror", ...) — store callbacks
        registered_callbacks = {}
        def mock_on(event_name, callback):
            registered_callbacks[event_name] = callback
        mock_page.on.side_effect = mock_on

        # Simulate firing pageerror during goto
        def mock_goto(url, **kwargs):
            if "pageerror" in registered_callbacks:
                registered_callbacks["pageerror"](Exception("Uncaught TypeError: something is not a function"))
            return {}
        mock_page.goto.side_effect = mock_goto

        frontend_result = self._make_frontend_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)

            _, evidence = self.renderer._render_with_browser(
                task_id="task-123",
                preview_id="preview-456",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )

            # Verify pageerror listener was registered
            self.assertIn("pageerror", registered_callbacks)

            # Verify the error was captured
            self.assertEqual(len(evidence.desktop.page_errors), 1)
            self.assertIn("pageerror", evidence.desktop.page_errors[0]["type"])
            self.assertIn("Uncaught TypeError", evidence.desktop.page_errors[0]["message"])

    @patch('playwright.sync_api.sync_playwright')
    def test_console_error_captured(self, mock_sync_playwright):
        """Test 9: Console error captured in evidence."""
        mock_playwright = MagicMock()
        mock_sync_playwright.return_value.__enter__.return_value = mock_playwright
        mock_browser = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page

        def mock_screenshot(path, **kwargs):
            Path(path).write_bytes(b"fake png data")
        mock_page.screenshot.side_effect = mock_screenshot
        mock_page.evaluate.return_value = MOCK_EVIDENCE

        # Simulate page.on("console", ...) — store callbacks
        registered_callbacks = {}
        def mock_on(event_name, callback):
            registered_callbacks[event_name] = callback
        mock_page.on.side_effect = mock_on

        # Simulate firing console error during goto
        def mock_goto(url, **kwargs):
            if "console" in registered_callbacks:
                console_msg = Mock()
                console_msg.type = "error"
                console_msg.text = "Failed to load resource"
                console_msg.location = {"url": "test.html", "line": 10, "column": 5}
                registered_callbacks["console"](console_msg)
            return {}
        mock_page.goto.side_effect = mock_goto

        frontend_result = self._make_frontend_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)

            _, evidence = self.renderer._render_with_browser(
                task_id="task-123",
                preview_id="preview-456",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )

            # Verify console listener was registered
            self.assertIn("console", registered_callbacks)

            # Verify the error was captured
            self.assertEqual(len(evidence.desktop.console_errors), 1)
            self.assertIn("console_error", evidence.desktop.console_errors[0]["type"])
            self.assertIn("Failed to load", evidence.desktop.console_errors[0]["message"])

    @patch('playwright.sync_api.sync_playwright')
    def test_infrastructure_failure_no_fake_evidence(self, mock_sync_playwright):
        """Test 10: Infrastructure failure does not produce fake evidence."""
        mock_playwright = MagicMock()
        mock_sync_playwright.return_value.__enter__.return_value = mock_playwright
        mock_brower = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_brower
        mock_context = MagicMock()
        mock_brower.new_context.return_value = mock_context
        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page

        def mock_screenshot_fail(path, **kwargs):
            pass
        mock_page.screenshot.side_effect = mock_screenshot_fail
        mock_page.evaluate.return_value = MOCK_EVIDENCE

        frontend_result = self._make_frontend_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)

            # Use render() which returns the 3-tuple
            artifact, failure, evidence = self.renderer.render(
                task_id="task-123",
                frontend_result=frontend_result,
                attempt_number=1,
            )

            self.assertIsNone(artifact)
            self.assertIsNotNone(failure)
            self.assertIsNone(evidence)
            self.assertEqual(failure.error_type, "screenshot_capture_failed")
            self.assertEqual(failure.failure_category, FailureCategory.INFRASTRUCTURE)

    def test_serialization_roundtrip_rendered_evidence(self):
        """Test 11: RenderedEvidence serialization round-trip via contracts."""
        desktop = ViewportRenderedEvidence(
            viewport_width=1440, viewport_height=900,
            document_scroll_width=1440, document_client_width=1440,
            document_scroll_height=2000, document_client_height=900,
            console_errors=[], page_errors=[],
            image_load_states=[], element_bounding_boxes=[],
        )
        mobile = ViewportRenderedEvidence(
            viewport_width=390, viewport_height=844,
            document_scroll_width=390, document_client_width=390,
            document_scroll_height=1500, document_client_height=844,
            console_errors=[], page_errors=[],
            image_load_states=[], element_bounding_boxes=[],
        )
        evidence = RenderedEvidence(
            task_id="t1", preview_id="p1", attempt_number=2,
            desktop=desktop, mobile=mobile,
            created_at="2024-01-01T12:00:00",
        )

        data = evidence.to_dict()
        restored = RenderedEvidence.from_dict(data)
        self.assertEqual(restored.task_id, "t1")
        self.assertEqual(restored.attempt_number, 2)
        self.assertEqual(restored.desktop.viewport_width, 1440)
        self.assertEqual(restored.mobile.viewport_width, 390)


class TestPreviewArtifactUnchanged(unittest.TestCase):
    """Test 12: PreviewArtifact screenshot behavior remains unchanged."""

    def setUp(self):
        from tools.preview_renderer import PreviewRenderer
        self.config = MockConfig()
        self.renderer = PreviewRenderer(self.config)
        self.temp_dir = self.config.preview_base_dir

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_preview_artifact_paths_unchanged(self):
        """Test 12: PreviewArtifact still has correct screenshot paths after evidence addition."""
        frontend_result = FrontendResult(
            task_id="task-123", success=True,
            html="<div>Test</div>", css="body { color: red; }", javascript="",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            html_path = self.renderer._assemble_preview_html(frontend_result, preview_dir)

            artifact, evidence = self.renderer._render_with_browser(
                task_id="task-123",
                preview_id="preview-456",
                attempt_number=1,
                html_path=html_path,
                preview_dir=preview_dir,
            )

            # Verify PreviewArtifact has all 4 screenshot paths
            self.assertEqual(artifact.desktop_viewport_screenshot_path, str(preview_dir / "desktop-viewport.png"))
            self.assertEqual(artifact.desktop_full_page_screenshot_path, str(preview_dir / "desktop-full.png"))
            self.assertEqual(artifact.mobile_viewport_screenshot_path, str(preview_dir / "mobile-viewport.png"))
            self.assertEqual(artifact.mobile_full_page_screenshot_path, str(preview_dir / "mobile-full.png"))

            # Verify viewport dimensions unchanged
            self.assertEqual(artifact.desktop_viewport.width, 1440)
            self.assertEqual(artifact.desktop_viewport.height, 900)
            self.assertEqual(artifact.mobile_viewport.width, 390)
            self.assertEqual(artifact.mobile_viewport.height, 844)


# ==================== Real Chromium Integration Test ====================

class TestRealChromiumEvidence(unittest.TestCase):
    """Real browser integration test for evidence collection."""

    def setUp(self):
        from tools.preview_renderer import PreviewRenderer
        self.config = MockConfig()
        self.renderer = PreviewRenderer(self.config)

    def test_real_chromium_horizontal_overflow_evidence(self):
        """Real Chromium integration test: horizontal overflow evidence captured from browser."""
        from playwright.sync_api import sync_playwright

        # Check if browser is available
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
        except Exception:
            self.skipTest("Chromium not available for integration test")

        # Create HTML with intentional horizontal overflow
        html_with_overflow = "<div style='width: 2000px; height: 100px; background: red;'>Overflow content</div>"

        frontend_result = FrontendResult(
            task_id="integration-test",
            success=True,
            html=html_with_overflow,
            css="",
            javascript="",
            blocks="",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            preview_dir = Path(tmpdir)
            artifact, failure, evidence = self.renderer.render(
                task_id="integration-test",
                frontend_result=frontend_result,
                attempt_number=1,
                preview_id="integration-evidence",
            )

            self.assertIsNotNone(artifact)
            self.assertIsNotNone(evidence)

            # Desktop evidence: document scrollWidth should exceed clientWidth
            # due to the 2000px wide div
            self.assertGreater(
                evidence.desktop.document_scroll_width,
                evidence.desktop.document_client_width,
                "Desktop document should show horizontal overflow from 2000px div"
            )

            # Mobile evidence: same overflow should be detected
            self.assertGreater(
                evidence.mobile.document_scroll_width,
                evidence.mobile.document_client_width,
                "Mobile document should show horizontal overflow from 2000px div"
            )


if __name__ == '__main__':
    unittest.main(verbosity=2)
