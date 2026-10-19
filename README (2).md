# Pentest Command Center

A single-page dashboard for running scoped, authorized penetration test engagements — tracking what's in scope, logging tool runs against it, turning raw tool output into findings, and generating a client-facing report.

**Core rule the whole tool is built around: nothing gets logged against a target that isn't in an active engagement's authorized scope.** There's no "scan anything" button. You run real tools yourself, on your own machine, against your own authorized targets — this dashboard organizes and reports on the results.

## What it does

### Engagements
Each engagement is a record for one client (or your own business, for self-audits):
- **Engagement kind** — Self-audit (your own assets, no third-party authorization needed) or Third-party pentest (a client engagement)
- **Scope** — authorized IP ranges/CIDRs and domains, one per line
- **Authorization** (third-party only) — SOW/contract reference, authorized signer name & title, and the actual signed-document text or a description of where the signed document is filed, plus an attestation checkbox confirming a real signed document exists
- **Window** — start/end dates, used to flag an engagement as scheduled / active / expired

### Toolkit
A methodology/category reference mirroring how Kali, Parrot, and BlackArch organize their tool menus (Recon, Vulnerability Analysis, Web App, Database, Password Attacks, Wireless, Reverse Engineering, Exploitation, Sniffing/Spoofing, Post-Exploitation, plus the blue-team side: Forensics, Detection & Monitoring) and the standard methodology frameworks (PTES, OWASP Testing Guide, NIST SP 800-115, OSSTMM).

**Log a Tool Run** — paste the exact command you ran and its raw output. The page:
- Extracts anything that looks like an IP/CIDR/hostname from the command+output and flags anything outside the active engagement's authorized scope before you can save it
- Parses common output shapes (nmap open-port lines, Nuclei JSON results, CVE mentions, generic "VULNERABLE"/"CRITICAL" markers) into suggested findings you review and accept one at a time

**Run Log** — every logged run, tied to engagement + category, for the audit trail.

### Findings
Severity-tagged (Critical/High/Medium/Low) findings tied to a specific engagement, with detail notes. Can be added manually or accepted from the Toolkit's output parser.

### Report
Pick an engagement, generate a plain-text report: scope, authorization block (for third-party engagements), engagement window, and findings sorted by severity. This is the client deliverable.

## Running it

It's a single self-contained HTML file (`command_center.html`) — open it in any modern browser. No server, no build step.

Data (engagements, findings, run log) is saved in your browser's local storage, per device/browser — it doesn't sync across devices and isn't sent anywhere.

## What this tool deliberately does NOT do

- It does not scan, probe, or connect to anything itself. Running actual tools (nmap, Nuclei, Burp, hashcat, etc.) happens on your own machine; this dashboard is where you record and organize the results.
- It does not verify authorization claims beyond what you enter — the attestation and signed-document fields create a record, not independent verification. Treat them as part of your own documentation discipline, not a substitute for actually having a signed SOW on file.
- It has no built-in exploit, cracking, or interception capability. The scope check on the Toolkit tab is a sanity check against typos/accidents, not a security boundary — it's pattern-matching on IPs/hostnames in text, not a technical enforcement mechanism.

## Files

- `command_center.html` — the application
- `README.md` — this file
