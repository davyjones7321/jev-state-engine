# Important project notes

- The Laya wrapper cleanup remains a repository-maintenance task; do not treat the Laya comparison runner as part of the default production CLI.
- Live validation has primarily covered JavaScript/TypeScript scenarios. Python, Rust, Go, JVM, and infrastructure runners are implemented or unit-tested in parts, but broad end-to-end agent coverage across those ecosystems is not verified here.
- Git worktrees are isolation for repository changes, not an OS sandbox. Use disposable targets and never run the engine against production or untrusted repositories.
