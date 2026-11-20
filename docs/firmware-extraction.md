# 固件基线提取与准备指南 (`docs/firmware-extraction.md`)

出于版权合规要求，本仓库**不分发**任何索尼专有固件、镜像或资源文件。请按照本指南从你**合法持有的设备或官方固件包**中提取输入，并放到 Git 忽略目录 `firmware/` 中。

---

## 1. 需要准备的原始资产

| 输入项 | 获取方式 | 推荐存放位置 |
| :--- | :--- | :--- |
| **ZV-E10 官方固件 2.03 更新包** | 从 [Sony 官方下载页面](https://www.sony.com/electronics/support/e-mount-body-zv-e-series/zv-e10/downloads) 下载 `Update_ZVE10V203.dmg` | 仓库根目录或任意本地解包路径 |
| **ZV-E10 出厂 `/usr` 分区备份 (`nflasha15.bin`)** | 通过真机 Fastboot + `stage2.bin` 读出自己设备的 `/dev/nflasha15`（314,572,800 B），用于构建基线与救砖兜底 | `firmware/backup/nflasha15.bin` |
| **ZV-E10 出厂 `WBI1` 引导分区 (`nflasha5.bin`)** | 读出自己设备的 `/dev/nflasha5`（62,914,560 B，含 `0x2C000` 原厂 `WBI1` 签名），用于红灯不亮应急恢复 | `firmware/backup/nflasha5.bin` |
| **ZV-1M2 (Gen 5 ASTRA) 解包目录** | 从你合法持有的 ZV-1M2 官方固件解包提取，包含 `lib/appFw.so`、`lib/appFwBoot.so`、`lib/libObj.so`、`lib/libmpr.so`、`lib/AdjChkConfig.so` 及 `share/app/` | 本地任意解包目录（如 `D:\fw\zv1m2_unpacked`） |

> [!TIP]
> 关于 Sony 固件更新包（`.dmg` / `.dat`）的解包工具与脚本，可参考前置开源仓库：
> - [DavidBuchanan314/ILCE-7M4-RE](https://github.com/DavidBuchanan314/ILCE-7M4-RE)
> - [erlla/sony-a7m4-custom-firmware](https://github.com/erlla/sony-a7m4-custom-firmware)

---

## 2. 一键提取基线到 `firmware/`

准备好上述两份基线后，运行内置提取脚本 `tools/build/prepare_inputs.py`（纯 Python 读取 ext2 镜像，无需 WSL root 权限）：

```powershell
uv run python tools/build/prepare_inputs.py `
  --gen4-image firmware/backup/nflasha15.bin `
  --gen5-root <你的_ZV1M2_解包根目录> `
  [--gen4-fw-imdb <可选: 从 Update_ZVE10V203.dmg 解包出来的 37,204 B 版 libIMDB.so>]
```

### 脚本会自动完成以下工作：
1. 检查 `nflasha15.bin` 文件大小（`314,572,800` B）与出厂基线 SHA-256（参考：`cb15bc410800add6ad605a3433773c7a287703140c85b1b2939192976240bc60`）。
2. 从 `nflasha15.bin` 内部精准提取 Gen4 宿主与底盘文件至 `firmware/gen4/`：
   - `bin/im.elf`、`bin/bootin.elf`、`bin/sen.elf`
   - `lib/libObj.so`、`lib/libMWF.so`、`lib/libInfraSKD.so`、`lib/libInfraLensCommunicator.so`、`lib/libmpr.so`、`lib/libmprctrl.so`
   - `lib/libIMDB_dump_37044.so`（或按实际提取大小命名）
3. 从 `--gen5-root` 整理五代核心引擎与 UI 资产至 `firmware/gen5/`：
   - `lib/appFw.so`、`lib/appFwBoot.so`、`lib/AdjChkConfig.so`、`lib/libObj_g5.so`、`lib/libmpr_g5.so`（以及可选 `lib/libInfraFsys_g5.so`）
   - `share/app/` 下所有五代界面资源文件（约 232 个文件；脚本不会复制 302 MB 的 `screenreader` 无障碍语音包）

---

## 3. 确认就绪

执行完成后，你可以通过以下命令核对 `git status`（应看不到任何 `.bin` / `.so` / `.elf` 被 Git 追踪），且 `firmware/gen4/` 与 `firmware/gen5/` 已包含全部构建输入文件。
