# 系统架构总览 (`docs/architecture.md`)

本文档从 **启动加载链路**、**跨代符号隔离**、**双目标触控与渲染数据流**、**开源复现构建流水线** 四个维度描述 Sony ZV-E10（Gen 4）跨代运行 Sony ZV-1M2（Gen 5 ASTRA）的完整软件架构。

---

## 1. 跨代宿主加载与启动链架构

四代宿主 `im.elf` 通过 `bootin.elf` 启动，加载剪裁后的 `libIMDB.so`，并将原本直指四代 UI 的 `Application_System_Init` 重定向至定制引导垫片 `appFwBoot.so`（编译自 `src/appFwBoot_g4host.c`）。引导垫片创建 **16MB 专属栈线程**，在隔离保护下完成五代依赖闭包加载与 `appFw.so` 初始化，最后转交五代原厂引导器 `_Z4MainPv(Application_Start)` 点火进入主事件循环。

```mermaid
flowchart TD
    Boot["bootin.elf 启动"] --> Im["im.elf (Gen4 主宿主 + 500ms 硬件看门狗)"]
    Im -->|"patch_im_elf.py: 0x975e movs r2, #1"| ImThread["THREAD_APP 遍历 libIMDB.so"]
    ImThread -->|"0x7ca4 条目 me_psid=0x00000083 -> appFwBoot.so"| BootShim["appFwBoot_g4host.c (定制引导垫片)"]
    ImThread -->|"psid=0x07fb 硬件/镜头/触控/音频服务"| G4LibObj["Gen4 libObj.so (21.3MB)\n已改名 Application_G4Old_Init"]
    BootShim -->|"pthread_attr_setstacksize 16MB 独立栈"| Stack16M["AstraAppFw 16MB 专属栈线程"]
    Stack16M -->|"免疫 __assert_fail / abort\ncondition_variable::wait 2s 超时兜底"| DlopenSeq["按序 dlopen (RTLD_LAZY | RTLD_GLOBAL)"]
    DlopenSeq --> Libs["libMWF.so -> libOb5.so -> libmp5.so -> libObjShim.so -> AdjChkConfig.so -> libappFwBoot_g5.so"]
    Libs --> AppFw["dlopen appFw.so (64.3MB)\n安全通过 2,730 个 .init_array 构造函数"]
    AppFw -->|"调用 _Z4MainPv(Application_Start)"| MainLoop["ASTRA 主事件循环 (0x0054f664)"]
```

---

## 2. 跨代符号隔离与双代共存架构

直接将五代库丢进四代 `/usr/lib` 会因同名全局符号（如 `UXEngine`、`GRM`、`CEC` 等）和硬件密文库（五代 `libInfraSKD.so`）引发 `SIGILL` 或虚表错乱（`fault=0x234`）。通过“保留四代硬件底盘 + 五代 `DT_SONAME` 改名 + GNU Hash 等长同哈希符号重命名 + `libObjShim.so` 空桩补全 + `libMWF.so` 影子继承”五重手段实现双代安全共存。

```mermaid
flowchart LR
    subgraph Gen4["Gen4 底盘层 (保留原厂明文)"]
        G4Obj["libObj.so (21.3MB)\nApplication_System_Init -> Application_G4Old_Init"]
        G4SKD["libInfraSKD.so (明文 10KB)\n替代五代 LDec 硬件密文版"]
        G4Lens["libInfraLensCommunicator.so + /sabin/\nE 卡口光学与变焦协议"]
        G4MWF["libMWF.so\n0x2d2ac cbz -> nop 影子继承"]
    end

    subgraph Gen5["Gen5 UI 层 (SONAME + GNU Hash 隔离)"]
        G5Ob5["libOb5.so (原 libObj_g5.so)\n354 个冲突符号同 Hash 改名"]
        G5Mp5["libmp5.so (原 libmpr_g5.so)\n190 个冲突符号同 Hash 改名"]
        G5Shim["libObjShim.so\n93 个 PLT 函数空桩 + 2 个 sidb 符号"]
        G5Adj["AdjChkConfig.so\n73 个冲突符号同 Hash 改名"]
        G5App["appFw.so (64.3MB)\n108 个冲突符号同 Hash 改名"]
    end

    G4Obj -.->|"彻底切断全局强符号劫持"| G5App
    G5Ob5 -->|"软链接 libuxengine.so / libgrm.so"| G5App
    G5Shim -->|"闭合 93 个五代新增 PLT 延迟绑定"| G5App
    G4MWF -->|"共享宿主 MonCtrl 单例"| G5App
```

---

## 3. 双目标触控交互与显示渲染数据流

