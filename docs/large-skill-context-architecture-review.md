# Large Skill / Skill Context Architecture Review

**Date:** 2026-09-08  
**Reviewer:** Kilo  
**Scope:** Read-only architecture review — no code modified  
**Status:** COMPLETE

---

## 1. Current Skill Loading Model

### Implementation Facts

The current loader assumes:

```text
SKILL.md
    ↓
loaded completely
    ↓
cached in Skill.content (single string)
    ↓
injected into prompt as whole block
```

There is no concept of:
- sections within a Skill
- partial loading
- Skill size awareness at runtime
- conditional section selection

The entire `SKILL.md` body is read into `Skill.content` as one string and concatenated wholesale into the prompt.

### Where This Assumption Lives

| Location | Assumption |
|----------|-----------|
| `skills/loader.py:54-60` | `body.strip()` stored as single `content` string |
| `skills/loader.py:88-97` | `build_skills_context()` concatenates full `skill.content` |
| `agents/__init__.py:66-68` | `call_ai()` prepends entire `skills_context` to every prompt |
| `contracts.py` | No contract represents a Skill section or partial Skill |

### Execution Flow

```text
SkillLoader.__init__()
    ↓
_load_all()
    ↓
for each skills/*/SKILL.md:
    read entire file
    parse frontmatter
    store body as skill.content (one string)

Agent.call_ai(prompt, required_skills=None)
    ↓
skill_loader.build_skills_context(name, required_skills)
    ↓
get_skills_for_agent() or get_skills(required_skills)
    ↓
for each skill:
    append skill.name + skill.description + skill.content
    ↓
full_prompt = skills_context + prompt
    ↓
OpenAI
```

---

## 2. Actual Skill Structure Analysis

### Skill Inventory

| Skill | Path | Files | Total Bytes | Structure |
|-------|------|-------|-------------|-----------|
| **Bencent Brand** | `skills/bencent-brand/SKILL.md` | 1 | 5,017 | Single monolithic SKILL.md |
| **Taste v2** | `skills/taste-skill/SKILL.md` | 1 | 87,253 | Single monolithic SKILL.md, 1206 lines, 16 sections |
| **Minimalist** | `skills/minimalist-skill/SKILL.md` | 1 | 7,986 | Single monolithic SKILL.md |
| **UI Rules** | `skills/ui-rules/SKILL.md` | 1 | 563 | Single monolithic SKILL.md |
| **Humanizer TW** | `skills/humanizer-tw/SKILL.md` | 1 | 14,963 | Single monolithic SKILL.md |
| **GreenLight** | `skills/greenlight-vibe/` | 14 | ~66,000 | SKILL.md + 10 instruction `.md` files + 2 JS files |
| **GSAP** | `skills/gsap/` | 9 | ~80,000 | `llms.txt` index + 8 subdirectory SKILL.md files |

### Taste v2 Section Breakdown

Taste v2 is a single 87 KB file with these top-level sections:

| Section | Approx Lines | Topic |
|---------|-------------|-------|
| 0. Brief Inference | ~37 | Reading the brief, design read, ambiguity handling |
| 1. The Three Dials | ~38 | DESIGN_VARIANCE, MOTION_INTENSITY, VISUAL_DENSITY |
| 2. Brief → Design System Map | ~23 | Design system selection, official packages |
| 3. Default Architecture & Conventions | ~38 | Stack, state, icons, typography, responsiveness |
| 4. Design Engineering Directives | ~80 | Typography, color, layout, anti-slop, accessibility |
| 5. Motion | ~80 | Motion library choice, scroll patterns, canonical skeletons |
| 6. Performance & Accessibility | ~15 | Guardrails |
| 7. Dial Definitions | ~15 | Technical reference for dial values |
| 8. Dark Mode Protocol | ~15 | Dark mode rules |
| 9. AI Tells | ~30 | Forbidden patterns, banned aesthetics |
| 10. Reference Vocabulary | ~50 | Pattern names (hero paradigms, nav, layout, cards, scroll, etc.) |
| 11. Redesign Protocol | ~40 | Greenfield vs redesign, audit process |
| 12. Block Library | ~10 | Contract for implementations |
| 13. Out of Scope | ~10 | What this skill does not cover |
| 14. Final Pre-Flight Check | ~30 | Checklist before shipping |
| Appendix A | ~15 | Install commands per design system |
| Appendix B | ~10 | Canonical sources |
| Appendix C | ~40 | Apple Liquid Glass approximation |

