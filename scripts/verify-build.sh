#!/usr/bin/env bash
set -Eeuo pipefail
expected_version="${1:?PHP version is required}"
expected_api="${2:?PHP module API is required}"
expected_arch="${3:?Debian architecture is required}"
expected_package_version="${4:?Package version is required}"
branch="${expected_version%.*}"

cd /build/php-source
cli="$PWD/cli-build/sapi/cli/php"
cgi="$PWD/cgi-build/sapi/cgi/php-cgi"
fpm="$PWD/fpm-build/sapi/fpm/php-fpm"
test "$("$cli" -n -r 'echo PHP_VERSION;')" = "$expected_version"
test "$(awk '/^#define ZEND_MODULE_API_NO / {print $3}' Zend/zend_modules.h)" = "$expected_api"
"$cli" -n -v
"$fpm" -n -v

php_args=(-n -d "extension_dir=$PWD/ext-build/modules")
while IFS= read -r module; do
    test -n "$module" || continue
    test -f "ext-build/modules/$module.so"
    php_args+=(-d "extension=$module.so")
done < /build/extensions.txt
while IFS= read -r module; do
    test -n "$module" || continue
    test -f "ext-build/modules/$module.so"
    php_args+=(-d "zend_extension=$PWD/ext-build/modules/$module.so")
done < /build/zend-extensions.txt
"$cli" "${php_args[@]}" -r '
$modules = file("/build/extensions.txt", FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
foreach ($modules as $module) {
    if (!extension_loaded($module)) {
        fwrite(STDERR, "Missing extension: $module\n");
        exit(1);
    }
}
if (in_array("opcache", file("/build/zend-extensions.txt", FILE_IGNORE_NEW_LINES), true)
    && !extension_loaded("Zend OPcache")) { exit(1); }
if (extension_loaded("dom")) {
    $document = new DOMDocument();
    if (!$document->loadXML("<check>ok</check>")) { exit(1); }
}
if (extension_loaded("mbstring") && mb_strlen("Привет", "UTF-8") !== 6) { exit(1); }
if (extension_loaded("bcmath") && bcadd("1.2", "3.4", 1) !== "4.6") { exit(1); }
if (extension_loaded("pdo_mysql") && !in_array("mysql", PDO::getAvailableDrivers(), true)) { exit(1); }
if (extension_loaded("gd")) {
    $image = imagecreatetruecolor(1, 1);
    if ($image === false) { exit(1); }
    imagedestroy($image);
}
echo "Extension load checks passed.\n";
'

mapfile -t apache_debs < <(find /build -maxdepth 1 -type f \
    -name "libapache2-mod-php${branch}_*.deb")
test "${#apache_debs[@]}" = 1
dpkg-deb -x "${apache_debs[0]}" /tmp/php-apache-check
apache_module="/tmp/php-apache-check/usr/lib/apache2/modules/libphp${branch}.so"
test -f "$apache_module"
a2dismod mpm_event
a2enmod mpm_prefork
apache2ctl -t -C "LoadModule php_module $apache_module"

cat > /tmp/php-fpm-build-check.conf <<'CONF'
[global]
error_log = /tmp/php-fpm-build-check.log
daemonize = no
[www]
user = www-data
group = www-data
listen = /tmp/php-fpm-build-check.sock
pm = static
pm.max_children = 1
CONF
"$fpm" -n -t -y /tmp/php-fpm-build-check.conf

mapfile -t test_paths < /build/test-paths.txt
NO_INTERACTION=1 REPORT_EXIT_STATUS=1 \
    TEST_PHP_EXECUTABLE="$cli" TEST_PHP_CGI_EXECUTABLE="$cgi" \
    "$cli" -n run-tests.php -n --offline "${test_paths[@]}" \
    2>&1 | tee /build/php-tests.log

printf 'Package\tVersion\tArchitecture\tDepends\n' > /build/package-manifest.txt
for package in /build/*.deb; do
    arch="$(dpkg-deb -f "$package" Architecture)"
    test "$arch" = "$expected_arch" || test "$arch" = all
    test "$(dpkg-deb -f "$package" Version)" = "$expected_package_version"
    dpkg-deb --show --showformat='${Package}\t${Version}\t${Architecture}\t${Depends}\n' "$package" \
        >> /build/package-manifest.txt
done
