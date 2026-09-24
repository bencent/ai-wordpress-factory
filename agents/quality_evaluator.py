# AI WordPress Factory 品質評估代理人
# 负責評估內容品質、發現問題並打分

from typing import Optional, Dict, Any, List
from state import Task
from contracts import ReviewResult, ReviewAction
from . import BaseAgent


class QualityEvaluatorAgent(BaseAgent):
    """品質評估代理人，負責評估內容品質、發現問題並打分。"""

    def __init__(self, config, *, providers=None):
        super().__init__(config, providers=providers)
        self.description = "負責評估內容品質、發現問題並打分。"

    def evaluate(self, task: Task) -> ReviewResult:
        content = task.revised_content or task.optimized_content or task.draft_content or ""
        issues = self._check_quality(content, task)
        score = self._score_content(content, task)
        passed = score >= 6
        feedback = self._generate_feedback(content, task, score)
        return ReviewResult(
            passed=passed,
            score=score,
            issues=issues,
            feedback=feedback,
            suggested_action=ReviewAction.PUBLISH.value if passed else ReviewAction.REWRITE.value,
        )

    def _score_content(self, content: str, task: Task) -> int:
        prompt = f"""
        你是一位嚴格的評分專家。請為以下文章打分（1-10分）：

        文章標題: {task.title}
        文章內容:
        {content}

        評分標準：
        - 語法和拼寫
        - 事實準確性
        - 邏輯一致性
        - 內容完整性
        - 流暢性和可讀性
        - 是否有 AI 寫作痕跡
        - 是否有真正的觀點而不只是整理資訊

        只回傳一個整數分數（1-10），不要加任何解釋。
        """
        response = self.call_ai(
            prompt,
            required_skills=["Bencent"],
            temperature=0.2,
            max_tokens=10,
        )
        try:
            score = int(response.strip())
            return max(1, min(10, score))
        except (ValueError, TypeError):
            return 5

    def _generate_feedback(self, content: str, task: Task, score: int) -> str:
        if score >= 8:
            return f"文章品質良好（分數：{score}/10），可以發布。"
        elif score >= 6:
            return f"文章基本合格（分數：{score}/10），但仍有改進空間。"
        else:
            return f"文章品質不足（分數：{score}/10），需要重大修改。"

    def _check_quality(self, content: str, task: Task) -> List[str]:
        issues = []
        prompt = f"""
        你是一位嚴格的內容審稿人。請檢查以下文章的質量問題：

        文章標題: {{title}}
        文章內容:
        {{content}}

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
            prompt.format(title=task.title, content=content),
            required_skills=["Bencent"],
            temperature=0.2,
        )
        import json
        try:
            return [issue["描述"] for issue in json.loads(response)]
        except (json.JSONDecodeError, KeyError):
            return []
