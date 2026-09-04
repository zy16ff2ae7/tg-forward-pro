/* ДОЧА — кабинет автоматизаций Telegram.
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
  // Шаг, на котором сейчас стоит шторка подключения аккаунта. Настоящее
  // состояние входа помнит сервер (строка в pending_logins), здесь лежит
  // только то, что нужно нарисовать: какой вопрос задать и сколько попыток.
  login: { stage: 'phone', phone: '', attemptsLeft: null },
  commands: [],
  commandGroups: [],   // порядок и подписи блоков каталога (из /api/commands)
  commandGroup: null,  // выбранный чипс-фильтр каталога (null — все команды)
  promptHits: [],      // что нашёл умный поиск на «Главной»
  tab: 'home',
  build: '',           // метка сборки — показываем в «Ещё», чтобы видеть, что открыто
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
  // Шторка выбора: чаты для поля формы (mode 'chats') или сохранённые
  // сообщения для рассылки (mode 'library'). Одна шторка на оба случая —
  // список с отметками и поиском у них одинаковый.
  picker: { mode: 'chats', key: null, multi: false, chosen: [], chats: [] },
  library: [],             // сохранённые сообщения (/api/library)
  libraryPick: [],         // id сообщений, выбранных в форме рассылки
  features: {
    account_login_enabled: true,
    account_login_status: 'ready',
  },
};

/* Ключ поля из needs/optional (приходит из /api/commands) → как его нарисовать.
   Порядок полей в шторке задаёт сама команда, а не этот словарь. */
const FIELD_SPEC = {
  account: { label: 'Аккаунт', control: 'select' },
  // pick — у поля есть кнопка «выбрать чат»: 'one' ставит один чат, 'many'
  // собирает список через запятую. Руками вписать тоже можно: поле обычное.
  source: {
    label: 'Источник',
    placeholder: '@channel или ссылка t.me/...',
    note: 'кнопка «выбрать» покажет чаты аккаунта',
    pick: 'one',
  },
  target: {
    label: 'Приёмник',
    placeholder: '@my_channel или ссылка',
    note: 'кнопка «выбрать» покажет чаты аккаунта',
    pick: 'one',
  },
  targets: {
    label: 'Получатели',
    placeholder: '@chan1, @chan2, t.me/+invite',
    note: 'через запятую или кнопкой «выбрать»',
    pick: 'many',
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
    note: 'каждая строка — отдельное сообщение',
  },
  interval: { label: 'Интервал (мин)', control: 'number', placeholder: '2', note: 'минимум 1 минута' },
  start: { label: 'Начало (ЧЧ:ММ)', placeholder: '00:00' },
  end: { label: 'Конец (ЧЧ:ММ)', placeholder: '23:59' },
  gap: { label: 'Пауза между чатами (сек)', control: 'number', placeholder: '5', note: 'быстрее секунды Telegram всё равно не даст' },
  cycle: { label: 'Пауза перед новым кругом (сек)', control: 'number', placeholder: '10' },
  repeats: { label: 'Сколько кругов', control: 'number', placeholder: '1', note: '0 — крутить без конца' },
  typing: { label: 'Показывать «печатает» перед отправкой', control: 'check' },
  random_pick: { label: 'Брать сообщение наугад, а не по очереди', control: 'check' },
};

/* Заголовок шторки результатов для каждого типа задачи. */
const RESULTS_TITLES = {
  parser: 'Собранная аудитория',
  checks: 'Пойманные чеки',
  dialogs: 'Входящие сообщения',
};

const CHAT_TAGS = ['реклама', 'вопросы', 'продажа', 'работа', 'объявления'];

/* Цвет иконки по типу задачи: список команд читается как набор инструментов,
   а не как одна простыня. Классы описаны в styles.css (.ico--*). */
const KIND_ICO = {
  forward: 'ico--pink',
  broadcast: 'ico--blue',
  poster: 'ico--cyan',
  mailing: 'ico--teal',
  parser: 'ico--violet',
  autosubscribe: 'ico--teal',
  checks: 'ico--amber',
  dialogs: 'ico--blue',
  baiting: 'ico--violet',
  mute: 'ico--pink',
};

const icoClass = (kind) => KIND_ICO[kind] || 'ico--pink';

/* Эмодзи по типу задачи — тем же набором, что и в каталоге команд. Держим
   отдельным словарём, а не берём из /api/commands: карточки задач рисуются
   раньше, чем каталог успевает приехать. */
const KIND_EMOJI = {
  forward: '🔁',
  broadcast: '📣',
  poster: '📤',
  mailing: '📨',
  parser: '🕵️',
  autosubscribe: '🤝',
  checks: '🧾',
  dialogs: '💬',
  baiting: '🎣',
  mute: '🔇',
};

const kindEmoji = (kind) => KIND_EMOJI[kind] || '⚙️';

/* Плитки «быстрый старт» на Главной: восемь слотов, последний — весь каталог.
   Подписи короткие: на 390 px в четыре столбца длинное название не влезает. */
const TILES = [
  { id: 'copy_channel', name: 'Пересылка' },
  { id: 'broadcast', name: 'Рассылка' },
  { id: 'poster', name: 'Постинг' },
  { id: 'parser', name: 'Парсер' },
  { id: 'autosubscribe', name: 'Подписка' },
  { id: 'checks', name: 'Чеки' },
  { id: 'dialogs', name: 'Диалоги' },
  { id: null, name: 'Все', emoji: '☰', ico: 'ico--violet', tab: 'commands' },
];

/* Умный поиск по командам работает локально: фраза → слова → команды.
   Никакого внешнего AI и ключей — значит, ничего не стоит и не отваливается.
   Ключевые слова подобраны под то, как о задачах говорят вслух. */
const SMART_WORDS = {
  copy_channel: ['перес', 'копир', 'дубл', 'зеркал', 'репост', 'канал в канал'],
  broadcast: ['рассыл', 'разосл', 'спам', 'всем', 'в чаты', 'массов', 'реклам'],
  poster: ['пост', 'публик', 'по расписан', 'кажд', 'таймер', 'автопост', 'интервал'],
  mailing: ['рассыл', 'разосл', 'по чатам', 'отправ', 'прогрев', 'всем'],
  parser: ['парс', 'собра', 'участник', 'аудитор', 'база', 'юзер', 'подписчик'],
  autosubscribe: ['подпис', 'вступ', 'войти', 'инвайт', 'присоедин'],
  checks: ['чек', 'подар', 'gift', 'ловец', 'халяв', 'промо'],
  dialogs: ['личк', 'диалог', 'входящ', 'сообщен мне', 'уведомл', 'дм'],
  baiting: ['байт', 'реакц', 'эмодзи', 'лайк'],
  mute: ['мут', 'удал', 'модер', 'бан', 'молч'],
};

/* Что показать, когда человек просто открыл поле и ничего не набрал. */
const SMART_FALLBACK = ['copy_channel', 'broadcast', 'poster'];

const SETTINGS = [
  { emoji: '👥', title: 'Рефералы', desc: 'Ссылка, зеркала и выплаты', start: 'referrals' },
  { emoji: '🌐', title: 'Язык', desc: 'Русский', start: 'language' },
  { emoji: '📖', title: 'Гайды', desc: 'Инструкции по основным сценариям', start: 'guides' },
  { emoji: '🛟', title: 'Ресурсы', desc: 'Чат, канал и поддержка', start: 'resources' },
];

/* Экран «Ещё»: то, что убрали из навигации ради короны в центре. Разделы
   кабинета открываются здесь же (data-tab), остальное — в боте (SETTINGS). */
const MORE_ITEMS = [
  { emoji: '👤', title: 'Аккаунты и подписка', desc: 'Номера, копилка дней, оплата', tab: 'accounts' },
  { emoji: '💬', title: 'Чаты', desc: 'Выбрать чаты и запустить задачу по ним', tab: 'chats' },
  { emoji: '📚', title: 'Библиотека сообщений', desc: 'Тексты, которые уходят в рассылку', tab: 'library' },
  { emoji: '📦', title: 'Архив задач', desc: 'Завершённые и остановленные', tab: 'tasks', status: 'done' },
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
      progress: { done: 842, total: null },
      created_at: '2026-08-12T10:20:00' },
    { id: 2, title: 'Афиша → Зеркало афиши', kind: 'forward', kind_label: 'пересылка',
      source: 'Афиша', target: 'Зеркало афиши', archived: false, oneshot: false,
      enabled: true, mode: 'forward', delay: 0, forwarded: 317, account_id: 1,
      progress: { done: 317, total: null },
      created_at: '2026-08-18T09:05:00' },
    { id: 3, title: 'Подборки → Черновики', kind: 'forward', kind_label: 'пересылка',
      source: 'Подборки', target: 'Черновики', archived: false, oneshot: false,
      enabled: false, mode: 'copy', delay: 300, forwarded: 125, account_id: 1,
      progress: { done: 125, total: null },
      created_at: '2026-08-21T18:40:00' },
    { id: 4, title: 'Парсер аудитории: Конкуренты', kind: 'parser', kind_label: 'парсер аудитории',
      source: 'Конкуренты', target: 'Конкуренты', archived: true, oneshot: true,
      enabled: false, mode: 'copy', delay: 0, forwarded: 640, account_id: 1,
      progress: { done: 640, total: 1000 },
      created_at: '2026-08-25T11:00:00' },
  ],
  nextId: 5,
};

/* Демо-каталог повторяет COMMANDS и COMMAND_GROUPS из app/webapp_api.py:
   в демо-режиме кабинет должен выглядеть точно так же, как с сервером. */
const DEMO_COMMAND_GROUPS = [
  { id: 'publish', title: 'пересылка и публикация' },
  { id: 'audience', title: 'аудитория' },
  { id: 'inbox', title: 'входящее' },
  { id: 'moderation', title: 'модерация' },
];

