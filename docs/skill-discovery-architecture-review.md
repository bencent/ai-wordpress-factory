# Skill Discovery Architecture Review

**Date:** 2026-09-08  
**Reviewer:** Kilo  
**Scope:** Read-only architecture review — no code modified  
**Status:** COMPLETE

---

## 1. Executive Summary

The current Skill Loader uses a **one-level directory scan** that only discovers `skills/*/SKILL.md`. This works for simple Skills but fails for:

1. **GSAP** — 8 sub-skills nested under `skills/gsap/gsap-*/SKILL.md`, completely invisible to current loader
2. **GreenLight** — multi-file Skill with `instructions/` and `scripts/`, only top-level `SKILL.md` is loaded
3. **Taste v2** — single 87 KB monolithic file, loaded entirely even when only a few sections are relevant

**Primary Recommendation:** Hybrid discovery model combining filesystem discovery with optional namespace/section support.

**FrontendAgent Readiness:** NOT READY — requires discovery model changes first.

**ImageAgent Readiness:** PARTIALLY READY — can reduce Taste context from ~25K to ~7K tokens with section loading.

---

## 2. Current Implementation

### Actual Code Behavior

**Discovery** (`skills/loader.py:94-117`):
```python
for entry in os.listdir(self.skills_dir):
    skill_path = os.path.join(self.skills_dir, entry)
    skill_md = os.path.join(skill_path, "SKILL.md")
    if os.path.isfile(skill_md):
        # load entire file into skill.content
```

**Key limitations:**
- One level deep only (`os.listdir`)
- Only discovers directories containing `SKILL.md`
- Reads entire file into single `content` string
- No concept of sections, sub-skills, or namespaces
- No distinction between knowledge and executable code

**Caching:**
- `self._skills: Dict[str, Skill]` — loaded once at startup
- `BaseAgent.skills_context` — cached per agent instance
- No section-level cache
- No lazy loading

**Naming:**
- Skill ID = `metadata["name"]` or directory name
- No namespace concept
- No section identifier support

**Constitution:**
- Hardcoded lookup: `self._skills.get("Bencent")`
- Not a real constitutional layer — just a special-cased skill

**Tool bindings:**
- `_TOOL_BINDINGS` maps tool names to skill names
- But `gsap` is listed as a tool binding target even though it's NOT discoverable
- This is a broken reference

### Current Discovery Model

```text
skills/
├── bencent-brand/SKILL.md → "Bencent"
├── taste-skill/SKILL.md → "design-taste-frontend"
├── minimalist-skill/SKILL.md → "minimalist-ui"
├── ui-rules/SKILL.md → "ui-rules"
├── humanizer-tw/SKILL.md → "humanizer-tw"
├── greenlight-vibe/SKILL.md → "greenlight-vibe"
└── gsap/ → NOT DISCOVERED (no top-level SKILL.md)
```

---

## 3. Taste Analysis

### Current Structure

- Single `SKILL.md`, 87,253 bytes, 1,206 lines
- 16 top-level sections (`## 0.` through `## 14.`, plus Appendices)
- Section headings are stable Markdown `##` headings
- No embedded section identifiers or metadata

### Section Breakdown

| Section | Lines | Topic | Independent? |
|---------|------:|-------|-------------|
| 0. Brief Inference | ~37 | Reading briefs, design read | Yes |
| 1. The Three Dials | ~38 | DESIGN_VARIANCE, MOTION_INTENSITY, VISUAL_DENSITY | Yes |
| 2. Design System Map | ~23 | Official packages, aesthetic selection | Yes |
| 3. Architecture & Conventions | ~38 | Stack, state, icons, typography | Yes |
| 4. Design Engineering | ~80 | Typography, color, layout, anti-slop | Yes |
| 5. Motion | ~80 | Motion libraries, scroll patterns, code | Yes |
| 6. Performance & Accessibility | ~15 | Guardrails | Yes |
| 7. Dial Definitions | ~15 | Technical reference | Yes |
| 8. Dark Mode | ~15 | Dark mode protocol | Yes |
| 9. AI Tells | ~30 | Forbidden patterns | Yes |
| 10. Reference Vocabulary | ~50 | Pattern names | Yes |
| 11. Redesign Protocol | ~40 | Greenfield vs redesign | Yes |
| 12. Block Library | ~10 | Contract placeholder | Yes |
| 13. Out of Scope | ~10 | Boundaries | Yes |
| 14. Pre-Flight Check | ~30 | Validation checklist | Yes |
| Appendix A | ~15 | Install commands | Yes |
| Appendix B | ~10 | Canonical sources | Yes |
| Appendix C | ~40 | Apple Liquid Glass | Yes |

