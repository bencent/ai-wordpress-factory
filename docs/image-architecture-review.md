# Image Architecture Review

**Date:** 2026-09-08  
**Reviewer:** Kilo  
**Scope:** Read-only architecture review — no code modified  
**Status:** COMPLETE

---

## 1. Current Image Architecture

### Implementation Facts

| Aspect | Current State |
|--------|--------------|
| **Agent** | `ImageAgent` (`agents/image.py`) |
| **Responsibility** | Prompt generation, image generation, WordPress upload — all in one class |
| **Input** | `Task` (uses `final_content`, `optimized_content`, `draft_content`, `title`) |
| **Output** | `Tuple[Optional[int], Optional[str]]` — WordPress media ID and media URL |
| **OpenAI Dependency** | Direct `openai.OpenAI` client call inside `_generate_image_with_dalle()` |
| **WordPress Dependency** | Direct `requests.post` to `wp-json/wp/v2/media` inside `_upload_to_wordpress()` |
| **Skill Injection** | Only `Bencent` brand skill is mapped to `image` agent in `skills/loader.py` |
| **State** | Uses `Task` fields: `image_prompt`, `image_url`, `hero_image_id`, `hero_image_url`, `image_status` |
| **Contracts** | No dedicated image contracts in `contracts.py` |

### Current Method Breakdown

```text
ImageAgent.generate_hero_image(task)
    ├── _build_image_prompt(title, content) → AI-generated DALL-E prompt
    ├── _generate_image_with_dalle(prompt) → OpenAI DALL-E 3 URL
    └── _upload_to_wordpress(image_url, title) → WordPress media_id + media_url
```

### Issues Identified

1. **Single Responsibility Violation** — `ImageAgent` handles prompt engineering, API communication, and platform integration
2. **Hardcoded Provider** — DALL-E 3 is hardcoded; no abstraction for alternative providers
3. **No Normalized Result** — Raw OpenAI response URL is passed directly to WordPress upload
4. **Limited Skill Context** — Only brand skill is injected; visual design skills are excluded
5. **No Image Contracts** — No structured request/result types for image generation
6. **Tight WordPress Coupling** — Upload logic lives inside the agent instead of a dedicated media tool

---

## 2. ImageAgent vs ImageProvider

### Evaluation

The proposed separation is **appropriate and recommended**.

### Recommended Boundaries

#### ImageAgent (Orchestrator)

| Responsible For | NOT Responsible For |
|-----------------|---------------------|
| Determine image purpose and use case | Direct API communication |
| Construct generation request | Provider-specific parameter translation |
| Select appropriate provider | Binary upload/download handling |
| Interpret generation result | WordPress REST API details |
| Apply brand/taste constraints | File format conversion |
| Decide aspect ratio and resolution | Network retry logic |

#### ImageProvider (Abstraction)

| Responsible For | NOT Responsible For |
|-----------------|---------------------|
| Communicate with image-generation API | Business logic / use case decisions |
| Translate provider-specific parameters | Brand/taste interpretation |
| Generate image | WordPress upload |
| Return normalized result | Prompt engineering |
| Handle provider-specific errors | Use case determination |

#### WordPress Media Tool (Existing)

| Responsible For | NOT Responsible For |
|-----------------|---------------------|
| Upload image to WordPress | Image generation |
| Create media attachment | Prompt engineering |
| Assign metadata | Provider selection |
| Return WordPress media ID | Image content decisions |

### Assessment

This separation is **correct** because:
- Each layer has a single, clear responsibility
- ImageProvider enables future provider additions without changing ImageAgent
- WordPress upload remains a tool concern, not an agent concern
- ImageAgent stays focused on "what image do we need and why"

---

## 3. Skill Loader Interaction

### Current State

`skills/loader.py` line 82 currently maps only `Bencent` to the `image` agent:

```python
"image": ["Bencent"],
```

### Recommended Skill Mapping

| Skill | ImageAgent | Reason |
|-------|-----------|--------|
| **Bencent Brand** | ✅ YES | Brand mood, prohibited visual directions, color palette, material textures |
| **Taste v2** | ✅ YES | Composition, visual hierarchy, anti-slop, art direction, layout principles |
| **Minimalist** | ✅ YES | Density control, whitespace discipline, component restraint — directly applies to image composition |
| **UI Rules** | ❌ NO | UI-specific rules (buttons, fonts, error states) — not relevant to image generation |
| **Humanizer TW** | ❌ NO | Chinese writing naturalization — irrelevant to image generation |
| **GreenLight** | ❌ NO | HTML/CSS/JS block conversion — not an image generation concern |
| **GSAP** | ❌ NO | Animation guidance — not relevant to static image generation |

### Rationale

