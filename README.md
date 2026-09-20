# workflowy-importer

A small, safety-first toolkit around the official Workflowy API.

It started as a Markdown importer and now also provides a local automation layer (`wf`) for backup/cache, quick capture, mirrors, routing, ChatGPT capture, external-event ingestion, PersonalHub deep links, reviews and other workflows.

See [AUTOMATIONS.md](AUTOMATIONS.md) for the map of the 30 automation ideas.

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/gernalix/workflowy-importer.git
cd workflowy-importer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

## API key

Credential access is provider-based rather than hard-wired to one plaintext file. In automatic mode the resolver uses, in order:

1. a systemd credential named `workflowy-api-key` from `$CREDENTIALS_DIRECTORY`;
2. the desktop Secret Service/libsecret entry identified by `application=workflowy-importer`, `credential=workflowy-api-key`;
3. an already-set ephemeral `WORKFLOWY_API_KEY` environment variable;
4. the legacy protected file `~/.config/codex/secrets/workflowy-api-key`.

The legacy file is kept only for migration/backward compatibility. New interactive Fedora setups should use Secret Service; systemd services should use `LoadCredential=` or `LoadCredentialEncrypted=`. The program never prints or persists the key.

To store the current key in Secret Service without putting it on a command line, run:

```bash
secret-tool store --label='Workflowy API key' application workflowy-importer credential workflowy-api-key
```

Enter the value on stdin when prompted. Verify the new provider before deleting any legacy file.

Use `--credential-provider` (or `WORKFLOWY_CREDENTIAL_PROVIDER`) to force one provider and fail closed instead of falling back.

## Markdown importer

Dry-run:

```bash
workflowy-import-md ~/Documents/my-vault --dry-run
```

Import:

```bash
workflowy-import-md ~/Documents/my-vault
```

Import below Inbox:

```bash
workflowy-import-md ~/Documents/my-vault --parent inbox
```

Import below a Workflowy node URL or custom shortcut:

```bash
workflowy-import-md ~/Documents/my-vault \
  --parent 'https://workflowy.com/#/xxxxxxxxxxxx' \
  --root-name 'Imported knowledge'
```

### Import safety

- Dry-run needs no key and performs no writes.
- One tracked root is created per import.
- Re-running unchanged input is a no-op.
- If source content changed, a normal rerun stops rather than duplicating data.
- `--replace` builds the new tree completely before deleting the previously tracked root.
- Interrupted initial/replacement imports are journaled and reconciled on the next run.
- Read-only API calls may retry transient errors; mutations are not blindly replayed after ambiguous network failures.
- Ambiguous internal links are never guessed.

The importer intentionally uses transactional whole-root replacement rather than risky in-place mutation. Machine-side incremental behavior is provided by the cache/event bridge; Markdown source refresh remains explicit with `--replace`.

### Markdown mapping

| Markdown/source | Workflowy result |
| --- | --- |
| directory | parent bullet |
| `file.md` | file-name bullet |
| `# H1` | H1 node |
| `## H2` | H2 node |
| deeper headings | H3 node while preserving hierarchy |
| `- item` | bullet |
| numbered item | bullet preserving numeric marker |
| `- [ ]` / `- [x]` | open/completed todo |
| `> quote` | quote block |
| fenced code | `Code` parent plus code-block children |
| bold/italic/strike/inline code | Workflowy-supported inline HTML |
| external Markdown link | hyperlink |
| `[[Page]]`, `[[Page#Heading]]`, `[[Page|label]]` | Workflowy node hyperlink when uniquely resolvable |
| `[label](page.md#Heading)` | Workflowy node hyperlink when uniquely resolvable |
| YAML front matter | preserved below a `Front matter` node |

Images/attachments are not uploaded: the public Workflowy API currently exposes node operations, not a binary-upload endpoint. Markdown image syntax is preserved as text.

## `wf`: the simple local interface

Think of `wf` as a remote control for Workflowy. A human, a shell script, Codex or another local service can all use the same commands instead of each reimplementing API access.

