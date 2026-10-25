from reprouter.cli import child_env, main


def test_off_on_status_toggle_kill_file(tmp_path, monkeypatch, capsys):
    kill = tmp_path / "off"
    monkeypatch.setenv("REPROUTER_KILL_FILE", str(kill))
    assert main(["off"]) == 0 and kill.exists()
    main(["status"])
    assert "OFF" in capsys.readouterr().out
    assert main(["on"]) == 0 and not kill.exists()
    main(["status"])
    assert "ON" in capsys.readouterr().out


def test_child_env_points_claude_at_the_proxy_without_touching_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-SECRET")
    env = child_env(47901)
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:47901"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-SECRET"  # passed through untouched, never printed


def test_unknown_command_is_an_error(capsys):
    try:
        main(["nope"])
    except SystemExit as e:
        assert e.code != 0


def test_proxy_options_flow_into_config(tmp_path, monkeypatch):
    from reprouter.cli import build_parser, _proxy_config

    monkeypatch.setenv("REPROUTER_TRANSFORMS", "whitespace,log_filter")
    args = build_parser().parse_args(["proxy", "--port", "1234", "--mode", "record", "--canary",
                                      "--state-file", str(tmp_path / "s.json"), "--corpus-dir", str(tmp_path / "c")])
    cfg = _proxy_config(args)
    assert cfg.mode == "record" and cfg.canary is True
    assert str(cfg.state_file) == str(tmp_path / "s.json") and str(cfg.corpus_dir) == str(tmp_path / "c")
    assert [t.name for t in cfg.policy.transforms] == ["whitespace", "log_filter"]
