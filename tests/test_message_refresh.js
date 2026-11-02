const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app.py', 'utf8');
const elements = {
  '#messages-view': {classList: {contains: () => hiddenView}},
  '#matches': {innerHTML: ''},
  '#match-count': {textContent: ''},
};
let hiddenView = false;
let fetches = 0;
let messageLoads = 0;
let unread = true;
const context = vm.createContext({
  console,
  document: {hidden: false},
  s: {user: {id: 'me'}, active: null, matches: []},
  $: selector => elements[selector],
  esc: value => value,
  api: async () => {fetches++; return [{id:'match',unread,last_message:'Salut',profile:{first_name:'Camille',photo_paths:[]}}]},
  loadMessages: async () => {messageLoads++},
});
for (const name of ['conversationVisible', 'loadMatches', 'refreshVisibleMessages']) {
  const line = source.split('\n').find(line => line.startsWith(`function ${name}(`) || line.startsWith(`async function ${name}(`));
  assert.ok(line, name);
  vm.runInContext(line, context);
}
vm.runInContext('let refreshingMessages=false', context);
(async () => {
  await context.refreshVisibleMessages();
  assert.match(elements['#matches'].innerHTML, /class="match unread /);
  unread = false;
  await context.refreshVisibleMessages();
  assert.doesNotMatch(elements['#matches'].innerHTML, /class="match unread /);
  assert.equal(fetches, 2);
  hiddenView = true;
  await context.refreshVisibleMessages();
  context.document.hidden = true;
  hiddenView = false;
  await context.refreshVisibleMessages();
  assert.equal(fetches, 2);
  context.document.hidden = false;
  context.s.active = {id:'match'};
  await context.refreshVisibleMessages();
  assert.equal(messageLoads, 1);
  assert.equal(fetches, 2);
  console.log('Message refresh and unread rendering checks passed');
})().catch(error => {console.error(error); process.exitCode=1});
