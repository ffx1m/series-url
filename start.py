import os
import signal
import subprocess
import sys
import time


processes = []
shutting_down = False


def terminate_all():
    global shutting_down
    shutting_down = True

    for process in processes:
        if process.poll() is None:
            process.terminate()

    deadline = time.time() + 20
    while time.time() < deadline:
        if all(process.poll() is not None for process in processes):
            return
        time.sleep(0.2)

    for process in processes:
        if process.poll() is None:
            process.kill()


def handle_signal(signum, _frame):
    print(f"Received signal {signum}, shutting down", flush=True)
    terminate_all()


def start_processes():
    port = os.environ.get("PORT", "10000")
    web_env = os.environ.copy()
    web_env["START_EMBEDDED_WORKER"] = "false"

    worker_env = os.environ.copy()
    worker_env["START_EMBEDDED_WORKER"] = "false"

    worker = subprocess.Popen([sys.executable, "run_worker.py"], env=worker_env)
    web = subprocess.Popen(
        [
            "gunicorn",
            "--bind",
            f"0.0.0.0:{port}",
            "--workers",
            os.environ.get("WEB_WORKERS", "1"),
            "--threads",
            os.environ.get("WEB_THREADS", "8"),
            "--timeout",
            os.environ.get("WEB_TIMEOUT", "0"),
            "app:app",
        ],
        env=web_env,
    )

    processes.extend([worker, web])
    return worker, web


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    worker, web = start_processes()
    print(f"Started worker pid={worker.pid}", flush=True)
    print(f"Started web pid={web.pid}", flush=True)

    while not shutting_down:
        for process in processes:
            code = process.poll()
            if code is not None:
                print(f"Process pid={process.pid} exited with code {code}; stopping container", flush=True)
                terminate_all()
                return code if code else 1
        time.sleep(1)

    return 0


if __name__ == "__main__":
    sys.exit(main())
