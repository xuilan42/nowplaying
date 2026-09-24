# nowplaying 🎧

**«Now Playing» из плеера телефона — в био Telegram.**

```
Android (плеер)
   │ медиа-уведомление
   ▼
Termux-клиент (читает mediaSession, шлёт JSON каждые 4 сек)
   │ POST /update
   ▼
Сервер на OpenWrt-роутере (статический Go-бинарник)
   │ MTProto (gotd/td)
   ▼
Био профиля Telegram: "🎧 Артист — Трек"
```

Сервер — один бинарник без зависимостей, живёт на роутере с 128+ МБ RAM
(soft-лимит памяти 48 MiB, GC 60%).

## Как он себя ведёт

- **Last-Write-Wins**: очередей нет, каждое сообщение клиента — новое состояние;
  запоздавшие пакеты (`timestamp` старее текущего на 5+ с) игнорируются.
- **Троттлинг**: запросы к Telegram не чаще раза в 12 с (`min-api-gap`),
  FloodWait обрабатывается паузой.
- **Stale**: тишина от клиента дольше 15 с (`stale-after`) или пауза в плеере →
  био очищается.
- **Resync**: раз в час сервер сверяет био с реальным профилем — закрывает
  ручные правки и расхождения после сбоев.
- Обрезка по лимиту Telegram (70 символов UTF-16, `…` на конце; 140 для Premium).

## Состав репозитория

```
server/   сервер на Go (github.com/gotd/td)
client/   nowplaying_client.py — клиент для Termux (Android)
deploy/   config.example.json, procd init-скрипт для OpenWrt
```

## Сборка сервера

```sh
cd server
# под текущую машину:
CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o nowplaying-server .

# под роутер MIPS little-endian без FPU (MediaTek MT7621 и т.п.):
GOOS=linux GOARCH=mipsle GOMIPS=softfloat CGO_ENABLED=0 \
    go build -trimpath -ldflags "-s -w" -o nowplaying-server-mipsle .

# big-endian (некоторые MIPS):
GOOS=linux GOARCH=mips GOMIPS=softfloat CGO_ENABLED=0 go build ...
```

Проверить endianness роутера: `head -c 6 /bin/busybox | hexdump -C` —
5-й байт ELF-заголовка: `01` = big-endian, `02` = little-endian.

## Развёртывание на OpenWrt

```sh
scp nowplaying-server-mipsle root@ROUTER_IP:/usr/bin/nowplaying-server
ssh root@ROUTER_IP
mkdir -p /etc/nowplaying && chmod 700 /etc/nowplaying
cp config.example.json /etc/nowplaying/config.json   # заполнить token/api_id/api_hash/phone
cp init.d-nowplaying /etc/init.d/nowplaying && chmod +x /etc/init.d/nowplaying
/etc/init.d/nowplaying enable
```

`api_id`/`api_hash` — с [my.telegram.org](https://my.telegram.org) → API development tools.

### ⚠️ Первый вход на слабых роутерах (MIPS)

При самом первом запуске gotd генерирует MTProto-ключ (DH 2048).
На MIPS это вычисление на чистом Go занимает больше минуты и **не укладывается
в 60-секундный таймаут gotd** — клиент вечно циклит «Generating new auth key» и
никогда не доходит до запроса кода.

Решение — выполнить первый вход на быстрой машине и перенести готовую сессию:

```sh
# на ПК (или любом x86/ARM):
./nowplaying-server -config config.json -session ./session.json
# ввести код из Telegram и пароль 2FA → session.json создан

scp session.json root@ROUTER_IP:/etc/nowplaying/session.json
chmod 600 /etc/nowplaying/session.json   # на роутере
```

С существующей сессией роутеру нужен только лёгкий AES — MIPS тянет его
спокойно. Дальше всё работает по кругу: телефон → роутер → био.

## Клиент (Termux, Android)

1. `pkg update && pkg install python termux-api && pip install requests`
2. Приложение **Termux:API** (F-Droid/GitHub) + разрешить ему доступ к уведомлениям
3. Заполнить `SERVER_URL` и `AUTH_TOKEN` в шапке скрипта
4. `termux-wake-lock && python nowplaying_client.py`

Клиент шлёт состояние даже на паузе (сервер видит, что отправитель жив).
Пауза определяется по замершему прогрессбару уведомления (`PAUSE_CONFIRM`).

## Конфиг сервера (`/etc/nowplaying/config.json`)

| Ключ | По умолчанию | Описание |
|---|---|---|
| `listen` | `0.0.0.0:7854` | адрес HTTP-сервера |
| `token` | — | общий секрет клиента (обязателен) |
| `api_id` | — | с my.telegram.org (обязателен) |
| `api_hash` | — | с my.telegram.org (обязателен) |
| `phone` | `""` | телефон для первого входа (+79...) |
| `session` | `/etc/nowplaying/session.json` | файл сессии MTProto |
| `proxy` | `""` | SOCKS5 `host:port` для MTProto; пусто = напрямую |

Переменные окружения: `NPLAY_TOKEN`, `NPLAY_API_ID`, `NPLAY_API_HASH`,
`NPLAY_PHONE` перекрывают конфиг. `NPLAY_DEBUG=1` — подробный лог gotd.
Флаги CLI перекрывают всё: `-listen -token -api-id -api-hash -phone -session
-proxy -stale-after -min-api-gap -check-every -resync -max-bio`.

## HTTP API

**`POST /update`** — от клиента (лимит тела 4 КБ):

```json
{"token":"...","title":"Track","artist":"Artist","is_playing":true,"timestamp":unix_time}
```

Ответы: `{"status":"ok","detail":"stored"}` / `{"detail":"stale ignored"}` /
`403 bad token` / `400 bad json`.

**`GET /`** — текущее состояние и возраст последнего пакета.

## Troubleshooting

- **Висит и не просит код** — смотрите раздел про первый вход выше; для отладки
  запустите с `NPLAY_DEBUG=1` (будут видны фазы: ReqPQ → DH → auth).
- **Провайдер глушит MTProto** — пустите трафик через прокси/туннель:
  либо `"proxy": "127.0.0.1:PORT"` (SOCKS5), либо прозрачный туннель на роутере
  (PassWall2/OpenClash) — сервер подхватит его автоматически.
- **Изменился WAN IP** (PPPoE динамический) — клиент с внешним IP перестанет
  доходить; помогает ddns-scripts на OpenWrt.
- **FloodWait** — сервер сам выдерживает паузу и логирует её; ничего делать не надо.

## Безопасность

`/update` прикрыт только общим токеном (сравнение constant-time). Если порт
открыт в интернет — используйте длинный случайный токен (`openssl rand -hex 16`).
Файлы `config.json` и `session.json` храните с правами 600 — они дают полный
доступ к аккаунту.

## Лицензия

MIT
