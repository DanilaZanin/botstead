#!/usr/bin/env python3
"""Переносит persistent cookies из профиля человека в профиль бота при возврате управления.

Запускает chromium-supervisor.sh (uid 1001, `python3 -I`, не дольше 20 секунд) между остановкой Chromium человека и
стартом Chromium бота, поэтому обе базы никто не держит. В профиль бота попадает только таблица cookies: строки профиля
человека заменяют строки бота с тем же ключом (host_key, top_frame_site_key, name, path), остальные cookies бота
остаются. Всё в одной транзакции: при сбое профиль бота не меняется. Session cookies Chromium на диск не пишет, до этого
места они не доходят.

База бота лежит в профиле, которым управляет код бота (через CDP), поэтому ей не доверяют: профиль, каталоги по пути и
сама база (и её -journal, -wal, -shm) обязаны быть обычными файлами и каталогами без ссылок, база с одной жёсткой ссылкой;
соединение с PRAGMA trusted_schema=OFF; триггеры в любой из двух баз (они сработали бы на наших INSERT и DELETE) — отказ
до первой записи. Если базы бота ещё нет, база человека копируется через backup API во временный файл (O_EXCL, 0600) и
ставится на место атомарно.

    cookie-merge.py HUMAN_PROFILE BOT_PROFILE   → stdout `merged N`, код 0; при сбое сообщение в stderr и код 1
"""
import os
import re
import sqlite3
import stat
import sys

KEY_COLUMNS = ("host_key", "top_frame_site_key", "name", "path")
REQUIRED_KEY = ("host_key", "name", "path")
# Chromium хранит базу в Default/Network/Cookies (с версии 96), раньше в Default/Cookies.
COOKIE_PATHS = (os.path.join("Default", "Network", "Cookies"), os.path.join("Default", "Cookies"))
SIDECARS = ("-journal", "-wal", "-shm")
IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")
BUSY_TIMEOUT = 5


class MergeError(Exception):
    pass


def _lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _require_real_dir(path):
    """Каталог существует и это не ссылка. Нет каталога: None."""
    info = _lstat(path)
    if info is None:
        return False
    if not stat.S_ISDIR(info.st_mode):
        raise MergeError(f"{path} is not a directory (links are refused)")
    return True


def _require_regular(path, what):
    info = _lstat(path)
    if info is None:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise MergeError(f"{what} is not a regular file (links and special files are refused)")
    if info.st_nlink != 1:
        raise MergeError(f"{what} has more than one hard link")
    return True


def cookie_db(profile):
    """Путь к базе cookies профиля или None, если базы нет. Ссылка, не обычный файл, ссылка на месте каталога или
    сопутствующего файла, жёсткая ссылка: MergeError."""
    if not _require_real_dir(profile):
        return None
    for rel in COOKIE_PATHS:
        path = os.path.join(profile, rel)
        walk = profile
        present = True
        for part in os.path.dirname(rel).split(os.sep):
            walk = os.path.join(walk, part)
            if not _require_real_dir(walk):
                present = False
                break
        if not present:
            continue
        if _require_regular(path, f"{path}"):
            for suffix in SIDECARS:
                _require_regular(path + suffix, f"{path}{suffix}")
            return path
        for suffix in SIDECARS:  # лишний -journal без базы: ссылка на чужой файл не должна дожить до записи
            _require_regular(path + suffix, f"{path}{suffix}")
    return None


def _open(path):
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT, isolation_level=None)
    # Первым делом: схема базы не может вызывать небезопасные функции и вьюхи.
    con.execute("PRAGMA trusted_schema=OFF")
    return con


def _refuse_triggers(con, schema, label):
    found = con.execute(f"SELECT count(*) FROM {schema}.sqlite_master WHERE type = 'trigger'").fetchone()[0]
    if found:
        raise MergeError(f"{label} cookie database has triggers")


def _columns(con, schema):
    if not con.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = 'cookies'").fetchone():
        raise MergeError(f"cookies table is missing in {schema}")
    cols = [row[1] for row in con.execute(f"PRAGMA {schema}.table_info(cookies)")]
    for col in cols:
        if not IDENTIFIER.fullmatch(col):
            raise MergeError(f"unexpected column name in {schema}.cookies")
    return cols


def _copy_as_new_bot_database(source, bot_profile):
    """В профиле бота базы ещё нет (Chromium бота не успел её создать): база человека копируется как есть, но через
    временный файл и после проверки на триггеры."""
    target = os.path.join(bot_profile, COOKIE_PATHS[0])
    walk = bot_profile
    if not _require_real_dir(bot_profile):
        os.makedirs(bot_profile, mode=0o700)
    for part in os.path.dirname(COOKIE_PATHS[0]).split(os.sep):
        walk = os.path.join(walk, part)
        if not _require_real_dir(walk):
            os.mkdir(walk, 0o700)
    for suffix in ("",) + SIDECARS:
        if _lstat(target + suffix) is not None:
            raise MergeError(f"{target}{suffix} appeared while merging")
    temporary = target + ".merge-tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    try:
        src = _open(source)
        try:
            _columns(src, "main")
            _refuse_triggers(src, "main", "human")
            dst = sqlite3.connect(temporary, timeout=BUSY_TIMEOUT)
            try:
                src.backup(dst)
            finally:
                dst.close()
            count = src.execute("SELECT count(*) FROM cookies").fetchone()[0]
        finally:
            src.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        return count
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def merge_cookies(human_profile, bot_profile):
    """Сливает cookies, возвращает число перенесённых строк."""
    source = cookie_db(human_profile)
    if source is None:
        return 0
    target = cookie_db(bot_profile)
    if target is None:
        return _copy_as_new_bot_database(source, bot_profile)

    con = _open(target)
    try:
        con.execute("ATTACH DATABASE ? AS human", (source,))
        bot_cols = _columns(con, "main")
        human_cols = _columns(con, "human")
        _refuse_triggers(con, "main", "bot")
        _refuse_triggers(con, "human", "human")
        common = [col for col in bot_cols if col in human_cols]
        key = [col for col in KEY_COLUMNS if col in common]
        if any(col not in key for col in REQUIRED_KEY):
            raise MergeError("cookies table has no usable key")
        names = ", ".join(f'"{col}"' for col in common)
        same_key = " AND ".join(f'cookies."{col}" IS h."{col}"' for col in key)
        con.execute("BEGIN IMMEDIATE")
        try:
            count = con.execute("SELECT count(*) FROM human.cookies").fetchone()[0]
            con.execute(f"DELETE FROM main.cookies WHERE EXISTS "
                        f"(SELECT 1 FROM human.cookies AS h WHERE {same_key})")
            con.execute(f"INSERT INTO main.cookies ({names}) SELECT {names} FROM human.cookies")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        return count
    finally:
        con.close()


def main(argv):
    if len(argv) != 3:
        print("usage: cookie-merge.py HUMAN_PROFILE BOT_PROFILE", file=sys.stderr)
        return 2
    try:
        merged = merge_cookies(argv[1], argv[2])
    except (MergeError, sqlite3.Error, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"merged {merged}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
