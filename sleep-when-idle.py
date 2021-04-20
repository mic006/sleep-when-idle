#! /usr/bin/python3

"""Sleep when idle.

Daemon to detect when the system is idle and initiate the transition to sleep.

Desktop environments perform a transition to sleep when there is no input from the user.
But the machine can have on-going tasks that shall maintain the machine alive until completion:
- long download from Internet or the network
- media served to a TV
- heavy CPU task (compilation, compression...)

The criteria to identify that the system is idle is configurable:
- no X user inputs (requires `xprintidle`)
- idle CPU time
- idle network

VERSION:
"""

import argparse
import datetime
import json
import logging
import multiprocessing
import re
import signal
import subprocess
import threading
import typing

Logger = logging.getLogger()


def parse_duration(duration_str: str) -> int:
    """Parse string giving a duration.

    The string provides a duration, with mandatory unit(s): 10s, 3m30s, 2h.
    (optional unit would lead to ambiguity).
    Upper 'M' stand for 'Month', lower 'm stands for 'minute'. Other units are case insensitive (no ambiguity).
    Big units are converted simply: a year is 365 days, a month is 30 days.

    If no units are provided, the value is considered in seconds (can be a float value).

    Args:
        duration_str: string representing a duration

    Returns:
        number of seconds
    """
    match = re.fullmatch(
        r"(?:(\d+)[yY])?(?:(\d+)M)?(?:(\d+)[wW])?(?:(\d+)[dD])?(?:(\d+)[hH])?(?:(\d+)m)?(?:(\d+)[sS])?",
        duration_str,
    )
    if not match:
        # no unit, shall contain a single integer or float value
        if not re.fullmatch(r"\d+(?:\.\d*)?", duration_str):
            raise ValueError(f"invalid duration string '{duration_str}'")
        return float(duration_str)
    years, months, weeks, days, hours, minutes, seconds = map(
        lambda x: 0 if x is None else int(x), match.groups()
    )
    days += 365 * years + 30 * months + 7 * weeks
    hours += 24 * days
    minutes += 60 * hours
    seconds += 60 * minutes
    return seconds


def parse_time(time_str: str) -> datetime.time:
    """Parse string giving a time of the day.

    Expected input: HH:MM[:SS], in 24-hour format
    """
    match = re.fullmatch(
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?",
        time_str,
    )
    if not match:
        raise ValueError(f"invalid time string '{time_str}', wrong format")
    hour, minute, second = map(lambda x: 0 if x is None else int(x), match.groups())
    if hour >= 24 or minute >= 60 or second >= 60:
        raise ValueError(f"invalid time string '{time_str}', out of range value")
    return datetime.time(hour, minute, second)


