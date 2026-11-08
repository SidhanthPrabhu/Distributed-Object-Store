# Starting work with Codex

These are repository-specific task prompts. The repository's `AGENTS.md` records scope and ownership; this use of a repository instruction file follows [official OpenAI guidance](https://developers.openai.com/codex/guides/agents-md). A prompt still identifies which teammate role and card to implement.

## Before either person starts

1. The repository owner commits and pushes this planning package; see [GitHub setup](GITHUB-START.md).
2. Each person opens their own clone/checkout of the same repository in Codex and starts from the same pushed commit. No previous chat history is required.
3. Use separate branches. In all repository documents, **You = controller owner**, and **Friend = worker/deployment teammate**, regardless of who opens the chat.
4. Read the shared contract before integrating. Start with one small slice, get it reviewed, merge it, then sync before the next dependent slice. Do not ask either agent to implement the whole handoff.

## Teammate's first prompt — worker/deployment role

Copy this into Codex with the repository open:

```text
I am the worker/deployment teammate, called Friend in TEAM-PLAN.md. The other person owns A1-A6 and the controller. Implement the first independent slice of my B1/B2 work, not the entire project.

Read AGENTS.md, README.md, PROJECT-SCOPE.md, TEAM-PLAN.md, docs/COURSE-TRACKER.md, docs/ASSIGNMENT-REQUIREMENTS.md, and docs/COURSE-CONTRACT.md. Follow the current course plan; the research handoff is background, and its full F1-F12 roadmap is not my task.

Inspect the checkout and preserve existing changes. Create codex/b1-b2-worker-foundation if a suitable task branch is not already active. Build the project packaging/dependency lock and the durable local chunk-store foundation in my owner paths. Include meaningful checks for binary round-trip, invalid length/hash, persistence, and interrupted writes. Expose worker routes where the shared models allow it. Use the agreed startup interfaces.

If A1 shared models or the controller are not merged, continue with independently testable storage code and packaging. Record the exact shared types/integration still needed; do not write the controller, duplicate src/common models, or mark Compose integration passed using a fake controller. A Compose definition may be prepared while its real startup gate remains pending.

Update my B1/B2 tracker entries with actual work, checks, remaining acceptance items, and blockers. Leave the other person's status unchanged. Work ready for human review is IN REVIEW, not DONE. Explain what the other person must merge next. Do not push, merge, create remote issues, or implement portfolio features in this task.
```

This starts real implementation without making the teammate wait for the whole controller. Full B1 acceptance still requires real service startup and persistence checks after dependencies are merged.

## Repository owner's first prompt — controller role

```text
I am the controller owner, called You in TEAM-PLAN.md. My teammate owns B1-B6, worker storage, packaging, and deployment. Implement A1 and the independently testable foundation of A2.

Read AGENTS.md, README.md, PROJECT-SCOPE.md, TEAM-PLAN.md, docs/COURSE-TRACKER.md, docs/ASSIGNMENT-REQUIREMENTS.md, and docs/COURSE-CONTRACT.md. Create codex/a1-controller-foundation if a suitable task branch is not already active. Preserve existing work.

Implement the shared wire/configuration models, controller entry point, error/health behavior, HTTPX lifecycle, and durable metadata/registry foundation in my owner paths. Match the contract and startup interfaces. Do not write the teammate's worker service, Docker files, or dependency lock. If packaging or workers are not merged, continue the independent foundation and record the exact dependency and checks not yet runnable.

Run meaningful available checks, update only the relevant A1/A2 tracker rows with evidence and remaining items, and summarize the shared models the teammate can now consume. Use IN REVIEW for unreviewed completed work; do not claim human sign-off. Do not push, merge, create remote issues, or implement portfolio features in this task.
```

## Continuing after a merge

Start from the updated shared branch and replace `<ROLE>` and `<CARD>` below with `Friend`/a B-card or `You`/an A-card:

```text
I am <ROLE> in TEAM-PLAN.md. Implement <CARD> using its task description and acceptance criteria. Read AGENTS.md and the current tracker/contract first. Check what is actually merged and preserve existing work. Work only within my scope; record missing cross-service dependencies while continuing useful independent work. Run relevant checks and record exact outcomes. Update my tracker row with remaining work and review status. Do not implement later portfolio features or push/merge remotely.
```

## What to exchange between teammates

Share the branch/PR and commit, card ID, contract changes (if any), checks actually run, and remaining dependency. Use the checked-in PR template. Once reviewed and merged, update the tracker with real links and evidence. Both people must run the integrated demos before the course release.
