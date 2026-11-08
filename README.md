# Distributed Object Store

Planning stage: the object-store application has not been implemented yet.

Start with the [two-person implementation plan](TEAM-PLAN.md): the repository owner implements the controller, metadata, client-facing APIs, and repair coordination; their teammate implements worker storage, heartbeats, integrity scanning, deployment, and demo tools. Both review and verify the integrated system.

| Document | Purpose |
|---|---|
| [Course and portfolio scope](PROJECT-SCOPE.md) | Chosen features, assignment coverage, later extensions, and viva preparation |
| [Team plan](TEAM-PLAN.md) | Ownership, file boundaries, task cards A1–A6/B1–B6, milestones, and GitHub workflow |
| [Progress tracker](docs/COURSE-TRACKER.md) | Actual task status, dependencies, evidence, demo sign-off, and decisions |
| [Course contract](docs/COURSE-CONTRACT.md) | Shared controller/worker messages and API behavior for parallel development |
| [Compiled assignment checklist](docs/ASSIGNMENT-REQUIREMENTS.md) | All original requirements in one readable place, with PDF pages, section names, and task owners |
| [Codex starting prompts](docs/CODEX-START.md) | Copy-and-paste prompts for each teammate and later task continuation |
| [First GitHub push](docs/GITHUB-START.md) | What to commit, push commands, and teammate setup |

All implementation cards are currently TODO. Start in parallel with A1 (shared models/controller skeleton) and B1/B2 (packaging/worker storage), integrating against the reviewed contract. No application build/test/demo commands are available yet. GitHub task and pull-request templates are included under `.github/`.

Repository-wide agent instructions live in [AGENTS.md](AGENTS.md). In every planning document, **You** means the controller owner and **Friend** means the worker/deployment teammate, regardless of who opens Codex.

The original [research handoff](CC-research-handoff/README.md) contains the assignment, design specifications, paper reviews, and proposed experiments. Its full research feature inventory is reference material, rather than the course submission checklist.
