# AI WordPress Factory 撰寫代理人
# 负責根據計劃和資料撰寫內容

from typing import Optional, Dict, Any
from state import Task
from . import BaseAgent


class WriterAgent(BaseAgent):
    """撰寫代理人，負責根據計劃和資料撰寫高質量的內容。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "負責根據計劃和資料撰寫高質量的內容，包括文章、頁面等。"

    def write_content(self, task: Task) -> str:
        """根據任務的計劃和資料撰寫內容。
        
        Args:
            task: 任務對象。
        
        Returns:
            str: 撰寫的內容。
        """
        # 獲取提示模板
        prompt_template = self.get_prompt("writer")
        
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
            
            請遵循以下要求：
            1. 使用適當的標題層次（H1, H2, H3等）
            2. 內容應結構清晰，邏輯嚴謹
            3. 每個段落不超過5-6句
            4. 使用簡潔明了的語言
            5. 如果是博客文章，字數應在800-1500字之間
            6. 使用繁體中文撰寫
            
            請返回完整的文章內容。
            """
        
        # 准备提示中的變量
        plan_str = json.dumps(task.plan, ensure_ascii=False, indent=2) if task.plan else "無"
        research_data_str = "\n".join(
            [f"- [{item['標題']}]({item.get('來源', '#')}): {item['內容']}" 
             for item in (task.research_data or [])]
        ) or "無"
        
        # 替換提示中的變量
        prompt = prompt_template.format(
            title=task.title,
            description=task.description or "",
            content_type=task.content_type.name,
            plan=plan_str,
            research_data=research_data_str,
        )
        
        # 调用 AI 撰寫內容
        content = self.call_ai(
            prompt,
            temperature=0.8,  # 稍高的溫度以增加創造性
            max_tokens=3000,
        )
        
        return content

    def refine_content(self, task: Task, feedback: str) -> str:
        """根據反饋 Fine-tune 內容。
        
        Args:
            task: 任務對象。
            feedback: 反饋意見。
        
        Returns:
            str: 修改後的內容。
        """
        prompt = f"""
        你是一位專業的內容編輯。請根據以下反饋意見修改文章：
        
        原始文章:
        {content}
        
        反饋意見:
        {feedback}
        
        請返回修改後的完整文章。
        """
        
        refined_content = self.call_ai(
            prompt.format(
                content=task.draft_content or "",
                feedback=feedback,
            ),
            temperature=0.3,  # 較低的溫度以保持一致性
        )
        
        return refined_content


# 輔助函數：導入 json 模組
import json
