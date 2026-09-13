"""PreviewRenderer: Browser-based frontend preview rendering tool.
 
Renders validated frontend artifacts in a real browser (Chromium via Playwright)
and captures four screenshots per preview:
- Desktop viewport (1440x900)
- Desktop full-page
- Mobile viewport (390x844)
- Mobile full-page

Phase 7D-2A: Also collects deterministic browser-rendered evidence
(document measurements, page errors, console errors, image load states,
element bounding boxes) alongside the PreviewArtifact.

This is a deterministic infrastructure tool - it does NOT judge visual quality,
call LLMs, or make routing decisions.
"""

import os
import uuid
import datetime
import tempfile
import shutil
from html import escape
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum

from contracts import (
    PreviewArtifact, PreviewViewport, PreviewInfrastructureFailure,
    FailureCategory, FrontendResult, RenderedEvidence, ViewportRenderedEvidence,
    ImageArtifact, ImageArtifactStatus,
)
from . import BaseTool


class PreviewRenderError(Exception):
    """Exception for preview rendering failures."""
    def __init__(self, error_type: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.retryable = retryable


class PreviewRenderer(BaseTool):
    """Deterministic browser rendering tool for frontend preview capture.
    
    Responsibilities:
    - Assemble temporary local HTML document from frontend artifacts
    - Launch Chromium browser via Playwright
    - Render page at two viewports (desktop + mobile)
    - Capture viewport and full-page screenshots for each viewport
    - Return PreviewArtifact with screenshot paths
    
    Does NOT:
    - Judge visual quality
    - Call LLMs
    - Make routing decisions
    - Rewrite frontend code
    - Publish to WordPress
    """
    
    # Frozen viewport dimensions for v1
    DESKTOP_WIDTH = 1440
    DESKTOP_HEIGHT = 900
    MOBILE_WIDTH = 390
    MOBILE_HEIGHT = 844
    
    # Preview infrastructure retry limit
    MAX_INFRA_RETRIES = 2
    
    def __init__(self, config):
        super().__init__(config)
        self.description = "Deterministic browser rendering for frontend preview capture."
        
        # Preview storage base directory (relative to project root)
        self.preview_base_dir = getattr(config, 'preview_base_dir', 'artifacts/previews')
        
        # Browser launch options
        self.browser_headless = getattr(config, 'preview_browser_headless', True)
        self.browser_timeout_ms = getattr(config, 'preview_browser_timeout_ms', 30000)
        self.page_load_timeout_ms = getattr(config, 'preview_page_load_timeout_ms', 30000)
        
    def render(
        self,
        task_id: str,
        frontend_result: FrontendResult,
        attempt_number: int,
        preview_id: Optional[str] = None,
        image_artifact: Optional[ImageArtifact] = None,
    ) -> Tuple[Optional[PreviewArtifact], Optional[PreviewInfrastructureFailure], Optional[RenderedEvidence]]:
        """Render frontend artifact and capture preview screenshots.
        
        Args:
            task_id: Task identifier
            frontend_result: Validated frontend result (after production quality gate)
            attempt_number: Frontend generation attempt number (1-indexed)
            preview_id: Optional preview ID (generated if not provided)
            image_artifact: Optional READY ImageArtifact to compose into preview
            
        Returns:
            Tuple of (PreviewArtifact, PreviewInfrastructureFailure, RenderedEvidence)
            Exactly one of the first two will be non-None.
            On success, RenderedEvidence is also non-None.
            On failure, RenderedEvidence is None.
        """
        if preview_id is None:
            preview_id = str(uuid.uuid4())[:8]
            
        # Create task-scoped preview directory
        preview_dir = self._create_preview_directory(task_id, attempt_number, preview_id)
        
        # Assemble temporary HTML document
        html_path = self._assemble_preview_html(frontend_result, preview_dir, image_artifact)
        
        # Render with browser (with infrastructure retry)
        for infra_attempt in range(self.MAX_INFRA_RETRIES + 1):
            try:
                artifact, evidence = self._render_with_browser(
                    task_id=task_id,
                    preview_id=preview_id,
                    attempt_number=attempt_number,
                    html_path=html_path,
                    preview_dir=preview_dir,
                    image_artifact=image_artifact,
                )
                return artifact, None, evidence
                
            except PreviewRenderError as e:
                # Check if we should retry
                if infra_attempt < self.MAX_INFRA_RETRIES and e.retryable:
                    self.log(f"Preview render attempt {infra_attempt + 1} failed, retrying: {e.message}", "warning")
                    continue
                
                # Exhausted retries or non-retryable error
                failure = PreviewInfrastructureFailure(
                    task_id=task_id,
                    preview_id=preview_id,
                    attempt_number=attempt_number,
                    error_type=e.error_type,
                    message=e.message,
                    retryable=e.retryable,
                    occurred_at=datetime.datetime.now().isoformat(),
                    failure_category=FailureCategory.INFRASTRUCTURE,
                )
                return None, failure, None
                
            except Exception as e:
                # Unexpected error - treat as infrastructure failure
                if infra_attempt < self.MAX_INFRA_RETRIES:
                    self.log(f"Unexpected preview render error, retrying: {e}", "warning")
                    continue
                    
                failure = PreviewInfrastructureFailure(
                    task_id=task_id,
                    preview_id=preview_id,
                    attempt_number=attempt_number,
                    error_type="unexpected_error",
                    message=str(e),
                    retryable=False,
                    occurred_at=datetime.datetime.now().isoformat(),
                    failure_category=FailureCategory.INFRASTRUCTURE,
                )
                return None, failure, None
    
    def _create_preview_directory(
        self, 
        task_id: str, 
        attempt_number: int, 
        preview_id: str
    ) -> Path:
        """Create task-scoped preview directory.
        
        Structure: artifacts/previews/<task_id>/attempt-<n>/<preview_id>/
        """
        base = Path(self.preview_base_dir)
        preview_dir = base / task_id / f"attempt-{attempt_number}" / preview_id
        preview_dir.mkdir(parents=True, exist_ok=True)
        return preview_dir
    
    def _assemble_preview_html(self, frontend_result: FrontendResult, preview_dir: Path, image_artifact: Optional[ImageArtifact] = None) -> Path:
        """Assemble a complete HTML document for browser rendering.
        
        Uses the most production-representative artifact available:
        - If GreenLight conversion produced blocks, those represent WordPress output
        - Otherwise use raw HTML/CSS/JS from FrontendAgent
        
        The HTML document includes:
        - Valid HTML5 structure
        - UTF-8 encoding
        - Generated CSS (inlined in <style>)
        - Generated JS (inlined in <script>) if present
        - Frontend content
        - Optional hero image from READY ImageArtifact
        - Responsive viewport meta tag
        - No external dependencies (security gate already passed)
        """
        html = frontend_result.html or ""
        css = frontend_result.css or ""
        javascript = frontend_result.javascript or ""
        blocks = frontend_result.blocks or ""
        
        # Prefer converted blocks (WordPress-representative) if available
        if blocks and blocks.strip():
            content_html = blocks
        else:
            content_html = html
            
        # Inject hero image from READY ImageArtifact if available
        hero_markup = ""
        if image_artifact and self._is_ready_image_artifact(image_artifact):
            hero_markup = self._build_hero_image_markup(image_artifact)
        
        # Build complete HTML document
        document = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Preview</title>
    <style>
{css}
    </style>
</head>
<body>
{hero_markup}
{content_html}
    <script>
{javascript}
    </script>
</body>
</html>"""
        
        html_path = preview_dir / "preview.html"
        html_path.write_text(document, encoding='utf-8')
        return html_path
    
    def _is_ready_image_artifact(self, image_artifact: ImageArtifact) -> bool:
        status = image_artifact.status.value if isinstance(image_artifact.status, ImageArtifactStatus) else image_artifact.status
        return status == ImageArtifactStatus.READY.value

    def _resolve_image_source(self, image_artifact: ImageArtifact) -> Optional[str]:
        if image_artifact.local_path:
            local_path = Path(image_artifact.local_path).expanduser()
            if local_path.is_file():
                return local_path.resolve().as_uri()

        if image_artifact.wordpress_media_url:
            return image_artifact.wordpress_media_url

        if image_artifact.source_url:
            return image_artifact.source_url

        return None

    def _build_hero_image_markup(self, image_artifact: ImageArtifact) -> str:
        """Build deterministic preview-only hero image markup from READY ImageArtifact."""
        image_src = self._resolve_image_source(image_artifact)
        if not image_src:
            return ""

        artifact_id = escape(str(image_artifact.artifact_id), quote=True)
        image_src = escape(image_src, quote=True)
        alt_text = escape(image_artifact.alt_text or "", quote=True)

        return f'''<figure data-preview-hero-image="true" data-image-artifact-id="{artifact_id}">
    <img src="{image_src}" alt="{alt_text}" style="max-width: 100%; height: auto;">
</figure>'''
    
    def _render_with_browser(
        self,
        task_id: str,
        preview_id: str,
        attempt_number: int,
        html_path: Path,
        preview_dir: Path,
        image_artifact: Optional[ImageArtifact] = None,
    ) -> Tuple[PreviewArtifact, RenderedEvidence]:
        """Render HTML in browser, capture four screenshots, and collect rendered evidence.
        
        Uses Playwright synchronous API for browser interaction.
        Returns (PreviewArtifact, RenderedEvidence) on success.
        Raises PreviewRenderError on infrastructure failure.
        """
        from playwright.sync_api import sync_playwright
        
        # Screenshot paths
        desktop_viewport_path = preview_dir / "desktop-viewport.png"
        desktop_full_path = preview_dir / "desktop-full.png"
        mobile_viewport_path = preview_dir / "mobile-viewport.png"
        mobile_full_path = preview_dir / "mobile-full.png"
        
        # Evidence collection containers (page-level, not per-viewport)
        page_errors = []
        console_errors = []
        
        # JavaScript to collect per-viewport evidence
        evidence_js = """() => {
            const doc = document.documentElement;
            const images = Array.from(document.querySelectorAll('img'));
            const image_states = images.map(img => ({
                src: img.currentSrc || img.src || '',
                complete: img.complete,
                natural_width: img.naturalWidth,
                natural_height: img.naturalHeight
            }));
            
            const target_selectors = ['main', 'section', 'article', 'button', 'a[href]', 'input[type="submit"]', 'img'];
            const element_bboxes = [];
            for (const selector of target_selectors) {
                const elem = document.querySelector(selector);
                if (elem) {
                    const box = elem.getBoundingClientRect();
                    if (elem.ownerDocument === document) {
                        const cs = window.getComputedStyle(elem);
                        element_bboxes.push({
                            selector: selector,
                            tag: elem.tagName.toLowerCase(),
                            width: Math.round(box.width),
                            height: Math.round(box.height),
                            scroll_width: elem.scrollWidth,
                            client_width: elem.clientWidth,
                            scroll_height: elem.scrollHeight,
                            client_height: elem.clientHeight,
                            overflow_x: cs.overflowX,
                            overflow_y: cs.overflowY,
                            visibility: cs.visibility,
                            display: cs.display
                        });
                    }
                }
            }
            
            return {
                document_scroll_width: doc.scrollWidth,
                document_client_width: doc.clientWidth,
                document_scroll_height: doc.scrollHeight,
                document_client_height: doc.clientHeight,
                image_states: image_states,
                element_bounding_boxes: element_bboxes
            };
        }"""
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.browser_headless)
                context = browser.new_context()
                
                try:
                    page = context.new_page()
                    page.set_default_timeout(self.page_load_timeout_ms)
                    
                    # Set up page error listener (JS exceptions)
                    def on_page_error(exc):
                        page_errors.append({
                            "type": "pageerror",
                            "message": str(exc),
                            "location": {"url": "", "line": 0, "column": 0},
                        })
                    page.on("pageerror", on_page_error)
                    
                    # Set up console error listener (error-level console messages only)
                    def on_console(msg):
                        if msg.type == "error":
                            console_errors.append({
                                "type": "console_error",
                                "message": msg.text,
                                "location": {
                                    "url": msg.location.get("url", "") if msg.location else "",
                                    "line": msg.location.get("line", 0) if msg.location else 0,
                                    "column": msg.location.get("column", 0) if msg.location else 0,
                                },
                            })
                    page.on("console", on_console)
                    
                    # Load the local HTML file
                    file_url = html_path.resolve().as_uri()
                    page.goto(file_url, wait_until="networkidle", timeout=self.page_load_timeout_ms)
                    
                    # Wait for any initial rendering
                    page.wait_for_load_state("domcontentloaded")
                    
                    # === DESKTOP (1440x900) ===
                    page.set_viewport_size({"width": self.DESKTOP_WIDTH, "height": self.DESKTOP_HEIGHT})
                    page.wait_for_timeout(500)  # Allow layout to settle
                    
                    # Desktop viewport screenshot
                    page.screenshot(path=str(desktop_viewport_path), full_page=False)
                    
                    # Desktop full-page screenshot
                    page.screenshot(path=str(desktop_full_path), full_page=True)
                    
                    # Collect desktop evidence after screenshots
                    desktop_data = page.evaluate(evidence_js)
                    
                    # === MOBILE (390x844) ===
                    page.set_viewport_size({"width": self.MOBILE_WIDTH, "height": self.MOBILE_HEIGHT})
                    page.wait_for_timeout(500)  # Allow layout to settle
                    
                    # Mobile viewport screenshot
                    page.screenshot(path=str(mobile_viewport_path), full_page=False)
                    
                    # Mobile full-page screenshot
                    page.screenshot(path=str(mobile_full_path), full_page=True)
                    
                    # Collect mobile evidence after screenshots
                    mobile_data = page.evaluate(evidence_js)
                    
                finally:
                    context.close()
                    browser.close()
        
        except PreviewRenderError:
            raise
        except Exception as e:
            # Wrap any browser/playwright exception as infrastructure failure
            raise PreviewRenderError(
                error_type="browser_launch_failed",
                message=f"Browser rendering failed: {e}",
                retryable=True
            )
        
        # Verify all screenshots were created and are non-empty
        for path in [desktop_viewport_path, desktop_full_path, mobile_viewport_path, mobile_full_path]:
            if not path.exists() or path.stat().st_size == 0:
                raise PreviewRenderError(
                    error_type="screenshot_capture_failed",
                    message=f"Screenshot not created or empty: {path}",
                    retryable=True
                )
        
        # Determine image_artifact_id for traceability
        image_artifact_id = None
        if image_artifact and self._is_ready_image_artifact(image_artifact):
            image_artifact_id = image_artifact.artifact_id
        
        # Create PreviewArtifact
        artifact = PreviewArtifact(
            task_id=task_id,
            preview_id=preview_id,
            attempt_number=attempt_number,
            desktop_viewport_screenshot_path=str(desktop_viewport_path),
            desktop_full_page_screenshot_path=str(desktop_full_path),
            mobile_viewport_screenshot_path=str(mobile_viewport_path),
            mobile_full_page_screenshot_path=str(mobile_full_path),
            desktop_viewport=PreviewViewport(width=self.DESKTOP_WIDTH, height=self.DESKTOP_HEIGHT),
            mobile_viewport=PreviewViewport(width=self.MOBILE_WIDTH, height=self.MOBILE_HEIGHT),
            created_at=datetime.datetime.now().isoformat(),
            image_artifact_id=image_artifact_id,
        )
        
        # Build RenderedEvidence from collected browser data
        desktop_evidence = ViewportRenderedEvidence(
            viewport_width=self.DESKTOP_WIDTH,
            viewport_height=self.DESKTOP_HEIGHT,
            document_scroll_width=desktop_data.get("document_scroll_width", 0),
            document_client_width=desktop_data.get("document_client_width", 0),
            document_scroll_height=desktop_data.get("document_scroll_height", 0),
            document_client_height=desktop_data.get("document_client_height", 0),
            console_errors=list(console_errors),
            page_errors=list(page_errors),
            image_load_states=desktop_data.get("image_states", []),
            element_bounding_boxes=desktop_data.get("element_bounding_boxes", []),
        )
        
        mobile_evidence = ViewportRenderedEvidence(
            viewport_width=self.MOBILE_WIDTH,
            viewport_height=self.MOBILE_HEIGHT,
            document_scroll_width=mobile_data.get("document_scroll_width", 0),
            document_client_width=mobile_data.get("document_client_width", 0),
            document_scroll_height=mobile_data.get("document_scroll_height", 0),
            document_client_height=mobile_data.get("document_client_height", 0),
            console_errors=list(console_errors),
            page_errors=list(page_errors),
            image_load_states=mobile_data.get("image_states", []),
            element_bounding_boxes=mobile_data.get("element_bounding_boxes", []),
        )
        
        evidence = RenderedEvidence(
            task_id=task_id,
            preview_id=preview_id,
            attempt_number=attempt_number,
            desktop=desktop_evidence,
            mobile=mobile_evidence,
            created_at=datetime.datetime.now().isoformat(),
        )
        
        return artifact, evidence


def render_preview(
    task_id: str,
    frontend_result: FrontendResult,
    attempt_number: int,
    config=None,
    preview_id: Optional[str] = None,
    image_artifact: Optional[ImageArtifact] = None,
) -> Tuple[Optional[PreviewArtifact], Optional[PreviewInfrastructureFailure], Optional[RenderedEvidence]]:
    """Convenience function to render a preview."""
    renderer = PreviewRenderer(config or type('Config', (), {})())
    return renderer.render(task_id, frontend_result, attempt_number, preview_id, image_artifact)