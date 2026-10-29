# Copyright (c) 2026 Manuel Hilgert
# SPDX-License-Identifier: GPL-3.0-only

"""Exercise archive corrections against conflicts and unexpected source data."""

import contextlib
import hashlib
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time

import pytest

import user.archivechecklib.archive as archive_tools
import user.archivechecklib.cli as archive_cli
import user.archivechecklib.web as archive_web


@pytest.fixture(autouse=True)
def utc_timezone():
    previous = os.environ.get('TZ')
    os.environ['TZ'] = 'UTC'
    time.tzset()
    yield
    if previous is None:
        os.environ.pop('TZ', None)
    else:
        os.environ['TZ'] = previous
    time.tzset()


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / 'weather.sdb')
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE archive (dateTime INTEGER PRIMARY KEY, usUnits INTEGER, '
                           'interval INTEGER, outTemp REAL, energyprod REAL, power REAL, note TEXT)')
        connection.executemany('INSERT INTO archive VALUES (?,1,5,?,?,?,?)', [
            (300, 57.9, None, None, None), (306, None, 100.0, 0.0, None),
            (600, 57.8, None, None, None), (605, None, 110.0, 120.0, None),
            (900, 57.6, None, None, None), (905, None, 120.0, 120.0, None),
            (1200, 57.4, None, None, None), (1206, None, 120.0, 0.0, None)])
    return path


def modify(path, statement, parameters=()):
    with sqlite3.connect(path) as connection:
        connection.execute(statement, parameters)


def digest(path):
    with open(path, 'rb') as source:
        return hashlib.sha256(source.read()).hexdigest()


def plan_for(path, **options):
    with archive_tools.Archive(path) as archive:
        return archive_tools.plan_merge(archive, ['energyprod', 'power'], **options)


def test_report_describes_interleaved_records_and_real_schema(database):
    before = digest(database)
    with archive_tools.Archive(database) as archive:
        report = archive_tools.analyze(archive)
        assert report['records'] == 8
        assert report['interval_seconds'] == 300
        assert report['offsets'] == [{'seconds': 0, 'count': 4},
                                     {'seconds': 5, 'count': 2}, {'seconds': 6, 'count': 2}]
        assert next(field for field in report['fields'] if field['name'] == 'energyprod')['count'] == 4
        assert next(field for field in report['fields'] if field['name'] == 'note')['missing'] == 8
        assert report['integrity'] == ['ok']
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            archive.connection.execute('DELETE FROM archive')
    assert digest(database) == before


def test_alignment_distinguishes_weather_and_imported_values_including_zero(database):
    modify(database, 'UPDATE archive SET energyprod = 0 WHERE dateTime = 300')
    with archive_tools.Archive(database) as archive:
        report = archive_tools.analyze(archive)
    fields = {field['name']: field for field in report['fields']}
    assert fields['energyprod']['count'] == 5
    assert fields['energyprod']['on_grid'] == 1
    assert fields['energyprod']['off_grid'] == 4
    assert fields['outTemp']['on_grid'] == 4
    assert fields['outTemp']['off_grid'] == 0
    assert fields['note']['on_grid'] == fields['note']['off_grid'] == 0
    examples = report['alignment_examples']
    assert [row['timestamp'] for row in examples] == [300, 306, 600, 605, 900, 905]
    assert examples[0]['values'] == {'outTemp': 57.9, 'energyprod': 0}
    assert examples[1]['values'] == {'energyprod': 100, 'power': 0}
    assert examples[1]['offset_seconds'] == 6


def test_alignment_examples_and_counts_respect_the_selected_dates(database):
    with archive_tools.Archive(database, start=605, stop=905) as archive:
        report = archive_tools.analyze(archive)
    assert [row['timestamp'] for row in report['alignment_examples']] == [605, 900, 905]
    fields = {field['name']: field for field in report['fields']}
    assert fields['energyprod']['on_grid'] == 0
    assert fields['energyprod']['off_grid'] == 2
    assert fields['outTemp']['on_grid'] == 1


def test_alignment_is_unknown_without_an_archive_interval(database):
    modify(database, 'UPDATE archive SET interval = NULL')
    with archive_tools.Archive(database) as archive:
        report = archive_tools.analyze(archive)
    assert report['interval_seconds'] is None
    assert report['alignment_examples'] == []
    assert all(field['on_grid'] is None and field['off_grid'] is None
               for field in report['fields'])


def test_snapshot_includes_committed_wal_records(database):
    with contextlib.closing(sqlite3.connect(database)) as writer:
        writer.execute('PRAGMA journal_mode = WAL')
        writer.execute('INSERT INTO archive (dateTime, usUnits, interval, energyprod) VALUES (1505,1,5,130)')
        writer.commit()
        with archive_tools.snapshot(database) as path:
            with archive_tools.Archive(path) as archive:
                assert archive.record(1505)['energyprod'] == 130
        assert not os.path.exists(path)


