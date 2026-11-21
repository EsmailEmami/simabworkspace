/* Simab Workspace — saved prompts: texts you send again and again ("write tests and run them",
   "review my changes"…), for all projects or one. Kept on the server (~/.config/simabworkspace/
   prompts.json): the installed app and the web version share them. In a chat, "/" lists them above
   Claude's own commands; ⚙ → Saved prompts… edits them. */
(function () {
  'use strict';
  var SW = window.SW, h = SW.h, api = SW.api, fail = SW.fail, toast = SW.toast, S = SW.S;
  var data = { rev: 0, prompts: [] };
  var loaded = false;
  var EXAMPLES = [
    { name: 'tests', text: 'Write tests for what you just changed, run them, and fix what fails.' },
    { name: 'review', text: 'Review my uncommitted changes: bugs first, then anything unclear. Short list, file:line.' },
    { name: 'run', text: 'Run the app of this project for me and give me its address.' }];

  function load() {
    return api('GET', 'prompts').then(function (d) { data = d && d.prompts ? d : { rev: 0, prompts: [] }; loaded = true; return data; }).catch(function () { return data; });
  }

  // for the "/" list of a chat: this project's and the general ones, by name
  function match(q, project) {
    if (!loaded) load();
    q = (q || '').toLowerCase();
    return data.prompts.filter(function (p) { return (!p.project || p.project === project) && p.name.toLowerCase().indexOf(q) >= 0; })
      .sort(function (a, b) { return (b.name.toLowerCase().indexOf(q) === 0) - (a.name.toLowerCase().indexOf(q) === 0) || (!!b.project - !!a.project); })
      .slice(0, 8);
  }

  function dialog(project) {
    var rows = h('div', { class: 'pr-list' });
    var work = [];
    var md = SW.openModal({ title: 'Saved prompts', size: 'wide', body: h('div', { class: 'pr-dlg' },
      h('div', { class: 'pc-note', style: 'margin-top:0' }, 'In a chat type / and the name: the text goes into the message box. '
        + '“All projects” or only one. Shared by the app and the web version.'), rows,
      h('div', { class: 'pc-actions', style: 'justify-content:flex-start' },
        h('button', { class: 'btn sm', onclick: function () { work.push({ name: '', text: '', project: project || '' }); render(true); } }, '+ New prompt'),
        h('button', { class: 'btn sm ex', onclick: function () { work = work.concat(EXAMPLES.map(function (x) { return Object.assign({ project: '' }, x); })); render(); } }, '+ Add examples'))),
      foot: [h('button', { class: 'btn', onclick: function () { md.close(); } }, 'Cancel'), h('button', { class: 'btn primary', onclick: save }, 'Save')] });
    function render(focusLast) {
      var projects = (S.projects || []).map(function (p) { return p.name; });
      rows.replaceChildren.apply(rows, work.length ? work.map(function (p, i) {
        var name = h('input', { class: 'inp mono', value: p.name, placeholder: 'name', maxlength: 60 });
        name.addEventListener('input', function () { p.name = name.value.trim(); });
        var sel = h('select', { class: 'inp' }, [h('option', { value: '' }, 'All projects')].concat(projects.map(function (n) { return h('option', { value: n }, n); })));
        sel.value = p.project || '';
        sel.addEventListener('change', function () { p.project = sel.value; });
        var text = h('textarea', { class: 'inp', rows: 3, dir: 'auto', placeholder: 'the text' });
        text.value = p.text;
        text.addEventListener('input', function () { p.text = text.value; });
        if (focusLast && i === work.length - 1) setTimeout(function () { name.focus(); }, 30);
        return h('div', { class: 'pr-row' }, h('div', { class: 'pr-top' }, h('b', { class: 'muted' }, '/'), name, sel,
          h('button', { class: 'icon-btn', title: 'Delete', onclick: function () { work.splice(i, 1); render(); } }, '✕')), text);
      }) : [h('div', { class: 'muted' }, 'No saved prompts yet.')]);
    }
    function save() {
      var bad = work.filter(function (p) { return !p.name || !(p.text || '').trim(); });
      if (bad.length) { toast('Each prompt needs a name and a text', 'warn'); return; }
      api('POST', 'prompts', { rev: data.rev, prompts: work }).then(function (d) { data = d; md.close(); toast('Saved', 'ok'); }).catch(fail);
    }
    load().then(function () { work = data.prompts.map(function (p) { return Object.assign({}, p); }); render(); });
    return md;
  }

  setTimeout(load, 2500);
  window.SWPrompts = { match: match, dialog: dialog, load: load, _data: function () { return data; } };
})();
