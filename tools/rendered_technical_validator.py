"""RenderedTechnicalValidator: Deterministic technical validation of rendered evidence.

Analyzes RenderedEvidence collected by PreviewRenderer and produces
RenderedTechnicalResult with ERROR and WARNING diagnostics.

This validator answers:
    "Did the rendered page exhibit deterministic technical defects?"

It does NOT answer:
    "Does the page look good?"

Frozen v1 policy (Phase 7D-2B):
    ERROR:   document horizontal overflow, broken <img>, page runtime errors
    WARNING: element overflow, zero-size visible interactive elements, text clipping
"""

from typing import Any, Dict, List, Optional

from contracts import (
    RenderedEvidence,
    RenderedTechnicalGate,
    RenderedTechnicalResult,
    RenderedTechnicalSeverity,
    ViewportRenderedEvidence,
)
from . import BaseTool


HORIZONTAL_OVERFLOW_TOLERANCE_PX = 2


class RenderedTechnicalValidator(BaseTool):
    """Deterministic, LLM-free validator for browser-rendered evidence.

    Reads RenderedEvidence and produces RenderedTechnicalResult.
    Does not launch browsers, mutate Task, or call external APIs.
    """

    def __init__(self, config=None):
        super().__init__(config)
        self.description = "Deterministic technical validation of browser-rendered evidence."

    def validate(self, evidence: RenderedEvidence) -> RenderedTechnicalResult:
        """Validate RenderedEvidence and return a RenderedTechnicalResult.

        Args:
            evidence: RenderedEvidence from PreviewRenderer.

        Returns:
            RenderedTechnicalResult with errors, warnings, and diagnostics.
        """
        errors: List[Dict[str, Any]] = []
        warnings: List[ Dict[str, Any]] = []
        diagnostics: Dict[str, Any] = {}

        for viewport_name, viewport_evidence in (
            ("desktop", evidence.desktop),
            ("mobile", evidence.mobile),
        ):
            vp_errors, vp_warnings = self._validate_viewport(viewport_name, viewport_evidence)
            errors.extend(vp_errors)
            warnings.extend(vp_warnings)

        diagnostics["total_errors"] = len(errors)
        diagnostics["total_warnings"] = len(warnings)
        diagnostics["viewports_checked"] = ["desktop", "mobile"]

        passed = len(errors) == 0
        has_warnings = len(warnings) > 0

        if passed and not has_warnings:
            validation_status = "passed"
        elif passed and has_warnings:
            validation_status = "warnings"
        else:
            validation_status = "failed"

        return RenderedTechnicalResult(
            task_id=evidence.task_id,
            preview_id=evidence.preview_id,
            attempt_number=evidence.attempt_number,
            passed=passed,
            validation_status=validation_status,
            errors=errors,
            warnings=warnings,
            diagnostics=diagnostics,
            created_at=evidence.created_at,
        )

    def _validate_viewport(
        self, viewport_name: str, viewport_evidence: ViewportRenderedEvidence
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Run all 6 checks for a single viewport.

        Returns (errors, warnings).
        """
        errors: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []

        checks = [
            self._check_horizontal_overflow,
            self._check_broken_images,
            self._check_page_errors,
            self._check_console_errors,
            self._check_element_overflow,
            self._check_zero_size_interactive,
            self._check_text_clipping,
        ]

        for check in checks:
            check(viewport_name, viewport_evidence, errors, warnings)

        return errors, warnings

    def _add_diagnostic(self, diagnostics: List[Dict[str, Any]], viewport: str, dtype: str,
                        severity: str, message: str, context: Optional[Dict[str, Any]] = None,
                        location: Optional[Dict[str, Any]] = None) -> None:
        """Helper to append a structured diagnostic."""
        diag: Dict[str, Any] = {
            "gate": RenderedTechnicalGate.RENDERED_TECHNICAL.value,
            "viewport": viewport,
            "type": dtype,
            "severity": severity,
            "message": message,
        }
        if context is not None:
            diag["context"] = context
        if location is not None:
            diag["location"] = location
        diagnostics.append(diag)

    # === CHECK 1: Document Horizontal Overflow ===
    def _check_horizontal_overflow(self, viewport: str, evidence: ViewportRenderedEvidence,
                                    errors: List, warnings: List) -> None:
        scroll_w = evidence.document_scroll_width
        client_w = evidence.document_client_width
        if scroll_w > client_w + HORIZONTAL_OVERFLOW_TOLERANCE_PX:
            overflow_px = scroll_w - client_w
            errors.append({
                "gate": RenderedTechnicalGate.RENDERED_TECHNICAL.value,
                "viewport": viewport,
                "type": "document_horizontal_overflow",
                "severity": RenderedTechnicalSeverity.ERROR.value,
                "message": (
                    f"Document horizontal overflow detected: scrollWidth "
                    f"({scroll_w}px) exceeds clientWidth ({client_w}px) "
                    f"by {overflow_px}px (tolerance={HORIZONTAL_OVERFLOW_TOLERANCE_PX}px)"
                ),
                "context": {
                    "viewport": viewport,
                    "scroll_width": scroll_w,
                    "client_width": client_w,
                    "overflow_px": overflow_px,
                    "tolerance_px": HORIZONTAL_OVERFLOW_TOLERANCE_PX,
                },
                "location": {"line": 0, "column": 0},
            })

    # === CHECK 2: Broken Images ===
    def _check_broken_images(self, viewport: str, evidence: ViewportRenderedEvidence,
                              errors: List, warnings: List) -> None:
        for img in evidence.image_load_states:
            complete = img.get("complete", False)
            natural_width = img.get("natural_width", 0)
            natural_height = img.get("natural_height", 0)

            if complete and natural_width == 0:
                src = img.get("src", "") or "<unknown>"
                errors.append({
                    "gate": RenderedTechnicalGate.RENDERED_TECHNICAL.value,
                    "viewport": viewport,
                    "type": "broken_image",
                    "severity": RenderedTechnicalSeverity.ERROR.value,
                    "message": f"Broken image: resource at '{src}' loaded but has zero natural dimensions",
                    "context": {
                        "viewport": viewport,
                        "src": src,
                        "complete": complete,
                        "natural_width": natural_width,
                        "natural_height": natural_height,
                    },
                    "location": {"line": 0, "column": 0},
                })

    # === CHECK 3: Page Runtime Errors ===
    def _check_page_errors(self, viewport: str, evidence: ViewportRenderedEvidence,
                            errors: List, warnings: List) -> None:
        for page_err in evidence.page_errors:
            message = page_err.get("message", "")
            location = page_err.get("location", {})
            errors.append({
                "gate": RenderedTechnicalGate.RENDERED_TECHNICAL.value,
                "viewport": viewport,
                "type": "page_runtime_error",
                "severity": RenderedTechnicalSeverity.ERROR.value,
                "message": f"Page runtime error: {message}",
                "context": {
                    "viewport": viewport,
                    "message": message,
                    "location": location,
                },
                "location": {
                    "line": location.get("line", 0) if location else 0,
                    "column": location.get("column", 0) if location else 0,
                },
            })

    # === CHECK 4: Console Errors (WARNING per frozen v1 policy) ===
    def _check_console_errors(self, viewport: str, evidence: ViewportRenderedEvidence,
                               errors: List, warnings: List) -> None:
        for console_err in evidence.console_errors:
            message = console_err.get("message", "")
            location = console_err.get("location", {})
            warnings.append({
                "gate": RenderedTechnicalGate.RENDERED_TECHNICAL.value,
                "viewport": viewport,
                "type": "console_error",
                "severity": RenderedTechnicalSeverity.WARNING.value,
                "message": f"Console error: {message}",
                "context": {
                    "viewport": viewport,
                    "message": message,
                    "location": location,
                },
                "location": {
                    "line": location.get("line", 0) if location else 0,
                    "column": location.get("column", 0) if location else 0,
                },
            })

    # === CHECK 5: Element Overflow (WARNING, conservative) ===
    def _check_element_overflow(self, viewport: str, evidence: ViewportRenderedEvidence,
                                 errors: List, warnings: List) -> None:
        seen = set()
        for elem in evidence.element_bounding_boxes:
            scroll_w = elem.get("scroll_width", 0)
            client_w = elem.get("client_width", 0)
            scroll_h = elem.get("scroll_height", 0)
            client_h = elem.get("client_height", 0)
            overflow_x = elem.get("overflow_x", "")
            overflow_y = elem.get("overflow_y", "")

            has_overflow = (
                scroll_w > client_w + HORIZONTAL_OVERFLOW_TOLERANCE_PX
                or scroll_h > client_h + HORIZONTAL_OVERFLOW_TOLERANCE_PX
            )

            if not has_overflow:
                continue

            visible_overflow = overflow_x in ("hidden", "clip", "auto", "scroll") or \
                overflow_y in ("hidden", "clip", "auto", "scroll")

            if not visible_overflow:
                continue

            key = (viewport, elem.get("selector", ""), elem.get("tag", ""))
            if key in seen:
                continue
            seen.add(key)

            warnings.append({
                "gate": RenderedTechnicalGate.RENDERED_TECHNICAL.value,
                "viewport": viewport,
                "type": "element_overflow",
                "severity": RenderedTechnicalSeverity.WARNING.value,
                "message": (
                    f"Element '{elem.get('tag', '?')}' ({elem.get('selector', '?')}) has "
                    f"content overflow: scrollWidth={scroll_w}, clientWidth={client_w}, "
                    f"scrollHeight={scroll_h}, clientHeight={client_h}"
                ),
                "context": {
                    "viewport": viewport,
                    "selector": elem.get("selector", ""),
                    "tag": elem.get("tag", ""),
                    "width": elem.get("width", 0),
                    "height": elem.get("height", 0),
                    "scroll_width": scroll_w,
                    "client_width": client_w,
                    "scroll_height": scroll_h,
                    "client_height": client_h,
                    "overflow_x": overflow_x,
                    "overflow_y": overflow_y,
                },
                "location": {"line": 0, "column": 0},
            })

    # === CHECK 6: Zero-Size Visible Interactive Elements (WARNING) ===
    def _check_zero_size_interactive(self, viewport: str, evidence: ViewportRenderedEvidence,
                                      errors: List, warnings: List) -> None:
        interactive_tags = {"button", "a", "input"}
        for elem in evidence.element_bounding_boxes:
            tag = elem.get("tag", "")
            if tag not in interactive_tags:
                continue

            display = elem.get("display", "")
            visibility = elem.get("visibility", "")
            if display == "none" or visibility == "hidden":
                continue

            width = elem.get("width", 0)
            height = elem.get("height", 0)
            if width <= 0 or height <= 0:
                warnings.append({
                    "gate": RenderedTechnicalGate.RENDERED_TECHNICAL.value,
                    "viewport": viewport,
                    "type": "zero_size_interactive",
                    "severity": RenderedTechnicalSeverity.WARNING.value,
                    "message": (
                        f"Visible interactive element '{tag}' ({elem.get('selector', '?')}) "
                        f"has zero size: width={width}px, height={height}px"
                    ),
                    "context": {
                        "viewport": viewport,
                        "selector": elem.get("selector", ""),
                        "tag": tag,
                        "width": width,
                        "height": height,
                        "display": display,
                        "visibility": visibility,
                    },
                    "location": {"line": 0, "column": 0},
                })

    # === CHECK 7: Text Clipping Indicator (WARNING, conservative) ===
    def _check_text_clipping(self, viewport: str, evidence: ViewportRenderedEvidence,
                              errors: List, warnings: List) -> None:
        text_bearing_tags = {"main", "section", "article", "button", "a"}
        seen = set()
        for elem in evidence.element_bounding_boxes:
            tag = elem.get("tag", "")
            if tag not in text_bearing_tags:
                continue

            scroll_w = elem.get("scroll_width", 0)
            client_w = elem.get("client_width", 0)
            scroll_h = elem.get("scroll_height", 0)
            client_h = elem.get("client_height", 0)
            overflow_x = elem.get("overflow_x", "")
            overflow_y = elem.get("overflow_y", "")

            has_overflow = (
                scroll_w > client_w + HORIZONTAL_OVERFLOW_TOLERANCE_PX
                or scroll_h > client_h + HORIZONTAL_OVERFLOW_TOLERANCE_PX
            )
            if not has_overflow:
                continue

            clipping_signal = overflow_x in ("hidden", "clip") or \
                overflow_y in ("hidden", "clip")
            if not clipping_signal:
                continue

            key = (viewport, elem.get("selector", ""), tag)
            if key in seen:
                continue
            seen.add(key)

            warnings.append({
                "gate": RenderedTechnicalGate.RENDERED_TECHNICAL.value,
                "viewport": viewport,
                "type": "possible_text_clipping",
                "severity": RenderedTechnicalSeverity.WARNING.value,
                "message": (
                    f"Possible text clipping detected for '{tag}' "
                    f"({elem.get('selector', '?')}): element content overflows "
                    f"with overflow set to hidden/clip"
                ),
                "context": {
                    "viewport": viewport,
                    "selector": elem.get("selector", ""),
                    "tag": tag,
                    "scroll_width": scroll_w,
                    "client_width": client_w,
                    "scroll_height": scroll_h,
                    "client_height": client_h,
                    "overflow_x": overflow_x,
                    "overflow_y": overflow_y,
                },
                "location": {"line": 0, "column": 0},
            })
