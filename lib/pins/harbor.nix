# Harbor CLI pinned to the release the runtime adapter (src/mango/harbor/environment.py
# and src/mango/runtime/materialize.py) was written against: it overrides Harbor internals.
{ pkgs }:
let
  python = pkgs.python313;
  wheel = { pname, version, sha256, url, dependencies ? [ ] }:
    python.pkgs.buildPythonPackage {
      inherit pname version dependencies;
      format = "wheel";
      src = pkgs.fetchurl { inherit url sha256; };
    };
  scantree = python.pkgs.scantree;
  dirhash = wheel {
    pname = "dirhash";
    version = "0.5.0";
    url = "https://files.pythonhosted.org/packages/5e/1f/c8bf92552b7f0a13b9f12b85e3de8df6d9814240e0f8ce8f37433df028b3/dirhash-0.5.0-py3-none-any.whl";
    sha256 = "523dfd6b058c64f45b31604376926c6e2bd2ea301d0df23095d4055674e38b09";
    dependencies = [ scantree ];
  };
in
wheel {
  pname = "harbor";
  version = "0.23.0";
  url = "https://files.pythonhosted.org/packages/19/c7/607ff037dff1f40d1f941b9854742d66fd43630274fd4e7b8de8480dad34/harbor-0.23.0-py3-none-any.whl";
  sha256 = "8747400dbb2a5e2298e1338e17e88eba38433c0433fd700f34d1a9021bba5c37";
  dependencies = with python.pkgs; [
    pydantic shortuuid typer requests pyyaml rich toml tenacity python-dotenv
    litellm jinja2 dirhash packaging fastapi uvicorn pathspec supabase httpx
    filelock platformdirs pyjwt jsonschema
  ];
}
