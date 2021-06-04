#!/usr/bin/env python3
# -*- coding:utf-8 -*-

from pathlib import Path

import click
import webbrowser

from click_project.decorators import (
    argument,
    flag,
    option,
    group,
    use_settings,
)
from click_project.lib import (
    call,
    createfile,
    updated_env,
    find_available_port,
)
from click_project.config import config
from click_project.log import get_logger
from click_project.types import DynamicChoice
from click_project.colors import Colorer
from click_project.core import settings_stores

LOGGER = get_logger(__name__)


class SupervisorFileList(DynamicChoice):
    def choices(self):
        return config.settings2["supervisor"].get("files", [])


class SupervisorProcessList(DynamicChoice):
    def choices(self):
        s = settings_stores["supervisor"]
        return [
            info["name"]
            for info in
            s.rpc.supervisor.getAllProcessInfo()
        ]


class Supervisor:
    def __init__(self):
        self.location = Path(
            (config.local_profile or config.global_profile).location
        ) / "supervisor"
        self.conf_file = self.location / "supervisord.conf"
        self.socket_file = self.location / "supervisord.sock"
        self.log_file = self.location / "supervisord.log"
        self.pid_file = self.location / "supervisord.pid"
        self.port_file = self.location / "port.txt"

    @property
    def port(self):
        if Path(self.port_file).exists():
            return int(Path(self.port_file).read_text())

    @property
    def configuration(self):
        return f"""
[unix_http_server]
file={self.socket_file}

[inet_http_server]
port=:{self.port}

[supervisord]
logfile={self.log_file}
logfile_maxbytes=50MB
logfile_backups=10
loglevel=info
pidfile={self.pid_file}
nodaemon=false
minfds=1024
minprocs=200
childlogdir={self.location}

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix://{self.socket_file}

[include]
files = {" ".join(self.resolved_files)}
"""

    def create_config(self):
        port = find_available_port(9001)
        Path(self.port_file).write_text(str(port))
        createfile(
            self.conf_file,
            self.configuration,
            makedirs=True,
        )

    @property
    def rpc(self):
        from supervisor.xmlrpc import SupervisorTransport
        from xmlrpc.client import ServerProxy
        s = SupervisorTransport(None, None, f"unix://{self.socket_file}")
        return ServerProxy("http://127.0.0.1", s)

    @property
    def resolved_files(self):
        for profile in config.filter_enabled_profiles(
                config.all_directory_profiles
        ):
            for file in profile.settings.get("supervisor").get("files", []):
                if not(Path(file).is_absolute()):
                    candidate = Path(profile.location).resolve() / file
                    if candidate.exists():
                        yield str(candidate)
                    else:
                        # particular case of the project files
                        if (
                                config.project and
                                (Path(config.project).resolve() / file).exists()
                        ):
                            yield str(Path(config.project).resolve() / file)
                        else:
                            LOGGER.warning(f"{candidate} does not exist anymore")

    @property
    def files(self):
        if "files" not in config.supervisor.writable:
            config.supervisor.writable["files"] = []
        return config.supervisor.writable["files"]

    def save(self):
        config.supervisor.write()

    def run(self):
        if self.pid_file.exists():
            self.shutdown()
        self.create_config()
        with updated_env(**config.external_commands_environ_variables):
            call(
                [
                    "supervisord",
                    "--config",
                    self.conf_file,
                ],
            )

    def ctl(self, commands=[]):
        call(
            [
                "supervisorctl",
                "--config",
                self.conf_file,
            ] + commands,
        )

    def log(self):
        call(
            [
                "tail",
                "-f",
                self.log_file,
            ],
        )

    def shutdown(self):
        self.ctl(["shutdown"])


@group()
@use_settings("supervisor", Supervisor)
def supervisor():
    "Manipulate a local supervisor"


@supervisor.group(default_command="show")
def files():
    "Configure the source files"


@files.command()
@Colorer.color_options
def show(**kwargs):
    """Show the files used in the supervisor"""
    with Colorer(kwargs) as colorer:
        values = {
            k: "\n".join(v.get("files", []))
            for k, v in config.supervisor.all_settings.items()
        }
        values = colorer.colorize(values, config.supervisor.readprofile)
        print(
            "\n".join(values)
        )


