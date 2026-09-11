# Phase 7C — Frontend Pipeline Architecture Review

**Date:** 2026-09-09  
**Reviewer:** Kilo  
**Scope:** Read-only architecture review — no code modified  
**Status:** COMPLETE

---

## 1. Current Architecture

### 1.1 Completed Components (Phases 7A–7B)

| Phase | Component | Type | Status |
|-------|-----------|------|--------|
| 7A | `FrontendRequest`, `FrontendResult`, `GreenLightConversionResult` | Contracts | ✅ Complete |
| 7B-2 | `GreenLightConverter` | Tool (stdin/stdout, no tempfiles) | ✅ Complete |
| 7B-3 | `FrontendAgent` | Agent (method-level skill selection) | ✅ Complete |
| 7B-4 | `FrontendSecurityGate` | Tool (deterministic, fail-closed) | ✅ Complete |
| 7B-4A | CSS `url()` + SVG hardening | Tool enhancements | ✅ Complete |

### 1.2 Current Pipeline Flow

```
FrontendAgent
      ↓
FrontendSecurityGate
      ↓
GreenLightConverter
      ↓
FrontendValidator (NOT YET IMPLEMENTED)
      ↓
WordPressPublisher
```

### 1.3 Contracts in Use

| Contract | Producer | Consumer |
|----------|----------|----------|
| `FrontendRequest` | Workflow/Planner | FrontendAgent |
| `FrontendResult` | FrontendAgent | SecurityGate, Converter, Validator, Publisher |
| `GreenLightConversionResult` | GreenLightConverter | Validator, Publisher |
| `FrontendSecurityResult` | FrontendSecurityGate | Workflow (for gate decision) |

---

## 2. FrontendValidator Responsibility

### 2.1 Three-Layer Separation

| Layer | Question | Tool | Verdict |
|-------|----------|------|---------|
| **Security** | Is this output allowed to continue? | `FrontendSecurityGate` | ✅ Implemented |
| **Technical Validity** | Is this output technically valid and deliverable? | `FrontendValidator` | ❌ Not yet implemented |
| **Quality / Design** | Is this output good, on-brand, free of AI-slop? | Quality Review | ❌ Not yet implemented |

### 2.2 FrontendValidator Must Validate (Technical Only)

| Check | Rationale |
|-------|-----------|
| HTML well-formedness (parseable) | Prevents WordPress block parser errors |
| CSS syntax validity | Prevents broken styles |
| JavaScript syntax validity | Prevents runtime errors |
| Size limits (re-verify) | Defense in depth |
| Block syntax (post-conversion) | Ensures Greenshift compatibility |
| Required attributes present (e.g., `alt` on images) | Accessibility baseline |

### 2.3 FrontendValidator Must NOT Validate

| Check | Belongs To |
|-------|------------|
| XSS, event handlers, dangerous URLs | `FrontendSecurityGate` |
| Brand compliance, tone, anti-slop | Quality Review |
| SEO completeness | SEO Agent / Quality Review |
| Design aesthetics | Quality Review |

### 2.4 Recommended Interface

```python
class FrontendValidator(BaseTool):
    def validate(self, result: FrontendResult) -> FrontendResult:
        """
        Mutates result.validation_status to "passed" | "failed" | "warnings"
        Appends to result.errors / result.warnings
        Returns the same result object (no new contract)
        """
```

---

## 3. Security vs Validator vs Quality Boundary

| Responsibility | Security Gate | Technical Validator | Quality Review |
|----------------|---------------|---------------------|----------------|
| Dangerous tags/handlers | ✅ REJECT | — | — |
| Malformed HTML/CSS/JS | — | ✅ REJECT | — |
| Brand colors/fonts | — | — | ✅ SCORE |
| Anti-AI patterns | — | — | ✅ SCORE |
| SEO completeness | — | — | ✅ SCORE |
| Accessibility baseline | — | ✅ MINIMAL | — |
| **Decision** | **Gate: pass/fail** | **Gate: pass/fail** | **Score: pass/retry/fail** |

**Key Principle:** Security and Technical Validation are **binary gates**. Quality Review produces a **score** with retry routing.

