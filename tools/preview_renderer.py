"""PreviewRenderer: Browser-based frontend preview rendering tool.

Renders validated frontend artifacts in a real browser (Chromium via Playwright)
and captures four screenshots per preview:
- Desktop viewport (1440x900)
- Desktop full-page
- Mobile viewport (390x844)
- Mobile full-page

This is a deterministic infrastructure tool - it does NOT judge visual quality,
call LLMs, or make routing decisions.
"""

import os
import uuid
import datetime
import tempfile
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum

from contracts import (
    PreviewArtifact, PreviewViewport, PreviewInfrastructureFailure,
    FailureCategory, FrontendResult
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
    ) -> Tuple[Optional[PreviewArtifact], Optional[PreviewInfrastructureFailure]]:
        """Render frontend artifact and capture preview screenshots.
        
        Args:
            task_id: Task identifier
            frontend_result: Validated frontend result (after production quality gate)
            attempt_number: Frontend generation attempt number (1-indexed)
            preview_id: Optional preview ID (generated if not provided)
            
        Returns:
            Tuple of (PreviewArtifact on success, PreviewInfrastructureFailure on failure)
            Exactly one will be non-None.
        """
        if preview_id is None:
            preview_id = str(uuid.uuid4())[:8]
            
        # Create task-scoped preview directory
        preview_dir = self._create_preview_directory(task_id, attempt_number, preview_id)
        
        # Assemble temporary HTML document
        html_path = self._assemble_preview_html(frontend_result, preview_dir)
        
        # Render with browser (with infrastructure retry)
        for infra_attempt in range(self.MAX_INFRA_RETRIES + 1):
            try:
                artifact = self._render_with_browser(
                    task_id=task_id,
                    preview_id=preview_id,
                    attempt_number=attempt_number,
                    html_path=html_path,
                    preview_dir=preview_dir,
                )
                return artifact, None
                
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
                return None, failure
                
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
                return None, failure
    
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
    
    def _assemble_preview_html(self, frontend_result: FrontendResult, preview_dir: Path) -> Path:
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
{content_html}
    <script>
{javascript}
    </script>
</body>
</html>"""
        
        html_path = preview_dir / "preview.html"
        html_path.write_text(document, encoding='utf-8')
        return html_path
    
    def _render_with_browser(
        self,
        task_id: str,
        preview_id: str,
        attempt_number: int,
        html_path: Path,
        preview_dir: Path,
    ) -> PreviewArtifact:
        """Render HTML in browser and capture four screenshots.
        
        Uses Playwright synchronous API for simplicity.
        """
        from playwright.sync_api import sync_playwright
        
        # Screenshot paths
        desktop_viewport_path = preview_dir / "desktop-viewport.png"
        desktop_full_path = preview_dir / "desktop-full.png"
        mobile_viewport_path = preview_dir / "mobile-viewport.png"
        mobile_full_path = preview_dir / "mobile-full.png"
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.browser_headless)
                context = browser.new_context()
                
                try:
                    page = context.new_page()
                    page.set_default_timeout(self.page_load_timeout_ms)
                    
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
                    
                    # === MOBILE (390x844) ===
                    page.set_viewport_size({"width": self.MOBILE_WIDTH, "height": self.MOBILE_HEIGHT})
                    page.wait_for_timeout(500)  # Allow layout to settle
                    
                    # Mobile viewport screenshot
                    page.screenshot(path=str(mobile_viewport_path), full_page=False)
                    
                    # Mobile full-page screenshot
                    page.screenshot(path=str(mobile_full_path), full_page=True)
                    
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
        )
        
        return artifact


def render_preview(
    task_id: str,
    frontend_result: FrontendResult,
    attempt_number: int,
    config=None,
    preview_id: Optional[str] = None,
) -> Tuple[Optional[PreviewArtifact], Optional[PreviewInfrastructureFailure]]:
    """Convenience function to render a preview."""
    renderer = PreviewRenderer(config or type('Config', (), {})())
    return renderer.render(task_id, frontend_result, attempt_number, preview_id)