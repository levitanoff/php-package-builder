"""Exercise real FPM over loopback FastCGI, without an application or database."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time


def record(kind, payload=b""):
    padding = (-len(payload)) % 8
    return struct.pack("!BBHHBB", 1, kind, 1, len(payload), padding, 0) + payload + b"\0" * padding


def length(value):
    return bytes([value]) if value < 128 else struct.pack("!I", value | 0x80000000)


def receive(connection, size):
    output = b""
    while len(output) < size:
        data = connection.recv(size - len(output))
        if not data:
            raise EOFError("FPM closed the connection before END_REQUEST")
        output += data
    return output


def request(port, script, body=b"", content_type="", cookie=""):
    params = {"GATEWAY_INTERFACE": "CGI/1.1", "SERVER_PROTOCOL": "HTTP/1.1",
              "SERVER_SOFTWARE": "runtime-check", "SERVER_NAME": "localhost",
              "SERVER_ADDR": "127.0.0.1", "SERVER_PORT": "80", "REMOTE_ADDR": "127.0.0.1",
              "REMOTE_PORT": "12345", "REQUEST_METHOD": "POST" if body else "GET",
              "REQUEST_URI": "/" + script.name, "SCRIPT_NAME": "/" + script.name,
              "SCRIPT_FILENAME": str(script), "DOCUMENT_ROOT": str(script.parent),
              "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": content_type,
              "HTTP_COOKIE": cookie, "QUERY_STRING": ""}
    encoded = b""
    for key, value in params.items():
        key, value = key.encode(), value.encode()
        encoded += length(len(key)) + length(len(value)) + key + value
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(record(1, struct.pack("!HB5s", 1, 0, b"\0" * 5)) +
                           record(4, encoded) + record(4) +
                           (record(5, body) if body else b"") + record(5))
        stdout, stderr = b"", b""
        while True:
            version, kind, request_id, size, padding, _ = struct.unpack("!BBHHBB", receive(connection, 8))
            if version != 1 or request_id != 1:
                raise RuntimeError("Invalid FastCGI response")
            data = receive(connection, size)
            receive(connection, padding)
            if kind == 6:
                stdout += data
            elif kind == 7:
                stderr += data
            elif kind == 3:
                app_status, protocol_status = struct.unpack("!IB3x", data)
                if app_status or protocol_status:
                    raise RuntimeError("FPM returned a failed request")
                break
            if len(stdout) + len(stderr) > 1024 * 1024:
                raise RuntimeError("Unexpectedly large FastCGI response")
    if stderr:
        raise RuntimeError("FPM emitted request errors: " + stderr.decode(errors="replace"))
    headers, separator, payload = stdout.partition(b"\r\n\r\n")
    if not separator or b"Status:" in headers:
        raise RuntimeError("Missing or unsuccessful HTTP response: " + stdout.decode(errors="replace"))
    return payload


def main():
    profile = json.loads(Path("/usr/local/share/php-runtime/profile.json").read_text())
    with tempfile.TemporaryDirectory(prefix="php56-smoke-") as directory:
        work = Path(directory)
        work.chmod(0o755)
        for name in ("sessions", "uploads"):
            folder = work / name
            folder.mkdir()
            os.chown(folder, 33, 33)  # Debian www-data; test-only writable directories.
        marker = work / "uploads" / "shutdown-ran"
        oom = work / "oom.php"
        oom.write_text("<?php\nregister_shutdown_function(function () { file_put_contents(" +
                       json.dumps(str(marker)) + ", 'unsafe'); });\n" +
                       "$data = str_repeat('x', 64 * 1024 * 1024);\necho 'OOM_NOT_TRIGGERED';\n")
        cli = subprocess.run(["php5.6", "-n", "-d", "memory_limit=16M", str(oom)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        assert cli.returncode == profile["verification"]["oom_exit_code"], cli.stdout
        assert b"Allowed memory size" in cli.stdout, cli.stdout
        assert b"OOM_NOT_TRIGGERED" not in cli.stdout and not marker.exists(), cli.stdout

        probe = work / "probe.php"
        probe.write_text("""<?php
