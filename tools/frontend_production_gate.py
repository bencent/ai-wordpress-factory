import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from contracts import FrontendGate, FrontendFailureSeverity, RuleSource, BrandProductionRules
from . import BaseTool


class _ProductionHTMLParser(HTMLParser):
    """Parser for production quality HTML analysis."""
    
    def __init__(self):
        super().__init__()
        self.images: List[Dict[str, Any]] = []
        self.inline_styles: List[Dict[str, Any]] = []
        self.external_scripts: List[str] = []
        self.external_stylesheets: List[str] = []
        self.external_fonts: List[str] = []
        self.form_controls: List[Dict[str, Any]] = []
        self.headings: List[Dict[str, Any]] = []
        self.fixed_width_elements: List[Dict[str, Any]] = []
        self.animated_elements: List[Dict[str, Any]] = []
        self.css_animations: List[Dict[str, Any]] = []
        self._tag_stack: List[str] = []
        self._current_attrs: Dict[str, str] = {}
    
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        self._tag_stack.append(tag)
        self._current_attrs = attrs_dict
        
        tag_lower = tag.lower()
        
        if tag_lower == 'img':
            self.images.append({
                'src': attrs_dict.get('src', ''),
                'width': attrs_dict.get('width'),
                'height': attrs_dict.get('height'),
                'loading': attrs_dict.get('loading'),
                'alt': attrs_dict.get('alt'),
                'has_alt': 'alt' in attrs_dict,
            })
        
        if 'style' in attrs_dict:
            self.inline_styles.append({
                'tag': tag_lower,
                'style': attrs_dict['style'],
            })
        
        if tag_lower == 'script':
            src = attrs_dict.get('src', '')
            if src and not src.startswith('data:') and not src.startswith('#'):
                self.external_scripts.append(src)
        
        if tag_lower == 'link':
            rel = attrs_dict.get('rel', '').lower()
            href = attrs_dict.get('href', '')
            if 'stylesheet' in rel and href:
                self.external_stylesheets.append(href)
            if 'preload' in rel and 'font' in attrs_dict.get('as', '') and href:
                self.external_fonts.append(href)
            if href and ('font' in href.lower() or 'googleapis.com' in href or 'fonts.gstatic.com' in href):
                self.external_fonts.append(href)
        
        if tag_lower in ('input', 'textarea', 'select'):
            self.form_controls.append({
                'tag': tag_lower,
                'id': attrs_dict.get('id'),
                'name': attrs_dict.get('name'),
                'aria_label': attrs_dict.get('aria-label'),
                'aria_labelledby': attrs_dict.get('aria-labelledby'),
                'type': attrs_dict.get('type', 'text') if tag_lower == 'input' else None,
            })
        
        if tag_lower in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            level = int(tag_lower[1])
            self.headings.append({
                'level': level,
                'tag': tag_lower,
            })
        
        style = attrs_dict.get('style', '')
        width_match = re.search(r'width\s*:\s*(\d+)px', style, re.IGNORECASE)
        if width_match:
            width_val = int(width_match.group(1))
            if width_val > 600:
                self.fixed_width_elements.append({
                    'tag': tag_lower,
                    'width_px': width_val,
                    'style': style,
                })
        
        animation_indicators = [
            'animation', 'transition', 'transform', '@keyframes',
            'gsap', 'scrolltrigger', 'scroll-trigger',
        ]
        class_attr = attrs_dict.get('class', '').lower()
        style_attr = style.lower()
        for indicator in animation_indicators:
            if indicator in class_attr or indicator in style_attr:
                self.animated_elements.append({
                    'tag': tag_lower,
                    'indicator': indicator,
                    'class': class_attr,
                })
                break
    
    def handle_endtag(self, tag):
        if self._tag_stack:
            self._tag_stack.pop()