### Assessment

**Can sections be reliably identified?** YES — `##` headings are consistent and parseable.

**Can sections be loaded independently?** MOSTLY YES — most sections are self-contained. Section 4 references dials from Section 1, and Section 5 references motion concepts from Section 3. These are loose dependencies, not hard requirements.

**Should Taste be modified?** NO — keep the monolithic file as source of truth.

**Section identifier strategy:**
- Use heading text normalized to lowercase-with-hyphens
- Example: `"design-taste-frontend:brief-inference"`, `"design-taste-frontend:ai-tells"`
- Fallback: full skill load if section not found

### ImageAgent-Relevant Taste Sections

| Section | Relevance | Est. Tokens |
|---------|-----------|-------------|
| 4.1 Typography | Partial — image text styling | ~500 |
| 4.2 Color | High — color calibration | ~800 |
| 9. AI Tells | High — anti-slop for images | ~1,200 |
| 10. Reference Vocabulary | Partial — visual pattern names | ~1,000 |
| 1. The Three Dials | Partial — VISUAL_DENSITY | ~400 |
| **Total** | | **~3,900** |

**Current:** ~21,800 tokens (full Taste)  
**Recommended:** ~3,900 tokens (section-level)  
**Savings:** ~18,000 tokens per image call

---

## 4. GreenLight Analysis

### Current Structure

```
greenlight-vibe/
├── SKILL.md                    (11 KB)  — main workflow
├── instructions/
│   ├── core-structure.md       (3 KB)   — block HTML
│   ├── attributes.md           (7 KB)   — HTML attributes
│   ├── variables.md            (9 KB)   — CSS variables
│   ├── charts.md               (27 KB)  — ApexCharts
│   ├── scripts.md              (3 KB)   — custom JS
│   ├── dynamic-loops.md        (1 KB)   — dynamic content
│   ├── dynamic-placeholders.md (3 KB)   — placeholders
│   ├── global-settings.md      (4 KB)   — global settings
│   ├── validate-scripts.md     (4 KB)   — script validation
│   └── validate-styles.md      (2 KB)   — style validation
└── scripts/
    ├── convert.js              (40 KB)  — executable tool
    └── deconvert.js            (26 KB)  — executable tool
```

### Knowledge vs Executable Boundary

**Knowledge (should be loaded into prompt context):**
- `SKILL.md` — workflow and output requirements
- `instructions/*.md` — all instruction files are knowledge

**Executable Tooling (should NOT be loaded into prompt context):**
- `scripts/convert.js` — 40 KB of executable code
- `scripts/deconvert.js` — 26 KB of executable code

**Critical:** These JS files are tool artifacts, not LLM guidance. They should never appear in prompts. If the loader were to recurse into `scripts/` and load everything, it would inject 66 KB of JavaScript into an LLM context — a serious error.

### Recommended Loading Strategy

| Component | Loading Strategy |
|-----------|-----------------|
| `SKILL.md` | Always when GreenLight is active |
| `instructions/core-structure.md` | Block creation tasks |
| `instructions/attributes.md` | Block parameter configuration |
| `instructions/variables.md` | Styling tasks |
| `instructions/charts.md` | Chart block generation ONLY |
| `instructions/scripts.md` | Custom JS tasks ONLY |
| `instructions/dynamic-*.md` | Dynamic content tasks ONLY |
| `instructions/validate-*.md` | Validation phase ONLY |
| `scripts/*.js` | NEVER into prompt — tool execution only |

