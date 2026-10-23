ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS runtime
SHELL ["/bin/bash", "-e", "-o", "pipefail", "-c"]
ARG TARGET_SUITE
ARG TARGET_ARCH
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

RUN . /etc/os-release \
    && test "$ID" = debian \
    && test "$VERSION_CODENAME" = "$TARGET_SUITE" \
    && test "$(dpkg --print-architecture)" = "$TARGET_ARCH" \
    && printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 755 /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/runtime-inputs
COPY SHA256SUMS package-inputs.tsv debsuryorg-archive-keyring.deb ./
COPY packages/ ./packages/
RUN sha256sum --check SHA256SUMS \
    && dpkg -i debsuryorg-archive-keyring.deb \
    && printf 'deb [signed-by=/usr/share/keyrings/debsuryorg-archive-keyring.gpg] https://packages.sury.org/php/ %s main\n' "$TARGET_SUITE" > /etc/apt/sources.list.d/php.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ./packages/*.deb \
    && while IFS=$'\t' read -r package version arch; do \
         test "$(dpkg-query -W -f='${Version}' "$package")" = "$version" || exit 1; \
         test "$(dpkg-query -W -f='${Architecture}' "$package")" = "$arch" || exit 1; \
       done < package-inputs.tsv \
    && mkdir -p /usr/local/share/php-runtime /usr/local/etc/php-runtime /var/www/html /run/php \
    && dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /usr/local/share/php-runtime/packages.tsv \
    && cp SHA256SUMS package-inputs.tsv /usr/local/share/php-runtime/ \
    && ln -s /usr/bin/php5.6 /usr/local/bin/php \
    && ln -s /usr/sbin/php-fpm5.6 /usr/local/sbin/php-fpm \
    && rm -rf /var/lib/apt/lists/* /tmp/runtime-inputs

COPY profile.json /usr/local/share/php-runtime/profile.json
COPY php-fpm.conf /usr/local/etc/php-runtime/php-fpm.conf
COPY legacy.ini /etc/php/5.6/fpm/conf.d/99-runtime.ini
COPY legacy.ini /etc/php/5.6/cli/conf.d/99-runtime.ini
WORKDIR /var/www/html
EXPOSE 9000
STOPSIGNAL SIGQUIT
CMD ["php-fpm5.6", "--nodaemonize", "--fpm-config", "/usr/local/etc/php-runtime/php-fpm.conf"]

# Verification tools are isolated from the image that serves the application.
FROM runtime AS checks
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY verify.php smoke.py /tmp/runtime-checks/
RUN php5.6 /tmp/runtime-checks/verify.php \
    && php-fpm5.6 --test --fpm-config /usr/local/etc/php-runtime/php-fpm.conf \
    && python3 /tmp/runtime-checks/smoke.py \
    && printf 'CLI, extensions, FPM, multipart, disabled upload progress, OOM exit: passed\nApplication and database compatibility: not tested\n' > /tmp/runtime-checks/passed.txt

FROM runtime AS final
COPY --from=checks /tmp/runtime-checks/passed.txt /usr/local/share/php-runtime/checks.txt
