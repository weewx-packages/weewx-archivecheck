# Copyright (c) 2026 Manuel Hilgert
# SPDX-License-Identifier: GPL-3.0-only

"""Read SQLite snapshots, inspect observations, and prepare scalar merges."""

import collections
import contextlib
import datetime
import hashlib
import json
import math
import os
import pathlib
import re
import sqlite3
import tempfile
import time

from . import __version__


def quote_identifier(name):
    """Quote an SQLite table or column name."""
    return '"%s"' % name.replace('"', '""')


def json_value(value):
    """Represent SQLite values in strict JSON, including BLOBs and infinities."""
    if isinstance(value, bytes):
        return {'blob_hex': value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return {'non_finite': str(value)}
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':'))


def is_number(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def open_readonly(path):
    """Open an existing database without granting write access."""
    uri = pathlib.Path(path).resolve().as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only = ON')
    return connection


def copy_database(source, destination):
    """Copy a connection to an open destination, including committed WAL data."""
    if hasattr(source, 'backup'):
        source.backup(destination)
    else:
        # Python 3.6 has no backup API. Keep one read transaction throughout the dump.
        source.execute('BEGIN')
        try:
            destination.executescript('\n'.join(source.iterdump()))
        finally:
            source.rollback()


def create_backup(archive, directory, name='archive.sdb'):
    """Save and verify a permanent, complete backup of the analyzed database.

    Args:
        archive (Archive): Read-only database connection; date bounds do not limit the backup.
        directory (str): Backup directory, created when necessary.
        name (str): Source display name used in the backup filename and metadata.

    Returns:
        dict: Backup path, UTC creation time, size, checksum and metadata file path.
    """
    directory = os.path.abspath(os.path.expanduser(directory))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    created = datetime.datetime.now(datetime.timezone.utc)
    stem = re.sub(r'[^A-Za-z0-9_.-]+', '_', os.path.splitext(os.path.basename(name))[0])
    stem = stem.strip('._')[:80] or 'archive'
    prefix = '%s-before-%s-' % (stem, created.strftime('%Y%m%dT%H%M%S.%fZ'))
    descriptor, path = tempfile.mkstemp(prefix=prefix, suffix='.sdb', dir=directory)
    os.close(descriptor)
    metadata_path = path + '.json'
    metadata_created = False
    try:
        with contextlib.closing(sqlite3.connect(path)) as destination:
            copy_database(archive.connection, destination)
            if [row[0] for row in destination.execute('PRAGMA quick_check')] != ['ok']:
                raise ValueError('The backup failed its integrity check.')
        checksum = hashlib.sha256()
        with open(path, 'rb') as backup_file:
            for block in iter(lambda: backup_file.read(1024 * 1024), b''):
                checksum.update(block)
        result = {'path': path, 'filename': os.path.basename(path),
                  'metadata_path': metadata_path,
                  'created_utc': created.isoformat(), 'source': os.path.basename(name),
                  'scope': 'complete_analyzed_snapshot', 'version': __version__,
                  'bytes': os.path.getsize(path), 'sha256': checksum.hexdigest(), 'integrity': 'ok'}
        metadata_descriptor = os.open(metadata_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        metadata_created = True
        with os.fdopen(metadata_descriptor, 'w', encoding='utf-8') as metadata_file:
            json.dump(result, metadata_file, indent=2, allow_nan=False)
            metadata_file.write('\n')
            metadata_file.flush()
            os.fsync(metadata_file.fileno())
        return result
    except Exception:
        os.unlink(path)
        if metadata_created:
            os.unlink(metadata_path)
        raise


class ExportError(ValueError):
    """A failed correction whose verified backup remains available."""

    def __init__(self, message, backup):
        super().__init__('%s Backup retained at %s.' % (message, backup['path']))
        self.backup = backup


@contextlib.contextmanager
def snapshot(path):
    """Create a temporary, consistent copy of an existing SQLite database.

    Args:
        path (str): Source database path.

    Yields:
        str: Path to the snapshot, removed on leaving the context.
    """
    with tempfile.TemporaryDirectory(prefix='weewx-archivecheck-') as directory:
        destination_path = os.path.join(directory, 'snapshot.sdb')
        with contextlib.closing(open_readonly(path)) as source:
            with contextlib.closing(sqlite3.connect(destination_path)) as destination:
                copy_database(source, destination)
        yield destination_path


class Archive:
    """Read an existing archive table with its actual, possibly custom schema."""

    def __init__(self, path, table='archive', start=None, stop=None):
        """Open an archive without creating tables or loading WeeWX services.

        Args:
            path (str): Database path.
            table (str): Archive table name.
            start (int|None): Inclusive first timestamp.
            stop (int|None): Inclusive last timestamp.
        """
        self.connection = open_readonly(path)
        self.table = table
        self.start = start
        self.stop = stop
        try:
            if start is not None and stop is not None and start > stop:
                raise ValueError('The start must not be after the end.')
            exists = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,)).fetchone()
            if exists is None:
                raise ValueError('Archive table %r does not exist.' % table)
            self.schema = [dict(row) for row in self.connection.execute(
                'PRAGMA table_info(%s)' % quote_identifier(table))]
            self.columns = [column['name'] for column in self.schema]
            by_lower = {name.lower(): name for name in self.columns}
            if not {'datetime', 'interval', 'usunits'}.issubset(by_lower):
                raise ValueError('An archive needs dateTime, interval and usUnits columns.')
            self.timestamp = by_lower['datetime']
            self.interval = by_lower['interval']
            self.units = by_lower['usunits']
            self.observations = [name for name in self.columns
                                 if name not in (self.timestamp, self.interval, self.units)]
        except Exception:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.connection.close()

    def where(self):
        """Return the timestamp filter and its bound values."""
        timestamp = quote_identifier(self.timestamp)
        conditions = ["typeof(%s) = 'integer'" % timestamp]
        parameters = []
        if self.start is not None:
            conditions.append('%s >= ?' % timestamp)
            parameters.append(self.start)
        if self.stop is not None:
            conditions.append('%s <= ?' % timestamp)
            parameters.append(self.stop)
        return ' AND '.join(conditions), parameters

    def select(self, expressions, suffix='', extra_parameters=()):
        where, parameters = self.where()
        return self.connection.execute(
            'SELECT %s FROM %s WHERE %s %s' % (
                expressions, quote_identifier(self.table), where, suffix),
            parameters + list(extra_parameters))

    def record(self, timestamp):
        row = self.connection.execute('SELECT * FROM %s WHERE %s = ?' % (
            quote_identifier(self.table), quote_identifier(self.timestamp)),
            (timestamp,)).fetchone()
        return dict(row) if row is not None else None


