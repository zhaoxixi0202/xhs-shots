"""
XHS Shots — Streamlit GUI
==========================
Run:  streamlit run app.py
(or)  ./run.sh
"""

from __future__ import annotations

import io
import os
import sys
import subprocess
import tempfile
import time
from pathlib import Path

import streamlit as st
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).parent))
from capture import (  # noqa: E402
    CaptureEngine, CaptureError, LoginWallError,
    save_cookies, load_cookies, clear_cookies, cookie_status,
)
from excelio import run_excel, preview_columns, list_sheets, find_link_columns  # noqa: E402

PROFILE = Path(__file__).parent / "profile"
PROFILE.mkdir(exist_ok=True)
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
HISTORY_DIR = Path(__file__).parent / "history"
HISTORY_DIR.mkdir(exist_ok=True)

# Auto-cleanup: delete files older than 90 days (3 months)
_MAX_AGE_SECONDS = 90 * 24 * 3600


def cleanup_old_history():
    """Remove files older than 3 months from history dir. Returns count deleted."""
    now = time.time()
    deleted = 0
    for f in HISTORY_DIR.iterdir():
        if f.is_file() and (now - f.stat().st_mtime) > _MAX_AGE_SECONDS:
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
    # Also clean empty output dir files
    for f in OUTPUT_DIR.iterdir():
        if f.is_file() and (now - f.stat().st_mtime) > _MAX_AGE_SECONDS:
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted


def get_history_items(limit: int = 50) -> list[dict]:
    """Return list of historical result files, sorted by mtime desc."""
    items = []
    for d in [HISTORY_DIR, OUTPUT_DIR]:
        if not d.exists():
            continue
        for f in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not f.is_file():
                continue
            stat = f.stat()
            items.append({
                "path": f,
                "name": f.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                "is_image": f.suffix.lower() == ".png",
                "is_excel": f.suffix.lower() == ".xlsx",
            })
            if len(items) >= limit:
                break
    return sorted(items, key=lambda x: x["mtime"], reverse=True)

st.set_page_config(page_title="小红书笔记截图工具", page_icon="📸", layout="wide")

MODE_HELP = {
    "to_keyword": "🎯 截到关键词位置 — 横向全屏宽度，纵向从顶部截到关键词出现的位置（关键词留空则直接截整篇笔记正文）",
    "note": "✨ 仅笔记正文 — 自动截取笔记内容区，不含侧边栏和推荐",
    "full": "整页长截图（包含侧边栏、导航、底部推荐等全部内容）",
    "viewport": "只截当前屏幕可见区域",
    "element": "按 CSS 选择器截取某一个元素（手动填选择器，如 .note-content）",
    "keyword": "在笔记里找到包含某关键词的内容块并截取（如「价格」「教程步骤」）",
    "region": "按固定像素区域截取 x,y,w,h（手动填坐标）",
}


def launch_login():
    subprocess.Popen([sys.executable, "cli.py", "login"], cwd=str(Path(__file__).parent),
                     start_new_session=True)


def mode_params(mode: str, tab_id: str = "single"):
    """Render the param widgets for the chosen mode; return a dict.
    `tab_id` ensures widget keys are unique between the two tabs."""
    st.caption(MODE_HELP[mode])
    out = {}
    if mode == "element":
        st.markdown("**常用选择器参考：**")
        st.code("""#detail-title      → 笔记标题
.note-content       → 笔记正文
.cover              → 封面图
#noteContainer      → 整个笔记容器
section.note        → 笔记主体""", language="text")
        out["selector"] = st.text_input("CSS 选择器", placeholder="例如 .note-content 或 #detail-title", key=f"sel_{tab_id}")
    elif mode in ("keyword", "to_keyword"):
        if mode == "to_keyword":
            st.markdown("**输入关键词，截图将从页面顶部截到该关键词出现的位置（全屏宽度）。**")
        else:
            st.markdown("**输入笔记正文里包含的关键词，工具会找到并截取该内容块。**")
        st.caption("示例：价格、教程、材料、步骤、总结、链接、夸克扫描王")
        out["keyword"] = st.text_input("关键词", placeholder="笔记里要截取的内容包含的词，例如「价格」「夸克扫描王」", key=f"kw_{tab_id}_{mode}")
    elif mode == "region":
        st.markdown("**填写截图区域的左上角坐标 + 宽高（像素）。**")
        st.caption("提示：桌面视图宽 1280，手机视图宽 414。x=0 是最左边，y=0 是最上边。")
        col1, col2 = st.columns(2)
        with col1:
            out["region"] = st.text_input("区域 x,y,w,h", placeholder="例如 200,100,880,700", key=f"reg_{tab_id}")
        with col2:
            st.markdown("""
            **快速参考：**
            - 仅中间内容区：`200,80,880,800`
            - 仅标题+封面：`200,80,880,400`
            - 去掉顶部导航：`0,60,1280,840`
            """)
    return out


