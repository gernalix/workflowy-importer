# workflowy-importer

Read project-capsule.yaml, README.md and AUTOMATIONS.md. Keep Markdown/model/link
conversion, API/credential access, cache/event persistence, CLI argument parsing
and ChatGPT record/storage modules separate. Reuse existing clients and storage
writers; never replay an ambiguous remote mutation or guess an internal link.

Fedora owns the runtime. Use global Python, isolated task worktrees and the
installed Git writer. Offline tests use PYTHONPATH=src and fake clients. Do not
run live smoke, collectors, import/replace, browser capture, semantic enrichment
or service installation as verification. Historical C2/C3 routes stay retired.

Configured FEDORA_WORKFLOWY_ENV uses Bitwarden exclusively and fails closed;
never substitute a legacy key after vault failure. Preserve cache, source
archives, remote nodes, import journals and PersonalHub data. Never log keys,
cookies, private chat bodies or browser profile data. A source merge does not
prove deployment or a successful remote import.
