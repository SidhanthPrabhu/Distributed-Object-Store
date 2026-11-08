# Repository instructions

## Scope and reading order

This repository is building a course version of a mini distributed object store before later portfolio extensions. Read `README.md`, `PROJECT-SCOPE.md`, `TEAM-PLAN.md`, `docs/COURSE-TRACKER.md`, `docs/COURSE-CONTRACT.md`, and `docs/ASSIGNMENT-REQUIREMENTS.md` before implementation. `docs/CODEX-START.md` contains role-specific starting prompts.

The original assignment is `CC-research-handoff/references/assignment.pdf`; its requirements are authoritative. The compiled checklist identifies requirements separately from our design choices. The current root scope/contract/team plan define our chosen implementation. The handoff's instruction to keep every F1–F12 feature and finish M0–M8 is historical research planning, not authorization to implement portfolio features during course work. Consult handoff specs for rationale, while preserving the simpler current course decisions.

## Stable role labels and ownership

In TEAM-PLAN and the tracker, **You** always means the repository owner's controller role (A1–A6). **Friend** always means the teammate's worker/deployment role (B1–B6), even when the teammate is the person talking to Codex. Use the role/task ID in the user's prompt; if neither is supplied, ask which card to implement before changing application files.

- Controller role owns `src/common/`, `src/controller/`, `tests/controller/`, and architecture documentation.
- Worker role owns `src/worker/`, `tests/worker/`, `src/client/`, `tests/integration/`, `scripts/demo/`, packaging/dependency lock, Docker/Compose/CI, and demo documentation.
- Preserve the other person's work. Do not independently rewrite their service or duplicate shared wire models. Contract changes must be explicit, documented, and coordinated; ordinary implementation within the assigned card can proceed.

## Working rules

- Implement the requested card or slice, with appropriate tests and accurate progress updates. Do not claim that the other human reviewed it. Unreviewed completed work belongs IN REVIEW, with any unfinished acceptance checks listed.
- Work directly on `main` and share changes through `origin/main`; task branches and pull requests are not required. Preserve unrelated changes. Commit local work before rebasing onto newer remote commits; resolve conflicts and rerun affected checks before pushing. Never force-push over teammate work. Agents must not commit, push, or create remote issues unless requested; a request for commands alone is not permission to execute them. Technical review and acceptance checks remain separate from permission to push.
- Record dependencies in `docs/COURSE-TRACKER.md`. When a missing teammate component blocks integration, continue independent work in your area and list the remaining integration gap. Test fakes are acceptable but cannot satisfy real-service milestone gates. Do not mark partial work DONE.
- No application packaging, tests, or commands existed when this planning package was prepared. Inspect the actual checkout; once commands exist, run the relevant checks and document exact results. Never present planned commands as tested.
- Retain 1 controller, 4 persistent workers, 1 MB ordered chunks (initially 1,000,000 bytes), 2 total copies on distinct workers, required APIs, checked reads, serial automatic repair, fixed-order local scans, and durable cleanup/restart behavior. Use the agreed contract rather than inventing incompatible messages.
- Do not add adaptive routing, speculative reads, advanced repair scheduling, batches, declaration/reuse engines, or a dashboard to course cards. Those belong to the separately recorded later scope.
- Never use worker inventories to redefine original expected checksums. Do not return corrupt bytes or use them as repair sources. Do not expose incomplete uploads or resurrect deleted data.
- Do not commit secrets, runtime databases, worker volume contents, or generated large fixtures. Do not delete persistent volumes during restart/failure demonstrations.
