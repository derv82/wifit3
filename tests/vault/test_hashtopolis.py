from __future__ import annotations

from types import SimpleNamespace

import pytest

from wifit3.hashtopolis.client import HashtopolisConfigError, HashtopolisTransientError
from wifit3.hashtopolis.ledger import HashtopolisLedger
from wifit3.vault.tools.base import ToolAlreadySubmitted, ToolTransientError
from wifit3.models import PersistedCapture
from wifit3.models.access_point import CaptureType
from wifit3.models.jobs import ToolStatus
from wifit3.vault.tools.hashtopolis import HashtopolisTool, line_digest

_ESSID = "Home".encode("utf-8").hex()
L1 = f"WPA*01*{'a' * 32}*001122334455*aabbccddeeff*{_ESSID}***"
L2 = f"WPA*02*{'b' * 32}*001122334455*aabbccddeeff*{_ESSID}*{'c' * 64}*{'d' * 32}*00"


class FakeClient:
    def __init__(self, base_url="http://ht.local"):
        self.base_url = base_url
        self.created: list = []
        self.hashlists: dict = {}
        self._next = 100
        self.access_group = 1
        self.fail_create = None
        self.fail_after_create = None

    def resolve_access_group(self):
        return self.access_group

    def find_hashlist(self, upload_marker, access_group):
        return self.hashlists.get((upload_marker, access_group))

    def create_hashlist(self, name, lines, access_group, *, notes=""):
        if self.fail_create is not None:
            raise self.fail_create
        hid = self._next
        self._next += 1
        self.created.append((name, list(lines), access_group))
        self.hashlists[(notes, access_group)] = hid
        if self.fail_after_create is not None:
            exc = self.fail_after_create
            self.fail_after_create = None
            raise exc
        return hid


def _cfg(tmp_path, **over):
    base = dict(hashtopolis_url="http://ht.local", hashtopolis_token="tok",
                hashtopolis_access_group_id=None, hashtopolis_trusted_agents_only=True,
                captures_dir=str(tmp_path))
    base.update(over)
    return SimpleNamespace(**base)


def _tool(tmp_path, client, cfg=None):
    cfg = cfg or _cfg(tmp_path)
    return HashtopolisTool(config=cfg, client_factory=lambda *a: client,
                           ledger_factory=lambda: HashtopolisLedger(tmp_path / "hashtopolis.json"))


def _capture(tmp_path, lines=(L1,)):
    path = tmp_path / "Home_00-11-22-33-44-55.hc22000"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return PersistedCapture(type=CaptureType.HS, timestamp=0, path=str(path),
                            bssid="00:11:22:33:44:55", ssid="Home")


def test_can_crack_requires_hc22000_and_config(tmp_path):
    client = FakeClient()
    cap = _capture(tmp_path)
    assert _tool(tmp_path, client).can_crack(cap) is True
    txt = PersistedCapture(type=CaptureType.WEP, timestamp=0, path="x.txt", bssid="00:11:22:33:44:55")
    assert _tool(tmp_path, client).can_crack(txt) is False
    assert _tool(tmp_path, client, cfg=_cfg(tmp_path, hashtopolis_url=None)).can_crack(cap) is False
    assert _tool(tmp_path, client, cfg=_cfg(tmp_path, hashtopolis_token=None)).can_crack(cap) is False


def test_legacy_password_fields_do_not_count_as_configured(tmp_path):
    client = FakeClient()
    cap = _capture(tmp_path)
    cfg = _cfg(tmp_path, hashtopolis_token=None,
               hashtopolis_username="u", hashtopolis_password="p")
    assert _tool(tmp_path, client, cfg=cfg).can_crack(cap) is False


def test_launch_uploads_hashlist_named_after_wlan_and_records(tmp_path):
    client = FakeClient()
    cap = _capture(tmp_path, [L1])
    tracking = _tool(tmp_path, client).launch(cap, {})
    assert len(client.created) == 1
    name, lines, access_group = client.created[0]
    assert name.startswith("Home [wifit3-") and lines == [L1] and access_group == 1
    assert tracking["hashlist_id"] == 100
    assert tracking["hashlist_name"] == name
    assert tracking["count"] == 1
    ledger = HashtopolisLedger(tmp_path / "hashtopolis.json")
    assert line_digest(L1) in ledger.submitted_digests("http://ht.local", 1)


def test_launch_dedup_raises_already_submitted(tmp_path):
    client = FakeClient()
    cap = _capture(tmp_path, [L1])
    _tool(tmp_path, client).launch(cap, {})
    with pytest.raises(ToolAlreadySubmitted):
        _tool(tmp_path, client).launch(cap, {})
    assert len(client.created) == 1


