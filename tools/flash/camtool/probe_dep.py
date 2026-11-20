#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_dep.py — SD 卡活体探针（Level 0 / Level 1），零写入门禁。

用法:
    python cam.py probe                    # 自动跑完全部步骤
    python cam.py probe --lib /mnt/sd/test_lib
    python cam.py probe --step 1           # 只跑 Level 1

流程:
    Step A  探测 Sony USB 设备（0x0D95  MSC / 0x0336 服务模式）
    Step B  探测 SD 卡挂载点与测试目录（历史上真实节点是 /dev/mmca1）
    Step 0  ld.so --help          冒烟测试，确认 loader 可执行
    Step 1  ld.so --verify        全依赖闭合验证（语义为本用途设计）
    Step 1b ld.so --list          等价于 ldd，若 --verify 不认 .so 则用它

判读:
    出现 "not found" / "cannot open shared object" → 缺库，补进测试目录
    输出全部 "=> <path>"                          → DT_NEEDED 链闭环
    ld.so 拒绝 .so 作主参数                        → 换另一个参数试

安全属性:
    cam.py sh 走 Jiritsu fork+execve，ld.so 是独立子进程。
    即使它在解析时崩溃，死的只是该子进程，im.elf 与 USB 通道不受影响。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 终端编码加固：PowerShell/cmd 默认 cp936 时，遇到 emoji 会 UnicodeEncodeError，
# 而崩点往往正是"探针成功"的判定行。这里统一转 utf-8 / 容错替换，
# 保证判读信息一定打得出来。需 Python 3.7+。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from cam import sh  # noqa: E402

LD = '/lib/ld-linux.so.3'
NOT_FOUND_MARKERS = ('not found', 'cannot open shared object', 'No such file or directory')
REJECT_MARKERS = ('not a valid', 'cannot execute', 'not an ELF', 'Invalid', 'usage:', 'unrecognized option')


def probe_usb():
    import usb.core
    devs = list(usb.core.find(find_all=True, idVendor=0x054c))
    if not devs:
        print('  X 未检测到 Sony USB 设备')
        print('    请确认：相机已开机、USB 已连接、系统里装过 libusbK/Zadig 驱动')
        return None
    pids = [hex(d.idProduct) for d in devs]
    print(f'  Sony 设备 PID = {pids}')
    if '0x336' in pids or '0x0d95' in pids or '0xd95' in pids:
        print('  OK 已就绪（cam.py sh 会自动完成 0x0D95 -> 0x0336 认证切换）')
        return pids
    print(f'  ! PID 不在预期集合 {{0x0D95, 0x0336}} 内：{pids}')
    print('    这可能是更新模式(0x03E2)或其他状态，继续尝试可能失败')
    return pids


def probe_sd(user_lib):
    """探测相机内的 SD 卡挂载点与测试目录"""
    if user_lib:
        print(f'  （使用指定路径 {user_lib}）')
        code, out = sh(f'ls {user_lib}/appFw.so', timeout=20.0)
        if code != 0 or not out:
            print(f'  X {user_lib} 下找不到 appFw.so')
            print('    相机内可能未挂载 SD 卡，或路径不同')
            return None
        print(f'  OK {user_lib}/appFw.so 可见')
        return user_lib

    print('  探测 SD 卡挂载点...')
    code, out = sh('cat /proc/mounts', timeout=20.0)
    mounts = []
    if out:
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 3 and ('mmc' in f[0] or '/mnt/' in f[1] or 'sd' in f[1]):
                mounts.append((f[1], f[2]))
    for m, t in mounts:
        print(f'    {m}  ({t})')

    # 也直接问设备节点
    code, out = sh('ls /dev/mmca* 2>/dev/null; cat /proc/partitions | grep -i mmc', timeout=20.0)
    if out:
        print('    /dev 与 /proc/partitions:')
        for line in out.splitlines():
            print(f'      {line}')

    # Auto-mount SD if not mounted
    sh('mkdir -p /tmp/sd 2>/dev/null; mount /dev/mmca1 /tmp/sd 2>/dev/null', timeout=15.0)
    # 逐个候选路径试
    candidates = [m for m, t in mounts if 'test_lib' in m]
    candidates += ['/tmp/sd/test_lib', '/mnt/sd/test_lib', '/mnt/mmca1/test_lib', '/mnt/test_lib',
                   '/sd/test_lib', '/media/test_lib']
    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        code, out = sh(f'ls {c}/appFw.so', timeout=15.0)
        if code == 0 and out:
            print(f'  OK 测试目录：{c}')
            return c

    print('  X 找不到 test_lib 目录')
    print('    请在相机里确认 SD 卡已插入并挂载，然后把 firmware/gen5/lib/ 的相关文件')
    print('    拷到 SD 卡根目录的 test_lib/ 子目录下')
    print('    然后用 --lib <相机内绝对路径> 指定，例如 --lib /mnt/sd/test_lib')
    return None


