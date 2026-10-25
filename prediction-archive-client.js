/* Browser persistence and authenticated submission. No credentials are stored. */
const predictionArchiveState={data:null,bySignature:new Map(),byFixture:new Map(),error:'',loading:false,saving:false,lastChecked:null,token:null};

function readPredictionJournal(){
  const text=localStorage.getItem(PredictionArchive.JOURNAL);
  if(!text)return {locks:[],matches:[]};
  const value=JSON.parse(decompressStorageText(text));
  if(value.version!==1||!Array.isArray(value.locks)||!Array.isArray(value.matches))throw new Error('Poškozený lokální archiv predikcí — původní kopie nebyla přepsána.');
  value.locks.forEach(PredictionArchive.validateLock);
  return value;
}

function persistPredictionJournal(data){
  const prior=readPredictionJournal();
  const incoming=(data?.lockedPredictions||lockedPredictions).filter(isActivePredictionLock).map(normalizeLockedPrediction);
  for(const lock of incoming)if(!lockedPredictionIntegrityOK(lock))throw new Error('Porušená uzamčená predikce; archiv nebyl přepsán.');
  const locks=PredictionArchive.mergeLocks(prior.locks,incoming);
  const matches=mergeStoredRows(prior.matches,data?.predMatches||predMatches,m=>healthPredictionKey(m,false));
  const value=encodeStorageSnapshot(JSON.stringify({version:1,locks,matches}));
  if(localStorage.getItem(PredictionArchive.JOURNAL)!==value)localStorage.setItem(PredictionArchive.JOURNAL,value);
  // Never free space by deleting this journal. A failed setItem keeps its old value.
  return {locks,matches};
}

function archiveLockSignature(lock){
  const copy=normalizeLockedPrediction(lock);
  return PredictionArchive.signature(copy);
}

function archivedLockRecord(lock){
  const signature=archiveLockSignature(lock);
  return predictionArchiveState.bySignature.get(signature)||null;
}

function predictionEvaluationStatus(lock){
  const record=archivedLockRecord(lock);
  if(record)return record.evaluation;
  const fixture=predictionFixture(lock);
  return PredictionArchive.eligibility(lock,fixture);
}

function canonicalArchivedLock(lock){
  const fixture=predictionFixture(lock);
  return fixture?predictionArchiveState.byFixture.get(String(fixture.officialMatchId))||null:null;
}

function mergePublishedPredictionArchive(payload){
  PredictionArchive.validateArchive(payload);
  // Reject a stale/deleted server history; never adopt a shorter replacement.
  if(predictionArchiveState.data){
    const newer=new Map(payload.records.map(r=>[r.recordId,r]));
    for(const old of predictionArchiveState.data.records){
      if(!newer.has(old.recordId)||PredictionArchive.stable(newer.get(old.recordId))!==PredictionArchive.stable(old))
        throw new Error('Serverový archiv ztratil nebo změnil dřívější záznam. Obnova zastavena.');
    }
  }
  const rows=PredictionArchive.primaryRecords(payload);
  const restored=rows.map(record=>{
    const lock=normalizeLockedPrediction(record.lock);
    if(!lockedPredictionIntegrityOK(lock))throw new Error('Kontrola integrity archivu selhala: '+lock.home+' – '+lock.away);
    return lock;
  });
  const journal=readPredictionJournal();
  const all=PredictionArchive.mergeLocks(journal.locks,lockedPredictions,restored);
  // Persist before adopting the download so a quota failure cannot erase history.
  // If the large shared storage is full, the server copy remains authoritative.
  predictionArchiveState.data=payload;
  predictionArchiveState.bySignature=new Map(payload.records.map(r=>[archiveLockSignature(r.lock),r]));
  predictionArchiveState.byFixture=new Map(rows.map(r=>[r.fixtureId,normalizeLockedPrediction(r.lock)]));
  lockedPredictions=all;
  const archivedMatches=rows.map(({lock,fixtureId})=>{
    const fixture=predictionFixture({...lock,officialMatchId:fixtureId});
    return {home:lock.home,away:lock.away,season:lock.season,round:lock.round,
      officialMatchId:fixtureId,date:fixture?.date||null,kickoff:fixture?.time||null,
      hDisadv:0,aDisadv:0,hXI:[],aXI:[]};
  });
  // Existing editable drafts win; missing historical rows are added for viewing.
  predMatches=mergeStoredRows(archivedMatches,[...journal.matches,...predMatches],m=>healthPredictionKey(m,false));
  dedupeRuntimeData();
  clearPred3Cache();
  try{persistPredictionJournal();}catch(error){predictionArchiveState.error='Archiv je na GitHubu; lokální kopii se nepodařilo uložit: '+error.message;}
  rebuildPredRoundSelects();renderPred();renderPred2();renderAccuracy();
}