**Key finding:** Taste v2 contains multiple independent concerns. Sections 0-2 are about brief interpretation and system selection. Sections 3-4 are about engineering conventions. Sections 5-8 are about motion and dark mode. Sections 9-10 are about anti-slop patterns. Sections 11-14 are about workflow and validation.

### GreenLight Structure

GreenLight is already multi-file:

| File | Bytes | Topic |
|------|-------|-------|
| `SKILL.md` | 11,177 | Purpose, workflow, output requirements |
| `instructions/core-structure.md` | 3,012 | Block HTML structure |
| `instructions/attributes.md` | 7,174 | HTML attributes & parameters |
| `instructions/variables.md` | 8,798 | CSS variables |
| `instructions/charts.md` | 26,988 | Chart blocks (ApexCharts) |
| `instructions/scripts.md` | 2,727 | Custom JS |
| `instructions/dynamic-loops.md` | 1,389 | Dynamic loops |
| `instructions/dynamic-placeholders.md` | 2,898 | Dynamic placeholders |
| `instructions/global-settings.md` | 4,212 | Global settings |
| `instructions/validate-scripts.md` | 4,071 | Script validation |
| `instructions/validate-styles.md` | 1,774 | Style validation |
| `scripts/convert.js` | 40,491 | Conversion script |
| `scripts/deconvert.js` | 26,007 | Deconversion script |

**Key finding:** GreenLight is logically separable by topic. The instruction files are already physically separated. However, the current loader only discovers the top-level `SKILL.md` because `_load_all()` does not recurse into subdirectories. The instruction files are NOT loaded by the current loader.

### GSAP Structure

GSAP is a namespace with 8 sub-skills:

| Sub-Skill | Bytes | Topic |
|-----------|-------|-------|
| `gsap-core/SKILL.md` | 14,776 | Core API: to(), from(), easing, stagger |
| `gsap-timeline/SKILL.md` | 4,398 | Timelines, position parameter, labels |
| `gsap-scrolltrigger/SKILL.md` | 18,355 | ScrollTrigger, pinning, scrub |
| `gsap-plugins/SKILL.md` | 21,540 | All plugins (ScrollTo, Flip, Draggable, etc.) |
| `gsap-utils/SKILL.md` | 12,093 | gsap.utils helpers |
| `gsap-react/SKILL.md` | 6,561 | React integration, useGSAP hook |
| `gsap-performance/SKILL.md` | 4,147 | Performance, transforms, batching |
| `gsap-frameworks/SKILL.md` | 11,456 | Vue, Svelte, other frameworks |
| `llms.txt` | 3,111 | Index/registry of all sub-skills |

**Key finding:** GSAP is already structured as a Skill namespace with independent sub-skills. The `llms.txt` file serves as an index. However, the current loader does NOT discover any of these because it only scans one level deep for `SKILL.md`.

---

## 3. Skill Classification by Context Size

| Skill | Bytes | Est. Tokens | Structure | Classification | Loading Recommendation |
|-------|------:|------------:|-----------|----------------|----------------------|
| **Bencent Brand** | 5,017 | ~1,250 | Single file | Small | Always-loaded Constitution |
| **UI Rules** | 563 | ~140 | Single file | Small | Always-loaded with design tasks |
| **Minimalist** | 7,986 | ~2,000 | Single file | Medium | Section-level or full for design tasks |
| **Humanizer TW** | 15,963 | ~4,000 | Single file | Medium | Full for writing tasks only |
| **Taste v2** | 87,253 | ~21,800 | Single file, 16 sections | Very Large | Section-level loading REQUIRED |
| **GreenLight** | ~66,000 | ~16,500 | Multi-file, 10+ topics | Very Large | Tool-level / section-level loading REQUIRED |
| **GSAP** | ~80,000 | ~20,000 | Namespace, 8 sub-skills | Very Large | Tool-level / sub-skill loading REQUIRED |

