# Phase 6B — Agent Skill Integration

**Date:** 2026-09-08  
**Status:** Complete

---

## 1. Phase 6B Goal

Move from automatic Agent-level skill loading to method-level explicit skill selection.

**Before:**
```text
Agent
    ↓
call_ai()
    ↓
all mapped skills
    ↓
LLM
```

**After:**
```text
Agent
    ↓
Method
    ↓
Task Responsibility
    ↓
Minimum Required Skills
    ↓
LLM
```

---

## 2. Agents Reviewed

| Agent | Methods with call_ai() | Reviewed |
|-------|------------------------|----------|
| WriterAgent | `write_content()`, `refine_content()` | Yes |
| ImageAgent | `_build_image_prompt()` | Yes |
| QualityEvaluatorAgent | `_score_content()`, `_check_quality()` | Yes |
| ContentFixerAgent | `fix()` | Yes |
| FinalReviewerAgent | `final_review()` | Yes |
| CriticAgent | `critique()` | Yes |
| PlannerAgent | `create_plan()` | Yes |
| ResearchAgent | `_generate_simulated_research()` | Yes |
| SEOAgent | `_optimize_content_body()`, `_generate_seo_metadata()` | Yes |
| LearnerAgent | `analyze_review()` | Yes |
| Router | None | N/A |

---

## 3. Agents Modified

### ImageAgent

**Method:** `_build_image_prompt()`

**Before:**
- Used default agent skills: Bencent + Taste v2 full + Minimalist
- Context size: ~24,185 tokens

**After:**
- Uses explicit `required_skills`:
  - Bencent
  - `design-taste-frontend:48-image-visual-asset-strategy`
  - `design-taste-frontend:9-ai-tells-forbidden-patterns`
  - `design-taste-frontend:9a-visual-css`
  - `design-taste-frontend:42-color-calibration`
  - `design-taste-frontend:9f-production-test-tells-banned-outright`
  - `design-taste-frontend:hero-paradigms`
  - `minimalist-ui`
- Context size: ~3,449 tokens (estimated)

**Reason:** Full Taste v2 is 87 KB and was being injected into every image prompt generation. Only 6 specific sections are relevant to image/visual direction.

**Also changed:** Updated ImageAgent default mapping from `["Bencent", "design-taste-frontend", "minimalist-ui"]` to `["Bencent", "minimalist-ui"]` to prevent accidental full Taste loading in future methods.

### WriterAgent

**Method:** `write_content()`

**Before:** Default agent skills (Bencent + humanizer-tw)

**After:** No change — default is appropriate for article generation

**Method:** `refine_content()`

**Before:** Already used explicit `["Bencent"]`

**After:** No change

**Reason:** Article writing genuinely requires brand voice and naturalization. No optimization needed.

### QualityEvaluatorAgent

**Method:** `_score_content()`

**Before:** Already used explicit `["Bencent"]`

**After:** No change

**Method:** `_check_quality()`

**Before:** Already used explicit `["Bencent"]`

**After:** No change

**Reason:** Quality scoring needs brand compliance context only.

### ContentFixerAgent

**Method:** `fix()`

**Before:** Default agent skills (Bencent only)

**After:** No change

**Reason:** Content repair is mechanical and needs minimal context.

### FinalReviewerAgent

**Method:** `final_review()`

**Before:** Default agent skills (Bencent + humanizer-tw)

**After:** No change

**Reason:** Final review needs both brand compliance and AI-pattern detection.

### CriticAgent, PlannerAgent, ResearchAgent, SEOAgent, LearnerAgent

**No changes needed.** All use appropriate default skill contexts.

---

## 4. ImageAgent Optimization

### Before

| Aspect | Value |
|--------|-------|
| Default agent skills | Bencent + Taste v2 full + Minimalist |
| Default context tokens | ~24,185 |
| Prompt-building context | Inherits all default skills |
| Full Taste loaded | Yes |

### After

| Aspect | Value |
|--------|-------|
| Default agent skills | Bencent + Minimalist |
| Default context tokens | ~2,409 |
| Prompt-building context | Bencent + 6 Taste sections + Minimalist |
| Prompt-building context tokens | ~3,449 |
| Full Taste loaded | No |

### Selected Taste Sections

