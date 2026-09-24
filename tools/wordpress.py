# AI WordPress Factory WordPress 工具
# 提供與 WordPress REST API 交互的功能

from typing import Optional, Tuple, Dict, Any, List
from . import BaseTool
import requests
import json
from urllib.parse import urljoin


class WordPressPublisher(BaseTool):
    """WordPress 發布工具，提供文章發布、更新、刪除等功能。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "提供與 WordPress REST API 交互的功能，包括發布、更新、刪除文章等。"

    def validate_config(self) -> bool:
        """驗證 WordPress 配置。"""
        required_keys = ["wordpress_url", "wordpress_username", "wordpress_app_password"]
        return all(
            hasattr(self.config, key) and getattr(self.config, key)
            for key in required_keys
        )

    def publish_content(self, task, featured_media_id: Optional[int] = None) -> Tuple[Optional[int], Optional[str]]:
        """發布內容到 WordPress。
        
        Args:
            task: 任務對象，包含要發布的內容和 SEO 元數據。
            featured_media_id: 精選圖片媒體 ID（可選）。
        
        Returns:
            Tuple[Optional[int], Optional[str]]: 文章 ID 和文章 URL。
        """
        if not self.validate_config():
            self.log("WordPress 配置無效，無法發布內容。", "error")
            return None, None
        
        content = task.final_content or task.optimized_content or task.draft_content or ""
        
        # 准备文章數據
        post_data = {
            "title": task.seo_title or task.title,
            "content": content,
            "status": "publish",
            "comment_status": "open",
            "ping_status": "open",
        }
        
        # 設定精選圖片
        if featured_media_id:
            post_data["featured_media"] = featured_media_id
        
        # 添加 SEO 元數據
        if hasattr(task, "seo_description") and task.seo_description:
            post_data["yoast_meta"] = json.dumps({
                "yoast_wpseo_metadesc": task.seo_description,
            })
        
        if hasattr(task, "seo_keywords") and task.seo_keywords:
            post_data["yoast_meta"] = json.dumps({
                **json.loads(post_data.get("yoast_meta", "{}")),
                "yoast_wpseo_focuskw": ", ".join(task.seo_keywords),
            })
        
        try:
            # 發布文章
            post_id, post_url = self._create_post(post_data)
            self.log(f"成功發布文章: {post_url}")
            return post_id, post_url
        except Exception as e:
            self.log(f"發布文章失敗: {str(e)}", "error")
            return None, None

    def _create_post(self, post_data: Dict[str, Any]) -> Tuple[int, str]:
        """創建新文章。
        
        Args:
            post_data: 文章數據。
        
        Returns:
            Tuple[int, str]: 文章 ID 和文章 URL。
        """
        base_url = self.config.wordpress_url
        username = self.config.wordpress_username
        app_password = self.config.wordpress_app_password
        
        # WordPress REST API 端點
        api_url = urljoin(base_url, "wp-json/wp/v2/posts")
        
        #.supports basic authentication
        response = requests.post(
            api_url,
            auth=(username, app_password),
            json=post_data,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        
        post = response.json()
        post_id = post.get("id")
        post_url = post.get("link")
        
        return post_id, post_url

    def update_post(self, post_id: int, post_data: Dict[str, Any]) -> bool:
        """更新現有文章。
        
        Args:
            post_id: 文章 ID。
            post_data: 要更新的文章數據。
        
        Returns:
            bool: 更新是否成功。
        """
        if not self.validate_config():
            self.log("WordPress 配置無效，無法更新文章。", "error")
            return False
        
        base_url = self.config.wordpress_url
        username = self.config.wordpress_username
        app_password = self.config.wordpress_app_password
        
        # WordPress REST API 端點
        api_url = urljoin(base_url, f"wp-json/wp/v2/posts/{post_id}")
        
        try:
            response = requests.post(
                api_url,
                auth=(username, app_password),
                json=post_data,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
            self.log(f"成功更新文章: {post_id}")
            return True
        except Exception as e:
            self.log(f"更新文章失敗: {str(e)}", "error")
            return False

    def delete_post(self, post_id: int) -> bool:
        """刪除文章。
        
        Args:
            post_id: 文章 ID。
        
        Returns:
            bool: 刪除是否成功。
        """
        if not self.validate_config():
            self.log("WordPress 配置無效，無法刪除文章。", "error")
            return False
        
        base_url = self.config.wordpress_url
        username = self.config.wordpress_username
        app_password = self.config.wordpress_app_password
        
        # WordPress REST API 端點
        api_url = urljoin(base_url, f"wp-json/wp/v2/posts/{post_id}")
        
        try:
            # WordPress REST API 使用 DELETE 方法刪除文章
            # 需要添加 force=True 參數來跳過回收站
            response = requests.delete(
                api_url,
                auth=(username, app_password),
                params={"force": "true"},
                timeout=30,
            )
            response.raise_for_status()
            self.log(f"成功刪除文章: {post_id}")
            return True
        except Exception as e:
            self.log(f"刪除文章失敗: {str(e)}", "error")
            return False

    def get_post(self, post_id: int) -> Optional[Dict[str, Any]]:
        """獲取文章信息。
        
        Args:
            post_id: 文章 ID。
        
        Returns:
            Optional[Dict[str, Any]]: 文章數據，如果不存在則返回 None。
        """
        if not self.validate_config():
            self.log("WordPress 配置無效，無法獲取文章。", "error")
            return None
        
        base_url = self.config.wordpress_url
        username = self.config.wordpress_username
        app_password = self.config.wordpress_app_password
        
        # WordPress REST API 端點
        api_url = urljoin(base_url, f"wp-json/wp/v2/posts/{post_id}")
        
        try:
            response = requests.get(
                api_url,
                auth=(username, app_password),
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"獲取文章失敗: {str(e)}", "error")
            return None

    def list_posts(self, per_page: int = 10, page: int = 1) -> List[Dict[str, Any]]:
        """列出所有文章。
        
        Args:
            per_page: 每頁返回的文章數量（默認為 10）。
            page: 頁碼（默認為 1）。
        
        Returns:
            List[Dict[str, Any]]: 文章列表。
        """
        if not self.validate_config():
            self.log("WordPress 配置無效，無法列出文章。", "error")
            return []
        
        base_url = self.config.wordpress_url
        username = self.config.wordpress_username
        app_password = self.config.wordpress_app_password
        
        # WordPress REST API 端點
        api_url = urljoin(base_url, "wp-json/wp/v2/posts")
        
        try:
            response = requests.get(
                api_url,
                auth=(username, app_password),
                params={
                    "per_page": per_page,
                    "page": page,
                },
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"列出文章失敗: {str(e)}", "error")
            return []

    def upload_media(self, file_path: str, title: str = "") -> Tuple[Optional[int], Optional[str]]:
        """上傳媒體檔案到 WordPress 媒體庫。

        Args:
            file_path: 本地檔案路徑或 URL。
            title: 媒體標題（可選）。

        Returns:
            Tuple[Optional[int], Optional[str]]: 媒體 ID 和媒體 URL。
        """
        if not self.validate_config():
            self.log("WordPress 配置無效，無法上傳媒體。", "error")
            return None, None
        
        base_url = self.config.wordpress_url
        username = self.config.wordpress_username
        app_password = self.config.wordpress_app_password
        api_url = urljoin(base_url, "wp-json/wp/v2/media")
        
        try:
            if file_path.startswith("http://") or file_path.startswith("https://"):
                file_response = requests.get(file_path, timeout=30)
                file_response.raise_for_status()
                file_data = file_response.content
                filename = title or file_path.split("/")[-1] or "image.png"
                content_type = file_response.headers.get("Content-Type", "application/octet-stream")
            else:
                with open(file_path, "rb") as f:
                    file_data = f.read()
                filename = title or file_path.split("/")[-1] or "image.png"
                content_type = "application/octet-stream"
            
            headers = {
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Type": content_type,
            }
            
            response = requests.post(
                api_url,
                auth=(username, app_password),
                data=file_data,
                headers=headers,
                timeout=60,
            )
            response.raise_for_status()
            media = response.json()
            media_id = media.get("id")
            media_url = media.get("source_url") or media.get("guid", {}).get("rendered")
            self.log(f"媒體上傳成功: media_id={media_id}")
            return media_id, media_url
        except Exception as e:
            self.log(f"媒體上傳失敗: {str(e)}", "error")
            return None, None