def test_empty_archive_and_bounded_range(database):
    with archive_tools.Archive(database, start=700, stop=1000) as archive:
        assert archive_tools.analyze(archive)['records'] == 2
    with archive_tools.Archive(database, start=2000) as archive:
        assert archive_tools.analyze(archive)['records'] == 0
        assert archive_tools.field_details(archive, 'energyprod')['buckets'] == []
    with pytest.raises(ValueError, match='start'):
        archive_tools.Archive(database, start=1000, stop=100)


def test_unknown_field_and_table_are_rejected(database):
    with archive_tools.Archive(database) as archive:
        with pytest.raises(ValueError, match='Unknown observation'):
            archive_tools.field_details(archive, 'outTemp; DROP TABLE archive')
    with pytest.raises(ValueError, match='does not exist'):
        archive_tools.Archive(database, 'missing')


def test_quoted_custom_table_and_column(database):
    modify(database, 'ALTER TABLE archive RENAME TO "archive""custom"')
    modify(database, 'ALTER TABLE "archive""custom" ADD COLUMN "extra""sensor" REAL')
    with archive_tools.Archive(database, 'archive"custom') as archive:
        assert archive_tools.analyze(archive)['records'] == 8
        assert archive_tools.field_details(archive, 'extra"sensor')['count'] == 0
        assert archive_tools.plan_merge(archive, ['energyprod'])['totals']['ready'] == 4


def test_field_details_preserve_zero_and_distinguish_invalid_values(database):
    modify(database, 'UPDATE archive SET energyprod = 0 WHERE dateTime = 306')
    modify(database, "UPDATE archive SET energyprod = 'broken' WHERE dateTime = 905")
    modify(database, 'UPDATE archive SET energyprod = ? WHERE dateTime = 1206', (float('inf'),))
    with archive_tools.Archive(database) as archive:
        details = archive_tools.field_details(archive, 'energyprod')
        assert details['first'] == {'timestamp': 306, 'value': 0.0}
        assert details['numeric_count'] == 2
        assert details['invalid_count'] == 2
        assert details['difference'] is None
        assert any(bucket['records'] == 0 for bucket in details['buckets'])
        json.dumps(details, allow_nan=False)


def test_decreases_are_measured_without_assuming_resets(database):
    modify(database, 'UPDATE archive SET energyprod = 10 WHERE dateTime = 905')
    with archive_tools.Archive(database) as archive:
        details = archive_tools.field_details(archive, 'energyprod')
        assert details['decreases'] == 1
        assert details['difference'] == 20
        assert details['decrease_examples'][0]['before']['timestamp'] == 605


def test_dry_run_and_export_preserve_weather_and_zero_readings(database, tmp_path):
    before = digest(database)
    output = str(tmp_path / 'corrected.sdb')
    plan = plan_for(database)
    assert plan['totals']['ready'] == 4
    assert plan['totals']['source_rows_deleted'] == 4
    assert plan['totals']['values_transferred'] == 8
    with archive_tools.Archive(database) as archive:
        result = archive_tools.export_copy(archive, plan, output)
    assert result['plan_id'] == plan['plan_id']
    assert digest(database) == before
    with archive_tools.Archive(output) as archive:
        assert archive_tools.analyze(archive)['records'] == 4
        assert archive.record(300)['outTemp'] == 57.9
        assert archive.record(300)['power'] == 0
        assert archive.record(600)['energyprod'] == 110
        assert archive.record(306) is None


@pytest.mark.parametrize('statement,reason', [
    ('DELETE FROM archive WHERE dateTime = 300', 'missing_target'),
    ('UPDATE archive SET energyprod = 99 WHERE dateTime = 300', 'existing_value'),
    ('UPDATE archive SET outTemp = 0 WHERE dateTime = 306', 'source_contains_weather'),
    ('UPDATE archive SET outTemp = NULL WHERE dateTime = 300', 'target_has_no_anchor'),
    ('UPDATE archive SET usUnits = 16 WHERE dateTime = 306', 'unit_mismatch'),
    ('UPDATE archive SET usUnits = NULL WHERE dateTime IN (300,306)', 'unit_mismatch'),
    ('UPDATE archive SET interval = 1 WHERE dateTime = 306', 'interval_mismatch'),
    ("UPDATE archive SET energyprod = 'bad' WHERE dateTime = 306", 'non_numeric_value'),
    ('INSERT INTO archive VALUES (307,1,5,NULL,100,0,NULL)', 'multiple_sources')])
