"""Build and smoke-test the wheel outside the source checkout."""

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SMOKE = """
import os
from pathlib import Path
from fastapi.testclient import TestClient
import main
import self_improvement

installed = Path(os.environ["SMOKE_ENV"]).resolve()
assert Path(main.__file__).resolve().is_relative_to(installed)
assert Path(self_improvement.__file__).resolve().is_relative_to(installed)
with TestClient(main.app) as client:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    assert client.get("/static/vendor/purify.min.js").status_code == 200
    for role in ("master", "agent"):
        prompt = client.get(f"/api/prompts/{role}").json()["prompt"]
        assert "Core Operating Principles" in prompt
    headers = {"Authorization": "Bearer package-smoke-agent-token"}
    body = {"order_id": "WHEEL", "content": "Verified package", "message_key": "wheel-smoke"}
    created = client.post("/api/executions", headers=headers, json=body)
    assert created.status_code == 200, created.text
    retry = client.post("/api/executions", headers=headers, json=body)
    assert retry.json() == created.json()
    records = client.get("/api/improvements", headers=headers).json()
    assert len(records) == 1
    record = client.get(f'/api/improvements/{records[0]["filename"]}', headers=headers)
    assert record.status_code == 200
print("Installed wheel API, static assets, prompts, and improvement records verified.")
"""


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bridge-package-") as temp:
        work = Path(temp)
        wheels = work / "wheels"
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "--wheel-dir", str(wheels)],
            check=True,
        )
        wheel, = wheels.glob("agent_bridge-*.whl")
        environment = work / "venv"
        # Reuse test dependencies, but install Bridge exclusively from its wheel.
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
            cwd=work, env=env, check=True,
        )
        env.update({
            "SMOKE_ENV": str(environment),
            "MASTER_TOKEN": "package-smoke-master-token",
            "AGENT_TOKEN": "package-smoke-agent-token",
            "BRIDGE_DB": str(work / "data" / "bridge.db"),
            "IMPROVEMENTS_DIR": str(work / "improvements"),
        })
        subprocess.run([str(python), "-c", SMOKE], cwd=work, env=env, check=True)


if __name__ == "__main__":
    main()