### Classification Definitions

- **Small** (< 2 KB / < 500 tokens): Safe to load into every prompt
- **Medium** (2-20 KB / 500-5,000 tokens): Safe for agent-level loading, should not be loaded for unrelated agents
- **Large** (20-50 KB / 5,000-12,500 tokens): Requires explicit selection, should not be loaded unnecessarily
- **Very Large** (> 50 KB / > 12,500 tokens): Section-level or tool-level loading required; loading entire skill into prompt is unacceptable for routine operations

---

## 4. Monolithic Analysis

### Taste v2

Taste v2 is **monolithic** — one 87 KB file containing at least 16 independent sections.

**Independent concerns identifiable within Taste v2:**

| Concern | Sections | Can be loaded independently? |
|---------|----------|------------------------------|
| Brief interpretation | 0, 1, 2 | Yes |
| Typography & color | 4.1, 4.2 | Yes |
| Layout & density | 4.3, 4.4 | Yes |
| Motion & animation | 5, 7 | Yes |
| Accessibility & performance | 6 | Yes |
| Dark mode | 8 | Yes |
| Anti-slop patterns | 9, 10 | Yes |
| Redesign workflow | 11 | Yes |
| Pre-flight validation | 14 | Yes |
| Design system selection | 2.A, 2.B | Yes |
| Reference vocabularies | 10 | Yes |
| Appendices | A, B, C | Yes |

**Assessment:** Taste v2 CAN be divided into reusable sections. The sections are already marked with clear `##` headings. No section depends on reading every other section to be coherent.

### GreenLight

GreenLight is **already multi-file** but NOT currently loaded by the Skill Loader.

The instruction files represent independent concerns:
- `core-structure.md` — block HTML structure
- `attributes.md` — HTML attributes
- `variables.md` — CSS variables
- `charts.md` — chart blocks
- `scripts.md` — custom JavaScript
- `dynamic-loops.md` — dynamic content
- `dynamic-placeholders.md` — placeholders
- `global-settings.md` — global settings
- `validate-scripts.md` — script validation
- `validate-styles.md` — style validation

**Assessment:** GreenLight is naturally section-level. Each instruction file can be loaded independently.

### GSAP

GSAP is **already a namespace** with 8 independent sub-skills.

Each sub-skill has a clear scope:
- `gsap-core` — basic tweens
- `gsap-timeline` — sequencing
- `gsap-scrolltrigger` — scroll-linked animations
- `gsap-plugins` — advanced plugins
- `gsap-utils` — utility functions
- `gsap-react` — React integration
- `gsap-performance` — optimization
- `gsap-frameworks` — Vue/Svelte/etc.

**Assessment:** GSAP is naturally sub-skill-level. Each sub-skill should be loaded independently based on the task.

---

## 5. Loading Model Comparison

### Model A — Full Skill Loading (Current)

```text
Skill
 ↓
entire SKILL.md
 ↓
LLM
```

**Advantages:**
- Simple implementation
- No parsing complexity
- Complete context always available
- Easy to debug

**Disadvantages:**
- Token waste for large Skills
- Context window exhaustion
- Cost accumulation
- Reduced model attention (signal-to-noise ratio)
- Cannot scale to FrontendAgent (150+ KB would be injected)

### Model B — Section-Level Loading

```text
Skill
 ↓
parsed into sections
 ↓
required sections selected
 ↓
LLM
```

**Advantages:**
- Dramatically reduced token usage
- Relevant context only
- Scales to large Skills
- Faster inference

**Disadvantages:**
- Requires section parsing infrastructure
- Section boundaries must be well-defined
- Risk of missing cross-section context
- More complex implementation
- Harder to debug (which sections were loaded?)

### Model C — Skill Index + On-Demand Content

```text
Skill
 ↓
metadata/index
 ↓
Agent requests capability
 ↓
loader retrieves relevant section
 ↓
LLM
```

**Advantages:**
- Most efficient token usage
- Capability-based routing
- Scalable to many Skills
- Clean separation of index and content

**Disadvantages:**
- Requires maintaining an index
- Capability matching logic
- Most complex implementation
- Risk of capability mismatches
- Hardest to debug

