"""Disposable declarative environment fixtures shared by the tests."""

from pathlib import Path


def write_recipe(
    root: Path,
    *,
    containers=("main", "egress"),
    services='main.container = "main";',
    verifiers="",
    builds="",
    extra="",
    lazy=True,
):
    """Write ``default.nix`` returning ``mkEnvironment``.

    With ``lazy`` every container and build throws when forced, proving that
    declaration evaluation never builds or forces image derivations.
    """
    def output(name):
        if lazy:
            return f'throw "{name} forced during metadata evaluation"'
        return f'h.pkgs.writeText "{name}" "{name}"'

    entries = " ".join(f"{name} = {output(name)};" for name in containers)
    built = " ".join(f"{name} = {output(name)};" for name in builds.split()) if builds else ""
    (root / "default.nix").write_text(
        "{ harness }:\n"
        "let h = import harness {}; in h.mkEnvironment {\n"
        '  name = "synthetic"; task = ./task;\n'
        f"  containers = {{ {entries} }};\n"
        f"  services = {{ {services} }};\n"
        f"  verifiers = {{ {verifiers} }};\n"
        f"  builds = {{ {built} }};\n"
        f"  {extra}\n"
        "}\n"
    )


def separate_grader_toml(image="grader"):
    return (
        '[environment]\n[verifier]\nenvironment_mode = "separate"\n'
        f'[verifier.environment]\ndocker_image = "{image}"\n'
    )
