# 外部工具模組
# 包含所有外部工具的基類和工具函數

from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class BaseTool:
    """外部工具基類，提供共同的功能。"""

    def __init__(self, config):
        """初始化工具。
        
        Args:
            config: 全局配置。
        """
        self.config = config
        self.name = self.__class__.__name__.replace("Tool", "").lower()

    def validate_config(self) -> bool:
        """驗證工具配置是否有效。
        
        Returns:
            bool: 配置是否有效。
        """
        return True

    def log(self, message: str, level: str = "info") -> None:
        """記錄日誌。
        
        Args:
            message: 日誌信息。
            level: 日誌級別（info、warning、error 等）。
        """
        if level == "info":
            logger.info(f"[{self.name}] {message}")
        elif level == "warning":
            logger.warning(f"[{self.name}] {message}")
        elif level == "error":
            logger.error(f"[{self.name}] {message}")
        else:
            logger.debug(f"[{self.name}] {message}")


from .frontend_security import FrontendSecurityGate
from .greenlight_converter import GreenLightConverter
from .frontend_validator import FrontendValidator
from .frontend_production_gate import FrontendProductionQualityGate
from .preview_renderer import PreviewRenderer, PreviewRenderError, render_preview
from .rendered_technical_validator import RenderedTechnicalValidator
from .wordpress import WordPressPublisher

__all__ = [
    "BaseTool",
    "FrontendSecurityGate",
    "GreenLightConverter",
    "FrontendValidator",
    "FrontendProductionQualityGate",
    "PreviewRenderer",
    "PreviewRenderError",
    "render_preview",
    "RenderedTechnicalValidator",
    "WordPressPublisher",
]
