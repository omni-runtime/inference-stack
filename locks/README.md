# Imported release contracts

Import a project B contract with `scripts/configure.py import-release`, then
reference it from `artifacts.release` in your instance's `stack.yaml`. Paths in
`stack.yaml` are relative to that file. `*.local.yaml` contracts are ignored.

Import copies exact bytes and validates image digests/platforms; it does not
build images, publish them, verify their availability or grant acceptance.
The checked-in `versions.lock.yml` and MLX manifests remain version metadata,
not daily deployment settings.