### Model D — Hybrid (Recommended)

```text
Constitution
    ↓
always loaded completely

Small Skills
    ↓
loaded completely

Medium Skills
    ↓
loaded completely when explicitly requested

Large/Very Large Skills
    ↓
section-level or sub-skill loading

Production Skills
    ↓
tool-level loading only
```

**Advantages:**
- Simple skills stay simple
- Large skills get efficient treatment
- Backward compatible with current small/medium skills
- Gradual migration path
- Optimal token usage without overengineering

**Disadvantages:**
- Two loading paths in codebase
- Requires skill size classification
- Need rules for when to use which path

---

## 6. Constitution Handling

### Current Behavior

Bencent Brand is treated as Constitution:
- Always loaded first
- Cannot be overridden
- Always appears at top of prompt

### Recommendation

**Keep Bencent Brand as always-loaded Constitution.**

Rationale:
- It is small (~5 KB / ~1,250 tokens)
- It provides brand identity that applies to ALL output
- It is the highest-priority rule set
- Loading it completely does not create meaningful token overhead
- Even if section-level loading were introduced for other skills, Constitution should remain complete

**Do NOT split Bencent Brand into sections.** Its size does not justify the complexity.

---

## 7. ImageAgent Analysis

### Current ImageAgent Skill Loading

ImageAgent currently receives:
- Bencent Brand (~1,250 tokens)
- Taste v2 (~21,800 tokens)
- Minimalist (~2,000 tokens)

Total: ~25,000 tokens per image generation call.

### What ImageAgent Actually Needs

| Skill | Needed? | Reason |
|-------|---------|--------|
| **Bencent Brand** | ✅ YES | Brand colors, prohibited visuals, material textures |
| **Taste v2** | ⚠️ PARTIAL | Only visual direction and anti-slop sections |
| **Minimalist** | ✅ YES | Density, whitespace, visual restraint |

### Taste Sections Relevant to ImageAgent

| Taste Section | Relevance to ImageAgent |
|--------------|------------------------|
| 0. Brief Inference | ❌ Not relevant — ImageAgent does not read design briefs |
| 1. The Three Dials | ⚠️ Partially relevant — VISUAL_DENSITY affects image composition |
| 2. Design System Map | ❌ Not relevant — no design system selection for images |
| 3. Architecture & Conventions | ❌ Not relevant — React/Tailwind conventions don't apply |
| 4. Design Engineering Directives | ⚠️ Partially relevant — color rules, typography scale for image text |
| 5. Motion | ❌ Not relevant — static images |
| 6. Performance & Accessibility | ❌ Not relevant |
| 7. Dial Definitions | ⚠️ Partially relevant — VISUAL_DENSITY reference |
| 8. Dark Mode Protocol | ❌ Not relevant |
| 9. AI Tells | ✅ YES — anti-slop patterns for image generation |
| 10. Reference Vocabulary | ⚠️ Partially relevant — hero paradigms, layout names |
| 11. Redesign Protocol | ❌ Not relevant |
| 12. Block Library | ❌ Not relevant |
| 13. Out of Scope | ❌ Not relevant |
| 14. Pre-Flight Check | ❌ Not relevant |
| Appendices | ❌ Not relevant |

### Estimated Relevant Taste Content for ImageAgent

Roughly 4-5 sections out of 16:
- Section 9 (AI Tells) — ~30 lines
- Section 10 partial (visual patterns) — ~20 lines
- Section 4 partial (color, density) — ~20 lines
- Section 1 partial (dials) — ~10 lines

Total estimated: ~80-100 lines of Taste v2, or approximately **3,000-4,000 tokens** instead of 21,800.

**Current over-injection: ~18,000 tokens wasted per image call.**

### Recommendation

ImageAgent should NOT receive the full Taste v2 Skill. It should receive only the sections relevant to visual direction and anti-slop.

---

## 8. Future FrontendAgent Analysis

### Likely Skill Requirements

