# 📸 小红书笔记截图工具（XHS Shots）

给一个小红书笔记链接，或上传一个含「小红书链接」列的 Excel，自动打开浏览器截取笔记页面，
生成截图（可插入回 Excel 指定列）。

## 功能
- **单条链接**：粘贴笔记链接 → 一键截图预览 / 下载 PNG。
- **Excel 批量**：上传 .xlsx → 选「链接列」和「截图写入列」→ 批量截图并嵌回 Excel。
- **四种截取方式（手动指定）**：
  - `full` 整页长截图（最常用）
  - `viewport` 仅当前可见区域
  - `element` 按 CSS 选择器截取某元素（你填选择器，如 `.note-content`）
  - `keyword` 在页面里找到「包含某关键词」的内容块并截取（你填关键词）
  - `region` 按固定像素区域 `x,y,w,h` 截取
- **登录复用**：首次用「打开浏览器登录」按钮登录一次，登录态保存在本地 `profile/`，
  之后免登录截图（沿用 browser-use 的思路：复用真实已登录浏览器档案）。

## 运行
```bash
cd xhs_shots
./run.sh                # 或：python -m streamlit run app.py --server.port 8501
```
浏览器打开 http://localhost:8501

> 依赖已装在隔离 venv：`/Users/zhaoxixi/.workbuddy/binaries/python/envs/xhs/bin/python`
> 自行部署时：`pip install -r requirements.txt` 然后 `playwright install chromium`

## 使用步骤
1. 左侧点 **🔓 打开浏览器登录小红书** → 弹出的真实浏览器里登录（含滑块/验证码）→ 关闭窗口。
   登录态会保存到 `xhs_shots/profile/`，后续运行自动复用。
2. 切到「单条链接」或「Excel 批量」标签页。
3. 选截取方式并填写参数 → 开始。

## 命令行（自动化 / 调试）
```bash
python cli.py login                       # 打开浏览器手动登录
python cli.py single --url "<链接>" --mode full --out shot.png
python cli.py excel --file notes.xlsx --link-col A --out-col C --mode full
```

## 注意事项
- 小红书对未登录访问几乎都会弹登录墙 / 验证；**务必先登录一次**。
- 个别笔记触发风控验证码时，再次点「打开浏览器登录」手动过一次即可，状态会保留。
- `element` 模式需要的 CSS 选择器可在浏览器「检查」里复制；XHS 页面结构可能调整，选择器以实际为准。
- 截图图片以缩略图（默认宽 320px）嵌入 Excel，原图同时保存在 `output/` 目录。
- 频繁批量请求请适度（工具内部已加 ~0.3s 间隔），避免触发风控。

## 文件结构
```
xhs_shots/
  app.py        # Streamlit GUI
  cli.py        # 命令行入口
  capture.py    # Playwright 截图引擎（含登录态复用、登录墙检测）
  excelio.py    # Excel 读写 + 图片嵌入
  profile/      # 登录态（自动生成）
  output/       # 截图与带截图的 Excel 输出
  requirements.txt
  run.sh
```