Examples:

```bash
# Put one thought in Inbox
wf inbox "idea da sistemare"

# Put a task directly in Today
wf today "Controllare PersonalHub"

# Create below a shortcut/node
wf add "Bug: Places riapre Home" --parent ph

# Search the machine cache
wf sync
wf find "PersonalHub"

# Generate/print the deep link for a node
wf url NODE_ID

# Complete, move or mirror a node
wf done NODE_ID
wf move NODE_ID today
wf mirror NODE_ID today

# Full backup
wf backup ~/Documents/Workflowy/backups/manual.json
wf backup ~/Documents/Workflowy/backups/manual.md --format md
```

The native Workflowy search remains the right tool for normal human searching. The SQLite cache exists for programs: deduplication, history/diffs, joins with PersonalHub, offline processing and avoiding repeated full API reads.

## Automatic classification

Copy `rules.example.json` to:

```text
~/.config/workflowy-bridge/rules.json
```

Then:

```bash
wf route "Bug PersonalHub: Places riapre Home"
```

Rules are deliberately conservative:

1. explicit deterministic rules are checked first;
2. an optional external classifier may handle unmatched items;
3. classifier output is accepted only above the configured confidence threshold;
4. otherwise the item stays in Inbox.

So an ambiguous note is not silently filed in the wrong project.

## ChatGPT → Workflowy

### One conversation from an account export

```bash
wf chatgpt conversations.json \
  --conversation-id CONVERSATION_ID \
  --parent inbox
```

This selects one conversation from the normal ChatGPT account export instead of importing the entire archive.

### The currently open browser chat

Run:

```bash
wf serve
```

Then load `browser-extension/` as an unpacked Chromium extension and click its action while the desired ChatGPT conversation is open.

The extension only reads that open conversation and posts it to `127.0.0.1`. It never receives the Workflowy API key; the local bridge performs the authenticated write.

The account-export route remains the stable fallback if ChatGPT changes its web DOM.

## PersonalHub deep links

Workflowy node IDs can be converted to stable Workflowy URLs. The tool can therefore enrich a selected table/column of a local PersonalHub SQLite database.

Always preview first:

```bash
wf sync
wf personalhub-links ~/path/to/personalhub.db \
  --table TABLE \
  --name-column NAME_COLUMN \
  --url-column workflowy_url
```

Only exact case-insensitive names that correspond to exactly one Workflowy node are considered. Ambiguous matches are skipped.

Apply only after inspecting the count:

```bash
wf personalhub-links ~/path/to/personalhub.db \
  --table TABLE \
  --name-column NAME_COLUMN \
  --url-column workflowy_url \
  --apply
```

Use `--create-column` only when you intentionally want the program to add that column.

## Codex roadmap ↔ Workflowy

`wf roadmap-sync` usa Workflowy come **unica centralina operativa visibile** della roadmap canonica. `roadmap.sqlite` resta la source of truth; GitHub conserva audit e codice. Le viste Markdown sono compatibilità/documentazione e non determinano più lo stato mostrato.

La dashboard combina roadmap + stato reale di `github-autosync` + binding di `chrome-codex-switcher`:

- `Ready`: prompt pendenti con dipendenze soddisfatte;
- `Waiting`: prompt pendenti ancora dipendenti da altri task;
- `Running`: Codex ha realmente acquisito il prompt;
- `Integration`: worker finito, PR/CI/rebase/merge gestiti asincronamente;
- `Needs fix`: BLOCKED/FAIL o hard blocker dell'integratore;
- `Done`: PASS canonico;
- `Archive`: stati storici/non operativi.

Ogni prompt espone azioni operative nella nota: `🚀 Apri`, `📋 Copia`, `🌐 ChatGPT` quando esiste il binding Chrome, e il deep link `🧠 Codex` quando CCS lo conosce. `PROMPT_ID` è la chiave comune e non viene mai dedotto da URL o titoli.

Sotto un prompt usa **un solo nodo figlio di stato**:

