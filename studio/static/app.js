"use strict";

const $ = (selector) => document.querySelector(selector);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const labels = {draft:"コマ割り作成",generating:"処理中",assembly:"合成待ち",review:"レビュー待ち",approved:"承認済み",archived:"アーカイブ",queued:"待機中",running:"処理中",succeeded:"完了",failed:"失敗"};
const fieldNames = {visual:"映像・構図",action:"動き",camera:"カメラ",end_state:"終端状態",sound:"効果音",dialogue:"セリフ"};
const state = {config:null,library:[],project:null,dirty:false,filter:"all",query:"",provider:"h3",busy:false,pending:null,upload:null,modalTake:null};
let toastTimer;
let operations = Promise.resolve();
let operationCounter = 0;

function runOperation(work) {
  const pending = operations.then(async () => {
    operationCounter++;
    state.busy = true;
    const surfaces = $$("#app, .sidebar, #modal-body");
    surfaces.forEach((node) => node.inert = true);
    try { await work(); } catch (error) { toast(error.message); }
    finally {
      state.busy = false;
      surfaces.forEach((node) => node.inert = false);
    }
  });
  operations = pending;
  return pending;
}

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").hidden = true, 6000);
}

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-Studio-Request":"1"},body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作に失敗しました");
  return result;
}

function endpoint(action) { return `/api/projects/${state.project.id}/${action}`; }
function badge(status, label) { return `<span class="badge ${escapeHTML(status)}">${escapeHTML(label || labels[status] || status)}</span>`; }
function button(action, label, extra="", disabled=false, cls="") { return `<button class="btn ${cls}" data-action="${action}" ${extra} ${disabled ? "disabled" : ""}>${label}</button>`; }
function active() { return state.project?.jobs.some((j) => ["queued","running"].includes(j.status)); }
function locked() { return Boolean(state.project?.approved || state.project?.archived || active()); }
function date(value) { return new Date(value).toLocaleDateString("ja-JP",{month:"numeric",day:"numeric"}); }

function modal(title, body, footer="") {
  $("#modal-body").innerHTML = `<div class="modal-head"><h2>${escapeHTML(title)}</h2><button data-action="close-modal" aria-label="閉じる">×</button></div>${body}${footer ? `<div class="modal-actions">${footer}</div>` : ""}`;
  if (!$("#modal").open) $("#modal").showModal();
}
function closeModal() { $("#modal").close(); state.pending = null; }

function newProject() {
  modal("新しい30秒をつくる",`<p class="subtitle">アイデアをコマ割りに。カットごとに生成し、ひとつの動画に仕上げます。</p>
    <div class="form-grid">
      <label class="field wide">タイトル<input id="new-title" placeholder="例：小さな冒険、帰り道の発見" maxlength="200" autofocus></label>
      <label class="field wide">どんな動画にしたいですか？<textarea id="new-brief" placeholder="登場人物、舞台、出来事、見せたい結末を自由に書いてください。" rows="4"></textarea></label>
      <label class="field">コマ割り<select id="new-layout"><option value="5x6">5カット × 6秒（標準）</option><option value="6x5">6カット × 5秒</option><option value="3x10">3カット × 10秒</option><option value="2x15">2カット × 15秒</option></select></label>
      <label class="field">画面比率<select id="new-aspect"><option value="9:16">9:16 縦型</option><option value="16:9">16:9 横型</option><option value="1:1">1:1 正方形</option><option value="4:3">4:3</option><option value="3:4">3:4</option><option value="21:9">21:9</option></select></label>
    </div><p class="help">空のコマ割りから編集できます。AIを設定済みなら、作成後にアイデアから構成を提案できます。</p>`,
    `${button("close-modal","キャンセル")}${button("create","コマ割りを作成","",false,"primary")}`);
}

function renderLibrary() {
  const projects = state.library;
  const visible = projects.filter((p) => (state.filter === "archived" ? p.status === "archived" : p.status !== "archived" && (state.filter === "all" || p.status === state.filter)) && `${p.title} ${p.brief}`.toLowerCase().includes(state.query.toLowerCase()));
  const count = (statuses) => projects.filter((p) => statuses.includes(p.status)).length;
  $("#app").innerHTML = `<div class="page-heading"><div><div class="eyebrow">YOUR VIDEO WORKSPACE</div><h1>動画ライブラリ</h1><div class="subtitle">アイデアから、30秒の物語へ。</div></div>${button("new","＋ 新規制作","",false,"primary")}</div>
    <div class="stats"><div class="stat"><span>制作中</span><strong>${count(["draft","generating","assembly"])}</strong></div><div class="stat"><span>レビュー待ち</span><strong>${count(["review"])}</strong></div><div class="stat"><span>承認済み</span><strong>${count(["approved"])}</strong></div><div class="stat"><span>保存したプロジェクト</span><strong>${count(["draft","generating","assembly","review","approved"])}</strong></div></div>
    <div class="filters"><div class="tabs">${[["all","すべて"],["draft","コマ割り"],["generating","処理中"],["review","レビュー"],["approved","承認済み"],["archived","アーカイブ"]].map(([k,v]) => `<button class="tab ${state.filter === k ? "active" : ""}" data-action="filter" data-filter="${k}">${v}</button>`).join("")}</div><input class="search" id="search" placeholder="タイトル・アイデアを検索" value="${escapeHTML(state.query)}" aria-label="プロジェクト検索"></div>
    ${visible.length ? `<div class="grid">${visible.map((p) => `<article class="project-card"><button class="cover" data-action="open" data-id="${p.id}" aria-label="${escapeHTML(p.title)}を開く">${p.cover_url ? `<img src="${p.cover_url}" alt="${escapeHTML(p.title)}" loading="lazy">` : `<div class="cover-placeholder"><span></span><span></span><span></span></div>`}<span class="cover-label">30 SEC / ${p.cut_count} CUTS</span></button><div class="card-body"><a href="#project/${p.id}"><h3>${escapeHTML(p.title)}</h3></a><p>${escapeHTML(p.brief || "アイデアを編集して制作を始めましょう")}</p><div class="card-meta">${badge(p.status)}${p.demo ? badge("demo","DEMO") : ""}<span>${date(p.updated)} · ${p.ready_count}/${p.cut_count} カット</span></div></div></article>`).join("")}</div>` : `<div class="empty"><div class="empty-icon">▤</div><h2>${projects.length ? "該当する動画がありません" : "最初の物語をつくりましょう"}</h2><p>コマ割りをつくって、カットごとに動画を生成。<br>気になる部分を直しながら、30秒の動画に仕上げられます。</p>${button("new","＋ 新規制作","",false,"primary")}</div>`}`;
}

