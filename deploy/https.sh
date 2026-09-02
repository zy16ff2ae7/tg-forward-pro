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
#
# Бесплатный домен без регистратора: если у вас нет своего домена, возьмите
# любой *.nip.io, который указывает на ваш IP автоматически, например
#   107-173-89-192.nip.io  (меняется на ваш IP)
#   papa.107-173-89-192.nip.io
# certbot выпустит сертификат сразу, DNS править не нужно.
#
# ── Ловушка: порт 443 занят stream-блоком (VPN/xray) ───────────────────────
# Если на сервере nginx слушает 443 через stream с ssl_preread (разбор SNI для
# VPN), то HTTP-сайт на `listen 443 ssl` не отвечает — соединение уходит на
# VPN-бэкенд и клиент видит чужой сертификат. Признак: после certbot внешний
# запрос https://ДОМЕН/ отдаёт сертификат не от Let's Encrypt.
#
# Решение — дать сайту свой порт (8443) и добавить строку в карту SNI, чтобы
# stream перенаправлял наш SNI на этот порт. Скрипт сделает это сам по флагу
# --auto-sni (бэкапы конфигов сохраняются):
#
#   bash deploy/https.sh papa.107-173-89-192.nip.io --auto-sni
#
set -euo pipefail

DOMAIN="${1:-tg.fominadaria.ru}"
AUTO_SNI=0
SITE_PORT=8443
for arg in "${@:2}"; do
  case "$arg" in
    --auto-sni) AUTO_SNI=1 ;;
    --port=*)   SITE_PORT="${arg#*=}" ;;
  esac
done

SITE="/etc/nginx/sites-available/tg-forward"
LINK="/etc/nginx/sites-enabled/tg-forward"
STREAM_DIR="/etc/nginx/stream-conf.d"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }
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
  echo "  Для *.nip.io этого не нужно — он указывает на IP автоматически."
  echo "  Проверить:  dig +short A $DOMAIN @1.1.1.1"
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

# ── 5. Проверка, что 443 действительно отдаёт наш сертификат ─────────────────
issuer="$(echo | openssl s_client -connect 127.0.0.1:443 -servername "$DOMAIN" 2>/dev/null \
          | openssl x509 -noout -issuer 2>/dev/null || true)"
if [[ "$issuer" == *"Let's Encrypt"* ]]; then
  ok "на 443 для $DOMAIN сертификат Let's Encrypt — stream-SNI не мешает"
else
  warn "на 443 для $DOMAIN другой сертификат (не Let's Encrypt): ${issuer:-нет ответа}"
  echo
  echo "  Похоже, порт 443 занят stream-блоком с ssl_preread (VPN/xray):"
  echo "  HTTP-сайт на нём не отвечает, соединение уходит на VPN-бэкенд."
  if [[ "$AUTO_SNI" -eq 1 ]]; then
    say "Переношу сайт на порт $SITE_PORT и добавляю SNI-маршрут"
    _fix_stream_sni
  else
    echo "  Исправить можно флагом --auto-sni (сохранит бэкапы конфигов):"
    echo "      bash deploy/https.sh $DOMAIN --auto-sni"
    echo "  Или вручную: в сайте заменить 'listen 443 ssl' на 'listen $SITE_PORT ssl',"
    echo "  а в карту SNI stream-блока ($(_map_file)) добавить строку"
    echo "      $DOMAIN  127.0.0.1:$SITE_PORT;   # перед default"
    echo "  и выполнить: nginx -t && systemctl reload nginx"
    exit 1
  fi
fi

systemctl reload nginx
ok "nginx перезагружен"

# ── 6. Финальная проверка ─────────────────────────────────────────────────────
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

# ── helpers ───────────────────────────────────────────────────────────────────
_map_file() {
  grep -rls "map \$ssl_preread_server_name" "$STREAM_DIR" 2>/dev/null | head -n1 \
    || echo "$STREAM_DIR/*.conf"
}

_fix_stream_sni() {
  # сайт: 443 -> SITE_PORT
  local bak; bak="$(mktemp -p /root tgf_site.XXXXXX.bak)"
  cp "$SITE" "$bak"
  sed -i -E "s/^(\s*)listen[[:space:]]+443[[:space:]]+ssl;/\1listen ${SITE_PORT} ssl;/" "$SITE"
  ok "сайт: listen 443 ssl -> $SITE_PORT (бэкап $bak)"

  # карта SNI: строка перед default
  local mf; mf="$(_map_file)"
  if [[ -z "$mf" || ! -f "$mf" ]]; then
    fail "не нашёл файл карты SNI в $STREAM_DIR — добавьте строку вручную"
    return 1
  fi
  local sbak; sbak="$(mktemp -p /root tgf_stream.XXXXXX.bak)"
  cp "$mf" "$sbak"
  sed -i -E "s|^(\s*)default(\s+127\.0\.0\.1:[0-9]+;)?|\1${DOMAIN}  127.0.0.1:${SITE_PORT};\n\1default\2|" "$mf"
  ok "SNI: $DOMAIN -> 127.0.0.1:$SITE_PORT (бэкап $sbak) в $mf"
  nginx -t
  systemctl reload nginx
}