def analyze(archive, name='archive.sdb'):
    """Summarize field availability, archive cadence and storage integrity.

    Args:
        archive (Archive): Read-only archive, with optional timestamp bounds.
        name (str): Display name, without a source filesystem path.

    Returns:
        dict: JSON-compatible archive report.
    """
    timestamp = quote_identifier(archive.timestamp)
    counts = archive.select('COUNT(*), MIN(%s), MAX(%s)' % (
        timestamp, timestamp)).fetchone()
    field_counts = archive.select(', '.join('COUNT(%s)' % quote_identifier(field)
                                           for field in archive.observations)).fetchone() \
        if archive.observations else []
    fields = []
    for field, count in zip(archive.observations, field_counts):
        definition = next(item for item in archive.schema if item['name'] == field)
        fields.append({'name': field, 'type': definition['type'], 'count': count,
                       'missing': counts[0] - count,
                       'coverage': count / counts[0] if counts[0] else 0})
    intervals = [{'minutes': json_value(row[0]), 'count': row[1]} for row in archive.select(
        '%s, COUNT(*)' % quote_identifier(archive.interval),
        'GROUP BY %s ORDER BY COUNT(*) DESC' % quote_identifier(archive.interval))]
    suggested_intervals = intervals
    interval_basis = None
    if 'outTemp' in archive.observations:
        weather_intervals = [{'minutes': json_value(row[0]), 'count': row[1]}
                             for row in archive.select(
                                 '%s, COUNT(*)' % quote_identifier(archive.interval),
                                 'AND "outTemp" IS NOT NULL GROUP BY %s ORDER BY COUNT(*) DESC'
                                 % quote_identifier(archive.interval))]
        if weather_intervals:
            suggested_intervals = weather_intervals
            interval_basis = 'outTemp'
    usable_intervals = [item for item in suggested_intervals
                        if is_number(item['minutes']) and item['minutes'] > 0
                        and float(item['minutes'] * 60).is_integer()]
    interval = int(usable_intervals[0]['minutes'] * 60) if usable_intervals else None
    units = [{'value': json_value(row[0]), 'count': row[1]} for row in archive.select(
        '%s, COUNT(*)' % quote_identifier(archive.units),
        'GROUP BY %s ORDER BY COUNT(*) DESC' % quote_identifier(archive.units))]
    offsets = []
    alignment_examples = []
    for field in fields:
        field['on_grid'] = None
        field['off_grid'] = None
    if interval:
        offset_expression = '((%s %% %d) + %d) %% %d' % (
            timestamp, interval, interval, interval)
        offsets = [{'seconds': row[0], 'count': row[1]} for row in archive.select(
            '%s, COUNT(*)' % offset_expression,
            'GROUP BY 1 ORDER BY 1')]
        if fields:
            off_grid_counts = archive.select(', '.join(
                'COUNT(CASE WHEN (%s) != 0 THEN %s END)' % (
                    offset_expression, quote_identifier(field['name'])) for field in fields)).fetchone()
            for field, off_grid_count in zip(fields, off_grid_counts):
                field['off_grid'] = off_grid_count
                field['on_grid'] = field['count'] - off_grid_count
        first_off_grid = archive.select(timestamp, 'AND (%s) != 0 ORDER BY %s LIMIT 1' % (
            offset_expression, timestamp)).fetchone()
        if first_off_grid is not None:
            boundary = first_off_grid[0] - first_off_grid[0] % interval
            for row in archive.select('*', 'AND %s >= ? ORDER BY %s LIMIT 6' % (
                    timestamp, timestamp), (boundary,)):
                alignment_examples.append({
                    'timestamp': row[archive.timestamp],
                    'offset_seconds': row[archive.timestamp] % interval,
                    'values': {field: json_value(row[field]) for field in archive.observations
                               if row[field] is not None}})
    gaps = []
    gap_count = 0
    short_count = 0
    previous = None
    for row in archive.select('%s, %s' % (timestamp, quote_identifier(archive.interval)),
                              'ORDER BY %s' % timestamp):
        expected = row[1] * 60 if is_number(row[1]) and row[1] > 0 else interval
        if previous is not None and expected:
            delta = row[0] - previous
            if delta > expected:
                gap_count += 1
                if len(gaps) < 20:
                    gaps.append({'start': previous, 'stop': row[0], 'seconds': delta})
            elif delta < expected:
                short_count += 1
        previous = row[0]
    invalid_timestamps = archive.connection.execute(
        "SELECT COUNT(*) FROM %s WHERE typeof(%s) != 'integer'" % (
            quote_identifier(archive.table), timestamp)).fetchone()[0]
    integrity = [row[0] for row in archive.connection.execute('PRAGMA quick_check')]
    summaries = [row[0] for row in archive.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND substr(name, 1, ?) = ?",
        (len(archive.table) + 5, archive.table + '_day_'))]
    return {'version': __version__, 'source': name, 'table': archive.table,
            'start': archive.start, 'stop': archive.stop,
            'records': counts[0], 'first_timestamp': counts[1], 'last_timestamp': counts[2],
            'fields': fields, 'interval_seconds': interval, 'interval_basis': interval_basis,
            'intervals': intervals,
            'units': units, 'offsets': offsets, 'alignment_examples': alignment_examples,
            'gap_count': gap_count,
            'short_interval_count': short_count, 'gap_examples': gaps,
            'invalid_timestamps': invalid_timestamps, 'integrity': integrity,
            'daily_tables': summaries, 'summary_timezone': '/'.join(time.tzname)}


