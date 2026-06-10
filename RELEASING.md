# Cutting a release

The `VERSION` file and the git tag are the same number; the tag carries a `v` prefix. CI fails the release if they disagree.

1. **Bump the version.** Edit `VERSION` (e.g. `1.0.0-beta.3`). If the plugin changed, also bump `plugin/note-kit-ui/manifest.json` — the plugin versions independently.
2. **Commit and push:**

   ```
   git add VERSION
   git commit -m "Release 1.0.0-beta.3"
   git push
   ```

3. **Tag the release commit and push the tag** (annotated, `v` + the VERSION string):

   ```
   git tag -a v1.0.0-beta.3 -m "note-kit 1.0.0-beta.3"
   git push origin v1.0.0-beta.3
   ```

4. **Check CI** — the tag push runs the smoke test, the plugin build, and a guard that the tag matches `VERSION`. Green check = the release is good; point reviewers at the tag.

Rules of thumb:

- Tag only `main`, and only after CI is green on the commit.
- Never reuse or move a published tag; if a release is bad, cut the next number.
- Commits and tags should be authored as `JJ-9000 <JJ-9000@users.noreply.github.com>` — the repo-local `git config` already pins this; don't override it with `--author` or global config.
