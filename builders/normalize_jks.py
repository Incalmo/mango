"""Normalize trusted-certificate JKS timestamps without reserializing certificates.

Python 3.8+ stdlib only. This is not a certificate-chain or trust-policy validator.
"""

import argparse
import hashlib
import hmac
import json
import os
import stat
import struct
import tempfile
from pathlib import Path


def checksum(body, password):
    prefix = password.encode("utf-16-be", errors="surrogatepass")
    return hashlib.sha1(prefix + b"Mighty Aphrodite" + body).digest()


class Reader:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def take(self, size):
        if size < 0 or size > len(self.data) - self.offset:
            raise ValueError("truncated JKS or invalid length")
        start = self.offset
        self.offset += size
        return self.data[start : self.offset]

    def number(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def utf(self):
        raw = self.take(self.number(">H"))
        # Java modified UTF-8 encodes UTF-16 code units, including surrogates.
        units = []
        pos = 0
        while pos < len(raw):
            first = raw[pos]
            pos += 1
            if 1 <= first <= 0x7F:
                units.append(first)
                continue
            width = 2 if 0xC0 <= first <= 0xDF else 3 if 0xE0 <= first <= 0xEF else 0
            if not width or pos + width - 1 > len(raw):
                raise ValueError("invalid modified UTF-8 in JKS")
            tail = raw[pos : pos + width - 1]
            if any(byte & 0xC0 != 0x80 for byte in tail):
                raise ValueError("invalid modified UTF-8 in JKS")
            value = first & (0x1F if width == 2 else 0x0F)
            for byte in tail:
                value = (value << 6) | (byte & 0x3F)
            if (width == 2 and value < 0x80 and value != 0) or (
                width == 3 and value < 0x800
            ):
                raise ValueError("noncanonical modified UTF-8 in JKS")
            units.append(value)
            pos += width - 1
        return tuple(units)


def normalize(data, epoch, password="changeit"):
    if type(epoch) is not int or not 0 <= epoch <= (2**63 - 1) // 1000:
        raise ValueError(
            "epoch must be nonnegative seconds fitting signed 64-bit milliseconds"
        )
    if len(data) < 32:
        raise ValueError("truncated JKS")
    body, digest = data[:-20], data[-20:]
    if not hmac.compare_digest(checksum(body, password), digest):
        raise ValueError("JKS checksum mismatch or incorrect password")
    reader = Reader(body)
    if reader.number(">I") != 0xFEEDFEED:
        raise ValueError("unsupported format: expected JKS")
    version = reader.number(">i")
    if version not in (1, 2):
        raise ValueError("unsupported JKS version")
    count = reader.number(">i")
    if count < 0 or count > (len(body) - 12) // 18:
        raise ValueError("invalid JKS entry count")
    result = bytearray(body)
    aliases = set()
    for _ in range(count):
        tag = reader.number(">i")
        if tag != 2:
            raise ValueError(
                "only trusted certificates supported; private-key/unknown entry rejected"
            )
        alias = reader.utf()
        if alias in aliases:
            raise ValueError("duplicate JKS alias")
        aliases.add(alias)
        offset = reader.offset
        reader.take(8)
        result[offset : offset + 8] = struct.pack(">q", epoch * 1000)
        if version == 2 and reader.utf() not in (
            tuple(map(ord, "X.509")),
            tuple(map(ord, "X509")),
        ):
            raise ValueError("unsupported certificate type")
        size = reader.number(">i")
        if size <= 0:
            raise ValueError("empty or invalid certificate length")
        reader.take(size)  # Opaque DER: never re-encode or change trust contents.
    if reader.offset != len(body):
        raise ValueError("unexpected trailing JKS data")
    result = bytes(result)
    return result + checksum(result, password), count


def normalize_file(path, epoch, password="changeit"):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("JKS path must be a regular, non-linked file")
    original = path.read_bytes()
    normalized, count = normalize(original, epoch, password)
    if normalized == original:
        return count
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".jks-", delete=False
        ) as output:
            temporary = output.name
            current = os.fstat(output.fileno())
            if (current.st_uid, current.st_gid) != (info.st_uid, info.st_gid):
                os.fchown(output.fileno(), info.st_uid, info.st_gid)
            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
            output.write(normalized)
            output.flush()
            os.fsync(output.fileno())
        latest = path.lstat()
        fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
        )
        if (
            any(getattr(latest, field) != getattr(info, field) for field in fields)
            or path.read_bytes() != original
        ):
            raise ValueError("JKS changed during normalization")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument(
        "--password-file",
        type=Path,
        help="UTF-8 password, exact contents (no trailing newline)",
    )
    args = parser.parse_args()
    password = (
        args.password_file.read_text(encoding="utf-8")
        if args.password_file
        else "changeit"
    )
    try:
        count = normalize_file(args.path, args.epoch, password)
    except (OSError, ValueError) as error:
        parser.exit(1, "JKS normalization failed: {}\n".format(error))
    print(json.dumps({"trusted_certificates": count, "epoch": args.epoch}))


if __name__ == "__main__":
    main()