const DEMO_COMMANDS = [
  { id: 'copy_channel', group: 'publish', kind: 'forward', emoji: '🔁', title: 'Копирование канала', status: 'ready',
    needs: ['account', 'source', 'target'], optional: ['mode'],
    description: 'Копирует новые публикации между каналами с заменами текста.' },
  { id: 'broadcast', group: 'publish', kind: 'broadcast', emoji: '📣', title: 'Пересылка в несколько чатов', status: 'ready',
    needs: ['account', 'source', 'target', 'targets'], optional: [],
    description: 'Одно сообщение из источника — в несколько чатов сразу.',
    hint: 'Источник — откуда берём пост, получатели — куда он уйдёт. Чаты отмечайте кнопкой «выбрать» у поля или заранее во вкладке «Чаты».' },
  { id: 'parser', group: 'audience', kind: 'parser', emoji: '🕵️', title: 'Парсер аудитории', status: 'ready',
    needs: ['account', 'source'], optional: ['limit'],
    description: 'Собирает участников чужого чата в список по вашей команде.',
    hint: 'Чат-источник отмечайте кнопкой «выбрать» у поля или заранее во вкладке «Чаты». Запускается сразу, результат — кнопкой «Результаты».' },
  { id: 'autosubscribe', group: 'audience', kind: 'autosubscribe', emoji: '🤝', title: 'Автоподписка', status: 'ready',
    needs: ['account', 'targets'], optional: ['source'],
    description: 'Вступает в каналы из списка и подхватывает ссылки из источника.',
    hint: 'Каналы — через запятую: @chan1, t.me/+invite.' },
  { id: 'checks', group: 'inbox', kind: 'checks', emoji: '🧾', title: 'Ловец чеков', status: 'ready',
    needs: ['account', 'source', 'target'], optional: ['keywords'],
    description: 'Ловит чеки и подарочные ссылки в чатах и складывает в одно место.' },
  { id: 'dialogs', group: 'inbox', kind: 'dialogs', emoji: '💬', title: 'Уведомления из диалогов', status: 'ready',
    needs: ['account', 'target'], optional: ['keywords'],
    description: 'Присылает входящие личные сообщения в выбранный чат.',
    hint: 'Источник не нужен: задача слушает все личные диалоги аккаунта.' },
  { id: 'baiting', group: 'moderation', kind: 'baiting', emoji: '🎣', title: 'Байтинг', status: 'ready',
    needs: ['account', 'source', 'target_user'], optional: ['reaction'],
    description: 'Ставит реакцию на сообщения выбранного человека в общем чате.' },
  { id: 'mute', group: 'moderation', kind: 'mute', emoji: '🔇', title: 'Мут', status: 'ready',
    needs: ['account', 'source', 'target_user'], optional: ['keywords'],
    description: 'Удаляет сообщения выбранного человека в чате, где вы администратор.' },
  { id: 'poster', group: 'publish', kind: 'poster', emoji: '📤', title: 'Авто-постинг', status: 'ready',
    needs: ['account', 'target', 'message'], optional: ['interval', 'start', 'end'],
    description: 'Шлёт ваше сообщение в чат каждые N минут в заданном окне времени.',
    hint: 'Приёмник — куда постить, кнопка «выбрать» покажет чаты аккаунта. Сообщений может быть несколько (каждое с новой строки) — уходят по очереди. Интервал в минутах, окно — ЧЧ:ММ.' },
  { id: 'mailing', group: 'publish', kind: 'mailing', emoji: '📨', title: 'Рассылка по чатам', status: 'ready',
    needs: ['account', 'targets', 'message'], optional: ['gap', 'cycle', 'repeats', 'typing', 'random_pick'],
    description: 'Шлёт ваши сообщения по списку чатов: по одному в круг, с паузой между чатами.',
    hint: 'Получателей отмечайте кнопкой «выбрать» — или заранее во вкладке «Чаты». Сообщения наберите здесь либо возьмите из библиотеки: уходят по очереди, первое — всем, затем второе. Пауза между чатами в секундах, «кругов 0» — крутить без конца.' },
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
      accounts: DEMO_ACCOUNTS.length,
      forwarded: 1284,
    },
    tariffs: { rub: 990, stars: 299, usdt: 12, trial_days: 3, max_rules_free: 3 },
    features: DEMO_FEATURES,
    // Демо показывает оба контура: звёзды внутри Telegram, карта и крипта — на
    // сайте. Ссылку демо не выдаёт: подписать её может только сервер.
    pay: { mode: 'external', inline: ['stars', 'manual'], external: ['yookassa', 'usdt'], url: null },
  };
}

/* Аккаунты и незавершённый вход в демо живут между запросами: иначе демо-вход
   заканчивался бы ничем, а список аккаунтов не показывал бы результат.
   Код и пароль фиксированные — их видно в подсказке под полем. */
const DEMO_ACCOUNTS = [{
  id: 1,
  phone: '+7 916 •••• 12 34',
  is_active: true,
  online: true,
  last_error: null,
  created_at: '2026-08-12T10:00:00',
}];
const DEMO_LOGIN = { pending: null, nextId: 2 };
const DEMO_MAX_ATTEMPTS = 5;
const DEMO_CODE = '11111';
const DEMO_PASSWORD = 'doca';

/* Отказ в демо выглядит как отказ сервера: тот же status, тот же текст.
   Так шторка входа проверяется целиком, включая «осталось попыток». */
function demoFail(status, message, details) {
  const error = new Error(message);
  error.status = status;
  error.data = { error: message, ...(details || {}) };
  throw error;
}

function demoAccounts() {
  const pending = DEMO_LOGIN.pending;
  return {
    accounts: DEMO_ACCOUNTS,
    subscription: {
      active: true,
      until: '2026-09-27T12:00:00',
      days_left: DEMO_BANK.days_left,
      piggy_bank_days: DEMO_BANK.banked,
    },
    pending_login: pending
      ? {
        exists: true,
        phone: pending.phone,
        stage: `waiting_${pending.stage}`,
        step: pending.stage,
        attempts_left: Math.max(DEMO_MAX_ATTEMPTS - pending.attempts, 0),
      }
      : { exists: false, phone: null, stage: null, step: null, attempts_left: null },
    bot_url: 'https://t.me/papina_do4a_bot',
    features: DEMO_FEATURES,
  };
}

const DEMO_CHATS = [
  { id: 1001, title: 'Новости театра', username: 'teatr_news', is_channel: true, is_group: false },
  { id: 1002, title: 'Афиша', username: 'afisha_demo', is_channel: true, is_group: false },
  { id: 1003, title: 'Мой канал', username: 'my_demo_channel', is_channel: true, is_group: false },
  { id: 1004, title: 'Зеркало афиши', is_channel: true, is_group: false },
  { id: 1005, title: 'Подборки', is_channel: false, is_group: false },
  { id: 1006, title: 'Команда (чат)', is_channel: false, is_group: true },
];

function demoChats(path) {
  const query = (new URLSearchParams(path.split('?')[1] || '').get('q') || '').toLowerCase();
  const chats = query ? DEMO_CHATS.filter((c) => c.title.toLowerCase().includes(query)) : DEMO_CHATS;
  return { chats, total: chats.length, online: true };
}

/* Библиотека сообщений в демо: живёт в памяти страницы, как и задачи. */
const DEMO_LIBRARY = {
  nextId: 3,
  items: [
    { id: 2, title: 'Приглашение на спектакль', text: 'Приглашаем на премьеру! Билеты по ссылке в описании.', chat_id: 0, message_id: 0, created_at: '2026-09-03T18:10:00' },
    { id: 1, title: 'Короткое напоминание', text: 'Напоминаем: показ сегодня в 19:00.', chat_id: 0, message_id: 0, created_at: '2026-09-02T09:30:00' },
  ],
};

function demoLibrary(clean, options, method) {
  if (method === 'POST') {
    const body = JSON.parse(options.body || '{}');
    const text = String(body.text || '').trim();
    if (!text) demoFail(400, 'Дайте текст сообщения или ссылку на пост');
    const item = {
      id: DEMO_LIBRARY.nextId++,
      title: (body.title || text).slice(0, 48),
      text,
      chat_id: 0,
      message_id: 0,
      created_at: new Date().toISOString(),
    };
    DEMO_LIBRARY.items.unshift(item);
    return { item };
  }
  if (method === 'DELETE') {
    const id = Number(clean.split('/')[3]);
    const idx = DEMO_LIBRARY.items.findIndex((item) => item.id === id);
    if (idx < 0) demoFail(404, 'Сообщение не найдено');
    DEMO_LIBRARY.items.splice(idx, 1);
    return { ok: true };
  }
  return { items: DEMO_LIBRARY.items };
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
    if (command.kind === 'mailing') {
      const count = splitList(body.targets).length || 1;
      return `Рассылка по чатам: ${count} чат.`;
    }
    if (command.kind === 'broadcast') {
      return `Рассылка: ${body.source} → ${[body.target, ...(body.targets || [])].join(', ')}`;
    }
  }
  return `${body.source} → ${body.target}`;
}

/* Вход в демо повторяет сервер по шагам и по ошибкам: номер → код → пароль 2FA.
   Демо всегда спрашивает пароль, чтобы все три шага можно было увидеть. */
