# AI WordPress Factory 撰寫代理人
# 负責根據計劃和資料撰寫內容

import json
from typing import Optional, Dict, Any
from state import Task
from . import BaseAgent
from worker.revision import RevisionContext


class WriterAgent(BaseAgent):
    """撰寫代理人，負責根據計劃和資料撰寫高質量的內容。"""

    def __init__(self, config, *, providers=None):
        super().__init__(config, providers=providers)
        self.description = "負責根據計劃和資料撰寫高質量的內容，包括文章、頁面等。"

    def write_content(self, task: Task, revision_context: Optional[RevisionContext] = None) -> str:
        """根據任務的計劃和資料撰寫內容。

        Args:
            task: 任務對象。
            revision_context: Optional RevisionContext for REVISION runs.

        Returns:
            str: 撰寫的內容。
        """
        # 獲取提示模板
        prompt_template = self.get_prompt("writer")

        # 准备提示中的變量
        plan_str = json.dumps(task.plan, ensure_ascii=False, indent=2) if task.plan else "無"
        research_data_str = "\n".join(
            [f"- [{item['標題']}]({item.get('來源', '#')}): {item['內容']}"
             for item in (task.research_data or [])]
        ) or "無"

        style_rules = self._load_file("prompts/style-rules.md")
        tone_sample = self._load_file("prompts/tone-sample.md")

        if revision_context is not None:
            # REVISION run: Writer receives original requirement + exact source + feedback
            prompt = self._build_revision_prompt(
                task, revision_context, plan_str, research_data_str,
                style_rules, tone_sample
            )
        else:
            # INITIAL run: Preserve existing Writer behavior exactly
            if not prompt_template:
                prompt_template = """
                你是一位專業的內容撰寫者。請根據以下信息撰寫一篇高質量的文章：

                任務標題: {title}
                任務描述: {description}
                內容類型: {content_type}

                計劃:
                {plan}

                研究數據:
                {research_data}

                風格規則:
                {style_rules}

                語氣樣本:
                {tone_sample}

                請遵循以下要求：
                1. 使用適當的標題層次（H1, H2, H3等）
                2. 內容應結構清晰，邏輯嚴謹
                3. 每個段落不超過5-6句
                4. 使用簡潔明了的語言
                5. 如果是博客文章，字數應在800-1500字之間
                6. 使用繁體中文撰寫
                7. 嚴格遵守風格規則中的所有禁止事項
                8. 參考語氣樣本的寫作風格

                請返回完整的文章內容。
                """

            # 替換提示中的變量
            prompt = prompt_template.format(
                title=task.title,
                description=task.description or "",
                content_type=task.content_type.name,
                plan=plan_str,
                research_data=research_data_str,
                style_rules=style_rules,
                tone_sample=tone_sample,
            )

        # 調用 AI 撰寫內容
        content = self.call_ai(
            prompt,
            temperature=0.8,  # 稍高的溫度以增加創造性
            max_tokens=3000,
        )

        return content

    def _build_revision_prompt(
        self,
        task: Task,
        revision_context: RevisionContext,
        plan_str: str,
        research_data_str: str,
        style_rules: str,
        tone_sample: str,
    ) -> str:
        """Build revision-aware prompt with original requirement, source content, and feedback."""
        return f"""
        你是一位專業的內容編輯。請根據以下信息修訂現有文章：

        【原始任務需求】
        任務標題: {task.title}
        任務描述: {task.description or ""}
        內容類型: {task.content_type.name}

        原始計劃:
        {plan_str}

        原始研究數據:
        {research_data_str}

        【需修訂的源內容】
        {revision_context.source_content}

        【審閱者回饋】
        {revision_context.reviewer_feedback}

        風格規則:
        {style_rules}

        語氣樣本:
        {tone_sample}

        請遵循以下要求：
        1. 基於源內容進行修訂，而非從頭生成
        2. 重點解決審閱者回饋中提出的問題
        3. 保持源內容的優點和結構
        4. 使用適當的標題層次（H1, H2, H3等）
        5. 內容應結構清晰，邏輯嚴謹
        6. 每個段落不超過5-6句
        7. 使用簡潔明了的語言
        8. 使用繁體中文撰寫
        9. 嚴格遵守風格規則中的所有禁止事項
        10. 參考語氣樣本的寫作風格

        請返回修訂後的完整文章內容。
        """

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

    def refine_content(self, task: Task, feedback: str) -> str:
        """根據反饋 Fine-tune 內容。

        Args:
            task: 任務對象。
            feedback: 反饋意見。

        Returns:
            str: 修改後的內容。
        """
        content = task.draft_content or ""
        prompt = f"""
        你是一位專業的內容編輯。請根據以下反饋意見修改文章：

        原始文章:
        {content}

        反饋意見:
        {feedback}

        請返回修改後的完整文章。
        """

        refined_content = self.call_ai(
            prompt,
            required_skills=["Bencent"],
            temperature=0.3,
        )

        return refined_content
