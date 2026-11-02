const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('app.py', 'utf8');
const pending = [];
let chat = null;
const container = {
  dataset: {matchId: 'match'},
  set innerHTML(value) {
    chat = {innerHTML: value, scrollTop: 0, scrollHeight: 1600, clientHeight: 400};
  },
};
const context = vm.createContext({
  s: {active: {id: 'match', profile: {first_name: 'Camille'}}, messageLoad: 0},
  $: selector => selector === '#conversation' ? container : chat,
  conversationVisible: () => true,
  api: () => new Promise(resolve => pending.push(resolve)),
  messageBubbles: () => 'Messages du plus ancien au plus récent',
  markMessagesRead: async () => {},
  esc: value => value,
  showConversationError: (_, error) => {throw error},
  note: error => {throw new Error(error)},
});
for (const name of ['loadMessages', 'drawMessages']) {
  vm.runInContext(source.split('\n').find(line =>
    line.startsWith(`function ${name}(`) || line.startsWith(`async function ${name}(`)), context);
}

(async () => {
  // A background refresh can supersede the opening request before it returns.
  const opening = context.loadMessages(true);
  const refresh = context.loadMessages(false);
  pending.shift()([]);
  await opening;
  assert.equal(chat, null);
  pending.shift()([]);
  await refresh;
  assert.equal(chat.scrollTop, chat.scrollHeight, 'New conversation opens at the latest message');

  chat.scrollTop = 250;
  const readingHistory = context.loadMessages(false);
  pending.shift()([]);
  await readingHistory;
  assert.equal(chat.scrollTop, 250, 'Refreshing preserves the position when reading older messages');

  chat.scrollTop = 1200;
  const readingLatest = context.loadMessages(false);
  pending.shift()([]);
  await readingLatest;
  assert.equal(chat.scrollTop, chat.scrollHeight, 'Refreshing follows messages when already at the bottom');
  console.log('Conversation opening and scroll checks passed');
})().catch(error => {console.error(error); process.exitCode = 1});
