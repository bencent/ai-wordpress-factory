import json
import os
import re
import subprocess
import tempfile
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from contracts import FrontendValidationResult, FrontendResult
from . import BaseTool


class _HTMLValidationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tag_stack: List[str] = []
        self.errors: List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []
        self.pos = 0
        self._html = ""

    def set_html(self, html: str):
        self._html = html
        self.pos = 0

    def handle_starttag(self, tag, attrs):
        self.tag_stack.append(tag)
        self.pos = self.getpos()[1] if hasattr(self, 'getpos') else 0

    def handle_endtag(self, tag):
        if not self.tag_stack:
            self.errors.append({
                "gate": "validation",
                "type": "html_structure",
                "severity": "error",
                "message": f"Unexpected closing tag: </{tag}>",
                "location": self._get_location(),
                "context": f"Tag stack: {self.tag_stack[-5:] if len(self.tag_stack) > 5 else self.tag_stack}"
            })
            return
        if self.tag_stack[-1] != tag:
            self.errors.append({
                "gate": "validation",
                "type": "html_structure",
                "severity": "error",
                "message": f"Unclosed tag: <{self.tag_stack[-1]}> (closed by </{tag}>)",
                "location": self._get_location(),
                "context": f"Expected </{self.tag_stack[-1]}>, got </{tag}>"
            })
        else:
            self.tag_stack.pop()

    def handle_startendtag(self, tag, attrs):
        pass

    def _get_location(self) -> Dict[str, int]:
        try:
            line, col = self.getpos()
            return {"line": line, "column": col}
        except Exception:
            return {"line": 0, "column": 0}

    def get_errors(self) -> List[Dict[str, Any]]:
        errors = list(self.errors)
        for unclosed in self.tag_stack:
            errors.append({
                "gate": "validation",
                "type": "html_structure",
                "severity": "error",
                "message": f"Unclosed tag: <{unclosed}>",
                "location": self._get_location(),
                "context": f"Tag was opened but never closed"
            })
        return errors

    def get_warnings(self) -> List[Dict[str, Any]]:
        return list(self.warnings)


