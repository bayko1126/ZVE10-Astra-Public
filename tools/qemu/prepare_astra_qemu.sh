#!/usr/bin/env bash
# 准备 ZV-E10 QEMU 五代 ASTRA 运行环境
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${ZVE10_REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
ROOT="${ZVE10_QEMU_ROOT:-$HOME/zve10-qemu}"
WORK="${ZVE10_QEMU_WORK:-$ROOT/work}"
ASTRA_DIR="$WORK/initramfs-astra"
BUILD_OUT="${ZVE10_BUILD_OUT:-$REPO_ROOT/build_out}"
G5_SHARE="${ZVE10_G5_SHARE:-$REPO_ROOT/firmware/gen5/share/app}"

echo "========================================================"
echo "  Preparing ZV-E10 Gen 5 ASTRA QEMU Initramfs Tree"
echo "========================================================"

if [ ! -d "$WORK/initramfs-ui" ]; then
    echo "[ERROR] $WORK/initramfs-ui not found!"
    exit 1
fi

if [ ! -f "$BUILD_OUT/appFw.so" ]; then
    echo "[ERROR] $BUILD_OUT/appFw.so not found!"
    exit 1
fi

# 1. 复制 initramfs-ui 基线 (保留设备节点与权限)
echo "[1/6] Copying baseline initramfs-ui to initramfs-astra..."
rm -rf "$ASTRA_DIR"
cp -a "$WORK/initramfs-ui" "$ASTRA_DIR"

# 2. 清理四代旧 UI 库与旧资源
echo "[2/6] Removing Gen 4 legacy UI libraries and resources..."
rm -f "$ASTRA_DIR"/usr/lib/viewUnified*.so
rm -f "$ASTRA_DIR"/usr/lib/Caution*Config.so
rm -f "$ASTRA_DIR"/usr/lib/ModelCautionConfig.so
rm -rf "$ASTRA_DIR"/usr/share/app/*
mkdir -p "$ASTRA_DIR"/usr/share/app

# 3. 注入五代改名闭包与核心底盘库与配置环境
echo "[3/6] Injecting Gen 5 ASTRA binaries & patched Gen 4 host..."
cp -f "$BUILD_OUT/libObj.so" "$ASTRA_DIR/usr/lib/libObj.so"
cp -f "$BUILD_OUT/libMWF.so" "$ASTRA_DIR/usr/lib/libMWF.so"
cp -f "$BUILD_OUT/libIMDB_qemu_astra.so" "$ASTRA_DIR/usr/lib/libIMDB.so"
cp -f "$BUILD_OUT/appFwBoot.so" "$ASTRA_DIR/usr/lib/appFwBoot.so"
cp -f "$BUILD_OUT/libappFwBoot_g5.so" "$ASTRA_DIR/usr/lib/libappFwBoot_g5.so"
cp -f "$BUILD_OUT/libOb5.so" "$ASTRA_DIR/usr/lib/libOb5.so"
cp -f "$BUILD_OUT/libmp5.so" "$ASTRA_DIR/usr/lib/libmp5.so"
cp -f "$BUILD_OUT/libObjShim.so" "$ASTRA_DIR/usr/lib/libObjShim.so"
cp -f "$BUILD_OUT/AdjChkConfig.so" "$ASTRA_DIR/usr/lib/AdjChkConfig.so"
cp -f "$BUILD_OUT/appFw.so" "$ASTRA_DIR/usr/lib/appFw.so"
[ -f "$ASTRA_DIR/setting/Backup.bin" ] && cp -f "$ASTRA_DIR/setting/Backup.bin" "$ASTRA_DIR/setting/BACKUP.BIN"
[ -f "$ASTRA_DIR/setting/Backup.bak" ] && cp -f "$ASTRA_DIR/setting/Backup.bak" "$ASTRA_DIR/setting/BACKUP.BAK"

# 4. 创建五代引擎软链接与调试跳板白名单
echo "[4/6] Creating UXEngine & GRM symlinks and LeakTracer preload stub..."
ln -sf libOb5.so "$ASTRA_DIR/usr/lib/libuxengine.so"
ln -sf libOb5.so "$ASTRA_DIR/usr/lib/libgrm.so"
mkdir -p "$ASTRA_DIR/usr/tool"
cp -f "$BUILD_OUT/appFwBoot.so" "$ASTRA_DIR/usr/tool/LeakTracer.so"
chmod 755 "$ASTRA_DIR"/usr/tool/LeakTracer.so
chmod 755 "$ASTRA_DIR"/usr/lib/*.so

# 5. 注入五代完整 UI 资源 (232 项)
echo "[5/6] Injecting Gen 5 ASTRA UI assets (share/app)..."
cp -a "$G5_SHARE"/* "$ASTRA_DIR/usr/share/app/"

# 6. 配置 ASTRA 专用 init 脚本 (标准 POSIX shell)
echo "[6/6] Configuring init script for ASTRA..."
cat << 'INIT_EOF' > "$ASTRA_DIR/init"
#!/bin/sh
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
export LD_LIBRARY_PATH=/usr/lib:/lib
unset LD_PRELOAD
ulimit -s 1024 || true
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
exec >/dev/kmsg 2>&1
mkdir -p /log/astra /setting/astra /setting/mode
echo -n "p" > /setting/mode/dmode
printf "/usr/tool/LeakTracer.so" > /setting/mode/preload
echo "=== ZV-E10 GEN 5 ASTRA QEMU BOOT INITIALIZED ==="
echo "[init] ulimit -s = $(ulimit -s 2>/dev/null || echo unknown)"

load() {
    echo "MODULE_BEGIN $1"
    insmod "$@"
    result=$?
    echo "MODULE_END $1 status=$result"
    return "$result"
}
load /kmod/devno.ko
load /kmod/dmac.ko
load /usr/kmod/osal_utm.ko
load /usr/kmod/osal_uipc.ko
load /usr/kmod/dma330_stub.ko
load /usr/kmod/backup.ko Backup_load=1 Backup_save=0
load /usr/kmod/utimer.ko
load /usr/kmod/osal_ulogio.ko ulogio_initfile=/setting/ulogio.bin
load /usr/kmod/kikilog.ko
load /usr/kmod/stream.ko
load /usr/kmod/stream2.ko
load /usr/kmod/dmm.ko DmmConfig=/setting/DmmConfig.bin
load /usr/kmod/grm_ma.ko
load /usr/kmod/grm_gles.ko

for spec in 'uipc 251 207' 'backup 251 208' 'dmpgles2 248 29' 'stream 249 7' 'stream2 249 8' 'ldec 251 205'; do
    set -- $spec
    [ -e /dev/"$1" ] || mknod /dev/"$1" c "$2" "$3"
done

echo "PRE_BOOT CHECK: mode=$(cat /setting/mode/dmode) preload=$(cat /setting/mode/preload)"
echo "PRE_BOOT LINK: $(ls -l /usr/tool/LeakTracer.so)"

echo "Starting bootin.elf (ASTRA mode via /setting/mode/preload)..."
( /usr/bin/bootin.elf 'target=qemu ' dmode=3 amode=normal; echo "BOOTIN_EXIT=$?" ) &
echo "BOOTIN_PID=$!"

canary_done=0
bootlog_done=0
i=1
while [ "$i" -le 45 ]; do
    sleep 1
    if [ "$canary_done" -eq 0 ] && [ -f /log/canary_ctor.txt ]; then
        echo "CANARY DETECTED: $(cat /log/canary_ctor.txt)"
        canary_done=1
    fi
    if [ -s /log/astra/boot.log ]; then
        echo "ASTRA BOOT LOG DETECTED:"
        cat /log/astra/boot.log
        echo "END ASTRA BOOT LOG"
        bootlog_done=1
        break
    fi
    if [ $((i % 5)) -eq 0 ]; then
        echo "--- [HEARTBEAT tick=$i canary=$canary_done bootlog=$bootlog_done] ---"
    fi
    i=$((i + 1))
done

if [ "$canary_done" -eq 0 ]; then
    echo "CANARY_CHECK: [CANARY_NOT_FOUND]"
fi
if [ "$bootlog_done" -eq 0 ]; then
    echo "BOOTLOG_CHECK: [BOOTLOG_NOT_FOUND]"
    if [ -f /log/astra/boot.log ]; then
        echo "Partial boot.log content:"
        cat /log/astra/boot.log
    fi
fi

sleep 100000
INIT_EOF
chmod 755 "$ASTRA_DIR/init"

# 7. 打包 cpio 镜像
echo "[*] Packing initramfs-astra into work/astra.cpio..."
pack=(bash -o pipefail -c 'cd "$1" && find . -print0 | cpio --null -o -H newc --quiet --owner=0:0' bash "$ASTRA_DIR")
if [[ -f "$WORK/fakeroot.state" ]]; then
    pack=(fakeroot -i "$WORK/fakeroot.state" -- "${pack[@]}")
fi
"${pack[@]}" > "$WORK/astra.cpio.part"
mv "$WORK/astra.cpio.part" "$WORK/astra.cpio"

echo "[SUCCESS] ASTRA initramfs ready: $WORK/astra.cpio ($(stat -c %s "$WORK/astra.cpio") bytes)"
