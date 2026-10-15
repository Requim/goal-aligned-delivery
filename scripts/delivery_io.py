"""进度与交付工具共享的可靠标准库 IO。"""

import contextlib
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path


MAX_JSON_BYTES = 2 * 1024 * 1024


class UsageError(ValueError):
    """输入格式、结构或调用方式无效。"""


class ConditionError(Exception):
    """输入有效，但执行前提或完成条件尚未满足。"""


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UsageError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise UsageError(f"invalid JSON constant: {value}")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise UsageError(f"non-finite JSON number: {value}")
    return number


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise UsageError("JSON input exceeds 2 MiB")
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except UnicodeDecodeError as error:
        raise UsageError("JSON input must be UTF-8") from error
    except json.JSONDecodeError as error:
        raise UsageError(f"invalid JSON: {error.msg}") from error


def canonical_hash(value):
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise UsageError(f"value is not canonical JSON: {error}") from error
    return hashlib.sha256(encoded).hexdigest()


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            hasher.update(block)
    return hasher.hexdigest()


def atomic_write(path, text):
    target = Path(path)
    if not isinstance(text, str):
        raise UsageError("atomic_write text must be a string")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def atomic_create(path, text):
    """原子发布新文件；任何已存在的目录项都不会被替换。"""
    target = Path(path)
    if not isinstance(text, str):
        raise UsageError("atomic_create text must be a string")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def write_json(path, value):
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
    except (TypeError, ValueError) as error:
        raise UsageError(f"value is not JSON serializable: {error}") from error
    atomic_write(path, text)


@contextlib.contextmanager
def locked(path):
    target = Path(path)
    lock_path = target.parent / f".{target.name}.lock"
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
