# media-tools

## Packaging CI (template)

`.github/workflows/build-matrix.yml` is a not-yet-wired template that builds
a Tkinter/PyInstaller GUI script into Windows/macOS/Linux binaries via a
matrix build. This repo has no GUI entrypoint today, so the workflow only
runs manually and fails fast with a config-needed message. Fill in
`ENTRY_SCRIPT`/`APP_NAME` in the workflow once there's a script to package.
The working reference implementation lives in `rancerstank/network-tools`
(`.github/workflows/build-matrix.yml`), built for its FortiGate debug-flow
GUI tool.