def test_launch_uploads_only_new_lines(tmp_path):
    client = FakeClient()
    cap = _capture(tmp_path, [L1])
    _tool(tmp_path, client).launch(cap, {})
    (tmp_path / "Home_00-11-22-33-44-55.hc22000").write_text("\n".join([L1, L2]) + "\n", encoding="utf-8")
    _tool(tmp_path, client).launch(cap, {})
    assert len(client.created) == 2
    assert client.created[1][1] == [L2]


def test_launch_digest_filter_uploads_only_requested(tmp_path):
    client = FakeClient()
    cap = _capture(tmp_path, [L1, L2])
    _tool(tmp_path, client).launch(cap, {"line_digests": [line_digest(L2)]})
    assert client.created[0][1] == [L2]


def test_launch_no_valid_lines_raises(tmp_path):
    client = FakeClient()
    path = tmp_path / "empty_00-11-22-33-44-55.hc22000"
    path.write_text("not a hashline\n", encoding="utf-8")
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path=str(path), bssid="00:11:22:33:44:55")
    with pytest.raises(ValueError):
        _tool(tmp_path, client).launch(cap, {})


def test_launch_hidden_ssid_uses_bssid_as_name(tmp_path):
    client = FakeClient()
    path = tmp_path / "x.hc22000"
    path.write_text(L1 + "\n", encoding="utf-8")
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path=str(path),
                           bssid="00:11:22:33:44:55", ssid=None)
    _tool(tmp_path, client).launch(cap, {})
    assert client.created[0][0].startswith("00:11:22:33:44:55 [wifit3-")


def test_launch_transient_becomes_tool_transient(tmp_path):
    client = FakeClient()
    client.fail_create = HashtopolisTransientError("offline")
    cap = _capture(tmp_path, [L1])
    with pytest.raises(ToolTransientError):
        _tool(tmp_path, client).launch(cap, {})


@pytest.mark.parametrize("failure", [
    HashtopolisTransientError("response lost"),
    HashtopolisConfigError("response contained no id"),
    ValueError("response was not JSON"),
])
def test_launch_recovers_when_post_response_is_unclear(tmp_path, failure):
    client = FakeClient()
    client.fail_after_create = failure
    tracking = _tool(tmp_path, client).launch(_capture(tmp_path, [L1]), {})
    assert tracking["hashlist_id"] == 100
    assert len(client.created) == 1


def test_confirmed_upload_survives_ledger_write_error_without_duplicate(tmp_path):
    class BrokenLedger:
        def submitted_digests(self, server, access_group_id):
            return set()

        def record(self, server, access_group_id, digests):
            raise OSError("disk full")

    client = FakeClient()
    cap = _capture(tmp_path, [L1])
    tool = HashtopolisTool(config=_cfg(tmp_path), client_factory=lambda *a: client,
                           ledger_factory=BrokenLedger)
    first = tool.launch(cap, {})
    second = tool.launch(cap, {})
    assert first["hashlist_id"] == second["hashlist_id"] == 100
    assert first["warning"] == "local upload history could not be saved"
    assert len(client.created) == 1


def test_poll_status_reports_upload_success(tmp_path):
    res = _tool(tmp_path, FakeClient()).poll_status({"hashlist_name": "Home", "count": 2})
    assert res.status == ToolStatus.SUCCESS
    assert "Home" in res.value
    assert res.result_data is None


def test_poll_status_includes_local_history_warning(tmp_path):
    res = _tool(tmp_path, FakeClient()).poll_status(
        {"hashlist_name": "Home", "count": 1,
         "warning": "local upload history could not be saved"})
    assert res.status == ToolStatus.SUCCESS
    assert "local upload history could not be saved" in res.value


def test_tracking_has_no_secrets_or_hashlines(tmp_path):
    client = FakeClient()
    cap = _capture(tmp_path, [L1, L2])
    tracking = _tool(tmp_path, client).launch(cap, {})
    blob = repr(tracking)
    assert L1 not in blob and L2 not in blob
    assert "tok" not in blob


def test_dedup_is_scoped_to_access_group(tmp_path):
    client = FakeClient()
    cap = _capture(tmp_path, [L1])
    _tool(tmp_path, client).launch(cap, {})
    client.access_group = 2
    tracking = _tool(tmp_path, client).launch(cap, {})
    assert len(client.created) == 2
    assert tracking["count"] == 1