function field(label, key, value, cutId=null, multiline=true, extra="") {
  const attrs = cutId ? `data-cut="${cutId}" data-field="${key}"` : `data-global="${key}"`;
  return `<label class="field ${extra}">${label}${multiline ? `<textarea ${attrs} ${locked() ? "disabled" : ""}>${escapeHTML(value)}</textarea>` : `<input ${attrs} value="${escapeHTML(value)}" ${locked() ? "disabled" : ""}>`}</label>`;
}

function selectedTake(cut) { return state.project.takes.find((t) => t.cut_id === cut.id && t.selected); }
function matchingTake(cut) { return state.project.takes.find((t) => t.cut_id === cut.id && t.matches && t.status === "succeeded" && t.provider === state.provider); }
function takeBadge(take) { return !take ? badge("draft","未生成") : take.status !== "succeeded" ? badge(take.status) : take.matches ? badge("review","本文と一致") : badge("stale","本文と不一致"); }

function cutCard(cut, index, start) {
  const project = state.project;
  const take = selectedTake(cut);
  const history = project.takes.filter((t) => t.cut_id === cut.id);
  return `<section class="cut-card" id="cut-${cut.id}">
    <div class="cut-head"><div class="cut-title"><span class="cut-number">${String(index+1).padStart(2,"0")}</span><h3>${escapeHTML(cut.title)}</h3><span class="muted">${start.toFixed(1)}–${(start+cut.seconds).toFixed(1)}s</span></div><div class="actions">${takeBadge(take)}${take ? `<span class="help">r${take.revision}</span>` : ""}</div></div>
    <div class="cut-body"><div>
      <div class="form-grid">${field("カット名", "title", cut.title, cut.id, false)}<label class="field">尺（秒）<input type="number" data-cut="${cut.id}" data-field="seconds" value="${cut.seconds}" min="4" max="15" step="0.0416666667" ${locked()?"disabled":""}></label>${field("コマの内容・メモ（日本語）", "description_ja", cut.description_ja, cut.id, true,"wide")}${field("映像・構図（H3入力／英語）", "visual", cut.visual, cut.id)}${field("動き（H3入力／英語）", "action", cut.action, cut.id)}</div>
      <details><summary>カメラ・終端状態・音・セリフを編集</summary><div class="form-grid">${field("カメラ（英語）", "camera", cut.camera, cut.id)}${field("終端状態（英語・次カットに引き継ぎ）", "end_state", cut.end_state, cut.id)}${field("効果音（英語）", "sound", cut.sound, cut.id)}${field("セリフ（話す言語のまま）", "dialogue", cut.dialogue, cut.id)}<label class="field">セリフの言語<select data-cut="${cut.id}" data-field="dialogue_language" ${locked()?"disabled":""}>${["Auto","Japanese","English","Chinese","Korean","Spanish","French","German"].map((language)=>`<option value="${language}" ${language===(cut.dialogue_language||"Auto")?"selected":""}>${language==="Auto"?"自動（判別できない場合は指定なし）":language}</option>`).join("")}</select></label></div></details>
      <div class="cut-reference">${cut.references.map((id) => {const asset=project.assets.find((a)=>a.id===id);return asset ? `<img src="${asset.url}" alt="コマの参照画像">${button("remove-reference","×",`data-cut="${cut.id}" data-asset="${id}"`,locked(),"small")}` : "";}).join("")}${button("upload-reference","＋ コマの参照画像",`data-cut="${cut.id}"`,locked(),"small")}</div>
      <label class="inline"><input type="checkbox" data-cut="${cut.id}" data-field="mute" ${cut.mute?"checked":""} ${locked()?"disabled":""}>このカットの元音声を合成時に消す</label>
    </div><div class="cut-media"><button class="take-preview" ${take?.url ? `data-action="play-take" data-take="${take.id}"` : "disabled"} aria-label="カットを再生">${take?.thumb_url ? `<img src="${take.thumb_url}" alt="採用テイク r${take.revision}"><span class="play-circle">▶</span>` : `<span class="help">コマ ${index+1}<br>未生成</span>`}</button><p class="help">${take ? `使用テイク r${take.revision}${take.provider==="demo"?" · DEMO":""}` : "生成した映像がここに表示されます"}</p></div></div>
    <div class="cut-footer">${button("generate-cut",take?"この本文で再生成":"このカットを生成",`data-cut="${cut.id}"`,locked(),"small")}${button("revise","AI修正案",`data-cut="${cut.id}"`,locked()||!state.config.llm_available,"small")}${take ? button("generate-cut","撮り直し",`data-cut="${cut.id}"`,locked(),"small") : ""}<span class="spacer"></span>${button("show-prompt","H3プロンプト",`data-cut="${cut.id}"`,false,"small")}</div>
    ${history.length ? `<details class="take-list"><summary>テイク履歴 (${history.length}) · 使用テイクを選択</summary>${history.map((t)=>`<div class="take-row">${t.thumb_url?`<button class="text-button" data-action="play-take" data-take="${t.id}"><img src="${t.thumb_url}" alt="r${t.revision}"></button>`:`<span>${badge(t.status)}</span>`}<div><strong>r${t.revision} ${t.selected?"· 使用中":""}</strong> ${takeBadge(t)}${t.provider==="demo"?badge("demo","DEMO"):""}<p>${date(t.created)} · seed ${t.input.request.seed}</p>${t.error?`<p class="preformatted">${escapeHTML(t.error)}</p>`:""}<details><summary>生成時の内容・実投入プロンプト</summary><div class="snapshot">${escapeHTML(t.input.snapshot.description_ja)}\n映像: ${escapeHTML(t.input.snapshot.visual)}\n動き: ${escapeHTML(t.input.snapshot.action)}\n終端: ${escapeHTML(t.input.snapshot.end_state)}</div><pre class="prompt">${escapeHTML(t.input.request.prompt)}</pre></details></div><div class="actions">${t.status==="succeeded"?button("choose",t.selected?"使用中":"合成に使う",`data-take="${t.id}"`,locked()||t.selected,"small"):t.status==="failed"&&t.external_id?button("recover","同じジョブを回収",`data-take="${t.id}"`,locked(),"small"):""}</div></div>`).join("")}</details>` : ""}
  </section>`;
}

