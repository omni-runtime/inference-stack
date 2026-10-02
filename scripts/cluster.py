#!/usr/bin/env python3
"""Render, inspect or join workers to the single cluster declared by stack.yaml."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

import yaml

from stack_config import StackConfig


def agent_config(config, worker, token_path):
    cluster = config.document.get('cluster')
    if not cluster or worker not in cluster['workers']:
        raise ValueError('select a declared cluster worker')
    node = cluster['workers'][worker]
    return {
        'server': cluster['server'], 'token-file': str(token_path),
        'node-name': node['node'], 'node-ip': node['address'],
        'flannel-iface': node['flannel_interface'],
        'data-dir': node.get('data_dir', '/var/lib/rancher/k3s-inference-stack'),
        # Kubernetes reserves node-role labels for an administrator; kubelet
        # rejects them on recent releases. This label is owned by our stack.
        'node-label': [f'inference-stack/worker={worker}'],
        'kubelet-arg': node.get('kubelet_args', []),
    }


def inspect_nodes(config, nodes):
    cluster = config.document['cluster']
    observed = {n['metadata']['name']: n for n in nodes['items']}
    rows = []
    for role, node in [('control-plane', cluster['control_plane']), *cluster['workers'].items()]:
        actual = observed.get(node['node'], {})
        labels = actual.get('metadata', {}).get('labels', {})
        status = actual.get('status', {})
        ready = any(x['type'] == 'Ready' and x['status'] == 'True' for x in status.get('conditions', []))
        is_master = 'node-role.kubernetes.io/control-plane' in labels
        version = status.get('nodeInfo', {}).get('kubeletVersion')
        address = any(x['type'] == 'InternalIP' and x['address'] == node['address'] for x in status.get('addresses', []))
        gpu = int(status.get('allocatable', {}).get('nvidia.com/gpu', 0))
        valid = (ready and address and version == cluster['version'] and
                 labels.get('kubernetes.io/arch') == node['platform'].split('/')[1] and
                 is_master == (role == 'control-plane') and gpu >= node.get('gpu_count', 0))
        rows.append({'node': node['node'], 'role': role, 'ready': ready, 'gpu': gpu, 'valid': valid})
    return rows


def install_worker(config, worker, token_file, directory, backup):
    """Run on the worker after an operator has preserved and stopped an old cluster.

    Migration is intentionally separate: joining must never destroy an existing
    server database, persistent volumes, manifests or running foreign workloads.
    """
    if os.geteuid() != 0 or platform.system() != 'Linux':
        raise ValueError('install-worker must run as root on the Linux worker')
    node = config.document['cluster']['workers'][worker]
    architecture = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if node['platform'] != 'linux/' + str(architecture):
        raise ValueError('worker platform differs from this machine')
    interfaces = json.loads(subprocess.check_output(['ip', '-j', 'address', 'show', 'dev', node['flannel_interface']]))
    if not any(a.get('local') == node['address'] for interface in interfaces for a in interface.get('addr_info', [])):
        raise ValueError('declared node address is not on this machine and flannel interface')
    binary = shutil.which('k3s')
    if not binary or config.document['cluster']['version'] not in subprocess.check_output([binary, '--version'], text=True).split():
        raise ValueError('install the exact declared K3s version before joining')
    for service in ('k3s', 'k3s-agent'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode == 0:
            raise ValueError(f'{service} is active; back up and migrate its workloads before joining')
    token = Path(token_file).read_bytes().strip()
    if not token or b'\n' in token:
        raise ValueError('token file must contain one nonempty join token')
    directory, backup = Path(directory), Path(backup)
    if not directory.is_absolute() or not backup.is_absolute():
        raise ValueError('installation and backup directories must be absolute')
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'inference-stack-agent']).returncode == 0:
        current = directory / 'agent.yaml'
        same = current.is_file() and yaml.safe_load(current.read_text()) == agent_config(config, worker, directory / 'agent-token')
        if same and (directory / 'agent-token').read_bytes().strip() == token:
            return
        raise ValueError('an active stack worker has different settings; migrate it explicitly before replacing its identity')
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    unit = Path('/etc/systemd/system/inference-stack-agent.service')
    if directory.exists():
        shutil.copytree(directory, backup / 'config')
    if unit.exists():
        shutil.copy2(unit, backup / unit.name)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, contents in {
        'agent-token': token,
        'agent.yaml': yaml.safe_dump(agent_config(config, worker, directory / 'agent-token'), sort_keys=False).encode(),
    }.items():
        target = directory / name
        target.write_bytes(contents)
        target.chmod(0o600)
    # Fixed binary/config paths, not arbitrary command fragments from stack.yaml.
    for path in (binary, str(directory)):
        if any(c in path for c in ('\n', '\r', '"', '%')):
            raise ValueError('unsupported systemd path')
    unit.write_text(f'''[Unit]
Description=Inference stack Kubernetes worker
After=network-online.target zerotier-one.service
Wants=network-online.target
Conflicts=k3s.service k3s-agent.service
[Service]
Type=notify
ExecStart="{binary}" agent --config "{directory}/agent.yaml"
KillMode=process
Delegate=yes
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
TimeoutStartSec=0
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
''')
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', unit.name], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    render = sub.add_parser('render-worker')
    render.add_argument('--worker', required=True)
    render.add_argument('--token-path', default='/etc/rancher/k3s-inference-stack/agent-token')
    sub.add_parser('check')
    install = sub.add_parser('install-worker')
    install.add_argument('--worker', required=True)
    install.add_argument('--token-file', type=Path, required=True)
    install.add_argument('--directory', type=Path, default=Path('/etc/rancher/k3s-inference-stack'))
    install.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    try:
        config = StackConfig(args.config)
        if not config.document.get('cluster'):
            raise ValueError('stack.yaml does not declare a cluster')
        if args.action == 'render-worker':
            print(yaml.safe_dump(agent_config(config, args.worker, args.token_path), sort_keys=False), end='')
        elif args.action == 'check':
            kube = config.environment['kubernetes']
            raw = subprocess.check_output(['kubectl', '--kubeconfig', kube['kubeconfig'], '--context', kube['context'], 'get', 'nodes', '-o', 'json'])
            rows = inspect_nodes(config, json.loads(raw))
            print(json.dumps(rows, indent=2))
            return int(not all(r['valid'] for r in rows))
        else:
            install_worker(config, args.worker, args.token_file, args.directory, args.backup)
    except (ValueError, KeyError, FileNotFoundError) as error:
        parser.exit(2, f'cluster: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
