<?php
// PHP 5.6 syntax: this code executes using the installed runtime, not host PHP.
$profile = json_decode(file_get_contents('/usr/local/share/php-runtime/profile.json'), true);
function check($condition, $message) {
    if (!$condition) {
        fwrite(STDERR, $message . "\n");
        exit(1);
    }
}
check(in_array(PHP_VERSION, array($profile['php']['version'], $profile['php']['package_version']), true),
      'Unexpected PHP version: ' . PHP_VERSION);
check(PHP_SAPI === 'cli' && !PHP_ZTS, 'Expected CLI NTS runtime');
check(basename(PHP_EXTENSION_DIR) === $profile['php']['module_api'], 'Unexpected module API');
foreach ($profile['verification']['extensions'] as $extension) {
    check(extension_loaded($extension), 'Missing extension: ' . $extension);
}
check(extension_loaded('Zend OPcache'), 'Missing OPcache');
foreach ($profile['verification']['functions'] as $function) {
    check(function_exists($function), 'Missing legacy function: ' . $function);
}
$gd = gd_info();
foreach ($profile['verification']['gd_features'] as $feature) {
    check(!empty($gd[$feature]), 'Missing GD feature: ' . $feature);
}
$image = imagecreatetruecolor(2, 2);
check($image !== false, 'GD allocation failed');
ob_start();
imagepng($image);
$png = ob_get_clean();
check(substr($png, 0, 8) === "\x89PNG\r\n\x1a\n", 'GD PNG encoding failed');
imagedestroy($image);
$cp1251 = iconv('UTF-8', 'Windows-1251', 'Привет');
check(bin2hex($cp1251) === 'cff0e8e2e5f2', 'cp1251 conversion failed');
check(iconv('Windows-1251', 'UTF-8', $cp1251) === 'Привет', 'cp1251 roundtrip failed');
check(mb_strtolower($cp1251, 'Windows-1251') === iconv('UTF-8', 'Windows-1251', 'привет'),
      'cp1251 case conversion failed');
$document = new DOMDocument();
check($document->loadXML('<check>ok</check>'), 'DOM parsing failed');
check(in_array('mysql', PDO::getAvailableDrivers(), true), 'Missing PDO MySQL driver');
check((bool) ini_get('short_open_tag'), 'Short tags are disabled');
echo "CLI and legacy extension checks passed.\n";
