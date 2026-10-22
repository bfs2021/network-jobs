# Releasing `@hirefrank/network-jobs`

## Checklist (current as of 1.7.0)

1. Bump `version` in `package.json` (use the edit tool — never `npm version`,
   which auto-commits and auto-tags).
2. Add a `CHANGELOG.md` entry.
3. Run `bash tests/run.sh` (must exit 0) and
   `bash -n bin/network-jobs setup lib/agents.sh`. CI repeats both on the tag,
   but catching it locally avoids a failed release.
4. Commit `main`.
5. Tag and push (tags are lightweight, so `--follow-tags` would skip them):

   ```bash
   git tag vX.Y.Z
   git push origin main
   git push origin vX.Y.Z
   ```

   The tag triggers [`.github/workflows/publish.yml`](../.github/workflows/publish.yml),
   which re-runs the tests, checks the CLI syntax, then publishes.
6. Watch the run, then confirm:

   ```bash
   npm view @hirefrank/network-jobs/latest version
   ```

   The full packument can 404 for a few minutes after a publish while the
   registry replicates; `/latest` and the website converge first.

The workflow refuses to publish when the tag doesn't match `package.json`, so a
mistyped tag fails before anything ships.

## Trusted publishing (default since 1.7.0)

The workflow authenticates with GitHub's OIDC token instead of a long-lived
secret: no `NPM_TOKEN`, no passkey step-up per release, and provenance is
attached automatically. Node 24 is pinned because trusted publishing requires
npm >= 11.5.1.

**One-time setup** (maintainer's browser, once per package):

1. npmjs.com → Packages → `@hirefrank/network-jobs` → Settings → **Trusted
   Publisher** → GitHub Actions.
2. Organization/user `hirefrank`, repository `network-jobs`, workflow filename
   `publish.yml`, environment **blank**.
3. If you ever add a GitHub environment (e.g. `npm` with required reviewers),
   the workflow must declare the same `environment:` or npm rejects the publish.

Until that entry exists the workflow fails at the publish step and nothing
ships — the manual path below still works. To roll back, delete the trusted
publisher entry; CI publishes then fail closed.

> **Open follow-up (2026-10-22):** after the first green CI release, switch
> **Publishing access** on npmjs.com to *“Require two-factor authentication and
> disallow bypass 2fa tokens (recommended)”*. Trusted publishing is unaffected;
> this closes the legacy bypass-token path before the Jan 2027 deprecation.
> Deliberately not done yet, so the manual fallback still works while CI is
> being proven.

## Manual publish (fallback)

Use when Actions is unavailable or trusted publishing has been removed.

1. Verify the tarball before publishing:

   ```bash
   npm pack --pack-destination /tmp/nj-pack
   tar -tzf /tmp/nj-pack/*.tgz
   ```

   Expect `bin/` intact, no `__pycache__/` (the `prepack` script deletes it), a
   sane file count, and a single `scripts` key in `package/package.json`.
2. Publish from a real terminal (passkey step-up needs a TTY):

   ```bash
   npm publish            # or: npm stage publish, then approve in the browser
   ```
3. `npm view @hirefrank/network-jobs/latest version`

## Auth notes (manual path, learned 2026-09-29)

- The maintainer account uses passkey 2FA with no TOTP. Headless/CI shells
  **cannot** complete publish-time step-up (`EOTP` + browser URL fails fast
  without a TTY).
- First publish of a **new** package cannot use a stage-only token (the stage
  endpoint 404s until the package exists) — birth the package from a real
  terminal.
- Keep a TOTP authenticator registered as a fallback (`npm publish --otp=…`
  always works).
- The bypass-2FA token deprecation (Jan 2027) is why CI publishing is now the
  default path; trusted publishing is not affected by it.