@pytest.mark.parametrize("blob", ["[]", "null", "123", '"x"', "not json at all"])
def test_ledger_tolerates_malformed_json(tmp_path, blob):
    path = tmp_path / "hashtopolis.json"
    path.write_text(blob, encoding="utf-8")
    ledger = HashtopolisLedger(path)
    assert ledger.submitted_digests("http://ht.local", 1) == set()
    ledger.record("http://ht.local", 1, ["abc"])
    assert "abc" in ledger.submitted_digests("http://ht.local", 1)


def test_ledger_tolerates_wrong_value_type(tmp_path):
    path = tmp_path / "hashtopolis.json"
    path.write_text('{"http://ht.local#g1": "oops"}', encoding="utf-8")
    ledger = HashtopolisLedger(path)
    assert ledger.submitted_digests("http://ht.local", 1) == set()


def test_ledger_record_tolerates_unhashable_corrupt_list(tmp_path):
    path = tmp_path / "hashtopolis.json"
    path.write_text('{"http://ht.local#g1": [["unhashable"]]}', encoding="utf-8")
    ledger = HashtopolisLedger(path)
    ledger.record("http://ht.local", 1, ["abc"])   # must not raise TypeError
    assert "abc" in ledger.submitted_digests("http://ht.local", 1)


def test_upload_survives_non_oserror_ledger_failure(tmp_path):
    class ExplodingLedger:
        def submitted_digests(self, server, access_group_id):
            return set()

        def record(self, server, access_group_id, digests):
            raise TypeError("corrupt ledger")

    client = FakeClient()
    tracking = HashtopolisTool(config=_cfg(tmp_path), client_factory=lambda *a: client,
                               ledger_factory=ExplodingLedger).launch(_capture(tmp_path, [L1]), {})
    assert tracking["hashlist_id"] == 100
    assert tracking["warning"] == "local upload history could not be saved"
    assert len(client.created) == 1


def test_prepare_submit_config_sanitizes_url(tmp_path):
    tool = HashtopolisTool(config=_cfg(tmp_path, hashtopolis_url="http://user:pass@ht.local/?token=x"))
    target = tool.prepare_submit_config({})["target"]
    assert target["url"] == "http://ht.local"
    assert "pass" not in target["url"] and "token" not in target["url"]


def test_prepare_submit_config_snapshots_target(tmp_path):
    cfg = _cfg(tmp_path, hashtopolis_url="http://a.local", hashtopolis_access_group_id=5,
               hashtopolis_trusted_agents_only=True)
    tool = HashtopolisTool(config=cfg)
    enriched = tool.prepare_submit_config({"line_digests": ["x"]})
    assert enriched["target"] == {"url": "http://a.local", "access_group_id": 5, "is_secret": True}
    cfg.hashtopolis_url = "http://changed.local"       # a later config change must not rewrite it
    assert tool.prepare_submit_config(enriched)["target"]["url"] == "http://a.local"


def test_launch_builds_client_from_pinned_target(tmp_path):
    captured = {}
    client = FakeClient()

    def factory(target):
        captured["target"] = target
        return client

    tool = HashtopolisTool(config=_cfg(tmp_path), client_factory=factory,
                           ledger_factory=lambda: HashtopolisLedger(tmp_path / "l.json"))
    target = {"url": "http://pinned.local", "access_group_id": 9, "is_secret": False}
    tool.launch(_capture(tmp_path, [L1]), {"target": target})
    assert captured["target"] == target


def test_client_from_config_uses_pinned_target_when_it_still_matches(tmp_path):
    tool = HashtopolisTool(config=_cfg(tmp_path, hashtopolis_url="http://ht.local"))
    client = tool._client_from_config(
        {"url": "http://ht.local", "access_group_id": 7, "is_secret": False})
    assert client.base_url == "http://ht.local"
    assert client.access_group_id == 7
    assert client.is_secret is False


def test_client_from_config_refuses_when_server_changed(tmp_path):
    # Token now belongs to live.local; the queued job was pinned to ht.local. Refuse rather than
    # send the current token to the stale host.
    tool = HashtopolisTool(config=_cfg(tmp_path, hashtopolis_url="http://live.local"))
    with pytest.raises(HashtopolisConfigError):
        tool._client_from_config({"url": "http://ht.local", "access_group_id": 1, "is_secret": True})


def test_launch_refuses_stale_target(tmp_path):
    # The real client factory refuses before any network call, so a stale pinned target can never
    # ship the current token to the old server.
    tool = HashtopolisTool(config=_cfg(tmp_path, hashtopolis_url="http://live.local"),
                           ledger_factory=lambda: HashtopolisLedger(tmp_path / "l.json"))
    with pytest.raises(HashtopolisConfigError):
        tool.launch(_capture(tmp_path, [L1]), {"target": {"url": "http://old.local",
                                                          "access_group_id": 1, "is_secret": True}})
