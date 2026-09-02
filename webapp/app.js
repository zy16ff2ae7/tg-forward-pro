/* Кабинет автопересылки — мини-апп Telegram.
   Общается с /api/* и передаёт initData в заголовке X-Telegram-Init-Data. */

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
const initData = tg ? tg.initData || '' : '';

/* Демо-режим: вне Telegram (нет window.Telegram.WebApp) или по адресу /app/?demo=1.
   Позволяет посмотреть кабинет глазами без запуска бота: данные статичные,
   запросы на сервер не уходят, но переключатели и списки работают по-настоящему. */
const DEMO = new URLSearchParams(location.search).has('demo') || !tg;

const state = {
  me: null,
  accounts: [],
  pendingLogin: null,
  commands: [],
  tasks: [],
  // Все задачи пользователя, разложенные по статусу — нужны для агрегации
  // бейджей команд: видим «N на паузе» даже когда пользователь смотрит
  // вкладку «Активные», и наоборот.
  tasksByStatus: { active: [], paused: [], done: [] },
  taskCounts: {}, // kind → { active, paused, done }
  taskStatus: 'active',
  chatTag: null,
  mode: 'copy',
  botUrl: '',
  activeCommand: null, // команда, под которую сейчас собрана шторка
  lastResultsId: null, // для кнопки «Повторить» в шторке результатов
  bankedDays: 0,
  chats: [],                // последний список чатов с бэка
  selectedChats: [],       // объекты выбранных чатов (полные, не только id) —
                           // иначе при поиске выборка «исчезает» с экрана,
                           // и действия над выбранным работать перестают.
  features: {
    account_login_enabled: true,
    account_login_status: 'ready',
  },
};

/* Ключ поля из needs/optional (приходит из /api/commands) → как его нарисовать.
   Порядок полей в шторке задаёт сама команда, а не этот словарь. */
const FIELD_SPEC = {
  account: { label: 'Аккаунт', control: 'select' },
  source: { label: 'Источник', placeholder: '@channel или ссылка t.me/...' },
  target: { label: 'Приёмник', placeholder: '@my_channel или ссылка' },
  targets: {
    label: 'Получатели',
    placeholder: '@chan1, @chan2, t.me/+invite',
    note: 'через запятую',
  },
  target_user: { label: 'За кем следим', placeholder: '@username или ссылка на профиль' },
  keywords: {
    label: 'Ключевые слова',
    placeholder: 'чек, подарок, gift',
    note: 'через запятую; пусто — ловим всё подряд',
  },
  reaction: { label: 'Реакция', placeholder: '👍', note: 'любой эмодзи' },
  limit: { label: 'Сколько участников', control: 'number', placeholder: '200', note: 'не больше 10 000' },
  mode: { label: 'Режим', control: 'mode' },
  message: {
    label: 'Сообщение',
    control: 'textarea',
    placeholder: 'Текст для постинга. Несколько сообщений — каждое с новой строки, уходят по очереди.',
  },
  interval: { label: 'Интервал (мин)', control: 'number', placeholder: '2', note: 'минимум 1 минута' },
  start: { label: 'Начало (ЧЧ:ММ)', placeholder: '00:00' },
  end: { label: 'Конец (ЧЧ:ММ)', placeholder: '23:59' },
};

/* Заголовок шторки результатов для каждого типа задачи. */
const RESULTS_TITLES = {
  parser: 'Собранная аудитория',
  checks: 'Пойманные чеки',
  dialogs: 'Входящие сообщения',
};

const CHAT_TAGS = ['реклама', 'вопросы', 'продажа', 'работа', 'объявления'];

const SETTINGS = [
  { emoji: '👥', title: 'Рефералы', desc: 'Ссылка, зеркала и выплаты', start: 'referrals' },
  { emoji: '💬', title: 'Сообщения', desc: 'Сохранённые тексты, медиа и репосты', start: 'messages' },
  { emoji: '📚', title: 'Библиотека сообщений', desc: 'Публикации из вашего приватного канала', start: 'library' },
  { emoji: '🌐', title: 'Язык', desc: 'Русский', start: 'language' },
  { emoji: '📖', title: 'Гайды', desc: 'Инструкции по основным сценариям', start: 'guides' },
  { emoji: '🛟', title: 'Ресурсы', desc: 'Чат, канал и поддержка', start: 'resources' },
];

/* ───────────────────────────── Утилиты ───────────────────────────────── */

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

