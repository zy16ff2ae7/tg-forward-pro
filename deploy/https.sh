#!/usr/bin/env bash
# Выпуск HTTPS-сертификата для мини-аппа. Запускать НА СЕРВЕРЕ (под root).
#
#   bash deploy/https.sh tg.fominadaria.ru
#
# Идём по шагам и останавливаемся там, где что-то не готово:
#   1. домен резолвится и указывает на этот сервер (иначе certbot не пройдёт);
#   2. nginx-сайт существует и конфиг валиден;
#   3. certbot выпускает сертификат и сам дописывает блок 443;
#   4. проверяем, что https://ДОМЕН/app/ отвечает 200.
#
# Скрипт можно перезапускать сколько угодно: если сертификат уже есть,
# он просто проверит срок и обновит его при необходимости.
set -euo pipefail

DOMAIN="${1:-tg.fominadaria.ru}"
SITE="/etc/nginx/sites-available/tg-forward"
LINK="/etc/nginx/sites-enabled/tg-forward"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
fail() { printf '  ✗ %s\n' "$*" >&2; }

say "HTTPS для $DOMAIN"

# ── 1. Публичный IP этого сервера ────────────────────────────────────────────
MY_IP="$(curl -4 -s -m 10 https://ifconfig.me || true)"
if [[ -z "$MY_IP" ]]; then
  MY_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
if [[ -z "$MY_IP" ]]; then
  fail "не удалось узнать публичный IP сервера"
  exit 1
fi
ok "IP сервера: $MY_IP"

# ── 2. A-запись домена ───────────────────────────────────────────────────────
A_RECORD="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -n1 || true)"
if [[ -z "$A_RECORD" ]]; then
  fail "$DOMAIN не резолвится — A-запись ещё не добавлена или не разошлась."
  echo
  echo "  Добавьте у регистратора (там, где куплен домен):"
  echo "      A    ${DOMAIN%%.*}    $MY_IP"
  echo "  либо, если нужен именно поддомен:"
  echo "      A    $DOMAIN    $MY_IP"
  echo
  echo "  Проверить расхождение:  dig +short A $DOMAIN @1.1.1.1"
  echo "  После этого запустите скрипт снова."
  exit 1
fi
ok "$DOMAIN → $A_RECORD"

if [[ "$A_RECORD" != "$MY_IP" ]]; then
  fail "A-запись указывает на $A_RECORD, а сервер имеет $MY_IP."
  echo "  Поправьте запись и запустите скрипт снова."
  exit 1
fi

# ── 3. nginx-сайт ────────────────────────────────────────────────────────────
if [[ ! -f "$SITE" ]]; then
  fail "нет $SITE — скопируйте туда deploy/nginx.conf и замените YOUR_DOMAIN."
  exit 1
fi
if [[ ! -e "$LINK" ]]; then
  ln -s "$SITE" "$LINK"
  ok "сайт включён ($LINK)"
fi
nginx -t 2>/dev/null
ok "конфиг nginx валиден"

# ── 4. Сертификат ────────────────────────────────────────────────────────────
if [[ -d "/etc/letsencrypt/live/$DOMAIN" ]]; then
  say "Сертификат уже есть — проверяю срок"
  certbot renew --cert-name "$DOMAIN" --quiet --nginx
  ok "обновление проверено"
else
  say "Выпускаю сертификат (certbot --nginx)"
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
    --redirect -m "admin@${DOMAIN#*.}"
  ok "сертификат выпущен"
fi

systemctl reload nginx
ok "nginx перезагружен"

# ── 5. Проверка ──────────────────────────────────────────────────────────────
CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 15 "https://$DOMAIN/app/" || true)"
if [[ "$CODE" == "200" ]]; then
  ok "https://$DOMAIN/app/ отвечает 200"
else
  fail "https://$DOMAIN/app/ отвечает $CODE (ждали 200)"
  exit 1
fi

echo
say "Готово. Не забудьте прописать в /opt/tg-forward/.env:"
echo "    WEBAPP_URL=https://$DOMAIN"
echo "и перезапустить сервис: systemctl restart tg-forward"
echo "после этого в логе появится «Кнопка мини-аппа установлена: https://$DOMAIN»."
