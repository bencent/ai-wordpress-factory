# AI WordPress Factory 結構化結果定義
# 用於 Critique / Review / Router / Learning 的合約

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum, auto
from state import ContentType
import datetime
import uuid


class CritiqueAction(str, Enum):
    KEEP = "KEEP"
    REWRITE = "REWRITE"
    DELETE = "DELETE"
    RESEARCH_MORE = "RESEARCH_MORE"


class AIPatternType(str, Enum):
    OVERSTATEMENT = "OVERSTATEMENT"
    FAKE_SINCERITY = "FAKE_SINCERITY"
    RULE_OF_THREE = "RULE_OF_THREE"
    FORMULAIC_ENDING = "FORMULAIC_ENDING"
    AI_TRANSLATION_VOCAB = "AI_TRANSLATION_VOCAB"
    FAKE_DEPTH = "FAKE_DEPTH"
    ARTIFICIAL_SCOPE = "ARTIFICIAL_SCOPE"
    OVER_SIGNPOSTING = "OVER_SIGNPOSTING"
    EXCESSIVE_BOLD = "EXCESSIVE_BOLD"
    EMOJI_OVERUSE = "EMOJI_OVERUSE"


class ReviewAction(str, Enum):
    PUBLISH = "PUBLISH"
    REWRITE = "REWRITE"
    RESEARCH = "RESEARCH"
    SEO = "SEO"
    FAIL = "FAIL"


class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ImageArtifactStatus(str, Enum):
    """Image artifact lifecycle status."""
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


@dataclass
class AIPattern:
    type: str
    text: str
    reason: str
    suggestion: str


@dataclass
class CritiqueResult:
    score: int = 0
    issues: List[str] = field(default_factory=list)
    ai_patterns: List[AIPattern] = field(default_factory=list)
    keep: List[str] = field(default_factory=list)
    rewrite: List[Dict[str, Any]] = field(default_factory=list)
    delete: List[str] = field(default_factory=list)
    research_more: List[str] = field(default_factory=list)
    overall_feedback: str = ""


@dataclass
class ReviewResult:
    passed: bool = False
    score: int = 0
    issues: List[str] = field(default_factory=list)
    feedback: str = ""
    suggested_action: str = ReviewAction.PUBLISH.value


@dataclass
class LearningProposal:
    rule: str = ""
    reason: str = ""
    source_task: str = ""
    created_at: str = ""
    status: str = ProposalStatus.PENDING.value


@dataclass
class ImageArtifact:
    """Stable, serializable image artifact contract for durable reuse across preview, review, approval, and publish."""
    artifact_id: str
    status: ImageArtifactStatus = ImageArtifactStatus.PENDING
    provider: Optional[str] = None
    model: Optional[str] = None
    source_url: Optional[str] = None
    local_path: Optional[str] = None
    wordpress_media_id: Optional[int] = None
    wordpress_media_url: Optional[str] = None
    prompt: Optional[str] = None
    alt_text: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    content_type: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "status": self.status.value if isinstance(self.status, ImageArtifactStatus) else self.status,
            "provider": self.provider,
            "model": self.model,
            "source_url": self.source_url,
            "local_path": self.local_path,
            "wordpress_media_id": self.wordpress_media_id,
            "wordpress_media_url": self.wordpress_media_url,
            "prompt": self.prompt,
            "alt_text": self.alt_text,
            "width": self.width,
            "height": self.height,
            "content_type": self.content_type,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ImageArtifact":
        status = data.get("status", "pending")
        if isinstance(status, str):
            status = ImageArtifactStatus(status)
        return cls(
            artifact_id=data["artifact_id"],
            status=status,
            provider=data.get("provider"),
            model=data.get("model"),
            source_url=data.get("source_url"),
            local_path=data.get("local_path"),
            wordpress_media_id=data.get("wordpress_media_id"),
            wordpress_media_url=data.get("wordpress_media_url"),
            prompt=data.get("prompt"),
            alt_text=data.get("alt_text"),
            width=data.get("width"),
            height=data.get("height"),
            content_type=data.get("content_type"),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", datetime.datetime.now().isoformat()),
        )


def create_image_artifact_id() -> str:
    """Generate a stable unique artifact ID for an image artifact.
    
    Format: img_<uuid>
    """
    import uuid
    return f"img_{uuid.uuid4().hex[:12]}"


