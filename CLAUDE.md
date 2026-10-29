# Archivecheck

Standalone WeeWX SQLite archive analyzer and corrected-copy exporter.

## Status

Alpha. Runtime code uses Python standard-library modules; `--config` also uses WeeWX.

## Structure

- `bin/user/archivecheck.py`: command entry point.
- `bin/user/archivechecklib/`: archive operations, HTTP server and static interface.
- `install.py`: WeeWX extension installer. Installs below `bin/user` only.
- `package.py`: standalone ZIP builder.
- `docs/guide.md`: usage guide.
- `tests/`: archive, conflict, backup and HTTP regression tests.

## Conventions

Follow the WeeWX Python style: no runtime type annotations; Google-style docstring types.
Preserve the original database. Corrections write a new copy after a verified backup.
Published release bytes are immutable. Follow the parent organization's release and catalog rules.

## Commands

- Tests: `PYTHONPATH=bin python -m pytest -q tests`, 68 cases, about 7 seconds.
- Fresh environments: `PYTHONPATH=bin uv run --isolated --no-project --python 3.10 --with weewx --with pytest python -m pytest -q tests`, also with Python 3.14.
- Build: `python3 package.py --output dist/archivecheck.zip`.
- CLI: `python3 bin/user/archivecheck.py --help`.
