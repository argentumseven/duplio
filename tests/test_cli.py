from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from duplio.cli.main import cli
from duplio.config import dump_toml, load_settings, save_settings, validate_profile
from duplio.errors import ConfigError
from duplio.schedule import make_schedule

from .conftest import rand_bytes, write


def invoke(*args: str, input: str | None = None):  # type: ignore[no-untyped-def]
    runner = CliRunner(env={"COLUMNS": "200"})
    result = runner.invoke(cli, ["--no-color", *args], input=input, catch_exceptions=False)
    return result


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    data = rand_bytes(200_000, 1)
    write(tmp_path / "a" / "x.jpg", data, mtime=1_600_000_000)
    write(tmp_path / "b" / "x (1).jpg", data, mtime=1_600_000_000)
    write(tmp_path / "b" / "[odd] name.txt", "same" * 30)
    write(tmp_path / "a" / "[odd] name.txt", "same" * 30)
    write(tmp_path / "b" / "only-b.bin", rand_bytes(999, 2))
    return tmp_path


def test_help_lists_sections() -> None:
    out = invoke("--help").output
    for word in ("Find & clean duplicates", "Safety net", "Back up", "dedupe", "ingest", "mirror",
                 "Compare, merge & sync folders", "Network drives", "Automation", "diff", "merge", "sync", "snapshot",
                 "manifest", "versions", "probe", "sentinel", "watch", "notify"):
        assert word in out


