/* Подставка для macOS: значения из linux/seccomp.h (стабильный ABI ядра), только для теста bot-guard. */
#ifndef BOT_GUARD_STUB_LINUX_SECCOMP_H
#define BOT_GUARD_STUB_LINUX_SECCOMP_H
#include <stdint.h>

#define SECCOMP_SET_MODE_FILTER 1
#define SECCOMP_RET_ERRNO 0x00050000U
#define SECCOMP_RET_ALLOW 0x7fff0000U

struct seccomp_data {
    int nr;
    uint32_t arch;
    uint64_t instruction_pointer;
    uint64_t args[6];
};
#endif
