# Third-Party Notices and Attribution

This repository contains or references third-party open-source projects and tools.
All Sony firmware binaries, libraries, assets, and update packages remain the
intellectual property of Sony Group Corporation and are **NOT** distributed in this
repository.

## Bundled Third-Party Components

1. **Sony-PMCA-RE (`tools/flash/camtool/vendor/pmca/`)**
   - Upstream: https://github.com/ma1co/Sony-PMCA-RE
   - Usage: USB Jiritsu/MSC communication protocol helpers used by `camtool`.
   - License: MIT License (Copyright (c) 2015-2023 ma1co and contributors).

2. **Sony-ZVE-10-Camtool (`tools/flash/camtool/`)**
   - Upstream: https://github.com/nolan2628/Sony-ZVE-10-Camtool
   - Usage: ZV-E10 USB Jiritsu/MSC online debugging, shell, and patching suite.

## External Upstream Dependencies & Prerequisite Repositories

- **zve10-qemu**: https://github.com/chocotan/zve10-qemu (`-M zve10` QEMU machine model & ZV-E10 emulation environment)
- **a7c2-qemu**: https://github.com/chocotan/a7c2-qemu (QEMU machine model / Sony camera emulation reference)
- **ILCE-7M4-RE**: https://github.com/DavidBuchanan314/ILCE-7M4-RE (Sony Gen4/Gen5 reverse engineering research & tooling)
- **sony-a7m4-custom-firmware**: https://github.com/erlla/sony-a7m4-custom-firmware (Sony custom firmware, flashing workflows, and custom LUT training)
- **sony-look-bridge**: https://github.com/751K/sony-look-bridge (Creative Look `FL`/`VV` extraction & bridging from other Sony models)
- **QEMU 11.1.2**: https://download.qemu.org/qemu-11.1.2.tar.xz
- **Sony Open Source Linux Kernel (ZV-E10 Ver. 2.02)**: https://prodgpl.blob.core.windows.net/download/DI/ZV-E10_Ver.2.02/linux-kernel.tar.gz
- **Bootlin ARMv7 EABIhf Toolchain (2020.08-1)**: https://toolchains.bootlin.com/downloads/releases/toolchains/armv7-eabihf/tarballs/armv7-eabihf--glibc--stable-2020.08-1.tar.bz2
- **Zig (`ziglang`)**: Used via `uv run` for cross-compiling ARM EABI (`arm-linux-gnueabi.2.4`) shared libraries.