---

## 5. Why Each Validation Belongs to FrontendValidator (Completion Report Item 5)

### 5.1 HTML Tag-Balance Checking → FrontendValidator (Technical Validity)

**Why not SecurityGate:** SecurityGate rejects *dangerous* constructs (XSS vectors: `<script>`, `on*`, `javascript:`). A missing `</div>` is not a security vulnerability — it cannot execute code or exfiltrate data. It is a structural defect that breaks the WordPress block parser and rendering.

**Why not Quality Review:** Quality Review evaluates subjective design/brand criteria. Tag balance is a binary technical property — the HTML either parses or it doesn't. No LLM judgment is needed.

**Why Validator:** WordPress expects well-formed block HTML. An unclosed tag cascades into downstream rendering failures (broken layout, editor crashes). This is a *deliverability* concern: technically invalid output cannot be published reliably.

### 5.2 CSS Brace-Matching / Syntax → FrontendValidator (Technical Validity)

**Why not SecurityGate:** SecurityGate validates CSS for dangerous patterns (`expression()`, `behavior:`, `javascript:` URLs, non-image `data:`). A missing `}` or malformed selector is not an attack vector — it simply breaks the stylesheet.

**Why not Quality Review:** Quality Review might flag "unused CSS" or "overly specific selectors" as quality issues. A syntax error is not a style choice — it prevents the browser from applying any subsequent rules.

**Why Validator:** Browsers are strict about CSS syntax. A single unclosed brace invalidates the entire following stylesheet. This is a technical correctness check required for the output to function.

### 5.3 JavaScript Syntax Check → FrontendValidator (Technical Validity)

**Why not SecurityGate:** SecurityGate rejects dangerous JS patterns (`eval`, `new Function`, `document.cookie`, `fetch` to non-allowlisted domains). A syntax error like `const x =` is not malicious — it is broken code that throws at parse time.

**Why not Quality Review:** Quality Review might evaluate "code quality," "modern patterns," or "bundle size." A syntax error is not a quality issue — the code simply does not run.

**Why Validator:** The browser's JS parser rejects invalid syntax before execution. This is a binary technical gate: valid syntax = runnable; invalid syntax = guaranteed runtime error.

### 5.4 GreenLight Blocks Format (WP Comment Balance + JSON Attributes) → FrontendValidator (Technical Validity)

**Why not SecurityGate:** SecurityGate validates block *content* for dangerous patterns. Block *syntax* (balanced `<!-- wp:... -->` / `<!-- /wp:... -->` comments, valid JSON in attributes) is a structural requirement for the WordPress block parser.

**Why not Quality Review:** Quality Review evaluates whether blocks are "on-brand" or "well-designed." A block with unbalanced comments is not a design choice — it is structurally invalid and will fail to render in the editor.

**Why Validator:** The WordPress block parser requires balanced comment delimiters and valid JSON attributes. This is a platform compatibility check: invalid block syntax = WordPress cannot parse the block = publish failure.

### 5.5 Summary Table

| Validation | SecurityGate Concern? | Quality Review Concern? | Validator Concern? | Reason |
|------------|----------------------|------------------------|-------------------|--------|
| HTML tag balance | No (not XSS) | No (not subjective) | **Yes** | Deliverability: parser must succeed |
| CSS brace matching | No (not attack) | No (not style) | **Yes** | Deliverability: stylesheet must parse |
| JS syntax | No (not malicious) | No (not quality) | **Yes** | Deliverability: code must parse |
| Block syntax | No (not content danger) | No (not design) | **Yes** | Platform compatibility: WP block parser must succeed |

---

## 6. GreenLightConverter Boundary

### 4.1 What Converter Guarantees

| Guarantee | Mechanism |
|-----------|-----------|
| Deterministic: same input → same output | No LLM in path |
| Structured output: `GreenLightConversionResult` | Contract |
| Errors captured: Node exit code, stderr, timeout | `subprocess.run` |
| No temp files (Phase 7B-2A) | stdin/stdout |

### 4.2 What Converter Does NOT Guarantee