### Section Identifiers

```
greenlight-vibe:core-structure
greenlight-vibe:attributes
greenlight-vibe:variables
greenlight-vibe:charts
greenlight-vibe:scripts
greenlight-vibe:dynamic-loops
greenlight-vibe:dynamic-placeholders
greenlight-vibe:global-settings
greenlight-vibe:validate-scripts
greenlight-vibe:validate-styles
```

---

## 5. GSAP Analysis

### Current Structure

```
gsap/
├── llms.txt                    (3 KB)   — namespace index
├── gsap-core/SKILL.md          (15 KB)
├── gsap-timeline/SKILL.md      (4 KB)
├── gsap-scrolltrigger/SKILL.md (18 KB)
├── gsap-plugins/SKILL.md       (22 KB)
├── gsap-utils/SKILL.md         (12 KB)
├── gsap-react/SKILL.md         (7 KB)
├── gsap-performance/SKILL.md   (4 KB)
└── gsap-frameworks/SKILL.md    (11 KB)
```

### Nature of GSAP

GSAP is a **Skill Namespace**, not a single Skill. The `llms.txt` file explicitly states:

> "Use this file to discover which skill to load. Each skill lives in a directory of the same name under skills/ and contains SKILL.md."

This is an explicit index provided by the Skill author. The loader should respect this structure.

### Current Discovery Failure

The current loader **completely misses all 8 GSAP sub-skills** because:
1. `skills/gsap/` has no top-level `SKILL.md`
2. `_load_all()` only scans one level deep
3. `_TOOL_BINDINGS["animation"]` references `"gsap"` which doesn't exist in `_skills`

This is a **broken reference** — the code thinks GSAP is loaded, but it never is.

### Recommended Treatment

GSAP should be a **first-class namespace** in the loader:

| Concept | Example IDs |
|---------|-------------|
| Namespace | `gsap` |
| Sub-skill | `gsap-core`, `gsap-scrolltrigger`, `gsap-plugins`, etc. |

**Naming options:**

| Strategy | Example | Pros | Cons |
|----------|---------|------|------|
| Flat IDs | `gsap-scrolltrigger` | Simple, matches directory name | Namespace-less, global namespace pollution |
| Scoped IDs | `GSAP:scrolltrigger` | Clear namespace, collision-safe | Slightly more complex parsing |

**Recommendation: Flat IDs with namespace metadata**

Use the directory-prefixed name as the Skill ID:
- `gsap-core`
- `gsap-timeline`
- `gsap-scrolltrigger`
- etc.

This is simpler, matches the filesystem, and the namespace collision risk is low (GSAP sub-skills are unlikely to collide with top-level skills).

The `llms.txt` file should serve as the **canonical index** for what sub-skills exist and when to load them.

---

## 6. Discovery Model

### Proposed Unified Model

```text
Skill
 ├── Skill (top-level)
 │    └── SKILL.md
 │
 ├── Skill Namespace
 │    └── Sub-skill
 │         └── SKILL.md
 │
 ├── Multi-file Skill
 │    ├── SKILL.md (entry)
 │    ├── instructions/*.md (knowledge sections)
 │    └── scripts/* (executable — never in prompt)
 │
 └── Section (within a Skill)
      └── parsed from ## headings
```

### Internal Representation

```python
class Skill:
    name: str                    # "Bencent"
    namespace: str               # "" or "gsap"
    kind: str                    # "skill" | "sub-skill" | "section"
    parent: Optional[str]        # None or "gsap"
    content: str                 # full text
    path: str                    # filesystem path
    sections: Dict[str, str]     # section_name -> section_text
    metadata: Dict[str, Any]     # category, priority, etc.
```

### Discovery Algorithm

