# Distributed Object Store

This course project stores binary objects across four persistent workers behind one REST controller. It splits objects into 1 MB chunks, keeps two copies, and verifies SHA-256 on reads. C0–C2 are accepted, including the full public API and initial CLI. See the tracker for repair and submission verification.

Start the services and check readiness:

```bash
docker compose up -d --build --wait
docker compose run --rm client health
```

Use the [CLI guide](docs/CLIENT-CLI.md) for upload, download, metadata, and inspection commands. Use the [course walkthrough](docs/DEMO.md) for the six demonstrations.

| Document | Purpose |
|---|---|
| [Course and portfolio scope](PROJECT-SCOPE.md) | Chosen features, assignment coverage, later extensions, and viva preparation |
| [Team plan](TEAM-PLAN.md) | Ownership, file boundaries, task cards A1–A6/B1–B6, milestones, and GitHub workflow |
| [Progress tracker](docs/COURSE-TRACKER.md) | Actual task status, dependencies, evidence, demo sign-off, and decisions |
| [Course contract](docs/COURSE-CONTRACT.md) | Shared controller/worker messages and API behavior for parallel development |
| [Architecture](docs/ARCHITECTURE.md) | Storage lifecycle, health, repair, and durable deletion |
| [Controller foundation handoff](docs/CONTROLLER-FOUNDATION.md) | Available shared imports, configuration, 56 local checks, and remaining A2/B1–B3 dependencies |
| [Worker and deployment foundation](docs/WORKER-FOUNDATION.md) | Locked setup, Compose commands, registration, storage recovery, and C1 integration evidence |
| [Compiled assignment checklist](docs/ASSIGNMENT-REQUIREMENTS.md) | All original requirements in one readable place, with PDF pages, section names, and task owners |
| [Codex starting prompts](docs/CODEX-START.md) | Copy-and-paste prompts for each teammate and later task continuation |
| [GitHub workflow](docs/GITHUB-START.md) | Direct-main commit/push commands and teammate synchronization |

See the tracker for task status, review, and joint milestone gates. GitHub task and pull-request templates are included under `.github/`.

Repository-wide agent instructions live in [AGENTS.md](AGENTS.md). In every planning document, **You** means the controller owner and **Friend** means the worker/deployment teammate, regardless of who opens Codex.

The original [research handoff](CC-research-handoff/README.md) contains the assignment, design specifications, paper reviews, and proposed experiments. Its full research feature inventory is reference material, rather than the course submission checklist.