| Non-Guarantee | Why |
|---------------|-----|
| Visual fidelity | HTML→Block is lossy; spacing/alignment may differ |
| Full HTML/CSS support | `convert.js` has limited tag/property coverage |
| Round-trip perfection | `deconvert` is best-effort |
| Block validity beyond syntax | WordPress may still reject blocks at runtime |

### 4.3 Failure Handling Recommendation

| Failure Mode | Action |
|--------------|--------|
| Node.js not found | Hard fail — infrastructure issue |
| Timeout (120s) | Hard fail — retry won't help |
| Non-zero exit + stderr | Soft fail — pass error to Validator/Workflow |
| Empty blocks output | Soft fail — treat as conversion failure |

### 4.4 Fallback Recommendation

**Do NOT implement fallback to raw HTML in WordPressPublisher.**

Rationale:
- WordPress block editor expects blocks; raw HTML in Custom HTML block is a different UX
- Mixing blocks + raw HTML creates inconsistent editing experience
- If conversion fails, the task should **retry with feedback** (see Section 5)

**Exception:** A future "emergency publish" mode could bypass blocks, but not in the standard pipeline.

### 4.5 Verdict

| Aspect | Verdict |
|--------|---------|
| Converter as mandatory gate | **KEEP** |
| Converter as sole path to WordPress | **KEEP** (with retry feedback) |
| Fallback to raw HTML | **REMOVE** (not needed, creates inconsistency) |

---

## 5. Frontend Retry Loop Architecture

### 5.1 Failure Flow

```
FrontendAgent
      ↓
FrontendSecurityGate
      ↓ (FAIL) → RETRY with security feedback
GreenLightConverter
      ↓ (FAIL) → RETRY with conversion error
FrontendValidator
      ↓ (FAIL) → RETRY with validation errors
```

### 5.2 Retry Ownership

| Layer | Owner | Feedback Mechanism |
|-------|-------|-------------------|
| Security | Workflow | `FrontendSecurityResult.errors` → injected into next `FrontendRequest` |
| Conversion | Workflow | `GreenLightConversionResult.errors` → injected into next `FrontendRequest` |
| Validation | Workflow | `FrontendResult.errors` → injected into next `FrontendRequest` |

### 5.3 Retry Contract Extension

Add to `FrontendRequest`:

```python
previous_errors: List[str] = field(default_factory=list)
previous_warnings: List[str] = field(default_factory=list)
retry_count: int = 0
```

### 5.4 Maximum Retries

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Frontend max retries | 3 | Separate from content retry (`max_retries`) |
| Content max retries | 3 (existing) | Unchanged |
| Total per task | 6 | Bounded total cost |

### 5.5 After Max Retries — Failure Visibility Contract

| Outcome | Action |
|---------|--------|
| All retries exhausted | Task status → `FAILED_NEEDS_ATTENTION`, error = "Frontend pipeline failed after N retries" |
| Partial success (security pass, conversion fail) | Still `FAILED_NEEDS_ATTENTION` — no partial publish |

#### 5.5.1 Failure-State Contract: `FAILED_NEEDS_ATTENTION`

A distinct terminal task status is required, conceptually different from generic `TaskStatus.FAILED`:

| Status | Meaning |
|--------|---------|
| `FAILED` | Generic/unclassified execution failure (exception, infrastructure, etc.) |
| `FAILED_NEEDS_ATTENTION` | **The system exhausted its permitted automatic recovery/retry attempts and could not safely or correctly complete the task.** |

This distinction is architecturally significant because `FAILED_NEEDS_ATTENTION` is:

- **Queryable** — can be filtered, counted, listed
- **Dashboard-ready** — future UI can surface it without parsing error strings
- **Notification-ready** — external systems can react to this specific state
- **Actionable** — tells an operator "retries are done, human intervention required"

> **Do NOT implement the Enum now.** This is a documented architectural requirement for future implementation.

#### 5.5.2 Failure Record — Durable State

When a task reaches `FAILED_NEEDS_ATTENTION`, the durable workflow state MUST retain:

1. **Final failed gate**
   - `Security` | `Conversion` | `Validation` | `Quality` | `Content`

