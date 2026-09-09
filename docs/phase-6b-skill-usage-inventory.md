# Phase 6B — Agent Skill Usage Inventory

**Date:** 2026-09-08  
**Purpose:** Document all Agent `call_ai()` usage before modifying Agent code.

---

## Agent Inventory

| Agent | Method | Current Loading Mode | Current Skills | Actual Task Purpose | Recommended required_skills | Change Needed |
|-------|--------|---------------------|----------------|---------------------|-----------------------------|---------------|
| **WriterAgent** | `write_content()` | Default agent-level | Bencent + humanizer-tw (~2,041 tokens) | Article generation with brand voice and naturalization | Keep default or explicit `["Bencent", "humanizer-tw"]` | No change needed |
| **WriterAgent** | `refine_content()` | Explicit | Bencent only (~441 tokens) | Refine content based on feedback | Keep `["Bencent"]` | No change needed |
| **ImageAgent** | `_build_image_prompt()` | Default agent-level | Bencent + Taste v2 full + Minimalist (~24,185 tokens) | Generate DALL-E image prompt from article | Bencent + Taste sections + Minimalist | **YES — HIGH PRIORITY** |
| **ImageAgent** | `_build_request()` | No LLM | N/A | Build ImageGenerationRequest | N/A | No change needed |
| **ImageAgent** | `_upload_to_wordpress()` | No LLM | N/A | Upload to WordPress | N/A | No change needed |
| **QualityEvaluatorAgent** | `_score_content()` | Explicit | Bencent only (~441 tokens) | Score content quality 1-10 | Keep `["Bencent"]` | No change needed |
| **QualityEvaluatorAgent** | `_check_quality()` | Explicit | Bencent only (~441 tokens) | Check quality issues | Keep `["Bencent"]` | No change needed |
| **ContentFixerAgent** | `fix()` | Default agent-level | Bencent only (~441 tokens) | Fix content issues | Keep default | No change needed |
| **FinalReviewerAgent** | `final_review()` | Default agent-level | Bencent + humanizer-tw (~2,041 tokens) | Final style/AI-pattern review | Keep default or explicit `["Bencent", "humanizer-tw"]` | No change needed |
| **CriticAgent** | `critique()` | Default agent-level | Bencent + humanizer-tw (~2,041 tokens) | Self-critique, AI pattern detection | Keep default | No change needed |
| **PlannerAgent** | `create_plan()` | Default agent-level | Bencent only (~441 tokens) | Create content plan | Keep default | No change needed |
| **ResearchAgent** | `_generate_simulated_research()` | Default agent-level | Bencent only (~441 tokens) | Generate simulated research data | Keep default | No change needed |
| **SEOAgent** | `_optimize_content_body()` | Default agent-level | Bencent only (~441 tokens) | SEO content optimization | Keep default | No change needed |
| **SEOAgent** | `_generate_seo_metadata()` | Default agent-level | Bencent only (~441 tokens) | Generate SEO metadata | Keep default | No change needed |
| **LearnerAgent** | `analyze_review()` | Default agent-level | Bencent only (~441 tokens) | Analyze review changes | Keep default | No change needed |
| **Router** | `decide()` | No LLM | N/A | Route based on review result | N/A | No change needed |

---

## Summary

**Agents requiring modification:** 1 (ImageAgent)

**Agents reviewed but not modified:** 6 (WriterAgent, QualityEvaluatorAgent, ContentFixerAgent, FinalReviewerAgent, CriticAgent, PlannerAgent, ResearchAgent, SEOAgent, LearnerAgent)

**Reason for minimal changes:**
- All other Agents already load appropriate, minimal skill contexts
- WriterAgent `refine_content()` already uses explicit `["Bencent"]`
- QualityEvaluatorAgent already uses explicit `["Bencent"]`
- No other Agent loads large unnecessary Skills by default

---

## ImageAgent Detailed Analysis

### Current Behavior
- `_build_image_prompt()` uses default agent skills: Bencent + Taste v2 full + Minimalist
- Context size: ~24,185 tokens
- Taste v2 full content: ~88,868 characters (~22,217 tokens)

### Problem
Loading full Taste v2 for a 300-token image prompt is excessive context bloat.

### Solution
Use section-level Taste loading for only image-relevant sections:
- `48-image-visual-asset-strategy` — image generation priorities
- `9-ai-tells-forbidden-patterns` — anti-slop patterns
- `9a-visual-css` — visual/CSS rules
- `42-color-calibration` — color rules
- `9f-production-test-tells-banned-outright` — banned production patterns
- `hero-paradigms` — hero visual composition patterns
- Minimalist (full, 8KB is acceptable)
- Bencent (constitution, always)

Estimated context after fix: ~7,500-10,000 tokens

---

## Taste Section IDs for ImageAgent

| Section ID | Topic | Relevance |
|------------|-------|-----------|
| `48-image-visual-asset-strategy` | Image generation priorities | HIGH |
| `9-ai-tells-forbidden-patterns` | Anti-slop patterns | HIGH |
| `9a-visual-css` | Visual/CSS rules | HIGH |
| `42-color-calibration` | Color rules | MEDIUM |
| `9f-production-test-tells-banned-outright` | Banned patterns | HIGH |
| `hero-paradigms` | Hero visual patterns | MEDIUM |
