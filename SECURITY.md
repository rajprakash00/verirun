# Security Policy

## Supported versions

The latest `0.1.x` release receives security fixes. Verirun is a prototype:
it runs against a local Mock Suite, and the dashboard binds to localhost with no
authentication.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub Security Advisories:

<https://github.com/rajprakash00/verirun/security/advisories/new>

Do not open a public issue for a security report. Include:

- what the issue is and the impact you believe it has;
- steps to reproduce, with a failing test if you have one;
- the version or commit, and whether Chromium and a live model were involved.

We aim to acknowledge a report within a few days and to agree on a disclosure
timeline with you. There is no bug bounty.

## Scope

The most sensitive code paths are:

- the policy gate and approval gates, which must not let an irreversible action
  submit without a human yes;
- the Verifier, which must read ground truth rather than the executor's claims;
- the file tools, which must keep reads and writes inside the shared document
  tree.

Reports that the Mock Suite can be edited or that replay fixtures can be
rewritten are not vulnerabilities: both are test infrastructure. Real
credentials and real third-party systems are out of scope because Verirun never
touches them.