2. **Final error / feedback**
   - The concrete reason the final attempt failed
   - Structured where possible (e.g., `["Dangerous HTML tag detected: <script>"]`)

3. **Retry history** (per attempt)
   - attempt number
   - gate involved
   - error/feedback
   - action taken (retry / abort)
   - result

4. **Terminal failure timestamp** (ISO 8601)

5. **Existing task identity information** (`task_id`, `title`, `content_type`, etc.)

The purpose is not only debugging. The record must allow a future operator to answer:

> **What failed, why did it fail, how many times did the system try to recover, and what happened on each attempt?**

#### 5.5.3 Durability Requirement

> **Failure visibility is a state and logging requirement, NOT a dashboard requirement.**

Even before any client-facing UI exists:

```
Task
  ↓
FAILED_NEEDS_ATTENTION
  ↓
Persisted Workflow State
  +
Structured Logs
```

must make the failure discoverable.

- Do NOT require a UI for failure visibility
- Do NOT implement notifications in this phase
- Do NOT implement a dashboard in this phase

The persisted workflow state (JSON/file) and structured logs ARE the failure visibility mechanism until a dashboard exists.

#### 5.5.4 Retry Architecture Clarification

`retry_count` alone is insufficient. The system needs **both**:

```
retry count
+
retry history
```

Because:
- `retry_count` answers: **How many attempts?**
- `retry_history` answers: **What happened during those attempts?**

The retry mechanism must also distinguish:

- **frontend retry** (Security / Converter / Validator failures)
- **content retry** (Writer / Critic / SEO / Reviewer failures)

And must not create an uncontrolled infinite loop. Maximum retries are bounded (Section 5.4).

#### 5.5.5 Product Principle

> **No autonomous workflow should be allowed to fail silently.**

Intended product behavior:

```
SUCCESS
    ↓
published

RECOVERABLE FAILURE
    ↓
automatic retry / correction

UNRECOVERABLE FAILURE
    ↓
FAILED_NEEDS_ATTENTION
    ↓
durably recorded + queryable
    ↓
future dashboard / notification can surface it
```

This allows the current project to remain CLI/state/log based while still being architecturally ready for a client-operated product.

### 5.6 Verdict

| Aspect | Verdict |
|--------|---------|
| Separate frontend retry counter | **KEEP** |
| Error feedback injection | **KEEP** |
| Shared retry with content | **REWORK** — must be separate counter |

---

## 6. Known Limitations & False-Positive Risks (Completion Report Item 7)

### 6.1 HTML Validation — Custom HTMLParser

| Risk | Description | Impact |
|------|-------------|--------|
| **Void element handling** | Parser treats `<br>`, `<img>`, `<input>` as void (no closing tag needed). If input uses `<br></br>` or `<img></img>`, parser may report "unclosed" for the explicit closing tag. | False positive: valid HTML flagged as error. Consumes 1 retry. |
| **Self-closing syntax** | `<div/>` is valid HTML5 (void-like) but parser may not recognize it as self-closing, expecting `</div>`. | False positive on valid self-closing tags. |
| **Comments with `--`** | HTML comments cannot contain `--` except at start/end. Parser may not detect this, allowing invalid comments through (false negative) OR may over-report on valid comments with dashes in content. | False positive or false negative on edge-case comments. |
| **Template/interpolation syntax** | FrontendAgent may output templating syntax (`{{variable}}`, `{% if %}`) inside HTML. Parser treats as text, not tags. Valid templated HTML may pass; malformed templated HTML may not be caught. | False negative risk for templated output. |
| **Case sensitivity** | HTML is case-insensitive for tag names. Parser uses lowercase comparison. `<DIV></div>` passes; `<Div></Div>` passes. No false positive here, but worth noting. | None. |

**Pipeline Impact of False Positive:** A technically valid page fails validation → retry consumes 1 of 3 attempts. If the Agent cannot produce output that satisfies the parser (e.g., due to template syntax), all 3 retries exhaust → task routes to `FAILED_NEEDS_ATTENTION`. Since no human review exists in the automated delivery model, a publishable page is never published. This is a **real product-quality risk**.

