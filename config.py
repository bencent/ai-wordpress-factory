# AI WordPress Factory 配置文件
# 存儲 API Key 和其他全局設定

import os
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class Config:
    """全局配置類，存儲 API Key 和其他設定。"""
    # OpenAI API Key
    openai_api_key: Optional[str] = None
    
    # WordPress API 配置
    wordpress_url: Optional[str] = None
    wordpress_username: Optional[str] = None
    wordpress_password: Optional[str] = None
    wordpress_app_password: Optional[str] = None  # WordPress 應用密碼（用於 REST API）
    
    # 搜索 API 配置（例如：Google Custom Search、Bing Search 等）
    search_api_key: Optional[str] = None
    search_engine_id: Optional[str] = None
    
    # AI 模型配置
    ai_model: str = "gpt-4"
    ai_temperature: float = 0.7
    ai_max_tokens: int = 2000
    
    # 工作流程配置
    max_retries: int = 3
    image_required: bool = False
    
    # 前端安全配置
    allowed_domains: List[str] = None
    frontend_max_html_size: int = 100 * 1024
    frontend_max_css_size: int = 50 * 1024
    frontend_max_js_size: int = 50 * 1024
    frontend_max_total_size: int = 200 * 1024
    
    # 代理人配置
    agents: dict = None

    def __post_init__(self):
        """初始化後加載環境變量中的配置。"""
        if self.openai_api_key is None:
            self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.wordpress_url = os.getenv("WORDPRESS_URL", self.wordpress_url)
        self.wordpress_username = os.getenv("WORDPRESS_USERNAME", self.wordpress_username)
        self.wordpress_password = os.getenv("WORDPRESS_PASSWORD", self.wordpress_password)
        self.wordpress_app_password = os.getenv("WORDPRESS_APP_PASSWORD", self.wordpress_app_password)
        self.search_api_key = os.getenv("SEARCH_API_KEY", self.search_api_key)
        self.search_engine_id = os.getenv("SEARCH_ENGINE_ID", self.search_engine_id)
        
        # 預設允許的域名：從 WordPress URL 解析
        if self.allowed_domains is None:
            self.allowed_domains = []
            if self.wordpress_url:
                from urllib.parse import urlparse
                parsed = urlparse(self.wordpress_url)
                if parsed.netloc:
                    self.allowed_domains.append(parsed.netloc)
        
        # 預設代理人配置
        if self.agents is None:
            self.agents = {
                "planner": {"enabled": True},
                "research": {"enabled": True},
                "writer": {"enabled": True},
                "critic": {"enabled": True},
                "seo": {"enabled": True},
                "reviewer": {"enabled": False},
                "quality_evaluator": {"enabled": True},
                "content_fixer": {"enabled": True},
                "final_reviewer": {"enabled": True},
                "router": {"enabled": True},
                "image": {"enabled": True},
                "learner": {"enabled": True},
            }


# 全局配置實例
config = Config()


def load_config_from_env():
    """從環境變量加載配置。"""
    global config
    config = Config()
    return config


def load_config_from_file(file_path: str = "config.json") -> Config:
    """從 JSON 文件加載配置。
    
    Args:
        file_path: 配置文件路徑，默認為 "config.json"。
    
    Returns:
        Config: 加載後的配置實例。
    """
    import json
    
    if not os.path.exists(file_path):
        return Config()
    
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    return Config(
        openai_api_key=data.get("openai_api_key"),
        wordpress_url=data.get("wordpress_url"),
        wordpress_username=data.get("wordpress_username"),
        wordpress_password=data.get("wordpress_password"),
        wordpress_app_password=data.get("wordpress_app_password"),
        search_api_key=data.get("search_api_key"),
        search_engine_id=data.get("search_engine_id"),
        ai_model=data.get("ai_model", "gpt-4"),
        ai_temperature=data.get("ai_temperature", 0.7),
        ai_max_tokens=data.get("ai_max_tokens", 2000),
        max_retries=data.get("max_retries", 3),
        image_required=data.get("image_required", False),
        allowed_domains=data.get("allowed_domains"),
        frontend_max_html_size=data.get("frontend_max_html_size", 100 * 1024),
        frontend_max_css_size=data.get("frontend_max_css_size", 50 * 1024),
        frontend_max_js_size=data.get("frontend_max_js_size", 50 * 1024),
        frontend_max_total_size=data.get("frontend_max_total_size", 200 * 1024),
        agents=data.get("agents"),
    )