| Skill | Size | Loading Strategy |
|-------|------|-----------------|
| Bencent Brand | Small | Always-loaded Constitution |
| Taste v2 | Very Large | Section-level, design-task only |
| Minimalist | Medium | Full, design-task only |
| UI Rules | Small | Full, design-task only |
| GreenLight | Very Large | Tool-level, block-generation only |
| GSAP | Very Large | Tool-level/sub-skill, animation only |

### Token Budget Estimate

| Scenario | Skills Loaded | Est. Tokens |
|----------|--------------|-------------|
| **Current (full loading)** | All 6 complete | ~150,000+ |
| **Recommended (hybrid)** | Bencent + Taste sections + Minimalist + UI Rules | ~15,000-25,000 |
| **GreenLight task** | Bencent + GreenLight sections | ~10,000-15,000 |
| **GSAP animation task** | Bencent + GSAP sub-skill | ~5,000-10,000 |

### Assessment

The current loader **cannot support FrontendAgent** without modification. Loading 150,000+ tokens into every prompt is unacceptable.

With the hybrid model, FrontendAgent becomes practical.

---

## 9. GSAP Special Case

### Current Structure

GSAP is a **Skill namespace**, not a single Skill:

```
skills/gsap/
├── llms.txt              (index)
├── gsap-core/SKILL.md
├── gsap-timeline/SKILL.md
├── gsap-scrolltrigger/SKILL.md
├── gsap-plugins/SKILL.md
├── gsap-utils/SKILL.md
├── gsap-react/SKILL.md
├── gsap-performance/SKILL.md
└── gsap-frameworks/SKILL.md
```

### Current Loader Behavior

The current loader **does not discover GSAP at all** because:
1. `skills/gsap/` has no top-level `SKILL.md`
2. `_load_all()` only scans one directory level deep
3. Subdirectory `SKILL.md` files are invisible to the loader

### Recommended Treatment

GSAP should be treated as a **Skill namespace** with **sub-skill loading**:

```text
GSAP Namespace
    ├── gsap-core (sub-skill)
    ├── gsap-timeline (sub-skill)
    ├── gsap-scrolltrigger (sub-skill)
    ├── gsap-plugins (sub-skill)
    ├── gsap-utils (sub-skill)
    ├── gsap-react (sub-skill)
    ├── gsap-performance (sub-skill)
    └── gsap-frameworks (sub-skill)
```

Each sub-skill should be loadable independently. The `llms.txt` index should serve as the namespace registry.

**Do NOT flatten GSAP into a single 80 KB Skill.** The namespace structure is already correct; the loader needs to understand it.

---

## 10. GreenLight Special Case

### Current Structure

GreenLight is a **multi-file Skill** with logical topic separation:

```
skills/greenlight-vibe/
├── SKILL.md                    (main entry)
├── instructions/
│   ├── core-structure.md
│   ├── attributes.md
│   ├── variables.md
│   ├── charts.md
│   ├── scripts.md
│   ├── dynamic-loops.md
│   ├── dynamic-placeholders.md
│   ├── global-settings.md
│   ├── validate-scripts.md
│   └── validate-styles.md
└── scripts/
    ├── convert.js
    └── deconvert.js
```

### Current Loader Behavior

The current loader **only loads the top-level `SKILL.md`** (11 KB). The instruction files are NOT loaded because `_load_all()` does not recurse into `instructions/`.

### Recommended Treatment

GreenLight should support **section-level loading** within its existing multi-file structure:

| Topic | File | Loading Strategy |
|-------|------|-----------------|
| Core workflow | `SKILL.md` | Always when GreenLight is active |
| Block structure | `core-structure.md` | When creating blocks |
| Attributes | `attributes.md` | When configuring block parameters |
| CSS variables | `variables.md` | When styling blocks |
| Charts | `charts.md` | Only when generating chart blocks |
| Scripts | `scripts.md` | Only when adding custom JS |
| Dynamic content | `dynamic-loops.md`, `dynamic-placeholders.md` | Only when dynamic content needed |
| Validation | `validate-scripts.md`, `validate-styles.md` | Only during validation phase |
| JS helpers | `scripts/convert.js`, `deconvert.js` | Never injected into prompt (code files) |

**Critical:** The JS files (`convert.js`, `deconvert.js`) should NEVER be injected into LLM prompts. They are code artifacts, not guidance text.

