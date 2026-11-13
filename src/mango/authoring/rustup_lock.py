"""Pin a Rust release channel and its profile components from official manifests.

Only metadata is fetched here. Declarative builds fetch and verify the component
archives using the recorded SHA256 values; source rust-toolchain files stay intact.
"""
import argparse
import hashlib
import json
import re
import tomllib
import urllib.request
from urllib.parse import urlsplit


def component_lock(data, expected_sha256, *, channel, target, profile, manifest_url):
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError('Rust channel manifest checksum mismatch')
    if not re.fullmatch(r'(stable|beta|nightly|[0-9]+\.[0-9]+\.[0-9]+)', channel):
        raise ValueError('Unsupported Rust channel selector')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', target):
        raise ValueError('Unsafe Rust target triple')
    document = tomllib.loads(data.decode())
    if document.get('manifest-version') != '2' or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', document.get('date', '')):
        raise ValueError('Expected dated Rust manifest version2')
    if profile not in ('minimal', 'default', 'complete') or profile not in document.get('profiles', {}):
        raise ValueError('Unknown Rust installation profile')
    host = document['pkg']['rust']['target'].get(target)
    if not host or not host.get('available'):
        raise ValueError('Rust release is unavailable for the requested host')
    supported = {item['pkg'] for item in host.get('components', []) + host.get('extensions', [])
                 if item['target'] in (target, '*')}
    components = []
    for requested in document['profiles'][profile]:
        name = document.get('renames', {}).get(requested, {}).get('to', requested)
        if not re.fullmatch(r'[a-z0-9_-]+', name):
            raise ValueError('Unsafe component name')
        # Profiles are cross-platform: e.g. minimal includes rust-mingw, which
        # is absent from the Linux release's component/extension inventory.
        if name not in supported:
            continue
        package = document['pkg'][name]
        record = package['target'].get(target, package['target'].get('*'))
        if not record or not record.get('available'):
            raise ValueError(f'Component {name} is unavailable for {target}')
        url, sha256 = record['xz_url'], record['xz_hash']
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not parsed.path.endswith('.tar.xz')):
            raise ValueError('Rust component URL must be public HTTPS tar.xz')
        if not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise ValueError('Every Rust component needs its SHA256')
        components.append({'name': name, 'version': package['version'], 'url': url, 'sha256': sha256})
    if not {'rustc', 'rust-std', 'cargo'}.issubset({x['name'] for x in components}):
        raise ValueError('Profile lacks the compiler, standard library, or Cargo')
    return {'schema_version': 1, 'channel': channel, 'target': target, 'profile': profile,
            'date': document['date'], 'toolchain': channel + '-' + target,
            'rust_version': document['pkg']['rust']['version'],
            'manifest': {'url': manifest_url, 'sha256': expected_sha256}, 'components': components}


def pin(channel, target, profile):
    if not re.fullmatch(r'(stable|beta|nightly|[0-9]+\.[0-9]+\.[0-9]+)', channel):
        raise ValueError('Unsupported Rust channel selector')
    url = f'https://static.rust-lang.org/dist/channel-rust-{channel}.toml'
    data = urllib.request.urlopen(url, timeout=60).read()
    expected = urllib.request.urlopen(url + '.sha256', timeout=60).read().decode().split()[0]
    preliminary = component_lock(data, expected, channel=channel, target=target, profile=profile, manifest_url=url)
    dated_url = f"https://static.rust-lang.org/dist/{preliminary['date']}/channel-rust-{channel}.toml"
    dated = urllib.request.urlopen(dated_url, timeout=60).read()
    if dated != data:
        raise ValueError('Dated Rust manifest differs from selected channel')
    return component_lock(data, expected, channel=channel, target=target, profile=profile, manifest_url=dated_url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--profile', choices=['minimal', 'default', 'complete'], required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    lock = pin(args.channel, args.target, args.profile)
    with open(args.output, 'w') as stream:
        json.dump(lock, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'toolchain': lock['toolchain'], 'date': lock['date'], 'rust_version': lock['rust_version']}))


if __name__ == '__main__':
    main()
