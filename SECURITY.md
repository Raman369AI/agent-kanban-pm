# Security Policy

## Supported versions

Security fixes are provided for the latest published release. Pre-release
builds are supported on a best-effort basis and may receive breaking fixes.

## Supported deployment model

Agent Kanban PM is a local, single-operator tool. Run one server process on a
trusted Linux or macOS machine (or Windows through WSL), bind it to loopback,
and use the generated instance token. Do not expose the server to an untrusted
network or share an instance with users you would not trust with local shell
access.

Per-task Git worktrees isolate branches and working copies. They do not sandbox
agent processes from the host filesystem, credentials, network, or commands.
Supervised autonomy is the safe default. Enabling automatic approval grants the
selected CLI agent the permissions provided by that CLI and the current OS
account.

Remote multi-user hosting, untrusted local OS users, native Windows execution,
multiple server replicas, and hostile-agent containment are outside the current
security boundary.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability reporting for this repository:

https://github.com/Raman369AI/agent-kanban-pm/security/advisories/new

Include the affected version, deployment conditions, reproduction steps,
impact, and any suggested mitigation. If private reporting is unavailable,
open a minimal issue asking the maintainer for a private contact channel
without publishing exploit details.