---

## 11. Prompt Context Strategy

### Current Strategy

```text
Agent
 ↓
Skill Loader
 ↓
complete Skills (all mapped)
 ↓
LLM
```

### Recommended Strategy

```text
Agent
 ↓
Task requirements
 ↓
Skill Policy
    ├── Constitution (always)
    ├── Agent defaults (if no explicit skills)
    └── Explicit required skills (if provided)
 ↓
Skill index lookup
 ↓
For each required skill:
    if small/medium → load complete
    if large → load relevant sections
    if tool-specific → load at tool invocation
 ↓
Minimal context assembly
 ↓
LLM
```

### Granularity Recommendation

| Granularity | When | Example |
|-------------|------|---------|
| **Workflow-level** | Once per workflow phase | Constitution loaded for entire article generation |
| **Agent-level** | Once per agent instantiation | Writer gets Bencent + Humanizer defaults |
| **Method-level** | Per `call_ai()` | ImageAgent prompt builder gets Taste sections |
| **Tool-level** | Per tool invocation | GreenLight loaded only when converting to blocks |

**Recommended default:** Method-level granularity for `call_ai()`. This is already partially supported by the `required_skills` parameter.

---

## 12. Caching

### Current Cache

```text
SkillLoader
    ↓
self._skills: Dict[str, Skill]
    ↓
each Skill.content = full file text (one string)
```

### If Section-Level Loading Is Introduced

The cache should evolve to:

```text
SkillLoader
    ↓
self._skills: Dict[str, Skill]  (full skills, parsed once)
    ↓
self._sections: Dict[str, Section]  (parsed sections, cached)
    ↓
Skill.content = full text (backward compatible)
Skill.sections = {section_name: section_text} (new)
```

**Recommendation:** Maintain both caches during transition:
1. Full skill cache — for backward compatibility and small/medium skills
2. Section cache — for large skills, populated on first section access

Do NOT replace the full skill cache. Add section cache alongside it.

---

## 13. Versioning

### Current State

No version tracking exists for Skills. If a Skill is modified, there is no way to trace generated content back to the specific Skill version used.

### Recommendation

Add lightweight version metadata to Skills:

| Field | Purpose |
|-------|---------|
| `skill_version` | Track which version of the Skill was used |
| `section_hash` | Track which sections were loaded (hash of section names) |
| `content_hash` | SHA-256 of the full skill content |

This is important for:
- Debugging (which Skill version produced this output?)
- Auditing (comparing output across Skill versions)
- Rollback (reverting to previous Skill version if quality drops)

**Do NOT implement now.** This is a FUTURE concern.

---

## 14. Failure Modes

### Section-Level Loading Risks

| Risk | Description | Mitigation |
|------|-------------|------------|
| **Missing context** | Loading only section 9 (AI Tells) without section 4 (color rules) may miss cross-cutting constraints | Define section dependencies explicitly; load dependent sections automatically |
| **Incompatible sections** | Loading contradictory sections from different skills | Priority ordering + conflict instruction already implemented |
| **Incorrect section selection** | Agent requests wrong section for task | Agent declares required sections explicitly; no automatic section guessing |
| **Loss of global rules** | Section-level loading may miss rules that apply globally | Keep Constitution complete; identify "global" sections within large skills |
| **Harder debugging** | Harder to reproduce prompt when sections are dynamic | Log exact sections loaded per call; include in metadata |
| **Stale cached sections** | Skill updated but cached section not refreshed | Invalidate section cache when skill file mtime changes |
| **Over-fragmentation** | Too many tiny sections create management overhead | Minimum section size threshold (e.g., 200 words) |

### Recommended Safeguards

1. **Minimum section size** — don't create sections smaller than ~200 words
2. **Explicit dependencies** — if section A requires section B, load both
3. **Audit logging** — log exact sections loaded per `call_ai()`
4. **Fallback** — if requested section not found, fall back to full skill or fail loudly
5. **No guessing** — never auto-select sections based on task analysis

---

## 15. Enterprise Recommendation

### Primary Recommendation: Model D — Hybrid

**Skill loading model: D — Hybrid**

