# AI WordPress Factory SEO 代理人
# 负責優化內容的 SEO 屬性

from typing import Dict, Any, Optional, Tuple
from state import Task
from . import BaseAgent


class SEOAgent(BaseAgent):
    """SEO 代理人，負責優化內容的 SEO 屬性，包括標題、描述、關鍵字等。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "負責優化內容的 SEO 屬性，包括標題、描述、關鍵字、內部鏈接等。"

    def optimize_content(self, task: Task) -> Tuple[str, Dict[str, Any]]:
        """優化內容的 SEO 屬性。
        
        Args:
            task: 任務對象。
        
        Returns:
            Tuple[str, Dict[str, Any]]: 優化後的內容和 SEO 元數據。
        """
        # 1. 內容優化
        optimized_content = self._optimize_content_body(task)
        
        # 2. 生成 SEO 元數據
        seo_metadata = self._generate_seo_metadata(task)
        
        return optimized_content, seo_metadata

    def _optimize_content_body(self, task: Task) -> str:
        """優化內容主體，添加 SEO 相關元素。
        
        Args:
            task: 任務對象。
        
        Returns:
            str: 優化後的內容。
        """
        content = task.optimized_content or task.draft_content or ""
        
        plan_str = json.dumps(task.plan, ensure_ascii=False, indent=2) if task.plan else "無"
        research_data_str = "\n".join(
            [f"- {item['標題']}: {item['內容']}" 
             for item in (task.research_data or [])]
        ) or "無"
        
        prompt = """
        你是一位 SEO 專家。請優化以下文章的內容，以提高其搜索引擎友好性：
        
        文章標題: {title}
        文章內容:
        {content}
        
        計劃:
        {plan}
        
        研究數據:
        {research_data}
        
        優化要求：
        1. 確保主要關鍵字出現在標題、前100字和整個文章中
        2.添加適當的內部鏈接（如果有相關內容）
        3. 使用語義相關的關鍵字
        4. 確保內容可讀性高
        5. 添加適當的標題層次（H1, H2, H3等）
        6. 為圖片添加 alt 屬性（如果有圖片）
        
        返回優化後的完整文章內容。
        """
        
        optimized_content = self.call_ai(
            prompt.format(
                title=task.title,
                content=content,
                plan=plan_str,
                research_data=research_data_str,
            ),
            temperature=0.5,
        )
        
        return optimized_content

    def _generate_seo_metadata(self, task: Task) -> Dict[str, Any]:
        """生成 SEO 元數據。
        
        Args:
            task: 任務對象。
        
        Returns:
            Dict[str, Any]: SEO 元數據，包含標題、描述、關鍵字等。
        """
        plan_str = json.dumps(task.plan, ensure_ascii=False, indent=2) if task.plan else "無"
        content = task.optimized_content or task.draft_content or ""
        
        prompt = """
        你是一位 SEO 專家。請為以下文章生成 SEO 元數據：
        
        文章標題: {title}
        文章內容:
        {content}
        
        計劃:
        {plan}
        
        要求：
        1. SEO 標題：60 字符以内，包含主要關鍵字
        2. SEO 描述：160 字符以内，吸引人且包含主要關鍵字
        3. 關鍵字：5-10 個相關關鍵字，用逗號分隔
        
        以 JSON 格式返回，使用繁體中文：
        {{
            "title": "SEO 標題",
            "description": "SEO 描述",
            "keywords": ["關鍵字1", "關鍵字2"]
        }}
        """
        
        response = self.call_ai(
            prompt.format(
                title=task.title,
                content=content,
                plan=plan_str,
            ),
            temperature=0.3,
        )
        
        import json
        try:
            metadata = json.loads(response)
            return metadata
        except json.JSONDecodeError:
            return {
                "title": f"{task.title} | AI WordPress Factory",
                "description": f"了解更多關於 {task.title} 的詳細信息和觀點。",
                "keywords": [task.title] + (task.plan.get("關鍵字", []) if task.plan else []),
            }


# 輔助函數：導入 json 模組
import json
