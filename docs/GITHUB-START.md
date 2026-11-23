# Sharing work through main

Both teammates work directly on `main` and push to `origin/main`. Task branches, pull requests, and PR approval are not required. This supersedes the original branch-and-PR workflow. Ownership boundaries, contract coordination, acceptance checks, and accurate review status still apply; publishing a commit does not establish C0 or mark a card DONE.

The configured repository is `SidhanthPrabhu/Distributed-Object-Store`, remote `origin`. Remote authentication, permissions, branch protection, and the latest remote commits have not been verified here. If GitHub rejects a direct push because of branch protection, the repository owner needs to adjust the applicable rule for this workflow.

## Publish the controller foundation

The controller foundation was integrated into `main` during A1/A2. The historical publication commands below show the staged scope. If Git refuses to switch branches because of local changes, inspect them before continuing.

Run from the repository root:

```bash
git switch main
git add .gitignore README.md PROJECT-SCOPE.md TEAM-PLAN.md docs src/common src/controller tests/controller
git diff --cached --check
git diff --cached --stat
git commit -m "A1/A2: add controller foundation and direct-main workflow"
git pull --rebase origin main
git push -u origin main
```

Review the staged list before committing. The included paths contain controller/common code, controller tests, and documentation; packaging and workers remain Friend's responsibility. Keep secrets, local environments, runtime databases, worker volumes, and generated data outside commits.

If rebase reports conflicts, resolve them, stage the resolved files, and run `git rebase --continue`. Rerun checks affected by the combined changes before pushing. If a teammate pushes again first and your push is rejected, synchronize again; never force-push over their work.

## Ongoing work for either teammate

Before starting new work, with a clean checkout:

```bash
git switch main
git pull --ff-only origin main
```

After implementing a card, run its checks, update the tracker, stage the intended files, and commit. Before pushing use `git pull --rebase origin main`; if synchronization changes the combined code, rerun affected checks, then `git push origin main`. A teammate with no local commits receives the published work using `git pull --ff-only origin main`.

Share the commit ID, card ID, behavior, actual check results, shared interfaces, and outstanding dependencies directly. Technical review can happen in conversation or by inspecting commits; no pull request is necessary. Do not claim the other person reviewed something unless they did. Both people must still run the required integrated demonstrations before the course release.

See [controller foundation evidence](CONTROLLER-FOUNDATION.md) and [the tracker](COURSE-TRACKER.md) for completed local checks and open integration gates. The foundation has 56 passing controller-local tests; worker startup, Compose, and real-service durability have not been established. Remote commit/push commands above are instructions for the user, not actions executed by this preparation.
