import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mango.authoring import package_state as state
from mango.authoring import packages

IMAGE = 'sha256:' + 'b' * 64
SHA = 'c' * 64


def receipt():
    request = packages.LockRequest(manager='apt', base='example/base@sha256:' + 'a' * 64,
        sources=('deb https://snapshot.test stable main',),
        stages=(packages.PackageStage(packages=('curl',)),))
    return {'package_request': request.model_dump(mode='json'), 'prefixes': ['apt/key/0'],
            'lock': {'schema_version': 1, 'artifacts': [{'path': 'apt/key/0/curl.deb', 'urls': ['https://archive.test/curl.deb'], 'sha256': SHA}]}}


class PackageStateTests(unittest.TestCase):
    def test_explicit_service_start_policy_is_ordered_and_cache_bound(self):
        request = packages.LockRequest.model_validate(receipt()['package_request'])
        self.assertNotIn('policy_rc_d', request.cache_document()['stages'][0])
        changed = request.model_copy(update={'stages': (request.stages[0].model_copy(
            update={'policy_rc_d': 'deny'}),)})
        self.assertNotEqual(state.request_sha256(request), state.request_sha256(changed))
        script = packages.apt_script(changed)
        self.assertIn("#!/bin/sh\nexit 101\n", script)
        self.assertLess(script.index('chmod +x /usr/sbin/policy-rc.d'), script.index('update >&2'))
        with self.assertRaises(packages.LockError):
            packages.apk_script(changed.model_copy(update={'manager': 'apk'}))

    def test_resolved_receipt_contains_a_consumable_input_lock(self):
        request = packages.LockRequest.model_validate(receipt()['package_request'])
        selected = packages.LockedFile(name='curl.deb', url='https://archive.test/curl.deb', sha256=SHA)
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'MANGO_LOCK_CACHE': directory}), \
                packages.use_resolver(lambda _: [[selected]]):
            root = Path(directory)
            value = state.resolve_packages(request, root / 'inputs.lock.json')
            self.assertEqual(value['lock']['schema_version'], 1)
            restored = root / 'restored.lock.json'
            restored.write_text(json.dumps(value['lock']))
            packages.update_lock(restored, [])
            self.assertEqual(json.loads(restored.read_text()), value['lock'])

    def test_disk_reserve_stops_before_docker(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'MANGO_AUTHOR_MIN_FREE_BYTES': str(2**63)}), \
                patch.object(state.packages, 'cache_dir', return_value=Path(directory)), \
                patch.object(state.apt_prefix_cache, '_resolve_output') as resolve:
            with self.assertRaisesRegex(packages.LockError, 'disk reserve'):
                state.prepare_prefix(receipt())
            resolve.assert_not_called()

    def test_receipt_rejects_ambiguous_or_unbound_transaction_artifacts(self):
        original = receipt()
        variants = []
        duplicate = copy.deepcopy(original)
        duplicate['lock']['artifacts'] *= 2
        variants.append(duplicate)
        traversal = copy.deepcopy(original)
        traversal['lock']['artifacts'][0]['path'] = 'apt/key/0/../curl.deb'
        variants.append(traversal)
        missing_prefix = copy.deepcopy(original)
        missing_prefix['prefixes'] = []
        variants.append(missing_prefix)
        unknown_schema = copy.deepcopy(original)
        unknown_schema['lock']['schema_version'] = 2
        variants.append(unknown_schema)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                state.locked_transactions(value)

    def test_prepared_binding_requires_exact_selected_bytes_before_leasing(self):
        output = "@@mango-stage 0\n'https://archive.test/curl.deb' curl.deb 12 SHA256:" + SHA + '\n'
        with patch.object(state.apt_prefix_cache, '_resolve_output', return_value=(output, IMAGE)), \
                patch.object(state.packages, 'download_hash', return_value='d' * 64), \
                patch.object(state.apt_prefix_cache, 'docker') as docker:
            with self.assertRaisesRegex(packages.LockError, 'different from its lock'):
                state.prepare_prefix(receipt())
            docker.assert_not_called()

    def test_prepared_receipt_binds_image_and_archive_closure_and_lease(self):
        output = "@@mango-stage 0\n'https://archive.test/curl.deb' curl.deb 12 SHA256:" + SHA + '\n'
        def docker(*args):
            return subprocess.CompletedProcess(args, 0, json.dumps([{'Id': IMAGE}]), '')
        with patch.object(state.apt_prefix_cache, '_resolve_output', return_value=(output, IMAGE)), \
                patch.object(state.packages, 'download_hash', return_value=SHA), \
                patch.object(state.apt_prefix_cache, 'docker', side_effect=docker) as calls:
            value = state.prepare_prefix(receipt())
        request = packages.LockRequest.model_validate(receipt()['package_request'])
        self.assertEqual(value['prepared_image']['apt_request_sha256'], state.request_sha256(request))
        self.assertEqual(value['apt_receipt']['lock'], receipt()['lock'])
        self.assertTrue(value['selection_matches_lock'])
        self.assertEqual(calls.call_args_list[-1].args[0], 'create')
        self.assertRegex(value['lease']['container'], r'^mango-prepared-[0-9a-f]{32}$')

    def test_cannot_release_foreign_or_running_container(self):
        value = {'prepared_image': {'id': IMAGE, 'apt_request_sha256': SHA},
                 'lease': {'container': 'mango-prepared-' + 'f' * 32, 'request_sha256': SHA}}
        inspected = {'Image': IMAGE, 'State': {'Running': True}, 'Config': {'Labels': {state.LEASE_LABEL: SHA}}}
        with patch.object(state.apt_prefix_cache, 'docker', return_value=subprocess.CompletedProcess([], 0, json.dumps([inspected]), '')) as docker:
            with self.assertRaisesRegex(ValueError, 'ownership or state'):
                state.release_prefix(value)
            self.assertEqual(docker.call_count, 1)

    def test_file_observation_is_bound_and_distinguishes_symlinks(self):
        packages_receipt = receipt()
        request = packages.LockRequest.model_validate(packages_receipt['package_request'])
        prepared = {'apt_receipt': packages_receipt, 'selection_matches_lock': True,
                    'prepared_image': {'id': IMAGE, 'base': request.base,
                                       'apt_request_sha256': state.request_sha256(request)}}
        def docker(*args):
            output = json.dumps([{'Id': IMAGE}]) if args[0] == 'image' else '0\tregular\t755 0 0 19\t' + SHA + '\n1\tsymlink\n2\tmissing\n'
            return subprocess.CompletedProcess(args, 0, output, '')
        with patch.object(state.apt_prefix_cache, 'docker', side_effect=docker) as run:
            observed = state.observe_prefix_files(prepared, ['/usr/sbin/policy-rc.d', '/link', '/missing'])
        self.assertEqual(observed['files']['/usr/sbin/policy-rc.d']['mode'], 0o755)
        self.assertEqual(observed['files']['/link'], {'kind': 'symlink'})
        self.assertEqual(observed['files']['/missing'], {'kind': 'missing'})
        args = run.call_args.args
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertEqual(args[args.index('--pull') + 1], 'never')
        prepared['prepared_image']['apt_request_sha256'] = 'd' * 64
        with patch.object(state.apt_prefix_cache, 'docker') as run:
            with self.assertRaisesRegex(ValueError, 'verified prefix binding'):
                state.observe_prefix_files(prepared, ['/usr/sbin/policy-rc.d'])
            run.assert_not_called()

    def test_index_replay_is_disconnected_and_policy_mismatch_is_not_published(self):
        mappings = [{'author_uri': 'https://snapshot.test/dists/stable/', 'runtime_uri': 'https://runtime.test/dists/stable/'}]
        def capture(request, transaction, mappings, expected, destination):
            destination.write_bytes(b'index tar')
            return {'sha256': hashlib.sha256(b'index tar').hexdigest(), 'author_policy': 'Pinned packages:\n',
                    'transaction': transaction, 'selection_matches_lock': True}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(state.apt_indexes, 'capture', side_effect=capture), \
                patch.object(state, 'mount_args', return_value=[]), \
                patch.object(state.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'Changed policy\n', '')) as run:
            with self.assertRaisesRegex(packages.LockError, 'repository policy'):
                state.capture_indexes(receipt(), 0, mappings, {'/etc/apt/sources.list': 'deb https://runtime.test stable main\n'}, Path(directory))
            self.assertIn('--network', run.call_args.args[0])
            self.assertEqual(run.call_args.args[0][run.call_args.args[0].index('--network') + 1], 'none')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_index_runtime_paths_fail_before_any_authoring(self):
        with patch.object(state.apt_indexes, 'capture') as capture:
            with self.assertRaisesRegex(ValueError, 'source configuration'):
                state.capture_indexes(receipt(), 0, [], {'/etc/shadow': 'payload'}, Path('/unused'))
            capture.assert_not_called()

    def test_index_replay_leases_verified_preceding_prefix(self):
        value = receipt()
        value['package_request']['stages'] *= 2
        value['prefixes'].append('apt/key/1')
        mappings = [{'author_uri': 'https://snapshot.test/dists/stable/', 'runtime_uri': 'https://runtime.test/dists/stable/'}]
        def capture(request, transaction, mappings, expected, destination):
            destination.write_bytes(b'index tar')
            return {'sha256': hashlib.sha256(b'index tar').hexdigest(), 'author_policy': 'Pinned packages:\n'}
        prepared = {'prepared_image': {'id': IMAGE}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(state.apt_indexes, 'capture', side_effect=capture), \
                patch.object(state, 'prepare_prefix', return_value=prepared) as prepare, \
                patch.object(state, 'release_prefix') as release, \
                patch.object(state, 'mount_args', return_value=[]), \
                patch.object(state.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'Pinned packages:\n', '')):
            result = state.capture_indexes(value, 1, mappings, {'/etc/apt/sources.list': 'deb https://runtime.test stable main\n'}, Path(directory))
            self.assertTrue(result['runtime_policy_equal'])
            prepare.assert_called_once_with(value, count=1)
            release.assert_called_once_with(prepared)

    def test_json_cli_preserves_repositories_and_writes_portable_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = root / 'request.json'
            request.write_text(json.dumps(receipt()['package_request']))
            result = root / 'receipt.json'
            argv = ['mango-lock-packages', '--request', str(request), '--lock', str(root / 'lock.json'), '--receipt', str(result)]
            with patch('sys.argv', argv), patch.object(state, 'resolve_packages', return_value=receipt()) as resolve:
                packages.main()
            self.assertEqual(resolve.call_args.args[0].sources, ('deb https://snapshot.test stable main',))
            self.assertEqual(json.loads(result.read_text()), receipt())

    def test_prepare_cli_releases_lease_if_receipt_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'packages.json'
            source.write_text(json.dumps(receipt()))
            prepared = {'lease': {'container': 'owned'}}
            with patch.object(state, 'prepare_prefix', return_value=prepared), \
                    patch.object(state, 'write_receipt', side_effect=OSError('cannot publish')), \
                    patch.object(state, 'release_prefix') as release:
                with self.assertRaisesRegex(OSError, 'cannot publish'):
                    state.prepare_main([str(source), '--output', str(Path(directory) / 'prepared.json')])
                release.assert_called_once_with(prepared)