function demoLogin(clean, options) {
  const body = JSON.parse(options.body || '{}');
  const pending = DEMO_LOGIN.pending;

  if (clean === '/api/accounts/login/cancel') {
    DEMO_LOGIN.pending = null;
    return { ok: true, dropped: Boolean(pending) };
  }

  if (clean === '/api/accounts/login/start') {
    const phone = String(body.phone || '').replace(/[\s\-()]+/g, '');
    if (!/^\+?\d{10,15}$/.test(phone)) {
      demoFail(400, 'Нужен номер в международном формате, например +79001234567.');
    }
    DEMO_LOGIN.pending = { phone: phone.startsWith('+') ? phone : `+${phone}`, stage: 'code', attempts: 0 };
    return { stage: 'code', phone: DEMO_LOGIN.pending.phone, attempts_left: DEMO_MAX_ATTEMPTS };
  }

  if (!pending) demoFail(409, 'Незавершённого входа нет. Начните заново: «Подключить аккаунт».');

  if (clean === '/api/accounts/login/code') {
    if (pending.stage !== 'code') demoFail(409, 'Шаг входа не тот: сервис ждёт облачный пароль.');
    const code = String(body.code || '').replace(/\D/g, '');
    if (!code) demoFail(400, 'В коде только цифры — пришлите их подряд, без пробелов.');
    if (code !== DEMO_CODE) {
      pending.attempts += 1;
      const left = DEMO_MAX_ATTEMPTS - pending.attempts;
      if (left <= 0) {
        DEMO_LOGIN.pending = null;
        demoFail(409, 'Код не подошёл слишком много раз. Начните подключение заново.');
      }
      demoFail(400, `Код не подошёл. Осталось попыток: ${left}.`, { attempts_left: left });
    }
    pending.stage = 'password';
    pending.attempts = 0;
    return { stage: 'password', phone: pending.phone };
  }

  // /api/accounts/login/password
  if (pending.stage !== 'password') demoFail(409, 'Шаг входа не тот: сервис ждёт код из Telegram.');
  if (!String(body.password || '').trim()) demoFail(400, 'Пароль пустой.');
  if (String(body.password) !== DEMO_PASSWORD) {
    demoFail(400, 'Пароль не подошёл (PasswordHashInvalidError). Попробуйте снова.');
  }
  const account = {
    id: DEMO_LOGIN.nextId++,
    phone: pending.phone,
    is_active: true,
    online: true,
    last_error: null,
    created_at: new Date().toISOString(),
  };
  DEMO_ACCOUNTS.push(account);
  DEMO_LOGIN.pending = null;
  return { stage: 'done', phone: account.phone, account_id: account.id, name: 'Демо-аккаунт' };
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
      progress: { done: 0, total: kind === 'parser' ? (Number(body.limit) || 200) : null },
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
  if (clean === '/api/commands') return { commands: DEMO_COMMANDS, groups: DEMO_COMMAND_GROUPS };
  if (clean === '/api/accounts') return demoAccounts();
  if (clean.startsWith('/api/accounts/login/')) return demoLogin(clean, options);
  if (clean.startsWith('/api/accounts/') && method === 'DELETE') {
    const id = Number(clean.split('/')[3]);
    const idx = DEMO_ACCOUNTS.findIndex((item) => item.id === id);
    if (idx < 0) demoFail(404, 'Аккаунт не найден');
    const [removed] = DEMO_ACCOUNTS.splice(idx, 1);
    return { ok: true, phone: removed.phone };
  }
  if (clean === '/api/chats') return demoChats(path);
  if (clean === '/api/library' || clean.startsWith('/api/library/')) {
    return demoLibrary(clean, options, method);
  }

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
    // Тело отказа несёт машиночитаемые поля (например, attempts_left) — они
    // нужны обработчику, а вытаскивать их из текста сообщения нельзя.
    error.data = data;
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

/* Кабинет живёт в собственной неоновой палитре (см. :root в styles.css), а не в
   цветах темы Telegram: иначе оформление разъезжалось бы у каждого пользователя.
   Telegram-овские theme_params намеренно НЕ перекрывают брендовые переменные. */
const BRAND_BG = '#0A0510';

function applyTheme() {
  if (!tg) return;
  if (tg.setHeaderColor) tg.setHeaderColor(BRAND_BG);
  if (tg.setBackgroundColor) tg.setBackgroundColor(BRAND_BG);
  if (tg.setBottomBarColor) tg.setBottomBarColor(BRAND_BG);
}

/* ────────────────────────────── Навигация ────────────────────────────── */

const TABS = ['home', 'commands', 'tasks', 'chats', 'accounts', 'library', 'more'];

/* В навигации пять слотов, а экранов больше: «Чаты», «Аккаунты» и «Библиотека»
   открываются из «Ещё» и из мастера задач. Пока открыт такой экран, подсвечиваем
   «Ещё» — иначе панель выглядит так, будто мы никуда не переходили. */
const NAV_FOR_TAB = { chats: 'more', accounts: 'more', library: 'more' };

function switchTab(name) {
  const tab = TABS.includes(name) ? name : 'home';
  state.tab = tab;
  TABS.forEach((item) => {
    $(`tab-${item}`).classList.toggle('hidden', item !== tab);
  });
  const navName = NAV_FOR_TAB[tab] || tab;
  document.querySelectorAll('.nav__item').forEach((item) => {
    const active = item.dataset.tab === navName;
    item.classList.toggle('is-active', active);
    if (active) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  // FAB живёт вне .tab (см. комментарий в index.html), поэтому скрываем
  // его явно — иначе он «прилипнет» поверх любой вкладки.
  const fab = $('addTaskBtn');
  const onTasks = tab === 'tasks';
  fab.classList.toggle('hidden', !onTasks);
  fab.setAttribute('aria-hidden', onTasks ? 'false' : 'true');
  updateChatBar();
  syncFloatingPad();
  if (tab === 'home') renderHome();
  if (tab === 'tasks') loadTasks();
  if (tab === 'chats') loadChats();
  if (tab === 'accounts') loadAccounts();
  if (tab === 'library') loadLibrary();
  // Прокрутка у документа общая: без сброса новый экран открывается с середины.
  window.scrollTo({ top: 0, behavior: 'auto' });
}

/* У нижнего запаса один хозяин: на задачах — FAB, на чатах — панель выбора.
   Иначе две функции наперегонки ставили и снимали отступ, и на задачах он
   пропадал. Высоту берём с живого элемента: она зависит от переносов подписей. */
function syncFloatingPad() {
  const onTasks = !$('tab-tasks').classList.contains('hidden');
  const onChats = !$('tab-chats').classList.contains('hidden');
  const floating = onTasks
    ? $('addTaskBtn')
    : onChats && state.selectedChats.length
    ? $('chatBar')
    : null;
  const content = document.querySelector('.content');
  content.classList.toggle('content--float', Boolean(floating));
  // +26 px — просвет между кнопкой и последней карточкой плюс её отрыв от навигации.
  content.style.setProperty('--float-h', floating ? `${floating.offsetHeight + 26}px` : '0px');
}

/* ─────────────────────────────── Шапка ───────────────────────────────── */

function renderHeader() {
  const me = state.me;
  if (!me) return;
  state.features = me.features || state.features;
  if (!state.features.account_login_enabled) {
    $('headerSub').textContent = 'кабинет готов · вход аккаунтов на настройке';
    setHeroStatus('вход аккаунтов на настройке');
    return;
  }
  const sub = me.subscription || {};
  $('headerSub').textContent = sub.active
    ? `подписка: ${sub.days_left} дн.`
    : 'подписка не активна';
  setHeroStatus(sub.active ? `подписка ${sub.days_left} дн.` : 'подписка не активна');
}

/* Подпись в герой-блоке: то же, что в шапке, но фразой для человека. */
function setHeroStatus(text) {
  const node = $('heroStatus');
  if (node) node.textContent = text;
}

/* ─────────────────────────────── Главная ─────────────────────────────── */

/* Плитки быстрого старта. Команду берём из каталога — если сервер её не отдал
   (выключена флагом), плитку не рисуем вовсе, чтобы не вести в тупик. */
function renderTiles() {
  const holder = $('homeTiles');
  if (!holder) return;
  const items = TILES.map((tile) => {
    if (tile.tab) return tile;
    const command = state.commands.find((item) => item.id === tile.id);
    if (!command) return null;
    return { ...tile, emoji: command.emoji, ico: icoClass(command.kind), title: command.title };
  }).filter(Boolean);

  holder.innerHTML = items
    .map(
      (tile) => `
      <button class="tile" ${tile.tab ? `data-goto="${tile.tab}"` : `data-command="${tile.id}"`}
              title="${esc(tile.title || tile.name)}">
        <span class="tile__ico ${tile.ico}" aria-hidden="true">${tile.emoji}</span>
        <span class="tile__name">${esc(tile.name)}</span>
      </button>`
    )
    .join('');
}

/* Главная показывает три задачи, которые реально идут прямо сейчас: остальное
   живёт на своей вкладке. Пусто — значит пусто, выдуманных карточек нет. */
function renderHomeTasks() {
  const holder = $('homeTasks');
  if (!holder) return;
  const active = (state.tasksByStatus.active || []).slice(0, 3);
  if (!active.length) {
    const paused = (state.tasksByStatus.paused || []).length;
    holder.innerHTML = emptyHtml(
      '🌙',
      'Пока ничего не работает',
      paused
        ? `${paused} задач(и) на паузе — снимите с паузы или создайте новую.`
        : 'Нажмите корону внизу или «создать задачу» — соберём первую вместе.'
    );
    return;
  }
  holder.innerHTML = active.map((task) => taskCardHtml(task, { compact: true })).join('');
}

function renderHome() {
  renderTiles();
  renderHomeTasks();
}

/* ────────────────── Умный поиск по командам (локальный) ───────────────── */

/* Считаем совпадения фразы с ключевыми словами команд и с их названиями.
   Никакого внешнего AI: разбор строчный, работает офлайн и мгновенно. */
function smartMatch(query) {
  const text = String(query || '').toLowerCase().trim();
  if (!text) {
    return SMART_FALLBACK.map((id) => state.commands.find((item) => item.id === id))
      .filter(Boolean)
      .map((command) => ({ command, why: 'чаще всего запускают' }));
  }

  const words = text.split(/[^a-zа-яё0-9@+]+/i).filter((word) => word.length > 2);
  const scored = state.commands.map((command) => {
    const hints = SMART_WORDS[command.id] || [];
    let score = 0;
    let why = '';
    hints.forEach((hint) => {
      if (text.includes(hint)) {
        score += 3;
        // Показывать сам стем («похоже на «собра…»») бессмысленно — человек
        // видит обрубок слова и не понимает, что произошло.
        if (!why) why = 'по вашей фразе';
      }
    });
    const title = command.title.toLowerCase();
    const description = (command.description || '').toLowerCase();
    words.forEach((word) => {
      if (title.includes(word)) {
        score += 2;
        if (!why) why = 'совпало название';
      } else if (description.includes(word)) {
        score += 1;
        if (!why) why = 'совпало описание';
      }
    });
    return { command, score, why: why || 'по описанию' };
  });

  const ranked = scored
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
  // Если фраза попала по смыслу (стем-подсказка — это 3 балла), слабые
  // однословные совпадения только мешают выбрать: «собрать участников чата»
  // не должно тянуть за собой «Рассылку по чатам» из-за слова «чата».
  const strong = ranked.filter((item) => item.score >= 3);
  return (strong.length ? strong : ranked).slice(0, 3);
}

function renderPromptHits() {
  const holder = $('promptHits');
  if (!holder) return;
  const query = ($('promptInput').value || '').trim();
  // Пустое поле — молчим: подсказки под пустым вопросом выглядят как мусор.
  if (!query) {
    state.promptHits = [];
    holder.innerHTML = '';
    return;
  }
  state.promptHits = smartMatch(query);
  if (!state.promptHits.length) {
    holder.innerHTML = `
      <button class="prompt-hit" data-goto="commands">
        <span class="tile__ico ico--violet" aria-hidden="true">🔍</span>
        <span>
          <span class="prompt-hit__title">Не подобрала команду</span><br>
          <span class="prompt-hit__why">Откройте каталог — там все девять</span>
        </span>
      </button>`;
    return;
  }
  holder.innerHTML = state.promptHits
    .map(
      ({ command, why }) => `
      <button class="prompt-hit" data-command="${command.id}">
        <span class="tile__ico ${icoClass(command.kind)}" aria-hidden="true">${command.emoji}</span>
        <span>
          <span class="prompt-hit__title">${esc(command.title)}</span><br>
          <span class="prompt-hit__why">${esc(why)}</span>
        </span>
      </button>`
    )
    .join('');
}

/* Enter в поле — сразу открыть лучшую догадку: лишний тап не нужен. */
function runSmartSearch() {
  const query = ($('promptInput').value || '').trim();
  if (!query) {
    switchTab('commands');
    return;
  }
  const hits = smartMatch(query);
  if (!hits.length) {
    $('commandSearch').value = query;
    switchTab('commands');
    renderCommands();
    return;
  }
  openCommand(hits[0].command.id);
}

/* Открыть команду по id: одна дорога для плиток, подсказок и каталога. */
function openCommand(id) {
  const command = state.commands.find((item) => item.id === id);
  if (!command) {
    toast('Каталог команд ещё не загружен');
    return;
  }
  if (command.status === 'ready') {
    openTaskSheet(command);
  } else if (command.status === 'setup_required') {
    toast('Подключение аккаунтов на настройке. Кабинет работает, команды включатся вместе с ним.');
  } else {
    toast(`${command.title} — выключено в настройках сервиса`);
  }
}

/* ─────────────────────────────── Команды ─────────────────────────────── */

async function loadCommands() {
  const holder = $('commandList');
  beginLoad(holder, 'card', 4);
  try {
    const data = await api('/api/commands');
    state.commands = data.commands;
    state.commandGroups = data.groups || [];
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

/* Карточка команды: эмодзи, название, описание, статус — по одной строке
   каждое. Раньше статус стоял рядом с названием и на узком экране срывался на
   вторую строку, разъезжая с эмодзи и шевроном; подсказка «как пользоваться»
   лежала прямо в списке и делала его нечитаемым. Подсказка теперь живёт в
   шторке команды (там она и нужна — при заполнении полей). */
function commandCardHtml(command) {
  const meta = computeCommandState(command);
  // Карточка кликабельна ТОЛЬКО если команда доступна на сервере.
  // «На настройке» — карточка остаётся видимой, но реагирует тостом.
  const clickable = command.status === 'ready';
  const tag = `card--cmd${clickable ? '' : ' card--cmd--locked'}`;
  return `
    <button class="${tag}" data-command="${command.id}">
      <div class="cmd__ico ${icoClass(command.kind)}" aria-hidden="true">${command.emoji}</div>
      <div class="cmd__main">
        <div class="cmd__title">${esc(command.title)}</div>
        <div class="cmd__desc">${esc(command.description)}</div>
        <div class="cmd__foot">
          <span class="status status--${meta.kind}">
            <span class="status__dot" aria-hidden="true"></span>${esc(meta.label)}
          </span>
        </div>
      </div>
      <span class="cmd__chevron" aria-hidden="true">›</span>
    </button>`;
}

/* Чипсы каталога: «все» + блоки, которые прислал сервер. Счётчик в чипсе
   считается по тем же данным, что и список, — расходиться им негде. */
function renderCommandTags() {
  const holder = $('commandTags');
  if (!holder) return;
  const groups = state.commandGroups;
  if (!groups.length) {
    holder.innerHTML = '';
    return;
  }
  const chip = (id, title, count) => `
    <button class="chip${state.commandGroup === id ? ' is-active' : ''}"
            data-group="${id || ''}" role="tab"
            aria-selected="${state.commandGroup === id ? 'true' : 'false'}">
      ${esc(title)} <b>${count}</b>
    </button>`;
  holder.innerHTML =
    chip(null, 'все', state.commands.length) +
    groups
      .map((group) =>
        chip(group.id, group.title, state.commands.filter((item) => item.group === group.id).length)
      )
      .join('');
}

function renderCommands() {
  const query = ($('commandSearch').value || '').toLowerCase();
  const list = state.commands.filter(
    (command) =>
      (!state.commandGroup || command.group === state.commandGroup) &&
      (!query ||
        command.title.toLowerCase().includes(query) ||
        command.description.toLowerCase().includes(query))
  );
  renderCommandTags();

  if (!list.length) {
    $('commandList').innerHTML = emptyHtml(
      '🔍',
      'Ничего не найдено',
      state.commandGroup ? 'Попробуйте другой запрос или снимите фильтр' : 'Попробуйте другой запрос'
    );
    return;
  }

  // Блоки идут в порядке, который задал сервер; пустые (например, всё
  // отфильтровано поиском) не рисуем вовсе — заголовок без карточек не нужен.
  // При выбранном чипсе заголовки не нужны: блок ровно один, и он уже подписан.
  const byGroups = Boolean(state.commandGroups.length) && !state.commandGroup;
  const groups = byGroups ? state.commandGroups : [{ id: null, title: '' }];
  const blocks = groups.map((group) => ({
    title: group.title,
    items: list.filter((command) => (group.id ? command.group === group.id : true)),
  }));
  // Команда с неизвестной группой (сервер завёл новую, кабинет ещё не знает)
  // не должна пропадать из каталога — собираем такие в «прочее». При активном
  // чипсе этого блока нет: единственный блок уже содержит все карточки.
  if (byGroups) {
    const known = new Set(state.commandGroups.map((group) => group.id));
    const rest = list.filter((command) => !known.has(command.group));
    if (rest.length) blocks.push({ title: 'прочее', items: rest });
  }

  $('commandList').innerHTML = blocks
    .filter((block) => block.items.length)
    .map(
      (block) =>
        (block.title ? `<div class="section-label">${esc(block.title)}</div>` : '') +
        block.items.map(commandCardHtml).join('')
    )
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
    // «Главная» показывает активные задачи — обновляем её вместе со списком.
    if (status === 'active') renderHomeTasks();
    renderCommands();
  } catch (error) {
    if (status === state.taskStatus) failLoad(holder, error, 'loadTasks');
  }
}

/* Переключение сегмента «Активные / На паузе / Завершённые». Отдельной
   функцией, потому что дорога сюда не одна: сам сегмент, «Архив задач» из
   «Ещё» и обновление после действий над задачей. */
function setTaskStatus(status) {
  state.taskStatus = status;
  document.querySelectorAll('#taskStatus .seg').forEach((seg) => {
    const active = seg.dataset.status === status;
    seg.classList.toggle('is-active', active);
    seg.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  // Если данные для этого статуса уже подгружены (агрегатор на boot),
  // показываем их без сети; иначе подгружаем.
  const cached = state.tasksByStatus[status];
  if (cached && cached.length) renderTasks(cached);
  else loadTasks();
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

/* Метка состояния задачи. «Нет связи» важнее «работает»: включённая задача при
   отключённом аккаунте не делает ничего, и об этом надо сказать прямо.
   Сравнение строгое (=== false): в демо-данных поля просто нет. */
function taskBadge(task) {
  if (task.archived) return { kind: 'done', label: 'завершена' };
  if (!task.enabled) return { kind: 'paused', label: 'пауза' };
  if (task.account_online === false) return { kind: 'error', label: 'нет связи' };
  if (task.oneshot) return { kind: 'plan', label: 'по кнопке' };
  return { kind: 'live', label: 'работает' };
}

/* Строка под названием: что это за задача и как настроена. */
function taskMetaLines(task) {
  const kind = task.kind || 'forward';
  const isForward = kind === 'forward';
  const lines = [task.kind_label || (isForward ? 'пересылка' : kind)];
  if (isForward) lines.push(task.mode === 'copy' ? 'копия без метки' : 'обычный форвард');
  if (kind === 'poster') {
    // Авто-постер: показываем расписание вместо «задержки в секундах».
    lines.push(`каждые ${task.interval_min || 1} мин`);
    if (task.window_start && task.window_end) {
      lines.push(`окно ${task.window_start}–${task.window_end}`);
    }
    if (task.messages_count) lines.push(`${task.messages_count} сообщ.`);
  } else if (kind === 'mailing') {
    // Рассылка: её расписание — это паузы и число кругов, а не «задержка».
    const info = task.mailing || {};
    if (info.recipients) lines.push(`${info.recipients} чат.`);
    lines.push(`пауза ${info.gap_seconds || 5} сек`);
    if (info.messages_count) lines.push(`${info.messages_count} сообщ.`);
    lines.push(info.repeats ? `${info.repeats} круг(ов)` : 'круги без конца');
  } else if (task.oneshot) {
    lines.push('запуск по кнопке');
  } else {
    lines.push(`задержка ${task.delay} сек`);
  }
  if (kind === 'broadcast' && task.targets_count) lines.push(`${task.targets_count} получат.`);
  return lines;
}

/* Полоса прогресса. Долю рисуем только там, где сервер знает «сколько всего»
   (парсер — из лимита, автоподписка — из списка ссылок). У постоянных задач
   конца нет, и вместо выдуманной доли идёт бегунок: честнее пустой шкалы. */
function taskProgressHtml(task) {
  const progress = task.progress || {};
  const done = Number(progress.done || 0);
  const total = Number(progress.total || 0);
  if (!total) {
    return `
      <div class="task__progress task__progress--endless"><span></span></div>
      <div class="task__nums"><b>${done}</b><i>обработано</i></div>`;
  }
  const pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
  return `
    <div class="task__progress"><span style="width:${pct}%"></span></div>
    <div class="task__nums">
      <b>${done}</b><i>из ${total}</i><span class="task__pct">${pct}%</span>
    </div>`;
}

/* Главная кнопка карточки: «Пауза» для постоянных задач и «Запустить» для
   разовых. Вынесена отдельно, потому что нужна и полной карточке, и краткой
   на «Главной» — двух разных реализаций тут быть не должно. */
function taskPauseButton(task) {
  if (task.oneshot) {
    return `<button class="btn" data-action="run" data-id="${task.id}">▶️ Запустить</button>`;
  }
  return (
    `<button class="btn" data-action="toggle" data-id="${task.id}">` +
    (task.enabled ? '⏸ Пауза' : '▶️ Запустить') +
    '</button>'
  );
}

/* Кнопки карточки. Один и тот же набор работает и на «Задачах», и на
   «Главной»: слушатель делегирован на оба списка (см. bindEvents). */
function taskActionsHtml(task) {
  const kind = task.kind || 'forward';
  const acts = [];
  if (task.archived) {
    acts.push(`<button class="btn" data-action="unarchive" data-id="${task.id}">↩︎ Из архива</button>`);
  } else {
    acts.push(taskPauseButton(task));
    if (kind === 'forward') {
      acts.push(`<button class="btn" data-action="mode" data-id="${task.id}">🔁 Режим</button>`);
    }
    if (RESULTS_TITLES[kind] || task.oneshot) {
      acts.push(`<button class="btn" data-action="results" data-id="${task.id}">📄 Результаты</button>`);
    }
    acts.push(`<button class="btn" data-action="archive" data-id="${task.id}">📦 Архив</button>`);
  }
  // Корзина стоит отдельным столбцом, а не в общем ряду: иначе при переносе
  // она уезжала на пустую строку одна, и карточка выглядела оборванной.
  return `
    <div class="task__actions">
      <div class="task__acts">${acts.join('')}</div>
      <button class="btn btn--danger" data-action="delete" data-id="${task.id}">🗑</button>
    </div>`;
}

function taskCardHtml(task, options) {
  const kind = task.kind || 'forward';
  const badge = taskBadge(task);
  // На «Главной» карточка — только сводка: полный набор кнопок живёт на вкладке
  // «Задачи», иначе главный экран превращается в её копию. Пауза остаётся —
  // это то действие, которое нужно срочно и на бегу.
  const compact = Boolean(options && options.compact);
  const actions = compact
    ? `<div class="task__actions">
         <div class="task__acts">${taskPauseButton(task)}</div>
         <button class="btn" data-goto="tasks" type="button">открыть</button>
       </div>`
    : taskActionsHtml(task);
  return `
    <div class="task${task.archived ? ' task--archived' : ''}">
      <div class="task__top">
        <span class="task__ico ${icoClass(kind)}" aria-hidden="true">${task.emoji || kindEmoji(kind)}</span>
        <div class="task__head">
          <div class="task__title">${esc(task.title)}</div>
          <div class="task__meta">${esc(taskMetaLines(task).join(' · '))}</div>
        </div>
        <span class="badge badge--${badge.kind}">${badge.label}</span>
      </div>
      ${taskProgressHtml(task)}
      ${actions}
    </div>`;
}

function renderTasks(tasks) {
  const holder = $('taskList');
  // Подпись экрана честно считает по всем трём спискам, а не по видимому.
  const counts = state.tasksByStatus;
  $('taskSummary').textContent =
    `${(counts.active || []).length} работают · ${(counts.paused || []).length} на паузе · ` +
    `${(counts.done || []).length} в архиве`;

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

  holder.innerHTML = tasks.map(taskCardHtml).join('');
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
      const active = seg.dataset.status === visible;
      seg.classList.toggle('is-active', active);
      seg.setAttribute('aria-selected', active ? 'true' : 'false');
    });
  }
  renderTasks(state.tasksByStatus[visible]);
  renderHomeTasks();
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
  const onChats = !$('tab-chats').classList.contains('hidden');
  const shown = onChats && n > 0;
  // Панель вынесена из вкладки (см. index.html), поэтому вместе с вкладкой уже
  // не исчезает — гасим руками, иначе она всплывёт поверх задач и аккаунтов.
  bar.classList.toggle('hidden', !onChats);
  bar.classList.toggle('is-visible', shown);
  bar.setAttribute('aria-hidden', shown ? 'false' : 'true');
  const countEl = $('chatBarCount');
  if (countEl) countEl.textContent = String(n);
  const sumEl = $('chatBarSummary');
  if (sumEl) sumEl.textContent = n ? selectedChatsSummary() : '';
  // Панель висит поверх списка, как и FAB: пока она видна, последнему чату
  // нужен запас снизу, иначе выбранный чат прячется под своей же кнопкой.
  // Считаем после подписи — от неё зависит высота панели.
  syncFloatingPad();
}

/* Тип чата словами, а не только картинкой: «канал» и «группа» ведут себя
   по-разному (в канал нужно право публиковать, из группы можно собрать людей),
   и по одному эмодзи это не читается. */
function chatKind(chat) {
  if (chat.is_channel) return { emoji: '📢', label: 'канал' };
  if (chat.is_group) return { emoji: '👥', label: 'группа' };
  return { emoji: '💬', label: 'диалог' };
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
      const kind = chatKind(chat);
      return `
        <button class="chat${selected ? ' is-selected' : ''}" data-chat-id="${chat.id}" type="button">
          <div class="chat__emoji">${kind.emoji}</div>
          <div class="chat__body">
            <div class="chat__title">${esc(chat.title)}</div>
            <div class="chat__sub"><code>${chat.id}</code>${chat.username ? ' · @' + esc(chat.username) : ''}</div>
          </div>
          <span class="chat__kind">${kind.label}</span>
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
  } else if (command.kind === 'mailing') {
    // У рассылки по чатам поля «приёмник» нет: все выбранные чаты — получатели,
    // даже если выбран один. Иначе форма открывалась бы пустой.
    prefill.targets = refs.join(', ');
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

/* ─────────────────────── Библиотека сообщений ────────────────────────── */

/* Тексты, которые рассылает «Рассылка по чатам». Экран кабинета вместо двух
   пунктов настроек («Сообщения» и «Библиотека сообщений»), которые лишь
   открывали бота и читались как одно и то же. */

async function loadLibrary() {
  const holder = $('libraryList');
  beginLoad(holder, 'plain', 3);
  try {
    const data = await api('/api/library');
    endLoad(holder);
    state.library = data.items || [];
    renderLibrary();
  } catch (error) {
    failLoad(holder, error, 'loadLibrary');
  }
}

function libraryPreview(item, max = 90) {
  const text = (item.text || '').replace(/\s+/g, ' ').trim();
  if (text) return text.length > max ? `${text.slice(0, max)}…` : text;
  // Пост из канала лежит ссылкой (chat_id + message_id), своего текста у него нет.
  return item.message_id ? `готовый пост · ${item.chat_id}/${item.message_id}` : 'пустое сообщение';
}

/* Две строки записи библиотеки. Заголовок у сохранённых из формы — это начало
   того же текста, и печатать одну строку дважды незачем: вторую показываем
   только когда заголовок — настоящее имя, а не обрезок текста. */
function libraryLines(item, max = 90) {
  const preview = libraryPreview(item, max);
  const title = (item.title || '').trim();
  const sameThing = !title || preview === title || preview.startsWith(title);
  return sameThing ? { head: preview, sub: '' } : { head: title, sub: preview };
}

function renderLibrary() {
  const holder = $('libraryList');
  const label = $('libraryLabel');
  if (label) {
    label.textContent = state.library.length
      ? `сохранённые · ${state.library.length}`
      : 'сохранённые';
  }
  if (!state.library.length) {
    holder.innerHTML = emptyHtml(
      '📚',
      'Библиотека пуста',
      'Добавьте первый текст — он появится в выборе сообщений у рассылки.'
    );
    return;
  }
  holder.innerHTML = state.library.map((item) => {
    const { head, sub } = libraryLines(item);
    return `
    <div class="lib">
      <div class="lib__body">
        <div class="lib__title">${esc(head)}</div>
        ${sub ? `<div class="lib__text">${esc(sub)}</div>` : ''}
      </div>
      <button class="lib__del" data-action="delete-library" data-id="${item.id}"
              aria-label="Удалить сообщение" title="Удалить из библиотеки">🗑</button>
    </div>`;
  }).join('');
}

/* Каждая строка — отдельное сообщение: так же читает многострочный ввод
   рассылка, и человеку не приходится жать «сохранить» по разу на текст. */
async function addLibraryItems(button) {
  const field = $('libraryText');
  const lines = String(field.value || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  if (!lines.length) {
    toast('Напишите текст сообщения');
    field.focus();
    return;
  }
  try {
    await withLoading(button, async () => {
      for (const text of lines) {
        await api('/api/library', {
          method: 'POST',
          body: JSON.stringify({ text, title: text.slice(0, 48) }),
        });
      }
    });
    field.value = '';
    toast(lines.length === 1 ? 'Сообщение сохранено' : `Сохранено сообщений: ${lines.length}`);
    await loadLibrary();
  } catch (error) {
    toast(error.message);
  }
}

async function deleteLibraryItem(id, button) {
  const agreed = await confirmAction('Убрать сообщение из библиотеки? Задачи не остановятся — рассылка возьмёт то, что осталось.');
  if (!agreed) return;
  try {
    await withLoading(button, () => api(`/api/library/${id}`, { method: 'DELETE' }));
    // Выбор в открытой форме тоже чистим: id больше не существует.
    state.libraryPick = state.libraryPick.filter((item) => String(item) !== String(id));
    renderLibraryPicks();
    await loadLibrary();
  } catch (error) {
    toast(error.message);
  }
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

  const pending = state.pendingLogin;
  const pendingHtml = pending && pending.exists
    ? `<button class="card card--add card--resume" data-action="resume-login">
         ▶️ Продолжить вход ${esc(pending.phone || '')} · ${pending.step === 'password' ? 'ждём пароль 2FA' : 'ждём код'}
       </button>`
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
        <button class="account__del" data-action="delete-account" data-id="${account.id}"
                aria-label="Отключить аккаунт ${esc(account.phone)}" title="Отключить аккаунт">🗑</button>
      </div>`
    )
    .join('');
}

/* ────────────────── Шторка подключения аккаунта ──────────────────────── */

/* Вход проходит целиком в кабинете: номер → код из Telegram → облачный пароль.
   Раньше кнопка «Добавить аккаунт» умела только открыть чат с ботом, и человек
   уходил из мини-аппа на середине пути. Шаг помнит сервер (строка в БД), так
   что закрытая шторка, перезапуск бота и переход в бота ничего не теряют. */
const LOGIN_STEPS = {
  phone: {
    title: 'Подключение аккаунта',
    lead: 'Номер того аккаунта, который будет читать источники и пересылать посты.',
    label: 'Номер телефона',
    placeholder: '+79001234567',
    type: 'tel',
    inputmode: 'tel',
    submit: 'Получить код',
    note: 'Международный формат, например +79001234567.',
  },
  code: {
    title: 'Код из Telegram',
    lead: 'Код пришёл в чат «Telegram» в приложении, а не по SMS.',
    label: 'Код подтверждения',
    placeholder: '12345',
    type: 'text',
    inputmode: 'numeric',
    submit: 'Подтвердить код',
    note: 'Код вида 1 2 3 4 5 — это 12345, без пробелов.',
  },
  password: {
    title: 'Облачный пароль',
    lead: 'На аккаунте включена двухэтапная проверка. Сервис хранит только полученную сессию, сам пароль не сохраняется.',
    label: 'Пароль 2FA',
    placeholder: 'Облачный пароль Telegram',
    type: 'password',
    inputmode: 'text',
    submit: 'Войти',
    note: '',
  },
};
const LOGIN_ORDER = ['phone', 'code', 'password'];
const LOGIN_PATHS = { phone: 'start', code: 'code', password: 'password' };
const LOGIN_FIELDS = { phone: 'phone', code: 'code', password: 'password' };

function loginReset() {
  state.login = { stage: 'phone', phone: '', attemptsLeft: null };
}

function openLoginSheet() {
  if (!state.features.account_login_enabled) {
    toast('Вход аккаунтов пока на настройке');
    return;
  }
  const pending = state.pendingLogin;
  if (pending && pending.exists) {
    state.login = {
      stage: pending.step || 'code',
      phone: pending.phone || '',
      attemptsLeft: pending.attempts_left != null ? pending.attempts_left : null,
    };
  } else {
    loginReset();
  }
  renderLoginStage();
  $('loginSheet').classList.add('is-open');
  // Фокус — после появления шторки: пока она скрыта, клавиатура не поднимется.
  setTimeout(() => $('loginInput').focus(), 220);
}

function renderLoginStage(message) {
  const { stage, phone, attemptsLeft } = state.login;
  const spec = LOGIN_STEPS[stage] || LOGIN_STEPS.phone;
  const lead = stage !== 'phone' && phone ? `Номер ${phone}. ${spec.lead}` : spec.lead;
  const notes = [spec.note];
  if (stage === 'code' && attemptsLeft != null) notes.push(`Осталось попыток: ${attemptsLeft}.`);
  if (DEMO) notes.push(`Демо: код ${DEMO_CODE}, пароль ${DEMO_PASSWORD}.`);

  $('loginTitle').textContent = spec.title;
  $('loginLead').textContent = lead;
  $('loginLabel').textContent = spec.label;
  $('loginSubmit').textContent = spec.submit;
  $('loginNote').textContent = notes.filter(Boolean).join(' ');
  $('loginError').textContent = message || '';
  $('loginRestart').hidden = stage === 'phone';

  const input = $('loginInput');
  input.type = spec.type;
  input.inputMode = spec.inputmode;
  input.placeholder = spec.placeholder;
  input.value = stage === 'phone' ? phone : '';

  const current = LOGIN_ORDER.indexOf(stage);
  document.querySelectorAll('#loginSteps .steps__item').forEach((item) => {
    const idx = LOGIN_ORDER.indexOf(item.dataset.step);
    item.classList.toggle('is-done', idx < current);
    item.classList.toggle('is-current', idx === current);
  });
}

async function submitLogin() {
  const stage = state.login.stage;
  const value = ($('loginInput').value || '').trim();
  if (!value) {
    renderLoginStage(stage === 'phone' ? 'Введите номер телефона.' : 'Поле пустое.');
    return;
  }
  if (stage === 'phone') state.login.phone = value;

  try {
    const step = await withLoading($('loginSubmit'), () =>
      api(`/api/accounts/login/${LOGIN_PATHS[stage]}`, {
        method: 'POST',
        body: JSON.stringify({ [LOGIN_FIELDS[stage]]: value }),
      })
    );
    await applyLoginStep(step);
  } catch (error) {
    await loginFailed(error);
  }
}

/* Ответ сервера — это и есть следующий вопрос: code → password → done. */
async function applyLoginStep(step) {
  if (!step || !step.stage) return;
  if (step.stage === 'done') {
    closeSheets();
    loginReset();
    toast(`Аккаунт ${step.phone || ''} подключён`.replace('  ', ' '));
    await loadAccounts();
    return;
  }
  const wasStage = state.login.stage;
  state.login = {
    stage: step.stage,
    phone: step.phone || state.login.phone,
    attemptsLeft: step.attempts_left != null ? step.attempts_left : null,
  };
  renderLoginStage();
  if (step.stage === 'code' && wasStage === 'phone') toast('Код отправлен в Telegram');
  $('loginInput').focus();
}

/* 400 — ввод не подошёл, шаг тот же: опечатка в цифре кода не должна выбрасывать
   человека в начало (код-то ещё живой). 503 — шлюза нет, входить некуда.
   Остальное (409: код устарел, попытки кончились, сессия побилась) — вход
   закончился, возвращаемся к номеру и сверяем состояние с сервером. */
async function loginFailed(error) {
  if (error.status === 400) {
    // Остаток попыток берём из ответа: иначе подсказка под полем осталась бы с
    // прежним числом и спорила бы с текстом ошибки.
    const left = error.data ? error.data.attempts_left : null;
    if (left != null) state.login.attemptsLeft = left;
    renderLoginStage(error.message);
    $('loginInput').focus();
    $('loginInput').select();
    return;
  }
  if (error.status === 503) {
    closeSheets();
    toast(error.message);
    await loadAccounts();
    return;
  }
  loginReset();
  renderLoginStage(error.message);
  await loadAccounts();
}

/* «Другой номер» — забыть незавершённый вход и начать с первого шага. */
async function restartLogin() {
  try {
    await withLoading($('loginRestart'), () =>
      api('/api/accounts/login/cancel', { method: 'POST' })
    );
  } catch (error) {
    toast(error.message);
  }
  loginReset();
  state.pendingLogin = null;
  renderLoginStage();
  $('loginInput').focus();
  await loadAccounts();
}

/* Отключение аккаунта необратимо: сессия стирается, задачи на нём встают.
   Поэтому спрашиваем подтверждение — нативным окном Telegram, если оно есть. */
async function deleteAccount(id, button) {
  const account = state.accounts.find((item) => item.id === Number(id));
  const phone = account ? account.phone : `ID ${id}`;
  const agreed = await confirmAction(
    `Отключить аккаунт ${phone}? Сохранённая сессия будет удалена, задачи на этом аккаунте остановятся.`
  );
  if (!agreed) return;
  try {
    const data = await withLoading(button, () => api(`/api/accounts/${id}`, { method: 'DELETE' }));
    toast(`Аккаунт ${data.phone || phone} отключён`);
    await loadAccounts();
    await refreshAllTaskLists();
  } catch (error) {
    toast(error.message);
  }
}

function confirmAction(question) {
  if (tg && tg.showConfirm) {
    return new Promise((resolve) => tg.showConfirm(question, (ok) => resolve(Boolean(ok))));
  }
  return Promise.resolve(window.confirm(question));
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
    // Рассылка берёт тексты из библиотеки, поэтому у её поля есть кнопка
    // выбора: перепечатывать сохранённое не нужно. Другим командам библиотека
    // не положена — они читают только это поле.
    const fromLibrary = key === 'message'
      && state.activeCommand
      && state.activeCommand.kind === 'mailing';
    return `<label class="field"><span>${spec.label}</span>
      <textarea id="task_${key}" rows="4" placeholder="${esc(spec.placeholder || '')}"></textarea>
      ${spec.note ? `<i class="field__note">${esc(spec.note)}</i>` : ''}
      ${fromLibrary ? `<div class="field__aside">
        <button type="button" class="btn btn--pick" data-pick-library="1">📚 из библиотеки</button>
      </div>
      <div class="picks" id="libraryPicks"></div>` : ''}
    </label>`;
  }
  if (spec.control === 'check') {
    return `<label class="field field--check">
      <input type="checkbox" id="task_${key}">
      <span>${spec.label}</span>
      ${spec.note ? `<i class="field__note">${esc(spec.note)}</i>` : ''}
    </label>`;
  }
  const type = spec.control === 'number' ? 'number' : 'text';
  const input = `<input type="${type}" id="task_${key}"
      placeholder="${esc(spec.placeholder || '')}" autocomplete="off">`;
  const note = spec.note ? `<i class="field__note">${esc(spec.note)}</i>` : '';
  // Поле с выбором чата: рядом с ним кнопка, которая открывает список чатов
  // аккаунта. Раньше единственным способом было вписать @username или id.
  if (spec.pick) {
    return `<label class="field"><span>${spec.label}</span>
      <div class="field__row">
        ${input}
        <button type="button" class="btn btn--pick" data-pick="${key}"
                data-multi="${spec.pick === 'many' ? '1' : ''}">💬 выбрать</button>
      </div>
      ${note}
    </label>`;
  }
  return `<label class="field"><span>${spec.label}</span>
    ${input}
    ${note}
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
  if (spec.control === 'check') return node ? node.checked : false;
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
    openLoginSheet();
    return;
  }

  state.activeCommand = command
    || state.commands.find((item) => item.id === 'copy_channel')
    || { id: 'copy_channel', kind: 'forward', title: 'Копирование канала', emoji: '🔁', needs: ['account', 'source', 'target'], optional: ['mode'] };
  state.mode = 'copy';
  // Выбор из библиотеки живёт ровно одну форму: чужой выбор в новой задаче
  // молча отправил бы не те сообщения.
  state.libraryPick = [];

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

/* ─────────────── Выбор мышкой: чаты и сообщения для поля ─────────────── */

/* Кнопка «💬 выбрать» у полей источника, приёмника и получателей и кнопка
   «📚 из библиотеки» у поля сообщения. До этого чат в форме можно было только
   вписать руками — @username или числовой id, — и задача падала на любой
   опечатке. Шторка одна на оба случая: список с отметками и поиск у них
   одинаковые, а два почти одинаковых экрана расходятся при первой же правке.
   Чаты берём те же, что на вкладке «Чаты»: /api/chats того аккаунта, который
   выбран в самой форме. */

/* Аккаунт для списка чатов: тот, что стоит в форме. Селект может быть ещё
   пустым (аккаунт один и не онлайн) — тогда берём первый известный. */
function pickerAccount() {
  const select = $('taskAccount');
  const id = select ? Number(select.value) : 0;
  return state.accounts.find((item) => item.id === id) || state.accounts[0] || null;
}

function openFieldPicker(key, multi) {
  if (!pickerAccount()) {
    toast('Сначала подключите аккаунт');
    return;
  }
  // Уже вписанное руками не теряем: разбираем поле на ссылки и отмечаем их.
  const current = splitList(fieldValue(key));
  state.picker = {
    mode: 'chats',
    key,
    multi: Boolean(multi),
    chosen: multi ? current : current.slice(0, 1),
    chats: [],
  };
  $('pickerTitle').textContent = multi ? 'Выбор чатов' : 'Выбор чата';
  $('pickerSearch').value = '';
  renderPickerFooter();
  $('pickerSheet').classList.add('is-open');
  loadPickerChats();
}

/* Сообщения для рассылки. Отмеченные уходят в задачу ссылками на библиотеку:
   пополнили библиотеку — рассылка подхватит новое, пересоздавать не нужно. */
function openLibraryPicker() {
  state.picker = {
    mode: 'library',
    key: 'message',
    multi: true,
    chosen: state.libraryPick.map(String),
    chats: [],
  };
  $('pickerTitle').textContent = 'Сообщения из библиотеки';
  $('pickerSearch').value = '';
  renderPickerFooter();
  $('pickerSheet').classList.add('is-open');
  loadPickerLibrary();
}

/* Подсказка и кнопка внизу шторки. Один чат встаёт в поле сразу по нажатию,
   поэтому кнопка «Готово» там лишняя — прячем, чтобы не искать её глазами. */
function renderPickerFooter() {
  const { mode, multi, chosen, key } = state.picker;
  const label = (FIELD_SPEC[key] || {}).label || 'Поле';
  const apply = $('pickerApply');
  if (mode === 'library') {
    $('pickerLead').textContent =
      `Отметьте сообщения — рассылка отправит их по очереди. Отмечено: ${chosen.length}.`;
  } else {
    $('pickerLead').textContent = multi
      ? `${label}: отмечайте чаты — уйдут в поле через запятую. Отмечено: ${chosen.length}.`
      : `${label}: нажмите чат — он встанет в поле, шторка закроется.`;
  }
  apply.hidden = !multi;
  apply.textContent = chosen.length ? `Готово · ${chosen.length}` : 'Готово';
}

/* Поиск в шторке: чаты ищет сервер, библиотеку фильтруем на месте — она
   маленькая и уже загружена целиком. */
function pickerReload() {
  if (state.picker.mode === 'library') renderPickerList();
  else loadPickerChats();
}

async function loadPickerChats() {
  const holder = $('pickerList');
  const account = pickerAccount();
  if (!account) {
    holder.innerHTML = emptyHtml('👤', 'Нет аккаунта', 'Подключите аккаунт во вкладке «Аккаунты».');
    return;
  }
  const query = encodeURIComponent($('pickerSearch').value || '');
  beginLoad(holder, 'plain', 4);
  try {
    const data = await api(`/api/chats?account_id=${account.id}&q=${query}`);
    endLoad(holder);
    state.picker.chats = data.chats || [];
    if (!data.online) {
      holder.innerHTML = emptyHtml('📴', 'Аккаунт не в сети', 'Перезапустите аккаунт в боте — список чатов читает он.');
      return;
    }
    renderPickerList();
  } catch (error) {
    failLoad(holder, error, 'loadPickerChats');
  }
}

async function loadPickerLibrary() {
  const holder = $('pickerList');
  beginLoad(holder, 'plain', 3);
  try {
    const data = await api('/api/library');
    endLoad(holder);
    state.library = data.items || [];
    renderPickerList();
  } catch (error) {
    failLoad(holder, error, 'loadPickerLibrary');
  }
}

function renderPickerList() {
  if (state.picker.mode === 'library') {
    renderPickerLibrary();
    return;
  }
  const holder = $('pickerList');
  const chats = state.picker.chats;
  if (!chats.length) {
    holder.innerHTML = emptyHtml('💬', 'Ничего не найдено', 'Измените запрос — или впишите @username прямо в поле.');
    return;
  }
  holder.innerHTML = chats.map((chat) => {
    const ref = chatToRef(chat);
    const kind = chatKind(chat);
    const on = state.picker.chosen.includes(ref);
    return `
      <button type="button" class="chat${on ? ' is-selected' : ''}" data-pick-ref="${esc(ref)}">
        <div class="chat__emoji">${kind.emoji}</div>
        <div class="chat__body">
          <div class="chat__title">${esc(chatTitle(chat))}</div>
          <div class="chat__sub"><code>${esc(ref)}</code></div>
        </div>
        <span class="chat__kind">${kind.label}</span>
        ${pickerMarkHtml(on)}
      </button>`;
  }).join('');
}

/* Отметка справа. Когда чат нужен один, пустая рамка врёт: она обещает выбор
   пачкой, а нажатие сразу закрывает шторку — поэтому там шеврон «внутрь». */
function pickerMarkHtml(on) {
  if (!state.picker.multi) return '<span class="chat__go" aria-hidden="true">›</span>';
  return `<span class="chat__check" aria-hidden="true">${on ? '✓' : ''}</span>`;
}

function renderPickerLibrary() {
  const holder = $('pickerList');
  const query = String($('pickerSearch').value || '').trim().toLowerCase();
  const items = state.library.filter((item) =>
    !query || `${item.title || ''} ${item.text || ''}`.toLowerCase().includes(query)
  );
  if (!items.length) {
    holder.innerHTML = state.library.length
      ? emptyHtml('📚', 'Ничего не найдено', 'Измените запрос.')
      : emptyHtml('📚', 'Библиотека пуста', 'Наберите текст в поле «Сообщение» — он уйдёт в рассылку и сохранится сам.');
    return;
  }
  holder.innerHTML = items.map((item) => {
    const on = state.picker.chosen.includes(String(item.id));
    const { head, sub } = libraryLines(item, 60);
    return `
      <button type="button" class="chat${on ? ' is-selected' : ''}" data-pick-ref="${item.id}">
        <div class="chat__emoji">📄</div>
        <div class="chat__body">
          <div class="chat__title">${esc(head)}</div>
          ${sub ? `<div class="chat__sub">${esc(sub)}</div>` : ''}
        </div>
        ${pickerMarkHtml(on)}
      </button>`;
  }).join('');
}

function togglePickerRef(ref) {
  if (!ref) return;
  const picker = state.picker;
  if (!picker.multi) {
    picker.chosen = [ref];
    applyPicker();
    return;
  }
  const idx = picker.chosen.indexOf(ref);
  if (idx >= 0) picker.chosen.splice(idx, 1);
  else picker.chosen.push(ref);
  markPickerRow(ref, idx < 0);
  renderPickerFooter();
}

/* Отметку рисуем на месте, а не перерисовкой всего списка: перерисовка сбивает
   прокрутку в начало, и на длинном списке чатов каждая отметка отбрасывала
   человека к первому чату. */
function markPickerRow(ref, on) {
  const row = [...$('pickerList').querySelectorAll('[data-pick-ref]')]
    .find((node) => node.dataset.pickRef === String(ref));
  if (!row) {
    renderPickerList();
    return;
  }
  row.classList.toggle('is-selected', on);
  const mark = row.querySelector('.chat__check');
  if (mark) mark.textContent = on ? '✓' : '';
}

function applyPicker() {
  const { mode, key, chosen } = state.picker;
  if (mode === 'library') {
    state.libraryPick = chosen.map(Number).filter(Boolean);
    renderLibraryPicks();
    closePicker();
    return;
  }
  const node = key ? $(`task_${key}`) : null;
  if (node) node.value = chosen.join(', ');
  closePicker();
}

/* Отмеченные сообщения рядом с полем: видно, что уйдёт, и можно снять по одному
   не открывая шторку заново. */
function renderLibraryPicks() {
  const holder = $('libraryPicks');
  if (!holder) return;
  if (!state.libraryPick.length) {
    holder.innerHTML = '';
    return;
  }
  const chips = state.libraryPick.map((id) => {
    const item = state.library.find((row) => Number(row.id) === Number(id));
    const title = item ? (item.title || libraryPreview(item, 40)) : `сообщение #${id}`;
    return `<span class="pick"><span class="pick__t">${esc(title)}</span><button type="button" class="pick__x"
      data-library-drop="${id}" aria-label="Убрать сообщение">✕</button></span>`;
  }).join('');
  holder.innerHTML =
    '<i class="field__note">уйдут из библиотеки — поле выше можно оставить пустым</i>' + chips;
}