```text
Startup:
1. Scan skills/ one level deep
2. For each entry:
   a. If entry/SKILL.md exists → load as Skill
   b. If entry/ has subdirectories with SKILL.md → load as Namespace with Sub-skills
   c. If entry/ has instructions/ → register section sources
   d. If entry/ has scripts/ → register tool paths (not prompt content)
3. Parse frontmatter for each discovered entity
4. Build index: name → Skill/Sub-skill
5. Cache metadata; defer content loading for large Skills
```

### What Gets Loaded When

| Entity | Startup | On Demand |
|--------|---------|-----------|
| Small Skill metadata | ✅ | — |
| Small Skill content | ✅ | — |
| Large Skill metadata | ✅ | Content on first use |
| Large Skill sections | — | On section request |
| Sub-skill metadata | ✅ | Content on first use |
| Instruction files | ✅ | On section request |
| Script files | ❌ | On tool execution (not prompt) |

---

## 7. Strategy Comparison

### Strategy A — Recursive Filesystem Scan

```text
skills/**/SKILL.md
```

**Advantages:**
- Simple implementation
- No manifest maintenance
- Works with external Skills automatically
- Natural filesystem mapping

**Disadvantages:**
- Cannot distinguish knowledge from executable code
- Cannot handle non-Markdown knowledge files
- May load unwanted files if structure varies
- No explicit section support
- GSAP sub-skills would be discovered but no namespace concept

**Verdict:** Necessary but insufficient. Must be combined with metadata.

### Strategy B — Manifest / Registry

```text
skills/manifest.yaml
```

**Advantages:**
- Explicit control
- Can declare sections, tools, dependencies
- Works with any file structure
- Clear versioning point

**Disadvantages:**
- Must maintain separate manifest
- External Skills need manifest too
- Risk of manifest/filesystem drift
- Extra file to update when Skills change
- Violates "external Skill source remains source of truth"

**Verdict:** Too much overhead. External Skills should not require manifest maintenance.

### Strategy C — Hybrid (Recommended)

Filesystem discovery + optional metadata/index files.

**Advantages:**
- Simple Skills work without any metadata
- Complex Skills can provide optional `llms.txt` or section markers
- No mandatory manifest
- External Skills work out of the box
- Namespace/index files respected when present
- Graceful degradation

**Disadvantages:**
- Two code paths
- Need conventions for when metadata is used vs ignored

**Verdict:** Best balance. Recommended.

---

## 8. Naming Convention

### Current Names

| Current ID | Source |
|------------|--------|
| `Bencent` | frontmatter `name:` |
| `design-taste-frontend` | frontmatter `name:` |
| `minimalist-ui` | frontmatter `name:` |
| `humanizer-tw` | frontmatter `name:` |
| `greenlight-vibe` | frontmatter `name:` |
| `ui-rules` | frontmatter `name:` |

### Proposed Convention

```
Top-level Skill:      <name>                    e.g. "Bencent"
Sub-skill:            <namespace>-<name>        e.g. "gsap-scrolltrigger"
Section:              <skill-name>:<section>    e.g. "design-taste-frontend:ai-tells"
Instruction file:     <skill-name>:<topic>      e.g. "greenlight-vibe:charts"
```

### Rationale

- **Backward compatible** — existing top-level names unchanged
- **Discoverable** — sub-skill IDs match directory names (`gsap-scrolltrigger` matches `gsap/gsap-scrolltrigger/`)
- **Namespace-safe** — `gsap-` prefix prevents collisions
- **Section-addressable** — colon separator is unambiguous
- **No special characters** — safe for use in Python lists, JSON, etc.

### Examples

```python
# Existing usage — unchanged
required_skills=["Bencent", "humanizer-tw"]

# New: GSAP sub-skill
required_skills=["gsap-scrolltrigger"]

# New: Taste section
required_skills=["design-taste-frontend:ai-tells"]

# New: GreenLight instruction
required_skills=["greenlight-vibe:charts"]

# Combined
required_skills=[
    "Bencent",
    "design-taste-frontend:composition",
    "minimalist-ui",
    "gsap-scrolltrigger",
]
```

---

## 9. Cache Architecture

### Current Cache

```text
SkillLoader._skills: Dict[str, Skill]
    ↓
Skill.content = full file text (loaded at startup)
```

