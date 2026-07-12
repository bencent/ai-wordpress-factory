# AI WordPress Factory 審閱代理人
# 负責審閱和修改內容，確保質量

from typing import Optional, Dict, Any, List
from state import Task
from . import BaseAgent


class ReviewerAgent(BaseAgent):
    """審閱代理人，負責審閱和修改內容，確保質量、準確性和一致性。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "負責審閱和修改內容，確保質量、準確性、語法、流暢性和一致性。"

    def review_content(self, task: Task) -> str:
        """審閱並修改內容。
        
        Args:
            task: 任務對象。
        
        Returns:
            str: 審閱後的Final內容。
        """
        content = task.optimized_content or task.draft_content or ""
        
        # 1.質量檢查
        quality_issues = self._check_quality(content, task)
        
        # 2.如果有問題，要求修改
        if quality_issues:
            final_content = self._fix_issues(content, quality_issues)
        else:
            final_content = content
        
        return final_content

    def _check_quality(self, content: str, task: Task) -> List[str]:
        """檢查內容的質量問題。
        
        Args:
            content: 要檢查的內容。
            task: 任務對象。
        
        Returns:
            List[str]:質量問題列表。
        """
        issues = []
        
        # 使用 AI 進行質量檢查
        prompt = f"""
        你是一位嚴格的內容審稿人。請檢查以下文章的質量問題：
        
        文章標題: {title}
        文章內容:
        {content}
        
        請檢查以下方面：
        1.語法和拼寫錯誤
        2.事實準確性
        3.邏輯一致性
        4.內容完整性
        5.流暢性和可讀性
        6.是否符合任務要求
        
        以 JSON 陣列格式返回所有發現的問題，每個問題包含：
        - 類型（語法、事實、邏輯、完整性、流暢性、其他）
        - 描述（問題的具體描述）
        - 位置（可選，問題在文章中的大致位置）
        
        如果沒有發現問題，返回空陣列 []。
        """
        
        response = self.call_ai(
            prompt.format(
                title=task.title,
                content=content,
            ),
            temperature=0.2,  # 低溫度以保持一致性
        )
        
        import json
        try:
            return [issue["描述"] for issue in json.loads(response)]
        except (json.JSONDecodeError, KeyError):
            return []

    def _fix_issues(self, content: str, issues: List[str]) -> str:
        """修復內容中的質量問題。
        
        Args:
            content: 原始內容。
            issues: 质量問題列表。
        
        Returns:
            str: 修改後的內容。
        """
        issues_str = "\n".join([f"- {issue}" for issue in issues])
        
        prompt = f"""
        你是一位專業的內容編輯。請修復以下文章中的問題：
        
        文章內容:
        {content}
        
        需要修復的問題：
        {issues_str}
        
        請返回修改後的完整文章。確保修改後的文章保持原意，只是修復了問題。
        """
        
        fixed_content = self.call_ai(
            prompt.format(
                content=content,
                issues_str=issues_str,
            ),
            temperature=0.3,
        )
        
        return fixed_content

    def grade_content(self, content: str, task: Task) -> Dict[str, Any]:
        """為內容打分。
        
        Args:
            content: 要評分的內容。
            task: 任務對象。
        
        Returns:
            Dict[str, Any]: 報告字典，包含各個維度的分數和反饋。
        """
        prompt = f"""
        你是一位嚴格的評分專家。請為以下文章打分：
        
        文章標題: {title}
        文章內容:
        {content}
        
        任務要求:
        {requirements}
        
        請按照以下維度打分（每個維度 1-10 分，10 分為滿分）：
        1. 語法和拼寫
        2. 事實準確性
        3. 邏輯一致性
        4. 內容完整性
        5. 流暢性和可讀性
        6. 任務符合性
        
        以 JSON 格式返回：
        {{
            "語法和拼寫": 10,
            "事實準確性": 9,
            "邏輯一致性": 8,
            "內容完整性": 10,
            "流暢性和可讀性": 9,
            "任務符合性": 10,
            "總分": 9.5,
            "反饋": "整體表現優秀，但邏輯部分可以進一步加強。"
        }}
        """
        
        requirements = ""
        if task.plan:
            requirements = f"計劃: {json.dumps(task.plan, ensure_ascii=False)}"
        
        response = self.call_ai(
            prompt.format(
                title=task.title,
                content=content,
                requirements=requirements,
            ),
            temperature=0.2,
        )
        
        import json
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return {
                "語法和拼寫": 8,
                "事實準確性": 8,
                "邏輯一致性": 8,
                "內容完整性": 8,
                "流暢性和可讀性": 8,
                "任務符合性": 8,
                "總分": 8.0,
                "反饋": "內容質量良好，但需要進一步優化。",
            }


# 輔助函數：導入 json 模組
import json
