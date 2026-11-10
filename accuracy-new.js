/* UI glue: both variants are frozen inside ONE existing append-only archive record. */
let PREDICTION_DATA_OBSERVED_AT=Date.now();
let predictionEvidenceLive=null;
let predictionInitialLoadComplete=!!window.__PREDICTION_ARCHIVE_RUNNER__;
let predictionScheduleReady=!!window.__PREDICTION_ARCHIVE_RUNNER__;
async function loadPredictionEvidence(){
  try{
    if(WEB_CONNECTIONS_PAUSED){
      predictionEvidenceLive=PredictionEvidence.validateFeed(savedDataCopy('evidence'));
    }else{
      const response=await fetch('prediction-evidence-live.json',{cache:'no-store'});
      if(!response.ok)throw new Error('HTTP '+response.status);
      predictionEvidenceLive=PredictionEvidence.validateFeed(await response.json());
    }
    clearPred3Cache();renderPred2();renderAccuracyNew();
  }catch(error){
    predictionEvidenceLive=null;
    console.warn('Accuracy New: evidence unavailable; numerical context disabled.',error);
    clearPred3Cache();renderPred2();renderAccuracyNew();
  }
}
function buildContextVariant(base,match,cutoff){
  const fixture=predictionFixture(match),kickoff=PredictionArchive.kickoffMs(fixture);
  const evidence=PredictionEvidence.context(predictionEvidenceLive,match.home,match.away,kickoff,cutoff);
  const lh=base.scoreModel.lambdaHome*evidence.home.multiplier,la=base.scoreModel.lambdaAway*evidence.away.multiplier;
  const matrix=p3ScoreMatrix(lh,la),p=pred3CalcFromMatrix(matrix);
  return {...p,hP:p.p1,dP:p.px,aP:p.p2,predOutcome:p.mainTip,
    topScore:`${matrix.top[0].h}:${matrix.top[0].a}`,modelVersion:PredictionEvidence.VERSION,
    cutoff,kickoffAt:kickoff===null?null:new Date(kickoff).toISOString(),
    scoreModel:{lambdaHome:lh,lambdaAway:la,top:matrix.top},evidence};
}
function buildContextPreviewHTML(p){
  const v=p.contextVariant;if(!v)return '';
  const gap=v.evidence.home.coverage!=='verified'||v.evidence.away.coverage!=='verified';
  return `<div style="font-size:11px;padding:8px;margin-bottom:10px;background:var(--bg2);border-left:3px solid var(--amber)">
    <strong>Accuracy New${p._locked?' · zamčeno':' · náhled'}</strong> · 1 ${pred2Pct(v.hP)} / X ${pred2Pct(v.dP)} / 2 ${pred2Pct(v.aP)}
    <br>${gap?'Neúplné nebo zastaralé podklady: nedoložená korekce je nulová.':'Ověřené vytížení: domácí '+((v.evidence.home.multiplier-1)*100).toFixed(0)+' %, hosté '+((v.evidence.away.multiplier-1)*100).toFixed(0)+' % očekávaných gólů.'}
    Při uložení se současně zamknou obě varianty. Zprávy nejsou automatický bonus.</div>`;
}
function renderAccuracyNew(){
  const list=document.getElementById('an-list'),summary=document.getElementById('an-summary');
  if(!list||!summary)return;
  const locks=lockedPredictions.filter(isActivePredictionLock),pairs=locks.filter(l=>l.snapshot?.calculation?.contextVariant);
  const round=document.getElementById('an-round'),status=document.getElementById('an-status'),selected=round.value;
  round.innerHTML='<option value="">Všechna</option>'+[...new Set(pairs.map(l=>String(l.round)))].sort((a,b)=>Number(a)-Number(b)).map(r=>`<option value="${lsEsc(r)}"${r===selected?' selected':''}>${lsEsc(r)}</option>`).join('');
  const rows=pairs.filter(l=>!round.value||String(l.round)===round.value).map(l=>{
    const v=l.snapshot.calculation.contextVariant,r=findResultForPred(l),eligible=predictionEvaluationStatus(l);
    return {l,v,r,eligible,b:PredictionEvidence.metrics(l,r?.score),n:PredictionEvidence.metrics(v,r?.score)};
  }).filter(x=>!status.value||(status.value==='excluded'?!x.eligible.eligible:status.value==='settled'?x.eligible.eligible&&x.b:x.eligible.eligible&&!x.b));
  const settled=rows.filter(x=>x.eligible.eligible&&x.b),n=settled.length;
  const mean=(key,metric)=>n?(settled.reduce((s,x)=>s+Number(x[key][metric]),0)/n):null;
  const metric=(title,key,percent=false)=>{
    const base=mean('b',key),next=mean('n',key),fmt=v=>v===null?'—':percent?(100*v).toFixed(1)+' %':v.toFixed(3);
    return `<tr><th style="text-align:left">${title}</th><td>${fmt(base)}</td><td>${fmt(next)}</td><td>${base===null?'—':((next-base)*(percent?100:1)).toFixed(percent?1:3)+(percent?' p. b.':'')}</td></tr>`;
  };
  summary.innerHTML=`<strong>${n} vyhodnocených párů</strong> · ${rows.filter(x=>x.eligible.eligible&&!x.b).length} čeká · ${rows.filter(x=>!x.eligible.eligible).length} vyloučeno
    <div style="overflow-x:auto"><table style="width:100%;min-width:410px;margin:10px 0;font-size:12px"><thead><tr><th></th><th>Základ</th><th>New</th><th>Rozdíl New − základ</th></tr></thead><tbody>
    ${metric('Brier ↓','brier')}${metric('Log loss ↓','logLoss')}${metric('Správný tip 1X / X2','correct',true)}${metric('Správný výsledek 1 / X / 2','hardCorrect',true)}${metric('Přesné skóre','exact',true)}</tbody></table></div>
    <div style="font-size:11px;color:var(--text3)">Nižší Brier a log loss jsou lepší. ${locks.length-pairs.length} starších záznamů nemá dobový kontextový snapshot a není zpětně přepočítáváno. ${n<30?'Zatím malý vzorek; z rozdílu nelze vyvozovat spolehlivé zlepšení.':''}</div>`;
  const link=(url,label)=>/^https?:\/\//i.test(url||'')?`<a href="${lsEsc(url)}" target="_blank" rel="noopener noreferrer">${lsEsc(label)}</a>`:lsEsc(label);
  const date=v=>v?new Date(v).toLocaleString('cs-CZ',{timeZone:'Europe/Prague'}):'neznámé';
  list.innerHTML=rows.length?rows.map(({l,v,r,eligible,b,n})=>{
    const facts=v.evidence,inputs=l.snapshot.inputs||{},last5=l.snapshot.calculation.modelMeta?.lastFive||{};
    const teamDetails=(side,team)=>{
      const e=facts[side],xi=inputs[side+'XI']||{};
      const games=(last5[side]||[]).map(m=>`<li>${lsEsc(date(m.playedAt))}: ${link(m.sourceUrl,`${m.home} – ${m.away} ${m.score}`)} (kolo ${lsEsc(m.round)})</li>`).join('');
      const workload=e.matches.map(m=>`<li>${lsEsc(date(m.kickoffAt))}: ${link(m.sourceUrl,`${m.home} – ${m.away}`)} · ${lsEsc(m.competition)} · ${m.status==='scheduled'?'plánováno, výsledek ještě nebyl znám':lsEsc(m.score||'odehráno')} · ověřeno ${lsEsc(date(m.fetchedAt))}</li>`).join('');
      const news=e.news.map(a=>`<li>${link(a.sourceUrl,a.title)} · vydáno ${lsEsc(date(a.publishedAt))}, načteno ${lsEsc(date(a.fetchedAt))} · vliv 0</li>`).join('');
      const names=rows=>(rows||[]).map(x=>typeof x==='string'?x:x.name||x.playerName||x.reason||'?').map(lsEsc).join(', ')||'žádní doložení v načteném zdroji (nikoli potvrzení plného zdraví)';
      return `<h4>${lsEsc(team)}</h4><p>Vytížení: ${e.coverage==='verified'?'ověřené zdroje':'neúplné / zastaralé, korekce 0'} · ${((e.multiplier-1)*100).toFixed(0)} % λ · ${e.restHours===null?'odpočinek neznámý':e.restHours+' h od konce posledního utkání'}${e.projected?' (část programu je zatím plánovaná)':''}</p>
        <strong>Posledních ${last5[side]?.length||0} doložených ligových utkání</strong><ol>${games||'<li>Chybí datovaná minulá utkání; nepoužívá se náhrada podle kola.</li>'}</ol>
        <strong>Zátěž ve všech soutěžích</strong><ul>${workload||'<li>Žádná dostupná položka; bez automatického bonusu.</li>'}</ul>
        <p>Potvrzené absence v podkladu XI: ${names(xi.missing)}<br>Nejisté: ${names(xi.uncertain)}<br>${link(inputs.sourceUrl,'Zdroj předpokládané sestavy')} · ${lsEsc(xi.source||'proxy')} · kontrola zdroje ${lsEsc(date(xi.sourceCheckedAt))}</p>
        <strong>Klubové zprávy: pouze informace, žádná automatická interpretace</strong><ul>${news||'<li>Žádné datované články dostupné v tomto snapshotu; neznamená to, že neexistují.</li>'}</ul>
        <p>Pokrytí zpráv: ${lsEsc(e.checks?.news?.status||'nedostupné')}. Sociální sítě: jen veřejné odkazy, nikoli kompletní prohledání. ${(e.social||[]).map(u=>link(u,new URL(u).hostname)).join(' · ')}</p>`;
    };
    return `<article class="pred-card" style="padding:14px;margin-bottom:12px"><h3 style="font-size:15px;margin:0 0 8px">${lsEsc(l.home)} – ${lsEsc(l.away)} <small>R${lsEsc(l.round)} · ${r?lsEsc(r.score):'čeká na výsledek'}</small></h3>
      <div style="overflow-x:auto"><table style="min-width:470px;width:100%;font-size:12px"><tr><th></th><th>1</th><th>X</th><th>2</th><th>Tip / skóre</th><th>Brier / log loss</th></tr>
      ${[[l,'Základ',b],[v,'New',n]].map(([p,label,e])=>`<tr><th>${label}</th><td>${pred2Pct(p.hP)}</td><td>${pred2Pct(p.dP)}</td><td>${pred2Pct(p.aP)}</td><td>${lsEsc(p.predOutcome)} / ${lsEsc(p.topScore)}</td><td>${e&&eligible.eligible?e.brier.toFixed(3)+' / '+e.logLoss.toFixed(3):'—'}</td></tr>`).join('')}</table></div>
      <p style="font-size:11px;color:var(--text3)">🔒 ${lsEsc(date(v.cutoff))} · ${lsEsc(v.modelVersion)}${eligible.eligible?'':' · vyloučeno: '+lsEsc(eligible.reason)}</p>
      <details><summary style="cursor:pointer">Použitá forma, zdroje a přesný dopad</summary><div style="font-size:12px;line-height:1.6">${teamDetails('home',l.home)}${teamDetails('away',l.away)}<p>Evropské výsledky nejsou samy o sobě bonus za „morálku“. Korekce zátěže je experimentální, nejvýše −5 % vlastních očekávaných gólů; absence se neodečítají dvakrát. Pozdější informace tento snapshot nemění.</p></div></details></article>`;
  }).join(''):'<p style="padding:20px;color:var(--text3)">Zatím žádné nové zamčené páry pro tento filtr. Objeví se při příštím ručním uložení nebo automatickém pátečním běhu. Historické predikce zůstávají v Accuracy beze změn.</p>';
}