/* Своя кнопка закрытия: общий обработчик [data-close] гасит все шторки сразу
   и увёл бы вместе с выбором саму форму задачи. */
function closePicker() {
  $('pickerSheet').classList.remove('is-open');
  state.picker = { mode: 'chats', key: null, multi: false, chosen: [], chats: [] };
}

function closeSheets() {
  const loginWasOpen = $('loginSheet').classList.contains('is-open');
  document.querySelectorAll('.sheet').forEach((sheet) => sheet.classList.remove('is-open'));
  state.activeCommand = null;
  state.picker = { mode: 'chats', key: null, multi: false, chosen: [], chats: [] };
  // Закрыли шторку на середине входа — в списке должна появиться карточка
  // «Продолжить вход»: шаг никуда не делся, он лежит в БД на сервере.
  if (loginWasOpen && state.login.stage !== 'phone') loadAccounts();
}

/* Значения полей → тело запроса POST /api/tasks. */
function collectTaskPayload() {
  const command = state.activeCommand;
  const values = {};
  [...command.needs, ...(command.optional || [])].forEach((key) => {
    values[key] = fieldValue(key);
  });

  const missing = command.needs
    // Сообщение можно не набирать, если выбрано из библиотеки: рассылка возьмёт
    // тексты оттуда, и требовать копию того же текста в поле незачем.
    .filter((key) => !values[key] && !(key === 'message' && state.libraryPick.length))
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
  if (values.gap) body.gap = Number(values.gap) || 0;
  if (values.cycle) body.cycle = Number(values.cycle) || 0;
  if (values.repeats) body.repeats = Number(values.repeats) || 0;
  if (values.typing) body.typing = true;
  if (values.random_pick) body.random_pick = true;
  // Явный выбор из библиотеки важнее набранного текста — так же считает сервер.
  if (state.libraryPick.length) body.library_ids = state.libraryPick;
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
  // Половина ряда — это ~158 px, подпись в две строки. Число от «дн.» отрывать
  // нельзя, поэтому между ними неразрывный пробел: перенос уйдёт до числа.
  if (distribute) {
    distribute.disabled = !state.bankedDays;
    distribute.textContent = state.bankedDays
      ? `Распределить ${state.bankedDays} дн.`
      : 'Копилка пуста';
  }
  if (freeze) {
    freeze.disabled = !daysLeft || daysLeft <= 1;
    freeze.textContent = daysLeft > 1 ? `❄️ Заморозить ${daysLeft - 1} дн.` : '❄️ Заморозить дни';
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

/* ───────────────── Оплата вне Telegram: карта и крипта ───────────────── */

/* Внутри Telegram абонемент продаётся только за звёзды — так требуют правила
   Telegram (ToS для разработчиков, п. 6.2). Карта и USDT работают на странице
   сервиса, и кабинет открывает её через WebApp.openLink, то есть во внешнем
   браузере. Ничего не прячем: на кнопке прямо написано «на сайте». */
const WEB_PAY_LABELS = { yookassa: 'картой', usdt: 'криптой' };

function renderWebPayButton() {
  const button = $('webPayBtn');
  if (!button) return;
  const pay = (state.me && state.me.pay) || {};
  const methods = pay.external || [];
  button.hidden = !methods.length;
  if (!methods.length) return;
  const names = methods.map((method) => WEB_PAY_LABELS[method] || method).join(' или ');
  button.textContent = `🌐 Оплатить ${names} на сайте`;
}

async function payOnWeb(button) {
  if (DEMO) {
    toast('В демо страница оплаты не открывается — она работает из бота.');
    return;
  }
  try {
    // Ссылка подписана и живёт час, поэтому берём свежую в момент нажатия,
    // а не ту, что пришла с /api/me при открытии кабинета.
    const data = await withLoading(button, () => api('/api/pay/link'));
    openExternal(data.url);
  } catch (error) {
    toast(error.message);
  }
}

/* openLink — это внешний браузер, а не WebView Telegram: платёж проходит вне
   Telegram, как и задумано. Вне Telegram (обычный браузер) уходим в ту же
   ссылку обычным переходом: window.open после await блокируется. */
function openExternal(url) {
  if (!url) {
    toast('Оплата на сайте недоступна');
    return;
  }
  if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
  else location.href = url;
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

/* Одна разметка на «Ещё» и на шторку настроек: разница только в том, куда
   ведёт пункт — внутрь кабинета (data-tab) или в бота (data-start). */
function settingRowHtml(item) {
  const attr = item.tab
    ? `data-tab-goto="${item.tab}"${item.status ? ` data-tab-status="${item.status}"` : ''}`
    : `data-start="${item.start}"`;
  return `
    <button class="setting" ${attr}>
      <div class="setting__emoji" aria-hidden="true">${item.emoji}</div>
      <div class="setting__body">
        <div class="setting__title">${esc(item.title)}</div>
        <div class="setting__desc">${esc(item.desc)}</div>
      </div>
      <div class="setting__chev" aria-hidden="true">›</div>
    </button>`;
}

function renderSettings() {
  $('settingsList').innerHTML = SETTINGS.map(settingRowHtml).join('');
}

/* Экран «Ещё»: сначала разделы кабинета, потом то, что живёт в боте. */
function renderMore() {
  const holder = $('moreList');
  if (!holder) return;
  holder.innerHTML =
    MORE_ITEMS.map(settingRowHtml).join('') +
    '<div class="section-label">в боте</div>' +
    SETTINGS.map(settingRowHtml).join('');
  const about = $('aboutLine');
  if (about) {
    about.textContent = state.build
      ? `Кабинет автоматизаций Telegram · сборка ${state.build}`
      : 'Кабинет автоматизаций Telegram.';
  }
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
      <svg class="mascot mascot--md" viewBox="0 0 120 120" aria-hidden="true"><use href="#crown" /></svg>
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
  // Корона в центре навигации — то же, что «создать задачу»: главное действие
  // кабинета должно быть под большим пальцем, а не в глубине экрана.
  $('crownBtn').addEventListener('click', () => openTaskSheet(null));

  // главная
  $('homeCreate').addEventListener('click', () => openTaskSheet(null));
  $('homeTiles').addEventListener('click', (event) => {
    const tile = event.target.closest('[data-command], [data-goto]');
    if (!tile) return;
    if (tile.dataset.goto) switchTab(tile.dataset.goto);
    else openCommand(tile.dataset.command);
  });
  // Карточки задач на «Главной» краткие: снять с паузы можно здесь же,
  // «открыть» уводит на вкладку с полным набором кнопок. Слушатель делегирован,
  // потому что содержимое списка перерисовывается на каждом обновлении.
  $('homeTasks').addEventListener('click', (event) => {
    const link = event.target.closest('[data-goto]');
    if (link) {
      switchTab(link.dataset.goto);
      return;
    }
    const button = event.target.closest('[data-action]');
    if (!button) return;
    taskAction(button.dataset.action, button.dataset.id, button);
  });
  $('promptForm').addEventListener('submit', (event) => {
    event.preventDefault();
    runSmartSearch();
  });
  $('promptInput').addEventListener('input', renderPromptHits);
  $('promptHits').addEventListener('click', (event) => {
    const hit = event.target.closest('[data-command], [data-goto]');
    if (!hit) return;
    if (hit.dataset.goto) switchTab(hit.dataset.goto);
    else openCommand(hit.dataset.command);
  });
  // «смотреть все» и прочие ссылки-переходы по кабинету
  document.querySelectorAll('[data-goto]').forEach((node) => {
    if (node.closest('#homeTiles, #promptHits, #homeTasks')) return; // у них свой делегат
    node.addEventListener('click', () => switchTab(node.dataset.goto));
  });

  // ещё
  $('moreList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-tab-goto], [data-start]');
    if (!item) return;
    if (item.dataset.start) {
      openBot(item.dataset.start);
      return;
    }
    // «Архив задач» ведёт на вкладку задач сразу в нужный статус.
    if (item.dataset.tabStatus) setTaskStatus(item.dataset.tabStatus);
    switchTab(item.dataset.tabGoto);
  });

  // команды
  $('commandSearch').addEventListener('input', renderCommands);
  $('commandTags').addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    const group = chip.dataset.group || null;
    // Повторный тап по активному чипсу снимает фильтр: так работает вся
    // остальная фильтрация в кабинете (чипсы чатов), не будем удивлять.
    state.commandGroup = state.commandGroup === group ? null : group;
    renderCommands();
    // Ряд чипсов прокручивается по горизонтали, и выбранный часто остаётся
    // обрезанным у края. Подтягиваем его в центр — видно, что именно выбрано.
    // block: 'nearest' держит вертикальную прокрутку страницы на месте.
    const active = $('commandTags').querySelector('.chip.is-active');
    if (active && active.scrollIntoView) {
      active.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' });
    }
  });
  $('commandList').addEventListener('click', (event) => {
    const card = event.target.closest('[data-command]');
    if (!card) return;
    openCommand(card.dataset.command);
  });

  // задачи
  document.querySelectorAll('#taskStatus .seg').forEach((seg) => {
    seg.addEventListener('click', () => setTaskStatus(seg.dataset.status));
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
    } else if (action === 'mailing') {
      openTaskForSelection('mailing');
    } else if (action === 'forward') {
      openTaskForSelection('copy_channel');
    } else if (action === 'parser') {
      openTaskForSelection('parser');
    } else if (action === 'poster') {
      openTaskForSelection('poster');
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
    // Вход проходит здесь же. В боте он остаётся запасным путём — на случай,
    // когда шлюз выключен и кабинету нечего показать.
    if (!state.features.account_login_enabled) {
      toast('Вход аккаунтов пока на настройке');
      openBot('add_account');
      return;
    }
    openLoginSheet();
  });
  $('accountList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    if (button.dataset.action === 'resume-login') openLoginSheet();
    else if (button.dataset.action === 'delete-account') deleteAccount(button.dataset.id, button);
  });
  $('loginSubmit').addEventListener('click', submitLogin);
  $('loginInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      submitLogin();
    }
  });
  $('loginRestart').addEventListener('click', restartLogin);
  $('loginInBot').addEventListener('click', () => openBot('add_account'));
  $('topUpBtn').addEventListener('click', (event) => {
    // currentTarget, а не target: внутри кнопки может лежать <span>, и тогда
    // индикатор загрузки (withLoading) повесился бы не на ту кнопку.
    payWithStars(event.currentTarget);
  });
  $('webPayBtn').addEventListener('click', (event) => {
    payOnWeb(event.currentTarget);
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

  // выбор чата мышкой: кнопка «💬 выбрать» живёт в пересобираемой разметке
  // полей, поэтому слушатель делегированный — на контейнер.
  $('taskFields').addEventListener('click', (event) => {
    // Кнопки лежат внутри <label>: без preventDefault клик заодно уходит в
    // поле и на телефоне выскакивает клавиатура поверх списка.
    const library = event.target.closest('[data-pick-library]');
    if (library) {
      event.preventDefault();
      openLibraryPicker();
      return;
    }
    const drop = event.target.closest('[data-library-drop]');
    if (drop) {
      event.preventDefault();
      const id = Number(drop.dataset.libraryDrop);
      state.libraryPick = state.libraryPick.filter((item) => Number(item) !== id);
      renderLibraryPicks();
      return;
    }
    const button = event.target.closest('[data-pick]');
    if (!button) return;
    event.preventDefault();
    openFieldPicker(button.dataset.pick, button.dataset.multi === '1');
  });
  $('pickerList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-pick-ref]');
    if (!item) return;
    togglePickerRef(item.dataset.pickRef);
  });
  $('pickerApply').addEventListener('click', applyPicker);
  let pickerTimer = null;
  $('pickerSearch').addEventListener('input', () => {
    clearTimeout(pickerTimer);
    pickerTimer = setTimeout(pickerReload, 350);
  });
  document.querySelectorAll('[data-picker-close]').forEach((node) => {
    node.addEventListener('click', closePicker);
  });

  // библиотека сообщений
  $('libraryAdd').addEventListener('click', (event) => addLibraryItems(event.currentTarget));
  $('libraryList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action="delete-library"]');
    if (!button) return;
    deleteLibraryItem(button.dataset.id, button);
  });

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