class FrontendValidator(BaseTool):
    def __init__(self, config):
        super().__init__(config)
        self.description = "Deterministic technical validation for frontend output after GreenLight conversion."

    def validate(self, result: FrontendResult) -> FrontendValidationResult:
        errors: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        diagnostics: Dict[str, Any] = {}

        html = result.html or ""
        css = result.css or ""
        javascript = result.javascript or ""
        blocks = result.blocks or ""

        diagnostics["html_size"] = len(html.encode('utf-8'))
        diagnostics["css_size"] = len(css.encode('utf-8'))
        diagnostics["js_size"] = len(javascript.encode('utf-8'))
        diagnostics["blocks_size"] = len(blocks.encode('utf-8'))
        diagnostics["has_html"] = bool(html.strip())
        diagnostics["has_css"] = bool(css.strip())
        diagnostics["has_js"] = bool(javascript.strip())
        diagnostics["has_blocks"] = bool(blocks.strip())

        html_errors = self._validate_html(html)
        errors.extend(html_errors)

        css_errors, css_warnings = self._validate_css(css)
        errors.extend(css_errors)
        warnings.extend(css_warnings)

        js_errors = self._validate_javascript(javascript)
        errors.extend(js_errors)

        block_errors, block_warnings = self._validate_blocks(blocks)
        errors.extend(block_errors)
        warnings.extend(block_warnings)

        validator_errors = [e for e in errors if e.get("validator_error")]
        execution_errors = [e for e in errors if not e.get("validator_error")]

        passed = len(execution_errors) == 0
        has_warnings = any(w.get("severity") in ("warning", "error") for w in warnings)
        validation_status = "passed" if passed and not has_warnings else ("warnings" if passed and has_warnings else "failed")

        return FrontendValidationResult(
            task_id=result.task_id,
            passed=passed,
            validation_status=validation_status,
            errors=errors,
            warnings=warnings,
            diagnostics=diagnostics,
            validator_error=len(validator_errors) > 0,
        )

    def _validate_html(self, html: str) -> List[Dict[str, Any]]:
        errors: List[Dict[str, Any]] = []

        if not html or not html.strip():
            errors.append({
                "gate": "validation",
                "type": "html_empty",
                "severity": "error",
                "message": "HTML output is empty",
                "location": {"line": 0, "column": 0},
                "context": "FrontendAgent produced no HTML content"
            })
            return errors

        parser = _HTMLValidationParser()
        parser.set_html(html)
        try:
            parser.feed(html)
        except Exception as e:
            errors.append({
                "gate": "validation",
                "type": "html_parse_error",
                "severity": "error",
                "message": f"HTML parsing failed: {e}",
                "location": {"line": 0, "column": 0},
                "context": "Parser exception during validation"
            })
            return errors

        errors.extend(parser.get_errors())
        return errors

    def _validate_css(self, css: str) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        errors: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []

        if not css or not css.strip():
            warnings.append({
                "gate": "validation",
                "type": "css_empty",
                "severity": "warning",
                "message": "CSS output is empty",
                "location": {"line": 0, "column": 0},
                "context": "No CSS generated; may be intentional for minimal pages"
            })
            return errors, warnings

        open_braces = 0
        line_num = 1
        for i, ch in enumerate(css):
            if ch == '\n':
                line_num += 1
            elif ch == '{':
                open_braces += 1
            elif ch == '}':
                open_braces -= 1
                if open_braces < 0:
                    errors.append({
                        "gate": "validation",
                        "type": "css_syntax",
                        "severity": "error",
                        "message": "Unmatched closing brace '}' in CSS",
                        "location": {"line": line_num, "column": i},
                        "context": "More closing braces than opening braces"
                    })
                    open_braces = 0

        if open_braces > 0:
            errors.append({
                "gate": "validation",
                "type": "css_syntax",
                "severity": "error",
                "message": f"Unclosed CSS rule: {open_braces} unmatched opening brace(s) '{{\"",
                "location": {"line": line_num, "column": len(css)},
                "context": "CSS rule started but not closed with '}'"
            })

        return errors, warnings

    def _validate_javascript(self, js: str) -> List[Dict[str, Any]]:
        errors: List[Dict[str, Any]] = []

        if not js or not js.strip():
            return errors

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False, encoding='utf-8') as f:
                f.write(js)
                temp_path = f.name

            result = subprocess.run(
                ["node", "--check", temp_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                errors.append({
                    "gate": "validation",
                    "type": "js_syntax",
                    "severity": "error",
                    "message": f"JavaScript syntax error: {stderr}",
                    "location": {"line": 0, "column": 0},
                    "context": "Node.js --check reported syntax error"
                })
        except FileNotFoundError:
            errors.append({
                "gate": "validation",
                "type": "js_syntax",
                "severity": "error",
                "message": "Node.js not available for syntax validation",
                "location": {"line": 0, "column": 0},
                "context": "Validator requires Node.js for JS syntax check",
                "validator_error": True
            })
        except subprocess.TimeoutExpired:
            errors.append({
                "gate": "validation",
                "type": "js_syntax",
                "severity": "error",
                "message": "JavaScript syntax check timed out",
                "location": {"line": 0, "column": 0},
                "context": "Validator timeout after 10 seconds",
                "validator_error": True
            })
        except Exception as e:
            errors.append({
                "gate": "validation",
                "type": "js_syntax",
                "severity": "error",
                "message": f"JavaScript validation failed: {e}",
                "location": {"line": 0, "column": 0},
                "context": "Validator execution error",
                "validator_error": True
            })
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

        return errors

    def _validate_blocks(self, blocks: str) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        errors: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []

        if not blocks or not blocks.strip():
            errors.append({
                "gate": "validation",
                "type": "blocks_empty",
                "severity": "error",
                "message": "GreenLight conversion produced empty blocks output",
                "location": {"line": 0, "column": 0},
                "context": "Converter succeeded but returned no block markup"
            })
            return errors, warnings

        if not re.search(r'<!--\s*wp:', blocks):
            errors.append({
                "gate": "validation",
                "type": "blocks_format",
                "severity": "error",
                "message": "Blocks output does not contain WordPress block comments",
                "location": {"line": 0, "column": 0},
                "context": "Expected format: <!-- wp:block-name {...} -->...<!-- /wp:block-name -->"
            })
            return errors, warnings

        block_openings = len(re.findall(r'<!--\s*wp:([a-zA-Z0-9/_-]+)\s', blocks))
        block_closings = len(re.findall(r'<!--\s*/wp:([a-zA-Z0-9/_-]+)\s*-->', blocks))

        if block_openings != block_closings:
            errors.append({
                "gate": "validation",
                "type": "blocks_format",
                "severity": "error",
                "message": f"Unbalanced block comments: {block_openings} openings, {block_closings} closings",
                "location": {"line": 0, "column": 0},
                "context": "Each block opening must have a matching closing comment"
            })

        json_blocks = re.findall(r'<!--\s*wp:[a-zA-Z0-9/_-]+\s+([^>]+)\s*-->', blocks)
        for i, json_str in enumerate(json_blocks):
            try:
                json.loads(json_str)
            except json.JSONDecodeError as e:
                errors.append({
                    "gate": "validation",
                    "type": "blocks_json",
                    "severity": "error",
                    "message": f"Invalid JSON in block attributes (block {i+1}): {e}",
                    "location": {"line": 0, "column": 0},
                    "context": f"Block attributes must be valid JSON: {json_str[:100]}..."
                })

        if not errors:
            warnings.append({
                "gate": "validation",
                "type": "blocks_format",
                "severity": "info",
                "message": f"Blocks validation passed: {block_openings} block(s) verified",
                "location": {"line": 0, "column": 0},
                "context": "All block comments balanced and JSON attributes valid"
            })

        return errors, warnings