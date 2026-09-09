# Taste Skill Source Documentation

## Repository

* **Repository:** `leonxlnx/taste-skill`
* **URL:** https://github.com/leonxlnx/taste-skill
* **Branch:** `main`
* **Latest commit:** 154 commits (as of migration date)

## Adopted Version

* **Taste v2 / `design-taste-frontend`**
* **Install name:** `design-taste-frontend`
* **Status:** v2 experimental (as defined by upstream)
* **Migration date:** 2026-09-08

## Adoption Scope

* **Migrated:** `design-taste-frontend` Taste Skill only
* **Not migrated:** All other skills in the repository
  * `taste-skill-v1` (design-taste-frontend-v1)
  * `gpt-tasteskill` (gpt-taste)
  * `image-to-code-skill` (image-to-code)
  * `redesign-skill` (redesign-existing-projects)
  * `soft-skill` (high-end-visual-design)
  * `minimalist-skill` (minimalist-ui)
  * `brutalist-skill` (industrial-brutalist-ui)
  * `stitch-skill` (stitch-design-taste)
  * `brandkit` (brandkit)
  * `imagegen-frontend-web` (imagegen-frontend-web)
  * `imagegen-frontend-mobile` (imagegen-frontend-mobile)
  * `output-skill` (full-output-enforcement)

## Local Source

* **Migrated from:** GitHub `main` branch
* **NOT from:** `~/.claude/skills/taste-skill/` (local legacy version preserved, not used)

## File Structure

```
skills/taste-skill/
└── SKILL.md  (87,253 bytes)
```

## Notes

* Taste v2 is a single-file skill (`SKILL.md` only).
* The skill includes inline code skeletons for GSAP patterns (Sticky-Stack, Horizontal-Pan, Scroll-Reveal Stagger).
* No additional supporting files, scripts, or examples were required for standalone operation.
* The skill is framework-agnostic and works with React, Vue, Svelte, or vanilla HTML/CSS/JS.

## Integration Status

* **Current phase:** Migration only
* **Not integrated into:** Agents, Workflow, or any Python code
* **Future integration:** To be handled in the FrontendAgent / Website Factory phase
