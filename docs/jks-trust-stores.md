# Deterministic JKS trust stores

Some Java package installers stamp trusted-certificate entries with installation
time. For an explicitly selected JDK stage, add `h.jksTrustStoreNormalizer` to that
stage's `extraContext`, then run the script with the stage's Python 3.8+:

```
python3 /context/normalize_jks.py /etc/ssl/certs/java/cacerts --epoch 0
```

Choose a declared epoch, normally the build's `SOURCE_DATE_EPOCH`. Do not add the
script to every native stage's context: unrelated stages need no new input.
The default password is the public JDK trust-store password `changeit`.
`--password-file PATH` accepts exact UTF-8 file contents, without trimming a
newline; never put private credentials into a Nix derivation.

The script verifies the JKS integrity checksum before parsing. It accepts versions
1 and 2 with trusted X.509 certificate entries only. Private keys, other entry
tags, other formats/types, malformed lengths/text, duplicate aliases, bad
checksums and trailing data fail before replacement. Certificate bytes, raw alias
and type encodings, ordering and entry count are retained. This is deliberately
not an X.509 parser, certificate validator or trust-policy editor. SHA-1 here is
the existing JKS checksum format, not a newly chosen artifact integrity scheme.

Only entry timestamps and the format checksum change. Replacement is atomic in
the same directory, preserving owner/group and permission bits. The input must
be an exclusively managed regular file with one link; symlinks/hardlinks are
rejected. ACLs and extended attributes are not copied; use this only for ordinary
package-stage trust-store files. File modification time is left to the image
builder's normal filesystem timestamp normalization.

Implementation is independent, based on the documented binary format and
serialization in [OpenJDK JavaKeyStore](https://github.com/openjdk/jdk/blob/master/src/java.base/share/classes/sun/security/provider/JavaKeyStore.java).
No Java implementation code is incorporated.

Run parser and real Java interoperability tests:

```
nix-shell tests/support/jks-shell.nix --run 'python -m unittest discover -s tests -t . -p test_normalize_jks.py -v'
```

The integration test generates ephemeral test certificates/keys outside the
repository, checks Java can still list/export every certificate byte-for-byte,
checks different input dates normalize identically, and rejects a real private-key
store. No environment fixtures or private material are included.
