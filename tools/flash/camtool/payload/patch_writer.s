.syntax unified
.arch armv7-a
.text
.global _start

_start:
    ldr r4, [sp]            @ argc
    cmp r4, #4
    blt error_exit

    ldr r5, [sp, #8]        @ target_file
    ldr r6, [sp, #12]       @ offset string
    ldr r7, [sp, #16]       @ patch_file

    @ 1. 打开输入补丁文件 (O_RDONLY = 0)
    mov r0, r7
    mov r1, #0
    mov r2, #0
    mov r7, #5              @ sys_open
    svc #0
    cmp r0, #0
    blt error_exit
    mov r8, r0              @ r8 = in_fd

    @ 2. 解析偏移参数
    mov r1, r6
    bl parse_num
    mov r10, r0             @ r10 = target offset

    @ 3. 打开目标文件 (O_RDWR = 2)
    mov r0, r5
    mov r1, #2
    mov r2, #0
    mov r7, #5              @ sys_open
    svc #0
    cmp r0, #0
    blt close_in_and_exit
    mov r9, r0              @ r9 = out_fd

    @ 4. lseek(out_fd, target_offset, SEEK_SET = 0)
    mov r0, r9
    mov r1, r10
    mov r2, #0
    mov r7, #19             @ sys_lseek
    svc #0
    cmp r0, #0
    blt close_both_and_exit

    @ 5. 循环读写流式覆盖 (缓冲区 512 字节)
    sub sp, sp, #512

copy_loop:
    mov r0, r8              @ in_fd
    mov r1, sp              @ buf
    mov r2, #512            @ count
    mov r7, #3              @ sys_read
    svc #0
    cmp r0, #0
    blt copy_error
    beq copy_done           @ EOF (读取完成)
    mov r4, r0              @ r4 = bytes_read

    @ 写入到目标文件
    mov r0, r9              @ out_fd
    mov r1, sp              @ buf
    mov r2, r4              @ count
    mov r7, #4              @ sys_write
    svc #0
    cmp r0, r4
    bne copy_error

    b copy_loop

copy_done:
    add sp, sp, #512

    @ 6. fsync(out_fd)
    mov r0, r9
    mov r7, #118            @ sys_fsync
    svc #0

    @ 7. 关闭两个文件并安全退出
    mov r0, r9
    mov r7, #6              @ sys_close
    svc #0

    mov r0, r8
    mov r7, #6              @ sys_close
    svc #0

    mov r0, #0              @ exit code 0
    mov r7, #1              @ sys_exit
    svc #0

copy_error:
    add sp, sp, #512

close_both_and_exit:
    mov r0, r9
    mov r7, #6
    svc #0

close_in_and_exit:
    mov r0, r8
    mov r7, #6
    svc #0

error_exit:
    mov r0, #1
    mov r7, #1              @ sys_exit(1)
    svc #0

@ 数字解析辅助子例程 (支持十进制与 0x 十六进制)
parse_num:
    mov r0, #0
    ldrb r2, [r1]
    cmp r2, #48             @ '0'
    bne parse_dec
    ldrb r3, [r1, #1]
    cmp r3, #120            @ 'x'
    beq parse_hex_prefix
    cmp r3, #88             @ 'X'
    beq parse_hex_prefix

parse_dec:
    ldrb r2, [r1], #1
    cmp r2, #48
    blt parse_done
    cmp r2, #57
    bgt parse_done
    sub r2, r2, #48
    mov r3, #10
    mul r0, r0, r3
    add r0, r0, r2
    b parse_dec

parse_hex_prefix:
    add r1, r1, #2
parse_hex:
    ldrb r2, [r1], #1
    cmp r2, #48
    blt parse_done
    cmp r2, #57
    ble hex_digit
    cmp r2, #97             @ 'a'
    blt check_upper
    cmp r2, #102            @ 'f'
    ble hex_lower
check_upper:
    cmp r2, #65             @ 'A'
    blt parse_done
    cmp r2, #70             @ 'F'
    bgt parse_done
    sub r2, r2, #65
    add r2, r2, #10
    b hex_accum
hex_digit:
    sub r2, r2, #48
    b hex_accum
hex_lower:
    sub r2, r2, #97
    add r2, r2, #10
hex_accum:
    lsl r0, r0, #4
    add r0, r0, r2
    b parse_hex

parse_done:
    bx lr
