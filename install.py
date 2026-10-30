# Copyright (c) 2026 Manuel Hilgert
# SPDX-License-Identifier: GPL-3.0-only

"""Install the standalone archive inspector without enabling a WeeWX service."""

import weecfg.extension


def loader():
    return ArchivecheckInstaller()


class ArchivecheckInstaller(weecfg.extension.ExtensionInstaller):
    def __init__(self):
        super().__init__(
            version='0.2.0', name='archivecheck',
            description='Inspect SQLite archives, preview merges and export corrected copies.',
            author='Manuel Hilgert',
            files=[
                ('bin/user', ['bin/user/archivecheck.py']),
                ('bin/user/archivechecklib', [
                    'bin/user/archivechecklib/__init__.py',
                    'bin/user/archivechecklib/archive.py',
                    'bin/user/archivechecklib/cli.py',
                    'bin/user/archivechecklib/mysqlsource.py',
                    'bin/user/archivechecklib/web.py']),
                ('bin/user/archivechecklib/static', [
                    'bin/user/archivechecklib/static/index.html',
                    'bin/user/archivechecklib/static/style.css',
                    'bin/user/archivechecklib/static/app.js'])])
