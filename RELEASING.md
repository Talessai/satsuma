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