def test_conflicts_block_the_whole_source_record(database, statement, reason):
    modify(database, statement)
    plan = plan_for(database)
    entry = next(entry for entry in plan['preview'] if entry['source_timestamp'] == 306)
    assert entry['status'] == 'blocked'
    assert reason in entry['reasons']
    assert not any(operation['source_timestamp'] == 306 for operation in plan['operations'])


def test_equal_destination_and_null_source_do_not_erase_values(database, tmp_path):
    modify(database, 'UPDATE archive SET energyprod = 100, power = 7 WHERE dateTime = 300')
    modify(database, 'UPDATE archive SET power = NULL WHERE dateTime = 306')
    plan = plan_for(database)
    assert plan['totals']['ready'] == 4
    output = str(tmp_path / 'corrected.sdb')
    with archive_tools.Archive(database) as archive:
        archive_tools.export_copy(archive, plan, output)
    with archive_tools.Archive(output) as corrected:
        assert corrected.record(300)['power'] == 7
        assert corrected.record(300)['energyprod'] == 100


def test_remaining_observations_keep_the_source_row(database, tmp_path):
    modify(database, 'UPDATE archive SET note = ? WHERE dateTime = 306', ('retain me',))
    plan = plan_for(database)
    assert plan['totals']['source_rows_retained'] == 1
    output = str(tmp_path / 'retained.sdb')
    with archive_tools.Archive(database) as archive:
        archive_tools.export_copy(archive, plan, output)
    with archive_tools.Archive(output) as corrected:
        source = corrected.record(306)
        assert source['note'] == 'retain me'
        assert source['energyprod'] is None
        assert source['power'] is None


def test_selected_fields_alone_do_not_delete_other_solar_values(database):
    with archive_tools.Archive(database) as archive:
        plan = archive_tools.plan_merge(archive, ['energyprod'])
    assert plan['totals']['source_rows_retained'] == 4
    assert all(entry['retained_fields'] == ['power'] for entry in plan['preview'])


def test_direction_tolerance_ties_and_date_limits(database):
    assert plan_for(database, tolerance=5)['totals']['ready'] == 2
    assert plan_for(database, direction='next')['totals']['ready'] == 0
    assert plan_for(database, direction='nearest')['totals']['ready'] == 4
    modify(database, 'UPDATE archive SET dateTime = 450 WHERE dateTime = 306')
    plan = plan_for(database, direction='nearest', tolerance=160)
    assert 'equidistant_boundaries' in next(entry for entry in plan['preview']
                                          if entry['source_timestamp'] == 450)['reasons']
    with archive_tools.Archive(database, start=605) as archive:
        bounded = archive_tools.plan_merge(archive, ['energyprod', 'power'])
    assert 'target_outside_range' in bounded['preview'][0]['reasons']


def test_stale_dry_run_and_forged_operations_cannot_be_applied(database, tmp_path):
    plan = plan_for(database)
    modify(database, 'UPDATE archive SET note = ? WHERE dateTime = 300', ('changed since review',))
    output = str(tmp_path / 'stale.sdb')
    with archive_tools.Archive(database) as archive:
        with pytest.raises(ValueError, match='changed'):
            archive_tools.export_copy(archive, plan, output)
    assert not os.path.exists(output)
    fresh = plan_for(database)
    fresh['operations'][0]['values']['outTemp'] = -999
    with archive_tools.Archive(database) as archive:
        archive_tools.export_copy(archive, fresh, output)
    with archive_tools.Archive(output) as archive:
        assert archive.record(300)['outTemp'] == 57.9


def test_exports_require_explicit_handling_of_blocked_rows(database, tmp_path):
    modify(database, 'UPDATE archive SET energyprod = 99 WHERE dateTime = 300')
    output = str(tmp_path / 'partial.sdb')
    plan = plan_for(database)
    with archive_tools.Archive(database) as archive:
        with pytest.raises(ValueError, match='blocked'):
            archive_tools.export_copy(archive, plan, output)
        archive_tools.export_copy(archive, plan, output, allow_blocked=True)
    with archive_tools.Archive(output) as corrected:
        assert corrected.record(300)['energyprod'] == 99
        assert corrected.record(306)['energyprod'] == 100
        assert corrected.record(600)['energyprod'] == 110


def test_existing_output_is_never_replaced(database, tmp_path):
    output = tmp_path / 'existing.sdb'
    output.write_bytes(b'keep this')
    with archive_tools.Archive(database) as archive:
        with pytest.raises(FileExistsError):
            archive_tools.export_copy(archive, plan_for(database), str(output))
    assert output.read_bytes() == b'keep this'


