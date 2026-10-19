# Releasing Satsuma

## One-time setup (repository owner)

1. Create the GitHub repository `Talessai/satsuma` (public) and push `main`.
2. Repository **Settings → Pages → Source: GitHub Actions**. The `Website` workflow
   then publishes `site/` to https://talessai.github.io/satsuma/ on every push that
   touches `site/`.

## Cutting a release

1. Bump the version in `satsuma/__init__.py` (`__version__`) and in
   `site/config.json` (`version`). `tests/test_release.py` fails if they differ.
2. Run the full build locally and make sure it passes:

   ```powershell
   powershell -ExecutionPolicy Bypass -File build.ps1
   ```

3. Commit, then tag and push the tag:

   ```powershell
   git tag v0.1.0
   git push origin main --tags
   ```

   The `Release` workflow builds on a clean Windows runner, runs the tests and the
   built-exe self-test, and attaches `Satsuma-Setup.exe` and `Satsuma-win64.zip` to a
   GitHub Release. The website's Download button always points at the latest release.

## Links shown in the app and on the site

`satsuma/about.py` (app, exe properties, installer) and `site/config.json` (website)
hold the author, repository, website and Ko-fi URLs. A test keeps them in sync.

## Publishing to winget

Satsuma is listed in the Windows Package Manager community repository as
`Talessai.Satsuma` (`winget install Talessai.Satsuma`). The manifest lives in
`packaging/winget/manifests/t/Talessai/Satsuma/<version>/` and must be updated for every
release, after the GitHub Release is published (the manifest points at the versioned
`releases/download/vX.Y.Z/Satsuma-Setup.exe` URL and its SHA256).

1. Install the tool once: `winget install Microsoft.WingetCreate`.
2. Generate the new version folder from the one published in winget-pkgs. This downloads
   the installer, computes the hash and bumps `PackageVersion`, `InstallerUrl`,
   `ReleaseDate` and `ReleaseNotesUrl`:

   ```powershell
   wingetcreate update Talessai.Satsuma --version X.Y.Z `
     --urls https://github.com/Talessai/satsuma/releases/download/vX.Y.Z/Satsuma-Setup.exe `
     --release-date YYYY-MM-DD `
     --release-notes-url https://github.com/Talessai/satsuma/releases/tag/vX.Y.Z `
     --out packaging/winget
   ```

   It writes `packaging/winget/manifests/t/Talessai/Satsuma/X.Y.Z/`. Update `ReleaseNotes`
   and the `v0.1.0` tag in `LicenseUrl`/`CopyrightUrl` by hand, then validate:
   `winget validate --manifest packaging/winget/manifests/t/Talessai/Satsuma/X.Y.Z`.
3. Commit the new folder, then submit it to winget-pkgs. Either add `--submit` to the
   `wingetcreate update` command above (it forks `microsoft/winget-pkgs` under your GitHub
   account and opens the pull request; it asks you to sign in to GitHub the first time),
   or copy the folder into a fork of winget-pkgs and open the PR by hand.
4. The winget-pkgs bots validate the manifest and test-install it; a moderator merges it,
   usually within a day or two. Nothing else is needed on our side.

To test a manifest locally before submitting, `winget install --manifest <folder>` needs
the admin-only setting `winget settings --enable LocalManifestFiles` (run once from an
elevated prompt). Without it, run `Satsuma-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`
and check that `winget list --name Satsuma` shows the new version.
