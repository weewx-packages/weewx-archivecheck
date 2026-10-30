# Inspecting an archive with Archivecheck

Archivecheck helps you investigate missing observations, timestamp offsets and
separately imported records in a WeeWX SQLite or MySQL database. It produces shareable
reports and previews corrections before exporting a new database file.

The tool reads a consistent snapshot. The running station can continue writing
to its archive while you inspect that snapshot. An export contains the records
present when the snapshot was taken.

## Install and start the local interface

Install `archivecheck` from the [WeeWX packages catalog](https://github.com/weewx-packages/catalog/blob/main/packages/archivecheck.md).
The catalog page lists the current release, requirements and installation commands.

For a pip installation rooted at `~/weewx-data`, start the interface with:

```sh
python3 ~/weewx-data/bin/user/archivecheck.py serve \
    --config ~/weewx-data/weewx.conf
```

For a package installation, the command is normally at
`/usr/share/weewx/user/archivecheck.py` and the configuration at
`/etc/weewx/weewx.conf`. Use the Python environment that contains WeeWX when
passing `--config`.

Open the printed address, normally `http://127.0.0.1:8765/`. To open the interface
from another computer on your network, bind it to the station's IPv4 address:

```sh
python3 archivecheck.py serve --config ~/weewx-data/weewx.conf --host 192.168.1.10
```

Open `http://192.168.1.10:8765/` on that computer. The interface has no login;
use a trusted network. The default remains loopback. You can also forward the
default port with SSH:

```sh
ssh -L 8765:127.0.0.1:8765 station-user@station
```

When the binding in `weewx.conf` points to MySQL, Archivecheck connects with
the settings from that file. It copies the archive table and its daily summaries
into a temporary SQLite snapshot and works on that copy. This needs the MySQL
driver WeeWX already uses, mysqlclient or PyMySQL. The copy is consistent for
InnoDB tables, the WeeWX default.

You can also pass a SQLite file directly with `--database /path/to/weewx.sdb`.
This mode needs only Python's standard library and can run on another computer.
Use `--timezone Europe/Berlin`, or your station's timezone, when that computer
has a different timezone. Daily summaries follow the station's local days;
chart timestamps and date filters are UTC.

The following examples use `archivecheck.py` for the installed script's path.

The interface opens with a collapsible step-by-step guide at the top. No
observation, transfer field, weather field or aggregation method is preselected.

## Investigate missing observations

The overview shows field availability, recorded archive intervals, timestamp
offsets and gaps. Choose an observation to see its stored numeric range and
coverage over time. First-to-last difference uses the first and last non-NULL
stored values; it does not interpolate values at time boundaries. A decrease
is displayed without assuming that it represents a counter reset.

The alignment panel shows off-grid counts per field and actual archive rows
around the first affected interval. Amber rows have timestamps between archive
boundaries. The field table also lists off-grid counts. A timestamp offset alone
does not determine the correct merge direction; select that in the dry run.

Save an HTML report to share a readable snapshot, or JSON to share the detailed
counts and examples. The HTML file opens on its own without a running server.
The report contains observation names, times and example values, but does not
include `weewx.conf` or database credentials.

You can also create reports from the command line:

```sh
python3 archivecheck.py analyze --database weewx.sdb \
    --field energyprod --html report.html --json report.json
```

## Preview a merge

Choose the observations to transfer in the field table. Select another
observation, such as `outTemp`, that identifies the destination weather rows.
Specify the archive grid, maximum shift, and the direction in which readings
belong. The optional date range can limit a historical import.

Run the dry run. Each preview shows the source and destination timestamps,
destination values before and after the operation, and what remains in the
source row. Conflicts include different existing values, missing destinations,
multiple readings for one destination, incompatible units or archive intervals,
and shifts beyond the tolerance. An entire source record is blocked if any
selected field conflicts.

The preview contains up to 200 destination groups, prioritizing conflicts and
examples of multi-reading compaction. The interface and saved HTML report show
20 groups per page. Initially, only conflicts are shown if present; use **Show** to switch
to all preview groups or ready groups, and **Per page** to choose 10, 20 or 50.
These controls filter the preview, while all candidates are checked.

Non-NULL values transfer;
NULL source values do not replace destination values. Source rows with other
observations remain after the selected values are cleared. A row is removed
only when no other observations remain.

The equivalent command-line dry run is:

```sh
python3 archivecheck.py dry-run --database weewx.sdb \
    --fields energyprod,power --anchor outTemp \
    --interval 300 --tolerance 15 --direction previous \
    --json dry-run.json --html dry-run.html
```

## Compact denser observations

If several imported readings belong with one weather record, choose an
**Aggregation** method directly in the field table. The method selector offers
an arithmetic mean, sum, minimum, maximum, first value or last value for each
observation. Choosing a method also checks **Transfer** for that field. Uncheck
**Transfer** to exclude it. Without a method, multiple readings for the field
remain conflicts. No method is inferred from the field name. The dry run groups
its preview by destination and lists the input values alongside the calculated
result.

For example, two power readings of 100 and 200 become 150 with **Mean**.
For a running energy counter, use **Last value** to retain the most recent
counter reading. For amounts accumulated during separate source intervals,
**Sum** adds the amounts. Means give each non-NULL sample equal weight; they
are not weighted by elapsed time or by the source interval length.

Choose **Nearest weather record** to snap to an actual record
rather than an epoch-aligned grid boundary. A match must still fall within
the shift tolerance. Ties and incompatible unit systems are conflicts.
When compacting, the source and destination may have different positive
archive intervals; the destination interval must match the chosen archive grid.
An existing destination value must equal the calculated result, otherwise
the whole destination group is blocked.

The corresponding command-line options are:

```sh
python3 archivecheck.py dry-run --database weewx.sdb \
    --fields energyprod,power --direction record \
    --interval 300 --tolerance 150 \
    --reduce mean --reducers energyprod=last \
    --json compact.json --html compact.html
```

This operates on records already in the database, including separately imported
records. It does not change the behaviour of `weectl import`. In particular,
the importer's `interval = conf` option sets interval metadata without rounding
timestamps or averaging incoming records.

## Export a reviewed correction

After reviewing the dry run, choose **Create corrected copy**. When some rows
are blocked, explicitly choose whether to export just the ready rows. Blocked
rows remain unchanged. Changes to merge settings require a new dry run.

The export checks the plan against the snapshot again, saves a verified backup,
then creates a new SQLite database from that backup. If the backup cannot be
written or verified, the export stops. Existing output files are never replaced.
It rebuilds the affected scalar daily summaries for the transferred observations and leaves
other summary tables in place. Download the result before stopping the server;
temporary copies are removed when it stops.

## Load a correction into MySQL

The corrected copy is a SQLite file, also for a MySQL station. To use it, create
an empty database on the same server, and give the MySQL account from
`weewx.conf` access to it:

```sql
CREATE DATABASE weewx_corrected;
GRANT ALL ON weewx_corrected.* TO 'weewx'@'localhost';
```

Then copy the corrected tables into it:

```sh
python3 archivecheck.py copy-to-mysql --config ~/weewx-data/weewx.conf \
    --input archive-corrected.sdb --target weewx_corrected
```

Each table gets the definition of the station's table, so column types, keys
and the storage engine stay the same. The command compares row counts after
copying. It refuses a target database that already contains tables, and it
leaves the station database unchanged.

To switch the station to the corrected data, stop WeeWX, set
`database_name = weewx_corrected` in the MySQL entry under `[Databases]` in
`weewx.conf`, and start WeeWX again. The copy lacks records the station wrote
after the snapshot. For a final correction, stop WeeWX before starting
Archivecheck.

## Keep a backup

Every correction export automatically creates a permanent backup in
`archivecheck-backups/` beside the source database. Each backup has a unique
timestamped filename. Earlier backups are retained, including when a later
correction fails. Dry runs and analysis do not create permanent backups.

Backups contain the complete analyzed database snapshot, including other tables
and records outside your selected date range. In a web session, this is the
snapshot loaded when the session started. It does not include records the
running station added afterwards. Start a fresh session, or use the `backup`
command, to capture newer records.

The interface shows the saved path and a download link after an export.
You can also use **Create backup** without preparing a correction. Backups
remain on disk after the server stops; only their session download links expire.
For a MySQL archive, backups go to `archivecheck-backups/` beside `weewx.conf`.
They are SQLite files with the archive table and its daily summaries, not a
dump of the MySQL database.

Read an existing backup with `analyze --database /path/to/backup.sdb`, or use
the normal [WeeWX restore procedure](https://github.com/weewx/weewx/blob/master/docs_src/usersguide/backup.md).

Choose another permanent directory with `--backup-dir` on `serve`, `export`, or
`backup`. The directory must be writable by the account running Archivecheck.
For example, to create a backup directly:

```sh
python3 archivecheck.py backup --database weewx.sdb --backup-dir /path/to/backups
```

Each backup is checked with SQLite's integrity checker and accompanied by a
JSON file containing its SHA-256 checksum, size and UTC creation time. The
command-line output includes this information and the saved path. The tool
does not automatically remove old backups.

## Export from a saved command-line dry run

To replay a saved command-line dry run:

```sh
python3 archivecheck.py export --database weewx.sdb \
    --plan dry-run.json --output weewx-corrected.sdb
```

Add `--ready-only` to explicitly retain blocked candidates. If the dry run used
date bounds or a station timezone, use the same options for the export. A changed
source or timezone can invalidate the plan and require a new dry run. Review
the corrected file separately before deciding whether to use it for a station.

## Try the included demonstration

```sh
python3 archivecheck.py demo --output pv-demo.sdb
python3 archivecheck.py serve --database pv-demo.sdb --timezone UTC
```

The synthetic archive contains separate weather and solar rows, a later period
of combined records, and examples of missing destinations, duplicate readings,
different units and existing destination values.

## Command options and supported data

Use `analyze --help`, `serve --help`, `dry-run --help`, `export --help`, or `backup --help` for
the full list of arguments. Common source options are `--database` or `--config`,
`--binding`, `--table`, `--from`, `--to`, and `--timezone`. Dates accept epoch
seconds, `YYYY-mm-dd`, or `YYYY-mm-ddTHH:MM[:SS]`. A date-only `--to` includes
that entire UTC day. The server port defaults to 8765.

This version analyzes SQLite and MySQL archives and merges numeric scalar observations
on an explicitly selected archive grid or to the nearest existing weather record.
It requires a single `dateTime`
primary key for merging. Archive triggers, wind vector summary corrections and
unrecognized daily summary layouts require separate review. Archivecheck does
not read MySQL archives with triggers on their tables. No interpolation or automatic
inference of measurement-time meaning is performed.
