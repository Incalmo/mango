# Runtime topology and image outputs have one authored source of truth.
{ pkgs }:
{ name
, task
, containers
, services
, networks ? { }
, volumes ? { }
, configs ? { }
, secrets ? { }
, verifiers ? { }
, builds ? { }
, platform ? "linux/amd64"
, environmentFiles ? { }
, executableFiles ? [ ]
}:
let
  lib = pkgs.lib;
  names = builtins.attrNames containers;
  validName = value: builtins.match "[a-zA-Z0-9][a-zA-Z0-9_-]*" value != null;
  validateService = service: value:
    assert lib.assertMsg (value ? container && builtins.elem value.container names)
      "Service ${service} must name a declared container";
    assert lib.assertMsg (!(value ? image) && !(value ? build) && !(value ? pull_policy))
      "Service ${service}: image, build and pull_policy are managed by mkEnvironment";
    value;
  checkedServices = lib.mapAttrs validateService services;
  checkedVerifiers = lib.mapAttrs (_: lib.mapAttrs validateService) verifiers;
  mapping = values: lib.mapAttrs (_: value: value.container) values;
  document = values: {
    inherit networks volumes configs secrets;
    services = lib.mapAttrs (_: value:
      (builtins.removeAttrs value [ "container" ]) // {
        # Placeholder is only used before the loaded digest map is applied.
        image = "mango-declared-${value.container}";
      }) values;
  };
in
assert lib.assertMsg (builtins.all validName (names ++ builtins.attrNames builds))
  "Container and build names must be simple output names";
assert lib.assertMsg (containers ? egress) "mkEnvironment requires containers.egress";
assert lib.assertMsg (services ? main) "mkEnvironment requires services.main";
assert lib.assertMsg (builtins.all (n: !(builtins.hasAttr n builds)) names)
  "Container and build output names must not overlap";
{
  # Do not inspect derivations here: evaluating declaration must not build or
  # force application sources, including import-from-derivation expressions.
  images = containers;
  inherit builds;
  declaration = {
    version = 1;
    inherit name;
    manifest = {
      task = toString task;
      inherit platform;
      images = lib.genAttrs names (n: "images.${n}");
      builds = lib.mapAttrs (n: _: "builds.${n}") builds;
      services = mapping checkedServices;
      verifiers = lib.mapAttrs (_: mapping) checkedVerifiers;
      environment_files = lib.mapAttrs (_: toString) environmentFiles;
      executable_files = executableFiles;
    };
    compose = (document checkedServices) // {
      inherit networks volumes configs secrets;
    };
    verifierCompose = lib.mapAttrs (_: document) checkedVerifiers;
  };
}
