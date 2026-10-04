---
name: silence-computer-use
description: 在独立的 Windows 虚拟桌面/影子会话中使用 Kimi CU 操作 GUI，不抢占用户的前台操作。用于需要截图、UIA 读屏、点击、输入、快捷键、滚动或拖拽的桌面任务，或用户明确指定在虚拟桌面中使用 Kimi CU 时。
---

# Silence Computer Use

为 agent 提供不抢占用户前台操作的 computer-use 环境。由 `vdcu` 管理独立桌面/影子会话，Kimi CU agent 在该会话内执行 GUI 操作，Python 桥通过独立命名管道调用它。

真实键鼠输入由会话内的 agent 执行，不向用户正在使用的桌面发送。虚拟桌面与本机仍共享文件系统，剪贴板也可能共享；这不是虚拟机或安全沙箱。Windows 确认当前本地账户为空密码时直接进入；其他账户或无法确认时，授权框可能出现在用户桌面。

## 运行条件

- Windows 和 Python 3.10+；控制脚本使用 Python 标准库。
- 保留完整技能目录，包括 `scripts/`、`runtime/cu.exe` 和 `runtime/vdcu/`。
- 使用本技能内的脚本和运行时，不依赖宿主专用 API。运行前用 `doctor` 检查依赖；缺失时按其提示修复。

以下命令以技能目录为工作目录；从其他位置调用时使用脚本的绝对路径。

## 启动与退出

```powershell
python scripts/vd_cu.py doctor
python scripts/vd_cu.py up
python scripts/vd_cu.py status
python scripts/vd_cu.py call list_windows
python scripts/vd_cu.py down
```

启动前检查状态，已有可用会话时复用，不重复创建。没有会话时，`up` 会启动影子会话并在会话内启动 Kimi CU agent。

启动前告诉用户：空密码本地账户经 Windows 验证后自动进入；有密码或无法确认时可能出现 Windows 授权框，请在窗口内完成授权，不要把密码发送到聊天。取消、超时或启动失败时停止并报告，不循环弹框。不修改账户密码或 Windows 安全策略；检测逻辑见 [引擎说明](runtime/vdcu/docs/ENGINE-NOTES.md)。会话和 agent 健康状态均正常后再开始任务。

结束任务调用 `down`。异常时先检查残留：

```powershell
python runtime/vdcu/run_cli.py cleanup --dry-run
```

只处理能确认属于本次任务的残留，不终止用户无关的应用或第三方助手进程。

## 观察、定位与操作

1. **先观察窗口**：`list_windows` 返回 `{app, id, title}`，后续调用使用返回的原始 `window` 对象。窗口失效或重建时重新获取。
2. **UIA 优先**：调用 `get_window_state` 时显式指定 `include_text:true`、`include_screenshot:false`。`accessibility.tree` 是字符串，其中的 `[N]` 对应 `element_index`；优先按索引定位控件。
3. **必要时使用截图**：UIA 信息缺失或不可靠时获取截图并读图，使用该帧的 `screenshotId` 与像素坐标操作。这里的坐标不是 0–1000 归一化坐标。
4. **先确定输入目标**：`type_text` 向当前焦点粘贴文本；先点击目标输入框，必要时检查焦点。文本可能留在共享剪贴板中，不粘贴秘密或覆盖无关剪贴板内容。
5. **按状态变化重新观察**：相关动作可顺序执行；弹窗、导航、刷新或结果不确定时重新获取状态。不要复用已失效的控件索引或截图。
6. **验证结果**：输入调用完成只代表已派发，不代表应用已完成动作。优先用 UIA 文本验证；需要视觉确认时截图。保存文件后核对本机文件，重新打开测试必须实际打开磁盘文件。

按键示例：`Enter`、`Tab`、`Control_L+a`、`Shift_L+Tab`、`F5`、`Left`、`Right`、`Up`、`Down`。`+` 分隔组合键，加号本身使用 `Shift_L+equal`。滚动参数 `scrollY > 0` 表示向下。

## 调用入口

单次方法调用使用 `call`；参数字段和返回结构查阅 [API 参考](references/api.md)。不要混用其他 computer-use 工具的参数或坐标约定。

```powershell
python scripts/vd_cu.py call list_windows
python scripts/vd_cu.py tree 计算器 按钮
```

`tree` 按窗口 id 或标题获取 UIA 树，可用关键词过滤，便于定位 `element_index`。

批量调用使用 `serve`，通过 stdin 输入 NDJSON，每行一个请求、一个响应：

```json
{"id":1,"method":"list_windows"}
{"id":2,"method":"quit"}
```

```powershell
Get-Content -LiteralPath ops.jsonl -Encoding utf8 | python scripts/vd_cu.py serve
```

请求可带 `arguments` 和 `tree_filter`。截图保存到 `cu_shots/`，通过响应的 `screenshots[].path` 读取图像。长 UIA 树保存到 `cu_trees/`；响应提供预览和完整文本路径，过滤不删除落盘的完整树。

命名管道协议及逐方法契约见 [API 参考](references/api.md)；桥接实现位于 `scripts/cu_bridge.py`。仅在需要排查或扩展协议时阅读实现。

## 会话层辅助操作

Kimi CU 负责窗口观察与交互，`runtime/vdcu/run_cli.py` 用于会话管理和辅助操作：

- 应用执行别名导致 `launch_app` 失败时，可改用会话引擎启动：`python runtime/vdcu/run_cli.py launch "记事本"`。启动请求接受后仍需等待并检查 `list_windows`。
- Kimi CU 截取单个窗口；需要全桌面视图时使用 `python runtime/vdcu/run_cli.py capture --shot look.jpg`，并读取实际截图。

## 操作边界

- 默认只连接虚拟会话管道 `\\.\pipe\kimi-cu-win-vd`。不要连接用户会话或其他宿主拥有的 Kimi CU 管道；自定义管道也必须属于本任务的虚拟会话。
- 影子会话同时只能有一个；已有会话时复用，不重复 `up` 或直接 `enter`。
- `KimiCU Overlay` 和 `KimiCU Display Overlay` 是 agent 自身的悬浮层，不点击或操作它们。
- 不为任务切换或接管用户桌面。发现操作目标位于用户会话时停止，不继续发送输入。
- 成果保存到明确的本机绝对路径，避免覆盖已有文件；退出会话不会删除已保存成果。
- 不修改签名运行组件或绕过认证、安全策略。报告实际完成情况，不把接口返回成功当作 GUI 任务完成证明。