function pendingArchiveLocks(){
  let journal;
  try{journal=readPredictionJournal().locks;}catch{journal=[];}
  return PredictionArchive.mergeLocks(journal,lockedPredictions).filter(lock=>!archivedLockRecord(lock));
}

function renderPredictionArchiveStatus(){
  const el=document.getElementById('prediction-archive-status');
  if(!el)return;
  const state=predictionArchiveState;
  let pending=[];
  try{pending=pendingArchiveLocks();}catch(error){state.error=error.message;}
  const count=state.data?PredictionArchive.primaryRecords(state.data).length:0;
  const bad=state.error||pending.length;
  el.style.borderLeft='3px solid var('+(bad?'--red':'--green')+')';
  el.innerHTML=`<strong>${state.error?'Archiv · chyba':state.loading?'Archiv · načítání':state.saving?'Archiv · čeká na potvrzení GitHubu':`GitHub archiv · ${count} zápasů`}</strong>`
    +`<span style="margin-left:10px">${state.error?lsEsc(state.error):pending.length?`${pending.length} lokálních predikcí ještě není potvrzeno na GitHubu.`:'Historie se obnovuje z GitHubu; automatické predikce vznikají na serveru před výkopem.'}</span>`
    +` <button class="btn" onclick="loadPredictionArchive(true)">Obnovit archiv</button>`
    +(pending.length?` <button class="btn" onclick="openPredictionArchiveSync()">Zálohovat lokální predikce</button>`:'');
}

async function loadPredictionArchive(fresh=false){
  if(predictionArchiveState.loading)return;
  predictionArchiveState.loading=true;
  renderPredictionArchiveStatus();
  try{
    // Read directly on production: bot commits need not rebuild GitHub Pages.
    const url=(fresh||location.hostname==='kordic99.github.io')?'https://raw.githubusercontent.com/Kordic99/Predictions/main/'+PredictionArchive.FILE:new URL(PredictionArchive.FILE,location.href).href;
    const response=await fetch(url+'?v='+Date.now(),{cache:'no-store',signal:AbortSignal.timeout(20000)});
    if(!response.ok)throw new Error('Archiv není dostupný (HTTP '+response.status+'). Lokální historie zůstává zachována.');
    predictionArchiveState.error='';
    mergePublishedPredictionArchive(await response.json());
    predictionArchiveState.lastChecked=new Date().toISOString();
  }catch(error){predictionArchiveState.error=error.message;}
  finally{predictionArchiveState.loading=false;renderPredictionArchiveStatus();}
}