function renderProject() {
  const p=state.project, d=p.document, cuts=d.cuts;
  const latest=p.renders[0], canEdit=!locked();
  let time=0;
  const proposals=p.jobs.filter((j)=>j.status==="succeeded" && j.payload.proposal && j.payload.version===p.version);
  $("#app").innerHTML = `<a class="back" href="#">← 動画ライブラリ</a>
    <div class="page-heading"><div><div class="eyebrow">STORYBOARD / 30 SECONDS</div><h1>${escapeHTML(d.title)}</h1><div class="actions">${badge(p.status)}<span class="help">${cuts.length}カット · ${d.aspect_ratio} · ローカル保存</span><span class="dirty" id="dirty-label" ${state.dirty?"":"hidden"}>未保存の変更</span></div></div><div class="actions">${button("save","変更を保存","",!canEdit,"primary")}${button(p.archived?"restore":"archive",p.archived?"復元":"アーカイブ","",active())}</div></div>
    <div class="workflow"><div class="${p.status==="draft"?"on":""}"><b>01</b>コマ割り作成</div><div class="${p.status==="generating"?"on":""}"><b>02</b>カット生成</div><div class="${["assembly","review","approved"].includes(p.status)?"on":""}"><b>03</b>合成・レビュー</div></div>
    ${p.approved?`<div class="notice">この動画は承認済みです。修正する場合は「編集へ戻す」を選んでください。完成版と履歴は保存されます。</div>`:""}
    <div class="workspace"><div>
      <section class="panel"><div class="panel-title"><h2>物語の設定</h2><div class="actions">${button("plan","AIでコマ割り案","",!canEdit||!state.config.llm_available,"small")}${button("sample","サンプルを入力","",!canEdit,"small")}</div></div>
        <div class="form-grid">${field("タイトル","title",d.title,null,false)}<label class="field">画面比率<select data-global="aspect_ratio" ${!canEdit?"disabled":""}>${["9:16","16:9","1:1","4:3","3:4","21:9"].map((v)=>`<option ${v===d.aspect_ratio?"selected":""}>${v}</option>`).join("")}</select></label>${field("アイデア・あらすじ（日本語）","brief",d.brief,null,true,"wide")}${field("キャラクターの特徴（H3入力／英語）","subject",d.subject)}${field("画風（H3入力／英語）","style",d.style)}</div>
        <details><summary>世界観・全体の環境音を編集</summary><div class="form-grid">${field("世界観（英語）","world",d.world)}${field("環境音（英語）","soundscape",d.soundscape)}</div><p class="help">全体の設定を変えると、全カットのプロンプトが変わります。以前のテイクは履歴に残ります。</p></details>
        <div class="actions">${button("upload-character","＋ キャラクター参照","",!canEdit,"small")}${button("import","JSONからコマ割りを読み込む","",!canEdit,"small")}<a class="btn small" href="/api/projects/${p.id}/export" download="${p.id}.json">制作データを書き出す</a></div>
        ${d.character_reference ? (()=>{const a=p.assets.find((a)=>a.id===d.character_reference);return a?`<div class="asset-chip"><img src="${a.url}" alt="キャラクター参照"><span>${escapeHTML(a.name)} · 全カットで共有</span>${button("remove-character","解除","",!canEdit,"small")}</div>`:"";})():""}
        ${!state.config.llm_available?`<p class="help">AI未設定：手動編集・JSON読込が使えます。「生成環境の設定」からAIを接続できます。日本語メモは制作管理用です。映像・動きの欄を埋めて生成してください。</p>`:""}
      </section>
      <div class="section-header"><h2>コマ割り・タイムライン</h2><span class="help" id="duration-total">合計 ${cuts.reduce((s,c)=>s+c.seconds,0).toFixed(2)} / 30秒</span></div>
      <div class="timeline">${cuts.map((c,i)=>`<button data-action="scroll-cut" data-cut="${c.id}">${String(i+1).padStart(2,"0")}<small>${c.seconds}s</small></button>`).join("")}</div><div class="time-scale"><span>0:00</span><span>0:15</span><span>0:30</span></div>
      <p class="help">保存しただけでは映像は変わりません。本文と不一致のカットを生成し、使用テイクを選んで合成します。</p>
      ${cuts.map((c,i)=>{const html=cutCard(c,i,time);time+=c.seconds;return html;}).join("")}
      ${retiredTakes(p)}
    </div><div class="right-column">
      <section class="panel sticky-panel"><div class="panel-title"><h3>完成版プレビュー</h3>${latest?`<span class="help">r${latest.revision}</span>`:""}</div>
        ${latest ? `<video class="player" controls preload="metadata" src="${latest.url}" poster="${latest.cover_url}"></video><div class="render-meta"><strong>${latest.manifest.demo?"DEMO · 操作検証用動画":"30秒 · 24fps"}</strong><div class="help">${latest.manifest.takes.map((t,i)=>`${i+1}:r${t.revision}`).join(" / ")}</div></div>${p.master_current?badge("review","現在の構成と一致"):badge("stale","構成を更新済み・再合成が必要")}` : `<div class="player-placeholder">30秒の物語が、ここに。<br>全カットを生成して合成すると<br>完成版を再生できます。</div>`}
        <div class="notice ${state.provider==="demo"?"warning":""}">${state.provider==="demo"?"デモは色付きのテスト映像を作ります。AI映像の生成・GPUの利用はありません。":"H3サーバーでカットを順に生成します。新しいテイクの採用と完成版への反映は画面から行えます。"}</div>
        <label class="field">生成先<select id="provider" ${active()?"disabled":""}><option value="h3" ${state.provider==="h3"?"selected":""}>MiniMax H3</option><option value="demo" ${state.provider==="demo"?"selected":""}>デモ（GPUなし・操作検証）</option></select></label>
        <p class="help">一致テイク ${p.ready_count} / ${cuts.length}</p>
        <div class="actions">${button("generate-all","全カットを生成","",!canEdit,"primary full-width")}${button("generate-stale","不一致・未生成だけ生成","",!canEdit,"full-width")}${button("assemble","選択したテイクで合成","",!canEdit||p.ready_count!==cuts.length,"light full-width")}</div>
        <p class="help">合成は追加のAI生成を行いません。撮り直しでは既存の使用テイクと完成版を維持します。</p>
        ${latest?`<div class="actions"><a class="btn small" href="${latest.url}" download="${p.id}-r${latest.revision}.mp4">MP4を保存</a><a class="btn small" href="/media/renders/${latest.id}/manifest.json" download="manifest-r${latest.revision}.json">構成記録</a>${button(p.approved?"reopen":"approve",p.approved?"編集へ戻す":"完成版を承認","",active()||p.archived||(!p.approved&&(!p.master_current||latest.manifest.demo)),"small")}</div><details><summary>完成版の履歴 (${p.renders.length})</summary><div class="master-history">${p.renders.map((r)=>`<a href="${r.url}" download="${p.id}-r${r.revision}.mp4">↓ r${r.revision} · ${date(r.created)} ${r.manifest.demo?"· DEMO":""} · ${r.manifest.takes.map((t,i)=>`${i+1}:r${t.revision}`).join(" ")}</a>`).join("")}</div></details>`:""}
      </section>
      <section class="panel"><div class="panel-title"><h3>BGM・仕上げ</h3></div><p class="help">全カットをつなぎ、BGMを30秒通して重ねます。各カットの元音声はコマごとに消せます。</p>${button("upload-bgm","＋ BGMを追加","",!canEdit,"small")}${d.bgm?`<p class="help">${escapeHTML(p.assets.find((a)=>a.id===d.bgm)?.name)}</p><label class="field">BGM音量<input type="number" min="0" max="1" step="0.05" data-global="bgm_volume" value="${d.bgm_volume}" ${!canEdit?"disabled":""}></label>${button("remove-bgm","BGMを解除","",!canEdit,"small")}`:""}</section>
      <section class="panel"><div class="panel-title"><h3>処理履歴</h3><span class="help">直近20件</span></div><div id="queue">${p.jobs.length?p.jobs.map((j)=>`<div class="queue-item" data-job="${j.id}">${badge(j.status)} <strong>${escapeHTML({generate:"動画生成",assemble:"合成",plan:"AIコマ割り案",revise:"AI部分修正案",recover:"生成結果の回収"}[j.kind])}</strong><p data-job-stage>${escapeHTML(j.stage)}</p>${["queued","running"].includes(j.status)?`<progress data-job-progress max="100" value="${j.progress}"></progress>`:""}${j.error?`<p class="preformatted">${escapeHTML(j.error)}</p>`:""}${j.payload.proposal&&j.payload.version===p.version?button("proposal","提案を確認",`data-job="${j.id}"`,locked(),"small"):""}</div>`).join(""):`<p class="help">まだ処理はありません</p>`}</div>${proposals.length?`<div class="notice">AI提案が届いています。内容を確認してから反映してください。</div>`:""}</section>
    </div></div>`;
  $$(".timeline button").forEach((node,i)=>node.style.flex=String(cuts[i].seconds));
}
function retiredTakes(project) {
  const ids=new Set(project.document.cuts.map((cut)=>cut.id));
  const takes=project.takes.filter((take)=>!ids.has(take.cut_id));
  if(!takes.length)return "";
  return `<section class="panel"><details><summary>現在のコマ割りから外れたテイク (${takes.length})</summary><p class="help">JSON読込などでカットが変更された後も、過去の生成結果を保持しています。</p>${takes.map((take)=>`<div class="take-row"><div><strong>${escapeHTML(take.input.snapshot.title)} · r${take.revision}</strong>${badge(take.status)}<p>${escapeHTML(take.input.snapshot.description_ja)}</p><details><summary>生成時のプロンプト</summary><pre class="prompt">${escapeHTML(take.input.request.prompt)}</pre></details></div>${take.url?`<div class="actions">${button("play-take","再生",`data-take="${take.id}"`,false,"small")}<a class="btn small" href="${take.url}" download="${take.id}.mp4">MP4を保存</a></div>`:""}</div>`).join("")}</details></section>`;
}
function $$(selector) { return [...document.querySelectorAll(selector)]; }
function detailKey(node) {
  const cut=node.closest(".cut-card")?.id||state.project?.id||"library";
  const take=node.closest(".take-row")?.querySelector("[data-take]")?.dataset.take||"";
  return `${cut}:${take}:${node.querySelector("summary")?.textContent.replace(/\(\d+\)/g,"")}`;
}
function render() {
  const open=new Set($$("#app details[open]").map(detailKey));
  const players=$$("#app video").map((node)=>({node,src:node.getAttribute("src"),playing:!node.paused}));
  if(state.project)renderProject();else renderLibrary();
  $$("#app details").forEach((node)=>{if(open.has(detailKey(node)))node.open=true;});
  $$("#app video").forEach((node)=>{
    const old=players.find((player)=>player.src===node.getAttribute("src"));
    if(old){node.replaceWith(old.node);if(old.playing)old.node.play().catch(()=>{});}
  });
}