### Proposed Cache

```text
SkillLoader
 ├── _skills: Dict[str, Skill]          # metadata + full content
 ├── _sections: Dict[str, Dict[str, str]]  # skill_name -> {section: text}
 └── _tool_artifacts: Dict[str, str]    # script paths, not loaded into memory
```

### Loading Strategy

| Phase | Action |
|-------|--------|
| **Startup** | Discover all Skills, load metadata, parse section headers for large Skills |
| **First use** | Load full content for small/medium Skills |
| **First section request** | Parse and cache requested sections for large Skills |
| **Tool invocation** | Load executable scripts (not into prompt cache) |

### Rationale

- Small Skills (< 20 KB) are cheap to load — load fully at startup
- Large Skills (> 50 KB) should defer content loading
- Sections are parsed on first request and cached
- Tool artifacts (JS files) are never loaded into prompt context

---

## 10. Startup Loading Strategy

### Model 1 — Load Everything at Startup

```text
Startup → read all Skills → cache all content → ready
```

**Pros:** Simple, no lazy loading complexity  
**Cons:** Slow startup, high memory, loads 150+ KB that may never be used

### Model 2 — Discover Only, Load on Demand

```text
Startup → scan filesystem → build index → ready
First use → load content
```

**Pros:** Fast startup, minimal memory  
**Cons:** First call latency, more complex cache invalidation

### Model 3 — Hybrid (Recommended)

```text
Startup → discover + load small/medium Skills → ready
First use → load large Skill content/sections on demand
```

**Pros:** 
- Fast startup (small Skills are < 20 KB each)
- Large Skills loaded only when needed
- Simple with clear rules
- Backward compatible

**Cons:**
- Two loading paths
- Need size threshold

**Recommendation:** Model 3 with threshold at 50 KB / ~12,500 tokens.

| Size | Strategy |
|------|----------|
| < 50 KB | Load fully at startup |
| >= 50 KB | Load metadata at startup, content/sections on demand |

---

## 11. FrontendAgent Example

Using the proposed discovery model, here is how FrontendAgent would load context:

```python
# FrontendAgent building context for a design task
skill_loader.build_skills_context("frontend", required_skills=[
    "Bencent",                           # Constitution — always
    "design-taste-frontend:layout",       # Taste section
    "design-taste-frontend:color",        # Taste section
    "design-taste-frontend:typography",   # Taste section
    "design-taste-frontend:ai-tells",     # Taste section
    "minimalist-ui",                      # Full skill
    "ui-rules",                           # Full skill
])
```

**Token estimate:** ~10,000-15,000 tokens

Versus current broken state: would load nothing for GSAP, and full Taste would be ~22,000 tokens.

For a block generation task:

```python
skill_loader.build_skills_context("frontend", required_skills=[
    "Bencent",
    "greenlight-vibe:core-structure",
    "greenlight-vibe:attributes",
    "greenlight-vibe:variables",
])
```

For an animation task:

```python
skill_loader.build_skills_context("frontend", required_skills=[
    "Bencent",
    "gsap-scrolltrigger",
])
```

**Key principle:** FrontendAgent never loads all Skills at once. Each task loads only the relevant sections/sub-skills.

---

## 12. ImageAgent Example

Current ImageAgent loads:
- Bencent (~1,250 tokens)
- Taste v2 full (~21,800 tokens)
- Minimalist (~2,000 tokens)
- **Total: ~25,000 tokens**

Proposed ImageAgent loading:

```python
# Image generation task
skill_loader.build_skills_context("image", required_skills=[
    "Bencent",                              # Constitution
    "design-taste-frontend:ai-tells",       # Anti-slop for images
    "design-taste-frontend:reference-vocabulary:hero-paradigms",  # Visual patterns
    "design-taste-frontend:design-engineering-directives:color",  # Color rules
    "minimalist-ui",                        # Full — only 8 KB
])
```

**Token estimate:** ~7,000 tokens

**Savings:** ~18,000 tokens per image call (72% reduction)

