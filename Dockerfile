FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
        sshpass \
        git \
        rsync \
        curl \
        bash \
        podman \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r ansible && useradd -r -g ansible -u 1000 -m ansible

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY ansible.cfg /etc/ansible/ansible.cfg

RUN mkdir -p /workspace/inventory /workspace/roles \
    && chown -R ansible:ansible /workspace /app

ENV WORKSPACE_DIR=/workspace
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000
ENV ANSIBLE_CONFIG=/etc/ansible/ansible.cfg

EXPOSE 8000

USER ansible
CMD ["python", "server.py"]
