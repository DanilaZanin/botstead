/*
 * bot-guard: второй seccomp-фильтр для кода бота (uid 1000) и PID 1 контейнера.
 *
 * Профиль deploy/seccomp/bot.json открывает clone и unshare без условий: иначе Chromium (uid 1001) не может
 * войти в user namespace. Тот же профиль действует на uid 1000, поэтому код бота получил бы user namespace
 * вместе с поверхностью ядра, которую он открывает. bot-guard закрывает это для всего, что стартует под ним:
 *
 *     bot-guard /абсолютный/путь/к/программе аргумент...
 *
 * prctl(PR_SET_NO_NEW_PRIVS), seccomp(SECCOMP_SET_MODE_FILTER) и execv. Фильтр наследуется детьми и после
 * execve, снять его нельзя. Если фильтр не встал, программа не запускается (код выхода 125).
 *
 * Правила (остальное ALLOW; фильтр ядра всегда берёт самый строгий результат из всех фильтров):
 *   другая архитектура (в том числе 32-битный i386/ARM на 64-битном ядре)  KILL_PROCESS
 *   x86_64, номер вызова с битом x32 (0x40000000)                           KILL_PROCESS
 *   unshare                                                                 EPERM
 *   setns                                                                   EPERM
 *   clone, если в flags (arg0) есть CLONE_NEWUSER                           EPERM
 *   clone3                                                                  ENOSYS (glibc откатывается на clone)
 *
 * clone с CLONE_NEWNET, CLONE_NEWPID и т.д. без CLONE_NEWUSER ядро отдаёт только при CAP_SYS_ADMIN, а у
 * контейнера бота его нет; с CLONE_NEWUSER эти флаги идут в паре, и она закрыта. Подробности: docs/isolation.md.
 *
 * Флаги читаются из младших 32 бит arg0: на x86_64 и aarch64 clone берёт flags как unsigned long, а ядро
 * использует lower_32_bits(flags). Порядок байт: little endian на обеих архитектурах.
 *
 * Сборка: gcc -O2 -static -Wall -Wextra -Werror -o bot-guard bot-guard.c (bot-image/Dockerfile).
 * Для проверки таблицы -DBOT_GUARD_DUMP печатает программу BPF и выходит (bot-image/tests).
 */
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/prctl.h>
#include <unistd.h>

#include <linux/filter.h>
#include <linux/seccomp.h>

#if !defined(__BYTE_ORDER__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "bot-guard рассчитан на little endian (x86_64, aarch64)"
#endif

/* BOT_GUARD_TARGET_*: только для тестов, чтобы собрать таблицу другой архитектуры на любом хосте. */
#if defined(BOT_GUARD_TARGET_X86_64) || (!defined(BOT_GUARD_TARGET_AARCH64) && defined(__x86_64__))
#define GUARD_AUDIT_ARCH 0xC000003Eu /* AUDIT_ARCH_X86_64 */
#define NR_UNSHARE 272
#define NR_SETNS 308
#define NR_CLONE 56
#define NR_CLONE3 435
#define NR_SECCOMP 317
#define X32_SYSCALL_BIT 0x40000000u
#elif defined(BOT_GUARD_TARGET_AARCH64) || defined(__aarch64__)
#define GUARD_AUDIT_ARCH 0xC00000B7u /* AUDIT_ARCH_AARCH64 */
#define NR_UNSHARE 97
#define NR_SETNS 268
#define NR_CLONE 220
#define NR_CLONE3 435
#define NR_SECCOMP 277
#else
#error "bot-guard: поддерживаются только x86_64 и aarch64"
#endif

#define CLONE_NEWUSER_FLAG 0x10000000u

#ifndef SECCOMP_RET_KILL_PROCESS
#define SECCOMP_RET_KILL_PROCESS 0x80000000U
#endif

#define EXIT_GUARD_FAILED 125

/* Значения Linux (одинаковы на x86_64 и aarch64), а не errno хоста: при сборке на macOS для теста они бы разошлись. */
#define GUARD_EPERM 1
#define GUARD_ENOSYS 38

