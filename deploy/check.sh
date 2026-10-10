#!/usr/bin/env bash
# CCTV Map — health report. Run on the VM and paste the output back to Claude.
#   sudo bash /opt/cctv-map/deploy/check.sh
APP_DIR=/opt/cctv-map
PY=$APP_DIR/.venv/bin/python
ok()  { echo "  [OK]   $*"; }
bad() { echo "  [FAIL] $*"; }
sec() { echo; echo "== $* =="; }

sec "System"
. /etc/os-release; echo "  $PRETTY_NAME | $(uname -r) | up $(uptime -p | sed 's/up //')"
echo "  CPU $(nproc) | RAM $(free -h | awk '/Mem/ {print $3"/"$2}') | Disk $(df -h / | awk 'NR==2 {print $3"/"$2" ("$5")"}')"
echo "  IPs: $(hostname -I) | TZ: $(timedatectl show -p Timezone --value 2>/dev/null)"

sec "Services"
for s in cctv-map nginx fail2ban cctv-grabber; do
  st=$(systemctl is-active $s 2>/dev/null); en=$(systemctl is-enabled $s 2>/dev/null)
  [[ $st == active ]] && ok "$s ($en)" || echo "  [--]   $s: $st ($en)"
done
ss -tlnH | awk '{print $4}' | grep -E ':(80|443|8080)$' | sed 's/^/  listening /'

sec "Firewall"
ufw status | sed 's/^/  /' | head -15

sec "App API"
if curl -sf --max-time 5 http://127.0.0.1:8080/api/stats >/tmp/cctv_stats; then ok "API: $(cat /tmp/cctv_stats)"; else bad "API not responding on 127.0.0.1:8080"; fi
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1/)
[[ $code == 200 || $code == 401 ]] && ok "nginx :80 -> HTTP $code" || bad "nginx :80 -> HTTP $code"

sec "Camera data"
[[ -f $APP_DIR/data/cameras.json ]] && echo "  cameras.json updated $(date -r $APP_DIR/data/cameras.json '+%F %T')"
$PY - <<'EOF' 2>/dev/null || bad "could not read cameras.json"
import json, collections
d = json.load(open('/opt/cctv-map/data/cameras.json'))
c = collections.Counter((x['source'], x['status']) for x in d)
for s in sorted({x['source'] for x in d}):
    n = sum(v for (src, _), v in c.items() if src == s)
    print(f"  {s:10s} total={n:4d} online={c[(s,'online')]:4d} offline={c[(s,'offline')]:4d} n/a={c[(s,'unknown')]:4d}")
EOF

sec "Live source reachability (from this VM)"
probe() { code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -A 'Mozilla/5.0' "$2"); [[ $code =~ ^(200|401|403)$ ]] && ok "$1 HTTP $code" || bad "$1 HTTP $code"; }
probe "Medan portal       " https://atcsdishub.medan.go.id/streaming
probe "Jogja list API     " https://cctv.jogjakota.go.id/home/getdata
probe "Jogja stream host  " https://cctvjss.jogjakota.go.id/atcs/ATCS_kmnol.stream/playlist.m3u8
probe "Bina Marga page    " https://binamarga.pu.go.id/cctv-non-tol
probe "BM ITS :8989       " https://its.binamarga.pu.go.id:8989/play/hls/CT-01/index.m3u8
probe "BM bridge :8078    " https://apps.ptbtu.com:8078/hls-proxy/6/index.m3u8

sec "Collector dry-run (live, not saved)"
cd $APP_DIR && sudo -u cctv timeout 180 $PY - <<'EOF' 2>&1 | sed 's/^/  /'
from collector.sources import REGISTRY
for name, mod in REGISTRY.items():
    try:
        cams = mod.fetch()
        hls = sum(1 for c in cams if c.playback == 'hls' and c.stream_url)
        print(f"[OK]   {name:10s} live={len(cams)} hls={hls}")
    except Exception as e:
        print(f"[FAIL] {name:10s} {type(e).__name__}: {str(e)[:120]}")
EOF

sec "Proxy playback test (one stream per source through /hls)"
$PY - <<'EOF' 2>/dev/null | sed 's/^/  /'
import json, urllib.parse, urllib.request
d = json.load(open('/opt/cctv-map/data/cameras.json'))
for src in ('medan', 'jogja', 'binamarga'):
    cam = next((c for c in d if c['source'] == src and c['playback'] == 'hls' and c['status'] != 'offline'), None)
    if not cam: print(f"[--]   {src}: no candidate"); continue
    u = 'http://127.0.0.1:8080/hls?u=' + urllib.parse.quote(cam['stream_url'], safe='')
    try:
        body = urllib.request.urlopen(u, timeout=15).read(300).decode('utf-8', 'replace')
        print(("[OK]   " if '#EXTM3U' in body else "[FAIL] ") + f"{src}: {cam['id']}")
    except Exception as e:
        print(f"[FAIL] {src}: {cam['id']} {e}")
EOF

sec "Recent errors (cctv-map, last 2h)"
journalctl -u cctv-map --since '-2h' -p warning --no-pager | tail -15 | sed 's/^/  /'
echo; echo "== end of report =="
