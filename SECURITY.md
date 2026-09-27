# Security Policy

Merced AI is pre-1.0 software. Do not rely on it as a security boundary around an otherwise
untrusted harness.

Please report vulnerabilities privately through GitHub's security advisory interface rather than a
public issue. Include the affected version, operating system, harness and version, reproduction
steps, and whether credentials or workspace files may have been exposed.

Merced AI will never intentionally broaden a harness's permissions. A profile request is advisory
and the underlying harness remains the final policy authority.

## Threat model

[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) covers the surfaces added in 0.8.0 (the ACP client and
server, the A2A endpoint, worktree rooms, the OAP delta inbox, harness plugins, the eval judge,
private prompt files, and the run journal): assets, STRIDE threats, mitigations with file
references, residual risk, and the tests that cover each. It is a self-review by the authors, not
an independent audit. The bugs it found are fixed and listed there with their regression tests.