/* Куда прыгает условный переход: на следующую инструкцию или на один из итоговых ответов. */
enum { NEXT = -1, L_ALLOW = 0, L_EPERM, L_ENOSYS, L_KILL, L_COUNT };

#define MAX_INSNS 24
static struct sock_filter prog[MAX_INSNS];
static unsigned n_prog;
static struct {
    unsigned at;
    int jt, jf;
} jumps[MAX_INSNS];
static unsigned n_jumps;

static void die(const char *what)
{
    fprintf(stderr, "bot-guard: %s: %s\n", what, strerror(errno));
    _exit(EXIT_GUARD_FAILED);
}

static void emit(uint16_t code, uint32_t k)
{
    if (n_prog >= MAX_INSNS) {
        errno = E2BIG;
        die("слишком длинная программа BPF");
    }
    prog[n_prog++] = (struct sock_filter)BPF_STMT(code, k);
}

static void jump(uint16_t op, uint32_t k, int jt, int jf)
{
    if (n_jumps >= MAX_INSNS) {
        errno = E2BIG;
        die("слишком много переходов");
    }
    jumps[n_jumps].at = n_prog;
    jumps[n_jumps].jt = jt;
    jumps[n_jumps].jf = jf;
    n_jumps++;
    emit(BPF_JMP | op | BPF_K, k);
}

static void build_program(void)
{
    unsigned label[L_COUNT];

    emit(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch));
    jump(BPF_JEQ, GUARD_AUDIT_ARCH, NEXT, L_KILL);
    emit(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr));
#ifdef X32_SYSCALL_BIT
    jump(BPF_JSET, X32_SYSCALL_BIT, L_KILL, NEXT);
#endif
    jump(BPF_JEQ, NR_UNSHARE, L_EPERM, NEXT);
    jump(BPF_JEQ, NR_SETNS, L_EPERM, NEXT);
    jump(BPF_JEQ, NR_CLONE3, L_ENOSYS, NEXT);
    jump(BPF_JEQ, NR_CLONE, NEXT, L_ALLOW);
    emit(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])); /* младшие 32 бита flags */
    jump(BPF_JSET, CLONE_NEWUSER_FLAG, L_EPERM, L_ALLOW);

    label[L_ALLOW] = n_prog;
    emit(BPF_RET | BPF_K, SECCOMP_RET_ALLOW);
    label[L_EPERM] = n_prog;
    emit(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | GUARD_EPERM);
    label[L_ENOSYS] = n_prog;
    emit(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | GUARD_ENOSYS);
    label[L_KILL] = n_prog;
    emit(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS);

    for (unsigned i = 0; i < n_jumps; i++) {
        unsigned at = jumps[i].at;
        /* BPF прыгает только вперёд: смещение считается от следующей инструкции. */
        prog[at].jt = jumps[i].jt == NEXT ? 0 : (uint8_t)(label[jumps[i].jt] - at - 1);
        prog[at].jf = jumps[i].jf == NEXT ? 0 : (uint8_t)(label[jumps[i].jf] - at - 1);
    }
}

int main(int argc, char **argv)
{
    build_program();

#ifdef BOT_GUARD_DUMP
    (void)argc;
    (void)argv;
    for (unsigned i = 0; i < n_prog; i++)
        printf("%u %u %u %u\n", prog[i].code, prog[i].jt, prog[i].jf, prog[i].k);
    return 0;
#else
    if (argc < 2 || argv[1][0] != '/') {
        fprintf(stderr, "usage: bot-guard /absolute/path/to/program [args...]\n");
        return EXIT_GUARD_FAILED;
    }

    struct sock_fprog fprog = {.len = (unsigned short)n_prog, .filter = prog};
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0)
        die("prctl(PR_SET_NO_NEW_PRIVS)");
    if (syscall(NR_SECCOMP, SECCOMP_SET_MODE_FILTER, 0, &fprog) != 0)
        die("seccomp(SECCOMP_SET_MODE_FILTER)");

    execv(argv[1], argv + 1);
    fprintf(stderr, "bot-guard: execv %s: %s\n", argv[1], strerror(errno));
    return errno == ENOENT ? 127 : 126;
#endif
}