function markDirty() {
  state.dirty=true;
  if($("#dirty-label"))$("#dirty-label").hidden=false;
  if($("#duration-total"))$("#duration-total").textContent=`合計 ${state.project.document.cuts.reduce((s,c)=>s+c.seconds,0).toFixed(2)} / 30秒`;
  sessionStorage.setItem(`draft:${state.project.id}`,JSON.stringify({version:state.project.version,document:state.project.document}));
}
async function save() {
  while(state.dirty){
    const id=state.project.id;
    const sent=JSON.stringify(state.project.document);
    const project=await api(`/api/projects/${id}/save`,{document:JSON.parse(sent),version:state.project.version});
    if(state.project?.id!==id)return;
    const current=state.project.document;
    const edited=JSON.stringify(current)!==sent;
    state.project=project;
    if(edited){state.project.document=current;markDirty();}
    else{sessionStorage.removeItem(`draft:${id}`);state.dirty=false;render();}
  }
}

async function route() {
  closeModal();
  if(state.dirty){
    try{await save();}catch(e){toast(e.message);location.hash=`project/${state.project.id}`;return;}
  }
  const target=location.hash;
  const id=target.match(/^#project\/([a-f0-9]{32})$/)?.[1];
  if(id){
    const project=await api(`/api/projects/${id}`);
    if(location.hash!==target)return;
    const cached=sessionStorage.getItem(`draft:${id}`);
    state.project=project;state.dirty=false;
    if(cached&&!locked()){
      try{const draft=JSON.parse(cached);if(draft.version===project.version){state.project.document=draft.document;state.dirty=true;toast("未保存の編集内容を復元しました");}}catch{/* Invalid cache is ignored. */}
    }
  }else{
    const library=await api("/api/projects");
    if(location.hash!==target)return;
    state.project=null;state.library=library;
  }
  render();
}

async function refresh() {
  if(state.busy)return;
  const epoch=operationCounter;
  if(state.project&&!state.dirty){
    const id=state.project.id;
    const p=await api(`/api/projects/${id}`);
    if(state.dirty||state.busy||state.project?.id!==id||operationCounter!==epoch||p.version<state.project.version)return;
    const signature=(project)=>[project.version,project.status,project.jobs.map((j)=>[j.id,j.status,j.error,Boolean(j.payload.proposal)])];
    const changed=JSON.stringify(signature(p))!==JSON.stringify(signature(state.project));
    state.project=p;
    if(changed)render();
    else p.jobs.forEach((job)=>{
      const row=$(`#queue [data-job="${job.id}"]`);
      const stage=row?.querySelector("[data-job-stage]"),progress=row?.querySelector("[data-job-progress]");
      if(stage)stage.textContent=job.stage;
      if(progress)progress.value=job.progress;
    });
  }else if(!state.project){
    const library=await api("/api/projects");
    if(state.project||state.busy||operationCounter!==epoch)return;
    if(JSON.stringify(library)!==JSON.stringify(state.library)){
      const search=$("#search"),focused=document.activeElement===search,pos=search?.selectionStart;
      state.library=library;renderLibrary();
      if(focused){$("#search").focus();$("#search").setSelectionRange(pos,pos);}
    }
  }
}

async function generate(cutIds) {
  await save();
  const cuts=state.project.document.cuts.filter((c)=>cutIds.includes(c.id));
  state.pending={cut_ids:cutIds,provider:state.provider,version:state.project.version,auto_assemble:true};
  modal("生成内容の確認",`<p>${cuts.length}カット・計 ${cuts.reduce((s,c)=>s+c.seconds,0)}秒を${state.provider==="demo"?"デモ":"MiniMax H3"}で生成します。</p><div class="notice ${state.provider==="demo"?"warning":""}">${state.provider==="demo"?"操作検証用の色付き映像です。GPUを使用しません。":"設定したH3サーバーに生成を投入します。GPUの稼働時間が発生します。"}</div>${cuts.map((c)=>`<div class="proposal-row"><h3>${escapeHTML(c.title)} · ${c.seconds}s</h3><p>${escapeHTML(c.description_ja)}</p><details><summary>実投入プロンプト</summary><pre class="prompt">${escapeHTML(state.project.prompts[c.id])}</pre></details></div>`).join("")}<p class="help">最初の生成では完了テイクを自動選択し、全カットがそろうと合成します。撮り直したテイクは履歴から選択して反映します。</p>`,`${button("close-modal","戻る")}${button("confirm-generate","生成を開始","",false,"primary")}`);
}

async function enqueue(action, options={}) {
  const result=await api(endpoint(action),{version:state.project.version,...options});
  state.project=result.project;render();toast("処理を受け付けました");
}

function settings() {
  modal("生成環境の設定",`<p class="subtitle">設定はプロジェクトルートの .env.studio に保存し、Studioを再起動すると読み込まれます。</p>
    <h3>動画生成サーバー</h3><p class="help">テキスト生成（FL2VAサーバー）</p><pre class="prompt">H3_BASE_URL=${escapeHTML(state.config.h3_base_url)}</pre><p class="help">キャラクター・コマ画像の参照生成（Ref2VAサーバー）</p><pre class="prompt">H3_REF_BASE_URL=${escapeHTML(state.config.h3_ref_base_url)}</pre><p class="help">画像はデータとして送信します。別のGPUマシンでも使用できます。</p>
    <h3>AIコマ割り・部分修正</h3><p class="help">${state.config.llm_available?"接続設定あり":"現在は未設定"}。Chat Completions互換のテキストモデルを指定してください。</p><pre class="prompt">STUDIO_LLM_BASE_URL=http://127.0.0.1:8000/v1\nSTUDIO_LLM_MODEL=your-text-model\nSTUDIO_LLM_API_KEY=</pre><p class="help">AI提案の内容を確認して反映すると、H3用の英語本文も更新されます。APIキーは画面へ表示しません。詳しい起動方法は docs/studio.md を参照してください。</p>`,button("close-modal","閉じる"));
}

function sample() {
  modal("サンプル内容を入力",`<p>現在の本文を「小さなロボットが花を見つける物語」に置き換えます。5カットで試せます。生成はまだ実行しません。</p>`,`${button("close-modal","戻る")}${button("apply-sample","入力する","",false,"primary")}`);
}

function applySample() {
  const d=state.project.document;
  if(d.cuts.length!==5){toast("このサンプルは5カット構成用です。5カットのプロジェクトを作成してください");closeModal();return;}
  Object.assign(d,{brief:"小さなロボットが庭を歩き、しおれた花を見つけ、水をあげて、咲いた花を見守る。",subject:"A small rounded cream-colored robot with two blue circular eyes and short articulated arms.",style:"3D animation with soft matte materials.",world:"A quiet garden with a stone path and a flower pot. Warm morning light."});
  const scenes=[
    ["導入","庭を歩くロボット","A medium wide shot shows the small robot on a garden path beside a flower pot.","The robot walks slowly toward the pot, then stops beside it.","The robot stands beside the pot, with both hands empty."],
    ["発見","しおれた花に気づく","The robot stands beside the pot. A single yellow flower bends downward.","The robot leans forward and studies the wilted flower, tilting its head.","The robot is looking at the wilted flower, with both hands empty."],
    ["行動","そばのじょうろを持つ","The robot stands beside the flower pot; a small blue watering can rests on the ground.","The robot bends down, grips the watering can and lifts it carefully with both hands.","The robot holds the blue watering can with both hands beside the flower pot."],
    ["変化","花に水をあげる","The robot holds a blue watering can above the flower pot.","The robot tilts the can, pouring a gentle stream onto the soil, then brings it upright.","The robot holds the upright watering can and watches the flower."],
    ["結末","咲いた花を見守る","The robot holds an upright watering can beside the pot.","The yellow flower slowly straightens and opens its petals. The robot's blue eyes brighten as it watches.","The robot remains still beside the open flower, holding the watering can."],
  ];
  d.cuts.forEach((c,i)=>Object.assign(c,{title:scenes[i][0],description_ja:scenes[i][1],visual:scenes[i][2],action:scenes[i][3],end_state:scenes[i][4],camera:"The camera pushes in with small amplitude at slow speed.",sound:i===3?"A soft stream of water falls onto the soil.":"Subtle mechanical movement sounds accompany the robot.",dialogue:"",seconds:6}));
  closeModal();markDirty();render();
}

function upload(target, cutId=null) {
  state.upload={target,cutId,projectId:state.project.id};
  $("#asset-upload").accept=target==="bgm"?"audio/mpeg,audio/wav,.mp3,.wav":"image/png,image/jpeg,image/webp";
  $("#asset-upload").value="";$("#asset-upload").click();
}

function proposal(jobId) {
  if(state.dirty){toast("未保存の編集があります。保存すると現在のAI提案は無効になります。先に編集内容を保存してください");return;}
  const job=state.project.jobs.find((j)=>j.id===jobId);
  const next=job.payload.proposal, old=state.project.document;
  state.pending={jobId};
  const changed=next.cuts.map((c,i)=>({c,old:old.cuts[i]})).filter(({c,old})=>JSON.stringify(c)!==JSON.stringify(old));
  modal("AI提案を確認",`<p class="help">反映しても動画は生成されません。本文を反映後、変更したカットを生成できます。</p>${["subject","style","world","soundscape"].filter((key)=>next[key]!==old[key]).map((key)=>`<div class="proposal-row"><strong>${escapeHTML(key)}</strong><p class="muted">変更前：${escapeHTML(old[key])}</p><p>変更後：${escapeHTML(next[key])}</p></div>`).join("")}${changed.map(({c,old})=>`<div class="proposal-row"><h3>${escapeHTML(c.title)} · ${c.seconds}s</h3><p class="muted">変更前：${escapeHTML(old.description_ja)}</p><p>変更後：${escapeHTML(c.description_ja)}</p><details><summary>項目ごとの差分</summary>${Object.entries(fieldNames).filter(([key])=>old[key]!==c[key]).map(([key,label])=>`<strong>${label}</strong><p class="muted">前：${escapeHTML(old[key])}</p><p>後：${escapeHTML(c[key])}</p>`).join("")}</details></div>`).join("")}`,`${button("close-modal","閉じる")}${button("apply-proposal","本文に反映","",false,"primary")}`);
}

async function handleAction(action, node) {
  switch(action){
    case "new":newProject();break;
    case "close-modal":closeModal();break;
    case "settings":settings();break;
    case "create":{
      const project=await api("/api/projects",{title:$("#new-title").value,brief:$("#new-brief").value,layout:$("#new-layout").value,aspect_ratio:$("#new-aspect").value});
      closeModal();location.hash=`project/${project.id}`;break;
    }
    case "open":location.hash=`project/${node.dataset.id}`;break;
    case "filter":state.filter=node.dataset.filter;renderLibrary();break;
    case "save":await save();toast("保存しました。映像は生成時の内容のままです");break;
    case "scroll-cut":$("#cut-"+node.dataset.cut).scrollIntoView({behavior:"smooth"});break;
    case "sample":sample();break;
    case "apply-sample":applySample();break;
    case "plan":await save();await enqueue("plan");break;
    case "revise":{
      await save();state.pending={cutId:node.dataset.cut};
      modal("このコマのAI修正案",`<label class="field">どのように直しますか？<textarea id="revise-instruction" placeholder="例：花を片手で持つ動きに変更。表情は驚いた顔に。" maxlength="2000"></textarea></label><p class="help">このカットの本文だけを改稿します。提案を確認してから反映してください。</p>`,`${button("close-modal","戻る")}${button("confirm-revise","修正案を作成","",false,"primary")}`);break;
    }
    case "confirm-revise":{const id=state.pending.cutId;const instruction=$("#revise-instruction").value;await enqueue("revise",{cut_id:id,instruction});closeModal();break;}
    case "proposal":proposal(node.dataset.job);break;
    case "apply-proposal":{
      if(state.dirty)throw new Error("未保存の編集を保存してから、AI提案を作り直してください");
      const p=await api(endpoint("apply-proposal"),{job_id:state.pending.jobId,version:state.project.version});state.project=p;state.dirty=false;closeModal();render();toast("提案を本文に反映しました。必要なカットを再生成してください");break;
    }
    case "generate-all":await generate(state.project.document.cuts.map((c)=>c.id));break;
    case "generate-cut":await generate([node.dataset.cut]);break;
    case "generate-stale":{
      await save();const ids=state.project.document.cuts.filter((c)=>!matchingTake(c)).map((c)=>c.id);if(!ids.length)toast("一致する完了テイクがあります。テイク履歴から「合成に使う」で選択してください");else await generate(ids);break;
    }
    case "confirm-generate":{const options=state.pending;await enqueue("generate",options);closeModal();break;}
    case "assemble":await save();await enqueue("assemble");break;
    case "choose":await save();state.project=await api(endpoint("choose"),{take_id:node.dataset.take});render();toast("使用テイクを選びました。合成すると完成版に反映されます");break;
    case "recover":await save();await enqueue("recover",{take_id:node.dataset.take});break;
    case "play-take":{
      const take=state.project.takes.find((t)=>t.id===node.dataset.take);modal(`テイク r${take.revision}${take.provider==="demo"?" · DEMO":""}`,`<video controls autoplay src="${take.url}" poster="${take.thumb_url}"></video><p class="help">${escapeHTML(take.input.snapshot.description_ja)}</p>`);break;
    }
    case "show-prompt":await save();modal("現在の本文から組み立てたH3プロンプト",`<pre class="prompt">${escapeHTML(state.project.prompts[node.dataset.cut])}</pre>`,`${button("close-modal","閉じる")}${button("copy-prompt","コピー",`data-cut="${node.dataset.cut}"`,false,"primary")}`);break;
    case "copy-prompt":await navigator.clipboard.writeText(state.project.prompts[node.dataset.cut]);toast("コピーしました");break;
    case "upload-character":upload("character");break;
    case "upload-reference":upload("reference",node.dataset.cut);break;
    case "upload-bgm":upload("bgm");break;
    case "remove-reference":state.project.document.cuts.find((c)=>c.id===node.dataset.cut).references=state.project.document.cuts.find((c)=>c.id===node.dataset.cut).references.filter((id)=>id!==node.dataset.asset);markDirty();render();break;
    case "remove-character":state.project.document.character_reference=null;markDirty();render();break;
    case "remove-bgm":state.project.document.bgm=null;markDirty();render();break;
    case "import":$("#json-upload").value="";$("#json-upload").click();break;
    case "archive":await save();modal("プロジェクトをアーカイブ",`<p>ライブラリの通常表示から外します。素材と履歴は保持され、アーカイブから復元できます。</p>`,`${button("close-modal","戻る")}${button("confirm-archive","アーカイブ","",false,"primary")}`);break;
    case "confirm-archive":state.project=await api(endpoint("archive"),{});closeModal();render();break;
    case "restore":case "approve":case "reopen":await save();state.project=await api(endpoint(action),{});render();break;
  }
}

document.addEventListener("click",(event)=>{
  const node=event.target.closest("[data-action]");if(!node||node.disabled||state.busy)return;
  runOperation(()=>handleAction(node.dataset.action,node));
});
$("#new-link").addEventListener("click",()=>runOperation(newProject));
$("#settings-link").addEventListener("click",()=>runOperation(settings));
document.addEventListener("input",(event)=>{
  const node=event.target;
  if(node.id==="search"){state.query=node.value;const pos=node.selectionStart;renderLibrary();$("#search").focus();$("#search").setSelectionRange(pos,pos);return;}
  if(!state.project||locked())return;
  let value=node.type==="checkbox"?node.checked:node.type==="number"?Number(node.value):node.value;
  if(node.dataset.global){state.project.document[node.dataset.global]=value;markDirty();}
  if(node.dataset.cut&&node.dataset.field){const cut=state.project.document.cuts.find((c)=>c.id===node.dataset.cut);cut[node.dataset.field]=value;markDirty();}
});
document.addEventListener("change",(event)=>{
  if(event.target.id==="provider"){state.provider=event.target.value;render();}
});
$("#asset-upload").addEventListener("change",(event)=>runOperation(async()=>{
  const file=event.target.files[0];if(!file)return;
  try{
    if(state.project?.id!==state.upload?.projectId)throw new Error("素材を追加するプロジェクトを開き直してください");
    if(file.size>15*1024*1024)throw new Error("素材は15MBまでです");
    await save();
    const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(",")[1]);reader.onerror=()=>reject(new Error("素材を読み込めません"));reader.readAsDataURL(file);});
    const mime=file.type==="audio/x-wav"?"audio/wav":file.type||(/\.wav$/i.test(file.name)?"audio/wav":/\.mp3$/i.test(file.name)?"audio/mpeg":"");
    const asset=await api(endpoint("upload"),{name:file.name,mime,content:data});
    state.project.assets.push(asset);
    if(state.upload.target==="character")state.project.document.character_reference=asset.id;
    else if(state.upload.target==="bgm")state.project.document.bgm=asset.id;
    else state.project.document.cuts.find((c)=>c.id===state.upload.cutId).references.push(asset.id);
    markDirty();await save();toast("素材を保存しました");
  }catch(error){toast(error.message);}
}));
$("#json-upload").addEventListener("change",(event)=>runOperation(async()=>{
  const file=event.target.files[0];if(!file)return;
  try{
    if(file.size>1024*1024)throw new Error("コマ割りJSONは1MBまでです");
    await save();const value=JSON.parse(await file.text());
    const doc=value.project?.document||value.document||value;
    state.pending={importDoc:doc};
    modal("コマ割りJSONを読み込む",`<p>現在の本文を「${escapeHTML(doc.title)}」に置き換えます。カット${Array.isArray(doc.cuts)?doc.cuts.length:"?"}件。既存のテイクと完成版は履歴に残ります。</p><p class="help">参照素材IDは、このプロジェクトに保存済みのIDを指定してください。</p>`,`${button("close-modal","戻る")}<button class="btn primary" id="confirm-import">読み込む</button>`);
    $("#confirm-import").addEventListener("click",()=>runOperation(async()=>{state.project=await api(endpoint("import"),{document:state.pending.importDoc,version:state.project.version});state.dirty=false;closeModal();render();toast("コマ割りを読み込みました");}));
  }catch(error){toast(error.message);}
}));
window.addEventListener("hashchange",()=>runOperation(route));
window.addEventListener("beforeunload",(event)=>{if(state.dirty){event.preventDefault();event.returnValue="";}});
$("#modal").addEventListener("click",(event)=>{if(!state.busy&&event.target===$("#modal"))closeModal();});
$("#modal").addEventListener("cancel",(event)=>{if(state.busy)event.preventDefault();else state.pending=null;});

(async()=>{
  try{state.config=await api("/api/config");await runOperation(route);setInterval(()=>refresh().catch(()=>{}),2500);}catch(error){$("#app").textContent=error.message;}
})();
