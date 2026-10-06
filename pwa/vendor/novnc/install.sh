#!/bin/sh
# Ставит ядро noVNC (@novnc/novnc, лицензия MPL-2.0, текст в LICENSE.txt) как ES-модули в pwa/vendor/novnc/:
# npm во временном каталоге, затем копия core/, vendor/ (pako) и лицензии. Сборки нет: экран браузера бота подключает
# core/rfb.js лениво через import(). Запуск: sh pwa/vendor/novnc/install.sh (из корня репозитория).
# Прежние файлы сохраняются в .backup-<время>/. Список модулей для service worker пишется в precache.txt.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
stamp=$(date +%Y%m%d-%H%M%S)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cd "$tmp"
npm init -y >/dev/null
npm install --no-audit --no-fund @novnc/novnc

pkg=node_modules/@novnc/novnc
for f in "$pkg/core/rfb.js" "$pkg/vendor/pako/lib/zlib/inflate.js" "$pkg/LICENSE.txt"; do
  [ -f "$f" ] || { echo "нет файла $f: структура пакета изменилась" >&2; exit 1; }
done
grep -q '^import ' "$pkg/core/rfb.js" || { echo "core/rfb.js не ES-модуль: нужна версия noVNC с папкой core" >&2; exit 1; }

for name in core vendor LICENSE.txt VERSIONS.txt precache.txt; do
  if [ -e "$here/$name" ]; then
    mkdir -p "$here/.backup-$stamp"
    cp -R "$here/$name" "$here/.backup-$stamp/$name"
  fi
done

rm -rf "$here/core" "$here/vendor"
cp -R "$pkg/core" "$here/core"
cp -R "$pkg/vendor" "$here/vendor"
cp "$pkg/LICENSE.txt" "$here/LICENSE.txt"
printf '@novnc/novnc %s\n' "$(node -p "require('./$pkg/package.json').version")" > "$here/VERSIONS.txt"
(cd "$here" && find core vendor -name '*.js' | sort) > "$here/precache.txt"

echo "готово: $here"
cat "$here/VERSIONS.txt"
