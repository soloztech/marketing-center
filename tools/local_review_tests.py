#!/usr/bin/env python3
"""Run review regression suites in a task-owned local PostgreSQL cluster.

No credentials or existing database are used. The cluster listens only on a
private Unix socket; mail, scheduled work and the queue runner are disabled.
Runtime sources and Python packages are read-only inputs, never installed into.
"""

import argparse
import os
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "test", "upgrade", "serve", "stop"))
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--peer", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--suite", default="core")
    parser.add_argument("--modules", default="marketing_center_base")
    parser.add_argument("--tags")
    parser.add_argument("--http-port", type=int, default=18182)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", args.suite):
        parser.error("suite must be a short lowercase identifier")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*(,[a-z_][a-z0-9_]*)*", args.modules):
        parser.error("modules must be comma-separated addon names")
    state = args.state.resolve()
    if state.name != "20260912-marketing-review-fixes":
        parser.error("state must be the dedicated review directory")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = state / "local-cluster-owner"
    expected = "marketing-review-fixes-20260912\n"
    if marker.exists() and marker.read_text() != expected:
        parser.error("cluster ownership marker mismatch")
    runtime = args.runtime.resolve()
    pg = runtime / "postgres/usr/lib/postgresql/16/bin"
    pgdata = state / "pgdata"
    # Socket lives below our owned directory, never an existing runtime socket.
    socket = state / "socket"
    env = os.environ.copy()
    env.update(
        PGOPTIONS="-c jit=off",
        PYTHONDONTWRITEBYTECODE="1",
        ODOO_QUEUE_JOB_CHANNELS="root:0",
        LD_LIBRARY_PATH=":".join(
            str(runtime / p)
            for p in (
                "native/usr/lib/aarch64-linux-gnu",
                "postgres/usr/lib/aarch64-linux-gnu",
            )
        ),
        PYTHONPATH=":".join(
            str(p)
            for p in (
                args.packages.resolve(),
                runtime / "native/usr/lib/python3/dist-packages",
            )
        ),
    )

    def run(command, **kwargs):
        return subprocess.run([str(x) for x in command], env=env, check=True, **kwargs)

    if args.action == "init":
        if pgdata.exists() and not marker.exists():
            parser.error("refusing to adopt an existing unmarked cluster")
        marker.write_text(expected)
        socket.mkdir(exist_ok=True, mode=0o700)
        if not (pgdata / "PG_VERSION").exists():
            with (state / "initdb.log").open("w") as log:
                run(
                    [
                        pg / "initdb",
                        "-D",
                        pgdata,
                        "-A",
                        "trust",
                        "-U",
                        "review_local",
                        "--no-locale",
                        "--encoding=UTF8",
                    ],
                    stdout=log,
                )
        run(
            [
                pg / "pg_ctl",
                "-D",
                pgdata,
                "-l",
                state / "postgres.log",
                "-o",
                f"-k {socket} -p 55492 -c listen_addresses='' -c jit=off",
                "-w",
                "start",
            ]
        )
        return
    if not marker.exists() or not (pgdata / "PG_VERSION").exists():
        parser.error("initialize this task's cluster first")
    if args.action == "stop":
        run([pg / "pg_ctl", "-D", pgdata, "-m", "fast", "-w", "stop"])
        return
    repo = Path(__file__).resolve().parents[1]
    addons = [
        runtime / "OCB/odoo/addons",
        runtime / "OCB/addons",
        runtime / "queue",
        repo,
        args.peer.resolve(),
    ]
    db = "mc_review_20260912_" + args.suite
    command = [
        runtime / "venv/bin/python",
        runtime / "OCB/odoo-bin",
        "--db_host",
        socket,
        "--db_port",
        "55492",
        "--db_user",
        "review_local",
        "-d",
        db,
        "--data-dir",
        state / "data",
        "--addons-path",
        ",".join(str(p) for p in addons),
        "--max-cron-threads=0",
        "--workers=0",
        "--http-interface=127.0.0.1",
        "--http-port",
        str(args.http_port),
        "--without-demo=all",
        "--load=base,web",
        "--smtp=127.0.0.1",
        "--smtp-port=1",
        "--logfile",
        state / f"{args.suite}-{args.action}.log",
    ]
    if args.action in ("test", "upgrade"):
        command += [
            "-i" if args.action == "test" else "-u",
            args.modules,
            "--stop-after-init",
            "--test-enable",
            "--test-tags",
            args.tags or ",".join("/" + name for name in args.modules.split(",")),
        ]
    else:
        command += ["--db-filter", "^" + db + "$"]
    run(command)


if __name__ == "__main__":
    main()
