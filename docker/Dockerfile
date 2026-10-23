ARG BASE_IMAGE
FROM ${BASE_IMAGE}
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG TARGET_SUITE
ARG TARGET_ARCH
ARG PHP_VERSION
ARG MODULE_API
ARG PACKAGE_VERSION
ARG SOURCE_DSC
ARG BUILD_JOBS=2

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 \
    DEBFULLNAME="Local PHP Builder" DEBEMAIL="php-builder@localhost"

COPY apt-sources.list /etc/apt/sources.list
RUN . /etc/os-release \
    && test "$ID" = debian \
    && test "$VERSION_CODENAME" = "$TARGET_SUITE" \
    && test "$(dpkg --print-architecture)" = "$TARGET_ARCH" \
    && rm -f /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources \
    && printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 755 /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       apache2 build-essential ca-certificates devscripts equivs fakeroot dpkg-dev \
       libdistro-info-perl quilt xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY sources/ /build/
RUN sha256sum --check SHA256SUMS \
    && dpkg-source -x "$SOURCE_DSC" php-source

WORKDIR /build/php-source
RUN dch --force-bad-version --newversion "$PACKAGE_VERSION" \
        --distribution "$TARGET_SUITE" --force-distribution \
        "Rebuild PHP $PHP_VERSION for Debian $TARGET_SUITE." \
    && apt-get update \
    && mk-build-deps --install --remove \
       --tool 'apt-get -y --no-install-recommends' debian/control \
    && dpkg-checkbuilddeps \
    && dpkg-buildpackage -us -uc -b -j"${BUILD_JOBS}"

COPY verify-build.sh /usr/local/bin/verify-php-build
COPY extensions.txt zend-extensions.txt test-paths.txt profile.json /build/
RUN chmod 755 /usr/local/bin/verify-php-build \
    && /usr/local/bin/verify-php-build "$PHP_VERSION" "$MODULE_API" "$TARGET_ARCH" "$PACKAGE_VERSION" \
    && mkdir /artifacts \
    && cp /build/*.deb /build/*.changes /build/*.buildinfo \
          /build/package-manifest.txt /build/php-tests.log /build/profile.json /artifacts/ \
    && cd /artifacts \
    && sha256sum ./*.deb > SHA256SUMS

CMD ["true"]
