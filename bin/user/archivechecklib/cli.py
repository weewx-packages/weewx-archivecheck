# Copyright (c) 2026 Manuel Hilgert
# SPDX-License-Identifier: GPL-3.0-only

"""Command-line entry points for archive analysis and reviewed exports."""

import argparse
import contextlib
import datetime
import json
import math
import os
import sqlite3
import sys
import time

from . import __version__
from .archive import Archive, analyze, create_backup, export_copy, field_details, plan_merge, public_plan, snapshot
from .web import render_report, serve


def parse_timestamp(value, end_of_day=False):
    """Parse a Unix timestamp or a UTC date/time.

    Args:
        value (str|None): Epoch seconds, YYYY-mm-dd or YYYY-mm-ddTHH:MM[:SS].
        end_of_day (bool): Include the whole day when only a date was provided.

    Returns:
        int|None: UTC timestamp, or None for an unspecified bound.
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    for pattern in ('%Y-%m-%d', '%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'):
        try:
            parsed = datetime.datetime.strptime(value, pattern)
        except ValueError:
            continue
        timestamp = int(parsed.replace(tzinfo=datetime.timezone.utc).timestamp())
        return timestamp + 86399 if end_of_day and pattern == '%Y-%m-%d' else timestamp
    raise ValueError('Use epoch seconds or a UTC date such as 2026-10-19T12:00:00.')


def resolve_source(namespace):
    """Resolve a direct SQLite path or a configured WeeWX binding."""
    if namespace.database:
        return os.path.expanduser(namespace.database), namespace.table or 'archive'
    try:
        import weecfg
        import weewx.manager
    except ImportError as error:
        raise ValueError('Reading --config requires the WeeWX Python environment; '
                         'alternatively use --database.') from error
    config_path, config = weecfg.read_config(namespace.config)
    try:
        binding = config['DataBindings'][namespace.binding]
        database = weewx.manager.get_database_dict_from_config(config, binding['database'])
    except KeyError as error:
        raise ValueError('The requested database binding is incomplete.') from error
    if database.get('driver') != 'weedb.sqlite':
        raise ValueError('Archivecheck currently supports SQLite archives. Use a SQLite export.')
    return (os.path.join(database.get('SQLITE_ROOT', ''), database['database_name']),
            namespace.table or binding.get('table_name', 'archive'))


def write_text(path, content):
    """Create a report without replacing an existing file."""
    with open(path, 'x', encoding='utf-8') as output:
        output.write(content)
        output.write('\n')


def create_demo(path):
    """Create a synthetic fortnight with split PV records and several known conflicts."""
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.execute('CREATE TABLE archive (dateTime INTEGER PRIMARY KEY, '
                               'usUnits INTEGER, interval INTEGER, outTemp REAL, '
                               'outHumidity REAL, radiation REAL, energyprod REAL, '
                               'power REAL, voltage REAL)')
            start = 1791158400
            energy = 40000000.0
            with connection:
                for index in range(14 * 288):
                    timestamp = start + (index + 1) * 300
                    hour = ((index + 1) % 288) / 12
                    daylight = max(0, math.sin((hour - 6) / 12 * math.pi)) if 6 <= hour <= 18 else 0
                    power = round(4200 * daylight, 1)
                    energy += power / 12
                    weather = [timestamp, 1, 5, round(55 + 12 * daylight, 2),
                               round(75 - 20 * daylight, 1), round(800 * daylight, 1),
                               None, None, None]
                    if index >= 12 * 288:
                        weather[6:] = [round(energy, 2), power, 230.0]
                    if index != 420:
                        connection.execute('INSERT INTO archive VALUES (?,?,?,?,?,?,?,?,?)', weather)
                    if index < 12 * 288:
                        source_time = timestamp + (5 if index % 2 else 6)
                        solar = [source_time, 1, 5,
                                 None, None, None, round(energy, 2), power, 230.0]
                        if index == 840:
                            solar[1] = 16
                        connection.execute('INSERT INTO archive VALUES (?,?,?,?,?,?,?,?,?)', solar)
                        if index == 1000:
                            solar[0] = source_time + 2
                            connection.execute('INSERT INTO archive VALUES (?,?,?,?,?,?,?,?,?)', solar)
                        if index == 1400:
                            connection.execute('UPDATE archive SET energyprod = ? WHERE dateTime = ?',
                                               (energy - 100, timestamp))
    except Exception:
        os.unlink(path)
        raise


def build_parser():
    parser = argparse.ArgumentParser(description='Inspect WeeWX SQLite archives and dry-run scalar merges.')
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command')
    for command in ('analyze', 'serve', 'dry-run', 'export', 'backup'):
        subparser = commands.add_parser(command)
        source = subparser.add_mutually_exclusive_group(required=True)
        source.add_argument('--database', help='Path to a SQLite archive.')
        source.add_argument('--config', help='Path to weewx.conf; uses its SQLite binding.')
        subparser.add_argument('--binding', default='wx_binding')
        subparser.add_argument('--table', help='Archive table; defaults to archive or the configured table.')
        subparser.add_argument('--from', dest='start', help='Inclusive UTC date/time or epoch seconds.')
        subparser.add_argument('--to', dest='stop', help='Inclusive UTC date/time or epoch seconds.')
        subparser.add_argument('--timezone', help='Station timezone for daily summaries, e.g. Europe/Berlin. '
                               'Defaults to this machine\'s local timezone.')
        if command in ('serve', 'export', 'backup'):
            subparser.add_argument('--backup-dir',
                                   help='Permanent backup directory. Defaults to archivecheck-backups '
                                   'beside the source database.')
        if command in ('analyze', 'dry-run'):
            subparser.add_argument('--json', help='Create a shareable JSON report.')
            subparser.add_argument('--html', help='Create a standalone HTML report.')
        if command == 'analyze':
            subparser.add_argument('--field', help='Observation to chart; defaults to energyprod if present.')
        if command == 'serve':
            subparser.add_argument('--host', default='127.0.0.1',
                                   help='IPv4 address to listen on. Defaults to 127.0.0.1.')
            subparser.add_argument('--port', type=int, default=8765)
        if command == 'dry-run':
            subparser.add_argument('--fields', required=True, help='Comma-separated observations to transfer.')
            subparser.add_argument('--anchor', default='outTemp')
            subparser.add_argument('--interval', type=int, default=300, help='Archive grid in seconds.')
            subparser.add_argument('--tolerance', type=int, default=15, help='Maximum timestamp shift in seconds.')
            subparser.add_argument('--direction', choices=('previous', 'next', 'nearest', 'record'), default='previous')
            subparser.add_argument('--reduce', dest='reducer', default='reject',
                                   choices=('reject', 'mean', 'first', 'last', 'sum', 'min', 'max'),
                                   help='Aggregate multiple readings per destination; default: reject.')
            subparser.add_argument('--reducers', default='',
                                   help='Per-field overrides, e.g. energyprod=last,power=mean.')
        if command == 'export':
            subparser.add_argument('--plan', required=True, help='JSON file from a previous dry run.')
            subparser.add_argument('--output', required=True, help='New corrected SQLite file; must not exist.')
            subparser.add_argument('--ready-only', action='store_true',
                                   help='Explicitly leave blocked candidates unchanged.')
    demo = commands.add_parser('demo', help='Create a synthetic example database.')
    demo.add_argument('--output', required=True)
    return parser


def main(argv=None):
    """Run archivecheck with command-line arguments.

    Args:
        argv (list[str]|None): Arguments, or None to use sys.argv.
    """
    parser = build_parser()
    namespace = parser.parse_args(argv)
    if namespace.command is None:
        parser.print_help()
        return
    try:
        if namespace.command == 'demo':
            create_demo(namespace.output)
            print('Created synthetic archive: %s' % namespace.output)
            return
        if namespace.timezone:
            if not hasattr(time, 'tzset'):
                raise ValueError('--timezone requires time.tzset on this platform.')
            # Set this once, before starting HTTP threads. Daily boundaries must use the station timezone.
            if namespace.timezone.startswith('/') or '..' in namespace.timezone:
                raise ValueError('Use a timezone name such as Europe/Berlin.')
            zone_path = os.path.join('/usr/share/zoneinfo', namespace.timezone)
            if not os.path.isfile(zone_path) and namespace.timezone not in ('UTC', 'GMT'):
                raise ValueError('Unknown station timezone %r.' % namespace.timezone)
            os.environ['TZ'] = namespace.timezone
            time.tzset()
        source, table = resolve_source(namespace)
        backup_directory = getattr(namespace, 'backup_dir', None) or os.path.join(
            os.path.dirname(os.path.abspath(source)), 'archivecheck-backups')
        start = parse_timestamp(namespace.start)
        stop = parse_timestamp(namespace.stop, end_of_day=True)
        with snapshot(source) as path:
            if namespace.command == 'serve':
                serve(path, os.path.basename(source), table, start, stop, namespace.port,
                      backup_directory, namespace.host)
                return
            with Archive(path, table, start, stop) as archive:
                if namespace.command == 'backup':
                    result = create_backup(archive, backup_directory, os.path.basename(source))
                    print(json.dumps(result, indent=2, allow_nan=False))
                    return
                if namespace.command == 'export':
                    with open(namespace.plan, encoding='utf-8') as plan_file:
                        saved = json.load(plan_file)
                    plan = saved.get('plan', saved)
                    result = export_copy(archive, plan, namespace.output, namespace.ready_only,
                                         backup_directory, os.path.basename(source))
                    print(json.dumps(result, indent=2, allow_nan=False))
                    return
                report = analyze(archive, os.path.basename(source))
                field = getattr(namespace, 'field', None)
                if field is None:
                    field = 'energyprod' if 'energyprod' in archive.observations else next(
                        iter(archive.observations), None)
                details = field_details(archive, field) if field else None
                plan = None
                if namespace.command == 'dry-run':
                    reducers = {}
                    for specification in namespace.reducers.split(','):
                        if specification.strip():
                            field_name, separator, method = specification.partition('=')
                            if not separator:
                                raise ValueError('Use field=method in --reducers.')
                            reducers[field_name.strip()] = method.strip()
                    plan = public_plan(plan_merge(
                        archive, [name.strip() for name in namespace.fields.split(',') if name.strip()],
                        namespace.anchor, namespace.interval, namespace.tolerance, namespace.direction,
                        namespace.reducer, reducers))
                payload = {'report': report, 'details': details, 'plan': plan}
                encoded = json.dumps(payload, indent=2, allow_nan=False)
                if namespace.json:
                    write_text(namespace.json, encoded)
                if namespace.html:
                    write_text(namespace.html, render_report(payload))
                if not namespace.json and not namespace.html:
                    print(encoded)
                else:
                    print('%s records; %s observations. Reports created.' % (
                        report['records'], len(report['fields'])))
                    if plan is not None:
                        print('Dry run: %s ready, %s blocked. Plan %s' % (
                            plan['totals']['ready'], plan['totals']['blocked'], plan['plan_id']))
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        parser.exit(2, 'archivecheck: %s\n' % error)
    except KeyboardInterrupt:
        print('\nArchivecheck stopped.', file=sys.stderr)
