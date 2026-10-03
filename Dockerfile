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

ENV PUID=99 PGID=100 UMASK=002 TZ=UTC ROOT_DIR=/backup
VOLUME ["/backup"]
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
