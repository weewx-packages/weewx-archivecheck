# Copyright (c) 2026 Manuel Hilgert
# SPDX-License-Identifier: GPL-3.0-only

"""Serve a local archive snapshot and render standalone reports."""

import http.server
import itertools
import json
import os
import secrets
import shutil
import socketserver
import sqlite3
import tempfile
import threading
import urllib.parse

from .archive import Archive, analyze, create_backup, export_copy, field_details, plan_merge, public_plan


ASSET_DIRECTORY = os.path.join(os.path.dirname(__file__), 'static')


def read_asset(name):
    with open(os.path.join(ASSET_DIRECTORY, name), encoding='utf-8') as asset:
        return asset.read()


def render_report(payload):
    """Render a self-contained report that can be opened without a web server.

    Args:
        payload (dict): Archive report, optional field details and optional dry run.

    Returns:
        str: HTML with embedded styles, scripts and safely encoded report data.
    """
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False).replace('<', '\\u003c')
    document = read_asset('index.html')
    document = document.replace('<link rel="stylesheet" href="/style.css">',
                                '<style>%s</style>' % read_asset('style.css'))
    return document.replace('<script src="/app.js" defer></script>',
                            '<script id="report-data" type="application/json">%s</script>'
                            '<script>%s</script>' % (encoded, read_asset('app.js')))


class LocalServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    session_token = ''


