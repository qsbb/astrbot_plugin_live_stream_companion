# FORK 说明（qsbb 维护）

本仓库是 [`menglimi/astrbot_plugin_live_stream_companion`](https://github.com/menglimi/astrbot_plugin_live_stream_companion) 的 **fork**，不是官方版本。
上游作者与版权信息完整保留：`LICENSE`（xfgryujk 2018 / Raven95676 2025）、`metadata.yaml` 的 `author: menglimi` 均未改动，仅把 `repo:` 指向本 fork，便于 AstrBot 从这里取更新。

| 项 | 值 |
|---|---|
| 上游 | https://github.com/menglimi/astrbot_plugin_live_stream_companion |
| 本 fork | https://github.com/qsbb/astrbot_plugin_live_stream_companion |
| 维护者 | qsbb |
| fork 目的 | 直播链路与外接 TTS 服务之间的参数透传（情绪/语气）与本地化使用需求 |
| 许可证 | MIT（随上游） |

## 一、相对上游的差异台账

`git rev-list --left-right --count upstream/main...HEAD` → `0	10`（上游无本 fork 缺失的提交；本 fork 领先 10 个提交，其中 2 个是本次变更）。

| 提交 | 内容 | 开关 / 默认值 | 回退行为 |
|---|---|---|---|
| `dffa4c5` | 支持注册式外部直播 TTS 服务（按工具名反查所属插件） | `live_tts_backend=registered_service/auto`，默认 `astrbot_provider` | 找不到服务即回退，`registered_service` 退回纯文字 |
| `56fe958` | 管理页暴露直播 TTS 设置 | 无 | — |
| `f54c426` | 下拉选择外部 TTS 服务与远程 VTS 目标 | 无 | 手填仍可用 |
| `4432b67` | 统一 VTS / OBS 入口（连接分组、锚点、OBS 分组） | 无 | 旧配置继续可用 |
| `7e31c5b` `4a5200e` | 外部 TTS 方法改按关键词动态枚举（不再写死方法名） | 无 | 枚举失败时手填 |
| `0aa1d60` | 拓展页「语音诊断与试听」（不开播跑真实合成链路） | 无 | — |
| **本次** | **情绪/语气透传给外部 TTS**（Soullink 意图 → `emotion` / `context`，按签名裁剪） | `live_tts_emotion_passthrough_enabled`（默认**开**）、`live_tts_style_hint_enabled`（默认**关**） | 对方签名不接受即不传；异常仍走既有回退链 |
| **本次** | `scripts/audit_fork.py` 发布前自检（私货关键词 / 环境泄漏 / 溯源 / 版本一致性） | 手动运行 | — |

设计约束（每条都可核对）：

1. **零私有耦合**：代码里不出现任何具体插件名、系列名或私有契约名——外部服务只按「方法签名」被适配；
2. **不改上游默认**：两个新开关关闭时，发送给外部服务的参数与旧版本逐字一致；
3. **失败可回退**：外部服务报错/超时依旧回退 AstrBot TTS 或纯文字，日志保留原因；
4. **可自证**：拓展页「语音诊断与试听」回显本次实际使用的情绪与语气提示。

## 二、不夹带私货的纪律

发布前跑：

```bash
python scripts/audit_fork.py          # 0 = 干净；非 0 列出问题
git diff upstream/main --stat         # 只应涉及预期文件
```

自检覆盖：私货关键词（系列名/私有插件 id/私有契约名）、环境泄漏（个人路径、内网主机、口令痕迹）、溯源（LICENSE 署名 + metadata 作者 + README fork 声明）、版本一致性（metadata / README / CHANGELOG）。

改代码时的三条硬规矩：

- 新能力一律**通用化**（按签名/能力探测适配），不写死任何具体插件；
- 新功能一律**可关闭**且默认值不改变老行为；
- 一条提交只做一件事，提交信息按 `feat/fix/chore` + 中文描述（fork 变更在 CHANGELOG 里单列）。

## 三、如何跟进上游

```bash
git remote add upstream https://github.com/menglimi/astrbot_plugin_live_stream_companion   # 已配置
git fetch upstream
git log --oneline HEAD..upstream/main     # 上游新增
git rebase upstream/main                  # 保持线性历史
```

通用改进（例如本次的情绪透传）可以整理成 PR 提给上游；上游合并后本地补丁即可删除。

## 四、测试

| 范围 | 命令 | 说明 |
|---|---|---|
| 本地可跑（不依赖 AstrBot） | `cd tests && PYTHONPATH=.. pytest -q test_live_tts_params.py test_live_tts_wiring.py test_fork_audit.py test_config_options.py test_audio_diagnose.py` | 54 passed / 1 skipped（纯函数 + 接线静态断言 + fork 自检） |
| AstrBot 环境（容器内） | `pytest -q tests/` | `tests/test_pr1_regressions.py` 等需要 `data.plugins…` 与 AstrBot 运行时 |

> 注意：仓库根目录带 `__init__.py`，在**没有 AstrBot 的机器上**从仓库根跑 pytest 会因导入包而失败；
> 本地请进 `tests/` 目录（`cd tests && PYTHONPATH=.. pytest …`）或直接 `python -m unittest`。

## 五、已知格式差异

`CHANGELOG.md`、`README.md`、`vts_discovery.py` 在本 fork 里是 **LF**，上游是 **CRLF**，因此与上游对比时会出现整文件差异（内容差异分别只有 8 / 27 / 78 行）。
功能无影响；**若要向上游提 PR，先统一行尾**再对比。

另外 `blivedm/__pycache__/` 下有两个**上游提交进来的 `.pyc`**（cpython-312 / 313），本 fork 不动它们，避免无谓差异；本地跑测试可能重写其中的 cpython-313 文件，提交前用 `git checkout -- blivedm/__pycache__` 还原即可。
