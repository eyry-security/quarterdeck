# Quarterdeck agent sandbox image.
# Baked toolchain so agents don't reinstall on every boot.
# /work is mounted from the host (persistent) — see PersistentDockerSandbox.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl wget \
        nmap \
        dnsutils \
        whois \
        netcat-openbsd \
        iputils-ping \
        traceroute \
        git \
        jq \
        unzip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir requests httpx beautifulsoup4 lxml

RUN mkdir -p /work
WORKDIR /work

# Sanity: image ships with the tools agents kept reinstalling.
RUN curl --version | head -1 && nmap --version | head -1 && jq --version
