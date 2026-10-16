# slaac-prefix-watch

Logs which global IPv6 prefixes the host it runs on holds, to find out how often the ISP renumbers the line.

Most consumer routers get a dynamic prefix by DHCPv6-PD and announce a `/64` of it to the LAN, but can't be asked about it themselves.
Every host on that LAN can be: its SLAAC addresses carry the prefix and the lifetimes the router announced.

```bash
./slaac-prefix-watch.py            # append one sample (what cron runs; silent unless the prefixes changed)
./slaac-prefix-watch.py --report   # samples taken, changes seen, how long the current prefix has held
```

Samples go to `~/.local/state/slaac-prefix-watch/slaac-prefix-watch.jsonl` (or under `$XDG_STATE_HOME`), one JSON line each.
A sample lists every global-unicast prefix (`2000::/3`) on any interface, with its remaining `preferred`/`valid` lifetimes in seconds and whether it is `deprecated`.
ULAs and privacy (temporary) addresses are left out.

A **change** means the set of non-deprecated prefixes differs from the previous sample.
That includes a prefix appearing on a host that had none, so the tool also works as a sentinel on a LAN without IPv6.
When a router renumbers, it keeps announcing the old prefix as deprecated (preferred lifetime 0) until it expires, so the old and new prefixes overlap in the log for a while.

## Running it from cron

It needs only the standard library and runs on Python 3.10+, so on hosts without uv you can call the system interpreter:

```cron
*/15 * * * * /usr/bin/python3 $HOME/dev/tools/slaac-prefix-watch/slaac-prefix-watch.py
```

A failure to run `ip` is logged as an `{"error": ...}` line and printed, so `--report`'s failure count is real.