def make_server(path, name, table='archive', start=None, stop=None, port=8765, directory=None,
                backup_directory=None, host='127.0.0.1'):
    """Build an HTTP server for one fixed snapshot.

    Args:
        path (str): Snapshot database path.
        name (str): Display name of the source.
        table (str): Archive table.
        start (int|None): Inclusive first timestamp.
        stop (int|None): Inclusive last timestamp.
        port (int): Local port, or zero for an automatically allocated port.
        directory (str|None): Private export directory, required for correction downloads.
        backup_directory (str|None): Permanent backup directory. Defaults to archivecheck-backups
            beside the snapshot for direct server callers.
        host (str): IPv4 address to bind. Defaults to loopback.

    Returns:
        LocalServer: Bound server; the caller starts and closes it.
    """
    if not 0 <= port <= 65535:
        raise ValueError('Use a port between 0 and 65535.')
    with Archive(path, table, start, stop) as archive:
        report = analyze(archive, name)
    token = secrets.token_hex(32)
    plans = {}
    exports = {}
    export_counter = itertools.count(1)
    lock = threading.Lock()
    persistent_backups = backup_directory if backup_directory is not None else os.path.join(
        os.path.dirname(os.path.abspath(path)), 'archivecheck-backups')

    def publish_backup(backup):
        result = dict(backup)
        route = '/download/backup-%s.sdb' % secrets.token_hex(8)
        with lock:
            exports[route] = (backup['path'], backup['filename'])
        result['download'] = route + '?token=' + token
        return result

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format_string, *arguments):
            # Download URLs carry a local session token, so do not put request paths in logs.
            pass

        def send_bytes(self, body, content_type='application/json; charset=utf-8', status=200,
                       filename=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; "
                             "script-src 'self'; connect-src 'self'; img-src 'self' data:; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header('Content-Disposition', 'attachment; filename="%s"' % filename)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, value, status=200):
            self.send_bytes(json.dumps(value, allow_nan=False).encode('utf-8'), status=status)

        def allowed(self, authenticated=False):
            address = self.connection.getsockname()[0]
            allowed_hosts = ('%s:%d' % (address, server.server_port),)
            if address.startswith('127.'):
                allowed_hosts += ('localhost:%d' % server.server_port,)
            if self.headers.get('Host') not in allowed_hosts:
                self.send_json({'error': 'Use the server address.'}, 403)
                return False
            origin = self.headers.get('Origin')
            if origin is not None and origin not in tuple('http://' + host for host in allowed_hosts):
                self.send_json({'error': 'This request is not from the archive interface.'}, 403)
                return False
            if authenticated and not secrets.compare_digest(
                    self.headers.get('X-Archive-Token', ''), token):
                self.send_json({'error': 'Reload the local interface to start a session.'}, 403)
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path in ('/', '/style.css', '/app.js'):
                filename = {'/': 'index.html', '/style.css': 'style.css', '/app.js': 'app.js'}[parsed.path]
                content_type = {'/': 'text/html', '/style.css': 'text/css',
                                '/app.js': 'text/javascript'}[parsed.path] + '; charset=utf-8'
                self.send_bytes(read_asset(filename).replace('__SESSION_TOKEN__', token).encode('utf-8'),
                                content_type)
                return
            if parsed.path.startswith('/download/'):
                if not secrets.compare_digest(query.get('token', [''])[0], token):
                    self.send_json({'error': 'Invalid download session.'}, 403)
                    return
                with lock:
                    export = exports.get(parsed.path)
                if export is None:
                    self.send_json({'error': 'No such export.'}, 404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/vnd.sqlite3')
                self.send_header('Content-Length', str(os.path.getsize(export[0])))
                self.send_header('Content-Disposition', 'attachment; filename="%s"' % export[1])
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                with open(export[0], 'rb') as database:
                    shutil.copyfileobj(database, self.wfile)
                return
            if not self.allowed(authenticated=True):
                return
            try:
                if parsed.path == '/api/report':
                    self.send_json(report)
                elif parsed.path == '/api/field':
                    with Archive(path, table, start, stop) as archive:
                        self.send_json(field_details(archive, query.get('name', [''])[0]))
                elif parsed.path in ('/api/report.json', '/api/report.html'):
                    with Archive(path, table, start, stop) as archive:
                        field = query.get('field', [''])[0]
                        details = field_details(archive, field) if field else None
                    with lock:
                        plan = public_plan(plans['current']) if 'current' in plans else None
                    payload = {'report': report, 'details': details, 'plan': plan}
                    if parsed.path.endswith('.html'):
                        self.send_bytes(render_report(payload).encode('utf-8'),
                                        'text/html; charset=utf-8', filename='archive-report.html')
                    else:
                        self.send_bytes(json.dumps(payload, indent=2, allow_nan=False).encode('utf-8'),
                                        filename='archive-report.json')
                else:
                    self.send_json({'error': 'No such page.'}, 404)
            except (ValueError, sqlite3.Error) as error:
                self.send_json({'error': str(error)}, 400)

        def do_POST(self):
            if not self.allowed(authenticated=True):
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    raise ValueError('Invalid request size.')
                options = json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(options, dict):
                    raise ValueError('Expected an object with merge options.')
                if self.path == '/api/backup':
                    with Archive(path, table, start, stop) as archive:
                        backup = create_backup(archive, persistent_backups, name)
                    self.send_json(publish_backup(backup))
                elif self.path == '/api/plan':
                    plan_start = options.get('start', start)
                    plan_stop = options.get('stop', stop)
                    if any(value is not None and type(value) is not int
                           for value in (plan_start, plan_stop)):
                        raise ValueError('Date bounds must be integer UTC timestamps.')
                    if ((start is not None and (plan_start is None or plan_start < start))
                            or (stop is not None and (plan_stop is None or plan_stop > stop))):
                        raise ValueError('The dry run must stay within the loaded date range.')
                    with Archive(path, table, plan_start, plan_stop) as archive:
                        plan = plan_merge(archive, options['fields'], options.get('anchor', 'outTemp'),
                                          options.get('interval', 300), options.get('tolerance', 15),
                                          options.get('direction', 'previous'),
                                          options.get('reducer', 'reject'), options.get('reducers'))
                    with lock:
                        plans['current'] = plan
                    self.send_json(public_plan(plan))
                elif self.path == '/api/export':
                    with lock:
                        current_plan = plans.get('current')
                        if current_plan is None or options.get('plan_id') != current_plan['plan_id']:
                            raise ValueError('Run a dry run for the current settings first.')
                        if directory is None:
                            raise ValueError('No export directory is available.')
                        export_name = 'corrected-%d.sdb' % next(export_counter)
                    output = os.path.join(directory, export_name)
                    with Archive(path, table, current_plan['options']['start'], current_plan['options']['stop']) as archive:
                        result = export_copy(archive, current_plan, output,
                                             options.get('ready_only') is True,
                                             persistent_backups, name)
                        with Archive(output, table, start, stop) as corrected:
                            result['after'] = analyze(corrected, 'archive-corrected.sdb')
                    download = '/download/' + export_name
                    with lock:
                        exports[download] = (output, 'archive-corrected.sdb')
                    result['download'] = download + '?token=' + token
                    result['backup'] = publish_backup(result['backup'])
                    self.send_json(result)
                else:
                    self.send_json({'error': 'No such operation.'}, 404)
            except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
                failure = {'error': str(error)}
                retained_backup = getattr(error, 'backup', None)
                if retained_backup is not None:
                    failure['backup'] = publish_backup(retained_backup)
                self.send_json(failure, 400)

    server = LocalServer((host, port), Handler)
    server.session_token = token
    return server


def serve(path, name, table='archive', start=None, stop=None, port=8765, backup_directory=None,
          host='127.0.0.1'):
    """Run the interface until interrupted, then remove temporary exports."""
    with tempfile.TemporaryDirectory(prefix='weewx-archivecheck-exports-') as directory:
        persistent_backups = backup_directory if backup_directory is not None else os.path.expanduser(
            '~/.local/share/weewx-archivecheck/backups')
        server = make_server(path, name, table, start, stop, port, directory, persistent_backups, host)
        try:
            print('Archivecheck: http://%s:%d/' % server.server_address, flush=True)
            print('Reading a fixed snapshot. Stop with Ctrl+C.', flush=True)
            server.serve_forever()
        finally:
            server.server_close()
