FROM rclone/rclone:latest AS rclone

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /srv/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
ENV PUID=99 PGID=100 UMASK=002 TZ=UTC ROOT_DIR=/backup LOG_LEVEL=info
VOLUME ["/backup"]
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]

LABEL net.unraid.docker.webui="http://[IP]:[PORT:8080]/"
LABEL net.unraid.docker.icon="https://raw.githubusercontent.com/sgoertzen/cloudclone/main/app/static/assets/cloudclone.png"