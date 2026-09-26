# AI WordPress Factory 圖片代理人
# 负責依據文章內容生成 hero banner 圖片

from typing import Optional, Dict, Any, Tuple
from state import Task
from contracts import ImageGenerationRequest, ImageGenerationResult
from . import BaseAgent
from domain.ai_runtime import ProviderFailure, ErrorCode, classify_error


class ImageAgent(BaseAgent):
    """圖片代理人，負責根據文章內容生成 hero banner 圖片。"""

    def __init__(self, config, *, providers=None, upload_media=None):
        super().__init__(config, providers=providers)
        self.description = "負責根據文章內容生成 hero banner 圖片並上傳到 WordPress。"
        self.provider = self._providers.image
        self._upload_media = upload_media

    def generate_hero_image(self, task: Task) -> Tuple[Optional[int], Optional[str]]:
        """為文章生成 hero banner 圖片並上傳到 WordPress 作為精選圖片。

        Args:
            task: 任務對象。

        Returns:
            Tuple[Optional[int], Optional[str]]: 媒體 ID 和媒體 URL。
        """
        if self.provider is None:
            raise ProviderFailure(ErrorCode.UNSUPPORTED_CAPABILITY)
        content = task.final_content or task.optimized_content or task.draft_content or ""
        title = task.title or "文章標題"

        prompt = self._build_image_prompt(title, content)
        request = self._build_request(task, prompt)
        try:
            result = self.provider.generate(request)
        except Exception as error:
            raise classify_error(error) from None

        if not result.success or not result.image_url:
            task.image_status = "failed"
            return None, None

        media_id, media_url = self._upload_to_wordpress(result.image_url, title)
        task.image_prompt = prompt
        task.image_url = result.image_url
        task.hero_image_id = media_id
        task.hero_image_url = media_url
        task.image_status = "success" if media_id else "failed"
        return media_id, media_url

    def _build_request(self, task: Task, prompt: str) -> ImageGenerationRequest:
        """建立圖像生成請求。

        Args:
            task: 任務對象。
            prompt: 圖片生成提示詞。

        Returns:
            ImageGenerationRequest: 結構化生成請求。
        """
        return ImageGenerationRequest(
            task_id=task.id,
            use_case="hero",
            title=task.title or "文章標題",
            content_summary=(task.final_content or task.optimized_content or task.draft_content or "")[:800],
            resolution="1792x1024",
            aspect_ratio="16:9",
            prompt_used=prompt,
            provider="openai",
            model="dall-e-3",
            quality="standard",
            metadata={"agent": "image"},
        )

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
        ai_prompt = self.call_ai(
            prompt,
            required_skills=[
                "Bencent",
                "design-taste-frontend:48-image-visual-asset-strategy",
                "design-taste-frontend:9-ai-tells-forbidden-patterns",
                "design-taste-frontend:9a-visual-css",
                "design-taste-frontend:42-color-calibration",
                "design-taste-frontend:9f-production-test-tells-banned-outright",
                "design-taste-frontend:hero-paradigms",
                "minimalist-ui",
            ],
            temperature=0.7,
            max_tokens=300,
        )
        return ai_prompt.strip()

    def _upload_to_wordpress(self, image_url: str, title: str) -> Tuple[Optional[int], Optional[str]]:
        """將圖片上傳到 WordPress 媒體庫。

        Args:
            image_url: 圖片 URL。
            title: 圖片標題。

        Returns:
            Tuple[Optional[int], Optional[str]]: 媒體 ID 和媒體 URL。
        """
        if self._upload_media is None:
            raise ProviderFailure(ErrorCode.UNAVAILABLE)
        try:
            return self._upload_media(image_url,title)
        except Exception:
            raise ProviderFailure(ErrorCode.UNAVAILABLE) from None