def make_summaries(database):
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE archive_day__metadata (name TEXT PRIMARY KEY, value TEXT)')
        connection.executemany('INSERT INTO archive_day__metadata VALUES (?,?)',
                               [('Version', '4.0'), ('lastUpdate', '1206')])
        for field in ('energyprod', 'power', 'outTemp'):
            connection.execute('CREATE TABLE archive_day_%s (dateTime INTEGER PRIMARY KEY, '
                               'min REAL, mintime INTEGER, max REAL, maxtime INTEGER, '
                               'sum REAL, count INTEGER, wsum REAL, sumtime REAL)' % field)
            connection.execute('INSERT INTO archive_day_%s VALUES (0,1,1,999,2,1,1,1,1)' % field)


def test_only_affected_scalar_summaries_are_rebuilt(database, tmp_path):
    make_summaries(database)
    output = str(tmp_path / 'summaries.sdb')
    with archive_tools.Archive(database) as archive:
        result = archive_tools.export_copy(archive, plan_for(database), output)
    assert len(result['daily_summaries_rebuilt']) == 2
    with sqlite3.connect(output) as connection:
        row = connection.execute('SELECT * FROM archive_day_energyprod').fetchone()
        assert row == (0, 100, 300, 120, 900, 450, 4, 135000, 1200)
        assert connection.execute('SELECT max FROM archive_day_outTemp').fetchone() == (999,)
        assert connection.execute("SELECT value FROM archive_day__metadata WHERE name = 'lastUpdate'").fetchone() == ('1200',)


def test_unsupported_summaries_remove_the_failed_export(database, tmp_path):
    make_summaries(database)
    modify(database, 'ALTER TABLE archive_day_energyprod ADD COLUMN unexpected REAL')
    output = str(tmp_path / 'unsupported.sdb')
    with archive_tools.Archive(database) as archive:
        with pytest.raises(ValueError, match='Unsupported daily'):
            archive_tools.export_copy(archive, plan_for(database), output)
    assert not os.path.exists(output)


def test_station_day_boundaries_follow_daylight_saving_changes():
    os.environ['TZ'] = 'Europe/Berlin'
    time.tzset()
    spring = int(time.mktime((2026, 3, 29, 0, 0, 0, 0, 0, -1)))
    autumn = int(time.mktime((2026, 10, 25, 0, 0, 0, 0, 0, -1)))
    assert archive_tools._next_day(spring) - spring == 23 * 3600
    assert archive_tools._next_day(autumn) - autumn == 25 * 3600
    assert archive_tools._archive_day(spring) < spring
    assert archive_tools._archive_day(spring + 1) == spring


def test_late_conflicts_are_visible_in_a_bounded_preview(database):
    with sqlite3.connect(database) as connection:
        for index in range(5, 230):
            connection.execute('INSERT INTO archive VALUES (?,1,5,10,NULL,NULL,NULL)', (index * 300,))
            connection.execute('INSERT INTO archive VALUES (?,1,5,NULL,150,0,NULL)', (index * 300 + 5,))
        connection.execute('UPDATE archive SET energyprod = 42 WHERE dateTime = 68700')
    plan = plan_for(database)
    assert len(plan['preview']) == 200
    assert plan['totals']['blocked'] == 1
    assert any(entry['status'] == 'blocked' for entry in plan['preview'])


def test_standalone_report_escapes_database_values(database):
    with archive_tools.Archive(database) as archive:
        payload = {'report': archive_tools.analyze(archive, '</script><script>alert(1)</script>'),
                   'details': archive_tools.field_details(archive, 'energyprod'), 'plan': None}
    document = archive_web.render_report(payload)
    assert '</script><script>alert(1)</script>' not in document
    assert '\\u003c/script>' in document
    assert '<script src="/app.js"' not in document
    assert '<link rel="stylesheet" href="/style.css">' not in document


@pytest.fixture
def local_server(database, tmp_path):
    server = archive_web.make_server(database, 'weather.sdb', port=0, directory=str(tmp_path))
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    yield server
    server.shutdown()
    worker.join(timeout=5)
    server.server_close()


