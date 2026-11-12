import hashlib
import io
import unittest
from unittest.mock import patch

from mango.authoring.pin_file import pin


class Response(io.BytesIO):
    def geturl(self):
        return 'https://example.test/source.tar.gz'


class PinFileTests(unittest.TestCase):
    def test_identifies_mango_to_release_servers(self):
        with patch('mango.authoring.pin_file.urlopen', return_value=Response(b'bytes')) as fetch:
            pin('https://example.test/source.tar.gz')
        request = fetch.call_args.args[0]
        self.assertEqual(request.full_url, 'https://example.test/source.tar.gz')
        self.assertEqual(request.get_header('User-agent'), 'mango-pin-file/1.0')

    def test_signed_redirect_is_not_recorded_in_the_lock(self):
        response = Response(b'release bytes')
        response.geturl = lambda: 'https://cdn.example.test/release?signature=temporary'
        source = 'https://example.test/releases/v1/source.tar.gz'
        with patch('mango.authoring.pin_file.urlopen', return_value=response):
            result = pin(source)
        self.assertEqual(result['urls'], [source])
        self.assertEqual(result['sha256'], hashlib.sha256(b'release bytes').hexdigest())
        self.assertNotIn('temporary', str(result))

    def test_redirect_cannot_change_to_local_file_or_embed_user_credentials(self):
        for target in ['file:///tmp/archive', 'https://user:secret@cdn.test/archive']:
            response = Response(b'data')
            response.geturl = lambda target=target: target
            with patch('mango.authoring.pin_file.urlopen', return_value=response):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    pin('https://example.test/archive')

    def test_hashes_exact_bytes_into_lock_entry(self):
        payload = b'archive bytes' * 200000
        with patch('mango.authoring.pin_file.urlopen', return_value=Response(payload)):
            result = pin('https://example.test/source.tar.gz', 'sources/source.tar.gz')
        self.assertEqual(result, {'path': 'sources/source.tar.gz',
                                 'urls': ['https://example.test/source.tar.gz'],
                                 'sha256': hashlib.sha256(payload).hexdigest()})

    def test_rejects_unshareable_urls_and_escaping_paths_before_network(self):
        with patch('mango.authoring.pin_file.urlopen') as request:
            for url, path in [('https://user:secret@host/a', None),
                              ('https://host/a?token=secret', None),
                              ('file:///tmp/a', None),
                              ('https://host/a', '../escape')]:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    pin(url, path)
        request.assert_not_called()
