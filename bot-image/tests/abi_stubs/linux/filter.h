/* Подставка для macOS: константы и структуры классического BPF из linux/filter.h (стабильный ABI ядра).
 * Нужна только тесту bot-guard, который собирает программу фильтра и разбирает её в Python. */
#ifndef BOT_GUARD_STUB_LINUX_FILTER_H
#define BOT_GUARD_STUB_LINUX_FILTER_H
#include <stdint.h>

struct sock_filter {
    uint16_t code;
    uint8_t jt;
    uint8_t jf;
    uint32_t k;
};

struct sock_fprog {
    unsigned short len;
    struct sock_filter *filter;
};

#define BPF_LD 0x00
#define BPF_RET 0x06
#define BPF_JMP 0x05
#define BPF_W 0x00
#define BPF_ABS 0x20
#define BPF_K 0x00
#define BPF_JEQ 0x10
#define BPF_JSET 0x40

#define BPF_STMT(code, k) {(unsigned short)(code), 0, 0, k}
#define BPF_JUMP(code, k, jt, jf) {(unsigned short)(code), jt, jf, k}
#endif
