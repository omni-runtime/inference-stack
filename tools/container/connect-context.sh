#!/usr/bin/env bash
set -euo pipefail
[[ -f /.dockerenv && $PWD == /workspace/inference-stack ]] || {
  echo 'Run inside the project tools container.' >&2; exit 2;
}
endpoint=${1:?supply the verified tcp:// mutual-TLS management endpoint}
[[ $endpoint == tcp://* ]] || { echo 'Expected an explicit TLS TCP endpoint.' >&2; exit 2; }
certs=/run/docker-client
for file in ca.pem cert.pem key.pem; do
  [[ -r $certs/$file ]] || { echo "Missing Docker client certificate: $file" >&2; exit 2; }
done
# Context metadata lives in this tools container, never on the remote host.
context=${2:-remote-engine}
if docker context inspect "$context" >/dev/null 2>&1; then
  docker context update "$context" --docker "host=$endpoint,ca=$certs/ca.pem,cert=$certs/cert.pem,key=$certs/key.pem"
else
  docker context create "$context" --docker "host=$endpoint,ca=$certs/ca.pem,cert=$certs/cert.pem,key=$certs/key.pem"
fi
docker --context "$context" version
docker --context "$context" compose version
