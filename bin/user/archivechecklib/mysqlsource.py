# Copyright (c) 2026 Manuel Hilgert <hilman2@gmail.com>
# SPDX-License-Identifier: GPL-3.0-only

"""Copy MySQL archives into SQLite snapshots and corrected copies back to MySQL."""

import contextlib
import datetime
import decimal
import os
import re
import sqlite3
import tempfile

from .archive import open_readonly, quote_identifier

BATCH_ROWS = 10000


def quote_mysql(name):
    """Quote a MySQL table, column or database name."""
    return '`%s`' % name.replace('`', '``')


def sqlite_type(data_type):
    """Map a MySQL column type to the SQLite affinity that keeps its values.

    Args:
        data_type (str): DATA_TYPE from information_schema.COLUMNS, e.g., 'double'.

    Returns:
        str: INTEGER, REAL, BLOB or TEXT.
    """
    data_type = data_type.lower()
    if 'int' in data_type:
        return 'INTEGER'
    if data_type in ('double', 'float', 'real', 'decimal', 'numeric'):
        return 'REAL'
    if 'blob' in data_type or 'binary' in data_type:
        return 'BLOB'
    return 'TEXT'


def sqlite_value(value):
    """Convert a value from the MySQL driver to one that SQLite stores."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.time, datetime.timedelta)):
        return str(value)
    if isinstance(value, bytearray):
        return bytes(value)
    return value


def _text(value):
    # MySQL 8 returns some information_schema columns as bytes.
    return value.decode('utf-8') if isinstance(value, bytes) else value


def _driver_errors():
    import weedb
    import weedb.mysql
    return weedb.DatabaseError, weedb.mysql.MySQLdb.Error


@contextlib.contextmanager
def connect(database):
    """Open the raw MySQL driver connection for a WeeWX database dictionary.

    The weedb cursor turns DECIMAL values into integers and rewrites the word
    interval in statements, so the queries here use the driver's own cursor.

    Args:
        database (dict): WeeWX database dictionary with driver weedb.mysql.

    Yields:
        object: DB-API connection of mysqlclient or PyMySQL.
    """
    try:
        import weedb
        errors = _driver_errors()
    except ImportError as error:
        raise ValueError('Reading MySQL archives requires the MySQL driver WeeWX uses: '
                         'mysqlclient or PyMySQL.') from error
    try:
        connection = weedb.connect(database)
    except errors as error:
        raise ValueError('Cannot connect to MySQL database %r: %s' % (
            database['database_name'], error)) from error
    try:
        yield connection.connection
    except errors as error:
        raise ValueError('MySQL error: %s' % error) from error
    finally:
        connection.close()


def archive_tables(cursor, table):
    """List the archive table and its daily summary tables in the current database.

    Args:
        cursor (object): Driver cursor of a connection to the source database.
        table (str): Archive table name.

    Returns:
        list[str]: The archive table first, then the daily summary tables.
    """
    cursor.execute('SELECT TABLE_NAME FROM information_schema.TABLES '
                   'WHERE TABLE_SCHEMA = DATABASE()')
    names = [_text(row[0]) for row in cursor.fetchall()]
    if table not in names:
        raise ValueError('Archive table %r does not exist.' % table)
    return [table] + sorted(name for name in names if name.startswith(table + '_day_'))


def _columns(cursor, table):
    cursor.execute('SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM information_schema.COLUMNS '
                   'WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION',
                   (table,))
    columns = [(_text(name), _text(data_type), _text(nullable))
               for name, data_type, nullable in cursor.fetchall()]
    cursor.execute('SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE '
                   "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND CONSTRAINT_NAME = 'PRIMARY' "
                   'ORDER BY ORDINAL_POSITION', (table,))
    return columns, [_text(row[0]) for row in cursor.fetchall()]


def _create_statement(table, columns, primary):
    definitions = []
    for name, data_type, nullable in columns:
        definition = '%s %s' % (quote_identifier(name), sqlite_type(data_type))
        if nullable == 'NO':
            definition += ' NOT NULL'
        if primary == [name]:
            definition += ' PRIMARY KEY'
        definitions.append(definition)
    if len(primary) > 1:
        definitions.append('PRIMARY KEY (%s)' % ', '.join(quote_identifier(name) for name in primary))
    return 'CREATE TABLE %s (%s)' % (quote_identifier(table), ', '.join(definitions))


def _copy_rows(cursor, destination, table, names, primary):
    selected = ', '.join(quote_mysql(name) for name in names)
    insert = 'INSERT INTO %s VALUES (%s)' % (quote_identifier(table), ','.join('?' for name in names))
    if len(primary) != 1:
        cursor.execute('SELECT %s FROM %s' % (selected, quote_mysql(table)))
        destination.executemany(insert, ([sqlite_value(value) for value in row]
                                         for row in cursor.fetchall()))
        return
    # Read the table in key order, a batch at a time, so a large archive is not
    # held in memory at once. The open transaction keeps all batches consistent.
    key = names.index(primary[0])
    query = 'SELECT %s FROM %s %%s ORDER BY %s LIMIT %d' % (
        selected, quote_mysql(table), quote_mysql(primary[0]), BATCH_ROWS)
    cursor.execute(query % '')
    while True:
        rows = cursor.fetchall()
        if not rows:
            return
        destination.executemany(insert, ([sqlite_value(value) for value in row] for row in rows))
        cursor.execute(query % ('WHERE %s > %%s' % quote_mysql(primary[0])), (rows[-1][key],))


@contextlib.contextmanager
def snapshot(database, table):
    """Copy a MySQL archive table and its daily summaries to a temporary SQLite database.

    The copy reads all tables in one InnoDB snapshot, so records the station
    writes meanwhile appear either in every table or in none.

    Args:
        database (dict): WeeWX database dictionary with driver weedb.mysql.
        table (str): Archive table name.

    Yields:
        str: Path to the snapshot, removed on leaving the context.
    """
    with tempfile.TemporaryDirectory(prefix='weewx-archivecheck-') as directory:
        path = os.path.join(directory, 'snapshot.sdb')
        with connect(database) as source, contextlib.closing(sqlite3.connect(path)) as destination:
            cursor = source.cursor()
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            try:
                tables = archive_tables(cursor, table)
                cursor.execute('SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE FROM information_schema.TRIGGERS '
                               'WHERE TRIGGER_SCHEMA = DATABASE()')
                triggers = sorted(_text(name) for name, target in cursor.fetchall()
                                  if _text(target) in tables)
                if triggers:
                    # SQLite cannot run MySQL triggers. Without them, a corrected copy
                    # would silently differ from what the station database does.
                    raise ValueError('The MySQL archive has triggers (%s). Review them before '
                                     'using Archivecheck.' % ', '.join(triggers))
                with destination:
                    for name in tables:
                        columns, primary = _columns(cursor, name)
                        destination.execute(_create_statement(name, columns, primary))
                        _copy_rows(cursor, destination, name, [column[0] for column in columns], primary)
            finally:
                source.rollback()
        yield path


def copy_to_mysql(path, database, table, target):
    """Copy a corrected SQLite archive into a new set of tables in an empty MySQL database.

    The target tables are created with the definitions of the source database's
    tables, so column types, keys and the storage engine match the station.

    Args:
        path (str): Corrected SQLite archive from an export.
        database (dict): WeeWX database dictionary of the source MySQL archive.
        table (str): Archive table name.
        target (str): Existing, empty database on the same server and account.

    Returns:
        dict: Target database name and the number of rows per table.
    """
    if not re.match(r'^[A-Za-z0-9_$]+$', target):
        raise ValueError('Use a database name of letters, digits, _ and $.')
    if target == database['database_name']:
        raise ValueError('The target must be a different database than the station archive.')
    with contextlib.closing(open_readonly(path)) as corrected:
        tables = [table] + sorted(row[0] for row in corrected.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND substr(name, 1, ?) = ?",
            (len(table) + 5, table + '_day_')))
        if corrected.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                             (table,)).fetchone() is None:
            raise ValueError('Archive table %r does not exist in %s.' % (table, path))
        with connect(database) as source:
            cursor = source.cursor()
            missing = set(tables) - set(archive_tables(cursor, table))
            if missing:
                raise ValueError('The station database has no table %s.' % ', '.join(sorted(missing)))
            statements = []
            for name in tables:
                cursor.execute('SHOW CREATE TABLE %s' % quote_mysql(name))
                statements.append(_text(cursor.fetchone()[1]))
        destination_database = dict(database, database_name=target)
        with connect(destination_database) as destination:
            cursor = destination.cursor()
            cursor.execute('SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()')
            if cursor.fetchone()[0]:
                raise ValueError('The target database %r is not empty.' % target)
            created = []
            counts = {}
            try:
                for name, statement in zip(tables, statements):
                    cursor.execute(statement)
                    created.append(name)
                    names = [row[1] for row in corrected.execute(
                        'PRAGMA table_info(%s)' % quote_identifier(name))]
                    insert = 'INSERT INTO %s (%s) VALUES (%s)' % (
                        quote_mysql(name), ', '.join(quote_mysql(column) for column in names),
                        ', '.join('%s' for column in names))
                    rows = corrected.execute('SELECT %s FROM %s' % (
                        ', '.join(quote_identifier(column) for column in names), quote_identifier(name)))
                    cursor.execute('START TRANSACTION')
                    while True:
                        batch = rows.fetchmany(BATCH_ROWS)
                        if not batch:
                            break
                        cursor.executemany(insert, [tuple(row) for row in batch])
                    destination.commit()
                    cursor.execute('SELECT COUNT(*) FROM %s' % quote_mysql(name))
                    counts[name] = cursor.fetchone()[0]
                    expected = corrected.execute('SELECT COUNT(*) FROM %s' % quote_identifier(name)).fetchone()[0]
                    if counts[name] != expected:
                        raise ValueError('Table %s has %d rows in MySQL, expected %d.' % (
                            name, counts[name], expected))
            except Exception:
                destination.rollback()
                # DDL commits on its own in MySQL. Remove the tables this call
                # created so that a retry starts from an empty database again.
                for name in reversed(created):
                    cursor.execute('DROP TABLE %s' % quote_mysql(name))
                raise
    return {'database': target, 'rows': counts}
