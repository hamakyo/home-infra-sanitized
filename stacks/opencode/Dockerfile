FROM node:22-slim

ARG OPENCODE_VERSION=1.19.6

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      jq \
      openssh-client \
      ripgrep \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "opencode-ai@${OPENCODE_VERSION}" \
 && npm cache clean --force \
 && groupadd -g 10001 opencode \
 && useradd -u 10001 -g 10001 -d /data -s /bin/sh -M opencode \
 && mkdir -p /data/workspace /etc/opencode \
 && chown -R 10001:10001 /data

ENV HOME=/data \
    OPENCODE_DISABLE_AUTOUPDATE=true

USER 10001:10001
WORKDIR /data/workspace

EXPOSE 4096
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