### 6.2 CSS Validation — Brace Matching

| Risk | Description | Impact |
|------|-------------|--------|
| **Strings containing braces** | CSS strings/urls: `content: "}";` or `background: url("data:image/svg+xml,{...}")`. Simple brace counter sees `}` inside string as rule-end. | False positive: valid CSS flagged as unclosed rule. |
| **`@media` / `@supports` / `@keyframes` nesting** | These at-rules contain nested braces. A simple depth counter works *if* it correctly tracks at-rule boundaries. Mis-tracked nesting → false positive/negative. | False positive on valid nested at-rules. |
| **CSS custom properties with braces** | `--foo: { bar: baz; };` — valid but rare. Counter may misinterpret. | False positive on valid custom properties. |
| **Calc/func with braces** | `clamp(1rem, 2vw, 3rem)` — parentheses, not braces. No issue. But `var(--x, { fallback })` — not valid CSS. | Low risk. |
| **Comments with braces** | `/* } */` — counter may see closing brace. | False positive on valid comments. |

**Pipeline Impact:** Same as HTML — consumes retry, may exhaust retries → `FAILED_NEEDS_ATTENTION` for publishable CSS.

### 6.3 JavaScript Validation — `node --check`

| Risk | Description | Impact |
|------|-------------|--------|
| **Top-level `await`** | Valid in ES modules (`type: "module"`). `node --check` on CommonJS treats as syntax error. FrontendAgent output context may be ambiguous. | False positive if Agent outputs module-style code. |
| **Experimental syntax** | Stage 3 proposals (decorators, private fields) may parse in newer Node but fail in older. Version mismatch → false positive. | False positive on valid modern JS. |
| **TypeScript / Flow syntax** | `const x: number = 1;` — invalid JS, valid TS. If Agent accidentally outputs TS, validator rejects. | Correct rejection (not false positive) but may indicate Agent issue. |
| **Hashbang / shebang** | `#!/usr/bin/env node` at top — valid for CLI scripts. `node --check` accepts it. | No false positive. |
| **Source maps / pragmas** | `//# sourceMappingURL=...` — valid. No issue. | None. |

**Pipeline Impact:** If Agent outputs valid modern/module JS that `node --check` rejects due to version or mode, retries exhaust → `FAILED_NEEDS_ATTENTION`.

### 6.4 GreenLight Blocks Validation — WP Comment Balance + JSON

| Risk | Description | Impact |
|------|-------------|--------|
| **Comments inside block content** | Block inner HTML may contain `<!-- comment -->`. Validator counts these as block delimiters if regex is naive. | False positive: valid inner comments flagged as unbalanced blocks. |
| **JSON with escaped quotes** | `{"attr":"value with \"quotes\""}` — regex-based JSON extraction may fail on escaped quotes. | False positive: valid JSON flagged as invalid. |
| **Multiple blocks on same line** | `<!-- wp:b1 -->content<!-- /wp:b1 --><!-- wp:b2 -->content<!-- /wp:b2 -->` — regex may not handle adjacent blocks. | False positive/negative on compact output. |
| **Block variations (dynamic blocks)** | `<!-- wp:block {"ref":123} /-->` self-closing variant. Validator expects separate open/close. | False positive on valid self-closing block syntax. |
| **Whitespace in block comments** | `<!--wp:block-->` (no space) — WordPress accepts. Validator regex may require space. | False positive on valid compact syntax. |

**Pipeline Impact:** Conversion output from `convert.js` may produce valid block syntax that validator rejects → retries exhaust → `FAILED_NEEDS_ATTENTION`. Since Converter and Validator are both deterministic, this indicates a *contract mismatch* between them, not an Agent error.

### 6.5 Cross-Category Mitigations (Future)

| Mitigation | Applies To |
|------------|------------|
| Use proper parsers (htmlparser2, PostCSS, Acorn, WordPress block parser) instead of custom regex/state machines | HTML, CSS, JS, Blocks |
| Align Validator logic with Converter output — test against Converter snapshots | Blocks |
| Add Node version pinning and `--experimental-vm-modules` for module support | JS |
| Allow "warning" severity for edge cases that are technically valid but parser-uncertain | All |
| Log false-positive patterns to evolve parser without breaking pipeline | All |

