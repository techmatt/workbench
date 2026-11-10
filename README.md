# workbench

A home for small, unrelated scripts, each in its own top-level subfolder with its own README
(what it's for, how to run it, its settings). Subfolders share no code, Python scripts carry
their dependencies inline and run with `uv run`, and personal data is never committed: inputs
and outputs are paths passed in. The rules for working here are in [CLAUDE.md](CLAUDE.md).

## Subfolders

- [photo-collector](photo-collector/README.md): gathers kids' photos and videos from many
  sources into one collection, removing duplicates and sorting by year and by type.
