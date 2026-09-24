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

import requests

# ============================ НАСТРОЙКИ ============================
SERVER_URL     = "http://192.168.1.1:7854/update"  # или внешний WAN IP роутера  # IP:порт сервера-приёмника
AUTH_TOKEN     = "CHANGE_ME_RANDOM_HEX"                   # hex: openssl rand -hex 16  # тот же токен, что на сервере
SEND_INTERVAL  = 4        # период отправки, сек (3-5)
PAUSE_CONFIRM  = 3.0      # прогресс не двигался дольше этого -> считаем паузой
PREFERRED_APPS = []       # белый список пакетов плееров ([] = любой), напр.:
                          # ["com.maxmpz.audioplayer", "com.spotify.music",
                          #  "ru.yandex.music", "com.google.android.apps.youtube.music"]
VERBOSE        = False    # True -> печатать результат каждой отправки
# ===================================================================

# pkg -> {"progress": int, "changed_at": float}  (для детекта паузы)
_progress_state = {}


def read_notifications():
    """Список активных уведомлений Android (JSON из termux-notification-list)."""
    proc = subprocess.run(
        ["termux-notification-list"],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        raise RuntimeError("termux-notification-list: " + (proc.stderr or "").strip())
    return json.loads(proc.stdout)


def find_media_notification(notifications):
    """Самое свежее уведомление с медиасессией (extras['android.mediaSession'])."""
    candidates = []
    for n in notifications:
        extras = n.get("extras") or {}
        if "android.mediaSession" not in extras:
            continue
        if PREFERRED_APPS and n.get("packageName") not in PREFERRED_APPS:
            continue
        candidates.append(n)
    if not candidates:
        return None
    candidates.sort(key=lambda n: n.get("when") or 0, reverse=True)
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
    while True:
        started = time.time()
        title, artist, playing = "", "", False

        try:
            media = find_media_notification(read_notifications())
            if media:
                title, artist = extract_track(media)
                playing = detect_playing(media)
                if not title and not artist:
                    playing = False   # медиа-уведомление без трека не публикуем
            else:
                _progress_state.clear()
        except Exception as exc:
            print("[!] Ошибка чтения уведомлений: %s" % exc)

        # Даже при паузе шлём состояние: сервер видит, что отправитель жив
        ok = send_state(title, artist, playing)
        state = (title, artist, playing)
        if VERBOSE or state != prev_state:
            icon  = "▶" if playing else "⏸"
            track = ("%s — %s" % (artist, title)).strip(" —") or "<нет трека>"
            print("[%s] %s %s -> %s" % (time.strftime("%H:%M:%S"), icon, track,
                                        "ok" if ok else "FAIL"))
            prev_state = state

        time.sleep(max(0.0, SEND_INTERVAL - (time.time() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[i] Остановлено.")