def parse_region(s):
    if not s:
        return None
    return tuple(int(x) for x in s.split(","))


def validate_mode_params(mode: str, p: dict) -> str | None:
    """Return an error message if the chosen mode is missing required params, else None."""
    # NOTE: `to_keyword` does NOT require a keyword — if left empty it falls back
    # to capturing the whole note content (see CaptureEngine.capture).
    if mode == "keyword":
        kw = (p.get("keyword") or "").strip()
        if not kw:
            return ("「keyword」模式需要先填写「关键词」才能截图（它只截关键词那一块）。\n\n"
                    "👉 如果你是想截整篇笔记，请把「截取方式」改成「to_keyword」或「note」，"
                    "这两个模式不填关键词也能直接截整篇。")
    if mode == "element":
        sel = (p.get("selector") or "").strip()
        if not sel:
            return "「element」模式需要先填写「CSS 选择器」才能截图。"
    if mode == "region":
        reg = (p.get("region") or "").strip()
        if not reg:
            return "「region」模式需要先填写「区域 x,y,w,h」才能截图。"
    return None


def main():
    # Auto-cleanup old files (runs silently each page load)
    deleted = cleanup_old_history()

    st.title("📸 小红书笔记截图工具")
    st.markdown(
        "给一个笔记链接，或上传一个含小红书链接列的 Excel，自动截取笔记页面并生成截图"
        "（可插入回 Excel 指定列）。"
    )

    with st.sidebar:
        # ---- cookie config (most reliable auth) ----
        st.header("🍪 Cookie 配置")
        cstat = cookie_status()
        st.caption(f"当前状态：{cstat}")
        with st.expander("粘贴 Cookie（推荐，最稳）", expanded=(cstat == "未配置")):
            st.markdown("""
**获取方法：**
1. 用 Chrome 打开 [小红书](https://www.xiaohongshu.com) 并确保已登录
2. 按 `F12` → `Application` → 左侧 `Cookies` → `https://www.xiaohongshu.com`
3. 全选复制（或用插件 **EditThisCookie** / **Cookie-Editor** 导出）
4. 粘贴到下方文本框
            """)
            raw = st.text_area(
                "Cookie 文本",
                height=140,
                placeholder='粘贴这里… 支持两种格式：\n'
                           '① name=value; name2=value2; ...\n'
                           '② [{"name":"a1","value":"xx","domain":".xiaohongshu.com",...}, ...]',
            )
            col_save, col_clear = st.columns(2)
            with col_save:
                if st.button("💾 保存 Cookie", use_container_width=True, disabled=not raw.strip()):
                    try:
                        n = save_cookies(raw)
                        st.success(f"已保存 {n} 条 Cookie ✅\n刷新页面后生效。")
                        st.rerun()
                    except CaptureError as ex:
                        st.error(f"保存失败：{ex}")
            with col_clear:
                if st.button("🗑️ 清除 Cookie", use_container_width=True):
                    clear_cookies()
                    st.rerun()

        st.divider()

        # ---- browser profile login (alternative) ----
        st.header("🔓 浏览器登录")
        st.caption(f"Profile 保存在：{PROFILE}")
        if st.button("打开浏览器登录小红书", use_container_width=True):
            launch_login()
            st.success("已弹出浏览器，请登录（含验证）后关闭窗口。")
        st.divider()

        # ---- view settings ----
        st.header("⚙️ 视图设置")
        mobile = st.checkbox("手机视图（竖屏，更接近 App 截图）", value=False)
        humanize = st.checkbox("🤖 拟人浏览（先逛首页+随机滚动鼠标，防检测）", value=True)
        show_stats = st.checkbox("📊 截图包含互动数据（点赞/收藏/评论/分享）", value=True)
        st.divider()
        st.caption("💡 提示：Cookie 方式比浏览器 Profile 更稳；开启拟人浏览可进一步降低被判定为机器人的概率。")

    tab1, tab2 = st.tabs(["单条链接", "Excel 批量"])

    # ---------------- single ----------------
    with tab1:
        url = st.text_input("笔记链接", placeholder="https://www.xiaohongshu.com/explore/...")
        mode = st.selectbox("截取方式", list(MODE_HELP.keys()),
                            index=list(MODE_HELP.keys()).index("note"),
                            format_func=lambda m: f"{m} — {MODE_HELP[m]}")
        p = mode_params(mode, tab_id="single")
        if st.button("📸 截图", use_container_width=True) and url.strip():
            kw_err = validate_mode_params(mode, p)
            if kw_err:
                st.error(kw_err)
            else:
                with st.spinner("正在打开浏览器截图…"):
                    out_path = OUTPUT_DIR / "single.png"
                try:
                    saved = load_cookies()
                    with CaptureEngine(PROFILE, headless=True, mobile=mobile, cookies=saved, humanize=humanize) as e:
                        e.capture(
                            url.strip(), mode,
                            selector=p.get("selector"), keyword=p.get("keyword"),
                            region=parse_region(p.get("region")), out_path=out_path,
                            show_stats=show_stats,
                        )
                    st.image(str(out_path), caption="截图结果", use_container_width=True)
                    # Save a timestamped copy to history
                    import shutil
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    hist_name = f"single_{ts}.png"
                    try:
                        shutil.copy2(out_path, HISTORY_DIR / hist_name)
                    except Exception:
                        pass
                    st.download_button("⬇️ 下载 PNG", data=out_path.read_bytes(),
                                       file_name="note.png", mime="image/png")
                    st.caption(f"📁 文件路径：{out_path}")
                except LoginWallError as ex:
                    st.error(f"遇到登录墙：{ex}\n请先在左侧配置 Cookie 或点「打开浏览器登录小红书」。")
                except CaptureError as ex:
                    st.error(f"截图失败：{ex}")
                except Exception as ex:  # noqa: BLE001
                    st.error(f"出错了：{ex}")

    # ---------------- excel ----------------
    with tab2:
        up = st.file_uploader("上传 Excel（.xlsx）", type=["xlsx"])
        if up:
            tmp = OUTPUT_DIR / "_upload.xlsx"
            tmp.write_bytes(up.getvalue())
            sheets = list_sheets(tmp)
            sheet = st.selectbox("工作表", sheets, index=0) if len(sheets) > 1 else (sheets[0] if sheets else None)
            cols = preview_columns(tmp, sheet=sheet)
            col_opts = {f"{c['letter']}  （示例：{c['sample'] or '—'}）": c["letter"] for c in cols}
            st.markdown("**选择链接所在列：**")
            link_col = st.selectbox("链接列", list(col_opts.keys()), label_visibility="collapsed")
            link_letter = col_opts[link_col]

            st.markdown("**截图写入列：**")
            out_choice = st.radio("写入方式", ["新建一列", "选择已有列"], horizontal=True)
            if out_choice == "新建一列":
                out_letter = get_column_letter(len(cols) + 1)
                st.caption(f"将新建列：{out_letter}")
            else:
                oc = st.selectbox("输出列", list(col_opts.keys()), key="oc")
                out_letter = col_opts[oc]

            header_rows = st.number_input("表头行数（跳过前几行）", min_value=0, max_value=10, value=1)

            if st.button("🔍 扫描所有工作表，找出含链接的列", use_container_width=True):
                with st.spinner("正在全表扫描…"):
                    try:
                        found = find_link_columns(tmp, None, int(header_rows), all_sheets=True)
                    except Exception as ex:  # noqa: BLE001
                        found = []
                        st.warning(f"扫描出错：{ex}")
                if found:
                    st.success("找到以下含链接的位置，请把上方「链接列」改成对应的列：")
                    for c in found[:12]:
                        st.markdown(f"- **{c['sheet']}!{c['letter']}** — {c['count']} 条，示例：`{c['sample']}`")
                else:
                    st.warning("全表（含所有工作表、深扫约 300 行）未发现任何链接形态的单元格。"
                               "可能：①链接在更靠下的行；②链接是图片/二维码而非文本网址；"
                               "③文件由程序生成且公式结果未被 Excel 计算过（请先用 Excel 打开、重算后另存）。")

            mode = st.selectbox("截取方式", list(MODE_HELP.keys()), key="m2",
                                index=list(MODE_HELP.keys()).index("note"),
                                format_func=lambda m: f"{m} — {MODE_HELP[m]}")
            p = mode_params(mode, tab_id="batch")

            # ---- 高级：防封 / 限速（上百条建议保持默认） ----
            with st.expander("⚙️ 性能 / 防封设置（上百条建议保持默认）", expanded=False):
                st.caption("小红书对同一 IP 高频访问会限流/风控。以下参数用于自动降速与分批，"
                           "避免一次性长跑被拦截。")
                min_interval = st.slider("每条最小间隔(秒)", min_value=0.5, max_value=10.0,
                                          value=2.0, step=0.5,
                                          help="成功时相邻两条之间的间隔下界；被限流会自动拉长。")
                batch_size = st.number_input("分批大小(条/批, 0=不分批)", min_value=0, max_value=200,
                                             value=25, step=5,
                                             help="每处理这么多条就保存一次进度并冷却，防止中途崩溃丢结果。")
                batch_pause = st.slider("批次间冷却(秒)", min_value=0, max_value=120,
                                        value=30, step=5,
                                        help="每批之间暂停多久让 IP 降温。")

            if st.button("🚀 开始批量截图", use_container_width=True):
                kw_err = validate_mode_params(mode, p)
                if kw_err:
                    st.error(kw_err)
                else:
                    progress = st.progress(0, text="准备中…")
                log_box = st.empty()
                logs = []

                def on_progress(i, total, row, url, status, msg):
                    progress.progress(i / total, text=f"{i}/{total}  row {row} → {status}")
                    logs.append(f"{i}/{total} row {row}: {status}  {url}")
                    log_box.code("\n".join(logs[-12:]))

                try:
                    saved = load_cookies()
                    with CaptureEngine(PROFILE, headless=True, mobile=mobile, cookies=saved, humanize=humanize) as e:
                        out_xlsx = run_excel(
                            tmp, e, link_letter, out_letter,
                            header_rows=int(header_rows), sheet=sheet, mode=mode,
                            selector=p.get("selector"), keyword=p.get("keyword"),
                            region=parse_region(p.get("region")),
                            out_dir=OUTPUT_DIR, show_stats=show_stats,
                            on_progress=on_progress, log=logs.append,
                            min_interval=min_interval, batch_size=int(batch_size),
                            batch_pause=float(batch_pause),
                        )
                    progress.progress(1.0, text="完成 ✅")
                    # Also save a copy to history dir for persistence
                    import shutil
                    hist_copy = HISTORY_DIR / out_xlsx.name
                    try:
                        shutil.copy2(out_xlsx, hist_copy)
                    except Exception:
                        pass
                    st.success(f"已生成：{out_xlsx}")
                    st.download_button("⬇️ 下载带截图的 Excel", data=Path(out_xlsx).read_bytes(),
                                       file_name=Path(out_xlsx).name,
                                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    st.caption(f"📁 文件路径：{out_xlsx}")
                    # preview a few shots
                    shots = sorted(OUTPUT_DIR.glob("row*_*.png"))
                    if shots:
                        st.markdown("**预览（前 6 张）：**")
                        for s in shots[:6]:
                            st.image(str(s), width=260, caption=s.name)
                except LoginWallError as ex:
                    st.error(f"遇到登录墙：{ex}")
                except CaptureError as ex:
                    st.error(f"失败：{ex}")
                except Exception as ex:  # noqa: BLE001
                    st.error(f"出错了：{ex}")

    # ------------------ history section ------------------
    st.divider()
    with st.expander("📂 历史记录（自动保留，3 个月前自动清理）", expanded=False):
        items = get_history_items(limit=60)
        if not items:
            st.caption("暂无历史记录。截图或批量操作后会在此显示。")
        else:
            # Stats
            n_img = sum(1 for it in items if it["is_image"])
            n_xlsx = sum(1 for it in items if it["is_excel"])
            st.caption(f"共 {len(items)} 个文件（{n_img} 张截图 + {n_xlsx} 个 Excel）"
                       f" · 超过 90 天的文件会自动清理")
            # Grid of images, list of files
            col_img, col_file = st.columns([3, 2])
            with col_img:
                img_items = [it for it in items if it["is_image"]][:12]
                if img_items:
                    # Display in a 4-column grid
                    for row_start in range(0, len(img_items), 4):
                        cols = st.columns(4)
                        batch = img_items[row_start : row_start + 4]
                        for col_idx, sub_it in enumerate(batch):
                            try:
                                cols[col_idx].image(str(sub_it["path"]), width=180,
                                                    caption=f"{sub_it['mtime_str']}\n{sub_it['name'][:30]}")
                            except Exception:
                                pass
            with col_file:
                xlsx_items = [it for it in items if it["is_excel"]]
                if xlsx_items:
                    st.markdown("**Excel 文件：**")
                    for it in xlsx_items[:10]:
                        try:
                            data = Path(it["path"]).read_bytes()
                            st.download_button(
                                f"📊 {it['name']}  ({it['mtime_str']}, {it['size']//1024}KB)",
                                data=data,
                                file_name=it["name"],
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                key=f"hist_{it['name']}",
                            )
                        except Exception:
                            st.markdown(f"~ {it['name']} ({it['mtime_str']}) — 文件不可读")


if __name__ == "__main__":
    main()
