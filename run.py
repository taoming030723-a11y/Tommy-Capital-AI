"""One-command launcher. Creates an isolated environment beside this file."""
import os
from pathlib import Path
import subprocess
import sys
import venv


def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    if sys.version_info < (3, 11):
        print("请安装64位 Python 3.11 或更新版本。", file=sys.stderr)
        return 2
    env = root / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        if not python.exists():
            print("首次运行：建立运行环境并安装真实数据接口……", flush=True)
            venv.create(env, with_pip=True)
        marker = env / ".tommy-version"
        fingerprint = (root / "pyproject.toml").read_text(encoding="utf-8")
        if not marker.exists() or marker.read_text(encoding="utf-8") != fingerprint:
            subprocess.run([str(python), "-m", "pip", "install", "-e", str(root)], check=True)
            marker.write_text(fingerprint, encoding="utf-8")
        flags = sys.argv[1:]
        if (root / "config.json").exists() and "--config" not in flags:
            flags += ["--config", "config.json"]
        if (root / "holdings.json").exists() and "--holdings" not in flags:
            flags += ["--holdings", "holdings.json"]
        return subprocess.call([str(python), "-m", "tommy_capital.cli", *flags])
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"运行环境安装失败：{exc}；请检查网络和Python安装。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