`src/appFwBoot_g4host.c` 通过编译宏（默认 QEMU 软光栅模式，`-DZVE10_REAL_DEVICE` 真机生产模式，`-DZVE10_USB_SANDBOX` 真机 USB 沙盒模式）适配两种截然不同的图形与输入后端：

```mermaid
flowchart TB
    subgraph QEMU["QEMU 仿真目标 (--target qemu)"]
        WebUI["浏览器前端 astra_web_ui.html"] -->|"HTTP POST /api/touch /api/key"| WebPy["astra_web_qemu.py"]
        WebPy -->|"Serial Socket (/dev/ttyAM0)"| UartRx["uart_touch_rx_thread"]
        UartRx -->|"TOUCH -> WidgetSystem::postMouse*"| UXQ["libOb5.so WidgetSystem"]
        UartRx -->|"KEY -> g5_tact_key_table"| KeyQ["appFw.so 按键分发器"]
        UXQ --> SwRender["软光栅 Hook (sw_dmp_blit / glClear / swapBuffers)\n+ Morton Z-order RGBA4444 编码"]
        SwRender -->|"写入保留物理显存 0x20000000"| VRAM["QMP pmemsave 抓帧 -> PNG"]
        VRAM --> WebUI
    end

    subgraph RealDevice["真机生产目标 (--target real)"]
        TP["Gen4 libInfraTp (psid=0x07fb)"] -->|"0x026c 触控事件 (640x480 + 翻转方向)"| AppLoop["appFw.so 主循环 (0x0054f83e)"]
        AppLoop -->|"驱动 WidgetSystem"| UXReal["libOb5.so UXEngine"]
        G4Handoff["g4_handoff_real_device\n停靠四代 RM 释放 /dev/dmpgles2"] --> RealGL["硬件 GLES 渲染输出至 LCD"]
    end
```

---

## 4. 开源复现构建与验证流水线

```mermaid
flowchart LR
    Inputs["自备固件\nGen4 nflasha15.bin + Gen5 解包"] --> Prep["tools/build/prepare_inputs.py\n提取基线至 firmware/"]
    Prep --> Build["补丁与交叉编译\npatch_im_elf / patch_mwf_shadow / patch_libobj\ngenerate_hybrid_imdb / batch_rename_g5\nbuild_libobjshim / build_appfwboot"]
    Build --> Choice{目标环境}
    Choice -->|"QEMU 仿真"| QemuPack["patch_imdb_qemu_fw.py\nprepare_astra_qemu.sh -> astra.cpio"]
    QemuPack --> WebStart["start-astra-web.ps1\n浏览器触控与菜单直达验收"]
    Choice -->|"真机生产"| ImgBuild["build_prod_image.py -> 300MB ext2\nverify_prod_image.py 静态全量校验"]
    ImgBuild --> DiffFlash["check_image.py -> flash_robust.py\n保护 0x2C000 秒级差量刷入"]
    DiffFlash --> Recovery["应急回退: restore_factory_pure.py\n或 restore_wbi_auto.py"]
```

---

## 5. 仓库核心文件职责映射

| 路径 | 职责说明 |
| :--- | :--- |
| `src/appFwBoot_g4host.c` | 跨代引导器核心源码：16MB 栈线程、Hook 装甲、QEMU 软光栅与 UART 触控/按键注入、真机 RM 交接 |
| `src/libObjShim.S` | 五代新增符号空桩汇编源码（93 个函数桩 + 2 个 `sidb` 数据符号） |
| `manifests/rename_plan.json` | 经闭包分析验证的 557 个跨代冲突符号等长同 Hash 改名映射表 |
| `manifests/prod_image_manifest.json` | 生产镜像删减旧 Gen4 UI 库、注入新 Gen5 库与软链接的清单 |
| `tools/build/prepare_inputs.py` | 从用户自备 `nflasha15.bin` 与 ZV-1M2 解包目录一键提取输入基线 |
| `tools/build/elf_samehash_rename.py` | `.dynstr` 原地修改且保持 `.gnu.hash` 不变的符号隔离引擎 |
| `tools/build/build_prod_image.py` | 基于 WSL `resize2fs`/`debugfs`/`e2fsck` 生成 314,572,800 B 生产镜像 |
| `tools/build/verify_prod_image.py` | 离线校验镜像大小、9 个核心 `.so` ABI/GOT/Hook、全库 `DT_NEEDED` 闭包与资源属主 |
| `tools/qemu/astra_web_qemu.py` | QEMU `-M zve10` 守护进程、QMP 显存抓取、RGBA4444 C 解码与 Web 触控后端 |
| `tools/flash/flash_robust.py` | 带 `0x2C000` 扇区保护、掉线断点续传与 `--diff-from` 差量刷写器 |