---

## 7. Publisher Gate Conditions

### 6.1 Required Preconditions

`WordPressPublisher.publish_content()` executes IFF:

```
FrontendSecurityGate:     PASSED
GreenLightConverter:      PASSED (blocks non-empty)
FrontendValidator:        PASSED (validation_status == "passed")
Quality Review:           PASSED (score ≥ threshold)
Content Review:           PASSED (existing Reviewer)
```

### 6.2 Gate Enforcement Location

**In `main.py` workflow**, before calling `_get_agent("publisher")`:

```python
# Pseudo-code
if not all([
    task.frontend_security_passed,
    task.frontend_conversion_passed,
    task.frontend_validation_passed,
    task.content_review_passed,
]):
    raise RuntimeError("Publisher gate not satisfied")
```

### 6.3 Verdict

| Condition | Verdict |
|-----------|---------|
| Security pass required | **KEEP** |
| Conversion pass required | **KEEP** |
| Technical validation pass required | **KEEP** |
| Quality pass required | **KEEP** |
| Content review pass required | **KEEP** |
| Gate in Workflow (not Publisher) | **KEEP** |

---

## 7. Client Profile Architecture

### 7.1 Current → Future Transition

| Current | Future |
|---------|--------|
| `Bencent` Brand (hardcoded global) | `ClientProfile` (configurable) |
| Global Skills (Taste, Minimalist, UI Rules) | Global Skills + Client Overrides |
| Single `config.py` | Per-client config + shared defaults |

### 7.2 Client Profile Data Model

```python
@dataclass
class ClientProfile:
    client_id: str
    brand_name: str
    tone: str                    # e.g., "professional", "friendly", "editorial"
    colors: Dict[str, str]       # primary, secondary, accent, neutral
    typography: Dict[str, str]   # headline_font, body_font, sizes
    writing_rules: List[str]     # forbidden words, preferred terms
    frontend_design_rules: Dict  # spacing, density, motion preferences
    seo_rules: Dict              # keyword density, meta templates
    image_style: Dict            # aspect ratio, style prompts
    forbidden_patterns: List[str] # regex or keyword lists
```

### 7.3 What Remains Global Skills

| Skill | Reason |
|-------|--------|
| `Taste` | Anti-slop design principles (universal) |
| `Minimalist` | UI restraint philosophy (universal) |
| `UI Rules` | Component standards (universal) |
| `GreenLight` | WordPress block conversion (platform-specific, not brand-specific) |
| `GSAP` | Animation techniques (universal) |
| `Humanizer` | Natural writing (language-specific, not brand-specific) |

### 7.4 What Becomes Client Configuration

| Element | Current Location | Future Location |
|---------|------------------|-----------------|
| Brand colors | `bencent-brand` Skill | `ClientProfile.colors` |
| Typography | `bencent-brand` Skill | `ClientProfile.typography` |
| Tone/voice | `bencent-brand` + `humanizer-tw` | `ClientProfile.tone` + writing rules |
| Forbidden words | `bencent-brand` | `ClientProfile.forbidden_patterns` |
| Design density/motion | `Taste` defaults | `ClientProfile.frontend_design_rules` |
| SEO templates | Prompt files | `ClientProfile.seo_rules` |

### 7.5 Bencent Brand → Client Profile Migration

**Bencent Brand becomes the *default* Client Profile.**

- No hardcoded authority
- System ships with `bencent` profile pre-loaded
- Clients add their own profiles
- Agent loading logic: `ClientProfile` overrides Skill defaults

---

## 8. Skills vs Client Configuration

### 8.1 Architecture Principle

```
Global Skills (capabilities)
      +
Client Configuration (brand/identity)
      ↓
Agent receives merged context
```

### 8.2 Skill Classification

