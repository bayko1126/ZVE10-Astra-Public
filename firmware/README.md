# `firmware/` 本地固件资产占位目录（Git 已忽略）

本目录用于存放您从**自有相机备份或官方固件更新包**提取的基线文件，**切勿将任何索尼专有二进制文件提交到 Git 仓库**。

## 1. 需自行准备的原始输入文件

| 文件 / 目录 | 说明 |
| :--- | :--- |
| `firmware/backup/nflasha15.bin` | ZV-E10 原厂 `/usr` ext2 分区镜像（314,572,800 B / 300 MB，救砖基线与四代文件提取源） |
| `firmware/backup/nflasha5.bin` | ZV-E10 原厂 `nflasha5` 分区备份（62,914,560 B，含 `0x2C000` 原厂 `WBI1` 暖启动头） |
| Gen5（ZV-1M2）解包目录 | 包含五代 `lib/appFw.so`、`lib/appFwBoot.so`、`lib/libObj.so`、`lib/libmpr.so`、`lib/AdjChkConfig.so` 及 `share/app/` |

## 2. 一键提取与整理

将上述文件准备好后，在仓库根目录运行：

```powershell
uv run python tools/build/prepare_inputs.py `
  --gen4-image firmware/backup/nflasha15.bin `
  --gen5-root <你的_ZV1M2_解包根目录>
```

脚本会自动从 `nflasha15.bin` 中提取四代所需宿主/底层库至 `firmware/gen4/`，并将五代引擎与 UI 资源整理至 `firmware/gen5/`。详细步骤见 [docs/firmware-extraction.md](../docs/firmware-extraction.md)。
