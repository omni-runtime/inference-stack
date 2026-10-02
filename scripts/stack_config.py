"""Load one explicit deployment document; never merge desired state from inventory."""
import copy
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
import yaml

ROOT = Path(__file__).resolve().parents[1]


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError(f"configuration has a duplicate/non-string key at line {key_node.start_mark.line + 1}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def read_document(path):
    try:
        return yaml.load(Path(path).read_text(), Loader=UniqueLoader)
    except yaml.YAMLError as error:
        mark = getattr(error, 'problem_mark', None)
        location = f" at line {mark.line + 1}" if mark else ''
        raise ValueError(f"invalid YAML{location}; check indentation and field syntax") from None


def validate_document(document):
    schema = json.loads((ROOT / 'schemas/stack.schema.json').read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda e: str(list(e.path)))
    if errors:
        error = errors[0]
        location = '.'.join(map(str, error.absolute_path)) or '<root>'
        # jsonschema's full message may include a mistakenly pasted credential.
        raise ValueError(f"{location}: invalid configuration ({error.validator}); see schemas/stack.schema.json")


def check_url(value, field):
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (parsed.scheme in {'http', 'https'} and parsed.hostname and
                 not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and
                 (port is None or 0 < port < 65536))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f'{field}: requires an HTTP(S) address without embedded credentials, query or fragment')


def read_secrets(path):
    """Literal dotenv subset: no shell execution or implicit environment expansion."""
    values = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        name = name.strip()
        if not separator or not re.fullmatch(r'[A-Z][A-Z0-9_]*', name) or name in values:
            raise ValueError(f'secrets file: invalid or duplicate name at line {number}')
        value = value.strip()
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f'secrets file: unmatched quote at line {number}')
            value = value[1:-1]
        values[name] = value
    return values


