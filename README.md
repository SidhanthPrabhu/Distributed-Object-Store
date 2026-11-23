# Distributed Object Store

This course project stores binary objects across four persistent workers behind one REST controller. It splits objects into 1 MB chunks, keeps two copies, and verifies SHA-256 on reads. C0–C4 are complete. The tested code is tagged `course-v1` at commit `4eb9979`. The [tracker](docs/COURSE-TRACKER.md#joint-milestone-gates) records both teammates' independent demo runs and the final sign-off.

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
| [Controller foundation handoff](docs/CONTROLLER-FOUNDATION.md) | Early shared imports, configuration, and controller checks |
| [Worker and deployment foundation](docs/WORKER-FOUNDATION.md) | Locked setup, Compose commands, registration, storage recovery, and C1 integration evidence |
| [Compiled assignment checklist](docs/ASSIGNMENT-REQUIREMENTS.md) | All original requirements in one readable place, with PDF pages, section names, and task owners |
| [GitHub workflow](docs/GITHUB-START.md) | Direct-main commit/push commands and teammate synchronization |

See the tracker for task status, review, and joint milestone gates. GitHub task and pull-request templates are included under `.github/`.

In the planning documents, **You** means the controller owner and **Friend** means the worker/deployment teammate.

The original [research handoff](CC-research-handoff/README.md) contains the assignment, design specifications, paper reviews, and proposed experiments. Its full research feature inventory is reference material, rather than the course submission checklist.