def create_image_artifact(**kwargs) -> ImageArtifact:
    """Factory function to create an ImageArtifact with auto-generated artifact_id.
    
    Args:
        **kwargs: All ImageArtifact fields except artifact_id (auto-generated)
        
    Returns:
        ImageArtifact with generated artifact_id
    """
    artifact_id = kwargs.pop("artifact_id", None) or create_image_artifact_id()
    return ImageArtifact(artifact_id=artifact_id, **kwargs)


@dataclass
class ImageGenerationRequest:
    task_id: str = ""
    use_case: str = "hero"
    title: str = ""
    content_summary: str = ""
    aspect_ratio: str = "16:9"
    resolution: str = "1792x1024"
    style_constraints: Dict[str, Any] = field(default_factory=dict)
    negative_prompt: str = ""
    provider: str = "openai"
    model: str = "dall-e-3"
    quality: str = "standard"
    prompt_used: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageGenerationResult:
    task_id: str = ""
    success: bool = False
    provider: str = ""
    model: str = ""
    image_url: Optional[str] = None
    image_bytes: Optional[bytes] = None
    content_type: str = "image/png"
    width: int = 0
    height: int = 0
    prompt_used: str = ""
    tokens_used: Optional[int] = None
    cost_estimate: Optional[float] = None
    error: Optional[str] = None
    generation_time_ms: Optional[int] = None


@dataclass
class FrontendRequest:
    task_id: str = ""
    content_type: ContentType = ContentType.BLOG_POST
    frontend_scope: str = "page"
    animation_required: bool = False
    design_brief: str = ""
    brand_constraints: Dict[str, Any] = field(default_factory=dict)
    seo_title: str = ""
    seo_description: str = ""
    seo_keywords: List[str] = field(default_factory=list)
    content_outline: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FrontendResult:
    task_id: str = ""
    success: bool = False
    html: Optional[str] = None
    css: Optional[str] = None
    javascript: Optional[str] = None
    blocks: Optional[str] = None
    validation_status: str = "pending"
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    animation_used: bool = False
    gsap_subskills_loaded: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GreenLightConversionResult:
    task_id: str = ""
    success: bool = False
    blocks: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    conversion_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FrontendSecurityResult:
    task_id: str = ""
    passed: bool = False
    html: str = ""
    css: str = ""
    javascript: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    blocked_items: List[str] = field(default_factory=list)


@dataclass
class FrontendValidationResult:
    task_id: str = ""
    passed: bool = False
    validation_status: str = "failed"  # "passed", "failed", "warnings"
    errors: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    validator_error: bool = False


@dataclass
class FrontendProductionQualityResult:
    """Result from FrontendProductionQualityGate check."""
    task_id: str = ""
    passed: bool = False
    validation_status: str = "failed"  # "passed", "failed", "warnings"
    errors: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "passed": self.passed,
            "validation_status": self.validation_status,
            "errors": self.errors,
            "warnings": self.warnings,
            "diagnostics": self.diagnostics,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FrontendProductionQualityResult":
        return cls(
            task_id=data.get("task_id", ""),
            passed=data.get("passed", False),
            validation_status=data.get("validation_status", "failed"),
            errors=data.get("errors", []),
            warnings=data.get("warnings", []),
            diagnostics=data.get("diagnostics", {}),
        )


class FrontendGate(str, Enum):
    """Frontend pipeline gate identifiers."""
    FRONTEND_SECURITY = "FRONTEND_SECURITY"
    FRONTEND_CONVERSION = "FRONTEND_CONVERSION"
    FRONTEND_VALIDATION = "FRONTEND_VALIDATION"
    FRONTEND_PRODUCTION_QUALITY = "FRONTEND_PRODUCTION_QUALITY"
    RENDERED_TECHNICAL = "RENDERED_TECHNICAL"


class FrontendFailureSeverity(str, Enum):
    """Failure severity levels."""
    ERROR = "error"
    WARNING = "warning"


class ApprovalPolicyMode(str, Enum):
    """Approval policy modes for workflow governance."""
    AUTO_PUBLISH = "auto_publish"
    REQUIRE_HUMAN_REVIEW = "require_human_review"


