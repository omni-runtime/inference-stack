# Operator tools image; serving containers do not receive Docker management access.
FROM docker.io/library/docker@sha256:018edbc908e08fcc9dbf029c812c34251e9b4719e6f71ca0e5eae2a987d014ca AS docker-client
FROM docker.io/library/python@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111
COPY --from=docker-client /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-client /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY tools/container/packages.lock /opt/inference-stack/packages.lock
RUN xargs apk add --no-cache < /opt/inference-stack/packages.lock
COPY requirements.lock /opt/inference-stack/requirements.lock
RUN python -m pip install --no-cache-dir -r /opt/inference-stack/requirements.lock
WORKDIR /workspace/inference-stack
CMD ["python", "-c", "import time; time.sleep(2147483647)"]
