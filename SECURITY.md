# Security

Report vulnerabilities privately through the repository's GitHub Security Advisories page. Please include the affected version, steps to reproduce, and the expected impact. Do not open a public issue for an unpatched vulnerability.

## Threat model

Bots can execute untrusted actions inside containers. A malicious prompt, tool result, or file may try to change a bot's behavior or reach data available to that container. Operators should limit each bot's permissions and review actions that cross a trust boundary.

An instance is intended for its administrator and invited users. It has no open registration model. Authentication, deployment configuration, and host isolation remain part of the operator's security boundary.
