"""Convert a pip installation report into explicit, hashed wheel inputs.

Generate the report using the target interpreter and platform, for example
``python -m pip install --dry-run --ignore-installed
--report report.json -r requirements.txt``. Resolution belongs on an authoring
host; this command neither installs packages nor builds images. Source archives
and editable/VCS inputs need separate authored builds and are rejected.
"""

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from mango.authoring.packages import update_lock


def report_artifacts(report: dict, prefix: str) -> list[dict]:
    """Return fetchLockedInputs entries, failing on incomplete/unsafe reports."""
    parts = prefix.split("/")
    if not prefix or any(p in ("", ".", "..") for p in parts) or "\\" in prefix:
        raise ValueError("prefix must be a safe relative directory")
    if report.get("version") != "1" or not isinstance(report.get("install"), list):
        raise ValueError("Expected pip installation report version 1")
    artifacts = {}
    for item in report["install"]:
        download = item.get("download_info", {})
        url = download.get("url", "")
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Wheel URL must be public HTTP(S), without credentials or query")
        name = unquote(PurePosixPath(parsed.path).name)
        if (not name.endswith(".whl") or "/" in name or "\\" in name
                or any(ord(c) < 32 for c in name)
                or download.get("vcs_info") or download.get("dir_info")):
            raise ValueError("Only immutable wheel downloads are supported")
        digest = download.get("archive_info", {}).get("hashes", {}).get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Every wheel needs its SHA-256 from the pip report")
        artifact = {"path": f"{prefix}/{name}", "urls": [url], "sha256": digest}
        if name in artifacts and artifacts[name] != artifact:
            raise ValueError(f"Conflicting wheel filename: {name}")
        artifacts[name] = artifact
    return sorted(artifacts.values(), key=lambda value: value["path"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args()
    artifacts = report_artifacts(json.loads(args.report.read_text()), args.prefix)
    update_lock(args.lock, artifacts, [args.prefix])
    print(json.dumps({"prefix": args.prefix, "wheels": len(artifacts)}))


if __name__ == "__main__":
    main()
