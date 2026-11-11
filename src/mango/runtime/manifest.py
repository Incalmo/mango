"""Image manifest shared by hosted builders and execution workers.

A hosted build publishes exactly this document and workers consume it; hosts
that keep their own copy of these models must keep them schema-compatible.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Version 3: images come from a mango declaration and the manifest carries
#: the runtime declaration that turns the source tree into a Harbor task.
BUILD_RECIPE_VERSION = 3
RUNTIME_SCHEMA_VERSION = 1
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Image = Annotated[str, Field(pattern=r"^[a-zA-Z0-9._:/-]+@sha256:[0-9a-f]{64}$")]


class RuntimeDeclaration(BaseModel):
    """Everything needed to generate the Harbor task from the source tree.

    Produced by evaluating ``default.nix``'s ``declaration``; applying it needs
    no Nix. Compose documents carry placeholder images that the image manifest
    replaces during materialization.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = RUNTIME_SCHEMA_VERSION
    #: Source-relative Harbor task directory.
    task: str
    #: Task-relative context (``environment``, ``tests``...) to Compose document.
    compose: dict[str, dict] = Field(min_length=1)
    #: Task-environment-relative destination directory to source-relative directory.
    environment_files: dict[str, str] = Field(default_factory=dict)
    #: Task-environment-relative files that need executable mode.
    executable_files: list[str] = Field(default_factory=list)


class BuildManifest(BaseModel):
    build_id: str
    source_digest: Digest
    platform: Literal["linux/amd64"] = "linux/amd64"
    recipe_version: int = BUILD_RECIPE_VERSION
    services: dict[str, Image] = Field(min_length=1)
    verifiers: dict[str, dict[str, Image]] = Field(default_factory=dict)
    egress_image: Image
    runtime: RuntimeDeclaration
