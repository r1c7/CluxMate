"""Tests for egress wiring in the shell sandbox backends."""

from cluxmate.tools._sandbox import (
    BwrapSandbox,
    DarwinSeatbeltSandbox,
    apply_egress_env,
)


def test_apply_egress_env_proxy_injects_all_variants():
    env = apply_egress_env({"PATH": "/bin"}, "proxy", ("127.0.0.1", 7890))
    url = "http://127.0.0.1:7890"
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert env[k] == url
    assert env["NO_PROXY"] == ""
    assert env["no_proxy"] == ""
    assert env["PATH"] == "/bin"


def test_apply_egress_env_shared_is_noop():
    env = {"PATH": "/bin"}
    assert apply_egress_env(env, "shared", None) == env


def test_bwrap_off_adds_unshare_net(tmp_path):
    sb = BwrapSandbox(egress_mode="off")
    argv = sb._bwrap_argv(str(tmp_path))
    assert "--unshare-net" in argv


def test_bwrap_shared_has_no_unshare_net(tmp_path):
    sb = BwrapSandbox(egress_mode="shared")
    argv = sb._bwrap_argv(str(tmp_path))
    assert "--unshare-net" not in argv


def test_seatbelt_off_denies_network():
    sb = DarwinSeatbeltSandbox(egress_mode="off")
    assert "(deny network*)" in sb._profile()


def test_seatbelt_shared_has_no_network_deny():
    sb = DarwinSeatbeltSandbox(egress_mode="shared")
    assert "(deny network*)" not in sb._profile()