def run_ld(ld_args, lib, title):
    arg = ' '.join(ld_args)
    full = f'{LD} {arg} --library-path {lib}:/usr/lib {lib}/appFw.so'
    print(f'\n--- {title} ---')
    print(f'  $ {full}')
    code, out = sh(full, timeout=180.0)
    if out:
        for line in out.splitlines():
            print(f'  | {line}')
    else:
        print('  （无输出）')
    print(f'  [退出码 {code}]')

    text = (out or '').lower()
    bad = [m for m in NOT_FOUND_MARKERS if m.lower() in text]
    reject = [m for m in REJECT_MARKERS if m.lower() in text]
    if bad:
        print(f'  判定: 缺库（命中 {bad}）')
        return 'missing'
    if reject:
        print('  判定: ld.so 拒绝了该调用形式或文件类型')
        return 'rejected'
    if code == 0 and out and '=>' in out:
        print('  判定: DT_NEEDED 依赖链全部解析到路径 [OK]')
        return 'ok'
    if code == 0 and not out:
        print('  判定: 正常退出但无输出（--verify 静默成功通常是这个表现）[OK]')
        return 'ok'
    if code == 0:
        print('  判定: 正常退出，无 not found [OK]')
        return 'ok'
    print(f'  判定: 非零退出码 {code}，需人工判读上面输出')
    return 'unknown'


def main():
    argv = sys.argv[1:]
    user_lib = None
    only_step = None
    i = 0
    while i < len(argv):
        if argv[i] == '--lib' and i + 1 < len(argv):
            user_lib = argv[i + 1]
            i += 2
            continue
        if argv[i] == '--step' and i + 1 < len(argv):
            only_step = argv[i + 1]
            i += 2
            continue
        i += 1

    print('=' * 66)
    print('SD 卡活体探针  Level 0 / Level 1（零写入门禁）')
    print('=' * 66)

    print('\n--- Step A：USB 链路 ---')
    pids = probe_usb()
    if not pids:
        print('\n结论：相机未连接，探针中止。插上 USB 后重试。')
        return 1

    print('\n--- Step B：测试目录 ---')
    lib = probe_sd(user_lib)
    if not lib:
        return 1

    if only_step == '1':
        results = [('Level 1 --verify', run_ld(['--verify'], lib, 'Level 1a ld.so --verify'))]
    else:
        print('\n--- Step 0：loader 冒烟测试 ---')
        code, out = sh(f'{LD} --help', timeout=30.0)
        if out:
            first = out.splitlines()[:12]
            for line in first:
                print(f'  | {line}')
            print(f'  ...（共 {len(out.splitlines())} 行）')
        else:
            print('  （无输出）')
        print(f'  [退出码 {code}]')
        if code != 0:
            print('  判定: ld.so 无法执行，后续步骤没意义')
            print('  可能原因：/lib/ld-linux.so.3 路径不对，或 Jiritsu 通道异常')
            return 1
        print('  判定: loader 可执行，通道正常 [OK]')

        print('\n--- Level 1：依赖链闭合验证 ---')
        print('  说明：--verify 的语义就是"验证能否加载、不执行"，先试它；')
        print('        若它不把 .so 当合法输入，再退到 --list（等价 ldd）。')
        r1 = run_ld(['--verify'], lib, 'Level 1a  ld.so --verify')
        if r1 == 'ok':
            results = [('Level 1a --verify', 'ok')]
        elif r1 == 'missing':
            results = [('Level 1a --verify', 'missing')]
        else:
            r2 = run_ld(['--list'], lib, 'Level 1b  ld.so --list')
            results = [('Level 1a --verify', r1), ('Level 1b --list', r2)]

    print('\n' + '=' * 66)
    print('汇总')
    print('=' * 66)
    verdict = 'unknown'
    for name, r in results:
        print(f'  {name:22s} -> {r}')
    vals = [r for _, r in results]
    if 'missing' in vals:
        verdict = 'missing'
    elif 'ok' in vals:
        verdict = 'ok'

    print()
    if verdict == 'ok':
        print('结论：静态依赖层闭环 —— appFw.so 的 DT_NEEDED 链在 ZV-E10 上可完整解析。')
        print('      "因缺库导致启动即崩"这一项风险已排除。')
        print('')
        print('下一步（需你确认后才执行）：')
        print('  uv run python tools/flash/flash_robust.py --image <新镜像> --diff-from firmware/backup/nflasha15.bin --flash')
    elif verdict == 'missing':
        print('结论：有依赖缺失 —— 按上面输出把对应 .so 补进 SD 卡测试目录后重跑。')
        print('      不要进入刷写步骤。')
    else:
        print('结论：两种参数形式都未能给出可信判定，需人工判读上面的原始输出。')
    print('=' * 66)
    return 0 if verdict == 'ok' else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已中断')
        sys.exit(130)
