"""Launch the existing tools and own only the processes started by this workspace."""

from http.client import HTTPConnection
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Literal
from urllib.parse import urlencode, urlsplit

from fba.apps.inseason.server import serve_inseason
from fba.apps.server import auction_launch_key, serve
from fba.apps.workers import worker_limit
from fba.contracts.base import DataError, Record, Text
from fba.data.codec import canonical, decode, digest, read_bytes
from fba.data.storage import atomic_write
from fba.runtime.local import Instance, InstanceLock, existing_url
from fba.runtime.processes import watch_parent


class AuctionFiles(Record):
    input_path: Text
    draft_path: Text
    log_path: Text


class OpenMode(Record):
    mode: Literal["season", "auction"]
    auction: AuctionFiles | None = None


class WorkspaceSettings(Record):
    auction: AuctionFiles | None = None


def run_mode(root: Path, request: OpenMode, errors: Connection) -> None:
    watch_parent()
    try:
        if request.mode == "season":
            serve_inseason(root, open_browser=False)
        else:
            files = request.auction
            if files is None:
                raise DataError("請先指定競標資料與名單檔案。")
            serve(
                Path(files.input_path),
                Path(files.draft_path),
                Path(files.log_path),
                worker_limit("auto"),
                0,
                open_browser=False,
            )
    except Exception as exc:
        errors.send(str(exc))
    finally:
        errors.close()


def normalize_files(files: AuctionFiles | None) -> AuctionFiles:
    if files is None:
        raise DataError("請先指定競標資料、名單與紀錄檔案。")
    paths = [
        Path(value).expanduser() for value in (files.input_path, files.draft_path, files.log_path)
    ]
    if any(not path.is_absolute() for path in paths):
        raise DataError("請填入完整檔案路徑。")
    source, draft, log = (path.resolve() for path in paths)
    if not source.is_file() or not draft.is_file():
        raise DataError("找不到競標資料或名單檔案；請選擇已建立的 JSON 檔。")
    if not log.parent.is_dir() or log.is_dir():
        raise DataError("紀錄檔的資料夾不存在，或指定的是資料夾。")
    return AuctionFiles(input_path=str(source), draft_path=str(draft), log_path=str(log))


def stop_mode(process: BaseProcess, instance: Instance | None) -> None:
    # A reused service belongs to its original launcher, never to this workspace.
    if process.is_alive() and instance is not None and instance.pid == process.pid:
        connection = HTTPConnection("127.0.0.1", instance.port, timeout=2)
        try:
            connection.request(
                "POST",
                "/api/quit",
                b"{}",
                {
                    "Authorization": "Bearer " + instance.token,
                    "Origin": f"http://127.0.0.1:{instance.port}",
                    "Content-Type": "application/json",
                },
            )
            connection.getresponse().read(4096)
        except OSError:
            pass  # The owned process may already have stopped; join below is authoritative.
        finally:
            connection.close()
    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
    if process.is_alive():
        raise DataError("工具未能停止，請保留啟動視窗並檢查執行中的行程。")
    process.close()


class ModeManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.settings_path = root / "workspace.json"
        self.settings = (
            decode(WorkspaceSettings, read_bytes(self.settings_path), "workspace.settings")
            if self.settings_path.exists()
            else WorkspaceSettings()
        )
        self.children: list[tuple[BaseProcess, Instance | None]] = []
        self.mutex = Lock()
        self.closing = Event()

    def open(self, request: OpenMode) -> str:
        with self.mutex:
            if self.closing.is_set():
                raise DataError("入口正在關閉，請重新啟動助手。")
            if request.mode == "auction":
                request = request.model_copy(update={"auction": normalize_files(request.auction)})
            files = request.auction
            launch_key = None
            if request.mode == "auction" and files is not None:
                paths = (Path(files.input_path), Path(files.draft_path), Path(files.log_path))
                reserved = (self.settings_path, self.root / "workspace.lock")
                if any(
                    path == protected.resolve()
                    or (path.exists() and protected.exists() and path.samefile(protected))
                    for path in paths
                    for protected in reserved
                ):
                    raise DataError("競標檔案不能使用工作區設定或鎖定檔，請選擇其他路徑。")
                launch_key = auction_launch_key(paths, digest(read_bytes(paths[0])))
            lock = InstanceLock(
                Path(files.draft_path).with_suffix(Path(files.draft_path).suffix + ".lock")
                if request.mode == "auction" and files is not None
                else self.root / "inseason.lock"
            )
            app = "desk" if request.mode == "auction" else "inseason"
            if not lock.acquire():
                url = existing_url(lock, app, expected_launch_key=launch_key)
            else:
                lock.release()
                url = self.start(request, lock, app, launch_key)
            if request.mode == "auction":
                settings = WorkspaceSettings(auction=files)
                atomic_write(self.settings_path, canonical(settings))
                self.settings = settings
            return url

    def start(self, request: OpenMode, lock: InstanceLock, app: str, launch_key: str | None) -> str:
        context = get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=run_mode, args=(self.root, request, sender))
        process.start()
        sender.close()
        instance = None
        try:
            deadline = monotonic() + 30
            while monotonic() < deadline and not self.closing.is_set():
                try:
                    url = existing_url(lock, app, timeout=0.25)
                except DataError:
                    if receiver.poll():
                        try:
                            message = receiver.recv()
                        except EOFError:
                            message = "工具已停止，請確認資料檔案後重試。"
                        raise DataError(str(message)) from None
                    if not process.is_alive():
                        raise DataError("工具啟動失敗，請確認資料檔案後重試。") from None
                    continue
                instance = lock.read()
                if launch_key is not None:
                    # Do not retry a healthy service with incompatible launch settings.
                    url = existing_url(lock, app, expected_launch_key=launch_key)
                if instance.pid != process.pid:
                    # Another launcher won the lock while this process was starting.
                    process.join(timeout=3)
                    if process.is_alive():
                        raise DataError("工具啟動競爭尚未結束，請重試。")
                    process.close()
                else:
                    self.children.append((process, instance))
                return url
            raise DataError("工具未在 30 秒內完成啟動，請確認資料檔案後重試。")
        except Exception:
            stop_mode(process, instance)
            raise
        finally:
            receiver.close()

    def close(self) -> None:
        self.closing.set()
        with self.mutex:
            for process, instance in self.children:
                stop_mode(process, instance)
            self.children.clear()


def mode_url(url: str, workspace_origin: str) -> str:
    target = urlsplit(url)
    query = urlencode({"workspace": workspace_origin + "/"})
    return f"{target.scheme}://{target.netloc}/?{query}#{target.fragment}"