| Skill | Type | Client Override? |
|-------|------|------------------|
| `Taste` | Capability (judgment framework) | Partial (dials) |
| `Minimalist` | Capability (design philosophy) | Yes (density/motion) |
| `UI Rules` | Capability (component rules) | Yes (spacing/typography) |
| `GreenLight` | Capability (platform integration) | No |
| `GSAP` | Capability (animation knowledge) | No |
| `Humanizer` | Capability (writing style) | Yes (tone/rules) |
| `Bencent` | **Becomes** Client Profile | N/A |

### 8.3 Injection Mechanism (Future)

```python
# In SkillLoader.build_skills_context()
def build_skills_context(agent_name, required_skills, client_profile=None):
    # 1. Load global skills
    # 2. If client_profile: append client-specific overrides as "virtual skill"
    # 3. Client overrides have HIGHEST priority (override Skills)
```

**Critical:** Client configuration must **override** Skill defaults, not just append. Priority order:

1. Constitution (Client Profile identity)
2. Client Profile overrides
3. Global Skills (Taste, Minimalist, UI Rules)
4. Project defaults

---

## 9. Future Product Architecture

### 9.1 Logical Multi-Client Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    AI WORDPRESS FACTORY                       │
├─────────────────────────────────────────────────────────────┤
│  Global Capabilities (Skills)                                 │
│  ├── Taste, Minimalist, UI Rules, GreenLight, GSAP,          │
│  │   Humanizer, SEO, Research, Critic...                     │
│  │                                                           │
│  Client Profiles                                              │
│  ├── Client A Profile → colors, tone, rules                  │
│  ├── Client B Profile → colors, tone, rules                  │
│  └── ...                                                      │
│                                                               │
│  Workflow Engine (stateless, profile-aware)                   │
│  ├── Planner → Research → Writer → Critic → SEO → Reviewer   │
│  ├── FrontendAgent → SecurityGate → Converter → Validator    │
│  └── Publisher                                                │
└─────────────────────────────────────────────────────────────┘
```

### 9.2 Key Boundaries

| Boundary | Enforcement |
|----------|-------------|
| Client isolation | Separate `Task` objects, separate `ClientProfile` per task |
| Skill sharing | Global skills loaded once, client config merged per-request |
| Data privacy | Per-client config stored separately; no cross-client data flow |
| Billing/quotas | Out of scope for Phase 7C (handled at API layer) |

### 9.3 Agent Profile Awareness

Agents become **profile-aware**, not **client-aware**:

```python
class BaseAgent:
    def call_ai(self, prompt, required_skills, client_profile=None):
        skills_context = skill_loader.build_skills_context(
            self.name, required_skills, client_profile=client_profile
        )
        # ... rest unchanged
