# dsh Progress Viz 常见问题（FAQ）

> 看板纯本地运行（不访问网络、不烧 token）。遇到问题时先看这里；如果问题
> 仍未解决，可结合 `dashboard.py` / `session_progress.py` 顶部的注释定位。

## 1. 报错 `ModuleNotFoundError: No module named 'zstandard'` 怎么办？

看板依赖 zstandard 做会话文件解压，未安装会直接启动失败。两种安装方式任选：

```bash
# 方式一：只装依赖
pip install "zstandard>=0.21"

# 方式二：按项目安装（pyproject.toml 已声明该依赖，会自动带上）
pip install -e .
```

安装完成后重新运行 `python dashboard.py` 即可。

## 2. 看不到任何任务，`~/.dsh/sessions` 目录不存在？

看板固定读取 `~/.dsh/sessions`（即 `session_progress.SESSIONS_ROOT`）。出现
「等待 dsh 任务开始...」通常是因为：

- **还没运行过 dsh（headless 模式）**：`~/.dsh/sessions` 由 dsh 首次执行任务时
  自动创建。先启动一个 dsh headless 任务，目录出现后看板下一次轮询（≤4s）就能看到。
- **DSH_HOME 自定义了数据目录**：dsh 的会话不在默认的 `~/.dsh/sessions` 下时，
  看板扫描不到。可将实际会话目录符号链接到 `~/.dsh/sessions`：
  ```bash
  # Linux/macOS 示例（把 DSH_HOME 指向的 sessions 目录链到默认位置）
  ln -s "$DSH_HOME/sessions" ~/.dsh/sessions
  ```
- **任务不在 1 小时窗口内**：看板只展示**最近 1 小时内有写入**（文件 mtime
  距今 < 3600s）的任务，更早的任务不会显示。

## 3. 端口被占用会怎样？如何指定端口？

启动时默认尝试 8123；若端口被占用，看板会**自动 +1 递增重试（最多 5 次）**，
并打印「端口 X 被占用，改用 Y」，不会启动失败。只有连续 5 个端口全部被占用
才报错退出。

```bash
python dashboard.py 8124 --no-open   # 指定从 8124 开始探测
```

实际监听端口会在启动日志中打印：`dsh progress viz: http://127.0.0.1:<端口>`。

## 4. Linux/macOS 上看到 Windows 风格路径（反斜杠）怎么办？

动作摘要里的文件路径显示问题**已修复**：`session_progress.format_action` 会把
`read`/`write` 工具参数中的反斜杠统一替换为正斜杠再显示；会话目录编码
（`encode_cwd`）也对两种分隔符做了归一化，目录匹配不受影响。

如果任务卡片上仍显示 `C:\...` 这类路径，那是**会话事件本身**的 `cwd` 字段就是
Windows 路径（例如任务在兼容层/远程环境下产生），属于数据源内容，看板会原样
展示而不改写。

## 5. 看板显示「步骤N」而不是任务名？

「步骤N」是**兜底显示**：阶段名优先来自模型用 todo 工具写入的任务清单
（`todo/write` 事件），取第一个未完成项作为当前阶段。如果任务事件流里**没有任何
`todo/write`**，看板只能回退到 `step/start` 计数，显示「步骤N」。

解决办法：在任务提示里引导模型使用 todo 工具维护清单，例如：

```
【输出约定】请使用 todo 工具维护你的任务清单：每个主要步骤列一项
（开始标记 in_progress、完成标记 completed，清单变化时更新）。
```

模型按要求写清单后，看板即可显示任务名与阶段进度条。

## 6. 任务已经完成，卡片为什么还在显示？

看板展示**最近 1 小时窗口**（`RECENT_WINDOW = 3600s`）内有写入的任务。任务完成后
其会话文件不再写入，但 mtime 距今仍不足 1 小时，所以卡片会继续显示（状态变为灰色
「已完成」），**超过 1 小时后自动消失**——这是有意设计，方便完成任务后回看结果。
若想立刻确认任务已结束，看状态点：绿色「正在运行」（mtime 距今 ≤ 30s）→ 灰色
「已完成」。

## 7. 如何停止/卸载看板？

- **停止**：在运行看板的终端按 `Ctrl+C` 即可退出（看板是前台进程，无后台驻留）。
- **卸载**：如果之前执行过 `pip install -e .`，先 `pip uninstall dsh-progress-viz`；
  然后删除项目源码目录即可。看板不写注册表、不设开机自启、不产生全局状态，
  删除目录后即完全清除。
