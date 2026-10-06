import { createServer } from 'node:http';
import { readFile, realpath } from 'node:fs/promises';
import { extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = await realpath(resolve(fileURLToPath(new URL('../pwa/', import.meta.url))));
// По умолчанию no-store: service worker ничего не кэширует и тесты всегда видят свежие файлы.
// STATIC_CACHE=1 включает обычные заголовки кэширования, как у настоящего хостинга: нужно для офлайн-сценария.
const cacheMode = process.env.STATIC_CACHE === '1';
const port = Number(process.env.PORT || 4173);
const types = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
};

createServer(async (request, response) => {
  try {
    const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
    const relative = pathname === '/' ? 'index.html' : pathname.slice(1);
    const path = resolve(root, relative);
    if (path !== root && !path.startsWith(root + sep)) {
      response.writeHead(403).end('Forbidden');
      return;
    }
    const body = await readFile(path);
    response.writeHead(200, {
      'content-type': types[extname(path)] || 'application/octet-stream',
      'cache-control': cacheMode ? 'public, max-age=300' : 'no-store',
    });
    response.end(body);
  } catch {
    response.writeHead(404).end('Not found');
  }
}).listen(port, '127.0.0.1');
