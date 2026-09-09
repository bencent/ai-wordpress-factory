# Image Architecture

**Date:** 2026-09-08  
**Status:** Implemented

## Final Dependency Structure

```
                    ImageAgent
                         │
           ┌─────────────┼─────────────┐
           │             │             │
      Brand/Taste    Use Case     WordPressPublisher
           │             │             │
           └─────────────┼─────────────┘
                         ↓
                  ImageProvider
                   (OpenAI MVP)
                         │
                  Image Result
                   (normalized)
                         │
                WordPress Media
```

## Components

| Component | Responsibility |
|-----------|----------------|
| **ImageAgent** | Orchestration: prompt engineering, request construction, brand/taste constraints, provider selection |
| **ImageProvider** | Abstraction: API communication, parameter translation, normalized result |
| **OpenAIImageProvider** | OpenAI DALL-E 3 implementation |
| **WordPressPublisher** | Media upload via existing `upload_media()` |
| **Contracts** | `ImageGenerationRequest`, `ImageGenerationResult` |

## Skill Mapping

| Skill | ImageAgent |
|-------|-----------|
| Bencent Brand | ✅ YES |
| Taste v2 | ✅ YES |
| Minimalist | ✅ YES |
| UI Rules | ❌ NO |
| Humanizer TW | ❌ NO |
| GreenLight | ❌ NO |
| GSAP | ❌ NO |

## Files

- `contracts.py` — added `ImageGenerationRequest`, `ImageGenerationResult`
- `providers/image_provider.py` — `ImageProvider` base + `OpenAIImageProvider`
- `agents/image.py` — refactored to use provider + WordPressPublisher
- `skills/loader.py` — updated image skill mapping
