# Documentation

| Document | For whom |
|---|---|
| **[RUNBOOK.md](RUNBOOK.md)** | Analysts running a study. Plain language, no Python assumed. |
| **[SECURITY.md](SECURITY.md)** | Security review boards. Scan results, native binaries, network behaviour. |
| **[SAS_PARITY.md](SAS_PARITY.md)** | Audit against the SAS macros: two confirmed divergences, several stages verified. |
| **[INCLUSION.md](INCLUSION.md)** | Inclusion/exclusion criteria: semantics implemented and what is not. |
| **[LOGGING.md](LOGGING.md)** | What the run logs contain and how to compare runs. |
| **[PERFORMANCE_AND_TERMINALS.md](PERFORMANCE_AND_TERMINALS.md)** | Memory floors, spilling, scaling to 200-300GB, terminal compatibility. |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | Developers. Threading model, data flow, module map. |
| **[UI.md](UI.md)** | UI architecture and the framework comparison. |
| **[DEPLOYMENT.md](DEPLOYMENT.md)** | Getting it running at a Data Partner site: single executable, Docker, or pip. |
| **[OUTPUTS.md](OUTPUTS.md)** | Every output table and column, and how each maps to SAS's. |
| **[PARITY_RUN.md](PARITY_RUN.md)** | How to run a comparison against SAS, and what to send. |
| **[PARITY_FINDINGS.md](PARITY_FINDINGS.md)** | The record of every SAS comparison made and every discrepancy fixed. |
| **[PERFORMANCE.md](PERFORMANCE.md)** | Performance measurements and the changes they drove. |
| **[screenshots/](screenshots/)** | Real renders of the terminal UI: controls, a healthy run, spilling, and the OOM message. |

Screenshots are `export_screenshot()` output — the app's real terminal
buffer, not mockups. Trimmed to four; near-duplicate states were dropped
because each SVG is ~90 KB (Textual emits one `<text>` element per
character) and they were over half the archive.

Start with the RUNBOOK if you just want to run a query.
