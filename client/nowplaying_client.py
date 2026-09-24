#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Клиент "Now Playing" для Termux (Android).

Раз в SEND_INTERVAL секунд: спросить termux-notification-list, какое
уведомление плеера сейчас активно, и отправить его на сервер.
Никаких очередей и состояний — проверка и отдача.

Установка:
    pkg update && pkg install python termux-api
    pip install requests
    + приложение Termux:API (F-Droid / GitHub), тот же источник, что и Termux
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
SERVER_URL    = "http://90.189.120.103:7854/update"  # IP:порт сервера-приёмника
AUTH_TOKEN    = "4d40fe0243728d40ae0b9e291f47315a"   # тот же токен, что на сервере
SEND_INTERVAL = 4     # период опроса, сек
VERBOSE       = False # True -> печатать результат каждой отправки
PREFERRED_APPS = []   # [] = DEFAULT_PLAYERS; сюда можно вписать свой плеер
# ===================================================================

# Termux:API (>=0.50) не отдаёт mediaSession, поэтому медиа-уведомление ищем
# по пакету плеера. Пакет своего плеера виден в termux-notification-list
# (поле packageName) — добавьте его в PREFERRED_APPS.
DEFAULT_PLAYERS = [
    "com.maxrave.simpmusic",                  # SIMP Music
    "com.maxmpz.audioplayer",                 # Poweramp
    "com.spotify.music",                      # Spotify
    "ru.yandex.music",                        # Яндекс Музыка
    "com.google.android.apps.youtube.music",  # YT Music
    "com.android.bluetooth",                  # звук по Bluetooth
]


def _when_key(n):
    """'when' бывает int (epoch) или строкой 'YYYY-MM-DD HH:MM:SS'."""
    w = n.get("when")
    if isinstance(w, (int, float)):
        return w
    try:
        return datetime.strptime(str(w), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0


def read_notifications(retries=2, delay=0.7):
    """Активные уведомления Android. Termux:API при сбое listener'а отдаёт
    пустой stdout с кодом 0 — поэтому пустой/битый ответ ретраим пару раз."""
    for _ in range(retries):
        proc = subprocess.run(
            ["termux-notification-list"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError:
                pass
        time.sleep(delay)
    raise RuntimeError("termux-notification-list не ответил")


def current_track(notifications):
    """(title, artist) самого свежего уведомления плеера или None."""
    apps = PREFERRED_APPS or DEFAULT_PLAYERS
    players = [n for n in notifications if n.get("packageName") in apps]
    if not players:
        return None
    media = max(players, key=_when_key)
    title  = str(media.get("title") or "").strip()
    artist = str(media.get("content") or "").strip()
    if artist == title:
        artist = ""
    return (title, artist) if (title or artist) else None


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
    print("[i] Клиент запущен: %s каждые %s сек. Выход: Ctrl+C"
          % (SERVER_URL, SEND_INTERVAL))
    try:
        subprocess.run(["termux-wake-lock"], capture_output=True)
    except Exception:
        pass

    try:
        read_notifications()
    except FileNotFoundError:
        sys.exit("[x] Нет команды termux-notification-list: pkg install termux-api "
                 "+ приложение Termux:API")
    except Exception as exc:
        # Termux:API бывает заморожен системой — не выходим, цикл сам повторит
        print("[!] Termux:API не отвечает (%s) — буду повторять в цикле" % exc)

    last_err = ""
    last = ("", "", False)   # последнее успешно прочитанное состояние
    while True:
        started = time.time()
        try:
            track = current_track(read_notifications())
            last = (*track, True) if track else ("", "", False)
            last_err = ""
        except Exception as exc:
            # чтение не удалось: шлём последнее известное состояние,
            # чтобы сервер не счёл клиента умершим и не чистил био
            if str(exc) != last_err:
                print("[!] Ошибка чтения уведомлений: %s" % exc)
                last_err = str(exc)

        ok = send_state(*last)
        if VERBOSE or last != getattr(main, "_prev", None):
            main._prev = last
            icon  = "▶" if last[2] else "⏸"
            track = ("%s — %s" % (last[1], last[0])).strip(" —") or "<нет трека>"
            print("[%s] %s %s -> %s" % (time.strftime("%H:%M:%S"), icon, track,
                                        "ok" if ok else "FAIL"))

        time.sleep(max(0.0, SEND_INTERVAL - (time.time() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[i] Остановлено.")
