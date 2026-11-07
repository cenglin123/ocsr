# 市场素材

这里存放 dsh 插件市场条目引用的图标与截图。

## 图标

`icon-180.png` 由 [OpenCode](https://github.com/sst/opencode) 的 logo
（`packages/ui/src/assets/favicon/web-app-manifest-512x512.png`，MIT 许可）派生：
重新配色，并把内部矩形拆成三根竖条，表示并行 worker。它仅用于 OCSR 的市场卡片，
与 OpenCode 项目无从属或背书关系。

## 截图

- `screenshot-1-dispatch-flow.png`：DSH agent → `ocsr_dispatch` → Python driver → 并行
  `opencode run` worker → 产物文件的派发链路。
- `screenshot-2-model-gate.png`：模型门语义——只与 driver 的
  `~/.ocsr/allowed-models.json` 取交集，只收窄不放宽。
- `screenshot-3-composition.png`：`dsh --dump-config` 中本 bundle 的两个 entry
  （`ocsr-skill`、`ocsr-tools`）与安装命令。

截图是架构示意与命令输出，不是伪造的 GUI 画面。
