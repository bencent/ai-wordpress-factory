# AI 代理人模組
# 包含所有 AI 代理人的基類和工具函數

from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from skills.loader import skill_loader
from domain.agent_settings import agent_settings
from domain.ai_runtime import (ProviderBundle, MissingTextProvider, TextRequest, TextResult,
                               ProviderFailure, ErrorCode, classify_error)


@dataclass
class AgentConfig:
    """代理人配置類。"""
    name: str
    description: str
    enabled: bool = True
    
    # AI 模型配置
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    
    # 代理人特定配置
    extra_config: Optional[Dict[str, Any]] = None


class BaseAgent:
    """代理人基類，提供共同的功能。"""
    
    def __init__(self, config, *, providers=None):
        """初始化代理人。
        
        Args:
            config: 全局配置或代理人配置。
        """
        self.config = agent_settings(config)
        self._providers = providers if providers is not None else ProviderBundle(MissingTextProvider())
        self.last_ai_result = None
        self.name = self.__class__.__name__.replace("Agent", "").lower()
        self.skills_context = skill_loader.build_skills_context(self.name)
        self.constitution_context = skill_loader.build_constitution_context()
        
    def get_prompt(self, prompt_name: str) -> str:
        """獲取指定的提示文件內容。
        
        Args:
            prompt_name: 提示文件名稱（不帶擴展名）。
            
        Returns:
            str: 提示文件的內容。
        """
        import os
        prompt_path = os.path.join("prompts", f"{prompt_name}.md")
        if os.path.exists(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""
    
    def call_ai(self, prompt: str, required_skills: Optional[List[str]] = None, **kwargs) -> str:
        """調用 AI 模型。
        
        Args:
            prompt: 提示文本。
            required_skills: 本次調用額外需要的技能名稱列表。
            **kwargs: 其他參數（如 temperature、max_tokens 等）。
            
        Returns:
            str: AI 生成的文本。
        """
        
        if required_skills is not None:
            skills_context = skill_loader.build_skills_context(self.name, required_skills=required_skills)
        else:
            skills_context = self.skills_context
        
        full_prompt = prompt
        if skills_context:
            full_prompt = f"{skills_context}\n\n{prompt}"
        
        temperature = kwargs.get('temperature',self.config.ai_temperature)
        max_tokens = kwargs.get('max_tokens',self.config.ai_max_tokens)
        self.last_ai_result = None
        try:
            result = self._providers.text.complete(TextRequest(full_prompt,temperature,max_tokens))
            if not isinstance(result,TextResult):
                raise ProviderFailure(ErrorCode.INVALID_RESPONSE)
        except Exception as error:
            raise classify_error(error) from None
        self.last_ai_result = result
        return result.content



# 導出所有代理人
from .visual_quality import VisualQualityReviewer
from .content_fixer import ContentFixerAgent
from .critic import CriticAgent
from .final_reviewer import FinalReviewerAgent
from .frontend import FrontendAgent
from .image import ImageAgent
from .learner import LearnerAgent
from .planner import PlannerAgent
from .quality_evaluator import QualityEvaluatorAgent
from .research import ResearchAgent
from .router import Router
from .seo import SEOAgent
from .writer import WriterAgent

__all__ = [
    "BaseAgent",
    "AgentConfig",
    "VisualQualityReviewer",
    "ContentFixerAgent",
    "CriticAgent",
    "FinalReviewerAgent",
    "FrontendAgent",
    "ImageAgent",
    "LearnerAgent",
    "PlannerAgent",
    "QualityEvaluatorAgent",
    "ResearchAgent",
    "Router",
    "SEOAgent",
    "WriterAgent",
]
