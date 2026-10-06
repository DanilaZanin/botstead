# xterm.js для терминала входа

Терминал входа по подписке (`#/settings/providers/<id>/login`) использует [xterm.js](https://github.com/xtermjs/xterm.js)
(лицензия MIT). Файлы подключаются лениво, только на этом экране: `terminal.js` грузит `xterm.css`, `xterm.js`, затем
необязательный `addon-fit.js`.

## Установка

```sh
sh pwa/vendor/xterm/install.sh
```

Скрипт ставит `@xterm/xterm` и `@xterm/addon-fit` через npm во временный каталог, копирует сюда `xterm.js`, `xterm.css`,
`addon-fit.js`, `LICENSE` и пишет версии в `VERSIONS.txt`. Прежние файлы уходят в `.backup-<время>/`.

## Пока файлов нет

Экран входа работает на встроенном выводе (`createPlain` в `terminal.js`): текст без цвета, ввод с клавиатуры, вставка.
Ссылка входа, поле кода и ряд клавиш работают так же. Service worker кэширует эти файлы по одному и без них
оболочка собирается.

## Лицензия

xterm.js и дополнение fit распространяются по лицензии MIT, текст в файле `LICENSE` рядом. Не удаляйте его при обновлении.
