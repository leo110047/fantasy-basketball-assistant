import json
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from threading import Barrier, Event, Thread
from urllib.parse import parse_qs, urlsplit

import pytest
from test_auction_io import annual_case as annual_case
from test_auction_io import frozen_auction as frozen_auction
from test_auction_io import projection_bundle as projection_bundle

from fba.adapters.auction import draft_template
from fba.apps.server import DeskServer
from fba.apps.workspace.modes import AuctionFiles, ModeManager, OpenMode, mode_url
from fba.apps.workspace.server import WorkspaceServer
from fba.contracts.base import DataError
from fba.data.codec import canonical
from fba.runtime.local import InstanceLock, existing_url


def request(origin, token, method, path, payload=None, headers=None):
    url = urlsplit(origin)
    connection = HTTPConnection(url.hostname, url.port, timeout=40)
    try:
        connection.request(
            method,
            path,
            json.dumps(payload) if payload is not None else None,
            {
                "Authorization": "Bearer " + token,
                "Origin": origin,
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def auction_files(root, source):
    root.mkdir(parents=True, exist_ok=True)
    draft = root / "draft.json"
    draft_template(source, 1, draft)
    return AuctionFiles(
        input_path=str(source), draft_path=str(draft), log_path=str(root / "calculations.jsonl")
    )


@pytest.mark.parametrize("mode", ["season", "auction"])
def test_real_mode_start_reuse_return_link_and_owned_shutdown(
    tmp_path, monkeypatch, mode, frozen_auction
):
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    root = tmp_path / "共用 入口"
    root.mkdir()
    files = auction_files(tmp_path / "競標 資料", frozen_auction[0]) if mode == "auction" else None
    wanted = OpenMode(mode=mode, auction=files)
    owner, reuse = ModeManager(root), ModeManager(root)
    try:
        url = owner.open(wanted)
        target = urlsplit(url)
        origin = f"{target.scheme}://{target.netloc}"
        assert request(origin, target.fragment, "GET", "/health")[0] == 200
        assert len(owner.children) == 1
        assert reuse.open(wanted) == owner.open(wanted) == url
        assert not reuse.children
        reuse.close()
        assert request(origin, target.fragment, "GET", "/health")[0] == 200
        linked = urlsplit(mode_url(url, "http://127.0.0.1:54321"))
        assert linked.fragment == target.fragment
        assert parse_qs(linked.query) == {"workspace": ["http://127.0.0.1:54321/"]}
        status, _, body = request(origin, target.fragment, "GET", "/?" + linked.query)
        assert status == 200 and b"workspaceLink" in body
        assert request(origin, target.fragment, "GET", "/workspace-link.js")[0] == 200
        before = Path(files.draft_path).read_bytes() if files else None
        assert (
            request(
                origin, target.fragment, "POST", "/api/quit", {}, {"Authorization": "Bearer wrong"}
            )[0]
            == 403
        )
        assert request(origin, target.fragment, "GET", "/health")[0] == 200
        if files:
            assert ModeManager(root).settings.auction == files
            assert Path(files.draft_path).read_bytes() == before
    finally:
        reuse.close()
        owner.close()
    lock_path = (
        root / "inseason.lock" if mode == "season" else root.parent / "競標 資料/draft.json.lock"
    )
    lock = InstanceLock(lock_path)
    assert lock.acquire()
    lock.release()
    with pytest.raises(DataError, match="沒有回應"):
        existing_url(lock, "inseason" if mode == "season" else "desk", timeout=0.1)


def test_workspace_http_boundary_does_not_launch_on_invalid_requests(tmp_path):
    manager = ModeManager(tmp_path)
    with WorkspaceServer(manager) as server:
        thread = Thread(target=server.run)
        thread.start()
        try:

            def call(method, path, payload=None, headers=None):
                return request(server.origin, server.token, method, path, payload, headers)

            for path in ("/", "/?view=home", "/app.js", "/style.css", "/court.jpg"):
                status, headers, _ = call("GET", path, headers={"Authorization": ""})
                assert status == 200 and headers["Cache-Control"] == "no-store"
            assert call("GET", "/api/bootstrap", headers={"Authorization": ""})[0] == 403
            assert call("GET", "/health", headers={"Authorization": ""})[0] == 403
            for headers in (
                {"Origin": "https://example.invalid"},
                {"Authorization": "bad"},
                {"Host": "bad.invalid"},
            ):
                assert call("POST", "/api/open", {"mode": "season"}, headers)[0] == 403
                assert call("POST", "/api/quit", {}, headers)[0] == 403
            assert call("POST", "/api/open", {"mode": "other"})[0] == 400
            assert call("POST", "/api/open", {"mode": "auction"})[0] == 400
            assert call("GET", "/../workspace.json")[0] == 404
            assert not manager.children
            assert not (tmp_path / "workspace.json").exists()
            assert call("POST", "/api/quit", {})[0] == 200
        finally:
            server.stopping.set()
            thread.join(timeout=2)
            manager.close()


def test_invalid_auction_keeps_saved_settings_and_releases_child(tmp_path, frozen_auction):
    files = auction_files(tmp_path / "files", frozen_auction[0])

    Path(files.input_path).write_text("invalid")
    manager = ModeManager(tmp_path)
    try:
        with pytest.raises(DataError, match="Expecting value"):
            manager.open(OpenMode(mode="auction", auction=files))
        assert not manager.children
        assert not manager.settings_path.exists()
        lock = InstanceLock(Path(files.draft_path).with_suffix(".json.lock"))
        assert lock.acquire()
        lock.release()
    finally:
        manager.close()


@pytest.mark.parametrize("name", ["workspace.json", "workspace.lock"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_reserved_workspace_paths_cannot_be_used_for_auction_logs(
    tmp_path, frozen_auction, name, alias
):
    root = tmp_path / "workspace"
    root.mkdir()
    manager = ModeManager(root)
    protected = root / name
    protected.write_bytes(b"preserve workspace state")
    log = protected
    if alias != "direct":
        log = tmp_path / "aliased.jsonl"
        if alias == "symlink":
            log.symlink_to(protected)
        else:
            log.hardlink_to(protected)
    files = auction_files(tmp_path / "files", frozen_auction[0])
    files = files.model_copy(update={"log_path": str(log)})
    try:
        with pytest.raises(DataError, match="工作區設定或鎖定檔"):
            manager.open(OpenMode(mode="auction", auction=files))
        assert protected.read_bytes() == b"preserve workspace state"
        assert not manager.children
        assert not Path(files.draft_path).with_suffix(".json.lock").exists()
    finally:
        manager.close()


@pytest.mark.parametrize("change", ["input_path", "log_path", "input_contents"])
def test_auction_reuse_rejects_changed_launch_files_without_remembering_them(
    tmp_path, frozen_auction, change
):
    files = auction_files(tmp_path / "files", frozen_auction[0])
    root = tmp_path / "workspace"
    root.mkdir()
    owner = ModeManager(root)
    source = Path(files.input_path)
    original = source.read_bytes()
    try:
        wanted = OpenMode(mode="auction", auction=files)
        url = owner.open(wanted)
        saved = owner.settings_path.read_bytes()
        draft = Path(files.draft_path).read_bytes()
        if change == "input_contents":
            source.write_bytes(original + b"\n")
            changed = files
        else:
            other = tmp_path / "other.json"
            if change == "input_path":
                other.write_bytes(original)
            changed = files.model_copy(update={change: str(other)})
        reuse = ModeManager(root)
        try:
            with pytest.raises(DataError, match="競標桌與所選檔案不一致"):
                reuse.open(OpenMode(mode="auction", auction=changed))
            assert not reuse.children
            assert reuse.settings.auction == files
            assert owner.settings_path.read_bytes() == saved
            assert Path(files.draft_path).read_bytes() == draft
            target = urlsplit(url)
            assert request(f"http://{target.netloc}", target.fragment, "GET", "/health")[0] == 200
            source.write_bytes(original)
            assert reuse.open(wanted) == url
        finally:
            reuse.close()
    finally:
        source.write_bytes(original)
        owner.close()


def test_legacy_desk_without_launch_identity_is_not_assumed_to_match(tmp_path, frozen_auction):
    files = auction_files(tmp_path / "files", frozen_auction[0])
    lock = InstanceLock(Path(files.draft_path).with_suffix(".json.lock"))
    assert lock.acquire()
    manager = ModeManager(tmp_path)
    with DeskServer(None, 0) as server:
        # The optional field must not alter older season/workspace health payloads.
        assert b"launch_key" not in canonical(server.instance)
        lock.publish(server.instance)
        assert lock.read() == server.instance
        thread = Thread(target=server.run)
        thread.start()
        try:
            with pytest.raises(DataError, match="無法確認其設定"):
                manager.open(OpenMode(mode="auction", auction=files))
            assert not manager.children
            assert not manager.settings_path.exists()
            assert request(server.origin, server.token, "GET", "/health")[0] == 200
        finally:
            server.stopping.set()
            thread.join(timeout=2)
            manager.close()
            lock.release()


def test_racing_launchers_only_remember_the_files_the_winning_desk_loaded(
    tmp_path, frozen_auction, monkeypatch
):
    files = auction_files(tmp_path / "files", frozen_auction[0])
    managers = [ModeManager(tmp_path / str(i)) for i in range(2)]
    choices = [files, files.model_copy(update={"log_path": str(tmp_path / "other.jsonl")})]
    ready = Barrier(2)
    reserved = Event()
    start = ModeManager.start

    def racing_start(manager, *args):
        reserved.set()
        ready.wait(timeout=10)
        return start(manager, *args)

    monkeypatch.setattr(ModeManager, "start", racing_start)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(managers[0].open, OpenMode(mode="auction", auction=choices[0]))
            # Enter after the first launcher's temporary lock has been released,
            # then race both real child processes for the lifetime draft lock.
            assert reserved.wait(timeout=5)
            second = pool.submit(managers[1].open, OpenMode(mode="auction", auction=choices[1]))
            results = [first, second]
        winners = [i for i, result in enumerate(results) if result.exception() is None]
        assert len(winners) == 1, [repr(result.exception()) for result in results]
        winner = winners[0]
        loser = 1 - winner
        with pytest.raises(DataError, match="競標桌與所選檔案不一致"):
            results[loser].result()
        assert managers[winner].settings.auction == choices[winner]
        assert ModeManager(managers[winner].root).settings.auction == choices[winner]
        assert not managers[loser].settings_path.exists()
        assert not managers[loser].children
        target = urlsplit(results[winner].result())
        assert request(f"http://{target.netloc}", target.fragment, "GET", "/health")[0] == 200
    finally:
        for manager in managers:
            manager.close()
