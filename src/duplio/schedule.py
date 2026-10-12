"""Generate OS-native schedules for ``duplio run PROFILE``.

Duplio does not run a daemon. Instead it writes the configuration your
operating system already uses to run things on time — systemd user timers on
Linux, launchd on macOS, Task Scheduler on Windows — or prints a cron line.
"""

from __future__ import annotations

import shlex
import shutil
import sys
from dataclasses import dataclass

from duplio.errors import ConfigError

CALENDARS = {
    "hourly": ("hourly", "0 * * * *", {"Minute": 0}, "HOURLY"),
    "daily": ("*-*-* 02:30:00", "30 2 * * *", {"Hour": 2, "Minute": 30}, "DAILY"),
    "weekly": ("Sun *-*-* 03:00:00", "0 3 * * 0", {"Weekday": 0, "Hour": 3, "Minute": 0}, "WEEKLY"),
    "monthly": ("*-*-01 04:00:00", "0 4 1 * *", {"Day": 1, "Hour": 4, "Minute": 0}, "MONTHLY"),
}


@dataclass
class ScheduleFiles:
    kind: str
    files: dict[str, str]  # filename -> content
    instructions: str


def _command(profile: str) -> list[str]:
    exe = shutil.which("duplio")
    base = [exe] if exe else [sys.executable, "-m", "duplio"]
    return [*base, "run", profile, "--yes", "--quiet"]


def make_schedule(profile: str, every: str, *, platform: str | None = None) -> ScheduleFiles:
    if every not in CALENDARS:
        raise ConfigError(f"--every must be one of {', '.join(CALENDARS)}")
    systemd_cal, cron, launchd_cal, win = CALENDARS[every]
    platform = platform or sys.platform
    cmd = _command(profile)
    unit = f"duplio-{profile}"
    if platform.startswith("linux"):
        service = (
            "[Unit]\n"
            f"Description=Duplio profile {profile}\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"ExecStart={shlex.join(cmd)}\n"
            "Nice=10\n"
            "IOSchedulingClass=idle\n"
        )
        timer = (
            "[Unit]\n"
            f"Description=Run Duplio profile {profile} {every}\n\n"
            "[Timer]\n"
            f"OnCalendar={systemd_cal}\n"
            "Persistent=true\n"
            "RandomizedDelaySec=10min\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )
        return ScheduleFiles(
            "systemd",
            {f"{unit}.service": service, f"{unit}.timer": timer},
            "Copy both files to ~/.config/systemd/user/ and run:\n"
            f"  systemctl --user daemon-reload && systemctl --user enable --now {unit}.timer\n"
            f"Alternative cron line:  {cron} {shlex.join(cmd)}",
        )
    if platform == "darwin":
        keys = "".join(f"\n      <key>{k}</key><integer>{v}</integer>" for k, v in launchd_cal.items())
        args = "".join(f"\n    <string>{a}</string>" for a in cmd)
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            f"  <key>Label</key><string>org.duplio.{profile}</string>\n"
            f"  <key>ProgramArguments</key>\n  <array>{args}\n  </array>\n"
            f"  <key>StartCalendarInterval</key>\n    <dict>{keys}\n    </dict>\n"
            "  <key>LowPriorityIO</key><true/>\n"
            "</dict>\n</plist>\n"
        )
        name = f"org.duplio.{profile}.plist"
        return ScheduleFiles(
            "launchd",
            {name: plist},
            f"Copy {name} to ~/Library/LaunchAgents/ and run:\n  launchctl load ~/Library/LaunchAgents/{name}",
        )
    if platform == "win32":
        inner = " ".join(f'\\"{a}\\"' if " " in a else a for a in cmd)
        return ScheduleFiles(
            "schtasks",
            {},
            f'Run in a terminal:\n  schtasks /Create /SC {win} /TN "Duplio {profile}" /TR "{inner}"',
        )
    return ScheduleFiles("cron", {}, f"Add to 'crontab -e':\n  {cron} {shlex.join(cmd)}")