class StackConfig:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.document = read_document(self.path)
        validate_document(self.document)
        doc = self.document
        self.name = doc['name']
        self.mock = doc['mode'] == 'mock'
        self.inline_files = {}
        gateway = doc['gateway']
        runtime = gateway['runtime']
        if runtime not in gateway or ({'kubernetes', 'docker'} - {runtime}) & gateway.keys():
            raise ValueError('gateway: exactly the selected runtime configuration must be present')
        if gateway['host'] not in doc['hosts']:
            raise ValueError('gateway.host: unknown host')
        for name, host in doc['hosts'].items():
            if not Path(host['root']).is_absolute():
                raise ValueError(f'hosts.{name}.root: remote project directory must be absolute')
        host = doc['hosts'][gateway['host']]
        self.environment = dict(name=self.name, runtime=runtime, platform=gateway['platform'],
                                project=gateway['project'], root=host['root'],
                                resources=copy.deepcopy(doc['resources']))
        self.environment[runtime] = copy.deepcopy(gateway[runtime])
        self.environment[runtime]['ssh'] = host['ssh']
        for field in ('container_only', 'verification_not_before'):
            if field in gateway:
                self.environment[field] = gateway[field]
        if runtime == 'kubernetes':
            self.environment[runtime]['kubeconfig'] = str(self.resolve(gateway[runtime]['kubeconfig']))
        else:
            self.environment[runtime]['project'] = gateway['project']
        if gateway.get('url'):
            check_url(gateway['url'], 'gateway.url')
        self.release_path = self.resolve(doc['artifacts']['release'])
        self.versions_path = self.resolve(doc['artifacts']['versions'])
        self.environment['release'] = str(self.release_path)
        self.images = read_document(self.versions_path)['images']
        self.release = read_document(self.release_path)
        self.secrets_path = self.resolve(doc['secrets']['file'])
        self._validate_cluster()
        self._validate_backends()
        names = [model['name'] for model in doc['models']]
        if len(names) != len(set(names)):
            raise ValueError('models: duplicate model name')
        if set(doc['routing']['enabled_models']) - set(names):
            raise ValueError('routing.enabled_models: unknown model reference')
        for model in doc['models']:
            if model['backend'] not in doc['backends']:
                raise ValueError(f"models.{model['name']}.backend: unknown backend")
        for model in doc['models']:
            embedding = model.get('embedding')
            if embedding and embedding.get('min_dimensions', embedding['dimensions']) > embedding['dimensions']:
                raise ValueError(f"models.{model['name']}.embedding: min_dimensions exceeds dimensions")
            if embedding and embedding.get('allowed_dimensions'):
                allowed = embedding['allowed_dimensions']
                low = embedding.get('min_dimensions', embedding['dimensions'])
                if embedding['dimensions'] not in allowed or any(d < low or d > embedding['dimensions'] for d in allowed):
                    raise ValueError(f"models.{model['name']}.embedding: allowed_dimensions must include the default and stay within the declared range")
        self.models = []
        for name in doc['routing']['enabled_models']:
            model = copy.deepcopy(next(m for m in doc['models'] if m['name'] == name))
            service = model.pop('backend')
            backend = doc['backends'][service]
            model.update({k: backend[k] for k in ('pool', 'provider', 'base_url', 'api_key_env')})
            model['service'] = service
            model['deployment'] = copy.deepcopy(backend['deployment'])
            if 'node' in model['deployment']:
                worker = doc['cluster']['workers'][model['deployment']['node']]
                model['deployment']['placement'] = copy.deepcopy(worker)
            for filename, data in model['deployment'].pop('config_files', {}).items():
                payload = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode()
                if filename in self.inline_files and self.inline_files[filename] != payload:
                    raise ValueError(f'backends.{service}.deployment.config_files: conflicting filename')
                self.inline_files[filename] = payload
            if model['api_format'] == 'mlx_video' and 'mlx' in backend:
                manifest = json.loads(self.resolve(backend['mlx']['model_manifest']).read_text())
                if model.get('revision', manifest['sha']) != manifest['sha']:
                    raise ValueError(f'models.{name}.revision: differs from the locked MLX manifest')
                model['revision'] = manifest['sha']
                model.setdefault('model_directory', backend['mlx']['model_directory'])
            self.models.append(model)
        self.pools = list(dict.fromkeys(m['pool'] for m in self.models))
        if not self.mock and any(m['deployment']['mode'] == 'managed' for m in self.models):
            if 'model_memory' not in doc['resources']:
                raise ValueError('resources.model_memory: required for managed engines')
            if runtime == 'kubernetes' and not doc.get('cluster'):
                for field in ('runtime_class', 'model_root'):
                    if field not in gateway['kubernetes']:
                        raise ValueError(f'gateway.kubernetes.{field}: required for managed engines')
                if doc['resources'].get('gpu_count', 0) < 1:
                    raise ValueError('resources.gpu_count: managed engines require an available NVIDIA GPU')
        self.required_keys = {'GATEWAY_TOKEN'} | ({'MODEL_API_KEY'} if self.mock else {m['api_key_env'] for m in self.models})
        if self.mock:
            self.required_keys.update(m['api_key_env'] for m in self.models)
        if doc.get('alp', {}).get('enabled'):
            alp = doc['alp']
            if runtime != 'kubernetes':
                raise ValueError('alp: the private contract worker currently requires Kubernetes')
            for field in ('worker_directory', 'worker_mount', 'python', 'catalog_file'):
                if not Path(alp[field]).is_absolute():
                    raise ValueError(f'alp.{field}: requires an absolute path')
            if not Path(alp['catalog_file']).is_relative_to(alp['worker_mount']):
                raise ValueError('alp.catalog_file: must be inside the private worker mount')
            for name in alp['models']:
                model = next((m for m in self.models if m['name'] == name), None)
                if not model or model['api_format'] != 'openai' or 'tools' not in model['capabilities']:
                    raise ValueError('alp.models: requires an enabled OpenAI Chat model with tools capability')
            self.required_keys.add(alp['task_key_env'])

    def _validate_cluster(self):
        doc = self.document
        cluster = doc.get('cluster')
        if not cluster:
            return
        gateway = doc['gateway']
        if gateway['runtime'] != 'kubernetes':
            raise ValueError('cluster: requires a Kubernetes gateway')
        check_url(cluster['server'], 'cluster.server')
        if not cluster['server'].startswith('https://'):
            raise ValueError('cluster.server: requires TLS')
        master = cluster['control_plane']
        if (master['host'], master['node'], master['platform']) != (
                gateway['host'], gateway['kubernetes']['node'], gateway['platform']):
            raise ValueError('cluster.control_plane: must match the gateway host, node and platform')
        nodes = [master, *cluster['workers'].values()]
        if len({n['node'] for n in nodes}) != len(nodes):
            raise ValueError('cluster: duplicate Kubernetes node name')
        if len({n['address'] for n in nodes}) != len(nodes):
            raise ValueError('cluster: duplicate node address')
        if 'control-plane' in cluster['workers']:
            raise ValueError('cluster.workers: control-plane is a reserved name')
        for node in nodes:
            try:
                ipaddress.ip_address(node['address'])
            except ValueError:
                raise ValueError('cluster.address: requires a node IP address') from None
            if node['host'] not in doc['hosts']:
                raise ValueError('cluster: unknown host')
            for field in ('model_root', 'data_dir'):
                if field in node and not Path(node[field]).is_absolute():
                    raise ValueError(f'cluster.{field}: requires an absolute host path')

    def resolve(self, value):
        return (self.path.parent / value).resolve()

    def _validate_backends(self):
        doc = self.document
        gateway = doc['gateway']
        ports = set()
        if gateway['runtime'] == 'kubernetes':
            ports.add(gateway['kubernetes']['gateway_node_port'])
        host_ports = set()
        for service, backend in doc['backends'].items():
            field = f'backends.{service}'
            if service in {'router', 'envoy', 'media-bindings'}:
                raise ValueError(f'{field}: reserved service name')
            check_url(backend['base_url'], field + '.base_url')
            if backend.get('host') and backend['host'] not in doc['hosts']:
                raise ValueError(f'{field}.host: unknown host')
            deployment = backend['deployment']
            if deployment['mode'] == 'external':
                if set(deployment) != {'mode'}:
                    raise ValueError(f'{field}.deployment: external services cannot declare managed options')
            else:
                node = deployment.get('node')
                if deployment.get('existing_claims') and not doc.get('cluster'):
                    raise ValueError(f'{field}.deployment.existing_claims: requires cluster placement')
                if node:
                    worker = doc.get('cluster', {}).get('workers', {}).get(node)
                    if not worker:
                        raise ValueError(f'{field}.deployment.node: unknown cluster worker')
                    if backend.get('host', worker['host']) != worker['host']:
                        raise ValueError(f'{field}.host: does not match the selected worker')
                    if not self.mock and (not worker.get('runtime_class') or not worker.get('model_root') or worker.get('gpu_count', 0) < 1):
                        raise ValueError(f'{field}: worker needs runtime_class, model_root and GPU capacity')
                    if not self.mock and worker['platform'] != 'linux/amd64':
                        raise ValueError(f'{field}: the locked NVIDIA engine images require linux/amd64')
                    namespace = gateway['kubernetes']['namespace']
                    address = urlsplit(backend['base_url'])
                    names = {service, f'{service}.{namespace}', f'{service}.{namespace}.svc',
                             f'{service}.{namespace}.svc.cluster.local'}
                    if address.scheme != 'http' or address.hostname.rstrip('.') not in names or address.port != 8000:
                        raise ValueError(f'{field}.base_url: managed cluster backends must use their own Service DNS on port 8000')
                elif doc.get('cluster'):
                    raise ValueError(f'{field}.deployment.node: select a worker for managed inference')
                if backend['pool'] == 'cloud' or (not node and backend.get('host') and backend['host'] != gateway['host']):
                    raise ValueError(f'{field}: managed services must run on the gateway target')
                if 'command' not in deployment or deployment.get('image') not in self.images:
                    raise ValueError(f'{field}.deployment: requires command and a locked image')
                if 'node_port' in deployment:
                    if deployment['node_port'] in ports:
                        raise ValueError(f'{field}.deployment.node_port: duplicate port')
                    ports.add(deployment['node_port'])
                for filename in deployment.get('config_files', {}):
                    if filename in {'router.yaml','envoy.yaml','mock_backend.py','compose.yaml','kustomization.yaml'}:
                        raise ValueError(f'{field}.deployment.config_files: reserved filename')
            if 'mlx' in backend:
                mlx = backend['mlx']
                if deployment['mode'] != 'external' or 'host' not in backend:
                    raise ValueError(f'{field}.mlx: requires an external backend and an explicit host')
                if not Path(mlx['home']).is_absolute():
                    raise ValueError(f'{field}.mlx.home: must be an absolute host path')
                endpoint = (backend['host'], mlx.get('port', 11234))
                if endpoint in host_ports:
                    raise ValueError(f'{field}.mlx.port: duplicate host listener port')
                host_ports.add(endpoint)
                manifest = json.loads(self.resolve(mlx['model_manifest']).read_text())
                lock = json.loads(self.resolve(mlx['engine_lock']).read_text())
                if manifest['sha'] != lock['revision'] or manifest['id'] != lock['model']:
                    raise ValueError(f'{field}.mlx: engine/model locks disagree')

    def credentials(self):
        values = read_secrets(self.secrets_path)
        missing = sorted(k for k in self.required_keys if not values.get(k) or values[k].startswith('replace-with-'))
        if missing:
            raise ValueError('secrets: missing/non-configured values for ' + ', '.join(missing))
        return {k: values[k] for k in sorted(self.required_keys)}

    def target(self):
        env = self.environment
        if env['runtime'] == 'kubernetes':
            return {k: env['kubernetes'][k] for k in ('context','kubeconfig','namespace')}
        return {'context': env['docker']['context'], 'project': env['project']}

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.document, sort_keys=True).encode()).hexdigest()

    def mlx_settings(self, service):
        if self.mock:
            raise ValueError('MLX host operations require mode: real')
        backend = self.document['backends'].get(service, {})
        if 'mlx' not in backend:
            raise ValueError('selected backend has no MLX host configuration')
        mlx = backend['mlx']
        root = Path(mlx['home'])
        manifest = json.loads(self.resolve(mlx['model_manifest']).read_text())
        settings = dict(binary=str(root / 'bin/mlx-serve-macos-arm64/mlx-serve'),
                    model=str(root / 'models' / mlx['model_directory']), revision=manifest['sha'],
                    secret_file=str(root / 'secrets' / backend['api_key_env']),
                    listen=mlx.get('listen', '127.0.0.1'), port=mlx.get('port',11234),
                    max_concurrent=mlx.get('max_concurrent',1), timeout=mlx.get('timeout',3600))
        if mlx.get('engine') == 'mlx-embeddings':
            settings.pop('binary')
            models = [model for model in self.models if model['service'] == service]
            if len(models) != 1 or models[0]['api_format'] != 'embeddings' or mlx.get('max_concurrent', 1) != 1:
                raise ValueError('MLX embeddings requires exactly one embedding model and concurrency 1')
            model = models[0]
            embedding = model['embedding']
            settings.update(engine='mlx-embeddings', model_id=model['provider_model_id'],
                            dimensions=embedding['dimensions'], min_dimensions=embedding.get('min_dimensions', embedding['dimensions']),
                            max_batch_size=embedding['max_batch_size'],
                            max_input_tokens=mlx.get('max_input_tokens', model['context_window']),
                            max_pixels=mlx.get('max_pixels', 262144), memory_limit_gib=mlx.get('memory_limit_gib', 8))
        return settings


def reject_mixed_args(args):
    if getattr(args, 'config', None):
        conflicting = [name for name in ('environment','runtime','example','catalog','release','overlay')
                       if getattr(args, name, None) is not None]
        if conflicting:
            raise ValueError('--config cannot be combined with ' + ', '.join('--'+name for name in conflicting))
