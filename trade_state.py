"""Private, account-scoped trading state with process-wide transaction locking.

Importing or constructing TradingState does not touch the filesystem. Callers
must hold a transaction across read/check/order/write and explicitly save their
changes. Corrupt or missing live state is never interpreted as an empty account.
"""
from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import re
import secrets
import stat
from contextlib import contextmanager
from datetime import date
from pathlib import Path


class StateError(RuntimeError):
    pass


class StateBusy(StateError):
    pass


_ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_TOP_KEYS = {"version", "mode", "account", "positions", "alerts", "orders", "events", "halt_reason"}
_MAX_STATE_BYTES = 32 * 1024 * 1024


def _directory_fd(path: Path, *, create: bool = False) -> int:
    """Walk using directory descriptors; never traverse a symlink component."""
    absolute = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def private_directory(path) -> Path:
    """Create a private owned directory, rejecting symlinks in every component."""
    path = Path(path).expanduser()
    fd = None
    try:
        fd = _directory_fd(path, create=True)
        if os.fstat(fd).st_uid != os.geteuid():
            raise StateError("state directory is not owned by the current user")
        os.fchmod(fd, 0o700)
    except OSError as exc:
        raise StateError("cannot establish private state directory") from exc
    finally:
        if fd is not None:
            os.close(fd)
    return path


def _integer(value, *, positive=False):
    return type(value) is int and value >= (1 if positive else 0)


