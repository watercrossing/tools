<?php

// github-push-deploy — GitHub push-webhook listener.
//
// Verifies the webhook HMAC-SHA256 signature and, on a push to the configured
// branch of the configured repository, runs update.sh.

error_reporting(0);
header('Content-Type: text/plain');

// With no date.timezone in php.ini, PHP's date() falls back to UTC — while
// update.sh's run markers use GNU date and the box's local zone, so one deploy
// gets two timestamps an hour apart under BST. Adopt the system zone here rather
// than editing php.ini, reading it from the same places the C library looks.
function system_timezone() {
    $ids = array();
    if (is_link('/etc/localtime')) {            // what glibc actually reads; systemd keeps this a symlink
        $target = readlink('/etc/localtime');   // ... usually relative: ../usr/share/zoneinfo/Europe/London
        $at     = strpos($target, 'zoneinfo/');
        if ($at !== false) {
            $ids[] = substr($target, $at + strlen('zoneinfo/'));
        }
    }
    if (is_readable('/etc/timezone')) {         // Debian/Ubuntu, where /etc/localtime may be a plain copy
        $ids[] = trim(file_get_contents('/etc/timezone'));
    }
    foreach ($ids as $id) {
        // timezone_open() returns false for an unknown ID (the DateTimeZone constructor would throw
        // instead), so only an identifier PHP actually knows ever reaches the setter.
        if ($id !== '' && timezone_open($id)) {
            return $id;
        }
    }
    return date_default_timezone_get();  // nothing to go on — leave PHP's default (UTC) alone
}

date_default_timezone_set(system_timezone());

// Timestamp format for both logs. The trailing offset mirrors GNU date's %z in
// update.sh's markers, so the two files are comparable line for line — and a
// zone that failed to line up shows as +0000 instead of hiding.
define('LOG_TIME_FORMAT', 'Y-m-d H:i:s O');

// Read a plain KEY="value" config file (the same deploy.conf the shell sources).
function read_config($path) {
    $config = array();
    if (!is_readable($path)) {
        return $config;
    }
    foreach (file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
        $line = trim($line);
        if ($line === '' || $line[0] === '#') {
            continue;
        }
        $eq = strpos($line, '=');
        if ($eq === false) {
            continue;
        }
        $key = trim(substr($line, 0, $eq));
        $val = trim(substr($line, $eq + 1));
        $len = strlen($val);
        if ($len >= 2 && ($val[0] === '"' || $val[0] === "'") && $val[$len - 1] === $val[0]) {
            $val = substr($val, 1, -1);
        }
        $config[$key] = $val;
    }
    return $config;
}

$config   = read_config(__DIR__ . '/../deploy.conf');
$base_dir = isset($config['BASE_DIR']) ? $config['BASE_DIR'] : '';
$secret   = isset($config['WEBHOOK_SECRET']) ? $config['WEBHOOK_SECRET'] : '';
$repo     = isset($config['REPO_FULL_NAME']) ? $config['REPO_FULL_NAME'] : '';
$branch   = isset($config['BRANCH']) ? $config['BRANCH'] : '';

if ($base_dir === '' || $secret === '' || $repo === '' || $branch === '') {
    http_response_code(500);
    die("Server misconfigured: deploy.conf is missing or incomplete\n");
}

// Refuse to run with the shipped placeholder — otherwise the endpoint would
// accept a publicly-known secret and let anyone trigger a deploy.
if ($secret === 'REPLACE_WITH_A_LONG_RANDOM_STRING') {
    http_response_code(500);
    die("Server misconfigured: WEBHOOK_SECRET is still the placeholder — set a real secret\n");
}

define('LOGFILE', $base_dir . '/deploy.log');
define('CMDLOG', $base_dir . '/deploy-cmd.log');

// Both logs only ever grow, so cap them: once a file passes MAX, keep its last
// KEEP bytes and drop the rest.
define('LOG_MAX_BYTES', 5 * 1024 * 1024);
define('LOG_KEEP_BYTES', 4 * 1024 * 1024);

$cmd = escapeshellarg($base_dir . '/update.sh')
     . ' >> ' . escapeshellarg(CMDLOG) . ' 2>&1';

function log_msg($msg) {
    file_put_contents(LOGFILE, date(LOG_TIME_FORMAT) . ' ' . $msg . "\n", FILE_APPEND);
}

// Drop the oldest bytes of an oversized log, cutting at a line boundary so the
// file never starts mid-line. Called before update.sh runs, so nothing else is
// writing to deploy-cmd.log at the time.
function trim_log($path) {
    clearstatcache(true, $path);
    if (!is_file($path) || filesize($path) <= LOG_MAX_BYTES) {
        return;
    }
    $fh = fopen($path, 'r+');
    if ($fh === false) {
        return;
    }
    if (flock($fh, LOCK_EX)) {
        fseek($fh, filesize($path) - LOG_KEEP_BYTES);
        fgets($fh);  // discard the partial line we landed in the middle of
        $kept = stream_get_contents($fh);
        ftruncate($fh, 0);
        rewind($fh);
        fwrite($fh, '===== log trimmed ' . date(LOG_TIME_FORMAT) . ' — older entries dropped =====' . "\n" . $kept);
        fflush($fh);
        flock($fh, LOCK_UN);
    }
    fclose($fh);
}

// Verify the signature BEFORE touching the body any further. GitHub signs the
// raw request body with HMAC-SHA256 and sends it in this header. An
// unauthenticated caller must not reach json_decode or the log below: this
// endpoint is public, and an unbounded log would be a disk-fill DoS.
$post_data = file_get_contents('php://input');
$expected  = 'sha256=' . hash_hmac('sha256', $post_data, $secret);
$received  = isset($_SERVER['HTTP_X_HUB_SIGNATURE_256']) ? $_SERVER['HTTP_X_HUB_SIGNATURE_256'] : '';

if (!is_string($received) || !hash_equals($expected, $received)) {
    // Deliberately no log write here — this path is reachable by anyone who
    // knows the URL. GitHub's webhook "Recent Deliveries" shows the 403 for you.
    http_response_code(403);
    die("Forbidden\n");
}

// Authenticated from here on: the caller holds the shared secret.
$remote = isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : '?';
$event  = isset($_SERVER['HTTP_X_GITHUB_EVENT']) ? $_SERVER['HTTP_X_GITHUB_EVENT'] : '';
$data   = json_decode($post_data, true);

$push_ok   = ($event === 'push');
$repo_ok   = isset($data['repository']['full_name']) && $data['repository']['full_name'] === $repo;
$branch_ok = isset($data['ref']) && $data['ref'] === 'refs/heads/' . $branch;

trim_log(LOGFILE);
trim_log(CMDLOG);

log_msg("=== Request from {$remote} event={$event} ===");

if ($push_ok && $repo_ok && $branch_ok) {
    log_msg("Signature OK, push to {$repo}@{$branch} — running update.sh");
    $started = microtime(true);
    passthru($cmd, $status);
    log_msg(sprintf('update.sh exited %d after %.1fs — output in deploy-cmd.log', $status, microtime(true) - $started));
} else {
    // Authenticated, but not a push to the branch/repo we deploy — acknowledge and ignore.
    log_msg("Ignoring (push_ok={$push_ok} repo_ok={$repo_ok} branch_ok={$branch_ok})");
    http_response_code(200);
    echo "Ignored\n";
}