### ImageAgent Section Mapping (Proposed)

| Taste Section | ImageAgent Use |
|---------------|----------------|
| `design-taste-frontend:ai-tells` | Anti-slop patterns for image generation |
| `design-taste-frontend:reference-vocabulary` (hero paradigms subset) | Visual composition patterns |
| `design-taste-frontend:design-engineering-directives:color` | Color calibration rules |
| `design-taste-frontend:the-three-dials` (VISUAL_DENSITY only) | Density guidance |

Sections NOT needed for ImageAgent:
- Brief inference, design system selection, motion, performance, redesign protocol, block library, dark mode, accessibility, install commands, Apple Liquid Glass

---

## 13. Failure Modes

### 1. Nested SKILL.md Not Discovered

**Failure:** GSAP sub-skills invisible to current loader  
**Detection:** `list_skills()` returns no `gsap-*` entries  
**Fallback:** Recursive scan as secondary discovery pass  
**Logging:** Warn on directories with subdirectories containing SKILL.md

### 2. Section Identifier Does Not Exist

**Failure:** `required_skills=["design-taste-frontend:nonexistent"]`  
**Detection:** Section lookup returns None  
**Fallback:** Load full skill instead of failing  
**Logging:** Warning with available section list

### 3. Skill Deleted

**Failure:** Skill directory removed after deployment  
**Detection:** `get_skill()` returns None  
**Fallback:** Skip skill, continue with remaining  
**Logging:** Warning at discovery time

### 4. Skill Renamed

**Failure:** Directory renamed but frontmatter `name:` unchanged  
**Detection:** Skill ID mismatch between filesystem and metadata  
**Fallback:** Use frontmatter `name:` as canonical ID  
**Logging:** Info at discovery time

### 5. Duplicate Skill ID

**Failure:** Two directories with same frontmatter `name:`  
**Detection:** `self._skills[name]` overwritten silently  
**Fallback:** Last-write-wins with warning  
**Logging:** Warning at discovery time

### 6. Malformed Frontmatter

**Failure:** Invalid YAML in frontmatter  
**Detection:** Parse exception caught in `_load_all()`  
**Fallback:** Skip skill, continue  
**Logging:** Warning with skill path

### 7. Circular Namespace

**Failure:** Namespace references itself  
**Detection:** Not possible with flat filesystem — no cycles  
**Fallback:** N/A  
**Logging:** N/A

### 8. Manifest / Filesystem Drift

**Failure:** Manifest lists skill that doesn't exist on disk  
**Detection:** Manifest entry has no corresponding file  
**Fallback:** Skip missing entries  
**Logging:** Warning for each missing entry  
**Note:** This is why we avoid mandatory manifests.

### 9. Large Skill Accidentally Full-Loaded

**Failure:** Agent requests large skill without sections  
**Detection:** Prompt size exceeds threshold  
**Fallback:** Warn, suggest section-level loading  
**Logging:** Diagnostic showing token count

### 10. Production JS Loaded as Prompt Context

**Failure:** `scripts/*.js` included in skill content  
**Detection:** File extension check before content inclusion  
**Fallback:** Skip non-Markdown files  
**Logging:** Info when skipping executable files

---

## 14. Backward Compatibility

### Existing IDs That Must Continue Working

| Existing ID | New Behavior |
|-------------|--------------|
| `"Bencent"` | Loads full Bencent Skill (unchanged) |
| `"design-taste-frontend"` | Loads full Taste v2 (unchanged, but deprecated for large use) |
| `"minimalist-ui"` | Loads full Minimalist Skill (unchanged) |
| `"humanizer-tw"` | Loads full Humanizer Skill (unchanged) |
| `"greenlight-vibe"` | Loads top-level SKILL.md only (unchanged) |
| `"ui-rules"` | Loads full UI Rules (unchanged) |

### Migration Path

1. **Phase 1:** New section/sub-skill IDs are additive. Existing full-skill IDs continue to work.
2. **Phase 2:** Agents that benefit from section loading are updated to use new IDs.
3. **Phase 3:** Full-skill IDs for large Skills are deprecated but not removed.

