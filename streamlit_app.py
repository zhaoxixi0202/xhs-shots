"""Streamlit Cloud 入口。

在云端首次启动时自动安装 Playwright 的 chromium 浏览器（Streamlit Cloud 不会自动执行
`playwright install`），安装完成后再加载真正的应用逻辑（app.py 里的 main()）。

本地运行时 Streamlit 直接跑 app.py 也行；本文件是给云端部署用的主入口。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_MARKER = Path.home() / ".cache" / "pw_chromium_installed"


def _ensure_chromium() -> None:
    """Install Playwright chromium once (idempotent)."""
    if _MARKER.exists():
        return
    print("[bootstrap] installing Playwright chromium (首次部署可能需 1-2 分钟)…")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            timeout=900,
        )
        _MARKER.write_text("ok")
        print("[bootstrap] chromium 安装完成 ✅")
    except Exception as exc:  # noqa: BLE001
        # 失败不应阻塞应用启动；运行时若缺浏览器会在截图时报错
        print(f"[bootstrap] playwright install 失败（可忽略，运行时会被捕获）: {exc}")


_ensure_chromium()

# 真正应用入口
from app import main  # noqa: E402

if __name__ == "__main__":
    main()
