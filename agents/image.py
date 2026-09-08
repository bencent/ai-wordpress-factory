# AI WordPress Factory 圖片代理人
# 负責依據文章內容生成 hero banner 圖片

from typing import Optional, Dict, Any, Tuple
from state import Task
from . import BaseAgent


class ImageAgent(BaseAgent):
    """圖片代理人，負責根據文章內容生成 hero banner 圖片。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "負責根據文章內容生成 hero banner 圖片並上傳到 WordPress。"

    def generate_hero_image(self, task: Task) -> Tuple[Optional[int], Optional[str]]:
        """為文章生成 hero banner 圖片並上傳到 WordPress 作為精選圖片。

        Args:
            task: 任務對象。

        Returns:
            Tuple[Optional[int], Optional[str]]: 媒體 ID 和媒體 URL。
        """
        content = task.final_content or task.optimized_content or task.draft_content or ""
        title = task.title or "文章標題"

        prompt = self._build_image_prompt(title, content)
        image_url = self._generate_image_with_dalle(prompt)
        if not image_url:
            return None, None

        media_id, media_url = self._upload_to_wordpress(image_url, title)
        return media_id, media_url

    def _build_image_prompt(self, title: str, content: str) -> str:
        """根據文章標題與內容建立圖片生成提示詞。

        Args:
            title: 文章標題。
            content: 文章內容。

        Returns:
            str: 圖片生成提示詞。
        """
        prompt_template = self.get_prompt("image")
        if not prompt_template:
            prompt_template = """
            你是一位視覺設計專家。請根據以下文章內容，寫一段英文的 DALL-E 圖片生成提示詞，用於生成一張 1200x630 的 hero banner。

            文章標題: {title}
            文章內容摘要: {content}

            要求：
            1. 風格：極簡編輯風、低飽和大地色調、職人質感、煙燻紙張感
            2. 禁止：鮮豔橘紅、霓虹色、3D AI 罐頭圖、純黑純白
            3. 構圖：寬版橫幅、大量留白、單一視覺重點、印刷版面感
            4. 色調：墨炭 #2C2420、赭石 #8B5E3C、生土 #C4A882、煙燻白 #F5F1EB
            5. 直接輸出英文提示詞，不要加任何解釋
            6. 提示詞結尾加上： --no text watermark logo signature --style raw
            """

        content_summary = content[:800] if len(content) > 800 else content
        prompt = prompt_template.format(title=title, content=content_summary)
        ai_prompt = self.call_ai(prompt, temperature=0.7, max_tokens=300)
        return ai_prompt.strip()

    def _generate_image_with_dalle(self, prompt: str) -> Optional[str]:
        """使用 DALL-E 生成圖片。

        Args:
            prompt: 圖片生成提示詞。

        Returns:
            Optional[str]: 生成圖片的 URL，失敗則返回 None。
        """
        try:
            import openai
            api_key = getattr(self.config, "openai_api_key", None)
            if not api_key:
                self.log("OpenAI API Key 未配置，無法生成圖片", "error")
                return None

            client = openai.OpenAI(api_key=api_key)
            response = client.images.generate(
                model="dall-e-3",
                prompt=prompt,
                size="1792x1024",
                quality="standard",
                n=1,
            )
            image_url = response.data[0].url
            self.log(f"DALL-E 圖片生成成功")
            return image_url
        except Exception as e:
            self.log(f"DALL-E 圖片生成失敗: {str(e)}", "error")
            return None

    def _upload_to_wordpress(self, image_url: str, title: str) -> Tuple[Optional[int], Optional[str]]:
        """將圖片上傳到 WordPress 媒體庫。

        Args:
            image_url: 圖片 URL。
            title: 圖片標題。

        Returns:
            Tuple[Optional[int], Optional[str]]: 媒體 ID 和媒體 URL。
        """
        try:
            import requests
            from urllib.parse import urljoin

            base_url = self.config.wordpress_url
            username = self.config.wordpress_username
            app_password = self.config.wordpress_app_password

            api_url = urljoin(base_url, "wp-json/wp/v2/media")

            image_response = requests.get(image_url, timeout=30)
            image_response.raise_for_status()

            filename = f"{title}.png"
            content_type = image_response.headers.get("Content-Type", "image/png")

            headers = {
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Type": content_type,
                "Content-Length": str(len(image_response.content)),
            }

            response = requests.post(
                api_url,
                auth=(username, app_password),
                data=image_response.content,
                headers=headers,
                timeout=60,
            )
            response.raise_for_status()
            media = response.json()
            media_id = media.get("id")
            media_url = media.get("source_url") or media.get("guid", {}).get("rendered")
            self.log(f"圖片上傳成功: media_id={media_id}")
            return media_id, media_url
        except Exception as e:
            self.log(f"圖片上傳失敗: {str(e)}", "error")
            return None, None