def test_scan_json_and_export(tree: Path, tmp_path: Path) -> None:
    result = invoke("scan", str(tree / "a"), str(tree / "b"), "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data["groups"]) == 2
    report = tmp_path / "report.html"
    result = invoke("scan", str(tree / "a"), str(tree / "b"), "--export", str(report))
    assert result.exit_code == 0 and "duplicate groups" in result.output
    html = report.read_text()
    assert "[odd] name.txt" in html and "<html" in html
    csv_path = tmp_path / "r.csv"
    invoke("scan", str(tree / "a"), str(tree / "b"), "--export", str(csv_path))
    assert csv_path.read_text().count("\n") == 1 + 4


def test_scan_errors_are_clean(tmp_path: Path) -> None:
    result = invoke("scan", str(tmp_path / "missing"))
    assert result.exit_code == 2 and "Error:" in result.output
    result = invoke("scan", str(tmp_path), str(tmp_path))
    assert result.exit_code == 2 and "overlap" in result.output


def test_dedupe_requires_confirmation_then_undo(tree: Path) -> None:
    result = invoke("dedupe", str(tree / "a"), str(tree / "b"))
    assert result.exit_code == 2 and "--yes" in result.output  # non-interactive without --yes
    assert (tree / "b" / "x (1).jpg").exists()
    result = invoke("dedupe", str(tree / "a"), str(tree / "b"), "--dry-run")
    assert result.exit_code == 0 and "Dry run" in result.output
    assert (tree / "b" / "x (1).jpg").exists()
    result = invoke("dedupe", str(tree / "a"), str(tree / "b"), "--yes")
    assert result.exit_code == 0, result.output
    assert not (tree / "b" / "x (1).jpg").exists() and (tree / "a" / "x.jpg").exists()
    assert "duplio undo" in result.output
    hist = invoke("history")
    assert "quarantine" in hist.output
    q = invoke("quarantine", "list")
    assert "held" in q.output
    result = invoke("undo", "--yes")
    assert result.exit_code == 0 and "restored" in result.output
    assert (tree / "b" / "x (1).jpg").exists()


def test_delete_needs_explicit_acknowledgement(tree: Path) -> None:
    result = invoke("dedupe", str(tree / "a"), str(tree / "b"), "--action", "delete", "--yes")
    assert result.exit_code == 2 and "--i-understand" in result.output
    assert (tree / "b" / "x (1).jpg").exists()
    result = invoke("dedupe", str(tree / "a"), str(tree / "b"), "--action", "delete", "--yes", "--i-understand")
    assert result.exit_code == 0 and not (tree / "b" / "x (1).jpg").exists()


def test_plan_and_apply_from_session(tree: Path, tmp_path: Path) -> None:
    invoke("scan", str(tree / "a"), str(tree / "b"))
    sessions = invoke("sessions")
    assert "all" in sessions.output
    plan_path = tmp_path / "plan.json"
    result = invoke("plan", "--out", str(plan_path), "--keep", "prefer-root=2")
    assert result.exit_code == 0 and plan_path.exists()
    plan = json.loads(plan_path.read_text())
    assert {item["keeper"]["root"] for item in plan["items"]} == {1}
    result = invoke("apply", str(plan_path), "--yes")
    assert result.exit_code == 0
    assert not (tree / "a" / "x.jpg").exists() and (tree / "b" / "x (1).jpg").exists()


def test_compare_and_copy_missing(tree: Path, tmp_path: Path) -> None:
    rescue = tmp_path / "rescue"
    result = invoke("compare", str(tree / "b"), "--against", str(tree / "a"), "--copy-missing-to", str(rescue))
    assert result.exit_code == 0
    assert "only-b.bin" in result.output
    assert (rescue / "only-b.bin").read_bytes() == (tree / "b" / "only-b.bin").read_bytes()
    listed = invoke("-q", "compare", str(tree / "b"), "--against", str(tree / "a"), "--list")
    assert str(tree / "b" / "only-b.bin") in listed.output


def test_backup_commands(tmp_path: Path) -> None:
    src = tmp_path / "src"
    write(src / "a.txt", "hello", mtime=1_600_000_000)
    repo = tmp_path / "repo"
    assert invoke("backup", "init", str(repo)).exit_code == 0
    assert invoke("backup", "run", str(src), str(repo), "--label", "docs").exit_code == 0
    write(src / "b.txt", "world", mtime=1_600_000_001)
    assert invoke("backup", "run", str(src), str(repo), "--label", "docs").exit_code == 0
    snaps = invoke("backup", "snapshots", str(repo))
    assert snaps.output.count("docs") == 2
    ls = invoke("backup", "ls", str(repo), "latest")
    assert "a.txt" in ls.output and "b.txt" in ls.output
    assert invoke("backup", "check", str(repo), "--read-data").exit_code == 0
    out = tmp_path / "out"
    assert invoke("backup", "restore", str(repo), "latest", str(out)).exit_code == 0
    assert (out / "b.txt").read_text() == "world"
    forget = invoke("backup", "forget", str(repo), "--keep-last", "1")
    assert "Dry run" in forget.output
    forget = invoke("backup", "forget", str(repo), "--keep-last", "1", "--apply", "--prune")
    assert "Forgot 1 snapshot" in forget.output
    assert "1 snapshots" in invoke("backup", "stats", str(repo)).output


def test_mirror_and_ingest_commands(tmp_path: Path) -> None:
    src = tmp_path / "phone"
    write(src / "DCIM" / "IMG_20240101_101010.jpg", rand_bytes(3000, 1), mtime=1_700_000_000)
    result = invoke("mirror", str(src), str(tmp_path / "mirror"))
    assert result.exit_code == 0 and (tmp_path / "mirror" / "DCIM" / "IMG_20240101_101010.jpg").exists()
    assert "Already in sync" in invoke("mirror", str(src), str(tmp_path / "mirror")).output
    result = invoke("ingest", str(src), str(tmp_path / "lib"))
    assert result.exit_code == 0
    assert (tmp_path / "lib" / "2024" / "2024-01" / "IMG_20240101_101010.jpg").exists()
    assert "previously imported" in invoke("ingest", str(src), str(tmp_path / "lib")).output


def test_profiles_run_and_schedule(tmp_path: Path) -> None:
    src = tmp_path / "phone"
    write(src / "IMG_20240101_101010.jpg", rand_bytes(3000, 1), mtime=1_700_000_000)
    result = invoke("profile", "add", "phone", "--type", "ingest", "--set", f"source={src}", "--set", f"dest={tmp_path / 'lib'}")
    assert result.exit_code == 0
    assert "phone" in invoke("profile", "list").output
    result = invoke("run", "phone", "--yes")
    assert result.exit_code == 0
    assert list((tmp_path / "lib").rglob("*.jpg"))
    sched = invoke("schedule", "phone", "--every", "weekly")
    assert "OnCalendar=Sun" in sched.output or "StartCalendarInterval" in sched.output or "schtasks" in sched.output
    invoke("profile", "add", "gone", "--type", "ingest", "--set", f"source={tmp_path / 'unplugged'}", "--set", f"dest={tmp_path}")
    result = invoke("run", "gone", "--yes")
    assert result.exit_code == 3 and "not connected" in result.output
    assert invoke("profile", "add", "bad", "--type", "backup", "--set", "colour=red").exit_code == 2


def test_narrow_terminal_history_is_readable(tree: Path) -> None:
    invoke("dedupe", str(tree / "a"), str(tree / "b"), "--yes")
    narrow = CliRunner(env={"COLUMNS": "60"}).invoke(cli, ["--no-color", "history"])
    assert "quarantine" in narrow.output.replace("\n", "")


def test_misc_commands() -> None:
    assert "Keep rules" in invoke("rules").output
    assert invoke("devices").exit_code == 0
    doctor = invoke("doctor", "--no-benchmark")
    assert doctor.exit_code == 0 and "Python" in doctor.output
    assert invoke("cache", "stats").exit_code == 0
    assert invoke("config", "init").exit_code == 0
    assert "[general]" in invoke("config").output


# ---------------------------------------------------------------------------
# Config and schedules
# ---------------------------------------------------------------------------


def test_settings_roundtrip(dirs) -> None:  # type: ignore[no-untyped-def]
    settings = load_settings(dirs)
    settings.exclude = ["*.tmp", 'we"ird']
    settings.profiles["p"] = {"type": "mirror", "source": "/a", "dest": "smb://nas/b", "delete": True}
    save_settings(settings, dirs)
    again = load_settings(dirs)
    assert again.exclude == ["*.tmp", 'we"ird']
    assert again.profiles["p"]["delete"] is True


def test_profile_validation() -> None:
    with pytest.raises(ConfigError):
        validate_profile("x", {"type": "nope"})
    with pytest.raises(ConfigError):
        validate_profile("x", {"type": "backup", "source": "/a"})
    with pytest.raises(ConfigError):
        validate_profile("x", {"type": "dedupe", "roots": ["/a"], "action": "shred"})
    with pytest.raises(ConfigError):
        validate_profile("x", {"type": "backup", "source": "/a", "repo": "/b", "keep_daily": "7"})
    assert validate_profile("x", {"type": "dedupe", "roots": ["/a"], "keep": ["oldest"]})


def test_dump_toml_quoting() -> None:
    import tomllib

    text = dump_toml({"a": {"b": 'x"y\\z', "n": 3, "flag": False, "list": ["1", "2"]}, "profiles": {"my job": {"type": "x"}}})
    data = tomllib.loads(text)
    assert data["a"]["b"] == 'x"y\\z' and data["profiles"]["my job"]["type"] == "x"


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32", "freebsd"])
def test_schedules(platform: str) -> None:
    sched = make_schedule("job", "daily", platform=platform)
    text = sched.instructions + "".join(sched.files.values())
    assert "run" in text and "job" in text
    with pytest.raises(ConfigError):
        make_schedule("job", "fortnightly")


def test_every_registered_command_is_listed_in_the_help() -> None:
    out = invoke("--help").output
    for name, command in cli.commands.items():  # type: ignore[attr-defined]
        if not command.hidden:
            assert name in out, f"'{name}' is registered but missing from the top-level help"