def field_details(archive, field, buckets=60):
    """Inspect stored values without assuming that decreases are counter resets.

    Args:
        archive (Archive): Read-only archive.
        field (str): Observation column name.
        buckets (int): Maximum number of evenly spaced chart buckets.

    Returns:
        dict: Coverage buckets, numeric extrema, endpoints and decrease examples.
    """
    if field not in archive.observations:
        raise ValueError('Unknown observation %r.' % field)
    if not 1 <= buckets <= 500:
        raise ValueError('Use between 1 and 500 chart buckets.')
    timestamp = quote_identifier(archive.timestamp)
    bounds = archive.select('MIN(%s), MAX(%s)' % (timestamp, timestamp)).fetchone()
    first_timestamp = bounds[0] if bounds[0] is not None else 0
    last_timestamp = bounds[1] if bounds[1] is not None else 0
    width = max(1, math.ceil((last_timestamp - first_timestamp + 1) / buckets))
    record_counts = collections.Counter()
    value_counts = collections.Counter()
    bucket_minimum = {}
    bucket_maximum = {}
    count = numeric_count = invalid_count = decreases = 0
    minimum = maximum = None
    first = last = None
    examples = []
    previous = None
    for row in archive.select('%s, %s' % (timestamp, quote_identifier(field)),
                              'ORDER BY %s' % timestamp):
        key = (row[0] - first_timestamp) // width
        record_counts[key] += 1
        value = row[1]
        if value is None:
            continue
        count += 1
        value_counts[key] += 1
        point = {'timestamp': row[0], 'value': json_value(value)}
        if first is None:
            first = point
        last = point
        if not is_number(value):
            invalid_count += 1
            previous = None
            continue
        numeric_count += 1
        minimum = value if minimum is None else min(value, minimum)
        maximum = value if maximum is None else max(value, maximum)
        bucket_minimum[key] = min(value, bucket_minimum.get(key, value))
        bucket_maximum[key] = max(value, bucket_maximum.get(key, value))
        if previous is not None and value < previous['value']:
            decreases += 1
            if len(examples) < 20:
                examples.append({'before': previous, 'after': point})
        previous = point
    difference = last['value'] - first['value'] if first is not None and last is not None \
        and invalid_count == 0 else None
    if difference is not None and not is_number(difference):
        difference = None
    chart = []
    if bounds[0] is not None:
        for index in range((last_timestamp - first_timestamp) // width + 1):
            chart.append({'timestamp': first_timestamp + index * width,
                          'records': record_counts[index], 'count': value_counts[index],
                          'minimum': bucket_minimum.get(index), 'maximum': bucket_maximum.get(index)})
    return {'field': field, 'count': count, 'numeric_count': numeric_count,
            'invalid_count': invalid_count, 'minimum': minimum, 'maximum': maximum,
            'first': first, 'last': last, 'difference': difference, 'decreases': decreases,
            'decrease_examples': examples, 'buckets': chart}


def _boundary(timestamp, interval, direction):
    previous = timestamp - timestamp % interval
    if direction == 'previous':
        return previous
    if direction == 'next':
        return previous + interval
    remainder = timestamp % interval
    if remainder * 2 == interval:
        return None
    return previous if remainder * 2 < interval else previous + interval


def _validate_merge(archive, options):
    fields = options['fields']
    if not isinstance(fields, list) or not fields or any(not isinstance(field, str) for field in fields):
        raise ValueError('Choose a list of observation names.')
    if len(set(fields)) != len(fields):
        raise ValueError('Choose at least one observation, without duplicates.')
    if any(field not in archive.observations for field in fields):
        raise ValueError('Every transfer field must be an observation in this archive.')
    if options['anchor'] not in archive.observations or options['anchor'] in fields:
        raise ValueError('Choose a separate observation to identify the weather records.')
    if type(options['interval']) is not int or options['interval'] <= 0:
        raise ValueError('The archive interval must be a positive integer in seconds.')
    if type(options['tolerance']) is not int or not 0 <= options['tolerance'] < options['interval']:
        raise ValueError('The tolerance must be non-negative and less than the interval.')
    if options['direction'] not in ('previous', 'next', 'nearest', 'record'):
        raise ValueError('Choose previous, next, nearest or record as the alignment rule.')
    supported = ('reject', 'mean', 'first', 'last', 'sum', 'min', 'max')
    if not isinstance(options['reducers'], dict):
        raise ValueError('Per-field aggregations must map observation names to methods.')
    if options['reducer'] not in supported or any(
            field not in fields or method not in supported
            for field, method in options['reducers'].items()):
        raise ValueError('Choose reject, mean, first, last, sum, min or max for each transfer field.')
    primary = [column for column in archive.schema if column['pk']]
    if len(primary) != 1 or primary[0]['name'] != archive.timestamp:
        raise ValueError('Merging requires dateTime to be the single primary key.')
    touched_tables = [archive.table, archive.table + '_day__metadata'] + [
        archive.table + '_day_' + field for field in fields]
    if archive.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND tbl_name IN (%s)" %
            ','.join('?' for table in touched_tables), touched_tables).fetchone():
        raise ValueError('This archive has custom triggers. Review them before merging.')


