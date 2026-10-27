"""Inspect and export the public source without reading local account or content files."""

import argparse
import gzip
import io
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


ROOT_FILES = (
    '.gitignore', '.gitattributes', 'README.md', 'LICENSE', 'SECURITY.md',
    'CONTRIBUTING.md', 'pyproject.toml', 'MANIFEST.in', 'requirements.txt', 'config.example.json',
    'examples/posts.json', 'examples/config.generic.json', 'scripts/release.py',
    '.github/workflows/ci.yml',
)
PATTERNS = {
    'personal home path': re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/|[A-Za-z]:\\Users\\[^\\\s]+'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'GitHub credential': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'AWS access key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'API credential': re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b'),
    'JWT': re.compile(r'\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b'),
}


def public_files(root):
    root = Path(root).resolve()
    names = set(ROOT_FILES)
    names.update(p.relative_to(root).as_posix() for p in (root / 'uploader').glob('*.py'))
    names.update(p.relative_to(root).as_posix() for p in (root / 'tests').glob('test_*.py')
                 if p.name != 'test_reserve.py')
    files = {}
    for name in sorted(names):
        path = root / name
        if not path.is_file():
            raise ValueError(f'Missing public file: {name}')
        if not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in (path, *path.parents) if p != root):
            raise ValueError(f'Symlink is not allowed in public source: {name}')
        files[name] = path.read_bytes()
    return files


def inspect_files(files, denied=()):
    findings = []
    for name, content in files.items():
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError:
            findings.append({'file': name, 'reason': 'unexpected binary file'})
            continue
        for reason, pattern in PATTERNS.items():
            match = pattern.search(text)
            if match:
                findings.append({'file': name, 'line': text[:match.start()].count('\n') + 1, 'reason': reason})
        if any(value in text for value in denied):
            findings.append({'file': name, 'reason': 'private marker'})
    return findings


def inspect_git(root, files):
    if not (Path(root) / '.git').exists():
        return []
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], capture_output=True)
    if result.returncode:
        return []
    tracked = result.stdout.decode('utf-8').split('\0')
    return [{'file': name, 'reason': 'tracked file outside public allowlist'}
            for name in tracked if name and name not in files]


def make_archive(files, output, force=False):
    output = Path(output)
    if output.suffix.lower() != '.zip':
        raise ValueError('Output must be a .zip file')
    if output.exists() and not force:
        raise ValueError('Output exists; use --force to replace the generated archive')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(files.items()):
                info = zipfile.ZipInfo('cafe-publisher/' + name, date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def sanitize_sdist(archive_path, files, denied=()):
    """Remove local user/group identifiers from Python source archive headers."""
    archive_path = Path(archive_path)
    if not archive_path.name.endswith('.tar.gz'):
        raise ValueError('Source distribution must end in .tar.gz')
    generated = {'PKG-INFO', 'setup.cfg', *(f'cafe_publisher.egg-info/{name}' for name in (
        'PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'entry_points.txt', 'requires.txt', 'top_level.txt'))}
    members = []
    payloads = {}
    with tarfile.open(archive_path, 'r:gz') as archive:
        for member in archive.getmembers():
            parts = member.name.split('/')
            if member.name.startswith('/') or '..' in parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unexpected path or link in source distribution')
            if member.isfile():
                relative = '/'.join(parts[1:])
                if relative not in files and relative not in generated:
                    raise ValueError(f'Unexpected source distribution file: {relative}')
                content = archive.extractfile(member).read()
                payloads[member.name] = content
            members.append((member.name, member.isdir()))
    if inspect_files(payloads, denied):
        raise ValueError('Source distribution contains a private marker or credential pattern')
    temporary = archive_path.with_suffix('.gz.tmp')
    try:
        with temporary.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for name, is_dir in members:
                    info = tarfile.TarInfo(name)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    info.mtime = 1767225600
                    info.mode = 0o755 if is_dir else 0o644
                    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
                    content = payloads.get(name, b'')
                    info.size = len(content)
                    archive.addfile(info, None if is_dir else io.BytesIO(content))
        temporary.replace(archive_path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--check', action='store_true', help='Inspect source without creating an archive')
    parser.add_argument('--output', type=Path, help='Write a sanitized source ZIP')
    parser.add_argument('--sanitize-sdist', type=Path, help='Remove owner metadata from a built .tar.gz source archive')
    parser.add_argument('--deny-file', type=Path, help='Private JSON array of additional forbidden strings')
    parser.add_argument('--force', action='store_true', help='Replace an existing generated ZIP')
    args = parser.parse_args()
    if not args.check and args.output is None and args.sanitize_sdist is None:
        parser.error('Choose --check, --output or --sanitize-sdist')
    try:
        denied = json.loads(args.deny_file.read_text(encoding='utf-8')) if args.deny_file else []
        if not isinstance(denied, list) or any(not isinstance(x, str) or len(x) < 4 for x in denied):
            raise ValueError('--deny-file must contain a JSON array of strings of at least 4 characters')
        files = public_files(args.root)
        findings = inspect_files(files, denied) + inspect_git(args.root, files)
        report = {'public_files': len(files), 'findings': findings, 'passed': not findings}
        if findings:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        if args.output is not None:
            make_archive(files, args.output, args.force)
            report['archive'] = args.output.as_posix()
        if args.sanitize_sdist is not None:
            sanitize_sdist(args.sanitize_sdist, files, denied)
            report['sanitized_sdist'] = args.sanitize_sdist.as_posix()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f'Release stopped: {type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
