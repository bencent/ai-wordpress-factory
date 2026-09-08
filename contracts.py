# AI WordPress Factory 結構化結果定義
# 用於 Critique / Review / Router / Learning 的合約

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum, auto


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
