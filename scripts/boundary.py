"""Enforce operator-declared workload and mount boundaries before Docker calls."""
from datetime import datetime, timezone
from pathlib import Path

class BoundaryError(ValueError):
    pass


def enforce_execution(environment):
    if environment["runtime"] != "docker":
        return
    if environment.get("verification_not_before"):
        not_before = datetime.fromisoformat(environment["verification_not_before"])
        if not_before.tzinfo is None:
            raise BoundaryError("verification_not_before must include a timezone")
        if datetime.now(timezone.utc) <= not_before:
            raise BoundaryError(f"Docker verification is forbidden before {not_before.isoformat()}")
    if environment.get("container_only"):
        if not Path("/.dockerenv").exists():
            raise BoundaryError("project CLI must execute inside its tools container")
        if not Path.cwd().is_relative_to(Path(environment["root"])):
            raise BoundaryError("tools workload must run inside the configured project root")


def validate_compose(config, environment):
    if config.get("name") != environment["project"]:
        raise BoundaryError("unexpected Compose project name")
    allowed_source = environment["docker"]["allowed_readonly_model_source"]
    allowed_volumes = {environment["docker"]["volumes"][name]
                       for name in ("config", "credentials", "cache", "outputs", "media_bindings")}
    for name, spec in config.get("volumes", {}).items():
        if spec.get("driver_opts") or spec.get("name") not in allowed_volumes:
            raise BoundaryError(f"unmanaged volume: {name}")
    for name, service in config.get("services", {}).items():
        if service.get("privileged") or any(service.get(key) == "host" for key in ("pid", "ipc", "uts", "cgroup", "network_mode")):
            raise BoundaryError(f"host privilege/namespace forbidden: {name}")
        if any(service.get(key) for key in ("cap_add", "devices", "volumes_from", "post_start", "pre_stop")):
            raise BoundaryError(f"unrestricted host/device access forbidden: {name}")
        if name != "envoy" and service.get("ports"):
            raise BoundaryError(f"backend may not publish host ports: {name}")
        for mount in service.get("volumes", []):
            if not isinstance(mount, dict):
                raise BoundaryError("mounts must use unambiguous long syntax")
            if "docker.sock" in mount.get("target", ""):
                raise BoundaryError("business containers cannot access Docker socket")
            if mount.get("type") == "bind":
                if mount.get("source") != allowed_source or mount.get("read_only") is not True:
                    raise BoundaryError("only the exact existing model directory may be mounted read-only")
                if mount.get("bind", {}).get("create_host_path") is not False:
                    raise BoundaryError("host path creation forbidden")
            elif mount.get("type") != "volume" or mount.get("source") not in config.get("volumes", {}):
                raise BoundaryError("only project named volumes and the read-only model bind are allowed")
            elif config["volumes"][mount["source"]].get("name") in {
                environment["docker"]["volumes"]["config"], environment["docker"]["volumes"]["credentials"]
            } and mount.get("read_only") is not True:
                raise BoundaryError("serving configuration and credentials must be read-only")
