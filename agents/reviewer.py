# AI WordPress Factory 審閱代理人
# 负責審閱和修改內容，確保質量

from typing import Optional, Dict, Any, List
from state import Task
from contracts import ReviewResult, ReviewAction
from . import BaseAgent


class ReviewerAgent(BaseAgent):
    """審閱代理人，負責審閱和修改內容，確保質量、準確性和一致性。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "負責審閱和修改內容，確保質量、準確性、語法、流暢性和一致性。"

    def review_content(self, task: Task) -> ReviewResult:
        """審閱內容並回傳結構化結果。

        Args:
            task: 任務對象。

        Returns:
            ReviewResult: 結構化審閱結果。
        """
        content = task.revised_content or task.optimized_content or task.draft_content or ""
        
        quality_issues = self._check_quality(content, task)
        
        if quality_issues:
            fixed_content = self._fix_issues(content, quality_issues)
        else:
            fixed_content = content
        
        final_content = self._final_review(fixed_content, task)
        
        score = self._score_content(final_content, task)
        passed = score >= 6
        
        return ReviewResult(
            passed=passed,
            score=score,
            issues=quality_issues,
            feedback=self._generate_feedback(final_content, task, score),
            suggested_action=ReviewAction.PUBLISH.value if passed else ReviewAction.REWRITE.value,
        )
    
    def _final_review(self, content: str, task: Task) -> str:
        """對內容進行最終審核，檢查是否違反所有風格規則。

        Args:
            content: 要檢查的內容。
            task: 任務對象。

        Returns:
            str: 審核後的內容。
        """
        style_rules = self._load_file("prompts/style-rules.md")
        anti_ai = self._load_file("prompts/anti-ai-writing.md")
        
        if not style_rules and not anti_ai:
            return content

        prompt = f"""
        你是一位嚴格的最終審核專家。請檢查以下文章是否違反了任何風格規則或 AI 寫作模式。

        文章內容:
        {content}

        風格規則:
        {style_rules}

        Anti-AI Writing Patterns:
        {anti_ai}

        審核要求：
        1. 逐條檢查文章是否違反了風格規則中的任何禁止事項
        2. 檢查是否有 AI 寫作模式（假真誠、過度誇大、制式結尾等）
        3. 檢查是否有過度使用的句式模式
        4. 檢查文體是否符合基本要求（直接、節奏變化、信任讀者）
        5. 如果發現問題，直接返回修正後的完整文章
        6. 如果沒有發現問題，直接返回原文

        請直接返回修正後的完整文章或原文，不要加任何解釋。
        """

        reviewed_content = self.call_ai(prompt, temperature=0.2, max_tokens=4000)
        return reviewed_content.strip()
    
    def _score_content(self, content: str, task: Task) -> int:
        """為內容打分（1-10）。

        Args:
            content: 要評分的内容。
            task: 任務對象。

        Returns:
            int: 分數（1-10）。
        """
        prompt = f"""
        你是一位严格的評分專家。請為以下文章打分（1-10分）：

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

        response = self.call_ai(prompt, temperature=0.2, max_tokens=10)
        try:
            score = int(response.strip())
            return max(1, min(10, score))
        except (ValueError, TypeError):
            return 5
    
    def _generate_feedback(self, content: str, task: Task, score: int) -> str:
        """生成審閱反饋。

        Args:
            content: 審閱後的內容。
            task: 任務對象。
            score: 分數。

        Returns:
            str: 反饋文字。
        """
        if score >= 8:
            return f"文章品質良好（分數：{score}/10），可以發布。"
        elif score >= 6:
            return f"文章基本合格（分數：{score}/10），但仍有改進空間。"
        else:
            return f"文章品質不足（分數：{score}/10），需要重大修改。"
    
    def _load_file(self, file_path: str) -> str:
        """讀取文件內容。

        Args:
            file_path: 文件路徑。

        Returns:
            str: 文件內容。
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def _check_quality(self, content: str, task: Task) -> List[str]:
        """檢查內容的質量問題。

        Args:
            content: 要檢查的內容。
            task: 任務對象。

        Returns:
            List[str]:質量問題列表。
        """
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
            prompt.format(
                title=task.title,
                content=content,
            ),
            temperature=0.2,
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
