# AI WordPress Factory 視覺品質審查提供者
# 负责多模態視覺審查 API 抽象與 OpenAI 實現

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from contracts import PreviewArtifact, VisualQualityResult, VisualQualityAction, VisualQualityIssue, VisualIssueCategory, VisualIssueSeverity
import base64
import json


@dataclass
class VisualReviewRequest:
    """視覺審查請求。"""
    task_id: str
    preview: PreviewArtifact
    brand_constraints: Optional[Dict[str, Any]] = None


@dataclass
class VisualReviewResult:
    """視覺審查結果（提供者層級，內部使用）。"""
    success: bool
    action: Optional[VisualQualityAction] = None
    summary: str = ""
    issues: List[VisualQualityIssue] = None
    reviewed_viewports: List[str] = None
    reviewer: Optional[str] = None
    error: Optional[str] = None
    raw_response: Optional[str] = None
    
    def __post_init__(self):
        if self.issues is None:
            self.issues = []
        if self.reviewed_viewports is None:
            self.reviewed_viewports = []


class VisualQualityProvider(ABC):
    """視覺品質審查提供者抽象基類。"""
    
    @abstractmethod
    def review(self, request: VisualReviewRequest) -> VisualReviewResult:
        """根據預覽截圖進行視覺品質審查。
        
        Args:
            request: 視覺審查請求。
            
        Returns:
            VisualReviewResult: 標準化審查結果。
        """
        raise NotImplementedError