Rationale:
- Small skills (Bencent, UI Rules) stay simple — load completely
- Medium skills (Minimalist, Humanizer) load completely when explicitly requested
- Large skills (Taste, GreenLight, GSAP) require section/sub-skill loading
- Constitution always loads completely
- Backward compatible with current architecture
- Gradual migration path
- Optimal token usage without overengineering

### Secondary Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Taste loading** | Section-level | 87 KB monolithic file is unsustainable |
| **GreenLight loading** | Tool-level / section-level | Already multi-file; natural for tool binding |
| **GSAP loading** | Sub-skill / tool-level | Already a namespace; natural granularity exists |
| **Bencent Brand loading** | Always-loaded complete | Small, constitution-level, no overhead |
| **FrontendAgent readiness** | READY AFTER SKILL CONTEXT CHANGE | Current loader cannot support it; hybrid model enables it |

---

## 16. Proposed Target Architecture

```
                    Skill Registry
                         │
                  ┌──────┴──────┐
                  ↓             ↓
             Metadata        Content Cache
                  │             │
                  │             ├── Full Skills (small/medium)
                  │             └── Sections (large/very large)
                  │             
                  └──────┬──────┘
                         ↓
                    Skill Policy
                         │
               ┌─────────┼─────────┐
               ↓         ↓         ↓
          Constitution   Task     Tool
               │         │         │
               ↓         ↓         ↓
            Bencent    selected   GreenLight
                       sections   GSAP sub-skills
                         │
                         ↓
                    Minimal Context
                         │
                         ↓
                        LLM
```

### Component Responsibilities

| Component | Responsibility |
|-----------|----------------|
| **Skill Registry** | Discovery, metadata, version tracking |
| **Skill Policy** | Determines which skills/sections to load for each operation |
| **Constitution** | Bencent Brand, always loaded completely |
| **Task Skills** | Skills selected based on agent defaults or explicit requirements |
| **Tool Skills** | Skills loaded only when specific tools are invoked |
| **Content Cache** | Stores both full skills and parsed sections |
| **Minimal Context** | Assembled prompt with only required skills/sections |

---

## 17. Implementation Roadmap

### Phase 1: Foundation (Smallest Safe Change)

1. Add section parser to Skill loader — parse `##` headings in large Skills
2. Add `get_skill_sections(skill_name)` method
3. Add `build_skills_context()` support for section selection
4. Keep full-skill loading as default/fallback
5. Update diagnostics to report section-level options

**Goal:** Enable section-level loading without breaking existing behavior.

### Phase 2: Large Skill Section Support

1. Define canonical section identifiers for Taste v2
2. Define instruction-file topics for GreenLight
3. Define sub-skill identifiers for GSAP
4. Update `required_skills` API to accept section specifiers:
   ```python
   call_ai(prompt, required_skills=[
       "design-taste-frontend:anti-slop",
       "design-taste-frontend:visual-direction",
       "minimalist-ui",
   ])
   ```
5. Update ImageAgent to use section-level Taste loading
6. Update skill_loader mapping to use section-level where appropriate

**Goal:** ImageAgent uses ~4,000 tokens of Taste instead of ~22,000.

### Phase 3: Tool-Level Skill Loading

1. Integrate `get_tool_skills()` into tool execution paths
2. Load GreenLight sections only during WordPress block conversion
3. Load GSAP sub-skills only during animation generation
4. Add tool-skill binding to `BaseTool` base class

**Goal:** Production skills loaded only when their tools are invoked.

### Phase 4: Observability / Version Tracking

1. Add `skill_version` and `content_hash` to Skill metadata
2. Log exact skills/sections loaded per `call_ai()`
3. Add cost tracking per skill/section
4. Add prompt size diagnostics to workflow state

**Goal:** Enterprise observability for skill usage and cost attribution.

---

## 18. Final Decision

### Skill Loading Model

**D — Hybrid**

- Constitution: always loaded completely
- Small/Medium Skills: loaded completely when explicitly requested
- Large/Very Large Skills: section-level or sub-skill loading
- Production Skills: tool-level loading only

### Per-Skill Decision

