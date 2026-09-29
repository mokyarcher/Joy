#!/bin/bash
set -euo pipefail
cat >/etc/nginx/conf.d/joy-download.conf <<'EOF'
server {
    listen 80;
    server_name down.sharex.my;

    root /var/www/joy-download;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location = /api/latest.json {
        default_type application/json;
        add_header Cache-Control "no-store" always;
        add_header Access-Control-Allow-Origin "*" always;
    }

    location /releases/ {
        add_header Cache-Control "public, max-age=300";
        types { application/octet-stream exe; }
        default_type application/octet-stream;
    }

    autoindex off;
    client_max_body_size 200m;
}
EOF
nginx -t
systemctl reload nginx
echo "=== headers ==="
curl -sI -H "Host: down.sharex.my" http://127.0.0.1/ | head -10
echo "=== latest.json ==="
curl -s -H "Host: down.sharex.my" http://127.0.0.1/api/latest.json
echo
