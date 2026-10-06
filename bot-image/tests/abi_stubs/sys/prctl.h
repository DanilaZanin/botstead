/* Подставка для macOS: в режиме BOT_GUARD_DUMP prctl не вызывается, нужны только объявления. */
#ifndef BOT_GUARD_STUB_SYS_PRCTL_H
#define BOT_GUARD_STUB_SYS_PRCTL_H
#define PR_SET_NO_NEW_PRIVS 38
int prctl(int option, ...);
#endif
