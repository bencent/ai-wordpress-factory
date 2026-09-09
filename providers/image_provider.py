# AI WordPress Factory 圖片生成提供者
# 负责图像生成 API 抽象与 OpenAI 实现

from abc import ABC, abstractmethod
from typing import Optional
from contracts import ImageGenerationRequest, ImageGenerationResult


class ImageProvider(ABC):
    """图像生成提供者抽象基类。"""

    @abstractmethod
    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        """根据请求生成图像。

        Args:
            request: 图像生成请求。

        Returns:
            ImageGenerationResult: 标准化生成结果。
        """
        raise NotImplementedError


class OpenAIImageProvider(ImageProvider):
    """OpenAI DALL-E 3 图像生成提供者。"""

    def __init__(self, config):
        self.config = config

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        try:
            import openai

            api_key = getattr(self.config, "openai_api_key", None)
            if not api_key:
                return ImageGenerationResult(
                    task_id=request.task_id,
                    success=False,
                    provider="openai",
                    model=request.model,
                    error="OpenAI API Key 未配置",
                )

            client = openai.OpenAI(api_key=api_key)
            response = client.images.generate(
                model=request.model,
                prompt=request.prompt_used or request.content_summary,
                size=request.resolution,
                quality=request.quality,
                n=1,
            )
            image_url = response.data[0].url

            width, height = 1792, 1024
            if request.resolution and "x" in request.resolution:
                parts = request.resolution.split("x")
                if len(parts) == 2:
                    try:
                        width, height = int(parts[0]), int(parts[1])
                    except ValueError:
                        pass

            return ImageGenerationResult(
                task_id=request.task_id,
                success=True,
                provider="openai",
                model=request.model,
                image_url=image_url,
                content_type="image/png",
                width=width,
                height=height,
                prompt_used=request.prompt_used or "",
            )
        except Exception as e:
            return ImageGenerationResult(
                task_id=request.task_id,
                success=False,
                provider="openai",
                model=request.model,
                error=str(e),
            )
