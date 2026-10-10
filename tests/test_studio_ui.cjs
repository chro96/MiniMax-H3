"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

function ui() {
  const nodes = new Map(), drafts = new Map();
  const surfaces = [{}, {}, {}];
  const context = vm.createContext({
    document: {
      querySelector(selector) {
        if (!nodes.has(selector)) nodes.set(selector, {addEventListener() {}, close() {}, querySelector() { return null; }, hidden: false});
        return nodes.get(selector);
      },
      querySelectorAll() { return surfaces; },
      addEventListener() {},
    },
    window: {addEventListener() {}}, location: {hash: ""},
    sessionStorage: {
      getItem: (key) => drafts.get(key), setItem: (key, value) => drafts.set(key, value),
      removeItem: (key) => drafts.delete(key),
    },
    setTimeout() {}, clearTimeout() {}, console,
  });
  const source = fs.readFileSync(path.join(__dirname, "../studio/static/app.js"), "utf8");
  // Load the actual application functions without its network startup.
  vm.runInContext(source.slice(0, source.lastIndexOf("\n(async()=>{")), context);
  vm.runInContext("render=()=>{};renderLibrary=()=>{};", context);
  return {run: (code) => vm.runInContext(code, context), context, drafts, surfaces};
}

const a = "a".repeat(32), b = "b".repeat(32);

test("ComfyUI generation confirmation retains the chosen backend and cut", async () => {
  const app = ui();
  app.run(`state.provider="comfy";state.project={id:"${a}",version:7,document:{cuts:[{id:"${b}",title:"cut",seconds:6,description_ja:"memo"}]},prompts:{"${b}":"H3 prompt"}};
    modal=(title,body)=>{globalThis.confirmation=body;};`);
  await app.run(`generate(["${b}"])`);
  assert.equal(app.run("state.pending.provider"), "comfy");
  assert.equal(app.run("state.pending.version"), 7);
  assert.equal(app.run("state.pending.cut_ids[0]"), b);
  assert.match(app.run("confirmation"), /ComfyUI H3/);
  assert.match(app.run("confirmation"), /H3 prompt/);
});

test("connection without a workflow asks for the working API workflow", async () => {
  const app = ui();
  app.run("api=async()=>({missing_nodes:{},configuration:{workflow_counts:[]}});");
  await app.run("handleAction('check-comfy',{})");
  assert.match(app.run("$('#comfy-check-result').textContent"), /ワークフローを読み込んで/);
  app.run("api=async()=>({missing_nodes:{'1':['CustomGGUF']},configuration:{workflow_counts:[1]}});");
  await app.run("handleAction('check-comfy',{})");
  assert.match(app.run("$('#comfy-check-result').textContent"), /参照1枚用: CustomGGUF/);
});

test("editing during save persists the newer draft at the returned version", async () => {
  const app = ui();
  app.run(`state.project={id:"${a}",version:1,document:{title:"first",cuts:[]}};state.dirty=true;
    globalThis.calls=[];api=(url,data)=>new Promise(resolve=>calls.push({url,data,resolve}));`);
  const saving = app.run("save()");
  app.run(`state.project.document.title="newer input";markDirty();
    calls[0].resolve({id:"${a}",version:2,document:{title:"first",cuts:[]}});`);
  await Promise.resolve();
  assert.equal(app.run("calls.length"), 2);
  assert.equal(app.run("calls[1].data.version"), 2);
  assert.equal(app.run("calls[1].data.document.title"), "newer input");
  assert.equal(JSON.parse(app.drafts.get(`draft:${a}`)).version, 2);
  app.run(`calls[1].resolve({id:"${a}",version:3,document:{title:"newer input",cuts:[]}});`);
  await saving;
  assert.equal(app.run("state.project.document.title"), "newer input");
  assert.equal(app.run("state.dirty"), false);
  assert.equal(app.drafts.has(`draft:${a}`), false);
});

test("an old save response cannot replace another project", async () => {
  const app = ui();
  app.run(`state.project={id:"${a}",version:1,document:{title:"A",cuts:[]}};state.dirty=true;
    api=()=>new Promise(resolve=>globalThis.finish=resolve);`);
  const saving = app.run("save()");
  app.run(`state.project={id:"${b}",version:1,document:{title:"B",cuts:[]}};
    finish({id:"${a}",version:2,document:{title:"A",cuts:[]}});`);
  await saving;
  assert.equal(app.run("state.project.id"), b);
});

test("navigation discards a response for a route that has changed", async () => {
  const app = ui();
  app.run(`location.hash="#project/${a}";api=()=>new Promise(resolve=>globalThis.finish=resolve);`);
  const routing = app.run("route()");
  app.run(`location.hash="#project/${b}";finish({id:"${a}",version:1,document:{cuts:[]}});`);
  await routing;
  assert.equal(app.run("state.project"), null);
});