- **Bencent Brand** provides the constitution-level visual identity. ImageAgent must know brand colors, prohibited elements, and material textures.
- **Taste v2** provides art direction and anti-slop guidance. This is critical for avoiding AI-default aesthetics in generated images.
- **Minimalist** provides density and composition constraints. ImageAgent should respect these when determining aspect ratio, negative space, and visual weight.

**UI Rules, Humanizer TW, GreenLight, and GSAP should NOT be injected into ImageAgent** — they operate at different layers (UI components, text, animation, block conversion).

---

## 4. Brand vs Taste Responsibility

### Layer Definitions

```
Bencent Brand → Taste → ImageAgent
```

#### Bencent Brand (Constitution Layer)

Controls:
- **Brand mood** — 職人質感、煙燻紙張、印刷墨水
- **Brand colors** — 墨炭 #2C2420, 赭石 #8B5E3C, 生土 #C4A882, 煙燻白 #F5F1EB
- **Prohibited visual directions** — 禁止純黑純白、禁止鮮豔橘紅、禁止3D AI罐頭圖
- **Material textures** — 紙張感、微粒感、印刷感
- **Typography feel** — 大標題緊字距、閱讀友善

**Does NOT control:**
- Composition rules
- Aspect ratio selection
- Specific layout decisions

#### Taste (Art Direction Layer)

Controls:
- **Composition** — 不對稱美感、大量留白、單一視覺重點
- **Visual hierarchy** — 主視覺與次要元素的比例
- **Visual density** — 避免擁擠、呼吸感
- **Anti-slop** — 避免AI預設美學、對稱card grid、漸層球體
- **Art direction** — 日式職人美學、編輯風格、手工感
- **Motion** — Not applicable to static images, but influences perceived dynamism

**Does NOT control:**
- Brand color values
- Brand prohibition lists
- WordPress-specific requirements

#### ImageAgent (Execution Layer)

Controls:
- **Use case** — Hero, featured, editorial, product, social, decorative
- **Image type** — Banner, illustration, photograph, abstract
- **Aspect ratio** — 16:9, 3:1, 1:1, etc.
- **Resolution** — 1792x1024, 1024x1024, etc.
- **Generation requirements** — Prompt construction, negative prompts
- **Provider selection** — OpenAI, FLUX, Recraft, etc.
- **Output handling** — Upload, metadata assignment

### Overlap Analysis

| Concern | Bencent Brand | Taste | ImageAgent | Overlap? |
|---------|--------------|-------|------------|----------|
| Color palette | ✅ Controls values | ✅ May constrain usage | ✅ Applies to generation | Low — Brand wins on values |
| Composition | ❌ | ✅ Primary control | ✅ Executes | None — clear boundary |
| Prohibited elements | ✅ Lists bans | ✅ Reinforces anti-slop | ✅ Enforces in prompt | Low — redundant but consistent |
| Density/whitespace | ❌ | ✅ Controls | ✅ Applies | None — clear boundary |
| Material texture | ✅ Defines | ❌ | ✅ Requests | None — clear boundary |

**Key Finding:** Bencent Brand and Taste v2 have **complementary, not conflicting, responsibilities**. Brand provides the "what" (colors, prohibitions, materials), Taste provides the "how" (composition, density, anti-slop). ImageAgent translates both into generation requests.

**Potential Conflict Zone:** Both Brand and Taste mention "minimalism" and "whitespace". Resolution: Taste's density/composition rules should be treated as constraints applied within Brand's color/material boundaries.

---

## 5. Image Use Cases

### Current State

`ImageAgent` currently supports only **Hero Image** generation. The state model has generic image fields but no use case differentiation.

### Recommended Use Cases

| Use Case | Prompt Change | Aspect Ratio | Resolution | Style | Provider | Output Handling |
|----------|--------------|--------------|------------|-------|----------|-----------------|
| **Hero Image** | Article-topic-driven, brand-aligned | 16:9 or 3:1 | 1792x1024 | Editorial, atmospheric | OpenAI DALL-E 3 | Featured media |
| **Featured Image** | Social-shareable, topic-focused | 16:9 | 1792x1024 | Clean, recognizable | OpenAI DALL-E 3 | Featured media |
| **Editorial Illustration** | Concept-driven, metaphor-rich | 4:3 or 16:9 | 1792x1024 | Artistic, editorial | OpenAI DALL-E 3 | In-content media |
| **Product Image** | Product-focused, clean background | 1:1 or 4:5 | 1024x1024 | Studio, minimal | OpenAI DALL-E 3 / Recraft | Media library |
| **Social Media Image** | Attention-grabbing, text-safe | 1:1 or 9:16 | 1024x1024 | Bold, simple | OpenAI DALL-E 3 | Media library |
| **Background/Decorative** | Abstract, texture-focused | 21:9 or custom | 1792x1024 | Subtle, atmospheric | OpenAI DALL-E 3 / FLUX | Media library |