def _target_timestamp(archive, timestamp, options):
    if options['direction'] != 'record':
        return _boundary(timestamp, options['interval'], options['direction'])
    column = quote_identifier(archive.timestamp)
    anchor = quote_identifier(options['anchor'])
    candidates = []
    for comparison, order in (('<=', 'DESC'), ('>', 'ASC')):
        row = archive.connection.execute(
            "SELECT %s FROM %s WHERE %s IS NOT NULL AND typeof(%s) = 'integer' AND %s %s ? "
            'ORDER BY %s %s LIMIT 1' % (
                column, quote_identifier(archive.table), anchor, column,
                column, comparison, column, order), (timestamp,)).fetchone()
        if row is not None:
            candidates.append(row[0])
    if not candidates:
        return None
    if len(candidates) == 2 and timestamp - candidates[0] == candidates[1] - timestamp:
        return None
    return min(candidates, key=lambda value: abs(value - timestamp))


def _reduce_values(values, method):
    if method in ('reject', 'first'):
        return values[0]
    if method == 'last':
        return values[-1]
    if method == 'min':
        return min(values)
    if method == 'max':
        return max(values)
    if method == 'sum':
        return math.fsum(values)
    # Dividing first avoids overflowing when the mean of large finite values is still finite.
    return math.fsum(value / len(values) for value in values)