session_start();
header('Content-Type: application/json');
echo json_encode(array(
    'sapi' => PHP_SAPI,
    'version' => PHP_VERSION,
    'pid' => getmypid(),
    'upload_enabled' => (bool) ini_get('session.upload_progress.enabled'),
    'progress_cleanup' => (bool) ini_get('session.upload_progress.cleanup'),
    'session_id' => session_id(),
    'progress' => isset($_SESSION['upload_progress_probe']),
    'file_error' => isset($_FILES['file']) ? $_FILES['file']['error'] : null,
    'file' => isset($_FILES['file']) ? file_get_contents($_FILES['file']['tmp_name']) : null,
    'cp1251' => bin2hex(iconv('UTF-8', 'Windows-1251', 'Привет'))
));
""")
        short = work / "short.php"
        short.write_text("<? echo 'short-tags-ok'; ?>")
        # An ephemeral loopback port avoids collisions in CI. The socket is
        # closed before FPM starts; all connections remain inside the build.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = work / "php-fpm.conf"
        config.write_text("""[global]
daemonize = no
error_log = {work}/fpm.log
[test]
user = www-data
group = www-data
listen = 127.0.0.1:{port}
pm = static
pm.max_children = 1
catch_workers_output = yes
security.limit_extensions = .php
php_admin_value[memory_limit] = 16M
php_admin_value[session.save_path] = {work}/sessions
php_admin_value[upload_tmp_dir] = {work}/uploads
php_admin_flag[session.upload_progress.enabled] = on
php_admin_flag[session.upload_progress.cleanup] = off
php_admin_value[session.upload_progress.min_freq] = 0
""".format(work=work, port=port))
        with (work / "master.log").open("w") as log:
            process = subprocess.Popen(["php-fpm5.6", "--nodaemonize", "--fpm-config", str(config)],
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("FPM failed to start: " + (work / "master.log").read_text())
                    try:
                        first = json.loads(request(port, probe))
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
                assert first["sapi"] == "fpm-fcgi", first
                assert first["version"] in (profile["php"]["version"], profile["php"]["package_version"]), first
                assert first["cp1251"] == "cff0e8e2e5f2", first
                assert request(port, short) == b"short-tags-ok"
                boundary = "php56runtimecheck"
                upload_contents = "normal upload contents\n" * 1024
                body = ("--{b}\r\nContent-Disposition: form-data; name=\"PHP_SESSION_UPLOAD_PROGRESS\"\r\n"
                        "\r\nprobe\r\n--{b}\r\nContent-Disposition: form-data; name=\"file\"; "
                        "filename=\"check.txt\"\r\nContent-Type: text/plain\r\n\r\n"
                        "{contents}\r\n--{b}--\r\n").format(b=boundary, contents=upload_contents).encode()
                upload = json.loads(request(port, probe, body,
                                            "multipart/form-data; boundary=" + boundary,
                                            "PHPSESSID=php56runtimeprobe"))
                assert upload["file_error"] == 0 and upload["file"] == upload_contents, upload
                # Force the INI setting ON in this test pool. If progress is
                # merely disabled in legacy.ini rather than patched in C,
                # the progress record would be retained (cleanup is OFF).
                assert upload["upload_enabled"] and not upload["progress"], upload
                assert not upload["progress_cleanup"] and upload["session_id"] == "php56runtimeprobe", upload
                assert upload["pid"] == first["pid"], "Expected one reusable FPM worker"
                try:
                    request(port, oom)
                except (EOFError, OSError):
                    pass
                assert not marker.exists(), "OOM ran a shutdown callback instead of exiting"
                after = json.loads(request(port, probe))
                assert after["pid"] != first["pid"], "OOM worker was not replaced"
                assert after["sapi"] == "fpm-fcgi" and process.poll() is None
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    print("CLI OOM exit, FPM, short tags, cp1251, multipart upload, disabled progress and FPM OOM recovery passed.")


if __name__ == "__main__":
    main()
