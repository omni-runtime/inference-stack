#!/usr/bin/env python3
"""Offline deployment operations; no request forwarding or model selection."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import plistlib
import shutil
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import yaml

from boundary import BoundaryError, enforce_execution, validate_compose
from envoy import render as render_envoy
from stack_config import StackConfig, reject_mixed_args

ROOT = Path(__file__).resolve().parents[1]
LABEL = "app.kubernetes.io/part-of=inference-stack"
RUNTIME_WRAPPERS = ("router-entrypoint.sh", "model-entrypoint.sh", "mock-entrypoint.sh", "http-health.sh")


def read_yaml(path):
    return yaml.safe_load(Path(path).read_text())


def write_yaml(path, value):
    Path(path).write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True))


def execute(command, *, capture=False, timeout=1000):
    # No shell interpretation and no credentials in command arguments.
    result = subprocess.run([str(x) for x in command], check=True, text=True,
                            capture_output=capture, timeout=timeout)
    return result.stdout if capture else None


class Stack:
    def __init__(self, args):
        self.args = args
        self.config = None
        if getattr(args, "config", None):
            reject_mixed_args(args)
            self.config = StackConfig(args.config)
            self.environment = self.config.environment
            args.environment = self.config.name
            args.runtime = self.environment['runtime']
        else:
            self.environment = read_yaml(ROOT / "environments" / args.environment / "environment.yaml")
            if self.environment.get("name") != args.environment:
                raise ValueError("environment name must match its selected directory")
            if self.environment["runtime"] != args.runtime:
                raise ValueError("environment/runtime mismatch")
        # Unified offline commands do not access Docker or stage its live volume.
        if not self.config or args.action not in {'check', 'render', 'plan'}:
            enforce_execution(self.environment)
        self.state_dir = ROOT / ".state" / args.environment / args.runtime
        self.state_file = self.state_dir / "inventory.json"
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {
            "resources": [], "stopped": [], "status": "new"}
        if not self.config and self.state.get('config_path'):
            raise ValueError('this instance now uses unified configuration; pass --config with its stack.yaml')
        self.example = 'stack' if self.config else args.example or self.state.get("example")
        if not self.example and args.action not in {"status", "logs"}:
            raise ValueError("--example is required before the first deployment")
        self.output = ROOT / "generated" / args.environment / (self.example or "status")
        self.runtime = args.runtime
        self.mock = self.config.mock if self.config else (args.overlay == "mock" or (not args.example and self.state.get("overlay") == "mock"))
        self.images = dict(self.config.images) if self.config else read_yaml(ROOT / "versions.lock.yml")["images"]
        self.release_path = self.config.release_path if self.config else (Path(args.release) if args.release else ROOT / self.environment["release"])
        self.release = self.config.release if self.config else read_yaml(self.release_path)
        if self.config and self.state.get('target') and self.state['target'] != self.config.target():
            raise ValueError('deployment target changed for this instance; use a different name for a different target')
        # Even status/stop/down render the Compose file to address its services.
        # Select the target artifact consistently; acceptance is checked before
        # any deploy/start or ordinary render, while cleanup remains available.
        matching_images = [i for i in self.release.get("images", []) if i.get("platform") == self.environment["platform"]]
        if matching_images:
            self.images["router"] = matching_images[0]["reference"]
        self.renderer = Environment(loader=FileSystemLoader(ROOT), undefined=StrictUndefined,
                                    autoescape=False, trim_blocks=True, lstrip_blocks=True)
        if self.example:
            if self.config:
                self.pools = self.config.pools
                self.catalog_path = None
                catalog = self.config.models
            else:
                self.pools = read_yaml(ROOT / "examples" / self.example / "example.yaml")["pools"]
                if not self.pools or set(self.pools) - {"vllm", "omni", "cloud"}:
                    raise ValueError("zero or unknown backend pools")
                catalog_name = args.catalog or (self.state.get("catalog") if not args.example else None) or "config/models/catalog.yaml"
                self.catalog_path = (ROOT / catalog_name).resolve()
                if not self.catalog_path.is_relative_to(ROOT):
                    raise ValueError("--catalog must be inside the project for repeatable remote deployment")
                catalog_document = read_yaml(self.catalog_path)
                if catalog_document.get("mock_only") and not self.mock:
                    raise ValueError("this catalog contains mock models and requires --overlay mock")
                catalog = catalog_document["models"]
            self.models = [m for m in catalog if m["pool"] in self.pools]
            if not self.models:
                raise ValueError("no configured models in the requested pools")
            if self.mock:
                for model in self.models:
                    model["base_url"] = f'http://{model["service"]}:8000/v1'
            self.backends = {}
            for model in self.models:
                if model["pool"] == "cloud" and not self.mock:
                    continue
                service = model["service"]
                deployment = model.get("deployment", {"replicas": 1})
                mode = deployment.get("mode", "managed")
                if mode not in {"managed", "external"}:
                    raise ValueError(f"unknown deployment mode for {service}")
                if mode == "external" and not self.mock:
                    address = urlsplit(model["base_url"])
                    if address.scheme not in {"http", "https"} or not address.hostname or address.username or address.password or address.query or address.fragment:
                        raise ValueError(f"external service {service} requires an HTTP(S) URL without credentials, query or fragment")
                    if set(deployment) != {"mode"}:
                        raise ValueError(f"external service {service} must not declare managed deployment options")
                    continue
                if not self.mock and (not deployment.get("command") or deployment.get("image") not in self.images):
                    raise ValueError(f"service {service} requires a configured command and locked image")
                if service in self.backends and self.backends[service] != deployment:
                    raise ValueError(f"conflicting deployment definitions for service {service}")
                self.backends[service] = deployment
            self.backend_key_env = {}
            for model in self.models:
                service = model["service"]
                if service in self.backend_key_env and self.backend_key_env[service] != model["api_key_env"]:
                    raise ValueError(f"conflicting credential declarations for service {service}")
                self.backend_key_env[service] = model["api_key_env"]
            self.video_models = [m for m in self.models if m["api_format"] == "video"]
            for model in self.video_models:
                if not model.get("stateful_instance"):
                    raise ValueError("asynchronous video requires an explicit stable instance identity")
                if model["service"] in self.backends and self.backends[model["service"]].get("replicas", 1) != 1:
                    raise ValueError("process-local video jobs require one replica per named backend; register replicas separately")
            self.enabled_services = (["media-bindings"] if self.video_models else []) + ["router", "envoy", *self.backends]

    def kubectl(self, *args, capture=False):
        env = self.environment["kubernetes"]
        return execute(["kubectl", "--kubeconfig", env["kubeconfig"], "--context", env["context"],
                        "--namespace", env["namespace"], *args], capture=capture)

    def compose(self, *args, capture=False):
        env = self.environment["docker"]
        return execute(["docker", "--context", env["context"], "compose", "--project-name", env["project"],
                        "--file", self.output / "compose.yaml", *args], capture=capture)

    def compose_resources(self):
        output = self.compose("ps", "--all", "--format", "json", capture=True).strip()
        if not output:
            return []
        try:
            resources = json.loads(output)
        except json.JSONDecodeError:
            # Compose 5 emits one JSON object per line; older clients use arrays.
            resources = [json.loads(line) for line in output.splitlines() if line.strip()]
        return [resources] if isinstance(resources, dict) else resources

    def tools_network(self, connected):
        """Attach only the operator tools container to this Compose network."""
        env = self.environment["docker"]
        command = ["docker", "--context", env["context"]]
        container = env["tools_container"]
        network = env["project"] + "_inference"
        current = json.loads(execute([*command, "inspect", "--format", "{{json .NetworkSettings.Networks}}", container], capture=True))
        if connected and network not in current:
            execute([*command, "network", "connect", network, container])
        elif not connected and network in current:
            execute([*command, "network", "disconnect", network, container])

    def check_docker_target(self):
        """Read daemon identity and published ports before changing resources."""
        env = self.environment["docker"]
        command = ["docker", "--context", env["context"]]
        info = json.loads(execute([*command, "info", "--format", "{{json .}}"], capture=True))
        architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(info["Architecture"], info["Architecture"])
        actual = info["OSType"] + "/" + architecture
        if actual != self.environment["platform"]:
            raise ValueError(f"Docker daemon platform {actual} differs from environment {self.environment['platform']}; emulation is not accepted")
        ids = execute([*command, "ps", "--filter", f"publish={env['gateway_port']}", "--format", "{{.ID}}"], capture=True).split()
        if ids:
            # Inspect stays in memory; no other project's environment is logged.
            for container in json.loads(execute([*command, "inspect", *ids], capture=True)):
                labels = container["Config"].get("Labels") or {}
                owned = (labels.get("com.docker.compose.project") == env["project"]
                         and labels.get("com.docker.compose.service") == "envoy")
                for bindings in (container["NetworkSettings"].get("Ports") or {}).values():
                    for binding in bindings or []:
                        same_interface = binding["HostIp"] in ("", "0.0.0.0", "::", env["gateway_bind"])
                        if binding["HostPort"] == str(env["gateway_port"]) and same_interface and not owned:
                            raise ValueError(f"gateway port {env['gateway_bind']}:{env['gateway_port']} is already published by another container; no resource changed")

    def save(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, indent=2) + "\n")
        temp.replace(self.state_file)

    def stage_compose_config(self, digest, model_files):
        # This method executes only in the authorized tools container after
        # enforce_execution. Serving containers mount one immutable generation
        # from the project config volume; a label change triggers Compose's
        # native service recreation instead of mutating a live config in place.
        destination = Path(self.environment["docker"]["config_volume_mount"])
        if str(destination) != "/runtime-config" or not destination.is_dir():
            raise BoundaryError("tools container must mount the project config volume at /runtime-config")
        generation = destination / "releases" / digest
        generation.mkdir(parents=True, exist_ok=True)
        files = {name:(self.output / name).read_bytes() for name in ["router.yaml","envoy.yaml"]}
        files.update(model_files)
        files.update({name:(ROOT / "tools/container" / name).read_bytes() for name in RUNTIME_WRAPPERS})
        if self.mock:
            files["mock_backend.py"] = (ROOT / "tests/overlays/mock/mock_backend.py").read_bytes()
        for name, data in files.items():
            target = generation / name
            if target.exists():
                if target.read_bytes() != data:
                    raise ValueError("immutable Compose configuration digest collision")
            else:
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_bytes(data)
                temporary.replace(target)

    def check_contract(self):
        candidate_test = self.args.candidate_test and self.mock and self.release.get("status") == "candidate"
        if self.args.candidate_test and not candidate_test:
            raise ValueError("--candidate-test requires an explicit mock overlay and a candidate release")
        if self.release.get("status") not in {"preview", "released"} and not candidate_test:
            raise ValueError("SR release is not deployable: image/build/acceptance evidence is incomplete")
        candidates = [i for i in self.release.get("images", []) if i.get("platform") == self.environment["platform"]]
        if not candidates:
            raise ValueError(f'no verified SR image for {self.environment["platform"]}')
        selected = candidates[0]
        if selected.get("acceptance") not in {"tested-with-mock", "tested-with-real-backend"} and not (candidate_test and selected.get("acceptance") == "built-unit-tested"):
            raise ValueError("SR image lacks real gateway acceptance evidence")
        if "@sha256:" not in selected["reference"]:
            raise ValueError("SR release must reference an immutable image digest")
        required = {"chat" if m["api_format"] == "openai" else m["api_format"] for m in self.models}
        if self.config and self.config.document.get('alp', {}).get('enabled'):
            required.add('alp_chat')
        available = set(selected.get("interfaces", []))
        if not required <= available or not self.release.get("config", {}).get("require_entrypoint"):
            raise ValueError(f"release lacks required interfaces/entrypoint enforcement: {sorted(required - available)}")
        declared = {cap for model in self.models for cap in model.get("capabilities", [])}
        transport = set(selected.get("request_capabilities", []))
        if not declared <= transport:
            raise ValueError(f"SR release cannot preserve declared model request capabilities: {sorted(declared - transport)}")
        self.images["router"] = selected["reference"]

    def check_resources(self):
        if self.mock:
            return
        local = {m["pool"] for m in self.models if m["service"] in self.backends} & {"vllm", "omni"}
        if len(local) > 1 and not self.environment["resources"].get("local_engines_concurrent"):
            raise ValueError("target lacks verified concurrent-engine capacity; use separate explicit scenarios or --overlay mock")
        if self.runtime == "docker" and local:
            if not self.environment["resources"].get("gpu_device_ids"):
                raise ValueError("no container-visible compatible GPU has been verified")
            if self.environment["resources"].get("real_models") != "verified":
                raise ValueError("existing read-only weights have not passed model/format/engine compatibility audit")

    def check_credentials(self):
        if getattr(self, 'config', None):
            values = self.config.credentials()
            private = ROOT / 'secrets' / self.args.environment
            stale = [k for k, v in values.items() if not (private / k).is_file() or (private / k).read_text().strip() != v]
            if stale:
                raise ValueError('credential material is missing or stale; run scripts/credentials.py --config with the same stack file')
            return
        required = {"GATEWAY_TOKEN"}
        if not self.mock:
            required.update(m["api_key_env"] for m in self.models)
        else:
            required.add("MODEL_API_KEY")
        private = ROOT / "secrets" / self.args.environment
        missing = sorted(key for key in required if not (private / key).is_file() or not (private / key).read_bytes().strip())
        if missing:
            raise ValueError("missing non-empty credential files: " + ", ".join(missing) + "; run scripts/credentials.py with an explicit private source")

    def render(self, *, stage=True):
        output = self.output
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.is_symlink():
            raise ValueError('generated output directory must not be a symlink')
        # Build a complete generation first. Failed validation leaves the previous
        # files intact, and switching enabled models cannot retain stale fragments.
        with tempfile.TemporaryDirectory(prefix='.render-', dir=output.parent) as directory:
            self.output = Path(directory)
            try:
                self._render(stage=stage)
                backup = None
                if output.exists():
                    backup = Path(tempfile.mkdtemp(prefix='.previous-', dir=output.parent))
                    backup.rmdir()
                    output.rename(backup)
                try:
                    self.output.rename(output)
                except OSError:
                    if backup:
                        backup.rename(output)
                    raise
                if backup:
                    shutil.rmtree(backup)
            finally:
                self.output = output
        return output

    def _render(self, *, stage=True):
        self.output.mkdir(parents=True, exist_ok=True)
        scopes = {}
        for scope, pools in [("local-only", {"vllm", "omni"}), ("cloud-only", {"cloud"})]:
            candidates = [m for m in self.models if m["pool"] in pools]
            if candidates:
                scopes[scope] = candidates
        replicas = {s: (0 if s in self.state["stopped"] else int(d.get("replicas", 1)))
                    for s,d in self.backends.items()}
        common = dict(models=self.models, scopes=scopes, environment=self.environment, images=self.images,
                      cluster=self.config.document.get('cluster') if self.config else None,
                      alp=self.config.document.get('alp') if self.config else None,
                      video_models=self.video_models,
                      media_store_replicas=0 if "media-bindings" in self.state["stopped"] else 1,
                      pools=self.pools, mock=self.mock, replicas=replicas,
                      backends=self.backends, backend_key_env=self.backend_key_env,
                      router_replicas=0 if "router" in self.state["stopped"] else 1,
                      envoy_replicas=0 if "envoy" in self.state["stopped"] else 1)
        router = self.renderer.get_template("config/router/config.yaml.j2").render(**common)
        (self.output / "router.yaml").write_text(router)
        write_yaml(self.output / "envoy.yaml", render_envoy(self.models))
        credential_revision = ROOT / ".state" / self.args.environment / "credentials.version"
        material = router.encode() + (self.output / "envoy.yaml").read_bytes()
        if credential_revision.exists():
            material += credential_revision.read_bytes()
        if self.mock:
            material += (ROOT / "tests/overlays/mock/mock_backend.py").read_bytes()
        model_files = dict(self.config.inline_files) if self.config else {}
        for deployment in self.backends.values():
            for filename, source in deployment.get("config_files", {}).items():
                path = (ROOT / source).resolve()
                if Path(filename).name != filename or filename in {"router.yaml", "envoy.yaml", "mock_backend.py", "compose.yaml", "kustomization.yaml"}:
                    raise ValueError("model config filenames must be unique plain basenames")
                if not path.is_relative_to(ROOT) or not path.is_file():
                    raise ValueError("model config source must be a file inside this project")
                data = path.read_bytes()
                if filename in model_files and model_files[filename] != data:
                    raise ValueError("conflicting model config file: " + filename)
                model_files[filename] = data
        if sum(map(len, model_files.values())) > 900000:
            raise ValueError("model config files exceed the ConfigMap size budget")
        for filename, data in sorted(model_files.items()):
            (self.output / filename).write_bytes(data)
            material += filename.encode() + data
        if self.runtime == "docker":
            for name in RUNTIME_WRAPPERS:
                material += name.encode() + (ROOT / "tools/container" / name).read_bytes()
        common["config_sha"] = hashlib.sha256(material).hexdigest()
        common["backend_services"] = list(self.backends)
        common["model_commands"] = {s:d.get("command", []) for s,d in self.backends.items()}
        if self.runtime == "docker":
            fragments = {}
            for filename in ["compose.common.yaml", "compose.models.yaml", "compose.yaml"]:
                text = self.renderer.get_template("deploy/docker/" + filename + ".j2").render(**common)
                (self.output / filename).write_text(text)
                fragments[filename] = yaml.safe_load(text)
            # Check the complete mount/port boundary before invoking Compose.
            # Native include.path performs the actual merge and validation.
            complete = fragments["compose.common.yaml"]
            complete["name"] = self.environment["project"]
            complete["services"].update(fragments["compose.models.yaml"]["services"] or {})
            validate_compose(complete, self.environment)
            if stage:
                self.stage_compose_config(common["config_sha"], model_files)
        else:
            resources = []
            for category in ["router", "envoy", "models"]:
                text = self.renderer.get_template(f"deploy/k8s/{category}/deployment.yaml.j2").render(**common)
                filename = f"deployment-{category}.yaml"
                (self.output / filename).write_text(text)
                resources.append(filename)
            if self.video_models:
                filename = "media-bindings.yaml"
                (self.output / filename).write_text(self.renderer.get_template("deploy/k8s/media-bindings.yaml.j2").render(**common))
                resources.append(filename)
            if self.backends:
                claims = ([f'{kind}-{service}' for service, backend in self.backends.items()
                           if not backend.get('existing_claims') for kind in ('model-cache','model-outputs')]
                          if common['cluster'] else ['model-cache','model-outputs'])
                write_yaml(self.output / "storage.yaml", {"apiVersion":"v1", "kind":"List", "items":[
                    {"apiVersion":"v1", "kind":"PersistentVolumeClaim", "metadata":{"name":name},
                     "spec":{"accessModes":["ReadWriteOnce"], "resources":{"requests":{"storage":"5Gi"}}}}
                    for name in claims]})
                resources.append("storage.yaml")
            mock_file = ROOT / "tests/overlays/mock/mock_backend.py"
            if mock_file.exists():
                (self.output / "mock_backend.py").write_text(mock_file.read_text())
            else:
                (self.output / "mock_backend.py").write_text("raise RuntimeError('mock fixture not installed')\n")
            write_yaml(self.output / "kustomization.yaml", {
                "apiVersion":"kustomize.config.k8s.io/v1beta1", "kind":"Kustomization",
                "namespace":self.environment["kubernetes"]["namespace"],
                "labels":[{"pairs":{"app.kubernetes.io/part-of":"inference-stack"}, "includeSelectors":False}],
                "resources":resources,
                "generatorOptions":{"disableNameSuffixHash":True},
                "configMapGenerator":[{"name":"router-config", "files":["router.yaml"]},
                    {"name":"envoy-config", "files":["envoy.yaml"]},
                    {"name":"backend-config", "files":["mock_backend.py", *sorted(model_files)]}],
            })
        if self.config:
            write_yaml(self.output / 'resolved.yaml', self.config.document)
            for service, backend in self.config.document['backends'].items():
                if self.mock or 'mlx' not in backend or service not in {m['service'] for m in self.models}:
                    continue
                directory = self.output / 'host-mlx' / service
                directory.mkdir(parents=True, exist_ok=True)
                settings = self.config.mlx_settings(service)
                (directory / 'settings.json').write_text(json.dumps(settings, indent=2) + '\n')
                mlx = backend['mlx']
                host_root = Path(self.config.document['hosts'][backend['host']]['root'])
                plist = {'Label': 'local.inference-stack.' + self.config.name + '.' + service,
                         'ProgramArguments': [mlx.get('python','/usr/bin/python3'), str(host_root / ('tools/host-embedding/serve.py' if mlx.get('engine') == 'mlx-embeddings' else 'tools/host-mlx/serve.py')),
                                              '--settings', str(Path(mlx['home']) / 'settings.json')],
                         'WorkingDirectory': mlx['home'], 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30,
                         'StandardOutPath': str(Path(mlx['home']) / 'logs/server.log'),
                         'StandardErrorPath': str(Path(mlx['home']) / 'logs/server.log')}
                (directory / 'service.plist').write_bytes(plistlib.dumps(plist))
        self.state["config_sha"] = common["config_sha"]
        return self.output

    def snapshot(self):
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.output.iterdir()
                 if p.is_file() and p.name != 'resolved.yaml'}
        external = sorted({m['service'] for m in self.models if m['service'] not in self.backends})
        host_files = {str(p.relative_to(self.output)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (self.output / 'host-mlx').rglob('*') if p.is_file()}
        return {'services': sorted(self.enabled_services), 'files': files, 'external': external,
                'host_files': host_files, 'stopped': sorted(self.state['stopped'])}

    def plan(self):
        if not self.config:
            raise ValueError('plan requires --config')
        # Render elsewhere: inspecting a plan cannot replace the active Compose files.
        previous_output = self.output
        try:
            with tempfile.TemporaryDirectory(prefix='inference-plan-') as directory:
                self.output = Path(directory)
                self.render(stage=False)
                desired = self.snapshot()
        finally:
            self.output = previous_output
        baseline = self.state.get('applied_snapshot')
        before = set(baseline['services']) if baseline else set()
        after = set(desired['services'])
        changed = baseline is not None and baseline['files'] != desired['files']
        result = {'basis': 'last successful local deployment; live cluster drift not inspected',
                  'baseline_available': baseline is not None,
                  'create': sorted(after - before), 'remove': sorted(before - after),
                  'update_may_restart': sorted(before & after) if changed else [],
                  'remain_stopped': sorted(after & set(self.state['stopped'])),
                  'external_not_managed': desired['external'],
                  'host_configuration_changed': (baseline or {}).get('host_files', {}) != desired['host_files'],
                  'host_configuration_action': 'explicit host installation/restart required; gateway deploy does not apply it',
                  'credential_values': 'omitted; run credentials separately after changing secrets'}
        if baseline is None:
            result['note'] = 'No baseline: listed creates are desired resources, not a claim that the target is empty.'
        print(json.dumps(result, indent=2))

    def check(self):
        if self.config:
            if not self.args.config_only:
                self.check_contract()
                self.check_resources()
                self.config.credentials()
            self.render(stage=False)
            print('Unified configuration and generated files: valid (offline; no cluster or engine calls)')
            return
        if not self.args.config_only:
            self.check_contract()
            self.check_resources()
            self.check_credentials()
        if self.runtime == "docker":
            self.check_docker_target()
        self.render()
        if self.runtime == "kubernetes":
            self.kubectl("apply", "--dry-run=client", "-k", self.output)
        else:
            self.compose("config", "--quiet")

    def deploy(self):
        self.check_contract()
        self.check_resources()
        self.check_credentials()
        if self.runtime == "docker":
            self.check_docker_target()
        self.render()
        self.state.update(example=self.example, overlay="mock" if self.mock else "real", status="applying",
                          catalog=str(self.catalog_path.relative_to(ROOT)) if self.catalog_path else None)
        if self.config:
            self.state.update(target=self.config.target(), config_path=str(self.config.path),
                              config_fingerprint=self.config.fingerprint())
        self.save()
        try:
            if self.runtime == "kubernetes":
                self.kubectl("get", "secret/inference-credentials", "-o", "name")
                existing = json.loads(self.kubectl("get", "deployments", "-l", LABEL, "-o", "json", capture=True))
                obsolete = sorted({d["metadata"]["name"] for d in existing["items"]} - set(self.enabled_services))
                # An explicit example switch removes only this project's prior
                # services. It never scales unrelated engines to free a GPU.
                self.state["removing_services"] = obsolete
                self.save()
                for service in obsolete:
                    self.kubectl("delete", "deployment,service", service, "--ignore-not-found", "--wait=true")
                self.kubectl("apply", "-k", self.output)
                for service in self.enabled_services:
                    if service not in self.state["stopped"]:
                        self.kubectl("rollout", "status", f"deployment/{service}", "--timeout=900s")
                inventory = json.loads(self.kubectl("get", "deployment,service,configmap,pvc", "-l", LABEL, "-o", "json", capture=True))
                self.state["resources"] = [{"kind":i["kind"], "name":i["metadata"]["name"], "uid":i["metadata"]["uid"]}
                                           for i in inventory["items"]]
            else:
                args = ["up", "-d", "--wait", "--wait-timeout", "900", "--remove-orphans"]
                for service in self.state["stopped"]:
                    if service in self.enabled_services:
                        args += ["--scale", f"{service}=0"]
                self.compose(*args)
                self.tools_network(True)
                self.state["resources"] = self.compose_resources()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            self.state["status"] = "apply-failed-rerun-same-deploy-to-reconcile"
            self.save()
            raise
        if self.config:
            self.state['applied_snapshot'] = self.snapshot()
        self.state["status"] = "ready-for-end-to-end-test"
        self.save()

    def operate(self):
        action = self.args.action
        if action == "plan":
            self.plan(); return
        if action == "render":
            if not self.args.config_only:
                self.check_contract()
                self.check_resources()
            print(self.render(stage=not bool(self.config))); return
        if action == "check":
            self.check(); return
        if action == "deploy":
            self.deploy(); return
        if action in {"start", "stop", "logs"}:
            if self.args.service not in self.enabled_services:
                raise ValueError("--service must name a managed service")
            service = self.args.service
            if action == "logs":
                if self.runtime == "kubernetes": self.kubectl("logs", f"deployment/{service}", "--tail=100")
                else: self.render(); self.compose("logs", "--tail=100", service)
                return
            count = int(self.backends.get(service, {}).get("replicas", 1)) if action == "start" else 0
            if action == "stop":
                # Record operator intent before the API call. A failed wait
                # must not let the next deploy silently undo an explicit stop.
                self.state["stopped"] = sorted(set(self.state["stopped"]) | {service})
                self.save()
            if self.runtime == "kubernetes":
                self.kubectl("scale", f"deployment/{service}", f"--replicas={count}")
                self.kubectl("rollout", "status", f"deployment/{service}", "--timeout=900s")
                if action == "stop":
                    self.kubectl("wait", "--for=delete", "pod", "-l", f"app={service}", "--timeout=120s")
            else:
                if action == "start":
                    self.check_contract()
                    self.check_resources()
                    self.check_credentials()
                    self.check_docker_target()
                self.render()
                if action == "stop": self.compose("stop", service)
                else: self.compose("up", "-d", "--wait", "--wait-timeout", "900", "--scale", f"{service}={count}", service)
            stopped = set(self.state["stopped"])
            if action == "stop": stopped.add(service)
            else: stopped.discard(service)
            self.state["stopped"] = sorted(stopped); self.save(); return
        if action == "status":
            if self.runtime == "kubernetes": self.kubectl("get", "pods,deployments,services,pvc", "-l", LABEL, "-o", "wide")
            else: self.render(); self.compose("ps", "--all")
            return
        if action == "down":
            if self.runtime == "kubernetes":
                self.kubectl("delete", "deployment,service,configmap", "-l", LABEL, "--ignore-not-found")
            else:
                self.render()
                self.tools_network(False)
                self.compose("down", "--remove-orphans")
            self.state["status"] = "down-data-retained"; self.save(); return
        if action == "test":
            if self.runtime == "docker":
                self.tools_network(True)
            command = [sys.executable, ROOT / ("tests/integration/run.py" if self.mock else "tests/integration/real.py")]
            if self.config:
                command += ['--config', str(self.config.path)]
            else:
                command += ["--environment", self.args.environment, "--runtime", self.runtime, "--example", self.example,
                            "--overlay", "mock" if self.mock else "real", "--catalog", str(self.catalog_path.relative_to(ROOT))]
            execute(command); return
        raise ValueError("unknown action")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check","plan","render","deploy","start","stop","status","logs","test","down"])
    parser.add_argument("--config", type=Path, help="single stack.yaml deployment configuration")
    parser.add_argument("--runtime", choices=["kubernetes","docker"])
    parser.add_argument("--environment", choices=sorted(p.name for p in (ROOT / "environments").iterdir() if (p / "environment.yaml").is_file()))
    parser.add_argument("--example", choices=sorted(p.name for p in (ROOT / "examples").iterdir() if (p / "example.yaml").is_file()))
    parser.add_argument("--overlay", choices=["mock"])
    parser.add_argument("--service")
    parser.add_argument("--release")
    parser.add_argument("--catalog", help="project-relative model catalog; saved for subsequent operations")
    parser.add_argument("--candidate-test", action="store_true", help="allow a built, unit-tested candidate only with explicit mock backends")
    parser.add_argument("--config-only", action="store_true", help="validate deployment files without claiming deployable release")
    args = parser.parse_args()
    try:
        if not args.config and (not args.runtime or not args.environment):
            raise ValueError('provide --config, or both legacy --runtime and --environment')
        Stack(args).operate()
    except (ValueError, BoundaryError, FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"inference-stack: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
