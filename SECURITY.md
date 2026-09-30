# Security policy

## Supported versions

This project is a preview. Security fixes target the default branch and the
current preview contract; there is no supported legacy/LTS branch.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/omni-runtime/inference-stack/security/advisories/new).
Do not publish exploit details, API keys, kubeconfigs, or deployment logs in an
ordinary issue. Include the affected commit, configuration shape with values
redacted, impact, and the smallest safe reproduction. No response-time guarantee
is implied.

## Deployment expectations

Treat the gateway token, model keys, cloud keys, and Kubernetes credentials as
secrets. Put TLS and suitable network controls around public deployments. The
example gateway is a single-operator deployment scaffold, not a multi-tenant
identity or billing system. Use your own credential files and access policy.

Build/prepare operations execute upstream code. Review the locked revision and
patches before running them. Model download, remote model code, native libraries,
and engine containers have their own trust and licensing requirements.
