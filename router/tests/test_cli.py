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