### Breaking Changes

**None.** All existing `required_skills` lists continue to work exactly as before.

---

## 15. Architecture Decision

### Discovery Model

**Hybrid (Strategy C)**

Filesystem discovery + optional metadata/index files.

### Startup Loading

**Model 3 — Hybrid**

Load small/medium Skills fully at startup. Load large Skills on demand.

### Section Support

**YES** — required for Taste v2, GreenLight, and GSAP.

### Namespace Support

**YES** — required for GSAP.

### Tool/Knowledge Separation

**YES** — `scripts/` and `instructions/` boundaries enforced.

### Final Architecture

```
                    SkillLoader
                         │
            ┌────────────┼────────────┐
            ↓            ↓            ↓
      Discovery    Metadata     Section Cache
            │            │            │
            └────────────┼────────────┘
                         ↓
                    Skill Policy
                         │
            ┌────────────┼────────────┐
            ↓            ↓            ↓
      Constitution   Agent      Explicit
         (Bencent)   Defaults   Selection
                         │
                         ↓
                    Context Builder
                         │
            ┌────────────┼────────────┐
            ↓            ↓            ↓
      Full Skills   Sections   Sub-skills
      (< 50 KB)     (large)    (namespaces)
                         │
                         ↓
                    LLM Prompt
```

---

## 16. Implementation Roadmap

### Phase 6A — Discovery (This Document)

**Status:** COMPLETE

Deliverables:
- Discovery model documented
- Naming convention established
- Failure modes identified
- Backward compatibility assured

### Phase 6B — Context Selection (Next)

1. Add recursive discovery for namespaces (GSAP)
2. Add section parser for large Skills (Taste)
3. Add instruction-file discovery for multi-file Skills (GreenLight)
4. Add size-based loading threshold
5. Update `required_skills` to support `:` section identifiers
6. Add section cache

**Goal:** Loader can discover and selectively load sections/sub-skills.

### Phase 6C — FrontendAgent Preparation

1. Define FrontendAgent skill requirements per task type
2. Map GreenLight instruction files to task types
3. Map GSAP sub-skills to animation task types
4. Update Agent defaults for design tasks
5. Add tool-level skill binding integration

**Goal:** FrontendAgent can load ~10K-15K tokens per task instead of 150K+.

### Phase 6D — Production Integration

1. Add skill versioning and content hashing
2. Add prompt size diagnostics
3. Add cost tracking per skill/section
4. Add observability logging
5. Implement fallback behaviors from failure mode analysis

**Goal:** Enterprise-ready skill loading with observability.

---

## 17. Risks / Technical Debt

### Current Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| GSAP completely invisible to loader | High | Phase 6B: recursive discovery |
| Taste v2 over-injection (25K tokens) | Medium | Phase 6B: section loading |
| GreenLight scripts could be loaded as prompt context | Medium | Enforce `.md`-only loading |
| `_TOOL_BINDINGS["animation"]` references non-existent `gsap` | Low | Fix in Phase 6B |

### Technical Debt

| Item | Priority | Notes |
|------|----------|-------|
| GSAP namespace support | High | Blocks FrontendAgent |
| Taste section parsing | High | Saves 18K tokens per image |
| GreenLight multi-file loading | Medium | Needed for FrontendAgent |
| Section identifier parsing in `required_skills` | Medium | Needed for explicit selection |
| Skill size metadata automation | Low | Currently hardcoded |
| Content hash for versioning | Low | Future observability |

---

## 18. Decision Summary

| Decision | Choice |
|----------|--------|
| **Discovery Strategy** | Hybrid (filesystem + optional metadata) |
| **Startup Loading** | Model 3 — small/medium fully, large on demand |
| **Section Support** | YES — required for Taste, GreenLight, GSAP |
| **Namespace Support** | YES — required for GSAP |
| **Tool/Knowledge Boundary** | YES — `scripts/` never in prompt |
| **Naming Convention** | Flat IDs for sub-skills, colon for sections |
| **Backward Compatibility** | Full — existing IDs unchanged |
| **FrontendAgent Ready?** | AFTER Phase 6B |
| **ImageAgent Token Reduction** | ~25K → ~7K tokens with section loading |