let toastTimer = null;
function toast(text) {
  const node = $('toast');
  node.textContent = text;
  node.classList.add('is-show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('is-show'), 2600);
}

/* ───────────────────────── Демо-режим (mock API) ─────────────────────── */

const DEMO_STATE = {
  tasks: [
    { id: 1, title: 'Новости театра → Мой канал', kind: 'forward', kind_label: 'пересылка',
      source: 'Новости театра', target: 'Мой канал', archived: false, oneshot: false,
      enabled: true, mode: 'copy', delay: 60, forwarded: 842, account_id: 1,
      created_at: '2026-08-12T10:20:00' },
    { id: 2, title: 'Афиша → Зеркало афиши', kind: 'forward', kind_label: 'пересылка',
      source: 'Афиша', target: 'Зеркало афиши', archived: false, oneshot: false,
      enabled: true, mode: 'forward', delay: 0, forwarded: 317, account_id: 1,
      created_at: '2026-08-18T09:05:00' },
    { id: 3, title: 'Подборки → Черновики', kind: 'forward', kind_label: 'пересылка',
      source: 'Подборки', target: 'Черновики', archived: false, oneshot: false,
      enabled: false, mode: 'copy', delay: 300, forwarded: 125, account_id: 1,
      created_at: '2026-08-21T18:40:00' },
    { id: 4, title: 'Парсер аудитории: Конкуренты', kind: 'parser', kind_label: 'парсер аудитории',
      source: 'Конкуренты', target: 'Конкуренты', archived: true, oneshot: true,
      enabled: false, mode: 'copy', delay: 0, forwarded: 640, account_id: 1,
      created_at: '2026-08-25T11:00:00' },
  ],
  nextId: 5,
};

const DEMO_COMMANDS = [
  { id: 'copy_channel', kind: 'forward', emoji: '🔁', title: 'Копирование канала', status: 'ready',
    needs: ['account', 'source', 'target'], optional: ['mode'],
    description: 'Копирует новые публикации между каналами с заменами текста.' },
  { id: 'broadcast', kind: 'broadcast', emoji: '📣', title: 'Рассылка по чатам', status: 'ready',
    needs: ['account', 'source', 'target', 'targets'], optional: [],
    description: 'Одно сообщение из источника — в несколько чатов сразу.',
    hint: 'Выберите чаты во вкладке «Чаты» и нажмите «📣 Рассылка» — они станут получателями. Источник: сообщение из него уйдёт во все выбранные чаты.' },
  { id: 'parser', kind: 'parser', emoji: '🕵️', title: 'Парсер аудитории', status: 'ready',
    needs: ['account', 'source'], optional: ['limit'],
    description: 'Собирает участников чужого чата в список по вашей команде.',
    hint: 'Выберите чат во вкладке «Чаты» и нажмите «🕵️ Парсер» — он станет источником. Запускается сразу, результат — кнопкой «Результаты».' },
  { id: 'autosubscribe', kind: 'autosubscribe', emoji: '🤝', title: 'Автоподписка', status: 'ready',
    needs: ['account', 'targets'], optional: ['source'],
    description: 'Вступает в каналы из списка и подхватывает ссылки из источника.',
    hint: 'Каналы — через запятую: @chan1, t.me/+invite.' },
  { id: 'checks', kind: 'checks', emoji: '🧾', title: 'Ловец чеков', status: 'ready',
    needs: ['account', 'source', 'target'], optional: ['keywords'],
    description: 'Ловит чеки и подарочные ссылки в чатах и складывает в одно место.' },
  { id: 'dialogs', kind: 'dialogs', emoji: '💬', title: 'Уведомления из диалогов', status: 'ready',
    needs: ['account', 'target'], optional: ['keywords'],
    description: 'Присылает входящие личные сообщения в выбранный чат.',
    hint: 'Источник не нужен: задача слушает все личные диалоги аккаунта.' },
  { id: 'baiting', kind: 'baiting', emoji: '🎣', title: 'Байтинг', status: 'ready',
    needs: ['account', 'source', 'target_user'], optional: ['reaction'],
    description: 'Ставит реакцию на сообщения выбранного человека в общем чате.' },
  { id: 'mute', kind: 'mute', emoji: '🔇', title: 'Мут', status: 'ready',
    needs: ['account', 'source', 'target_user'], optional: ['keywords'],
    description: 'Удаляет сообщения выбранного человека в чате, где вы администратор.' },
  { id: 'poster', kind: 'poster', emoji: '📤', title: 'Авто-постинг', status: 'ready',
    needs: ['account', 'target', 'message'], optional: ['interval', 'start', 'end'],
    description: 'Шлёт ваше сообщение в чат каждые N минут в заданном окне времени.',
    hint: 'Чат — куда постить. Сообщений может быть несколько (каждое с новой строки) — уходят по очереди. Интервал в минутах, окно — ЧЧ:ММ.' },
];

const DEMO_FEATURES = { account_login_enabled: true, account_login_status: 'ready' };

/* Копилка подписок в демо-режиме: живёт между запросами, чтобы кнопки
   «заморозить» и «распределить» было видно в работе. */
const DEMO_BANK = { banked: 0, days_left: 27 };

function demoMe() {
  return {
    id: 1,
    username: 'demo',
    name: 'Демо',
    photo_url: null,
    is_admin: true,
    subscription: { active: true, until: '2026-09-27T12:00:00', days_left: DEMO_BANK.days_left },
    stats: {
      rules: DEMO_STATE.tasks.filter((task) => !task.archived).length,
      accounts: 1,
      forwarded: 1284,
    },
    tariffs: { rub: 990, stars: 299, usdt: 12, trial_days: 3, max_rules_free: 3 },
    features: DEMO_FEATURES,
  };
}

function demoAccounts() {
  return {
    accounts: [{
      id: 1,
      phone: '+7 916 •••• 12 34',
      is_active: true,
      online: true,
      last_error: null,
      created_at: '2026-08-12T10:00:00',
    }],
    subscription: {
      active: true,
      until: '2026-09-27T12:00:00',
      days_left: DEMO_BANK.days_left,
      piggy_bank_days: DEMO_BANK.banked,
    },
    pending_login: { exists: false, phone: null, stage: null },
    bot_url: 'https://t.me/papa_is_working_for_you_bot',
    features: DEMO_FEATURES,
  };
}

const DEMO_CHATS = [
  { id: 1001, title: 'Новости театра', is_channel: true, is_group: false },
  { id: 1002, title: 'Афиша', is_channel: true, is_group: false },
  { id: 1003, title: 'Мой канал', is_channel: true, is_group: false },
  { id: 1004, title: 'Зеркало афиши', is_channel: true, is_group: false },
  { id: 1005, title: 'Подборки', is_channel: false, is_group: false },
  { id: 1006, title: 'Команда (чат)', is_channel: false, is_group: true },
];

function demoChats(path) {
  const query = (new URLSearchParams(path.split('?')[1] || '').get('q') || '').toLowerCase();
  const chats = query ? DEMO_CHATS.filter((c) => c.title.toLowerCase().includes(query)) : DEMO_CHATS;
  return { chats, total: chats.length, online: true };
}

function demoTasks(path) {
  const status = new URLSearchParams(path.split('?')[1] || '').get('status') || 'active';
  if (status === 'active') {
    return { tasks: DEMO_STATE.tasks.filter((t) => t.enabled && !t.archived) };
  }
  if (status === 'paused') {
    return { tasks: DEMO_STATE.tasks.filter((t) => !t.enabled && !t.archived) };
  }
  return { tasks: DEMO_STATE.tasks.filter((t) => t.archived) };
}

const DEMO_RESULTS = {
  4: {
    total: 3,
    items: [
      { id: 1, payload: { user_id: 501, username: 'art_deco_fan', name: 'Аня К.' }, created_at: '2026-08-25T11:02:00' },
      { id: 2, payload: { user_id: 502, username: 'night_owl', name: 'Марк' }, created_at: '2026-08-25T11:02:00' },
      { id: 3, payload: { user_id: 503, username: null, name: 'Ольга' }, created_at: '2026-08-25T11:02:00' },
    ],
  },
};

function demoTaskTitle(body, command) {
  if (command) {
    if (command.kind === 'parser') return `Парсер аудитории: ${body.source}`;
    if (command.kind === 'autosubscribe') return `Автоподписка: ${(body.targets || []).join(', ')}`;
    if (command.kind === 'dialogs') return `Уведомления из диалогов → ${body.target}`;
    if (command.kind === 'baiting') return `Байтинг: ${body.target_user} в ${body.source}`;
    if (command.kind === 'mute') return `Мут: ${body.target_user} в ${body.source}`;
    if (command.kind === 'checks') return `Ловец чеков: ${body.source} → ${body.target}`;
    if (command.kind === 'poster') {
      return `Авто-постинг → ${body.target || ''}`;
    }
    if (command.kind === 'broadcast') {
      return `Рассылка: ${body.source} → ${[body.target, ...(body.targets || [])].join(', ')}`;
    }
  }
  return `${body.source} → ${body.target}`;
}

function demoApi(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const clean = path.split('?')[0];
  const query = new URLSearchParams(path.split('?')[1] || '');
  const taskMatch = clean.match(/^\/api\/tasks\/(\d+)(?:\/(.+))?$/);

  if (taskMatch) {
    const id = Number(taskMatch[1]);
    const tail = taskMatch[2] || '';
    const task = DEMO_STATE.tasks.find((t) => t.id === id);

    if (tail === 'toggle' && task) {
      if (task.archived) return { ok: false, error: 'Задача в архиве' };
      task.enabled = !task.enabled;
      return { ok: true };
    }
    if (tail === 'mode' && task) {
      if (task.kind !== 'forward') {
        return { ok: false, error: 'Режим переключается только у обычной пересылки' };
      }
      task.mode = task.mode === 'copy' ? 'forward' : 'copy';
      return { ok: true };
    }
    if (tail === 'archive' && task) {
      task.archived = query.get('undo') !== '1';
      task.enabled = task.archived ? false : true;
      return { ok: true };
    }
    if (tail === 'run' && task) {
      if (!task.oneshot) return { ok: false, error: 'Задача работает по сообщениям' };
      if (!task.enabled || task.archived) return { ok: false, error: 'Задача не активна' };
      return task.kind === 'parser'
        ? { ok: true, collected: 640, limit: 200 }
        : { ok: true, joined: 3, total: 5 };
    }
    if (tail === 'results') {
      return Object.assign({ kind: task ? task.kind : 'parser', total: 0, items: [] }, DEMO_RESULTS[id] || {});
    }
    if (method === 'DELETE' && !tail) {
      DEMO_STATE.tasks = DEMO_STATE.tasks.filter((t) => t.id !== id);
      return { ok: true };
    }
    return { ok: true };
  }

  if (clean === '/api/tasks' && method === 'POST') {
    const body = JSON.parse(options.body || '{}');
    const command = DEMO_COMMANDS.find((item) => item.id === body.command)
      || DEMO_COMMANDS.find((item) => item.kind === body.kind)
      || DEMO_COMMANDS[0];
    const kind = command.kind;
    const task = {
      id: DEMO_STATE.nextId++,
      kind,
      kind_label: command.title.toLowerCase(),
      title: demoTaskTitle(body, command),
      source: String(body.source || ''),
      target: String(body.target || body.source || ''),
      archived: false,
      oneshot: kind === 'parser' || kind === 'autosubscribe',
      enabled: kind !== 'parser' && kind !== 'autosubscribe',
      mode: kind === 'forward' ? (body.mode || 'copy') : 'copy',
      delay: 0,
      forwarded: 0,
      account_id: Number(body.account_id) || 1,
      created_at: new Date().toISOString(),
    };
    DEMO_STATE.tasks.push(task);
    const run = kind === 'parser'
      ? { ok: true, collected: 128, limit: Number(body.limit) || 200 }
      : kind === 'autosubscribe'
        ? { ok: true, joined: 2, total: (body.targets || []).length }
        : null;
    return { ok: true, id: task.id, task, run };
  }

  if (clean === '/api/tasks') return demoTasks(path);
  if (clean === '/api/me') return demoMe();
  if (clean === '/api/commands') return { commands: DEMO_COMMANDS };
  if (clean === '/api/accounts') return demoAccounts();
  if (clean === '/api/chats') return demoChats(path);

  if (clean === '/api/subscription/bank') {
    const body = JSON.parse(options.body || '{}');
    const days = Number(body.days) || 0;
    const moved = days > 0 ? days : 7;
    DEMO_BANK.banked += moved;
    DEMO_BANK.days_left = Math.max(DEMO_BANK.days_left - moved, 1);
    return { ok: true, moved, banked_days: DEMO_BANK.banked };
  }
  if (clean === '/api/subscription/distribute') {
    const body = JSON.parse(options.body || '{}');
    const days = Number(body.days) || 0;
    if (DEMO_BANK.banked <= 0) return { ok: false, error: 'В копилке нет дней' };
    const moved = days > 0 ? Math.min(days, DEMO_BANK.banked) : DEMO_BANK.banked;
    DEMO_BANK.banked -= moved;
    DEMO_BANK.days_left += moved;
    return { ok: true, moved, banked_days: DEMO_BANK.banked, active: true, days_left: DEMO_BANK.days_left };
  }
  if (clean === '/api/subscription') {
    return {
      active: true,
      until: '2026-09-27T12:00:00',
      days_left: DEMO_BANK.days_left,
      banked_days: DEMO_BANK.banked,
      tariffs: { rub: 990, stars: 299, usdt: 12, trial_days: 3 },
    };
  }

  return { ok: true };
}

async function api(path, options = {}) {
  if (DEMO) return demoApi(path, options);

  const response = await fetch(path, {
    ...options,
    headers: {
      'X-Telegram-Init-Data': initData,
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    /* пустой ответ — просто передадим статус */
  }
  if (!response.ok) {
    const error = new Error(data.error || 'Ошибка запроса');
    error.status = response.status;
    throw error;
  }
  return data;
}

function openBot(startParam) {
  const base = state.botUrl || '';
  if (!base) {
    toast('Откройте бота в Telegram');
    return;
  }
  const url = startParam ? `${base}?start=${startParam}` : base;
  if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
  else window.open(url, '_blank');
}

/* ─────────────────────────────── Тема ────────────────────────────────── */

/* Кабинет живёт в собственной арт-деко палитре (см. :root в styles.css), а не в
   цветах темы Telegram: иначе оформление разъезжалось бы у каждого пользователя.
   Telegram-овские theme_params намеренно НЕ перекрывают брендовые переменные. */
const BRAND_BG = '#04120D';

function applyTheme() {
  if (!tg) return;
  if (tg.setHeaderColor) tg.setHeaderColor(BRAND_BG);
  if (tg.setBackgroundColor) tg.setBackgroundColor(BRAND_BG);
  if (tg.setBottomBarColor) tg.setBottomBarColor(BRAND_BG);
}

/* ────────────────────────────── Навигация ────────────────────────────── */

function switchTab(name) {
  ['commands', 'tasks', 'chats', 'accounts'].forEach((tab) => {
    $(`tab-${tab}`).classList.toggle('hidden', tab !== name);
  });
  document.querySelectorAll('.nav__item').forEach((item) => {
    item.classList.toggle('is-active', item.dataset.tab === name);
  });
  // FAB живёт вне .tab (см. комментарий в index.html), поэтому скрываем
  // его явно — иначе он «прилипнет» поверх любой вкладки.
  const fab = $('addTaskBtn');
  const onTasks = name === 'tasks';
  fab.classList.toggle('hidden', !onTasks);
  fab.setAttribute('aria-hidden', onTasks ? 'false' : 'true');
  if (name === 'tasks') loadTasks();
  if (name === 'chats') loadChats();
  if (name === 'accounts') loadAccounts();
}

/* ─────────────────────────────── Шапка ───────────────────────────────── */

function renderHeader() {
  const me = state.me;
  if (!me) return;
  state.features = me.features || state.features;
  if (!state.features.account_login_enabled) {
    $('headerSub').textContent = 'кабинет готов · вход аккаунтов на настройке';
    return;
  }
  $('headerSub').textContent = me.subscription.active
    ? `подписка: ${me.subscription.days_left} дн.`
    : 'подписка не активна';
}

/* ─────────────────────────────── Команды ─────────────────────────────── */

async function loadCommands() {
  const holder = $('commandList');
  beginLoad(holder, 'card', 4);
  try {
    const data = await api('/api/commands');
    state.commands = data.commands;
    endLoad(holder);
    renderCommands();
  } catch (error) {
    failLoad(holder, error, 'loadCommands');
  }
}

/* Статус команды с точки зрения пользователя, а не сервиса.
   Раньше метка была «включено / на настройке» — и читалась как «задача активна»,
   хотя это значило лишь «сервис поддерживает команду». Теперь честно считаем
   по задачам пользователя: «не запущено» по умолчанию, «N активных» если есть,
   «M на паузе» если только на паузе, «выключено» / «на настройке» — по флагам
   сервера. Так пользователь видит, что реально работает, а что — пусто. */
const STATUS_LABELS = {
  off: 'выключено',
  setup: 'на настройке',
  idle: 'не запущено',
  ready: 'работает',
  paused: 'на паузе',
};

function computeCommandState(command) {
  if (command.status === 'setup_required') {
    return { label: STATUS_LABELS.setup, kind: 'setup', active: 0, paused: 0 };
  }
  if (command.status === 'disabled') {
    return { label: STATUS_LABELS.off, kind: 'off', active: 0, paused: 0 };
  }
  // status === 'ready': берём готовый агрегат по всем трём спискам задач
  const counts = state.taskCounts[command.kind] || { active: 0, paused: 0, done: 0 };
  if (counts.active > 0) {
    const parts = [`${counts.active} активн.`];
    if (counts.paused > 0) parts.push(`${counts.paused} пауз.`);
    if (counts.done > 0) parts.push(`${counts.done} архив`);
    return { label: parts.join(' · '), kind: 'ready', ...counts };
  }
  if (counts.paused > 0) {
    return { label: `${counts.paused} на паузе`, kind: 'paused', ...counts };
  }
  if (counts.done > 0) {
    return { label: `${counts.done} в архиве`, kind: 'paused', ...counts };
  }
  return { label: STATUS_LABELS.idle, kind: 'off', active: 0, paused: 0, done: 0 };
}

function renderCommands() {
  const query = ($('commandSearch').value || '').toLowerCase();
  const list = state.commands.filter(
    (command) =>
      !query ||
      command.title.toLowerCase().includes(query) ||
      command.description.toLowerCase().includes(query)
  );

  if (!list.length) {
    $('commandList').innerHTML = emptyHtml('🔍', 'Ничего не найдено', 'Попробуйте другой запрос');
    return;
  }

  $('commandList').innerHTML = list
    .map((command) => {
      const meta = computeCommandState(command);
      // Карточка кликабельна ТОЛЬКО если команда доступна на сервере.
      // «На настройке» — карточка остаётся видимой, но реагирует тостом.
      const clickable = command.status === 'ready';
      const tag = `card--cmd${clickable ? '' : ' card--cmd--locked'}`;
      return `
        <button class="${tag}" data-command="${command.id}">
          <div class="card__emoji">${command.emoji}</div>
          <div class="cmd__main">
            <div class="cmd__head">
              <div class="cmd__title">${esc(command.title)}</div>
              <span class="status status--${meta.kind}" title="${esc(meta.label)}">
                <span class="status__dot" aria-hidden="true"></span>${esc(meta.label)}
              </span>
            </div>
            <div class="cmd__desc">${esc(command.description)}</div>
            ${command.hint ? `<div class="cmd__hint">${esc(command.hint)}</div>` : ''}
          </div>
          <span class="cmd__chevron" aria-hidden="true">›</span>
        </button>`;
    })
    .join('');
}

/* ──────────────────────────────── Задачи ─────────────────────────────── */

async function loadTasks(targetStatus) {
  const status = targetStatus || state.taskStatus;
  const holder = $('taskList');
  // Скелетон рисуем только когда это активная вкладка и в ней ещё нет данных.
  if (status === state.taskStatus && !state.tasksByStatus[status].length) {
    beginLoad(holder, 'plain', 3);
  }
  try {
    const data = await api(`/api/tasks?status=${status}`);
    state.tasksByStatus[status] = data.tasks || [];
    if (status === state.taskStatus) state.tasks = state.tasksByStatus[status];
    // Все списки под рукой — пересчитываем агрегат по командам.
    aggregateTaskCounts();
    if (status === state.taskStatus) {
      endLoad(holder);
      renderTasks(state.tasksByStatus[status]);
    }
    renderCommands();
  } catch (error) {
    if (status === state.taskStatus) failLoad(holder, error, 'loadTasks');
  }
}

function aggregateTaskCounts() {
  const counts = {};
  for (const list of Object.values(state.tasksByStatus)) {
    for (const task of list) {
      const kind = task.kind || 'forward';
      if (!counts[kind]) counts[kind] = { active: 0, paused: 0, done: 0 };
      if (task.enabled && !task.archived) counts[kind].active++;
      else if (!task.archived) counts[kind].paused++;
      else counts[kind].done++;
    }
  }
  state.taskCounts = counts;
}

function renderTasks(tasks) {
  const holder = $('taskList');
  // tasksByStatus[active] может быть пустым просто потому, что у пользователя
  // нет активных рассылок. Скелетон в этом случае не нужен — покажем сразу
  // дружелюбное пустое состояние.
  if (!tasks || !tasks.length) {
    const texts = {
      active: ['✅', 'Нет задач', 'Здесь появятся активные рассылки и триггеры. Запустите первую — она будет работать, даже когда вы офлайн.'],
      paused: ['⏸', 'Нет задач на паузе', 'Остановленные задачи можно вернуть в работу одним нажатием.'],
      done: ['📦', 'Завершённых задач нет', 'Архив появится здесь после первых запусков.'],
    }[state.taskStatus];
    holder.innerHTML = emptyHtml(texts[0], texts[1], texts[2]);
    return;
  }

  holder.innerHTML = tasks
    .map((task) => {
      const kind = task.kind || 'forward';
      const isForward = kind === 'forward';
      const lines = [task.kind_label || (isForward ? 'пересылка' : kind)];
      if (isForward) lines.push(task.mode === 'copy' ? 'копия без метки' : 'обычный форвард');
      if (task.oneshot) lines.push('запуск по кнопке');
      else lines.push(`задержка ${task.delay} сек`);
      lines.push(`переслано ${task.forwarded}`);

      const actions = [];
      if (task.archived) {
        actions.push('<button class="btn" data-action="unarchive" data-id="' + task.id + '">↩︎ Из архива</button>');
      } else {
        if (task.oneshot) {
          actions.push('<button class="btn" data-action="run" data-id="' + task.id + '">▶️ Запустить</button>');
        } else {
          actions.push(
            '<button class="btn" data-action="toggle" data-id="' + task.id + '">' +
              (task.enabled ? '⏸ На паузу' : '▶️ Запустить') +
              '</button>'
          );
        }
        if (isForward) {
          actions.push('<button class="btn" data-action="mode" data-id="' + task.id + '">🔁 Режим</button>');
        }
        if (RESULTS_TITLES[kind] || task.oneshot) {
          actions.push('<button class="btn" data-action="results" data-id="' + task.id + '">📄 Результаты</button>');
        }
        actions.push('<button class="btn" data-action="archive" data-id="' + task.id + '">📦 В архив</button>');
      }
      actions.push('<button class="btn btn--danger" data-action="delete" data-id="' + task.id + '">🗑</button>');

      return `
      <div class="task${task.archived ? ' task--archived' : ''}">
        <div class="task__top">
          <span class="task__dot ${task.enabled && !task.archived ? '' : 'task__dot--off'}"></span>
          <div class="task__title">${esc(task.title)}</div>
        </div>
        <div class="task__meta">${lines.join(' · ')}</div>
        <div class="task__actions">${actions.join('')}</div>
      </div>`;
    })
    .join('');
}

const ACTION_MESSAGES = {
  delete: 'Задача удалена',
  archive: 'Задача в архиве',
  unarchive: 'Задача вернулась в работу',
  mode: 'Режим переключён',
  toggle: 'Готово',
};

/* Любое действие, меняющее состав задач — обновляет не только текущую
   вкладку, но и счётчики по всем трём статусам, чтобы бейджи команд
   оставались честными. */
async function refreshAllTaskLists() {
  const visible = state.taskStatus;
  await Promise.all([
    loadTasks('active'),
    loadTasks('paused'),
    loadTasks('done'),
  ]);
  if (state.taskStatus !== visible) {
    state.taskStatus = visible;
    document.querySelectorAll('#taskStatus .seg').forEach((seg) => {
      seg.classList.toggle('is-active', seg.dataset.status === visible);
    });
  }
  renderTasks(state.tasksByStatus[visible]);
  renderCommands();
}

async function taskAction(action, id, button) {
  try {
    if (action === 'toggle') await withLoading(button, () => api(`/api/tasks/${id}/toggle`, { method: 'POST' }));
    if (action === 'mode') await withLoading(button, () => api(`/api/tasks/${id}/mode`, { method: 'POST' }));
    if (action === 'archive') await withLoading(button, () => api(`/api/tasks/${id}/archive`, { method: 'POST' }));
    if (action === 'unarchive') await withLoading(button, () => api(`/api/tasks/${id}/archive?undo=1`, { method: 'POST' }));
    if (action === 'delete') await withLoading(button, () => api(`/api/tasks/${id}`, { method: 'DELETE' }));
    if (action === 'results') {
      await openResults(id);
      return;
    }
    if (action === 'run') {
      const data = await withLoading(button, () => api(`/api/tasks/${id}/run`, { method: 'POST' }));
      toast(runMessage(data.run));
      await refreshAllTaskLists();
      return;
    }
    toast(ACTION_MESSAGES[action] || 'Готово');
    await refreshAllTaskLists();
  } catch (error) {
    toast(error.message);
  }
}

/* Сводка запуска разовой задачи → понятная фраза для тоста. */
function runMessage(run) {
  if (!run) return 'Готово';
  if (!run.ok) return run.error || 'Запуск не удался';
  if (run.collected != null) return `Собрано участников: ${run.collected}`;
  if (run.joined != null) return `Вступили в чаты: ${run.joined} из ${run.total}`;
  return 'Готово';
}

async function openResults(id) {
  const task = (state.tasks || []).find((item) => item.id === Number(id));
  $('resultsTitle').textContent = RESULTS_TITLES[task ? task.kind : ''] || 'Результаты';
  const body = $('resultsBody');
  state.lastResultsId = id;
  beginLoad(body, 'plain', 4);
  $('resultsSheet').classList.add('is-open');

  try {
    const data = await api(`/api/tasks/${id}/results?limit=100`);
    endLoad(body);
    if (!data.items.length) {
      body.innerHTML = emptyHtml(
        '📭',
        'Пока пусто',
        'Задача ещё ничего не насобирала. Запустите её и вернитесь сюда.'
      );
      return;
    }
    const rows = data.items.map((item) => {
      const payload = item.payload || {};
      if (payload.username || payload.user_id != null) {
        const name = payload.name || payload.username || `ID ${payload.user_id}`;
        const handle = payload.username ? `@${payload.username}` : (payload.phone || '');
        return `<div class="result">
          <div class="result__title">${esc(name)}</div>
          <div class="result__sub">${esc(handle || 'без ника')} · ID ${esc(payload.user_id)}</div>
        </div>`;
      }
      const text = payload.text || payload.link || payload.preview || JSON.stringify(payload);
      return `<div class="result">
        <div class="result__title">${esc(String(text).slice(0, 140))}</div>
        <div class="result__sub">${esc((item.created_at || '').replace('T', ' ').slice(0, 16))}</div>
      </div>`;
    });
    body.innerHTML =
      `<div class="results__meta">Всего: ${data.total}${data.total > data.items.length ? ` · показаны последние ${data.items.length}` : ''}</div>` +
      rows.join('');
  } catch (error) {
    failLoad(body, error, 'openResults');
  }
}

/* ───────────────────────────────── Чаты ──────────────────────────────── */

/* ───────────────────────────────── Чаты ──────────────────────────────── */

/* Как передать выбранный чат в форму задачи: приоритет у @username (его
   сервер резолвит надёжно по manager.resolve_chat). Если username нет —
   подставляем «id» числом: бэк понимает любой из этих форматов. */
function chatToRef(chat) {
  if (!chat) return '';
  if (chat.username) return `@${String(chat.username).replace(/^@/, '')}`;
  return String(chat.id);
}

function chatTitle(chat) {
  if (!chat) return '';
  return chat.title || String(chat.id || '');
}

/* Текущая выборка чатов в читабельном виде: «Aльфа, Браво, +2». */
function selectedChatsSummary() {
  const n = state.selectedChats.length;
  if (n === 0) return '';
  const first = state.selectedChats.slice(0, 2).map(chatTitle).join(', ');
  return n > 2 ? `${first}, +${n - 2}` : first;
}

function getSelectedChats() {
  return state.selectedChats;
}

function isChatSelected(id) {
  return state.selectedChats.some((chat) => chat.id === id);
}

function toggleChatSelection(chat) {
  if (!chat || chat.id == null) return;
  const idx = state.selectedChats.findIndex((item) => item.id === chat.id);
  if (idx >= 0) state.selectedChats.splice(idx, 1);
  else state.selectedChats.push(chat);
  updateChatBar();
}

function clearChatSelection() {
  state.selectedChats = [];
  // Перерисовать отметки на текущем списке, не дёргая сервер
  document.querySelectorAll('#chatList .chat.is-selected').forEach((node) => {
    node.classList.remove('is-selected');
    const check = node.querySelector('.chat__check');
    if (check) check.textContent = '';
  });
  updateChatBar();
}

function updateChatBar() {
  const bar = $('chatBar');
  if (!bar) return;
  const n = state.selectedChats.length;
  bar.classList.toggle('is-visible', n > 0);
  const countEl = $('chatBarCount');
  if (countEl) countEl.textContent = String(n);
  const sumEl = $('chatBarSummary');
  if (sumEl) sumEl.textContent = n ? selectedChatsSummary() : '';
}

async function loadChats() {
  const holder = $('chatList');
  const account = state.accounts[0];

  if (!state.features.account_login_enabled) {
    holder.innerHTML = emptyHtml('⚙️', 'Нужен MTProto-вход', 'Список чатов появится после заполнения API_ID/API_HASH и подключения аккаунта по телефону.');
    updateChatBar();
    return;
  }

  if (!account) {
    holder.innerHTML = emptyHtml('👤', 'Нет аккаунта', 'Подключите аккаунт во вкладке «Аккаунты».');
    updateChatBar();
    return;
  }

  const query = encodeURIComponent($('chatSearch').value || '');
  beginLoad(holder, 'plain', 5);
  try {
    const data = await api(`/api/chats?account_id=${account.id}&q=${query}`);
    endLoad(holder);
    state.chats = data.chats || [];
    if (!data.online) {
      holder.innerHTML = emptyHtml('📴', 'Аккаунт не в сети', 'Перезапустите аккаунт в боте.');
      updateChatBar();
      return;
    }
    if (!state.chats.length) {
      holder.innerHTML = emptyHtml('💬', 'Ничего не найдено', 'Измените запрос или тег.');
      updateChatBar();
      return;
    }
    // Рисуем чаты КАК КНОПКИ: теперь их можно выбрать мышкой.
    // Выделение сохраняется между поисковыми запросами — хранится в state,
    // поэтому id чата не «теряется» после фильтра поиска.
    holder.innerHTML = state.chats.map((chat) => {
      const selected = isChatSelected(chat.id);
      return `
        <button class="chat${selected ? ' is-selected' : ''}" data-chat-id="${chat.id}" type="button">
          <div class="chat__emoji">${chat.is_channel ? '📢' : chat.is_group ? '👥' : '💬'}</div>
          <div class="chat__body">
            <div class="chat__title">${esc(chat.title)}</div>
            <div class="chat__sub"><code>${chat.id}</code>${chat.username ? ' · @' + esc(chat.username) : ''}</div>
          </div>
          <span class="chat__check" aria-hidden="true">${selected ? '✓' : ''}</span>
        </button>`;
    }).join('');
    updateChatBar();
  } catch (error) {
    failLoad(holder, error, 'loadChats');
    updateChatBar();
  }
}

/* Из выбранных чатов — открыть шторку задачи с уже заполненными полями.
   Куда именно подставить выбранные чаты — зависит от команды, иначе
   валидация на сохранении падает «Укажите: источник»:
     • parser      → источник (кого парсим — сам выбранный чат)
     • forward      → приёмник (куда пересылаем)
     • broadcast/.. → получатели (куда рассылаем / на что подписываемся).
   Один чат → он же единственный получатель. Несколько → первый приёмник,
   остальные — в список получателей. */
function openTaskForSelection(kind) {
  const selected = getSelectedChats();
  if (!selected.length) return;
  const refs = selected.map(chatToRef);
  const command =
    state.commands.find((item) => item.id === kind)
    || state.commands.find((item) => item.kind === kind)
    || state.commands.find((item) => item.id === 'copy_channel');
  if (!command) {
    toast('Каталог команд ещё не загружен');
    return;
  }
  const prefill = {};
  if (command.kind === 'parser') {
    // Парсер собирает участников ВЫБРАННОГО чата — он и есть источник.
    prefill.source = refs[0];
  } else if (command.kind === 'forward') {
    // Пересылка кладёт выбранный чат в приёмник; источник допишет пользователь.
    prefill.target = refs[0];
  } else {
    // Рассылка / автоподписка и пр.: выбранные чаты — получатели.
    prefill.target = refs[0];
    prefill.targets = refs.length > 1 ? refs.join(', ') : '';
  }
  openTaskSheet(command, prefill);
}

function renderChatTags() {
  $('chatTags').innerHTML = CHAT_TAGS.map(
    (tag) => `<button class="chip" data-tag="${tag}">${tag}</button>`
  ).join('');
}

/* ─────────────────────────────── Аккаунты ────────────────────────────── */

async function loadAccounts() {
  const holder = $('accountList');
  beginLoad(holder, 'plain', 2);
  try {
    const data = await api('/api/accounts');
    endLoad(holder);
    state.accounts = data.accounts || [];
    state.pendingLogin = data.pending_login || null;
    state.botUrl = data.bot_url || '';
    state.features = data.features || state.features;

    const sub = data.subscription || {};
    renderPiggyBank(sub.piggy_bank_days, sub.days_left);
    const badge = $('subBadge');
    badge.textContent = sub.active ? `активна · ${sub.days_left} дн.` : 'нет подписки';
    badge.classList.toggle('is-active', Boolean(sub.active));

    // шапка показывает те же дни, что и карточка подписки — держим их в согласии
    if (state.me) {
      state.me.subscription = {
        active: Boolean(sub.active),
        until: sub.until || null,
        days_left: sub.days_left || 0,
      };
      renderHeader();
    }

    const addBtn = $('addAccountBtn');
    if (addBtn) {
      addBtn.textContent = state.features.account_login_enabled
        ? '＋ Добавить аккаунт'
        : '⚙️ Нужен MTProto-вход';
    }

    renderAccountList();
    fillTaskAccounts();
  } catch (error) {
    failLoad(holder, error, 'loadAccounts');
  }
}

function renderAccountList() {
  const holder = $('accountList');
  if (!state.features.account_login_enabled) {
    holder.innerHTML = emptyHtml(
      '⚙️',
      'Подключение на настройке',
      'Кабинет, меню, подписки и платежи работают. Вход аккаунтов по телефону включится после подключения MTProto-шлюза сервиса.'
    );
    return;
  }

  const pendingHtml = state.pendingLogin && state.pendingLogin.exists
    ? `<button class="card card--add" data-action="resume-login">▶️ Продолжить вход ${esc(state.pendingLogin.phone || '')}</button>`
    : '';

  if (!state.accounts.length) {
    holder.innerHTML = pendingHtml + emptyHtml('👤', 'Аккаунтов нет', 'Добавьте первый аккаунт — он будет читать источники.');
    return;
  }

  holder.innerHTML = pendingHtml + state.accounts
    .map(
      (account) => `
      <div class="account">
        <div class="account__avatar">${esc((account.phone || '?').replace('+', '').slice(0, 1))}</div>
        <div class="account__body">
          <div class="account__phone">${esc(account.phone)}</div>
          <div class="account__id">ID ${account.id}${account.last_error ? ' · ' + esc(account.last_error) : ''}</div>
        </div>
        <div class="account__state ${account.online ? 'account__state--on' : 'account__state--off'}">
          ${account.online ? '🟢 на связи' : '🔴 офлайн'}
        </div>
      </div>`
    )
    .join('');
}

/* ─────────────────────────── Шторка задачи ───────────────────────────── */

function fillTaskAccounts() {
  const select = $('taskAccount');
  if (!select) return;
  if (!state.features.account_login_enabled) {
    select.innerHTML = '<option value="">Вход аккаунтов на настройке</option>';
    return;
  }
  if (!state.accounts.length) {
    select.innerHTML = '<option value="">Нет аккаунтов</option>';
    return;
  }
  select.innerHTML = state.accounts
    .map((account) => `<option value="${account.id}">${esc(account.phone)}</option>`)
    .join('');
  const firstActive = state.accounts.find((account) => account.online);
  if (firstActive) select.value = String(firstActive.id);
}

/* Разметка одного поля шторки по ключу из needs/optional команды. */
function fieldHtml(key) {
  const spec = FIELD_SPEC[key];
  if (!spec) return '';

  if (spec.control === 'select') {
    return `<label class="field"><span>${spec.label}</span><select id="taskAccount"></select></label>`;
  }
  if (spec.control === 'mode') {
    return `<div class="field"><span>${spec.label}</span>
      <div class="segmented segmented--sm" id="taskMode">
        <button type="button" class="seg is-active" data-mode="copy">Копия (без метки)</button>
        <button type="button" class="seg" data-mode="forward">Форвард</button>
      </div></div>`;
  }
  if (spec.control === 'textarea') {
    return `<label class="field"><span>${spec.label}</span>
      <textarea id="task_${key}" rows="4" placeholder="${esc(spec.placeholder || '')}"></textarea>
      ${spec.note ? `<i class="field__note">${esc(spec.note)}</i>` : ''}
    </label>`;
  }
  const type = spec.control === 'number' ? 'number' : 'text';
  return `<label class="field"><span>${spec.label}</span>
    <input type="${type}" id="task_${key}" placeholder="${esc(spec.placeholder || '')}" autocomplete="off">
    ${spec.note ? `<i class="field__note">${esc(spec.note)}</i>` : ''}
  </label>`;
}

function fieldValue(key) {
  const spec = FIELD_SPEC[key];
  if (!spec) return '';
  if (spec.control === 'select') {
    const select = $('taskAccount');
    return select ? select.value.trim() : '';
  }
  if (spec.control === 'mode') return state.mode;
  const node = $(`task_${key}`);
  return node && node.value ? String(node.value).trim() : '';
}

function splitList(value) {
  return String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function openTaskSheet(command, prefill) {
  if (!state.features.account_login_enabled) {
    toast('Вход аккаунтов пока на настройке');
    switchTab('accounts');
    return;
  }
  if (!state.accounts.length) {
    toast('Сначала подключите аккаунт');
    switchTab('accounts');
    return;
  }

  state.activeCommand = command
    || state.commands.find((item) => item.id === 'copy_channel')
    || { id: 'copy_channel', kind: 'forward', title: 'Копирование канала', emoji: '🔁', needs: ['account', 'source', 'target'], optional: ['mode'] };
  state.mode = 'copy';

  $('taskSheetTitle').textContent = `${state.activeCommand.emoji || ''} ${state.activeCommand.title}`.trim();
  $('taskSheetLead').textContent = state.activeCommand.description || '';
  $('taskFields').innerHTML = [
    ...state.activeCommand.needs,
    ...(state.activeCommand.optional || []),
  ].map(fieldHtml).join('');
  $('taskHint').textContent = state.activeCommand.hint
    || 'Аккаунт должен быть подписан на источник и иметь право писать в приёмник.';
  $('taskError').textContent = '';

  fillTaskAccounts();
  bindSheetFields();
  applyTaskPrefill(prefill || {});
  $('taskSheet').classList.add('is-open');
}

/* Заполняем поля формы значениями из выбранных чатов. Применяется после
   построения разметки полей (DOM уже существует). Источник оставляем пустым —
   иначе легко отправить парсер на свой собственный канал по ошибке. */
function applyTaskPrefill(prefill) {
  const setValue = (key, value) => {
    if (value == null || value === '') return;
    const node = $(`task_${key}`);
    if (node && node.value !== undefined) node.value = value;
  };
  setValue('target', prefill.target);
  if (Array.isArray(prefill.targets)) {
    setValue('targets', prefill.targets.join(', '));
  } else if (prefill.targets) {
    setValue('targets', prefill.targets);
  }
  setValue('source', prefill.source);
  setValue('target_user', prefill.target_user);
}

function bindSheetFields() {
  document.querySelectorAll('#taskMode .seg').forEach((seg) => {
    seg.addEventListener('click', () => {
      state.mode = seg.dataset.mode;
      document.querySelectorAll('#taskMode .seg').forEach((item) => {
        item.classList.toggle('is-active', item === seg);
      });
    });
  });
}

function closeSheets() {
  document.querySelectorAll('.sheet').forEach((sheet) => sheet.classList.remove('is-open'));
  state.activeCommand = null;
}

/* Значения полей → тело запроса POST /api/tasks. */
function collectTaskPayload() {
  const command = state.activeCommand;
  const values = {};
  [...command.needs, ...(command.optional || [])].forEach((key) => {
    values[key] = fieldValue(key);
  });

  const missing = command.needs
    .filter((key) => !values[key])
    .map((key) => (FIELD_SPEC[key] ? FIELD_SPEC[key].label.toLowerCase() : key));
  if (missing.length) return { error: 'Заполните: ' + missing.join(', ') };

  const body = { command: command.id, account_id: Number(values.account) || 0 };
  if (values.source) body.source = values.source;
  if (values.target) body.target = values.target;
  if (values.target_user) body.target_user = values.target_user;
  if (values.reaction) body.reaction = values.reaction;
  if (values.keywords) body.keywords = splitList(values.keywords);
  if (values.targets) body.targets = splitList(values.targets);
  if (values.limit) body.limit = Number(values.limit) || 0;
  if (values.mode) body.mode = values.mode;
  if (values.message) body.message = values.message;
  if (values.interval) body.interval = Number(values.interval) || 0;
  if (values.start) body.start = values.start;
  if (values.end) body.end = values.end;
  return { body };
}

async function submitTask() {
  if (!state.activeCommand) return;

  const { error, body } = collectTaskPayload();
  if (error) {
    $('taskError').textContent = error;
    return;
  }

  const button = $('taskSubmit');
  try {
    const data = await withLoading(button, () =>
      api('/api/tasks', { method: 'POST', body: JSON.stringify(body) })
    );
    closeSheets();

    const run = data.run;
    if (run) toast(runMessage(run));
    else toast('Задача запущена');

    // разовая задача отработала и ушла в архив — показываем архив, иначе активные
    state.taskStatus = data.task && data.task.archived ? 'done' : 'active';
    document.querySelectorAll('#taskStatus .seg').forEach((seg) => {
      seg.classList.toggle('is-active', seg.dataset.status === state.taskStatus);
    });
    await refreshAllTaskLists();
    switchTab('tasks');
  } catch (requestError) {
    if (requestError.status === 402) {
      $('taskError').textContent = 'Лимит правил. Оформите абонемент.';
      setTimeout(() => openBot('subscribe'), 1200);
    } else if (requestError.status === 503) {
      $('taskError').textContent = requestError.message;
      setTimeout(() => openBot('add_account'), 1200);
    } else {
      $('taskError').textContent = requestError.message;
    }
  }
}

/* ─────────────────────── Копилка подписок ────────────────────────────── */

function renderPiggyBank(banked, daysLeft) {
  state.bankedDays = banked || 0;
  $('piggyDays').textContent = state.bankedDays;
  const distribute = $('distributeBtn');
  const freeze = $('freezeBtn');
  if (distribute) {
    distribute.disabled = !state.bankedDays;
    distribute.textContent = state.bankedDays
      ? `Распределить ${state.bankedDays} дн.`
      : 'Копилка пуста';
  }
  if (freeze) {
    freeze.disabled = !daysLeft || daysLeft <= 1;
    freeze.textContent = daysLeft > 1 ? `❄️ Заморозить ${daysLeft - 1} дн.` : '❄️ Заморозить дни';
  }
}

/* ─────────────────────────────── Оплата Stars ─────────────────────────── */

/* Инвойс создаёт бот через Bot API, кабинет только открывает его:
   сумма и payload формируются на сервере, пользователь не покидает
   мини-апп. Зачисление приходит в хендлер successful_payment бота. */
async function payWithStars(button, months = 1) {
  // Вне Telegram (демо или обычный браузер) платёжный WebView не поднимется —
  // честно уводим в бота, а не делаем вид, что что-то произошло.
  if (DEMO || !tg || !tg.openInvoice) {
    openBot('subscribe');
    return;
  }

  try {
    const invoice = await withLoading(button, () =>
      api('/api/subscription/invoice', { method: 'POST', body: JSON.stringify({ months }) })
    );
    openStarsInvoice(invoice.url);
  } catch (error) {
    toast(error.message);
  }
}

function openStarsInvoice(url) {
  tg.openInvoice(url, (status) => {
    if (status === 'paid') {
      toast('Оплата прошла — абонемент активен.');
      loadAccounts();
    } else if (status === 'pending') {
      toast('Платёж обрабатывается, абонемент появится после подтверждения.');
    } else if (status === 'failed') {
      toast('Платёж не прошёл. Попробуйте ещё раз.');
    }
    // 'cancelled' — пользователь закрыл окно сам, молчим.
  });
}

/* На кнопке сразу видно цену: она приходит в /api/me вместе с тарифами. */
function renderTopUpButton() {
  const button = $('topUpBtn');
  if (!button) return;
  const stars = (state.me && state.me.tariffs && state.me.tariffs.stars) || 0;
  button.textContent = stars ? `⭐ Оплатить ${stars} звёзд` : '⭐ Оплатить звёздами';
}

async function moveBankDays(direction, button) {
  const path = direction === 'freeze' ? '/api/subscription/bank' : '/api/subscription/distribute';
  try {
    const data = await withLoading(button, () => api(path, { method: 'POST', body: '{}' }));
    toast(
      direction === 'freeze'
        ? `${data.moved} дн. убрано в копилку`
        : `${data.moved} дн. вернулось в подписку`
    );
    await loadAccounts();
  } catch (error) {
    toast(error.message);
  }
}

/* ─────────────────────────── Шторка настроек ─────────────────────────── */

function renderSettings() {
  $('settingsList').innerHTML = SETTINGS.map(
    (item) => `
    <button class="setting" data-start="${item.start}">
      <div class="setting__emoji">${item.emoji}</div>
      <div class="setting__body">
        <div class="setting__title">${esc(item.title)}</div>
        <div class="setting__desc">${esc(item.desc)}</div>
      </div>
      <div class="setting__chev">›</div>
    </button>`
  ).join('');
}

/* ────────────────────────────── Пустое состояние ─────────────────────── */

function emptyHtml(emoji, title, text) {
  return `
    <div class="empty">
      <div class="empty__big">${emoji}</div>
      <div class="empty__title">${esc(title)}</div>
      <div>${esc(text)}</div>
    </div>`;
}

/* ───────────────────── Загрузка, ошибка, индикатор ───────────────────── */

function skeletonHtml(kind, count) {
  const withIcon = `
    <div class="sk-card">
      <div class="sk sk-dot"></div>
      <div class="sk-card__body">
        <div class="sk sk-line"></div>
        <div class="sk sk-line--sm"></div>
      </div>
    </div>`;
  const plain = `
    <div class="sk-card">
      <div class="sk-card__body">
        <div class="sk sk-line" style="width:72%"></div>
        <div class="sk sk-line--sm"></div>
      </div>
    </div>`;
  const template = kind === 'plain' ? plain : withIcon;
  return Array.from({ length: count }, () => template).join('');
}

/* Скелетон показываем только когда списка ещё нет. Если данные уже на экране,
   обновляем их бесшовно — иначе при каждом переключении вкладки экран мигал бы. */
function beginLoad(holder, kind = 'card', count = 3) {
  if (holder.children.length && !holder.querySelector('.state')) return;
  holder.setAttribute('aria-busy', 'true');
  holder.innerHTML = skeletonHtml(kind, count);
}

function endLoad(holder) {
  holder.removeAttribute('aria-busy');
}

/* Ошибка обязана предлагать выход, а не только сообщать о беде. */
function errorHtml(message, retryFn) {
  return `
    <div class="state">
      <svg class="mascot mascot--md" viewBox="0 0 120 120" aria-hidden="true"><use href="#papa" /></svg>
      <div class="state__title">Не удалось загрузить</div>
      <div class="state__text">${esc(message)}</div>
      <button class="btn state__retry" data-retry="${retryFn}">Повторить</button>
    </div>`;
}

function failLoad(holder, error, retryFn) {
  endLoad(holder);
  holder.innerHTML = errorHtml(error.message, retryFn);
}

/* Пока идёт запрос, кнопка показывает спиннер: повторный тап ничего не сломает. */
async function withLoading(button, action) {
  if (!button) return action();
  const label = button.textContent;
  button.classList.add('is-loading');
  button.disabled = true;
  try {
    return await action();
  } finally {
    button.classList.remove('is-loading');
    button.disabled = false;
    button.textContent = label;
  }
}

/* ───────────────────────────────── Старт ─────────────────────────────── */

function bindEvents() {
  // нижняя навигация
  document.querySelectorAll('.nav__item').forEach((item) => {
    item.addEventListener('click', () => switchTab(item.dataset.tab));
  });

  // команды
  $('commandSearch').addEventListener('input', renderCommands);
  $('commandList').addEventListener('click', (event) => {
    const card = event.target.closest('[data-command]');
    if (!card) return;
    const command = state.commands.find((item) => item.id === card.dataset.command);
    if (!command) return;
    if (command.status === 'ready') openTaskSheet(command);
    else if (command.status === 'setup_required') {
      toast('Подключение аккаунтов на настройке. Кабинет работает, команды включатся вместе с ним.');
    } else toast(`${command.title} — выключено в настройках сервиса`);
  });

  // задачи
  document.querySelectorAll('#taskStatus .seg').forEach((seg) => {
    seg.addEventListener('click', () => {
      state.taskStatus = seg.dataset.status;
      document.querySelectorAll('#taskStatus .seg').forEach((item) => {
        item.classList.toggle('is-active', item === seg);
      });
      // Если данные для этого статуса уже подгружены (агрегатор на boot),
      // показываем их без сети; иначе подгружаем.
      const cached = state.tasksByStatus[state.taskStatus];
      if (cached && cached.length) {
        renderTasks(cached);
      } else {
        loadTasks();
      }
    });
  });
  $('taskList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    taskAction(button.dataset.action, button.dataset.id, button);
  });
  // «＋ Запустить задачу» без выбранной команды — открываем пересылку
  $('addTaskBtn').addEventListener('click', () => openTaskSheet(null));

  // чаты
  renderChatTags();
  let chatTimer = null;
  $('chatSearch').addEventListener('input', () => {
    clearTimeout(chatTimer);
    chatTimer = setTimeout(loadChats, 350);
  });
  // Один делегированный слушатель: чаты перерисовываются при каждом поиске,
  // поэтому вешать обработчик на каждый .chat бессмысленно.
  $('chatList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-chat-id]');
    if (!item) return;
    const id = Number(item.dataset.chatId);
    const chat = state.chats.find((item) => item.id === id);
    if (!chat) return;
    const wasSelected = isChatSelected(id);
    toggleChatSelection(chat);
    // Визуально отражаем состояние без перерисовки всего списка — дешевле.
    const check = item.querySelector('.chat__check');
    if (wasSelected) {
      item.classList.remove('is-selected');
      if (check) check.textContent = '';
    } else {
      item.classList.add('is-selected');
      if (check) check.textContent = '✓';
    }
  });
  $('chatBar').addEventListener('click', (event) => {
    const button = event.target.closest('[data-chat-action]');
    if (!button) return;
    const action = button.dataset.chatAction;
    if (action === 'clear') {
      clearChatSelection();
      return;
    }
    if (!state.selectedChats.length) {
      toast('Сначала выберите чаты');
      return;
    }
    if (action === 'broadcast') {
      openTaskForSelection('broadcast');
    } else if (action === 'forward') {
      openTaskForSelection('copy_channel');
    } else if (action === 'parser') {
      openTaskForSelection('parser');
    }
  });
  $('chatTags').addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    const tag = chip.dataset.tag;
    const isActive = chip.classList.contains('is-active');
    document.querySelectorAll('#chatTags .chip').forEach((item) => {
      item.classList.remove('is-active');
    });
    if (isActive) {
      state.chatTag = null;
      $('chatSearch').value = '';
    } else {
      chip.classList.add('is-active');
      state.chatTag = tag;
      $('chatSearch').value = tag;
    }
    loadChats();
  });

  // аккаунты
  $('addAccountBtn').addEventListener('click', () => {
    if (!state.features.account_login_enabled) {
      toast('Вход аккаунтов пока на настройке');
      openBot('add_account');
      return;
    }
    openBot('add_account');
  });
  $('accountList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action="resume-login"]');
    if (!button) return;
    openBot('resume_login');
  });
  $('topUpBtn').addEventListener('click', (event) => {
    // currentTarget, а не target: внутри кнопки может лежать <span>, и тогда
    // индикатор загрузки (withLoading) повесился бы не на ту кнопку.
    payWithStars(event.currentTarget);
  });
  $('distributeBtn').addEventListener('click', (event) => {
    moveBankDays('distribute', event.currentTarget);
  });
  $('freezeBtn').addEventListener('click', (event) => {
    moveBankDays('freeze', event.currentTarget);
  });
  $('freeSubCard').addEventListener('click', () => openBot('trial'));

  // шторки
  $('settingsBtn').addEventListener('click', () => $('settingsSheet').classList.add('is-open'));
  $('settingsList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-start]');
    if (!item) return;
    openBot(item.dataset.start);
  });
  document.querySelectorAll('[data-close]').forEach((node) => {
    node.addEventListener('click', closeSheets);
  });

  // форма задачи: поля и переключатель режима собираются при каждом открытии
  // шторки (у каждой команды свой набор), поэтому слушатели вешаются в bindSheetFields
  $('taskSubmit').addEventListener('click', submitTask);

  // «Повторить» в состоянии ошибки. Один delegated-слушатель на весь документ:
  // блоки ошибки пересоздаются при каждой отрисовке, и вешать слушателя на
  // каждый из них было бы бессмысленно.
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-retry]');
    if (!button) return;
    const name = button.dataset.retry;
    if (name === 'openResults') openResults(state.lastResultsId);
    else if (typeof window[name] === 'function') window[name]();
  });
}

