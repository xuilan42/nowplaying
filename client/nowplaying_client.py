#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Клиент "Now Playing" для Termux (Android).

Читает активные уведомления через `termux-notification-list`, находит
медиа-уведомление плеера и каждые SEND_INTERVAL секунд отправляет его
состояние (JSON) на сервер.

Установка:
    pkg update && pkg install python termux-api
    pip install requests
    + приложение Termux:API (F-Droid / GitHub)
    + Настройки Android -> Доступ к уведомлениям -> разрешить Termux:API

Запуск:
    termux-wake-lock          # чтобы Android не убивал процесс
    python nowplaying_client.py
"""

import json
import subprocess
import sys
import time
from datetime import datetime

import requests

# ============================ НАСТРОЙКИ ============================
SERVER_URL     = "http://90.189.120.103:7854/update"  # IP:порт сервера-приёмника
AUTH_TOKEN     = "4d40fe0243728d40ae0b9e291f47315a"  # тот же токен, что на сервере
SEND_INTERVAL  = 4        # период отправки, сек (3-5)
PAUSE_CONFIRM  = 3.0      # прогресс не двигался дольше этого -> считаем паузой
EMPTY_CONFIRM  = 2        # чтений подряд без трека, прежде чем очистить био
                          # (Termux:API иногда отдаёт устаревший кэш списка)
PREFERRED_APPS = []       # белый список пакетов плееров ([] = DEFAULT_PLAYERS), напр.:
                          # ["com.maxmpz.audioplayer", "com.spotify.music",
                          #  "ru.yandex.music", "com.google.android.apps.youtube.music"]
VERBOSE        = False    # True -> печатать результат каждой отправки
# ===================================================================

# Termux:API (>=0.50) не отдаёт extras с mediaSession, поэтому медиа-уведомление
# ищем по пакету плеера. Если вашего плеера тут нет — добавьте его пакет
# (виден в termux-notification-list -> packageName) в PREFERRED_APPS.
DEFAULT_PLAYERS = [
    "com.maxrave.simpmusic",                  # SIMP Music
    "com.maxmpz.audioplayer",                 # Poweramp
    "com.spotify.music",                      # Spotify
    "ru.yandex.music",                        # Яндекс Музыка
    "com.google.android.apps.youtube.music",  # YT Music
    "fm.last.android",                        # Last.fm
    "com.android.bluetooth",                  # звук по Bluetooth
]

# pkg -> {"progress": int, "changed_at": float}  (для детекта паузы)
_progress_state = {}


def read_notifications(retries=3, delay=0.7):
    """Список активных уведомлений Android (JSON из termux-notification-list).

    Termux:API при сбое listener-сервиса печатает ошибку в уведомления и
    отдаёт ПУСТОЙ stdout с кодом 0 — поэтому пустой/битый ответ ретраим.
    """
    last_exc = None
    for _ in range(retries):
        proc = subprocess.run(
            ["termux-notification-list"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError("termux-notification-list: " + (proc.stderr or "").strip())
        out = proc.stdout.strip()
        if out:
            try:
                return json.loads(out)
            except json.JSONDecodeError as exc:
                last_exc = exc
        else:
            last_exc = RuntimeError("пустой ответ Termux:API (listener не подключён)")
        time.sleep(delay)
    raise RuntimeError("termux-notification-list: %s" % last_exc)


def _when_key(n):
    """'when' бывает int (epoch) или строкой 'YYYY-MM-DD HH:MM:SS'."""
    w = n.get("when")
    if isinstance(w, (int, float)):
        return w
    try:
        return datetime.strptime(str(w), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0


def find_media_notification(notifications, min_when=0.0):
    """Самое свежее уведомление от плеера из белого списка.

    min_when — временной пол: уведомления о треках, начавшихся раньше уже
    опубликованного, игнорируются (Termux:API иногда отдаёт устаревший кэш
    со старым треком — без пола они мигают в био).
    """
    apps = PREFERRED_APPS or DEFAULT_PLAYERS
    candidates = [n for n in notifications
                  if n.get("packageName") in apps and _when_key(n) >= min_when]
    if not candidates:
        return None
    candidates.sort(key=_when_key, reverse=True)
    return candidates[0]


def extract_track(media):
    """(title, artist); у разных плееров поля лежат в разных местах."""
    extras = media.get("extras") or {}
    title  = str(media.get("title") or extras.get("android.title") or "").strip()
    artist = str(media.get("content") or extras.get("android.text") or "").strip()

    if title and (not artist or artist == title):
        # всё в тикере: "Artist - Title"
        ticker = str(media.get("tickerText") or media.get("ticker")
                     or extras.get("android.tickerText") or "").strip()
        if ticker and ticker != title:
            artist = ticker

    if title and artist.lower().endswith((" - " + title).lower()):
        # text = "Artist - Song" -> оставляем только исполнителя
        artist = artist[: -(len(title) + 3)].strip()

    # "Artist • Album" / "Artist — Album" / "Artist | Album" -> только артист
    for sep in (" • ", " — ", " – ", " | "):
        if sep in artist:
            artist = artist.split(sep)[0].strip()
            break

    return title, artist


def read_progress(media):
    """Позиция трека из extras['android.progress'] или None, если прогрессбара нет."""
    bundle = (media.get("extras") or {}).get("android.progress")
    if isinstance(bundle, dict):
        value, maximum = bundle.get("progress"), bundle.get("progressMax")
        if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum > 0:
            return int(value)
    return None


def detect_playing(media):
    """
    Эвристика "играет/пауза":
      - позиция в прогрессбаре изменилась -> играет;
      - позиция замерла дольше PAUSE_CONFIRM сек -> пауза;
      - прогрессбара нет вообще -> считаем, что играет (иначе не определить).
    """
    now  = time.time()
    pkg  = media.get("packageName") or "?"
    prog = read_progress(media)

    if prog is None:
        _progress_state.pop(pkg, None)
        return True

    st = _progress_state.get(pkg)
    if st is None or st["progress"] != prog:
        _progress_state[pkg] = {"progress": prog, "changed_at": now}
        return True
    return (now - st["changed_at"]) <= PAUSE_CONFIRM


def send_state(title, artist, is_playing):
    payload = {
        "token":      AUTH_TOKEN,
        "title":      title,
        "artist":     artist,
        "is_playing": bool(is_playing),
        "timestamp":  int(time.time()),
    }
    try:
        resp = requests.post(SERVER_URL, json=payload, timeout=5)
        if resp.status_code != 200:
            print("[!] Сервер: HTTP %s %s" % (resp.status_code, resp.text[:120]))
            return False
        return True
    except requests.RequestException as exc:
        print("[!] Сеть недоступна: %s" % exc)
        return False


def main():
    print("[i] Клиент запущен: %s каждые %s сек. Выход: Ctrl+C" % (SERVER_URL, SEND_INTERVAL))
    try:
        subprocess.run(["termux-wake-lock"], capture_output=True)  # не критично
    except Exception:
        pass

    # Проверка доступности termux-api до входа в цикл
    try:
        read_notifications()
    except FileNotFoundError:
        sys.exit("[x] Нет команды termux-notification-list: pkg install termux-api "
                 "+ приложение Termux:API")
    except Exception as exc:
        sys.exit("[x] Не удалось получить уведомления: %s" % exc)

    prev_state = None
    last_err = ""
    published = ("", "", False)  # (title, artist, playing) — текущее отражённое состояние
    min_when = 0.0               # пол времени старта трека (анти-устаревший-кэш)
    empty_streak = 0             # подряд идущие чтения без трека (только успешные)
    while True:
        started = time.time()
        title, artist, playing = "", "", False
        media = None
        read_failed = False

        try:
            media = find_media_notification(read_notifications(), min_when)
            if media:
                title, artist = extract_track(media)
                playing = detect_playing(media)
                if not title and not artist:
                    playing = False   # медиа-уведомление без трека не публикуем
                    media = None
                else:
                    # новый трек принят: поднимаем пол, старые кэши больше не страшны
                    min_when = max(min_when, _when_key(media))
            last_err = ""
        except Exception as exc:
            read_failed = True
            # спамим не каждую итерацию, а только когда текст ошибки сменился
            if str(exc) != last_err:
                print("[!] Ошибка чтения уведомлений: %s" % exc)
                last_err = str(exc)

        if media is not None:
            publish = (title, artist, playing)
        elif not read_failed and empty_streak + 1 >= EMPTY_CONFIRM:
            # нет трека в EMPTY_CONFIRM успешных чтениях подряд — плеер реально закрыт
            publish = ("", "", False)
        else:
            publish = published  # сбой или единичное «пустое» чтение — держим прежнее

        if media is not None:
            empty_streak = 0
        elif not read_failed:
            empty_streak += 1

        ok = send_state(*publish)
        published = publish
        state = publish
        if VERBOSE or state != prev_state:
            icon  = "▶" if state[2] else "⏸"
            track = ("%s — %s" % (state[1], state[0])).strip(" —") or "<нет трека>"
            print("[%s] %s %s -> %s" % (time.strftime("%H:%M:%S"), icon, track,
                                        "ok" if ok else "FAIL"))
            prev_state = state

        time.sleep(max(0.0, SEND_INTERVAL - (time.time() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[i] Остановлено.")