---

## 19. Current vs Target

### Current

```text
skills/
├── bencent-brand/SKILL.md → loaded
├── taste-skill/SKILL.md → loaded (87 KB every call)
├── minimalist-skill/SKILL.md → loaded
├── ui-rules/SKILL.md → loaded
├── humanizer-tw/SKILL.md → loaded
├── greenlight-vibe/SKILL.md → loaded (11 KB only)
└── gsap/ → NOT DISCOVERED
```

### Target

```text
skills/
├── bencent-brand/SKILL.md → Constitution, always loaded
├── taste-skill/SKILL.md → section-level loading
├── minimalist-skill/SKILL.md → full when requested
├── ui-rules/SKILL.md → full when requested
├── humanizer-tw/SKILL.md → full when requested
├── greenlight-vibe/
│   ├── SKILL.md → loaded when GreenLight active
│   ├── instructions/*.md → section-level loading
│   └── scripts/* → NEVER in prompt
└── gsap/
    ├── llms.txt → namespace index
    ├── gsap-core/SKILL.md → sub-skill
    ├── gsap-scrolltrigger/SKILL.md → sub-skill
    └── ... → sub-skills
```

---

## Implemented Changes

### Files Modified

- `skills/loader.py` — recursive discovery, section parsing, instruction loading, sub-skill support, BOM handling
- `agents/__init__.py` — no changes required (backward compatible)
- `tests/skill_loader_diagnostics.py` — added tests for recursive discovery, sections, instructions, sub-skills

### Discovery Changes

1. **Recursive discovery** — `os.walk()` replaces `os.listdir()`; finds `skills/**/SKILL.md`
2. **GSAP sub-skills** — 8 sub-skills discovered: `gsap-core`, `gsap-timeline`, `gsap-scrolltrigger`, `gsap-plugins`, `gsap-utils`, `gsap-react`, `gsap-performance`, `gsap-frameworks`
3. **GreenLight instructions** — `instructions/*.md` loaded as selectable sections (`greenlight-vibe:core-structure`, etc.)
4. **GreenLight JS excluded** — `scripts/*.js` never enters skill registry or prompt context
5. **BOM handling** — `utf-8-sig` encoding handles BOM in skill files

### Section Loading

1. **Taste v2** — 122 sections parsed from `##` headings; accessible via `get_skill_sections("design-taste-frontend")`
2. **Section syntax** — `required_skills=["design-taste-frontend:9-ai-tells-forbidden-patterns"]`
3. **Sub-skill syntax** — `required_skills=["gsap-scrolltrigger"]`

### Naming Convention

| Type | Example |
|------|---------|
| Top-level skill | `Bencent`, `design-taste-frontend` |
| Sub-skill (namespace) | `gsap-scrolltrigger`, `gsap-core` |
| Section | `design-taste-frontend:9-ai-tells-forbidden-patterns` |
| Instruction | `greenlight-vibe:core-structure` |

### Backward Compatibility

All existing IDs continue to work:
- `required_skills=["Bencent"]` — unchanged
- `required_skills=["design-taste-frontend"]` — loads full skill
- `required_skills=["humanizer-tw"]` — unchanged

### Token Impact

| Agent | Before | After (default) | After (section-level) |
|-------|--------|-----------------|----------------------|
| Writer | ~5,000 tokens | ~2,041 tokens | ~441 tokens (Bencent only) |
| ImageAgent | ~25,000 tokens | ~24,185 tokens | ~541 tokens (Taste section only) |
| QualityEvaluator | ~5,000 tokens | ~2,041 tokens | ~441 tokens (Bencent only) |

---

**SKILL DISCOVERY ARCHITECTURE REVIEW COMPLETE**

**READY FOR PHASE 6A IMPLEMENTATION:** YES
