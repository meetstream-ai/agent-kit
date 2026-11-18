# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: open the
**Security** tab of this repository and choose **Report a vulnerability**.
Do not open a public issue.

We acknowledge reports within 3 working days and keep you updated until the
problem is fixed. Please give us a reasonable time to release a fix before
disclosing it.

## Supported versions

Security fixes go into the latest release. Upgrade with
`uv tool upgrade meetstream-agent-kit` (or `pipx upgrade meetstream-agent-kit`).

## Scope

This package runs on the user's own machine and drives the user's own agent
CLI. In scope:

- authentication between MIA and the bridge (pairing tokens);
- the per-meeting MCP secret;
- access-level enforcement (`--max-access`, `readonly`, `confirm`, `full`);
- the activation code;
- handling of untrusted meeting text.

Out of scope: vulnerabilities in the agent CLIs themselves, and the tunnel
software the user chooses.