def _pair_problems(archive, source, target, target_time, options):
    problems = []
    source_time = source[archive.timestamp]
    if target_time is None:
        problems.append('equidistant_boundaries')
    elif abs(source_time - target_time) > options['tolerance']:
        problems.append('outside_tolerance')
    elif ((archive.start is not None and target_time < archive.start)
          or (archive.stop is not None and target_time > archive.stop)):
        problems.append('target_outside_range')
    if source[options['anchor']] is not None:
        problems.append('source_contains_weather')
    if target is None:
        problems.append('missing_target')
    else:
        if target[options['anchor']] is None:
            problems.append('target_has_no_anchor')
        if source[archive.units] != target[archive.units] or target[archive.units] not in (1, 16, 17):
            problems.append('unit_mismatch')
        compacting = options['reducer'] != 'reject' or bool(options['reducers'])
        if (not is_number(source[archive.interval]) or source[archive.interval] <= 0
                or not is_number(target[archive.interval])
                or target[archive.interval] * 60 != options['interval']
                or (not compacting and source[archive.interval] * 60 != options['interval'])):
            problems.append('interval_mismatch')
    return problems


def _preview_priority(entry):
    return 2 if entry['status'] == 'blocked' else 1 if entry['source_count'] > 1 else 0


def plan_merge(archive, fields, anchor='outTemp', interval=300,
               tolerance=15, direction='previous', reducer='reject', reducers=None):
    """Dry-run a merge, retaining every unselected observation in source records.

    Args:
        archive (Archive): Read-only source with optional timestamp bounds.
        fields (list[str]): Columns to transfer when a source value is not NULL.
        anchor (str): A non-NULL value identifies a destination weather record.
        interval (int): Explicit archive grid in seconds, aligned to Unix epoch.
        tolerance (int): Largest permitted distance to the chosen boundary.
        direction (str): Previous, next or nearest grid boundary, or nearest existing record.
        reducer (str): Default aggregation, or reject to block multiple source readings.
        reducers (dict|None): Per-field overrides of the default aggregation.

    Returns:
        dict: Dry-run options, totals, a bounded preview, operations and plan identifier.
    """
    field_reducers = reducers if reducers is not None else {}
    options = {'fields': fields, 'anchor': anchor, 'interval': interval,
               'tolerance': tolerance, 'direction': direction, 'reducer': reducer,
               'reducers': field_reducers,
               'table': archive.table, 'start': archive.start, 'stop': archive.stop,
               'summary_timezone': os.environ.get('TZ') or '/'.join(time.tzname)}
    _validate_merge(archive, options)
    digest = hashlib.sha256(canonical(options).encode('utf-8'))
    totals = collections.Counter()
    reasons = collections.Counter()
    preview = []
    preview_slots = [[], [], []]
    operations = []
    groups = collections.defaultdict(list)
    timestamp_column = quote_identifier(archive.timestamp)
    fields_expression = ' OR '.join('%s IS NOT NULL' % quote_identifier(field)
                                    for field in fields)
    where, parameters = archive.where()
    candidate_filter = '%s IS NULL' % quote_identifier(anchor) if direction == 'record' \
        else '%s %% %d != 0' % (timestamp_column, interval)
    query = 'SELECT %s FROM %s WHERE %s AND (%s) AND %s ORDER BY %s' % (
        timestamp_column, quote_identifier(archive.table), where, fields_expression,
        candidate_filter, timestamp_column)
    for row in archive.connection.execute(query, parameters):
        source_time = row[0]
        target_time = _target_timestamp(archive, source_time, options)
        # A distant reading is skipped independently. It must not block valid samples near that target.
        key = (target_time, source_time) if target_time is None \
            or abs(source_time - target_time) > tolerance else target_time
        groups[key].append(source_time)
    for key, source_times in groups.items():
        target_time = key if isinstance(key, int) else key[0]
        sources = [archive.record(source_time) for source_time in source_times]
        target = archive.record(target_time) if target_time is not None else None
        proof = [[[json_value(source[name]) for name in archive.columns] for source in sources],
                 [json_value(target[name]) for name in archive.columns] if target else None]
        digest.update(canonical(proof).encode('utf-8'))
        problems = []
        for source in sources:
            problems.extend(_pair_problems(archive, source, target, target_time, options))
        if len(sources) > 1 and reducer == 'reject' and not reducers:
            problems.append('multiple_sources')
        values = {}
        changes = []
        for field in fields:
            incoming = [source[field] for source in sources if source[field] is not None]
            if not incoming:
                continue
            method = field_reducers.get(field, reducer)
            if method == 'reject' and len(incoming) > 1:
                problems.append('multiple_sources')
            if not all(is_number(value) for value in incoming):
                problems.append('non_numeric_value')
                aggregate = None
            else:
                try:
                    aggregate = _reduce_values(incoming, method)
                    if not is_number(aggregate):
                        raise ValueError('Non-finite aggregate')
                except (ValueError, OverflowError):
                    problems.append('invalid_aggregate')
                    aggregate = None
            values[field] = aggregate
            if target is not None and target[field] is not None and target[field] != aggregate:
                problems.append('existing_value')
            changes.append({'field': field, 'source': json_value(incoming[0]),
                            'inputs': [json_value(value) for value in incoming[:10]],
                            'input_count': len(incoming), 'reducer': method,
                            'before': json_value(target[field]) if target else None,
                            'after': json_value(aggregate)})
        retained_by_source = [[field for field in archive.observations
                               if field not in fields and source[field] is not None]
                              for source in sources]
        retained = sorted({field for remaining in retained_by_source for field in remaining})
        status = 'blocked' if problems else 'ready'
        totals['candidates'] += len(sources)
        totals['destinations'] += 1
        totals[status] += len(sources)
        totals[status + '_destinations'] += 1
        for problem in set(problems):
            reasons[problem] += len(sources)
        entry = {'source_timestamp': source_times[0], 'source_timestamps': source_times[:10],
                 'source_count': len(sources), 'target_timestamp': target_time,
                 'offset_seconds': source_times[0] - target_time if target_time is not None else None,
                 'status': status, 'reasons': sorted(set(problems)),
                 'retained_fields': retained, 'changes': changes,
                 'deleted_source_count': sum(not remaining for remaining in retained_by_source)
                 if not problems else 0,
                 'retained_source_count': sum(bool(remaining) for remaining in retained_by_source)
                 if not problems else 0}
        priority = _preview_priority(entry)
        if len(preview) < 200:
            preview_slots[priority].append(len(preview))
            preview.append(entry)
        elif priority:
            # A long import must still show examples of conflicts and actual multi-reading reductions.
            for lower_priority in range(priority):
                if preview_slots[lower_priority]:
                    replacement = preview_slots[lower_priority].pop()
                    preview[replacement] = entry
                    preview_slots[priority].append(replacement)
                    break
        if not problems:
            for source, remaining in zip(sources, retained_by_source):
                operations.append({'source_timestamp': source[archive.timestamp],
                                   'target_timestamp': target_time,
                                   'values': values, 'delete_source': not remaining})
                totals['source_rows_deleted' if not remaining else 'source_rows_retained'] += 1
                totals['values_transferred'] += sum(source[field] is not None for field in fields)
    return {'version': __version__, 'options': options, 'plan_id': digest.hexdigest(),
            'totals': {key: totals[key] for key in (
                'candidates', 'destinations', 'ready', 'ready_destinations', 'blocked',
                'blocked_destinations', 'source_rows_deleted',
                'source_rows_retained', 'values_transferred')},
            'reasons': dict(reasons),
            'preview': sorted(preview, key=lambda entry: (-_preview_priority(entry), entry['source_timestamp'])),
            'preview_limit': 200, 'operations': operations}


