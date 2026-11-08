# Publishing this planning package to GitHub

This package is ready for an initial **planning/documentation commit**. It does not contain an implemented application. Before pushing, commit the new files: a push transfers commits, not uncommitted working-folder files.

The configured repository is `SidhanthPrabhu/Distributed-Object-Store`, remote `origin`, branch `main`. Local preparation does not verify your GitHub authentication, collaborator permissions, or the remote branch's current state.

## Initial publication by the repository owner

Run from the repository root after reviewing `git status`:

```bash
git status --short
git add README.md AGENTS.md PROJECT-SCOPE.md TEAM-PLAN.md .gitignore .github docs CC-research-handoff Mini_Distributed_Cloud_Object_Store.pdf
git diff --cached --stat
git diff --cached --check
git commit -m "Document course scope and two-person implementation plan"
git push -u origin main
```

This explicit staging list includes the assignment PDF, complete handoff, scope, ownership, contract, tracker, starter prompts, and GitHub templates. Review the staged files before committing. The two assignment PDF paths currently contain identical bytes; documentation uses the handoff copy. Neither copy needs Git LFS.

If push reports that the remote has newer commits, fetch and inspect the difference, then integrate normally; do not force-push over your teammate's work. If `main` requires a PR, push the planning commit on a `codex/` branch and open a PR instead.

## Teammate setup

The teammate needs repository access, then a clone or updated checkout containing this commit. Open that folder in Codex and use the worker-role prompt in [CODEX-START.md](CODEX-START.md). Opening a fresh chat is sufficient because the project context is checked in.

Start with B1/B2 in parallel with the owner's A1/A2. Use the included issue template for actual task issues if desired; no issues or PRs have been created automatically. Keep live storage, databases, secrets, and generated fixtures outside commits using the documented ignored paths.

## What has and has not been checked

- Local Markdown links, section anchors, task-card/tracker consistency, JSON examples, templates, Git whitespace, file sizes, and likely accidental secrets are checked during preparation.
- The original assignment is compared with the compiled checklist; the scope cites PDF pages and named sections.
- Application tests, service startup, Docker demonstrations, remote authentication, and push success cannot be claimed for a planning-only checkout.
