"""Hash an explicitly supplied public download into a fetchLockedInputs entry."""

import argparse
import hashlib
import json
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


def validate_url(url: str, *, redirect: bool = False) -> str:
    value = urlsplit(url)
    if (value.scheme not in ('https', 'http') or not value.hostname
            or value.username is not None or value.password is not None
            or (value.query and not redirect) or value.fragment):
        raise ValueError('Use a public HTTP(S) URL without credentials, query, or fragment')
    return url


def pin(url: str, path: str | None = None, *, timeout: float = 120) -> dict:
    validate_url(url)
    path = path or unquote(PurePosixPath(urlsplit(url).path).name)
    if (not path or path.startswith('/') or '\\' in path
            or any(p in ('', '.', '..') for p in path.split('/'))
            or any(ord(c) < 32 for c in path)):
        raise ValueError('Input path must be a safe relative file path')
    digest = hashlib.sha256()
    request = Request(url, headers={'User-Agent': 'mango-pin-file/1.0'})
    with urlopen(request, timeout=timeout) as response:
        # Public release URLs commonly redirect to a short-lived signed CDN
        # URL. Follow its transport query, but retain only the original stable
        # public URL in the lock; never serialize the redirect credential.
        validate_url(response.geturl(), redirect=True)
        for chunk in iter(lambda: response.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'path': path, 'urls': [url], 'sha256': digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--path', help='relative path in inputs.lock.json')
    args = parser.parse_args()
    print(json.dumps(pin(args.url, args.path)))


if __name__ == '__main__':
    main()
