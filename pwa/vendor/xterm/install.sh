#!/bin/sh
# Ставит xterm.js (лицензия MIT) в pwa/vendor/xterm/: npm во временном каталоге, затем копия нужных файлов сборки.
# Запуск: sh pwa/vendor/xterm/install.sh (из корня репозитория). Прежние файлы сохраняются в .backup-<время>/.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
stamp=$(date +%Y%m%d-%H%M%S)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cd "$tmp"
npm init -y >/dev/null
npm install --no-audit --no-fund @xterm/xterm @xterm/addon-fit

core=node_modules/@xterm/xterm
fit=node_modules/@xterm/addon-fit
for f in "$core/lib/xterm.js" "$core/css/xterm.css" "$fit/lib/addon-fit.js" "$core/LICENSE"; do
  [ -f "$f" ] || { echo "нет файла $f: структура пакета изменилась" >&2; exit 1; }
done

for name in xterm.js xterm.css addon-fit.js LICENSE VERSIONS.txt; do
  if [ -f "$here/$name" ]; then
    mkdir -p "$here/.backup-$stamp"
    cp "$here/$name" "$here/.backup-$stamp/$name"
  fi
done

cp "$core/lib/xterm.js" "$here/xterm.js"
cp "$core/css/xterm.css" "$here/xterm.css"
cp "$fit/lib/addon-fit.js" "$here/addon-fit.js"
cp "$core/LICENSE" "$here/LICENSE"
printf '@xterm/xterm %s\n@xterm/addon-fit %s\n' \
  "$(node -p "require('./$core/package.json').version")" \
  "$(node -p "require('./$fit/package.json').version")" > "$here/VERSIONS.txt"

echo "готово: $here"
cat "$here/VERSIONS.txt"