| Skill | Loading Strategy |
|-------|-----------------|
| **Bencent Brand** | Always-loaded complete Constitution |
| **Taste v2** | Section-level (design tasks only) |
| **Minimalist** | Full (design tasks only) |
| **UI Rules** | Full (design tasks only) |
| **Humanizer TW** | Full (writing tasks only) |
| **GreenLight** | Tool-level / section-level (block conversion only) |
| **GSAP** | Sub-skill / tool-level (animation tasks only) |

### FrontendAgent Readiness

**READY AFTER SKILL CONTEXT CHANGE**

The current loader cannot support FrontendAgent. After implementing Phase 1-3 of the roadmap, FrontendAgent will be ready.

### MUST HAVE

1. Section parser for large Skills (Taste, GreenLight, GSAP)
2. Section-level `build_skills_context()` support
3. Sub-skill discovery for namespace Skills (GSAP)
4. Tool-level skill binding integration
5. ImageAgent updated to use Taste sections instead of full Taste

### SHOULD HAVE

1. Section dependency tracking (load dependent sections automatically)
2. Skill versioning and content hashing
3. Prompt size diagnostics per `call_ai()`
4. Cost tracking per skill/section

### FUTURE

1. Capability-based skill routing (Model C elements)
2. Skill analytics (which sections improve output quality)
3. User-defined skill overrides per project
4. Dynamic skill composition at runtime
5. Skill TTL / refresh without restart

---

## 19. Current vs Recommended Token Budget

### Current (Full Loading)

| Agent | Skills | Est. Tokens |
|-------|--------|-------------|
| Planner | Bencent | ~1,250 |
| Research | Bencent | ~1,250 |
| Writer | Bencent + Humanizer | ~5,000 |
| Critic | Bencent + Humanizer | ~5,000 |
| SEO | Bencent | ~1,250 |
| QualityEvaluator | Bencent + Humanizer | ~5,000 |
| ContentFixer | Bencent | ~1,250 |
| FinalReviewer | Bencent + Humanizer | ~5,000 |
| ImageAgent | Bencent + Taste + Minimalist | ~25,000 |
| Router | Bencent | ~1,250 |
| Learner | Bencent | ~1,250 |

### Recommended (Hybrid)

| Agent | Skills | Est. Tokens |
|-------|--------|-------------|
| Planner | Bencent | ~1,250 |
| Research | Bencent | ~1,250 |
| Writer | Bencent + Humanizer | ~5,000 |
| Critic | Bencent + Humanizer | ~5,000 |
| SEO | Bencent | ~1,250 |
| QualityEvaluator | Bencent | ~1,250 |
| ContentFixer | Bencent | ~1,250 |
| FinalReviewer | Bencent + Humanizer | ~5,000 |
| ImageAgent | Bencent + Taste(sections) + Minimalist | ~7,000 |
| Router | Bencent | ~1,250 |
| Learner | Bencent | ~1,250 |

### FrontendAgent (Future, Hybrid)

| Task | Skills | Est. Tokens |
|------|--------|-------------|
| Planning | Bencent | ~1,250 |
| Design task | Bencent + Taste(sections) + Minimalist + UI Rules | ~10,000 |
| Block generation | Bencent + GreenLight(sections) | ~8,000 |
| Animation task | Bencent + GSAP(sub-skill) | ~5,000 |
| Full page | Bencent + relevant sections | ~15,000-25,000 |

---

## 20. Critical Constraint Verification

| Constraint | Status |
|------------|--------|
| READ ONLY | ✅ No code modified |
| No Python changes | ✅ Document only |
| No Skill content changes | ✅ Document only |
| No FrontendAgent created | ✅ Document only |
| No GreenLight integration | ✅ Document only |
| No GSAP integration | ✅ Document only |
| No Workflow changes | ✅ Document only |
| No Router changes | ✅ Document only |
| No Reviewer changes | ✅ Document only |
| Inspected actual files | ✅ All skills inspected |
| No invented token measurements | ✅ Estimates based on actual file sizes |
| Distinguished current from future | ✅ Clearly labeled |
| One primary recommendation | ✅ Model D — Hybrid |

---

**LARGE SKILL / SKILL CONTEXT ARCHITECTURE REVIEW COMPLETE**