### Impact Analysis

Each use case changes:

1. **Prompt** — Different descriptive focus, different negative prompts
2. **Aspect Ratio** — Provider-specific size parameter
3. **Resolution** — Provider-specific size parameter
4. **Style** — Brand/taste constraints applied differently
5. **Provider** — Some use cases may benefit from different providers (e.g., Recraft for product images)
6. **Output Handling** — Featured media vs. in-content vs. media library only

### Recommendation

ImageAgent should accept a `use_case` parameter (or derive it from `ContentType` and context). Default remains `HERO_IMAGE` for backward compatibility.

---

## 6. Provider Strategy

### MVP Recommendation

**YES**, start with single provider:

```text
ImageAgent
     ↓
ImageProvider (OpenAI only)
     ↓
OpenAI DALL-E 3
     ↓
Image Result
     ↓
WordPress Media
```

### Rationale

- Current implementation already uses DALL-E 3 exclusively
- Adding provider abstraction before need creates premature complexity
- Single provider validates the workflow before adding abstraction layers
- Provider switching is a future optimization, not an MVP requirement

### Future Strategy

After MVP validation, introduce `ImageProvider` interface:

```text
ImageAgent
     ↓
ImageProvider (interface)
     ├── OpenAIProvider (DALL-E 3)
     ├── FluxProvider (FLUX.1)
     ├── RecraftProvider (Recraft V2)
     └── GeminiProvider (Imagen)
     ↓
Normalized ImageResult
     ↓
WordPress Media
```

**Do NOT add providers now.** Wait until:
1. MVP workflow is stable
2. There is a demonstrated need (cost, quality, or capability gap)
3. The abstraction layer has been validated with a single provider

---

## 7. WordPress Separation

### Current Architecture

```text
ImageAgent
    ↓
OpenAI
    ↓
WordPress (direct upload in agent)
```

### Recommended Architecture

```text
ImageAgent
    ↓
ImageProvider (MVP: OpenAI only)
    ↓
Image Result (normalized)
    ↓
WordPressPublisher (existing tool)
    ↓
WordPress Media
```

### Assessment

The current architecture **should change** to use the existing `WordPressPublisher` tool.

**Reasoning:**
- `WordPressPublisher` already exists in `tools/wordpress.py`
- It already has `upload_media()` method (line 249)
- ImageAgent should not contain platform-specific upload logic
- Separation allows ImageAgent to be tested independently of WordPress
- Separation allows WordPressPublisher to handle all media concerns (retry, metadata, validation)

### Specific Change Required

`ImageAgent._upload_to_wordpress()` should be replaced with a call to `WordPressPublisher.upload_media()`.

**Current coupling:**
- ImageAgent imports `requests` directly
- ImageAgent constructs WordPress API URLs
- ImageAgent handles auth credentials
- ImageAgent parses WordPress response format

**Proposed coupling:**
- ImageAgent receives `WordPressPublisher` instance or calls it through dependency injection
- ImageAgent passes `image_url` and `title`
- WordPressPublisher handles all WordPress-specific logic

---

## 8. Enterprise Requirements

### MUST HAVE

| Requirement | Reason | Current State |
|-------------|--------|---------------|
| **Normalized generation result** | Enables provider switching and consistent handling | ❌ Raw OpenAI URL passed directly |
| **Error handling** | Network failures, API errors, rate limits | ⚠️ Basic try/except, no typed errors |
| **Provider abstraction** | Future multi-provider support | ❌ Hardcoded DALL-E 3 |
| **Retry compatibility** | Transient failures should retry | ❌ No retry logic |
| **Metadata** | Track generation details for audit/cost | ❌ Only `image_status` string |

### SHOULD HAVE

| Requirement | Reason | Current State |
|-------------|--------|---------------|
| **Cost tracking** | Track API costs per generation | ❌ Not tracked |
| **Model tracking** | Know which model generated each image | ❌ Not tracked |
| **Prompt version** | Track prompt iterations | ⚠️ Prompt generated but not stored versioned |
| **Provider fallback** | Fallback to alternative if primary fails | ❌ No fallback |
| **Observability** | Logging, metrics, tracing | ⚠️ Basic logging only |

### FUTURE

| Requirement | Reason |
|-------------|--------|
| **Image validation** | Validate generated images meet requirements before upload |
| **A/B generation** | Generate multiple variants and select best |
| **Image editing** | Post-generation editing capabilities |
| **Asset management** | Image library, reuse, deduplication |
| **CDN integration** | Direct CDN upload instead of WordPress media library |

---

## 9. Contract Proposal

### Conceptual Contracts Only

#### ImageGenerationRequest

