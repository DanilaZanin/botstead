// Логика панели «Квоты»: выбор режима quotaPanelMode из registry.js. Запуск: node pwa/tests/quota_panel.test.mjs
import { providerStatus, quotaPanelMode, runnerKind } from '../registry.js';

let failed = 0;
function eq(actual, expected, label) {
  if (actual !== expected) {
    failed += 1;
    console.error(`FAIL ${label}: ожидали ${expected}, получили ${actual}`);
  } else {
    console.log(`ok ${label}`);
  }
}

// Бот на подписочном CLI видит квоту своей подписки.
eq(quotaPanelMode({ provider: 'claude' }, { kind: 'cli_subscription', cli: 'claude' }), 'subscription', 'claude cli');
eq(quotaPanelMode({ provider: 'codex' }, { kind: 'cli_subscription', cli: 'codex' }), 'subscription', 'codex cli');
eq(quotaPanelMode({ provider: 'gemini' }, { kind: 'cli_subscription', cli: 'agy' }), 'subscription', 'agy cli ездит на gemini');

// Бот на API-провайдере (OpenRouter через «Свой адрес», API-ключи) видит свой расход, а не чужие подписки.
eq(quotaPanelMode({ provider: 'codex' }, { kind: 'openai_compatible' }), 'usage', 'свой адрес (OpenRouter)');
eq(quotaPanelMode({ provider: 'codex' }, { kind: 'openai_api' }), 'usage', 'OpenAI API');
eq(quotaPanelMode({ provider: 'claude' }, { kind: 'anthropic_api' }), 'usage', 'Anthropic API');
eq(quotaPanelMode({ provider: 'gemini' }, { kind: 'google_api' }), 'usage', 'Google API');
eq(quotaPanelMode({ provider: 'codex' }, null), 'usage', 'бот без провайдера');

// Подписка другого CLI не подсвечивается боту: чужая квота не показывается.
eq(quotaPanelMode({ provider: 'claude' }, { kind: 'cli_subscription', cli: 'codex' }), 'usage', 'подписка codex у claude-бота');
eq(quotaPanelMode({ provider: 'codex' }, { kind: 'cli_subscription', cli: 'agy' }), 'usage', 'подписка agy у codex-бота');
eq(quotaPanelMode({ provider: 'claude' }, { kind: 'cli_subscription' }), 'usage', 'подписка без cli');

// runnerKind остаётся согласован с серверным runner_provider.
eq(runnerKind({ kind: 'cli_subscription', cli: 'agy' }), 'gemini', 'runnerKind agy');
eq(runnerKind({ kind: 'openai_compatible' }), 'codex', 'runnerKind openai_compatible');
eq(providerStatus({ kind: 'cli_subscription', status: 'needs_login', last_error: '' }).text,
  'Нужен повторный вход', 'истёкший вход виден в списке провайдеров');
eq(providerStatus({ kind: 'cli_subscription', status: 'needs_login', last_error: '' }).kind,
  'attention', 'истёкший вход требует внимания');

if (failed) {
  console.error(`${failed} проверок упало`);
  process.exit(1);
}
console.log('Все проверки панели «Квоты» зелёные');