@dataclass
class ApprovalPolicy:
    """Workflow governance configuration."""
    mode: ApprovalPolicyMode
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value if isinstance(self.mode, ApprovalPolicyMode) else self.mode,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApprovalPolicy":
        mode = data.get("mode", ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW.value)
        if isinstance(mode, str):
            mode = ApprovalPolicyMode(mode)
        return cls(mode=mode)


@dataclass
class ClientProfile:
    """Client/project identity and configuration associations.
    
    Does NOT contain raw credentials (passwords, API keys, tokens).
    Site association uses existing WordPress config architecture.
    """
    client_id: str
    display_name: str = ""
    locale: str = "en_US"
    # Site association via existing config (wordpress_url, username, app_password in Config)
    # No raw credentials stored here
    approval_policy: Optional[ApprovalPolicy] = None
    # Phase 7C-4A: Brand profile for deterministic production rules
    brand_profile: Optional["BrandProfile"] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "display_name": self.display_name,
            "locale": self.locale,
            "approval_policy": self.approval_policy.to_dict() if self.approval_policy else None,
            "brand_profile": self.brand_profile.to_dict() if self.brand_profile else None,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClientProfile":
        approval_policy = data.get("approval_policy")
        if approval_policy:
            approval_policy = ApprovalPolicy.from_dict(approval_policy)
        brand_profile = data.get("brand_profile")
        if brand_profile:
            brand_profile = BrandProfile.from_dict(brand_profile)
        return cls(
            client_id=data.get("client_id", ""),
            display_name=data.get("display_name", ""),
            locale=data.get("locale", "en_US"),
            approval_policy=approval_policy,
            brand_profile=brand_profile,
            metadata=data.get("metadata", {}),
        )


class RuleSource(str, Enum):
    """Origin of a production quality rule."""
    GLOBAL = "global"
    BRAND = "brand"


