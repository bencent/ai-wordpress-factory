import re
from html.parser import HTMLParser
from typing import List
from urllib.parse import urlparse
from contracts import FrontendSecurityResult
from . import BaseTool


class _HTMLSecurityParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.dangerous_tags = []
        self.event_handlers = []
        self.urls = []
        self._dangerous_tags = {'script', 'iframe', 'object', 'embed', 'applet'}
        self._url_attrs = {'href', 'src', 'data', 'cite', 'background', 'action', 'formaction', 'xlink:href'}

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()
        if tag_lower in self._dangerous_tags:
            self.dangerous_tags.append(tag_lower)

        for attr, value in attrs:
            if attr:
                attr_lower = attr.lower()
                if attr_lower.startswith('on') and len(attr_lower) > 2:
                    self.event_handlers.append(f"{tag_lower}[{attr_lower}]")
                if attr_lower in self._url_attrs and value:
                    self.urls.append(value)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


class _SVGSecurityParser(HTMLParser):
    """Parser for inline SVG content. Applies the same security policy as HTML."""
    def __init__(self):
        super().__init__()
        self.dangerous_tags = []
        self.event_handlers = []
        self.urls = []
        self._dangerous_tags = {'script', 'iframe', 'object', 'embed', 'applet', 'foreignObject', 'use'}
        self._url_attrs = {'href', 'src', 'xlink:href', 'xmlns:xlink'}

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()
        if tag_lower in self._dangerous_tags:
            self.dangerous_tags.append(tag_lower)

        for attr, value in attrs:
            if attr:
                attr_lower = attr.lower()
                if attr_lower.startswith('on') and len(attr_lower) > 2:
                    self.event_handlers.append(f"{tag_lower}[{attr_lower}]")
                if attr_lower in self._url_attrs and value:
                    self.urls.append(value)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