class FrontendProductionQualityGate(BaseTool):
    """Deterministic production quality gate for frontend output."""
    
    def __init__(self, config):
        super().__init__(config)
        self.description = "Deterministic production quality gate for AI-generated frontend output."
        
        self.max_external_scripts = getattr(config, 'prod_max_external_scripts', 5)
        self.max_external_stylesheets = getattr(config, 'prod_max_external_stylesheets', 3)
        self.max_external_fonts = getattr(config, 'prod_max_external_fonts', 2)
        self.max_font_families = getattr(config, 'prod_max_font_families', 3)
        self.max_inline_style_size = getattr(config, 'prod_max_inline_style_size', 10 * 1024)
        self.max_inline_script_size = getattr(config, 'prod_max_inline_script_size', 10 * 1024)
        self.max_important_count = getattr(config, 'prod_max_important_count', 10)
        self.max_selector_depth = getattr(config, 'prod_max_selector_depth', 4)
        self.max_inline_style_ratio = getattr(config, 'prod_max_inline_style_ratio', 0.15)
        self.max_animated_elements = getattr(config, 'prod_max_animated_elements', 10)
        self.suspicious_fixed_width = getattr(config, 'prod_suspicious_fixed_width', 800)
        self.min_heading_level_diff = getattr(config, 'prod_min_heading_level_diff', 1)
    
    def check(
        self,
        html: str,
        css: str,
        javascript: str,
        frontend_scope: str = "page",
        content_type: str = "PAGE",
        brand_rules: Optional[BrandProductionRules] = None,
    ) -> "FrontendProductionQualityResult":
        from contracts import FrontendProductionQualityResult
        
        errors: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        diagnostics: Dict[str, Any] = {}
        
        html = html or ""
        css = css or ""
        javascript = javascript or ""
        
        is_lightweight = self._is_lightweight_scope(frontend_scope, content_type)
        diagnostics['frontend_scope'] = frontend_scope
        diagnostics['content_type'] = content_type
        diagnostics['is_lightweight'] = is_lightweight
        
        parser = _ProductionHTMLParser()
        try:
            parser.feed(html)
        except Exception as e:
            errors.append({
                "gate": "production_quality",
                "type": "html_parse_error",
                "severity": "error",
                "message": f"HTML parsing failed: {e}",
                "location": {"line": 0, "column": 0},
                "context": "Parser exception during production quality check",
                "rule_source": RuleSource.GLOBAL.value,
            })
        
        diagnostics['image_count'] = len(parser.images)
        diagnostics['inline_style_count'] = len(parser.inline_styles)
        diagnostics['external_script_count'] = len(parser.external_scripts)
        diagnostics['external_stylesheet_count'] = len(parser.external_stylesheets)
        diagnostics['external_font_count'] = len(parser.external_fonts)
        diagnostics['form_control_count'] = len(parser.form_controls)
        diagnostics['heading_count'] = len(parser.headings)
        diagnostics['fixed_width_element_count'] = len(parser.fixed_width_elements)
        diagnostics['animated_element_count'] = len(parser.animated_elements)
        
        # Global rules evaluation
        self._check_images(parser.images, errors, warnings, diagnostics)
        self._check_external_resources(
            parser.external_scripts,
            parser.external_stylesheets,
            parser.external_fonts,
            errors, warnings, diagnostics
        )
        self._check_font_families(css, errors, warnings, diagnostics)
        self._check_inline_code_size(html, css, javascript, errors, warnings, diagnostics)
        self._check_important_overuse(css, errors, warnings, diagnostics)
        self._check_selector_depth(css, errors, warnings, diagnostics)
        self._check_inline_style_overuse(parser.inline_styles, html, errors, warnings, diagnostics)
        
        if not is_lightweight:
            self._check_animation_budget(parser.animated_elements, css, errors, warnings, diagnostics)
            self._check_animation_anti_patterns(html, css, javascript, errors, warnings, diagnostics)
            self._check_responsive_safety(parser.fixed_width_elements, css, errors, warnings, diagnostics)
        
        self._check_accessibility(parser.images, parser.form_controls, parser.headings, errors, warnings, diagnostics, html)
        
        # Brand rules evaluation
        if brand_rules:
            self._evaluate_brand_rules(html, css, javascript, brand_rules, errors, warnings, diagnostics)
        
        passed = len(errors) == 0
        has_warnings = len(warnings) > 0
        validation_status = "passed" if passed and not has_warnings else ("warnings" if passed and has_warnings else "failed")
        
        return FrontendProductionQualityResult(
            passed=passed,
            validation_status=validation_status,
            errors=errors,
            warnings=warnings,
            diagnostics=diagnostics,
        )
    
    def _is_lightweight_scope(self, frontend_scope: str, content_type: str) -> bool:
        scope = (frontend_scope or "").lower()
        ctype = (content_type or "").lower()
        
        lightweight_scopes = {'article', 'section', 'blog', 'post', 'component', 'widget', 'partial'}
        lightweight_types = {'blog_post', 'article', 'section'}
        
        return scope in lightweight_scopes or ctype in lightweight_types
    
    def _check_images(self, images: List[Dict], errors: List, warnings: List, diagnostics: Dict):
        missing_dims = 0
        missing_lazy = 0
        missing_alt = 0
        
        for img in images:
            if not img['width'] or not img['height']:
                missing_dims += 1
            if img['loading'] != 'lazy':
                is_hero = img.get('is_hero', False)
                if not is_hero:
                    missing_lazy += 1
            if not img['has_alt']:
                missing_alt += 1
        
        if missing_dims > 0:
            warnings.append({
                "gate": "production_quality",
                "type": "image_missing_dimensions",
                "severity": "warning",
                "message": f"{missing_dims} image(s) missing explicit width/height attributes",
                "location": {"line": 0, "column": 0},
                "context": "Explicit dimensions prevent layout shift (CLS)",
                "rule_source": RuleSource.GLOBAL.value,
            })
        
        if missing_lazy > 0:
            warnings.append({
                "gate": "production_quality",
                "type": "image_missing_lazy",
                "severity": "warning",
                "message": f"{missing_lazy} image(s) missing loading=\"lazy\" (non-hero images)",
                "location": {"line": 0, "column": 0},
                "context": "Lazy loading improves initial load performance",
                "rule_source": RuleSource.GLOBAL.value,
            })
        
        if missing_alt > 0:
            errors.append({
                "gate": "production_quality",
                "type": "image_missing_alt",
                "severity": "error",
                "message": f"{missing_alt} image(s) missing alt attribute",
                "location": {"line": 0, "column": 0},
                "context": "Missing alt entirely (not empty alt=\"\") violates accessibility",
                "rule_source": RuleSource.GLOBAL.value,
            })
    
    def _check_external_resources(
        self,
        scripts: List[str],
        stylesheets: List[str],
        fonts: List[str],
        errors: List,
        warnings: List,
        diagnostics: Dict
    ):
        if len(scripts) > self.max_external_scripts:
            errors.append({
                "gate": "production_quality",
                "type": "excessive_external_scripts",
                "severity": "error",
                "message": f"Too many external scripts: {len(scripts)} > {self.max_external_scripts}",
                "location": {"line": 0, "column": 0},
                "context": f"External scripts: {scripts[:5]}{'...' if len(scripts) > 5 else ''}",
                "rule_source": RuleSource.GLOBAL.value,
            })
        
        if len(stylesheets) > self.max_external_stylesheets:
            errors.append({
                "gate": "production_quality",
                "type": "excessive_external_stylesheets",
                "severity": "error",
                "message": f"Too many external stylesheets: {len(stylesheets)} > {self.max_external_stylesheets}",
                "location": {"line": 0, "column": 0},
                "context": f"External stylesheets: {stylesheets[:5]}{'...' if len(stylesheets) > 5 else ''}",
                "rule_source": RuleSource.GLOBAL.value,
            })
        
        if len(fonts) > self.max_external_fonts:
            warnings.append({
                "gate": "production_quality",
                "type": "excessive_external_fonts",
                "severity": "warning",
                "message": f"Many external font resources: {len(fonts)} > {self.max_external_fonts}",
                "location": {"line": 0, "column": 0},
                "context": f"Font resources: {fonts[:5]}{'...' if len(fonts) > 5 else ''}",
                "rule_source": RuleSource.GLOBAL.value,
            })
    
    def _check_font_families(self, css: str, errors: List, warnings: List, diagnostics: Dict):
        if not css:
            return
        
        font_families = set()
        for match in re.finditer(r'font-family\s*:\s*([^;}]+)', css, re.IGNORECASE):
            families = match.group(1).split(',')
            for f in families:
                f = f.strip().strip('"\'')
                if f and f not in ('inherit', 'initial', 'unset', 'system-ui'):
                    font_families.add(f.lower())
        
        diagnostics['distinct_font_families'] = len(font_families)
        diagnostics['font_families_list'] = list(font_families)
        
        if len(font_families) > self.max_font_families:
            warnings.append({
                "gate": "production_quality",
                "type": "excessive_font_families",
                "severity": "warning",
                "message": f"Many distinct font families: {len(font_families)} > {self.max_font_families}",
                "location": {"line": 0, "column": 0},
                "context": f"Font families: {list(font_families)[:10]}",
                "rule_source": RuleSource.GLOBAL.value,
            })
    
    def _check_inline_code_size(
        self, html: str, css: str, javascript: str,
        errors: List, warnings: List, diagnostics: Dict
    ):
        inline_style_blocks = re.findall(r'<style[^>]*>(.*?)</style>', html, re.IGNORECASE | re.DOTALL)
        inline_script_blocks = re.findall(r'<script[^>]*>(.*?)</script>', html, re.IGNORECASE | re.DOTALL)
        
        total_inline_css = sum(len(block.encode('utf-8')) for block in inline_style_blocks)
        total_inline_js = sum(len(block.encode('utf-8')) for block in inline_script_blocks)
        
        diagnostics['inline_css_size'] = total_inline_css
        diagnostics['inline_js_size'] = total_inline_js
        diagnostics['inline_style_block_count'] = len(inline_style_blocks)
        diagnostics['inline_script_block_count'] = len(inline_script_blocks)
        
        if total_inline_css > self.max_inline_style_size:
            errors.append({
                "gate": "production_quality",
                "type": "excessive_inline_css",
                "severity": "error",
                "message": f"Inline CSS exceeds production budget: {total_inline_css} bytes > {self.max_inline_style_size} bytes",
                "location": {"line": 0, "column": 0},
                "context": f"{len(inline_style_blocks)} inline <style> blocks",
                "rule_source": RuleSource.GLOBAL.value,
            })
        
        if total_inline_js > self.max_inline_script_size:
            errors.append({
                "gate": "production_quality",
                "type": "excessive_inline_js",
                "severity": "error",
                "message": f"Inline JavaScript exceeds production budget: {total_inline_js} bytes > {self.max_inline_script_size} bytes",
                "location": {"line": 0, "column": 0},
                "context": f"{len(inline_script_blocks)} inline <script> blocks",
                "rule_source": RuleSource.GLOBAL.value,
            })
    
    def _check_important_overuse(self, css: str, errors: List, warnings: List, diagnostics: Dict):
        if not css:
            return
        
        important_count = len(re.findall(r'!important', css, re.IGNORECASE))
        diagnostics['important_count'] = important_count
        
        if important_count > self.max_important_count:
            errors.append({
                "gate": "production_quality",
                "type": "excessive_important",
                "severity": "error",
                "message": f"Excessive !important usage: {important_count} > {self.max_important_count}",
                "location": {"line": 0, "column": 0},
                "context": "High !important count indicates maintainability issues",
                "rule_source": RuleSource.GLOBAL.value,
            })
        elif important_count > self.max_important_count // 2:
            warnings.append({
                "gate": "production_quality",
                "type": "moderate_important",
                "severity": "warning",
                "message": f"Moderate !important usage: {important_count}",
                "location": {"line": 0, "column": 0},
                "context": "Consider reducing !important declarations",
                "rule_source": RuleSource.GLOBAL.value,
            })
    
    def _check_selector_depth(self, css: str, errors: List, warnings: List, diagnostics: Dict):
        if not css:
            return
        
        max_depth = 0
        for line in css.split('\n'):
            line = line.strip()
            if not line or line.startswith('/*') or line.startswith('@'):
                continue
            selector_part = line.split('{')[0] if '{' in line else line
            # Count combinators: space (descendant), > (child), + (adjacent), ~ (sibling)
            combinator_count = len(re.findall(r'[>+~]', selector_part))
            # Count descendant combinators (spaces between selectors)
            descendant_count = len(re.findall(r'(?<=[^ >+~])\s+(?=[^ >+~])', selector_part))
            depth = combinator_count + descendant_count
            max_depth = max(max_depth, depth)
        
        diagnostics['max_selector_depth'] = max_depth
        
        if max_depth > self.max_selector_depth:
            warnings.append({
                "gate": "production_quality",
                "type": "deep_selector",
                "severity": "warning",
                "message": f"Deep CSS selector detected: depth {max_depth} > {self.max_selector_depth}",
                "location": {"line": 0, "column": 0},
                "context": "Deep selectors increase specificity and maintenance burden",
                "rule_source": RuleSource.GLOBAL.value,
            })
    
    def _check_inline_style_overuse(
        self, inline_styles: List[Dict], html: str, errors: List, warnings: List, diagnostics: Dict
    ):
        if not inline_styles:
            return
        
        total_elements = len(re.findall(r'<[a-zA-Z][^>]*>', html))
        inline_ratio = len(inline_styles) / max(total_elements, 1)
        diagnostics['inline_style_ratio'] = inline_ratio
        diagnostics['inline_style_element_count'] = len(inline_styles)
        diagnostics['total_element_count'] = total_elements
        
        if inline_ratio > self.max_inline_style_ratio:
            warnings.append({
                "gate": "production_quality",
                "type": "inline_style_overuse",
                "severity": "warning",
                "message": f"High inline style usage: {inline_ratio:.1%} of elements ({len(inline_styles)}/{total_elements})",
                "location": {"line": 0, "column": 0},
                "context": f"Threshold: {self.max_inline_style_ratio:.0%}",
                "rule_source": RuleSource.GLOBAL.value,
            })
    
    def _check_animation_budget(
        self, animated_elements: List[Dict], css: str, errors: List, warnings: List, diagnostics: Dict
    ):
        css_animations = len(re.findall(r'@keyframes|animation\s*:|transition\s*:', css, re.IGNORECASE))
        total_animated = len(animated_elements) + css_animations
        diagnostics['css_animation_count'] = css_animations
        diagnostics['total_animated_elements'] = total_animated
        
        if total_animated > self.max_animated_elements:
            errors.append({
                "gate": "production_quality",
                "type": "animation_budget_exceeded",
                "severity": "error",
                "message": f"Animation budget exceeded: {total_animated} animated elements > {self.max_animated_elements}",
                "location": {"line": 0, "column": 0},
                "context": f"Class/style animated: {len(animated_elements)}, CSS animations: {css_animations}",
                "rule_source": RuleSource.GLOBAL.value,
            })
        elif total_animated > self.max_animated_elements // 2:
            warnings.append({
                "gate": "production_quality",
                "type": "moderate_animation",
                "severity": "warning",
                "message": f"Moderate animation usage: {total_animated} animated elements",
                "location": {"line": 0, "column": 0},
                "context": "Consider performance impact of animations",
                "rule_source": RuleSource.GLOBAL.value,
            })
    
    def _check_animation_anti_patterns(
        self, html: str, css: str, javascript: str, errors: List, warnings: List, diagnostics: Dict
    ):
        scroll_hijack_patterns = [
            r'wheel.*preventDefault',
            r'touchmove.*preventDefault',
            r'passive:\s*false',
            r'overflow:\s*hidden.*body',
            r'scroll.*lock',
        ]
        
        for pattern in scroll_hijack_patterns:
            if re.search(pattern, javascript, re.IGNORECASE) or re.search(pattern, html, re.IGNORECASE):
                warnings.append({
                    "gate": "production_quality",
                    "type": "scroll_hijacking_risk",
                    "severity": "warning",
                    "message": "Potential scroll hijacking pattern detected",
                    "location": {"line": 0, "column": 0},
                    "context": f"Pattern: {pattern}",
                    "rule_source": RuleSource.GLOBAL.value,
                })
                break
        
        particle_indicators = [
            'particle', 'particles', 'canvas.*particle', 'webgl.*particle',
            'three\.js', 'pixi\.js', 'babylon\.js',
        ]
        
        for indicator in particle_indicators:
            if re.search(indicator, javascript, re.IGNORECASE) or re.search(indicator, html, re.IGNORECASE):
                warnings.append({
                    "gate": "production_quality",
                    "type": "particle_effect_risk",
                    "severity": "warning",
                    "message": "Potential particle-heavy effect detected",
                    "location": {"line": 0, "column": 0},
                    "context": f"Indicator: {indicator}",
                    "rule_source": RuleSource.GLOBAL.value,
                })
                break
        
        webgl_indicators = [
            'webgl', 'WebGLRenderingContext', 'getContext.*webgl',
            'three\.js', 'babylon\.js', 'playcanvas', 'deck\.gl',
        ]
        
        for indicator in webgl_indicators:
            if re.search(indicator, javascript, re.IGNORECASE):
                warnings.append({
                    "gate": "production_quality",
                    "type": "webgl_heavy_risk",
                    "severity": "warning",
                    "message": "Potential WebGL-heavy frontend detected",
                    "location": {"line": 0, "column": 0},
                    "context": f"Indicator: {indicator}",
                    "rule_source": RuleSource.GLOBAL.value,
                })
                break
    
    def _check_responsive_safety(
        self, fixed_width_elements: List[Dict], css: str, errors: List, warnings: List, diagnostics: Dict
    ):
        for elem in fixed_width_elements:
            if elem['width_px'] >= self.suspicious_fixed_width:
                warnings.append({
                    "gate": "production_quality",
                    "type": "large_fixed_width",
                    "severity": "warning",
                    "message": f"Large fixed width element: {elem['width_px']}px on <{elem['tag']}>",
                    "location": {"line": 0, "column": 0},
                    "context": f"Style: {elem['style'][:100]}",
                    "rule_source": RuleSource.GLOBAL.value,
                })
        
        media_queries = len(re.findall(r'@media\s*[^{]+\{', css, re.IGNORECASE))
        diagnostics['media_query_count'] = media_queries
        
        if media_queries == 0 and fixed_width_elements:
            warnings.append({
                "gate": "production_quality",
                "type": "no_media_queries",
                "severity": "warning",
                "message": "Fixed-width elements present but no media queries detected",
                "location": {"line": 0, "column": 0},
                "context": "May cause horizontal overflow on mobile",
                "rule_source": RuleSource.GLOBAL.value,
            })
    
    def _check_accessibility(
        self,
        images: List[Dict],
        form_controls: List[Dict],
        headings: List[Dict],
        errors: List,
        warnings: List,
        diagnostics: Dict,
        html: str,
    ):
        missing_alt = [img for img in images if not img['has_alt']]
        if missing_alt:
            errors.append({
                "gate": "production_quality",
                "type": "missing_alt_attribute",
                "severity": "error",
                "message": f"{len(missing_alt)} image(s) missing alt attribute entirely",
                "location": {"line": 0, "column": 0},
                "context": "alt=\"\" is valid for decorative images; missing alt is not",
                "rule_source": RuleSource.GLOBAL.value,
            })
        
        unlabeled_forms = []
        for fc in form_controls:
            has_label = (
                fc.get('id') and self._has_associated_label(fc['id'], html) or
                fc.get('aria_label') or
                fc.get('aria_labelledby')
            )
            if not has_label:
                unlabeled_forms.append(fc)
        
        if unlabeled_forms:
            warnings.append({
                "gate": "production_quality",
                "type": "unlabeled_form_control",
                "severity": "warning",
                "message": f"{len(unlabeled_forms)} form control(s) without accessible label",
                "location": {"line": 0, "column": 0},
                "context": f"Controls: {[fc['tag'] for fc in unlabeled_forms]}",
                "rule_source": RuleSource.GLOBAL.value,
            })
        
        heading_issues = self._check_heading_hierarchy(headings)
        for issue in heading_issues:
            warnings.append({
                "gate": "production_quality",
                "type": "heading_hierarchy_skip",
                "severity": "warning",
                "message": f"Heading level skip: {issue['from']} → {issue['to']}",
                "location": {"line": 0, "column": 0},
                "context": f"At index {issue['index']}",
                "rule_source": RuleSource.GLOBAL.value,
            })
        
        diagnostics['unlabeled_forms'] = len(unlabeled_forms)
        diagnostics['heading_issues'] = len(heading_issues)
    
    def _has_associated_label(self, input_id: str, html: str) -> bool:
        pattern = rf'<label[^>]*for\s*=\s*["\']?{re.escape(input_id)}["\']?'
        if re.search(pattern, html, re.IGNORECASE):
            return True
        pattern = rf'<label[^>]*>.*?<input[^>]*id\s*=\s*["\']?{re.escape(input_id)}'
        if re.search(pattern, html, re.IGNORECASE | re.DOTALL):
            return True
        return False
    
    def _check_heading_hierarchy(self, headings: List[Dict]) -> List[Dict]:
        issues = []
        if not headings:
            return issues
        
        prev_level = 0
        for i, h in enumerate(headings):
            level = h['level']
            if prev_level > 0 and level - prev_level > self.min_heading_level_diff:
                issues.append({
                    'index': i,
                    'from': f'h{prev_level}',
                    'to': f'h{level}',
                })
            prev_level = level
        return issues

    # ==================== BRAND RULES EVALUATION ====================

    def _evaluate_brand_rules(
        self,
        html: str,
        css: str,
        javascript: str,
        brand_rules: BrandProductionRules,
        errors: List[Dict[str, Any]],
        warnings: List[Dict[str, Any]],
        diagnostics: Dict[str, Any],
    ) -> None:
        """Evaluate brand-specific production rules.
        
        Rule severities (frozen):
        - allowed_font_families: ERROR (FAIL)
        - allowed_colors: WARNING
        - max_content_width_px: WARNING
        - allowed_border_radius_px: WARNING
        """
        diagnostics['brand_rules_evaluated'] = True
        
        # Font family rule (ERROR)
        if brand_rules.allowed_font_families:
            self._check_brand_font_families(css, brand_rules.allowed_font_families, errors, warnings, diagnostics)
        
        # Color rule (WARNING)
        if brand_rules.allowed_colors:
            self._check_brand_colors(css, brand_rules.allowed_colors, errors, warnings, diagnostics)
        
        # Max content width rule (WARNING)
        if brand_rules.max_content_width_px is not None:
            self._check_brand_max_content_width(css, brand_rules.max_content_width_px, errors, warnings, diagnostics)
        
        # Border radius rule (WARNING)
        if brand_rules.allowed_border_radius_px:
            self._check_brand_border_radius(css, brand_rules.allowed_border_radius_px, errors, warnings, diagnostics)

    def _check_brand_font_families(
        self,
        css: str,
        allowed_families: List[str],
        errors: List[Dict[str, Any]],
        warnings: List[Dict[str, Any]],
        diagnostics: Dict[str, Any],
    ) -> None:
        """Check primary font-family declarations against allowed list.
        
        Conservative approach: only evaluates the PRIMARY font family in each
        font-family declaration stack. Generic fallbacks (serif, sans-serif, etc.)
        are not treated as violations when they appear as fallbacks.
        """
        if not css:
            return
        
        # Normalize allowed families for case-insensitive comparison
        allowed_normalized = {f.strip().lower().strip('"\'') for f in allowed_families}
        generic_families = {'serif', 'sans-serif', 'monospace', 'cursive', 'fantasy', 'system-ui', 'inherit', 'initial', 'unset'}
        
        # Find all font-family declarations
        violations = []
        for match in re.finditer(r'font-family\s*:\s*([^;}]+)', css, re.IGNORECASE):
            families = match.group(1).split(',')
            primary = families[0].strip().strip('"\'')
            primary_lower = primary.lower()
            
            # Skip generic families as primary
            if primary_lower in generic_families:
                continue
            
            if primary_lower not in allowed_normalized:
                violations.append({
                    'primary_font': primary,
                    'declaration': match.group(1).strip(),
                })
        
        if violations:
            diagnostics['brand_font_violations'] = violations
            errors.append({
                "gate": "production_quality",
                "type": "brand_font_family_not_allowed",
                "severity": "error",
                "message": f"Primary font(s) not allowed by brand profile: {', '.join(v['primary_font'] for v in violations)}",
                "location": {"line": 0, "column": 0},
                "context": f"Allowed: {', '.join(allowed_families)}",
                "rule_source": RuleSource.BRAND.value,
            })

    def _check_brand_colors(
        self,
        css: str,
        allowed_colors: List[str],
        errors: List[Dict[str, Any]],
        warnings: List[Dict[str, Any]],
        diagnostics: Dict[str, Any],
    ) -> None:
        """Check resolvable literal color values against allowed palette.
        
        Only evaluates directly resolvable literal colors:
        - hex (#rgb, #rrggbb, #rgba, #rrggbbaa)
        - rgb()/rgba()
        - hsl()/hsla()
        
        Does NOT resolve (skipped):
        - CSS variables: var(--...)
        - currentColor
        - inherited values
        - gradients
        - computed style
        
        UNRESOLVABLE != PASS, UNRESOLVABLE != FAIL → SKIP
        """
        if not css:
            return
        
        # Normalize allowed colors (case-insensitive)
        allowed_normalized = set()
        for color in allowed_colors:
            normalized = self._normalize_color(color)
            if normalized:
                allowed_normalized.add(normalized.lower())
        
        # Pattern for literal color values
        color_patterns = [
            # hex: #rgb, #rrggbb, #rgba, #rrggbbaa
            (r'#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?=[\s;,}])', 'hex'),
            # rgb/rgba
            (r'rgba?\s*\(\s*([^)]+)\)', 'rgb'),
            # hsl/hsla
            (r'hsla?\s*\(\s*([^)]+)\)', 'hsl'),
        ]
        
        violations = []
        
        # First, find and skip gradient functions entirely
        # Replace gradient functions with placeholder to avoid matching colors inside them
        css_for_color_check = css
        gradient_pattern = r'(linear-gradient|radial-gradient|conic-gradient|repeating-linear-gradient|repeating-radial-gradient|repeating-conic-gradient)\s*\([^)]*\)'
        css_for_color_check = re.sub(gradient_pattern, '/* gradient */', css_for_color_check, flags=re.IGNORECASE)
        
        for pattern, color_type in color_patterns:
            for match in re.finditer(pattern, css_for_color_check, re.IGNORECASE):
                color_str = match.group(0)
                normalized = self._normalize_color(color_str)
                
                if normalized is None:
                    # Unresolvable (CSS var, currentColor, gradient, etc.) - SKIP
                    continue
                
                if normalized.lower() not in allowed_normalized:
                    violations.append({
                        'color': color_str,
                        'normalized': normalized,
                    })
        
        if violations:
            diagnostics['brand_color_violations'] = violations
            warnings.append({
                "gate": "production_quality",
                "type": "brand_color_not_allowed",
                "severity": "warning",
                "message": f"Color(s) not in brand palette: {', '.join(v['color'] for v in violations)}",
                "location": {"line": 0, "column": 0},
                "context": f"Allowed palette: {', '.join(allowed_colors)}",
                "rule_source": RuleSource.BRAND.value,
            })

    def _normalize_color(self, color_str: str) -> Optional[str]:
        """Normalize a color string to a canonical form for comparison.
        
        Supports:
        - hex: #rgb, #rrggbb, #rgba, #rrggbbaa
        - rgb(r, g, b) / rgba(r, g, b, a)
        - hsl(h, s%, l%) / hsla(h, s%, l%, a)
        
        Returns None for unresolvable values (CSS vars, currentColor, gradients).
        """
        color_str = color_str.strip()
        
        # Skip CSS variables, currentColor
        if 'var(' in color_str or 'currentcolor' in color_str.lower():
            return None
        
        # Hex colors - match with optional trailing punctuation
        hex_match = re.match(r'^#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})', color_str)
        if hex_match:
            hex_val = hex_match.group(1)
            # Expand short hex (#rgb -> #rrggbb, #rgba -> #rrggbbaa)
            if len(hex_val) == 3:
                return '#' + ''.join(c * 2 for c in hex_val)
            elif len(hex_val) == 4:
                return '#' + ''.join(c * 2 for c in hex_val)
            elif len(hex_val) == 6:
                return '#' + hex_val
            elif len(hex_val) == 8:
                return '#' + hex_val
            return None
        
        # rgb/rgba
        rgb_match = re.match(r'rgba?\s*\(\s*([^)]+)\)', color_str, re.IGNORECASE)
        if rgb_match:
            parts = [p.strip() for p in rgb_match.group(1).split(',')]
            if len(parts) >= 3:
                try:
                    r = int(float(parts[0].rstrip('%')))
                    g = int(float(parts[1].rstrip('%')))
                    b = int(float(parts[2].rstrip('%')))
                    # Clamp to 0-255
                    r = max(0, min(255, r))
                    g = max(0, min(255, g))
                    b = max(0, min(255, b))
                    return f'#{r:02x}{g:02x}{b:02x}'
                except (ValueError, IndexError):
                    return None
        
        # hsl/hsla - convert to hex
        hsl_match = re.match(r'hsla?\s*\(\s*([^)]+)\)', color_str, re.IGNORECASE)
        if hsl_match:
            parts = [p.strip() for p in hsl_match.group(1).split(',')]
            if len(parts) >= 3:
                try:
                    h = float(parts[0]) % 360
                    s = max(0, min(100, float(parts[1].rstrip('%')))) / 100
                    l = max(0, min(100, float(parts[2].rstrip('%')))) / 100
                    
                    # HSL to RGB conversion
                    def hue_to_rgb(p, q, t):
                        if t < 0: t += 1
                        if t > 1: t -= 1
                        if t < 1/6: return p + (q - p) * 6 * t
                        if t < 1/2: return q
                        if t < 2/3: return p + (q - p) * (2/3 - t) * 6
                        return p
                    
                    if s == 0:
                        r = g = b = l
                    else:
                        q = l * (1 + s) if l < 0.5 else l + s - l * s
                        p = 2 * l - q
                        r = hue_to_rgb(p, q, h/360 + 1/3)
                        g = hue_to_rgb(p, q, h/360)
                        b = hue_to_rgb(p, q, h/360 - 1/3)
                    
                    r = int(round(r * 255))
                    g = int(round(g * 255))
                    b = int(round(b * 255))
                    return f'#{r:02x}{g:02x}{b:02x}'
                except (ValueError, IndexError):
                    return None
        
        return None

    def _check_brand_max_content_width(
        self,
        css: str,
        max_width_px: int,
        errors: List[Dict[str, Any]],
        warnings: List[Dict[str, Any]],
        diagnostics: Dict[str, Any],
    ) -> None:
        """Check for max-width or fixed width declarations exceeding brand limit.
        
        Conservative source-level check only. Does NOT implement layout engine.
        Rendered width may differ due to Grid/Flexbox/box-sizing/nesting.
        """
        if not css:
            return
        
        violations = []
        
        # Check max-width declarations
        for match in re.finditer(r'max-width\s*:\s*(\d+)px', css, re.IGNORECASE):
            width = int(match.group(1))
            if width > max_width_px:
                violations.append({
                    'property': 'max-width',
                    'value_px': width,
                    'declaration': match.group(0),
                })
        
        # Check width declarations (only if clearly content container)
        # This is more heuristic - we only flag if it looks like a container
        for match in re.finditer(r'(^|\s)width\s*:\s*(\d+)px', css, re.IGNORECASE | re.MULTILINE):
            width = int(match.group(2))
            # Only flag if it's a large fixed width that could be a container
            if width > max_width_px:
                violations.append({
                    'property': 'width',
                    'value_px': width,
                    'declaration': match.group(0).strip(),
                })
        
        if violations:
            diagnostics['brand_max_width_violations'] = violations
            warnings.append({
                "gate": "production_quality",
                "type": "brand_max_content_width_exceeded",
                "severity": "warning",
                "message": f"Content width exceeds brand limit ({max_width_px}px): {', '.join(str(v['value_px']) + 'px' for v in violations)}",
                "location": {"line": 0, "column": 0},
                "context": f"Brand max: {max_width_px}px. Note: source-level heuristic; rendered width may differ due to Grid/Flexbox/box-sizing/nesting.",
                "rule_source": RuleSource.BRAND.value,
            })

    def _check_brand_border_radius(
        self,
        css: str,
        allowed_radii: List[int],
        errors: List[Dict[str, Any]],
        warnings: List[Dict[str, Any]],
        diagnostics: Dict[str, Any],
    ) -> None:
        """Check border-radius declarations against allowed values.
        
        Conservative: only simple numeric px values. Skips:
        - percentages
        - slash syntax (elliptical)
        - CSS variables
        - calc()
        """
        if not css:
            return
        
        allowed_set = set(allowed_radii)
        violations = []
        
        # Simple border-radius: Npx or border-radius: Npx Mpx ...
        for match in re.finditer(r'border-radius\s*:\s*([^;}]+)', css, re.IGNORECASE):
            values_str = match.group(1).strip()
            
            # Split by space, handle slash syntax conservatively
            # If slash present, skip (elliptical/complex)
            if '/' in values_str:
                continue
            
            parts = values_str.split()
            for part in parts:
                part = part.strip()
                # Only handle simple Npx values
                px_match = re.match(r'^(\d+)px$', part)
                if px_match:
                    radius = int(px_match.group(1))
                    if radius not in allowed_set:
                        violations.append({
                            'radius_px': radius,
                            'declaration': match.group(0),
                        })
        
        if violations:
            diagnostics['brand_border_radius_violations'] = violations
            warnings.append({
                "gate": "production_quality",
                "type": "brand_border_radius_not_allowed",
                "severity": "warning",
                "message": f"Border radius not in brand allowed list: {', '.join(str(v['radius_px']) + 'px' for v in violations)}",
                "location": {"line": 0, "column": 0},
                "context": f"Allowed: {', '.join(str(r) + 'px' for r in sorted(allowed_radii))}. Complex values (%, /, calc, var) skipped.",
                "rule_source": RuleSource.BRAND.value,
            })


def check_production_quality(
    html: str,
    css: str,
    javascript: str,
    frontend_scope: str = "page",
    content_type: str = "PAGE",
    config=None,
) -> "FrontendProductionQualityResult":
    """Convenience function to check production quality."""
    gate = FrontendProductionQualityGate(config or type('Config', (), {})())
    return gate.check(html, css, javascript, frontend_scope, content_type)