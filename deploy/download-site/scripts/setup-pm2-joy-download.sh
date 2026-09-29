#!/bin/bash
set -euo pipefail

echo "==> remove old pm2 download-site"
pm2 delete download-site 2>/dev/null || true

echo "==> archive old project"
if [ -d /opt/projects/download-site ]; then
  rm -rf /opt/projects/download-site.bak
  mv /opt/projects/download-site /opt/projects/download-site.bak
fi

echo "==> create joy-download node app"
mkdir -p /opt/projects/joy-download
cat >/opt/projects/joy-download/package.json <<'EOF'
{
  "name": "joy-download",
  "version": "1.0.0",
  "private": true,
  "main": "server.js",
  "scripts": {
    "start": "node server.js"
  },
  "dependencies": {
    "express": "^4.18.2"
  }
}
EOF

cat >/opt/projects/joy-download/server.js <<'EOF'
const express = require("express");
const path = require("path");

const app = express();
const PORT = Number(process.env.PORT || 3080);
const ROOT = process.env.JOY_DOWNLOAD_ROOT || "/var/www/joy-download";

app.use((req, res, next) => {
  if (req.path.endsWith(".exe")) {
    res.setHeader("Content-Type", "application/octet-stream");
    res.setHeader("Content-Disposition", "attachment");
  }
  if (req.path === "/api/latest.json") {
    res.setHeader("Cache-Control", "no-store");
    res.setHeader("Access-Control-Allow-Origin", "*");
  }
  next();
});

app.use(express.static(ROOT, { index: ["index.html"], fallthrough: true }));

app.get("*", (req, res) => {
  res.sendFile(path.join(ROOT, "index.html"));
});

app.listen(PORT, "127.0.0.1", () => {
  console.log(`joy-download listening on 127.0.0.1:${PORT} root=${ROOT}`);
});
EOF

cd /opt/projects/joy-download
if [ ! -d node_modules/express ]; then
  npm install --omit=dev
fi

echo "==> start with pm2"
pm2 start /opt/projects/joy-download/server.js --name joy-download --cwd /opt/projects/joy-download
pm2 save

echo "==> rewrite nginx for down.sharex.my -> pm2:3080"
cat >/etc/nginx/conf.d/joy-download.conf <<'EOF'
server {
    listen 80;
    server_name down.sharex.my;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name down.sharex.my;

    ssl_certificate /etc/letsencrypt/live/down.sharex.my/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/down.sharex.my/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    client_max_body_size 200m;

    location / {
        proxy_pass http://127.0.0.1:3080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

nginx -t
systemctl reload nginx

echo "==> status"
pm2 list
curl -sI --max-time 5 -H "Host: down.sharex.my" http://127.0.0.1:3080/ | head -8
curl -skI --max-time 5 https://down.sharex.my/ | head -8
curl -s --max-time 5 https://down.sharex.my/api/latest.json
echo
echo "DONE"
