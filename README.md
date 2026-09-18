# workflowy-importer

Import a Markdown file or a whole Markdown directory tree into Workflowy using the official Workflowy API.

The importer creates one container node and reconstructs folders, files, headings, lists, tasks, quotes and fenced code. It can also turn Obsidian/Logseq-style `[[wikilinks]]` and local Markdown links into real Workflowy links after all destination node IDs are known.

## Safety model

- **Dry-run first:** `--dry-run` needs no API key and performs no writes.
- **One tracked root:** each import is stored under one Workflowy root node.
- **Idempotent by default:** a state file stores only source/config hashes and Workflowy node IDs, never the API key.
- **No accidental duplicate refresh:** if the source changed, a normal rerun stops and tells you to use `--replace`.
- **Transactional replace:** a replacement is built completely first. The old tracked root is deleted only after the new import succeeds; if that deletion fails, the new root is removed.
- **Partial-import cleanup:** the root ID is journaled immediately after creation, so a later rerun can clean an interrupted initial import or replacement before doing new work.
- **No unsafe mutation retries:** automatic retries are limited to read-only API calls; node creation/update/delete are never replayed blindly after an ambiguous network failure.
- **Ambiguous links are not guessed:** unresolved or ambiguous `[[links]]` stay literal.

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/gernalix/workflowy-importer.git
cd workflowy-importer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Get an API key from Workflowy and expose it only as an environment variable:

```bash
export WORKFLOWY_API_KEY='...'
```

Do not commit the key.

## Dry-run

```bash
workflowy-import-md ~/Documents/my-vault --dry-run
```

This prints counts plus a capped tree preview, including how many internal links can or cannot be resolved.

## Import

Create one top-level container:

```bash
workflowy-import-md ~/Documents/my-vault
```

Import under the Workflowy Inbox:

```bash
workflowy-import-md ~/Documents/my-vault --parent inbox
```

Import under a specific Workflowy node, URL or custom shortcut:

```bash
workflowy-import-md ~/Documents/my-vault \
  --parent 'https://workflowy.com/#/xxxxxxxxxxxx' \
  --root-name 'Imported knowledge'
```

Workflowy's API accepts full node IDs, 12-character IDs, Workflowy node URLs and configured shortcut targets as parent destinations.

## Refresh after Markdown changes

A rerun with exactly the same source/config is a no-op if the tracked Workflowy root still exists.

When source content changes:

```bash
workflowy-import-md ~/Documents/my-vault --replace
```

`--replace` only targets the prior root ID recorded by this importer's state file. The state file defaults to:

```text
<directory>/.workflowy-importer-state.json
```

For a single file it is stored beside the file as `.<stem>.workflowy-importer-state.json`. Override it with `--state-file`.

## Markdown mapping

| Markdown/source | Workflowy result |
| --- | --- |
| directory | parent bullet |
| `file.md` | file-name bullet |
| `# H1` | H1 node |
| `## H2` | H2 node |
| `### H3` and deeper | H3 node, hierarchy still follows source level |
| `- item` | bullet |
| numbered item | bullet preserving its numeric marker |
| `- [ ]` / `- [x]` | open/completed todo |
| `> quote` | quote block |
| fenced code | `Code` parent with one code-block child per source line |
| bold/italic/strike/inline code | Workflowy-supported inline HTML |
| external Markdown link | hyperlink |
| `[[Page]]`, `[[Page#Heading]]`, `[[Page|label]]` | Workflowy node hyperlink when uniquely resolvable |
| `[label](page.md#Heading)` | Workflowy node hyperlink when uniquely resolvable |
| YAML front matter | preserved under a `Front matter` node |

The two-pass link phase is deliberate: first all nodes are created and their Workflowy IDs are collected; then internal links are updated to point at those IDs.

## Known limitations

- Images and attachments are **not uploaded**. Markdown image syntax is preserved as text.
- Ambiguous duplicate page names are never auto-selected; path-qualified links such as `[[folder/Page]]` are safer.
- Markdown tables and uncommon extensions are preserved as ordinary text rather than recreated as special Workflowy structures.
- Fenced multi-line code is represented as a `Code` parent plus code-block children so no line is lost to Workflowy's multi-line node semantics.
- This is an importer, not a bidirectional sync engine. `--replace` rebuilds the tracked import rather than attempting an in-place diff.

## Live smoke test

After configuring `WORKFLOWY_API_KEY`, one command can exercise the real API with disposable data:

```bash
workflowy-import-smoke
```

The smoke test creates a uniquely named temporary root under the Workflowy Inbox, verifies import, completed todos, converted internal links, idempotent rerun, the `--replace` guard and replacement, then deletes every root it tracked in a `finally` cleanup path. It never imports your real Markdown files.

## Tests

```bash
python -m unittest discover -s tests -v
```

CI runs the same suite on Python 3.11 and 3.13.
