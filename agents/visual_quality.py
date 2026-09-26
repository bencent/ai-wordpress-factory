# AI WordPress Factory 視覺品質審查代理人
# 負責根據 PreviewArtifact 進行視覺品質審查

from typing import Optional, Dict, Any
from contracts import PreviewArtifact, VisualQualityResult, VisualQualityAction
from providers import VisualReviewRequest, VisualReviewResult
from providers.visual_quality_provider import MissingVisualQualityProvider
from domain.ai_runtime import classify_error
from . import BaseAgent


class VisualQualityReviewer(BaseAgent):
    """視覺品質審查代理人，負責判斷渲染頁面的視覺呈現品質。
    
    責任：
    - 接收 PreviewArtifact（四張截圖）
    - 調用多模態模型進行視覺審查
    - 返回結構化的 VisualQualityResult
    
    不做：
    - 控制工作流程
    - 發布
    - 重試
    - 重新生成前端
    - 修改 Task
    - 改變 ApprovalPolicy
    """
    
    def __init__(self, config, *, providers=None):
        super().__init__(config, providers=providers)
        self.description = "視覺品質審查：根據截圖判斷視覺呈現品質 (PASS / WARN / HUMAN_REVIEW)"
        self.provider = self._providers.visual_quality or MissingVisualQualityProvider()
    
    def review(self, preview: PreviewArtifact) -> VisualQualityResult:
        """審查預覽截圖的視覺品質。
        
        Args:
            preview: 預覽截圖產物（包含四張截圖路徑）
            
        Returns:
            VisualQualityResult: 視覺品質審查結果
        """
        # 驗證輸入
        if not preview:
            return VisualQualityResult(
                action=VisualQualityAction.HUMAN_REVIEW,
                summary="No preview artifact provided for visual review",
                issues=[],
                reviewed_viewports=[],
                reviewer="visual_quality_reviewer",
            )
        
        # 構建請求
        request = VisualReviewRequest(
            task_id=preview.task_id,
            preview=preview,
            brand_constraints=None,  # 可從 task.brand_profile 獲取
        )
        
        # 調用提供者
        try:
            provider_result = self.provider.review(request)
        except Exception as error:
            raise classify_error(error) from None
        
        # 處理提供者錯誤
        if not provider_result.success:
            return VisualQualityResult(
                action=VisualQualityAction.HUMAN_REVIEW,
                summary="Visual review failed: " + (provider_result.error if provider_result.error in {"AI provider: " + c for c in ("AUTHENTICATION", "RATE_LIMITED", "TIMEOUT", "UNAVAILABLE", "INVALID_RESPONSE", "UNSUPPORTED_CAPABILITY", "UNKNOWN")} else "AI provider: INVALID_RESPONSE"),
                issues=[],
                reviewed_viewports=[],
                reviewer="visual_quality_reviewer",
            )
        
        # 構建並返回標準結果
        return VisualQualityResult(
            action=provider_result.action,
            summary=provider_result.summary,
            issues=provider_result.issues,
            reviewed_viewports=provider_result.reviewed_viewports,
            reviewer=provider_result.reviewer,
        )