class FrontendSecurityGate(BaseTool):
    def __init__(self, config):
        super().__init__(config)
        self.description = "Deterministic security gate for AI-generated frontend output."

        self.allowed_domains = getattr(config, 'allowed_domains', []) or []
        if not self.allowed_domains and getattr(config, 'wordpress_url', None):
            parsed = urlparse(config.wordpress_url)
            if parsed.netloc:
                self.allowed_domains.append(parsed.netloc)

        self.max_html_size = getattr(config, 'frontend_max_html_size', 100 * 1024)
        self.max_css_size = getattr(config, 'frontend_max_css_size', 50 * 1024)
        self.max_js_size = getattr(config, 'frontend_max_js_size', 50 * 1024)
        self.max_total_size = getattr(config, 'frontend_max_total_size', 200 * 1024)

    def check(self, html: str, css: str, javascript: str) -> FrontendSecurityResult:
        errors = []
        warnings = []
        blocked_items = []

        html = html or ""
        css = css or ""
        javascript = javascript or ""

        html_size = len(html.encode('utf-8'))
        css_size = len(css.encode('utf-8'))
        js_size = len(javascript.encode('utf-8'))
        total_size = html_size + css_size + js_size

        if html_size > self.max_html_size:
            errors.append(f"HTML output exceeds size limit: {html_size} bytes > {self.max_html_size} bytes")
            blocked_items.append(f"html:size:{html_size}")

        if css_size > self.max_css_size:
            errors.append(f"CSS output exceeds size limit: {css_size} bytes > {self.max_css_size} bytes")
            blocked_items.append(f"css:size:{css_size}")

        if js_size > self.max_js_size:
            errors.append(f"JavaScript output exceeds size limit: {js_size} bytes > {self.max_js_size} bytes")
            blocked_items.append(f"javascript:size:{js_size}")

        if total_size > self.max_total_size:
            errors.append(f"Total output exceeds size limit: {total_size} bytes > {self.max_total_size} bytes")
            blocked_items.append(f"total:size:{total_size}")

        if errors:
            return FrontendSecurityResult(
                passed=False,
                html=html,
                css=css,
                javascript=javascript,
                errors=errors,
                warnings=warnings,
                blocked_items=blocked_items,
            )

        html_errors, html_blocked = self._check_html(html)
        errors.extend(html_errors)
        blocked_items.extend(html_blocked)

        svg_errors, svg_blocked = self._check_svg(html)
        errors.extend(svg_errors)
        blocked_items.extend(svg_blocked)

        css_errors, css_blocked = self._check_css(css)
        errors.extend(css_errors)
        blocked_items.extend(css_blocked)

        js_errors, js_blocked, js_warnings = self._check_javascript(javascript)
        errors.extend(js_errors)
        blocked_items.extend(js_blocked)
        warnings.extend(js_warnings)

        url_errors, url_blocked = self._check_urls(html, css)
        errors.extend(url_errors)
        blocked_items.extend(url_blocked)

        passed = len(errors) == 0
        return FrontendSecurityResult(
            passed=passed,
            html=html,
            css=css,
            javascript=javascript,
            errors=errors,
            warnings=warnings,
            blocked_items=blocked_items,
        )

    def _check_html(self, html: str):
        errors = []
        blocked = []

        if not html or not html.strip():
            return errors, blocked

        parser = _HTMLSecurityParser()
        try:
            parser.feed(html)
        except Exception as e:
            errors.append(f"HTML parsing error: {e}")
            blocked.append("html:parse_error")
            return errors, blocked

        for tag in parser.dangerous_tags:
            errors.append(f"Dangerous HTML tag detected: <{tag}>")
            blocked.append(f"html:tag:{tag}")

        for handler in parser.event_handlers:
            errors.append(f"Dangerous event handler detected: {handler}")
            blocked.append(f"html:handler:{handler}")

        return errors, blocked

    def _check_svg(self, html: str):
        """Check inline SVG content for security violations.
        
        Inline SVG in HTML is parsed using a dedicated SVG parser that applies
        the same security policy as HTML: reject <script>, event handlers,
        javascript: URLs, vbscript: URLs, and dangerous external references.
        
        Conservative policy: if SVG parsing fails or is uncertain, reject.
        """
        errors = []
        blocked = []

        if not html or not html.strip():
            return errors, blocked

        # Find inline SVG elements
        svg_contents = self._extract_inline_svg(html)
        if not svg_contents:
            return errors, blocked

        for svg_content in svg_contents:
            parser = _SVGSecurityParser()
            try:
                parser.feed(svg_content)
            except Exception as e:
                errors.append(f"SVG parsing error: {e}")
                blocked.append("svg:parse_error")
                continue

            for tag in parser.dangerous_tags:
                errors.append(f"Dangerous SVG tag detected: <{tag}>")
                blocked.append(f"svg:tag:{tag}")

            for handler in parser.event_handlers:
                errors.append(f"Dangerous SVG event handler detected: {handler}")
                blocked.append(f"svg:handler:{handler}")

            for url in parser.urls:
                if not self._is_url_allowed(url):
                    errors.append(f"SVG external resource from non-allowlisted domain or dangerous scheme: {url}")
                    blocked.append(f"svg:url:{url}")

        return errors, blocked

    def _extract_inline_svg(self, html: str) -> List[str]:
        """Extract inline SVG content from HTML.
        
        Uses regex to find <svg>...</svg> blocks. This is a conservative
        extraction - if the HTML is malformed, it may miss some SVG.
        """
        svg_contents = []
        # Find all <svg>...</svg> blocks (including self-closing)
        # Using a simple regex approach - handles nested tags poorly but catches most cases
        for match in re.finditer(r'<svg\b[^>]*>(.*?)</svg>', html, re.IGNORECASE | re.DOTALL):
            svg_contents.append(match.group(1))
        # Also handle self-closing <svg ... /> (no content)
        return svg_contents

    def _check_css(self, css: str):
        errors = []
        blocked = []

        if not css or not css.strip():
            return errors, blocked

        if re.search(r'\bexpression\s*\(', css, re.IGNORECASE):
            errors.append("Dangerous CSS construct detected: expression()")
            blocked.append("css:expression")

        if re.search(r'\bbehavior\s*:', css, re.IGNORECASE):
            errors.append("Dangerous CSS construct detected: behavior:")
            blocked.append("css:behavior")

        if re.search(r'-moz-binding\s*:', css, re.IGNORECASE):
            errors.append("Dangerous CSS construct detected: -moz-binding:")
            blocked.append("css:-moz-binding")

        import_urls = self._extract_css_import_urls(css)
        for url in import_urls:
            if not self._is_url_allowed(url):
                errors.append(f"CSS @import to non-allowlisted domain: {url}")
                blocked.append(f"css:import:{url}")

        css_urls = self._extract_css_urls(css)
        for url in css_urls:
            if not self._is_url_allowed(url):
                errors.append(f"CSS url() to non-allowlisted domain or dangerous scheme: {url}")
                blocked.append(f"css:url:{url}")

        return errors, blocked

    def _check_javascript(self, js: str):
        errors = []
        blocked = []
        warnings = []

        if not js or not js.strip():
            return errors, blocked, warnings

        dangerous_patterns = [
            (r'\beval\s*\(', 'eval()'),
            (r'\bnew\s+Function\s*\(', 'new Function()'),
            (r'\bdocument\.cookie\b', 'document.cookie'),
            (r'\bdocument\.location\b', 'document.location'),
            (r'\bwindow\.location\b', 'window.location'),
            (r'\blocation\.href\b', 'location.href'),
            (r'javascript\s*:', 'javascript: URL'),
            (r'\bdocument\.createElement\s*\(\s*["\']script["\']\s*\)', 'dynamic script creation'),
        ]

        for pattern, name in dangerous_patterns:
            if re.search(pattern, js, re.IGNORECASE):
                errors.append(f"Dangerous JavaScript construct detected: {name}")
                blocked.append(f"javascript:{name}")

        network_patterns = [
            (r'\bfetch\s*\(', 'fetch()'),
            (r'\bXMLHttpRequest\b', 'XMLHttpRequest'),
            (r'\bWebSocket\b', 'WebSocket'),
        ]

        for pattern, name in network_patterns:
            if re.search(pattern, js, re.IGNORECASE):
                errors.append(f"Unrestricted network access detected: {name}")
                blocked.append(f"javascript:network:{name}")

        return errors, blocked, warnings

    def _extract_css_urls(self, css: str) -> List[str]:
        urls = []
        for match in re.finditer(r'url\s*\(\s*["\']?([^"\')\s]*)["\']?\s*\)', css, re.IGNORECASE):
            urls.append(match.group(1))
        return urls

    def _extract_css_import_urls(self, css: str) -> List[str]:
        urls = []
        for match in re.finditer(r'@import\s+(?:url\s*\(\s*["\']?([^"\')\s]*)["\']?\s*\)|["\']([^"\']+)["\'])', css, re.IGNORECASE):
            url = match.group(1) or match.group(2)
            if url:
                urls.append(url)
        return urls

    def _extract_html_urls(self, html: str) -> List[str]:
        urls = []
        parser = _HTMLSecurityParser()
        try:
            parser.feed(html)
            urls.extend(parser.urls)
        except Exception:
            pass
        return urls

    def _check_urls(self, html: str, css: str):
        errors = []
        blocked = []

        all_urls = []
        all_urls.extend(self._extract_html_urls(html))
        all_urls.extend(self._extract_css_urls(css))

        for url in all_urls:
            if not self._is_url_allowed(url):
                errors.append(f"External resource from non-allowlisted domain: {url}")
                blocked.append(f"url:external:{url}")

        return errors, blocked

    def _is_url_allowed(self, url: str) -> bool:
        if not url:
            return True

        url = url.strip()

        if not url or url == '#':
            return True

        if url.startswith('/') or url.startswith('./') or url.startswith('../'):
            return True

        if url.lower().startswith('data:image/'):
            return True

        if url.lower().startswith('javascript:') or url.lower().startswith('vbscript:'):
            return False

        if url.lower().startswith('data:'):
            return False

        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            if not domain:
                return True

            for allowed in self.allowed_domains:
                if allowed.lower() == domain or domain.endswith('.' + allowed.lower()):
                    return True
            return False
        except Exception:
            return False