| Section ID | Topic | Tokens |
|------------|-------|-------:|
| `48-image-visual-asset-strategy` | Image generation priorities | ~965 |
| `9-ai-tells-forbidden-patterns` | Anti-slop patterns | ~25 |
| `9a-visual-css` | Visual/CSS rules | ~92 |
| `42-color-calibration` | Color rules | ~765 |
| `9f-production-test-tells-banned-outright` | Banned production patterns | ~2,018 |
| `hero-paradigms` | Hero visual patterns | ~115 |

### Full Taste Removal

Full Taste v2 has been removed from:
1. ImageAgent default agent mapping
2. ImageAgent `_build_image_prompt()` explicit skill selection

Full Taste v2 remains available via explicit request:
```python
required_skills=["design-taste-frontend"]
```

---

## 5. Context Reduction

| Agent | Method | Before Skills | After Skills | Before Est. Tokens | After Est. Tokens | Reduction |
|-------|--------|--------------|--------------|-------------------:|------------------:|----------:|
| ImageAgent | `_build_image_prompt()` | Bencent + Taste full + Minimalist | Bencent + 6 Taste sections + Minimalist | ~24,185 | ~3,449 | ~86% |
| ImageAgent | Default context | Bencent + Taste full + Minimalist | Bencent + Minimalist | ~24,185 | ~2,409 | ~90% |

**Note:** Other Agents did not require changes. Their contexts were already appropriate.

---

## 6. Tests Performed

### Existing Tests
- `tests/skill_loader_diagnostics.py` — all 60+ checks pass
  - Discovery, Constitution, Priority, Deduplication, Tool Bindings, Agent Defaults, Granular Selection, Metadata, Recursive Discovery, GreenLight Instructions, Taste Sections, Sub-skill Selection, Backward Compatibility

### New Integration Tests
- `tests/phase6b_integration.py` — 7 tests pass
  1. ImageAgent uses explicit skills in `_build_image_prompt()`
  2. ImageAgent default context is small (< 5,000 tokens)
  3. ImageAgent prompt context excludes full Taste
  4. WriterAgent `refine_content()` uses explicit `["Bencent"]`
  5. QualityEvaluatorAgent `_score_content()` uses explicit `["Bencent"]`
  6. Constitution appears first in mixed contexts
  7. No GreenLight or GSAP leakage into default agent contexts

### Syntax Checks
- All modified files compile cleanly

### Import Checks
- All modified modules import successfully

---

## 7. Existing Project Impact

- **No workflow changes** — main.py, state.py, contracts.py unchanged
- **No Router changes** — Router unchanged
- **No Reviewer architecture changes** — QualityEvaluator, ContentFixer, FinalReviewer logic unchanged
- **No ImageProvider changes** — OpenAI provider, WordPress upload unchanged
- **No Skill content changes** — All skill files remain untouched
- **Backward compatible** — All existing `required_skills` lists continue to work

---

## 8. Remaining Technical Debt

1. **WriterAgent `write_content()`** — still uses default skills. Could be optimized further but current context (~2,041 tokens) is acceptable.
2. **FinalReviewerAgent** — still uses default skills. Could use explicit `["Bencent", "humanizer-tw"]` for clarity but not required.
3. **CriticAgent** — still uses default skills. Appropriate for its task.
4. **Taste section IDs** — some IDs are verbose (e.g., `0-brief-inference-read-the-room-before-anything-else`). Future: consider shorter aliases.
5. **Skill metadata** — `_SKILL_METADATA` is hardcoded. Future: could be auto-generated from frontmatter.

---

## 9. Git Status

### Modified Files
- `skills/loader.py` — changed ImageAgent default mapping
- `agents/image.py` — added explicit `required_skills` in `_build_image_prompt()`
- `tests/skill_loader_diagnostics.py` — updated expected ImageAgent defaults
- `docs/phase-6b-skill-usage-inventory.md` — new inventory document
- `tests/phase6b_integration.py` — new integration tests

### New Files
- `docs/phase-6b-agent-skill-integration.md` — this document
- `tests/phase6b_integration.py` — integration tests

### Deleted Files
- None

---

## 10. Recommended Next Phase

**Phase 6C — FrontendAgent Preparation**

The loader infrastructure is now ready to support FrontendAgent with:
- Section-level Taste loading
- Sub-skill GSAP loading
- Instruction-level GreenLight loading
- Explicit skill selection per method

FrontendAgent itself should not be created until Phase 6C is planned and approved.
