"""Derive explicit index filename mappings from verified APT source policies."""

import shlex


def list_entry(line):
    words = shlex.split(line, comments=True)
    if not words:
        return None
    if words[0] != "deb":
        raise ValueError("Unreviewed repository entry type")
    if len(words) > 1 and words[1].startswith("["):
        end = next((i for i in range(1, len(words)) if words[i].endswith("]")), None)
        if end is None:
            raise ValueError("Unclosed repository options")
        options = " ".join(words[1 : end + 1])[1:-1].split()
        if any(
            option.split("=", 1)[0] not in {"arch", "signed-by", "check-valid-until"}
            or "=" not in option
            for option in options
        ):
            raise ValueError("Unreviewed repository options")
        words = [words[0], *words[end + 1 :]]
    if len(words) < 4:
        raise ValueError("Unreviewed runtime repository syntax")
    return words[1], words[2]


def active_sources(request, transaction):
    if type(transaction) is not int or not 0 <= transaction < len(request["stages"]):
        raise ValueError("Repository timeline requires a valid transaction index")
    sources = list(request["sources"])
    for stage in request["stages"][: transaction + 1]:
        if stage.get("sources") is not None:
            sources = list(stage["sources"])
        sources.extend(stage.get("extra_sources", []))
    return {"sources": list(dict.fromkeys(sources))}


def runtime_entries(path, content):
    if path.endswith(".sources"):
        paragraphs = []
        current = {}
        for line in [*content.splitlines(), ""]:
            if line.lstrip().startswith("#"):
                continue
            if not line.strip():
                if current:
                    paragraphs.append(current)
                    current = {}
                continue
            key, separator, value = line.partition(":")
            if (
                not separator
                or key in current
                or key not in {"Types", "URIs", "Suites", "Components", "Signed-By"}
            ):
                raise ValueError("Unreviewed deb822 source field")
            current[key] = value.strip()
        for fields in paragraphs:
            if (
                fields.get("Types") != "deb"
                or len(fields.get("URIs", "").split()) != 1
                or not fields.get("Components")
            ):
                raise ValueError("Unreviewed deb822 repository shape")
            for suite in fields.get("Suites", "").split():
                yield fields["URIs"], suite
        return
    for line in content.splitlines():
        entry = list_entry(line)
        if entry:
            yield entry


def mappings(spec, runtime):
    suites = {}
    for path, content in runtime.items():
        for source_uri, suite in runtime_entries(path, content):
            uri = source_uri.rstrip("/") + "/dists/" + suite + "/"
            if suite in suites and suites[suite] != uri:
                raise ValueError("Multiple runtime origins for one suite")
            suites[suite] = uri
    result = []
    for line in spec["sources"]:
        entry = list_entry(line)
        if not entry:
            raise ValueError("Empty author repository source")
        uri, suite = entry
        if suite not in suites:
            raise ValueError("No runtime repository for authored suite: " + suite)
        result.append(
            {
                "author_uri": uri.rstrip("/") + "/dists/" + suite + "/",
                "runtime_uri": suites[suite],
            }
        )
    return result