def public_plan(plan):
    """Return a dry-run report without the full execution list."""
    return {key: value for key, value in plan.items() if key != 'operations'}


def _archive_day(timestamp):
    parts = time.localtime(timestamp - 1)
    return int(time.mktime((parts.tm_year, parts.tm_mon, parts.tm_mday, 0, 0, 0, 0, 0, -1)))


def _next_day(timestamp):
    parts = time.localtime(timestamp)
    day = datetime.date(parts.tm_year, parts.tm_mon, parts.tm_mday) + datetime.timedelta(days=1)
    return int(time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1)))


def _rebuild_summaries(connection, archive, operations, fields):
    days = {_archive_day(operation[key]) for operation in operations
            for key in ('source_timestamp', 'target_timestamp')}
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if set(fields).intersection(('windSpeed', 'windDir', 'windGust', 'windGustDir')) \
            and archive.table + '_day_wind' in tables:
        raise ValueError('Wind vector summaries require the WeeWX database tools.')
    metadata = archive.table + '_day__metadata'
    version = '1.0'
    if metadata in tables:
        row = connection.execute('SELECT value FROM %s WHERE name = ?' % (
            quote_identifier(metadata)), ('Version',)).fetchone()
        if row is not None:
            version = str(row[0])
    rebuilt = []
    summary_columns = ['dateTime', 'min', 'mintime', 'max', 'maxtime',
                       'sum', 'count', 'wsum', 'sumtime']
    for field in fields:
        table = archive.table + '_day_' + field
        if table not in tables:
            continue
        actual = {row[1] for row in connection.execute(
            'PRAGMA table_info(%s)' % quote_identifier(table))}
        if actual != set(summary_columns):
            raise ValueError('Unsupported daily summary layout for %s.' % field)
        for day in sorted(days):
            records = connection.execute('SELECT %s, %s, %s, %s FROM %s '
                                         'WHERE %s > ? AND %s <= ? ORDER BY %s' % (
                quote_identifier(archive.timestamp), quote_identifier(field),
                quote_identifier(archive.interval), quote_identifier(archive.units),
                quote_identifier(archive.table),
                quote_identifier(archive.timestamp), quote_identifier(archive.timestamp),
                quote_identifier(archive.timestamp)), (day, _next_day(day)))
            values = [row for row in records if row[1] is not None]
            if any(not is_number(row[1]) or not is_number(row[2]) or row[2] <= 0
                   for row in values):
                raise ValueError('Invalid value or interval in the daily summary for %s.' % field)
            units = {row[3] for row in values}
            if len(units) > 1 or not units.issubset({1, 16, 17}):
                raise ValueError('Mixed or invalid unit systems in the daily summary for %s.' % field)
            connection.execute('DELETE FROM %s WHERE dateTime = ?' % quote_identifier(table), (day,))
            if values:
                minimum = min(values, key=lambda row: row[1])
                maximum = max(values, key=lambda row: row[1])
                weights = [row[2] * 60 if version >= '2.0' else 1 for row in values]
                result = [day, minimum[1], minimum[0], maximum[1], maximum[0],
                          sum(row[1] for row in values), len(values),
                          sum(row[1] * weight for row, weight in zip(values, weights)),
                          sum(weights)]
                if any(not is_number(value) for value in result):
                    raise ValueError('Daily summary arithmetic overflow for %s.' % field)
                connection.execute('INSERT INTO %s (%s) VALUES (%s)' % (
                    quote_identifier(table), ','.join(quote_identifier(name)
                                                    for name in summary_columns),
                    ','.join('?' for name in summary_columns)), result)
            rebuilt.append({'field': field, 'day': day})
    if metadata in tables and operations:
        last_timestamp = connection.execute('SELECT MAX(%s) FROM %s' % (
            quote_identifier(archive.timestamp), quote_identifier(archive.table))).fetchone()[0]
        connection.execute('UPDATE %s SET value = ? WHERE name = ?' % quote_identifier(metadata),
                           (str(last_timestamp), 'lastUpdate'))
    return rebuilt


