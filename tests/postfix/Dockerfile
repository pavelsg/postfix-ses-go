FROM debian:bookworm-slim

# Package installation happens during image build. The queue runs with no network.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends postfix python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --no-create-home --shell /usr/sbin/nologin sespipe

COPY capture.py /usr/local/libexec/postfix-ses-go
COPY check_queue.py /opt/test/check_queue.py
RUN chmod 0755 /usr/local/libexec/postfix-ses-go
ENTRYPOINT ["timeout", "--kill-after=5s", "90s", "python3", "-u", "/opt/test/check_queue.py"]