@files.command()
@argument("file", help="A file to add")
def add(file):
    """Add a file to be considered by the supervisor"""
    toadd = Path(file)
    if not toadd.is_absolute():
        if (
                toadd.resolve().is_relative_to(
                    Path(config.supervisor.profile.location).resolve()
                )
        ):
            toadd = toadd.resolve().relative_to(
                Path(config.supervisor.profile.location).resolve()
            )
        # special case for the local profile, look into the project
        # itself
        elif (
                config.project
                and
                toadd.resolve().is_relative_to(Path(config.project).resolve())
        ):
            toadd = toadd.resolve().relative_to(
                Path(config.project).resolve()
            )
    else:
        toadd = toadd.resolve()
    toadd = str(toadd)
    if toadd in config.supervisor.files:
        LOGGER.info(f"{toadd} already taken into account")
    else:
        LOGGER.status(f"Added {toadd} to the {config.supervisor.writeprofilename} files")
        config.supervisor.files.append(toadd)
        config.supervisor.save()


@files.command()
@argument("file", help="A file to remove", type=SupervisorFileList())
def remove(file):
    """Don't consider a file anymore"""
    while file in config.supervisor.files:
        config.supervisor.files.remove(file)
    LOGGER.status(
        f"Removed {file} from the {config.supervisor.writeprofilename} files"
    )
    config.supervisor.save()


@supervisor.command()
@flag("--status/--no-status",
      help="Immediately show the status")
def run(status):
    "Run the local supervisor"
    config.supervisor.run()
    if status:
        ctx = click.get_current_context()
        ctx.invoke(_status)
    LOGGER.status(f"Started and available on http://localhost:{config.supervisor.port}")


@supervisor.command()
def ctl():
    "Run the supervisor controller"
    config.supervisor.ctl()


@supervisor.command()
def log():
    "Show the logs of the supervisor"
    config.supervisor.log()


@supervisor.command()
def shutdown():
    "Stop the supervised programs and the supervisor"
    config.supervisor.shutdown()


@supervisor.command()
def update():
    "Update the supervisor without restarting it"
    config.supervisor.ctl(["update"])


@supervisor.command()
def _status():
    "Show the supervisor status"
    config.supervisor.ctl(["status"])


@supervisor.command()
@argument("process", help="The process to follow",
          type=SupervisorProcessList())
@option("-e", "--err/--out",
        help="Show the error stream instead of the stdout")
@option("-n", "--number",
        help="The number of bytes to get from the tail",
        type=int
        )
@flag("-f", "--follow", help="Don't stop")
def tail(process, err, follow, number):
    "Show the output of a process"
    stream = "stderr" if err else "stdout"
    args = ["tail"]
    if follow:
        args += ["-f"]
    if number is not None:
        args += [f"-{number}"]
    args += [process, stream]
    config.supervisor.ctl(args)


@supervisor.command()
@argument("process", help="The process to start",
          type=SupervisorProcessList())
@flag("-f", "--follow", help="Also follow its output")
@option("-e", "--err/--out",
        help="Show the error stream instead of the stdout")
def start(process, follow, err):
    "Start a process"
    config.supervisor.ctl(["start", process])
    if follow:
        ctx = click.get_current_context()
        ctx.invoke(tail, process=process, err=err, follow=follow)


@supervisor.command()
@argument("process", help="The process to stop",
          type=SupervisorProcessList())
def stop(process):
    "Stop a process"
    config.supervisor.ctl(["stop", process])


@supervisor.command()
def browse():
    "Open the web interface"
    webbrowser.open(f"http://127.0.0.1:{config.supervisor.port}")


@supervisor.command()
def dump_config():
    "Show the configuration created for supervisor"
    print(config.supervisor.configuration)


@supervisor.command()
def ipython():
    "Run ipython in the context of the command"
    s = config.supervisor
    import IPython
    IPython.start_ipython(argv=[], user_ns={**globals(), **locals()})