```text
R
P
B
F
```

`R = running`, `P = PASS`, `B = BLOCKED`, `F = FAIL`. Sono override di emergenza, non il flusso ordinario. Per task repository-backed `P` è rifiutato finché `github-autosync` non conferma `pipeline_state=done`; normalmente il PASS viene applicato automaticamente dopo il merge. `B/F` aggiungono anche un marker `#needs_fix`. Se sono presenti due comandi contemporaneamente, il bridge non sceglie e non scrive nulla.

Il timer opzionale `deploy/systemd/workflowy-roadmap-sync.timer` mantiene la dashboard sincronizzata con `roadmap.sqlite`, che resta l'unica source of truth.

## External systems

A common JSON-event adapter is available for:

```bash
wf ingest github event.json
wf ingest activitywatch event.json
wf ingest personalhub event.json
wf ingest calendar event.json
wf ingest gmail event.json
wf ingest generic event.json
```

Events are recorded in the local cache with an external ID/hash so repeated ingestion can be detected. Routing decides where a useful event belongs in Workflowy.

This keeps GitHub, ActivityWatch, PersonalHub, Calendar and mail selection logic outside the Workflowy client while giving them one common destination interface.

## Reviews, resurfacing and cleanup

```bash
# Weekly summary
wf weekly-review --days 7 --parent today

# Mirror a few old/open notes back into view
wf resurface --older-than-days 180 --limit 3 --parent today

# Find likely duplicate titles; report only
wf dedupe

# Preview explicit normalization rules
wf normalize --rules ~/.config/workflowy-bridge/rules.json

# Apply them only when intentionally requested
wf normalize --rules ~/.config/workflowy-bridge/rules.json --apply

# Preview old completed items eligible for archive
wf archive-completed --older-than-days 90 --archive-target archive

# Actually move them
wf archive-completed --older-than-days 90 --archive-target archive --apply
```

No bulk duplicate merge is attempted automatically.

## Project index

```bash
wf projects --root ~/projects --parent inbox
```

Workflowy does not provide reliable portable local-file links, so local paths are recorded as plain text. Git remotes are recorded when available.

## Workflowy as an automation control panel

`wf control` can inspect a chosen Workflowy node for children named:

```text
RUN: action-name
```

Only action names explicitly mapped to argv arrays in the local config may execute. There is no shell evaluation. Unknown actions remain untouched; successful actions are completed and receive a capped output child.

This provides a human-in-the-loop control surface without turning arbitrary Workflowy text into executable commands.

## Readwise / Reader

No custom bridge is needed for the normal Readwise use case: Readwise already provides a native Workflowy export/integration. Use that rather than duplicating highlight synchronization in this project.

## Runtime architecture

The canonical runtime is the ThinkPad/Fedora workstation, not the Oracle VM.

This is intentional: the workflows in this repository are primarily used while the workstation is already active (browser/ChatGPT capture, PersonalHub data, ActivityWatch, local Markdown and `~/projects`). Keeping the Workflowy bridge, API key, cache and timers on the same machine avoids a second deployment, SSH tunnels, duplicated configuration and cross-machine failure modes.

The Oracle VM is not part of the Workflowy architecture unless a future workflow has a concrete requirement to run independently while the ThinkPad is off.

Missed scheduled work does not require a 24/7 host: the provided systemd timers use `Persistent=true`, so a backup or weekly review missed while the ThinkPad is powered off can run after the machine becomes available again.

## Optional Fedora automation

Templates live in `deploy/systemd/` for:

- localhost capture bridge;
- periodic cache refresh;
- daily JSON backup;
- weekly review.

They are repository templates only until explicitly installed/enabled on the Fedora account.

## Live smoke test

```bash
workflowy-import-smoke
```

It creates disposable Workflowy data, verifies import, completed todos, internal links, no-op rerun, the replacement guard and replacement, and cleans up tracked smoke roots.

## Tests

```bash
python -m unittest discover -s tests -v
```

CI runs the suite on Python 3.11 and 3.13.
