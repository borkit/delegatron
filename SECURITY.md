# Security Policy

Delegatron is published by StoriaBrevi LLC as a read-only portfolio showcase and is provided
as-is, without warranty, under the [GNU Affero General Public License v3.0](LICENSE).

## Supported versions

None. No version of Delegatron receives security updates or fixes.

## Reporting a vulnerability

Vulnerability reports are not handled as a service: there is no response commitment, no
disclosure process and no bug bounty. Issues and pull requests are not accepted. If you run
Delegatron, you are responsible for reviewing and securing it yourself; see
[docs/security.md](docs/security.md) for how its security checks and sandboxes work.

## If you run it

Delegatron executes code written by AI models. Treat it as untrusted code.

- **Use a sandbox.** Run with the Docker (default) or E2B sandbox. The `local` sandbox runs
  generated code directly on your machine with no isolation.
- **Keep the API server private.** `delegatron serve` binds to `127.0.0.1` by default. With
  `DELEGATRON_AUTH_ENABLED` unset, every caller is an admin who can start runs, so turn auth on
  (with `DELEGATRON_STREAM_SECRET`) before exposing it on any network.
- **Limit what it can read.** Set `DELEGATRON_WORKSPACE_ROOT` so requests can only point at
  directories under it; with a cloud model, whatever it reads is sent to that provider.
- **GitHub webhooks.** Any commenter on a repository whose webhook points at your server can
  trigger a run with `/delegatron run`. Only connect repositories whose commenters you trust.
- **`serve` frees its ports on Windows.** On start it stops whatever process is listening on the
  API port and the next one, even if that process isn't Delegatron.
- **SWE-bench evaluation** runs model-written patches' tests directly on the host.
