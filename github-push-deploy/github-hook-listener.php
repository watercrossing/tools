<?php

// github-push-deploy — GitHub push-webhook listener.
//
// Verifies the webhook HMAC-SHA256 signature and, on a push to the configured
// branch of the configured repository, runs update.sh.

error_reporting(0);
header('Content-Type: text/plain');

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
$cmd = escapeshellarg($base_dir . '/update.sh')
     . ' >> ' . escapeshellarg($base_dir . '/deploy-cmd.log') . ' 2>&1';

function log_msg($msg) {
    file_put_contents(LOGFILE, date('Y-m-d H:i:s') . ' ' . $msg . "\n", FILE_APPEND);
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

log_msg("=== Request from {$remote} event={$event} ===");

if ($push_ok && $repo_ok && $branch_ok) {
    log_msg("Signature OK, push to {$repo}@{$branch} — running update.sh");
    passthru($cmd);
} else {
    // Authenticated, but not a push to the branch/repo we deploy — acknowledge and ignore.
    log_msg("Ignoring (push_ok={$push_ok} repo_ok={$repo_ok} branch_ok={$branch_ok})");
    http_response_code(200);
    echo "Ignored\n";
}
