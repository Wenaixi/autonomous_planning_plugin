# 自主规划插件 v4（xuqian13.autonomous-planning-plugin-v4）

源: GitHub `xuqian13`

## 主人改过的关键文件

- `_manifest.json`：`max_version` 改成 `1.99.99`（修 Host 1.1 不兼容）。
- `config.toml`：`config_version = "4.4.4"`（plugin runtime 校验需要）。

## 配置要点

- 已加载并生成 12 个日程。
- 上游 .gitignore 已精细化：`cache/` 不被整目录忽略（其内含 Python 源码如 `lru_cache.py`），仅 `cache/__pycache__/` 忽略。主人段已覆盖。

## 接入 & 依赖

- 对外 API：`get_current_activity`（被 MaiTrace 等其它插件调用获取当前活动）。

## 关键日志

- `自主规划插件 v4 已加载`
- `已生成 12 日程`

## 历史脚注

- 2026-07-28: 从服务器同步到本地仓库（baseline head=`04158e6`）；补充 `.gitignore` 主人段与本 CLAUDE.md。
