# CLAUDE.md — Project Rules

## Versioning

Version is in `rtsp_switcher/config.yaml` (`version: "x.y.z"`).

| Change type | Bump |
|---|---|
| Bug fix | patch (`0.0.x`) |
| Substantial feature change/rework | minor (`0.x.0`) |
| New feature added | major (`x.0.0`) |

### Every version bump requires all three:

1. **Update `version` in `rtsp_switcher/config.yaml`**
2. **Prepend a `## x.y.z` entry to `rtsp_switcher/CHANGELOG.md`** — reverse chronological, plain bullet points describing changes
3. **Add a git tag**: `git tag vx.y.z && git push origin vx.y.z` (or just `git tag vx.y.z` if no remote)

The HA Supervisor reads `CHANGELOG.md` (alongside `config.yaml`) when showing addon update notes in the UI. Format must be `## x.y.z` headers with bullet points under each.