class OpenAIVisualQualityProvider(VisualQualityProvider):
    """OpenAI GPT-4V 視覺品質審查提供者。"""
    
    def __init__(self, config):
        self.config = config
    
    def _encode_image(self, image_path: str) -> str:
        """將圖片編碼為 base64。"""
        try:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            raise ValueError(f"Failed to encode image {image_path}: {e}")
    
    def _build_messages(self, request: VisualReviewRequest) -> List[Dict[str, Any]]:
        """構建多模態訊息。"""
        prompt = self._build_prompt(request)
        
        # 編碼四張截圖
        screenshots = [
            ("Desktop Viewport", request.preview.desktop_viewport_screenshot_path),
            ("Desktop Full Page", request.preview.desktop_full_page_screenshot_path),
            ("Mobile Viewport", request.preview.mobile_viewport_screenshot_path),
            ("Mobile Full Page", request.preview.mobile_full_page_screenshot_path),
        ]
        
        content = [{"type": "text", "text": prompt}]
        
        for label, path in screenshots:
            try:
                base64_image = self._encode_image(path)
                content.append({
                    "type": "text",
                    "text": f"\n--- {label} ---"
                })
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{base64_image}",
                        "detail": "high"
                    }
                })
            except Exception as e:
                raise ValueError(f"Failed to load screenshot {label}: {e}") from e
        
        return [{"role": "user", "content": content}]
    
    def _build_prompt(self, request: VisualReviewRequest) -> str:
        """構建視覺審查提示詞。"""
        brand_info = ""
        if request.brand_constraints:
            brand_info = f"\n\nBrand constraints: {json.dumps(request.brand_constraints, ensure_ascii=False)}"
        
        return f"""You are a visual quality reviewer for a WordPress publishing system.

Review the provided screenshots of a rendered web page. You are given four views:
1. Desktop viewport (above-the-fold, 1440x900)
2. Desktop full-page
3. Mobile viewport (above-the-fold, 390x844)
4. Mobile full-page

Your task: Judge whether this rendered page would be VISUALLY ACCEPTABLE to publish under the current governance policy.

Compare desktop and mobile presentations. Focus on MEANINGFUL VISUAL DEFECTS only.

IGNORE deterministic technical defects already caught by the RenderedTechnicalValidator:
- document scrollWidth overflow calculations
- broken image naturalWidth logic
- JavaScript runtime errors
- DOM geometry validation

FOCUS ON visually observable issues:
- Layout that appears visibly broken
- Heading dominating most of mobile viewport
- CTA visually lost or indistinguishable
- Confusing text hierarchy
- Severe spacing inconsistency
- Obviously poor image composition
- Unexpected desktop/mobile presentation differences
- Materially inconsistent brand styling

Issue categories (use exactly these):
- layout
- responsive
- typography
- spacing
- visual_hierarchy
- image
- cta
- brand_consistency
- other

Severity levels (descriptive only, NOT triggering retry):
- info
- warning
- major

Allowed final actions (return EXACTLY ONE):
- "pass" = No visually meaningful issue requiring attention
- "warn" = Imperfections exist but page remains visually acceptable
- "human_review" = Visual quality is materially concerning/uncertain; human must inspect before publishing

NEVER return: "retry", "regenerate", "fail", or any workflow instructions.

Return ONLY a JSON object with this exact structure:
{{
  "action": "pass|warn|human_review",
  "summary": "Brief human-readable summary of visual assessment",
  "issues": [
    {{
      "category": "layout|responsive|typography|spacing|visual_hierarchy|image|cta|brand_consistency|other",
      "severity": "info|warning|major",
      "viewport": "desktop|mobile|cross_viewport|null",
      "message": "Specific issue description",
      "evidence": "Short visual evidence description or null"
    }}
  ],
  "reviewed_viewports": ["desktop", "mobile"]
}}{brand_info}"""
    
    def _parse_response(self, raw: str) -> VisualReviewResult:
        """解析模型回應。"""
        try:
            # 清理回應
            cleaned = raw.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            elif cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
            
            data = json.loads(cleaned)
            
            # 驗證必要字段
            if not isinstance(data, dict):
                return VisualReviewResult(
                    success=False,
                    error="Model response is not a JSON object",
                    raw_response=raw,
                )
            
            # 解析 action
            action_str = data.get("action")
            if not action_str:
                return VisualReviewResult(
                    success=False,
                    error="Missing required field: action",
                    raw_response=raw,
                )
            
            try:
                action = VisualQualityAction(action_str)
            except ValueError:
                return VisualReviewResult(
                    success=False,
                    error=f"Invalid action value: {action_str}. Must be one of: pass, warn, human_review",
                    raw_response=raw,
                )
            
            # 解析 summary
            summary = data.get("summary", "")
            if not summary:
                return VisualReviewResult(
                    success=False,
                    error="Missing or empty required field: summary",
                    raw_response=raw,
                )
            
            # 解析 issues
            issues = []
            for issue_data in data.get("issues", []):
                if not isinstance(issue_data, dict):
                    continue
                
                # 解析 category
                category_str = issue_data.get("category", "other")
                try:
                    category = VisualIssueCategory(category_str)
                except ValueError:
                    category = VisualIssueCategory.OTHER
                
                if "severity" not in issue_data:
                    return VisualReviewResult(
                        success=False,
                        error="Missing required field: severity",
                        raw_response=raw,
                    )
                severity_str = issue_data["severity"]
                try:
                    severity = VisualIssueSeverity(severity_str)
                except ValueError:
                    return VisualReviewResult(
                        success=False,
                        error=f"Invalid severity value: {severity_str}. Must be one of: info, warning, major",
                        raw_response=raw,
                    )
                
                # 解析 viewport
                viewport = issue_data.get("viewport")
                if viewport and viewport not in ["desktop", "mobile", "cross_viewport"]:
                    viewport = None
                
                # 解析 message
                message = issue_data.get("message", "")
                if not message:
                    continue
                
                # 解析 evidence
                evidence = issue_data.get("evidence")
                
                issues.append(VisualQualityIssue(
                    category=category,
                    severity=severity,
                    viewport=viewport,
                    message=message,
                    evidence=evidence,
                ))
            
            # 解析 reviewed_viewports
            reviewed_viewports = data.get("reviewed_viewports", ["desktop", "mobile"])
            if not isinstance(reviewed_viewports, list):
                reviewed_viewports = ["desktop", "mobile"]
            
            # 解析 reviewer (optional)
            reviewer = data.get("reviewer", "gpt-4-vision")
            
            return VisualReviewResult(
                success=True,
                action=action,
                summary=summary,
                issues=issues,
                reviewed_viewports=reviewed_viewports,
                reviewer=reviewer,
                raw_response=raw,
            )
            
        except json.JSONDecodeError as e:
            return VisualReviewResult(
                success=False,
                error=f"Invalid JSON response: {e}",
                raw_response=raw,
            )
        except Exception as e:
            return VisualReviewResult(
                success=False,
                error=f"Failed to parse response: {e}",
                raw_response=raw,
            )
    
    def review(self, request: VisualReviewRequest) -> VisualReviewResult:
        """執行視覺品質審查。"""
        try:
            import openai
            
            api_key = getattr(self.config, "openai_api_key", None)
            if not api_key:
                return VisualReviewResult(
                    success=False,
                    error="OpenAI API Key 未配置",
                )
            
            client = openai.OpenAI(api_key=api_key)
            
            # 構建訊息
            messages = self._build_messages(request)
            
            # 調用模型
            model = getattr(self.config, "visual_quality_model", "gpt-4o")
            temperature = getattr(self.config, "visual_quality_temperature", 0.3)
            max_tokens = getattr(self.config, "visual_quality_max_tokens", 2000)
            
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            
            raw = response.choices[0].message.content
            return self._parse_response(raw)
            
        except Exception as e:
            return VisualReviewResult(
                success=False,
                error=str(e),
            )


def create_visual_quality_provider(config) -> VisualQualityProvider:
    """工廠函數：創建視覺品質提供者。"""
    provider_type = getattr(config, "visual_quality_provider", "openai")
    if provider_type == "openai":
        return OpenAIVisualQualityProvider(config)
    else:
        raise ValueError(f"Unknown visual quality provider: {provider_type}")