# AI WordPress Factory 搜索工具
# 提供網路搜索和數據收集功能

from typing import List, Dict, Any, Optional
from . import BaseTool
import requests
import json
from urllib.parse import quote


class SearchTool(BaseTool):
    """搜索工具，提供網路搜索功能。 currently supports Google Custom Search."""

    def __init__(self, config):
        super().__init__(config)
        self.description = "提供網路搜索功能，支援 Google Custom Search 等搜索引擎。"

    def validate_config(self) -> bool:
        """驗證搜索工具的配置。"""
        required_keys = ["search_api_key", "search_engine_id"]
        return all(
            hasattr(self.config, key) and getattr(self.config, key)
            for key in required_keys
        )

    def search(self, query: str, num_results: int = 5) -> List[Dict[str, Any]]:
        """執行網路搜索。
        
        Args:
            query: 搜索查詢字符串。
            num_results: 返回的結果數量（默認為 5）。
        
        Returns:
            List[Dict[str, Any]]: 搜索結果列表，每個結果包含標題、URL、摘要等。
        """
        if not self.validate_config():
            self.log("搜索配置無效， tumb to default search.", "warning")
            return self._default_search(query, num_results)
        
        try:
            return self._google_custom_search(query, num_results)
        except Exception as e:
            self.log(f"Google Custom Search 失敗: {str(e)}", "error")
            return self._default_search(query, num_results)

    def _google_custom_search(self, query: str, num_results: int) -> List[Dict[str, Any]]:
        """使用 Google Custom Search API 進行搜索。
        
        Args:
            query: 搜索查詢字符串。
            num_results: 返回的結果數量。
        
        Returns:
            List[Dict[str, Any]]: 搜索結果列表。
        """
        api_key = self.config.search_api_key
        engine_id = self.config.search_engine_id
        
        if not api_key or not engine_id:
            raise ValueError("Google Custom Search API 配置不完整")
        
        # Google Custom Search API 端點
        url = f"https://www.googleapis.com/customsearch/v1"
        
        params = {
            "key": api_key,
            "cx": engine_id,
            "q": query,
            "num": min(num_results, 10),  # API 最多返回 10 個結果
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        results = []
        data = response.json()
        
        for item in data.get("items", [])[:num_results]:
            results.append({
                "類型": "網路搜索",
                "標題": item.get("title", "無標題"),
                "內容": item.get("snippet", ""),
                "來源": item.get("link", "#"),
                "相關性": "高",
            })
        
        return results

    def _default_search(self, query: str, num_results: int) -> List[Dict[str, Any]]:
        """默認的搜索實現（返回模擬數據）。
        
        Args:
            query: 搜索查詢字符串。
            num_results: 返回的結果數量。
        
        Returns:
            List[Dict[str, Any]]: 模擬搜索 результат。
        """
        results = []
        for i in range(num_results):
            results.append({
                "類型": "模擬搜索",
                "標題": f"搜索結果 {i + 1}: {query}",
                "內容": f"這是關於 {query} 的模擬搜索結果內容。提供了相關信息和資源。",
                "來源": f"https://example.com/search?q={quote(query)}&result={i + 1}",
                "相關性": "中",
            })
        return results

    def get_serp_analysis(self, query: str) -> Dict[str, Any]:
        """獲取搜索引擎結果頁面（SERP）的分析。
        
        Args:
            query: 搜索查詢字符串。
        
        Returns:
            Dict[str, Any]: SERP 分析結果，包含競爭分析、相關問題等。
        """
        # 使用 AI 進行 SERP 分析
        prompt = f"""
        你是一位 SEO 分析專家。請分析以下搜索查詢的 SERP：
        
        搜索查詢: {query}
        
        請提供以下分析：
        1. 競爭程度（低、中、高）
        2. 主要競爭對手（假設的網站）
        3. 相關搜索問題（用戶可能嘗試搜索的老問題）
        4. 搜索意圖（信息型、導航型、商業型、交易型）
        5. 推薦的內容策略
        
        以 JSON 格式返回，使用繁體中文：
        {{
            "競爭程度": "高",
            "主要競爭對手": ["example.com", "sample.com"],
            "相關搜索問題": ["問題1", "問題2"],
            "搜索意圖": "信息型",
            "推薦的內容策略": ["策略1", "策略2"]
        }}
        """
        
        from agents import BaseAgent
        agent = BaseAgent(self.config)
        response = agent.call_ai(prompt)
        
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return {
                "競爭程度": "未知",
                "主要競爭對手": [],
                "相關搜索問題": [],
                "搜索意圖": "未知",
                "推薦的內容策略": [],
            }
