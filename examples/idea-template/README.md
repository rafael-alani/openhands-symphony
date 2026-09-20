# Ideas-tier repository contract

Copy these files into a private, disposable idea repository. Replace `repo:` in `idea/SPEC.md` with that repository's exact `owner/name`, then keep the same frontmatter when regenerating `idea/PROGRESS.md`.

`idea/SPEC.md` is the wishlist. It may contain free-form prose under `##` feature headers, but its frontmatter must contain exactly one `symphony: idea` value and one valid `repo: owner/name` route. The human is its only writer.

`idea/PROGRESS.md` is generated after an ideas-mode run. It mirrors every byte owned by `SPEC.md` and inserts a short block directly below each `##` header: one of `done`, `partial`, `not started`, or `question`; one or two sentences; and a relative screenshot link. The coding agent is its only writer. Screenshots belong in `idea/assets/`; overwrite the stable image for a feature instead of accumulating versions.

`.symphony/idea.toml` declares the coding provider and a truthful preview command. `preview.start` is an argument array, never a shell command. Use the exact `{port}` argument placeholder or read the `PORT` environment variable so the manager can health-check a candidate without interrupting the last-good process. The app must bind to `HOST` (always loopback), use the effective port, and answer at `health_path` within `startup_timeout_seconds`. The declared port becomes immutable after the first healthy release.

The single-writer rule is the safety boundary: `SPEC.md` only travels from the vault to the repository, while `PROGRESS.md` and `assets/` only travel from the repository to the vault. Never hand-edit the generated mirror, never let an agent edit the spec, and never resolve a disagreement by silently choosing one side.