```python
@dataclass
class ImageGenerationRequest:
    task_id: str
    use_case: str  # "hero", "featured", "editorial", "product", "social", "background"
    title: str
    content_summary: str
    aspect_ratio: str  # "16:9", "3:1", "1:1", "4:5", "9:16"
    resolution: str  # "1792x1024", "1024x1024"
    style_constraints: Dict[str, Any]  # Brand colors, prohibited elements, etc.
    negative_prompt: str
    provider: str  # "openai", "flux", "recraft", "gemini"
    model: str  # "dall-e-3", "flux-1.1-pro", etc.
    quality: str  # "standard", "hd"
    metadata: Dict[str, Any]  # Prompt version, cost center, etc.
```

#### ImageGenerationResult

```python
@dataclass
class ImageGenerationResult:
    task_id: str
    success: bool
    provider: str
    model: str
    image_url: Optional[str]
    image_bytes: Optional[bytes]
    content_type: str
    width: int
    height: int
    prompt_used: str
    tokens_used: Optional[int]
    cost_estimate: Optional[float]
    error: Optional[str]
    generation_time_ms: Optional[int]
```

#### ImageProvider (Interface)

```python
class ImageProvider:
    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        """Generate an image based on the request."""
        raise NotImplementedError
    
    def supported_models(self) -> List[str]:
        """Return list of supported model identifiers."""
        raise NotImplementedError
    
    def supported_aspect_ratios(self) -> List[str]:
        """Return list of supported aspect ratio strings."""
        raise NotImplementedError
```

### Responsibility Summary

| Contract | Owner | Purpose |
|----------|-------|---------|
| `ImageGenerationRequest` | ImageAgent | Structured input to provider |
| `ImageGenerationResult` | ImageProvider | Normalized output from provider |
| `ImageProvider` | Provider implementations | Abstraction boundary |

---

## 10. Recommended Architecture

### MVP Architecture

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

### Future Architecture

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
                   (interface)
                         │
           ┌─────────────┼─────────────┐
           │             │             │
      OpenAIProvider  FluxProvider  RecraftProvider
           │             │             │
           └─────────────┼─────────────┘
                         ↓
                  ImageResult
                   (normalized)
                         │
                WordPress Media
```

---

## 11. Risks / Overengineering

### Current Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| ImageAgent does too much | Medium | Split into orchestrator + provider + tool usage |
| Hardcoded DALL-E 3 | Medium | Introduce ImageProvider abstraction |
| No image contracts | Low | Add ImageGenerationRequest/Result in next phase |
| Limited skill context | Low | Add Taste and Minimalist to image agent mapping |

### Overengineering Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Adding multiple providers before MVP | High | Stay single-provider until workflow is stable |
| Adding image validation/A-B testing | High | Defer to FUTURE phase |
| Complex provider fallback logic | Medium | Add only when second provider exists |
| Image asset management system | High | Not needed for MVP content generation |

### What NOT To Do Now

1. **Do NOT add FLUX/Recraft/Gemini providers** — validate DALL-E 3 workflow first
2. **Do NOT build image validation pipeline** — add after basic workflow works
3. **Do NOT create A/B generation** — single image per article is sufficient for MVP
4. **Do NOT build image asset management** — WordPress media library is sufficient
5. **Do NOT add GreenLight/GSAP to ImageAgent** — irrelevant to image generation

---

## 12. Implementation Priority

### Phase 1: Immediate (No Code Change Required)

1. Update `skills/loader.py` — add Taste and Minimalist to image agent mapping
2. Document use case matrix for future reference

### Phase 2: Next Sprint

1. Create `ImageGenerationRequest` and `ImageGenerationResult` contracts
2. Create `OpenAIProvider` class with normalized output
3. Refactor `ImageAgent` to use provider abstraction
4. Replace direct WordPress upload with `WordPressPublisher.upload_media()`

### Phase 3: Future

1. Add additional providers (FLUX, Recraft, Gemini)
2. Add use case differentiation in ImageAgent
3. Add cost tracking and model tracking
4. Add provider fallback logic

---

## Summary

The current `ImageAgent` is functional but violates separation of concerns by combining prompt engineering, API communication, and platform upload in a single class. The recommended architecture introduces:

1. **ImageProvider abstraction** — separates provider communication from agent orchestration
2. **Skill expansion** — add Taste and Minimalist to image agent skill mapping
3. **WordPress separation** — use existing `WordPressPublisher` tool instead of inline upload
4. **Contracts** — introduce normalized request/result types
5. **Use case awareness** — differentiate hero, featured, editorial, product, social, decorative

**MVP should remain simple:** single provider (OpenAI/DALL-E 3), single use case (hero image), existing WordPress tool for upload.

**IMAGE ARCHITECTURE REVIEW COMPLETE**