def http_request(server, method, path, payload=None, headers=None, address='127.0.0.1'):
    connection = http.client.HTTPConnection(address, server.server_port, timeout=10)
    request_headers = {'X-Archive-Token': server.session_token}
    if headers:
        request_headers.update(headers)
    connection.request(method, path, body=json.dumps(payload) if payload is not None else None,
                       headers=request_headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_http_dry_run_export_and_download(local_server):
    status, headers, body = http_request(local_server, 'GET', '/')
    assert status == 200
    assert 'frame-ancestors' in headers['Content-Security-Policy']
    assert local_server.session_token.encode() in body
    assert http_request(local_server, 'POST', '/api/export', {'plan_id': 'missing'})[0] == 400
    status, headers, body = http_request(local_server, 'POST', '/api/plan', {
        'fields': ['energyprod', 'power'], 'anchor': 'outTemp',
        'interval': 300, 'tolerance': 15, 'direction': 'previous'})
    plan = json.loads(body)
    assert status == 200 and plan['totals']['ready'] == 4
    assert 'operations' not in plan
    status, headers, body = http_request(local_server, 'POST', '/api/export', {'plan_id': plan['plan_id']})
    result = json.loads(body)
    assert status == 200 and result['after']['records'] == 4
    status, headers, body = http_request(local_server, 'GET', result['download'])
    assert status == 200 and body.startswith(b'SQLite format 3')
    assert http_request(local_server, 'GET', '/api/report.html?field=energyprod')[0] == 200


@pytest.mark.parametrize('headers', [
    {'Host': 'attacker.example'}, {'Origin': 'https://attacker.example'},
    {'X-Archive-Token': 'bad'}])
def test_http_rejects_cross_origin_and_unrelated_sessions(local_server, headers):
    assert http_request(local_server, 'POST', '/api/plan', {'fields': ['energyprod']}, headers)[0] == 403


@pytest.mark.parametrize('host', ['127.0.0.2', '0.0.0.0'])
def test_http_network_binding_checks_the_requested_interface(database, tmp_path, host):
    server = archive_web.make_server(database, 'weather.sdb', port=0, directory=str(tmp_path),
                                     host=host)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        address = '127.0.0.2'
        origin = 'http://%s:%d' % (address, server.server_port)
        assert http_request(server, 'GET', '/', address=address)[0] == 200
        status, headers, body = http_request(
            server, 'POST', '/api/plan', {'fields': ['energyprod']},
            {'Origin': origin}, address=address)
        assert status == 200 and json.loads(body)['totals']['ready'] == 4
        assert http_request(server, 'GET', '/',
                            headers={'Host': '127.0.0.1:%d' % server.server_port},
                            address=address)[0] == 403
        assert http_request(server, 'POST', '/api/plan', {'fields': ['energyprod']},
                            {'Origin': 'https://attacker.example'}, address=address)[0] == 403
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_cli_creates_reports_and_replays_a_saved_dry_run(database, tmp_path):
    report_path = str(tmp_path / 'dry-run.json')
    html_path = str(tmp_path / 'dry-run.html')
    output = str(tmp_path / 'cli.sdb')
    archive_cli.main(['dry-run', '--database', database, '--fields', 'energyprod,power',
                      '--json', report_path, '--html', html_path])
    with open(report_path) as report_file:
        assert json.load(report_file)['plan']['totals']['ready'] == 4
    archive_cli.main(['export', '--database', database, '--plan', report_path, '--output', output])
    with archive_tools.Archive(output) as corrected:
        assert archive_tools.analyze(corrected)['records'] == 4


def test_cli_demo_and_utc_date_parsing(tmp_path):
    demo = str(tmp_path / 'demo.sdb')
    archive_cli.main(['demo', '--output', demo])
    assert plan_for(demo)['totals']['blocked'] > 0
    assert archive_cli.parse_timestamp('1970-01-01') == 0
    assert archive_cli.parse_timestamp('1970-01-01', end_of_day=True) == 86399
    assert archive_cli.parse_timestamp('1970-01-01T00:05') == 300
    with pytest.raises(ValueError):
        archive_cli.parse_timestamp('29/10/2026')


@pytest.mark.parametrize('method,expected', [
    ('mean', 60), ('first', 0), ('last', 120), ('sum', 120), ('min', 0), ('max', 120)])
def test_compaction_groups_samples_with_per_field_methods(database, tmp_path, method, expected):
    modify(database, 'INSERT INTO archive VALUES (307,1,1,NULL,110,120,NULL)')
    output = str(tmp_path / 'compacted.sdb')
    with archive_tools.Archive(database) as archive:
        plan = archive_tools.plan_merge(archive, ['energyprod', 'power'],
                                        reducers={'energyprod': 'last', 'power': method})
        assert plan['totals']['ready'] == 5
        assert plan['totals']['ready_destinations'] == 4
        group = plan['preview'][0]
        assert group['source_timestamps'] == [306, 307]
        assert group['source_count'] == 2
        assert group['changes'][1]['input_count'] == 2
        assert group['changes'][1]['inputs'] == [0, 120]
        archive_tools.export_copy(archive, plan, output)
    with archive_tools.Archive(output) as corrected:
        assert corrected.record(300)['power'] == expected
        assert corrected.record(300)['energyprod'] == 110
        assert corrected.record(306) is None
        assert corrected.record(307) is None


def test_one_conflicting_sample_blocks_the_entire_compaction_group(database):
    modify(database, 'INSERT INTO archive VALUES (307,16,1,NULL,110,120,NULL)')
    with archive_tools.Archive(database) as archive:
        plan = archive_tools.plan_merge(archive, ['energyprod', 'power'], reducer='mean')
    assert plan['totals']['blocked'] == 2
    assert not any(operation['target_timestamp'] == 300 for operation in plan['operations'])


def test_compacted_destination_is_compared_with_the_aggregate(database):
    modify(database, 'INSERT INTO archive VALUES (307,1,1,NULL,110,120,NULL)')
    modify(database, 'UPDATE archive SET power = 60 WHERE dateTime = 300')
    with archive_tools.Archive(database) as archive:
        plan = archive_tools.plan_merge(archive, ['energyprod', 'power'],
                                        reducers={'energyprod': 'last', 'power': 'mean'})
    assert plan['totals']['ready'] == 5


def test_nearest_existing_record_does_not_require_an_epoch_aligned_grid(database, tmp_path):
    modify(database, 'UPDATE archive SET dateTime = dateTime + 2 WHERE outTemp IS NOT NULL')
    output = str(tmp_path / 'nearest.sdb')
    with archive_tools.Archive(database) as archive:
        plan = archive_tools.plan_merge(archive, ['energyprod', 'power'], direction='record')
        assert plan['totals']['ready'] == 4
        assert plan['preview'][0]['target_timestamp'] == 302
        archive_tools.export_copy(archive, plan, output)
    with archive_tools.Archive(output) as corrected:
        assert corrected.record(302)['energyprod'] == 100


def test_compaction_overflow_is_a_conflict(database):
    modify(database, 'UPDATE archive SET power = 1e308 WHERE dateTime = 306')
    modify(database, 'INSERT INTO archive VALUES (307,1,1,NULL,110,1e308,NULL)')
    with archive_tools.Archive(database) as archive:
        plan = archive_tools.plan_merge(archive, ['power'], reducer='sum')
    assert plan['totals']['blocked'] == 2
    assert plan['reasons']['invalid_aggregate'] == 2


def test_dense_imports_do_not_change_the_suggested_weather_archive_interval(database):
    with sqlite3.connect(database) as connection:
        for timestamp in range(310, 590, 10):
            connection.execute('INSERT INTO archive VALUES (?,1,1,NULL,110,100,NULL)', (timestamp,))
    with archive_tools.Archive(database) as archive:
        report = archive_tools.analyze(archive)
    assert report['intervals'][0]['minutes'] == 1
    assert report['interval_seconds'] == 300
    assert report['interval_basis'] == 'outTemp'


def test_http_date_scope_is_used_for_both_dry_run_and_export(local_server):
    status, headers, body = http_request(local_server, 'POST', '/api/plan', {
        'fields': ['energyprod', 'power'], 'start': 600, 'stop': 1000})
    plan = json.loads(body)
    assert status == 200 and plan['totals']['ready'] == 2
    status, headers, body = http_request(local_server, 'POST', '/api/export', {'plan_id': plan['plan_id']})
    assert status == 200


def test_weewx_binding_resolution_does_not_open_or_change_the_database(database, tmp_path):
    configuration = tmp_path / 'weewx.conf'
    configuration.write_text('WEEWX_ROOT = %s\n[DataBindings]\n'
                             ' [[wx_binding]]\n  database = weather\n  table_name = archive\n'
                             '[Databases]\n [[weather]]\n  database_type = SQLite\n'
                             '  database_name = weather.sdb\n[DatabaseTypes]\n'
                             ' [[SQLite]]\n  driver = weedb.sqlite\n  SQLITE_ROOT = .\n' % tmp_path)
    options = archive_cli.build_parser().parse_args(['analyze', '--config', str(configuration)])
    before = digest(database)
    path, table = archive_cli.resolve_source(options)
    assert os.path.realpath(path) == database
    assert table == 'archive'
    assert digest(database) == before


def test_scalar_summary_matches_weewx_accumulator(database, tmp_path):
    import weewx.accum
    import weeutil.weeutil

    make_summaries(database)
    output = str(tmp_path / 'weewx-compatible.sdb')
    with archive_tools.Archive(database) as archive:
        archive_tools.export_copy(archive, plan_for(database), output)
    weewx.accum.initialize({})
    accumulator = weewx.accum.Accum(weeutil.weeutil.TimeSpan(0, 86400))
    with archive_tools.Archive(output) as corrected:
        for row in corrected.connection.execute('SELECT * FROM archive ORDER BY dateTime'):
            accumulator.addRecord(dict(row), weight=row['interval'] * 60)
        actual = corrected.connection.execute('SELECT * FROM archive_day_energyprod').fetchone()
        assert tuple(actual) == (0,) + accumulator['energyprod'].getStatsTuple()


def test_malformed_aggregation_options_are_reported(local_server):
    status, headers, body = http_request(local_server, 'POST', '/api/plan', {
        'fields': ['energyprod'], 'reducers': ['mean']})
    assert status == 400
    assert 'map observation' in json.loads(body)['error']


def test_reading_outside_tolerance_does_not_block_a_valid_nearest_match(database):
    modify(database, 'DELETE FROM archive WHERE dateTime = 300')
    with archive_tools.Archive(database) as archive:
        plan = archive_tools.plan_merge(archive, ['energyprod', 'power'], direction='record',
                                        tolerance=150, reducer='mean')
    assert plan['totals']['ready'] == 3
    assert plan['totals']['blocked'] == 1
    assert next(operation for operation in plan['operations']
                if operation['target_timestamp'] == 600)['values']['energyprod'] == 110


def test_late_compaction_groups_are_visible_in_the_preview(database):
    with sqlite3.connect(database) as connection:
        for index in range(5, 230):
            connection.execute('INSERT INTO archive VALUES (?,1,5,10,NULL,NULL,NULL)', (index * 300,))
            connection.execute('INSERT INTO archive VALUES (?,1,5,NULL,150,0,NULL)', (index * 300 + 5,))
        connection.execute('INSERT INTO archive VALUES (68706,1,5,NULL,160,100,NULL)')
    with archive_tools.Archive(database) as archive:
        plan = archive_tools.plan_merge(archive, ['energyprod', 'power'],
                                        reducer='mean', reducers={'energyprod': 'last'})
    assert len(plan['preview']) == 200
    assert plan['preview'][0]['source_count'] == 2
    assert plan['preview'][0]['changes'][0]['after'] == 160


def test_summary_rebuild_rejects_remaining_mixed_unit_readings(database, tmp_path):
    make_summaries(database)
    modify(database, 'UPDATE archive SET usUnits = 16 WHERE dateTime = 306')
    output = str(tmp_path / 'mixed-units.sdb')
    with archive_tools.Archive(database) as archive:
        with pytest.raises(ValueError, match='Mixed or invalid unit systems'):
            archive_tools.export_copy(archive, plan_for(database), output, allow_blocked=True)
    assert not os.path.exists(output)


def test_automatic_backup_contains_the_original_before_correction(database, tmp_path):
    output = str(tmp_path / 'backed-up.sdb')
    before = digest(database)
    with archive_tools.Archive(database) as archive:
        result = archive_tools.export_copy(archive, plan_for(database), output)
    backup = result['backup']
    assert backup['integrity'] == 'ok'
    assert backup['sha256'] == digest(backup['path'])
    assert os.path.getsize(backup['path']) == backup['bytes']
    with open(backup['metadata_path']) as metadata:
        assert json.load(metadata) == backup
    with archive_tools.Archive(backup['path']) as original:
        assert original.record(300)['energyprod'] is None
        assert original.record(306)['energyprod'] == 100
        assert archive_tools.analyze(original)['records'] == 8
    with archive_tools.Archive(output) as corrected:
        assert corrected.record(300)['energyprod'] == 100
        assert corrected.record(306) is None
    assert digest(database) == before
    assert os.stat(backup['path']).st_mode & 0o777 == 0o600


def test_backups_include_the_whole_database_and_committed_wal_records(database, tmp_path):
    directory = str(tmp_path / 'permanent')
    with contextlib.closing(sqlite3.connect(database)) as writer:
        writer.execute('PRAGMA journal_mode = WAL')
        writer.execute('CREATE TABLE additional_data (value TEXT)')
        writer.execute("INSERT INTO additional_data VALUES ('preserve this too')")
        writer.execute('INSERT INTO archive VALUES (1505,1,5,NULL,130,0,NULL)')
        writer.commit()
        with archive_tools.Archive(database, start=600, stop=900) as archive:
            backup = archive_tools.create_backup(archive, directory, 'weather.sdb')
    with archive_tools.Archive(backup['path']) as saved:
        assert archive_tools.analyze(saved)['records'] == 9
        assert saved.record(1505)['energyprod'] == 130
        assert saved.connection.execute('SELECT value FROM additional_data').fetchone()[0] == 'preserve this too'


def test_backup_failure_stops_export_and_removes_its_empty_output(database, tmp_path, monkeypatch):
    def forbidden_copy(source, destination):
        raise AssertionError('A failed backup must stop before any correction copy is written.')

    monkeypatch.setattr(archive_tools, 'copy_database', forbidden_copy)
    blocked_directory = tmp_path / 'not-a-directory'
    blocked_directory.write_text('keep this file')
    output = str(tmp_path / 'must-not-exist.sdb')
    before = digest(database)
    with archive_tools.Archive(database) as archive:
        with pytest.raises(OSError):
            archive_tools.export_copy(archive, plan_for(database), output,
                                       backup_directory=str(blocked_directory))
    assert not os.path.exists(output)
    assert blocked_directory.read_text() == 'keep this file'
    assert digest(database) == before


def test_failed_backup_write_leaves_no_incomplete_backup(database, tmp_path, monkeypatch):
    original_copy = archive_tools.copy_database

    def failed_copy(source, destination):
        original_copy(source, destination)
        raise OSError('simulated disk-full error')

    monkeypatch.setattr(archive_tools, 'copy_database', failed_copy)
    directory = tmp_path / 'incomplete'
    with archive_tools.Archive(database) as archive:
        with pytest.raises(OSError, match='disk-full'):
            archive_tools.create_backup(archive, str(directory))
    assert list(directory.iterdir()) == []


def test_completed_backup_survives_a_later_export_failure(database, tmp_path):
    make_summaries(database)
    modify(database, 'ALTER TABLE archive_day_energyprod ADD COLUMN custom REAL')
    output = str(tmp_path / 'failed-export.sdb')
    with archive_tools.Archive(database) as archive:
        with pytest.raises(archive_tools.ExportError, match='Backup retained') as failure:
            archive_tools.export_copy(archive, plan_for(database), output)
    backup = failure.value.backup
    assert not os.path.exists(output)
    assert os.path.isfile(backup['path'])
    assert os.path.isfile(backup['metadata_path'])
    assert digest(backup['path']) == backup['sha256']


def test_each_export_creates_a_separate_backup(database, tmp_path):
    with archive_tools.Archive(database) as archive:
        first = archive_tools.export_copy(archive, plan_for(database), str(tmp_path / 'first.sdb'))
        first_checksum = digest(first['backup']['path'])
        second = archive_tools.export_copy(archive, plan_for(database), str(tmp_path / 'second.sdb'))
    assert first['backup']['path'] != second['backup']['path']
    assert digest(first['backup']['path']) == first_checksum


def test_changed_backup_baseline_invalidates_the_reviewed_plan(database, tmp_path, monkeypatch):
    original_backup = archive_tools.create_backup

    def changing_source(archive, directory, name):
        modify(database, 'UPDATE archive SET energyprod = 999 WHERE dateTime = 306')
        return original_backup(archive, directory, name)

    monkeypatch.setattr(archive_tools, 'create_backup', changing_source)
    output = str(tmp_path / 'changed-baseline.sdb')
    with archive_tools.Archive(database) as archive:
        with pytest.raises(archive_tools.ExportError, match='changed while creating the backup') as failure:
            archive_tools.export_copy(archive, plan_for(database), output)
    assert not os.path.exists(output)
    with archive_tools.Archive(failure.value.backup['path']) as saved:
        assert saved.record(306)['energyprod'] == 999


def test_cli_backup_uses_the_selected_permanent_directory(database, tmp_path, capsys):
    directory = tmp_path / 'cli-backups'
    archive_cli.main(['backup', '--database', database, '--backup-dir', str(directory)])
    backup = json.loads(capsys.readouterr().out)
    assert os.path.dirname(backup['path']) == str(directory)
    assert backup['sha256'] == digest(backup['path'])


def test_web_backups_remain_after_the_export_directory_is_removed(database, tmp_path):
    permanent_directory = str(tmp_path / 'persistent-web-backups')
    with tempfile.TemporaryDirectory(dir=str(tmp_path)) as temporary_exports:
        server = archive_web.make_server(database, 'weather.sdb', port=0,
                                          directory=temporary_exports,
                                          backup_directory=permanent_directory)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            status, headers, body = http_request(server, 'POST', '/api/backup', {})
            backup = json.loads(body)
            assert status == 200
            status, headers, body = http_request(server, 'GET', backup['download'])
            assert status == 200 and body.startswith(b'SQLite format 3')
            assert backup['filename'] in headers['Content-Disposition']
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()
    assert os.path.isfile(backup['path'])
    assert os.path.dirname(backup['path']) == permanent_directory


def test_http_failed_correction_still_offers_the_backup(database, tmp_path):
    make_summaries(database)
    modify(database, 'ALTER TABLE archive_day_energyprod ADD COLUMN custom REAL')
    server = archive_web.make_server(database, 'weather.sdb', port=0, directory=str(tmp_path))
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        status, headers, body = http_request(server, 'POST', '/api/plan', {'fields': ['energyprod', 'power']})
        plan = json.loads(body)
        status, headers, body = http_request(server, 'POST', '/api/export', {'plan_id': plan['plan_id']})
        failure = json.loads(body)
        assert status == 400
        assert failure['backup']['integrity'] == 'ok'
        assert http_request(server, 'GET', failure['backup']['download'])[0] == 200
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