function openPredictionArchiveSync(){
  let dialog=document.getElementById('prediction-archive-dialog');
  if(!dialog){
    dialog=document.createElement('dialog');dialog.id='prediction-archive-dialog';
    dialog.style.cssText='max-width:560px;border:1px solid #777;border-radius:12px;padding:24px';
    dialog.innerHTML='<h3>Záloha lokálních predikcí na GitHub</h3>'
      +'<p>Automatické predikce se archivují bez přihlášení. Pro odeslání vlastních lokálních predikcí použij token omezený na Kordic99/Predictions s oprávněním <b>Actions: write</b>. Token zůstane jen v paměti této stránky, nikdy v exportu ani úložišti.</p>'
      +'<p>Záznam přijatý až po výkopu se zachová, ale nebude započítán do hodnocení. Po odeslání počkáme na skutečné potvrzení v archivu.</p>'
      +'<label>GitHub token <input id="prediction-archive-token" type="password" autocomplete="off" spellcheck="false" style="width:100%"></label>'
      +'<p id="prediction-archive-sync-message" role="status"></p>'
      +'<button type="button" onclick="submitPredictionArchive()">Odeslat a zálohovat</button> '
      +'<button type="button" onclick="closePredictionArchiveSync()">Zavřít a zapomenout token</button>';
    document.body.append(dialog);
    dialog.addEventListener('close',()=>{predictionArchiveState.token=null;document.getElementById('prediction-archive-token').value='';});
  }
  dialog.showModal();
}

function closePredictionArchiveSync(){document.getElementById('prediction-archive-dialog')?.close();}

async function submitPredictionArchive(){
  const state=predictionArchiveState;
  if(state.saving)return;
  const message=document.getElementById('prediction-archive-sync-message');
  const field=document.getElementById('prediction-archive-token');
  state.token=field.value.trim();field.value='';
  if(!state.token){message.textContent='Chybí token.';return;}
  state.saving=true;state.error='';renderPredictionArchiveStatus();
  try{
    const locks=pendingArchiveLocks();
    // A separate small dispatch per snapshot avoids the GitHub input-size limit.
    for(const lock of locks){
      const submission=JSON.stringify({locks:[lock]});
      if(new TextEncoder().encode(submission).length>55000)throw new Error('Predikce je příliš velká pro odeslání; použij JSON export a správce archivu.');
      const response=await fetch('https://api.github.com/repos/Kordic99/Predictions/actions/workflows/archive-predictions.yml/dispatches',{
        method:'POST',headers:{Accept:'application/vnd.github+json',Authorization:'Bearer '+state.token,'Content-Type':'application/json'},
        body:JSON.stringify({ref:'main',inputs:{submission}}),signal:AbortSignal.timeout(30000)
      });
      if(!response.ok)throw new Error('GitHub odmítl odeslání (HTTP '+response.status+'). Zkontroluj oprávnění tokenu.');
    }
    state.token=null;
    message.textContent='Odesláno do GitHub Actions. Zatím NEPOTVRZENO; ověřuji dokončení…';
    const deadline=Date.now()+10*60*1000;
    while(Date.now()<deadline){
      await new Promise(resolve=>setTimeout(resolve,15000));
      await loadPredictionArchive(true);
      if(locks.every(lock=>archivedLockRecord(lock))){message.textContent='Potvrzeno: všechny odeslané predikce jsou v trvalém archivu.';return;}
    }
    throw new Error('GitHub zatím uložení nepotvrdil. Lokální kopie zůstává; zkontroluj workflow Archive predictions.');
  }catch(error){state.error=error.message;message.textContent=error.message;}
  finally{state.token=null;state.saving=false;renderPredictionArchiveStatus();}
}

function startPredictionArchive(){
  void loadPredictionArchive();
  setInterval(()=>loadPredictionArchive(),10*60*1000);
  window.addEventListener('online',()=>loadPredictionArchive());
  window.addEventListener('storage',event=>{
    if(event.key===PredictionArchive.JOURNAL&&event.newValue){
      try{
        const journal=readPredictionJournal();
        const received=JSON.parse(decompressStorageText(event.newValue));
        lockedPredictions=PredictionArchive.mergeLocks(lockedPredictions,journal.locks,received.locks||[]);
        predMatches=mergeStoredRows(journal.matches,predMatches,m=>healthPredictionKey(m,false));
        persistPredictionJournal();
        dedupeRuntimeData();renderPred2();renderAccuracy();
      }catch(error){predictionArchiveState.error=error.message;renderPredictionArchiveStatus();}
    }
  });
}
