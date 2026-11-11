import importlib.util
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location('normalize_jar', Path(__file__).resolve().parents[2] / 'builders/normalize_jar.py')
jar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jar)


class JarTests(unittest.TestCase):
    def test_deterministic_content_permissions_and_loader_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outputs = []
            entries = [('pkg/x.clj', b'(ns pkg.x)'), ('pkg/x.class', b'class bytes'), ('bin/run', b'#!/bin/sh'), ('link', b'bin/run')]
            for index, order in enumerate((entries, list(reversed(entries)))):
                source, target = root / f'{index}.jar', root / f'{index}.out'
                with zipfile.ZipFile(source, 'w') as archive:
                    archive.comment = b'archive comment'
                    for name, data in order:
                        item = zipfile.ZipInfo(name, (2020 + index, 1, 1, 0, 0, 0))
                        item.create_system = 3
                        mode = stat.S_IFLNK | 0o777 if name == 'link' else stat.S_IFREG | 0o755
                        item.external_attr = mode << 16
                        item.comment = b'entry comment'
                        item.extra = struct.pack('<HH', 0xCAFE, 2) + b'ok'
                        archive.writestr(item, data)
                source.chmod(0o640)
                jar.normalize(source, target)
                outputs.append(target.read_bytes())
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
                with zipfile.ZipFile(source) as original, zipfile.ZipFile(target) as result:
                    self.assertEqual(result.namelist(), sorted(original.namelist()))
                    self.assertEqual(result.comment, original.comment)
                    self.assertGreater(result.getinfo('pkg/x.class').date_time, result.getinfo('pkg/x.clj').date_time)
                    for item in original.infolist():
                        other = result.getinfo(item.filename)
                        self.assertEqual(result.read(item.filename), original.read(item.filename))
                        self.assertEqual(other.external_attr, item.external_attr)
                        self.assertEqual(other.comment, item.comment)
                        self.assertEqual(other.extra, item.extra)
                jar.normalize(target, target)
                self.assertEqual(outputs[-1], target.read_bytes())
            self.assertEqual(*outputs)

    def test_extra_fields(self):
        for kind in jar.VOLATILE_EXTRA_FIELDS:
            self.assertEqual(jar.stable_extra(struct.pack('<HH', kind, 0)), b'')
        for kind in jar.UNSUPPORTED_EXTRA_FIELDS:
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                jar.stable_extra(struct.pack('<HH', kind, 0))
        for data in (b'x', struct.pack('<HH', 123, 5) + b'x'):
            with self.assertRaisesRegex(ValueError, 'Truncated'):
                jar.stable_extra(data)

    def test_duplicate_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'in.jar', Path(directory) / 'out.jar'
            target.write_bytes(b'previous output')
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                with zipfile.ZipFile(source, 'w') as archive:
                    archive.writestr('x', b'1')
                    archive.writestr('x', b'2')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                jar.normalize(source, target)
            self.assertEqual(target.read_bytes(), b'previous output')
            self.assertEqual(list(Path(directory).glob('.jar-*')), [])

    def test_timestamp_bounds(self):
        with self.assertRaises(ValueError):
            jar.zip_date(0)
        self.assertEqual(jar.zip_date(315532802), (1980, 1, 1, 0, 0, 2))


if __name__ == '__main__':
    unittest.main()
