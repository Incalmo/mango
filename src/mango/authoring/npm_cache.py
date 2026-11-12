"""Capture manager-created npm metadata after a network-disabled lifecycle replay."""

from mango.authoring.cache_archive import pin_cache_archive


def capture(container, request, work, destination, installed, generated_lock):
    from .npm_lock import (
        CACHE,
        PROJECT,
        WORK,
        ResolverError,
        install_argv,
        npm_env,
        read_container_bytes,
        read_hidden_lock,
    )

    env = npm_env(request)
    cache_path = container.exec(
        ["npm", "config", "get", "cache"], env=env
    ).stdout.strip()
    if cache_path != "/root/.npm":
        raise ResolverError(
            "Original npm cache path requires explicit review", "cache-path"
        )
    container.disconnect_networks()
    container.exec(["rm", "-rf", PROJECT + "/node_modules"])
    container.exec(
        install_argv(request, CACHE, offline=True, force_ignore_scripts=False),
        env=env,
        workdir=PROJECT,
        timeout=request["timeout_seconds"],
    )
    hidden = read_hidden_lock(container, request["limits"]["max_hidden_lock_bytes"])
    if hidden.get("packages") != installed:
        raise ResolverError(
            "Lifecycle replay changed package selection", "replay-mismatch"
        )
    if (
        generated_lock is not None
        and read_container_bytes(
            container,
            PROJECT + "/package-lock.json",
            request["limits"]["max_lock_bytes"],
            "replay-mismatch",
        )
        != generated_lock
    ):
        raise ResolverError(
            "Lifecycle replay changed generated lock", "replay-mismatch"
        )
    archive_path = WORK + "/retained-npm-cache.tar.gz"
    container.exec(
        ["tar", "--numeric-owner", "-czpf", archive_path, "-C", CACHE, "."],
        timeout=request["timeout_seconds"],
    )
    local = work / "retained-npm-cache.tar.gz"
    container.copy_out(archive_path, local)
    try:
        artifact = pin_cache_archive(local, destination, prefix="npm/cache")
    except ValueError as exc:
        raise ResolverError(str(exc), "cache-member") from exc
    return {
        **artifact,
        "runtime_cache_path": cache_path,
        "lifecycle_replay_network_disabled": True,
        "installed_package_selection_equal": True,
        "generated_lock_equal": generated_lock is not None,
        "contents": "Original manager registry metadata and package cache, not a reconstructed tarball-only cache",
    }
