# Archivecheck

Inspect missing observations and timestamp offsets in a WeeWX SQLite archive.
Preview record alignment and aggregation, then export a corrected database with
an automatic verified backup. The interface reads a snapshot and leaves the
original database unchanged.

Developed for the WeeWX packages organization.

## Requirements

- Read access to the source SQLite archive and write access to the backup directory.
- No Python package beyond WeeWX. The `--database` mode uses only Python's standard library.

## Installation

Install `archivecheck` from the [WeeWX packages catalog](https://github.com/weewx-packages/catalog/blob/main/packages/archivecheck.md).
The catalog page lists the current release, its WeeWX and Python
requirements, and the install, update and removal commands.

The installer adds a standalone command and web interface. Start it manually;
it does not register a WeeWX service or require a station restart.

## Configuration

For a station rooted at `~/weewx-data`:

```sh
python3 ~/weewx-data/bin/user/archivecheck.py serve --config ~/weewx-data/weewx.conf
```

Open the printed address. For another computer on the same trusted network,
add `--host` with the station's IPv4 address. The interface has no login.

The [usage guide](docs/guide.md) covers database analysis, time alignment,
per-field aggregation, dry runs, backups and corrected-copy exports. A collapsible
step-by-step guide also appears at the top of the web interface.

## Limitations

Corrections support numeric scalar observations in SQLite archives with a single
`dateTime` primary key. MySQL archives require a SQLite export first. Archive
triggers, wind vector summaries and unrecognized daily summary layouts need
separate review. The tool does not infer measurement-time meaning or choose an
aggregation method from an observation name.

Exports contain the snapshot taken at startup, including its complete tables,
but exclude station records added after that snapshot. Daily summaries use the
station timezone; displayed timestamps and date filters use UTC.

## Development

In an environment with WeeWX and pytest installed:

```sh
PYTHONPATH=bin python -m pytest -q tests
```

Build an installable archive from this repository:

```sh
python3 package.py --output dist/archivecheck.zip
```

## License

Copyright 2026 Manuel Hilgert. Licensed under [GNU GPLv3](LICENSE.txt).