```

No Agent code changes required beyond passing `client_profile` through the call chain.

---

## 10. Adversarial Review

| Decision | Challenge | Verdict | Reason |
|----------|-----------|---------|--------|
| **FrontendValidator as separate Tool** | Why not merge into SecurityGate? | **KEEP** | Different failure semantics: security=hard fail, validation=retryable |
| **GreenLightConverter as mandatory gate** | Why not allow raw HTML fallback? | **KEEP** | Consistency: all output goes through blocks; fallback creates two publishing paths |
| **Separate frontend retry counter** | Why not reuse content retry? | **KEEP** | Frontend failures are independent; shared counter masks root cause |
| **Client Profile overrides Skills** | Why not just extend Skills? | **KEEP** | Skills are capabilities; Client Profile is identity. Mixing them conflates universal vs specific. |
| **Bencent as default profile** | Why not keep Bencent as Constitution? | **SIMPLIFY** | Constitution becomes "platform rules" (safety, ethics); Brand becomes Client Profile. |
| **Quality Review as separate from Validator** | Why not combine? | **KEEP** | Validator = deterministic/technical; Quality = LLM/subjective. Different retry behavior. |
| **Security Gate before Converter** | What if Converter adds vulnerabilities? | **REWORK** | Add post-conversion security check in Validator (block syntax + URL re-check). |
| **Validation in FrontendResult** | God Object risk? | **SIMPLIFY** | Add `FrontendValidationResult` if fields exceed 10. Currently acceptable. |

---

## 11. Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Client Profile complexity leaks into Agents | High | Keep Agents profile-agnostic; all merging in SkillLoader |
| Skill priority conflicts (Global vs Client) | Medium | Explicit priority order; test with contradiction cases |
| Converter non-determinism across Node versions | Medium | Lock Node version in CI; test conversion snapshots |
| Frontend retry loop token cost | Medium | Separate counter + max 3; feedback injection reduces repeated errors |
| Validator LLM calls add latency/cost | Low | Technical validation is deterministic; only Quality Review uses LLM |
| **Validator false positives exhaust retries** | **Medium** | Custom parsers may reject valid output; 3 retries → `FAILED_NEEDS_ATTENTION` for publishable content. Mitigation: proper parsers, warning severity for edge cases, Converter-Validator contract testing. |
| WordPress block format changes break Converter | Medium | Version `convert.js`; test against target WP version |
| **Silent terminal failure** | **Critical** | Distinct `FAILED_NEEDS_ATTENTION` terminal state; persisted failure reason; failed gate recorded; retry history retained; structured logs; future dashboard/notification can consume the same state |

### 11.1 Silent Terminal Failure — Detailed Analysis

**Risk:** If the workflow only records generic `FAILED`, the client may not know that a content task failed after retries were exhausted.

**Impact:**
- Website content may remain outdated
- Scheduled/expected content may never be published
- Client loses trust in automation
- Operational failure becomes invisible

**Mitigation (architectural, documented for future implementation):**
- Distinct `FAILED_NEEDS_ATTENTION` terminal state (not generic `FAILED`)
- Persisted failure reason (structured, not free-text)
- Failed gate recorded (`Security`, `Conversion`, `Validation`, `Quality`, `Content`)
- Retry history retained (per-attempt: gate, error, action, result)
- Structured logs (JSON, queryable)
- Future dashboard/notification can consume the same state without parsing

**Product Principle:** No autonomous workflow should be allowed to fail silently.

---

## 12. Recommended Next Implementation Phase

### Phase 7C-1: FrontendValidator Implementation

**Goal:** Deterministic technical validation tool.

**Files:**
- `tools/frontend_validator.py` — `FrontendValidator(BaseTool)`
- `tests/test_frontend_validator.py` — syntax, size, block structure tests

**Contract:** Reuses `FrontendResult` (mutates `validation_status`, `errors`, `warnings`)

### Phase 7C-2: Workflow Integration

**Goal:** Wire pipeline into `main.py`.

**Changes:**
- `state.py` — add `FRONTEND_*` TaskStatus values, frontend fields to `Task`
- `main.py` — add frontend steps after Reviewer, before Publisher
- `contracts.py` — add `previous_errors`, `retry_count` to `FrontendRequest`

### Phase 7C-3: Retry Feedback Loop

**Goal:** Error feedback from Security/Converter/Validator → next FrontendAgent attempt.

**Changes:**
- `agents/frontend.py` — accept `previous_errors` in `_build_frontend_prompt()`
- `main.py` — implement retry loop with error injection

### Phase 7C-4: Client Profile Foundation (Architecture Only)

**Goal:** Define `ClientProfile` dataclass and SkillLoader extension point.

**Files:**
- `contracts.py` — add `ClientProfile` dataclass
- `skills/loader.py` — add `client_profile` parameter to `build_skills_context()`

**No Agent changes yet.**

---

## 13. Summary

| Area | Decision |
|------|----------|
| FrontendValidator responsibility | Technical validity only (syntax, structure, size) |
| Security/Validator/Quality boundary | Three distinct layers: Gate → Gate → Score |
| GreenLightConverter | Mandatory gate, no fallback, retry on failure |
| Retry architecture | Separate counter (max 3), error feedback injection |
| Publisher gate | 5 conditions: Security, Conversion, Validation, Quality, Content |
| Client Profile | Replaces Bencent Brand as configurable identity |
| Skills | Remain global capabilities; Client Profile overrides |
| Future architecture | Profile-aware Workflow, shared Skills, isolated Tasks |

---

**READY FOR PHASE 7C-1 IMPLEMENTATION**

All architectural boundaries defined. No code modified. Document complete.