/* Свежесть бандла. Адрес кабинета несёт метку сборки (?v=…), а /api/health
   говорит, какая метка сейчас на сервере. Не совпали — значит WebView открыл
   кабинет по старому адресу и держит старые файлы: уходим на адрес с новой
   меткой (другой ключ кэша — файлы приедут с сервера).

   Метку сверяем один раз на метку: если перезагрузка почему-то не помогла,
   второй круг не начинаем, иначе кабинет зациклится вместо того, чтобы
   открыться хоть как-то. */
async function reloadIfBuildIsStale() {
  const params = new URLSearchParams(location.search);
  state.build = params.get('v') || '';
  let build = '';
  try {
    const response = await fetch('/api/health', { cache: 'no-store' });
    if (!response.ok) return false;
    build = (await response.json()).build || '';
  } catch (error) {
    return false; // сервер недоступен — это не повод не открывать кабинет
  }
  if (!build || params.get('v') === build) return false;

  const key = 'build-reload:' + build;
  try {
    if (sessionStorage.getItem(key)) return false;
    sessionStorage.setItem(key, '1');
  } catch (error) {
    // Приватный режим без sessionStorage: одна перезагрузка всё равно нужнее.
  }
  params.set('v', build);
  location.replace(location.pathname + '?' + params.toString() + location.hash);
  return true;
}

async function boot() {
  if (await reloadIfBuildIsStale()) return;
  if (tg) {
    tg.ready();
    tg.expand();
  }
  applyTheme();
  renderSettings();
  renderMore();
  bindEvents();
  if (DEMO) showDemoBar();

  try {
    try {
      state.me = await api('/api/me');
      renderHeader();
      // Цена в звёздах приходит с тарифами, поэтому подпись кнопки знает её
      // только здесь — до этого на кнопке нейтральный текст из index.html.
      renderTopUpButton();
      // Кнопка «на сайте» появляется только если контур внешней оплаты включён.
      renderWebPayButton();
    } catch (error) {
      toast(error.message);
    }

    await loadAccounts();
    await loadCommands();
    // Плитки «быстрого старта» и подсказки поиска живут на каталоге команд,
    // поэтому «Главную» собираем после него, а не на старте.
    renderHome();
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
