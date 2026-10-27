import importlib.util
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location('release', Path(__file__).resolve().parents[1] / 'scripts/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class PublicReleaseTests(unittest.TestCase):
    def test_source_archive_owner_metadata_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source.tar.gz'
            with tarfile.open(source, 'w:gz') as archive:
                info = tarfile.TarInfo('cafe_publisher-0.1.0/README.md')
                info.uid, info.gid = 1234, 1234
                info.uname, info.gname = 'private-owner-for-test', 'private-group-for-test'
                info.size = len(b'public example')
                archive.addfile(info, io.BytesIO(b'public example'))
            release.sanitize_sdist(source, {'README.md': b'public example'})
            with tarfile.open(source) as archive:
                for item in archive.getmembers():
                    self.assertEqual((item.uid,item.gid,item.uname,item.gname), (0,0,'',''))
                self.assertEqual(archive.extractfile(archive.getmembers()[0]).read(), b'public example')

    def test_only_allowlisted_files_are_exported_even_when_private_files_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in release.ROOT_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('public example', encoding='utf-8')
            for name in ['.auth/session.json', '.state/queue.sqlite3', 'config.json',
                         'content/posts.json', 'assets/photo.jpg', 'tests/test_reserve.py',
                         'scripts/personal_job.py', '.DS_Store']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('private data', encoding='utf-8')
            files = release.public_files(root)
            self.assertEqual(set(files), set(release.ROOT_FILES))
            archive = root / 'source.zip'
            release.make_archive(files, archive)
            with zipfile.ZipFile(archive) as result:
                self.assertEqual(set(result.namelist()), {'cafe-publisher/' + name for name in files})
                self.assertNotIn(b'private data', b''.join(result.read(n) for n in result.namelist()))

    def test_leaks_are_rejected_without_echoing_secret_values(self):
        secret = 'ghp_' + 'X' * 36
        marker = 'private-marker-for-test'
        findings = release.inspect_files({'example.py': ('token=' + secret).encode(),
                                         'README.md': marker.encode()}, [marker])
        self.assertEqual(len(findings), 2)
        self.assertNotIn(secret, repr(findings))
        self.assertNotIn(marker, repr(findings))

    def test_symlink_to_private_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in release.ROOT_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('public', encoding='utf-8')
            private = root / 'private.txt'
            private.write_text('private', encoding='utf-8')
            (root / 'README.md').unlink()
            (root / 'README.md').symlink_to(private)
            with self.assertRaisesRegex(ValueError, 'Symlink'):
                release.public_files(root)
