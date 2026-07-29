#!/usr/bin/env bash
set -euo pipefail

DOMAIN="soundpola.babelbeast.com"
BACKEND_PORT=9000

echo "==> [1/5] 安装 Nginx + Certbot ..."
apt update -qq
apt install -y -qq nginx certbot python3-certbot-nginx

echo "==> [2/5] 写入 Nginx 反向代理配置 ..."
cat > /etc/nginx/sites-available/soundpola << EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${BACKEND_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        client_max_body_size 50m;
    }
}
EOF

ln -sf /etc/nginx/sites-available/soundpola /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

echo "==> [3/5] 检测 Nginx 配置并启动 ..."
nginx -t
systemctl enable nginx
systemctl restart nginx

echo "==> [4/5] Certbot 签发证书 (自动改写 Nginx 为 HTTPS) ..."
certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos --register-unsafely-without-email --redirect

echo "==> [5/5] 验证续期 ..."
certbot renew --dry-run

echo ""
echo "=========================================="
echo " 完成! https://${DOMAIN}/ 已生效"
echo " 证书路径: /etc/letsencrypt/live/${DOMAIN}/"
echo " 自动续期: systemctl status certbot.timer"
echo "=========================================="
