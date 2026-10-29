# Copyright (c) 2026 Manuel Hilgert
# SPDX-License-Identifier: GPL-3.0-only

"""Build an installable Archivecheck ZIP without a WeeWX source checkout."""

import argparse
import pathlib
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    arguments = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parent
    destination = pathlib.Path(arguments.output).resolve()
    files = [root / name for name in
             ('install.py', 'README.md', 'LICENSE.txt', 'changelog', 'docs/guide.md')]
    files.extend(path for path in sorted((root / 'bin').rglob('*'))
                 if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc')
    if destination in files:
        parser.error('The output must not replace a package source file.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(destination), 'x', compression=zipfile.ZIP_DEFLATED) as output:
        for path in files:
            output.write(str(path), 'archivecheck/' + path.relative_to(root).as_posix())
    print(destination)


if __name__ == '__main__':
    main()
