# AI WordPress Factory 圖片生成提供者
# 负责图像生成 API 抽象与 OpenAI 实现

from abc import ABC, abstractmethod
from domain.ai_runtime import RuntimeOnly, classify_error, ProviderFailure, ErrorCode
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


class OpenAIImageProvider(ImageProvider, RuntimeOnly):
    """OpenAI DALL-E 3 图像生成提供者。"""

    def __init__(self, config, *, model=None):
        self.__api_key = getattr(config,"openai_api_key",None)
        self.__model = model

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        try:
            from providers.sdk_client import create_client

            api_key = self.__api_key
            if not api_key:
                return ImageGenerationResult(
                    task_id=request.task_id,
                    success=False,
                    provider="openai",
                    model=self.__model or request.model,
                    error=ProviderFailure(ErrorCode.AUTHENTICATION).safe_summary,
                )

            client = create_client(api_key)
            response = client.images.generate(
                model=self.__model or request.model,
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
                model=self.__model or request.model,
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
                model=self.__model or request.model,
                error=classify_error(e).safe_summary,
            )
