# Manual ideas-mode run prompt

Paste the prompt below into a coding agent that is already operating in a checkout of one ideas-tier repository. It performs one run only. Replace nothing in the prompt; the repository's own files provide the project-specific instructions.

```text
Perform exactly one manual ideas-mode run in this repository.

The ownership boundary is absolute:

- `idea/SPEC.md` belongs to the human. Never edit, format, normalize, rename, stage, or commit it. Read it as raw bytes and record its SHA-256 before doing anything else.
- `idea/PROGRESS.md` and `idea/assets/` belong to the coding agent. Regenerate/update them as described below.
- Do not create issues or a pull request. Do not add an Obsidian plugin, access a vault, implement a coordinator/webhook/preview manager, or perform graduation work.
- Do not force-push, reset, discard unrelated work, or stage with `git add --all`, `git add .`, or an equivalent broad command.

Preflight

1. Read all repository instructions (including `AGENTS.md` files that apply), `idea/SPEC.md`, the previous `idea/PROGRESS.md` if present, `.symphony/idea.toml`, and the relevant application code.
2. Validate the spec frontmatter strictly before changing code. It must be the first block in the file, delimited by exact `---` lines, and contain exactly one `symphony: idea` and exactly one `repo: owner/name`. Reject aliases, duplicate keys, extra path components, malformed UTF-8, or any other `symphony` value.
3. Validate `.symphony/idea.toml`. It must name a provider and contain `[preview]` with `start` as a non-empty argument array (not a shell string), an integer port, an absolute `health_path` beginning with `/`, and a positive `startup_timeout_seconds`. Do not guess a missing or untruthful command.
4. Check `git status`. Preserve all pre-existing changes. If unrelated edits would make implementation or an exact commit unsafe, stop and ask the human rather than moving, stashing, or overwriting them.
5. Find the last completed ideas run with `git log -1 --format=%H -- idea/PROGRESS.md`. Diff the `idea/SPEC.md` stored at that commit against the current spec (including working-tree content). If there is no prior progress commit, treat every `##` section as new. The changed and newly added `##` sections are this run's wishes. A prose change beneath a header makes that whole header affected; a rename makes the old section removed and the new section affected.

Implement

6. Implement only the changed/new wishes and the smallest supporting work they require. Use the repository's established architecture and tooling. Do not invent work for unchanged sections.
7. Run the repository's existing tests and quality gate. If `.openhands/quality-gate.sh` exists, run it non-interactively. This check is advisory in ideas mode, so record a genuine partial result when a non-preview check fails; never claim it passed.
8. Re-read `idea/SPEC.md` as bytes and compare its SHA-256 with the preflight value. If it differs for any reason, stop. Do not restore or commit it automatically; report that the human-owned file changed during the run.

Preview and screenshots — mandatory publication gate

9. Launch the preview using the exact `preview.start` argument array, without shell interpretation. Use the declared port and only loopback access. Poll `http://127.0.0.1:<port><health_path>` until it succeeds or `startup_timeout_seconds` expires. Capture logs without putting secrets in the repository.
10. If the process exits early, health never succeeds, or the contract is not truthful, do not commit this run. Report the command, exit/health evidence, and the focused correction needed. Never substitute a guessed start command.
11. Once healthy, exercise each affected wish in a real browser at `http://127.0.0.1:<port>/`. Capture one PNG per affected `##` section, at no more than about 1280 px wide. Store it at a stable, descriptive slug such as `idea/assets/suggest-dinner.png`, overwriting that feature's previous image. Do not accumulate timestamped screenshots. Use relative links from `PROGRESS.md`.

Regenerate the mirror

12. Regenerate all of `idea/PROGRESS.md` from the current raw bytes of `idea/SPEC.md`; do not hand-merge it. Preserve every spec-owned byte exactly, including frontmatter, whitespace, line endings, punctuation, and final newline. Directly below every `##` header, insert one generated block in this exact shape:

   > **done**
   >
   > One or two terse, factual sentences describing the observed result.
   >
   > ![Short description](assets/stable-feature-name.png)

   The status must be exactly `done`, `partial`, `not started`, or `question`. Use `question` with one focused question when the wish is ambiguous or blocked. For an unchanged section, carry forward its prior result and screenshot link when still accurate. For a removed section, remove its generated result and obsolete screenshot. Every current `##` section must have exactly one result block.
13. Construct the mirror by inserting bytes into the spec, not by parsing and reserializing Markdown or YAML. Verify programmatically that deleting only the generated blocks from `PROGRESS.md` reconstructs the original `SPEC.md` byte-for-byte. Recheck the spec SHA-256. If either check fails, stop before staging.

Commit and handoff

14. Review the diff for truthful statuses, readable screenshots, secrets, generated junk, and accidental scope. Ensure the preview is still healthy.
15. Stage only explicit paths that belong to this run: implementation files one by one, `idea/PROGRESS.md`, and the exact added/changed/deleted paths under `idea/assets/`. Never stage `idea/SPEC.md`, unrelated work, preview logs, caches, or broad directory globs. Confirm with `git diff --cached --name-status` and `git diff --cached -- idea/SPEC.md` (which must be empty).
16. Commit the staged run once with `ideas: implement spec <first-12-hex-of-spec-sha256>`. Do not amend an unrelated commit. Do not push unless the human separately asked this agent to push.
17. Leave the preview running when the execution environment supports durable child processes, and report its loopback URL and process/log location. Otherwise stop it cleanly and report the exact argument array the human can run to restore the preview.

Finish with a concise report containing: affected `##` sections, implementation summary, test/quality-gate evidence, preview health evidence, screenshot paths, the unchanged spec SHA-256, commit hash, whether the preview remains running, and any `partial` or `question` items. Stop after this one run.
```