def export_copy(archive, plan, output, allow_blocked=False, backup_directory=None,
                source_name='archive.sdb'):
    """Validate a dry run again and apply it only to a newly created database.

    Args:
        archive (Archive): Read-only archive against which to revalidate the plan.
        plan (dict): A previous dry run or saved public dry-run report.
        output (str): Destination path, which must not already exist.
        allow_blocked (bool): Explicitly export only ready rows when some rows are blocked.
        backup_directory (str|None): Permanent backup directory. Defaults to archivecheck-backups
            beside the output file.
        source_name (str): Original source display name for the backup.

    Returns:
        dict: Export totals, rebuilt daily summaries and the verified backup metadata.
    """
    options = plan['options']
    if (options['table'], options['start'], options['stop']) != (
            archive.table, archive.start, archive.stop):
        raise ValueError('The dry run belongs to a different table or date range.')
    current = plan_merge(archive, options['fields'], options['anchor'], options['interval'],
                         options['tolerance'], options['direction'], options.get('reducer', 'reject'),
                         options.get('reducers'))
    if current['plan_id'] != plan['plan_id']:
        raise ValueError('The archive or timezone has changed. Run a new dry run.')
    if current['totals']['blocked'] and not allow_blocked:
        raise ValueError('The dry run has blocked rows. Explicitly choose to export ready rows only.')
    if not current['operations']:
        raise ValueError('The dry run has no ready rows to export.')
    if [row[0] for row in archive.connection.execute('PRAGMA quick_check')] != ['ok']:
        raise ValueError('The source database failed its integrity check.')
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    backup = None
    try:
        directory = backup_directory if backup_directory is not None else os.path.join(
            os.path.dirname(os.path.abspath(output)), 'archivecheck-backups')
        backup = create_backup(archive, directory, source_name)
        with contextlib.closing(sqlite3.connect(output)) as destination:
            # The correction starts from the saved backup, so the two files share the same baseline.
            with Archive(backup['path'], archive.table, archive.start, archive.stop) as saved:
                reviewed = plan_merge(saved, options['fields'], options['anchor'], options['interval'],
                                      options['tolerance'], options['direction'],
                                      options.get('reducer', 'reject'), options.get('reducers'))
                if reviewed['plan_id'] != current['plan_id']:
                    raise ValueError('The database changed while creating the backup. Run a new dry run.')
                copy_database(saved.connection, destination)
            with destination:
                for operation in current['operations']:
                    values = operation['values']
                    destination.execute('UPDATE %s SET %s WHERE %s = ?' % (
                        quote_identifier(archive.table), ', '.join('%s = ?' % quote_identifier(field)
                                                                 for field in values),
                        quote_identifier(archive.timestamp)),
                        list(values.values()) + [operation['target_timestamp']])
                    if operation['delete_source']:
                        destination.execute('DELETE FROM %s WHERE %s = ?' % (
                            quote_identifier(archive.table), quote_identifier(archive.timestamp)),
                            (operation['source_timestamp'],))
                    else:
                        destination.execute('UPDATE %s SET %s WHERE %s = ?' % (
                            quote_identifier(archive.table), ', '.join('%s = NULL' % quote_identifier(field)
                                                                     for field in values),
                            quote_identifier(archive.timestamp)), (operation['source_timestamp'],))
                rebuilt = _rebuild_summaries(destination, archive, current['operations'], options['fields'])
                if list(destination.execute('PRAGMA foreign_key_check')):
                    raise ValueError('The corrected copy failed its foreign key check.')
                if [row[0] for row in destination.execute('PRAGMA quick_check')] != ['ok']:
                    raise ValueError('The corrected copy failed its integrity check.')
        return {'totals': current['totals'], 'daily_summaries_rebuilt': rebuilt,
                'plan_id': current['plan_id'], 'backup': backup}
    except Exception as error:
        os.unlink(output)
        if backup is not None:
            raise ExportError(str(error), backup) from error
        raise