def datetime_now() -> datetime.datetime:
    """Get current datetime in local aware timezone."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone()


def get_cpu_idle() -> float:
    """Get cumulated system idle time"""
    with open("/proc/uptime", "rt") as uptime_file:
        return float(uptime_file.readline().split()[1])


class NetStat(typing.NamedTuple):
    """Network statistics"""

    ifname: str = "No UP interface"  # interface name
    rx_packets: int = 0  # number of received packets
    tx_packets: int = 0  # number of transmitted packets


def get_net_stat() -> NetStat:
    """Get network statistics"""
    res = subprocess.run(
        ["ip", "-j", "-s", "link"], capture_output=True, check=True, text=True
    )
    parsed_output = json.loads(res.stdout)
    for interface in parsed_output:
        if interface["operstate"] == "UP":
            return NetStat(
                interface["ifname"],
                interface["stats64"]["rx"]["packets"],
                interface["stats64"]["tx"]["packets"],
            )
    return NetStat()


class SleepWhenIdle:
    """Main class, keeping context of daemon"""

    def __init__(self):
        self.exit_event = threading.Event()
        # configure signals
        for sig in signal.SIGINT, signal.SIGTERM:
            signal.signal(sig, self._signal_handler)

        # build parser
        parser = argparse.ArgumentParser(
            description=__doc__, formatter_class=argparse.RawTextHelpFormatter
        )
        parser.add_argument(
            "-d",
            "--debug",
            action="store_true",
            help="get debug log",
        )
        parser.add_argument(
            "-P",
            "--pretend",
            action="store_true",
            help="log instead of requesting sleep",
        )
        parser.add_argument(
            "-t",
            "--time",
            type=parse_duration,
            default="10m",
            help="minimum idle time to transition to sleep",
        )
        parser.add_argument(
            "-s",
            "--state",
            default="suspend",
            choices=["suspend", "hibernate", "hybrid-sleep"],
            help="wanted sleep state",
        )
        parser.add_argument(
            "-w",
            "--wake-up",
            metavar="HH:MM[:SS]",
            type=parse_time,
            help="local time for wake-up (in the next 24 hours)",
        )
        parser.add_argument(
            "-x",
            "--x-user",
            metavar="USER",
            help="check lack of X inputs from the user, via `xprintidle`",
        )
        parser.add_argument(
            "-p",
            "--meas-period",
            type=parse_duration,
            default="10s",
            help="measurement period, for CPU / network usage",
        )
        parser.add_argument(
            "-c",
            "--cpu",
            metavar="MAX_USAGE%",
            type=int,
            help="maximum CPU usage allowed to consider idle, in %%",
        )
        parser.add_argument(
            "-n",
            "--network",
            metavar="PACKETS",
            type=int,
            help="maximum number of Rx / Tx packets over a measurement period to consider idle",
        )

        # parse command line arguments
        self.args = parser.parse_args()

        if self.args.pretend:
            self.args.debug = True

        logging.basicConfig(
            level=logging.DEBUG if self.args.debug else logging.INFO,
            format=("%(asctime)s " if self.args.debug else "")
            + "%(levelname)-8s %(module)-25s %(message)s",
        )

        # context
        self.nb_threads = multiprocessing.cpu_count()
        self.wanted_idle_duration = (  # wanted idle time before transition to sleep
            datetime.timedelta(seconds=self.args.time)
        )
        if self.args.cpu is not None:
            self.cpu_idle_threshold = (  # threshold to consider CPU as idle
                1 - self.args.cpu / 100
            )

        self.reset()

        # validate access to xprintidle if requested
        if self.args.x_user is not None:
            self.get_x_user_idle()

    def _signal_handler(self, signum, _frame) -> None:
        """Signal handler"""
        Logger.info("Caught signal '%s'", signal.strsignal(signum))
        self.exit_event.set()

    def reset(self):
        """Reset the dynamic context"""
        Logger.debug("Reset dynamic context")
        self.prev_cpu_idle_counter = 0  # cpu idle counter at previous tick
        self.prev_net_stat = NetStat()  # net stats at previous tick
        self.last_idle = datetime_now()  # last time system was considered idle
        self.prev_check = self.last_idle  # time of previous tick
        self.now = self.last_idle  # current time

        if self.args.wake_up is not None:
            Logger.info("Resetting any programmed wake-up")
            if not self.args.pretend:
                res = subprocess.run(
                    ["rtcwake", "-m", "disable"],
                    capture_output=True,
                    check=True,
                    text=True,
                )
                Logger.debug("Command 'rtcwake -m disable' stdout: %s", res.stdout)

    def run(self):
        """Main task, run forever"""
        check_period = datetime.timedelta(seconds=self.args.meas_period)
        while not self.exit_event.is_set():
            # wait for next measurement period
            next_check = self.prev_check + check_period
            self.now = datetime_now()
            if self.now < next_check:
                self.exit_event.wait((next_check - self.now).total_seconds())
                self.now = datetime_now()

            # exit if requested
            if self.exit_event.is_set():
                break

            # skipping a check period indicates a sleep cycle
            if self.now > next_check + check_period:
                self.reset_idle()

            # check CPU usage
            if self.args.cpu is not None:
                self.check_cpu()

            # check network usage
            if self.args.network is not None:
                self.check_net()

            # check user input in X server
            if (
                self.args.x_user is not None
                and self.now > self.last_idle + self.wanted_idle_duration
            ):
                self.check_x_user_input()

            # enough idle time ?
            if self.now > self.last_idle + self.wanted_idle_duration:
                self.go_to_sleep()

            # go for a new period
            self.prev_check = self.now

        Logger.info("Terminated")

    def reset_idle(self, idle_delta=None):
        """Reset idle time to current"""
        if idle_delta is None:
            Logger.debug(
                "System is not considered as idle; resetting last_idle to current time"
            )
            self.last_idle = self.now
        else:
            last_idle = self.now - idle_delta
            if last_idle > self.last_idle:
                self.last_idle = last_idle
                Logger.debug("Updating last_idle as %s", self.last_idle)

    def check_cpu(self):
        """Check CPU usage over a period"""
        cpu_idle_counter = get_cpu_idle()
        Logger.debug("cpu_idle_counter: %s", cpu_idle_counter)
        average_cpu_idle = (
            (cpu_idle_counter - self.prev_cpu_idle_counter)
            / self.nb_threads
            / (self.now - self.prev_check).total_seconds()
        )
        Logger.debug("average_cpu_idle: %s", average_cpu_idle)

        if average_cpu_idle < self.cpu_idle_threshold:
            # CPU usage is too high, system is not idle
            self.reset_idle()

        self.prev_cpu_idle_counter = cpu_idle_counter

    def check_net(self):
        """Check network usage over a period"""
        net_stat = get_net_stat()
        Logger.debug("net_stat: %s", net_stat)

        if (
            net_stat.ifname != self.prev_net_stat.ifname
            or net_stat.rx_packets > self.prev_net_stat.rx_packets + self.args.network
            or net_stat.tx_packets > self.prev_net_stat.tx_packets + self.args.network
        ):
            # network usage is too high, system is not idle
            self.reset_idle()

        self.prev_net_stat = net_stat

    def get_x_user_idle(self) -> int:
        """Get idle time of user under X"""
        # root has no access to the Xsession
        # To launch xprintidle and have a proper access to the Xsession, use the following command:
        # DISPLAY=:0 runuser -l user -w DISPLAY -c xprintidle
        res = subprocess.run(
            ["runuser", "-l", self.args.x_user, "-w", "DISPLAY", "-c", "xprintidle"],
            env={"DISPLAY": ":0"},
            capture_output=True,
            check=True,
            text=True,
        )
        x_idle_ms = int(res.stdout)
        Logger.debug("x_idle: %d ms", x_idle_ms)
        return x_idle_ms

    def check_x_user_input(self):
        """Check inputs from user in Xserver"""
        x_idle_ms = self.get_x_user_idle()
        x_idle = datetime.timedelta(milliseconds=x_idle_ms)

        if x_idle < self.wanted_idle_duration:
            # update idle based on x_idle
            self.reset_idle(x_idle)

    def go_to_sleep(self):
        """Initiate transition to sleep"""
        Logger.info("System is idle, going to sleep")
        if self.args.wake_up is not None:
            self.program_wakeup()
        # reset state
        self.reset()
        if not self.args.pretend:
            # sleep; note: the request is asynchronous
            subprocess.run(["systemctl", self.args.state], check=True)

    def program_wakeup(self):
        """Program wake-up"""
        # determine wake-up time: today ?
        wake_up = datetime.datetime.combine(
            self.now.date(), self.args.wake_up
        ).astimezone()
        if wake_up < self.now:
            # wake_up is tomorrow
            wake_up = datetime.datetime.combine(
                self.now.date() + datetime.timedelta(days=1), self.args.wake_up
            ).astimezone()
        Logger.info("Programming wake-up for %s", wake_up)
        if not self.args.pretend:
            timestamp = str(int(wake_up.timestamp()))
            res = subprocess.run(
                ["rtcwake", "-m", "no", "-t", timestamp],
                capture_output=True,
                check=True,
                text=True,
            )
            Logger.debug(
                "Command 'rtcwake -m no -t %s' stdout: %s", timestamp, res.stdout
            )


if __name__ == "__main__":
    SleepWhenIdle().run()
