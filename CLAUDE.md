# CLAUDE.md

The standing contract for every executor working in this repo.

## What this repo is

**workbench**, a home for small, unrelated scripts. Each lives in its own top-level
subfolder; the [README](README.md) lists them.

## Subfolder rules

- **An item works only inside the subfolder its prompt names.** Code is never shared across
  subfolders: if two need the same helper, each keeps its own copy.
- **Each subfolder has a short `README.md`:** what it's for, how to run it, and the settings
  it takes. A new subfolder also gets its line in the top-level README.
- **Python scripts declare their dependencies inline** (a `# /// script` block) and run with
  `uv run <script>.py`. No virtual environments and no shared `pyproject.toml`.
- **Personal data (photos, documents) is never committed.** Inputs and outputs are paths
  passed as settings or arguments, never files in the repo.

## Working rules

- **Commit to the branch you are on.** Leave nothing uncommitted.
- **Background any step over ~30 seconds**, and wait on the real process, never on a
  launcher wrapper that returns before the work is done.
- **Times are local, 12-hour clock** (2:45pm), in reports, docs and progress lines.
- **LF line endings.** `.gitattributes` sets `* text=auto eol=lf`; don't override it.
- **Operational facts go in the subfolder's README** (commands, settings, quirks of this
  machine), never left only in a report.

## Report

Write the report to the path conductor's preamble gives, starting with the header it specifies.

- Soft target about 60 lines.
- Findings, decisions and surprises only: no process narration.
- Mention tests only for a red you couldn't fix.
- End your final message with `Report written to <path>`.