function showDemoBar() {
  const bar = document.createElement('div');
  bar.className = 'demo-bar';
  bar.innerHTML =
    '<span>Демо-данные: кабинет открыт вне Telegram. Внутри бота цифры будут ваши.</span>';
  document.body.insertBefore(bar, document.body.firstChild);
}

async function boot() {
  if (tg) {
    tg.ready();
    tg.expand();
  }
  applyTheme();
  renderSettings();
  bindEvents();
  if (DEMO) showDemoBar();

  try {
    try {
      state.me = await api('/api/me');
      renderHeader();
      // Цена в звёздах приходит с тарифами, поэтому подпись кнопки знает её
      // только здесь — до этого на кнопке нейтральный текст из index.html.
      renderTopUpButton();
    } catch (error) {
      toast(error.message);
    }

    await loadAccounts();
    await loadCommands();
    // Подгружаем задачи по всем трём статусам параллельно — бейджи команд
    // («N активных · M на паузе») считаются по сумме трёх списков, а не
    // только по текущей вкладке, иначе они врут при первом заходе.
    await Promise.all([
      loadTasks('active'),
      loadTasks('paused'),
      loadTasks('done'),
    ]);
  } finally {
    // Заставку убираем в любом случае: если часть запросов упала, пользователь
    // всё равно должен увидеть кабинет и кнопки «Повторить».
    $('boot').classList.add('is-hidden');
  }
}

boot();
