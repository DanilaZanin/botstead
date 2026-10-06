// Персонажи ботов: растровые clay-картинки pwa/avatars/<вид>.webp (256×256, фон залит в сам квадрат).
// Ключ аватара хранится строкой в bots.avatar; неизвестный ключ с сервера показывается как robot.
const T = {
  scout: { label: 'Скаут: шляпа рейнджера и подзорная труба' },
  mac: { label: 'Мак: монитор с пиксельным лицом' },
  sre: { label: 'SRE: каска с мигалкой' },
  coder: { label: 'Кодер: худи и наушники' },
  archive: { label: 'Архив: коробка в очках' },
  owl: { label: 'Сова: исследователь' },
  spark: { label: 'Искра: лампочка с идеей' },
  robot: { label: 'Робот: универсальный' },
  fox: { label: 'Лис: находчивый помощник' },
  cat: { label: 'Кот: спокойный компаньон' },
};
export const AVATAR_KINDS = Object.keys(T);
// Ключ приходит с сервера: в адрес картинки попадает только вид из таблицы.
const known = (kind) => (Object.hasOwn(T, kind) ? kind : 'robot');
export function avatarLabel(kind) { return T[known(kind)].label; }
// Случайный вид, не равный except (чтобы кнопка «Другой» всегда что-то меняла).
export function randomAvatar(except) {
  const pool = AVATAR_KINDS.filter((k) => k !== except);
  const buf = new Uint32Array(1);
  crypto.getRandomValues(buf);
  return pool[buf[0] % pool.length]; // 2^32 на 9–10 видов: перекос меньше 1e-8, отбраковка не нужна
}
export function avatarHtml(kind, provider = null, size = 44) {
  let dot = '';
  if (provider) { const d = Math.max(10, Math.round(size * 0.3));
    dot = `<span title="${provider}" style="position:absolute;right:-2px;bottom:-2px;width:${d}px;height:${d}px;border-radius:${d}px;background:var(--${provider}-fg);border:2px solid var(--bg-surface);box-sizing:border-box"></span>`; }
  const r = Math.round(size / 4);
  const img = `<img src="./avatars/${known(kind)}.webp" width="${size}" height="${size}" alt="" decoding="async" loading="lazy" class="avatar-img" style="border-radius:${r}px">`;
  return `<span aria-hidden="true" style="position:relative;display:inline-block;width:${size}px;height:${size}px;flex-shrink:0">${img}${dot}</span>`;
}