def _finite_json(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise StateError("state contains a nonfinite number")
        return
    if type(value) is list:
        for item in value:
            _finite_json(item)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _finite_json(item)
        return
    raise StateError("state contains unsupported JSON values")


def _validate(data, mode, account):
    try:
        _finite_json(data)
    except RecursionError as exc:
        raise StateError("state nesting is invalid") from exc
    if type(data) is not dict or set(data) != _TOP_KEYS:
        raise StateError("invalid state schema")
    if type(data["version"]) is not int or data["version"] != 1:
        raise StateError("unsupported state version")
    if data["mode"] != mode or data["account"] != account:
        raise StateError("state mode or account does not match its namespace")
    if data["halt_reason"] is not None and type(data["halt_reason"]) is not str:
        raise StateError("invalid halt reason")
    if type(data["positions"]) is not list:
        raise StateError("positions must be a list")
    ids = set()
    for position in data["positions"]:
        fields = {"position_id", "contract_symbol", "underlying", "qty_initial", "qty_remaining",
                  "fill_price", "latches", "status"}
        if type(position) is not dict or not fields <= position.keys():
            raise StateError("invalid position record")
        identifier = position["position_id"]
        if type(identifier) is not str or not identifier or identifier in ids:
            raise StateError("position IDs must be nonempty and unique")
        ids.add(identifier)
        symbol, underlying = position["contract_symbol"], position["underlying"]
        if (type(symbol) is not str or len(symbol) != 21
                or not re.fullmatch(r"[A-Z]{1,6} *[0-9]{6}[CP][0-9]{8}", symbol)
                or type(underlying) is not str or not re.fullmatch(r"[A-Z]{1,6}", underlying)
                or symbol[:6] != underlying.ljust(6)):
            raise StateError("invalid position contract identity")
        try:
            date(2000 + int(symbol[6:8]), int(symbol[8:10]), int(symbol[10:12]))
            if int(symbol[13:]) <= 0:
                raise ValueError("nonpositive strike")
        except ValueError as exc:
            raise StateError("invalid position OCC date or strike") from exc
        initial, remaining = position["qty_initial"], position["qty_remaining"]
        if not _integer(initial, positive=True) or not _integer(remaining) or remaining > initial:
            raise StateError("invalid position quantity")
        price = position["fill_price"]
        try:
            valid_price = type(price) in (int, float) and math.isfinite(price) and price > 0
        except OverflowError:
            valid_price = False
        if not valid_price:
            raise StateError("invalid position fill price")
        latches = position["latches"]
        if (type(latches) is not dict or set(latches) != {"sl", "tp50", "tp200", "tp300"}
                or any(type(value) is not bool for value in latches.values())):
            raise StateError("invalid position latches")
        if position["status"] not in ("open", "closed"):
            raise StateError("invalid position status")
        if (position["status"] == "closed") != (remaining == 0):
            raise StateError("position status disagrees with remaining quantity")
    if type(data["alerts"]) is not dict or any(type(v) is not dict for v in data["alerts"].values()):
        raise StateError("invalid alert records")
    if type(data["events"]) is not list or any(type(v) is not dict for v in data["events"]):
        raise StateError("invalid event records")
    if type(data["orders"]) is not dict:
        raise StateError("invalid order records")
    for identifier, order in data["orders"].items():
        fields = {"intent_id", "side", "quantity", "filled_qty", "status"}
        if not identifier or type(order) is not dict or not fields <= order.keys() or order["intent_id"] != identifier:
            raise StateError("invalid order identity")
        if order["side"] not in ("buy", "sell"):
            raise StateError("invalid order side")
        if (not _integer(order["quantity"], positive=True) or not _integer(order["filled_qty"])
                or order["filled_qty"] > order["quantity"]):
            raise StateError("invalid order quantities")
        if order["status"] not in ("prepared", "unknown", "pending", "partially_filled", "filled", "rejected", "canceled"):
            raise StateError("invalid order status")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StateError("duplicate JSON key in state")
        result[key] = value
    return result


class _Transaction:
    def __init__(self, owner, directory_fd, data):
        self._owner = owner
        self._directory_fd = directory_fd
        self.data = data
        self._active = True

    def save(self):
        if not self._active:
            raise StateError("transaction is no longer active")
        _validate(self.data, self._owner.mode, self._owner.account)
        temporary = ".state-" + secrets.token_hex(16) + ".tmp"
        try:
            content = json.dumps(self.data, allow_nan=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            if len(content) > _MAX_STATE_BYTES:
                raise StateError("state exceeds maximum supported size")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self._directory_fd)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, "state.json", src_dir_fd=self._directory_fd, dst_dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise StateError("state could not be persisted") from exc
        finally:
            try:
                os.unlink(temporary, dir_fd=self._directory_fd)
            except FileNotFoundError:
                pass


class TradingState:
    def __init__(self, mode="dry_run", account=None, root=None):
        if mode not in ("dry_run", "live"):
            raise StateError("mode must be dry_run or live")
        if account is None:
            if mode == "live":
                raise StateError("live state requires an explicit account identifier")
            account = "paper"
        if type(account) is not str or not _ACCOUNT_RE.fullmatch(account):
            raise StateError("account identifier is not a safe namespace")
        self.mode, self.account = mode, account
        self.root = Path(root if root is not None else os.environ.get("COPYTRADER_STATE_DIR", "~/.local/state/copypasta")).expanduser()
        self.directory = self.root / mode / account
        self.path = self.directory / "state.json"

    def _empty(self):
        return {"version": 1, "mode": self.mode, "account": self.account, "positions": [],
                "alerts": {}, "orders": {}, "events": [], "halt_reason": None}

    @contextmanager
    def _locked(self):
        private_directory(self.root)
        private_directory(self.root / self.mode)
        private_directory(self.directory)
        directory_fd = _directory_fd(self.directory)
        lock_fd = None
        try:
            lock_fd = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory_fd)
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise StateError("invalid state lock file")
            os.fchmod(lock_fd, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StateBusy("another process holds this state transaction") from exc
            yield directory_fd
        except OSError as exc:
            raise StateError("cannot lock trading state") from exc
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(directory_fd)

    def _read(self, directory_fd):
        try:
            fd = os.open("state.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > _MAX_STATE_BYTES):
                raise StateError("state file is not a private regular file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(_MAX_STATE_BYTES + 1)
            if len(raw) > _MAX_STATE_BYTES:
                raise StateError("state exceeds maximum supported size")
            data = json.loads(raw, object_pairs_hook=_unique_object)
            _validate(data, self.mode, self.account)
            return data
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise StateError("state JSON is malformed") from exc
        finally:
            os.close(fd)

    @contextmanager
    def transaction(self, initialize=False):
        with self._locked() as directory_fd:
            data = self._read(directory_fd)
            if data is None:
                if self.mode == "live" and not initialize:
                    raise StateError("live state is missing; explicitly initialize only after account reconciliation")
                data = self._empty()
                tx = _Transaction(self, directory_fd, data)
                tx.save()
            else:
                tx = _Transaction(self, directory_fd, data)
            try:
                yield tx
            finally:
                tx._active = False

    def initialize(self):
        with self._locked() as directory_fd:
            if self._read(directory_fd) is not None:
                raise StateError("refusing to overwrite existing state")
            tx = _Transaction(self, directory_fd, self._empty())
            try:
                tx.save()
            finally:
                tx._active = False

    def snapshot(self):
        with self.transaction() as tx:
            return copy.deepcopy(tx.data)
