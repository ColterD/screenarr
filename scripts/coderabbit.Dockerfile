ARG CODERABBIT_IMAGE_PLATFORM="linux/amd64"
ARG CODERABBIT_BASE_IMAGE="debian:bookworm-slim@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df"
ARG CODERABBIT_EXPECTED_VERSION="0.6.4"
# Refresh APT pins with: docker run --rm debian:bookworm-slim sh -lc "apt-get update && apt-cache policy bash ca-certificates curl git unzip"
ARG APT_BASH_VERSION="5.2.15-2+b13"
ARG APT_CA_CERTIFICATES_VERSION="20230311+deb12u1"
ARG APT_CURL_VERSION="7.88.1-10+deb12u14"
ARG APT_GIT_VERSION="1:2.39.5-0+deb12u3"
ARG APT_UNZIP_VERSION="6.0-28"
# CodeRabbit's published CLI archive here is x64-only, so both stages stay amd64.
FROM --platform=${CODERABBIT_IMAGE_PLATFORM} ${CODERABBIT_BASE_IMAGE} AS downloader

ARG CODERABBIT_EXPECTED_VERSION
ARG APT_BASH_VERSION
ARG APT_CA_CERTIFICATES_VERSION
ARG APT_CURL_VERSION
ARG APT_GIT_VERSION
ARG APT_UNZIP_VERSION
ENV CODERABBIT_ARCHIVE_SHA256="d4f28829e7243a831d837916ad163103496af99495a195c1c102e000040900f7"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        "bash=${APT_BASH_VERSION}" \
        "ca-certificates=${APT_CA_CERTIFICATES_VERSION}" \
        "curl=${APT_CURL_VERSION}" \
        "git=${APT_GIT_VERSION}" \
        "unzip=${APT_UNZIP_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0755 /opt/coderabbit/bin /tmp/coderabbit \
    && curl -fsSL --retry 5 --retry-delay 2 --retry-connrefused --max-time 120 "https://cli.coderabbit.ai/releases/${CODERABBIT_EXPECTED_VERSION}/coderabbit-linux-x64.zip" -o /tmp/coderabbit/coderabbit-linux-x64.zip \
    && echo "${CODERABBIT_ARCHIVE_SHA256}  /tmp/coderabbit/coderabbit-linux-x64.zip" | sha256sum -c - \
    && unzip -q /tmp/coderabbit/coderabbit-linux-x64.zip -d /tmp/coderabbit \
    && install -m 0755 /tmp/coderabbit/coderabbit /opt/coderabbit/bin/coderabbit \
    && rm -rf /tmp/coderabbit \
    && test "$(/opt/coderabbit/bin/coderabbit --version)" = "${CODERABBIT_EXPECTED_VERSION}"

# Match the downloader platform so the x64 CLI binary runs in the final image.
FROM --platform=${CODERABBIT_IMAGE_PLATFORM} ${CODERABBIT_BASE_IMAGE}

ARG CODERABBIT_UID="10001"
ARG CODERABBIT_GID="10001"
ARG CODERABBIT_EXPECTED_VERSION
ARG APT_BASH_VERSION
ARG APT_CA_CERTIFICATES_VERSION
ARG APT_GIT_VERSION

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        "bash=${APT_BASH_VERSION}" \
        "ca-certificates=${APT_CA_CERTIFICATES_VERSION}" \
        "git=${APT_GIT_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g "${CODERABBIT_GID}" coderabbit \
    && useradd --no-log-init -m -u "${CODERABBIT_UID}" -g "${CODERABBIT_GID}" -s /bin/bash coderabbit \
    && install -d -m 0755 -o root -g root /opt/coderabbit/bin

COPY --from=downloader --chown=root:root /opt/coderabbit/bin/coderabbit /opt/coderabbit/bin/coderabbit

RUN chmod 0555 /opt/coderabbit/bin/coderabbit \
    && test "$(/opt/coderabbit/bin/coderabbit --version)" = "${CODERABBIT_EXPECTED_VERSION}"

USER coderabbit
ENV PATH="/opt/coderabbit/bin:${PATH}"