test("uploads and navigation run in order with interaction locked until completion", async () => {
  const app = ui();
  app.run("globalThis.events=[];globalThis.release=null;");
  const uploading = app.run("runOperation(async()=>{events.push('upload');await new Promise(resolve=>release=resolve);events.push('saved');})");
  const routing = app.run("runOperation(async()=>{events.push('route');})");
  await Promise.resolve();
  assert.equal(app.run("events.join(',')"), "upload");
  assert.equal(app.run("state.busy"), true);
  assert.ok(app.surfaces.every((node) => node.inert));
  app.run("release()");
  await Promise.all([uploading, routing]);
  assert.equal(app.run("events.join(',')"), "upload,saved,route");
  assert.equal(app.run("state.busy"), false);
  assert.ok(app.surfaces.every((node) => !node.inert));
  await app.run("runOperation(async()=>{throw new Error('failed');})");
  assert.equal(app.run("state.busy"), false);
  assert.ok(app.surfaces.every((node) => !node.inert));
});

test("polling cannot overwrite an edit made while its request was in flight", async () => {
  const app = ui();
  app.run(`state.project={id:"${a}",version:1,document:{title:"first",cuts:[]}};
    api=()=>new Promise(resolve=>globalThis.finish=resolve);`);
  const refreshing = app.run("refresh()");
  app.run(`state.project.document.title="draft";markDirty();
    finish({id:"${a}",version:2,document:{title:"old response",cuts:[]}});`);
  await refreshing;
  assert.equal(app.run("state.project.document.title"), "draft");
  assert.equal(app.run("state.dirty"), true);
});

test("AI proposals cannot discard dirty edits", async () => {
  const app = ui();
  app.run(`state.project={id:"${a}",document:{title:"unsaved"}};state.dirty=true;
    globalThis.message="";toast=(text)=>message=text;`);
  app.run("proposal('old-job')");
  assert.match(app.run("message"), /未保存/);
  await assert.rejects(app.run("handleAction('apply-proposal',{})"), /未保存/);
  assert.equal(app.run("state.project.document.title"), "unsaved");
  assert.equal(app.run("state.dirty"), true);
});

test("stale generation skips an unselected matching take without another API call", async () => {
  const app = ui();
  app.run(`state.project={document:{cuts:[{id:"cut"}]},takes:[
      {cut_id:"cut",provider:"h3",selected:true,matches:false,status:"succeeded"},
      {cut_id:"cut",provider:"h3",selected:false,matches:true,status:"succeeded"}]};
    save=async()=>{};globalThis.generated=false;generate=async()=>{generated=true;};`);
  await app.run("handleAction('generate-stale',{})");
  assert.equal(app.run("generated"), false);
});

test("progress-only polling updates the queue without replacing the page", async () => {
  const app = ui();
  app.run(`state.project={id:"${a}",version:1,status:"generating",jobs:[
    {id:"job",status:"running",stage:"old",progress:0,payload:{}}]};
    globalThis.renders=0;render=()=>renders++;
    api=async()=>({id:"${a}",version:1,status:"generating",jobs:[
      {id:"job",status:"running",stage:"generating",progress:50,payload:{},updated:"new"}]});`);
  await app.run("refresh()");
  assert.equal(app.run("renders"), 0);
  assert.equal(app.run("state.project.jobs[0].progress"), 50);
});

test("library polling redraws changed statuses", async () => {
  const app = ui();
  app.run(`state.library=[{id:"${a}",status:"generating"}];
    globalThis.renders=0;renderLibrary=()=>renders++;
    api=async()=>[{id:"${a}",status:"review"}];`);
  await app.run("refresh()");
  assert.equal(app.run("renders"), 1);
  assert.equal(app.run("state.library[0].status"), "review");
});

test("a poll started before a completed operation cannot roll back its result", async () => {
  const app = ui();
  app.run(`state.project={id:"${a}",version:1,status:"review",jobs:[],document:{title:"before"}};
    api=()=>new Promise(resolve=>globalThis.finish=resolve);`);
  const refreshing = app.run("refresh()");
  await app.run(`runOperation(async()=>{state.project={id:"${a}",version:2,status:"review",jobs:[],document:{title:"new selection"}};})`);
  app.run(`finish({id:"${a}",version:1,status:"review",jobs:[],document:{title:"old selection"}});`);
  await refreshing;
  assert.equal(app.run("state.project.version"), 2);
  assert.equal(app.run("state.project.document.title"), "new selection");
});

test("a slow library poll cannot overwrite a newer navigation result", async () => {
  const app = ui();
  app.run(`state.library=[{status:"draft"}];api=()=>new Promise(resolve=>globalThis.finish=resolve);`);
  const refreshing = app.run("refresh()");
  await app.run("runOperation(async()=>{state.library=[{status:'review'}];})");
  app.run("finish([{status:'generating'}])");
  await refreshing;
  assert.equal(app.run("state.library[0].status"), "review");
});
