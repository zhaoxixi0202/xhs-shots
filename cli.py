"""
XHS Shots — command line interface
====================================
Examples
--------
  # 1) Log in once (headed browser) so cookies persist in ./profile
  python cli.py login

  # 2) Capture a single note
  python cli.py single --url "https://www.xiaohongshu.com/explore/xxxx" \
        --mode full --out shot.png

  # 3) Batch from Excel: links in column A, screenshots into column C
  python cli.py excel --file notes.xlsx --link-col A --out-col C --mode full
"""

from __future__ import annotations

import argparse
from pathlib import Path

from capture import CaptureEngine, CaptureError, LoginWallError
from excelio import run_excel

PROFILE = Path(__file__).parent / "profile"


def cmd_login(args):
    with CaptureEngine(PROFILE, headless=False) as e:
        e.open_login_page()
        print("OPENED")  # signal for the GUI
        print("已打开浏览器，请在小红书页面登录 / 完成验证。")
        print("登录（或解决验证）后直接关闭浏览器窗口即可，状态会自动保存。")
        try:
            e._ctx.wait_for_event("close", timeout=600000)
        except Exception:
            pass
        print("DONE")


def cmd_single(args):
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with CaptureEngine(PROFILE, headless=not args.show) as e:
        try:
            p = e.capture(
                args.url, args.mode,
                selector=args.selector, keyword=args.keyword,
                region=_parse_region(args.region), out_path=out,
            )
            print(f"OK -> {p}")
        except LoginWallError as ex:
            print(f"登录墙: {ex}")
            print("请先运行: python cli.py login")
        except CaptureError as ex:
            print(f"捕获失败: {ex}")


def cmd_excel(args):
    with CaptureEngine(PROFILE, headless=True) as e:
        out = run_excel(
            args.file, e, args.link_col, args.out_col,
            header_rows=args.header, mode=args.mode,
            selector=args.selector, keyword=args.keyword,
            region=_parse_region(args.region),
        )
        print(f"完成 -> {out}")


def _parse_region(s):
    if not s:
        return None
    parts = [int(x) for x in s.split(",")]
    if len(parts) != 4:
        raise SystemExit("region must be x,y,w,h")
    return tuple(parts)


def main():
    p = argparse.ArgumentParser(description="XHS Shots — 小红书笔记截图工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login", help="打开浏览器手动登录小红书")
    pl.set_defaults(func=cmd_login)

    ps = sub.add_parser("single", help="抓取单个链接")
    ps.add_argument("--url", required=True)
    ps.add_argument("--mode", default="full", choices=["full", "viewport", "element", "keyword", "region"])
    ps.add_argument("--selector")
    ps.add_argument("--keyword")
    ps.add_argument("--region", help="x,y,w,h")
    ps.add_argument("--out", default="shot.png")
    ps.add_argument("--show", action="store_true", help="显示浏览器窗口")
    ps.set_defaults(func=cmd_single)

    pe = sub.add_parser("excel", help="批量抓取 Excel 中的链接")
    pe.add_argument("--file", required=True)
    pe.add_argument("--link-col", required=True, help="链接所在列，如 A")
    pe.add_argument("--out-col", required=True, help="截图写入列，如 C")
    pe.add_argument("--header", type=int, default=1)
    pe.add_argument("--mode", default="full", choices=["full", "viewport", "element", "keyword", "region"])
    pe.add_argument("--selector")
    pe.add_argument("--keyword")
    pe.add_argument("--region", help="x,y,w,h")
    pe.set_defaults(func=cmd_excel)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
