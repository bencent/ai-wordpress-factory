# AI WordPress Factory 結構化結果定義
# 用於 Critique / Review / Router / Learning 的合約

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum, auto
from state import ContentType


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