@dataclass
class BrandProductionRules:
    """v1 deterministic brand production constraints.
    
    Does NOT include:
    - spacing_scale_px
    - heading_font_families / body_font_families split
    - 12-column grid enforcement
    - text max columns
    - layout semantics
    - per-client severity overrides
    - generic rules/metadata dicts
    """
    allowed_font_families: List[str]
    allowed_colors: Optional[List[str]] = None
    max_content_width_px: Optional[int] = None
    allowed_border_radius_px: Optional[List[int]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed_font_families": self.allowed_font_families,
            "allowed_colors": self.allowed_colors,
            "max_content_width_px": self.max_content_width_px,
            "allowed_border_radius_px": self.allowed_border_radius_px,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BrandProductionRules":
        return cls(
            allowed_font_families=data.get("allowed_font_families", []),
            allowed_colors=data.get("allowed_colors"),
            max_content_width_px=data.get("max_content_width_px"),
            allowed_border_radius_px=data.get("allowed_border_radius_px"),
        )


@dataclass
class BrandProfile:
    """Brand identity + structured production rules.
    
    Does NOT contain:
    - WordPress credentials
    - site config
    - ApprovalPolicy
    - workflow state
    - HTML/CSS/JS
    - Publisher config
    - visual score
    - Skill document reference
    - generic metadata
    """
    brand_id: str
    display_name: str
    production_rules: BrandProductionRules
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "brand_id": self.brand_id,
            "display_name": self.display_name,
            "production_rules": self.production_rules.to_dict(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BrandProfile":
        production_rules = data.get("production_rules")
        if production_rules:
            production_rules = BrandProductionRules.from_dict(production_rules)
        else:
            production_rules = BrandProductionRules(allowed_font_families=[])
        return cls(
            brand_id=data.get("brand_id", ""),
            display_name=data.get("display_name", ""),
            production_rules=production_rules,
        )


@dataclass
class FrontendFailureFeedback:
    """Structured feedback for frontend pipeline failures."""
    gate: FrontendGate
    severity: FrontendFailureSeverity
    error: str
    feedback: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.datetime.now().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "gate": self.gate.value if isinstance(self.gate, FrontendGate) else self.gate,
            "severity": self.severity.value if isinstance(self.severity, FrontendFailureSeverity) else self.severity,
            "error": self.error,
            "feedback": self.feedback,
            "details": self.details,
            "timestamp": self.timestamp,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FrontendFailureFeedback":
        """Create from dictionary."""
        gate = data.get("gate")
        if isinstance(gate, str):
            gate = FrontendGate(gate)
        
        severity = data.get("severity")
        if isinstance(severity, str):
            severity = FrontendFailureSeverity(severity)
        
        return cls(
            gate=gate,
            severity=severity,
            error=data.get("error", ""),
            feedback=data.get("feedback", ""),
            details=data.get("details", {}),
            timestamp=data.get("timestamp", datetime.datetime.now().isoformat()),
        )


class FailureCategory(str, Enum):
    """Failure category for distinguishing content vs infrastructure failures."""
    CONTENT = "content"
    INFRASTRUCTURE = "infrastructure"


@dataclass
class PreviewViewport:
    """Viewport dimensions for preview screenshots."""
    width: int
    height: int
    
    def to_dict(self) -> Dict[str, Any]:
        return {"width": self.width, "height": self.height}
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PreviewViewport":
        return cls(width=data.get("width", 0), height=data.get("height", 0))


@dataclass
class PreviewArtifact:
    """Captured browser preview artifact.
    
    Contains references to four screenshots:
    - desktop viewport (above-the-fold)
    - desktop full-page
    - mobile viewport (above-the-fold)
    - mobile full-page
    
    Does NOT contain screenshot bytes - only filesystem paths.
    """
    task_id: str
    preview_id: str
    attempt_number: int
    
    desktop_viewport_screenshot_path: str
    desktop_full_page_screenshot_path: str
    mobile_viewport_screenshot_path: str
    mobile_full_page_screenshot_path: str
    
    desktop_viewport: PreviewViewport
    mobile_viewport: PreviewViewport
    
    created_at: str
    image_artifact_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "preview_id": self.preview_id,
            "attempt_number": self.attempt_number,
            "desktop_viewport_screenshot_path": self.desktop_viewport_screenshot_path,
            "desktop_full_page_screenshot_path": self.desktop_full_page_screenshot_path,
            "mobile_viewport_screenshot_path": self.mobile_viewport_screenshot_path,
            "mobile_full_page_screenshot_path": self.mobile_full_page_screenshot_path,
            "desktop_viewport": self.desktop_viewport.to_dict(),
            "mobile_viewport": self.mobile_viewport.to_dict(),
            "created_at": self.created_at,
            "image_artifact_id": self.image_artifact_id,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PreviewArtifact":
        return cls(
            task_id=data.get("task_id", ""),
            preview_id=data.get("preview_id", ""),
            attempt_number=data.get("attempt_number", 1),
            desktop_viewport_screenshot_path=data.get("desktop_viewport_screenshot_path", ""),
            desktop_full_page_screenshot_path=data.get("desktop_full_page_screenshot_path", ""),
            mobile_viewport_screenshot_path=data.get("mobile_viewport_screenshot_path", ""),
            mobile_full_page_screenshot_path=data.get("mobile_full_page_screenshot_path", ""),
            desktop_viewport=PreviewViewport.from_dict(data.get("desktop_viewport", {"width": 1440, "height": 900})),
            mobile_viewport=PreviewViewport.from_dict(data.get("mobile_viewport", {"width": 390, "height": 844})),
            created_at=data.get("created_at", datetime.datetime.now().isoformat()),
            image_artifact_id=data.get("image_artifact_id"),
        )


@dataclass
class PreviewInfrastructureFailure:
    """Infrastructure failure during preview rendering.
    
    Distinguishes browser/render failures from frontend content failures.
    These failures do NOT trigger frontend retry.
    """
    task_id: str
    preview_id: Optional[str]
    attempt_number: int
    error_type: str
    message: str
    retryable: bool
    occurred_at: str
    failure_category: FailureCategory = FailureCategory.INFRASTRUCTURE
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "preview_id": self.preview_id,
            "attempt_number": self.attempt_number,
            "error_type": self.error_type,
            "message": self.message,
            "retryable": self.retryable,
            "occurred_at": self.occurred_at,
            "failure_category": self.failure_category.value if isinstance(self.failure_category, FailureCategory) else self.failure_category,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PreviewInfrastructureFailure":
        failure_category = data.get("failure_category", FailureCategory.INFRASTRUCTURE.value)
        if isinstance(failure_category, str):
            failure_category = FailureCategory(failure_category)
        return cls(
            task_id=data.get("task_id", ""),
            preview_id=data.get("preview_id"),
            attempt_number=data.get("attempt_number", 1),
            error_type=data.get("error_type", ""),
            message=data.get("message", ""),
            retryable=data.get("retryable", True),
            occurred_at=data.get("occurred_at", datetime.datetime.now().isoformat()),
            failure_category=failure_category,
        )


# ==================== Phase 7D-2: Rendered Technical Validator ====================

@dataclass
class ViewportRenderedEvidence:
    """Rendered evidence collected for a single viewport."""
    viewport_width: int
    viewport_height: int
    document_scroll_width: int
    document_client_width: int
    document_scroll_height: int
    document_client_height: int
    console_errors: List[Dict[str, Any]]
    page_errors: List[Dict[str, Any]]
    image_load_states: List[Dict[str, Any]]
    element_bounding_boxes: List[Dict[str, Any]]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "document_scroll_width": self.document_scroll_width,
            "document_client_width": self.document_client_width,
            "document_scroll_height": self.document_scroll_height,
            "document_client_height": self.document_client_height,
            "console_errors": self.console_errors,
            "page_errors": self.page_errors,
            "image_load_states": self.image_load_states,
            "element_bounding_boxes": self.element_bounding_boxes,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ViewportRenderedEvidence":
        return cls(
            viewport_width=data.get("viewport_width", 0),
            viewport_height=data.get("viewport_height", 0),
            document_scroll_width=data.get("document_scroll_width", 0),
            document_client_width=data.get("document_client_width", 0),
            document_scroll_height=data.get("document_scroll_height", 0),
            document_client_height=data.get("document_client_height", 0),
            console_errors=data.get("console_errors", []),
            page_errors=data.get("page_errors", []),
            image_load_states=data.get("image_load_states", []),
            element_bounding_boxes=data.get("element_bounding_boxes", []),
        )


@dataclass
class RenderedEvidence:
    """Deterministic browser-rendered evidence for technical validation.
    
    Collected by PreviewRenderer during preview capture.
    Contains only measurements needed for deterministic checks.
    Does NOT contain full DOM dumps.
    """
    task_id: str
    preview_id: str
    attempt_number: int
    desktop: ViewportRenderedEvidence
    mobile: ViewportRenderedEvidence
    created_at: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "preview_id": self.preview_id,
            "attempt_number": self.attempt_number,
            "desktop": self.desktop.to_dict(),
            "mobile": self.mobile.to_dict(),
            "created_at": self.created_at,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RenderedEvidence":
        return cls(
            task_id=data.get("task_id", ""),
            preview_id=data.get("preview_id", ""),
            attempt_number=data.get("attempt_number", 1),
            desktop=ViewportRenderedEvidence.from_dict(data.get("desktop", {})),
            mobile=ViewportRenderedEvidence.from_dict(data.get("mobile", {})),
            created_at=data.get("created_at", datetime.datetime.now().isoformat()),
        )


class RenderedTechnicalGate(str, Enum):
    """Rendered technical validator gate identifier."""
    RENDERED_TECHNICAL = "RENDERED_TECHNICAL"


class RenderedTechnicalSeverity(str, Enum):
    """Severity levels for rendered technical diagnostics."""
    ERROR = "error"
    WARNING = "warning"


@dataclass
class RenderedTechnicalResult:
    """Result from RenderedTechnicalValidator deterministic checks.
    
    Determines if browser-rendered page has technical defects.
    ERROR diagnostics fail the gate. WARNING diagnostics do not.
    """
    task_id: str
    preview_id: str
    attempt_number: int
    passed: bool
    validation_status: str  # "passed", "failed", "warnings"
    errors: List[Dict[str, Any]]
    warnings: List[Dict[str, Any]]
    diagnostics: Dict[str, Any]
    created_at: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "preview_id": self.preview_id,
            "attempt_number": self.attempt_number,
            "passed": self.passed,
            "validation_status": self.validation_status,
            "errors": self.errors,
            "warnings": self.warnings,
            "diagnostics": self.diagnostics,
            "created_at": self.created_at,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RenderedTechnicalResult":
        return cls(
            task_id=data.get("task_id", ""),
            preview_id=data.get("preview_id", ""),
            attempt_number=data.get("attempt_number", 1),
            passed=data.get("passed", False),
            validation_status=data.get("validation_status", "failed"),
            errors=data.get("errors", []),
            warnings=data.get("warnings", []),
            diagnostics=data.get("diagnostics", {}),
            created_at=data.get("created_at", datetime.datetime.now().isoformat()),
        )


# ==================== Phase 7D-3A: Visual Quality Contracts ====================

class VisualQualityAction(str, Enum):
    """Visual quality review action.
    
    These are governance outcomes, NOT retry/regeneration triggers.
    PASS = acceptable for publish
    WARN = visual imperfections but acceptable for existing governance flow
    HUMAN_REVIEW = materially concerning, human must review before publishing
    
    IMPORTANT: These values NEVER directly trigger:
    - RETRY
    - REGENERATE
    - FAIL
    """
    PASS = "pass"
    WARN = "warn"
    HUMAN_REVIEW = "human_review"


class VisualIssueCategory(str, Enum):
    """Visual issue categories v1.
    
    Intentionally small set for v1.
    """
    LAYOUT = "layout"
    RESPONSIVE = "responsive"
    TYPOGRAPHY = "typography"
    SPACING = "spacing"
    VISUAL_HIERARCHY = "visual_hierarchy"
    IMAGE = "image"
    CTA = "cta"
    BRAND_CONSISTENCY = "brand_consistency"
    OTHER = "other"


class VisualIssueSeverity(str, Enum):
    """Visual issue severity levels.
    
    Severity is descriptive only.
    MUST NOT trigger retry.
    No visual retry mechanism in Phase 7D-3.
    """
    INFO = "info"
    WARNING = "warning"
    MAJOR = "major"


@dataclass
class VisualQualityIssue:
    """A single visual quality issue.
    
    Serialization-ready dataclass for visual quality findings.
    """
    category: VisualIssueCategory
    severity: VisualIssueSeverity
    viewport: Optional[str] = None  # desktop, mobile, cross_viewport, None
    message: str = ""
    evidence: Optional[str] = None  # Short human-readable visual evidence
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value if isinstance(self.category, VisualIssueCategory) else self.category,
            "severity": self.severity.value if isinstance(self.severity, VisualIssueSeverity) else self.severity,
            "viewport": self.viewport,
            "message": self.message,
            "evidence": self.evidence,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VisualQualityIssue":
        category = data.get("category", VisualIssueCategory.OTHER.value)
        if isinstance(category, str):
            category = VisualIssueCategory(category)
        
        severity = data.get("severity", VisualIssueSeverity.INFO.value)
        if isinstance(severity, str):
            severity = VisualIssueSeverity(severity)
        
        return cls(
            category=category,
            severity=severity,
            viewport=data.get("viewport"),
            message=data.get("message", ""),
            evidence=data.get("evidence"),
        )


@dataclass
class VisualQualityResult:
    """Result from VisualQualityReviewer.
    
    Visual quality is NOT a deterministic technical gate.
    The reviewer classifies: PASS, WARN, HUMAN_REVIEW
    
    NEVER directly returns: RETRY, REGENERATE, FAIL
    
    HUMAN_REVIEW is governance escalation.
    It is NOT frontend retry.
    It is NOT failure.
    It is NOT regeneration.
    """
    action: VisualQualityAction
    summary: str
    issues: List[VisualQualityIssue] = field(default_factory=list)
    reviewed_viewports: List[str] = field(default_factory=list)
    reviewer: Optional[str] = None
    reviewed_at: str = field(default_factory=lambda: datetime.datetime.now().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value if isinstance(self.action, VisualQualityAction) else self.action,
            "summary": self.summary,
            "issues": [issue.to_dict() for issue in self.issues],
            "reviewed_viewports": self.reviewed_viewports,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VisualQualityResult":
        action = data.get("action", VisualQualityAction.PASS.value)
        if isinstance(action, str):
            action = VisualQualityAction(action)
        
        issues = []
        for issue_data in data.get("issues", []):
            issues.append(VisualQualityIssue.from_dict(issue_data))
        
        return cls(
            action=action,
            summary=data.get("summary", ""),
            issues=issues,
            reviewed_viewports=data.get("reviewed_viewports", []),
            reviewer=data.get("reviewer"),
            reviewed_at=data.get("reviewed_at", datetime.datetime.now().isoformat()),
        )
